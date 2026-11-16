"""Nominal voltage readback with explicit native precision. Copyright yangshaopeng.

Load/Generator expose a binary-double kV getter. The installed COM PVSystem
interface does not: its property text is retained as six-digit evidence. These
are configured nominal voltages, never solved voltages or engineering limits.
"""
from __future__ import annotations

import math

_CE = "OpenDSS.ActiveCircuit.ActiveCktElement."
_INTERFACES = {"load": "Loads", "generator": "Generators", "pvsystem": "PVSystems"}


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("bounded_native_text_required")
    return value


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("native_number_required")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("positive_finite_voltage_required")
    return value


def collect_voltage_rating(reader, circuit, requested):
    """Read only selectors and original input voltage; no electrical setters."""
    kind = requested.split(".", 1)[0].casefold()
    prefix = "OpenDSS.ActiveCircuit." + _INTERFACES[kind]
    result = {"version": "1.0", "scope": "read_only_native_nominal_voltage",
              "object_id": requested, "class": kind, "identity_verified": False,
              "identity_source": prefix + ".Name", "status": "not_available",
              "electrical_parameters_modified": False, "fields": {}, "issues": []}
    interface = reader.read(prefix, lambda: getattr(circuit, _INTERFACES[kind]))
    collection = interface["value"]
    if interface["status"] != "available" or collection is None:
        result["issues"].append({"code": "voltage_interface_unavailable", "source": prefix})
        return result
    previous = reader.read(prefix + ".Name:before_voltage_read", lambda: collection.Name, _text)
    try:
        selected = reader.select(collection, "Name", requested.split(".", 1)[1], prefix + ".Name:select_voltage")
        identity = reader.read(prefix + ".Name", lambda: collection.Name, _text)
        active = reader.read(_CE + "Name:before_voltage_read", lambda: circuit.ActiveCktElement.Name, _text)
        if (selected["status"] != "available" or identity["value"] is None
                or identity["value"].casefold() != requested.split(".", 1)[1].casefold()
                or active["value"] is None or active["value"].casefold() != requested.casefold()):
            result["issues"].append({"code": "voltage_identity_mismatch", "source": prefix + ".Name"})
            return result
        if kind == "pvsystem":
            source = _CE + "Properties(kv).Val"
            cell = reader.read(source, lambda: circuit.ActiveCktElement.Properties("kv").Val, _text)
            raw = cell["value"]
            if cell["status"] == "available":
                try:
                    cell["value"] = _positive(float(raw))
                except (ValueError, TypeError, OverflowError):
                    cell.update(value=None, status="not_available",
                                issues=[{"code": "invalid_native_voltage_text", "source": source}])
            cell["raw_text"] = raw
            cell["precision"] = {"kind": "native_significant_digits", "digits": 6}
        else:
            cell = reader.read(prefix + ".kV", lambda: collection.kV, _positive)
            cell["precision"] = {"kind": "native_binary_double"}
        result["fields"]["kv"] = cell
        final_name = reader.read(prefix + ".Name:after_voltage_read", lambda: collection.Name, _text)
        final_active = reader.read(_CE + "Name:after_voltage_read", lambda: circuit.ActiveCktElement.Name, _text)
        if (final_name["value"] is None or final_name["value"].casefold() != requested.split(".", 1)[1].casefold()
                or final_active["value"] is None or final_active["value"].casefold() != requested.casefold()):
            result["issues"].append({"code": "voltage_identity_changed_during_read", "source": prefix + ".Name"})
        else:
            result["identity_verified"] = True
    finally:
        if previous["status"] == "available":
            restored = reader.select(collection, "Name", previous["value"], prefix + ".Name:restore_voltage")
            actual = reader.read(prefix + ".Name:after_voltage_restore", lambda: collection.Name, _text)
            if (restored["status"] != "available" or actual["value"] is None
                    or actual["value"].casefold() != previous["value"].casefold()):
                result["issues"].append({"code": "voltage_selector_restore_failed", "source": prefix + ".Name"})
        else:
            result["issues"].append({"code": "voltage_selector_before_read_unavailable", "source": prefix + ".Name"})
        restored_element = reader.read("OpenDSS.ActiveCircuit.SetActiveElement:after_voltage_read",
                                       lambda: circuit.SetActiveElement(requested))
        actual_element = reader.read(_CE + "Name:after_voltage_restore", lambda: circuit.ActiveCktElement.Name, _text)
        if (restored_element["status"] != "available" or actual_element["value"] is None
                or actual_element["value"].casefold() != requested.casefold()):
            result["issues"].append({"code": "voltage_active_element_restore_failed", "source": _CE + "Name"})
        if result["issues"]:
            result["identity_verified"] = False
            for cell in result["fields"].values():
                cell.update(value=None, status="not_available",
                            issues=[{"code": "voltage_identity_or_restore_error", "source": cell["source"]}])
    if result["identity_verified"] and not result["issues"] and set(result["fields"]) == {"kv"}:
        result["status"] = "available" if result["fields"]["kv"]["status"] == "available" else "partial"
    return result
