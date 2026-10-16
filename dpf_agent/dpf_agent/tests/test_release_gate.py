"""Gate behavior with real isolated fixture subprocesses, never production run_all.

The .js fixture programs intentionally run under the fixture Python interpreter;
they exercise orchestration/exit/skip contracts, not Node or COM functionality.
Native result fixtures exercise JSON validation only and are not native evidence.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from .. import release_gate as gate


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("DPF_TEST_ARTIFACT_ROOT")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="release-gate-unit-", dir=base))
        self.dev = self.root / "development"
        self._write_tree(self.dev)
        self.config = gate.GateConfig(dev_root=self.dev, evidence_dir=self.root / "evidence",
                                      node=sys.executable, timeout_s=10)

    def _write(self, relative: str, text: str):
        destination = self.dev / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")

    def _write_tree(self, root: Path):
        previous = self.dev
        self.dev = root
        for directory in ("dpf_agent/dpf_agent/tests", "dpf_agent/dpf_agent/web", "tianshu_ui/renderer"):
            (root / directory).mkdir(parents=True)
        self._write("dpf_agent/dpf_agent/__init__.py", '__version__="2.0.0"\nADAPTER_VERSION="2.0.0"\n')
        self._write("dpf_agent/dpf_agent/tests/__init__.py", "")
        self._write("dpf_agent/dpf_agent/web/conversion_cache.py", 'CONVERTER_VERSIONS: dict[str,str]={"tables":"3.0.0"}\n')
        self._write("dpf_agent/requirements.txt", "# fixture only\n")
        self._write("tianshu_ui/package.json", '{"version":"1.0.0"}')
        self._write("tianshu_ui/main.js", "// fixture only\n")
        self._write("tianshu_ui/renderer/app.js", "// fixture UI source\n")
        self._write("dpf_agent/dpf_agent/tests/run_all.py", """import json,os,pathlib
print('FIXTURE dynamic suite entry; no production suite was executed')
names=('TEMP','TMP','DPF_CHAT_DB_DIR','DPF_WORKSPACE','DPF_OUTPUTS','DPF_TEST_ARTIFACT_ROOT')
pathlib.Path(os.environ['DPF_TEST_ARTIFACT_ROOT'],'environment.json').write_text(json.dumps({n:os.environ[n] for n in names}))
""")
        for script in gate.UI_CHECKS:
            self._write("tianshu_ui/" + script, "print('fixture UI entry, no Node/GUI/native engine')\n")
        self.dev = previous

    @staticmethod
    def step(result, name):
        return next(item for item in result["steps"] if item["name"] == name)

    def test_preflight_calls_default_dynamic_entry_and_never_claims_delivery_or_native(self):
        result = gate.run_gate(self.config)
        self.assertTrue(result["passed"])
        self.assertFalse(result["delivered"])
        self.assertEqual(result["status"], "preflight_passed")
        self.assertEqual(self.step(result, "backend_offline")["argv"][-1], "dpf_agent.tests.run_all")
        self.assertEqual(self.step(result, "native_com")["status"], "skipped")
        self.assertFalse(self.step(result, "native_com")["native_executed"])
        recorded = json.loads((self.config.evidence_dir / "test_artifacts/environment.json").read_text())
        self.assertTrue(all(Path(value).is_relative_to(self.config.evidence_dir) for value in recorded.values()))
        self.assertEqual(json.loads((self.config.evidence_dir / "gate_result.json").read_text())["run_id"], result["run_id"])

    def test_real_offline_subprocess_failure_is_not_ignored(self):
        self._write("dpf_agent/dpf_agent/tests/run_all.py", "print('intentional fixture failure');raise SystemExit(7)\n")
        result = gate.run_gate(self.config)
        self.assertFalse(result["passed"])
        self.assertEqual(self.step(result, "backend_offline")["exit_code"], 7)
        self.assertTrue(all(self.step(result, Path(script).stem)["status"] == "passed" for script in gate.UI_CHECKS))

    def test_render_skip_with_exit_zero_is_partial_not_pass(self):
        self._write("tianshu_ui/verify_render.js", "print('[SKIP] intentionally untested fixture page')\n")
        result = gate.run_gate(self.config)
        self.assertFalse(result["passed"])
        render = self.step(result, "verify_render")
        self.assertEqual(render["exit_code"], 0)
        self.assertEqual(render["status"], "partial")

    def test_discovered_batch_ui_check_becomes_required_and_cannot_skip_or_fail(self):
        for name, program, expected in (
            ("success", "print('fixture batch checks')\n", "passed"),
            ("failure", "print('intentional fixture batch failure');raise SystemExit(9)\n", "failed"),
            ("skip", "print('[SKIP] fixture batch check not covered')\n", "partial"),
        ):
            with self.subTest(name=name):
                self._write("tianshu_ui/verify_batch.js", program)
                result = gate.run_gate(replace(self.config, evidence_dir=self.root / ("batch-" + name)))
                batch = self.step(result, "verify_batch")
                self.assertTrue(batch["required"])
                self.assertEqual(batch["status"], expected)
                self.assertEqual(result["passed"], expected == "passed")
                self.assertIn("verify_batch.js", result["required_ui_checks"])

    def test_missing_interpreter_fails_required_ui_steps(self):
        result = gate.run_gate(replace(self.config, node=str(self.root / "missing-node")))
        self.assertFalse(result["passed"])
        self.assertEqual(self.step(result, "verify_races")["status"], "failed")

    def test_required_native_absence_is_visible_and_fails(self):
        result = gate.run_gate(replace(self.config, native="required"))
        self.assertFalse(result["passed"])
        native = self.step(result, "native_com")
        self.assertEqual(native["status"], "skipped")
        self.assertTrue(native["required"])

    def test_native_exit_zero_without_matching_result_is_rejected(self):
        command = (sys.executable, "-c", "print('unit fixture, not a native solve')")
        result = gate.run_gate(replace(self.config, native_command=command))
        self.assertFalse(result["passed"])
        native = self.step(result, "native_com")
        self.assertEqual(native["exit_code"], 0)
        self.assertEqual(native["status"], "failed")
        self.assertFalse(native["native_executed"])

    def test_native_unavailability_is_skip_optional_but_never_required_pass(self):
        script = """import os,json,pathlib
