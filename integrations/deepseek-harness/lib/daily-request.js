/** Host preparation of a daily, unchanged-model power-flow request.
 * Copyright (c) yangshaopeng.
 * Pure: no clock, filesystem, approval, service or solver is called here.
 */
import { createHash } from 'node:crypto';

const CONFIRMER = 'Tianshu Host（本次请求声明，非用户/工程签名）';
const ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
const fail = (code, message) => { const error = new Error(message); error.code = code; throw error; };
const exact = (value, keys, name) => {
  if (!plain(value) || Object.keys(value).some(key => !keys.includes(key))) fail('invalid_daily_arguments', `${name} contains unsupported fields.`);
};
const canonical = value => {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (plain(value)) return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  fail('invalid_daily_arguments', 'Daily preparation only accepts finite JSON values.');
};
const hash = value => createHash('sha256').update(canonical(value)).digest('hex');
const frozen = value => {
  if (value && typeof value === 'object') { for (const child of Object.values(value)) frozen(child); Object.freeze(value); }
  return value;
};
const frequency = value => typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= 1000;
const issue = (field, code, message, details = {}) => ({ field, code, message, ...details });
const needsInput = issues => frozen({ status: 'needs_input', issues, request: null,
  provenance: { kind: 'host_request_declaration', timestamp_semantics: 'host_prepared_at_not_approval_time',
    user_engineering_confirmation: false, native_approval_required: true, engineering_accepted: false } });

function modelReference(value) {
  if (!plain(value) || Object.keys(value).length !== 1) return null;
  const key = Object.keys(value)[0];
  if (!['case_id', 'snapshot_id'].includes(key) || typeof value[key] !== 'string' || !ID.test(value[key])) return null;
  if (key === 'snapshot_id' && !/^model-[0-9a-f]{64}$/.test(value[key])) return null;
  return { [key]: value[key] };
}
function evidenceCopy(value) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 20) fail('invalid_daily_model_summary', 'Frequency evidence must be a bounded array.');
  return value.map(row => {
    exact(row, ['source_file', 'line', 'detail'], 'modelSummary.frequency.evidence');
    if (typeof row.source_file !== 'string' || !row.source_file || row.source_file.length > 240 || /[\x00-\x1f]/.test(row.source_file)) fail('invalid_daily_model_summary', 'Frequency evidence requires a bounded source identifier.');
    if (row.line !== undefined && row.line !== null && (!Number.isSafeInteger(row.line) || row.line < 1)) fail('invalid_daily_model_summary', 'Frequency evidence line must be a positive integer or null.');
    if (row.detail !== undefined && (typeof row.detail !== 'string' || row.detail.length > 512 || /[\x00-\x1f]/.test(row.detail))) fail('invalid_daily_model_summary', 'Frequency evidence detail must be bounded plain text.');
    return { source_file: row.source_file, ...(row.line === undefined ? {} : { line: row.line }), ...(row.detail === undefined ? {} : { detail: row.detail }) };
  });
}

/**
 * Prepare exactly one immutable schema-1.1 request, without authorizing it.
 *
 * input.modelSummary is supplied by trusted Host inspection, never by an LLM:
 *   {model:{case_id|snapshot_id}, frequency:{status:'explicit'|'missing'|'ambiguous',
 *     value_hz?:number, evidence?:[{source_file,line?,detail?}]}}
 * input.frequency_hz, if present, is the user's explicit frequency declaration.
 * Host dependencies are not tool arguments: preparedAt is the persisted ISO
 * preparation time; callIdentityHash is the real current Host call's SHA256.
 *
 * confirmed_at deliberately means this caller's Host preparation/declaration
 * time, not approval completion or a user signature. The caller must persist
 * this complete request, authorize its unchanged canonical SHA256, and record
 * the actual native decision separately as receipt.decided_at. Preparation
 * does not permit submission and never implies engineering confirmation.
 */
