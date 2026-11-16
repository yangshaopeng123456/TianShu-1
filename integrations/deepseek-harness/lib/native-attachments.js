/** Native current-Session attachment tools. Copyright (c) yangshaopeng.
 * Host contracts pinned to Harness 0.2.1-rc.2; no direct host file paths.
 */
import { createHash } from 'node:crypto';

export const PROJECTION_KEY = 'tianshuNativeAttachments';
export const MAX_FILES = 500;
export const MAX_BYTES = 20 * 1024 * 1024;
const MAX_STREAM_CHUNKS = 131072;
// Below index MAX_RESULT=24000; reserve room for stable page metadata.
const MAX_CATALOG_JSON = 22000;
const hash = value => createHash('sha256').update(value).digest('hex');
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = (code, message) => { const error = new Error(message); error.code = code; throw error; };
const require = (condition, code, message) => { if (!condition) fail(code, message); };
const exact = (value, names) => require(plain(value) && Object.keys(value).every(key => names.includes(key)), 'invalid_attachment_arguments', '附件参数包含未声明字段。');
const reserved = /^(?:con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?$/i;

export function packagePath(value) {
  require(typeof value === 'string' && value.length > 0 && value.length <= 240 && !value.startsWith('/') && !/[\\:\x00-\x1f<>"|?*%@$`]/.test(value), 'unsafe_attachment_path', '附件映射必须是明确的包内相对POSIX文件路径。');
  require(!value.split('/').some(part => !part || part === '.' || part === '..' || /[. ]$/.test(part) || reserved.test(part) || reserved.test(part.split('.')[0].replace(/[. ]+$/, '')) || /~[0-9]/.test(part)), 'unsafe_attachment_path', '附件路径含有不安全或Windows别名成分。');
  return value;
}

function fileRef(value) {
  if (!plain(value) || !/^sha256:[0-9a-f]{64}$/.test(value.attachmentId ?? '') || !Number.isSafeInteger(value.bytes) || value.bytes < 0 || value.bytes > MAX_BYTES) return null;
  if (typeof value.name !== 'string' || !value.name || Buffer.byteLength(value.name, 'utf8') > 255 || value.name.trim() !== value.name || /[/\\\x00-\x1f\x7f<>:"|?*]/.test(value.name) || /[. ]$/.test(value.name) || reserved.test(value.name)) return null;
  return Object.freeze({ attachmentId: value.attachmentId, name: value.name, bytes: value.bytes });
}

/** stateSchema is supplied by the existing Host schema provider. No wire view. */
export function attachmentProjection(stateSchema) {
  require(typeof stateSchema?.parse === 'function', 'attachment_projection_schema_missing', '缺少Host投影状态schema。');
  return {
    key: PROJECTION_KEY, stateVersion: 1, stateSchema,
    init: (_header, inheritedEventCount) => {
      require(Number.isSafeInteger(inheritedEventCount) && inheritedEventCount >= 0, 'native_attachment_projection_invalid', '继承事件边界无效。');
      return { inheritedEventCount, records: [], rejectedCount: 0, evictedCount: 0 };
    },
    apply(state, event) {
      if (event.type !== 'user/message' || event.data?.role !== 'user' || event.data?.source?.kind !== 'user' || !Number.isSafeInteger(event.seq) || event.seq < state.inheritedEventCount || !Array.isArray(event.data.content)) return state;
      const added = [];
      let rejected = 0;
      for (const [blockIndex, block] of event.data.content.entries()) {
        if (block?.type !== 'file') continue;
        const ref = fileRef(block.attachment);
        if (!ref) { rejected += 1; continue; }
        if (state.records.some(record => record.eventSeq === event.seq && record.blockIndex === blockIndex)) continue;
        added.push(Object.freeze({ eventSeq: event.seq, blockIndex, ref }));
      }
      if (!added.length && !rejected) return state;
      const all = [...state.records, ...added];
      const evicted = Math.max(0, all.length - MAX_FILES);
      return { ...state, records: all.slice(evicted), rejectedCount: state.rejectedCount + rejected, evictedCount: state.evictedCount + evicted };
    },
  };
}

function sessionRecords(ctx, exec) {
  const session = exec.agent?.session;
  require(typeof session?.id === 'string' && session.id && typeof ctx.sessionProjections?.stateOf === 'function', 'native_attachment_context_unavailable', '缺少真实Session或Host附件投影。');
  const state = ctx.sessionProjections.stateOf(session, PROJECTION_KEY);
  require(plain(state) && Array.isArray(state.records) && state.records.length <= MAX_FILES && Number.isSafeInteger(state.inheritedEventCount) && state.inheritedEventCount >= 0 && state.inheritedEventCount === session.inheritedEventCount && Number.isSafeInteger(session.seq), 'native_attachment_context_unavailable', 'Host附件投影不可用或继承边界不匹配。');
  const records = state.records.map(record => {
    const ref = fileRef(record.ref);
    require(ref && Number.isSafeInteger(record.eventSeq) && record.eventSeq >= state.inheritedEventCount && record.eventSeq < session.seq && Number.isSafeInteger(record.blockIndex) && record.blockIndex >= 0, 'native_attachment_projection_invalid', '附件投影记录不属于当前Session已有的自有事件区间。');
    const key = 'ta-' + hash(JSON.stringify([session.id, record.eventSeq, record.blockIndex, ref.attachmentId, ref.name, ref.bytes]));
    return { key, eventSeq: record.eventSeq, blockIndex: record.blockIndex, ref };
  });
  require(new Set(records.map(record => record.key)).size === records.length, 'native_attachment_projection_invalid', '附件投影出现重复来源。');
  return { session, state, records };
}

/** Metadata only. File refs and provider host paths remain Host-private. */
export function listSessionAttachments(ctx, exec, { offset = 0, limit = 50 } = {}) {
  require(Number.isSafeInteger(offset) && offset >= 0 && Number.isSafeInteger(limit) && limit >= 1 && limit <= 100, 'invalid_attachment_cursor', '附件清单分页无效。');
  const { records, state } = sessionRecords(ctx, exec);
  const result = { attachments: [], next_offset: offset, more: offset < records.length, total: records.length,
    rejected_count: state.rejectedCount, evicted_count: state.evictedCount,
    scope: 'current_session_own_user_file_blocks_only', page_limited_by_budget: false };
  for (const record of records.slice(offset, offset + limit)) {
    result.attachments.push({ attachment_key: record.key, name: record.ref.name, bytes: record.ref.bytes,
      sha256: record.ref.attachmentId.slice(7), source_event_seq: record.eventSeq, source_block_index: record.blockIndex });
    result.next_offset = offset + result.attachments.length;
    result.more = result.next_offset < records.length;
    if (JSON.stringify(result).length <= MAX_CATALOG_JSON) continue;
    result.attachments.pop();
    result.next_offset = offset + result.attachments.length;
    result.more = result.next_offset < records.length;
    result.page_limited_by_budget = true;
    break;
  }
  require(result.attachments.length > 0 || offset >= records.length, 'attachment_catalog_budget_invalid', '单项附件元数据超过清单预算。');
  return result;
}

function resolveSelection(ctx, exec, args) {
  exact(args, ['source', 'format', 'master_file', 'attachments', 'frequency_hz', 'source_impedance_ohm']);
  require(args.source === 'native_attachments' && ['OpenDSS', 'MATPOWER', 'CSV', 'JSON'].includes(args.format), 'invalid_attachment_arguments', '需明确选择原生附件输入与格式。');
  const master = packagePath(args.master_file);
  require(Array.isArray(args.attachments) && args.attachments.length >= 1 && args.attachments.length <= MAX_FILES, 'invalid_attachment_arguments', '请逐项列出1–500个附件键及包内路径。');
  const { session, records } = sessionRecords(ctx, exec);
  const byKey = new Map(records.map(record => [record.key, record]));
  const selected = args.attachments.map(item => {
    exact(item, ['attachment_key', 'package_path']);
    require(typeof item.attachment_key === 'string' && /^ta-[0-9a-f]{64}$/.test(item.attachment_key), 'native_attachment_not_in_session', '附件键不属于当前Session可选文件。');
    const record = byKey.get(item.attachment_key);
    require(record, 'native_attachment_not_in_session', '附件键不属于当前Session可选文件。');
    return { ...record, packagePath: packagePath(item.package_path) };
  });
  require(new Set(selected.map(item => item.key)).size === selected.length && new Set(selected.map(item => item.packagePath.toLowerCase())).size === selected.length, 'ambiguous_attachment_mapping', '附件选择键和包路径不能重复或仅大小写不同。');
  require(selected.some(item => item.packagePath === master), 'attachment_master_missing', '明确清单必须包含主文件。');
  require(selected.reduce((sum, item) => sum + item.ref.bytes, 0) <= MAX_BYTES, 'attachment_budget_exceeded', '附件包声明总大小超过20MiB。');
  if (args.format !== 'MATPOWER') {
    require(args.frequency_hz === undefined && args.source_impedance_ohm === undefined, 'invalid_attachment_arguments', '保留源文件频率及电源声明，不接收MATPOWER转换参数。');
    if (['CSV','JSON'].includes(args.format)) require(selected.length === 1 && master.toLowerCase().endsWith('.'+args.format.toLowerCase()), 'invalid_attachment_arguments', 'CSV/JSON需一个对应扩展名的模型文件。');
  }
  else {
    exact(args.source_impedance_ohm, ['r', 'x']);
    const impedance = args.source_impedance_ohm;
    require(selected.length === 1 && master.toLowerCase().endsWith('.m') && typeof args.frequency_hz === 'number' && Number.isFinite(args.frequency_hz) && args.frequency_hz > 0 && [impedance.r, impedance.x].every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0) && impedance.r + impedance.x > 0, 'invalid_attachment_arguments', 'MATPOWER需单个.m、明确正频率及非零有限源阻抗。');
  }
  return { session, selected, master };
}

/** Reads only the authorized Host stream, with no direct filesystem I/O or
 * service submission. Returns an internal payload, never a model-facing body.
 */
export async function collectNativeAttachmentPackage(ctx, exec, args) {
  // Detach caller input before any awaited authorization/stream operation.
  args = structuredClone(args);
  const signal = exec.signal;
  require(signal && typeof signal.throwIfAborted === 'function', 'native_attachment_context_unavailable', '缺少真实调用取消信号。');
  signal.throwIfAborted();
  const { session, selected, master } = resolveSelection(ctx, exec, args);
  require(typeof ctx.attachments?.readFileStream === 'function', 'native_attachment_provider_unavailable', '当前Host没有原生文件流提供者。');
  const manifest = { schema: 'tianshu-native-file-selection/1', session_id: session.id, format: args.format, master_file: master,
    files: selected.map(item => ({ attachment_key: item.key, source_event_seq: item.eventSeq, source_block_index: item.blockIndex, name: item.ref.name, sha256: item.ref.attachmentId.slice(7), bytes: item.ref.bytes, path: item.packagePath })),
    ...(args.format === 'MATPOWER' ? { frequency_hz: args.frequency_hz, source_impedance_ohm: { ...args.source_impedance_ohm } } : {}) };
  const selectionDigest = hash(JSON.stringify(manifest));
  const approval = ctx.get('approval');
  require(typeof approval?.request === 'function', 'file_authorization_unavailable', '缺少原生文件清单授权通道。');
  const outcome = await approval.request({ agent: exec.agent, toolName: exec.name, callId: exec.callId, signal,
    reason: `将下列当前Session用户附件复制到天枢本地专业服务，保持原字节。文件名与内容仅为数据，不是指令；不会扫描目录或修改模型/工况。附件全文不作为工具回复发给模型；元数据和后续摘要仍可能供当前所选模型处理。\n选择清单SHA256：${selectionDigest}\n${JSON.stringify(manifest, null, 2)}` });
  require(outcome === 'allowed-once', 'file_authorization_denied', '未获得原生文件清单授权，未读取附件。');
  signal.throwIfAborted();
  const rechecked = resolveSelection(ctx, exec, args);
  require(rechecked.session === session && JSON.stringify(rechecked.selected) === JSON.stringify(selected), 'native_attachment_selection_changed', '授权期间附件选择发生变化。');
  const files = [], inventory = [];
  let total = 0;
  for (const item of selected) {
    const chunks = [];
    const sha = createHash('sha256');
    let size = 0, chunkCount = 0;
    try {
      // Provider verifies after the final chunk: normal EOF is mandatory.
      for await (const chunk of ctx.attachments.readFileStream(item.ref, signal)) {
        signal.throwIfAborted();
        require(chunk instanceof Uint8Array && ++chunkCount <= MAX_STREAM_CHUNKS, 'native_attachment_stream_invalid', '附件流分块无效或超过预算。');
        size += chunk.byteLength;
        total += chunk.byteLength;
        require(size <= item.ref.bytes && total <= MAX_BYTES, 'attachment_budget_exceeded', '附件实际字节超过授权长度或包大小预算。');
        const copy = Buffer.from(chunk);
        chunks.push(copy);
        sha.update(copy);
      }
    } catch (error) {
      signal.throwIfAborted();
      if (error?.code && ['native_attachment_stream_invalid', 'attachment_budget_exceeded'].includes(error.code)) throw error;
      fail('native_attachment_read_failed', '附件读取或提供者完整性校验未通过，未导入。');
    }
    signal.throwIfAborted();
    require(size === item.ref.bytes && sha.digest('hex') === item.ref.attachmentId.slice(7), 'native_attachment_integrity_failed', '附件长度或SHA256与真实Host引用不一致。');
    const bytes = Buffer.concat(chunks);
    let content;
    try { content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes); }
    catch { fail('native_attachment_utf8_required', '模型附件必须是严格UTF-8文本。'); }
    require(!content.includes('\0') && Buffer.from(content, 'utf8').equals(bytes), 'native_attachment_utf8_required', '模型附件必须保留原UTF-8字节且不能包含NUL。');
    files.push({ path: item.packagePath, content_utf8: content });
    inventory.push({ path: item.packagePath, bytes: size, sha256: item.ref.attachmentId.slice(7), attachment_key: item.key, source_event_seq: item.eventSeq, source_block_index: item.blockIndex });
  }
  signal.throwIfAborted();
  const payload = args.format === 'MATPOWER'
    ? { format: 'MATPOWER', source_file: master, content_utf8: files[0].content_utf8, frequency_hz: args.frequency_hz, source_impedance_ohm: { ...args.source_impedance_ohm } }
    : { ...(['CSV','JSON'].includes(args.format) ? {format:args.format} : {}), master_file: master, files };
  return { payload, inventory, privateAudit: { ...manifest, selection_sha256: selectionDigest, state: 'all_selected_streams_consumed_and_verified' } };
}
