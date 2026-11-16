/** Native tool contracts for Harness 0.2.1-rc.2. Copyright (c) yangshaopeng. */
const text = description => ({ type: 'string', required: true, description });
const object = properties => ({ type: 'object', additionalProperties: false, properties });

export const resultOutput = {
  type: 'object', additionalProperties: false,
  properties: {
    operation: text('Executed professional operation'),
    summary: text('Brief factual execution summary'),
    data: { type: 'object', required: true, additionalProperties: true },
  },
};

export const attachmentListParameters = {
  offset: { type: 'integer', description: 'Metadata-only current Session file cursor, default 0.' },
  limit: { type: 'integer', description: 'Maximum metadata page length 1–100, default 50; actual page also respects output budget. Continue with next_offset when more=true.' },
};

export const importParameters = {
  source: { type: 'string', enum: ['workspace', 'native_attachments'], description: 'Omitted means the unchanged legacy workspace input. The two input branches cannot be mixed.' },
  format: { type: 'string', enum: ['OpenDSS', 'MATPOWER', 'CSV', 'JSON'], description: 'CSV/JSON require the documented tianzhi-grid/1.0 explicit phase-aware model tables; arbitrary data JSON is not a grid model. Workspace filename can determine format.' },
  workspace_files: { type: 'array', items: { type: 'string' }, description: 'Daily workspace input: actual user-selected workspace-relative POSIX paths, including the entry. Host derives package_root/files and by default follows only literal same-package dependencies under native authorization. No directory scan or invented files. Do not mix with package_root/files/attachments. One model or one Master.dss determines the entry.' },
  include_dependencies: { type: 'boolean', description: 'Workspace OpenDSS only: follow bounded literal same-package references, without scans or parent/private paths. Default true with daily workspace_files; default false with legacy explicit package_root/files. False preserves an explicit restricted file list. Native attachment input cannot use this option.' },
  package_root: { type: 'string', description: 'Required for workspace source only: relative directory, . allowed. Never an absolute path.' },
  master_file: { type: 'string', description: 'Legacy/native: required package-relative master. With workspace_files: optional exact selected path or unambiguous selected filename; omit only for one .dss/.m or one Master.dss.' },
  files: { type: 'array', items: { type: 'string' }, description: 'Required for workspace source only: complete package-relative list, including master; 1–500 files/20MiB. No scan.' },
  attachments: { type: 'array', items: object({ attachment_key: text('Exact Host key from this Session list_attachments; never raw attachmentId or path.'), package_path: text('Explicit package-relative POSIX destination, preserving selected dependency tree.') }), description: 'Required for native_attachments source only: exact 1–500 chosen keys and paths, including master. No workspace fields.' },
  frequency_hz: { type: 'number', description: 'Required only for MATPOWER; explicitly supplied system frequency.' },
  source_impedance_ohm: { ...object({ r: { type: 'number', required: true }, x: { type: 'number', required: true } }), description: 'Required only for MATPOWER; explicit nonnegative source R/X, not both zero.' },
};

export const runParameters = {
  model: { oneOf: [object({ case_id: text('Explicit registered case') }), object({ snapshot_id: text('Snapshot returned for this Session selected file') })], description: 'Preferred daily mode. Provide only this model and frequency_hz if genuinely missing. Host prepares fixed existing-operating-point settings and requests native authorization. Do not also provide request.' },
  frequency_hz: { type: 'number', description: 'Daily mode only: explicit frequency supplied by the user if the verified model summary is missing it. Never guess 50/60 or copy from another task. Contradictory evidence is rejected.' },
  request: {
    ...object({
      schema_version: { type: 'string', const: '1.1', required: true },
      model: { oneOf: [object({ case_id: text('Registered case identifier') }), object({ snapshot_id: text('This Session imported snapshot identifier') })], required: true },
      scenario: { ...object({ scenario_id: text('Explicit scenario identifier'), mode: { type: 'string', const: 'existing_snapshot', required: true } }), required: true },
      solver: { ...object({
        engine: { type: 'string', const: 'OpenDSS', required: true },
        transport: { type: 'string', const: 'com', required: true },
        solve_mode: { type: 'string', const: 'snapshot', required: true },
        max_iterations: { type: 'integer', required: true },
        tolerance: { type: 'number', required: true },
      }), required: true },
      recovery: { ...object({ policy: { type: 'string', const: 'none', required: true }, max_retries: { type: 'integer', const: 0, required: true } }), required: true },
      criteria: { oneOf: [{ type: 'null' }, { type: 'object', additionalProperties: true }], required: true, description: 'Confirmed ProjectCriteria 2.0 or null. ProjectCriteria fields: schema_version=2.0, criteria_id, version, source, confirmed_by, confirmed_at, rules. Each voltage rule: rule_id, kind=voltage, object_id (actual bus), node (physical node), reference_node=0 for node-to-ground, unit=pu, measurement_basis=node_to_ground, base_voltage_v (explicit confirmed phase-to-ground base), lower, upper, source. Collect missing confirmation in one concise user question; never fabricate it. Use this advanced request for requested backend limit checks; reading old results and comparing them in text does not apply or persist criteria. Null leaves engineering checks unassessed.' },
      operating_conditions: { ...object({
        frequency_hz: { type: 'number', required: true }, source: text('User supplied declaration source'),
        confirmed_by: text('User supplied confirmer; not Host authentication'), confirmed_at: text('User supplied ISO date/time'),
        mode: { type: 'string', const: 'validate_model_operating_point', required: true },
      }), required: true },
      result_scopes: { type: 'array', items: { type: 'string', enum: ['bus', 'branch', 'transformer', 'system'] }, required: true, description: 'Include all four domains exactly once.' },
      output_formats: { type: 'array', items: { type: 'string', enum: ['json', 'html', 'csv'] }, required: true, description: 'Include all three formats exactly once.' },
    }),
    description: 'Advanced mode only: complete immutable RunRequest 1.1 without request_id; preserves user-declared metadata. Do not mix with daily model/frequency_hz. Ordinary users need not fill this object.',
  },
};

