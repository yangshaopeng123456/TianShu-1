"""Read-only, identity-checked native device and winding observations.

Copyright (c) yangshaopeng. Native ratings are observed model properties, not
approved engineering limits. Selecting an object/winding does not edit it.
No text commands, electrical setters, solve calls or source declarations are
used here. The caller must own the engine context for the entire collection.
"""
from __future__ import annotations

import math
from typing import Any, Callable


DEVICE_CONNECTION_SCHEMA_VERSION = "1.0"
SUPPORTED_DEVICE_CLASSES = frozenset({"line", "transformer", "load", "generator", "pvsystem", "vsource"})
_MAX_ARRAY_ITEMS = 100000
_MAX_TEXT_CHARACTERS = 1048576
_CE = "OpenDSS.ActiveCircuit.ActiveCktElement."
_TX = "OpenDSS.ActiveCircuit.Transformers"


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT_CHARACTERS:
        raise ValueError("nonempty_text_required")
    return value


def _integer(value: Any, *, minimum=0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 2147483647:
        raise ValueError("integer_out_of_range")
    return value


def _real(value: Any) -> float:
    if isinstance(value, str):
        value = float(value.strip())
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric_required")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("finite_number_required")
    return result


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("boolean_required")
    return value


def _connection(value: Any) -> str:
    text = _text(value).strip().casefold()
    if text in {"wye", "y", "ln"}:
        return "wye"
    if text in {"delta", "d", "ll"}:
        return "delta"
    raise ValueError("unknown_connection")


def _array(value: Any, convert: Callable) -> list:
    if not isinstance(value, (list, tuple)):
        raise ValueError("array_required")
    if len(value) > _MAX_ARRAY_ITEMS:
        raise ValueError("array_exceeds_collection_limit")
    return [convert(item) for item in value]


def _unavailable(source: str, code: str) -> dict:
    return {"value": None, "status": "not_available", "source": source,
            "issues": [{"code": code, "source": source}]}


class _Reader:
    def __init__(self, engine: Any):
        self.engine = engine

    def consume(self, source: str) -> list[dict]:
        issues = []
        try:
            number = self.engine.Error.Number
            if isinstance(number, bool) or not isinstance(number, int):
                raise ValueError("invalid_error_number")
            if number:
                issues.append({"code": "engine_error_" + str(number), "source": source})
                # Error.Number is a consuming getter in COM. A second read
                # proves the just-observed operation did not leave an error.
                remaining = self.engine.Error.Number
                if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining:
                    issues.append({"code": "engine_error_not_cleared", "source": source})
        except Exception as exc:
            issues.append({"code": "error_state_read_failed_" + type(exc).__name__, "source": source})
        return issues

    def read(self, source: str, getter: Callable, convert: Callable = lambda value: value) -> dict:
        value = None
        issues = []
        try:
            value = convert(getter())
        except Exception as exc:
            issues.append({"code": "read_failed_" + type(exc).__name__, "source": source})
        finally:
            issues.extend(self.consume(source))
        return {"value": value if not issues else None,
                "status": "not_available" if issues else "available", "source": source, "issues": issues}

    def select(self, target: Any, attribute: str, value: Any, source: str) -> dict:
        def setter():
            setattr(target, attribute, value)
            return True
        return self.read(source, setter, _boolean)


def _invalidate(fields: dict, code: str) -> None:
    for key, cell in tuple(fields.items()):
        fields[key] = _unavailable(cell["source"], code)


def _shape_check(fields: dict) -> None:
    def check(key: str, expected: int | None):
        cell = fields[key]
        if expected is not None and cell["value"] is not None and len(cell["value"]) != expected:
            fields[key] = _unavailable(cell["source"], "dimension_mismatch")
    nt = fields["num_terminals"]["value"]
    nc = fields["num_conductors"]["value"]
    np = fields["num_phases"]["value"]
    check("bus_names", nt)
    check("node_order", nt * nc if nt is not None and nc is not None else None)
    if np is not None and nc is not None and np > nc:
        fields["num_phases"] = _unavailable(fields["num_phases"]["source"], "phases_exceed_conductors")


def _windings(reader: _Reader, circuit: Any, name: str, row: dict) -> None:
    fields, issues = row["fields"], row["issues"]
    tx_cell = reader.read(_TX, lambda: circuit.Transformers)
    if tx_cell["status"] != "available" or tx_cell["value"] is None:
        fields["num_windings"] = _unavailable(_TX + ".NumWindings", "transformer_interface_not_available")
        issues.extend(tx_cell["issues"])
        return
    tx = tx_cell["value"]
    selected = reader.select(tx, "Name", name.split(".", 1)[1], _TX + ".Name:select")
    observed = reader.read(_TX + ".Name", lambda: tx.Name, _text)
    ce_name = reader.read(_CE + "Name", lambda: circuit.ActiveCktElement.Name, _text)
    if (selected["status"] != "available" or observed["value"] is None or
            observed["value"].casefold() != name.split(".", 1)[1].casefold() or
            ce_name["value"] is None or ce_name["value"].casefold() != name.casefold()):
        fields["num_windings"] = _unavailable(_TX + ".NumWindings", "transformer_identity_mismatch")
        issues.append({"code": "transformer_identity_mismatch", "source": _TX + ".Name"})
        row["identity_verified"] = False
        return
    previous = reader.read(_TX + ".Wdg", lambda: tx.Wdg, lambda value: _integer(value, minimum=1))
    fields["num_windings"] = reader.read(_TX + ".NumWindings", lambda: tx.NumWindings,
                                         lambda value: _integer(value, minimum=1))
    count = fields["num_windings"]["value"]
    if previous["value"] is None or count is None:
        issues.append({"code": "winding_selector_not_available", "source": _TX + ".Wdg"})
        return
    if count > 1024:
        fields["num_windings"] = _unavailable(_TX + ".NumWindings", "winding_count_exceeds_collection_limit")
        return
    try:
        for index in range(1, count + 1):
            source = _TX + "[Wdg=" + str(index) + "]"
            winding = {"index": index, "fields": {}, "status": "not_available", "issues": []}
            row["windings"].append(winding)
            selection = reader.select(tx, "Wdg", index, source + ".Wdg:select")
            actual = reader.read(source + ".Wdg", lambda: tx.Wdg, lambda value: _integer(value, minimum=1))
            actual_name = reader.read(_CE + "Name", lambda: circuit.ActiveCktElement.Name, _text)
            if (selection["status"] != "available" or actual["value"] != index or
                    actual_name["value"] is None or actual_name["value"].casefold() != name.casefold()):
                winding["issues"].append({"code": "winding_identity_mismatch", "source": source})
                continue
            wf = winding["fields"]
            for key, attribute, convert in (("conn", "IsDelta", lambda value: "delta" if _boolean(value) else "wye"),
                                            ("kv", "kV", _real), ("kva", "kVA", _real),
                                            ("tap", "Tap", _real), ("percent_r", "R", _real)):
                wf[key] = reader.read(source + "." + attribute, lambda attr=attribute: getattr(tx, attr), convert)
            bus_source = _CE + "BusNames[" + str(index - 1) + "]"
            buses = fields["bus_names"]["value"]
            wf["bus"] = ({"value": buses[index - 1], "status": "available", "source": bus_source, "issues": []}
                         if buses is not None and index <= len(buses) else _unavailable(bus_source, "winding_bus_not_available"))
            final_wdg = reader.read(source + ".Wdg:after_read", lambda: tx.Wdg,
                                    lambda value: _integer(value, minimum=1))
            final_name = reader.read(_CE + "Name:after_winding_read", lambda: circuit.ActiveCktElement.Name, _text)
            if (final_wdg["value"] != index or final_name["value"] is None or
                    final_name["value"].casefold() != name.casefold()):
                _invalidate(wf, "winding_identity_changed_during_read")
                winding["issues"].append({"code": "winding_identity_changed_during_read", "source": source})
            winding["status"] = "available" if wf and not winding["issues"] and all(
                cell["status"] == "available" for cell in wf.values()) else "not_available"
    finally:
        restored = reader.select(tx, "Wdg", previous["value"], _TX + ".Wdg:restore")
        actual = reader.read(_TX + ".Wdg:after_restore", lambda: tx.Wdg, lambda value: _integer(value, minimum=1))
        if restored["status"] != "available" or actual["value"] != previous["value"]:
            issues.append({"code": "winding_selector_restore_failed", "source": _TX + ".Wdg"})


def _collect_object(reader: _Reader, circuit: Any, requested: str) -> dict:
    kind = requested.split(".", 1)[0].casefold()
    row = {"requested_name": requested, "name": None, "class": kind, "identity_verified": False,
           "identity_source": _CE + "Name",
           "status": "not_available", "fields": {}, "windings": [], "issues": []}
    activated = reader.read("OpenDSS.ActiveCircuit.SetActiveElement", lambda: circuit.SetActiveElement(requested),
                            lambda value: _integer(value, minimum=0))
    ce_cell = reader.read("OpenDSS.ActiveCircuit.ActiveCktElement", lambda: circuit.ActiveCktElement)
    ce = ce_cell["value"]
    name = reader.read(_CE + "Name", lambda: ce.Name, _text) if ce is not None else _unavailable(_CE + "Name", "no_active_element")
    row["name"] = name["value"]
    if (activated["status"] != "available" or name["value"] is None or
            name["value"].casefold() != requested.casefold()):
        row["issues"].append({"code": "active_element_identity_mismatch", "source": _CE + "Name"})
        return row
    row["identity_verified"] = True
    fields = row["fields"]
    for key, attribute, convert in (("enabled", "Enabled", _boolean),
                                    ("num_phases", "NumPhases", lambda value: _integer(value, minimum=1)),
                                    ("num_conductors", "NumConductors", lambda value: _integer(value, minimum=1)),
                                    ("num_terminals", "NumTerminals", lambda value: _integer(value, minimum=1)),
                                    ("bus_names", "BusNames", lambda value: _array(value, _text)),
                                    ("node_order", "NodeOrder", lambda value: _array(value, _integer))):
        if (key == "node_order" and kind in {"load", "generator", "pvsystem"}
                and fields["enabled"]["status"] == "available" and fields["enabled"]["value"] is False):
            fields[key] = {"value": None, "status": "not_applicable", "source": _CE + attribute,
                           "issues": [], "reason": "disabled_power_conversion_element_no_node_order"}
        else:
            fields[key] = reader.read(_CE + attribute, lambda attr=attribute: getattr(ce, attr), convert)
    _shape_check(fields)
    if kind in {"line", "transformer"}:
        fields["norm_amps"] = reader.read(_CE + "NormalAmps", lambda: ce.NormalAmps, _real)
    if kind in {"load", "generator", "pvsystem", "vsource"}:
        properties = (("kv", "basekv", _real),) if kind == "vsource" else (
            ("conn", "conn", _connection), ("kv", "kv", _real), ("kva", "kva", _real))
        if kind == "generator":
            properties += (("mva", "mva", _real),)
        for key, prop, convert in properties:
            fields[key] = reader.read(_CE + "Properties(" + prop + ").Val",
                                      lambda property_name=prop: ce.Properties(property_name).Val, convert)
    # Nested readers select the requested object again. First prove that the
    # preceding shared-element reads did not drift to another active object;
    # a later successful re-selection cannot repair their lost provenance.
    before_nested = reader.read(_CE + "Name:before_nested_read", lambda: circuit.ActiveCktElement.Name, _text)
    if before_nested["value"] is None or before_nested["value"].casefold() != requested.casefold():
        row["identity_verified"] = False
        _invalidate(fields, "active_element_changed_before_nested_read")
        row["issues"].append({"code": "active_element_changed_before_nested_read", "source": _CE + "Name"})
        return row
    if kind == "transformer":
        _windings(reader, circuit, requested, row)
    if kind in {"load", "generator", "pvsystem"}:
        from .voltage_ratings import collect_voltage_rating
        row["voltage_rating"] = collect_voltage_rating(reader, circuit, requested)
        row["issues"].extend(issue for issue in row["voltage_rating"]["issues"] if issue.get("code") in {
            "voltage_identity_mismatch", "voltage_identity_changed_during_read",
            "voltage_selector_restore_failed", "voltage_active_element_restore_failed",
            "voltage_selector_before_read_unavailable"})
        from .power_setpoints import collect_power_setpoints
        row["power_setpoints"] = collect_power_setpoints(reader, circuit, requested)
        # A missing optional getter stays inside the additive block for old
        # consumers. Identity drift or failed restoration is never optional.
        row["issues"].extend(issue for issue in row["power_setpoints"]["issues"] if issue.get("code") in {
            "power_identity_mismatch", "power_identity_changed_during_read",
            "power_selector_restore_failed", "power_active_element_restore_failed"})
    final_name = reader.read(_CE + "Name:after_read", lambda: circuit.ActiveCktElement.Name, _text)
    if final_name["value"] is None or final_name["value"].casefold() != requested.casefold() or not row["identity_verified"]:
        row["identity_verified"] = False
        _invalidate(fields, "active_element_changed_during_read")
        for winding in row["windings"]:
            _invalidate(winding["fields"], "active_element_changed_during_read")
            winding["status"] = "not_available"
        row["issues"].append({"code": "active_element_changed_during_read", "source": _CE + "Name"})
    elif not row["issues"] and all(cell["status"] == "available" for cell in fields.values()) and all(
            winding["status"] == "available" for winding in row["windings"]):
        row["status"] = "available"
    else:
        row["status"] = "partial"
    return row


def collect_device_connections(engine: Any, *, evidence_dir=None) -> dict:
    """Collect actual device fields, preserving selectors when obtainable.

    Missing and invalid readings remain null with per-field issues. Object and
    winding selections are verified before/after reading. The original active
    element and transformer selector are restored in ``finally``; inability to
    restore is explicitly recorded, never hidden as a successful observation.
    """
    reader = _Reader(engine)
    result = {"schema_version": DEVICE_CONNECTION_SCHEMA_VERSION, "source": "OpenDSS.COM",
              "scope": "read_only_native_device_connections", "status": "not_available",
              "engineering_accepted": False, "objects": [], "coverage": {}, "issues": []}
    issues = result["issues"]
    issues.extend(reader.consume("OpenDSS.Error.Number:before_collection"))
    circuit_cell = reader.read("OpenDSS.ActiveCircuit", lambda: engine.ActiveCircuit)
    circuit = circuit_cell["value"]
    if circuit is None:
        issues.extend(circuit_cell["issues"] or [{"code": "no_active_circuit", "source": "OpenDSS.ActiveCircuit"}])
        result["coverage"] = {"enumerated_count": None, "supported_count": None, "collected_count": 0}
        return result
    original = reader.read(_CE + "Name:before_collection", lambda: circuit.ActiveCktElement.Name, _text)
    original_tx = reader.read(_TX, lambda: circuit.Transformers)
    tx = original_tx["value"]
    tx_name = reader.read(_TX + ".Name:before_collection", lambda: tx.Name, _text) if tx is not None else None
    tx_wdg = (reader.read(_TX + ".Wdg:before_collection", lambda: tx.Wdg, lambda value: _integer(value, minimum=1))
              if tx_name is not None and tx_name["value"] is not None else None)
    names = reader.read("OpenDSS.ActiveCircuit.AllElementNames", lambda: circuit.AllElementNames,
                        lambda value: _array(value, _text))
    coverage = {"enumerated_count": len(names["value"]) if names["value"] is not None else None,
                "supported_count": None, "collected_count": 0, "unsupported_names": [],
                "element_names": names["value"], "enumeration_status": names["status"],
                "enumeration_source": "OpenDSS.ActiveCircuit.AllElementNames"}
    result["coverage"] = coverage
    try:
        if names["value"] is None:
            issues.extend(names["issues"])
            return result
        selected = []
        seen = set()
        for name in names["value"]:
            if name.casefold() in seen:
                issues.append({"code": "duplicate_element_identity", "source": "OpenDSS.ActiveCircuit.AllElementNames"})
                continue
            seen.add(name.casefold())
            if "." not in name or not name.split(".", 1)[1]:
                issues.append({"code": "invalid_element_identity", "source": "OpenDSS.ActiveCircuit.AllElementNames"})
            elif name.split(".", 1)[0].casefold() in SUPPORTED_DEVICE_CLASSES:
                selected.append(name)
            else:
                coverage["unsupported_names"].append(name)
        coverage["supported_count"] = len(selected)
        for name in selected:
            result["objects"].append(_collect_object(reader, circuit, name))
        coverage["collected_count"] = len(result["objects"])
    finally:
        if tx is not None and tx_name is not None and tx_name["value"] is not None:
            restored = reader.select(tx, "Name", tx_name["value"], _TX + ".Name:restore_original")
            observed = reader.read(_TX + ".Name:after_restore_original", lambda: tx.Name, _text)
            if restored["status"] != "available" or observed["value"] is None or observed["value"].casefold() != tx_name["value"].casefold():
                issues.append({"code": "transformer_selector_restore_failed", "source": _TX + ".Name"})
            elif tx_wdg is not None and tx_wdg["value"] is not None:
                restored_wdg = reader.select(tx, "Wdg", tx_wdg["value"], _TX + ".Wdg:restore_original")
                observed_wdg = reader.read(_TX + ".Wdg:after_restore_original", lambda: tx.Wdg,
                                           lambda value: _integer(value, minimum=1))
                if restored_wdg["status"] != "available" or observed_wdg["value"] != tx_wdg["value"]:
                    issues.append({"code": "winding_selector_restore_failed", "source": _TX + ".Wdg"})
        if original["value"] is not None:
            restored = reader.read("OpenDSS.ActiveCircuit.SetActiveElement:restore_original",
                                   lambda: circuit.SetActiveElement(original["value"]), lambda value: _integer(value, minimum=0))
            observed = reader.read(_CE + "Name:after_restore_original", lambda: circuit.ActiveCktElement.Name, _text)
            if restored["status"] != "available" or observed["value"] is None or observed["value"].casefold() != original["value"].casefold():
                issues.append({"code": "active_element_restore_failed", "source": _CE + "Name"})
        else:
            issues.append({"code": "original_active_element_not_available", "source": _CE + "Name"})
        issues.extend(reader.consume("OpenDSS.Error.Number:after_collection"))
    result["status"] = ("available" if not issues and all(row["status"] == "available" for row in result["objects"])
                        else "partial" if result["objects"] else "not_available")
    from .snapshot_state import collect_snapshot_state
    result["snapshot_state"] = collect_snapshot_state(engine)
    from .passive_stamp import collect_passive_stamp
    result["passive_stamp"] = collect_passive_stamp(engine)
    from .pv_controls import collect_pv_controls
    result["pv_controls"] = collect_pv_controls(engine,evidence_dir)
    if result["pv_controls"].get("cim_evidence") is not None:
        after_export=collect_snapshot_state(engine)
        if after_export != result["snapshot_state"]:
            result["issues"].append({"code":"snapshot_state_changed_during_cim_export", "source":"OpenDSS.CIM100"})
            result["pv_controls"]["state_unchanged_during_export"]=False
        else:
            result["pv_controls"]["state_unchanged_during_export"]=True
        result["snapshot_state"]=after_export
        after_stamp=collect_passive_stamp(engine)
        if after_stamp != result["passive_stamp"]:
            result["issues"].append({"code":"passive_stamp_changed_during_cim_export", "source":"OpenDSS.CIM100"})
            result["pv_controls"]["state_unchanged_during_export"]=False
        result["passive_stamp"]=after_stamp
    return result
