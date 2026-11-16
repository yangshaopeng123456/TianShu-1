"""Explicit phase-aware JSON/CSV grid tables to immutable native DSS.

Copyright (c) yangshaopeng. No file discovery, execution or engineering defaults.
Object properties use the documented OpenDSS units and the existing bounded
snapshot parser. Bus/node inventory is explicit and checked before import.
"""
from __future__ import annotations
import csv,io,json,math,re,hashlib
from collections.abc import Mapping
from .contracts import ContractError

VERSION='grid-tables-1.0'
SCHEMA='tianzhi-grid/1.0'
KINDS={'circuit','linecode','line','transformer','load','generator','pvsystem','capacitor','reactor','xycurve','vsource'}
ID=re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,127}\Z')
TEXT=re.compile(r'[A-Za-z0-9_.%+:/-]{1,240}\Z')

def fail(field,message):raise ContractError('model.tables.'+field,message)
def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def number(v):return not isinstance(v,bool) and isinstance(v,(int,float)) and math.isfinite(v)
def unique(pairs):
 out={}
 for k,v in pairs:
  if k in out:fail('json','duplicate JSON property: '+k)
  out[k]=v
 return out
def parse_json(text):
 try:return json.loads(text,object_pairs_hook=unique,parse_constant=lambda x:fail('json','nonfinite JSON value'))
 except (ValueError,UnicodeError) as e:fail('json',str(e))
def literal(v):
 if isinstance(v,bool):return 'yes' if v else 'no'
 if number(v):return format(v,'.17g')
 if isinstance(v,str) and TEXT.fullmatch(v):return v
 if isinstance(v,list) and 0<len(v)<=100:
  if any(isinstance(x,list) for x in v):
   if not all(isinstance(x,list) and 0<len(x)<=100 and all(number(n) for n in x) for x in v):fail('properties','invalid numeric matrix')
   return '['+' | '.join(' '.join(literal(n) for n in x) for x in v)+']'
  return '['+' '.join(literal(x) for x in v)+']'
 fail('properties','use finite numbers, booleans, explicit arrays or bounded native identifiers')

def load_tables(payload):
 if not isinstance(payload,Mapping) or set(payload)!={'format','master_file','files'} or payload['format'] not in {'JSON','CSV'}:
  fail('input','expected format, master_file and files')
 from .models import _path,_unicode
 master=_path(payload['master_file'])
 if '/' in master or not isinstance(payload['files'],list) or len(payload['files'])!=1:fail('input','one package-root CSV or JSON source file required')
 item=payload['files'][0]
 if not isinstance(item,dict) or set(item)!={'path','content_utf8'} or item['path']!=master:fail('input','source entry must match the selected master')
 data=_unicode(item['content_utf8'],'model.tables.source')
 if not 0<len(data)<=2*1024*1024:fail('input','source exceeds 2 MiB budget')
 raw={master:data}
 if payload['format']=='JSON':
  if len(raw)!=1 or not master.lower().endswith('.json'):fail('input','JSON import requires one explicitly selected .json model')
  model=parse_json(raw[master].decode('utf-8-sig'))
 else:
  if len(raw)!=1 or not master.lower().endswith('.csv'):fail('input','CSV import requires one mixed-row .csv model')
  text=raw[master].decode('utf-8-sig')
  try:
   reader=csv.DictReader(io.StringIO(text,newline=''),strict=True)
   if reader.fieldnames!=['kind','id','properties']:fail('csv','header must be kind,id,properties; properties is a JSON object')
   rows=list(reader)
  except csv.Error as e:fail('csv',str(e))
  if not 2<=len(rows)<=20000:fail('csv','row budget is 2..20000')
  model={'schema_version':SCHEMA,'buses':[],'objects':[]}
  for r in rows:
   if None in r or any(v is None for v in r.values()):fail('csv','wrong column count')
   props=parse_json(r['properties'])
   if r['kind']=='system':
    if 'frequency_hz' in model:fail('csv','exactly one system row required')
    if r['id']!='system' or not isinstance(props,dict) or set(props)!={'frequency_hz','voltage_bases_kv'}:fail('csv','system properties require frequency_hz and voltage_bases_kv')
    model.update(props)
   elif r['kind']=='bus':model['buses'].append({'id':r['id'],**props}) if isinstance(props,dict) and set(props)=={'nodes'} else fail('csv','bus properties require nodes')
   else:model['objects'].append({'kind':r['kind'],'id':r['id'],'properties':props})
 return master,raw,model

