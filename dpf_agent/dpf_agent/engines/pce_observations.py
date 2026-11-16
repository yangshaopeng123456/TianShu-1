"""Read-only Load/Generator/PVSystem terminal inventory. Copyright yangshaopeng."""
from __future__ import annotations

from .terminal_observations import collect_terminal_data

_CLASSES = {"load": "Loads", "generator": "Generators", "pvsystem": "PVSystems"}
_MAX = 100000


def collect_power_conversion_elements(engine, *, requested: bool):
    """Enumerate disabled objects too; restore the active element on every path."""
    sources = {kind: "OpenDSS.ActiveCircuit." + interface + ".Count"
               for kind, interface in _CLASSES.items()}
    manifest = {"version": "1.0", "scope": "load_generator_pvsystem_terminal_observations",
                "source": "OpenDSS.ActiveCircuit.AllElementNames",
                "status": "not_requested", "expected_names": [], "errors": [],
                "class_counts": None, "class_counts_source": sources}
    if not requested:
        return None, manifest
    errors, rows = manifest["errors"], []
    circuit = engine.ActiveCircuit
    original = None

    def read(getter, field):
        value = None
        try:
            value = getter()
        except Exception as exc:
            errors.append({"field": field, "code": "read_failed_" + type(exc).__name__})
        try:
            number = int(engine.Error.Number)
            if number:
                errors.append({"field": field, "code": "engine_property_error_" + str(number)})
                value = None
        except Exception as exc:
            errors.append({"field": field, "code": "error_state_read_failed_" + type(exc).__name__})
            value = None
        return value

    def inventory(field):
        names = read(lambda: circuit.AllElementNames, field)
        if (not isinstance(names, (list, tuple)) or len(names) > _MAX
                or any(not isinstance(name, str) or "." not in name
                       or not name.split(".", 1)[1] or len(name) > 256 for name in names)
                or len({name.casefold() for name in names}) != len(names)):
            errors.append({"field": field, "code": "invalid_full_element_inventory"})
            return None
        return list(names)

    try:
        original = read(lambda: circuit.ActiveCktElement.Name, "active_element_before")
        if not isinstance(original, str) or not original:
            errors.append({"field": "active_element_before", "code": "restoration_identity_missing"})
            original = None
        before = inventory("AllElementNames_before")
        counts = {kind: read(lambda attr=interface: getattr(circuit, attr).Count, kind + ".Count")
                  for kind, interface in _CLASSES.items()}
        manifest["class_counts"] = counts
        if before is not None:
            names = [name for name in before if name.split(".", 1)[0].casefold() in _CLASSES]
            manifest["expected_names"] = names
            for kind, count in counts.items():
                if (type(count) is not int or not 0 <= count <= _MAX
                        or count != sum(name.split(".", 1)[0].casefold() == kind for name in names)):
                    errors.append({"field": kind + ".Count", "code": "inventory_class_count_mismatch"})
            for name in names:
                data = collect_terminal_data(circuit, name, engine=engine)
                connection_errors = []
                start = len(errors)
                value = read(lambda: circuit.ActiveCktElement.Properties("conn").Val, name + ".conn") if data["identity_verified"] else None
                connection_errors.extend(errors[start:])
                normalized = value.strip().casefold() if isinstance(value, str) else None
                normalized = {"wye": "wye", "y": "wye", "ln": "wye", "delta": "delta", "ll": "delta"}.get(normalized)
                if normalized is None:
                    connection_errors.append({"field": "conn", "code": "connection_not_available"})
                final_name = read(lambda: circuit.ActiveCktElement.Name, name + ".Name_after_connection")
                if not isinstance(final_name, str) or final_name.casefold() != name.casefold():
                    errors.append({"field": name, "code": "active_element_changed_during_connection_read"})
                rows.append({"name": name, "class": name.split(".", 1)[0].casefold(),
                             "connection": {"value": normalized, "status": "available" if normalized and not connection_errors else "not_available",
                                            "source": "OpenDSS.ActiveCktElement.Properties(conn).Val", "errors": connection_errors},
                             "terminal_data": data})
            after = inventory("AllElementNames_after")
            if after is None or {name.casefold() for name in after} != {name.casefold() for name in before}:
                errors.append({"field": "AllElementNames_after", "code": "inventory_changed_during_read"})
    finally:
        if original is not None:
            activated = read(lambda: circuit.SetActiveElement(original), "restore_active_element")
            restored = read(lambda: circuit.ActiveCktElement.Name, "active_element_after_restore")
            if (type(activated) is not int or activated < 0 or not isinstance(restored, str)
                    or restored.casefold() != original.casefold()):
                errors.append({"field": "restore_active_element", "code": "restoration_not_verified"})
    manifest["status"] = "available" if not errors else "not_available"
    return rows, manifest
