"""Read-only compiled passive admittance; no source interpretation or solving.

Copyright (c) yangshaopeng. Yprim is the engine's actual compiled stamp, not
proof that an arbitrary LineCode or winding declaration was correctly mapped.
Only object/winding selectors are restored. No electrical setter is used.
"""
from __future__ import annotations

import math

VERSION = "1.1"
SCOPE = "read_only_native_compiled_passive_admittance"
CLASSES = frozenset({"line", "transformer", "vsource", "capacitor", "reactor"})
CE = "OpenDSS.ActiveCircuit.ActiveCktElement."
INVENTORY = "OpenDSS.ActiveCircuit.AllElementNames"
MAX_ORDER = 64
MAX_OBJECTS = 10000
CONFIGURATION_SCOPE = "read_only_native_passive_configuration_text"
CONFIGURATION_FIELDS = {
    "line": ("length","units","r1","x1","r0","x0","c1","c0","rmatrix","xmatrix","cmatrix","rg","xg","rho","basefreq","switch","linecode"),
    "vsource": ("r1","x1","r0","x0","basefreq","model","sequence","scantype"),
    "transformer": ("buses","conns","kvs","kvas","taps","%rs","xhl","xht","xlt","%imag","%noloadloss","ppm_antifloat","leadlag","basefreq"),
    "capacitor": ("kv","kvar","conn","numsteps","states","r","xl","basefreq"),
    "reactor": ("kv","kvar","conn","r","basefreq"),
}
PROPERTY_PRECISION = {"kind": "native_property_text", "full_precision_equality_claimed": False}


