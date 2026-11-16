"""Bounded applied PV fixed-point verification. Copyright yangshaopeng.

Native CIM mode, nominal P/Q, control flags and inverter limits are independent
readbacks. A private requested-kvar getter is not invented. The bounded default
Q-priority path validates both outputs, including documented native limiting.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import math
import re
from .power_point_verification import quantized_consistency

_CE="OpenDSS.ActiveCircuit.ActiveCktElement."
_PV="OpenDSS.ActiveCircuit.PVSystems."
_LIMITS={"kvarmax","kvarmaxabs","%pminnovars","%pminkvarmax","%pmpp","%cutin","%cutout"}
_CONTROLS={"wattpriority","pfpriority","varfollowinverter"}


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def _native_number(cell,source):
    if (not isinstance(cell,Mapping) or cell.get("status")!="available" or cell.get("issues")!=[]
        or cell.get("source")!=source or cell.get("precision")!={"kind":"native_binary_double"}
        or not _finite(cell.get("value"))):
        raise ValueError("pv_native_double_untrusted")
    return cell["value"]


def _native_row(row):
    """Validate every baseline/current cell before any trusted comparison."""
    if not row["enabled"]:
        if (row.get("status")!="not_applicable" or row.get("reason")!="disabled_device_no_applied_operating_point"
            or row.get("identity_verified") is not True or row.get("identity_source")!=_CE+"Name_and_Enabled"):
            raise ValueError("pv_disabled_marker_invalid")
        return
    if (row.get("status")!="available" or row.get("identity_verified") is not True
        or row.get("identity_source")!=_CE+"Name_and_CIM_PhotovoltaicUnit_association"):
        raise ValueError("pv_enabled_identity_untrusted")
    mode=row.get("reactive_mode")
    if (not isinstance(mode,Mapping) or mode.get("status")!="available" or mode.get("issues")!=[]
        or mode.get("source")!="OpenDSS.CIM100.PowerElectronicsConnection.controlMode"
        or mode.get("value") not in {"source_selected_pf","source_selected_kvar"}):
        raise ValueError("pv_mode_not_observed")
    fields=row.get("fields")
    specs={"p_nominal_kw":"kW","q_nominal_kvar":"kvar","pf_nominal":"PF","inverter_kva":"kVArated"}
    if not isinstance(fields,Mapping) or set(fields)!=set(specs):raise ValueError("pv_nominal_coverage")
    for key,getter in specs.items():_native_number(fields[key],_PV+getter)
    if fields["inverter_kva"]["value"]<=0 or not 0<abs(fields["pf_nominal"]["value"])<=1:
        raise ValueError("pv_nominal_rating_or_pf_invalid")
    limits=row.get("limits")
    if not isinstance(limits,Mapping) or set(limits)!=_LIMITS:raise ValueError("pv_limit_coverage")
    for key,cell in limits.items():
        value=cell.get("value") if isinstance(cell,Mapping) else None
        if (not isinstance(cell,Mapping) or cell.get("status")!="available" or cell.get("issues")!=[]
            or cell.get("source")!=_CE+"Properties("+key+").Val"
            or cell.get("precision")!={"kind":"native_significant_digits","digits":6}
            or quantized_consistency(value,cell.get("raw_text"),value) is None
            or key not in {"%pminnovars","%pminkvarmax"} and value<0):
            raise ValueError("pv_limit_readback_untrusted")
    controls=row.get("controls")
    if not isinstance(controls,Mapping) or set(controls)!=_CONTROLS:raise ValueError("pv_control_coverage")
    for key,cell in controls.items():
        if (not isinstance(cell,Mapping) or cell.get("status")!="available" or cell.get("issues")!=[]
            or cell.get("source")!=_CE+"Properties("+key+").Val" or type(cell.get("value")) is not bool):
            raise ValueError("pv_control_readback_untrusted")
    data=row.get("active_power_input")
    if not isinstance(data,Mapping) or set(data)!={"pmpp_kw","variable_names","variable_values"}:
        raise ValueError("pv_pre_limit_input_missing")
    if _native_number(data["pmpp_kw"],_PV+"Pmpp")<0:raise ValueError("pv_negative_pmpp")
    for key,getter in (("variable_names","AllVariableNames"),("variable_values","AllVariableValues")):
        cell=data[key]
        if (not isinstance(cell,Mapping) or cell.get("status")!="available" or cell.get("issues")!=[]
            or cell.get("source")!=_CE+getter or not isinstance(cell.get("value"),list)):
            raise ValueError("pv_native_variables_untrusted")
    names,values=data["variable_names"]["value"],data["variable_values"]["value"]
    if (not 1<=len(names)<=100000 or len(names)!=len(values)
        or any(not isinstance(n,str) or not n or len(n)>256 for n in names) or len(set(names))!=len(names)
        or any(not _finite(v) for v in values) or not {"PanelkW","Efficiency","kW_out_desired"}.issubset(names)):
        raise ValueError("pv_native_variable_dimensions")


def _interval(cell):
    """A local conservative inverse of a validated six-digit native formatter.

    At a power of ten, the lower-side spacing is ten times finer. Preserve that
    asymmetry instead of enlarging the native quantization bin across decades.
    """
    value=cell["value"]
    proof=quantized_consistency(value,cell["raw_text"],value)
    if proof is None or value<0:raise ValueError("pv_nonnegative_quantized_interval_required")
    if value==0:return 0.,0.
    number=Decimal(cell["raw_text"].strip())
    quantum=Decimal(10)**(number.adjusted()-5)
    lower_quantum=quantum/10 if number==Decimal(10)**number.adjusted() else quantum
    allowance=8*math.ulp(float(value))
    lower,upper=max(0.,float(number-lower_quantum/2)-allowance),float(number+quantum/2)+allowance
    if not math.isfinite(lower) or not math.isfinite(upper):raise ValueError("pv_quantized_interval_not_representable")
    return lower,upper


def inspect_pv_limits(inspection):
    """Explicit native limits/flags only; omitted defaults are not manufactured."""
    result={}
    for row in inspection["objects"]:
        if row["class"]!="pvsystem":continue
        fields={}
        for prop in row["properties"]:
            key=prop["name"].casefold()
            if key not in _LIMITS|_CONTROLS:continue
            raw=prop["value"].strip().strip("\"'")
            if key in _CONTROLS:
                text=raw.casefold()
                if text not in {"yes","true","1","y","t","no","false","0","n","f"}:raise ValueError("pv_source_control_boolean_required")
                value=text in {"yes","true","1","y","t"}
            else:
                value=float(raw)
                if not math.isfinite(value) or key not in {"%pminnovars","%pminkvarmax"} and value<0:
                    raise ValueError("unsupported_pv_native_limit")
            fields[key]={"value":value,"source_refs":[{"source_file":prop["source_file"],"line":prop["line"],"property":key}]}
        result[row["name"].casefold()]=fields
    return result


def verify_pv_points(declarations,native,baseline=None):
    report={"version":"1.0","scope":"native_mode_and_applied_pv_fixed_point", "passed":False,
        "checks":[],"issues":[],"requested_kvar_parameter_readback":False,
        "terminal_power_is_nominal_output":False,"engineering_accepted":False,
        "supported_operating_point":"fixed_pf_or_kvar_default_q_priority_no_follow_no_ramp_deterministic_inverter_state"}
    def add(name,field,passed,expected=None,actual=None,reason=None):
        report["checks"].append({"object_id":name,"field":field,"status":"matched" if passed else "mismatch",
            "expected":expected,"actual":actual,"reason":reason})
        if not passed:report["issues"].append({"code":"pv_fixed_point_not_verified","object_id":name,"field":field})
    def frame(data):
        if (not isinstance(data,Mapping) or data.get("version")!="1.0"
            or data.get("scope")!="read_only_native_pv_fixed_operating_point"
            or data.get("source")!="OpenDSS.COM_and_native_CIM100"
            or data.get("status")!="available" or data.get("issues")!=[]
            or data.get("electrical_parameters_modified") is not False
            or data.get("requested_kvar_parameter_readback") is not False
            or not isinstance(data.get("objects"),list)):
            raise ValueError("pv_native_frame_untrusted")
        rows={}
        for row in data["objects"]:
            if not isinstance(row,Mapping):raise ValueError("pv_native_row_untrusted")
            name=row.get("object_id")
            if not isinstance(name,str) or not name.startswith("pvsystem.") or name in rows or type(row.get("enabled")) is not bool:
                raise ValueError("pv_native_identity_invalid")
            _native_row(row)
            rows[name]=row
        if any(row["enabled"] for row in rows.values()):
            evidence=data.get("cim_evidence")
            if (not isinstance(evidence,Mapping) or not isinstance(evidence.get("sha256"),str)
                or not re.fullmatch(r"[0-9a-f]{64}",evidence["sha256"])
                or type(evidence.get("bytes")) is not int or not 0<evidence["bytes"]<=80*1024*1024
                or evidence.get("source")!="OpenDSS.Text.Command:export cim100"
                or evidence.get("mode_property")!="PowerElectronicsConnection.controlMode"
                or data.get("state_unchanged_during_export") is not True):
                raise ValueError("pv_native_cim_provenance_untrusted")
        return rows
    number=_native_number
    try:
        rows=frame(native);old=frame(baseline) if baseline is not None else None
        names={row["object_id"].casefold() for row in declarations["objects"] if row["class"]=="pvsystem"}
        points={row["object_id"].casefold():row for row in declarations["power_operating_points"]["objects"] if row["class"]=="pvsystem"}
        limit_declarations=declarations.get("pv_limit_declarations")
        if not isinstance(limit_declarations,Mapping) or set(limit_declarations)!=names:
            raise ValueError("pv_limit_declaration_coverage")
        if any(not isinstance(fields,Mapping) or not set(fields).issubset(_LIMITS|_CONTROLS) for fields in limit_declarations.values()):
            raise ValueError("pv_limit_declaration_fields_invalid")
        if set(rows)!=names or set(points)!=names or old is not None and set(old)!=names:
            raise ValueError("pv_source_native_inventory_mismatch")
        for name,row in rows.items():
            if old is not None:
                add(name,"frozen.enabled",old[name].get("enabled") is row["enabled"],old[name].get("enabled"),row["enabled"])
            if not row["enabled"]:
                if (row.get("status")!="not_applicable" or row.get("reason")!="disabled_device_no_applied_operating_point"
                    or row.get("identity_verified") is not True
                    or row.get("identity_source")!="OpenDSS.ActiveCircuit.ActiveCktElement.Name_and_Enabled"):
                    raise ValueError("pv_disabled_marker_invalid")
                add(name,"applied_operating_point",True,reason="disabled_device_not_applicable")
                continue
            if row.get("status")!="available" or row.get("identity_verified") is not True or row.get("identity_source")!="OpenDSS.ActiveCircuit.ActiveCktElement.Name_and_CIM_PhotovoltaicUnit_association":
                raise ValueError("pv_enabled_identity_untrusted")
            mode=row.get("reactive_mode")
            if (not isinstance(mode,Mapping) or mode.get("status")!="available" or mode.get("issues")!=[]
                or mode.get("source")!="OpenDSS.CIM100.PowerElectronicsConnection.controlMode"
                or mode.get("value") not in {"source_selected_pf","source_selected_kvar"}):
                raise ValueError("pv_mode_not_observed")
            fields=row.get("fields",{})
            if set(fields)!={"p_nominal_kw","q_nominal_kvar","pf_nominal","inverter_kva"}:raise ValueError("pv_nominal_coverage")
            p=number(fields["p_nominal_kw"],"OpenDSS.ActiveCircuit.PVSystems.kW")
            q=number(fields["q_nominal_kvar"],"OpenDSS.ActiveCircuit.PVSystems.kvar")
            native_pf=number(fields["pf_nominal"],"OpenDSS.ActiveCircuit.PVSystems.PF")
            kva=number(fields["inverter_kva"],"OpenDSS.ActiveCircuit.PVSystems.kVArated")
            if kva<=0:raise ValueError("pv_inverter_rating_invalid")
            apparent=math.hypot(p,q)
            if not math.isfinite(apparent):raise ValueError("pv_nominal_apparent_power_not_representable")
            add(name,"nominal_apparent_power_limit",apparent<=kva+max(1e-8,1e-9*kva),kva,apparent)
            limits=row.get("limits")
            if not isinstance(limits,Mapping) or set(limits)!=_LIMITS:
                raise ValueError("pv_limit_coverage")
            for key,cell in limits.items():
                value=cell.get("value") if isinstance(cell,Mapping) else None
                if (not isinstance(cell,Mapping) or cell.get("status")!="available" or cell.get("issues")!=[]
                    or cell.get("source")!="OpenDSS.ActiveCircuit.ActiveCktElement.Properties("+key+").Val"
                    or cell.get("precision")!={"kind":"native_significant_digits","digits":6}
                    or quantized_consistency(value,cell.get("raw_text"),value) is None):
                    raise ValueError("pv_limit_readback_untrusted")
                if key in {"kvarmax","kvarmaxabs"} and value<0:raise ValueError("pv_negative_reactive_limit")
                source=limit_declarations[name].get(key)
                if source is not None:
                    expected_limit=source.get("value")
                    refs=source.get("source_refs")
                    if (not isinstance(refs,list) or not refs or any(not isinstance(r,Mapping) or r.get("property")!=key
                        or not isinstance(r.get("source_file"),str) or not r["source_file"] or type(r.get("line")) is not int or r["line"]<1 for r in refs)):
                        raise ValueError("pv_limit_source_provenance_invalid")
                    comparison=quantized_consistency(expected_limit,cell["raw_text"],value)
                    add(name,"source."+key,bool(comparison and comparison["consistent"]),expected_limit,value,
                        "Native six-significant-digit consistency, not a full-precision equality claim.")
            if limits["%pminnovars"]["value"]>0 or limits["%pminkvarmax"]["value"]>0:
                raise ValueError("pv_reactive_ramp_outside_verified_subset")
            controls=row["controls"]
            if any(cell["value"] for cell in controls.values()):
                raise ValueError("pv_priority_or_follow_mode_outside_verified_subset")
            for key,cell in controls.items():
                source=limit_declarations[name].get(key)
                if source is not None:
                    refs=source.get("source_refs")
                    if (type(source.get("value")) is not bool or not isinstance(refs,list) or not refs
                        or any(not isinstance(r,Mapping) or r.get("property")!=key or not isinstance(r.get("source_file"),str)
                            or not r["source_file"] or type(r.get("line")) is not int or r["line"]<1 for r in refs)):
                        raise ValueError("pv_control_source_provenance_invalid")
                    add(name,"source."+key,source["value"] is cell["value"],source["value"],cell["value"])
            active_input=row.get("active_power_input")
            if not isinstance(active_input,Mapping) or set(active_input)!={"pmpp_kw","variable_names","variable_values"}:
                raise ValueError("pv_pre_limit_input_missing")
            pmpp=number(active_input["pmpp_kw"],"OpenDSS.ActiveCircuit.PVSystems.Pmpp")
            names_cell,values_cell=active_input["variable_names"],active_input["variable_values"]
            for cell,source in ((names_cell,"AllVariableNames"),(values_cell,"AllVariableValues")):
                if (not isinstance(cell,Mapping) or cell.get("status")!="available" or cell.get("issues")!=[]
                    or cell.get("source")!="OpenDSS.ActiveCircuit.ActiveCktElement."+source or not isinstance(cell.get("value"),list)):
                    raise ValueError("pv_native_variables_untrusted")
            variable_names,variable_values=names_cell["value"],values_cell["value"]
            if (not 1<=len(variable_names)<=100000 or len(variable_names)!=len(variable_values)
                or any(not isinstance(n,str) or not n or len(n)>256 for n in variable_names)
                or len(set(variable_names))!=len(variable_names)
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in variable_values)):
                raise ValueError("pv_native_variable_dimensions")
            variables=dict(zip(variable_names,variable_values))
            panel,eff,available=(variables[key] for key in ("PanelkW","Efficiency","kW_out_desired"))
            if panel<0 or not 0<=eff<=1 or available<0 or not math.isclose(panel*eff,available,rel_tol=1e-10,abs_tol=1e-10) or pmpp<0:
                raise ValueError("pv_pre_limit_available_power_inconsistent")
            pct=limits["%pmpp"]["value"]
            if pct<0:raise ValueError("pv_negative_active_power_limit")
            cuts=[_interval(limits[key]) for key in ("%cutin","%cutout")]
            guaranteed_on=panel>=max(interval[1] for interval in cuts)*kva/100.
            guaranteed_off=panel<min(interval[0] for interval in cuts)*kva/100.
            if panel!=0 and not guaranteed_on and not guaranteed_off:
                raise ValueError("pv_inverter_hysteresis_or_cut_threshold_precision_outside_verified_subset")
            pct_low,pct_high=_interval(limits["%pmpp"])
            pre_limit_p=0. if guaranteed_off else min(available,pmpp*pct/100.)
            pre_low,pre_high=((0.,0.) if guaranteed_off else
                (min(available,pmpp*pct_low/100.),min(available,pmpp*pct_high/100.)))
            add(name,"inverter_state_domain",True,actual="guaranteed_off" if guaranteed_off else
                "guaranteed_on" if guaranteed_on else "zero_panel_output_determined",
                reason="Both native cut thresholds and their local text-precision intervals bound the deterministic domain; no prior on/off state is guessed.")
            expected=points[name]["fields"]
            selected=expected["reactive_mode"]
            if selected["status"]=="declared":
                add(name,"reactive_mode",mode["value"]==selected["value"],selected["value"],mode["value"])
            else:add(name,"reactive_mode_observed",True,actual=mode["value"],reason="source mode omitted; actual native default is observed, not inserted into source")
            if old is not None:
                prior=old[name]
                add(name,"frozen.reactive_mode",prior.get("enabled") is True and prior.get("reactive_mode",{}).get("value")==mode["value"],prior.get("reactive_mode",{}).get("value"),mode["value"])
                for key,getter in (("inverter_kva","kVArated"),("pf_nominal","PF")):
                    previous=number(prior.get("fields",{}).get(key),"OpenDSS.ActiveCircuit.PVSystems."+getter)
                    actual=fields[key]["value"]
                    add(name,"frozen."+key,math.isclose(previous,actual,rel_tol=1e-10,abs_tol=1e-12),previous,actual)
                for key,cell in limits.items():
                    previous=prior.get("limits",{}).get(key)
                    if not isinstance(previous,Mapping):raise ValueError("pv_baseline_limits_missing")
                    comparison=quantized_consistency(previous.get("value"),cell.get("raw_text"),cell["value"])
                    add(name,"frozen."+key,bool(comparison and comparison["consistent"]),previous.get("value"),cell["value"],
                        "Immutable native limit readback with explicit six-digit precision boundary.")
                for key,cell in controls.items():
                    add(name,"frozen."+key,prior["controls"][key]["value"] is cell["value"],prior["controls"][key]["value"],cell["value"])
                previous_pmpp=number(prior.get("active_power_input",{}).get("pmpp_kw"),"OpenDSS.ActiveCircuit.PVSystems.Pmpp")
                add(name,"frozen.pmpp_kw",math.isclose(previous_pmpp,pmpp,rel_tol=1e-10,abs_tol=1e-12),previous_pmpp,pmpp)
            target=None;target_low=None;target_high=None
            if mode["value"]=="source_selected_kvar" and expected["kvar_requested"]["status"]=="declared":
                target=expected["kvar_requested"]["value"]
                if not _finite(target):raise ValueError("pv_source_q_invalid")
                target_low=target_high=abs(target)
            elif mode["value"]=="source_selected_pf":
                pf=expected["pf_nominal"]["value"] if expected["pf_nominal"]["status"]=="declared" else native_pf
                if type(pf) not in (int,float) or not math.isfinite(pf) or not 0<abs(pf)<=1:raise ValueError("pv_pf_invalid")
                if expected["pf_nominal"]["status"]=="declared":
                    add(name,"source.pf_nominal",math.isclose(pf,native_pf,rel_tol=1e-10,abs_tol=1e-12),pf,native_pf)
                if limits["kvarmax"]["value"]!=limits["kvarmaxabs"]["value"]:
                    raise ValueError("pv_asymmetric_pf_q_limits_outside_verified_subset")
                tangent=math.sqrt(max(0.,(1-abs(pf))*(1+abs(pf))))/abs(pf)
                if not math.isfinite(tangent):raise ValueError("pv_pf_tangent_not_representable")
                target=pre_limit_p*tangent*math.copysign(1.,pf)
                target_low,target_high=pre_low*tangent,pre_high*tangent
                if not all(_finite(v) for v in (target,target_low,target_high)):
                    raise ValueError("pv_requested_pf_point_not_representable")
            if target is None:raise ValueError("pv_requested_q_source_unavailable")
            if target is not None:
                q_limit=limits["kvarmax" if target>=0 else "kvarmaxabs"]
                limit_low,limit_high=_interval(q_limit)
                q_low,q_high=min(target_low,limit_low),min(target_high,limit_high)
                magnitude=min(abs(target),q_limit["value"])
                expected_q=math.copysign(magnitude,target)
                allowance=max(1e-8,1e-9*max(abs(q),q_high))
                matched=(q==0 or (q>0)==(target>0)) and q_low-allowance<=abs(q)<=q_high+allowance
                precision_limited=not math.isclose(expected_q,q,rel_tol=1e-9,abs_tol=1e-8)
                add(name,"applied_nominal_q",matched,expected_q,q,
                    "Default Q-priority: source/native PF or source requested Q, then native Q limits; no private requested-kvar getter is claimed.")
                report["checks"][-1].update(source_requested_or_pf_derived_q=target,
                    limited_by_native_inverter=not math.isclose(target,q,rel_tol=1e-9,abs_tol=1e-8),
                    precision_limited=precision_limited,requested_kvar_parameter_readback=False,
                    allowed_magnitude_interval=[q_low,q_high],native_inverter_kva=kva)
                # Nominal Q is typed and independently validated above. Use
                # that observed value to retain the P/Q coupling at the kVA
                # boundary without a global tolerance or sqrt amplification.
                headroom=kva*math.sqrt(max(0.,1-(q/kva)**2))
                p_low,p_high=min(pre_low,headroom),min(pre_high,headroom)
                expected_p=min(pre_limit_p,headroom)
                allowance=max(1e-8,1e-9*max(abs(p),p_high))
                add(name,"applied_nominal_p",matched and p_low-allowance<=p<=p_high+allowance,expected_p,p,
                    "Native fixed default Q-priority must retain pre-limit active power unless the validated nominal Q consumes inverter capacity.")
                report["checks"][-1].update(pre_limit_active_power=pre_limit_p,
                    allowed_interval=[p_low,p_high],validated_nominal_q_used_for_kva_coupling=True,
                    precision_limited=not math.isclose(expected_p,p,rel_tol=1e-9,abs_tol=1e-8),
                    source_parameters_modified=False)
        report["passed"]=not report["issues"]
    except (KeyError,ValueError,TypeError,AttributeError,OverflowError,RecursionError) as exc:
        add(None,"contract",False,reason="Required native CIM mode, identity or applied fixed-point observations are unavailable, untrusted or outside this bounded subset: "+str(exc))
    report["status"]="matched" if report["passed"] else "not_verified"
    return report
