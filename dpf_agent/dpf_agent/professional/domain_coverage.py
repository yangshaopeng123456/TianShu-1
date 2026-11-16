"""Mandatory inventory and raw-v2 coverage checks. Copyright yangshaopeng."""
from __future__ import annotations

from collections.abc import Mapping
import math


_SOURCES={"bus_names":"AllBusNames", "bus_count":"NumBuses", "element_names":"AllElementNames",
          "line_count":"Lines.Count", "transformer_count":"Transformers.Count"}


def verify_domain_coverage(raw, observations, scopes):
    report={"version":"1.0", "scope":"requested_bus_line_transformer_domains",
            "passed":False, "domains":{}, "issues":[]}
    issues=report["issues"]
    def issue(code,domain=None): issues.append({"code":code,"domain":domain})
    try:
        pair=raw["solver"]["domain_inventory"]
        def inventory(value):
            if (not isinstance(value,Mapping) or value.get("version")!="1.0"
                or value.get("scope")!="native_bus_line_transformer_inventory"
                or value.get("status")!="available" or value.get("issues")!=[]):
                raise ValueError("untrusted_native_inventory")
            fields=value.get("fields")
            if not isinstance(fields,Mapping) or set(fields)!=set(_SOURCES):
                raise ValueError("inventory_fields_missing")
            out={}
            for key,source in _SOURCES.items():
                cell=fields[key]
                if (not isinstance(cell,Mapping) or cell.get("status")!="available"
                    or cell.get("source")!="OpenDSS.ActiveCircuit."+source or cell.get("issues")!=[]):
                    raise ValueError("inventory_field_untrusted")
                v=cell.get("value")
                if key.endswith("names"):
                    if (not isinstance(v,list) or len(v)>100000 or any(not isinstance(n,str) or not n or len(n)>256 for n in v)
                        or len({n.casefold() for n in v})!=len(v)):
                        raise ValueError("inventory_names_invalid")
                    out[key]={n.casefold() for n in v}
                else:
                    if type(v) is not int or not 0<=v<=100000: raise ValueError("inventory_count_invalid")
                    out[key]=v
            if out["bus_count"]!=len(out["bus_names"]):raise ValueError("bus_independent_count_mismatch")
            for kind in ("line","transformer"):
                out[kind+"_names"]={n for n in out["element_names"] if n.startswith(kind+".")}
                if len(out[kind+"_names"])!=out[kind+"_count"]:raise ValueError(kind+"_independent_count_mismatch")
            return out
        before,after=inventory(pair["before"]),inventory(pair["after"])
        if before!=after:raise ValueError("inventory_changed_during_extraction")
    except (KeyError,TypeError,ValueError) as exc:
        issue(str(exc));return report
    required_buses=bool(set(scopes)&{"bus","system","branch","transformer"})
    domains=[("buses","bus",required_buses), ("lines","line","branch" in scopes),
             ("transformers","transformer","transformer" in scopes),
             ("auxiliary_elements","auxiliary","system" in scopes)]
    for collection,kind,required in domains:
        if not required:
            report["domains"][collection]={"status":"not_requested"};continue
        expected=({n for n in before["element_names"] if n.split(".",1)[0] in {"vsource","capacitor","reactor"}}
                  if kind=="auxiliary" else before["bus_names" if kind=="bus" else kind+"_names"])
        rows=raw.get(collection)
        names=[]
        if not isinstance(rows,list):issue("requested_domain_missing",collection);rows=[]
        for row in rows:
            if not isinstance(row,Mapping) or not isinstance(row.get("name"),str):
                issue("record_identity_invalid",collection);continue
            name=row["name"].casefold();names.append(name)
            if kind=="bus":
                if (row.get("observation_schema_version")!="2.0" or row.get("observation_status")!="available"
                    or row.get("identity_verified") is not True or row.get("observation_errors")!=[]):
                    issue("bus_observation_not_verified",name)
            else:
                td=row.get("terminal_data")
                if (not isinstance(td,Mapping) or td.get("schema_version")!="2.0"
                    or td.get("identity_verified") is not True or td.get("errors")!=[]
                    or td.get("status") not in {"available","disabled"}
                    or not isinstance(td.get("active_element_name"),str)
                    or td["active_element_name"].casefold()!=name):
                    issue("terminal_observation_not_verified",name)
        if len(names)!=len(set(names)) or set(names)!=expected:
            issue("requested_domain_inventory_mismatch",collection)
        report["domains"][collection]={"status":"complete" if set(names)==expected and len(names)==len(set(names)) else "incomplete",
            "expected_count":len(expected), "observed_count":len(names),
            "missing_names":sorted(expected-set(names)), "unexpected_names":sorted(set(names)-expected)}
    # The observation builder verifies all representations, S=VI*, node/terminal
    # mapping and independent bus voltages. Its errors cannot be treated as gaps
    # which leave a supposedly valid numerical publication behind.
    for item in observations.get("issues",[]):
        if not isinstance(item,Mapping) or item.get("severity")=="error":
            issue("observation_consistency_error",item.get("object_id") if isinstance(item,Mapping) else None)
    if {"bus","branch","transformer","system"}.issubset(scopes) and not issues:
        report["network_physics"]=_network_physics(raw,before["element_names"])
        if not report["network_physics"]["passed"]:issue("network_conservation_not_verified")
    else:
        report["network_physics"]={"status":"not_checked", "reason":"complete and valid network terminal domains are required"}
    report["passed"]=not issues
    return report


