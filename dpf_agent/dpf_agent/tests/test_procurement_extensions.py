"""Deterministic contract and lifecycle checks; no native solver is started."""
from pathlib import Path
import copy,csv,io,json,os,tempfile,unittest
from dpf_agent.professional.tabular_input import adapt_tables
from dpf_agent.professional.models import ModelStore
from dpf_agent.professional.contracts import ContractError
from dpf_agent.professional.studies import validate_study
from dpf_agent.professional.scenario_comparison import compare_scenarios
from dpf_agent.professional.service import ProfessionalService
from dpf_agent.professional.api import create_app

def grid():
 return {'schema_version':'tianzhi-grid/1.0','frequency_hz':50,'voltage_bases_kv':[12.47],
  'buses':[{'id':'source','nodes':[1,2,3]},{'id':'b','nodes':[1,2,3]}],
  'objects':[
   {'kind':'circuit','id':'test','properties':{'bus1':'source.1.2.3','phases':3,'basekv':12.47,'pu':1,'r1':.01,'x1':.02,'r0':.01,'x0':.02}},
   {'kind':'line','id':'l','properties':{'bus1':'source.1.2.3','bus2':'b.1.2.3','phases':3,'length':1,'units':'km','r1':.1,'x1':.1,'r0':.3,'x0':.3,'c1':0,'c0':0}},
   {'kind':'load','id':'a','properties':{'bus1':'b.1.0','phases':1,'kv':12.47/(3**.5),'kw':10,'kvar':3,'conn':'wye','model':1}}]}
def payload(g=None):return {'format':'JSON','master_file':'grid.json','files':[{'path':'grid.json','content_utf8':json.dumps(g or grid())}]}
def base_request(snapshot='model-'+'0'*64):
 return {'schema_version':'1.1','request_id':'test-base','model':{'snapshot_id':snapshot},'scenario':{'scenario_id':'test','mode':'existing_snapshot'},
  'solver':{'engine':'OpenDSS','transport':'com','solve_mode':'snapshot','max_iterations':100,'tolerance':1e-9},
  'operating_conditions':{'frequency_hz':50,'source':'explicit unit fixture only','confirmed_by':'software test','confirmed_at':'2026-10-28','mode':'validate_model_operating_point'},
  'recovery':{'policy':'none','max_retries':0},'criteria':None,'result_scopes':['bus','branch','transformer','system'],'output_formats':['json','html','csv']}
def study(snapshot='model-'+'0'*64):return {'request_id':'test-series','mode':'quasistatic_fixed_controls','curve_source':'explicit unit fixture','base_request':base_request(snapshot),
 'points':[{'at':'2026-10-28T00:00:00+08:00','load_multiplier':.5},{'at':'2026-10-28T00:15:00+08:00','load_multiplier':1}],'on_point_error':'continue'}

