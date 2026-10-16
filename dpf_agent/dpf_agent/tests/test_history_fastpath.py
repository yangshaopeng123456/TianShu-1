"""精确历史问句快路由：真实文件快照与身份守卫；单测不启动引擎。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ..web import chat_api, history_fastpath as fast, history_qa


TASK = "pf-20261013-010101-abcdef"
OTHER = "pf-20261013-020202-fedcba"


class HistoryFastpathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="history-fastpath-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "tasks" / TASK
        self.output = self.root / "outputs" / TASK
        (self.work / "input").mkdir(parents=True)
        self.output.mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            "DPF_WORKSPACE": str(self.root / "tasks"), "DPF_OUTPUTS": str(self.root / "outputs"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        model = b"Clear\nNew Circuit.Test basekv=20\n"
        (self.work / "input" / "Master.dss").write_bytes(model)
        digest = hashlib.sha256(model).hexdigest()
        fingerprint = hashlib.sha256(f"Master.dss:{digest}\n".encode()).hexdigest()[:16]
        self.arts = {
            "state.json": {"task_id": TASK, "status": "success", "phase": "RESULT_VERIFIED", "error": None,
                           "_persisted_stage": "final", "task_spec_ref": str(self.work / "task_spec.json"),
                           "result_ref": str(self.work / "powerflow_result.json"), "verification_ref": str(self.work / "verification.json"),
                           "case_ref": "", "solver_profile_ref": "", "version_set": {"adapter": "1.6.2", "defaults": "sim-v1"}},
            "task_spec.json": {"task_id": TASK, "case_ref": "case4", "frozen": True},
            "validation.json": {"passed": True, "case": {"case_id": "Master", "case_fingerprint": fingerprint,
                                "master_dss_ref": "Master.dss", "canonical_workspace_ref": str(self.work),
                                "validation_ref": str(self.work / "validation.json"),
                                "dependency_manifest": {"master_dss": "Master.dss", "entries": [{
                                    "path": "Master.dss", "sha256": digest, "size_bytes": len(model), "role": "master"}]}}},
            "solver_profile.json": {"profile_id": "sp-test", "frozen": True, "version_set": {
                                    "engine_version": "OpenDSS COM test", "adapter_version": "1.6.2", "defaults_version": "sim-v1"}},
            "powerflow_result.json": {"task_id": TASK, "case_fingerprint": fingerprint, "solver_profile_ref": "sp-test",
                                      "engine_version": "OpenDSS COM test", "adapter_version": "1.6.2",
                                      "solver_status": {"converged": True, "iterations": 2, "error_flag": False},
                                      "result_status_by_scope": {"system": "ok", "bus": "ok"},
                                      "system_summary": {"min_voltage_pu": .9876, "min_voltage_bus": "b2",
                                                         "max_voltage_pu": 1.0234, "max_voltage_bus": "source",
                                                         "total_load_kw": 600.123, "total_generation_kw": 604.6908, "net_load_kw": 600.123},
                                      "loss_summary": {"total_loss_kw": 4.5678, "total_loss_kvar": 1.2345},
                                      "bus_results": [{"name": "b2", "voltage_pu": .9876, "base_kv": 20.0},
                                                      {"name": "source", "voltage_pu": 1.0234, "base_kv": 20.0}]},
            "verification.json": {"task_id": TASK, "passed": True,
                                  "checks": [{"check_id": name, "passed": True} for name in sorted(fast._CHECKS)]},
            "network_profile.json": {"case_id": "Master"},
        }
        for name in self.arts:
            self.write(name)
        (self.output / "report.html").write_text("verified report", encoding="utf-8")
        self.ctx = {"last_task_id": TASK, "last_success_task_id": TASK, "last_task_status": "success",
                    "last_success": True, "last_task_case_ref": "case4", "last_success_case_ref": "case4",
                    "message_count": 2, "last_attachments": [], "recent_turns": []}

    def write(self, name: str, both: bool = True) -> None:
        data = json.dumps(self.arts[name], ensure_ascii=False).encode()
        (self.output / name).write_bytes(data)
        if both:
            (self.work / name).write_bytes(data)

    def answer(self, content: str = "最低电压是多少", **kwargs):
        return fast.answer_verified_history(content, self.ctx, **kwargs)

    def test_all_four_metrics_reuse_deterministic_formatter_and_source_task(self) -> None:
        for question, value in [("最低电压是多少", "0.9876"), ("最高电压是多少", "1.0234"),
                                ("总网损是多少", "4.5678"), ("总负荷是多少", "600.123")]:
            with self.subTest(question=question), patch.object(history_qa, "answer_by_metric", wraps=history_qa.answer_by_metric) as formatter:
                reply = self.answer(question)
                self.assertIsNotNone(reply)
                self.assertIn(value, reply.text)
                self.assertIn(TASK, reply.text)
                self.assertIn("本次不重新求解", reply.text)
                self.assertEqual(reply.report_url, f"/api/tasks/{TASK}/report")
                formatter.assert_called_once()

    def test_exact_prefix_and_question_mark_only(self) -> None:
        self.assertIsNotNone(self.answer(" 刚才的最低电压是多少？ "))
        self.assertIsNotNone(self.answer("上次的总负荷多少?"))

    def test_registered_builtin_case_spelling_requires_builtin_provenance(self) -> None:
        self.arts["task_spec.json"]["case_ref"] = "CASE4"
        self.write("task_spec.json")
        self.assertIsNone(self.answer())
        self.arts["validation.json"]["case"]["source_type"] = "builtin"
        self.write("validation.json")
        self.assertIsNotNone(self.answer())
        self.arts["validation.json"]["case"]["source_type"] = "user_file"
        self.write("validation.json")
        self.assertIsNone(self.answer())

    def test_custom_names_and_paths_do_not_gain_case_or_alias_equivalence(self) -> None:
        self.arts["validation.json"]["case"]["source_type"] = "builtin"
        self.write("validation.json")
        for expected, parsed in [("custom_case", "CUSTOM_CASE"), ("./case4", "CASE4"),
                                 ("case4", "./CASE4"), ("case4", "case4/Master.dss"),
                                 ("case4", "ieee33"), ("C:/case4/Master.dss", "c:/case4/master.dss")]:
            with self.subTest(expected=expected, parsed=parsed):
                self.ctx["last_task_case_ref"] = self.ctx["last_success_case_ref"] = expected
                self.arts["task_spec.json"]["case_ref"] = parsed
                self.write("task_spec.json")
                self.assertIsNone(self.answer())

    def test_rerun_parameters_other_objects_and_ambiguous_queries_bypass(self) -> None:
        questions = ["重新计算最低电压是多少", "重新求解case4", "复算后最低电压是多少", "验算case4",
                     "容差改成1e-6再算", "最低电压是多少，容差1e-6", "case4的最低电压是多少",
                     "ieee33的最低电压是多少", "母线b2最低电压是多少", "最低电压是多少？然后重新计算",
                     "它的电压是多少", "上次失败的最低电压是多少", "请问最低电压是多少", "最低电压是多少??"]
        with patch.object(fast, "_snapshot", side_effect=AssertionError("unsafe queries must not read snapshots")):
            for question in questions:
                with self.subTest(question=question):
                    self.assertIsNone(self.answer(question))

    def test_new_attachments_never_use_history_fastpath(self) -> None:
        with patch.object(fast, "_snapshot", side_effect=AssertionError("new attachment is a new input")):
            self.assertIsNone(self.answer(attachments=[object()]))

    def test_latest_failed_cancelled_or_different_success_does_not_fall_back(self) -> None:
        for status in ["cancelled", "non_converged", "validation_failed", "partial", "converged"]:
            with self.subTest(status=status):
                self.ctx["last_task_status"] = status
                self.assertIsNone(self.answer())
        self.ctx["last_task_status"] = "success"
        self.ctx["last_success_task_id"] = OTHER
        self.assertIsNone(self.answer())

    def test_untrusted_chat_success_cannot_override_authoritative_failure(self) -> None:
        for status in ["cancelled", "non_converged", "validation_failed"]:
            self.arts["state.json"]["status"] = status
            self.write("state.json")
            self.assertIsNone(self.answer())

    def test_each_task_identity_mismatch_rejected_even_when_copies_agree(self) -> None:
        for name in ["state.json", "task_spec.json", "powerflow_result.json", "verification.json"]:
            with self.subTest(name=name):
                self.arts[name]["task_id"] = OTHER
                self.write(name)
                self.assertIsNone(self.answer())
                self.arts[name]["task_id"] = TASK
                self.write(name)

    def test_authoritative_reference_mismatch_or_escape_rejected(self) -> None:
        for field in ["task_spec_ref", "result_ref", "verification_ref", "case_ref", "solver_profile_ref"]:
            with self.subTest(field=field):
                original = self.arts["state.json"].get(field)
                self.arts["state.json"][field] = str(self.root / "tasks" / OTHER / "state.json")
                self.write("state.json")
                self.assertIsNone(self.answer())
                self.arts["state.json"][field] = original
                self.write("state.json")
        self.arts["state.json"]["case_ref"] = str(self.work / "validation.json")
        self.arts["state.json"]["solver_profile_ref"] = str(self.work / "solver_profile.json")
        self.write("state.json")
        self.assertIsNotNone(self.answer())

    def test_case_source_fingerprint_and_solver_profile_must_agree(self) -> None:
        edits = [("task_spec.json", "case_ref", "ieee33"),
                 ("powerflow_result.json", "case_fingerprint", "wrong-case"),
                 ("powerflow_result.json", "solver_profile_ref", "sp-other"),
                 ("network_profile.json", "case_id", "different-case"),
                 ("solver_profile.json", "frozen", False)]
        for name, field, value in edits:
            with self.subTest(name=name, field=field):
                original = self.arts[name][field]
                self.arts[name][field] = value
                self.write(name)
                self.assertIsNone(self.answer())
                self.arts[name][field] = original
                self.write(name)

    def test_verification_passed_checks_and_final_phase_required(self) -> None:
        edits = [("verification.json", "passed", False), ("verification.json", "checks", []),
                 ("state.json", "phase", "FAILED"), ("state.json", "_persisted_stage", "running"),
                 ("state.json", "error", {"code": "CANCELLED"}), ("validation.json", "passed", False)]
        for name, field, value in edits:
            with self.subTest(name=name, field=field):
                original = self.arts[name][field]
                self.arts[name][field] = value
                self.write(name)
                self.assertIsNone(self.answer())
                self.arts[name][field] = original
                self.write(name)
        self.arts["verification.json"]["checks"][0]["passed"] = False
        self.write("verification.json")
        self.assertIsNone(self.answer())

    def test_result_engine_adapter_and_defaults_versions_required(self) -> None:
        for field in ["engine_version", "adapter_version"]:
            original = self.arts["powerflow_result.json"][field]
            self.arts["powerflow_result.json"][field] = "different-version"
            self.write("powerflow_result.json")
            self.assertIsNone(self.answer())
            self.arts["powerflow_result.json"][field] = original
            self.write("powerflow_result.json")
        self.arts["state.json"]["version_set"]["defaults"] = "different-defaults"
        self.write("state.json")
        self.assertIsNone(self.answer())

    def test_missing_or_corrupt_artifacts_and_different_copies_rejected(self) -> None:
        for name in fast._ARTIFACTS:
            with self.subTest(name=name):
                path = self.work / name
                data = path.read_bytes()
                path.unlink()
                self.assertIsNone(self.answer())
                path.write_bytes(data)
        self.arts["state.json"]["status"] = "cancelled"
        self.write("state.json", both=False)
        self.assertIsNone(self.answer())
        for folder in (self.work, self.output):
            (folder / "state.json").write_text("invalid json", encoding="utf-8")
        self.assertIsNone(self.answer())

    def test_nonconverged_error_missing_metric_and_invalid_numbers_rejected(self) -> None:
        result = self.arts["powerflow_result.json"]
        for status in [{"converged": False, "error_flag": False}, {"converged": True, "error_flag": True}]:
            result["solver_status"] = status
            self.write("powerflow_result.json")
            self.assertIsNone(self.answer())

        result["solver_status"] = {"converged": True, "error_flag": False}
        for value in [None, True, "0.9876", float("nan"), float("inf")]:
            result["system_summary"]["min_voltage_pu"] = value
            self.write("powerflow_result.json")
            self.assertIsNone(self.answer())

    def test_excessively_nested_corrupt_json_falls_back_without_crashing_chat(self) -> None:
        data = b"[" * 2000 + b"0" + b"]" * 2000
        for folder in (self.output, self.work):
            (folder / "state.json").write_bytes(data)
        self.assertIsNone(self.answer())

    def test_input_content_manifest_and_workspace_references_checked(self) -> None:
        (self.work / "input" / "Master.dss").write_bytes(b"changed model")
        self.assertIsNone(self.answer())
        self.arts["validation.json"]["case"]["canonical_workspace_ref"] = str(self.root / "outside")
        self.write("validation.json")
        self.assertIsNone(self.answer())

    def test_dependency_path_cannot_escape_expected_input_directory(self) -> None:
        entry = self.arts["validation.json"]["case"]["dependency_manifest"]["entries"][0]
        entry["path"] = str(self.root / "outside.dss")
        self.write("validation.json")
        original_read = Path.read_bytes
        reads = []
        def record_read(path):
            reads.append(path)
            return original_read(path)
        with patch.object(Path, "read_bytes", new=record_read):
            self.assertIsNone(self.answer())
            self.assertFalse(any(str(path).endswith("outside.dss") for path in reads))

    def test_symlinked_artifact_and_report_cannot_escape_task_output(self) -> None:
        outside = self.root / "external_state.json"
        outside.write_bytes((self.output / "state.json").read_bytes())
        published = self.output / "state.json"
        data = published.read_bytes()
        published.unlink()
        try:
            published.symlink_to(outside)
        except OSError as exc:
            published.write_bytes(data)
            self.skipTest(f"symlinks not supported by this test host: {exc}")
        self.assertIsNone(self.answer())
        published.unlink()
        published.write_bytes(data)
        report = self.output / "report.html"
        report.unlink()
        report.symlink_to(outside)
        self.assertIsNone(self.answer())

    def test_task_id_path_injection_rejected_before_file_reads(self) -> None:
        self.ctx["last_task_id"] = self.ctx["last_success_task_id"] = "../outside"
        with patch.object(fast, "_snapshot", side_effect=AssertionError("path injection must be rejected")):
            self.assertIsNone(self.answer())

    def test_artifact_change_during_formatter_read_discards_reply(self) -> None:
        def change(metric, content, ctx):
            self.arts["powerflow_result.json"]["system_summary"]["min_voltage_pu"] = .5
            self.write("powerflow_result.json")
            return "最低电压 0.5"
        with patch.object(history_qa, "answer_by_metric", side_effect=change):
            self.assertIsNone(self.answer())

    def test_chat_entry_bypasses_intent_and_service_without_new_task_identity(self) -> None:
        session = SimpleNamespace(id="session-fast", title="fastpath")
        saved = []
        def add_message(session_id, role, text, **kwargs):
            saved.append({"role": role, "content": text, **kwargs})
            return SimpleNamespace(id=f"message-{len(saved)}", to_dict=lambda: {
                "role": role, "content": text, "task_id": kwargs.get("task_id"), "report_url": kwargs.get("report_url")})
        with patch.object(chat_api.store, "init_db"), patch.object(chat_api.store, "get_session", return_value=session), \
                patch.object(chat_api.store, "add_message", side_effect=add_message), \
                patch.object(chat_api, "_get_session_context", return_value=self.ctx), \
                patch.object(chat_api, "_get_service", side_effect=AssertionError("history must not compute")), \
                patch("dpf_agent.runtime.intent.decide", side_effect=AssertionError("history must not call LLM intent")):
            response = chat_api.send_message("session-fast", "最低电压是多少", [])
        body = json.loads(response.body)
        self.assertEqual(body["answer_source"], "verified_history")
        self.assertEqual(body["source_task_id"], TASK)
        self.assertIsNone(body["assistant_message"]["task_id"])
        self.assertEqual(body["assistant_message"]["report_url"], f"/api/tasks/{TASK}/report")
        self.assertIn("0.9876", body["assistant_message"]["content"])
        self.assertEqual(len(saved), 2)

    def test_chat_summary_and_history_convergence_show_cumulative_solve_counts(self) -> None:
        self.arts["powerflow_result.json"]["solver_status"].update(total_iterations=31, raw_solution_count=5)
        self.write("powerflow_result.json")
        chat = chat_api._extract_summary({"powerflow_result.json": str(self.output / "powerflow_result.json")})
        history = history_qa.answer_by_metric(history_qa.METRIC_CONVERGENCE, "收敛状态", self.ctx)
        for text in (chat, history):
            self.assertIn("当前解迭代 2 次", text)
            self.assertIn("累计迭代 31 次", text)
            self.assertIn("显式 Solve 调用 5 次", text)
            self.assertIn("不含 Compile 内部", text)
        legacy = history_qa.convergence_summary({"converged": True, "iterations": 2})
        self.assertIn("当前解迭代 2 次", legacy)
        self.assertNotIn("累计迭代", legacy)
        self.assertNotIn("显式 Solve 调用", legacy)


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(HistoryFastpathTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
