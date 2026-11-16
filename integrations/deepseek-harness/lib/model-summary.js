/** Conservative summaries of an authorized core model inspection.
 * Copyright (c) yangshaopeng. Pure data transformation: no files or solver I/O.
 */
import { createHash } from 'node:crypto';

const MODEL = /^model-[0-9a-f]{64}$/;
const HASH = /^[0-9a-f]{64}$/;
const NUMERIC = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;
const SET_OPTIONS = new Set(['voltagebases', 'basefrequency', 'defaultbasefrequency', 'frequency', 'mode', 'controlmode', 'maxiterations', 'tolerance', 'algorithm', 'loadmodel', 'loadmult', 'genmult']);
const COMMANDS = new Set(['clear', 'calcvoltagebases', 'redirect', 'buscoords', 'set', 'open', 'close', 'new', 'edit', '~']);
const CONTEXT_OPTIONS = new Set(['mode', 'basefrequency', 'defaultbasefrequency']);
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const validPath = value => typeof value === 'string' && value.length > 0 && value.length <= 240 && !/[\\:\x00-\x1f]/.test(value) && !value.startsWith('/') && value.split('/').every(part => part && part !== '.' && part !== '..');
const sha256 = data => createHash('sha256').update(data).digest('hex');

export class ModelSummaryError extends Error {
  constructor(code) { super(code); this.name = 'ModelSummaryError'; this.code = code; }
}
const fail = code => { throw new ModelSummaryError(code); };
const numeric = raw => {
  if (typeof raw !== 'string') return null;
  let value = raw;
  if (value.length >= 2 && ['"', "'"].includes(value[0]) && value.at(-1) === value[0]) value = value.slice(1, -1);
  value = value.trim();
  if (!NUMERIC.test(value)) return null;
  const result = Number(value);
  return Number.isFinite(result) && result > 0 && result <= 1000 ? result : null;
};

function cleanLines(text) {
  // Match the core's comment/quote rules and physical line locations. Never
  // evaluate commands or expressions. Unclosed constructs fail closed.
  const lines = text.replace(/^\uFEFF/, '').split(/\r\n|[\n\r\v\f\x1c-\x1e\u0085\u2028\u2029]/);
  let block = false;
  const cleaned = [];
  for (const line of lines) {
    let output = '', quote = null;
    for (let i = 0; i < line.length; i++) {
      const char = line[i], pair = line.slice(i, i + 2);
      if (block) { if (pair === '*/') { block = false; i++; } }
      else if (quote) { output += char; if (char === quote) quote = null; }
      else if (char === '"' || char === "'") { quote = char; output += char; }
      else if (pair === '/*') { block = true; i++; }
      else if (char === '!' || pair === '//') break;
      else output += char;
    }
    if (quote) fail('model_frequency_replay_unavailable');
    cleaned.push(output);
  }
  if (block) fail('model_frequency_replay_unavailable');
  return cleaned;
}

function tokens(line) {
  const output = [], pairs = { '[': ']', '(': ')', '{': '}' };
  for (let i = 0; i < line.length;) {
    if (/\s|,/.test(line[i])) { i++; continue; }
    if (line[i] === '=') { output.push('='); i++; continue; }
    const start = i, stack = [];
    let quote = null;
    for (; i < line.length; i++) {
      const char = line[i];
      if (quote) { if (char === quote) quote = null; }
      else if (char === '"' || char === "'") quote = char;
      else if (pairs[char]) stack.push(pairs[char]);
      else if ('])}'.includes(char)) { if (stack.pop() !== char) fail('model_frequency_replay_unavailable'); }
      else if (!stack.length && /\s|,|=/.test(char)) break;
    }
    if (quote || stack.length || i === start) fail('model_frequency_replay_unavailable');
    output.push(line.slice(start, i));
  }
  return output;
}

const scalar = value => value.length >= 2 && ['"', "'"].includes(value[0]) && value.at(-1) === value[0] ? value.slice(1, -1) : value;

function bindFiles(entries, files) {
  if (!Array.isArray(files) || files.length !== entries.length) fail('model_payload_binding_mismatch');
  const supplied = new Map();
  let bytes = 0;
  for (const row of files) {
    if (!plain(row) || !validPath(row.path) || typeof row.content_utf8 !== 'string' || supplied.has(row.path)) fail('model_payload_binding_mismatch');
    const data = Buffer.from(row.content_utf8, 'utf8');
    bytes += data.length;
    if (bytes > 20 * 1024 * 1024) fail('model_payload_binding_mismatch');
    supplied.set(row.path, { text: row.content_utf8, data });
  }
  for (const entry of entries) {
    const row = supplied.get(entry.path);
    if (!row || row.data.length !== entry.bytes || sha256(row.data) !== entry.sha256) fail('model_payload_binding_mismatch');
  }
  return supplied;
}

