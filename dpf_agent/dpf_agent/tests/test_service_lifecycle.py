"""任务进度、早期失败与报告阶段串行化的离线行为回归。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

from ..application import service
from ..config import AgentConfig
from ..engines.opendss_adapter import EngineHealth
from ..schemas.task_spec import UserRequest
from ..web import api
from ..web.schemas import RunRequest


class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="service-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.config = AgentConfig(
            llm_enabled=False, engine_backend="dss-python",
            workspace_root=os.path.join(self.tmp.name, "tasks"),
            outputs_root=os.path.join(self.tmp.name, "outputs"),
        )
        self.app = service.ApplicationService(self.config)
        self.request = UserRequest(message="计算 case4", case_ref="case4")
        self.adapter_patch = patch.object(service, "OpenDSSAdapter", return_value=SimpleNamespace())
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)
        self.real_probe = service.probe_engine_health
        self.health_patch = patch.object(
            service, "probe_engine_health",
            return_value=EngineHealth(available=False, detail="test engine unavailable"),
        )
        self.health_mock = self.health_patch.start()
        self.addCleanup(self.health_patch.stop)
        self.app._report_engine_failure = lambda *args: None

    def progress(self) -> dict:
        with open(os.path.join(self.config.workspace_root, "_current_progress.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_busy_request_preserves_active_progress(self) -> None:
        service._TASK_LOCK.acquire()
        service._TASK_LOCK_OWNER.update(task_id="running-task", since=1)
        try:
            self.app._write_progress("running-task", "solve", "running", 60)
            before = self.progress()
            with self.assertRaises(service.TaskBusyError):
                self.app.run(self.request, cli_callback=None)
            self.assertEqual(self.progress(), before)
            rejected = [n for n in os.listdir(self.config.workspace_root) if n.startswith("pf-")]
            self.assertEqual(len(rejected), 1)
            self.assertTrue(os.path.isfile(os.path.join(self.config.workspace_root, rejected[0], "state.json")))
        finally:
            service._TASK_LOCK_OWNER.update(task_id=None, since=None)
            service._TASK_LOCK.release()

    def test_engine_failure_persists_reason_and_terminal_status(self) -> None:
        response = self.app.run(self.request, cli_callback=None)
        self.assertEqual(response["status"], "system_failed")
        self.assertEqual(response["error"]["message"], "test engine unavailable")
        self.assertEqual(response["error"]["stage"], "engine_health")
        with open(response["artifacts"]["state.json"], encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["status"], response["status"])
        self.assertEqual(state["error"], response["error"])
        self.assertEqual(state["phase"], "FAILED")
        self.assertFalse(service.current_task_lock()["locked"])
        self.assertTrue(self.progress()["done"])

    def test_task_started_callback_runs_once_with_lock_and_before_engine(self) -> None:
        seen = []
        def started(task_id):
            self.assertTrue(service.current_task_lock()["locked"])
            self.assertEqual(service.current_task_lock()["task_id"], task_id)
            self.assertEqual(self.health_mock.call_count, 0)
            seen.append(task_id)
        result = self.app.run(self.request, cli_callback=None, on_task_started=started)
        self.assertEqual(seen, [result["task_id"]])

    def test_task_started_callback_failure_releases_lock_and_does_not_solve(self) -> None:
        def started(task_id):
            raise OSError("cannot persist batch task identity")
        with self.assertRaisesRegex(OSError, "cannot persist batch task identity"):
            self.app.run(self.request, cli_callback=None, on_task_started=started)
        self.assertFalse(service.current_task_lock()["locked"])
        self.health_mock.assert_not_called()

    def test_busy_request_does_not_register_started_task(self) -> None:
        seen = []
        service._TASK_LOCK.acquire()
        try:
            with self.assertRaises(service.TaskBusyError):
                self.app.run(self.request, cli_callback=None, on_task_started=seen.append)
        finally:
            service._TASK_LOCK.release()
        self.assertEqual(seen, [])

    def test_isolated_health_is_task_local_even_after_previous_failure(self) -> None:
        # Call the unpatched function, independent of the legacy native probe.
        adapter = SimpleNamespace(process_isolated=True, health=lambda: EngineHealth(True, engine_version="private"))
        with patch.dict(service._engine_health_state, checked=True, available=False, detail="old failed worker"):
            result = self.real_probe(adapter)
        self.assertTrue(result.available)
        self.assertEqual(result.engine_version, "private")

    def test_cancel_during_explanation_has_cancelled_report_and_never_commits_cache(self) -> None:
        from ..runtime import cancellation
        from ..schemas.verification import VerificationResult
        observed = []
        def completed_workflow(ctx, state, *args, **kwargs):
            self.assertTrue(ctx.defer_cache_commit)
            ctx.pending_solve_cache = ("verified-key", object(), object())
            ctx.canonical_result = SimpleNamespace()
            return SimpleNamespace(partial=False, verification=VerificationResult(task_id=state.task_id, passed=True), evidence=[])
        def explain(ctx, task_spec):
            observed.append(task_spec.task_id)
            cancellation.request_cancel(task_spec.task_id)
            return "discard this late success explanation", "rule"
        self.health_mock.return_value = EngineHealth(available=True)
        with patch.object(service, "run_fixed_powerflow_skill", side_effect=completed_workflow), \
                patch.object(self.app, "_explain", side_effect=explain), \
                patch.object(self.app, "_render", return_value={}), \
                patch("dpf_agent.tools.powerflow_tools.RunPowerFlowTool._save_solve_cache") as save:
            try:
                result = self.app.run(self.request, cli_callback=None)
            finally:
                for task_id in observed:
                    cancellation.clear(task_id)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"]["code"], "CANCELLED")
        self.assertFalse(result["verification"]["passed"])
        self.assertEqual(result["explanation"], "")
        save.assert_not_called()
        with open(result["artifacts"]["verification.json"], encoding="utf-8") as stream:
            self.assertFalse(json.load(stream)["passed"])
        self.assertFalse(service.current_task_lock()["locked"])

    def test_verified_cache_is_committed_only_after_successful_publication(self) -> None:
        from ..schemas.verification import VerificationResult
        events = []
        def completed_workflow(ctx, state, *args, **kwargs):
            ctx.pending_solve_cache = ("verified-key", object(), object())
            ctx.canonical_result = SimpleNamespace()
            return SimpleNamespace(partial=False, verification=VerificationResult(task_id=state.task_id, passed=True), evidence=[])
        publish = self.app._publish_outputs
        def published(*args):
            result = publish(*args)
            events.append("published")
            return result
        self.health_mock.return_value = EngineHealth(available=True)
        with patch.object(service, "run_fixed_powerflow_skill", side_effect=completed_workflow), \
                patch.object(self.app, "_explain", return_value=("verified", "rule")), \
                patch.object(self.app, "_render", return_value={}), \
                patch.object(self.app, "_publish_outputs", side_effect=published), \
                patch("dpf_agent.tools.powerflow_tools.RunPowerFlowTool._save_solve_cache", side_effect=lambda *a: events.append("cached")):
            result = self.app.run(self.request, cli_callback=None)
        self.assertEqual(result["status"], "success")
        self.assertEqual(events, ["published", "cached"])

    def test_report_remains_part_of_serial_task_lifecycle(self) -> None:
        rendering = threading.Event()
        finish = threading.Event()
        errors: list[BaseException] = []

        def render(*args):
            rendering.set()
            if not finish.wait(5):
                raise RuntimeError("test report wait timed out")
            return {}

        def run():
            try:
                self.app.run(self.request, cli_callback=None)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(self.app, "_render", side_effect=render):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(rendering.wait(3))
                before = self.progress()
                self.assertEqual(before["stage"], "report")
                other = service.ApplicationService(self.config)
                with self.assertRaises(service.TaskBusyError):
                    other.run(self.request, cli_callback=None)
                self.assertEqual(self.progress(), before)
            finally:
                finish.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        self.assertFalse(service.current_task_lock()["locked"])

    def test_report_publication_exception_releases_task_lock(self) -> None:
        with patch.object(self.app, "_publish_outputs", side_effect=OSError("test publication failed")):
            with self.assertRaisesRegex(OSError, "test publication failed"):
                self.app.run(self.request, cli_callback=None)
        self.assertFalse(service.current_task_lock()["locked"])
        self.assertIsNone(service.current_task_lock()["task_id"])
        self.assertTrue(self.progress()["done"])

    def test_early_trace_exception_releases_task_lock(self) -> None:
        def broken_callback(message):
            raise OSError("test output failed")
        with self.assertRaisesRegex(OSError, "test output failed"):
            self.app.run(self.request, cli_callback=broken_callback)
        self.assertFalse(service.current_task_lock()["locked"])
        self.assertTrue(self.progress()["done"])

    def test_cancel_request_preserves_other_task_progress(self) -> None:
        self.app._write_progress("new-task", "solve", "running", 60)
        before = self.progress()
        with patch("dpf_agent.runtime.cancellation.request_cancel") as cancel:
            response = asyncio.run(api.cancel_task("old-task"))
        cancel.assert_called_once_with("old-task")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.progress(), before)

    def test_busy_api_returns_conflict(self) -> None:
        busy = SimpleNamespace(run=lambda *args, **kwargs: (_ for _ in ()).throw(service.TaskBusyError("busy")))
        with patch.object(api, "_get_service", return_value=busy):
            with self.assertRaises(HTTPException) as caught:
                api.run_task(RunRequest(message="计算 case4", case_ref="case4"), BackgroundTasks())
        self.assertEqual(caught.exception.status_code, 409)

    def test_minimum_voltage_explanation_checks_both_limits(self) -> None:
        self.app.model_client = SimpleNamespace(explain=lambda facts, fallback: (fallback, "rule"))
        for voltage, expected in ((0.9, "低于仿真判据下限"), (0.95, "在仿真判据区间内"),
                                  (1.05, "在仿真判据区间内"), (1.1, "高于仿真判据上限")):
            with self.subTest(voltage=voltage):
                result = SimpleNamespace(
                    solver_status=SimpleNamespace(converged=True, iterations=2),
                    system_summary=SimpleNamespace(min_voltage_pu=voltage, min_voltage_bus="b1",
                        max_voltage_pu=voltage, max_voltage_bus="b1", total_load_kw=100),
                    loss_summary=SimpleNamespace(total_loss_kw=1, loss_pct_of_load=1),
                )
                text, _ = self.app._explain(SimpleNamespace(canonical_result=result), SimpleNamespace(focus_objects=[]))
                self.assertIn(expected, text)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ServiceLifecycleTests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
