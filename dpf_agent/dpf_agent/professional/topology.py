"""Source-supplied bus connectivity, separate from numerical convergence.

Copyright (c) yangshaopeng. This is a bus-level supply-reachability diagnostic,
not a phase-coupling, protection or intentional island-operation simulation.
An unresolved connection never becomes an invented healthy network.
"""
from __future__ import annotations
from collections import defaultdict,deque
from .device_semantics import _parts,_scalar
from .source_state import inspect_source_state

def base(value):return _scalar(value).casefold().split('.')[0]

def phase_count(props,issues,name):
 try:
  value=float(_scalar(props.get('phases','3')))
  if not value.is_integer() or not 1<=value<=3:raise ValueError()
  return int(value)
 except (ValueError,TypeError):
  issues.append(name+':phase_open_interpretation_unresolved');return 3

def inspect_topology(inspection,files):
 graph=defaultdict(set);roots=set();devices=[];issues=[];partial=[];active_buses=set()
 state=inspect_source_state(inspection,files)
 operations=defaultdict(list)
 for op in state.get('switch_operations',[]):operations[op['object_id']].append(op)
 combined={};order={(c['file'],c['line']):i for i,c in enumerate(inspection.get('commands',[]))}
 for row in inspection.get('objects',[]):
  key='vsource.source' if row['class']=='circuit' else row['name'].casefold()
  target=combined.setdefault(key,{'name':key,'class':'vsource' if row['class']=='circuit' else row['class'],'implicit':True,'properties':[]})
  target['properties'].extend(row['properties']);target['implicit'] &= row.get('implicit',False)
 for obj in combined.values():
  obj['properties'].sort(key=lambda p:order.get((p.get('source_file'),p.get('line')),-1))
  kind=obj['class'];props={};windings={};wdg=1
  for prop in obj['properties']:
   k,v=prop['name'],prop['value'];props[k]=v
   if k=='wdg':
    try:wdg=int(_scalar(v))
    except ValueError:issues.append('invalid_winding_selector')
   elif kind=='transformer' and k=='bus':windings[wdg]=v
   elif kind=='transformer' and k=='buses':
    try:windings={i+1:x for i,x in enumerate(_parts(v))}
    except ValueError:issues.append('invalid_winding_bus_array')
  if not props and obj.get('implicit'):continue
  if kind not in {'circuit','vsource','line','transformer','load','generator','pvsystem','capacitor','reactor'}:continue
  connections=[]
  if kind=='transformer':connections=list(windings.values())
  else:connections=[props[k] for k in ['bus1','bus2'] if k in props]
  buses=sorted({base(x) for x in connections if base(x) and not ('.' in _scalar(x) and all(n=='0' for n in _scalar(x).split('.')[1:]))})
  if not buses:
   issues.append(obj['name']+':connection_not_explicit');continue
  for bus in buses:graph[bus]
  enabled=_scalar(props.get('enabled','yes')).casefold() not in {'no','false','0'}
  if enabled:active_buses.update(buses)
  opened={};full_open=False
  for op in operations.get(obj['name'].casefold(),[]):
   term,cond=op['terminal'],op['conductor']
   phases=phase_count(props,issues,obj['name'])
   for c in (range(1,phases+1) if cond==0 else [cond]):opened[(term,c)]=op['open']
  if opened:
   phases=phase_count(props,issues,obj['name'])
   full_open=any(all(opened.get((t,c),False) for c in range(1,phases+1)) for t in (1,2))
   if not full_open and any(opened.values()):partial.append(obj['name'])
  devices.append({'object_id':obj['name'],'class':kind,'buses':buses,'enabled':enabled,'fully_open':full_open})
  if enabled and not full_open:
   if kind in {'circuit','vsource'} and 'bus1' in props:roots.add(base(props['bus1']))
   if kind in {'line','transformer'} or kind in {'capacitor','reactor'} and len(buses)>1:
    if len(buses)<2:issues.append(obj['name']+':fewer_than_two_bus_connections')
    for b in buses[1:]:graph[buses[0]].add(b);graph[b].add(buses[0])
 if state['status']!='complete':issues.append('switch_state_replay_not_complete')
 declared_sources=any(d['class'] in {'circuit','vsource'} and d['buses'] for d in devices)
 if not roots and not declared_sources:issues.append('source_bus_not_explicit')
 reachable=set(roots);todo=deque(roots)
 while todo:
  for b in graph[todo.popleft()]-reachable:reachable.add(b);todo.append(b)
 isolated=sorted(active_buses-reachable) if declared_sources else []
 remaining=set(graph);components=[]
 while remaining:
  start=min(remaining);part={start};queue=[start]
  while queue:
   for b in graph[queue.pop()]-part:part.add(b);queue.append(b)
  remaining-=part;components.append({'buses':sorted(part),'source_connected':bool(part&roots)})
 complete=not issues
 return {'schema_version':'1.0','scope':'declared_bus_connectivity_not_phase_or_operating_island_validation',
  'status':'no_active_source' if declared_sources and not roots and complete else 'islands_detected' if isolated and complete else 'connected' if complete and not partial else 'partial',
  'complete':complete,'bus_count':len(graph),'source_buses':sorted(roots),'isolated_buses':isolated,
  'inactive_or_disabled_only_buses':sorted(set(graph)-active_buses),
  'isolated_devices':[d['object_id'] for d in devices if d['enabled'] and d['buses'] and all(b in isolated for b in d['buses'])],
  'partial_phase_open_devices':sorted(partial),'components':components,'issues':issues,
  'engineering_accepted':False,'limitations':['Bus-level graph uses explicit input connections and Open/Close operations; omitted enabled/phases follow declared OpenDSS snapshot defaults for topology only, not engineering ratings.',
  'Partial-phase interruptions are listed separately and are not certified by the bus graph. No automatic source restoration, switching or intentional island operation is performed.']}

