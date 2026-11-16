/** Host-only bridge for the authenticated Tianshu professional core.
 * Copyright (c) yangshaopeng. No credentials or caller identity are model input.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import net from 'node:net';
import { createHash, randomBytes } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { loopbackFetch } from './loopback-http.js';

const TERMINAL = new Set(['finished', 'failed', 'cancelled', 'interrupted']);
const STATES = new Set(['created', 'queued', 'running', 'cancelling', ...TERMINAL]);
const ARTIFACTS = new Set(['result.json', 'report.html', 'checks.csv', 'observations.json', 'observations.csv', 'engineering.csv']);
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const digest = value => createHash('sha256').update(value).digest('hex');
const clone = value => JSON.parse(JSON.stringify(value));
const now = () => new Date().toISOString();
const CONTRACT_CODES = new Set(['invalid_contract', 'capability_unavailable', 'model_integrity_error', 'island_detected']);
const CONTRACT_ROOTS = new Set(['model', 'request', 'request_id', 'schema_version', 'operating_conditions', 'scenario', 'solver', 'recovery', 'criteria', 'result_scopes', 'output_formats', 'study', 'comparison', 'report']);
const CONTRACT_REASONS = new Map([
  ['a referenced package file is missing', '模型引用的依赖文件不在本次导入清单中，请补齐入口引用的文件。'],
  ['repeated or cyclic DSS script inclusion is not accepted', '模型存在重复或循环的DSS文件引用，当前导入器不支持这种依赖结构。'],
  ['leaf DSS scripts cannot include other files', '子DSS文件继续引用其他脚本的结构超出当前导入范围，请核对模型文件组织。'],
  ['internal Compile changes directory state and is not supported', '模型内部的Compile命令会改变目录状态，当前导入器不支持。'],
  ['Redirect requires exactly one quoted or bare file path', 'Redirect命令需要一个明确的引用文件路径。'],
  ['linked or reparse-point paths are not accepted', '模型路径包含链接或目录联接，不能作为固定输入读取。'],
  ['must be a bounded relative POSIX file path', '模型清单需要有效的包内相对文件路径。'],
  ['package exceeds the byte budget', '模型文件包超过当前大小限制。'],
  ['DSS input exceeds the declared line budget', '模型文件总行数超过当前导入范围。'],
  ['DSS device/library class is outside the snapshot policy', '模型包含当前稳态导入器尚不支持的设备或库类型。'],
  ['unsupported temporal, control or solution semantics', '模型包含当前稳态入口不支持的时序、自动控制或求解设置。'],
  ['Clear is supported once before circuit/object creation', 'Clear只能在电路或设备建立前出现一次，请核对命令顺序。'],
]);

export class BridgeError extends Error {
  constructor(code, message, details = {}) {
    super(message); this.name = 'BridgeError'; this.code = code;
    Object.assign(this, details);
  }
  toJSON() {
    return { code: this.code, message: this.message, ...(this.taskId ? { taskId: this.taskId } : {}),
      ...(Number.isInteger(this.status) ? { status: this.status } : {}),
      ...(typeof this.responseUnknown === 'boolean' ? { responseUnknown: this.responseUnknown } : {}),
      ...(CONTRACT_CODES.has(this.core_code) && typeof this.field === 'string' && typeof this.reason === 'string'
        ? { core_code: this.core_code, field: this.field, reason: this.reason } : {}) };
  }
}

function fail(code, message) { throw new BridgeError(code, message); }
function plain(value) { return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }

/** Translate only a tightly bounded device/node diagnostic, never raw errors. */
export function tableConnectionDiagnostic(reason) {
  if(typeof reason!=='string')return null;
  const duplicate=/^duplicate_device_id; object=([A-Za-z0-9_.-]{1,256}); first_index=([0-9]{1,5}); duplicate_index=([0-9]{1,5})$/.exec(reason);
  if(duplicate)return `设备标识 ${duplicate[1]} 重复：对象表第 ${Number(duplicate[2])+1} 项与第 ${Number(duplicate[3])+1} 项使用相同标识。`;
  const missing=/^missing_device_properties; object=([A-Za-z0-9_.-]{1,256}); missing=([a-z0-9_,|%]{1,256})$/.exec(reason);
  const propertyNames=new Set(['model','conn','kv','kw','kvar','pf','pmpp','irradiance','kva']);
  if(missing&&missing[2].split(',').every(group=>group.split('|').every(key=>propertyNames.has(key))))return `设备 ${missing[1]} 缺少必填参数：${missing[2].split(',').map(group=>group.split('|').join(' 或 ')).join('、')}。`;
  const nodes=/^undeclared_bus_nodes; object=([A-Za-z0-9_.-]{1,256}); connection=([A-Za-z0-9_.-]{1,240}); declared_nodes=([0-9,]{1,64})$/.exec(reason);
  if(nodes)return `设备 ${nodes[1]} 的连接 ${nodes[2]} 引用了母线未声明的节点；该母线已声明节点为 ${nodes[3].split(',').join('、')}。`;
  const bus=/^undeclared_bus; object=([A-Za-z0-9_.-]{1,256}); connection=([A-Za-z0-9_.-]{1,240})$/.exec(reason);
  return bus?`设备 ${bus[1]} 的连接 ${bus[2]} 引用了未声明的母线。`:null;
}
/** Recognize the core's small ContractError envelope, never display its raw text.
 * Only constant or strictly parsed device/node diagnoses and bounded fields leave here.
 */
