/** Tianshu native Harness tools. Copyright (c) yangshaopeng. */
import fs from 'node:fs/promises';
import { constants as fsConstants } from 'node:fs';
import path from 'node:path';
import { createHash, randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import z from '@deepseek-ai/schemastery';
import { z as projectionZ } from 'zod';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { createRuntime } from './lib/runtime.js';
import { exportNativeReport } from './lib/report-export.js';
import { resultOutput, importParameters, attachmentListParameters, runParameters, taskParameters, queryParameters, reportParameters, studyParameters, studyQueryParameters, comparisonParameters } from './schemas.js';
import { attachmentProjection, collectNativeAttachmentPackage, listSessionAttachments } from './lib/native-attachments.js';
import { normalizeWorkspaceSelection, rememberImportedModel, prepareDailyCall } from './lib/daily-workflow.js';
import { collectReferencedPackage, assertDependencyPackageRoot } from './lib/workspace-package.js';

export const name = 'tianshu-native-tools';
export const inject = ['tools', 'systemPrompt', 'sessionProjections', 'workingDirectory', 'attachments'];
export const Config = z.object({
  stateDir: z.string().default(''),
  python: z.string().default(''),
  coreRoot: z.string().default(''),
  casesRoot: z.string().default(''),
  connectionFile: z.string().default(''),
  engineeringReference: z.any(),
});

const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const packageIdentity = JSON.parse(await fs.readFile(fileURLToPath(new URL('./package.json', import.meta.url)), 'utf8'));
const prompt = await fs.readFile(fileURLToPath(new URL('./prompts/tianshu.md', import.meta.url)), 'utf8');
const MAX_IMPORT = 20 * 1024 * 1024;
const MAX_RESULT = 24000;
const TERMINAL = new Set(['finished', 'failed', 'cancelled', 'interrupted']);
const fail = (code, message) => { const error = new Error(message); error.code = code; throw error; };
const plain = value => value && typeof value === 'object' && !Array.isArray(value);

function exactArgs(args, names) {
  if (!plain(args) || Object.keys(args).some(key => !names.includes(key))) fail('invalid_tool_arguments', '工具参数含有未声明字段；会话和请求身份只能由 Host 提供。');
}
function normalizeCase(value) { return process.platform === 'win32' ? value.toLowerCase() : value; }
function relativeFile(value, allowDot = false) {
  if (allowDot && value === '.') return value;
  if (typeof value !== 'string' || !value || value.length > 240 || /[\\:\x00-\x1f<>"|?*%@$`]/.test(value) || value.startsWith('/') || value.split('/').some(p => !p || p === '.' || p === '..' || /[. ]$/.test(p))) fail('unsafe_input_path', '模型文件必须使用工作区内明确的相对 POSIX 路径，不能含父目录、链接或特殊路径。');
  return value;
}
function inside(root, filename) {
  const relative = path.relative(root, filename);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

/** Bind identity only to the live Host Session and registered turn projection. */
export async function trustedScope(ctx, exec) {
  const session = exec.agent?.session;
  const turn = session && ctx.sessionProjections.stateOf(session, 'turnBoundary');
  if (!session || typeof session.id !== 'string' || !session.id || !Number.isSafeInteger(turn?.openTurnStartSeq) || turn.openTurnStartSeq < 0 || !exec.callId || !exec.rootCallId || !exec.signal) fail('trusted_context_unavailable', '缺少真实会话、活动轮次或调用身份；不能执行专业工具。');
  const selectedRoot = session.header?.cwd;
  if (typeof selectedRoot !== 'string' || !path.isAbsolute(selectedRoot)) fail('workspace_unavailable', '本会话没有明确工作区，请先选择工作区。');
  const workspaceRoot = await fs.realpath(selectedRoot);
  if (!(await fs.stat(workspaceRoot)).isDirectory()) fail('workspace_unavailable', '工作区已不可用。');
  return Object.freeze({
    sessionId: session.id,
    workspaceId: sha256(normalizeCase(workspaceRoot)),
    turnId: String(turn.openTurnStartSeq), callId: String(exec.callId), rootCallId: String(exec.rootCallId),
    workspaceRoot,
  });
}

async function unlinkedPath(root, filename) {
  if (!inside(root, filename)) fail('unsafe_input_path', '文件超出本会话工作区。');
  const relative = path.relative(root, filename);
  let current = root;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    if ((await fs.lstat(current)).isSymbolicLink()) fail('unsafe_input_path', '模型包不能经过符号链接或目录联接。');
  }
  if (normalizeCase(await fs.realpath(filename)) !== normalizeCase(filename)) fail('unsafe_input_path', '模型路径解析结果与明确选择的路径不一致。');
}

/** Bounded immutable copy of only the individually authorized package files. */
export async function collectWorkspaceFiles(workspaceRoot, args, signal, maximumBytes = MAX_IMPORT) {
  if (!Number.isSafeInteger(maximumBytes) || maximumBytes < 1 || maximumBytes > MAX_IMPORT) fail('invalid_model_package', '文件读取预算无效。');
  const packageRoot = path.resolve(workspaceRoot, relativeFile(args.package_root, true));
  if (!inside(workspaceRoot, packageRoot) || !Array.isArray(args.files) || args.files.length < 1 || args.files.length > 500) fail('invalid_model_package', '需逐项列出 1–500 个工作区文件。');
  relativeFile(args.master_file);
  const names = args.files.map(value => relativeFile(value));
  if (!names.includes(args.master_file) || new Set(names.map(normalizeCase)).size !== names.length) fail('invalid_model_package', '文件列表必须包含主文件且没有重复或大小写别名。');
  await unlinkedPath(workspaceRoot, packageRoot);
  const files = [], inventory = [];
  let total = 0;
  for (const relative of names) {
    signal.throwIfAborted();
    const filename = path.resolve(packageRoot, relative);
    await unlinkedPath(workspaceRoot, filename);
    const handle = await fs.open(filename, fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0));
    try {
      const initial = await handle.stat();
      if (!initial.isFile() || initial.nlink !== 1 || initial.size > maximumBytes - total) fail('invalid_model_package', '只允许无硬链接的普通 UTF-8 文件，模型包总计最多 20 MiB。');
      const chunks = [];
      let fileBytes = 0;
      for (;;) {
        signal.throwIfAborted();
        const buffer = Buffer.alloc(Math.min(65536, maximumBytes - total - fileBytes + 1));
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
        if (!bytesRead) break;
        fileBytes += bytesRead;
        if (total + fileBytes > maximumBytes) fail('invalid_model_package', '模型包超过 20 MiB。');
        chunks.push(buffer.subarray(0, bytesRead));
      }
      const final = await handle.stat();
      await unlinkedPath(workspaceRoot, filename);
      const current = await fs.stat(filename);
      if (initial.size !== final.size || initial.mtimeMs !== final.mtimeMs || initial.ctimeMs !== final.ctimeMs || initial.ino !== current.ino || initial.dev !== current.dev || final.nlink !== 1) fail('input_changed', '读取时文件发生变化，请重新选择固定模型包。');
      const bytes = Buffer.concat(chunks);
      const content = bytes.toString('utf8');
      if (!Buffer.from(content, 'utf8').equals(bytes) || content.includes('\0')) fail('invalid_model_package', '模型文件必须是有效 UTF-8 文本。');
      files.push({ path: relative, content_utf8: content });
      inventory.push({ path: relative, bytes: bytes.length, sha256: sha256(bytes) });
      total += bytes.length;
    } finally { await handle.close(); }
  }
  if (args.format === 'MATPOWER') {
    if (files.length !== 1 || !Number.isFinite(args.frequency_hz) || !plain(args.source_impedance_ohm)) fail('invalid_model_package', 'MATPOWER 需单个 .m 文件、明确频率和电源阻抗。');
    return { payload: { format: 'MATPOWER', source_file: args.master_file, content_utf8: files[0].content_utf8, frequency_hz: args.frequency_hz, source_impedance_ohm: args.source_impedance_ohm }, inventory };
  }
  if (['CSV', 'JSON'].includes(args.format)) {
    if (files.length !== 1 || args.frequency_hz !== undefined || args.source_impedance_ohm !== undefined) fail('invalid_model_package', 'CSV/JSON需一个明确选择的标准模型表，频率及电源参数由模型给出。');
    return { payload: { format: args.format, master_file: args.master_file, files }, inventory };
  }
  if (args.format !== 'OpenDSS' || args.frequency_hz !== undefined || args.source_impedance_ohm !== undefined) fail('invalid_model_package', 'OpenDSS 包直接使用源文件声明；频率和电源阻抗导入参数只用于 MATPOWER。');
  return { payload: { master_file: args.master_file, files }, inventory };
}

function smallString(value, maximum = 600) { return typeof value === 'string' ? value.slice(0, maximum) : value; }
function boundedData(value, budget = MAX_RESULT) {
  const text = JSON.stringify(value);
  // Backend records have optional fields. Project them to actual JSON before
  // the SDK's lossless JSON validator, never return undefined object values.
  if (text.length <= budget) return JSON.parse(text);
  return {
    ...Object.fromEntries(['task_id', 'attempt_id', 'snapshot_id', 'execution_status', 'numerical_validity', 'url'].filter(key => typeof value[key] === 'string').map(key => [key, value[key]])),
    engineering_ready: false, truncated: true, reason: 'detail_budget_exceeded',
    guidance: '请使用更精确的 object_id 或更小 limit 分页查询；完整数据见报告。',
  };
}
function resultValue(operation, summary, data) { return { operation, summary, data: boundedData(data) }; }
function render(_args, value) {
  return [{ type: 'text', text: `${value.summary}\n${JSON.stringify(value.data)}` }];
}
const ENGINEERING_FILTERS = ['metric', 'unit', 'measurement_basis', 'phase', 'node', 'terminal', 'conductor', 'criteria_rule_id'];
const ENGINEERING_STATUSES = new Set(['available', 'not_assessed', 'not_applicable']);
const SUMMARY_METRICS = new Set(['network_active_loss_kw', 'network_net_reactive_absorption_kvar', 'native_minimum_voltage_pu', 'native_maximum_voltage_pu',
  'maximum_line_current_loading_percent', 'maximum_transformer_current_loading_percent', 'maximum_transformer_kva_loading_percent']);
const METRIC_FIELDS = ['metric', 'value', 'unit', 'measurement_basis', 'status', 'reason', 'object_id', 'node', 'reference_node', 'phase', 'terminal', 'conductor',
  'base_voltage_v', 'base_source', 'rating', 'rating_unit', 'criteria_rule_id', 'rule_source', 'scope', 'formula', 'included_element_classes', 'excluded_element_classes',
  'not_an_engineering_compliance_conclusion'];

function engineeringMetricSource(result) {
  const analysis = result.engineering_analysis;
  if (!plain(analysis) || !Array.isArray(analysis.metrics)) return { status: 'not_assessed', reason: 'recorded_engineering_metrics_missing', rows: [] };
  if (analysis.task_id !== result.task_id || result.numerical_validity !== 'converged_valid' || analysis.numerical_validity !== result.numerical_validity) {
    return { status: 'not_assessed', reason: 'engineering_metric_task_or_numerical_identity_mismatch', rows: [] };
  }
  if (analysis.metrics.some(row => !plain(row) || typeof row.metric !== 'string')) return { status: 'not_assessed', reason: 'recorded_engineering_metric_structure_invalid', rows: [] };
  return { status: 'available', reason: 'recorded_engineering_analysis_metrics_only', rows: analysis.metrics };
}

/** Bounded projection of one recorded metric, preserving its explicit basis. */
function metricProjection(row, index, evidenceOffset = 0, evidenceLimit = 8, compact = false) {
  const result = {};
  const shortened = {};
  const textLimit = compact ? 256 : 1024;
  const refLimit = compact ? Math.min(evidenceLimit, 4) : evidenceLimit;
  for (const key of METRIC_FIELDS) {
    if (!Object.hasOwn(row, key)) continue;
    const value = row[key];
    if (typeof value === 'string' && value.length > textLimit) { result[key] = value.slice(0, textLimit); shortened[key] = value.length; }
    else if (Array.isArray(value)) {
      result[key] = value.slice(0, 20).map(item => typeof item === 'string' ? item.slice(0, 256) : null);
      if (value.length > 20 || value.some(item => typeof item !== 'string' || item.length > 256)) shortened[key] = value.length;
    } else if (plain(value)) { result[key] = null; shortened[key] = 'compound_record_metadata_not_projected'; }
    else result[key] = value;
  }
  const missing = [];
  if (typeof row.value !== 'number' || !Number.isFinite(row.value)) { result.value = null; missing.push('finite_value'); }
  if (typeof row.unit !== 'string' || !row.unit) { result.unit = null; missing.push('unit'); }
  if (typeof row.measurement_basis !== 'string' || !row.measurement_basis) { result.measurement_basis = null; missing.push('measurement_basis'); }
  if (!ENGINEERING_STATUSES.has(row.status)) missing.push('metric_status');
  const refs = Array.isArray(row.evidence_refs) ? row.evidence_refs : [];
  if (!Array.isArray(row.evidence_refs) || refs.some(value => typeof value !== 'string')) missing.push('evidence_refs');
  if (missing.length) {
    if (row.status !== undefined) result.recorded_status = typeof row.status === 'string' ? row.status.slice(0, textLimit) : null;
    if (row.reason !== undefined) result.recorded_reason = typeof row.reason === 'string' ? row.reason.slice(0, textLimit) : null;
    result.status = 'not_assessed'; result.reason = 'recorded_metric_fields_missing_or_invalid'; result.missing_recorded_fields = missing;
  }
  result.evidence_refs = refs.slice(evidenceOffset, evidenceOffset + refLimit).map(value => typeof value === 'string' ? value.slice(0, textLimit) : null);
  result.evidence_ref_count = refs.length; result.evidence_offset = evidenceOffset;
  result.next_evidence_offset = evidenceOffset + refLimit < refs.length ? evidenceOffset + refLimit : null;
  result.evidence_refs_truncated = evidenceOffset > 0 || evidenceOffset + refLimit < refs.length;
  if (refs.slice(evidenceOffset, evidenceOffset + refLimit).some(value => typeof value === 'string' && value.length > textLimit)) shortened.evidence_refs = 'reference_text_exceeds_projection_budget';
  result.source_json_pointer = `result.json#/engineering_analysis/metrics/${index}`;
  result.source_domain = 'engineering_analysis.metrics';
  if (Object.keys(shortened).length) result.truncated_record_fields = shortened;
  if (compact) result.projection_compact = true;
  return result;
}

function engineeringSummary(result) {
  const source = engineeringMetricSource(result), rows = [];
  let matching = 0;
  for (let index = 0; index < source.rows.length; index++) {
    if (!SUMMARY_METRICS.has(source.rows[index].metric)) continue;
    matching++;
    const row = metricProjection(source.rows[index], index, 0, 1);
    if (rows.length < 8 && JSON.stringify([...rows, row]).length <= 8000) rows.push(row);
  }
  return { engineering_metrics_source_status: source.status, engineering_metrics_source_reason: source.reason,
    engineering_metric_count: source.rows.length, engineering_metrics_summary: rows, engineering_metrics_summary_truncated: matching > rows.length };
}

function queryEngineeringMetrics(result, args, offset, limit) {
  for (const key of ['metric', 'object_id', 'unit', 'measurement_basis', 'phase', 'criteria_rule_id']) {
    if (args[key] !== undefined && (typeof args[key] !== 'string' || !args[key] || args[key].length > 1024)) fail('invalid_metric_filter', `${key} 必须是有界的精确字符串。`);
  }
  for (const key of ['node', 'terminal', 'conductor']) if (args[key] !== undefined && (!Number.isSafeInteger(args[key]) || args[key] < (key === 'node' ? 0 : 1))) fail('invalid_metric_filter', `${key} 必须是明确的有效整数。`);
  if (args.status !== undefined && !ENGINEERING_STATUSES.has(args.status)) fail('invalid_metric_filter', '工程量状态只能筛选 available/not_assessed/not_applicable，不能混用规则通过/越限状态。');
  const evidenceOffset = args.evidence_offset ?? 0, evidenceLimit = args.evidence_limit ?? 8;
  if (!Number.isSafeInteger(evidenceOffset) || evidenceOffset < 0 || !Number.isSafeInteger(evidenceLimit) || evidenceLimit < 1 || evidenceLimit > 20) fail('invalid_pagination', 'evidence_offset 必须非负，evidence_limit 必须为1–20。');
  const source = engineeringMetricSource(result), matching = [];
  for (let index = 0; index < source.rows.length; index++) {
    const row = source.rows[index];
    if (['object_id', ...ENGINEERING_FILTERS].some(key => args[key] !== undefined && row[key] !== args[key])) continue;
    const projected = metricProjection(row, index, evidenceOffset, evidenceLimit);
    if (args.status !== undefined && projected.status !== args.status) continue;
    matching.push(projected);
  }
  const rows = [];
  for (const item of matching.slice(offset, offset + limit)) {
    let row = item;
    if (rows.length === 0 && JSON.stringify([row]).length > MAX_RESULT - 3000) {
      const index = Number(row.source_json_pointer.split('/').at(-1));
      row = metricProjection(source.rows[index], index, evidenceOffset, evidenceLimit, true);
    }
    if (JSON.stringify([...rows, row]).length > MAX_RESULT - 3000) break;
    rows.push(row);
  }
  return { task_id: result.task_id, attempt_id: result.attempt_id, view: 'engineering_metrics', source_domain: 'engineering_analysis.metrics',
    source_status: source.status, source_reason: source.reason, source_metric_count: source.rows.length,
    query_status: source.status !== 'available' || matching.length === 0 ? 'not_assessed' : rows.length ? 'recorded_metrics' : 'empty_page',
    query_reason: source.status !== 'available' ? source.reason : matching.length === 0 ? 'no_recorded_metrics_match_exact_filters' : null,
    total: matching.length, offset, limit, returned_count: rows.length, next_offset: offset + rows.length < matching.length ? offset + rows.length : null,
    page_limited_by_response_budget: rows.length < Math.min(limit, Math.max(0, matching.length - offset)), rows,
    numerical_validity: result.numerical_validity, overall_compliance: result.assessment?.overall_compliance ?? 'not_assessed', engineering_ready: false };
}

export function summarizeResult(result) {
  return {
    task_id: result.task_id, attempt_id: result.attempt_id,
    execution_status: result.execution_status, numerical_validity: result.numerical_validity,
    publishable: result.publishable === true, engineering_publishable: result.engineering_publishable === true,
    engineering_assessment: result.assessment?.status ?? 'not_assessed',
    overall_compliance: result.assessment?.overall_compliance ?? 'not_assessed',
    assessed_record_counts: result.assessment?.counts ?? {},
    criteria_id: result.assessment?.criteria_id ?? null,
    criteria_digest: result.assessment?.criteria_digest ?? null,
    observation_count: result.professional_observations?.observations?.length ?? 0,
    engineering_ready: false,
    error: result.error ? { code: result.error.code, message: smallString(result.error.message) } : null,
    limitations: (result.limitations ?? []).map(value => smallString(value, 300)).slice(0, 12),
    ...engineeringSummary(result),
  };
}
export function queryResult(result, args) {
  const view = args.view ?? 'summary';
  if (view === 'summary') {
    if (['object_id', 'status', ...ENGINEERING_FILTERS, 'evidence_offset', 'evidence_limit'].some(key => args[key] !== undefined)) fail('invalid_result_view', 'summary 不接受明细筛选；具体工程量请指定 engineering_metrics 视图。');
    return summarizeResult(result);
  }
  const offset = args.offset ?? 0, limit = args.limit ?? 10;
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 20) fail('invalid_pagination', 'offset 必须非负，limit 必须为 1–20。');
  if (view === 'engineering_metrics') return queryEngineeringMetrics(result, args, offset, limit);
  const allowed = new Set(['assessment', 'observations', 'bus_nodes', 'element_terminals']);
  if (!allowed.has(view)) fail('invalid_result_view', '不支持此结果视图。');
  if ([...ENGINEERING_FILTERS, 'evidence_offset', 'evidence_limit'].some(key => args[key] !== undefined)) fail('invalid_result_view', '工程量筛选与证据分页只用于 engineering_metrics 视图，不能跨数据域套用。');
  if (args.status && view !== 'assessment') fail('invalid_result_view', 'status 过滤只适用于 assessment。');
  if (args.status && !['pass', 'violation', 'not_assessed', 'not_applicable'].includes(args.status)) fail('invalid_metric_filter', '规则状态不能混用工程量 available 状态。');
  const source = view === 'assessment' ? result.assessment?.items : result.professional_observations?.[view];
  const rows = (source ?? []).filter(row => (!args.object_id || row.object_id === args.object_id || row.bus_id === args.object_id || row.element_id === args.object_id) && (!args.status || row.status === args.status));
  return { task_id: result.task_id, attempt_id: result.attempt_id, view, total: rows.length, offset, limit, next_offset: offset + limit < rows.length ? offset + limit : null, rows: rows.slice(offset, offset + limit), numerical_validity: result.numerical_validity, overall_compliance: result.assessment?.overall_compliance ?? 'not_assessed', engineering_ready: false };
}

function hostConfig(config) {
  for (const key of ['stateDir', 'python', 'coreRoot', 'casesRoot']) if (typeof config[key] !== 'string' || !path.isAbsolute(config[key])) fail('host_configuration_required', '天智五号插件尚未配置专业核心及私有状态目录，请按安装说明设置 Host 配置。');
  return Object.fromEntries(['stateDir', 'python', 'coreRoot', 'casesRoot', 'connectionFile'].filter(key => config[key]).map(key => [key, config[key]]));
}

function canonicalJson(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value) && !Object.is(value, -0)) return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
  if (plain(value)) return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
  fail('invalid_run_request', '计算请求必须是有界、有限数值的规范 JSON。');
}
function callIdentity(scope) {
  return Object.fromEntries(['sessionId', 'workspaceId', 'turnId', 'callId', 'rootCallId'].map(key => [key, scope[key]]));
}
async function privateApprovalDirectory(config) {
  const state = await fs.realpath(config.stateDir);
  const directory = path.join(state, 'approvals');
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  if ((await fs.lstat(directory)).isSymbolicLink() || normalizeCase(await fs.realpath(directory)) !== normalizeCase(directory)) fail('unsafe_approval_store', '审批记录目录不能是链接。');
  return directory;
}
async function atomicApproval(filename, value) {
  const temporary = `${filename}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`;
  const handle = await fs.open(temporary, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(value, null, 2) + '\n'); await handle.sync(); }
  finally { await handle.close(); }
  await fs.rename(temporary, filename);
}
async function assertNotPrivateInput(config, scope, args) {
  const state = await fs.realpath(config.stateDir);
  const connection = config.connectionFile ? await fs.realpath(config.connectionFile) : null;
  const packageRoot = path.resolve(scope.workspaceRoot, relativeFile(args.package_root, true));
  for (const name of args.files) {
    relativeFile(name);
    let selected;
    try { selected = await fs.realpath(path.resolve(packageRoot, name)); }
    catch (error) {
      if (error.code === 'ENOENT' || error.code === 'ENOTDIR') fail('model_dependency_missing', `模型文件缺失：${name}。请把该引用文件放回模型包目录，或提供完整算例目录。`);
      throw error;
    }
    if (inside(state, selected)) fail('private_input_denied', '插件私有状态、审批与服务凭据不能作为模型输入读取。');
    if (connection && normalizeCase(connection) === normalizeCase(selected)) fail('private_input_denied', '已配置的专业服务连接文件不能作为模型依赖读取。');
  }
}

