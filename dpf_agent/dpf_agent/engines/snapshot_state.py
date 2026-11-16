"""Read-only snapshot state and full conductor-open matrix. Copyright yangshaopeng.

All electrical properties are getters. Only active object/collection selectors
are restored. Missing/erroring readback is never replaced by a source default.
"""
from __future__ import annotations

import math

from ..professional.source_state import (CE, SOL, CLASSES, GLOBALS, NATIVE_SCOPE, SOURCE_TYPED,
    CAPACITOR_TYPED, PROPERTY_TEXT_PRECISION, state_fields, passive_comparison_text)


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("bounded_native_text_required")
    return value


def _number(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("native_finite_double_required")
    return float(value)


def _integer(value):
    if type(value) is not int or not -2147483648 <= value <= 2147483647:
        raise ValueError("native_integer_required")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("native_boolean_required")
    return value


def _bool_text(value):
    text = _text(value).strip().casefold()
    if text not in {"yes", "no", "true", "false", "1", "0", "y", "n", "t", "f"}:
        raise ValueError("native_boolean_text_required")
    return text in {"yes", "true", "1", "y", "t"}


def _model_text(value):
    text = _text(value).strip()
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 8:
        raise ValueError("native_model_integer_required")
    return int(text)


def _status(value):
    text = _text(value).strip().casefold()
    if text not in {"variable", "fixed", "exempt"}:
        raise ValueError("native_status_required")
    return text


def _connection(value):
    text = _text(value).strip().casefold()
    if text not in {"wye", "ln", "y", "delta", "ll", "d"}:
        raise ValueError("native_connection_required")
    return "wye" if text in {"wye", "ln", "y"} else "delta"


def _states(value):
    parts = _text(value).strip("[]() ").replace(",", " ").split()
    if len(parts) != 1 or parts[0] not in {"0", "1"}:
        raise ValueError("native_single_capacitor_step_state_required")
    return [int(parts[0])]


def _names(value):
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 20000:
        raise ValueError("native_element_inventory_required")
    names = [_text(name) for name in value]
    if any("." not in name or name.split(".", 1)[0].casefold() not in CLASSES for name in names):
        raise ValueError("unsupported_snapshot_element_class")
    if len(names) != len(set(name.casefold() for name in names)):
        raise ValueError("duplicate_snapshot_element_identity")
    return names


def _invalidate(fields, code):
    for cell in fields.values():
        cell.update(value=None, status="not_available", issues=[{"code": code, "source": cell["source"]}])


def _collection_values(reader, circuit, requested, row, interface, getters):
    prefix = "OpenDSS.ActiveCircuit." + interface
    collection_cell = reader.read(prefix, lambda: getattr(circuit, interface))
    collection = collection_cell["value"]
    if collection_cell["status"] != "available" or collection is None:
        row["issues"].append({"code": "snapshot_typed_interface_unavailable", "source": prefix})
        return
    previous = reader.read(prefix + ".Name:before_snapshot", lambda: collection.Name, _text)
    try:
        selected = reader.select(collection, "Name", requested.split(".", 1)[1], prefix + ".Name:select_snapshot")
        name = reader.read(prefix + ".Name", lambda: collection.Name, _text)
        active = reader.read(CE + "Name:before_source_state", lambda: circuit.ActiveCktElement.Name, _text)
        if (selected["status"] != "available" or name["value"] is None or name["value"].casefold() != requested.split(".", 1)[1].casefold()
            or active["value"] is None or active["value"].casefold() != requested.casefold()):
            row["issues"].append({"code": "snapshot_vsource_identity_mismatch", "source": prefix + ".Name"})
            return
        for key, getter in getters.items():
            row["fields"][key] = reader.read(prefix + "." + getter, lambda attr=getter: getattr(collection, attr), _number)
            row["fields"][key]["precision"] = {"kind": "native_binary_double"}
        name = reader.read(prefix + ".Name:after_snapshot", lambda: collection.Name, _text)
        active = reader.read(CE + "Name:after_source_state", lambda: circuit.ActiveCktElement.Name, _text)
        if name["value"] is None or name["value"].casefold() != requested.split(".", 1)[1].casefold() or active["value"] is None or active["value"].casefold() != requested.casefold():
            row["issues"].append({"code": "snapshot_vsource_identity_changed", "source": prefix + ".Name"})
    finally:
        if previous["status"] == "available":
            restored = reader.select(collection, "Name", previous["value"], prefix + ".Name:restore_snapshot")
            actual = reader.read(prefix + ".Name:after_snapshot_restore", lambda: collection.Name, _text)
            if restored["status"] != "available" or actual["value"] is None or actual["value"].casefold() != previous["value"].casefold():
                row["issues"].append({"code": "snapshot_vsource_selector_restore_failed", "source": prefix + ".Name"})
        else:
            row["issues"].append({"code": "snapshot_vsource_previous_selector_unavailable", "source": prefix + ".Name"})
        restored = reader.read("OpenDSS.ActiveCircuit.SetActiveElement:after_source_snapshot", lambda: circuit.SetActiveElement(requested), _integer)
        active = reader.read(CE + "Name:after_source_snapshot_restore", lambda: circuit.ActiveCktElement.Name, _text)
        if restored["status"] != "available" or active["value"] is None or active["value"].casefold() != requested.casefold():
            row["issues"].append({"code": "snapshot_vsource_active_restore_failed", "source": CE + "Name"})


def collect_snapshot_state(engine, *, reader=None):
    """Collect one owned circuit context; never starts, compiles or solves it."""
    if reader is None:
        from .device_connections import _Reader
        reader = _Reader(engine)
    result = {"version": "1.0", "scope": NATIVE_SCOPE, "source": "OpenDSS.COM", "status": "not_available",
        "fields": {}, "objects": [], "issues": [], "electrical_parameters_modified": False,
        "coverage": {"source": "OpenDSS.ActiveCircuit.AllElementNames", "status": "not_available", "element_names": []}}
    initial_errors = reader.consume("snapshot_state:before_read")
    if initial_errors:
        result["issues"].extend(initial_errors)
        return result
    cc = reader.read("OpenDSS.ActiveCircuit", lambda: engine.ActiveCircuit)
    circuit = cc["value"]
    if cc["status"] != "available" or circuit is None:
        result["issues"].append({"code": "snapshot_circuit_unavailable"})
        return result
    before = reader.read(CE + "Name:before_snapshot_collection", lambda: circuit.ActiveCktElement.Name, _text)
    inventory = reader.read("OpenDSS.ActiveCircuit.AllElementNames", lambda: circuit.AllElementNames, _names)
    if inventory["status"] != "available":
        result["issues"].extend(inventory["issues"])
        return result
    solution = reader.read("OpenDSS.ActiveCircuit.Solution", lambda: circuit.Solution)
    if solution["status"] != "available" or solution["value"] is None:
        result["issues"].append({"code": "snapshot_solution_unavailable"})
        return result
    for key, (getter, kind) in GLOBALS.items():
        result["fields"][key] = reader.read(SOL + getter, lambda attr=getter: getattr(solution["value"], attr), _number if kind == "double" else _integer)
        if kind == "double":
            result["fields"][key]["precision"] = {"kind": "native_binary_double"}
    try:
        for requested in inventory["value"]:
            kind = requested.split(".", 1)[0].casefold()
            row = {"object_id": requested.casefold(), "class": kind, "identity_verified": False,
                "identity_source": CE + "Name", "fields": {}, "issues": [], "open_conductors": {"value": None,
                    "status": "not_available", "source": CE + "IsOpen(terminal,conductor)", "issues": []}}
            result["objects"].append(row)
            selected = reader.read("OpenDSS.ActiveCircuit.SetActiveElement", lambda name=requested: circuit.SetActiveElement(name), _integer)
            active = reader.read("OpenDSS.ActiveCircuit.ActiveCktElement", lambda: circuit.ActiveCktElement)
            ce = active["value"]
            identity = reader.read(CE + "Name", lambda: ce.Name, _text) if ce is not None else {"value": None}
            if selected["status"] != "available" or identity["value"] is None or identity["value"].casefold() != requested.casefold():
                row["issues"].append({"code": "snapshot_active_identity_untrusted"})
                continue
            fields = row["fields"]
            for key, getter, convert in (("enabled", "Enabled", _boolean), ("num_terminals", "NumTerminals", _integer), ("num_conductors", "NumConductors", _integer)):
                fields[key] = reader.read(CE + getter, lambda attr=getter: getattr(ce, attr), convert)
            for key, convert in (("model", _model_text), ("status", _status), ("switch", _bool_text)):
                if key in state_fields(kind):
                    fields[key] = reader.read(CE + f"Properties({key}).Val", lambda prop=key: ce.Properties(prop).Val, convert)
            if kind in {"capacitor", "reactor"}:
                fields["phases"] = reader.read(CE + "NumPhases", lambda: ce.NumPhases, _integer)
                for index in (0, 1):
                    fields["bus" + str(index + 1)] = reader.read(CE + f"BusNames[{index}]",
                        lambda terminal=index: _text(ce.BusNames[terminal]).casefold(), _text)
                fields["conn"] = reader.read(CE + "Properties(conn).Val", lambda: ce.Properties("conn").Val, _connection)
                for key in ("kv", "kvar") if kind == "reactor" else ():
                    source = CE + f"Properties({key}).Val"
                    cell = reader.read(source, lambda prop=key: ce.Properties(prop).Val, _text)
                    text = cell["value"]
                    if cell["status"] == "available":
                        try:
                            comparison = passive_comparison_text(text, key, kind)
                            cell["value"] = _number(float(comparison))
                            cell["comparison_text"] = comparison
                        except (ValueError, TypeError, OverflowError):
                            cell.update(value=None, status="not_available", issues=[{"code": "snapshot_invalid_passive_parameter", "source": source}])
                    cell.update(raw_text=text, precision=dict(PROPERTY_TEXT_PRECISION))
                    fields[key] = cell
            if kind == "capacitor":
                fields["numsteps"] = reader.read(CE + "Properties(numsteps).Val", lambda: ce.Properties("numsteps").Val, _model_text)
                fields["states"] = reader.read(CE + "Properties(states).Val", lambda: ce.Properties("states").Val, _states)
            nt, nc = fields["num_terminals"]["value"], fields["num_conductors"]["value"]
            if nt is None or nc is None or not 1 <= nt <= 1000 or not 1 <= nc <= 1000 or nt * nc > 100000:
                row["issues"].append({"code": "snapshot_terminal_dimensions_unavailable"})
            else:
                cells = [[reader.read(CE + f"IsOpen({terminal},{conductor})", lambda t=terminal, c=conductor: ce.IsOpen(t, c), _boolean)
                    for conductor in range(1, nc + 1)] for terminal in range(1, nt + 1)]
                row["open_conductors"].update(value=[[cell["value"] for cell in terminal] for terminal in cells],
                    status="available" if all(cell["status"] == "available" for terminal in cells for cell in terminal) else "not_available",
                    issues=[issue for terminal in cells for cell in terminal for issue in cell["issues"]])
            middle = reader.read(CE + "Name:before_nested_snapshot", lambda: circuit.ActiveCktElement.Name, _text)
            if middle["value"] is None or middle["value"].casefold() != requested.casefold():
                row["issues"].append({"code": "snapshot_identity_changed_before_nested_read"})
            elif kind == "vsource":
                _collection_values(reader, circuit, requested, row, "Vsources", SOURCE_TYPED)
            elif kind == "capacitor":
                _collection_values(reader, circuit, requested, row, "Capacitors", CAPACITOR_TYPED)
            final = reader.read(CE + "Name:after_snapshot_object", lambda: circuit.ActiveCktElement.Name, _text)
            if final["value"] is None or final["value"].casefold() != requested.casefold():
                row["issues"].append({"code": "snapshot_active_identity_changed"})
            row["identity_verified"] = not row["issues"]
            if row["issues"]:
                _invalidate(fields, "snapshot_identity_or_dimensions_untrusted")
                row["open_conductors"].update(value=None, status="not_available")
    finally:
        if before["status"] == "available":
            restored = reader.read("OpenDSS.ActiveCircuit.SetActiveElement:restore_snapshot_collection", lambda: circuit.SetActiveElement(before["value"]), _integer)
            actual = reader.read(CE + "Name:after_snapshot_collection_restore", lambda: circuit.ActiveCktElement.Name, _text)
            if restored["status"] != "available" or actual["value"] is None or actual["value"].casefold() != before["value"].casefold():
                result["issues"].append({"code": "snapshot_collection_active_restore_failed"})
        else:
            result["issues"].append({"code": "snapshot_collection_initial_active_unavailable"})
    after = reader.read("OpenDSS.ActiveCircuit.AllElementNames:after_snapshot", lambda: circuit.AllElementNames, _names)
    if after["status"] != "available" or after["value"] != inventory["value"]:
        result["issues"].append({"code": "snapshot_inventory_changed_during_read"})
    else:
        result["coverage"].update(status="available", element_names=inventory["value"])
    for key, (getter, kind) in GLOBALS.items():
        after_cell = reader.read(SOL + getter + ":after_snapshot", lambda attr=getter: getattr(solution["value"], attr), _number if kind == "double" else _integer)
        before_cell = result["fields"][key]
        if after_cell["status"] != "available" or before_cell["value"] != after_cell["value"]:
            result["issues"].append({"code": "snapshot_global_changed_during_read", "field": key})
    result["issues"].extend(reader.consume("snapshot_state:after_read"))
    available = (not result["issues"] and all(cell["status"] == "available" for cell in result["fields"].values())
        and all(row["identity_verified"] and not row["issues"] and row["open_conductors"]["status"] == "available"
            and set(row["fields"]) == set(state_fields(row["class"])) | {"num_terminals", "num_conductors"}
            and all(cell["status"] == "available" for cell in row["fields"].values()) for row in result["objects"]))
    result["status"] = "available" if available else "partial"
    return result
