/** Authorized, bounded file-reference closure. Copyright (c) yangshaopeng.
 * Reads only the selected package and literal references, never directories.
 */
import { parseModelDependencies } from './model-dependencies.js';

const MAX_FILES=500, MAX_BYTES=20*1024*1024;
const fold=value=>value.toLowerCase();
const fail=(code,message)=>{throw Object.assign(new Error(message),{code});};
const PRIVATE_PARTS=new Set(['.git','.dsh','.codex','.ssh','.aws','.azure','.venv','node_modules']);
export function assertDependencyPackageRoot(root) {
  if(root.replaceAll('\\','/').toLowerCase().split('/').some(part=>PRIVATE_PARTS.has(part))) {
    fail('private_dependency_denied','所选模型目录位于配置或依赖环境目录内，未自动读取；请提供独立的算例目录。');
  }
}
function assertDataReference(target) {
  const parts=target.toLowerCase().split('/'),name=parts.at(-1);
  if(parts.some(part=>PRIVATE_PARTS.has(part))||/^\.env(?:\.|$)|^(?:id_rsa|id_ed25519|authorized_keys|known_hosts)$/.test(name)
      || /(?:credential|secret|access[_-]?token|api[_-]?key)/.test(name)||/\.(?:pem|key|pfx|p12)$/.test(name)) {
    fail('private_dependency_denied',`模型引用${target}涉及配置或凭据文件，未自动读取；请核对算例依赖。`);
  }
}

export async function collectReferencedPackage(args,{readFiles,checkFiles,signal,includeDependencies=false}) {
  const entries=new Map(),files=new Map(),order=[],edges=[],processed=new Set();
  let bytes=0;
  async function read(names) {
    if(!names.length)return;
    signal.throwIfAborted();
    if(includeDependencies)names.forEach(assertDataReference);
    if(order.length+names.length>MAX_FILES)fail('model_package_budget','入口及引用文件合计最多500个。');
    if(bytes>=MAX_BYTES)fail('model_package_budget','入口及引用文件合计最多20 MiB。');
    await checkFiles(names);
    const selected=await readFiles(names,MAX_BYTES-bytes);
    if(selected.inventory.length!==names.length||selected.payload.files.length!==names.length)fail('model_package_identity','文件读取清单与请求不一致，未导入。');
    for(let i=0;i<names.length;i++) {
      const name=names[i],row=selected.inventory[i],file=selected.payload.files[i],key=fold(name);
      if(row.path!==name||file.path!==name||entries.has(key))fail('model_package_identity','引用文件存在重复或大小写别名，未导入。');
      bytes+=row.bytes;
      if(bytes>MAX_BYTES)fail('model_package_budget','入口及引用文件合计最多20 MiB。');
      entries.set(key,row);files.set(key,file);order.push(name);
    }
  }
  await read(args.files);
  const queue=[args.master_file];
  while(includeDependencies&&queue.length) {
    signal.throwIfAborted();
    const requested=queue.shift(),key=fold(requested);
    if(processed.has(key))continue;
    const file=files.get(key);
    if(!file)fail('model_package_identity','入口引用脚本未读取，未导入。');
    processed.add(key);
    const parsed=parseModelDependencies({source_file:file.path,master_file:args.master_file,content_utf8:file.content_utf8});
    if(parsed.status!=='supported') {
      const issue=parsed.issues[0];
      fail('model_dependency_unsupported',`无法安全整理模型引用：${issue?.message??parsed.status}（${file.path}${issue?.line?':'+issue.line:''}）。未读取未确认的引用，也未计算。`);
    }
    const missing=[];
    for(const edge of parsed.dependencies) {
      assertDataReference(edge.target);
      edges.push(edge);
      if(!entries.has(fold(edge.target))&&!missing.some(name=>fold(name)===fold(edge.target)))missing.push(edge.target);
    }
    await read(missing);
    for(const edge of parsed.dependencies)if(edge.kind==='script')queue.push(entries.get(fold(edge.target)).path);
  }
  // Re-read the finite resolved manifest once to reject changes while the
  // references were being discovered. The returned bytes are the final copy.
  await checkFiles(order);
  const final=await readFiles(order,MAX_BYTES);
  if(final.inventory.length!==order.length||final.payload.files.length!==order.length)fail('input_changed','整理模型引用时文件清单发生变化。');
  for(let i=0;i<order.length;i++) {
    const before=entries.get(fold(order[i])),after=final.inventory[i];
    if(before.path!==after.path||before.bytes!==after.bytes||before.sha256!==after.sha256)fail('input_changed',`整理模型引用时文件发生变化：${order[i]}。请固定文件后再试。`);
  }
  const explicit=new Set(args.files.map(fold));
  const discovered=order.filter(name=>!explicit.has(fold(name)));
  return {
    payload:{...final.payload,master_file:args.master_file},inventory:final.inventory,
    privateAudit:{source:'workspace',policy:includeDependencies?'selected_entry_literal_reference_closure':'explicit_file_list',
      package_root:args.package_root,master_file:args.master_file,explicit_files:[...args.files],resolved_files:order,dependency_edges:edges,
      boundary:'selected_package_root_only_no_directory_scan_no_model_edits'},
    dependencyResolution:{policy:includeDependencies?'literal_same_package_references':'explicit_file_list',
      explicit_file_count:args.files.length,resolved_file_count:order.length,discovered_file_count:discovered.length,
      discovered_files:discovered.slice(0,20),discovered_files_truncated:discovered.length>20,
      no_directory_scan:true,source_bytes_unchanged:true},
  };
}