function declarations(inspection, commandIndex) {
  const sourceEvents = [], bases = [];
  for (const object of inspection.objects) {
    if (!plain(object) || typeof object.class !== 'string' || typeof object.name !== 'string' || !Array.isArray(object.properties)) fail('invalid_model_inspection');
    for (const [propertyIndex, property] of object.properties.entries()) {
      if (!plain(property) || typeof property.name !== 'string') fail('invalid_model_inspection');
      if (!['frequency', 'basefreq'].includes(property.name.toLowerCase())) continue;
      const position = commandIndex.get(JSON.stringify([property.source_file, property.line]));
      if (position === undefined || numeric(property.value) === null) fail('model_frequency_replay_unavailable');
      const row = { kind: property.name.toLowerCase() === 'basefreq' ? 'element_base_frequency' : 'source_frequency',
        object_id: object.class.toLowerCase() === 'circuit' ? 'vsource.source' : object.name.toLowerCase(),
        source_file: property.source_file, line: property.line, property: property.name.toLowerCase(),
        value_hz: numeric(property.value), command_index: position, property_index: propertyIndex };
      if (row.kind === 'element_base_frequency') bases.push(row);
      else if (['circuit', 'vsource'].includes(object.class.toLowerCase())) sourceEvents.push(row);
    }
  }
  sourceEvents.sort((a, b) => a.command_index - b.command_index || a.property_index - b.property_index);
  const finalSources = new Map();
  for (const event of sourceEvents) finalSources.set(event.object_id, event);
  return { sources: [...finalSources.values()], bases };
}

function nativeFrequency(inspection, entries, files) {
  const commandIndex = new Map();
  for (const [index, command] of inspection.commands.entries()) {
    if (!plain(command) || !entries.some(row => row.path === command.file) || !Number.isSafeInteger(command.line) || command.line <= 0 || typeof command.command !== 'string') fail('model_frequency_replay_unavailable');
    const key = JSON.stringify([command.file, command.line]);
    if (commandIndex.has(key)) return { status: 'ambiguous', value_hz: null, sources: [], reason: 'repeated_command_location' };
    commandIndex.set(key, index);
  }
  const declared = declarations(inspection, commandIndex);
  const observed = [...declared.sources, ...declared.bases];
  const unresolved = reason => ({ status: ['authorized_source_text_not_available', 'solution_frequency_not_explicit', 'frequency_context_changed_after_declaration'].includes(reason) ? 'missing' : 'ambiguous', value_hz: null,
    sources: observed.slice(0, 20), source_count: observed.length, sources_truncated: observed.length > 20, reason });
  if (new Set(declared.sources.map(row => row.value_hz)).size > 1) return unresolved('conflicting_explicit_source_frequencies');
  if (files === undefined) return unresolved('authorized_source_text_not_available');
  const bound = bindFiles(entries, files), texts = new Map(), candidates = [];
  let current = null, contextAfterFrequency = false, clearCount = 0, circuitSeen = false;
  const redirects = new Set();
  for (const [index, command] of inspection.commands.entries()) {
    if (!texts.has(command.file)) texts.set(command.file, cleanLines(bound.get(command.file).text));
    const line = texts.get(command.file)[command.line - 1];
    if (line === undefined) return unresolved('command_source_location_unavailable');
    const words = tokens(line), verb = words[0]?.toLowerCase();
    if (verb !== command.command.toLowerCase()) return unresolved('command_source_disagrees');
    if (verb === 'compile') return unresolved('compile_context_not_supported');
    if (!COMMANDS.has(verb)) return unresolved('command_context_not_supported');
    if (verb === 'redirect') {
      if (words.length !== 2) return unresolved('redirect_not_replayable');
      const target = scalar(words[1]).toLowerCase();
      const matches = inspection.dependencies.filter(row => row.kind === 'script' && row.source === command.file && row.line === command.line && row.target.toLowerCase() === target);
      if (matches.length !== 1) return unresolved('redirect_dependency_not_verified');
      if (redirects.has(target)) return unresolved('repeated_redirect');
      redirects.add(target);
    }
    if (verb === 'clear') {
      if (++clearCount > 1 || circuitSeen) return unresolved('clear_context_not_supported');
      if (current) contextAfterFrequency = true;
      current = null;
    }
    if (verb === 'new' && /^circuit\./i.test(scalar(words[1] ?? ''))) {
      if (circuitSeen) return unresolved('multiple_circuit_contexts');
      circuitSeen = true;
      if (current) contextAfterFrequency = true;
      current = null;
    }
    // CalcVoltageBases is allowed initialization, not a new user frequency
    // declaration. Keep the explicit Set frequency as the expected source
    // declaration; the core still checks actual frequency before every solve.
    if (verb !== 'set') continue;
    if (words.length < 4 || (words.length - 1) % 3) return unresolved('set_assignments_not_replayable');
    for (let position = 1; position < words.length; position += 3) {
      const key = words[position].toLowerCase();
      if (words[position + 1] !== '=' || !SET_OPTIONS.has(key)) return unresolved('set_assignments_not_replayable');
      if (CONTEXT_OPTIONS.has(key) && current) { current = null; contextAfterFrequency = true; }
      if (key === 'frequency') {
        const value = numeric(words[position + 2]);
        if (value === null) return unresolved('frequency_not_explicit_supported_number');
        current = { kind: 'solution_frequency', source_file: command.file, line: command.line,
          property: 'frequency', value_hz: value, command_index: index, property_index: (position - 1) / 3 };
        candidates.push(current);
        contextAfterFrequency = false;
      }
    }
  }
  observed.unshift(...candidates);
  if (new Set(candidates.map(row => row.value_hz)).size > 1) return unresolved('conflicting_explicit_solution_frequencies');
  if (!current) return unresolved(contextAfterFrequency ? 'frequency_context_changed_after_declaration' : 'solution_frequency_not_explicit');
  if (declared.sources.some(row => row.value_hz !== current.value_hz)) return unresolved('source_solution_frequency_conflict');
  return { status: 'explicit', value_hz: current.value_hz, sources: [current],
    source_count: candidates.length, reason: 'manifest_bound_explicit_set_frequency',
    related_source_frequencies: declared.sources.slice(0, 20), related_source_frequency_count: declared.sources.length,
    related_source_frequencies_truncated: declared.sources.length > 20,
    element_base_frequency_count: declared.bases.length };
}

