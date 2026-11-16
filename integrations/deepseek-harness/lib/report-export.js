/** Native Harness document delivery, without embedded-browser downloads.
 * Copyright (c) yangshaopeng. Export only already authorized verified bytes.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';

const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
const escape=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const allowed=new Set(['report.html','report.pdf','result.json','points.csv','checks.csv','observations.json','observations.csv','engineering.csv']);
const normalize=p=>process.platform==='win32'?p.toLowerCase():p;
async function directory(root,parts){
 const canonical=await fs.realpath(root);let current=canonical;
 for(const part of parts){
  if(!part||part==='.'||part==='..'||/[\\/:\x00-\x1f]/.test(part))throw Error('invalid report export directory');
  current=path.join(current,part);await fs.mkdir(current,{recursive:true});
  const info=await fs.lstat(current);
  if(!info.isDirectory()||info.isSymbolicLink()||normalize(await fs.realpath(current))!==normalize(current))throw Error('report export directory cannot be linked');
 }
 return current;
}
async function publishFile(filename,bytes){
 try{
  const info=await fs.lstat(filename);
  if(!info.isFile()||info.isSymbolicLink()||info.nlink!==1||info.size!==bytes.length||hash(await fs.readFile(filename))!==hash(bytes))throw Error('existing report export differs; preserved without overwrite');
  return;
 }catch(e){if(e.code!=='ENOENT')throw e;}
 const temporary=path.join(path.dirname(filename),'.'+randomUUID()+'.tmp');
 await fs.writeFile(temporary,bytes,{flag:'wx'});
 try{await fs.link(temporary,filename);}catch(e){if(e.code!=='EEXIST')throw e;await publishFile(filename,bytes);}finally{await fs.unlink(temporary);}
}
/** Official dsh-resource Session file address; no raw browser download. */
export function sessionFileAddress(sessionId,relative){
 const encode=s=>encodeURIComponent(s).replace(/%3A/gi,':');
 return 'dsh-resource://file/session/'+encode(sessionId)+'/'+relative.split('/').map(encode).join('/');
}
const pending=new Map();
export async function exportNativeReport(scope,published,bridge,options={}){
 const key=JSON.stringify([scope.workspaceRoot,scope.workspaceId,scope.sessionId,published.study_id??published.task_id]);
 const operation=(pending.get(key)??Promise.resolve()).catch(()=>{}).then(()=>materialize(scope,published,bridge,options));
 pending.set(key,operation);
 try{return await operation;}finally{if(pending.get(key)===operation)pending.delete(key);}
}
async function materialize(scope,published,bridge,{study=false,format='html'}={}){
 if(!scope.workspaceRoot||!path.isAbsolute(scope.workspaceRoot)||!scope.sessionId)throw Error('native report requires the actual Session workspace');
 const id=study?published.study_id:published.task_id;
 if(!/^(?:study-[a-f0-9]{32}|pf-[A-Za-z0-9-]+)$/.test(id??''))throw Error('invalid report identity');
 const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
 const parts=['Codex工作实践',day+'_天智五号报告',hash(JSON.stringify([scope.workspaceId,scope.sessionId])).slice(0,16),id,'delivery-v1'];
 const folder=await directory(scope.workspaceRoot,parts),files=[];
 const status=study?await bridge.studyStatus(scope,id):await bridge.status(scope,id);
 const names=format==='pdf'?['report.pdf']:status.artifacts;
 for(const name of names){
  if(!allowed.has(name))throw Error('unsupported report export file');
  const item=name==='report.pdf'?await bridge.pdf(scope,id,{study}):study?await bridge.studyArtifact(scope,id,name):await bridge.artifact(scope,id,name);
  if(!Buffer.isBuffer(item.bytes)||hash(item.bytes)!==item.sha256)throw Error('report export integrity failed');
  let bytes=item.bytes;
  if(study&&name==='report.html'&&status.request_context){
   const supplement='<section><h2>输入与计算设置</h2><p>下列设置只读投影自本父任务已保存的原请求；未重算或改写原报告。</p><pre>'+escape(JSON.stringify(status.request_context,null,2))+'</pre></section>';
   bytes=Buffer.concat([bytes,Buffer.from(supplement)]);
  }
  const destination=path.join(folder,name);await publishFile(destination,bytes);
  const label=name==='report.html'?(study?'打开时序报告':'打开潮流报告'):name==='report.pdf'?(study?'打开时序PDF':'打开潮流PDF'):
   ({'points.csv':'查看逐时结果（CSV）','result.json':'查看完整计算结果（JSON）','checks.csv':'查看校核明细（CSV）','observations.json':'查看原始观测（JSON）','observations.csv':'查看原始观测（CSV）','engineering.csv':'查看工程指标（CSV）'})[name];
  const relative_path=[...parts,name].join('/');
  files.push({name,label,markdown_link:'['+label+'](<'+relative_path+'>)',relative_path,bytes:bytes.length,sha256:hash(bytes),source_sha256:item.sha256});
 }
 const selected=files.find(f=>f.name==='report.'+format);if(!selected)throw Error('requested native report missing');
 return {...published,http_url:published.url,url:selected.relative_path,
  label:selected.label,markdown_link:selected.markdown_link,
  native_preview_address:sessionFileAddress(scope.sessionId,selected.relative_path),
  delivery:'Harness native Session document preview; exported files follow workspace file permissions',
  workspace_export_files:files,recomputed:false};
}
