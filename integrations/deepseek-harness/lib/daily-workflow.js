/** Daily input preparation; private Host records, never a second solver.
 * Copyright (c) yangshaopeng.
 */
import fs from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { summarizeModel } from './model-summary.js';
import { prepareDailyRequest } from './daily-request.js';

const hash = value => createHash('sha256').update(value).digest('hex');
const queues = new Map();
const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
const snapshotId = value => /^model-[0-9a-f]{64}$/.test(value ?? '');
function canonical(value) {
  if (value === null || ['string', 'boolean'].includes(typeof value)) return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  fail('invalid_daily_input', '日常计算参数包含无效数据。');
}
const owner = scope => hash(canonical([scope.workspaceId, scope.sessionId]));
const call = scope => hash(canonical(['sessionId', 'workspaceId', 'turnId', 'callId', 'rootCallId'].map(key => scope[key])));
const manifestHash = inspected => hash(canonical(inspected.manifest));

function relative(value) {
  if (typeof value !== 'string' || !value || value.startsWith('/') || /[\\:\x00-\x1f<>"|?*%@$`]/.test(value)
      || value.split('/').some(part => !part || part === '.' || part === '..' || /[. ]$/.test(part))) {
    fail('invalid_file_selection', '请明确选择工作区里的模型文件；路径必须位于工作区内。');
  }
  return value;
}

/** File references are supplied by the model from the user's actual selection;
 * native file authorization still occurs before any file bytes are read.
 */
export function normalizeWorkspaceSelection(args) {
  if (args.source === 'native_attachments') {
    if (args.workspace_files !== undefined) fail('invalid_import_source', '附件与工作区路径不能混用。');
    return args;
  }
  if (args.workspace_files === undefined) return args;
  if (args.package_root !== undefined || args.files !== undefined || args.attachments !== undefined) {
    fail('invalid_file_selection', '请选择一种文件清单方式，不要混合路径与旧包目录字段。');
  }
  if (!Array.isArray(args.workspace_files) || !args.workspace_files.length || args.workspace_files.length > 500) {
    fail('invalid_file_selection', '需要本轮明确选择的模型文件清单。');
  }
  const selected = args.workspace_files.map(relative);
  if (new Set(selected.map(name => name.toLowerCase())).size !== selected.length) fail('invalid_file_selection', '文件清单存在重复项。');
  let master = args.master_file;
  if (master !== undefined) {
    relative(master);
    const matches = selected.filter(name => name === master || path.posix.basename(name) === master);
    if (matches.length !== 1) fail('model_entry_ambiguous', '无法唯一确定入口文件，请指出要使用哪个入口。');
    master = matches[0];
  } else if (selected.length === 1 && /\.(?:dss|m|csv|json)$/i.test(selected[0])) master = selected[0];
  else {
    const matches = selected.filter(name => /^master\.dss$/i.test(path.posix.basename(name)));
    if (matches.length !== 1) fail('model_entry_ambiguous', '有多个可能入口或没有明确入口，请指出主文件。');
    master = matches[0];
  }
  const inferred = /\.dss$/i.test(master) ? 'OpenDSS' : /\.m$/i.test(master) ? 'MATPOWER' : /\.csv$/i.test(master) ? 'CSV' : /\.json$/i.test(master) ? 'JSON' : null;
  if (!inferred || (args.format !== undefined && args.format !== inferred)) fail('unsupported_model_format', '当前专业入口支持原生DSS或受支持的MATPOWER模型；该文件格式不能直接用于本流程。');
  const dirs = selected.map(name => name.split('/').slice(0, -1));
  const common = [];
  for (let i = 0; i < Math.min(...dirs.map(parts => parts.length)); i++) {
    if (!dirs.every(parts => parts[i] === dirs[0][i])) break;
    common.push(dirs[0][i]);
  }
  const prefix = common.length ? common.join('/') + '/' : '';
  const { workspace_files: _selected, ...rest } = args;
  return { ...rest, source: 'workspace', format: inferred, package_root: common.join('/') || '.',
    master_file: master.slice(prefix.length), files: selected.map(name => name.slice(prefix.length)) };
}

async function directory(config, name) {
  const base = await fs.realpath(config.stateDir);
  const destination = path.join(base, name);
  await fs.mkdir(destination, { recursive: true, mode: 0o700 });
  const info = await fs.lstat(destination);
  if (!info.isDirectory() || info.isSymbolicLink() || await fs.realpath(destination) !== destination) {
    fail('unsafe_daily_store', '日常计算准备目录不可用，未提交计算。');
  }
  return destination;
}
async function readRecord(filename) {
  let handle;
  try {
    const visible = await fs.lstat(filename);
    if (!visible.isFile() || visible.isSymbolicLink() || visible.nlink !== 1 || visible.size > 128 * 1024) fail('unsafe_daily_store', '日常准备记录无效，未提交计算。');
    handle = await fs.open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const before = await handle.stat();
    const text = await handle.readFile('utf8');
    const after = await handle.stat();
    const current = await fs.lstat(filename);
    if (before.ino !== current.ino || before.dev !== current.dev || current.isSymbolicLink() || after.size !== before.size || after.mtimeMs !== before.mtimeMs) fail('unsafe_daily_store', '准备记录读取时发生变化。');
    return JSON.parse(text);
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  finally { await handle?.close(); }
}
async function saveNew(filename, value) {
  const handle = await fs.open(filename, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(value)); await handle.sync(); }
  finally { await handle.close(); }
}
async function serialized(filename, body) {
  const previous = queues.get(filename) ?? Promise.resolve();
  const next = previous.catch(() => {}).then(body);
  queues.set(filename, next);
  try { return await next; } finally { if (queues.get(filename) === next) queues.delete(filename); }
}

export async function rememberImportedModel(config, scope, inspected, payload) {
  if (!snapshotId(inspected.snapshot_id)) fail('invalid_model_summary', '模型没有返回固定快照身份。');
  const summary = summarizeModel(inspected, { snapshotId: inspected.snapshot_id, files: payload.files });
  const dir = await directory(config, 'daily-models');
  const filename = path.join(dir, owner(scope) + '-' + inspected.snapshot_id + '.json');
  const record = { schema: 'tianshu-daily-model/1', owner: owner(scope), snapshot_id: inspected.snapshot_id,
    fingerprint: inspected.fingerprint, manifest_sha256: manifestHash(inspected), summary };
  await serialized(filename, async () => {
    const existing = await readRecord(filename);
    if (existing && canonical(existing) !== canonical(record)) fail('daily_model_summary_conflict', '同一模型的准备摘要发生变化；未计算，请核对输入。');
    if (!existing) await saveNew(filename, record);
  });
  return summary;
}

export async function prepareDailyCall(config, scope, args, bridge) {
  const ref = args.model;
  if (!ref || typeof ref !== 'object' || Array.isArray(ref) || Object.keys(ref).length !== 1
      || !(snapshotId(ref.snapshot_id) || typeof ref.case_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(ref.case_id))) {
    fail('invalid_daily_model', '请使用本会话已导入的模型，或明确选择已登记算例。');
  }
  let summary;
  if (ref.snapshot_id) {
    const inspected = await bridge.inspectModel(scope, ref.snapshot_id);
    const dir = await directory(config, 'daily-models');
    const stored = await readRecord(path.join(dir, owner(scope) + '-' + ref.snapshot_id + '.json'));
    if (stored) {
      if (stored.schema !== 'tianshu-daily-model/1' || stored.owner !== owner(scope) || stored.snapshot_id !== ref.snapshot_id
          || stored.fingerprint !== inspected.fingerprint || stored.manifest_sha256 !== manifestHash(inspected)
          || canonical(stored.summary.model) !== canonical(ref)) fail('daily_model_summary_conflict', '固定模型与准备摘要不一致，未提交计算。');
      summary = stored.summary;
    } else summary = summarizeModel(inspected, { snapshotId: ref.snapshot_id });
  } else {
    const models = await bridge.models(scope);
    if (models.filter(item => item.case_id === ref.case_id).length !== 1) fail('invalid_daily_model', '找不到明确选择的已登记算例。');
    summary = { model: { ...ref }, frequency: { status: 'missing', evidence: [], reason: 'registered_case_requires_explicit_frequency' } };
  }
  const dir = await directory(config, 'daily-requests');
  const filename = path.join(dir, call(scope) + '.json');
  const input = { model: ref, ...(args.frequency_hz === undefined ? {} : { frequency_hz: args.frequency_hz }) };
  const inputHash = hash(canonical(input));
  return serialized(filename, async () => {
    const stored = await readRecord(filename);
    if (stored) {
      if (stored.schema !== 'tianshu-daily-request/1' || stored.owner !== owner(scope) || stored.input_sha256 !== inputHash
          || stored.model_summary_sha256 !== hash(canonical(summary)) || stored.call_sha256 !== call(scope)
          || stored.request_sha256 !== hash(canonical(stored.prepared.request))) fail('daily_request_conflict', '同一调用的模型或计算请求发生变化，不能复用原计算。');
      return stored.prepared;
    }
    const prepared = prepareDailyRequest({ model: ref, modelSummary: summary, ...input },
      { preparedAt: new Date().toISOString(), callIdentityHash: call(scope) });
    if (prepared.status !== 'ready') return prepared;
    await saveNew(filename, { schema: 'tianshu-daily-request/1', owner: owner(scope), call_sha256: call(scope),
      input_sha256: inputHash, model_summary_sha256: hash(canonical(summary)), request_sha256: hash(canonical(prepared.request)), prepared });
    return prepared;
  });
}