def _network_physics(raw, expected_names):
    """KCL and complex-power conservation across every terminal, not phase maxima."""
    rows=[row for collection in ("lines","transformers","power_conversion_elements","auxiliary_elements") for row in raw.get(collection,[])]
    names=[row["name"].casefold() for row in rows]
    result={"status":"not_verified", "passed":False, "issues":[], "current_absolute_tolerance_a":.001,
        "current_relative_tolerance":1e-4, "power_absolute_tolerance_kva":.001,
        "power_relative_tolerance":1e-4, "tolerances_are_engineering_limits":False}
    if len(names)!=len(set(names)) or set(names)!=expected_names:
        result["issues"].append({"code":"complete_electrical_inventory_required"});return result
    buses={};total=0j;through=0.
    try:
        for row in rows:
            td=row["terminal_data"]
            if td.get("enabled") is False:
                if td.get("status")!="disabled":raise ValueError("invalid_disabled_state")
                continue
            nc=td["num_conductors"]
            for index,node in enumerate(td["node_order"]):
                terminal=index//nc
                current=complex(*td["currents_complex_a"][2*index:2*index+2])
                power=complex(*td["powers_kw_kvar"][2*index:2*index+2])
                if not all(math.isfinite(v) for v in (current.real,current.imag,power.real,power.imag)):
                    raise ValueError("nonfinite_terminal_quantity")
                total+=power;through+=abs(power)
                if node==0:continue
                bus=td["bus_names"][terminal].split(".",1)[0].casefold()
                key=bus+"."+str(node)
                accumulated,magnitude=buses.get(key,(0j,0.))
                buses[key]=(accumulated+current,magnitude+abs(current))
        checks=[{"bus_node":key, "residual_a":abs(current),"through_current_a":magnitude,
            "limit_a":.001+1e-4*magnitude, "passed":abs(current)<=.001+1e-4*magnitude}
            for key,(current,magnitude) in sorted(buses.items())]
        power_limit=.001+1e-4*through
        result.update(bus_node_checks=checks,complex_power_residual_kw=total.real,
            complex_power_residual_kvar=total.imag,complex_power_limit_kva=power_limit,
            passed=bool(checks and all(c["passed"] for c in checks) and abs(total)<=power_limit))
        result["status"]="matched" if result["passed"] else "mismatch"
    except (KeyError,TypeError,ValueError,IndexError,OverflowError):
        result["issues"].append({"code":"required_terminal_quantities_invalid"})
    return result
