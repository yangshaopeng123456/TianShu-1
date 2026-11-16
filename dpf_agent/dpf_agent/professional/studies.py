"""Durable curve-driven quasi-static studies using verified snapshot children.

Copyright (c) yangshaopeng. Fixed controls; each point has an immutable derived
model, ordinary numerical gates and its own task identity. No dynamic/control
state continuity or silent replay after interruption is claimed.
"""
from __future__ import annotations
from pathlib import Path
import sqlite3,json,time,threading,hashlib,uuid,datetime,math,csv,io,html
from contextlib import contextmanager
from .contracts import ContractError,RunRequest
from .jobs import TERMINAL,RequestConflict
from .models import pack_directory,_canonical,_OBJECT

def fail(field,msg):raise ContractError('study.'+field,msg)
def finite(x):return type(x) in {int,float} and math.isfinite(x)

def validate_study(value):
 fields={'request_id','mode','curve_source','base_request','points','on_point_error'}
 file_mode=isinstance(value,dict) and 'curve_file' in value
 expected=(fields-{'points'})|{'curve_file'} if file_mode else fields
 if not isinstance(value,dict) or set(value)!=expected:fail('request','provide explicit points or one curve_file, not both')
 if file_mode:
  value=dict(value);source=value['curve_file']
  try:
   if isinstance(source,dict) and isinstance(source.get('content_utf8'),str):source['content_utf8'].encode('utf-8')
  except UnicodeError:fail('curve_file','invalid UTF-8 curve')
  if not isinstance(source,dict) or set(source)!={'format','name','content_utf8'} or source['format'] not in {'CSV','JSON'} or not isinstance(source['name'],str) or len(source['name'])>240 or not isinstance(source['content_utf8'],str) or len(source['content_utf8'].encode('utf-8'))>1024*1024:fail('curve_file','bounded selected CSV/JSON source content required')
  try:
   if source['format']=='JSON':
    from .tabular_input import parse_json
    points=parse_json(source['content_utf8'])
   else:
    reader=csv.DictReader(io.StringIO(source['content_utf8'].lstrip('\ufeff'),newline=''),strict=True)
    if not reader.fieldnames or reader.fieldnames[0]!='at' or len(set(reader.fieldnames))!=len(reader.fieldnames) or not set(reader.fieldnames)<={'at','load_multiplier','generator_multiplier'}:fail('curve_file','CSV header is at,load_multiplier,generator_multiplier (at and at least one multiplier required)')
    points=[]
    for row in reader:
     if None in row or any(v is None for v in row.values()):fail('curve_file','wrong CSV field count')
     points.append({k:(v if k=='at' else float(v)) for k,v in row.items() if v!=''})
     if len(points)>1000:fail('curve_file','at most 1000 curve rows supported')
   value['points']=points
  except (ValueError,TypeError,csv.Error,UnicodeError):fail('curve_file','invalid UTF-8 CSV/JSON curve')
 if not isinstance(value['request_id'],str) or not 1<=len(value['request_id'])<=128:fail('request_id','bounded unique request id required')
 if value['mode']!='quasistatic_fixed_controls':fail('mode','only fixed-control quasi-static curve studies supported')
 if not isinstance(value['curve_source'],str) or not 1<=len(value['curve_source'])<=1000:fail('curve_source','explicit curve source required')
 base=RunRequest.from_dict(value['base_request'])
 if base.schema_version!='1.1' or base.operating_conditions is None:fail('base_request','schema 1.1 with explicit operating conditions required')
 if set(base.output_formats)!={'json','html','csv'} or set(base.result_scopes)!={'bus','branch','transformer','system'}:fail('base_request','complete result domains and json/html/csv required')
 if value['on_point_error'] not in {'stop','continue'}:fail('on_point_error','choose stop or continue explicitly')
 points=value['points']
 if not isinstance(points,list) or not 1<=len(points)<=1000:fail('points','1..1000 explicit time points required')
 previous=None
 for point in points:
  if not isinstance(point,dict) or not {'at'}<=set(point) or not set(point)<={'at','load_multiplier','generator_multiplier','setpoints'} or set(point)=={'at'}:fail('points','point requires at and an explicit multiplier or setpoint')
  try:
   stamp=datetime.datetime.fromisoformat(point['at'].replace('Z','+00:00'))
   if stamp.tzinfo is None or stamp.utcoffset() is None:raise ValueError()
  except (ValueError,TypeError,AttributeError):fail('points','timestamps must include timezone offsets')
  if previous is not None and stamp<=previous:fail('points','timestamps must be strictly increasing and unique')
  previous=stamp
  for key in ['load_multiplier','generator_multiplier']:
   if key in point and (not finite(point[key]) or not 0<=point[key]<=1000):fail('points','multipliers must be finite numbers in 0..1000')
  seen=set();sets=point.get('setpoints',[])
  if not isinstance(sets,list) or len(sets)>2000:fail('setpoints','setpoint array exceeds budget')
  for s in sets:
   if not isinstance(s,dict) or set(s)!={'object_id','properties'} or not isinstance(s['object_id'],str) or not _OBJECT.fullmatch(s['object_id']):fail('setpoints','explicit device identity and properties required')
   ident=s['object_id'].casefold();kind=ident.split('.')[0]
   if ident in seen:fail('setpoints','duplicate device setpoint')
   seen.add(ident);props=s['properties']
   allowed={'load':{'kw','kvar','pf'},'generator':{'kw','kvar','pf'},'pvsystem':{'irradiance','temperature','pf','kvar'}}.get(kind,set())
   if not isinstance(props,dict) or not props or not set(props)<=allowed or any(not finite(x) for x in props.values()):fail('setpoints','unsupported or nonfinite fixed operating setpoint')
   if 'pf' in props and ('kvar' in props or not 0<abs(props['pf'])<=1):fail('setpoints','use either PF or kvar; nonzero PF must be within [-1,1]')
   if 'irradiance' in props and props['irradiance']<0:fail('setpoints','irradiance cannot be negative')
 # This also bounds deeply nested criteria/data and excludes nonfinite JSON.
 try:wire=_canonical(value)
 except (ValueError,TypeError,RecursionError):fail('request','finite bounded JSON required')
 if len(wire)>2*1024*1024:fail('request','study exceeds 2 MiB')
 return json.loads(wire),base

