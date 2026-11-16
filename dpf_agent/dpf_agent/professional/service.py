"""Independent professional service; engineering capabilities remain gated.

Copyright (c) yangshaopeng. No legacy web/chat module is imported here.
"""
from __future__ import annotations

import csv
from dataclasses import replace
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from .. import ADAPTER_VERSION
from ..application.service import ApplicationService, TaskBusyError
from ..config import AgentConfig
from ..runtime import cancellation
from ..schemas.enums import OutputFormat, ResultScope
from ..schemas.task_spec import PowerFlowTaskSpec, SolverConfig, UserConstraints
from .assessment import evaluate_assessment
from .contracts import ContractError, RunRequest, SUPPORTED_SCHEMA_VERSIONS
from .jobs import JobStore, TERMINAL
from .observations import build_professional_observations
from .models import ModelStore, _inspect
from .device_semantics import inspect_device_semantics
from .device_verification import verify_device_connections


PROFESSIONAL_VERSION = "0.14.4-diagnostics"
REGISTERED_CASES = ("case4", "case_tx", "case_broken", "case_nonconv")
ARTIFACTS = ("result.json", "report.html", "checks.csv")
OBSERVATION_ARTIFACTS = ("observations.json", "observations.csv")


def requested_artifacts(result):
    formats = result["request"]["output_formats"]
    names = (ARTIFACTS + (OBSERVATION_ARTIFACTS if "professional_observations" in result else ())
             + (("engineering.csv",) if "engineering_analysis" in result else ()))
    return [name for name in names if name.rsplit(".", 1)[1] in formats]


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class ProfessionalService:
    """One owned worker and one SQLite ledger per explicit state directory.

    Accepts immutable native DSS or strictly converted balanced MATPOWER inputs.
    Numerical observations remain separate from engineering approval and Harness.
    """
    def __init__(self, state_dir, cases_root=None, *, config=None, start_worker=True,
                 allowed_cases=REGISTERED_CASES):
        base = config or AgentConfig()
        self.root = Path(state_dir).resolve()
        self.cases_root = Path(cases_root or base.cases_root).resolve()
        self.allowed_cases = tuple(allowed_cases)
        self.config = replace(base, workspace_root=str(self.root / "runtime" / "tasks"),
                              outputs_root=str(self.root / "runtime" / "outputs"),
                              cases_root=str(self.cases_root), engine_backend="com", com_isolation="process",
                              llm_enabled=False, rag_enabled=False, power_rag_enabled=False,
                              solve_cache_enabled=False)
        self.store = JobStore(self.root)
        self.store.acquire_owner()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._dispatch_lock = threading.Lock()
        self._pdf_lock = threading.Lock()
        try:
            self.store.recover()
            self.model_store = ModelStore(self.root / "models")
            from .studies import StudyManager
            self.studies = StudyManager(self, False)
            if start_worker:
                self._thread = threading.Thread(target=self._loop, name="professional-worker", daemon=True)
                self._thread.start()
                self.studies.start()
        except BaseException:
            self.store.release_owner()
            raise

    def capabilities(self):
        return {"service":"tianshu-professional", "version":PROFESSIONAL_VERSION,
                "legacy_adapter_version":ADAPTER_VERSION, "schema_version":"1.0",
                "supported_request_schemas":sorted(SUPPORTED_SCHEMA_VERSIONS),
                "stage":"P3_project_engineering_rules_and_reports", "engineering_ready":False, "harness_connected":False,
                "solver":{"engine":"OpenDSS", "transport":"com", "isolation":"process", "health":"not_probed"},
                "input":"registered_dss_imported_dss_balanced_matpower_or_explicit_phase_grid_tables", "scenario":"existing_snapshot_only",
                "operating_conditions":"schema_1.1_declared_frequency_check_only",
                "model_import":"static_native_dss_literal_matpower_and_explicit_phase_grid_tables",
                "device_semantics":"versioned_explicit_declarations_no_engine_default_inference",
                "ordered_device_replay":"linecode_phases_implicit_source_and_explicit_winding_ratings_bounded_subset",
                "power_operating_points":"load_generator_configuration_and_pv_native_cim_mode_applied_fixed_point",
                "snapshot_state_verification":"source_and_initial_native_globals_devices_open_conductors_frozen",
                "passive_electrical_state":"native_Yprim_and_configuration_text_frozen_no_full_source_matrix_equivalence_claim",
                "pv_fixed_point_scope":"fixed_pf_or_kvar_default_priorities_no_positive_q_ramps_or_indeterminate_inverter_state",
                "device_runtime_verification":"imported_snapshots_pre_solve_each_solve_and_publication_supported_fields_only",
                "nominal_voltage_readback":"load_generator_typed_double_pv_explicit_six_digit_consistency",
                "disabled_pce_verification":"explicit_source_false_and_native_false_only_no_electrical_node_order_claim",
                "recovery":"none", "engineering_observations":"strict_native_nodes_and_terminal_conductors",
                "power_conversion_observations":"system_scope_required_load_generator_pvsystem_inventory_and_terminal_vi_pq",
                "domain_observations":"requested_native_bus_line_transformer_inventory_and_auxiliary_terminal_physics",
                "matpower_phase_scope":"balanced_equivalent_no_physical_phase_measurements",
                "engineering_rules_connected":True, "supported_criteria_schemas":["legacy_exact_measurement_rules", "2.0"],
                "engineering_rule_scope":"project_confirmed_voltage_connections_terminal_conductor_current_terminal_kva_and_passive_active_loss",
                "reference_comparison":{"single_max_points":10000, "collection_max_parts":8,
                    "collection_max_total_points":20000, "source_recognition":"caller_declared_only"},
                "report_bundle":{"format":"zip", "complete_artifacts_required":6,
                    "max_file_bytes":128 * 1024 * 1024, "max_total_bytes":256 * 1024 * 1024,
                    "source":"verified_committed_bytes_only", "regenerates_results":False},
                "three_phase_engineering_acceptance":"not_globally_approved_project_reference_recognition_is_external",
                "private_model_readiness":"not_verified", "artifacts":list(ARTIFACTS + OBSERVATION_ARTIFACTS + ("engineering.csv",)),
                "extensions":{"grid_tables":{"formats":["CSV","JSON"],"schema":"tianzhi-grid/1.0","physical_phase_nodes":"explicit_only"},
                    "topology":"declared_bus_supply_connectivity_and_partial_phase_open_flags",
                    "studies":{"mode":"quasistatic_fixed_controls","max_points":1000,"automatic_replay":False,"dynamic_controls":False},
                    "scenario_comparison":"read_only_matching_units_bases_and_selectors",
                    "pdf":"verified_committed_html_read_only_headless_chromium"},
                "model_catalog":{"endpoint":"/v1/models", "included":False,
                                 "reason":"capability_queries_do_not_scan_or_verify_model_packages"}}

    def models(self):
        registered = [{"case_id":name, "format":"OpenDSS", "engineering_accepted":False}
                      for name in self.allowed_cases if (self.cases_root / name / "Master.dss").is_file()]
        return registered + self.model_store.list_models()

    def import_model(self, payload):
        with self._dispatch_lock:
            if self._stop.is_set():
                raise RuntimeError("Service is closing")
            return self.model_store.import_payload(payload)

    def inspect_model(self, snapshot_id):
        return self.model_store.inspect(snapshot_id)

    def export_report_pdf(self, task_id):
        from .pdf_export import export_pdf
        with self._pdf_lock:
            return export_pdf(self, task_id)

    def export_study_pdf(self, study_id):
        from .pdf_export import export_pdf
        with self._pdf_lock:
            return export_pdf(self, study_id, study=True)

    def compare_scenarios(self, left_task_id, right_task_id):
        from .scenario_comparison import compare_scenarios
        return compare_scenarios(self.result(left_task_id), self.result(right_task_id))

    def submit_study(self, payload):
        return self.studies.submit(payload)

    def get_study(self, study_id):
        return self.studies.get(study_id)

    def cancel_study(self, study_id):
        return self.studies.cancel(study_id)

    def compare_reference(self, task_id, reference):
        """Read a committed result and compare explicit frozen reference values.

        Does not recalculate, modify the result, or certify the reference source.
        """
        from .reference_comparison import compare_engineering_reference
        result = self.result(task_id)
        if result is None:
            raise ContractError("task_id", "reference comparison requires a committed result")
        return compare_engineering_reference(result, reference)

    def compare_reference_collection(self, task_id, collection):
        """Read-only bounded union comparison; no task/reference mutation."""
        from .reference_comparison import compare_engineering_reference_collection
        result = self.result(task_id)
        if result is None:
            raise ContractError("task_id", "reference comparison requires a committed result")
        return compare_engineering_reference_collection(result, collection)

    def _freeze(self, request, task_id):
        case_id = request.model.case_id
        snapshot_id = request.model.snapshot_id
        imported_manifest = None
        if snapshot_id is not None:
            if request.schema_version != "1.1":
                raise ContractError("schema_version", "imported snapshots require explicit operating conditions in schema 1.1", "capability_unavailable")
            source, imported_manifest = self.model_store.resolve(snapshot_id)
            topology=self.model_store.inspect(snapshot_id)["topology"]
            if topology["status"] in {"islands_detected", "no_active_source"}:
                raise ContractError("model.topology", "source-disconnected buses: " + ", ".join(topology["isolated_buses"][:30]), "island_detected")
            master_file = imported_manifest["master_file"]
            approved_root = (self.root / "models").resolve()
        else:
            if case_id not in self.allowed_cases:
                raise ContractError("model.case_id", "case is not registered for this service", "capability_unavailable")
            source = self.cases_root / case_id
            master_file = "Master.dss"
            approved_root = self.cases_root
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise ContractError("model.case_id", "linked model directories are not accepted")
        if not source.is_dir() or approved_root not in source.resolve().parents:
            raise ContractError("model.case_id", "registered model directory is missing")
        target = self.root / "snapshots" / task_id / "model"
        target.mkdir(parents=True, exist_ok=False)
        entries, size = [], 0
        for path in sorted(source.rglob("*")):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ContractError("model", "linked model dependencies are not accepted")
            if path.is_dir():
                continue
            if source.resolve() not in path.resolve().parents:
                raise ContractError("model", "dependency leaves the registered model")
            if path.stat().st_size + size > 20 * 1024 * 1024 or len(entries) >= 500:
                raise ContractError("model", "P1 model package exceeds the declared budget")
            data = path.read_bytes()
            size += len(data)
            if size > 20 * 1024 * 1024 or len(entries) >= 500:
                raise ContractError("model", "P1 model package exceeds the declared budget")
            if path.suffix.lower() == ".dss" and imported_manifest is None:
                try:
                    code = re.sub(r"(?m)!.*$|//.*$", "", data.decode("utf-8-sig"))
                except UnicodeError:
                    raise ContractError("model", "P1 DSS files must use UTF-8") from None
                if re.search(r"(?i)\bregistryupdate\b", code):
                    raise ContractError("model", "model cannot change engine registry persistence", "capability_unavailable")
                if re.search(r"(?im)^\s*set\s+controliterations\s*=|\b(?:storage|regcontrol|invcontrol|capcontrol|swtcontrol)\s*[.]", code):
                    raise ContractError("model", "unapproved control/device semantics are outside P1", "capability_unavailable")
                for line in code.splitlines():
                    command = line.strip().split(maxsplit=1)
                    if command and command[0].lower() not in {"clear", "new", "edit", "~", "set", "calcvoltagebases", "redirect", "compile", "buscoords"}:
                        raise ContractError("model", "DSS command is outside the registered P1 snapshot policy", "capability_unavailable")
                if re.search(r"(?i)\b(?:mode\s*=\s*(?!snapshot\b)[A-Za-z0-9]+|controlmode\s*=\s*(?!off\b)[A-Za-z0-9]+)", code):
                    raise ContractError("model", "only snapshot with controlmode=off is supported in P1", "capability_unavailable")
            rel = path.relative_to(source).as_posix()
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ContractError("model", "input changed during snapshot creation")
            entries.append({"path":rel, "sha256":digest, "bytes":len(data)})
        if not (target / master_file).is_file():
            raise ContractError("model", "declared master DSS file is missing")
        if imported_manifest is not None:
            expected = {e["path"]:(e["sha256"], e["bytes"]) for e in imported_manifest["entries"]}
            actual = {e["path"]:(e["sha256"], e["bytes"]) for e in entries}
            if actual != expected:
                raise ContractError("model", "imported model changed while freezing the job", "model_integrity_error")
            self.model_store.inspect(snapshot_id)
        fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        record = {"case_id":case_id, "scenario":request.scenario.to_dict(), "fingerprint":fingerprint,
                  "master":master_file, "entries":entries, "source_modified":False}
        if imported_manifest is not None:
            record.update(snapshot_id=snapshot_id, imported_model_fingerprint=imported_manifest["fingerprint"],
                          input_inspection=imported_manifest["inspection"],
                          device_semantics=inspect_device_semantics(imported_manifest["inspection"]), topology=topology)
            converted=self.model_store.inspect(snapshot_id).get("conversion")
            if converted is not None:
                balanced=converted.get("source_format", "MATPOWER") == "MATPOWER"
                record.update(source_format=converted.get("source_format", "MATPOWER"), conversion=converted, balanced_equivalent=balanced,
                    phase_provenance="synthetic_three_phase_positive_sequence_embedding" if balanced else "explicit_input_bus_nodes",
                    physical_phase_mapping=not balanced)
        atomic_text(target.parent / "manifest.json", json.dumps(record, ensure_ascii=False, indent=2))
        return record

    def _check_snapshot(self, row):
        record = json.loads(row["snapshot_json"])
        root = self.root / "snapshots" / row["task_id"] / "model"
        expected = {entry["path"] for entry in record["entries"]}
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Frozen snapshot contains a link")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != expected:
            raise ValueError("Frozen snapshot file set changed")
        for entry in record["entries"]:
            path = root / entry["path"]
            if root.resolve() not in path.resolve().parents or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Frozen snapshot content changed")
        return root / record["master"]

    def submit(self, payload):
        if self._stop.is_set():
            raise RuntimeError("Service is closing")
        request = payload if isinstance(payload, RunRequest) else RunRequest.from_dict(payload)
        if request.recovery.policy != "none":
            raise ContractError("recovery.policy", "numeric recovery contract is not connected in P1", "capability_unavailable")
        with self._dispatch_lock:
            if self._stop.is_set():
                raise RuntimeError("Service is closing")
            row, created = self.store.submit(request, lambda task_id:self._freeze(request, task_id))
        self._wake.set()
        return {**self.get_task(row["task_id"]), "created":created}

    def get_task(self, task_id):
        row = self.store.get(task_id)
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return {"task_id":task_id, "request_id":row["request_id"], "attempt_id":row["attempt_id"],
                "execution_status":row["status"], "cancel_requested":bool(row["cancel_requested"]),
                "numerical_validity":result["numerical_validity"] if result else "not_run",
                "engineering_assessment":result["assessment"]["status"] if result else "not_assessed",
                "error":row["error"] or (result.get("error") if result else None),
                "created_at":row["created_at"], "updated_at":row["updated_at"],
                "artifacts":requested_artifacts(result) if result else []}

    def result(self, task_id):
        row = self.store.get(task_id)
        return json.loads(row["result_json"]) if row["result_json"] else None

    def events(self, task_id, after=0):
        rows = self.store.events(task_id, after)
        return {"task_id":task_id, "events":rows, "cursor":rows[-1]["seq"] if rows else after}

    def cancel(self, task_id):
        row = self.store.cancel(task_id)
        if row["status"] == "cancelling":
            cancellation.request_cancel(task_id)
        self._wake.set()
        return {**self.get_task(task_id), "confirmed_stopped":row["status"] in TERMINAL}

    def _execute(self, row):
        task_id = row["task_id"]
        request = RunRequest.from_dict(json.loads(row["request_json"]))
        master = self._check_snapshot(row)
        # Derive from the verified, task-owned native bytes, not a caller's
        # expected-values object or mutable global imported-model directory.
        device_declarations = None
        if request.model.snapshot_id is not None:
            snapshot = json.loads(row["snapshot_json"])
            files = {item["path"]: (master.parent / item["path"]).read_bytes() for item in snapshot["entries"]}
            device_declarations = inspect_device_semantics(_inspect(snapshot["master"], files))
            from .source_state import inspect_source_state
            device_declarations["snapshot_state"] = inspect_source_state(_inspect(snapshot["master"], files), files)
            device_declarations["passive_stamp_required"] = True
            device_declarations["pv_fixed_points_required"] = True
            from .pv_points import inspect_pv_limits
            device_declarations["pv_limit_declarations"] = inspect_pv_limits(_inspect(snapshot["master"], files))
            if device_declarations["status"] == "invalid":
                raise ContractError("model.device_semantics", "frozen native device declarations are contradictory")
        hard_constraints = {"max_iterations":request.solver.max_iterations,
                            "tolerance":request.solver.tolerance,
                            "result_scopes":list(request.result_scopes)}
        if request.operating_conditions is not None:
            hard_constraints["expected_frequency_hz"] = request.operating_conditions.frequency_hz
        if device_declarations is not None:
            hard_constraints["expected_device_semantics"] = device_declarations
        spec = PowerFlowTaskSpec(task_id=task_id, case_ref=str(master),
                                scenario_ref=request.scenario.scenario_id,
                                user_goal="对已冻结原生DSS模型进行指定snapshot求解与数值验证",
                                solver_config=SolverConfig(max_iterations=request.solver.max_iterations,
                                                           tolerance=request.solver.tolerance),
                                result_scopes=[ResultScope(s) for s in request.result_scopes],
                                output_formats=[OutputFormat.JSON],
                                constraints=UserConstraints(hard=hard_constraints))
        if self.store.get(task_id)["cancel_requested"]:
            cancellation.request_cancel(task_id)
        service = ApplicationService(self.config, structured_only=True)
        response = service.run_structured(spec, progress_callback=lambda stage,label,pct:
                                         self.store.event(task_id, "stage", {"stage":stage,"label":label,"percent":pct,"attempt_id":row["attempt_id"]}))
        legacy_status = response["status"]
        verification = response.get("verification")
        valid = legacy_status in {"success", "partial"} and bool(verification and verification.get("passed"))
        numerical = "converged_valid" if valid else ("non_converged" if legacy_status == "non_converged" else "validation_failed" if legacy_status in {"validation_failed", "success", "partial"} else "not_run")
        status = "finished" if legacy_status in {"success", "partial", "non_converged", "validation_failed"} else "cancelled" if legacy_status == "cancelled" else "failed"
        canonical = None
        result_ref = response.get("result_ref")
        if result_ref and Path(result_ref).is_file():
            canonical = json.loads(Path(result_ref).read_text(encoding="utf-8"))
        identity_error = None
        if valid:
            validation_ref = response.get("artifacts", {}).get("validation.json")
            loaded_case = {}
            if validation_ref and Path(validation_ref).is_file():
                loaded_case = json.loads(Path(validation_ref).read_text(encoding="utf-8")).get("case", {})
            if (not isinstance(canonical, dict) or canonical.get("task_id") != task_id
                    or verification.get("task_id") != task_id
                    or canonical.get("solver_status", {}).get("converged") is not True
                    or canonical.get("solver_status", {}).get("error_flag", False)
                    or not loaded_case.get("case_fingerprint")
                    or canonical.get("case_fingerprint") != loaded_case.get("case_fingerprint")):
                identity_error = "authoritative_result_or_verified_identity_missing"
            else:
                frozen = {entry["path"]:entry["sha256"] for entry in json.loads(row["snapshot_json"])["entries"]}
                actual_entries = loaded_case.get("dependency_manifest", {}).get("entries", [])
                if (not actual_entries or not isinstance(actual_entries, list)
                        or len({entry["path"] for entry in actual_entries}) != len(actual_entries)
                        or json.loads(row["snapshot_json"])["master"] not in {entry["path"] for entry in actual_entries}):
                    identity_error = "actual_numerical_input_manifest_missing_or_invalid"
                for entry in actual_entries:
                    if frozen.get(entry["path"]) != entry["sha256"]:
                        identity_error = "actual_numerical_input_differs_from_frozen_snapshot"
                        break
            if identity_error:
                valid, numerical = False, "validation_failed"
        result = {"schema_version":"1.0", "professional_version":PROFESSIONAL_VERSION,
                  "task_id":task_id, "attempt_id":row["attempt_id"], "request_id":request.request_id,
                  "request_digest":request.digest(), "input":json.loads(row["snapshot_json"]),
                  "observation_contract":{"power_conversion_elements":"1.0", "required":"system" in request.result_scopes},
                  "request":request.to_dict(), "execution_status":status, "numerical_validity":numerical,
                  "publishable":valid, "publishable_scope":"numerical_result_with_explicit_observation_coverage",
                  "engineering_publishable":False, "legacy_status":legacy_status,
                  "verification":verification, "solver_result":canonical,
                  "error":{"code":"RESULT_IDENTITY_INVALID", "message":identity_error} if identity_error else response.get("error"),
                  "hidden_model_calls":0, "model_recovery_allowed":False,
                  "engineering_ready":False,
                  "solver_result_scope":"legacy_normalized_diagnostics_not_engineering_phase_measurements",
                  "limitations":["严格节点/端口观测的覆盖与缺项单独记录，不是完整工程验收",
                                 "仅应用项目明确提供的判据；缺额定、基值或限值标未评估",
                                 "solver_result保留旧归一化诊断；节点及端口真实量见professional_observations",
                                 "实际求解频率、设备基频与引擎默认设置单独追溯，隐含默认不是项目确认条件",
                                 "原有仿真默认限值不作为本次工程判据",
                                 "公开与认可工程对照、新版Harness及离线流程仍待验"]}
        raw = {}
        raw_ref = response.get("artifacts", {}).get("raw_opendss_result.json")
        source_metadata = {"artifact":"raw_opendss_result.json", "available":False}
        expected_raw = self.root / "runtime" / "tasks" / task_id / "raw_opendss_result.json"
        if raw_ref and Path(raw_ref).resolve() == expected_raw and expected_raw.is_file():
            data = expected_raw.read_bytes()
            value = json.loads(data)
            if isinstance(value, dict):
                raw = value
                source_metadata.update(available=True, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        conditions = self._verify_operating_conditions(request, task_id, raw, response)
        result["operating_conditions_verification"] = conditions
        if request.operating_conditions is not None and conditions["status"] != "matched":
            result.update(publishable=False, numerical_validity="validation_failed" if valid else numerical)
            if valid:
                result["error"] = {"code":"OPERATING_CONDITION_NOT_VERIFIED",
                                   "message":"声明频率未与真实数值上下文核对一致，不发布有效结果"}
        # Build in the terminal transaction, after cancellation has been fenced.
        # Raw data is transient and never persisted as an unguarded valid result.
        result["_raw_observation_source"] = {"data":raw, "metadata":source_metadata}
        result["_device_declarations"] = device_declarations
        return status, result

    def _verify_devices(self, declarations, result, raw_source, *, required=False):
        if declarations is None:
            return {"version":"1.0", "status":"not_available" if required else "not_requested", "passed":False,
                    "scope":"imported_snapshot_missing_frozen_declarations" if required else "legacy_registered_case_without_new_device_gate",
                    "engineering_accepted":False, "publication_authorized":False}
        digest = hashlib.sha256(json.dumps(declarations, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        data = raw_source.get("data", {})
        solver = data.get("solver") if isinstance(data, dict) else None
        native = solver.get("device_connections") if isinstance(solver, dict) else None
        report = verify_device_connections(declarations, native)
        if "snapshot_state" in declarations and (not isinstance(native,dict) or native.get("snapshot_state_baseline") is None):
            report.update(passed=False,status="not_available")
            report.setdefault("issues",[]).append({"code":"final_frozen_state_baseline_missing", "message":"Final native state must retain the task-bound initial state."})
        if declarations.get("pv_fixed_points_required") is True and (not isinstance(native,dict) or native.get("pv_controls_baseline") is None):
            report.update(passed=False,status="not_available")
            report.setdefault("issues",[]).append({"code":"final_pv_control_baseline_missing", "message":"Final PV controls must retain the task-bound initial native mode evidence."})
        if declarations.get("passive_stamp_required") is True and (not isinstance(native,dict) or native.get("passive_stamp_baseline") is None):
            report.update(passed=False,status="not_available")
            report.setdefault("issues",[]).append({"code":"final_passive_stamp_baseline_missing", "message":"Final passive admittances must retain the task-bound initial native evidence."})
        final_available = bool(raw_source.get("metadata", {}).get("available") and isinstance(native, dict))
        evidence = "final_raw_result" if final_available else "pre_solve_or_missing"
        # Recompute comparisons from this task's owned evidence. Never trust
        # stored status/passed flags, and never replace a missing final reading
        # with an earlier successful one. A later rejected Solve overrides an
        # earlier raw result that happened to match.
        for filename, key in (("engine_process_diagnostics.json", "last_device_check"),
                              ("device_connections_verification.json", "check")):
            path = self.root / "runtime/tasks" / result["task_id"] / filename
            try:
                if (not path.is_file() or path.is_symlink() or path.resolve() != path
                        or getattr(path.parent, "is_junction", lambda:False)()
                        or path.stat().st_size > 32 * 1024 * 1024):
                    continue
                saved = json.loads(path.read_bytes())
            except (OSError, ValueError, UnicodeError, RecursionError):
                continue
            checked = saved.get(key) if isinstance(saved, dict) else None
            if (not isinstance(saved, dict) or saved.get("task_id") != result["task_id"]
                    or not isinstance(checked, dict) or checked.get("task_id") != result["task_id"]
                    or checked.get("declarations_sha256") != digest):
                continue
            earlier = verify_device_connections(declarations, checked.get("native_readback"))
            if not earlier["passed"]:
                report, evidence = earlier, filename
                break
            if not final_available:
                report["pre_solve_check"] = {"status":earlier["status"], "passed":True}
        report.update(task_id=result["task_id"], declarations_sha256=digest,
                      input_fingerprint=result["input"]["fingerprint"],
                      native_evidence=evidence,
                      source_file=raw_source.get("metadata", {}),
                      electrical_parameters_modified=False, publication_authorized=False)
        if not final_available:
            report["passed"] = False
            if report["status"] == "matched":
                report["status"] = "not_available"
                report.setdefault("issues", []).append({"code":"final_device_evidence_not_available",
                    "message":"最终结果的设备读回来源不可用，较早的匹配不能授权发布。"})
        return report

    def _verify_operating_conditions(self, request, task_id, raw, response):
        declaration = request.operating_conditions
        record = {"status":"not_declared", "expected_frequency_hz":None, "actual_frequency_hz":None,
                  "frequency_modified":False, "source":None, "declaration":None,
                  "scope":"actual_frequency_only_existing_model_values_unchanged"}
        if declaration is None:
            return record
        record.update(status="not_available", expected_frequency_hz=declaration.frequency_hz,
                      declaration=declaration.to_dict())
        solver = raw.get("solver", {})
        context = solver.get("frequency_context", {}) if isinstance(solver, dict) else {}
        actual = context.get("solution_frequency_hz") if isinstance(context, dict) else None
        sources = context.get("source", {}) if isinstance(context, dict) else {}
        source = sources.get("solution_frequency_hz") if isinstance(sources, dict) else None
        alias = solver.get("frequency_hz") if isinstance(solver, dict) else None

        def finite_positive(value):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            try:
                return math.isfinite(value) and value > 0
            except (ValueError, OverflowError):
                return False

        if (finite_positive(actual) and finite_positive(alias)
                and source == "OpenDSS.ActiveCircuit.Solution.Frequency"
                and math.isclose(actual, alias, rel_tol=0.0, abs_tol=1e-9)):
            record.update(actual_frequency_hz=actual, source=source,
                          status="matched" if math.isclose(actual, declaration.frequency_hz, rel_tol=0.0, abs_tol=1e-9) else "mismatch")
            return record
        # A pre-solve rejection may have no raw results. Keep the observed
        # native mismatch from this exact task, without inventing a frequency.
        reference = response.get("artifacts", {}).get("operating_conditions_verification.json")
        expected = self.root / "runtime" / "tasks" / task_id / "operating_conditions_verification.json"
        if reference and Path(reference).resolve() == expected and expected.is_file():
            value = json.loads(expected.read_text(encoding="utf-8"))
            frequency = value.get("frequency") if isinstance(value, dict) else None
            if isinstance(value, dict) and value.get("task_id") == task_id and isinstance(frequency, dict):
                actual = frequency.get("actual_frequency_hz")
                if (finite_positive(actual) and frequency.get("expected_frequency_hz") == declaration.frequency_hz
                        and frequency.get("source") == "OpenDSS.ActiveCircuit.Solution.Frequency"):
                    record.update(actual_frequency_hz=actual, source=frequency["source"],
                                  status="mismatch" if not math.isclose(actual, declaration.frequency_hz, rel_tol=0.0, abs_tol=1e-9) else "rejected")
        return record

    def _write_result(self, result):
        request = RunRequest.from_dict(result["request"])
        raw_source = result.pop("_raw_observation_source", None)
        if raw_source is None and "system" in request.result_scopes:
            raw_source = {"data": {}, "metadata": {"artifact": "raw_opendss_result.json", "available": False}}
        declarations = result.pop("_device_declarations", None)
        required = request.model.snapshot_id is not None
        check = self._verify_devices(declarations, result, raw_source or {}, required=required)
        result["device_connections_verification"] = check
        if declarations is not None:
            result["input"]["runtime_device_declarations"] = declarations
        if required:
            if result["publishable"] and not check["passed"]:
                result.update(publishable=False, numerical_validity="validation_failed",
                              error={"code":"DEVICE_CONNECTION_NOT_VERIFIED",
                                     "message":"设备/绕组声明未与最终真实数值上下文一致核对，不发布有效结果"})
            check["publication_authorized"] = bool(result["publishable"] and check["passed"])
        if raw_source is not None:
            from .topology import diagnose_native_topology
            topology=diagnose_native_topology(raw_source["data"])
            result["topology_diagnosis"]=topology
            if result["publishable"] and topology["status"] in {"islands_detected", "no_active_source"}:
                result.update(publishable=False,numerical_validity="validation_failed",error={"code":"ISLANDED_NETWORK","message":"存在与电源不连通的母线；详见topology_diagnosis，不作为有效全网潮流发布"})
            raw_solver = raw_source["data"].get("solver")
            if result["publishable"] and (not isinstance(raw_solver, dict)
                    or raw_solver.get("converged") is not True
                    or raw_solver.get("error_flag", False) is not False):
                result.update(publishable=False, numerical_validity="validation_failed",
                              error={"code":"RAW_OBSERVATION_SOLVER_NOT_VERIFIED",
                                     "message":"最终原生观测未证明求解收敛，不发布有效结果"})
            observations = build_professional_observations(
                raw_source["data"], task_id=result["task_id"],
                evidence_ref="raw_opendss_result.json", numerical_validity=result["numerical_validity"],
                require_power_conversion="system" in request.result_scopes,
            ).to_dict()
            pce = observations["coverage"]["power_conversion_elements"]
            from .domain_coverage import verify_domain_coverage
            domain_check = verify_domain_coverage(raw_source["data"], observations, request.result_scopes)
            result["domain_observations_verification"] = domain_check
            if result["publishable"] and not domain_check["passed"]:
                result.update(publishable=False, numerical_validity="validation_failed",
                              error={"code":"DOMAIN_OBSERVATIONS_NOT_VERIFIED",
                                     "message":"请求域库存、逐相端口或物理一致性检查失败，不发布有效结果"})
            if "system" in request.result_scopes and not pce.get("passed"):
                if result["publishable"]:
                    result.update(publishable=False, numerical_validity="validation_failed",
                                  error={"code":"POWER_CONVERSION_OBSERVATIONS_NOT_VERIFIED",
                                         "message":"负荷/电源端口观测或库存覆盖不完整，不发布有效结果"})
                observations = build_professional_observations(
                    raw_source["data"], task_id=result["task_id"], evidence_ref="raw_opendss_result.json",
                    numerical_validity=result["numerical_validity"], require_power_conversion=True).to_dict()
            if not result["publishable"]:
                observations["observations"] = []
                observations["coverage"]["measurement_count"] = 0
                observations["coverage"]["measurement_counts_by_metric"] = {}
                for facts in (observations["bus_nodes"], observations["element_terminals"]):
                    for fact in facts:
                        fact["numerically_valid"] = False
                        if fact.get("status") != "not_applicable":
                            fact["status"] = "diagnostic_only"
            observations["coverage"]["requested_domains"] = domain_check
            observations["coverage"]["all_network_coverage"] = bool(domain_check["passed"] and pce.get("passed")
                and {"bus","branch","transformer","system"}.issubset(request.result_scopes))
            observations["coverage"]["scope"] = "verified_requested_domains" if domain_check["passed"] else "raw_records_provided_only"
            if required:
                check["publication_authorized"] = bool(result["publishable"] and check["passed"])
            observations["provenance"]["source_file"] = raw_source["metadata"]
            if result["input"].get("balanced_equivalent") is True:
                result["solver_result_scope"]="balanced_equivalent_diagnostics_no_physical_phase_identity"
                if isinstance(result.get("solver_result"),dict):
                    result["solver_result"].update(phase_provenance="synthetic_three_phase_positive_sequence_embedding",physical_phase_mapping=False)
                observations["provenance"].update(balanced_equivalent=True, physical_phase_mapping=False,
                    phase_identity="synthetic_three_phase_positive_sequence_embedding",
                    source_format="MATPOWER", conversion=result["input"]["conversion"])
                # Retain computational nodes explicitly; never present these as
                # recovered physical phases from a positive-sequence model.
                observations["observations"] = []
                observations["coverage"].update(measurement_count=0, measurement_counts_by_metric={},
                    physical_phase_mapping=False, balanced_equivalent=True)
                for facts in (observations["bus_nodes"],observations["element_terminals"]):
                    for fact in facts:
                        fact["equivalent_phase_label"]=fact.get("phase")
                        fact["phase"]=None
                        fact["physical_phase_mapping"]=False
                        fact["phase_provenance"]="synthetic_three_phase_positive_sequence_embedding"
            observations["provenance"]["input_fingerprint"] = result["input"]["fingerprint"]
            observations["provenance"]["device_connections_verification"] = {
                key:check[key] for key in ("version", "status", "passed", "scope", "declarations_sha256", "coverage", "publication_authorized") if key in check}
            result["professional_observations"] = observations
        from .engineering import analyze_engineering
        result["engineering_analysis"], result["assessment"] = analyze_engineering(result, request.criteria)
        result["engineering_publishable"] = bool(result["publishable"] and
            any(i["status"] in {"pass", "violation"} for i in result["assessment"]["items"]))
        result["engineering_publishable_scope"] = "evaluated_project_checks_only_not_engineering_acceptance"
        target = self.root / "artifacts" / result["task_id"]
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        if "json" in request.output_formats:
            atomic_text(target / "result.json", payload)
        observation_package = result.get("professional_observations")
        if observation_package is not None:
            from .reporting import safe_csv_cell
            if "json" in request.output_formats:
                atomic_text(target / "observations.json", json.dumps(observation_package, ensure_ascii=False, allow_nan=False, indent=2))
            if "csv" in request.output_formats:
                stream = io.StringIO(newline="")
                writer = csv.writer(stream)
                fields = ["task_id", "solution_frequency_hz", "solution_frequency_status", "base_frequency_hz", "base_frequency_status",
                          "record_kind", "object_id", "element_class", "enabled", "conductor_role", "numerically_valid", "terminal", "conductor", "node", "phase", "status", "metadata_verified",
                          "bus_name", "voltage_mag_v", "voltage_angle_deg", "voltage_pu", "voltage_base_ln_kv",
                          "current_mag_a", "current_angle_deg", "p_kw", "q_kvar", "phase_provenance", "physical_phase_mapping", "equivalent_phase_label", "evidence_ref"]
                writer.writerow(fields)
                frequency = observation_package["provenance"]["frequency_context"]
                context = {"task_id":result["task_id"], "solution_frequency_hz":frequency.get("solution_frequency_hz"),
                           "solution_frequency_status":frequency.get("fields", {}).get("solution_frequency_hz", {}).get("status")}
                for row in observation_package["bus_nodes"]:
                    writer.writerow([safe_csv_cell({**row, **context, "record_kind":"bus_node"}.get(key)) for key in fields])
                for row in observation_package["element_terminals"]:
                    flattened = {**row, **context, "record_kind":row.get("record_kind", "terminal_conductor"),
                                 "voltage_mag_v":row.get("voltages_mag_v"), "voltage_angle_deg":row.get("voltages_angle_deg"),
                                 "current_mag_a":row.get("currents_mag_a"), "current_angle_deg":row.get("currents_angle_deg"),
                                 "p_kw":row.get("powers_kw"), "q_kvar":row.get("powers_kvar")}
                    writer.writerow([safe_csv_cell(flattened.get(key)) for key in fields])
                atomic_text(target / "observations.csv", stream.getvalue())
        from .reporting import render_assessment_outputs
        for name, text in render_assessment_outputs(result).items():
            if name.rsplit(".", 1)[1] in request.output_formats:
                atomic_text(target / name, text)
        return {name:{"sha256":hashlib.sha256((target / name).read_bytes()).hexdigest(),
                      "bytes":(target / name).stat().st_size}
                for name in requested_artifacts(result)}

    def _verified_artifact(self, task_id, name):
        result = self.result(task_id)
        if result is None or name not in requested_artifacts(result):
            raise KeyError(name)
        path = self.root / "artifacts" / task_id / name
        if (not path.is_file() or path.is_symlink() or path.resolve() != path
                or getattr(path.parent, "is_junction", lambda:False)()):
            raise KeyError(name)
        row = self.store.get(task_id)
        expected = json.loads(row["artifact_json"] or "{}").get(name)
        data = path.read_bytes()
        if (not expected or len(data) != expected["bytes"]
                or hashlib.sha256(data).hexdigest() != expected["sha256"]):
            raise KeyError("artifact_integrity_failed")
        return path, data

    def artifact(self, task_id, name):
        return self._verified_artifact(task_id, name)[0]

    def artifact_bytes(self, task_id, name):
        """Return the same bytes that were hashed, without a later file reopen."""
        return self._verified_artifact(task_id, name)[1]

    def export_report_bundle(self, task_id):
        """Export a complete offline report from committed bytes, never re-solve."""
        from .artifact_bundle import ARTIFACT_PURPOSES, ArtifactBundleError, build_artifact_bundle
        names = tuple(ARTIFACT_PURPOSES)
        result = self.result(task_id)
        if result is None:
            raise ContractError("task_id", "report bundle requires a committed result")
        if set(requested_artifacts(result)) != set(names):
            raise ContractError("output_formats", "complete report bundle requires committed HTML, JSON and CSV artifacts; missing artifacts are never regenerated")
        row = self.store.get(task_id)
        expected = json.loads(row["artifact_json"] or "{}")
        # Refuse excessive entries before reading them. Each subsequent read is
        # still checked against its committed length/hash by artifact_bytes.
        if (not isinstance(expected, dict) or set(expected) != set(names)
                or any(not isinstance(r, dict) or type(r.get("bytes")) is not int or not 0 <= r["bytes"] <= 128 * 1024 * 1024 for r in expected.values())
                or sum(r["bytes"] for r in expected.values()) > 256 * 1024 * 1024):
            raise ContractError("artifacts", "complete report bundle exceeds its contract or has incomplete artifact metadata")
        artifacts = {name:self.artifact_bytes(task_id, name) for name in names}
        try:
            return build_artifact_bundle(task_id, artifacts, expected_artifacts=expected,
                                         expected_professional_version=result["professional_version"])
        except ArtifactBundleError as exc:
            raise ContractError("artifacts", str(exc)) from None

    def _loop(self):
        while True:
            with self._dispatch_lock:
                if self._stop.is_set():
                    return
                row = self.store.claim()
            if row is None:
                self._wake.wait(.2)
                self._wake.clear()
                continue
            task_id = row["task_id"]
            try:
                status, result = self._execute(row)
                self.store.settle(task_id, status, result, writer=self._write_result)
            except TaskBusyError:
                # A legacy computation in the same process owns the numerical
                # lock. Do not resubmit silently or overwrite its progress.
                self.store.settle(task_id, "failed", error="numerical_resource_busy")
            except Exception as exc:
                self.store.settle(task_id, "failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                cancellation.clear(task_id)

    def close(self, timeout=20):
        if hasattr(self, "studies"):
            self.studies.close(timeout)
        with self._dispatch_lock:
            self._stop.set()
            with self.store.connect() as db:
                active = [r[0] for r in db.execute("SELECT task_id FROM jobs WHERE status IN ('running','cancelling')")]
        for task_id in active:
            self.cancel(task_id)
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError("Professional worker has not confirmed shutdown; owner lock retained")
        if self.store._owner is not None:
            self.store.recover()
        self.store.release_owner()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
