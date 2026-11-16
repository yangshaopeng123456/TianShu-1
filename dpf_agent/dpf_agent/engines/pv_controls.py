"""Read native PV mode through OpenDSS CIM100; never edit electrical state.

Copyright (c) yangshaopeng. The COM PV interface has no VarMode getter. EPRI's
CIM100 exporter reads that native field directly. Exported Q is output, never
misrepresented as the private requested-kvar parameter.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
import tempfile
import uuid
import xml.etree.ElementTree as ET

from .device_connections import _Reader, _text, _boolean, _integer, _array

_CIM="http://iec.ch/TC57/CIM100#"
_RDF="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_CE="OpenDSS.ActiveCircuit.ActiveCktElement."
_PV="OpenDSS.ActiveCircuit.PVSystems."


def parse_cim_pv_modes(data):
    if not isinstance(data,bytes) or len(data)>80*1024*1024 or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b",data,re.I):
        raise ValueError("bounded_plain_cim_xml_required")
    root=ET.fromstring(data)
    def one(item,key):
        values=item.findall("{"+_CIM+"}"+key)
        if len(values)!=1:raise ValueError("cim_property_missing_or_duplicate")
        return values[0]
    units={}
    for item in root.findall("{"+_CIM+"}PhotovoltaicUnit"):
        identity=item.get("{"+_RDF+"}about")
        name=one(item,"IdentifiedObject.name").text
        if not identity or not name or identity in units:raise ValueError("photovoltaic_identity_invalid")
        units[identity]=name.casefold()
    modes={}
    for item in root.findall("{"+_CIM+"}PowerElectronicsConnection"):
        unit=one(item,"PowerElectronicsConnection.PowerElectronicsUnit").get("{"+_RDF+"}resource")
        if unit not in units:continue
        name=one(item,"IdentifiedObject.name").text
        resource=one(item,"PowerElectronicsConnection.controlMode").get("{"+_RDF+"}resource")
        if not name or name.casefold()!=units[unit] or name.casefold() in modes:
            raise ValueError("pv_connection_unit_identity_mismatch")
        mode={_CIM+"ConverterControlModeKind.constantPowerFactor":"source_selected_pf",
              _CIM+"ConverterControlModeKind.constantReactivePower":"source_selected_kvar"}.get(resource)
        if mode is None:raise ValueError("unsupported_native_pv_control_mode")
        modes[name.casefold()]=mode
    if len(modes)!=len(units):raise ValueError("pv_cim_unit_coverage_incomplete")
    return modes


def _double(v):
    if type(v) not in (int,float) or not math.isfinite(v):raise ValueError("finite_native_double_required")
    return float(v)


def _bool_property(value):
    value=_text(value).strip().casefold()
    if value not in {"yes","true","1","y","t","no","false","0","n","f"}:
        raise ValueError("native_boolean_property_required")
    return value in {"yes","true","1","y","t"}


def collect_pv_controls(engine,evidence_dir=None):
    reader=_Reader(engine);c=engine.ActiveCircuit
    result={"version":"1.0","scope":"read_only_native_pv_fixed_operating_point", "status":"not_available",
        "source":"OpenDSS.COM_and_native_CIM100", "objects":[], "issues":[],
        "electrical_parameters_modified":False, "requested_kvar_parameter_readback":False}
    names=reader.read("OpenDSS.ActiveCircuit.AllElementNames",lambda:c.AllElementNames,
        lambda v:[_text(n) for n in v])
    if names["status"]!="available":result["issues"].append({"code":"pv_inventory_missing"});return result
    pvs=[n for n in names["value"] if n.casefold().startswith("pvsystem.")]
    if not pvs:result["status"]="available";return result
    original=reader.read(_CE+"Name:before_cim",lambda:c.ActiveCktElement.Name,_text)
    pv_cell=reader.read("OpenDSS.ActiveCircuit.PVSystems",lambda:c.PVSystems)
    tx_cell=reader.read("OpenDSS.ActiveCircuit.Transformers",lambda:c.Transformers)
    if pv_cell["status"]!="available" or tx_cell["status"]!="available" or pv_cell["value"] is None or tx_cell["value"] is None:
        result["issues"].append({"code":"pv_or_transformer_selector_interface_missing"});return result
    pv=pv_cell["value"]
    previous=reader.read(_PV+"Name:before_cim",lambda:pv.Name,_text)
    tx=tx_cell["value"]
    tx_count=reader.read("OpenDSS.ActiveCircuit.Transformers.Count:before_cim",lambda:tx.Count,_integer)
    tx_name=reader.read("OpenDSS.ActiveCircuit.Transformers.Name:before_cim",lambda:tx.Name,_text)
    tx_wdg=(reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg:before_cim",lambda:tx.Wdg,lambda v:_integer(v,minimum=1))
            if tx_name["status"]=="available" else None)
    enabled=[]
    try:
        if tx_count["status"]!="available" or (tx_count["value"]>0 and (tx_name["status"]!="available" or tx_wdg is None or tx_wdg["status"]!="available")):
            raise ValueError("original_transformer_selector_unavailable")
        for name in pvs:
            selected=reader.read("OpenDSS.ActiveCircuit.SetActiveElement",lambda n=name:c.SetActiveElement(n),_integer)
            identity=reader.read(_CE+"Name",lambda:c.ActiveCktElement.Name,_text)
            state=reader.read(_CE+"Enabled",lambda:c.ActiveCktElement.Enabled,_boolean)
            if selected["status"]!="available" or identity["value"] is None or identity["value"].casefold()!=name.casefold() or state["status"]!="available":
                raise ValueError("pv_inventory_identity_untrusted")
            if state["value"]:enabled.append(name)
            else:result["objects"].append({"object_id":name.casefold(),"enabled":False,"status":"not_applicable",
                "identity_verified":True, "identity_source":_CE+"Name_and_Enabled",
                "reason":"disabled_device_no_applied_operating_point"})
        if enabled:
            if evidence_dir is None:raise ValueError("owned_cim_evidence_directory_required")
            folder=Path(evidence_dir).absolute()
            if folder.is_symlink() or getattr(folder,"is_junction",lambda:False)():raise ValueError("linked_evidence_directory")
            folder.mkdir(parents=True,exist_ok=True)
            # The native exporter still has a Windows MAX_PATH limit even
            # when Python can preserve a longer task-evidence path. Export to
            # an owned short-lived directory without changing DSS.DataPath or
            # model state, then retain the exact bytes in the task directory.
            with tempfile.TemporaryDirectory(prefix="dpf-cim-",dir=tempfile.gettempdir()) as staging:
                staged=Path(staging)/"c.xml"
                command='export cim100 file="'+str(staged)+'"'
                exported=reader.read("OpenDSS.Text.Command:export_cim100",lambda:setattr(engine.Text,"Command",command) or True,_boolean)
                if (exported["status"]!="available" or not staged.is_file() or staged.is_symlink()
                    or not 0<staged.stat().st_size<=80*1024*1024):
                    raise ValueError("native_cim_export_unavailable")
                data=staged.read_bytes()
                if not 0<len(data)<=80*1024*1024:raise ValueError("native_cim_export_size_changed")
                target=folder/("pv-"+uuid.uuid4().hex+".xml")
                with target.open("xb") as stream:stream.write(data)
            modes=parse_cim_pv_modes(data)
            if set(modes)!={n.split(".",1)[1].casefold() for n in enabled}:raise ValueError("native_cim_pv_inventory_mismatch")
            result["cim_evidence"]={"path":str(target),"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data),
                "source":"OpenDSS.Text.Command:export cim100", "mode_property":"PowerElectronicsConnection.controlMode",
                "exported_via_owned_staging":True,
                "mode_native_origin":"TPVSystemObj.VarMode via EPRI ConverterControlEnum"}
            for name in enabled:
                selected=reader.select(pv,"Name",name.split(".",1)[1],_PV+"Name:select_point")
                identity=reader.read(_PV+"Name",lambda:pv.Name,_text)
                active=reader.read(_CE+"Name:before_point",lambda:c.ActiveCktElement.Name,_text)
                if selected["status"]!="available" or identity["value"] is None or identity["value"].casefold()!=name.split(".",1)[1].casefold() or active["value"] is None or active["value"].casefold()!=name.casefold():
                    raise ValueError("pv_point_identity_untrusted")
                fields={key:reader.read(_PV+getter,lambda attr=getter:getattr(pv,attr),_double)
                    for key,getter in (("p_nominal_kw","kW"),("q_nominal_kvar","kvar"),("pf_nominal","PF"),("inverter_kva","kVArated"))}
                for cell in fields.values():cell["precision"]={"kind":"native_binary_double"}
                limits={}
                for prop in ("kvarmax","kvarmaxabs","%pminnovars","%pminkvarmax","%pmpp","%cutin","%cutout"):
                    source=_CE+"Properties("+prop+").Val"
                    cell=reader.read(source,lambda p=prop:c.ActiveCktElement.Properties(p).Val,_text)
                    raw=cell["value"]
                    try:
                        value=float(raw)
                        if not math.isfinite(value):raise ValueError("nonfinite")
                        cell["value"]=value
                    except (TypeError,ValueError,OverflowError):
                        cell.update(value=None,status="not_available",issues=[{"code":"invalid_native_pv_limit","source":source}])
                    cell.update(raw_text=raw,precision={"kind":"native_significant_digits","digits":6})
                    limits[prop]=cell
                controls={prop:reader.read(_CE+"Properties("+prop+").Val",
                    lambda key=prop:c.ActiveCktElement.Properties(key).Val,_bool_property)
                    for prop in ("wattpriority","pfpriority","varfollowinverter")}
                active_input={"pmpp_kw":reader.read(_PV+"Pmpp",lambda:pv.Pmpp,_double),
                    "variable_names":reader.read(_CE+"AllVariableNames",lambda:c.ActiveCktElement.AllVariableNames,lambda v:_array(v,_text)),
                    "variable_values":reader.read(_CE+"AllVariableValues",lambda:c.ActiveCktElement.AllVariableValues,lambda v:_array(v,_double))}
                active_input["pmpp_kw"]["precision"]={"kind":"native_binary_double"}
                final=reader.read(_CE+"Name:after_point",lambda:c.ActiveCktElement.Name,_text)
                mandatory=tuple(fields.values())+tuple(limits.values())+tuple(controls.values())+tuple(active_input.values())
                if final["value"] is None or final["value"].casefold()!=name.casefold() or any(v["status"]!="available" for v in mandatory):
                    raise ValueError("pv_point_readback_untrusted")
                result["objects"].append({"object_id":name.casefold(),"enabled":True,"status":"available", "fields":fields,
                    "limits":limits,
                    "controls":controls,
                    "active_power_input":active_input,
                    "reactive_mode":{"value":modes[name.split(".",1)[1].casefold()],"status":"available",
                        "source":"OpenDSS.CIM100.PowerElectronicsConnection.controlMode", "issues":[]},
                    "identity_verified":True,"identity_source":_CE+"Name_and_CIM_PhotovoltaicUnit_association"})
    except (OSError,ValueError,TypeError,ET.ParseError) as exc:
        result["issues"].append({"code":"pv_control_readback_failed","detail":str(exc)})
    finally:
        if previous["status"]=="available":
            restored=reader.select(pv,"Name",previous["value"],_PV+"Name:restore_cim")
            actual=reader.read(_PV+"Name:after_cim_restore",lambda:pv.Name,_text)
            if restored["status"]!="available" or actual["value"] is None or actual["value"].casefold()!=previous["value"].casefold():
                result["issues"].append({"code":"pv_cim_selector_restoration_not_verified"})
        else:result["issues"].append({"code":"pv_original_selector_missing"})
        if tx_name["status"]=="available":
            restored=reader.select(tx,"Name",tx_name["value"],"OpenDSS.ActiveCircuit.Transformers.Name:restore_cim")
            actual=reader.read("OpenDSS.ActiveCircuit.Transformers.Name:after_cim_restore",lambda:tx.Name,_text)
            if restored["status"]!="available" or actual["value"] is None or actual["value"].casefold()!=tx_name["value"].casefold():
                result["issues"].append({"code":"pv_cim_transformer_selector_restore_failed"})
            if tx_wdg and tx_wdg["status"]=="available":
                restored=reader.select(tx,"Wdg",tx_wdg["value"],"OpenDSS.ActiveCircuit.Transformers.Wdg:restore_cim")
                actual=reader.read("OpenDSS.ActiveCircuit.Transformers.Wdg:after_cim_restore",lambda:tx.Wdg,lambda v:_integer(v,minimum=1))
                if restored["status"]!="available" or actual["value"]!=tx_wdg["value"]:
                    result["issues"].append({"code":"pv_cim_winding_selector_restore_failed"})
        if original["status"]=="available":
            restored=reader.read("OpenDSS.ActiveCircuit.SetActiveElement:restore_cim",lambda:c.SetActiveElement(original["value"]),_integer)
            actual=reader.read(_CE+"Name:after_cim_restore",lambda:c.ActiveCktElement.Name,_text)
            if restored["status"]!="available" or actual["value"] is None or actual["value"].casefold()!=original["value"].casefold():
                result["issues"].append({"code":"pv_cim_element_restoration_not_verified"})
        else:result["issues"].append({"code":"pv_original_element_missing"})
    if not result["issues"]:result["status"]="available"
    return result