class ExtensionTests(unittest.TestCase):
 def setUp(self):
  root=os.environ.get('TIANZHI_TEST_ROOT');Path(root).mkdir(parents=True,exist_ok=True) if root else None
  self.temp=tempfile.TemporaryDirectory(dir=root);self.root=Path(self.temp.name);self.store=ModelStore(self.root/'models')
 def tearDown(self):self.temp.cleanup()
 def reject_grid(self,change):
  g=grid();change(g)
  with self.assertRaises(ContractError):adapt_tables(payload(g))
 def test_original_bytes_and_phase_identity(self):
  p=payload();r=self.store.import_payload(p)
  self.assertEqual(r['source_format'],'JSON');self.assertFalse(r['balanced_equivalent']);self.assertTrue(r['conversion']['physical_phase_mapping'])
  folder,_=self.store.resolve(r['snapshot_id']);self.assertEqual((folder/'__tianshu_input__/original/grid.json').read_text('utf-8'),p['files'][0]['content_utf8'])
 def test_missing_frequency(self):self.reject_grid(lambda g:g.pop('frequency_hz'))
 def test_undeclared_phase(self):self.reject_grid(lambda g:g['objects'][-1]['properties'].update(bus1='b.4.0'))
 def test_bad_node_error_exact_object_and_field(self):
  g=grid();g['objects'][-1]['properties']['bus1']='b.4.0'
  with self.assertRaises(ContractError) as caught:adapt_tables(payload(g))
  self.assertEqual(caught.exception.field,'model.tables.objects[2].properties.bus1')
  self.assertIn('object=load.a; connection=b.4.0; declared_nodes=1,2,3',str(caught.exception))
 def test_unknown_bus_error_exact_connection(self):
  g=grid();g['objects'][-1]['properties']['bus1']='absent.1.0'
  with self.assertRaises(ContractError) as caught:adapt_tables(payload(g))
  self.assertIn('undeclared_bus; object=load.a; connection=absent.1.0',str(caught.exception))
 def test_duplicate_device_error_reports_both_positions(self):
  g=grid();g['objects'].append(copy.deepcopy(g['objects'][-1]))
  with self.assertRaises(ContractError) as caught:adapt_tables(payload(g))
  self.assertEqual(caught.exception.field,'model.tables.objects[3].id')
  self.assertIn('object=load.a; first_index=2; duplicate_index=3',str(caught.exception))
 def test_missing_power_property_reports_exact_device_and_key(self):
  g=grid();del g['objects'][-1]['properties']['kw']
  with self.assertRaises(ContractError) as caught:adapt_tables(payload(g))
  self.assertEqual(caught.exception.field,'model.tables.objects[2].properties.kw')
  self.assertIn('object=load.a; missing=kw',str(caught.exception))
 def test_missing_power_alternative_reports_group(self):
  g=grid();del g['objects'][-1]['properties']['kvar']
  with self.assertRaises(ContractError) as caught:adapt_tables(payload(g))
  self.assertIn('missing=kvar|pf',str(caught.exception))
 def test_duplicate_device_casefold(self):self.reject_grid(lambda g:g['objects'].append({**g['objects'][-1],'id':'A'}))
 def test_duplicate_bus_casefold(self):self.reject_grid(lambda g:g['buses'].append({'id':'B','nodes':[1]}))
 def test_duplicate_node(self):self.reject_grid(lambda g:g['buses'][0].update(nodes=[1,1]))
 def test_nan(self):self.reject_grid(lambda g:g.update(frequency_hz=float('nan')))
 def test_boolean_phase(self):self.reject_grid(lambda g:g['objects'][-1]['properties'].update(phases=True))
 def test_missing_line_units(self):self.reject_grid(lambda g:g['objects'][1]['properties'].pop('units'))
 def test_missing_power(self):self.reject_grid(lambda g:g['objects'][-1]['properties'].pop('kw'))
 def test_injected_command(self):self.reject_grid(lambda g:g['objects'][-1]['properties'].update(bus1='b.1\nSolve'))
 def test_duplicate_json_keys(self):
  p=payload();p['files'][0]['content_utf8']='{"schema_version":"tianzhi-grid/1.0","schema_version":"x"}'
  with self.assertRaises(ContractError):adapt_tables(p)
 def test_non_grid_json(self):
  p=payload();p['files'][0]['content_utf8']='[{"at":"2026-10-28"}]'
  with self.assertRaises(ContractError):adapt_tables(p)
 def test_tampered_frozen_native(self):
  r=self.store.import_payload(payload());folder,_=self.store.resolve(r['snapshot_id']);p=folder/'Master.dss';p.write_bytes(p.read_bytes()+b'\n! tampered\n')
  with self.assertRaises(ContractError):self.store.inspect(r['snapshot_id'])
 def test_island(self):
  g=grid();g['objects'].pop(1);r=self.store.import_payload(payload(g));self.assertEqual(r['topology']['isolated_buses'],['b']);self.assertEqual(r['topology']['status'],'islands_detected')
 def native_topology(self,extra):
  text=adapt_tables(payload())['native_payload']['files'][0]['content_utf8']+'\n'+extra
  return self.store.import_payload({'master_file':'Master.dss','files':[{'path':'Master.dss','content_utf8':text}]})['topology']
 def test_open_all(self):self.assertEqual(self.native_topology('Open Line.l term=1 cond=0')['status'],'islands_detected')
 def test_partial_phase_open(self):
  r=self.native_topology('Open Line.l term=1 cond=1');self.assertEqual(r['status'],'partial');self.assertEqual(r['partial_phase_open_devices'],['line.l'])
 def test_close_after_open(self):self.assertEqual(self.native_topology('Open Line.l term=1 cond=0\nClose Line.l term=1 cond=0')['status'],'connected')
 def test_disabled_source(self):self.assertEqual(self.native_topology('Edit Vsource.source enabled=no')['status'],'no_active_source')
 def test_disabled_only_buses(self):
  g=grid();g['objects'][1]['properties']['enabled']=False;g['objects'][-1]['properties']['enabled']=False
  r=self.store.import_payload(payload(g))['topology'];self.assertEqual(r['status'],'connected');self.assertEqual(r['inactive_or_disabled_only_buses'],['b'])
 def test_valid_curve(self):self.assertEqual(len(validate_study(study())[0]['points']),2)
 def reject_study(self,change):
  s=study();change(s)
  with self.assertRaises(ContractError):validate_study(s)
 def test_timezone_required(self):self.reject_study(lambda s:s['points'][0].update(at='2026-10-28T00:00:00'))
 def test_time_order(self):self.reject_study(lambda s:s['points'].reverse())
 def test_duplicate_timestamp(self):self.reject_study(lambda s:s['points'][1].update(at=s['points'][0]['at']))
 def test_empty_curve(self):self.reject_study(lambda s:s.update(points=[]))
 def test_negative_multiplier(self):self.reject_study(lambda s:s['points'][0].update(load_multiplier=-1))
 def test_boolean_multiplier(self):self.reject_study(lambda s:s['points'][0].update(load_multiplier=True))
 def test_nonfinite_curve(self):self.reject_study(lambda s:s['points'][0].update(load_multiplier=float('inf')))
 def test_curve_no_mutation(self):
  s=study();before=copy.deepcopy(s);validate_study(s);self.assertEqual(s,before)
 def test_unauthorized_setpoint_type(self):self.reject_study(lambda s:s['points'][0].update(setpoints=[{'object_id':'Line.l','properties':{'r1':0}}]))
 def test_topology_setpoint(self):self.reject_study(lambda s:s['points'][0].update(setpoints=[{'object_id':'Load.a','properties':{'bus1':1}}]))
 def test_pf_conflict(self):self.reject_study(lambda s:s['points'][0].update(setpoints=[{'object_id':'Load.a','properties':{'pf':1,'kvar':2}}]))
 def test_file_curve(self):
  s=study();s.pop('points');s['curve_file']={'name':'curve.csv','format':'CSV','content_utf8':'at,load_multiplier\n2026-10-28T00:00:00+08:00,0.5\n2026-10-28T00:15:00+08:00,1\n'}
  r,_=validate_study(s);self.assertEqual(len(r['points']),2);self.assertEqual(r['curve_file'],s['curve_file'])
 def test_curve_file_nan(self):
  s=study();s.pop('points');s['curve_file']={'name':'curve.csv','format':'CSV','content_utf8':'at,load_multiplier\n2026-10-28T00:00:00+08:00,NaN\n'}
  with self.assertRaises(ContractError):validate_study(s)
 def test_no_implicit_dynamic_mode(self):self.reject_study(lambda s:s.update(mode='dynamic'))
 def test_restart_does_not_replay_queued_study(self):
  with ProfessionalService(self.root/'service',start_worker=False) as s:
   m=s.import_model(payload());r=s.submit_study(study(m['snapshot_id']));ident=r['study_id'];self.assertEqual(r['execution_status'],'queued')
  with ProfessionalService(self.root/'service',start_worker=False) as s:
   r=s.get_study(ident);self.assertEqual(r['execution_status'],'interrupted');self.assertFalse(r['automatic_replay'])
   with s.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
 def test_capabilities_never_enumerate_or_inspect_snapshots(self):
  from unittest.mock import patch
  with ProfessionalService(self.root/'capability-health',start_worker=False) as s:
   with patch.object(s,'models',side_effect=AssertionError('capability must not list models')),patch.object(s.model_store,'inspect',side_effect=AssertionError('capability must not inspect models')):
    value=s.capabilities()
    self.assertEqual(value['model_catalog']['endpoint'],'/v1/models')
    self.assertFalse(value['model_catalog']['included'])
 def test_api_auth_on_new_endpoints(self):
  import asyncio
  async def call(app,path,method='GET',body=None,authorized=False):
   sent=[];first=True
   async def receive():
    nonlocal first
    if first:first=False;return {'type':'http.request','body':json.dumps(body).encode() if body is not None else b'','more_body':False}
    await asyncio.Event().wait()
   async def send(message):sent.append(message)
   headers=[(b'host',b'127.0.0.1'),(b'content-type',b'application/json')]
   if authorized:headers.append((b'authorization',b'Bearer unit-test-token-not-a-secret-123456'))
   await app({'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1','method':method,'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'','headers':headers,'client':('127.0.0.1',12345),'server':('127.0.0.1',80),'root_path':''},receive,send)
   return next(x['status'] for x in sent if x['type']=='http.response.start')
  with ProfessionalService(self.root/'api',start_worker=False) as s:
   app=create_app(s,'unit-test-token-not-a-secret-123456')
   for path in ['/v1/health','/v1/studies/missing','/v1/jobs/missing/report.pdf']:
    self.assertEqual(asyncio.run(call(app,path)),401)
   self.assertEqual(asyncio.run(call(app,'/v1/studies','POST',study())),401)
   self.assertEqual(asyncio.run(call(app,'/v1/studies','POST',{'mode':'dynamic'},True)),422)
 def test_restart_adopts_only_existing_child_without_replay(self):
  state=self.root/'crash-window'
  with ProfessionalService(state,start_worker=False) as s:
   m=s.import_model(payload());parent=s.submit_study(study(m['snapshot_id']));ident=parent['study_id']
   child=base_request(m['snapshot_id']);child['request_id']=ident+'-p0';created=s.submit(child)
  with ProfessionalService(state,start_worker=False) as s:
   restored=s.get_study(ident);self.assertEqual(restored['execution_status'],'interrupted')
   self.assertEqual(restored['points'][0]['task_id'],created['task_id'])
   self.assertEqual(restored['points'][0]['execution_status'],'interrupted')
   self.assertEqual(restored['points'][1]['execution_status'],'not_run')
   with s.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
 def result(self,task,value,base=230):return {'task_id':task,'request_digest':task+'-digest','publishable':True,'numerical_validity':'converged_valid','input':{'fingerprint':task+'-input'},'engineering_analysis':{'metrics':[{'metric':'project_voltage_pu','object_id':'b','node':1,'reference_node':0,'unit':'pu','measurement_basis':'node_to_ground','base_voltage_v':base,'status':'available','value':value}]}}
 def test_comparison_delta(self):
  r=compare_scenarios(self.result('a',1),self.result('b',.98));self.assertAlmostEqual(r['rows'][0]['right_minus_left'],-.02);self.assertFalse(r['recomputed'])
 def test_comparison_different_base(self):
  r=compare_scenarios(self.result('a',1),self.result('b',1,400));self.assertEqual(r['compared_count'],0);self.assertTrue(all(x['right_minus_left'] is None for x in r['rows']))
 def test_comparison_invalid_result(self):
  r=self.result('a',1);r['publishable']=False
  with self.assertRaises(ContractError):compare_scenarios(r,self.result('b',1))
 def test_comparison_different_ratings(self):
  a=self.result('a',50);b=self.result('b',60)
  for r,rating in [(a,100),(b,200)]:r['engineering_analysis']['metrics'][0].update(metric='line_current_loading_percent',unit='percent',rating=rating,rating_unit='A',criteria_rule_id='rating')
  self.assertEqual(compare_scenarios(a,b)['compared_count'],0)
 def test_comparison_duplicate_identity(self):
  r=self.result('a',1);r['engineering_analysis']['metrics']*=2
  with self.assertRaises(ContractError):compare_scenarios(r,self.result('b',1))
if __name__=='__main__':unittest.main()