export const taskParameters = { task_id: text('Task identifier returned to this Session; never infer another Session identity.') };
export const queryParameters = {
  ...taskParameters,
  view: { type: 'string', enum: ['summary', 'assessment', 'engineering_metrics', 'observations', 'bus_nodes', 'element_terminals'], description: 'Default summary. engineering_metrics reads recorded engineering_analysis.metrics (loss/loading/basis/evidence); assessment reads rule decisions; other views read native observations. No recalculation.' },
  object_id: { type: 'string', description: 'Optional exact object identifier filter; no regex.' },
  metric: { type: 'string', description: 'Exact recorded metric name, only for engineering_metrics; e.g. network_active_loss_kw. No inferred or translated aliases.' },
  unit: { type: 'string', description: 'Exact recorded unit, only for engineering_metrics.' },
  measurement_basis: { type: 'string', description: 'Exact recorded basis, only for engineering_metrics; native LN base is distinct from project-confirmed base.' },
  phase: { type: 'string', description: 'Exact recorded phase label, only for engineering_metrics; never infer a physical phase.' },
  node: { type: 'integer', description: 'Exact recorded node ID, only for engineering_metrics.' },
  terminal: { type: 'integer', description: 'Exact one-based terminal ID, only for engineering_metrics.' },
  conductor: { type: 'integer', description: 'Exact one-based conductor ID, only for engineering_metrics.' },
  criteria_rule_id: { type: 'string', description: 'Exact recorded project rule ID, only for engineering_metrics.' },
  status: { type: 'string', enum: ['available', 'pass', 'violation', 'not_assessed', 'not_applicable'], description: 'assessment accepts pass/violation/not_assessed/not_applicable; engineering_metrics accepts available/not_assessed/not_applicable. Status domains cannot be mixed.' },
  offset: { type: 'integer', description: 'Default 0; zero-based pagination after filters.' },
  limit: { type: 'integer', description: 'Default 10; 1–20 rows.' },
  evidence_offset: { type: 'integer', description: 'engineering_metrics only: default0, zero-based per-row evidence-reference page.' },
  evidence_limit: { type: 'integer', description: 'engineering_metrics only: default8, 1–20 exact recorded evidence references per row.' },
};

export const reportParameters = {...taskParameters, format:{type:'string',enum:['html','pdf'],description:'Default HTML; PDF is a read-only print derivation from the committed HTML, never a new solve.'}};
export const studyParameters = {
  model:runParameters.model, frequency_hz:runParameters.frequency_hz, request:runParameters.request,
  curve_source:{type:'string',description:'Explicit source and interpretation of an inline curve; file mode records the selected filename and hash.'},
  curve_workspace_file:{type:'string',description:'User-selected workspace-relative CSV/JSON curve file. Host reads after native authorization; do not also provide points. This is a time curve, not a grid model.'},
  points:{type:'array',items:object({at:text('ISO8601 timestamp including timezone, strictly increasing.'),
    load_multiplier:{type:'number',description:'Explicit global load multiplier; omitted means retain base model value.'},
    generator_multiplier:{type:'number',description:'Explicit generator multiplier; does not set PV irradiance.'},
    setpoints:{type:'array',items:object({object_id:text('Exact existing Load/Generator/PVSystem object ID.'),properties:{type:'object',required:true,additionalProperties:true,description:'Load/Generator kw,kvar or pf; PV irradiance,temperature,kvar or pf. No topology or ratings changes.'}})}})},
  on_point_error:{type:'string',enum:['stop','continue'],required:true,description:'Explicit treatment of a failed time point.'},
};
export const studyQueryParameters = {study_id:{type:'string',description:'Exact owned study ID; omit only with history=true.'},history:{type:'boolean',description:'Read this Session existing study history after an interrupted tool reply; never submit or replay.'},offset:{type:'integer',description:'Default 0.'},limit:{type:'integer',description:'Default 10, maximum20.'}};
export const comparisonParameters = {left_task_id:text('First owned task ID.'),right_task_id:text('Second owned task ID.'),metric:{type:'string',description:'Optional exact metric filter.'},offset:{type:'integer'},limit:{type:'integer',description:'Default10, maximum20.'}};
