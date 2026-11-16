"""Compare explicit nominal parameters, never solved output or engineering limits.

Copyright (c) yangshaopeng. Generator base P/Q is observable through a six
significant-digit text getter. Its bounded consistency check is explicitly
different from equality of native double getters and cannot detect sub-bin
changes. PV reactive mode/requested Q remain unobserved in this COM contract.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import math
import re
import sys


_CE='OpenDSS.ActiveCircuit.ActiveCktElement.'
_SCOPE={'load':{'kw_base','kvar_base','pf_nominal','kva'},
    'generator':{'kw_base','kvar_base','pf_nominal','kva'},
    'pvsystem':{'pmpp','irradiance','pf_nominal','kva','reactive_mode','kvar_requested'}}
_INTERFACE={'load':'Loads','generator':'Generators','pvsystem':'PVSystems'}
_DOUBLE={'load':{'kw_base':'kW','kvar_base':'kvar','pf_nominal':'PF','kva':'kVA'},
    'generator':{'pf_nominal':'PF','kva':'kVArated'},
    'pvsystem':{'pmpp':'Pmpp','irradiance':'Irradiance','pf_nominal':'PF','kva':'kVArated'}}
_RAW_NUMBER=re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?\Z')


def _finite(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_value(key,value):
    if key=='reactive_mode':
        return value in ('source_selected_pf','source_selected_kvar')
    if not _finite(value): return False
    if key=='pf_nominal': return 0<abs(value)<=1
    if key=='kva': return value>0
    if key in {'pmpp','irradiance'}: return value>=0
    return True


def quantized_consistency(expected,raw_text,native_value):
    """Validate the fixed official 6g getter, then compare its rounding bin.

    The bin is determined from EXPECTED magnitude. A rounded result crossing a
    power of ten must not enlarge the interval. Zero cannot match a nonzero raw
    number. Invalid native text returns None, rather than a false mismatch.
    """
    if not _finite(expected) or not _finite(native_value) or not isinstance(raw_text,str):
        return None
    # Subnormal doubles have ULPs comparable to the whole quantity. Their
    # binary allowance could swallow the six-digit decimal interval entirely.
    if expected!=0 and abs(expected)<sys.float_info.min:
        return None
    text=raw_text.strip()
    if not 1<=len(text)<=64 or not _RAW_NUMBER.fullmatch(text): return None
    try:
        decimal=Decimal(text)
        if not decimal.is_finite() or float(decimal)!=native_value: return None
        if decimal!=0 and native_value==0: return None
        digits=''.join(str(n) for n in decimal.as_tuple().digits).lstrip('0').rstrip('0')
        if len(digits)>6: return None
        target=Decimal(str(expected))
        if target==0:
            matched=decimal==0
            quantum=Decimal(0)
        else:
            quantum=Decimal(10)**(target.copy_abs().adjusted()-5)
            allowance=quantum/2+Decimal(str(8*math.ulp(float(expected))))
            matched=abs(target-decimal)<=allowance
        return {'consistent':matched,'comparison_kind':'native_6_significant_digit_consistency',
            'precision_limited':True,'full_precision_equality':False,
            'rounding_quantum':float(quantum),'raw_text':raw_text}
    except (InvalidOperation,ValueError,OverflowError):
        return None


def compare_power_operating_points(declarations,native_by_name):
    """Validate a complete bounded 1.2 declaration extension and return checks."""
    report=declarations.get('power_operating_points')
    if (not isinstance(report,Mapping) or report.get('version')!='1.0'
        or report.get('scope')!='bounded_native_power_setpoints'
        or report.get('status') not in {'complete','partial'}
        or report.get('engineering_accepted') is not False
        or not isinstance(report.get('objects'),list) or not isinstance(report.get('issues'),list)
        or any(not isinstance(i,Mapping) or i.get('severity')=='error' for i in report['issues'])):
        raise ValueError('Power declarations are absent, invalid or untrusted.')
    expected={row['object_id'].casefold():row['class'] for row in declarations['objects'] if row['class'] in _SCOPE}
    rows={}
    for row in report['objects']:
        if not isinstance(row,Mapping) or not isinstance(row.get('object_id'),str):
            raise ValueError('Power declaration identity is malformed.')
        name=row['object_id'].casefold()
        if (name in rows or name not in expected or row.get('class')!=expected[name]
            or row.get('status') not in {'complete','partial'}
            or not isinstance(row.get('issues'),list)
            or any(not isinstance(i,Mapping) or i.get('severity')=='error' for i in row['issues'])
            or not isinstance(row.get('fields'),Mapping) or set(row['fields'])!=_SCOPE[expected[name]]):
            raise ValueError('Power object identity or supported field coverage is invalid.')
        rows[name]=row
    if set(rows)!=set(expected): raise ValueError('Power declaration object coverage is incomplete.')
    checks=[]
    def add(row,key,status,cell,actual=None,reason=None,source=None,**extra):
        item={'object_id':row['object_id'],'field':'power.'+key,'status':status,
            'expected':cell.get('value'),'actual':actual,'source_refs':cell.get('source_refs',[]),
            'reason':reason,'native_source':source,**extra}
        checks.append(item)
    for name,row in rows.items():
        kind=row['class']
        prefix='OpenDSS.ActiveCircuit.'+_INTERFACE[kind]
        native=native_by_name.get(name)
        point=native.get('power_setpoints') if isinstance(native,Mapping) else None
        point_valid=(isinstance(point,Mapping) and point.get('version')=='1.0'
            and point.get('scope')=='read_only_native_power_setpoints'
            and isinstance(point.get('object_id'),str) and point['object_id'].casefold()==name
            and point.get('class')==kind and point.get('identity_verified') is True
            and point.get('identity_source')==prefix+'.Name'
            and point.get('status') in {'available','partial'} and point.get('issues')==[]
            and point.get('electrical_parameters_modified') is False and isinstance(point.get('fields'),Mapping))
        if not point_valid:
            checks.append({'object_id':row['object_id'],'field':'power.runtime_identity','status':'not_available',
                'expected':None,'actual':None,'source_refs':[],
                'reason':'Trusted additive native power readback with exact identity is required.',
                'native_source':prefix+'.Name'})
        for key,cell in row['fields'].items():
            if (not isinstance(cell,Mapping) or cell.get('status') not in {'declared','uncertain','not_declared'}
                or not isinstance(cell.get('source_refs'),list)):
                raise ValueError('Power field/source shape is malformed.')
            refs=cell['source_refs']
            for ref in refs:
                if (not isinstance(ref,Mapping) or any(not isinstance(ref.get(k),str) or not ref[k].strip() for k in ('source_file','property'))
                    or isinstance(ref.get('line'),bool) or not isinstance(ref.get('line'),int) or not 1<=ref['line']<=100000):
                    raise ValueError('Power field provenance is malformed.')
            if cell['status']!='declared':
                if cell.get('value') is not None: raise ValueError('An unresolved power field cannot claim a value.')
                add(row,key,'not_checked',cell,reason='No reliable source-only parameter value; native defaults are not filled.')
                continue
            value=cell.get('value')
            if not refs or not _valid_value(key,value):
                raise ValueError('Declared power value/provenance is invalid.')
            if kind=='pvsystem' and key in {'reactive_mode','kvar_requested'}:
                add(row,key,'not_checked',cell,reason='The COM contract does not expose the source-selected reactive mode or requested kvar; output kvar is not a substitute.')
                continue
            source=(_CE+'Properties('+{'kw_base':'kw','kvar_base':'kvar'}[key]+').Val'
                if kind=='generator' and key in {'kw_base','kvar_base'} else prefix+'.'+_DOUBLE[kind][key])
            observed=point.get('fields',{}).get(key) if point_valid else None
            if (not isinstance(observed,Mapping) or observed.get('status')!='available'
                or observed.get('source')!=source or observed.get('issues')!=[] or not _finite(observed.get('value'))):
                add(row,key,'not_available',cell,reason='Reliable parameter has no trusted finite getter observation.',source=source)
                continue
            actual=observed['value']
            if kind=='generator' and key in {'kw_base','kvar_base'}:
                comparison=(quantized_consistency(value,observed.get('raw_text'),actual)
                    if observed.get('precision')=={'kind':'native_significant_digits','digits':6} else None)
                if comparison is None:
                    add(row,key,'not_available',cell,reason='Native 6g parameter text/precision is invalid or missing.',source=source)
                else:
                    matched=comparison.pop('consistent')
                    add(row,key,'matched' if matched else 'mismatch',cell,actual=actual,source=source,**comparison)
            elif observed.get('precision')!={'kind':'native_binary_double'}:
                add(row,key,'not_available',cell,reason='Native double precision contract is missing.',source=source)
            else:
                matched=math.isclose(value,actual,rel_tol=1e-10,abs_tol=1e-12)
                add(row,key,'matched' if matched else 'mismatch',cell,actual=actual,source=source,
                    comparison_kind='native_double_parameter_equality',precision_limited=False,
                    relative_tolerance=1e-10,absolute_tolerance=1e-12)
    return checks