async function contractDiagnostic(response, token) {
  try {
    const media = response.headers?.get('content-type') ?? '';
    if (!/^application\/json(?:\s*;|$)/i.test(media)) return null;
    const length = response.headers?.get('content-length');
    if (length !== null && length !== undefined && (!/^\d+$/.test(length) || Number(length) > 8192)) return null;
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > 8192) return null;
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    if (!plain(value) || Object.keys(value).sort().join(',') !== 'code,field,message'
        || !CONTRACT_CODES.has(value.code) || typeof value.field !== 'string' || value.field.length > 96
        || !/^[A-Za-z_][A-Za-z0-9_]*(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[\d{1,5}\])){0,8}$/.test(value.field)
        || !CONTRACT_ROOTS.has(value.field.split(/[.\[]/, 1)[0])
        || typeof value.message !== 'string' || !value.message || value.message.length > 4096) return null;
    const combined = value.field + ' ' + value.message;
    if ((token && combined.includes(token)) || /[\x00-\x1f\x7f]|[A-Za-z]:[\\/]|\\\\|(?:^|[\s"'(=:])\/[A-Za-z_.-]|\b(?:traceback|authorization|bearer|api[_-]?key|access[_-]?token)\b/i.test(combined)) return null;
    const prefix = value.field + ': ';
    const originalReason = value.message.startsWith(prefix) ? value.message.slice(prefix.length) : value.message;
    const genericReason = value.code === 'model_integrity_error'
      ? '已保存的模型快照完整性检查失败，请停止使用该快照并核对原文件。'
      : value.field === 'model.dependencies'
        ? '模型依赖文件或引用结构检查未通过，请核对入口与完整文件清单。'
        : value.code === 'capability_unavailable'
          ? '该字段涉及的模型表达或计算能力超出当前支持范围。'
          : '模型或计算请求的该字段未通过输入合同检查。';
    return { core_code: value.code, field: value.field, reason: CONTRACT_REASONS.get(originalReason) ?? tableConnectionDiagnostic(originalReason) ?? genericReason };
  } catch { return null; }
}
function bounded(value, field) {
  if (typeof value !== 'string' || !value || value.length > 4096 || /[\x00-\x1f]/.test(value)) fail('invalid_scope', `Host ${field} is required`);
  return value;
}
function scopeOf(scope) {
  if (!plain(scope)) fail('invalid_scope', 'Trusted Host scope is required');
  return Object.fromEntries(['sessionId', 'workspaceId', 'turnId', 'callId', 'rootCallId'].map(k => [k, bounded(scope[k], k)]));
}
function ownerKey(scope) { return digest(JSON.stringify([scope.workspaceId, scope.sessionId])); }
function callKey(scope) { return digest(JSON.stringify([scope.workspaceId, scope.sessionId, scope.turnId, scope.callId])); }
function canonical(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (plain(value)) return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  fail('invalid_contract', 'Payload must contain only finite JSON values');
}
function exactKeys(value, allowed, required, field) {
  if (!plain(value) || Object.keys(value).some(k => !allowed.includes(k)) || required.some(k => !Object.hasOwn(value, k))) fail('invalid_contract', `${field} has missing or unknown fields`);
}
function validateRequest(request) {
  const fields = ['schema_version', 'model', 'scenario', 'solver', 'recovery', 'criteria', 'result_scopes', 'output_formats', 'operating_conditions'];
  exactKeys(request, fields, fields, 'request');
  if (request.schema_version !== '1.1') fail('invalid_contract', 'Harness requires explicit request schema 1.1');
  exactKeys(request.model, ['case_id', 'snapshot_id'], [], 'model');
  if (Object.keys(request.model).length !== 1 || !ID.test(request.model.case_id ?? request.model.snapshot_id ?? '') || String(request.model.case_id ?? request.model.snapshot_id).includes(':')) fail('invalid_contract', 'Provide one model identifier');
  exactKeys(request.scenario, ['scenario_id', 'mode'], ['scenario_id', 'mode'], 'scenario');
  if (!ID.test(request.scenario.scenario_id) || request.scenario.mode !== 'existing_snapshot') fail('invalid_contract', 'An explicit existing_snapshot scenario is required');
  const solverFields = ['engine', 'transport', 'solve_mode', 'max_iterations', 'tolerance'];
  exactKeys(request.solver, solverFields, solverFields, 'solver');
  if (request.solver.engine !== 'OpenDSS' || request.solver.transport !== 'com' || request.solver.solve_mode !== 'snapshot' || !Number.isInteger(request.solver.max_iterations) || request.solver.max_iterations < 1 || request.solver.max_iterations > 10000 || !Number.isFinite(request.solver.tolerance) || request.solver.tolerance <= 0 || request.solver.tolerance > 1) fail('invalid_contract', 'Provide explicit supported OpenDSS snapshot parameters');
  exactKeys(request.recovery, ['policy', 'max_retries', 'retry_solver'], ['policy', 'max_retries'], 'recovery');
  if (request.recovery.policy !== 'none' || request.recovery.max_retries !== 0 || request.recovery.retry_solver != null) fail('capability_unavailable', 'The connected core supports recovery policy none only');
  const conditions = ['frequency_hz', 'source', 'confirmed_by', 'confirmed_at', 'mode'];
  exactKeys(request.operating_conditions, conditions, conditions, 'operating_conditions');
  if (request.operating_conditions.mode !== 'validate_model_operating_point' || !Number.isFinite(request.operating_conditions.frequency_hz) || request.operating_conditions.frequency_hz <= 0 || request.operating_conditions.frequency_hz > 1000 || conditions.filter(k => !['frequency_hz', 'mode'].includes(k)).some(k => typeof request.operating_conditions[k] !== 'string' || !request.operating_conditions[k].trim())) fail('invalid_contract', 'Explicit declared operating conditions are required');
  for (const [key, values] of [['result_scopes', ['bus', 'branch', 'transformer', 'system']], ['output_formats', ['json', 'html', 'csv']]]) {
    if (!Array.isArray(request[key]) || request[key].length !== values.length || new Set(request[key]).size !== values.length || request[key].some(x => !values.includes(x))) fail('invalid_contract', `Harness requires the complete explicit ${key} set`);
  }
  if (request.criteria !== null && !plain(request.criteria)) fail('invalid_contract', 'criteria must be an explicit project object or null (unassessed)');
  const serialized = canonical(request);
  if (Buffer.byteLength(serialized) > 250 * 1024) fail('invalid_contract', 'Request exceeds the bridge payload budget');
  return JSON.parse(serialized);
}
function validateImport(payload) {
  if (!plain(payload)) fail('invalid_contract', 'A file content package is required');
  if (payload.format === 'MATPOWER') {
    if (Object.keys(payload).some(k => /(?:path|directory|url|workspace)/i.test(k))) fail('invalid_contract', 'Import accepts content, never host filesystem paths');
  } else {
    const table = ['CSV', 'JSON'].includes(payload.format);
    exactKeys(payload, table ? ['format', 'master_file', 'files'] : ['master_file', 'files'], table ? ['format', 'master_file', 'files'] : ['master_file', 'files'], 'model import');
    if (typeof payload.master_file !== 'string' || !Array.isArray(payload.files) || payload.files.length < 1 || payload.files.length > 500) fail('invalid_contract', 'Provide master_file and 1–500 UTF-8 file entries');
    let total = 0;
    for (const file of payload.files) {
      exactKeys(file, ['path', 'content_utf8'], ['path', 'content_utf8'], 'model file');
      if (typeof file.path !== 'string' || !file.path || file.path.length > 240 || file.path.startsWith('/') || /[\\:\x00-\x1f]/.test(file.path) || file.path.split('/').some(p => !p || p === '.' || p === '..') || typeof file.content_utf8 !== 'string') fail('invalid_contract', 'Model entries require relative POSIX paths and UTF-8 contents');
      total += Buffer.byteLength(file.content_utf8);
    }
    if (total > 20 * 1024 * 1024) fail('invalid_contract', 'Model content exceeds 20 MiB');
  }
  if (Buffer.byteLength(canonical(payload)) > 32 * 1024 * 1024) fail('invalid_contract', 'Import wire budget exceeded');
  return clone(payload);
}

async function regularFile(filename, limit) {
  const info = await fs.lstat(filename);
  if (!info.isFile() || info.isSymbolicLink() || info.size > limit) fail('unsafe_local_file', 'Host configuration/state is not a bounded regular file');
  return fs.readFile(filename, 'utf8');
}
async function safeDirectory(directory) {
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  let cursor = path.resolve(directory);
  for (;;) {
    if ((await fs.lstat(cursor)).isSymbolicLink()) fail('unsafe_state_directory', 'State directory must not traverse linked entries');
    const parent = path.dirname(cursor); if (parent === cursor) break; cursor = parent;
  }
  return fs.realpath(directory);
}
async function atomicJson(filename, value) {
  const temp = `${filename}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`;
  const handle = await fs.open(temp, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(value, null, 2) + '\n'); await handle.sync(); } finally { await handle.close(); }
  await fs.rename(temp, filename);
}

export class TianshuBridge {
  #state; #stateDir; #connectionFile; #connection; #fetch; #lock; #ready; #queue = Promise.resolve();
  #closed = false; #draining = false; #closing; #drainPromise; #inflight = new Map(); #timeout; #poll; #maxRecovery; #waits = new Set();
  constructor({ stateDir, connectionFile, fetchImpl = loopbackFetch, requestTimeoutMs = 15000, pollIntervalMs = 300, maxRecoveryAttempts = 3 } = {}) {
    if (!stateDir || !connectionFile || typeof fetchImpl !== 'function') fail('invalid_host_configuration', 'stateDir, connectionFile and fetch implementation are required');
    this.#stateDir = path.resolve(stateDir); this.#connectionFile = path.resolve(connectionFile); this.#fetch = fetchImpl;
    this.#timeout = requestTimeoutMs; this.#poll = pollIntervalMs; this.#maxRecovery = maxRecoveryAttempts;
    this.#ready = this.#initialize();
    // Construction is asynchronous; ready()/the first method reports errors.
    this.#ready.catch(() => {});
  }
  async ready() { await this.#ready; return this; }
  async #initialize() {
    this.#stateDir = await safeDirectory(this.#stateDir);
    const normalized = process.platform === 'win32' ? this.#stateDir.toLowerCase() : this.#stateDir;
    const lockName = digest(normalized);
    // The OS owns this listener, so a crashed process cannot leave a stale PID
    // lock. Hash collisions conservatively refuse a second owner, never steal.
    const port = 49152 + parseInt(lockName.slice(0, 6), 16) % 16384;
    this.#lock = net.createServer(socket => socket.destroy());
    try {
      await new Promise((resolve, reject) => { this.#lock.once('error', reject); this.#lock.listen({ host: '127.0.0.1', port, exclusive: true }, resolve); });
      this.#lock.unref();
      const filename = path.join(this.#stateDir, 'bridge-state.json');
      try { this.#state = JSON.parse(await regularFile(filename, 32 * 1024 * 1024)); }
      catch (error) { if (error.code !== 'ENOENT') throw error; this.#state = { schema: 'tianshu-harness-bridge/1', owners: {}, calls: {}, tasks: {}, created_at: now() }; }
      if (this.#state.schema !== 'tianshu-harness-bridge/1' || !plain(this.#state.owners) || !plain(this.#state.calls) || !plain(this.#state.tasks)) fail('invalid_persisted_state', 'Bridge state format is invalid; no task was replayed');
      this.#state.studyCalls ??= {};
      if (!plain(this.#state.studyCalls)) fail('invalid_persisted_state', 'Study bindings are invalid');
      for (const b of Object.values(this.#state.studyCalls)) {
        if (!plain(b) || !plain(b.scope) || b.owner !== ownerKey(scopeOf(b.scope)) || !plain(b.request) || (b.studyId && !/^study-[a-f0-9]{32}$/.test(b.studyId))) fail('invalid_persisted_state', 'Study ownership is invalid');
      }
      // JSON has no shared references: rebind the task index to its canonical
      // call object after every restart, so cursors/cancellation/history agree.
      for (const binding of Object.values(this.#state.calls)) {
        if (!plain(binding) || !plain(binding.scope) || binding.owner !== ownerKey(scopeOf(binding.scope)) || !plain(binding.request)) fail('invalid_persisted_state', 'Persisted call ownership is invalid');
        if (binding.taskId) {
          const old = this.#state.tasks[binding.taskId];
          if (!old || old.owner !== binding.owner || old.request?.request_id !== binding.request.request_id || old.attemptId !== binding.attemptId) fail('invalid_persisted_state', 'Persisted task index differs from the canonical call');
          this.#state.tasks[binding.taskId] = binding;
        }
      }
      await this.#readConnection();
    } catch (error) {
      if (this.#lock.listening) await new Promise(resolve => this.#lock.close(resolve));
      if (error.code === 'EADDRINUSE') fail('bridge_owned', 'Another bridge owns this state directory (or its local lock port)');
      throw error instanceof BridgeError ? error : new BridgeError('invalid_host_configuration', 'Bridge state or service configuration could not be loaded');
    }
  }
  async #readConnection() {
    let connection;
    try { connection = JSON.parse(await regularFile(this.#connectionFile, 64 * 1024)); }
    catch { fail('service_unavailable', 'Local professional service connection is unavailable'); }
    let url; try { url = new URL(connection.url); } catch { fail('invalid_service_connection', 'Invalid local professional endpoint'); }
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password || url.search || url.hash || url.pathname !== '/' || connection.service !== 'tianshu-professional' || typeof connection.token !== 'string' || connection.token.length < 24 || /[\r\n]/.test(connection.token)) fail('invalid_service_connection', 'Professional endpoint must be the configured authenticated IPv4 loopback service');
    this.#connection = { base: url.origin, token: connection.token };
  }
  async #ensure() { await this.#ready; if (this.#closed) fail('bridge_closed', 'Bridge has been closed'); }
  async #mutate(action) {
    const operation = this.#queue.then(async () => { await this.#ensure(); const value = action(); this.#state.updated_at = now(); await atomicJson(path.join(this.#stateDir, 'bridge-state.json'), this.#state); return value; });
    this.#queue = operation.catch(() => {}); return operation;
  }
  #redact(value) {
    if (typeof value === 'string') return this.#connection?.token ? value.replaceAll(this.#connection.token, '[redacted]') : value;
    if (Array.isArray(value)) return value.map(x => this.#redact(x));
    if (plain(value)) return Object.fromEntries(Object.entries(value).filter(([k]) => !/^(?:token|authorization|access_token|api_key)$/i.test(k)).map(([k, v]) => [k, this.#redact(v)]));
    return value;
  }
  async #http(method, route, payload, binary = false, timeoutMs = this.#timeout) {
    await this.#ensure(); await this.#readConnection();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await this.#fetch(this.#connection.base + route, {
        method, headers: { authorization: 'Bearer ' + this.#connection.token, ...(payload === undefined ? {} : { 'content-type': 'application/json' }) },
        ...(payload === undefined ? {} : { body: canonical(payload) }), signal: controller.signal, redirect: 'error',
      });
      if (!response.ok) {
        // Preserve the existing transport/recovery code. Only a recognized 422
        // ContractError adds a safe diagnostic; no arbitrary response is echoed.
        const diagnostic = response.status === 422 ? await contractDiagnostic(response, this.#connection.token) : null;
        const message = diagnostic ? `输入检查未通过：${diagnostic.reason}（字段：${diagnostic.field}）` : `Professional service returned HTTP ${response.status}`;
        throw new BridgeError(response.status >= 500 ? 'service_response_unknown' : 'service_rejected', message,
          { status: response.status, responseUnknown: response.status >= 500, ...(diagnostic ?? {}) });
      }
      if (binary) {
        const bytes = Buffer.from(await response.arrayBuffer());
        if (bytes.length > 128 * 1024 * 1024) fail('artifact_too_large', 'Artifact exceeds the bridge budget');
        return { bytes, mediaType: response.headers?.get('content-type') ?? 'application/octet-stream' };
      }
      return this.#redact(await response.json());
    } catch (error) {
      if (error instanceof BridgeError) throw error;
      throw new BridgeError('service_response_unknown', 'Professional service response is unknown; retain the original request identity', { responseUnknown: true });
    } finally { clearTimeout(timer); }
  }
  #owner(scope) { return this.#state.owners[ownerKey(scope)]; }
  #task(scope, taskId) {
    if (!ID.test(taskId ?? '')) fail('invalid_task_id', 'A task identifier is required');
    const task = this.#state.tasks[taskId];
    if (!task || task.owner !== ownerKey(scope)) fail('task_access_denied', 'Task does not belong to the active Host session and workspace');
    return task;
  }
  #identity(row, task, { submitting = false } = {}) {
    if (!plain(row) || !ID.test(row.task_id ?? '') || !ID.test(row.attempt_id ?? '') || row.request_id !== task.request.request_id || (task.taskId && row.task_id !== task.taskId) || (task.attemptId && row.attempt_id !== task.attemptId) || !STATES.has(row.execution_status)) fail('response_identity_mismatch', 'Professional task identity or execution status does not match the persisted binding');
    return row;
  }
  async capabilities(scope) {
    scope = scopeOf(scope); const value = await this.#http('GET', '/v1/capabilities');
    if (!plain(value) || value.service !== 'tianshu-professional') fail('invalid_service_response', 'Professional capability identity is invalid');
    if (Array.isArray(value.models)) value.models = this.#filterModels(scope, value.models);
    return { ...value, harness_bridge: { connected: true, owner_scope: 'workspace_and_session', request_schema: '1.1', history_resolves: false } };
  }
  #filterModels(scope, models) {
    const grants = this.#owner(scope)?.snapshots ?? [];
    return models.filter(model => plain(model) && (typeof model.case_id === 'string' || grants.includes(model.snapshot_id)));
  }
  async models(scope) {
    scope = scopeOf(scope); const models = await this.#http('GET', '/v1/models');
    if (!Array.isArray(models)) fail('invalid_service_response', 'Professional model list must be an array');
    return this.#filterModels(scope, models);
  }
  async importModel(scope, payload) {
    scope = scopeOf(scope); const content = validateImport(payload); await this.#ensure();
    if (this.#draining) fail('bridge_draining', 'Host is stopping this plugin; no new model may be imported');
    const imported = await this.#http('POST', '/v1/models/import', content);
    if (!plain(imported) || !/^model-[0-9a-f]{64}$/.test(imported.snapshot_id ?? '')) fail('response_identity_mismatch', 'Import did not return an immutable model identifier');
    await this.#mutate(() => {
      const key = ownerKey(scope); const owner = this.#state.owners[key] ??= { scope: { sessionId: scope.sessionId, workspaceId: scope.workspaceId }, snapshots: [] };
      if (!owner.snapshots.includes(imported.snapshot_id)) owner.snapshots.push(imported.snapshot_id);
    });
    return imported;
  }
  async inspectModel(scope, snapshotId) {
    scope = scopeOf(scope); await this.#ensure();
    if (!this.#owner(scope)?.snapshots.includes(snapshotId)) fail('model_access_denied', 'Snapshot was not imported by this session and workspace');
    const inspected = await this.#http('GET', `/v1/models/${encodeURIComponent(snapshotId)}`);
    if (inspected.snapshot_id !== snapshotId) fail('response_identity_mismatch', 'Model identity differs from the requested snapshot');
    return inspected;
  }
  async pdf(scope, taskId, {study=false}={}) {
    scope=scopeOf(scope); await this.#ensure(); study?this.#study(scope,taskId):this.#task(scope,taskId);
    const task=study?await this.studyStatus(scope,taskId):await this.status(scope,taskId);
    if (!TERMINAL.has(task.execution_status) || !task.artifacts?.includes('report.html')) fail('artifact_unavailable','No committed report is available');
    const value=await this.#http('GET',`/v1/${study?'studies':'jobs'}/${encodeURIComponent(taskId)}/report.pdf`,undefined,true,95000);
    if (!value.bytes.subarray(0,5).equals(Buffer.from('%PDF-'))) fail('invalid_pdf','Service did not return a PDF');
    return {...value,name:'report.pdf',taskId,sha256:digest(value.bytes),bytesLength:value.bytes.length};
  }
  async compareScenarios(scope,left,right) {
    scope=scopeOf(scope);await this.#ensure();this.#task(scope,left);this.#task(scope,right);
    const r=await this.#http('POST','/v1/compare-scenarios',{left_task_id:left,right_task_id:right});
    if(r.left_task_id!==left||r.right_task_id!==right||r.recomputed!==false)fail('response_identity_mismatch','Scenario comparison identities differ');
    return r;
  }
  #study(scope,id) {
    if(!/^study-[a-f0-9]{32}$/.test(id??''))fail('invalid_study_id','A study ID is required');
    const b=Object.values(this.#state.studyCalls).find(x=>x.studyId===id&&x.owner===ownerKey(scope));
    if(!b)fail('study_access_denied','Study does not belong to this Host session and workspace');return b;
  }
  #studyIdentity(row,b) {
    if(!plain(row)||!/^study-[a-f0-9]{32}$/.test(row.study_id??'')||row.request_id!==b.request.request_id||(b.studyId&&row.study_id!==b.studyId)||!STATES.has(row.execution_status)||!Array.isArray(row.points))fail('response_identity_mismatch','Study identity does not match the persisted Host call');
    return row;
  }
  async submitStudy(scope,input) {
    scope=scopeOf(scope);await this.#ensure();
    exactKeys(input,['mode','curve_source','base_request','points','curve_file','on_point_error'],['mode','curve_source','base_request','on_point_error'],'study');
    if((input.points!==undefined)===(input.curve_file!==undefined))fail('invalid_contract','Provide one explicit point array or curve file');
    const base=validateRequest(input.base_request);
    if(base.model.snapshot_id&&!this.#owner(scope)?.snapshots.includes(base.model.snapshot_id))fail('model_access_denied','Base snapshot is not owned by this session');
    const content={...clone(input),base_request:base};const encoded=canonical(content);
    if(Buffer.byteLength(encoded)>2*1024*1024)fail('invalid_contract','Study exceeds 2 MiB');
    const key=callKey(scope),hash=digest(encoded);let b=this.#state.studyCalls[key];
    if(b&&(b.owner!==ownerKey(scope)||b.contentHash!==hash))fail('call_request_conflict','Host study call is bound to different inputs');
    if(this.#draining&&!b?.studyId)fail('bridge_draining','No new study may start while stopping');
    if(b?.studyId)return this.studyStatus(scope,b.studyId);
    if(!b){
      await this.#mutate(()=>{const old=this.#state.studyCalls[key];if(old){if(old.owner!==ownerKey(scope)||old.contentHash!==hash)fail('call_request_conflict','Host study call is bound to different inputs');b=old;return;}const requestId='harness-study-'+randomBytes(20).toString('hex');b={owner:ownerKey(scope),scope,contentHash:hash,request:{...content,request_id:requestId,base_request:{...base,request_id:requestId+'-base'}},studyId:null,phase:'prepared',cancelRequested:false};this.#state.studyCalls[key]=b;});
    }
    const activeKey='study:'+key;
    if(this.#inflight.has(activeKey))return this.#inflight.get(activeKey).promise;
    const promise=(async()=>{
      if(b.phase==='submission_unknown'){
        // Look up the original accepted request, never create work in recovery.
        const found=this.#studyIdentity(await this.#http('GET',`/v1/studies/by-request/${encodeURIComponent(b.request.request_id)}`),b);
        await this.#mutate(()=>{b.studyId=found.study_id;b.phase='bound';b.status=found;});return found;
      }
      await this.#mutate(()=>{b.phase='submission_unknown';});
      let row;
      try{row=this.#studyIdentity(await this.#http('POST','/v1/studies',b.request),b);}
      catch(e){if(e.responseUnknown===false)await this.#mutate(()=>{b.phase='rejected';});throw e;}
      await this.#mutate(()=>{b.studyId=row.study_id;b.phase='bound';b.status=row;});return row;
    })();
    this.#inflight.set(activeKey,{promise});try{return await promise;}finally{this.#inflight.delete(activeKey);}
  }
  async studyStatus(scope,id) {
    scope=scopeOf(scope);await this.#ensure();const b=this.#study(scope,id);
    const row=this.#studyIdentity(await this.#http('GET',`/v1/studies/${encodeURIComponent(id)}`),b);
    await this.#mutate(()=>{
      b.status=row;
      for(const p of row.points){
        if(!p.task_id)continue;
        if(!ID.test(p.task_id)||!ID.test(p.attempt_id??'')||!Number.isSafeInteger(p.index)||p.index<0||p.index>=1000||p.child_request_id!==id+'-p'+p.index||!/^model-[a-f0-9]{64}$/.test(p.snapshot_id??''))fail('response_identity_mismatch','Study child binding is invalid');
        const old=this.#state.tasks[p.task_id];if(old&&old.owner!==ownerKey(scope))fail('task_access_denied','Study child has another owner');
        const key='study-child:'+id+':'+p.index;
        const child=old??{owner:ownerKey(scope),scope,request:{request_id:p.child_request_id},phase:'bound',taskId:p.task_id,attemptId:p.attempt_id,cancelRequested:false,cursor:0,created_at:now(),studyId:id};
        if(child.request.request_id!==p.child_request_id||child.attemptId!==p.attempt_id)fail('response_identity_mismatch','Study child changed identity');
        if(TERMINAL.has(p.execution_status))child.status={task_id:p.task_id,request_id:p.child_request_id,attempt_id:p.attempt_id,execution_status:p.execution_status};
        this.#state.calls[key]=child;this.#state.tasks[p.task_id]=child;
        const owner=this.#state.owners[ownerKey(scope)]??={scope:{sessionId:scope.sessionId,workspaceId:scope.workspaceId},snapshots:[]};
        if(!owner.snapshots.includes(p.snapshot_id))owner.snapshots.push(p.snapshot_id);
      }
    });return row;
  }
  async studyHistory(scope,{offset=0,limit=10}={}) {
    scope=scopeOf(scope);await this.#ensure();
    if(!Number.isSafeInteger(offset)||offset<0||!Number.isSafeInteger(limit)||limit<1||limit>20)fail('invalid_page','Study history requires offset>=0 and limit 1..20');
    const bindings=Object.values(this.#state.studyCalls).filter(b=>b.owner===ownerKey(scope)).reverse();const rows=[];
    for(const b of bindings.slice(offset,offset+limit)){
      if(!b.studyId&&['prepared','rejected'].includes(b.phase)){rows.push({study_id:null,execution_status:b.phase==='prepared'?'not_submitted':'submission_rejected',confirmed_stopped:true});continue;}
      try{
        if(!b.studyId){
          const found=this.#studyIdentity(await this.#http('GET',`/v1/studies/by-request/${encodeURIComponent(b.request.request_id)}`),b);
          await this.#mutate(()=>{b.studyId=found.study_id;b.phase='bound';});
        }
        const r=await this.studyStatus(scope,b.studyId);
        rows.push(Object.fromEntries(['study_id','execution_status','cancel_requested','completed_points','valid_points','failed_points','automatic_replay','artifacts'].map(k=>[k,r[k]])));
        rows.at(-1).point_count=r.points.length;
        rows.at(-1).point_status_counts=Object.fromEntries([...new Set(r.points.map(p=>p.execution_status))].map(k=>[k,r.points.filter(p=>p.execution_status===k).length]));
      }catch(e){rows.push({study_id:b.studyId,execution_status:'unknown',lookup_error:e.code,confirmed_stopped:false});}
    }
    return {history:true,studies:rows,total:bindings.length,offset,limit,next_offset:offset+rows.length<bindings.length?offset+rows.length:null,recomputed:false,
      guidance:'Only existing Session-owned bindings were read. An interrupted tool reply alone does not prove that no task was created or executed.'};
  }
  async cancelStudy(scope,id) {
    scope=scopeOf(scope);await this.#ensure();const b=this.#study(scope,id);
    await this.#mutate(()=>{b.cancelRequested=true;});
    const row=this.#studyIdentity(await this.#http('POST',`/v1/studies/${encodeURIComponent(id)}/cancel`,{}),b);
    await this.#mutate(()=>{b.status=row;});return {...row,confirmed_stopped:TERMINAL.has(row.execution_status)};
  }
  async waitStudy(scope,id,{signal}={}) {
    scope=scopeOf(scope);await this.#ensure();const b=this.#study(scope,id);let sent=false;
    for(;;){
      if((signal?.aborted||b.cancelRequested)&&!sent){await this.cancelStudy(scope,id);sent=true;}
      const row=await this.studyStatus(scope,id);
      if(TERMINAL.has(row.execution_status))return {...row,confirmed_stopped:true};
      await delay(this.#poll);
    }
  }
  async studyArtifact(scope,id,name) {
    scope=scopeOf(scope);await this.#ensure();this.#study(scope,id);
    const row=await this.studyStatus(scope,id);
    if(!TERMINAL.has(row.execution_status)||!['report.html','result.json','points.csv'].includes(name)||!row.artifacts.includes(name))fail('artifact_unavailable','Study artifact is unavailable');
    const value=await this.#http('GET',`/v1/studies/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(name)}`,undefined,true);
    return {...value,name,taskId:id,sha256:digest(value.bytes),bytesLength:value.bytes.length};
  }
  async adoptStudyChildren(scope,id) {
    scope=scopeOf(scope);const parent=await this.studyStatus(scope,id);
    for(const p of parent.points){
      if(!p.task_id||!TERMINAL.has(p.execution_status))continue;
      const task=await this.#http('GET',`/v1/jobs/${encodeURIComponent(p.task_id)}`);
      if(task.task_id!==p.task_id||task.request_id!==id+'-p'+p.index||task.request_id!==p.child_request_id||!ID.test(task.attempt_id??''))fail('response_identity_mismatch','Study child identity differs');
      let result;
      try{result=await this.#http('GET',`/v1/jobs/${encodeURIComponent(p.task_id)}/result`);}
      catch(error){if(error.status!==409)throw error;}
      if(result&&(result.task_id!==p.task_id||result.request_id!==task.request_id||result.request_digest!==p.request_digest||result.request?.model?.snapshot_id!==p.snapshot_id))fail('response_identity_mismatch','Study child result binding differs');
      await this.#mutate(()=>{
        const key='study-child:'+id+':'+p.index;const old=this.#state.tasks[p.task_id];
        if(old&&old.owner!==ownerKey(scope))fail('task_access_denied','Study child already belongs to another owner');
        const request=result?.request??{request_id:task.request_id};
        const b=old??{owner:ownerKey(scope),scope,request,contentHash:digest(canonical(request)),phase:'bound',taskId:p.task_id,attemptId:task.attempt_id,cancelRequested:false,cursor:0,created_at:now(),studyId:id};
        b.status=task;this.#state.calls[key]=b;this.#state.tasks[p.task_id]=b;
        const owner=this.#state.owners[ownerKey(scope)]??={scope:{sessionId:scope.sessionId,workspaceId:scope.workspaceId},snapshots:[]};
        if(!owner.snapshots.includes(p.snapshot_id))owner.snapshots.push(p.snapshot_id);
      });
    }
    return parent;
  }
  async submit(scope, request) {
    scope = scopeOf(scope); const content = validateRequest(request); await this.#ensure();
    if (content.model.snapshot_id && !this.#owner(scope)?.snapshots.includes(content.model.snapshot_id)) fail('model_access_denied', 'Snapshot was not imported by this session and workspace');
    const key = callKey(scope);
    const prior = this.#state.calls[key];
    // After a failed shutdown, the exact original Host call may explicitly find
    // its already-sent request again. A new/prepared call must never create work
    // while draining. Quarantined replies are not a recoverable identity.
    if (this.#draining && (!prior || !['bound', 'submission_unknown'].includes(prior.phase))) fail('bridge_draining', 'Host is stopping this plugin; no new calculation may be submitted');
    if (this.#inflight.has(key)) {
      const existing = this.#inflight.get(key);
      if (existing.contentHash !== digest(canonical(content))) fail('call_request_conflict', 'This Host call is already bound to another request');
      return existing.promise;
    }
    const operation = this.#submit(scope, key, content);
    this.#inflight.set(key, { contentHash: digest(canonical(content)), promise: operation });
    try { return await operation; } finally { this.#inflight.delete(key); }
  }
  async #submit(scope, key, content) {
    const binding = await this.#mutate(() => {
      const hash = digest(canonical(content)); const old = this.#state.calls[key];
      if (old) { if (old.contentHash !== hash || old.owner !== ownerKey(scope)) fail('call_request_conflict', 'This Host call is already bound to another request'); return old; }
      return this.#state.calls[key] = { owner: ownerKey(scope), scope, contentHash: hash, request: { ...content, request_id: 'harness-' + randomBytes(24).toString('hex') }, phase: 'prepared', taskId: null, attemptId: null, cancelRequested: false, cursor: 0, created_at: now() };
    });
    if (binding.phase === 'quarantined') fail('submission_quarantined', 'The original submission returned an untrusted identity; no automatic replay or task access is allowed');
    if (binding.taskId) return this.status(scope, binding.taskId);
    if (binding.phase === 'rejected') fail('request_previously_rejected', 'This call was rejected; submit a corrected request using a new Host call');
    return this.#recoverBinding(scope, binding, key);
  }
  async #recoverBinding(scope, binding, key) {
    let lastError;
    for (let attempt = 0; attempt < this.#maxRecovery; attempt++) {
      await this.#mutate(() => { binding.phase = 'submission_unknown'; binding.last_submit_at = now(); });
      try {
        const row = this.#identity(await this.#http('POST', '/v1/jobs', binding.request), binding, { submitting: true });
        await this.#mutate(() => {
          const prior = this.#state.tasks[row.task_id];
          if (prior && (prior.owner !== binding.owner || prior.request.request_id !== binding.request.request_id)) fail('response_identity_mismatch', 'Task already belongs to another persisted binding');
          binding.taskId = row.task_id; binding.attemptId = row.attempt_id; binding.phase = 'bound'; binding.status = clone(row);
          this.#state.tasks[row.task_id] = binding;
        });
        return row;
      } catch (error) {
        if (!error.responseUnknown) {
          // Identity mismatches remain quarantined, never bind/claim another task.
          await this.#mutate(() => { binding.phase = error.code === 'service_rejected' ? 'rejected' : 'quarantined'; binding.error = error.code; });
          throw error;
        }
        lastError = error; if (attempt + 1 < this.#maxRecovery) await delay(this.#poll);
      }
    }
    throw new BridgeError('submission_unknown', 'Submission may have been accepted. The original request is retained; use the same Host call to recover, never create a replacement automatically', { callKey: key, responseUnknown: true });
  }
  async status(scope, taskId) {
    scope = scopeOf(scope); await this.#ensure(); const binding = this.#task(scope, taskId);
    const row = this.#identity(await this.#http('GET', `/v1/jobs/${encodeURIComponent(taskId)}`), binding);
    await this.#mutate(() => { binding.status = clone(row); });
    return row;
  }
  async events(scope, taskId, after = undefined) {
    scope = scopeOf(scope); await this.#ensure(); const binding = this.#task(scope, taskId);
    const cursor = after ?? binding.cursor ?? 0;
    if (!Number.isSafeInteger(cursor) || cursor < 0) fail('invalid_cursor', 'Event cursor must be a nonnegative safe integer');
    const value = await this.#http('GET', `/v1/jobs/${encodeURIComponent(taskId)}/events?after=${cursor}`);
    if (!plain(value) || value.task_id !== taskId || !Array.isArray(value.events) || value.events.length > 250 || !Number.isSafeInteger(value.cursor) || value.cursor < cursor) fail('response_identity_mismatch', 'Invalid event page');
    let previous = cursor;
    for (const event of value.events) {
      if (!plain(event) || !Number.isSafeInteger(event.seq) || event.seq <= previous || event.data?.attempt_id && event.data.attempt_id !== binding.attemptId) fail('response_identity_mismatch', 'Event sequence or attempt identity differs from the binding');
      previous = event.seq;
    }
    if (value.cursor !== previous) fail('response_identity_mismatch', 'Event cursor does not match the page');
    await this.#mutate(() => { binding.cursor = Math.max(binding.cursor ?? 0, value.cursor); });
    return value;
  }
  async cancel(scope, taskId) {
    scope = scopeOf(scope); await this.#ensure(); const binding = this.#task(scope, taskId);
    await this.#mutate(() => { binding.cancelRequested = true; });
    const row = this.#identity(await this.#http('POST', `/v1/jobs/${encodeURIComponent(taskId)}/cancel`, {}), binding);
    const stopped = TERMINAL.has(row.execution_status);
    if (row.confirmed_stopped !== stopped) fail('response_identity_mismatch', 'Cancellation confirmation differs from the actual execution state');
    await this.#mutate(() => { binding.status = clone(row); });
    return { ...row, cancellation_accepted: true, confirmed_stopped: stopped };
  }
  async result(scope, taskId) {
    scope = scopeOf(scope); await this.#ensure(); const binding = this.#task(scope, taskId);
    const value = await this.#http('GET', `/v1/jobs/${encodeURIComponent(taskId)}/result`);
    // The core normalizes optional dataclass fields (for example explicit null
    // retry_solver and rule terminals). Identity remains the immutable server
    // request/task/attempt triple, not byte equality with pre-normalized JSON.
    if (!plain(value) || value.task_id !== taskId || value.attempt_id !== binding.attemptId || value.request?.request_id !== binding.request.request_id) fail('response_identity_mismatch', 'Committed result does not match the bound request/task/attempt');
    return value;
  }
  async artifact(scope, taskId, name) {
    scope = scopeOf(scope); await this.#ensure(); this.#task(scope, taskId);
    if (!ARTIFACTS.has(name)) fail('artifact_access_denied', 'Only committed professional report artifacts may be read');
    const status = await this.status(scope, taskId);
    if (!TERMINAL.has(status.execution_status) || !status.artifacts?.includes(name)) fail('artifact_unavailable', 'Artifact is not listed in the committed task');
    const value = await this.#http('GET', `/v1/jobs/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(name)}`, undefined, true);
    return { ...value, name, taskId, sha256: digest(value.bytes), bytesLength: value.bytes.length };
  }
  async history(scope) {
    scope = scopeOf(scope); await this.#ensure();
    // Pure ledger reads; no HTTP calls, POSTs, implicit retry or solver replay.
    return Object.values(this.#state.calls).filter(binding => binding.owner === ownerKey(scope)).map(binding => ({ task_id: binding.taskId, request_id: binding.request.request_id, attempt_id: binding.attemptId, phase: binding.phase, cancel_requested: binding.cancelRequested, cursor: binding.cursor, created_at: binding.created_at, status: binding.status ? clone(binding.status) : null }));
  }
  async wait(scope, taskId, { signal, onEvent } = {}) {
    scope = scopeOf(scope); await this.#ensure(); const binding = this.#task(scope, taskId);
    const waiting = this.#wait(scope, taskId, binding, signal, onEvent);
    this.#waits.add(waiting); try { return await waiting; } finally { this.#waits.delete(waiting); }
  }
  async #wait(scope, taskId, binding, signal, onEvent) {
    let cancellationSent = false;
    let deliveryFailures = 0;
    const deliver = async events => {
      if (!onEvent) return;
      for (const event of events) {
        try { await onEvent(event); }
        catch { deliveryFailures = Math.min(1000000, deliveryFailures + 1); }
      }
    };
    for (;;) {
      if (signal?.aborted || binding.cancelRequested) {
        if (!cancellationSent) { await this.cancel(scope, taskId); cancellationSent = true; }
      }
      const page = await this.events(scope, taskId);
      // A presentation callback cannot sever task ownership or suppress abort.
      // Raw callback errors are never reflected into model-visible output.
      await deliver(page.events);
      const status = await this.status(scope, taskId);
      if (TERMINAL.has(status.execution_status)) {
        // Drain remaining 250-row pages after terminal status before completion.
        // A terminal event may have committed between the preceding event read
        // and status read; always request at least one final page.
        let more = true;
        while (more) { const next = await this.events(scope, taskId); await deliver(next.events); more = next.events.length === 250; }
        return { ...status, confirmed_stopped: true, cancellation_requested: binding.cancelRequested || Boolean(signal?.aborted), ...(deliveryFailures ? { event_delivery_failures: deliveryFailures, projection_warning: 'Some execution events could not be displayed; task lifecycle tracking continued' } : {}) };
      }
      await delay(this.#poll);
    }
  }
  async submitAndWait(scope, request, { signal, onEvent } = {}) {
    scope = scopeOf(scope); await this.#ensure();
    if (signal?.aborted && !this.#state.calls[callKey(scope)]) return { execution_status: 'cancelled', confirmed_stopped: true, submitted: false };
    const recordAbort = () => this.#mutate(() => { const binding = this.#state.calls[callKey(scope)]; if (binding) binding.cancelRequested = true; }).catch(() => {});
    signal?.addEventListener('abort', recordAbort, { once: true });
    try {
      let status;
      try { status = await this.submit(scope, request); }
      catch (error) {
        if (signal?.aborted) {
          const binding = this.#state.calls[callKey(scope)];
          if (binding && !binding.taskId && binding.phase === 'submission_unknown') {
            // Even after abort, recover only the original accepted identity so
            // it can be cancelled. Failure stays explicitly unknown, not stopped.
            await recordAbort(); status = await this.#recoverBinding(scope, binding, callKey(scope));
          } else throw error;
        } else throw error;
      }
      return await this.wait(scope, status.task_id, { signal, onEvent });
    } finally { signal?.removeEventListener('abort', recordAbort); }
  }
  async close() {
    if (this.#closing) return this.#closing;
    const operation = (async () => {
      await this.#ready.catch(() => {}); if (this.#closed) return;
      this.#draining = true;
      // Dispose only after in-flight writers have persisted their actual state.
      // This releases bridge transport ownership; it does not claim that a
      // numerical job stopped. The runtime must separately require confirmation.
      if (this.#drainPromise) await this.#drainPromise.catch(() => {});
      await Promise.allSettled([...this.#inflight.values()].map(value => value.promise));
      await this.#queue; this.#closed = true;
      if (this.#lock?.listening) await new Promise(resolve => this.#lock.close(resolve));
    })();
    this.#closing = operation;
    try { return await operation; }
    finally { if (!this.#closed && this.#closing === operation) this.#closing = undefined; }
  }
  async cancelOwnedPending() {
    await this.#ensure();
    if (this.#drainPromise) return this.#drainPromise;
    this.#draining = true;
    const operation = this.#cancelOwnedPending();
    this.#drainPromise = operation;
    try { return await operation; }
    finally { if (this.#drainPromise === operation) this.#drainPromise = undefined; }
  }
  async #cancelOwnedPending() {
    await this.#mutate(() => { for (const binding of Object.values(this.#state.calls)) if (!TERMINAL.has(binding.status?.execution_status)) binding.cancelRequested = true; });
    // An already-persisted submit may still be crossing the HTTP boundary.
    // Join it before enumerating tasks, otherwise shutdown could miss a job
    // accepted immediately after a prepared binding was inspected.
    await Promise.allSettled([...this.#inflight.values()].map(value => value.promise));
    const results = [];
    for(const b of Object.values(this.#state.studyCalls)){
      try{
        if(!b.studyId){
          if(['prepared','rejected'].includes(b.phase)){results.push({request_id:b.request.request_id,confirmed_stopped:true,submitted:false});continue;}
          const found=this.#studyIdentity(await this.#http('GET',`/v1/studies/by-request/${encodeURIComponent(b.request.request_id)}`),b);
          await this.#mutate(()=>{b.studyId=found.study_id;b.phase='bound';});
        }
        const status=await this.studyStatus(b.scope,b.studyId);
        if(!TERMINAL.has(status.execution_status)){await this.cancelStudy(b.scope,b.studyId);results.push(await this.waitStudy(b.scope,b.studyId));}
        else results.push({...status,confirmed_stopped:true});
      }catch(error){results.push({study_id:b.studyId,confirmed_stopped:false,error:error.code});}
    }
    for (const binding of Object.values(this.#state.calls)) {
      // A response with conflicting identity is never authority to query or
      // cancel that task. Even a half-bound local field must remain quarantined.
      if (binding.phase === 'quarantined') {
        results.push({ request_id: binding.request.request_id, confirmed_stopped: false, error: 'submission_quarantined' });
        continue;
      }
      if (!binding.taskId) {
        if (binding.phase === 'prepared' && !binding.last_submit_at) {
          results.push({ request_id: binding.request.request_id, submitted: false, execution_status: 'not_submitted', confirmed_stopped: true });
        } else if (binding.phase === 'rejected') {
          results.push({ request_id: binding.request.request_id, submission_rejected: true, numerical_job_accepted: false, execution_status: 'rejected', confirmed_stopped: true });
        } else {
          // Shutdown must not POST a request whose acceptance is unknown: that
          // could start work while trying to stop. Keep its original identity so
          // an explicit same-call recovery can resolve it, then retry shutdown.
          results.push({ request_id: binding.request.request_id, confirmed_stopped: false, error: binding.phase === 'submission_unknown' ? 'submission_unknown' : 'submission_unresolved' });
        }
        continue;
      }
      try {
        const status = await this.status(binding.scope, binding.taskId);
        // cancelRequested was persisted before enumeration. wait() sends the
        // cancellation once and keeps polling through the real terminal state.
        if (!TERMINAL.has(status.execution_status)) results.push(await this.wait(binding.scope, binding.taskId));
        else results.push({ ...status, confirmed_stopped: true });
      } catch (error) { results.push({ task_id: binding.taskId, confirmed_stopped: false, error: error.code }); }
    }
    return results;
  }
}

export default TianshuBridge;