def diagnose_native_topology(raw):
 """Project identity-checked final COM connections and IsOpen facts to a graph."""
 rows=[];commands=[];missing=[]
 state=raw.get('solver',{}).get('device_connections',{}).get('snapshot_state',{})
 native={r.get('object_id','').casefold():r for r in state.get('objects',[])}
 elements=raw.get('lines',[])+raw.get('transformers',[])+raw.get('power_conversion_elements',[])+raw.get('auxiliary_elements',[])
 for index,item in enumerate(elements,1):
  name=item.get('name','');kind=name.split('.')[0].casefold();data=item.get('terminal_data',{})
  if kind not in {'line','transformer','load','generator','pvsystem','vsource','capacitor','reactor'}:continue
  if not data.get('identity_verified') or data.get('status')!='available':missing.append(name);continue
  buses=data.get('bus_names',[])
  props={'enabled':'yes' if data['enabled'] else 'no','phases':str(data['num_phases'])}
  if kind=='transformer':props['buses']='['+' '.join(buses)+']'
  else:props.update({f'bus{i+1}':v for i,v in enumerate(buses[:2])})
  rows.append({'name':name.casefold(),'class':kind,'implicit':False,'properties':[{'name':k,'value':v,'source_file':'native','line':index} for k,v in props.items()]})
  commands.append({'command':'new','file':'native','line':index})
 # Replay the actual observed open states, not guessed source defaults.
 for item in rows:
  if item['class']!='line':continue
  opened=native.get(item['name'],{}).get('open_conductors',{})
  if opened.get('status')!='available' or not isinstance(opened.get('value'),list):missing.append(item['name']+':IsOpen');continue
  for terminal,values in enumerate(opened['value'],1):
   for conductor,value in enumerate(values,1):
    if type(value) is not bool:missing.append(item['name']+':IsOpen');continue
    if value:
     # Make explicit source-equivalent Open operations for the graph helper.
     line=len(commands)+1
     commands.append({'command':'open','file':'native','line':line,'object_id':item['name'],
      'properties':[{'name':'term','value':str(terminal)},{'name':'cond','value':str(conductor)}]})
 result=inspect_topology({'objects':rows,'commands':commands},{})
 result.update(source='identity_checked_COM_connections_and_IsOpen',scope='native_bus_supply_connectivity_not_phase_or_island_operation_validation')
 if missing:
  result['issues']+=missing;result['complete']=False;result['status']='partial'
 return result
