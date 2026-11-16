"""Read-only, identity/unit/basis-aware comparison of committed scenarios."""
from __future__ import annotations
import math,json
from .contracts import ContractError

SELECTORS=['metric','object_id','node','reference_node','terminal','conductor','unit','measurement_basis','base_voltage_v','rating','rating_unit','criteria_rule_id']
def compare_scenarios(left,right):
 for r in [left,right]:
  if not isinstance(r,dict) or r.get('publishable') is not True or r.get('numerical_validity')!='converged_valid':raise ContractError('comparison','only verified publishable numerical results may be compared')
 if bool(left['input'].get('balanced_equivalent'))!=bool(right['input'].get('balanced_equivalent')):raise ContractError('comparison','physical phase data cannot be compared to synthetic balanced phase labels')
 def inventory(r):
  out={}
  for m in r.get('engineering_analysis',{}).get('metrics',[]):
   key=json.dumps([m.get(k) for k in SELECTORS],sort_keys=True,separators=(',',':'))
   if key in out:raise ContractError('comparison','ambiguous repeated engineering metric selector')
   out[key]=m
  return out
 a,b=inventory(left),inventory(right);rows=[]
 for key in sorted(a.keys()|b.keys()):
  x,y=a.get(key),b.get(key);m=x or y;v1=x.get('value') if x else None;v2=y.get('value') if y else None
  valid=bool(x and y and x.get('status')==y.get('status')=='available' and type(v1) in {int,float} and type(v2) in {int,float} and math.isfinite(v1) and math.isfinite(v2))
  delta=v2-v1 if valid else None
  if delta is not None and not math.isfinite(delta):valid=False;delta=None
  rows.append({**{k:m.get(k) for k in SELECTORS},'left_value':v1,'right_value':v2,'right_minus_left':delta,
   'status':'compared' if valid else 'not_comparable','reason':None if valid else 'missing_unavailable_or_incompatible_metric_basis'})
 return {'schema_version':'1.0','left_task_id':left['task_id'],'right_task_id':right['task_id'],
  'left_input_fingerprint':left['input']['fingerprint'],'right_input_fingerprint':right['input']['fingerprint'],
  'left_request_digest':left['request_digest'],'right_request_digest':right['request_digest'],
  'rows':rows,'compared_count':sum(r['status']=='compared' for r in rows),'not_comparable_count':sum(r['status']!='compared' for r in rows),
  'recomputed':False,'engineering_accepted':False,'delta_definition':'right minus left; matching exact object/node/terminal/unit/basis/base selectors only; missing values are not zero'}