/** Accept only a fresh trusted Bridge.inspectModel result; optional files are
 * exactly the explicit authorized import payload, never workspace discovery.
 * Source declarations are not a numerical or engineering acceptance result.
 */
export function summarizeModel(inspected, { snapshotId = inspected?.snapshot_id, files } = {}) {
  const manifest = inspected?.manifest, inspection = inspected?.inspection;
  if (!plain(inspected) || !MODEL.test(snapshotId ?? '') || inspected.snapshot_id !== snapshotId
      || !plain(manifest) || manifest.snapshot_id !== snapshotId || !HASH.test(inspected.fingerprint ?? '')
      || manifest.fingerprint !== inspected.fingerprint || snapshotId !== 'model-' + inspected.fingerprint
      || !validPath(manifest.master_file) || !Array.isArray(manifest.entries) || manifest.entries.length < 1 || manifest.entries.length > 500
      || !plain(inspection) || inspection.status !== 'supported_snapshot_input' || inspection.numerical_validity !== 'not_run'
      || !Array.isArray(inspection.commands) || !Array.isArray(inspection.objects) || !Array.isArray(inspection.dependencies)) fail('invalid_model_inspection');
  const names = new Set();
  for (const entry of manifest.entries) {
    if (!plain(entry) || !validPath(entry.path) || names.has(entry.path) || !HASH.test(entry.sha256 ?? '')
        || !Number.isSafeInteger(entry.bytes) || entry.bytes < 0) fail('invalid_model_inspection');
    names.add(entry.path);
  }
  if (!names.has(manifest.master_file)) fail('invalid_model_inspection');
  for (const dependency of inspection.dependencies) {
    if (!plain(dependency) || !names.has(dependency.source) || !names.has(dependency.target)
        || !Number.isSafeInteger(dependency.line) || dependency.line <= 0) fail('invalid_model_inspection');
  }
  const converted = inspected.source_format === 'MATPOWER';
  const table = ['CSV','JSON'].includes(inspected.source_format);
  let frequency;
  if (table) {
    const conversion=inspected.conversion, value=conversion?.frequency_hz;
    if (!plain(conversion) || conversion.source_format !== inspected.source_format || conversion.adapter_version !== 'grid-tables-1.0' || conversion.physical_phase_mapping !== true || conversion.balanced_equivalent !== false || !Number.isFinite(value) || value<=0 || value>1000 || !Array.isArray(conversion.source_files)) fail('invalid_model_conversion');
    if (files !== undefined) bindFiles(conversion.source_files,files);
    frequency={status:'explicit',value_hz:value,reason:'core_verified_grid_table_parameter',sources:[{kind:'explicit_grid_table_system_frequency',source_file:conversion.source_files[0].path,property:'frequency_hz',value_hz:value,pointer:'/conversion/frequency_hz'}]};
  } else if (converted) {
    const conversion = inspected.conversion, value = conversion?.parameters?.frequency_hz;
    if (!plain(conversion) || conversion.source_format !== 'MATPOWER' || conversion.target_format !== 'OpenDSS'
        || conversion.native_master_file !== manifest.master_file || !validPath(conversion.source_file) || conversion.balanced_equivalent !== true
        || conversion.physical_phase_mapping !== false || typeof value !== 'number' || !Number.isFinite(value) || value <= 0 || value > 1000) fail('invalid_model_conversion');
    if (files !== undefined) bindFiles(manifest.entries, files);
    frequency = { status: 'explicit', value_hz: value, reason: 'core_verified_conversion_parameter',
      sources: [{ kind: 'caller_supplied_conversion_parameter', source_file: '__tianshu_input__/request.json',
        property: 'frequency_hz', value_hz: value, pointer: '/conversion/parameters/frequency_hz' }] };
  } else {
    if (inspected.source_format !== 'OpenDSS' || inspected.conversion != null) fail('invalid_model_inspection');
    try { frequency = nativeFrequency(inspection, manifest.entries, files); }
    catch (error) {
      if (error instanceof ModelSummaryError && error.code === 'model_frequency_replay_unavailable') frequency = { status: 'ambiguous', value_hz: null, sources: [], reason: error.code };
      else throw error;
    }
  }
  const classes = {};
  for (const object of inspection.objects) {
    if (!plain(object) || typeof object.class !== 'string') fail('invalid_model_inspection');
    const key = object.class.toLowerCase();
    if (!/^[a-z][a-z0-9_]*$/.test(key) || ['constructor', 'prototype', '__proto__'].includes(key)) fail('invalid_model_inspection');
    classes[key] = (classes[key] ?? 0) + 1;
  }
  const required = frequency.status === 'explicit' ? [] : [{ field: frequency.status === 'missing' ? 'operating_conditions.frequency_hz' : 'model', reason: frequency.reason,
    question: frequency.status === 'missing'
      ? '模型尚无可确认的求解频率。请告诉我本次按多少 Hz 校核？'
      : '模型频率声明存在矛盾或无法可靠静态重放，需先核对原模型及依赖；需要修正模型时须另行明确授权。' }];
  frequency.evidence = frequency.sources.slice(0, 20).map(row => ({ source_file: row.source_file, line: row.line ?? null,
    detail: `${row.kind}: ${row.property}=${row.value_hz} Hz${row.object_id ? `; object=${row.object_id}` : ''}${row.pointer ? `; ${row.pointer}` : ''}` }));
  frequency.evidence_truncated = frequency.sources_truncated === true || frequency.sources.length > 20;
  return { schema_version: '1.0', source: 'professional_static_inspection', model: { snapshot_id: snapshotId },
    model_identity: { snapshot_id: snapshotId, fingerprint: inspected.fingerprint, master_file: manifest.master_file,
      source_format: inspected.source_format, file_count: manifest.entries.length, source_file_count: converted ? 1 : table ? inspected.conversion.source_files.length : manifest.entries.length,
      source_file: converted ? inspected.conversion.source_file : table ? inspected.conversion.source_files[0].path : manifest.master_file },
    dependency_status: 'verified_within_explicit_manifest', dependency_count: inspection.dependencies.length,
    capability: { input_status: inspection.status, numerical_validity: 'not_run', engineering_accepted: false,
      balanced_equivalent: converted, physical_phase_mapping: converted ? false : table ? true : null,
      phase_semantics: converted ? 'synthetic_balanced_embedding_not_physical_phases' : 'native_nodes_preserved_not_inferred',
      device_semantics_status: inspected.device_semantics?.status ?? 'not_available', object_counts: classes },
    frequency, required_inputs: required,
    limitations: ['Static source summary only; actual solution frequency remains subject to mandatory backend readback.',
      'No default frequency, phase assignment, project limit, or engineering compliance is inferred.'] };
}