r=json.loads(pathlib.Path(os.environ['DPF_GATE_REQUEST']).read_text())
r.update(status='skipped',native_executed=False,reason='unit fixture: COM unavailable')
pathlib.Path(os.environ['DPF_GATE_NATIVE_RESULT']).write_text(json.dumps(r))
"""
        command = (sys.executable, "-c", script)
        optional = gate.run_gate(replace(self.config, native_command=command))
        self.assertTrue(optional["passed"])
        self.assertEqual(self.step(optional, "native_com")["status"], "skipped")
        required = gate.run_gate(replace(self.config, evidence_dir=self.root / "required-evidence",
                                        native="required", native_command=command))
        self.assertFalse(required["passed"])
        self.assertFalse(self.step(required, "native_com")["native_executed"])

    def test_final_checks_complete_inventory_including_unexpected_release_source(self):
        release = self.root / "release"
        shutil.copytree(self.dev, release)
        extra = release / "dpf_agent/dpf_agent/unexpected.py"
        extra.write_text("# release-only extra program\n")
        result = gate.run_gate(replace(self.config, mode="final", release_root=release))
        self.assertFalse(result["passed"])
        self.assertFalse(result["delivered"])
        sync = self.step(result, "release_sync")
        self.assertIn("dpf_agent/dpf_agent/unexpected.py", sync["extra_in_release"])
        self.assertEqual(sync["status"], "failed")

    def test_final_comparison_detects_changed_and_missing_files(self):
        release = self.root / "release"
        shutil.copytree(self.dev, release)
        (release / "tianshu_ui/renderer/app.js").write_text("// changed release source\n")
        self._write("dpf_agent/dpf_agent/new_module.py", "# source missing from release\n")
        result = gate.run_gate(replace(self.config, mode="final", release_root=release))
        sync = self.step(result, "release_sync")
        self.assertFalse(result["passed"])
        self.assertIn("tianshu_ui/renderer/app.js", sync["different"])
        self.assertIn("dpf_agent/dpf_agent/new_module.py", sync["missing_in_release"])

    def test_user_runtime_and_secret_files_are_not_hashed_or_required_equal(self):
        original = gate.source_inventory(self.dev)
        for relative in ("dpf_agent/chat_data/session.json", "dpf_agent/dpf_agent/__pycache__/x.pyc",
                         "tianshu_ui/.env", "tianshu_ui/settings.json", "tianshu_ui/node_modules/a.js"):
            self._write(relative, "UNIT TEST ONLY, no user secret read\n")
        self.assertEqual(gate.source_inventory(self.dev), original)

    def test_version_read_is_static_and_backend_adapter_mismatch_rejects(self):
        self._write("dpf_agent/dpf_agent/__init__.py", '__version__="2.0.0"\nADAPTER_VERSION="2.0.0"\nraise RuntimeError("must never execute")\n')
        self.assertEqual(gate.read_versions(self.dev)["backend"], "2.0.0")
        self._write("dpf_agent/dpf_agent/__init__.py", '__version__="2.0.0"\nADAPTER_VERSION="old"\n')
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            gate.read_versions(self.dev)

    def test_source_changes_during_check_fail_audit(self):
        self._write("dpf_agent/dpf_agent/tests/run_all.py", "from pathlib import Path\nPath('dpf_agent/changed_during_check.py').write_text('# fixture mutation')\n")
        result = gate.run_gate(self.config)
        self.assertFalse(result["passed"])
        audit = self.step(result, "development_unchanged")
        self.assertEqual(audit["status"], "failed")
        self.assertIn("dpf_agent/dpf_agent/changed_during_check.py", audit["extra_in_release"])

    def test_preexisting_evidence_is_not_overwritten(self):
        self.config.evidence_dir.mkdir()
        sentinel = self.config.evidence_dir / "gate_result.json"
        sentinel.write_bytes(b"original evidence bytes")
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            gate.run_gate(self.config)
        self.assertEqual(sentinel.read_bytes(), b"original evidence bytes")

    def test_interruption_cannot_publish_a_partial_gate_as_passed(self):
        with patch.object(gate, "_command", side_effect=KeyboardInterrupt):
            result = gate.run_gate(self.config)
        self.assertFalse(result["passed"])
        self.assertFalse(result["delivered"])
        self.assertTrue(result["interrupted"])
        self.assertEqual(self.step(result, "gate_incomplete")["status"], "failed")
        self.assertFalse(json.loads((self.config.evidence_dir / "gate_result.json").read_text())["passed"])

    def test_command_timeout_terminates_only_the_launched_fixture_process(self):
        directory = self.root / "command-evidence"
        directory.mkdir()
        result = gate._command("blocked_fixture", [sys.executable, "-c", "import time;print('fixture',flush=True);time.sleep(20)"],
                               self.root, os.environ.copy(), directory, .3)
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["pid"], os.getpid())
        self.assertIsInstance(result["exit_code"], int)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(result["status"], "failed")

    def test_final_cannot_override_dynamic_suite_or_disable_native_requirement(self):
        release = self.root / "release"
        shutil.copytree(self.dev, release)
        result = gate.run_gate(replace(self.config, mode="final", release_root=release,
                                        suite_module="dpf_agent.tests.other", native="off"))
        self.assertFalse(result["passed"])
        self.assertEqual(self.step(result, "offline_suite_contract")["status"], "failed")
        self.assertTrue(self.step(result, "native_com")["required"])

    def native_fixture(self):
        evidence = self.root / "contract-evidence"
        evidence.mkdir()
        raw = evidence / "unit_only_artifact.txt"
        raw.write_text("Synthetic JSON contract fixture; no native engine was executed.\n")
        request = {"schema_version": 1, "run_id": "unit-run", "source_manifest_sha256": "unit-source-hash",
                   "versions": {"backend": "unit"}}
        result = {**request, "passed": True, "native_executed": True,
                  "engine": {"backend": "OpenDSS COM", "version": "synthetic-unit-contract"},
                  "cases": [{"task_id": "unit-task", "status": "success", "verification_passed": True,
                             "registered_solve_count": 2, "evidence_files": [str(raw)]}]}
        return evidence, request, result

    def test_native_contract_rejects_stale_identity_and_insufficient_verification(self):
        evidence, request, valid_fixture = self.native_fixture()
        variants = [dict(run_id="older-run"), dict(source_manifest_sha256="older-source"),
                    dict(native_executed=False), dict(cases=[]), dict(versions={"backend": "old"}),
                    dict(engine={"backend": "OpenDSS CLI", "version": "unit"})]
        for case_field, value in (("verification_passed", False), ("registered_solve_count", 1),
                                  ("registered_solve_count", True), ("status", "validation_failed")):
            cases = copy.deepcopy(valid_fixture["cases"])
            cases[0][case_field] = value
            variants.append({"cases": cases})
        path = evidence / "native_result.json"
        for override in variants:
            with self.subTest(override=override):
                path.write_text(json.dumps({**valid_fixture, **override}))
                with self.assertRaises(ValueError):
                    gate._validate_native_result(path, request, evidence)

    def test_native_contract_accepts_complete_current_identity_and_hashes_actual_file(self):
        evidence, request, fixture = self.native_fixture()
        path = evidence / "native_result.json"
        path.write_text(json.dumps(fixture))
        checked = gate._validate_native_result(path, request, evidence)
        self.assertEqual(checked["case_count"], 1)
        self.assertEqual(checked["evidence_files"][0]["sha256"], gate._sha256(evidence / "unit_only_artifact.txt"))
        self.assertEqual(checked["engine"]["version"], "synthetic-unit-contract")

    def test_native_contract_rejects_artifacts_outside_isolated_gate_directory(self):
        evidence, request, fixture = self.native_fixture()
        outside = self.root / "outside.txt"
        outside.write_text("unit fixture, not historical user data")
        fixture["cases"][0]["evidence_files"] = [str(outside)]
        path = evidence / "native_result.json"
        path.write_text(json.dumps(fixture))
        with self.assertRaisesRegex(ValueError, "isolated"):
            gate._validate_native_result(path, request, evidence)

    def test_cli_failure_returns_nonzero_and_writes_structured_result(self):
        self._write("dpf_agent/dpf_agent/tests/run_all.py", "raise SystemExit(3)\n")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = gate.main(["--dev-root", str(self.dev), "--evidence-dir", str(self.config.evidence_dir),
                              "--node", sys.executable])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["passed"])
        self.assertEqual(json.loads((self.config.evidence_dir / "gate_result.json").read_text())["status"], "failed")


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReleaseGateTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