export function prepareDailyRequest(input, trusted) {
  exact(input, ['model', 'modelSummary', 'frequency_hz'], 'daily input');
  exact(trusted, ['preparedAt', 'callIdentityHash'], 'trusted Host dependencies');
  if (typeof trusted.preparedAt !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(trusted.preparedAt)
    || !Number.isFinite(Date.parse(trusted.preparedAt)) || new Date(trusted.preparedAt).toISOString() !== trusted.preparedAt
    || typeof trusted.callIdentityHash !== 'string' || !/^[0-9a-f]{64}$/.test(trusted.callIdentityHash)) fail('invalid_daily_host_context', 'A fixed canonical Host preparation time and call SHA256 are required.');
  const model = modelReference(input.model);
  if (!model) return needsInput([issue('model', 'model_required', '请明确当前要计算的已绑定模型；不能猜测或沿用其他任务。')]);
  const hasUserFrequency = Object.hasOwn(input, 'frequency_hz');
  if (hasUserFrequency && !frequency(input.frequency_hz)) return needsInput([issue('frequency_hz', 'invalid_user_frequency', '请明确提供大于0且不超过1000的频率数值（Hz）。')]);
  let modelFrequency, frequencyEvidence = [], modelFrequencyMetadata = {};
  if (input.modelSummary !== undefined && input.modelSummary !== null) {
    exact(input.modelSummary, ['schema_version', 'source', 'model', 'model_identity', 'dependency_status', 'dependency_count', 'capability', 'frequency', 'required_inputs', 'limitations'], 'trusted model summary');
    const summaryModel = modelReference(input.modelSummary.model);
    if (!summaryModel || canonical(summaryModel) !== canonical(model)) return needsInput([issue('model', 'model_summary_identity_mismatch', '可信模型摘要与本次所选模型不一致，需重新核对模型。')]);
    const summaryFrequency = input.modelSummary.frequency;
    if (summaryFrequency !== undefined) {
      exact(summaryFrequency, ['status', 'value_hz', 'evidence', 'sources', 'reason', 'source_count', 'sources_truncated', 'evidence_truncated', 'related_source_frequencies', 'related_source_frequency_count', 'related_source_frequencies_truncated', 'element_base_frequency_count'], 'trusted frequency summary');
      if (!['explicit', 'missing', 'ambiguous'].includes(summaryFrequency.status)) return needsInput([issue('frequency_hz', 'model_frequency_unusable', '模型频率摘要状态未知，需重新核对。')]);
      if (summaryFrequency.status === 'ambiguous') return needsInput([issue('frequency_hz', 'model_frequency_ambiguous', '模型存在不明确或相互矛盾的频率信息，需先核清，不自动选择50或60Hz。')]);
      if (summaryFrequency.status === 'explicit') {
        if (!frequency(summaryFrequency.value_hz)) return needsInput([issue('frequency_hz', 'model_frequency_unusable', '模型摘要未提供有效的明确频率，需重新核对。')]);
        modelFrequency = summaryFrequency.value_hz;
        frequencyEvidence = evidenceCopy(summaryFrequency.evidence);
        if (summaryFrequency.evidence_truncated !== undefined && typeof summaryFrequency.evidence_truncated !== 'boolean') fail('invalid_daily_model_summary', 'Frequency evidence truncation must be explicit.');
        if (summaryFrequency.source_count !== undefined && (!Number.isSafeInteger(summaryFrequency.source_count) || summaryFrequency.source_count < 0)) fail('invalid_daily_model_summary', 'Frequency source count must be a nonnegative integer.');
        modelFrequencyMetadata = {
          ...(summaryFrequency.evidence_truncated === undefined ? {} : { evidence_truncated: summaryFrequency.evidence_truncated }),
          ...(summaryFrequency.source_count === undefined ? {} : { source_count: summaryFrequency.source_count }),
        };
      } else if (summaryFrequency.value_hz !== undefined && summaryFrequency.value_hz !== null) {
        return needsInput([issue('frequency_hz', 'model_frequency_unusable', '标记为缺失的模型频率摘要仍带有数值，需重新核对。')]);
      }
    }
  }
  if (modelFrequency !== undefined && hasUserFrequency && modelFrequency !== input.frequency_hz) {
    return needsInput([issue('frequency_hz', 'frequency_conflict', '用户声明频率与模型明确频率不一致；本工具保持原模型，不自动修改频率。', { model_frequency_hz: modelFrequency, user_frequency_hz: input.frequency_hz })]);
  }
  if (modelFrequency === undefined && !hasUserFrequency) return needsInput([issue('frequency_hz', 'frequency_required', '模型没有可信的明确频率，请补充本次频率（Hz）；不默认50或60Hz。')]);
  const hz = modelFrequency ?? input.frequency_hz;
  const basis = modelFrequency === undefined ? 'explicit_user_frequency' : 'explicit_trusted_model_frequency';
  const scopeDigest = hash({ callIdentityHash: trusted.callIdentityHash, preparedAt: trusted.preparedAt, model, frequency_hz: hz, frequency_basis: basis });
  const modelText = canonical(model);
  const source = `Host基于已绑定模型${modelText}的原有运行点生成的本次运行声明；频率${hz}Hz依据=${basis}；非用户提供或审核的工况声明，非工程工况审核。confirmed_at为Host准备/声明时间；实际运行授权只以Host原生审批回执及其decided_at为准。`;
  const request = {
    schema_version: '1.1', model,
    scenario: { scenario_id: 'daily-' + scopeDigest.slice(0, 32), mode: 'existing_snapshot' },
    solver: { engine: 'OpenDSS', transport: 'com', solve_mode: 'snapshot', max_iterations: 100, tolerance: 0.000001 },
    recovery: { policy: 'none', max_retries: 0 }, criteria: null,
    operating_conditions: { frequency_hz: hz, source, confirmed_by: CONFIRMER, confirmed_at: trusted.preparedAt, mode: 'validate_model_operating_point' },
    result_scopes: ['bus', 'branch', 'transformer', 'system'], output_formats: ['json', 'html', 'csv'],
  };
  return frozen({ status: 'ready', issues: [], request,
    provenance: { schema: 'tianshu-daily-host-declaration/1', kind: 'host_request_declaration',
      timestamp_semantics: 'host_prepared_at_not_approval_time', host_prepared_at: trusted.preparedAt,
      user_engineering_confirmation: false, engineering_accepted: false,
      frequency: { value_hz: hz, basis, evidence: frequencyEvidence, ...modelFrequencyMetadata, user_value_supplied: hasUserFrequency },
      unchanged_model_operating_point: true, fixed_technical_policy: 'OpenDSS_COM_snapshot_100_1e-6_no_recovery_all_domains_json_html_csv',
      native_approval_required: true, native_approval_time_record: 'separate_Host_receipt.decided_at',
      request_sha256: hash(request),
    } });
}
