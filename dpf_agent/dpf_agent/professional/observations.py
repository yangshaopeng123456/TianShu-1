"""Strict OpenDSS raw-v2 observations without legacy scalar fallbacks.

Copyright (c) yangshaopeng. This module is pure and performs no engineering
limit assessment. Native node numbers never imply physical phase A/B/C.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
import re
from typing import Any, Mapping

from .contracts import ContractError, Measurement, NUMERICAL_VALIDITIES, _choice, _text


OBSERVATION_SCHEMA_VERSION = "2.0"
_REL_TOL = 1e-8
_ABS_TOL = 1e-8
_POWER_REL_TOL = 1e-7
_POWER_ABS_TOL_KW_KVAR = 1e-6
_BUS_VOLTAGE_ABS_TOL_V = 1e-6


@dataclass(frozen=True)
class ProfessionalObservations:
    task_id: str
    numerical_validity: str
    observations: tuple[Measurement, ...]
    bus_nodes: tuple[dict[str, Any], ...]
    element_terminals: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "task_id": self.task_id, "numerical_validity": self.numerical_validity,
            "engineering_status": "not_assessed",
            "observations": [value.to_dict() for value in self.observations],
            "bus_nodes": self.bus_nodes, "element_terminals": self.element_terminals,
            "issues": self.issues, "coverage": self.coverage, "provenance": self.provenance,
        }
        return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))


def _safe_text(value: Any, maximum: int = 1024) -> str | None:
    try:
        return _text(value, "raw_text", maximum)
    except ContractError:
        return None


def _numeric(value: Any, minimum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        return None
    return 0.0 if value == 0 else value


def _integer(value: Any, minimum: int, maximum: int) -> int | None:
    return value if type(value) is int and minimum <= value <= maximum else None


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)


def _angles_close(left: float, right: float) -> bool:
    return abs((left - right + 180.0) % 360.0 - 180.0) <= 1e-7


class _Builder:
    def __init__(self, raw, task_id, evidence_ref, numerical_validity, require_power_conversion=False):
        self.raw, self.task_id, self.evidence_ref = raw, task_id, evidence_ref
        self.numerical_validity = numerical_validity
        self.numerical_observations_allowed = numerical_validity == "converged_valid"
        self.measurements: list[Measurement] = []
        self.bus_nodes: list[dict[str, Any]] = []
        self.terminals: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()
        self.bus_index: dict[str, dict[str, Any]] = {}
        self.frequency_issues: list[dict[str, Any]] = []
        self.element_frequencies: list[dict[str, Any]] = []
        self.require_power_conversion = require_power_conversion
        self.power_conversion_inventory_verified = False
        self.power_conversion_expected_names: tuple[str, ...] = ()
        self.power_conversion_expected_counts: dict[str, int] = {}
        self.power_conversion_measurement_start = 0
        self.power_conversion_issue_start = 0
        self.power_conversion_terminal_start = 0
        self.power_conversion_scope_status = "not_requested"

    def power_conversion_inventory(self):
        """Bind the independent AllElementNames inventory to every PCE row.

        An empty observed list is meaningful only with a successful independent
        inventory. Missing old raw fields preserve the old coverage boundary;
        current collectors cannot omit both fields to impersonate old output.
        """
        manifest = self.raw.get("power_conversion_elements_coverage")
        values = self.raw.get("power_conversion_elements")
        self.power_conversion_measurement_start = len(self.measurements)
        self.power_conversion_issue_start = len(self.issues)
        self.power_conversion_terminal_start = len(self.terminals)
        collector = self.raw.get("collector_version")
        match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][A-Za-z0-9_.-]+)?", collector) if isinstance(collector, str) and len(collector) <= 128 else None
        modern = bool(match and tuple(int(value) for value in match.groups()) >= (1, 7, 8))
        if manifest is None and values is None:
            if self.require_power_conversion or modern:
                self.power_conversion_scope_status = "not_available"
                self.issue("power_conversion_collection_required_missing", "/power_conversion_elements_coverage")
            return False
        self.power_conversion_scope_status = "not_available"
        if _safe_text(collector, 128) is None:
            self.issue("power_conversion_collector_version_missing", "/collector_version")
        path = "/power_conversion_elements_coverage"
        if not isinstance(manifest, Mapping):
            self.issue("power_conversion_coverage_metadata_missing", path)
            return isinstance(values, (list, tuple))
        for key, expected in (("version", "1.0"), ("scope", "load_generator_pvsystem_terminal_observations"),
                              ("source", "OpenDSS.ActiveCircuit.AllElementNames"), ("errors", [])):
            self.expected(manifest, key, expected, path)
        if manifest.get("status") == "not_requested":
            if (self.require_power_conversion or values is not None
                    or type(manifest.get("expected_names")) is not list or manifest["expected_names"]
                    or len(self.issues) != self.power_conversion_issue_start):
                self.issue("power_conversion_not_requested_conflicts_with_contract", path)
            else:
                self.power_conversion_scope_status = "not_requested"
            return isinstance(values, (list, tuple))
        if manifest.get("status") != "available":
            self.issue("power_conversion_inventory_not_available", path + "/status")
        names = manifest.get("expected_names")
        expected_names_valid = type(names) is list and len(names) <= 100000
        folded = []
        if expected_names_valid:
            for name in names:
                clean = _safe_text(name, 256)
                if clean is None or not re.fullmatch(r"(?:load|generator|pvsystem)\.[^\s.]+", clean, re.I):
                    expected_names_valid = False
                    break
                folded.append(clean.casefold())
            if len(set(folded)) != len(folded):
                expected_names_valid = False
        if not expected_names_valid:
            self.issue("power_conversion_inventory_identity_invalid_or_duplicate", path + "/expected_names")
        else:
            self.power_conversion_expected_names = tuple(names)
        class_counts, count_sources = manifest.get("class_counts"), manifest.get("class_counts_source")
        classes = {"load", "generator", "pvsystem"}
        counts_valid = (isinstance(class_counts, Mapping) and set(class_counts) == classes
                        and isinstance(count_sources, Mapping) and set(count_sources) == classes)
        if counts_valid:
            expected_sources = {"load": "OpenDSS.ActiveCircuit.Loads.Count", "generator": "OpenDSS.ActiveCircuit.Generators.Count",
                                "pvsystem": "OpenDSS.ActiveCircuit.PVSystems.Count"}
            counts_valid = all(_integer(class_counts[kind], 0, 100000) is not None
                               and count_sources[kind] == expected_sources[kind] for kind in classes)
        if not counts_valid:
            self.issue("power_conversion_independent_class_counts_not_verified", path + "/class_counts")
        elif expected_names_valid:
            actual_classes = Counter(name.split(".", 1)[0] for name in folded)
            if any(class_counts[kind] != actual_classes[kind] for kind in classes):
                self.issue("power_conversion_inventory_class_count_mismatch", path + "/class_counts")
            else:
                self.power_conversion_expected_counts = dict(class_counts)
        if not isinstance(values, (list, tuple)) or len(values) > 100000:
            self.issue("power_conversion_collection_missing_or_excessive", "/power_conversion_elements")
            return False
        observed = []
        for index, value in enumerate(values):
            if not isinstance(value, Mapping) or _safe_text(value.get("name"), 256) is None:
                self.issue("power_conversion_record_identity_invalid", f"/power_conversion_elements/{index}")
                continue
            observed.append(value["name"].casefold())
        if (expected_names_valid and (Counter(observed) != Counter(folded)
                                     or len(observed) != len(values))):
            self.issue("power_conversion_inventory_records_mismatch", "/power_conversion_elements")
        if len(set(observed)) != len(observed):
            self.issue("power_conversion_record_identity_duplicate", "/power_conversion_elements")
        self.power_conversion_native_identity(values, folded if expected_names_valid else None)
        self.power_conversion_inventory_verified = len(self.issues) == self.power_conversion_issue_start
        return True

    def power_conversion_native_identity(self, values, expected_names):
        """Cross-check final native state and topology when its frame is present.

        Registered historical paths do not necessarily collect this independent
        frame. Presence is a commitment: a corrupt frame or unreadable Enabled
        value cannot silently disable the cross-check or justify a fake disabled
        summary for a live element. Connection/topology decide conductor roles;
        a self-consistent alternate raw projection cannot replace native facts.
        """
        solver = self.raw.get("solver")
        if not isinstance(solver, Mapping) or "device_connections" not in solver:
            return
        path = "/solver/device_connections"
        native = solver["device_connections"]
        if (not isinstance(native, Mapping) or native.get("schema_version") != "1.0"
                or native.get("source") != "OpenDSS.COM" or native.get("scope") != "read_only_native_device_connections"
                or native.get("status") not in ("available", "partial") or type(native.get("issues")) is not list
                or native["issues"]):
            self.issue("power_conversion_native_frame_not_trusted", path)
            return
        coverage, objects = native.get("coverage"), native.get("objects")
        inventory = coverage.get("element_names") if isinstance(coverage, Mapping) else None
        if (not isinstance(coverage, Mapping) or coverage.get("enumeration_status") != "available"
                or coverage.get("enumeration_source") != "OpenDSS.ActiveCircuit.AllElementNames"
                or type(inventory) is not list or len(inventory) > 100000
                or not isinstance(objects, list) or len(objects) > 100000
                or any(_safe_text(name, 256) is None or "." not in name or not name.split(".", 1)[1] for name in inventory)
                or len({name.casefold() for name in inventory}) != len(inventory)):
            self.issue("power_conversion_native_inventory_not_trusted", path + "/coverage")
            return
        all_names = {name.casefold() for name in inventory}
        native_names = {name for name in all_names if name.split(".", 1)[0] in {"load", "generator", "pvsystem"}}
        if expected_names is None or native_names != set(expected_names):
            self.issue("power_conversion_native_inventory_disagrees", path + "/coverage/element_names")
        native_rows, native_row_paths, seen = {}, {}, set()
        for index, row in enumerate(objects):
            row_path = path + f"/objects/{index}"
            name = _safe_text(row.get("requested_name"), 256) if isinstance(row, Mapping) else None
            if name is None or "." not in name or not name.split(".", 1)[1]:
                self.issue("power_conversion_native_row_identity_not_trusted", row_path)
                continue
            name = name.casefold()
            actual = _safe_text(row.get("name"), 256)
            if (name in seen or name not in all_names or actual is None or actual.casefold() != name
                    or row.get("identity_verified") is not True
                    or row.get("identity_source") != "OpenDSS.ActiveCircuit.ActiveCktElement.Name"
                    or row.get("class") != name.split(".", 1)[0] or row.get("status") not in ("available", "partial")
                    or type(row.get("issues")) is not list or row["issues"]):
                self.issue("power_conversion_native_row_identity_not_trusted", row_path, name)
            seen.add(name)
            if name in native_names:
                native_rows[name] = row
                native_row_paths[name] = row_path
        if set(native_rows) != native_names:
            self.issue("power_conversion_native_rows_incomplete", path + "/objects")
        for index, observed in enumerate(values):
            if not isinstance(observed, Mapping) or not isinstance(observed.get("name"), str):
                continue
            name = observed["name"].casefold()
            if name not in native_rows:
                self.issue("power_conversion_native_enabled_not_available", path + "/objects", name)
                continue
            row = native_rows[name]
            fields = row.get("fields")
            enabled = fields.get("enabled") if isinstance(fields, Mapping) else None
            if (not isinstance(enabled, Mapping) or enabled.get("status") != "available"
                    or enabled.get("source") != "OpenDSS.ActiveCircuit.ActiveCktElement.Enabled"
                    or type(enabled.get("value")) is not bool or type(enabled.get("issues")) is not list or enabled["issues"]):
                self.issue("power_conversion_native_enabled_not_available", path + "/objects", name)
                continue
            terminal = observed.get("terminal_data")
            actual_enabled = terminal.get("enabled") if isinstance(terminal, Mapping) else None
            if type(actual_enabled) is not bool or actual_enabled is not enabled["value"]:
                self.issue("power_conversion_enabled_disagrees_with_native", f"/power_conversion_elements/{index}/terminal_data/enabled", name)
            terminal = terminal if isinstance(terminal, Mapping) else {}
            connection = observed.get("connection")
            actual_connection = connection.get("value") if isinstance(connection, Mapping) else None
            ce_source = "OpenDSS.ActiveCircuit.ActiveCktElement."
            checks = [
                ("conn", ce_source + "Properties(conn).Val", actual_connection,
                 lambda value: isinstance(value, str) and value in {"wye", "delta"}),
                ("num_terminals", ce_source + "NumTerminals", terminal.get("num_terminals"),
                 lambda value: _integer(value, 1, 32) is not None),
                ("num_conductors", ce_source + "NumConductors", terminal.get("num_conductors"),
                 lambda value: _integer(value, 1, 64) is not None),
                ("num_phases", ce_source + "NumPhases", terminal.get("num_phases"),
                 lambda value: _integer(value, 1, 64) is not None),
                ("bus_names", ce_source + "BusNames", terminal.get("bus_names"),
                 lambda value: isinstance(value, (list, tuple)) and 1 <= len(value) <= 32
                 and all(_safe_text(item, 256) is not None for item in value)),
            ]
            if enabled["value"]:
                checks.append(("node_order", ce_source + "NodeOrder", terminal.get("node_order"),
                               lambda value: isinstance(value, (list, tuple)) and 1 <= len(value) <= 2048
                               and all(_integer(item, 0, 2147483647) is not None for item in value)))
            for key, source, actual_value, validator in checks:
                cell = fields.get(key)
                if (not isinstance(cell, Mapping) or cell.get("status") != "available" or cell.get("source") != source
                        or type(cell.get("issues")) is not list or cell["issues"] or not validator(cell.get("value"))):
                    self.issue("power_conversion_native_topology_not_available", native_row_paths[name] + "/fields/" + key, name)
                    continue
                expected_value = cell["value"]
                consistent = (tuple(actual_value) == tuple(expected_value) if key in {"bus_names", "node_order"}
                              and validator(actual_value) else actual_value == expected_value and validator(actual_value))
                if not consistent:
                    target = f"/power_conversion_elements/{index}/connection/value" if key == "conn" else f"/power_conversion_elements/{index}/terminal_data/{key}"
                    self.issue("power_conversion_topology_disagrees_with_native", target, name)

    def power_conversion_coverage(self):
        errors = [item for item in self.issues[self.power_conversion_issue_start:] if item["severity"] == "error"]
        requested = self.power_conversion_scope_status != "not_requested"
        passed = requested and self.power_conversion_inventory_verified and not errors
        if passed:
            self.power_conversion_scope_status = "complete"
        if requested and not passed:
            # New-scope omissions or corrupt rows cannot be hidden by other
            # valid PCEs, or by valid bus/line measurements from the old scope.
            del self.measurements[self.power_conversion_measurement_start:]
        return {
            "version": "1.0", "scope": "load_generator_pvsystem_terminal_observations",
            "status": self.power_conversion_scope_status, "passed": passed,
            "required": self.require_power_conversion, "inventory_verified": self.power_conversion_inventory_verified,
            "expected_count": len(self.power_conversion_expected_names),
            "expected_counts_by_class": self.power_conversion_expected_counts,
            "records_seen": self.counts["power_conversion_elements_seen"],
            "enabled_count": self.counts["power_conversion_enabled"],
            "disabled_count": self.counts["power_conversion_disabled"],
            "enabled_with_complete_metadata": self.counts["power_conversion_enabled_complete"],
            "terminal_conductor_records": self.counts["power_conversion_terminal_conductor_records"],
            "disabled_summary_records": self.counts["power_conversion_disabled_summary_records"],
            "measurement_count": len(self.measurements) - self.power_conversion_measurement_start,
            "counts_by_class": {kind: self.counts["power_conversion_" + kind] for kind in ("load", "generator", "pvsystem")},
            "actual_terminal_power_sign": "into_element", "configuration_setpoints_used_as_measurements": False,
            "engineering_status": "not_assessed",
        }

    def disabled_power_conversion(self, data, name, path, duplicate, connection, initial_errors):
        """Disabled devices are inventory facts, never zero-valued phasors.

        Actual OpenDSS does not expose usable NodeOrder/Voltages for this
        state. Validate the explicit skip contract and produce one summary;
        electrical getters must not be represented as successful zeros.
        """
        for key, expected in (("schema_version", "2.0"), ("status", "disabled"), ("identity_verified", True),
                              ("enabled", False), ("source", "OpenDSS.ActiveCktElement"), ("errors", []),
                              ("layout", "terminal_major_conductor_minor"), ("power_sign", "into_element"),
                              ("voltage_basis", "node_to_ground"), ("polar_derivation", "from_complex_hypot_atan2"),
                              ("reason", "disabled_device_no_electrical_observations")):
            self.expected(data, key, expected, path)
        requested = _safe_text(data.get("requested_element_name"), 256)
        active = _safe_text(data.get("active_element_name"), 256)
        if (requested is None or active is None or requested.casefold() != name.casefold()
                or active.casefold() != name.casefold()):
            self.issue("active_element_identity_mismatch", path, name)
        terminals = _integer(data.get("num_terminals"), 1, 32)
        conductors = _integer(data.get("num_conductors"), 1, 64)
        phases = _integer(data.get("num_phases"), 1, 3)
        if (terminals != 1 or conductors is None or phases is None
                or (connection == "wye" and conductors != phases + 1)
                or (connection == "delta" and conductors != (2 if phases == 1 else 3))):
            self.issue("power_conversion_terminal_dimensions_incompatible", path, name)
        buses = data.get("bus_names")
        buses_valid = isinstance(buses, (list, tuple)) and len(buses) == 1 and all(_safe_text(bus, 256) for bus in buses)
        if not buses_valid:
            self.issue("terminal_bus_identity_missing", path + "/bus_names", name)
        for key in ("node_order", "voltages_mag_v", "voltages_angle_deg", "currents_mag_a", "currents_angle_deg",
                    "powers_kw", "powers_kvar", "voltages_complex_v", "currents_complex_a", "powers_kw_kvar"):
            if key not in data or data[key] is not None:
                self.issue("disabled_power_conversion_electrical_data_not_absent", path + "/" + key, name)
        valid = (not duplicate and connection is not None and self.power_conversion_inventory_verified
                 and len(self.issues) == initial_errors)
        self.counts["power_conversion_disabled"] += 1
        self.counts["power_conversion_disabled_summary_records"] += 1
        if valid:
            self.counts["power_conversion_elements_with_complete_metadata"] += 1
        frequency = self.element_frequency(data, path, name)
        self.terminals.append({
            "record_kind": "disabled_element_summary", "object_id": name,
            "element_class": name.split(".", 1)[0].casefold(), "connection": connection,
            "status": "not_applicable", "enabled": False, "numerically_valid": False,
            "metadata_verified": valid, "terminal": None, "conductor": None, "source_index": None,
            "node": None, "phase": None, "bus_name": buses[0] if buses_valid else None,
            "evidence_ref": self.ref(path), "reason": "disabled_device_no_electrical_observations",
            "configuration_setpoints_used": False, "power_sign": "into_element", "voltage_basis": "node_to_ground",
            "current_basis": "terminal_conductor_rms", "power_basis": "terminal_conductor_complex_power",
            "winding_phase_current": "not_assessed", "thermal_assessment": "not_assessed",
            "base_frequency_hz": frequency["value_hz"], "base_frequency_source": frequency["source"],
            "base_frequency_status": frequency["status"],
            **{key: None for key in ("voltages_mag_v", "voltages_angle_deg", "currents_mag_a", "currents_angle_deg",
                                    "powers_kw", "powers_kvar", "voltages_complex_v", "currents_complex_a", "powers_kw_kvar")},
        })

    def issue(self, code, path, object_id=None, severity="error"):
        self.issues.append({"code": code, "raw_ref": self.ref(path),
                            "object_id": object_id, "severity": severity})

    def ref(self, path):
        return self.evidence_ref + "#" + path

    def frequency_warning(self, code, path, object_id=None):
        # Frequency provenance is additive for raw-v2 compatibility. Missing
        # context does not retroactively validate/invalidate existing V/I/PQ.
        self.frequency_issues.append({"code": code, "raw_ref": self.ref(path),
                                      "object_id": object_id, "severity": "warning"})

    def frequency_field(self, value, source, path, object_id=None):
        numeric = _numeric(value, 0.0)
        source = _safe_text(source, 1024)
        if numeric is None or numeric <= 0:
            self.frequency_warning("frequency_value_missing_or_invalid", path, object_id)
            numeric = None
        if source is None:
            self.frequency_warning("frequency_source_not_available", path, object_id)
        available = numeric is not None and source is not None
        return {"value_hz": numeric if available else None, "source": source,
                "status": "available" if available else "not_available"}

    def element_frequency(self, data, path, name):
        value = self.frequency_field(data.get("base_frequency_hz"), data.get("base_frequency_source"),
                                     path + "/base_frequency_hz", name)
        reported = data.get("base_frequency_status")
        errors = data.get("base_frequency_errors")
        if (reported != "available" or type(errors) is not list or errors):
            self.frequency_warning("element_base_frequency_read_context_not_available", path + "/base_frequency_status", name)
            value["status"] = "not_available"
        if value["source"] != "OpenDSS.ActiveCktElement.Properties(basefreq).Val":
            self.frequency_warning("element_base_frequency_source_not_verified", path + "/base_frequency_source", name)
            value["status"], value["value_hz"] = "not_available", None
        self.element_frequencies.append({"object_id": name, "evidence_ref": self.ref(path), **value})
        return value

    def frequency_context(self):
        solver = self.raw.get("solver")
        context = solver.get("frequency_context") if isinstance(solver, Mapping) else None
        if not isinstance(context, Mapping):
            self.frequency_warning("frequency_context_not_available", "/solver/frequency_context")
            context = {}
        sources = context.get("source")
        if not isinstance(sources, Mapping):
            sources = {}
        fields = {}
        keys = ("solution_frequency_hz", "default_base_frequency_before_compile_hz",
                "default_base_frequency_after_compile_hz")
        for key in keys:
            fields[key] = self.frequency_field(context.get(key), sources.get(key), "/solver/frequency_context/" + key)
        solution = fields["solution_frequency_hz"]
        if solution["source"] != "OpenDSS.ActiveCircuit.Solution.Frequency":
            self.frequency_warning("solution_frequency_source_not_verified", "/solver/frequency_context/source/solution_frequency_hz")
            solution["status"], solution["value_hz"] = "not_available", None
        scalar = _numeric(solver.get("frequency_hz"), 0.0) if isinstance(solver, Mapping) else None
        if (scalar is None or scalar <= 0 or (solution["value_hz"] is not None
                                               and not _close(scalar, solution["value_hz"]))):
            self.frequency_warning("solution_frequency_scalar_context_missing_or_inconsistent", "/solver/frequency_hz")
            solution["status"] = "not_available"
        complete = (context.get("schema_version") == "1.0" and context.get("status") == "available"
                    and type(context.get("errors")) is list and not context["errors"]
                    and all(value["status"] == "available" for value in fields.values())
                    and all(value["status"] == "available" for value in self.element_frequencies))
        if not complete:
            self.frequency_warning("frequency_context_incomplete", "/solver/frequency_context")
        result = {"schema_version": "1.0", "status": "available" if complete else "not_available",
                  **{key: value["value_hz"] for key, value in fields.items()},
                  "source": {key: value["source"] for key, value in fields.items()},
                  "fields": fields, "element_base_frequencies": self.element_frequencies,
                  "frequency_defaults_inferred": False, "frequency_modified": False,
                  "engineering_status": "not_assessed"}
        isolation = context.get("registry_isolation")
        if isinstance(isolation, Mapping):
            source = _safe_text(isolation.get("source"), 1024)
            verification_source = _safe_text(isolation.get("verification_source"), 1024)
            allowed = isolation.get("registry_updates_allowed")
            isolated = (isolation.get("status") == "enabled" and allowed is False
                        and source is not None and verification_source is not None
                        and type(isolation.get("errors")) is list and not isolation["errors"])
            configured = (isolation.get("status") == "configured" and allowed is False
                          and source is not None and type(isolation.get("errors")) is list)
            result["registry_isolation"] = {"status": "enabled" if isolated else "configured" if configured else "not_available",
                                             "registry_updates_allowed": allowed if type(allowed) is bool else None,
                                             "source": source, "verification_source": verification_source,
                                             "readback_confirmed": isolated}
            if not isolated:
                self.frequency_warning("registry_isolation_readback_not_available" if configured else "registry_isolation_context_not_verified",
                                       "/solver/frequency_context/registry_isolation")
        return result

    def array(self, record, key, path, count=None, minimum=None):
        value = record.get(key)
        if not isinstance(value, (list, tuple)):
            self.issue("required_numeric_array_missing", path + "/" + key)
            return None
        if count is not None and len(value) != count:
            self.issue("array_length_mismatch", path + "/" + key)
        result = tuple(_numeric(item, minimum) for item in value)
        if any(item is None for item in result):
            self.issue("numeric_value_invalid_or_non_finite", path + "/" + key)
        return result

    @staticmethod
    def complete(values, count):
        return values is not None and len(values) == count and all(value is not None for value in values)

    def expected(self, record, key, expected, path):
        if record.get(key) != expected or type(record.get(key)) is not type(expected):
            self.issue("required_metadata_missing_or_incompatible", path + "/" + key)
            return False
        return True

    def records(self, key):
        values = self.raw.get(key)
        if not isinstance(values, (list, tuple)):
            self.issue("raw_collection_missing", "/" + key)
            return ()
        names = [value.get("name") for value in values if isinstance(value, Mapping)]
        valid_names = [_safe_text(name, 256) for name in names]
        duplicates = {name for name, count in Counter(name.casefold() for name in valid_names if name).items() if count > 1}
        for index, value in enumerate(values):
            path = f"/{key}/{index}"
            if not isinstance(value, Mapping):
                self.issue("raw_record_not_object", path)
                continue
            name = _safe_text(value.get("name"), 256)
            if name is None:
                self.issue("object_identity_missing", path)
                continue
            duplicate = name.casefold() in duplicates
            if duplicate:
                self.issue("object_identity_ambiguous", path, name)
            yield value, name, path, duplicate

    def buses(self):
        for bus, name, path, duplicate in self.records("buses"):
            self.counts["buses_seen"] += 1
            initial_errors = len(self.issues)
            for key, expected in (
                ("observation_schema_version", "2.0"), ("observation_status", "available"),
                ("identity_verified", True), ("voltage_base_basis", "line_to_neutral"),
                ("voltage_basis", "node_to_ground"), ("base_source", "OpenDSS.ActiveBus.kVBase"),
                ("observation_errors", []),
            ):
                self.expected(bus, key, expected, path)
            requested = _safe_text(bus.get("requested_bus_name"), 256)
            if requested is None or requested.casefold() != name.casefold():
                self.issue("active_bus_identity_mismatch", path, name)
            count = _integer(bus.get("num_nodes"), 1, 100000)
            nodes = bus.get("nodes")
            nodes_valid = (count is not None and isinstance(nodes, (list, tuple))
                           and len(nodes) == count and all(_integer(node, 0, 2147483647) is not None for node in nodes)
                           and len(set(nodes)) == count)
            if not nodes_valid:
                self.issue("native_node_identity_missing_or_ambiguous", path + "/nodes", name)
            base = _numeric(bus.get("voltage_base_ln_kv"), 0.0)
            if base is None or base == 0:
                self.issue("explicit_line_to_neutral_voltage_base_missing", path, name)
                base = None
            magnitude = self.array(bus, "voltage_mag_v", path, count, 0.0)
            angle = self.array(bus, "voltage_angle_deg", path, count)
            pu = self.array(bus, "voltage_pu", path, count, 0.0)
            pu_angle = self.array(bus, "angle_deg", path, count)
            v_interleaved = self.array(bus, "vmag_angle_v", path, 2 * count if count else None)
            pu_interleaved = self.array(bus, "pu_vmag_angle", path, 2 * count if count else None)
            arrays_valid = count is not None and all(self.complete(value, size) for value, size in (
                (magnitude, count), (angle, count), (pu, count), (pu_angle, count),
                (v_interleaved, 2 * count), (pu_interleaved, 2 * count),
            ))
            if arrays_valid:
                for index in range(count):
                    if (not _close(magnitude[index], v_interleaved[2 * index])
                            or not _angles_close(angle[index], v_interleaved[2 * index + 1])
                            or not _close(pu[index], pu_interleaved[2 * index])
                            or not _angles_close(pu_angle[index], pu_interleaved[2 * index + 1])
                            or not _angles_close(angle[index], pu_angle[index])):
                        self.issue("voltage_representation_inconsistent", path, name)
                        break
                    if base is not None and not _close(magnitude[index] / (base * 1000.0), pu[index]):
                        self.issue("voltage_per_unit_base_inconsistent", path, name)
                        break
            valid = (not duplicate and nodes_valid and arrays_valid and base is not None
                     and len(self.issues) == initial_errors)
            if valid:
                self.counts["buses_with_complete_metadata"] += 1
                self.bus_index[name.casefold()] = {
                    "name": name,
                    "voltages": {node: complex(magnitude[index] * math.cos(math.radians(angle[index])),
                                                magnitude[index] * math.sin(math.radians(angle[index])))
                                 for index, node in enumerate(nodes)},
                }
            # Without native node identity, even an apparently valid vector
            # cannot be assigned to a physical or ordinal phase.
            if not nodes_valid:
                continue
            for index, node in enumerate(nodes):
                standard = node in (1, 2, 3)
                status = "available" if valid and standard else "not_applicable" if node == 0 else "not_assessed"
                if node not in (0, 1, 2, 3):
                    self.issue("nonstandard_node_has_no_phase_assignment", path + f"/nodes/{index}", name, "warning")
                record = {
                    "object_id": name, "node": node, "phase": str(node) if standard else None,
                    "status": status, "voltage_basis": "node_to_ground",
                    "voltage_mag_v": magnitude[index] if self.complete(magnitude, count) else None,
                    "voltage_angle_deg": angle[index] if self.complete(angle, count) else None,
                    "voltage_pu": pu[index] if self.complete(pu, count) else None,
                    "voltage_base_ln_kv": base, "evidence_ref": self.ref(path),
                    "metadata_verified": valid,
                }
                self.bus_nodes.append(record)
                self.counts["bus_node_records"] += 1
                self.counts["ground_node_records" if node == 0 else "nonstandard_node_records" if not standard else "standard_node_records"] += 1
                if valid and standard and self.numerical_observations_allowed:
                    self.measurements.append(Measurement(
                        object_id=name, metric="voltage", unit="pu", phase=str(node),
                        measurement_basis="phase_to_ground", value=pu[index],
                        evidence_ref=self.ref(path + f"/voltage_pu/{index}"),
                    ))

    def terminal_bus(self, bus_reference, path, name):
        """Resolve against verified bus identities; dots are never stripped blindly."""
        folded = bus_reference.casefold()
        if folded in self.bus_index:
            return self.bus_index[folded], ()
        for key in sorted(self.bus_index, key=len, reverse=True):
            if folded.startswith(key + "."):
                suffix = bus_reference[len(key) + 1:].split(".")
                if suffix and all(part.isascii() and part.isdecimal() for part in suffix):
                    values = tuple(int(part) for part in suffix)
                    if all(node <= 2147483647 for node in values):
                        return self.bus_index[key], values
                self.issue("terminal_bus_suffix_invalid", path, name)
                return None, None
        self.issue("verified_terminal_bus_record_missing", path, name)
        return None, None

    def check_terminal_bus_mapping(self, buses, nodes, arrays, terminals, conductors, phases, path, name, *, native_nodes_only=False):
        """Cross-check independent bus facts, native connection and same-node V.

        Node zero is the ground reference even when absent from Bus.Nodes.
        Default/padded ground conductors are validated against native NodeOrder;
        they are retained as ground facts, never materialized as phase results.
        """
        for terminal in range(terminals):
            bus, suffix = self.terminal_bus(buses[terminal], path + f"/bus_names/{terminal}", name)
            if bus is None:
                continue
            actual_nodes = tuple(nodes[terminal * conductors:(terminal + 1) * conductors])
            if native_nodes_only:
                # PCE defaults depend on connection (single-phase delta has
                # two live conductors). The trusted NodeOrder is authoritative
                # for unspecified positions; no phase/ground default is made.
                if suffix and actual_nodes[:min(conductors, len(suffix))] != suffix[:conductors]:
                    self.issue("terminal_explicit_bus_nodes_disagree_with_node_order", path + f"/bus_names/{terminal}", name)
            elif suffix:
                expected_nodes = list(range(1, phases + 1)) + [0] * (conductors - phases)
                # OpenDSS starts with default phase/ground connections, then
                # overrides the explicitly supplied conductor positions. It
                # ignores tokens beyond NumConductors; these do not create new
                # conductors (confirmed against the native COM interface).
                for conductor, node in enumerate(suffix[:conductors]):
                    expected_nodes[conductor] = node
                if actual_nodes != tuple(expected_nodes):
                    self.issue("terminal_explicit_bus_nodes_disagree_with_node_order", path + f"/bus_names/{terminal}", name)
            elif actual_nodes != tuple(list(range(1, phases + 1)) + [0] * (conductors - phases)):
                self.issue("terminal_default_bus_nodes_disagree_with_node_order", path + f"/bus_names/{terminal}", name)
            for conductor in range(conductors):
                index = terminal * conductors + conductor
                node = nodes[index]
                if node == 0:
                    expected_voltage = 0j
                elif node not in bus["voltages"]:
                    self.issue("terminal_node_missing_from_verified_bus", path + f"/node_order/{index}", name)
                    continue
                else:
                    expected_voltage = bus["voltages"][node]
                real, imaginary = arrays["voltages_complex_v"][2 * index:2 * index + 2]
                if (not math.isclose(real, expected_voltage.real, rel_tol=_REL_TOL, abs_tol=_BUS_VOLTAGE_ABS_TOL_V)
                        or not math.isclose(imaginary, expected_voltage.imag, rel_tol=_REL_TOL, abs_tol=_BUS_VOLTAGE_ABS_TOL_V)):
                    self.issue("terminal_voltage_disagrees_with_same_bus_node", path + f"/voltages_complex_v/{2 * index}", name)

    def elements(self, collection):
        power_conversion = collection == "power_conversion_elements"
        auxiliary = collection == "auxiliary_elements"
        for element, name, path, duplicate in self.records(collection):
            self.counts[collection + "_seen"] += 1
            element_class = name.split(".", 1)[0].casefold()
            expected_prefix = "line." if collection == "lines" else "transformer."
            identity_valid = (element_class in {"load", "generator", "pvsystem"} and element.get("class") == element_class
                              if power_conversion else element_class in {"vsource","capacitor","reactor"} and element.get("class")==element_class
                              if auxiliary else name.casefold().startswith(expected_prefix))
            if not identity_valid:
                self.issue("element_collection_identity_mismatch", path, name)
                duplicate = True
            if power_conversion and element_class in {"load", "generator", "pvsystem"}:
                self.counts["power_conversion_" + element_class] += 1
            data = element.get("terminal_data")
            if not isinstance(data, Mapping):
                self.issue("terminal_metadata_missing_no_scalar_fallback", path, name)
                continue
            path += "/terminal_data"
            initial_errors = len(self.issues)
            connection = None
            if power_conversion:
                connection_data = element.get("connection")
                if (not isinstance(connection_data, Mapping) or not isinstance(connection_data.get("value"), str)
                        or connection_data["value"] not in {"wye", "delta"}
                        or connection_data.get("status") != "available"
                        or connection_data.get("source") != "OpenDSS.ActiveCktElement.Properties(conn).Val"
                        or type(connection_data.get("errors")) is not list or connection_data["errors"]):
                    self.issue("power_conversion_connection_not_verified", path.rsplit("/", 1)[0] + "/connection", name)
                else:
                    connection = connection_data["value"]
                if data.get("enabled") is False:
                    self.disabled_power_conversion(data, name, path, duplicate, connection, initial_errors)
                    continue
            for key, expected in (
                ("schema_version", "2.0"), ("status", "available"), ("identity_verified", True),
                ("layout", "terminal_major_conductor_minor"), ("power_sign", "into_element"),
                ("voltage_basis", "node_to_ground"), ("source", "OpenDSS.ActiveCktElement"),
                ("polar_derivation", "from_complex_hypot_atan2"), ("errors", []),
            ):
                self.expected(data, key, expected, path)
            requested = _safe_text(data.get("requested_element_name"), 256)
            active = _safe_text(data.get("active_element_name"), 256)
            if (requested is None or active is None or requested.casefold() != name.casefold()
                    or active.casefold() != name.casefold()):
                self.issue("active_element_identity_mismatch", path, name)
            terminals = _integer(data.get("num_terminals"), 1, 32)
            conductors = _integer(data.get("num_conductors"), 1, 64)
            phases = _integer(data.get("num_phases"), 1, 64)
            count = terminals * conductors if terminals and conductors else None
            if count is None or phases is None or phases > conductors:
                self.issue("terminal_dimensions_invalid", path, name)
            if power_conversion and (terminals != 1 or phases not in {1, 2, 3}
                                     or (connection == "wye" and conductors != phases + 1)
                                     or (connection == "delta" and conductors != (2 if phases == 1 else 3))):
                self.issue("power_conversion_terminal_dimensions_incompatible", path, name)
            enabled = data.get("enabled")
            if type(enabled) is not bool:
                self.issue("enabled_state_missing", path, name)
            elif power_conversion:
                self.counts["power_conversion_enabled" if enabled else "power_conversion_disabled"] += 1
            buses = data.get("bus_names")
            buses_valid = (terminals is not None and isinstance(buses, (list, tuple))
                           and len(buses) == terminals and all(_safe_text(bus, 256) for bus in buses))
            if not buses_valid:
                self.issue("terminal_bus_identity_missing", path + "/bus_names", name)
            nodes = data.get("node_order")
            nodes_valid = (count is not None and isinstance(nodes, (list, tuple)) and len(nodes) == count
                           and all(_integer(node, 0, 2147483647) is not None for node in nodes))
            if not nodes_valid:
                self.issue("terminal_node_identity_missing", path + "/node_order", name)
            arrays = {}
            for key in ("voltages_mag_v", "voltages_angle_deg", "currents_mag_a", "currents_angle_deg", "powers_kw", "powers_kvar"):
                arrays[key] = self.array(data, key, path, count, 0.0 if key in ("voltages_mag_v", "currents_mag_a") else None)
            for key in ("voltages_complex_v", "currents_complex_a", "powers_kw_kvar"):
                arrays[key] = self.array(data, key, path, 2 * count if count else None)
            arrays_valid = count is not None and all(self.complete(value, 2 * count if key in (
                "voltages_complex_v", "currents_complex_a", "powers_kw_kvar") else count) for key, value in arrays.items())
            if arrays_valid:
                for index in range(count):
                    for prefix, complex_key, magnitude_key, angle_key in (
                        ("voltage", "voltages_complex_v", "voltages_mag_v", "voltages_angle_deg"),
                        ("current", "currents_complex_a", "currents_mag_a", "currents_angle_deg"),
                    ):
                        real, imag = arrays[complex_key][2 * index:2 * index + 2]
                        if (not _close(math.hypot(real, imag), arrays[magnitude_key][index])
                                or not _angles_close(math.degrees(math.atan2(imag, real)), arrays[angle_key][index])):
                            self.issue(prefix + "_complex_polar_inconsistent", path, name)
                    if (not _close(arrays["powers_kw_kvar"][2 * index], arrays["powers_kw"][index])
                            or not _close(arrays["powers_kw_kvar"][2 * index + 1], arrays["powers_kvar"][index])):
                        self.issue("power_representation_inconsistent", path, name)
                    vr, vi = arrays["voltages_complex_v"][2 * index:2 * index + 2]
                    ir, ii = arrays["currents_complex_a"][2 * index:2 * index + 2]
                    expected_p, expected_q = (vr * ir + vi * ii) / 1000.0, (vi * ir - vr * ii) / 1000.0
                    if (not math.isclose(expected_p, arrays["powers_kw"][index], rel_tol=_POWER_REL_TOL,
                                         abs_tol=_POWER_ABS_TOL_KW_KVAR)
                            or not math.isclose(expected_q, arrays["powers_kvar"][index], rel_tol=_POWER_REL_TOL,
                                                abs_tol=_POWER_ABS_TOL_KW_KVAR)):
                        self.issue("terminal_voltage_current_power_inconsistent", path + f"/powers_kw_kvar/{2 * index}", name)
            if nodes_valid and arrays_valid and buses_valid and phases is not None and phases <= conductors:
                self.check_terminal_bus_mapping(buses, nodes, arrays, terminals, conductors, phases, path, name,
                                                native_nodes_only=power_conversion or auxiliary)
            valid = (not duplicate and nodes_valid and arrays_valid and buses_valid and phases is not None
                     and len(self.issues) == initial_errors
                     and (not power_conversion or self.power_conversion_inventory_verified))
            frequency = self.element_frequency(data, path, name)
            if valid:
                self.counts[collection + "_with_complete_metadata"] += 1
            if not nodes_valid:
                continue
            for index, node in enumerate(nodes):
                terminal, conductor = divmod(index, conductors)
                neutral = power_conversion and connection == "wye" and conductor == conductors - 1
                terminal_nodes = nodes[terminal * conductors:(terminal + 1) * conductors]
                ambiguous = node != 0 and terminal_nodes.count(node) != 1
                standard = node in (1, 2, 3)
                if ambiguous:
                    self.issue("terminal_node_assignment_ambiguous", path + f"/node_order/{index}", name)
                if node not in (0, 1, 2, 3):
                    self.issue("nonstandard_node_has_no_phase_assignment", path + f"/node_order/{index}", name, "warning")
                status = ("not_applicable" if node == 0 or enabled is False or neutral else "available"
                          if valid and standard and not ambiguous else "not_assessed")
                record = {
                    "object_id": name, "terminal": terminal + 1, "conductor": conductor + 1,
                    "element_class":element_class, "enabled":enabled,
                    "numerically_valid":bool(valid and enabled is True and self.numerical_observations_allowed),
                    "source_index": index, "node": node,
                    "phase": str(node) if standard and not (power_conversion and (neutral or enabled is False)) else None,
                    "bus_name": buses[terminal] if buses_valid else None,
                    "status": status, "metadata_verified": valid,
                    "power_sign": "into_element", "voltage_basis": "node_to_ground",
                    "current_basis": "terminal_conductor_rms",
                    "power_basis": "terminal_conductor_complex_power",
                    "winding_phase_current": "not_assessed", "thermal_assessment": "not_assessed",
                    "evidence_ref": self.ref(path),
                    "base_frequency_hz": frequency["value_hz"],
                    "base_frequency_source": frequency["source"],
                    "base_frequency_status": frequency["status"],
                }
                if power_conversion:
                    record.update(element_class=element_class, connection=connection, enabled=enabled,
                                  conductor_role="neutral" if neutral else "ground" if node == 0 else "terminal_phase_conductor",
                                  configuration_setpoints_used=False)
                for key in ("voltages_mag_v", "voltages_angle_deg", "currents_mag_a", "currents_angle_deg", "powers_kw", "powers_kvar"):
                    record[key] = arrays[key][index] if self.complete(arrays[key], count) else None
                for key in ("voltages_complex_v", "currents_complex_a", "powers_kw_kvar"):
                    record[key] = arrays[key][2 * index:2 * index + 2] if self.complete(arrays[key], 2 * count) else None
                self.terminals.append(record)
                self.counts["terminal_conductor_records"] += 1
                if power_conversion:
                    self.counts["power_conversion_terminal_conductor_records"] += 1
                if status == "available" and self.numerical_observations_allowed:
                    self.measurements.append(Measurement(
                        object_id=name, metric="current", unit="A", phase=str(node), terminal=terminal + 1,
                        measurement_basis="terminal_conductor_rms", value=arrays["currents_mag_a"][index],
                        evidence_ref=self.ref(path + f"/currents_mag_a/{index}"),
                    ))
            if (power_conversion and enabled is True and valid
                    and not any(item["severity"] == "error" for item in self.issues[initial_errors:])):
                self.counts["power_conversion_enabled_complete"] += 1

    def build(self):
        collector = _safe_text(self.raw.get("collector_version"), 128)
        if collector is None:
            self.issue("collector_version_missing", "/collector_version")
        solver = self.raw.get("solver")
        if self.numerical_observations_allowed and (not isinstance(solver, Mapping) or solver.get("converged") is not True):
            self.issue("validated_status_conflicts_with_raw_convergence", "/solver/converged")
            self.numerical_observations_allowed = False
        self.buses()
        self.elements("lines")
        self.elements("transformers")
        if self.raw.get("auxiliary_elements") is not None:
            self.elements("auxiliary_elements")
        if self.power_conversion_inventory():
            self.elements("power_conversion_elements")
        power_conversion_coverage = self.power_conversion_coverage()
        if not self.counts["buses_seen"] and not self.counts["lines_seen"] and not self.counts["transformers_seen"] and not self.counts["power_conversion_elements_seen"]:
            self.issue("no_observable_records_provided", "/")
        if collector is None:
            self.measurements.clear()
        if self.numerical_validity != "converged_valid":
            self.issue("numerical_result_not_validated_observations_diagnostic_only", "/solver", severity="warning")
        observation_issues = len(self.issues)
        frequency_context = self.frequency_context()
        self.issues.extend(self.frequency_issues)
        coverage = {key: self.counts[key] for key in (
            "buses_seen", "buses_with_complete_metadata", "bus_node_records", "standard_node_records",
            "ground_node_records", "nonstandard_node_records", "lines_seen", "lines_with_complete_metadata",
            "transformers_seen", "transformers_with_complete_metadata", "terminal_conductor_records",
        )}
        coverage.update({"scope": "raw_records_provided_only", "all_network_coverage": False,
                         "measurement_count": len(self.measurements),
                         "measurement_counts_by_metric": dict(Counter(value.metric for value in self.measurements)),
                         "issue_count": len(self.issues),
                         "status": "complete" if not observation_issues else "partially_available",
                         "frequency_context_status": frequency_context["status"],
                         "power_conversion_elements": power_conversion_coverage,
                         "engineering_status": "not_assessed"})
        return ProfessionalObservations(
            task_id=self.task_id, numerical_validity=self.numerical_validity,
            observations=tuple(self.measurements), bus_nodes=tuple(self.bus_nodes),
            element_terminals=tuple(self.terminals), issues=tuple(self.issues), coverage=coverage,
            provenance={"raw_evidence_ref": self.evidence_ref, "collector_version": collector,
                        "frequency_context": frequency_context,
                        "required_raw_observation_schema": "2.0", "phase_identity": "native_node_number_only",
                        "legacy_scalar_fallback": False,
                        "power_conversion_terminal_scope": "load_generator_pvsystem_terminal_observations",
                        "power_conversion_current_measurement_basis": "enabled_terminal_phase_conductor_native_canonical_node_only",
                        "power_conversion_bus_suffix_policy": "explicit prefix only; remaining positions use verified native NodeOrder, without default node inference",
                        "representation_relative_tolerance": _REL_TOL,
                        "representation_absolute_tolerance": _ABS_TOL,
                        "representation_angle_tolerance_deg": 1e-7,
                        "terminal_power_consistency_relative_tolerance": _POWER_REL_TOL,
                        "terminal_power_consistency_absolute_tolerance_kw_kvar": _POWER_ABS_TOL_KW_KVAR,
                        "terminal_power_equation": "P+jQ = V_node_to_ground * conjugate(I_into_element) / 1000",
                        "terminal_bus_voltage_consistency_absolute_tolerance_v": _BUS_VOLTAGE_ABS_TOL_V,
                        "terminal_bus_voltage_consistency_relative_tolerance": _REL_TOL,
                        "terminal_bus_connection_sources": [
                            "https://opendss.epri.com/SpecifyingConnections.html",
                            "https://opendss.epri.com/PhasesandOtherConductors.html",
                        ],
                        "bus_suffix_policy": "default phase/ground nodes overridden by first NumConductors suffix tokens; excess tokens ignored by engine",
                        "tolerances_are_engineering_limits": False},
        )


def build_professional_observations(
    raw: Mapping[str, Any], *, task_id: str, evidence_ref: str,
    numerical_validity: str, require_power_conversion: bool = False,
) -> ProfessionalObservations:
    """Convert raw-v2 records, retaining native identity and explicit gaps.

    Missing metadata never falls back to legacy scalar/base/phase guesses.
    Data defects become issues; invalid caller arguments are ContractErrors.
    Only validated standard node observations become Measurements. P/Q and
    phasors remain signed terminal-conductor facts, not winding phase ratings.
    """
    if not isinstance(raw, Mapping):
        raise ContractError("raw", "must be an object")
    _text(task_id, "task_id", 128)
    _text(evidence_ref, "evidence_ref", 2048)
    if "#" in evidence_ref:
        raise ContractError("evidence_ref", "must reference the raw artifact without an existing JSON fragment")
    _choice(numerical_validity, NUMERICAL_VALIDITIES, "numerical_validity")
    if type(require_power_conversion) is not bool:
        raise ContractError("require_power_conversion", "must be a boolean")
    return _Builder(raw, task_id, evidence_ref, numerical_validity, require_power_conversion).build()