def adapt_tables(payload):
 master,raw,model=load_tables(payload)
 if not isinstance(model,dict) or set(model)!={'schema_version','frequency_hz','voltage_bases_kv','buses','objects'} or model['schema_version']!=SCHEMA:fail('schema','unsupported grid table schema')
 hz=model['frequency_hz'];bases=model['voltage_bases_kv']
 if not number(hz) or not 0<hz<=1000:fail('frequency','explicit finite frequency_hz required')
 if not isinstance(bases,list) or not bases or len(bases)>100 or any(not number(x) or x<=0 for x in bases):fail('bases','positive line-line voltage bases in kV required')
 buses={}
 if not isinstance(model['buses'],list) or not 1<=len(model['buses'])<=10000:fail('buses','bus inventory required, at most 10000')
 for b in model['buses']:
  if not isinstance(b,dict) or set(b)!={'id','nodes'} or not isinstance(b['id'],str) or not ID.fullmatch(b['id']):fail('buses','bus requires bounded id and explicit nodes')
  nodes=b['nodes']
  if not isinstance(nodes,list) or not nodes or len(nodes)>16 or any(type(n)!=int or not 0<=n<=16 for n in nodes) or len(nodes)!=len(set(nodes)):fail('buses','unique integer nodes in 0..16 required')
  key=b['id'].casefold()
  if key in buses:fail('buses','duplicate bus id')
  buses[key]=set(nodes)
 def busref(value,object_id,field):
  if not isinstance(value,str):fail(field,'bus connection must be a string')
  parts=value.split('.');name=parts[0].casefold()
  if len(value)>240 or not ID.fullmatch(parts[0]) or len(parts)<2 or any(not x.isdigit() for x in parts[1:]):fail(field,'every connection requires a declared bus and explicit node suffixes')
  if name not in buses:fail(field,'undeclared_bus; object='+object_id+'; connection='+value)
  ns=list(map(int,parts[1:]))
  if any(n!=0 and n not in buses[name] for n in ns):fail(field,'undeclared_bus_nodes; object='+object_id+'; connection='+value+'; declared_nodes='+','.join(map(str,sorted(buses[name]))))
  return name,ns
 objects=model['objects']
 if not isinstance(objects,list) or not 1<=len(objects)<=20000:fail('objects','bounded object list required')
 seen={};circuits=0;used=set();lines=['Clear','Set DefaultBaseFrequency='+literal(hz)]
 for object_index,obj in enumerate(objects):
  if not isinstance(obj,dict) or set(obj)!={'kind','id','properties'} or obj['kind'] not in KINDS or not isinstance(obj['id'],str) or not ID.fullmatch(obj['id']):fail('objects','invalid object kind or id')
  kind=obj['kind'];ident=kind+'.'+obj['id'].casefold();props=obj['properties']
  if ident in seen:fail(f'objects[{object_index}].id','duplicate_device_id; object='+kind+'.'+obj['id']+'; first_index='+str(seen[ident])+'; duplicate_index='+str(object_index))
  seen[ident]=object_index
  if not isinstance(props,dict) or not props or any(not isinstance(k,str) or k!=k.lower() for k in props):fail('properties','explicit lowercase native property keys required')
  if kind=='circuit':
   circuits+=1
   if len(lines)!=2:fail('objects','circuit must be the first object')
   if not {'bus1','basekv','pu','phases','r1','x1','r0','x0'}<=set(props):fail('source','explicit source connection, voltage, phases and sequence impedances required')
   if 'frequency' in props and props['frequency']!=hz:fail('source','source frequency conflicts with system')
  def require_properties(required,alternative=()):
   missing=sorted(set(required)-set(props))
   if alternative and not (set(alternative)&set(props)):missing.append('|'.join(sorted(alternative)))
   if missing:
    first=missing[0];field=f'objects[{object_index}].properties'
    if re.fullmatch(r'[a-z][a-z0-9_]*',first):field+='.'+first
    fail(field,'missing_device_properties; object='+kind+'.'+obj['id']+'; missing='+','.join(missing))
  if kind not in {'linecode','xycurve'}:
   if type(props.get('phases'))!=int or props['phases'] not in {1,2,3}:fail('phases','explicit phases 1/2/3 required')
   if kind in {'circuit','vsource','load','generator','pvsystem'} and 'bus1' not in props:fail('connections','bus1 required')
   if kind=='line' and not {'bus1','bus2','units'}<=set(props):fail('connections','line requires bus1,bus2 and explicit length units')
   if kind=='transformer' and not {'windings','buses','conns','kvs','kvas','taps','%rs'}<=set(props):fail('connections','transformer requires explicit winding arrays')
   if kind in {'load','generator'}:require_properties({'model','conn','kv','kw'},{'kvar','pf'})
   if kind=='pvsystem':require_properties({'model','conn','kv','pmpp','irradiance','kva'},{'kvar','pf'})
  for key in ['bus','bus1','bus2']:
   if key in props:used.add(busref(props[key],kind+'.'+obj['id'],f'objects[{object_index}].properties.{key}')[0])
  if 'buses' in props:
   if not isinstance(props['buses'],list):fail('connections','buses must be an array')
   for bus_index,value in enumerate(props['buses']):used.add(busref(value,kind+'.'+obj['id'],f'objects[{object_index}].properties.buses[{bus_index}]')[0])
  lines.append('New '+kind+'.'+obj['id']+' '+' '.join(k+'='+literal(v) for k,v in props.items()))
 if circuits!=1:fail('source','exactly one first circuit required')
 if set(buses)!=used:fail('buses','declared bus inventory contains unused buses')
 lines+=['Set frequency='+literal(hz)+' mode=snapshot controlmode=off','Set voltagebases='+literal(bases),'CalcVoltageBases','']
 text='\n'.join(lines)
 # Apply the same lexical/property/device restrictions as native imports.
 from .models import _inspect,_require_consistent_devices
 from .device_semantics import inspect_device_semantics
 inspection=_inspect('Master.dss',{'Master.dss':text.encode('utf-8')})
 _require_consistent_devices(inspect_device_semantics(inspection))
 info={'version':'1.0','adapter_version':VERSION,'source_format':payload['format'],'target_format':'OpenDSS','balanced_equivalent':False,
  'physical_phase_mapping':True,'frequency_hz':hz,'bus_count':len(buses),'object_count':len(objects),
  'source_files':[{'path':n,'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)} for n,b in sorted(raw.items())]}
 return {'native_payload':{'master_file':'Master.dss','files':[{'path':'Master.dss','content_utf8':text}]},'conversion_manifest':info}

def packaged_tables(payload):
 a=adapt_tables(payload)
 return {'master_file':'Master.dss','files':a['native_payload']['files']+[
  {'path':'__tianshu_input__/request.json','content_utf8':canonical(payload).decode()},
  {'path':'__tianshu_input__/conversion.json','content_utf8':canonical(a['conversion_manifest']).decode()},
  *[{'path':'__tianshu_input__/original/'+r['path'],'content_utf8':r['content_utf8']} for r in payload['files']]]}

def verify_packaged_tables(master,files,request):
 rebuilt=packaged_tables(request)
 expected={x['path']:x['content_utf8'].encode('utf-8') for x in rebuilt['files']}
 if master!=rebuilt['master_file'] or files!=expected:fail('provenance','original input or deterministic conversion differs from frozen package')
 return adapt_tables(request)['conversion_manifest']