def _text(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or not value.strip():
        raise ValueError("bounded_nonempty_text_required")
    return value.casefold()


def _integer(value):
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise ValueError("native_nonnegative_integer_required")
    return value


def _dimension(value):
    if type(value) is not int or not 1 <= value <= MAX_ORDER:
        raise ValueError("native_positive_dimension_required")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("native_boolean_required")
    return value


def _array(value, convert, maximum):
    if not isinstance(value, (tuple, list)) or len(value) > maximum:
        raise ValueError("bounded_native_array_required")
    return [convert(item) for item in value]


def _number(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("finite_typed_native_double_required")
    return float(value)


def _names(value):
    names = _array(value, _text, MAX_OBJECTS)
    if not names or len(names) != len(set(names)) or any("." not in name or not all(name.split(".", 1)) for name in names):
        raise ValueError("unique_native_element_identities_required")
    return sorted(names)


def _marker(source, reason):
    return {"value": None, "status": "not_applicable", "source": source, "issues": [], "reason": reason}


def _property_text(value):
    # Empty LineCode names are legitimate native strings. Do not invent a
    # numeric precision or silently normalize formatting from this getter.
    if not isinstance(value,str) or len(value)>1048576 or "\x00" in value:
        raise ValueError("bounded_native_property_text_required")
    return value


def _configuration(reader,circuit,element,row):
    requested=row["object_id"]
    config={"scope":CONFIGURATION_SCOPE,"status":"not_available","issues":[],"fields":{},"windings":[]}
    row["configuration"]=config
    def property_cell(key,tx=None,winding=None):
        source=CE+"Properties("+key+").Val"
        before=reader.read(CE+"Name:before_passive_property",lambda:circuit.ActiveCktElement.Name,_text)
        before_wdg=reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg",lambda:tx.Wdg,_dimension) if tx is not None else None
        cell=reader.read(source,lambda:element.Properties(key).Val,_property_text)
        after=reader.read(CE+"Name:after_passive_property",lambda:circuit.ActiveCktElement.Name,_text)
        after_wdg=reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg",lambda:tx.Wdg,_dimension) if tx is not None else None
        if (before["status"]!="available" or before["value"]!=requested or after["status"]!="available" or after["value"]!=requested
                or tx is not None and (before_wdg["status"]!="available" or before_wdg["value"]!=winding
                    or after_wdg["status"]!="available" or after_wdg["value"]!=winding)):
            cell.update(value=None,status="not_available",issues=cell["issues"]+[{"code":"passive_configuration_property_identity_untrusted","source":source}])
        cell.update(raw_text=cell["value"],precision=dict(PROPERTY_PRECISION))
        return cell
    for key in CONFIGURATION_FIELDS[row["class"]]:
        config["fields"][key]=property_cell(key)
    if row["class"]=="transformer":
        tx_cell=reader.read("OpenDSS.ActiveCircuit.Transformers",lambda:circuit.Transformers)
        tx=tx_cell["value"]
        if tx_cell["status"]!="available" or tx is None:
            config["issues"].append({"code":"passive_configuration_transformer_unavailable"})
        else:
            name=reader.read("OpenDSS.ActiveCircuit.Transformers.Name",lambda:tx.Name,_text)
            previous=reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg",lambda:tx.Wdg,_dimension)
            count=reader.read("OpenDSS.ActiveCircuit.Transformers.NumWindings",lambda:tx.NumWindings,_dimension)
            if (name["status"]!="available" or name["value"]!=requested.split(".",1)[1]
                    or previous["status"]!="available" or count["status"]!="available"
                    or count["value"]!=row["fields"].get("num_terminals",{}).get("value")):
                config["issues"].append({"code":"passive_configuration_winding_identity_untrusted"})
            else:
                try:
                    for index in range(1,count["value"]+1):
                        selected=reader.select(tx,"Wdg",index,"OpenDSS.ActiveCircuit.Transformers.Wdg:passive_configuration_select")
                        actual=reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg",lambda:tx.Wdg,_dimension)
                        winding={"index":index,"selector_source":"OpenDSS.ActiveCircuit.Transformers.Wdg","fields":{}}
                        config["windings"].append(winding)
                        if selected["status"]!="available" or actual["value"]!=index:
                            config["issues"].append({"code":"passive_configuration_winding_selection_failed"}); continue
                        for key in ("rneut","xneut"):
                            winding["fields"][key]=property_cell(key,tx,index)
                        final=reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg",lambda:tx.Wdg,_dimension)
                        final_name=reader.read("OpenDSS.ActiveCircuit.Transformers.Name",lambda:tx.Name,_text)
                        if final["status"]!="available" or final["value"]!=index or final_name["value"]!=name["value"]:
                            config["issues"].append({"code":"passive_configuration_winding_changed_during_read"})
                finally:
                    restored=reader.select(tx,"Wdg",previous["value"],"OpenDSS.ActiveCircuit.Transformers.Wdg:passive_configuration_restore")
                    actual=reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg",lambda:tx.Wdg,_dimension)
                    if restored["status"]!="available" or actual["value"]!=previous["value"]:
                        config["issues"].append({"code":"passive_configuration_winding_restore_failed"})
    cells=list(config["fields"].values())+[cell for winding in config["windings"] for cell in winding["fields"].values()]
    config["status"]="available" if not config["issues"] and all(cell["status"]=="available" for cell in cells) else "not_available"
    if config["status"]!="available":
        row["issues"].append({"code":"passive_configuration_unavailable"})


def collect_passive_stamp(engine):
    """Observe complete typed Yprim or an explicit disabled marker, never zero-fill."""
    from .device_connections import _Reader
    reader = _Reader(engine)
    result = {"version": VERSION, "scope": SCOPE, "source": "OpenDSS.COM", "status": "not_available",
        "electrical_parameters_modified": False, "selection_restored": False, "issues": [], "objects": [],
        "coverage": {"source": INVENTORY, "status": "not_available", "element_names": [], "passive_object_ids": []}}
    issues = result["issues"]
    issues.extend(reader.consume("passive_stamp:before_collection"))
    if issues:
        return result
    circuit_cell = reader.read("OpenDSS.ActiveCircuit", lambda: engine.ActiveCircuit)
    circuit = circuit_cell["value"]
    if circuit_cell["status"] != "available" or circuit is None:
        issues.extend(circuit_cell["issues"] or [{"code": "passive_stamp_circuit_unavailable"}])
        return result
    original = reader.read(CE + "Name:before_passive_stamp", lambda: circuit.ActiveCktElement.Name, _text)
    inventory = reader.read(INVENTORY, lambda: circuit.AllElementNames, _names)
    if original["status"] != "available" or inventory["status"] != "available":
        issues.extend(original["issues"] + inventory["issues"])
        return result
    selected = [name for name in inventory["value"] if name.split(".", 1)[0] in CLASSES]
    if not selected:
        issues.append({"code": "passive_stamp_passive_inventory_empty"})
        return result
    tx = tx_name = tx_wdg = None
    if any(name.startswith("transformer.") for name in selected):
        cell = reader.read("OpenDSS.ActiveCircuit.Transformers", lambda: circuit.Transformers)
        tx = cell["value"]
        if cell["status"] != "available" or tx is None:
            issues.extend(cell["issues"] or [{"code": "passive_stamp_transformer_selector_unavailable"}])
            return result
        tx_name = reader.read("OpenDSS.ActiveCircuit.Transformers.Name:before_passive_stamp", lambda: tx.Name, _text)
        tx_wdg = reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg:before_passive_stamp", lambda: tx.Wdg, _dimension)
        if tx_name["status"] != "available" or tx_wdg["status"] != "available":
            issues.extend(tx_name["issues"] + tx_wdg["issues"])
            return result
    try:
        for requested in selected:
            row = {"object_id": requested, "class": requested.split(".", 1)[0], "identity_verified": False,
                "identity_source": CE + "Name", "status": "not_available", "issues": [], "fields": {}}
            result["objects"].append(row)
            select = reader.read("OpenDSS.ActiveCircuit.SetActiveElement", lambda name=requested: circuit.SetActiveElement(name), _integer)
            active = reader.read("OpenDSS.ActiveCircuit.ActiveCktElement", lambda: circuit.ActiveCktElement)
            element = active["value"]
            identity = reader.read(CE + "Name", lambda: element.Name, _text)
            if select["status"] != "available" or active["status"] != "available" or identity["value"] != requested:
                row["issues"].extend(select["issues"] + active["issues"] + identity["issues"])
                row["issues"].append({"code": "passive_stamp_identity_untrusted"})
                continue
            fields = row["fields"]
            for key, getter, convert in (("enabled", "Enabled", _boolean), ("num_terminals", "NumTerminals", _dimension),
                    ("num_conductors", "NumConductors", _dimension), ("num_phases", "NumPhases", _dimension),
                    ("bus_names", "BusNames", lambda value: _array(value, _text, MAX_ORDER))):
                fields[key] = reader.read(CE + getter, lambda attr=getter: getattr(element, attr), convert)
            nt, nc = fields["num_terminals"]["value"], fields["num_conductors"]["value"]
            order = nt * nc if type(nt) is int and type(nc) is int else None
            if order is None or order > MAX_ORDER or fields["num_phases"]["value"] is None or fields["num_phases"]["value"] > nc:
                row["issues"].append({"code": "passive_stamp_dimensions_untrusted"})
            elif fields["bus_names"]["value"] is None or len(fields["bus_names"]["value"]) != nt:
                row["issues"].append({"code": "passive_stamp_bus_dimensions_mismatch"})
            elif fields["enabled"]["value"] is False:
                fields["node_order"] = _marker(CE + "NodeOrder", "disabled_passive_element_no_node_order")
                fields["y_primitive"] = _marker(CE + "Yprim", "disabled_passive_element_no_admittance")
            elif fields["enabled"]["value"] is True:
                fields["node_order"] = reader.read(CE + "NodeOrder", lambda: element.NodeOrder,
                    lambda value: _array(value, _integer, MAX_ORDER))
                fields["y_primitive"] = reader.read(CE + "Yprim", lambda: element.Yprim,
                    lambda value: _array(value, _number, 2 * MAX_ORDER ** 2))
                fields["y_primitive"].update(precision={"kind": "native_binary_double"},
                    layout="native_interleaved_complex_matrix_vector",matrix_order=order)
                if fields["node_order"]["value"] is None or len(fields["node_order"]["value"]) != order:
                    row["issues"].append({"code": "passive_stamp_node_dimensions_mismatch"})
                if fields["y_primitive"]["value"] is None or len(fields["y_primitive"]["value"]) != 2 * order ** 2:
                    row["issues"].append({"code": "passive_stamp_admittance_dimensions_mismatch"})
            _configuration(reader,circuit,element,row)
            final = reader.read(CE + "Name:after_passive_stamp_object", lambda: circuit.ActiveCktElement.Name, _text)
            if final["status"] != "available" or final["value"] != requested:
                row["issues"].extend(final["issues"])
                row["issues"].append({"code": "passive_stamp_identity_changed_during_read"})
            if any(cell["status"] == "not_available" for cell in fields.values()):
                row["issues"].append({"code": "passive_stamp_native_field_unavailable"})
            row["identity_verified"] = not row["issues"]
            row["status"] = "available" if row["identity_verified"] else "not_available"
    finally:
        if tx is not None:
            restored = reader.select(tx, "Name", tx_name["value"], "OpenDSS.ActiveCircuit.Transformers.Name:restore_passive_stamp")
            observed = reader.read("OpenDSS.ActiveCircuit.Transformers.Name:after_passive_stamp", lambda: tx.Name, _text)
            winding = reader.select(tx, "Wdg", tx_wdg["value"], "OpenDSS.ActiveCircuit.Transformers.Wdg:restore_passive_stamp")
            actual_wdg = reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg:after_passive_stamp", lambda: tx.Wdg, _dimension)
            if restored["status"] != "available" or observed["value"] != tx_name["value"] or winding["status"] != "available" or actual_wdg["value"] != tx_wdg["value"]:
                issues.append({"code": "passive_stamp_transformer_selector_restore_failed"})
        restored = reader.read("OpenDSS.ActiveCircuit.SetActiveElement:restore_passive_stamp", lambda: circuit.SetActiveElement(original["value"]), _integer)
        observed = reader.read(CE + "Name:after_passive_stamp_restore", lambda: circuit.ActiveCktElement.Name, _text)
        result["selection_restored"] = restored["status"] == "available" and observed["value"] == original["value"]
        if not result["selection_restored"]:
            issues.append({"code": "passive_stamp_active_selector_restore_failed"})
    after = reader.read(INVENTORY + ":after_passive_stamp", lambda: circuit.AllElementNames, _names)
    if after["status"] != "available" or after["value"] != inventory["value"]:
        issues.append({"code": "passive_stamp_inventory_changed_during_read"})
    else:
        result["coverage"].update(status="available",element_names=inventory["value"],passive_object_ids=selected)
    issues.extend(reader.consume("passive_stamp:after_collection"))
    result["status"] = "available" if not issues and all(row["status"] == "available" for row in result["objects"]) else "partial"
    return result
