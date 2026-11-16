"""Pure fail-closed compiled-stamp validation, not a source-matrix interpreter.

Copyright (c) yangshaopeng. Runtime consistency tolerances are numerical drift
checks, not engineering limits. No engine, file I/O or model edits occur here.
"""
from __future__ import annotations

from collections.abc import Mapping
import math

VERSION = "1.1"
SCOPE = "read_only_native_compiled_passive_admittance"
CLASSES = frozenset({"line", "transformer", "vsource", "capacitor", "reactor"})
CE = "OpenDSS.ActiveCircuit.ActiveCktElement."
INVENTORY = "OpenDSS.ActiveCircuit.AllElementNames"
MAX_ORDER = 64
MAX_OBJECTS = 10000
RELATIVE_TOLERANCE = 1e-10
ABSOLUTE_TOLERANCE = 1e-12
CONFIGURATION_SCOPE = "read_only_native_passive_configuration_text"
CONFIGURATION_FIELDS = {
    "line": {"length","units","r1","x1","r0","x0","c1","c0","rmatrix","xmatrix","cmatrix","rg","xg","rho","basefreq","switch","linecode"},
    "vsource": {"r1","x1","r0","x0","basefreq","model","sequence","scantype"},
    "transformer": {"buses","conns","kvs","kvas","taps","%rs","xhl","xht","xlt","%imag","%noloadloss","ppm_antifloat","leadlag","basefreq"},
    "capacitor": {"kv","kvar","conn","numsteps","states","r","xl","basefreq"},
    "reactor": {"kv","kvar","conn","r","basefreq"},
}
PROPERTY_PRECISION = {"kind":"native_property_text","full_precision_equality_claimed":False}


def _finite(value):
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _text(value):
    return isinstance(value,str) and 0 < len(value) <= 1024 and bool(value.strip())


def _inventory(value):
    return (isinstance(value,list) and len(value)<=MAX_OBJECTS and all(_text(x) and "." in x
        and all(x.split(".",1)) and x==x.casefold() for x in value) and len(value)==len(set(value)) and value==sorted(value))


def _validate(frame, prefix, issues):
    def issue(code,object_id=None,field=None):
        issues.append({"code":prefix+code,**({"object_id":object_id} if object_id else {}),**({"field":field} if field else {})})
    if not isinstance(frame,Mapping):
        issue("frame_missing"); return None
    if (frame.get("version")!=VERSION or frame.get("scope")!=SCOPE or frame.get("source")!="OpenDSS.COM"
            or frame.get("status")!="available" or frame.get("issues")!=[]
            or frame.get("electrical_parameters_modified") is not False or frame.get("selection_restored") is not True):
        issue("frame_header_untrusted"); return None
    coverage=frame.get("coverage")
    if (not isinstance(coverage,Mapping) or coverage.get("source")!=INVENTORY or coverage.get("status")!="available"
            or not _inventory(coverage.get("element_names")) or not _inventory(coverage.get("passive_object_ids"))):
        issue("inventory_untrusted"); return None
    expected=[name for name in coverage["element_names"] if name.split(".",1)[0] in CLASSES]
    rows=frame.get("objects")
    if (not expected or coverage["passive_object_ids"]!=expected or not isinstance(rows,list) or len(rows)!=len(expected)
            or not all(isinstance(row,Mapping) and _text(row.get("object_id")) for row in rows)
            or len({row["object_id"] for row in rows})!=len(rows) or {row["object_id"] for row in rows}!=set(expected)):
        issue("object_inventory_mismatch"); return None
    observed={}
    expected_fields={"enabled","num_terminals","num_conductors","num_phases","bus_names","node_order","y_primitive"}
    getters={"enabled":"Enabled","num_terminals":"NumTerminals","num_conductors":"NumConductors","num_phases":"NumPhases",
             "bus_names":"BusNames","node_order":"NodeOrder","y_primitive":"Yprim"}
    for row in rows:
        object_id=row["object_id"]; fields=row.get("fields")
        if (row.get("class")!=object_id.split(".",1)[0] or row.get("class") not in CLASSES
                or row.get("identity_verified") is not True or row.get("identity_source")!=CE+"Name"
                or row.get("status")!="available" or row.get("issues")!=[]
                or not isinstance(fields,Mapping) or set(fields)!=expected_fields):
            issue("object_header_untrusted",object_id); continue
        valid=True
        for field,getter in getters.items():
            cell=fields[field]
            if not isinstance(cell,Mapping) or cell.get("source")!=CE+getter or cell.get("issues")!=[]:
                issue("field_source_untrusted",object_id,field); valid=False
        if not valid:continue
        disabled=fields["enabled"].get("value") is False
        for field in expected_fields-({"node_order","y_primitive"} if disabled else set()):
            if fields[field].get("status")!="available":issue("field_unavailable",object_id,field); valid=False
        if type(fields["enabled"].get("value")) is not bool:
            issue("enabled_untrusted",object_id); valid=False
        nt,nc,np=(fields[key].get("value") for key in ("num_terminals","num_conductors","num_phases"))
        if not (all(type(x) is int and 1<=x<=MAX_ORDER for x in (nt,nc,np)) and nt*nc<=MAX_ORDER and np<=nc):
            issue("dimensions_untrusted",object_id); continue
        buses=fields["bus_names"].get("value")
        if not isinstance(buses,list) or len(buses)!=nt or not all(_text(x) and x==x.casefold() for x in buses):
            issue("bus_names_untrusted",object_id); valid=False
        if disabled:
            for field,reason in (("node_order","disabled_passive_element_no_node_order"),("y_primitive","disabled_passive_element_no_admittance")):
                expected_cell={"value":None,"status":"not_applicable","source":CE+getters[field],"issues":[],"reason":reason}
                if fields[field]!=expected_cell:
                    issue("disabled_marker_untrusted",object_id,field); valid=False
        else:
            nodes=fields["node_order"].get("value")
            y=fields["y_primitive"]
            values=y.get("value")
            if not isinstance(nodes,list) or len(nodes)!=nt*nc or not all(type(x) is int and 0<=x<=2147483647 for x in nodes):
                issue("node_order_untrusted",object_id); valid=False
            if (y.get("precision")!={"kind":"native_binary_double"} or y.get("layout")!="native_interleaved_complex_matrix_vector"
                    or type(y.get("matrix_order")) is not int or y["matrix_order"]!=nt*nc
                    or not isinstance(values,list) or len(values)!=2*(nt*nc)**2 or not all(_finite(x) for x in values)):
                issue("admittance_untrusted",object_id); valid=False
        config=row.get("configuration")
        if (not isinstance(config,Mapping) or config.get("scope")!=CONFIGURATION_SCOPE or config.get("status")!="available"
                or config.get("issues")!=[] or not isinstance(config.get("fields"),Mapping)
                or set(config["fields"])!=CONFIGURATION_FIELDS[row["class"]] or not isinstance(config.get("windings"),list)):
            issue("configuration_untrusted",object_id); valid=False
        else:
            cells=[(key,cell) for key,cell in config["fields"].items()]
            windings=config["windings"]
            if row["class"]=="transformer":
                if (len(windings)!=nt or any(not isinstance(w,Mapping) or type(w.get("index")) is not int
                        or w["index"]!=index or w.get("selector_source")!="OpenDSS.ActiveCircuit.Transformers.Wdg"
                        or not isinstance(w.get("fields"),Mapping) or set(w["fields"])!={"rneut","xneut"}
                        for index,w in enumerate(windings,1))):
                    issue("configuration_windings_untrusted",object_id); valid=False
                else:cells.extend((key,cell) for winding in windings for key,cell in winding["fields"].items())
            elif windings:
                issue("configuration_unexpected_windings",object_id); valid=False
            for key,cell in cells:
                if (not isinstance(cell,Mapping) or cell.get("source")!=CE+"Properties("+key+").Val"
                        or cell.get("status")!="available" or cell.get("issues")!=[] or cell.get("precision")!=PROPERTY_PRECISION
                        or not isinstance(cell.get("value"),str) or len(cell["value"])>1048576 or "\x00" in cell["value"]
                        or cell.get("raw_text")!=cell["value"]):
                    issue("configuration_property_untrusted",object_id,key); valid=False
        if valid:observed[object_id]=row
    return observed if len(observed)==len(expected) else None


