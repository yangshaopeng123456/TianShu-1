"""Explicit snapshot operating state and immutable native-context verification.

Copyright (c) yangshaopeng. No I/O, engine calls, defaults or electrical edits.
Omitted source values are never reconstructed; trustworthy native values are
bound on first use and every subsequent observation must retain that baseline.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
import re

VERSION = "1.0"
SCOPE = "bounded_snapshot_source_state"
NATIVE_SCOPE = "read_only_snapshot_operating_state"
CE = "OpenDSS.ActiveCircuit.ActiveCktElement."
SOL = "OpenDSS.ActiveCircuit.Solution."
CLASSES = {"line", "transformer", "load", "generator", "pvsystem", "vsource", "capacitor", "reactor"}
GLOBALS = {"loadmult": ("LoadMult", "double"), "genmult": ("GenMult", "double"),
    "loadmodel": ("LoadModel", "integer"), "algorithm": ("Algorithm", "integer"),
    "mode": ("Mode", "integer"), "controlmode": ("ControlMode", "integer"),
    "hour": ("DblHour", "double"), "frequency": ("Frequency", "double")}
ENUMS = {"loadmodel": {"powerflow": 1, "admittance": 2}, "algorithm": {"normal": 0, "newton": 1},
    "mode": {"snapshot": 0}, "controlmode": {"off": -1}}
SOURCE_TYPED = {"pu": "pu", "angle": "AngleDeg", "frequency": "Frequency", "basekv": "BasekV"}
CAPACITOR_TYPED = {"kv": "kV", "kvar": "kvar"}
PROPERTY_TEXT_PRECISION = {"kind": "native_stored_property_numeric_text"}


def passive_comparison_text(raw, key, kind):
    """Validate Reactor's stored scalar property text without rounding it.

    Version8 Reactor.GetPropertyValue delegates kV/kvar to DSSObject's stored
    property string. This is a native property-text readback, not a typed
    internal-double getter or a fixed significant-digit formatter.
    """
    if not isinstance(raw, str) or not 0 < len(raw) <= 256:
        raise ValueError("bounded_passive_text_required")
    text = raw.strip()
    if kind != "reactor" or key not in {"kv", "kvar"} or re.fullmatch(
        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", text) is None:
        raise ValueError("scalar_reactor_property_numeric_text_required")
    return text


def state_fields(kind):
    fields = {"enabled": (CE + "Enabled", "boolean")}
    if kind in {"load", "generator", "pvsystem"}:
        fields["model"] = (CE + "Properties(model).Val", "integer")
    if kind in {"load", "generator"}:
        fields["status"] = (CE + "Properties(status).Val", "text")
    if kind == "line":
        fields["switch"] = (CE + "Properties(switch).Val", "boolean")
    if kind == "vsource":
        fields.update({key: ("OpenDSS.ActiveCircuit.Vsources." + getter, "double") for key, getter in SOURCE_TYPED.items()})
    if kind in {"capacitor", "reactor"}:
        fields.update({key: ("OpenDSS.ActiveCircuit.Capacitors." + getter, "double")
            for key, getter in CAPACITOR_TYPED.items()} if kind == "capacitor" else
            {key: (CE + f"Properties({key}).Val", "property_number") for key in ("kv", "kvar")})
        fields["conn"] = (CE + "Properties(conn).Val", "connection")
        fields["phases"] = (CE + "NumPhases", "integer")
        fields["bus1"] = (CE + "BusNames[0]", "bus")
        fields["bus2"] = (CE + "BusNames[1]", "bus")
    if kind == "capacitor":
        fields["numsteps"] = (CE + "Properties(numsteps).Val", "integer")
        fields["states"] = (CE + "Properties(states).Val", "integer_array")
    return fields


def _missing():
    return {"status": "not_declared", "value": None, "source_refs": []}


def _finite(value):
    return type(value) in {int, float} and math.isfinite(value)


def _bus(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    parts = value.casefold().split(".")
    if not parts[0] or any(not re.fullmatch(r"[0-9]{1,10}", token) or int(token) > 2147483647 for token in parts[1:]):
        return None
    return parts[0], [int(token) for token in parts[1:]]


def _value(value, kind):
    return (type(value) is bool if kind == "boolean" else type(value) is int if kind == "integer"
        else _finite(value) if kind in {"double", "property_number"}
        else isinstance(value, list) and len(value) == 1 and type(value[0]) is int and value[0] in {0, 1} if kind == "integer_array"
        else _bus(value) is not None if kind == "bus"
        else value in {"wye", "delta"} if kind == "connection" and isinstance(value, str)
        else isinstance(value, str) and 0 < len(value) <= 256)


def _normal(raw, key):
    value = raw.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1]
    text = value.casefold()
    if key in ENUMS:
        if text not in ENUMS[key]:
            raise ValueError("unsupported_explicit_snapshot_enum")
        return ENUMS[key][text]
    if key in {"enabled", "switch"}:
        if text not in {"yes", "no", "true", "false", "1", "0", "y", "n", "t", "f"}:
            raise ValueError("explicit_boolean_required")
        return text in {"yes", "true", "1", "y", "t"}
    if key == "status":
        if text not in {"variable", "fixed", "exempt"}:
            raise ValueError("unsupported_explicit_status")
        return text
    if key in {"bus1", "bus2"}:
        if _bus(text) is None:
            raise ValueError("explicit_passive_bus_required")
        return text
    if key == "conn":
        if text not in {"wye", "ln", "y", "delta", "ll", "d"}:
            raise ValueError("explicit_connection_required")
        return "wye" if text in {"wye", "ln", "y"} else "delta"
    if key == "states":
        parts = value.strip("[]() ").replace(",", " ").split()
        if len(parts) != 1 or parts[0] not in {"0", "1"}:
            raise ValueError("single_capacitor_step_state_required")
        return [int(parts[0])]
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("finite_explicit_number_required")
    if key in {"model", "numsteps", "phases"}:
        if not number.is_integer() or not 1 <= number <= 8:
            raise ValueError("explicit_model_integer_required")
        if key == "numsteps" and number != 1:
            raise ValueError("only_single_capacitor_step_supported")
        if key == "phases" and number not in {1, 2, 3}:
            raise ValueError("bounded_explicit_phase_count_required")
        return int(number)
    if key in {"loadmult", "genmult", "pu"} and number < 0:
        raise ValueError("nonnegative_explicit_setting_required")
    if key in {"frequency", "basekv", "kv"} and number <= 0:
        raise ValueError("positive_explicit_setting_required")
    return number


def _refs(item, key):
    refs = item.get("source_refs")
    return (isinstance(refs, list) and 0 < len(refs) <= 1000 and all(isinstance(ref, Mapping)
        and isinstance(ref.get("source_file"), str) and 0 < len(ref["source_file"]) <= 256
        and type(ref.get("line")) is int and 1 <= ref["line"] <= 100000
        and ref.get("property") == key for ref in refs))


def inspect_source_state(inspection, source_files=None):
    """Replay only explicit source settings from verified inspection and bytes.

    ``source_files`` contains task-frozen bytes. Existing inspection/manifest
    identities stay unchanged; Set assignments are resolved from those bytes.
    Open/Close supports explicit ``term`` and ``cond`` only (cond=0 means all).
    """
    result = {"version": VERSION, "scope": SCOPE, "status": "complete", "fields": {key: _missing() for key in GLOBALS},
        "objects": [], "switch_operations": [], "issues": [], "defaults_inserted": False,
        "electrical_parameters_modified": False}
    try:
        if not isinstance(inspection, Mapping) or not isinstance(inspection.get("objects"), list) or not isinstance(inspection.get("commands"), list):
            raise ValueError("inspection_shape")
        objects = {}
        commands = inspection["commands"]
        if len(commands) > 100000 or len(inspection["objects"]) > 20000:
            raise ValueError("source_budget")
        order = {(row["file"], row["line"]): index for index, row in enumerate(commands)}
        if len(order) != len(commands):
            raise ValueError("source_command_order_ambiguous")
        events = []
        for row in inspection["objects"]:
            kind = row["class"].casefold()
            if kind not in CLASSES | {"circuit"}:
                continue
            name = "vsource.source" if kind == "circuit" else row["name"].casefold()
            native_kind = "vsource" if kind == "circuit" else kind
            target = objects.setdefault(name, {"object_id": name, "class": native_kind,
                "fields": {key: _missing() for key in state_fields(native_kind)}})
            if target["class"] != native_kind:
                raise ValueError("source_identity_class")
            for index, prop in enumerate(row["properties"]):
                key = prop["name"].casefold()
                if key not in target["fields"]:
                    continue
                position = order.get((prop["source_file"], prop["line"]))
                if position is None:
                    raise ValueError("source_property_order_unavailable")
                events.append((position, index, name, key, prop["value"], prop["source_file"], prop["line"]))
        texts = {}
        for position, command in enumerate(commands):
            verb = command["command"].casefold()
            if verb not in {"set", "open", "close"}:
                continue
            props = command.get("properties")
            if props is None:
                if not isinstance(source_files, Mapping) or command["file"] not in source_files:
                    raise ValueError("explicit_command_bytes_unavailable")
                # Imported lazily: models itself uses this module after its
                # established static parser has completed the input check.
                from .models import _uncomment, _tokens, _assignments, _scalar
                if command["file"] not in texts:
                    content = source_files[command["file"]]
                    if not isinstance(content, bytes) or len(content) > 20 * 1024 * 1024:
                        raise ValueError("source_bytes")
                    texts[command["file"]] = _uncomment(content.decode("utf-8-sig"), "source_state").splitlines()
                tokens = _tokens(texts[command["file"]][command["line"] - 1], "source_state")
                if not tokens or tokens[0].casefold() != verb:
                    raise ValueError("source_command_bytes_disagree")
                props = [{"name": key, "value": value} for key, value in _assignments(tokens[1 if verb == "set" else 2:], "source_state")]
                if verb in {"open", "close"}:
                    command = {**command, "object_id": _scalar(tokens[1]).casefold()}
            if verb == "set":
                for index, prop in enumerate(props):
                    key = prop["name"].casefold()
                    if key in GLOBALS:
                        events.append((position, index, None, key, prop["value"], command["file"], command["line"]))
            else:
                values = {prop["name"].casefold(): prop["value"] for prop in props}
                name = command.get("object_id", "").casefold()
                if name not in objects or set(values) != {"term", "cond"} or len(props) != 2:
                    raise ValueError("explicit_switch_operation_required")
                terminal, conductor = int(values["term"]), int(values["cond"])
                if not 1 <= terminal <= 1000 or not 0 <= conductor <= 1000 or str(terminal) != values["term"] or str(conductor) != values["cond"]:
                    raise ValueError("switch_operation_dimensions")
                result["switch_operations"].append({"object_id": name, "terminal": terminal, "conductor": conductor,
                    "open": verb == "open", "source_refs": [{"source_file": command["file"], "line": command["line"], "property": verb}],
                    "command_index": position})
        for position, index, name, key, raw, file, line in sorted(events):
            value = _normal(raw, key)
            cell = {"status": "declared", "value": value, "source_refs": [{"source_file": file, "line": line, "property": key,
                "command_index": position, "property_index": index}]}
            (objects[name]["fields"] if name else result["fields"])[key] = cell
        result["objects"] = [objects[name] for name in sorted(objects)]
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, UnicodeError, OverflowError):
        result.update(status="invalid", issues=[{"code": "source_state_untrusted", "severity": "error"}])
    return result


def _equal(left, right, kind):
    if kind == "bus":
        expected, actual = _bus(left), _bus(right)
        return expected[0] == actual[0] and expected[1] == actual[1][:len(expected[1])]
    return math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-12) if kind in {"double", "property_number"} else left == right


def verify_source_state(declaration, native, baseline=None):
    """Strict readback and frozen-context check. Missing native facts fail closed."""
    checks, issues = [], []
    def add(name, field, passed, expected=None, actual=None, reason=None):
        checks.append({"object_id": name, "field": field, "status": "matched" if passed else "mismatch",
            "expected": expected, "actual": actual, "reason": reason})
        if not passed:
            issues.append({"code": "snapshot_state_not_verified", "object_id": name, "field": field})
    def compare_value(name, key, expected, actual_cell, kind):
        if kind == "property_number":
            text = passive_comparison_text(actual_cell.get("raw_text"), key.removeprefix("frozen."), name.split(".", 1)[0])
            add(name, key, _equal(expected, actual_cell["value"], kind), expected, actual_cell["value"])
            checks[-1].update(raw_text=actual_cell["raw_text"], comparison_text=text,
                comparison_kind="native_stored_property_numeric_text_consistency",
                native_property_text_readback=True, native_double_readback=False,
                full_precision_equality=False)
        elif kind == "bus" and key.startswith("frozen."):
            add(name, key, expected.casefold() == actual_cell["value"].casefold(), expected, actual_cell["value"])
        else:
            add(name, key, _equal(expected, actual_cell["value"], kind), expected, actual_cell["value"])
    def validate_cells(cells, specs, name):
        if not isinstance(cells, Mapping) or set(cells) != set(specs):
            raise ValueError("native_fields_coverage")
        for key, (source, kind) in specs.items():
            cell = cells[key]
            precision = {"kind": "native_binary_double"} if kind == "double" else None
            if (not isinstance(cell, Mapping) or cell.get("status") != "available" or cell.get("source") != source
                or cell.get("issues") != [] or not _value(cell.get("value"), kind)
                or precision is not None and cell.get("precision") != precision):
                raise ValueError("native_cell_untrusted")
            if kind == "property_number":
                text = passive_comparison_text(cell.get("raw_text"), key, name.split(".", 1)[0])
                if (cell.get("precision") != PROPERTY_TEXT_PRECISION
                    or cell.get("comparison_text", text) != text
                    or float(text) != cell["value"]):
                    raise ValueError("native_property_numeric_text_untrusted")
            value = cell["value"]
            if (key in {"loadmult", "genmult", "pu"} and value < 0
                or key in {"frequency", "basekv", "kv"} and value <= 0
                or key == "loadmodel" and value not in {1, 2}
                or key == "algorithm" and value not in {0, 1}
                or key == "model" and value not in set(range(1, 9))
                or key == "status" and value not in {"variable", "fixed", "exempt"}
                or key == "phases" and value not in {1, 2, 3}
                or key == "numsteps" and value != 1):
                raise ValueError("native_state_value_outside_scope")
        return cells
    def observed(frame):
        if (not isinstance(frame, Mapping) or frame.get("version") != VERSION or frame.get("scope") != NATIVE_SCOPE
            or frame.get("source") != "OpenDSS.COM" or frame.get("status") != "available"
            or frame.get("electrical_parameters_modified") is not False or frame.get("issues") != []):
            raise ValueError("native_snapshot_untrusted")
        validate_cells(frame.get("fields"), {key: (SOL + getter, kind) for key, (getter, kind) in GLOBALS.items()}, None)
        coverage = frame.get("coverage", {})
        names = coverage.get("element_names")
        if (not isinstance(coverage, Mapping) or coverage.get("source") != "OpenDSS.ActiveCircuit.AllElementNames" or coverage.get("status") != "available"
            or not isinstance(names, list) or not 1 <= len(names) <= 20000 or any(not isinstance(n, str) or "." not in n for n in names)
            or len(names) != len(set(n.casefold() for n in names)) or not isinstance(frame.get("objects"), list)):
            raise ValueError("native_inventory_untrusted")
        rows = {}
        for row in frame["objects"]:
            if not isinstance(row, Mapping):
                raise ValueError("native_object_shape")
            name = row.get("object_id")
            if not isinstance(name, str) or name.casefold() in rows or row.get("class") not in CLASSES:
                raise ValueError("native_identity")
            name = name.casefold()
            if (row.get("identity_verified") is not True or row.get("identity_source") != CE + "Name"
                or row.get("issues") != [] or name.split(".", 1)[0] != row["class"]):
                raise ValueError("native_identity_untrusted")
            specs = state_fields(row["class"])
            specs.update({"num_terminals": (CE + "NumTerminals", "integer"), "num_conductors": (CE + "NumConductors", "integer")})
            validate_cells(row.get("fields"), specs, name)
            nt, nc = row["fields"]["num_terminals"]["value"], row["fields"]["num_conductors"]["value"]
            opened = row.get("open_conductors")
            if (not 1 <= nt <= 1000 or not 1 <= nc <= 1000 or nt * nc > 100000 or not isinstance(opened, Mapping)
                or opened.get("status") != "available" or opened.get("source") != CE + "IsOpen(terminal,conductor)"
                or opened.get("issues") != [] or not isinstance(opened.get("value"), list) or len(opened["value"]) != nt
                or any(not isinstance(t, list) or len(t) != nc or any(type(v) is not bool for v in t) for t in opened["value"])):
                raise ValueError("native_open_matrix_untrusted")
            if row["class"] in {"capacitor", "reactor"}:
                passive = row["fields"]
                first_nodes = _bus(passive["bus1"]["value"])[1]
                grounded = _bus(passive["bus2"]["value"])[1]
                single_reactor_branch = (row["class"] == "reactor" and nc == passive["phases"]["value"] == 1
                    and len(first_nodes) == len(grounded) == 1)
                if (passive["conn"]["value"] != "wye" or nt != 2 or nc != passive["phases"]["value"]
                    or not (len(grounded) == nc and all(node == 0 for node in grounded) or single_reactor_branch)):
                    raise ValueError("only_observed_fixed_wye_shunts_or_explicit_single_reactor_branches_are_supported")
            rows[name] = row
        if set(rows) != {n.casefold() for n in names}:
            raise ValueError("native_full_inventory_not_observed")
        return rows
    try:
        if (not isinstance(declaration, Mapping) or declaration.get("version") != VERSION or declaration.get("scope") != SCOPE
            or declaration.get("status") != "complete" or declaration.get("issues") != []
            or declaration.get("defaults_inserted") is not False or declaration.get("electrical_parameters_modified") is not False
            or set(declaration.get("fields", {})) != set(GLOBALS) or not isinstance(declaration.get("objects"), list)):
            raise ValueError("source_declaration_untrusted")
        rows = observed(native)
        old = observed(baseline) if baseline is not None else None
        if old is not None and set(old) != set(rows):
            raise ValueError("frozen_inventory_changed")
        source_rows = {}
        for row in declaration["objects"]:
            name = row["object_id"].casefold()
            if name in source_rows or name not in rows or row["class"] != rows[name]["class"] or set(row["fields"]) != set(state_fields(row["class"])):
                raise ValueError("source_inventory")
            source_rows[name] = row
        if set(source_rows) != set(rows):
            raise ValueError("source_native_inventory_differs")
        for name, row in rows.items():
            if row["class"] == "reactor" and any(node != 0 for node in _bus(row["fields"]["bus2"]["value"])[1]):
                # A non-ground single-conductor branch is accepted only when
                # both source endpoints are explicit. Native defaults alone
                # cannot add a branch to the supported source-model scope.
                for key in ("bus1", "bus2"):
                    item = source_rows[name]["fields"][key]
                    if (not isinstance(item, Mapping) or item.get("status") != "declared"
                        or not _value(item.get("value"), "bus") or not _refs(item, key)
                        or len(_bus(item["value"])[1]) != 1):
                        raise ValueError("explicit_single_reactor_branch_source_endpoints_required")
        for name, source, actual, specs, previous in [(None, declaration["fields"], native["fields"],
            {key: (SOL + getter, kind) for key, (getter, kind) in GLOBALS.items()}, baseline["fields"] if baseline is not None else None)] + [
            (name, source_rows[name]["fields"], row["fields"], state_fields(row["class"]), old[name]["fields"] if old is not None else None)
            for name, row in rows.items()]:
            for key, (_, kind) in specs.items():
                item, value = source[key], actual[key]["value"]
                if not isinstance(item, Mapping) or item.get("status") not in {"declared", "not_declared"}:
                    raise ValueError("source_value_untrusted")
                if item["status"] == "declared":
                    if not _value(item.get("value"), kind) or not _refs(item, key):
                        raise ValueError("source_value_provenance_untrusted")
                    compare_value(name, key, item["value"], actual[key], kind)
                elif item.get("value") is not None or item.get("source_refs") != []:
                    raise ValueError("undeclared_source_has_value")
                if previous is not None:
                    compare_value(name, "frozen." + key, previous[key]["value"], actual[key], kind)
        add(None, "snapshot.mode", native["fields"]["mode"]["value"] == 0, 0, native["fields"]["mode"]["value"])
        add(None, "snapshot.controlmode", native["fields"]["controlmode"]["value"] == -1, -1, native["fields"]["controlmode"]["value"])
        # Parsing an OpenDSS model number is not numerical qualification of
        # that device family. Keep the first release's verified models bounded;
        # old imported bytes and committed results are never rewritten.
        families = {"load": {1, 2, 5}, "generator": {1}, "pvsystem": {1}}
        for name, row in rows.items():
            supported = families.get(row["class"])
            if supported is not None:
                actual_model = row["fields"]["model"]["value"]
                add(name, "qualified_model_family", actual_model in supported, sorted(supported), actual_model,
                    "A parsed or natively solvable model is outside this release unless its model family is qualified.")
        operations = declaration.get("switch_operations")
        if not isinstance(operations, list) or len(operations) > 100000:
            raise ValueError("switch_operation_budget")
        expected_open = {}
        last_operation = -1
        for operation in operations:
            name, terminal, conductor = operation["object_id"], operation["terminal"], operation["conductor"]
            if name not in rows or type(terminal) is not int or type(conductor) is not int or type(operation.get("open")) is not bool:
                raise ValueError("switch_operation_identity")
            position = operation.get("command_index")
            if type(position) is not int or not position > last_operation or not _refs(operation, "open" if operation["open"] else "close"):
                raise ValueError("switch_operation_provenance_or_order_untrusted")
            last_operation = position
            nt, nc = rows[name]["fields"]["num_terminals"]["value"], rows[name]["fields"]["num_conductors"]["value"]
            if not 1 <= terminal <= nt or not 0 <= conductor <= nc:
                raise ValueError("switch_operation_dimensions")
            for index in range(1, nc + 1) if conductor == 0 else (conductor,):
                expected_open[name, terminal, index] = operation["open"]
        for (name, terminal, conductor), value in expected_open.items():
            actual = rows[name]["open_conductors"]["value"][terminal - 1][conductor - 1]
            add(name, f"is_open.{terminal}.{conductor}", value is actual, value, actual)
        if old is not None:
            for name, row in rows.items():
                for key in ("num_terminals", "num_conductors"):
                    add(name, "frozen." + key, old[name]["fields"][key]["value"] == row["fields"][key]["value"], old[name]["fields"][key]["value"], row["fields"][key]["value"])
                add(name, "frozen.open_conductors", old[name]["open_conductors"]["value"] == row["open_conductors"]["value"],
                    old[name]["open_conductors"]["value"], row["open_conductors"]["value"])
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        add(None, "contract", False, reason="Required explicit source/native state is missing, malformed, untrusted or outside this bounded scope.")
    passed = bool(checks and not issues)
    return {"version": VERSION, "scope": SCOPE, "passed": passed, "status": "matched" if passed else "not_verified",
        "checks": checks, "issues": issues, "engineering_accepted": False, "baseline_bound": baseline is not None,
        "coverage": {"explicit_source_and_frozen_native_context": True, "defaults_reconstructed": False,
            "open_conductor_matrix_required": True}, "comparison_tolerance": {"relative": 1e-10, "absolute": 1e-12}}
