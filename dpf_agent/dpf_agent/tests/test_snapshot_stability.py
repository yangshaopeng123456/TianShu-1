"""有界稳定化行为测试：漂移、预算、真正的最终复算、取消及权威产物一致。"""
from __future__ import annotations
import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .test_recovery_verification import RecoveryVerificationTests, FakeAdapter
from ..engines.opendss_adapter import AdapterError
from ..runtime import cancellation
from ..schemas.enums import ErrorCode, TaskStatus
from ..schemas.task_spec import SolverConfig
from ..tools import powerflow_tools as tools
from ..tools import snapshot_stability as stability


class RefiningAdapter(FakeAdapter):
    supports_snapshot_refinement = True

    def __init__(self, profile, values):
        super().__init__()
        self.values = values
        self.limits = []
        self.tick = 0.0
        self.clock_step = 0.0
        self.break_identity_at = None
        self.break_state_at = None
        self.nonconverged_at = None
        self.engine_error_at = None
        self.invalid_balance_at = None
        self.sol = SimpleNamespace(Mode=0, ControlMode=-1, DblHour=0.0,
                                   MaxIterations=profile.iteration_config.max_iterations,
                                   Tolerance=profile.tolerance_config.tolerance)

    def solve(self, circuit, *, iteration_limit=None, timeout_s=None):
        self.limits.append(iteration_limit or self.sol.MaxIterations)
        self.tick += self.clock_step
        out = super().solve(circuit)
        if self.solve_calls == self.nonconverged_at:
            out.converged = False
        if self.solve_calls == self.engine_error_at:
            out.error_flag = True
        if self.solve_calls == self.break_state_at:
            self.sol.DblHour += 1
        return out

    def extract_raw_result(self, circuit, scopes, *, timeout_s=None):
        raw = copy.deepcopy(self.raw)
        index = self.solve_calls - 1
        q = self.values(index) if callable(self.values) else self.values[min(index, len(self.values) - 1)]
        raw.system.update(total_power_kvar=q, load_kvar=q)
        if self.solve_calls == self.break_identity_at:
            raw.buses[0]["name"] = "other"
        if self.solve_calls == self.invalid_balance_at:
            raw.system["total_power_kw"] = 300
        return raw

    def run_with_engine(self, fn, timeout_s=None):
        return fn(SimpleNamespace(ActiveCircuit=SimpleNamespace(Solution=self.sol)))


class CancellingAdapter(RefiningAdapter):
    """离线原生边界替身：在指定真实接口位置报取消或登记用户取消。"""

    def __init__(self, profile, values, boundary, *, registry_only=False):
        super().__init__(profile, values)
        self.boundary = boundary
        self.registry_only = registry_only

    def at_boundary(self, operation):
        if (operation, self.solve_calls) == self.boundary:
            if self.registry_only:
                cancellation.request_cancel("test")
            else:
                raise AdapterError(ErrorCode.CANCELLED, "任务已被用户取消")

    def solve(self, circuit, *, iteration_limit=None, timeout_s=None):
        result = super().solve(circuit, iteration_limit=iteration_limit, timeout_s=timeout_s)
        self.at_boundary("solve")
        return result

    def extract_raw_result(self, circuit, scopes, *, timeout_s=None):
        result = super().extract_raw_result(circuit, scopes, timeout_s=timeout_s)
        self.at_boundary("extract")
        return result

    def run_with_engine(self, fn, timeout_s=None):
        result = super().run_with_engine(fn, timeout_s=timeout_s)
        self.at_boundary("signature")
        return result


