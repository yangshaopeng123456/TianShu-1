"""Identity-checked OpenDSS COM observations with explicit raw array semantics.

Copyright (c) yangshaopeng. Collection does not approve engineering limits or
assign phase labels to arbitrary OpenDSS node numbers.
"""
from __future__ import annotations

import math
from typing import Any, Callable


OBSERVATION_SCHEMA_VERSION = "2.0"


def _real(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not_numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("not_finite")
    return result


def _frequency(value: Any) -> float:
    # Generic DSS property values are strings; they are not inferred defaults.
    if isinstance(value, str):
        value = float(value.strip())
    result = _real(value)
    if result <= 0:
        raise ValueError("frequency_not_positive")
    return result


def _option_boolean(value: Any) -> bool:
    if not isinstance(value, str):
        raise ValueError("option_boolean_not_text")
    text = value.strip().casefold()
    if text in {"yes", "true", "1"}:
        return True
    if text in {"no", "false", "0"}:
        return False
    raise ValueError("option_boolean_not_recognized")


def read_engine_option(engine: Any, option: str, *, boolean=False) -> dict:
    """Query a DSS option, consuming its own Error.Number even on failure."""
    query = "get " + option
    errors: list[dict[str, str]] = []
    value = None
    try:
        engine.Text.Command = query
        number = int(engine.Error.Number)
        text = engine.Text.Result
        if number:
            errors.append({"field": option, "code": "engine_query_error_" + str(number)})
        else:
            value = (_option_boolean if boolean else _frequency)(text)
    except Exception as exc:
        errors.append({"field": option, "code": "query_failed_" + type(exc).__name__})
    finally:
        try:
            remaining = int(engine.Error.Number)
            if remaining:
                errors.append({"field": option, "code": "query_error_consumed_" + str(remaining)})
        except Exception as exc:
            errors.append({"field": option, "code": "error_state_read_failed_" + type(exc).__name__})
    return {"value": value, "status": "available" if value is not None and not errors else "not_available",
            "source": "OpenDSS.Text.Command:" + query, "errors": errors}


def configure_registry_isolation(engine: Any, *, readback=False) -> dict:
    """Disable only host-setting persistence; never set an electrical frequency.

    A fresh engine may have no Circuit, so startup verification uses the command
    error code. Once a circuit exists, callers request an actual option readback.
    Unsupported/rejected setters raise. Some COM versions return invalid text for
    Get RegistryUpdate; an accepted setter then remains configured, unconfirmed.
    """
    command = "set registryupdate=no"
    try:
        engine.Text.Command = command
        number = int(engine.Error.Number)
    except Exception as exc:
        try:
            _ = engine.Error.Number
        except Exception:
            pass
        raise RuntimeError("RegistryUpdate=No could not be applied") from exc
    if number:
        raise RuntimeError("RegistryUpdate=No rejected by engine: " + str(number))
    result = {"status": "configured", "registry_updates_allowed": False,
              "source": "OpenDSS.Text.Command:" + command,
              "verification_source": None, "readback_confirmed": False, "errors": []}
    if readback:
        observed = read_engine_option(engine, "registryupdate", boolean=True)
        result["verification_source"] = observed["source"]
        if observed["status"] != "available":
            result["errors"] = observed["errors"]
        elif observed["value"] is not False:
            raise RuntimeError("RegistryUpdate=No contradicted by actual readback")
        else:
            result.update(status="enabled", readback_confirmed=True)
    return result


def collect_frequency_context(engine: Any, compile_context: dict | None) -> dict:
    """Preserve actual solution/default contexts; missing readings stay null."""
    context = compile_context or {}
    errors: list[dict[str, str]] = []
    frequency = _read(lambda: engine.ActiveCircuit.Solution.Frequency, "solution_frequency_hz", errors, _frequency)
    try:
        number = int(engine.Error.Number)
        if number:
            errors.append({"field": "solution_frequency_hz", "code": "engine_property_error_" + str(number)})
            frequency = None
    except Exception as exc:
        errors.append({"field": "solution_frequency_hz", "code": "error_state_read_failed_" + type(exc).__name__})
    result = {"schema_version": "1.0", "solution_frequency_hz": frequency,
              "source": {"solution_frequency_hz": "OpenDSS.ActiveCircuit.Solution.Frequency"}, "errors": errors}
    for key, phase in (("default_base_frequency_before_compile_hz", "before_compile"),
                       ("default_base_frequency_after_compile_hz", "after_compile")):
        observed = context.get(phase, {})
        result[key] = observed.get("value") if observed.get("status") == "available" else None
        result["source"][key] = observed.get("source")
        errors.extend({"field": key, "code": item["code"]} for item in observed.get("errors", []))
        if result[key] is None and not observed.get("errors"):
            errors.append({"field": key, "code": "compile_context_not_available"})
    isolation = dict(context.get("registry_isolation", {"status": "not_available", "errors": [{"field": "registry_isolation", "code": "initialization_context_not_available"}]}))
    result["registry_isolation"] = isolation
    result["status"] = "available" if not errors and all(result[key] is not None for key in (
        "solution_frequency_hz", "default_base_frequency_before_compile_hz", "default_base_frequency_after_compile_hz")) else "not_available"
    return result


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("not_nonnegative_integer")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("not_nonempty_text")
    return value


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("not_boolean")
    return value


def _array(value: Any, convert: Callable) -> list:
    if not isinstance(value, (tuple, list)):
        raise ValueError("not_array")
    return [convert(item) for item in value]


def _read(getter: Callable, field: str, errors: list, convert: Callable):
    try:
        return convert(getter())
    except Exception as exc:
        errors.append({"field": field, "code": "read_failed_" + type(exc).__name__})
        return None


def _length(value, count, field, errors):
    if value is not None and count is not None and len(value) != count:
        errors.append({"field": field, "code": "dimension_mismatch"})


def _polar(values, field, errors):
    if values is None or len(values) % 2:
        return None, None
    magnitudes = [math.hypot(real, imag) for real, imag in zip(values[::2], values[1::2])]
    if not all(math.isfinite(value) for value in magnitudes):
        errors.append({"field": field, "code": "derived_magnitude_not_finite"})
        return None, None
    # This is a documented mathematical conversion, not a second engine read.
    # atan2(0, 0) is represented as 0 degrees; original complex zeros remain.
    angles = [math.degrees(math.atan2(imag, real)) for real, imag in zip(values[::2], values[1::2])]
    return magnitudes, angles


def collect_bus_observation(circuit: Any, requested_name: str) -> dict:
    """Read actual node identities and node-to-ground voltages in COM order."""
    errors: list[dict[str, str]] = []
    row = {"name": requested_name, "requested_bus_name": requested_name,
           "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
           "observation_status": "not_available", "identity_verified": False,
           "num_nodes": None, "nodes": None, "base_kv": None,
           "voltage_base_ln_kv": None, "voltage_base_basis": "line_to_neutral",
           "base_source": "OpenDSS.ActiveBus.kVBase", "voltage_basis": "node_to_ground",
           "vmag_angle_v": None, "pu_vmag_angle": None,
           "voltage_mag_v": None, "voltage_angle_deg": None,
           "voltage_pu": None, "angle_deg": None, "observation_errors": errors}
    activated = _read(lambda: circuit.SetActiveBus(requested_name), "SetActiveBus", errors, int)
    bus = _read(lambda: circuit.ActiveBus, "ActiveBus", errors, lambda value: value)
    if bus is None:
        return row
    name = _read(lambda: bus.Name, "Name", errors, _text)
    if name is not None:
        row["name"] = name
    if activated is None or activated < 0 or name is None or name.casefold() != requested_name.casefold():
        errors.append({"field": "Name", "code": "active_bus_identity_mismatch"})
        return row
    row["identity_verified"] = True
    row["num_nodes"] = _read(lambda: bus.NumNodes, "NumNodes", errors, _integer)
    row["nodes"] = _read(lambda: bus.Nodes, "Nodes", errors, lambda value: _array(value, _integer))
    row["voltage_base_ln_kv"] = _read(lambda: bus.kVBase, "kVBase", errors, _real)
    row["vmag_angle_v"] = _read(lambda: bus.VMagAngle, "VMagAngle", errors, lambda value: _array(value, _real))
    row["pu_vmag_angle"] = _read(lambda: bus.puVmagAngle, "puVmagAngle", errors, lambda value: _array(value, _real))
    count = row["num_nodes"]
    if count == 0:
        errors.append({"field": "NumNodes", "code": "no_nodes"})
    _length(row["nodes"], count, "Nodes", errors)
    for key in ("vmag_angle_v", "pu_vmag_angle"):
        values = row[key]
        _length(values, 2 * count if count is not None else None, key, errors)
        if values is not None and len(values) % 2 == 0:
            if any(value < 0 for value in values[::2]):
                errors.append({"field": key, "code": "negative_magnitude"})
            if key == "vmag_angle_v":
                row["voltage_mag_v"], row["voltage_angle_deg"] = values[::2], values[1::2]
            else:
                row["voltage_pu"], row["angle_deg"] = values[::2], values[1::2]
    nodes = row["nodes"]
    if nodes is not None and len(nodes) != len(set(nodes)):
        errors.append({"field": "Nodes", "code": "duplicate_node_identity"})
    base = row["voltage_base_ln_kv"]
    if base is not None and base <= 0:
        errors.append({"field": "kVBase", "code": "nonpositive_voltage_base"})
    # Preserve the former scalar convention solely for old consumers. New
    # observations use voltage_base_ln_kv without inferring LL from node count.
    if base and nodes is not None:
        row["base_kv"] = base * math.sqrt(3) if len(nodes) >= 2 else base
    final_name = _read(lambda: bus.Name, "Name_after_read", errors, _text)
    if final_name is None or final_name.casefold() != requested_name.casefold():
        row["identity_verified"] = False
        errors.append({"field": "Name", "code": "active_bus_changed_during_read"})
    row["observation_status"] = "available" if not errors else "not_available"
    return row


def collect_terminal_data(circuit: Any, requested_name: str, *, engine: Any = None) -> dict:
    """Collect all conductor/terminal values, never inferring missing topology.

    Layout index is ``(terminal-1)*num_conductors+(conductor-1)``. Voltages
    and currents are original interleaved real/imaginary values in V and A;
    Powers is original interleaved kW/kvar flowing into each conductor.
    """
    errors: list[dict[str, str]] = []
    def read(getter, field, target, convert):
        value = _read(getter, field, target, convert)
        if engine is not None:
            try:
                number = int(engine.Error.Number)
                if number:
                    target.append({"field": field, "code": "engine_property_error_" + str(number)})
                    value = None
            except Exception as exc:
                target.append({"field": field, "code": "error_state_read_failed_" + type(exc).__name__})
                value = None
        return value
    row = {"schema_version": OBSERVATION_SCHEMA_VERSION, "status": "not_available",
           "requested_element_name": requested_name, "active_element_name": None,
           "identity_verified": False, "enabled": None,
           "num_terminals": None, "num_conductors": None, "num_phases": None,
           "node_order": None, "bus_names": None,
           "voltages_complex_v": None, "currents_complex_a": None, "powers_kw_kvar": None,
           "voltages_mag_v": None, "voltages_angle_deg": None,
           "currents_mag_a": None, "currents_angle_deg": None,
           "powers_kw": None, "powers_kvar": None,
           "layout": "terminal_major_conductor_minor", "power_sign": "into_element",
           "voltage_basis": "node_to_ground", "source": "OpenDSS.ActiveCktElement",
           "polar_derivation": "from_complex_hypot_atan2", "errors": errors}
    frequency_errors: list[dict[str, str]] = []
    row.update(base_frequency_hz=None, base_frequency_status="not_available",
               base_frequency_source="OpenDSS.ActiveCktElement.Properties(basefreq).Val",
               base_frequency_errors=frequency_errors)
    activated = read(lambda: circuit.SetActiveElement(requested_name), "SetActiveElement", errors, int)
    ce = read(lambda: circuit.ActiveCktElement, "ActiveCktElement", errors, lambda value: value)
    if ce is None:
        return row
    name = read(lambda: ce.Name, "Name", errors, _text)
    row["active_element_name"] = name
    if activated is None or activated < 0 or name is None or name.casefold() != requested_name.casefold():
        errors.append({"field": "Name", "code": "active_element_identity_mismatch"})
        return row
    row["identity_verified"] = True
    row["enabled"] = read(lambda: ce.Enabled, "Enabled", errors, _boolean)
    for field, attribute in (("num_terminals", "NumTerminals"), ("num_conductors", "NumConductors"), ("num_phases", "NumPhases")):
        row[field] = read(lambda attr=attribute: getattr(ce, attr), attribute, errors, _integer)
        if row[field] == 0:
            errors.append({"field": attribute, "code": "nonpositive_dimension"})
    row["bus_names"] = read(lambda: ce.BusNames, "BusNames", errors, lambda value: _array(value, _text))
    disabled = row["enabled"] is False
    for field, attribute, convert in (() if disabled else (("node_order", "NodeOrder", _integer),
                                       ("voltages_complex_v", "Voltages", _real), ("currents_complex_a", "Currents", _real),
                                       ("powers_kw_kvar", "Powers", _real))):
        row[field] = read(lambda attr=attribute: getattr(ce, attr), attribute, errors, lambda value, cv=convert: _array(value, cv))
    nt, nc, np = row["num_terminals"], row["num_conductors"], row["num_phases"]
    count = nt * nc if nt is not None and nc is not None else None
    if nc is not None and np is not None and nc < np:
        errors.append({"field": "NumPhases", "code": "phases_exceed_conductors"})
    _length(row["bus_names"], nt, "BusNames", errors)
    _length(row["node_order"], count, "NodeOrder", errors)
    for field in ("voltages_complex_v", "currents_complex_a", "powers_kw_kvar"):
        _length(row[field], 2 * count if count is not None else None, field, errors)
    row["voltages_mag_v"], row["voltages_angle_deg"] = _polar(row["voltages_complex_v"], "Voltages", errors)
    row["currents_mag_a"], row["currents_angle_deg"] = _polar(row["currents_complex_a"], "Currents", errors)
    powers = row["powers_kw_kvar"]
    if powers is not None and len(powers) % 2 == 0:
        row["powers_kw"], row["powers_kvar"] = powers[::2], powers[1::2]
    row["base_frequency_hz"] = read(lambda: ce.Properties("basefreq").Val, "base_frequency_hz", frequency_errors, _frequency)
    row["base_frequency_status"] = "available" if row["base_frequency_hz"] is not None and not frequency_errors else "not_available"
    final_name = read(lambda: ce.Name, "Name_after_read", errors, _text)
    if final_name is None or final_name.casefold() != requested_name.casefold():
        row["identity_verified"] = False
        errors.append({"field": "Name", "code": "active_element_changed_during_read"})
    row["status"] = ("disabled" if disabled else "available") if not errors else "not_available"
    if disabled:
        row["reason"] = "disabled_device_no_electrical_observations"
    return row