/** Persisted native authorization, not authentication of confirmed_by metadata. */
function runAuthorizer(ctx, config) {
  const inFlight = new Map();
  return async (request, exec, scope, declarationProvenance = null) => {
    const canonical = canonicalJson(request);
    if (Buffer.byteLength(canonical) > 250 * 1024) fail('invalid_run_request', '计算请求超过 250 KiB。');
    const requestHash = sha256(canonical);
    const identity = callIdentity(scope);
    const callHash = sha256(canonicalJson(identity));
    const active = inFlight.get(callHash);
    if (active) {
      if (active.requestHash !== requestHash) fail('run_authorization_conflict', '同一 Host 调用的请求已改变，不能复用原授权。');
      return active.promise;
    }
    const promise = (async () => {
      const directory = await privateApprovalDirectory(config);
      const filename = path.join(directory, `${callHash}.json`);
      let record;
      try {
        const info = await fs.lstat(filename);
        if (!info.isFile() || info.isSymbolicLink() || info.size > 64 * 1024) fail('unsafe_approval_store', '审批记录不是有界普通文件。');
        record = JSON.parse(await fs.readFile(filename, 'utf8'));
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (record) {
        if (record.schema !== 'tianshu-run-authorization/1' || record.call_sha256 !== callHash || canonicalJson(record.scope) !== canonicalJson(identity) || record.request_sha256 !== requestHash) fail('run_authorization_conflict', '同一 Host 调用的请求或归属已改变，不能复用原授权。');
        if (record.outcome === 'denied') fail('run_authorization_denied', '此 Host 调用的计算授权已被拒绝；未提交计算。');
        if (record.outcome === 'allowed-once') return { filename, record, reused: true };
        // A crash between a UI decision and its durable receipt cannot imply
        // acceptance. Ask the same native channel again, never submit blindly.
      }
      exec.signal.throwIfAborted();
      const approval = ctx.get('approval');
      if (!approval) fail('run_authorization_unavailable', '缺少原生计算确认通道；未提交计算。');
      record = { schema: 'tianshu-run-authorization/1', call_sha256: callHash, scope: identity, request_sha256: requestHash,
        outcome: 'pending', prepared_at: new Date().toISOString(), engineering_accepted: false,
        authorization_semantics: 'Native Harness calculation authorization only; neither confirmer identity nor engineering compliance is authenticated.' };
      if (declarationProvenance) record.declaration_provenance = declarationProvenance;
      await atomicApproval(filename, record);
      const criteria = request.criteria === null ? '未提供判据：criteria:null，缺依据项保持未评估。' : canonicalJson(request.criteria);
      const reason = declarationProvenance
        ? `按所选模型现有工况计算一次稳态潮流，不修改负荷、电源、开关或模型参数。\n模型：${canonicalJson(request.model)}\n运行频率：${request.operating_conditions.frequency_hz} Hz；来源：${request.operating_conditions.source}\n默认计算策略：OpenDSS COM，最多100次迭代，容差1e-6，不自动重试；输出报告和JSON/CSV。\n本次未提供工程限值，缺依据项标为未评估，不作工程合格结论。\n内部声明由Host记录，时间是请求准备时间，不是用户签名或审批完成时间；实际授权以本次原生决定回执为准。\n规范请求 SHA256：${requestHash}`
        : `执行固定模型/工况的稳态潮流；不会改动模型或已有运行点。\n模型：${canonicalJson(request.model)}\n工况：${canonicalJson(request.scenario)}\n声明：${canonicalJson(request.operating_conditions)}\n求解：${canonicalJson(request.solver)}\n判据：${criteria.slice(0, 12000)}${criteria.length > 12000 ? '\n[判据展示截断；完整规范请求由下列 SHA256 固定，授权前应核对已提供的判据原文。]' : ''}\n规范请求 SHA256：${requestHash}\n本确认只授权本次请求的计算，不认证 confirmed_by 身份，不将任何工程项目改判为合格。`;
      const outcome = await approval.request({ agent: exec.agent, toolName: exec.name, callId: exec.callId, signal: exec.signal, reason });
      record.outcome = outcome === 'allowed-once' ? 'allowed-once' : 'denied';
      record.decided_at = new Date().toISOString();
      record.decision_source = 'Harness native approval service; may apply an existing user policy grant, not a claimed handwritten signature';
      await atomicApproval(filename, record);
      if (record.outcome !== 'allowed-once') fail('run_authorization_denied', '未获得本次固定请求的计算授权；未提交计算。');
      exec.signal.throwIfAborted();
      return { filename, record, reused: false };
    })();
    inFlight.set(callHash, { requestHash, promise });
    try { return await promise; } finally { if (inFlight.get(callHash)?.promise === promise) inFlight.delete(callHash); }
  };
}


/** Private source audit is written before importing bytes into the core. */
async function writeNativeImportAudit(config, scope, selected, phase, snapshotId = null, sourceKind = 'native') {
  const state = await fs.realpath(config.stateDir);
  const directory = path.join(state, 'imports');
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const directoryInfo = await fs.lstat(directory);
  if (!directoryInfo.isDirectory() || directoryInfo.isSymbolicLink() || normalizeCase(await fs.realpath(directory)) !== normalizeCase(directory)) fail('unsafe_import_audit', '附件来源审计目录无效。');
  const identity = callIdentity(scope);
  const filename = path.join(directory, `${sha256(canonicalJson(identity))}.${phase}.json`);
  const record = { schema: sourceKind === 'workspace' ? 'tianshu-workspace-file-import/1' : 'tianshu-native-file-import/1', scope: identity, phase,
    source_selection: selected.privateAudit, source_inventory: selected.inventory,
    snapshot_id: snapshotId, numerical_validity: 'not_run', engineering_accepted: false };
  const text = JSON.stringify(record, null, 2) + '\n';
  if (Buffer.byteLength(text) > 1024 * 1024) fail('import_audit_budget', '附件来源审计超过预算。');
  let handle, existing = false;
  try { handle = await fs.open(filename, fsConstants.O_RDWR | fsConstants.O_CREAT | fsConstants.O_EXCL | (fsConstants.O_NOFOLLOW ?? 0), 0o600); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const info = await fs.lstat(filename);
    if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 1024 * 1024) fail('unsafe_import_audit', '已有附件审计不是有界普通文件。');
    handle = await fs.open(filename, fsConstants.O_RDWR | (fsConstants.O_NOFOLLOW ?? 0));
    existing = true;
  }
  try {
    const initial = await handle.stat();
    const current = await fs.lstat(filename);
    if (!initial.isFile() || initial.nlink !== 1 || current.isSymbolicLink() || !current.isFile() || initial.ino !== current.ino || initial.dev !== current.dev || initial.size > 1024 * 1024) fail('unsafe_import_audit', '附件审计文件身份无效。');
    if (existing) {
      const stored = JSON.parse(await handle.readFile('utf8'));
      if (canonicalJson(stored) !== canonicalJson(record)) fail('native_import_audit_conflict', '同一Host调用的附件清单或导入回执已改变。');
    } else await handle.writeFile(text);
    // A prior write/flush/close failure may leave valid JSON. Existence and
    // matching text are never a durable-commit receipt: every reuse must sync.
    await handle.sync();
    const final = await handle.stat();
    const visible = await fs.lstat(filename);
    if (!final.isFile() || final.nlink !== 1 || visible.isSymbolicLink() || !visible.isFile() || final.ino !== initial.ino || final.dev !== initial.dev || final.ino !== visible.ino || final.dev !== visible.dev || final.size !== Buffer.byteLength(text) || (existing && (final.size !== initial.size || final.mtimeMs !== initial.mtimeMs || final.ctimeMs !== initial.ctimeMs))) fail('unsafe_import_audit', '保存来源审计时文件发生变化。');
  } finally {
    // A close failure also blocks the caller. A retry reopens, revalidates and
    // syncs the same content before any professional import can be submitted.
    await handle.close();
  }
}

