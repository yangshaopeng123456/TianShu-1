"""Read native input parameters, independently from solved terminal powers.

Copyright (c) yangshaopeng. Generator base P/Q uses the official six-digit
property getters; their text precision is retained rather than hidden. PV
requested kvar and reactive mode have no trusted getter in this contract.
Only object selectors are written; no electrical parameter is modified.
"""
from __future__ import annotations

import math


_CE = 'OpenDSS.ActiveCircuit.ActiveCktElement.'
_INTERFACES = {'load':'Loads','generator':'Generators','pvsystem':'PVSystems'}


def _name(value):
    if not isinstance(value,str) or not value.strip() or len(value)>256:
        raise ValueError('invalid_name')
    return value


def _number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise ValueError('finite_native_number_required')
    return float(value)


def _failed(source,code):
    return {'value':None,'status':'not_available','source':source,
        'issues':[{'code':code,'source':source}]}


def collect_power_setpoints(reader,circuit,requested):
    """Additive row contract: old device-schema 1.0 consumers can ignore it."""
    kind=requested.split('.',1)[0].casefold()
    interface=_INTERFACES[kind]
    prefix='OpenDSS.ActiveCircuit.'+interface
    keys=({'kw_base','kvar_base','pf_nominal','kva'} if kind!='pvsystem'
        else {'pmpp','irradiance','pf_nominal','kva'})
    result={'version':'1.0','scope':'read_only_native_power_setpoints',
        'object_id':requested,'class':kind,'identity_verified':False,
        'identity_source':prefix+'.Name','status':'not_available','fields':{},'issues':[],
        'electrical_parameters_modified':False,
        'not_observable':['reactive_mode','kvar_requested'] if kind=='pvsystem' else [],
        'kva_role':'nominal_apparent_power' if kind=='load' else 'stored_equipment_rating_not_engineering_approval'}
    fields=result['fields']
    selected=reader.read(prefix,lambda:getattr(circuit,interface))
    collection=selected['value']
    if collection is None or selected['status']!='available':
        result['issues'].append({'code':'power_interface_unavailable','source':prefix})
        return result
    previous=reader.read(prefix+'.Name:before_power_read',lambda:collection.Name,_name)
    try:
        activation=reader.select(collection,'Name',requested.split('.',1)[1],prefix+'.Name:select')
        name=reader.read(prefix+'.Name',lambda:collection.Name,_name)
        active=reader.read(_CE+'Name:before_power_read',lambda:circuit.ActiveCktElement.Name,_name)
        if (activation['status']!='available' or name['value'] is None
            or name['value'].casefold()!=requested.split('.',1)[1].casefold()
            or active['value'] is None or active['value'].casefold()!=requested.casefold()):
            result['issues'].append({'code':'power_identity_mismatch','source':prefix+'.Name'})
            return result
        attributes=({'kw_base':'kW','kvar_base':'kvar','pf_nominal':'PF','kva':'kVA'} if kind=='load'
            else {'pf_nominal':'PF','kva':'kVArated'} if kind=='generator'
            else {'pmpp':'Pmpp','irradiance':'Irradiance','pf_nominal':'PF','kva':'kVArated'})
        for key,attribute in attributes.items():
            fields[key]=reader.read(prefix+'.'+attribute,lambda attr=attribute:getattr(collection,attr),_number)
            fields[key]['precision']={'kind':'native_binary_double'}
        if kind=='generator':
            for key,prop in (('kw_base','kw'),('kvar_base','kvar')):
                source=_CE+'Properties('+prop+').Val'
                cell=reader.read(source,lambda property_name=prop:circuit.ActiveCktElement.Properties(property_name).Val,
                    lambda value:_name(value))
                raw=cell['value']
                if cell['status']=='available':
                    try:
                        cell['value']=float(raw)
                        if not math.isfinite(cell['value']): raise ValueError('nonfinite')
                    except (ValueError,OverflowError):
                        cell=_failed(source,'invalid_native_power_text')
                cell['raw_text']=raw
                cell['precision']={'kind':'native_significant_digits','digits':6}
                fields[key]=cell
        final_name=reader.read(prefix+'.Name:after_power_read',lambda:collection.Name,_name)
        final_active=reader.read(_CE+'Name:after_power_read',lambda:circuit.ActiveCktElement.Name,_name)
        if (final_name['value'] is None or final_name['value'].casefold()!=requested.split('.',1)[1].casefold()
            or final_active['value'] is None or final_active['value'].casefold()!=requested.casefold()):
            result['issues'].append({'code':'power_identity_changed_during_read','source':prefix+'.Name'})
        else:
            result['identity_verified']=True
    finally:
        if previous['status']=='available':
            restored=reader.select(collection,'Name',previous['value'],prefix+'.Name:restore_power_selector')
            actual=reader.read(prefix+'.Name:after_power_restore',lambda:collection.Name,_name)
            if restored['status']!='available' or actual['value'] is None or actual['value'].casefold()!=previous['value'].casefold():
                result['issues'].append({'code':'power_selector_restore_failed','source':prefix+'.Name'})
        else:
            result['issues'].append({'code':'power_selector_before_read_unavailable','source':prefix+'.Name'})
        restored_element=reader.read('OpenDSS.ActiveCircuit.SetActiveElement:after_power_read',
            lambda:circuit.SetActiveElement(requested))
        final_element=reader.read(_CE+'Name:after_power_restore',lambda:circuit.ActiveCktElement.Name,_name)
        if restored_element['status']!='available' or final_element['value'] is None or final_element['value'].casefold()!=requested.casefold():
            result['issues'].append({'code':'power_active_element_restore_failed','source':_CE+'Name'})
        if result['issues']:
            result['identity_verified']=False
            for key,cell in list(fields.items()):
                fields[key]=_failed(cell['source'],'power_read_identity_or_restore_error')
    if result['identity_verified'] and not result['issues'] and set(fields)==keys:
        result['status']='available' if all(c['status']=='available' for c in fields.values()) else 'partial'
    return result