class SnapshotStabilityTests(unittest.TestCase):
    values = [0, 20, 30, 35, 35.01, 35.015, 35.017]

    def setUp(self):
        cancellation.clear("test")
        self.addCleanup(cancellation.clear, "test")
        fixture = RecoveryVerificationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.context()
        self.ctx.adapter = RefiningAdapter(self.ctx.solver_profile, self.values)
        self.cache = os.path.join(fixture.root, "stable-cache.json")

    def run_chain(self):
        with patch.object(tools.RunPowerFlowTool, "_cache_path", return_value=self.cache), \
                patch.object(tools, "AgentConfig", return_value=self.ctx.config):
            tools.RunPowerFlowTool().run(self.ctx)
            tools.ExtractPowerFlowResultTool().run(self.ctx)
            return tools.VerifyPowerBalanceTool().run(self.ctx)

    def audit(self):
        with open(self.ctx.artifacts["numerical_stabilization.json"], encoding="utf-8") as file:
            return json.load(file)

    def test_stabilized_candidate_is_recomputed_then_promoted_and_cached(self):
        out = self.run_chain()
        self.assertTrue(out["passed"])
        self.assertEqual(self.ctx.adapter.solve_calls, 7)
        self.assertEqual(self.ctx.canonical_result.system_summary.total_generation_kvar, 35.015)
        self.assertEqual(self.ctx.raw_result.system["total_power_kvar"], 35.015)
        self.assertEqual(self.ctx.canonical_result.solver_status.total_iterations, 14)
        self.assertEqual(self.ctx.canonical_result.solver_status.raw_solution_count, 7)
        self.assertEqual(self.audit()["calls"][-1]["stage"], "independent_final")
        for name in ("powerflow_result.json", "raw_opendss_result.json"):
            with open(self.ctx.artifacts[name], encoding="utf-8") as file:
                saved = json.load(file)
            self.assertEqual(saved["solver_status"]["total_iterations"] if name.startswith("powerflow")
                             else saved["solver"]["total_iterations"], 14)
        with open(self.cache, encoding="utf-8") as file:
            self.assertEqual(json.load(file)["system"]["total_power_kvar"], 35.015)

    def test_final_independent_drift_rejects_without_promoting_or_caching(self):
        self.ctx.adapter.values = self.values[:-1] + [60]
        out = self.run_chain()
        self.assertFalse(out["passed"])
        self.assertIn("独立复算仍不一致", self.audit()["stopped_reason"])
        self.assertEqual(self.ctx.raw_result.system["total_power_kvar"], 0)
        self.assertEqual(self.ctx.canonical_result.system_summary.total_generation_kvar, 0)
        self.assertFalse(os.path.exists(self.cache))

    def test_explicit_iteration_budget_and_native_limits_are_never_increased(self):
        self.ctx.task_spec.solver_config = SolverConfig(max_iterations=10, tolerance=1e-4)
        self.ctx.solver_profile.iteration_config.max_iterations = 10
        self.ctx.adapter.sol.MaxIterations = 10
        out = self.run_chain()
        self.assertFalse(out["passed"])
        self.assertLessEqual(self.audit()["total_iterations"], 10)
        self.assertTrue(all(limit <= 10 for limit in self.ctx.adapter.limits))
        self.assertEqual(self.ctx.solver_profile.param_dict()["max_iterations"], 10)
        self.assertFalse(os.path.exists(self.cache))

    def test_oscillation_stops_at_call_limit_instead_of_selecting_a_good_pair(self):
        self.ctx.config.stability_max_solves = 6
        self.ctx.adapter.values = lambda index: 0 if index % 2 == 0 else 20
        self.assertFalse(self.run_chain()["passed"])
        self.assertLessEqual(self.ctx.adapter.solve_calls, 6)
        self.assertIn("次数上限", self.audit()["stopped_reason"])
        self.assertFalse(os.path.exists(self.cache))

    def test_failed_native_refinement_never_uses_previous_convergence(self):
        for field in ("nonconverged_at", "engine_error_at"):
            with self.subTest(field=field):
                self.ctx.adapter = RefiningAdapter(self.ctx.solver_profile, self.values)
                setattr(self.ctx.adapter, field, 3)
                self.assertFalse(self.run_chain()["passed"])
                self.assertEqual(self.ctx.adapter.solve_calls, 3)
                self.assertFalse(os.path.exists(self.cache))

    def test_missing_identity_snapshot_change_and_invalid_balance_are_rejected(self):
        for field in ("break_identity_at", "break_state_at", "invalid_balance_at"):
            with self.subTest(field=field):
                self.ctx.adapter = RefiningAdapter(self.ctx.solver_profile, self.values)
                setattr(self.ctx.adapter, field, 3)
                self.assertFalse(self.run_chain()["passed"])
                self.assertEqual(self.ctx.adapter.solve_calls, 3)
                self.assertFalse(os.path.exists(self.cache))

    def test_cancellation_between_real_calls_stops_and_counts_last_completed_call(self):
        with patch.object(stability, "is_cancelled", side_effect=lambda _id: self.ctx.adapter.solve_calls >= 3):
            with self.assertRaises(tools.ToolFailure) as caught:
                self.run_chain()
        self.assertEqual(caught.exception.code, ErrorCode.CANCELLED)
        self.assertEqual(self.audit()["solution_count"], 3)
        self.assertEqual(self.audit()["stopped_reason"], "cancelled")
        self.assertIsNone(self.ctx.pending_solve_cache)
        self.assertFalse(os.path.exists(self.cache))

    def test_timeout_counts_last_completed_call_and_never_promotes(self):
        self.ctx.config.stability_timeout_s = 0.5
        self.ctx.adapter.clock_step = 0.3
        with patch.object(stability.time, "monotonic", side_effect=lambda: self.ctx.adapter.tick):
            self.assertFalse(self.run_chain()["passed"])
        self.assertEqual(self.audit()["solution_count"], self.ctx.adapter.solve_calls)
        self.assertFalse(os.path.exists(self.cache))

    def test_no_initial_drift_preserves_the_normal_two_solve_path(self):
        self.ctx.adapter.values = [0]
        self.assertTrue(self.run_chain()["passed"])
        self.assertEqual(self.ctx.adapter.solve_calls, 2)
        self.assertNotIn("numerical_stabilization.json", self.ctx.artifacts)

    def test_adapter_without_persistent_snapshot_keeps_original_rejection(self):
        self.ctx.adapter.supports_snapshot_refinement = False
        self.assertFalse(self.run_chain()["passed"])
        self.assertEqual(self.ctx.adapter.solve_calls, 2)
        self.assertNotIn("numerical_stabilization.json", self.ctx.artifacts)

    def assert_cancelled_without_publication(self):
        with self.assertRaises(tools.ToolFailure) as caught:
            self.run_chain()
        self.assertEqual(caught.exception.code, ErrorCode.CANCELLED)
        self.assertEqual(caught.exception.status, TaskStatus.CANCELLED)
        self.assertIsNone(self.ctx.pending_solve_cache)
        self.assertFalse(os.path.exists(self.cache))
        self.assertEqual(self.ctx.raw_result.system["total_power_kvar"], 0)
        self.assertEqual(self.ctx.canonical_result.system_summary.total_generation_kvar, 0)
        self.assertNotIn("verification.json", self.ctx.artifacts)

    def test_native_cancelled_initial_recheck_and_final_refinement_are_terminal(self):
        # 二次求解、采集、追加求解和最终独立复算均不应变成 validation_failed。
        for boundary in (("solve", 2), ("extract", 2), ("solve", 3), ("extract", 3),
                         ("solve", 7), ("extract", 7), ("signature", 7)):
            with self.subTest(boundary=boundary):
                self.ctx.adapter = CancellingAdapter(self.ctx.solver_profile, self.values, boundary)
                self.assert_cancelled_without_publication()

    def test_registry_cancel_after_successful_native_returns_is_still_terminal(self):
        for boundary in (("solve", 2), ("extract", 2), ("extract", 7)):
            with self.subTest(boundary=boundary):
                cancellation.clear("test")
                self.ctx.adapter = CancellingAdapter(self.ctx.solver_profile, self.values, boundary,
                                                     registry_only=True)
                self.assert_cancelled_without_publication()

    def test_cancel_after_refinement_returns_prevents_authoritative_promotion(self):
        actual_refine = stability.refine_snapshot

        def refine_then_cancel(*args, **kwargs):
            result = actual_refine(*args, **kwargs)
            self.assertTrue(result.passed)
            cancellation.request_cancel("test")
            return result

        with patch.object(stability, "refine_snapshot", side_effect=refine_then_cancel):
            self.assert_cancelled_without_publication()

    def test_cancel_after_verification_artifact_prevents_cache_commit(self):
        self.ctx.adapter.values = [0]
        actual_save = self.ctx.save_json

        def save_then_cancel(name, data):
            path = actual_save(name, data)
            if name == "verification.json":
                cancellation.request_cancel("test")
            return path

        with patch.object(self.ctx, "save_json", side_effect=save_then_cancel):
            with self.assertRaises(tools.ToolFailure) as caught:
                self.run_chain()
        self.assertEqual(caught.exception.code, ErrorCode.CANCELLED)
        self.assertIsNone(self.ctx.pending_solve_cache)
        self.assertFalse(os.path.exists(self.cache))

    def test_cancel_during_cache_serialization_does_not_publish_or_replace_old_cache(self):
        self.ctx.adapter.values = [0]
        original = b'{"solver":{"converged":true},"old_success":true}'
        with open(self.cache, "wb") as saved:
            saved.write(original)
        actual_dump = tools.json.dump

        def dump_then_cancel(data, stream, **kwargs):
            result = actual_dump(data, stream, **kwargs)
            if isinstance(data, dict) and "cache_key" in data:
                cancellation.request_cancel("test")
            return result

        with patch.object(tools.json, "dump", side_effect=dump_then_cancel):
            with self.assertRaises(tools.ToolFailure) as caught:
                self.run_chain()
        self.assertEqual(caught.exception.code, ErrorCode.CANCELLED)
        with open(self.cache, "rb") as saved:
            self.assertEqual(saved.read(), original)
        self.assertFalse(os.path.exists(self.cache + ".tmp"))
        self.assertIsNone(self.ctx.pending_solve_cache)

    def test_deferred_verified_cache_remains_pending_without_publication(self):
        self.ctx.adapter.values = [0]
        self.ctx.defer_cache_commit = True
        self.assertTrue(self.run_chain()["passed"])
        self.assertIsNotNone(self.ctx.pending_solve_cache)
        self.assertFalse(os.path.exists(self.cache))


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SnapshotStabilityTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