def verify_passive_stamp(native_frame, baseline_frame=None):
    """Validate a direct native block and, when supplied, its frozen first block."""
    issues=[]
    actual=_validate(native_frame,"actual_",issues)
    baseline=_validate(baseline_frame,"baseline_",issues) if baseline_frame is not None else None
    compared=baseline_frame is not None
    compared_values=0
    if compared and actual is not None and baseline is not None:
        if native_frame["coverage"]!=baseline_frame["coverage"] or set(actual)!=set(baseline):
            issues.append({"code":"passive_stamp_inventory_changed"})
        for object_id in sorted(set(actual)&set(baseline)):
            fields=actual[object_id]["fields"]; frozen=baseline[object_id]["fields"]
            if actual[object_id]["configuration"]!=baseline[object_id]["configuration"]:
                issues.append({"code":"passive_stamp_configuration_changed","object_id":object_id})
            for field in fields:
                if field=="y_primitive" and fields["enabled"]["value"] is True and frozen["enabled"]["value"] is True:
                    if {k:v for k,v in fields[field].items() if k!="value"}!={k:v for k,v in frozen[field].items() if k!="value"}:
                        issues.append({"code":"passive_stamp_metadata_changed","object_id":object_id,"field":field})
                    values,previous=fields[field]["value"],frozen[field]["value"]
                    if len(values)!=len(previous):
                        issues.append({"code":"passive_stamp_dimensions_changed","object_id":object_id}); continue
                    for index,(value,expected) in enumerate(zip(values,previous)):
                        compared_values+=1
                        error=abs(value-expected)
                        if error>ABSOLUTE_TOLERANCE+RELATIVE_TOLERANCE*abs(expected):
                            issues.append({"code":"passive_stamp_admittance_changed","object_id":object_id,"index":index,
                                "actual":value,"baseline":expected,"absolute_error":error if math.isfinite(error) else None,
                                "difference_representable":math.isfinite(error)})
                            break
                elif fields[field]!=frozen[field]:
                    issues.append({"code":"passive_stamp_metadata_changed","object_id":object_id,"field":field})
    passed=actual is not None and (not compared or baseline is not None) and not issues
    return {"version":VERSION,"scope":"compiled_passive_admittance_runtime_consistency","passed":passed,
        "status":"matched" if passed and compared else "baseline_validated" if passed else "failed",
        "compared_to_baseline":compared,"object_count":len(actual) if actual is not None else 0,
        "compared_scalar_count":compared_values,"issues":issues,
        "tolerances":{"absolute":ABSOLUTE_TOLERANCE,"relative":RELATIVE_TOLERANCE},
        "source_matrix_interpretation_claimed":False,"configuration_text_full_precision_claimed":False,"engineering_accepted":False}