def point_metrics(result):
 out={}
 if not result or not result.get('publishable') or result.get('numerical_validity')!='converged_valid':return out
 wanted={'native_minimum_voltage_pu','network_active_loss_kw','maximum_line_current_loading_percent','maximum_transformer_current_loading_percent','maximum_transformer_kva_loading_percent'}
 for m in result.get('engineering_analysis',{}).get('metrics',[]):
  if m['metric'] in wanted and m.get('status')=='available':
   out[m['metric']]={k:m.get(k) for k in ['value','unit','measurement_basis','object_id','node','reference_node','terminal','conductor','base_voltage_v']}
 return out

class StudyManager:
 def __init__(self,service,start_worker):
  self.service=service;self.root=service.root/'studies';self.root.mkdir(exist_ok=True)
  self.db=self.root/'studies.sqlite3';self.stop=threading.Event();self.wake=threading.Event();self.thread=None
  with self.connect() as c:
   c.execute('CREATE TABLE IF NOT EXISTS studies (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, digest TEXT NOT NULL, request_json TEXT NOT NULL, baseline TEXT NOT NULL, status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, result_json TEXT NOT NULL, artifacts_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)')
   rows=c.execute("SELECT id,result_json FROM studies WHERE status NOT IN ('finished','failed','cancelled','interrupted')").fetchall()
   for ident,raw in rows:
    r=json.loads(raw);r.update(execution_status='interrupted',automatic_replay=False,interruption_reason='service_restart_requires_explicit_new_study')
    # Read back only the deterministic existing child request identities. This
    # also closes the crash window between child submit and parent attachment;
    # recovery never creates a child or replays a numerical request.
    for point in r['points']:
     child_request=ident+'-p'+str(point['index'])
     with self.service.store.connect() as jobs:
      child=jobs.execute('SELECT task_id,request_json FROM jobs WHERE request_id=?',(child_request,)).fetchone()
     if child is None:continue
     task=self.service.get_task(child['task_id']);request=json.loads(child['request_json'])
     point.update(task_id=task['task_id'],attempt_id=task['attempt_id'],child_request_id=child_request,
      snapshot_id=request['model'].get('snapshot_id'),execution_status=task['execution_status'],
      numerical_validity=task['numerical_validity'],error=task['error'])
     committed=self.service.result(task['task_id']);point['metrics']=point_metrics(committed)
     if committed:point.update(request_digest=committed['request_digest'],engineering_assessment=committed['assessment']['status'])
    c.execute("UPDATE studies SET status='interrupted',result_json=?,updated_at=? WHERE id=?",(json.dumps(r,ensure_ascii=False),time.time(),ident))
  if start_worker:self.start()
 def start(self):
  if self.thread is not None:raise RuntimeError('study worker already started')
  self.thread=threading.Thread(target=self.loop,name='quasistatic-study',daemon=True);self.thread.start()
 @contextmanager
 def connect(self):
  c=sqlite3.connect(self.db,timeout=10)
  try:yield c;c.commit()
  except BaseException:c.rollback();raise
  finally:c.close()
 def submit(self,payload):
  request,base=validate_study(payload);digest=hashlib.sha256(_canonical(request)).hexdigest()
  with self.connect() as c:
   c.execute('BEGIN IMMEDIATE')
   old=c.execute('SELECT id,digest FROM studies WHERE request_id=?',(request['request_id'],)).fetchone()
   if old:
    if old[1]!=digest:raise RequestConflict('study request_id belongs to different curve or inputs')
    return self.get(old[0])
   if self.stop.is_set():raise RuntimeError('study manager is closing')
   if base.model.snapshot_id:
    baseline=base.model.snapshot_id
   else:
    if base.model.case_id not in self.service.allowed_cases:fail('base_request','case is not registered')
    baseline=self.service.import_model(pack_directory(self.service.cases_root/base.model.case_id,'Master.dss'))['snapshot_id']
   inspected=self.service.inspect_model(baseline)
   if inspected.get('balanced_equivalent'):
    raise ContractError('study.base_request','MATPOWER synthetic balanced embeddings currently support single snapshots only; derived studies must preserve physical-phase provenance','capability_unavailable')
   objects={o['name'].casefold() for o in inspected['inspection']['objects']}
   for p in request['points']:
    for s in p.get('setpoints',[]):
     if s['object_id'].casefold() not in objects:fail('setpoints','device does not exist in base snapshot')
   ident='study-'+uuid.uuid4().hex;now=time.time()
   result={'schema_version':'1.0','study_id':ident,'request_id':request['request_id'],'request_digest':digest,'base_snapshot_id':baseline,
    'mode':request['mode'],'curve_source':request['curve_source'],'execution_status':'queued','automatic_replay':False,
    'points':[{'index':i,'at':p['at'],'execution_status':'not_run','task_id':None,'numerical_validity':'not_run','metrics':{}} for i,p in enumerate(request['points'])],
    'engineering_accepted':False,'limitations':['Quasi-static, fixed controls; points are independently frozen snapshots. No dynamic state or automatic control continuity.',
     'Time labels belong to the explicit study curve; native snapshot DblHour is not a dynamic clock.','Generator multiplier does not change PV irradiance; use explicit PV setpoints.']}
   if 'curve_file' in request:result['curve_file']={'name':request['curve_file']['name'],'format':request['curve_file']['format'],'sha256':hashlib.sha256(request['curve_file']['content_utf8'].encode('utf-8')).hexdigest(),'original_preserved_in_request':True}
   c.execute('INSERT INTO studies(id,request_id,digest,request_json,baseline,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
    (ident,request['request_id'],digest,json.dumps(request,ensure_ascii=False),baseline,'queued',json.dumps(result,ensure_ascii=False),now,now))
  self.wake.set();return self.get(ident)
 def row(self,ident):
  with self.connect() as c:
   c.row_factory=sqlite3.Row;r=c.execute('SELECT * FROM studies WHERE id=?',(ident,)).fetchone()
  if r is None:raise KeyError(ident)
  return dict(r)
 def by_request(self,request_id):
  with self.connect() as c:r=c.execute('SELECT id FROM studies WHERE request_id=?',(request_id,)).fetchone()
  if r is None:raise KeyError(request_id)
  return self.get(r[0])
 def get(self,ident):
  row=self.row(ident);r=json.loads(row['result_json']);r.update(execution_status=row['status'],cancel_requested=bool(row['cancel_requested']),artifacts=list(json.loads(row['artifacts_json'] or '{}')))
  request=json.loads(row['request_json'])
  if hashlib.sha256(_canonical(request)).hexdigest()!=row['digest']:
   raise ContractError('study.request','saved study request integrity failed','model_integrity_error')
  r['request_context']={'base_snapshot_id':row['baseline'],'base_request':request['base_request'],'curve_source':request['curve_source'],
   'points':request['points'],'on_point_error':request['on_point_error'],'request_digest':row['digest'],
   'source':'read_only_projection_of_durable_original_request','recomputed':False}
  for point,settings in zip(r['points'],request['points']):point['requested_settings']=settings
  r['completed_points']=sum(p['execution_status'] in TERMINAL for p in r['points'])
  r['valid_points']=sum(p['numerical_validity']=='converged_valid' for p in r['points'])
  r['failed_points']=sum(p['execution_status'] in {'failed','interrupted'} or p['execution_status']=='finished' and p['numerical_validity']!='converged_valid' for p in r['points'])
  return r
 def cancel(self,ident):
  with self.connect() as c:
   c.execute("UPDATE studies SET cancel_requested=1,updated_at=? WHERE id=? AND status NOT IN ('finished','failed','cancelled','interrupted')",(time.time(),ident))
  self.wake.set();return self.get(ident)
 def update(self,ident,result,status=None):
  with self.connect() as c:
   c.execute('UPDATE studies SET result_json=?,status=COALESCE(?,status),updated_at=? WHERE id=?',
    (json.dumps(result,ensure_ascii=False,allow_nan=False),status,time.time(),ident))
 def derive(self,baseline,study_id,index,point):
  folder,manifest=self.service.model_store.resolve(baseline)
  files=[]
  for item in manifest['entries']:
   name=item['path'];target='study_source/'+name if name.startswith('__tianshu_input__/') else name
   files.append({'path':target,'content_utf8':(folder/name).read_bytes().decode('utf-8')})
  master=next(f for f in files if f['path']==manifest['master_file'])
  extra=[]
  for key,dss in [('load_multiplier','LoadMult'),('generator_multiplier','GenMult')]:
   if key in point:extra.append('Set '+dss+'='+format(point[key],'.17g'))
  for s in point.get('setpoints',[]):extra.append('Edit '+s['object_id']+' '+' '.join(k+'='+format(v,'.17g') for k,v in s['properties'].items()))
  master['content_utf8']+='\n! Explicit authorized quasi-static curve point\n'+'\n'.join(extra)+'\n'
  files.append({'path':'__tianzhi_study__/'+study_id+'-p'+str(index)+'.json','content_utf8':_canonical({'study_id':study_id,'index':index,'point':point,'base_snapshot_id':baseline,'base_fingerprint':manifest['fingerprint']}).decode()})
  return self.service.import_model({'master_file':manifest['master_file'],'files':files})['snapshot_id']
 def run(self,ident):
  row=self.row(ident);request=json.loads(row['request_json']);result=json.loads(row['result_json']);base=request['base_request'];started=time.perf_counter()
  result['execution_status']='running';self.update(ident,result,'running')
  for index,point in enumerate(request['points']):
   if self.stop.is_set() or self.get(ident)['cancel_requested']:break
   record=result['points'][index]
   try:
    snapshot=self.derive(row['baseline'],ident,index,point)
    child=json.loads(json.dumps(base));child['request_id']=ident+'-p'+str(index)
    child['model']={'snapshot_id':snapshot};child['scenario']={'scenario_id':ident+'-p'+str(index),'mode':'existing_snapshot'}
    child['operating_conditions']['source']='Explicit study '+ident+', point '+str(index)+', at '+point['at']+'; '+request['curve_source']
    # A parent cancellation racing the submit is observed again immediately
    # after the real child identity has been durably attached.
    task=self.service.submit(child)
    record.update(task_id=task['task_id'],attempt_id=task['attempt_id'],child_request_id=child['request_id'],snapshot_id=snapshot,execution_status=task['execution_status'])
    self.update(ident,result)
    while task['execution_status'] not in TERMINAL:
     if self.stop.is_set() or self.get(ident)['cancel_requested']:self.service.cancel(task['task_id'])
     self.wake.wait(.2);self.wake.clear();task=self.service.get_task(task['task_id'])
     if record['execution_status']!=task['execution_status']:
      record['execution_status']=task['execution_status'];self.update(ident,result)
    child_result=self.service.result(task['task_id'])
    record.update(execution_status=task['execution_status'],numerical_validity=task['numerical_validity'],error=task['error'],metrics=point_metrics(child_result))
    if child_result:record.update(request_digest=child_result['request_digest'],engineering_assessment=child_result['assessment']['status'])
   except Exception as exc:
    # An exception after submission must not leave an untracked child running.
    child_id=record.get('task_id')
    if child_id:
     current=self.service.get_task(child_id)
     if current['execution_status'] not in TERMINAL:
      self.service.cancel(child_id)
      while current['execution_status'] not in TERMINAL:
       time.sleep(.2);current=self.service.get_task(child_id)
     record['actual_child_execution_status']=current['execution_status']
    record.update(execution_status='failed',numerical_validity='not_run',error=str(exc),metrics={})
   self.update(ident,result)
   if record['numerical_validity']!='converged_valid' and request['on_point_error']=='stop':break
  cancelled=self.stop.is_set() or self.get(ident)['cancel_requested']
  result.update(elapsed_s=time.perf_counter()-started,execution_status='cancelled' if cancelled else 'finished',
   completion_status='cancelled' if cancelled else 'complete' if all(p['numerical_validity']=='converged_valid' for p in result['points']) else 'incomplete_or_failed_points')
  self.finish(ident,result)
 def finish(self,ident,result):
  from .service import atomic_text
  from .reporting import safe_csv_cell
  root=self.root/ident;root.mkdir(exist_ok=True)
  result['execution_status']='cancelled' if self.get(ident)['cancel_requested'] else result['execution_status']
  texts={'result.json':json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)}
  stream=io.StringIO(newline='');writer=csv.writer(stream)
  writer.writerow(['index','at','task_id','execution_status','numerical_validity','metric','value','unit','measurement_basis','object_id','node','error'])
  table=[]
  for p in result['points']:
   for metric,m in (p['metrics'] or {'':{}}).items():
    writer.writerow([safe_csv_cell(x) for x in [p['index'],p['at'],p['task_id'],p['execution_status'],p['numerical_validity'],metric,m.get('value'),m.get('unit'),m.get('measurement_basis'),m.get('object_id'),m.get('node'),p.get('error')]])
   labels={'native_minimum_voltage_pu':'最低节点电压','network_active_loss_kw':'全域有功网损','maximum_line_current_loading_percent':'线路最大电流负载率','maximum_transformer_current_loading_percent':'变压器最大电流负载率','maximum_transformer_kva_loading_percent':'变压器最大容量负载率'}
   escape=lambda value:html.escape(str(value))
   rows=[]
   for key,m in p['metrics'].items():
    position='；'.join(k+'='+str(m[k]) for k in ['object_id','node','reference_node','terminal','conductor','base_voltage_v'] if m.get(k) is not None)
    rows.append('<tr>'+''.join('<td>'+escape(v)+'</td>' for v in [labels.get(key,key),format(m['value'],'.12g'),m['unit'],str(m['measurement_basis'])+'；'+position])+'</tr>')
   table.append('<section><h2>时点 '+str(p['index']+1)+' · '+escape(p['at'])+'</h2><p>子任务：'+escape(p['task_id'] or '未运行')+'<br>执行：'+escape(p['execution_status'])+'；数值：'+escape(p['numerical_validity'])+'</p>'+('<p>异常：'+escape(p['error'])+'</p>' if p.get('error') else '')+('<table><thead><tr><th style="width:23%">指标</th><th style="width:17%">数值</th><th style="width:10%">单位</th><th>口径与位置</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>' if rows else '<p>没有可发布的有效指标；未补零。</p>')+'</section>')
  texts['points.csv']=stream.getvalue()
  texts['report.html']='<!doctype html><html lang="zh"><meta charset="utf-8"><title>天智五号时序报告</title><style>body{font-family:Microsoft YaHei,sans-serif;margin:32px;color:#182633}h2{font-size:18px;margin-top:24px}table{border-collapse:collapse;width:100%;table-layout:fixed}td,th{border:1px solid #ddd;padding:6px;overflow-wrap:anywhere;text-align:left}th{background:#eef3f8}@media print{body{margin:0}thead{display:table-header-group}tr{break-inside:avoid}h2{break-after:avoid}}</style><h1>天智五号准静态时序计算</h1><p>'+html.escape(ident)+'；'+html.escape(result['execution_status'])+'；'+html.escape(result.get('completion_status',''))+'</p><p>固定控制、独立逐点快照；不是动态或自动控制仿真。缺失指标不补零，整体工程合规不作合格结论。报告展示12位有效数字，完整原值保留在JSON/CSV。</p><p><a href="result.json">完整结果</a>　<a href="points.csv">逐点CSV</a></p>'+''.join(table)+'</html>'
  artifacts={}
  for name,text in texts.items():
   atomic_text(root/name,text);b=(root/name).read_bytes();artifacts[name]={'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
  with self.connect() as c:
   c.execute('BEGIN IMMEDIATE');cancel=c.execute('SELECT cancel_requested FROM studies WHERE id=?',(ident,)).fetchone()[0]
   if cancel and result['execution_status']!='cancelled':
    # Regenerate a raced terminal projection before committing its identity.
    result['execution_status']='cancelled';result['completion_status']='cancelled'
    c.rollback();return self.finish(ident,result)
   c.execute('UPDATE studies SET status=?,result_json=?,artifacts_json=?,updated_at=? WHERE id=?',
    (result['execution_status'],json.dumps(result,ensure_ascii=False),json.dumps(artifacts),time.time(),ident))
 def artifact(self,ident,name):
  if name not in {'report.html','result.json','points.csv'}:raise KeyError(name)
  row=self.row(ident);meta=json.loads(row['artifacts_json'] or '{}').get(name)
  if row['status'] not in TERMINAL or meta is None:raise KeyError(name)
  p=self.root/ident/name
  if p.is_symlink() or p.parent.is_symlink():raise KeyError('linked_study_artifact')
  data=p.read_bytes()
  if len(data)!=meta['bytes'] or hashlib.sha256(data).hexdigest()!=meta['sha256']:raise KeyError('study_artifact_integrity')
  return data
 def loop(self):
  while not self.stop.is_set():
   with self.connect() as c:
    c.execute('BEGIN IMMEDIATE');row=c.execute("SELECT id FROM studies WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
    if row:c.execute("UPDATE studies SET status='running' WHERE id=?",(row[0],))
   if row:
    try:self.run(row[0])
    except Exception as e:
     r=self.get(row[0]);r.update(execution_status='failed',error=str(e));self.finish(row[0],r)
   else:self.wake.wait(.2);self.wake.clear()
 def close(self,timeout=20):
  self.stop.set();self.wake.set()
  if self.thread:
   self.thread.join(timeout)
   if self.thread.is_alive():raise TimeoutError('Study worker has not confirmed child shutdown')
