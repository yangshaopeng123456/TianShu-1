"""Read-only PDF derivation of a committed, verified HTML report.

Copyright (c) yangshaopeng. Uses an installed headless Chromium/Edge renderer;
does not recalculate, change historical artifacts or call a model service.
"""
from __future__ import annotations
from pathlib import Path
import os,shutil,tempfile,subprocess,json,hashlib,re,uuid,html as html_escape
from .contracts import ContractError

def renderer_path():
 candidates=[]
 if os.name=='nt':
  for key in ['PROGRAMFILES(X86)','PROGRAMFILES','LOCALAPPDATA']:
   base=os.environ.get(key)
   if base:candidates.append(Path(base)/'Microsoft/Edge/Application/msedge.exe')
 else:
  candidates.extend(Path(p) for n in ['chromium','chromium-browser','google-chrome'] if (p:=shutil.which(n)))
 return next((str(p) for p in candidates if p.is_file()),None)

def export_pdf(service,task_id,*,study=False):
 from .service import atomic_text
 result=service.get_study(task_id) if study else service.result(task_id)
 if result is None:raise ContractError('task_id','PDF requires a committed task')
 source=service.studies.artifact(task_id,'report.html') if study else service.artifact_bytes(task_id,'report.html')
 source_sha=hashlib.sha256(source).hexdigest()
 context=result.get('request_context') if study else None
 supplement=('<section><h2>输入与计算设置</h2><p>只读原请求投影，未重新计算。</p><pre>'+html_escape.escape(json.dumps(context,ensure_ascii=False,indent=2))+'</pre></section>').encode('utf-8') if context else b''
 projection_sha=hashlib.sha256(source+supplement).hexdigest()
 root=service.root/'derived-reports'/task_id/'print-v1.3'
 root.mkdir(parents=True,exist_ok=True)
 if root.is_symlink() or getattr(root,'is_junction',lambda:False)():raise ContractError('report','linked PDF output directory')
 pdf=root/'report.pdf';receipt=root/'pdf.json'
 if receipt.exists():
  info=json.loads(receipt.read_text('utf-8'));data=pdf.read_bytes()
  if (info.get('task_id')!=task_id or info.get('source_html_sha256')!=source_sha or info.get('projection_sha256')!=projection_sha or info.get('sha256')!=hashlib.sha256(data).hexdigest() or not data.startswith(b'%PDF-')):
   raise ContractError('report','derived PDF integrity mismatch','model_integrity_error')
  return data
 executable=renderer_path()
 if not executable:raise ContractError('report','headless Chromium/Edge renderer is not installed','capability_unavailable')
 if len(source)>8*1024*1024:raise ContractError('report','HTML exceeds the 8 MiB PDF rendering budget','capability_unavailable')
 with tempfile.TemporaryDirectory(prefix='pdf-',dir=root) as tmp:
  tmp=Path(tmp);html=tmp/'report.html'
  print_style=b'<style>@page{size:A4 landscape;margin:12mm}@media print{body{font-size:10pt!important;line-height:1.35!important;margin:0!important;max-width:none!important}table{font-size:9pt!important;width:100%!important;min-width:0!important}th,td{padding:5px!important;overflow-wrap:anywhere}thead{display:table-header-group}tr,section{break-inside:avoid}h1,h2,h3{break-after:avoid}}</style>'
  # Print-only projection; committed HTML bytes and result hashes stay intact.
  # PDF is self-contained; sibling artifact links belong to the HTML bundle.
  projection=re.sub(rb'<a\b[^>]*>(.*?)</a>',rb'\1',source+supplement,flags=re.I|re.S)
  html.write_bytes(projection+print_style);target=tmp/'rendered.pdf'
  args=[executable,'--headless','--disable-gpu','--no-first-run','--no-default-browser-check',
   '--user-data-dir='+str(tmp/'browser'),'--print-to-pdf='+str(target),'--no-pdf-header-footer',html.as_uri()]
  try:p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90,check=False)
  except (OSError,subprocess.TimeoutExpired):raise ContractError('report','PDF renderer failed or timed out; no numerical recomputation','capability_unavailable') from None
  if p.returncode or not target.is_file():raise ContractError('report','PDF renderer did not produce a document','capability_unavailable')
  data=target.read_bytes()
  if not data.startswith(b'%PDF-') or not 100<len(data)<=64*1024*1024:raise ContractError('report','invalid or oversized PDF output')
  # Publish only fully rendered bytes, then bind them to the exact HTML source.
  temporary=root/('report-'+uuid.uuid4().hex+'.pdf.tmp')
  with temporary.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
  os.replace(temporary,pdf)
  atomic_text(receipt,json.dumps({'schema_version':'1.0','task_id':task_id,'request_digest':result['request_digest'],
   'source_html_sha256':source_sha,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),
   'renderer':'installed_headless_chromium','print_projection':'landscape-v1.3','projection_sha256':projection_sha,'recalculated':False},ensure_ascii=False,indent=2))
  return data