/** Exported factory also permits an isolated Host contract test, without a model. */
export function registerTianshuTools(ctx, config, { runtimeFactory = createRuntime, toolFactory = defineTool } = {}) {
  // Config uses schemastery; persisted projection restore requires Zod.parse().
  const ordinal = projectionZ.number().int().nonnegative();
  const refSchema = projectionZ.strictObject({ attachmentId: projectionZ.string(), name: projectionZ.string(), bytes: ordinal });
  const recordSchema = projectionZ.strictObject({ eventSeq: ordinal, blockIndex: ordinal, ref: refSchema });
  ctx.sessionProjections.register(attachmentProjection(projectionZ.strictObject({ inheritedEventCount: ordinal,
    records: projectionZ.array(recordSchema).max(500), rejectedCount: ordinal, evictedCount: ordinal })));
  const authorizeRun = runAuthorizer(ctx, config);
  let pendingRuntime;
  const runtime = async () => {
    pendingRuntime ??= Promise.resolve().then(() => runtimeFactory(hostConfig(config)));
    return pendingRuntime;
  };
  ctx.effect(() => async () => { if (pendingRuntime) { const running = await pendingRuntime.catch(() => null); await running?.close(); } });
  ctx.systemPrompt.section({ name: 'tianshu:professional', order: 100, interpolate: false, text: prompt });

  const register = (toolName, description, parameters, body, concurrency = false, needsRuntime = true) => {
    const definition = toolFactory({
      name: `tianshu_${toolName}`, description, parameters,
      output: { schema: resultOutput, render },
      isConcurrencySafe: () => concurrency,
      async execute(args, exec) {
        exactArgs(args, Object.keys(parameters));
        const scope = await trustedScope(ctx, exec);
        return body(args, exec, scope, needsRuntime ? await runtime() : undefined);
      },
    });
    ctx.tools.register(definition);
    return definition;
  };
  const tools = [];
  tools.push(register('capabilities', '读取真实专业后端能力、Harness桥接状态和限定参考登记。不会运行潮流；工程就绪和私有化状态分别说明。', {}, async (_args, _exec, scope, running) => {
    const capabilities = await running.bridge.capabilities(scope);
    return resultValue('capabilities', '已读取专业服务能力；限定参考认可不等于任意模型工程合格。', {
      service: capabilities.service, version: capabilities.version, solver: capabilities.solver,
      harness_plugin_version: packageIdentity.version, target_harness_version: packageIdentity.engines?.dsh ?? null,
      harness_runtime_version_verified: false,
      supported_request_schemas: capabilities.supported_request_schemas,
      supported_criteria_schemas: capabilities.supported_criteria_schemas,
      extensions: capabilities.extensions, model_catalog: capabilities.model_catalog,
      model_import: capabilities.model_import, scenario: capabilities.scenario,
      operating_conditions: capabilities.operating_conditions, recovery: capabilities.recovery,
      engineering_ready: false, harness_bridge_connected: true,
      private_model_readiness: capabilities.private_model_readiness,
      project_reference: config.engineeringReference ?? { status: 'not_configured', scope: 'no_general_engineering_acceptance' },
      input_authorization: 'explicit_workspace_or_native_session_file_list_with_native_approval',
      attachment_import: 'native_session_user_file_blocks_with_explicit_mapping',
      daily_input: { workspace_file_list: true, model_only_run: true, default_engineering_criteria: null,
        file_dependencies: 'authorized_literal_same_package_reference_closure_no_directory_scan',
        frequency: 'verified_explicit_model_source_or_explicit_user_value_no_guess',
        operating_declaration: 'Host_prepared_record_not_user_engineering_confirmation',
        unchanged_model_only: true },
    });
  }, true));
  tools.push(register('list_models', '列出已登记算例及本会话导入的模型；不读取任意工作区文件，不执行计算。', {}, async (_args, _exec, scope, running) => {
    const models = await running.bridge.models(scope);
    return resultValue('list_models', `已读取可用模型。`, { models });
  }, true));
  tools.push(register('import_model', '将明确选择的工作区文件或当前Session用户原生附件复制为不可变模型快照。原生附件需Host选择键和逐项包路径，完整消费授权流；不扫描目录，不信raw ref或宿主路径。OpenDSS受静态子集限制；MATPOWER仅平衡等值。', importParameters, async (args, exec, scope, running) => {
    if (args.include_dependencies !== undefined && typeof args.include_dependencies !== 'boolean') fail('invalid_import_arguments', 'include_dependencies必须为布尔值。');
    const includeDependencies = args.include_dependencies ?? (args.workspace_files !== undefined);
    if (args.source === 'native_attachments' && args.include_dependencies !== undefined) fail('invalid_import_source', '原生附件只能读取用户已上传的附件，不能自动读取本机目录依赖。');
    args = normalizeWorkspaceSelection(args);
    if (args.source === 'native_attachments') {
      const selected = await collectNativeAttachmentPackage(ctx, exec, args);
      const latest = await trustedScope(ctx, exec);
      if (canonicalJson(callIdentity(latest)) !== canonicalJson(callIdentity(scope))) fail('native_attachment_scope_changed', '附件授权后真实会话、工作区或调用归属发生变化，未导入。');
      try { await writeNativeImportAudit(config, scope, selected, 'verified'); }
      catch (error) {
        if (['unsafe_import_audit', 'import_audit_budget', 'native_import_audit_conflict'].includes(error?.code)) throw error;
        fail('native_import_source_audit_unavailable', '附件来源清单未能可靠保存，未提交专业导入；请保留现场并检查本地私有存储。');
      }
      exec.signal.throwIfAborted();
      const submitScope = await trustedScope(ctx, exec);
      if (canonicalJson(callIdentity(submitScope)) !== canonicalJson(callIdentity(scope))) fail('native_attachment_scope_changed', '来源审计后真实调用归属发生变化，未导入。');
      exec.signal.throwIfAborted();
      const imported = await running.bridge.importModel(scope, selected.payload);
      try { await writeNativeImportAudit(config, scope, selected, 'imported', imported.snapshot_id); }
      catch { fail('native_import_completion_audit_failed', `快照已导入为${imported.snapshot_id}，完整来源清单已保存，但末次导入回执未确认；请查询本会话模型，不要据此发起计算或猜测导入失败。`); }
      const preparation = await rememberImportedModel(config, scope, imported, selected.payload);
      return resultValue('import_model', '已从明确选择的本会话原生附件导入固定快照；尚未执行潮流或作工程合格判断。', {
        snapshot_id: imported.snapshot_id, format: imported.manifest?.format, source_format: imported.source_format,
        master_file: imported.manifest?.master_file, fingerprint: imported.fingerprint,
        source_files: selected.inventory.slice(0, 20), source_file_count: selected.inventory.length,
        source_inventory_truncated: selected.inventory.length > 20, source_kind: 'native_session_user_files',
        source_audit: 'verified_manifest_and_import_receipt_saved', engineering_ready: false,
        inspection_status: imported.inspection?.status,
        balanced_equivalent: imported.balanced_equivalent === true,
        analysis_preparation: preparation,
      });
    }
    if ((args.source !== undefined && args.source !== 'workspace') || args.attachments !== undefined) fail('invalid_import_source', '工作区与原生附件输入不能混合；请明确选择单一来源。');
    relativeFile(args.package_root, true); relativeFile(args.master_file);
    if (!Array.isArray(args.files) || args.files.length < 1 || args.files.length > 500) fail('invalid_model_package', '请明确列出 1–500 个文件。');
    args.files.forEach(value => relativeFile(value));
    if (args.format === 'OpenDSS' && includeDependencies) assertDependencyPackageRoot(path.resolve(scope.workspaceRoot, args.package_root));
    await assertNotPrivateInput(config, scope, args);
    const approval = ctx.get('approval');
    if (!approval) fail('file_authorization_unavailable', '缺少原生文件授权通道，未读取模型文件。');
    const discover = args.format === 'OpenDSS' && includeDependencies;
    const dependencyPolicy = discover ? '\n并读取入口中明确字面引用的同包依赖：仅Redirect及受支持的数据引用，限上述包目录及其子目录，最多500文件/20 MiB；不搜索目录、不读取父目录/绝对路径/链接或插件私有数据。读取后固定完整清单及SHA256；缺文件就说明文件名并停止。' : '\n严格仅读取上述完整文件清单，不自动扩大。';
    const outcome = await approval.request({ agent: exec.agent, toolName: exec.name, callId: exec.callId, signal: exec.signal,
      reason: `将本会话工作区中包目录 ${args.package_root} 的以下明确文件复制到天智五号本地专业服务，作为不可变计算输入（不向云端上传文件全文）：\n${args.files.join('\n')}\n主文件：${args.master_file}。${dependencyPolicy}\n后续工具摘要可能供当前所选模型处理。` });
    if (outcome !== 'allowed-once') fail('file_authorization_denied', '未获得本次文件清单授权，未读取或导入模型文件。');
    const selected = args.format === 'OpenDSS'
      ? await collectReferencedPackage(args, { signal: exec.signal, includeDependencies: discover,
        checkFiles: names => assertNotPrivateInput(config, scope, { ...args, files: names }),
        readFiles: (names, budget) => collectWorkspaceFiles(scope.workspaceRoot, { ...args, files: names, master_file: names[0] }, exec.signal, budget) })
      : await collectWorkspaceFiles(scope.workspaceRoot, args, exec.signal);
    selected.privateAudit ??= { source: 'workspace', policy: 'explicit_file_list', package_root: args.package_root, master_file: args.master_file, explicit_files: args.files };
    await writeNativeImportAudit(config, scope, selected, 'verified', null, 'workspace');
    exec.signal.throwIfAborted();
    const imported = await running.bridge.importModel(scope, selected.payload);
    try { await writeNativeImportAudit(config, scope, selected, 'imported', imported.snapshot_id, 'workspace'); }
    catch { fail('workspace_import_completion_audit_failed', `模型已导入为${imported.snapshot_id}，但末次来源回执未确认；请保留该快照身份，不要猜测失败或重新计算。`); }
    const preparation = await rememberImportedModel(config, scope, imported, selected.payload);
    return resultValue('import_model', '已导入固定模型快照；尚未执行潮流或作工程合格判断。', {
      snapshot_id: imported.snapshot_id, format: imported.manifest?.format, source_format: imported.source_format,
      master_file: imported.manifest?.master_file, fingerprint: imported.fingerprint,
      source_files: selected.inventory.slice(0, 20), source_file_count: selected.inventory.length,
      source_inventory_truncated: selected.inventory.length > 20, engineering_ready: false,
      inspection_status: imported.inspection?.status,
      balanced_equivalent: imported.balanced_equivalent === true,
      analysis_preparation: preparation,
      dependency_resolution: selected.dependencyResolution ?? { policy: 'explicit_file_list' },
    });
  }));
  tools.push(register('run_power_flow', '日常“计算这个文件”优先只传已导入model：Host从可信摘要取频率、整理固定计算策略与内部声明并请求原生授权，缺关键数据才返回简短缺项，不要求用户填请求对象。也保留完整request专业模式，二者不能混用。等待真实终态，不改模型/工况，缺限值未评估，停止转所属任务取消。', runParameters, async (args, exec, scope, running) => {
    if ((args.request !== undefined) === (args.model !== undefined) || (args.request !== undefined && args.frequency_hz !== undefined)) {
      fail('invalid_run_mode', '日常计算只需模型及必要的频率；完整请求方式单独使用，不能混合。');
    }
    const prepared = args.request === undefined ? await prepareDailyCall(config, scope, args, running.bridge) : null;
    if (prepared && prepared.status !== 'ready') return resultValue('run_power_flow', '还缺少必要信息，尚未提交计算。', { ...prepared, execution_status: 'not_submitted', numerical_validity: 'not_run', engineering_ready: false });
    const request = prepared?.request ?? args.request;
    const authorization = await authorizeRun(request, exec, scope, prepared?.provenance ?? null);
    const executionEvents = [];
    let eventCount = 0;
    const onEvent = event => {
      eventCount++;
      // These are backend events, never synthetic reasoning or inferred stages.
      if (executionEvents.length < 20) executionEvents.push(event);
    };
    let status;
    if (authorization.record.task_id) {
      status = await running.bridge.status(scope, authorization.record.task_id);
      if (!TERMINAL.has(status.execution_status)) status = await running.bridge.wait(scope, authorization.record.task_id, { signal: exec.signal, onEvent });
    } else {
      status = await running.bridge.submitAndWait(scope, request, { signal: exec.signal, onEvent });
      if (status.task_id) {
        authorization.record.task_id = status.task_id;
        if (status.attempt_id) authorization.record.attempt_id = status.attempt_id;
        await atomicApproval(authorization.filename, authorization.record);
      }
    }
    const data = { ...status, engineering_ready: false, execution_events: executionEvents, execution_event_count: eventCount, events_truncated: eventCount > executionEvents.length,
      compute_authorization: { request_sha256: authorization.record.request_sha256, source: 'native_calculation_authorization_not_engineering_certification', reused: authorization.reused } };
    if (prepared) data.declaration_provenance = prepared.provenance;
    if (status.task_id && status.execution_status === 'finished') data.result = summarizeResult(await running.bridge.result(scope, status.task_id));
    return resultValue('run_power_flow', `计算状态：${status.execution_status}；数值状态：${status.numerical_validity ?? 'not_run'}。`, data);
  }));
  tools.push(register('get_task', '读取本会话任务状态或历史归属；不触发计算或重算。传 task_id 查指定任务，不传则查本会话历史；events_after 可读取最多20条原始后端事件，按返回游标继续。', { task_id: { type: 'string' }, events_after: { type: 'integer' } }, async (args, _exec, scope, running) => {
    if (args.events_after !== undefined && (!args.task_id || !Number.isSafeInteger(args.events_after) || args.events_after < 0)) fail('invalid_cursor', 'events_after 需指定 task_id 及非负整数游标。');
    const data = args.task_id ? await running.bridge.status(scope, args.task_id) : { tasks: await running.bridge.history(scope) };
    if (args.events_after !== undefined) {
      const page = await running.bridge.events(scope, args.task_id, args.events_after);
      const events = page.events.slice(0, 20);
      data.events = events;
      data.next_cursor = events.at(-1)?.seq ?? args.events_after;
      data.more_in_page = page.events.length > events.length;
    }
    return resultValue('get_task', '已读取本会话任务记录。', data);
  }, true));
  tools.push(register('cancel_task', '请求取消本会话指定任务，并等待实际终态。已完成结果不会被迟到取消覆盖。', taskParameters, async (args, _exec, scope, running) => {
    const requested = await running.bridge.cancel(scope, args.task_id);
    const status = TERMINAL.has(requested.execution_status) ? requested : await running.bridge.wait(scope, args.task_id);
    return resultValue('cancel_task', `任务终态：${status.execution_status}；已核实停止。`, { ...status, confirmed_stopped: true });
  }));
  tools.push(register('get_result', '只读已提交结果。默认摘要含已记录的全域指标；网损/负载率等请用 engineering_metrics 视图，按metric/object_id/status/单位/基准/节点或端口精确过滤，并分页读取原值和证据。assessment是规则判定，observations等是观测，不混用数据域；缺指标保持未评估，不推导或重算。', queryParameters, async (args, _exec, scope, running) => resultValue('get_result', '已读取固定任务结果；工程整体合规按记录保留。', queryResult(await running.bridge.result(scope, args.task_id), args)), true));
  tools.push(register('open_report', '校验本会话固定任务产物，交付只读本地HTML报告或正式PDF下载。PDF由已提交HTML派生，不重算或改写原报告。', reportParameters, async (args, _exec, scope, running) => {
    const published = await running.publishReport(scope, args.task_id, {format:args.format??'html'});
    const delivered=await exportNativeReport(scope,published,running.bridge,{format:args.format??'html'});
    return resultValue('open_report', `报告文件：${delivered.markdown_link}`, delivered);
  }, true));
  const studySummary=(row,args={})=>{
    const offset=args.offset??0,limit=args.limit??10;
    if(!Number.isSafeInteger(offset)||offset<0||!Number.isSafeInteger(limit)||limit<1||limit>20)fail('invalid_page','时点分页需offset>=0、limit为1..20。');
    return {...Object.fromEntries(['study_id','request_id','execution_status','cancel_requested','completion_status','completed_points','valid_points','failed_points','elapsed_s','automatic_replay','engineering_accepted','artifacts'].filter(k=>row[k]!==undefined).map(k=>[k,row[k]])),point_count:row.points.length,offset,limit,next_offset:offset+limit<row.points.length?offset+limit:null,points:row.points.slice(offset,offset+limit),limitations:row.limitations};
  };
  tools.push(register('run_time_series','按用户明确曲线逐点执行准静态潮流。固定控制、每点独立快照，保留父子任务、失败与未运行点；不是动态仿真。请求整条曲线授权，停止时取消所属父任务和在途子任务。',studyParameters,async(args,exec,scope,running)=>{
    if((args.model!==undefined)===(args.request!==undefined))fail('invalid_run_mode','请选择已导入模型或完整基础请求之一。');
    const prepared=args.request===undefined?await prepareDailyCall(config,scope,{model:args.model,...(args.frequency_hz===undefined?{}:{frequency_hz:args.frequency_hz})},running.bridge):null;
    if(prepared&&prepared.status!=='ready')return resultValue('run_time_series','还缺基础模型信息，尚未提交时序任务。',prepared);
    if((args.points!==undefined)===(args.curve_workspace_file!==undefined))fail('invalid_curve','请提供明确时点或一个曲线文件，不要混合。');
    let curve;
    if(args.curve_workspace_file!==undefined){
      relativeFile(args.curve_workspace_file);
      const format=/\.csv$/i.test(args.curve_workspace_file)?'CSV':/\.json$/i.test(args.curve_workspace_file)?'JSON':null;
      if(!format)fail('invalid_curve','曲线需CSV或JSON文件。');
      const selection={format,package_root:path.posix.dirname(args.curve_workspace_file),master_file:path.posix.basename(args.curve_workspace_file),files:[path.posix.basename(args.curve_workspace_file)]};
      await assertNotPrivateInput(config,scope,selection);
      const approval=ctx.get('approval');if(!approval)fail('file_authorization_unavailable','缺少曲线文件授权。');
      const outcome=await approval.request({agent:exec.agent,toolName:exec.name,callId:exec.callId,signal:exec.signal,reason:`读取明确选择的工作区曲线文件${args.curve_workspace_file}，最多1MiB，仅用于本次逐点潮流；保留原内容和SHA256，不扫描其他文件。`});
      if(outcome!=='allowed-once')fail('file_authorization_denied','未获本次曲线文件读取授权。');
      if(canonicalJson(callIdentity(await trustedScope(ctx,exec)))!==canonicalJson(callIdentity(scope)))fail('curve_scope_changed','曲线授权后会话或工作区归属变化，未读取。');
      const selected=await collectWorkspaceFiles(scope.workspaceRoot,selection,exec.signal,1024*1024);
      curve={format,name:selection.master_file,content_utf8:selected.payload.files[0].content_utf8};
    }
    const request={mode:'quasistatic_fixed_controls',curve_source:args.curve_source??(curve?`用户明确选择的工作区曲线${args.curve_workspace_file}，原文件SHA256=${sha256(curve.content_utf8)}`:''),base_request:prepared?.request??args.request,...(curve?{curve_file:curve}:{points:args.points}),on_point_error:args.on_point_error};
    const encoded=canonicalJson(request);if(Buffer.byteLength(encoded)>2*1024*1024)fail('invalid_study','时序请求超过2MiB。');
    const directory=await privateApprovalDirectory(config);const filename=path.join(directory,'study-'+sha256(canonicalJson(callIdentity(scope)))+'.json');
    const hash=sha256(encoded);let record;
    try{const stat=await fs.lstat(filename);if(!stat.isFile()||stat.isSymbolicLink()||stat.nlink!==1||stat.size>65536)fail('unsafe_approval_store','时序授权记录无效。');record=JSON.parse(await fs.readFile(filename,'utf8'));}
    catch(e){if(e.code!=='ENOENT')throw e;}
    if(record&&(record.request_sha256!==hash||canonicalJson(record.scope)!==canonicalJson(callIdentity(scope))))fail('approval_request_conflict','同一调用的曲线或模型发生变化。');
    if(record?.decision!=='allowed-once'){
      const approval=ctx.get('approval');if(!approval)fail('compute_authorization_unavailable','缺少原生计算授权。');
      const decision=await approval.request({agent:exec.agent,toolName:exec.name,callId:exec.callId,signal:exec.signal,
        reason:`执行${Array.isArray(args.points)?args.points.length+'个时点':'明确曲线文件中的全部时点（上限1000）'}的准静态潮流。将派生新快照并应用每点明确的负荷/电源倍率和运行设定，不改原模型，不自动改拓扑、额定或控制策略。\n来源：${request.curve_source}\n失败策略：${args.on_point_error}\n完整曲线及基础请求SHA256：${hash}\n缺工程判据仍未评估；停止将取消所属时序任务。`});
      if(decision!=='allowed-once')fail('compute_authorization_denied','未获本次整条曲线计算授权。');
      record={schema:'tianzhi-study-native-authorization/1',scope:callIdentity(scope),request_sha256:hash,decision,decided_at:new Date().toISOString()};await atomicApproval(filename,record);
    }
    if(canonicalJson(callIdentity(await trustedScope(ctx,exec)))!==canonicalJson(callIdentity(scope)))fail('study_scope_changed','计算授权后会话或工作区归属变化，未提交。');
    exec.signal.throwIfAborted();const submitted=await running.bridge.submitStudy(scope,request);
    const finished=await running.bridge.waitStudy(scope,submitted.study_id,{signal:exec.signal});
    return resultValue('run_time_series','已读取真实时序终态；各点失败与未运行项保留。',studySummary(finished));
  }));
  tools.push(register('get_time_series','只读本会话时序父任务及分页时点；history=true找回本会话已有任务和真实状态，中断后不猜未提交或重算。',studyQueryParameters,async(args,_exec,scope,running)=>{
    if(args.history===true){if(args.study_id!==undefined)fail('invalid_study_query','历史查询与指定study_id不能混用。');return resultValue('get_time_series','已读取本会话真实时序历史，未提交或重算。',await running.bridge.studyHistory(scope,args));}
    if(typeof args.study_id!=='string')fail('invalid_study_query','请查询已知study_id，或用history=true找回本会话已有任务。');
    return resultValue('get_time_series','已读取时序记录。',studySummary(await running.bridge.studyStatus(scope,args.study_id),args));
  },true));
  tools.push(register('cancel_time_series','取消本会话时序任务，等待在途子任务真正停止；已完成点保持原结果，后续点不运行。',{study_id:{...studyQueryParameters.study_id,required:true}},async(args,_exec,scope,running)=>{
    await running.bridge.cancelStudy(scope,args.study_id);return resultValue('cancel_time_series','时序任务已到真实终态。',studySummary(await running.bridge.waitStudy(scope,args.study_id)));
  }));
  tools.push(register('open_time_series_report','只读已提交时序HTML、逐点CSV和JSON，或从HTML派生PDF；不重算。',{study_id:{...studyQueryParameters.study_id,required:true},format:reportParameters.format},async(args,_exec,scope,running)=>{
    const published=await running.publishReport(scope,args.study_id,{study:true,format:args.format??'html'});
    const delivered=await exportNativeReport(scope,published,running.bridge,{study:true,format:args.format??'html'});
    return resultValue('open_time_series_report',`报告文件：${delivered.markdown_link}`,delivered);
  },true));
  tools.push(register('compare_scenarios','只读比较两个本会话有效工况的电压、负载率、网损等已记录工程量。按对象、单位、基值与物理口径匹配；缺失不补零，不重算。',comparisonParameters,async(args,_exec,scope,running)=>{
    const comparison=await running.bridge.compareScenarios(scope,args.left_task_id,args.right_task_id);const offset=args.offset??0,limit=args.limit??10;
    if(!Number.isSafeInteger(offset)||offset<0||!Number.isSafeInteger(limit)||limit<1||limit>20)fail('invalid_page','分页需offset>=0、limit为1..20。');
    const rows=comparison.rows.filter(x=>args.metric===undefined||x.metric===args.metric);return resultValue('compare_scenarios','已比较已提交结果，差值为右侧减左侧。',{...comparison,rows:rows.slice(offset,offset+limit),total:rows.length,offset,limit,next_offset:offset+limit<rows.length?offset+limit:null});
  },true));
  tools.push(register('inspect_topology','读取本会话已导入快照的供电连通性、孤岛母线/设备及部分相开路标识。是母线级静态诊断，不是保护计算或孤岛运行认证。',{snapshot_id:{type:'string',required:true}},async(args,_exec,scope,running)=>{
    const inspected=await running.bridge.inspectModel(scope,args.snapshot_id);return resultValue('inspect_topology','已读取独立拓扑诊断。',{snapshot_id:args.snapshot_id,...inspected.topology});
  },true));
  tools.push(register('list_attachments', '只列出当前Session自身用户消息中已上传的原生文件元数据及Host选择键；不读字节、不启动专业服务。逐项选键并明确包路径，不能把raw ref或宿主路径当授权。', attachmentListParameters,
    async (args, exec) => resultValue('list_attachments', '已列出当前Session可明确选择的附件元数据；尚未读取或导入文件。', listSessionAttachments(ctx, exec, args)), true, false));
  return { tools, close: async () => { if (pendingRuntime) await (await pendingRuntime).close(); } };
}

export function apply(ctx, config) { registerTianshuTools(ctx, config); }
