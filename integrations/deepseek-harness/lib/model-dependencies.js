/** Static file references under professional/models.py native-snapshot-1.0.
 * Copyright (c) yangshaopeng. No filesystem, network, process, or solver I/O.
 */
const POLICY = 'native-snapshot-1.0';
const MAX_BYTES = 20 * 1024 * 1024;
const RESERVED = /^(?:con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?$/i;
const SPACE = /[\t-\r\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/;
const OBJECT = /^([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z0-9_][A-Za-z0-9_-]{0,127})$/;
const CLASSES = new Set(['circuit', 'vsource', 'line', 'linecode', 'transformer', 'load', 'generator', 'pvsystem', 'xycurve', 'capacitor', 'reactor']);
const SET = new Set(['voltagebases', 'basefrequency', 'defaultbasefrequency', 'frequency', 'mode', 'controlmode', 'maxiterations', 'tolerance', 'algorithm', 'loadmodel', 'loadmult', 'genmult']);
const EXTERNAL = /(?:file|csvfile|pqcsvfile|sngfile|dblfile)\s*=/i;
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
class Problem extends Error {
  constructor(status, code, message, line = null) { super(message); this.status = status; this.code = code; this.line = line; }
}
const reject = (code, message, line, status = 'unsupported') => { throw new Problem(status, code, message, line); };

function path(value, line = null) {
  if (typeof value !== 'string' || !value.isWellFormed() || !value || [...value].length > 240 || value.startsWith('/')
      || /[\\:<>"|?*%@$`\x00-\x1f]/.test(value)) reject('invalid_dependency_path', 'A bounded package-root relative POSIX path is required.', line, 'invalid');
  for (const part of value.split('/')) {
    if (!part || part === '.' || part === '..' || /[. ]$/.test(part) || RESERVED.test(part)
        || RESERVED.test(part.split('.')[0].replace(/[ .]+$/, '')) || /~[0-9]/.test(part)) {
      reject('invalid_dependency_path', 'Unsafe or aliased Windows path component.', line, 'invalid');
    }
  }
  return value;
}
const scalar = value => value.length >= 2 && ['"', "'"].includes(value[0]) && value.at(-1) === value[0] ? value.slice(1, -1) : value;

function uncomment(text) {
  // Span copying mirrors the core parser, including comment newlines. A ! or
  // // comment ends at LF; neither strings nor block comments execute anything.
  const out = [];
  let start = 0, quote = null, index = 0;
  while (index < text.length) {
    const char = text[index], pair = text.slice(index, index + 2);
    if (quote) {
      if (char === quote) quote = null;
      else if (char === '\r' || char === '\n') reject('unsupported_quoted_value', 'Multiline quoted DSS values are unsupported.');
      index++;
    } else if (char === '"' || char === "'") { quote = char; index++; }
    else if (pair === '/*') {
      out.push(text.slice(start, index));
      const end = text.indexOf('*/', index + 2);
      if (end < 0) reject('invalid_dss_comment', 'Unclosed DSS block comment.', null, 'invalid');
      out.push(text.slice(index + 2, end).replace(/[^\n]/g, ' '));
      index = end + 2; start = index;
    } else if (char === '!' || pair === '//') {
      out.push(text.slice(start, index));
      const end = text.indexOf('\n', index);
      index = end < 0 ? text.length : end; start = index;
    } else index++;
  }
  if (quote) reject('invalid_dss_quote', 'Unclosed DSS quoted value.', null, 'invalid');
  out.push(text.slice(start));
  return out.join('');
}

function tokens(line, number) {
  const result = [], pairs = { '[': ']', '(': ')', '{': '}' };
  for (let index = 0; index < line.length;) {
    if (SPACE.test(line[index]) || line[index] === ',') { index++; continue; }
    if (line[index] === '=') { result.push('='); index++; continue; }
    const start = index, stack = [];
    let quote = null;
    for (; index < line.length; index++) {
      const char = line[index];
      if (quote) { if (char === quote) quote = null; }
      else if (char === '"' || char === "'") quote = char;
      else if (pairs[char]) stack.push(pairs[char]);
      else if ('])}'.includes(char)) { if (stack.pop() !== char) reject('invalid_dss_delimiters', 'Unbalanced DSS value delimiters.', number, 'invalid'); }
      else if (!stack.length && (SPACE.test(char) || char === ',' || char === '=')) break;
    }
    if (quote || stack.length) reject('unsupported_dss_delimiters', 'Multiline or unclosed value delimiters are unsupported.', number);
    const value = line.slice(start, index);
    if (!value || value.includes(';')) reject('unsupported_multiple_commands', 'Multiple-command tokens are unsupported.', number);
    result.push(value);
  }
  return result;
}

function assignments(words, line) {
  if (words.length % 3 || words.some((word, index) => index % 3 === 1 && word !== '=')) reject('unsupported_dss_assignment', 'Only explicit named DSS properties are supported.', line);
  const result = [];
  for (let index = 0; index < words.length; index += 3) {
    const name = words[index].toLowerCase(), value = words[index + 2];
    if (!/^[%A-Za-z][%A-Za-z0-9_-]*$/.test(name)) reject('invalid_dss_property', 'Invalid DSS property name.', line, 'invalid');
    if (/[@$`]/.test(value) || /%result%/i.test(value)) reject('unsupported_dynamic_reference', 'Dynamic variables and external expressions are unsupported.', line);
    result.push([name, value]);
  }
  return result;
}

/** Parse one already-authorized DSS text. Targets remain package-root relative,
 * exactly as core _inspect.reference; never resolve them against source dirname.
 * A supported result concerns reference syntax only, not complete model validity.
 * On any invalid/unsupported construct, no dependencies are released for reads.
 */
export function parseModelDependencies(input) {
  const dependencies = [], source = typeof input?.source_file === 'string' ? input.source_file : null;
  let physicalLines = 0;
  try {
    if (!plain(input) || Object.keys(input).some(key => !['source_file', 'master_file', 'content_utf8'].includes(key))) reject('invalid_dependency_input', 'Expected source_file, master_file and content_utf8.', null, 'invalid');
    const sourceFile = path(input.source_file), master = path(input.master_file);
    if (typeof input.content_utf8 !== 'string' || !input.content_utf8.isWellFormed() || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(input.content_utf8)) reject('invalid_dss_text', 'Source must be UTF-8 text without unsupported control characters.', null, 'invalid');
    if (Buffer.byteLength(input.content_utf8, 'utf8') > MAX_BYTES) reject('dependency_byte_budget', 'The package byte budget is 20 MiB.');
    const root = sourceFile === master;
    const text = uncomment(input.content_utf8.replace(/^\uFEFF/, ''));
    physicalLines = (text.match(/\n/g) ?? []).length + 1;
    if (physicalLines > 100000) reject('dependency_line_budget', 'The package line budget is 100000.');
    const lines = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\u0085\u2028\u2029]/), scripts = new Set();
    let active = null;
    const reference = (raw, line, kind) => {
      if (!root) reject('unsupported_leaf_dependency', 'Leaf DSS scripts cannot reference package files under the current core policy.', line);
      const target = path(scalar(raw), line);
      if (kind === 'script') {
        const key = target.toLowerCase();
        if (key === master.toLowerCase() || scripts.has(key)) reject('unsupported_repeated_script', 'Repeated or cyclic script inclusion is unsupported.', line);
        scripts.add(key);
      }
      dependencies.push({ source: sourceFile, line, target, kind });
    };
    for (const [index, line] of lines.entries()) {
      const number = index + 1, words = tokens(line, number);
      if (!words.length) continue;
      const command = words[0].toLowerCase();
      if (command === 'compile') reject('unsupported_compile', 'Internal Compile changes directory state and is unsupported by the current core.', number);
      if (command === 'redirect' || command === 'buscoords') {
        if (words.length !== 2) reject('invalid_dependency_arguments', 'Exactly one quoted or bare file path is required.', number, 'invalid');
        reference(words[1], number, command === 'redirect' ? 'script' : 'bus_coordinates'); active = null; continue;
      }
      if (command === 'clear' || command === 'calcvoltagebases') {
        if (words.length !== 1) reject('invalid_dss_command', 'Unexpected command arguments.', number, 'invalid');
        if (command === 'clear' && !root) reject('unsupported_leaf_clear', 'Clear is not supported in leaf DSS scripts.', number);
        active = null; continue;
      }
      if (command === 'set') {
        const values = assignments(words.slice(1), number);
        if (!values.length || values.some(([name]) => !SET.has(name))) reject('unsupported_dss_setting', 'DSS setting is outside the core snapshot policy.', number);
        active = null; continue;
      }
      if (command === 'open' || command === 'close') {
        if (words.length < 2 || !OBJECT.test(scalar(words[1]))) reject('unsupported_dss_object', 'An explicit object identifier is required.', number);
        assignments(words.slice(2), number); active = null; continue;
      }
      let kind, values;
      if (command === 'new' || command === 'edit') {
        const match = typeof words[1] === 'string' ? OBJECT.exec(scalar(words[1])) : null;
        if (!match || !CLASSES.has(match[1].toLowerCase())) reject('unsupported_dss_object', 'Object class or identifier is outside the core snapshot policy.', number);
        active = kind = match[1].toLowerCase(); values = assignments(words.slice(2), number);
      } else if (command === '~') {
        if (active === null) reject('unsupported_dss_continuation', 'Continuation requires preceding New/Edit in the same file.', number);
        kind = active; values = assignments(words.slice(1), number);
      } else reject('unsupported_dss_command', 'Command or abbreviation is outside the core snapshot policy.', number);
      for (const [name, value] of values) {
        if (name === 'csvfile') {
          if (kind !== 'xycurve') reject('unsupported_file_property', 'csvfile is supported only for XYCurve.', number);
          reference(value, number, 'xycurve_csv');
        } else if (EXTERNAL.test(value)) {
          if (kind !== 'xycurve' || !['xarray', 'yarray'].includes(name) || !root) reject('unsupported_external_value', 'External value files are unsupported in this property/context.', number);
          if (!['[', '('].includes(value[0]) || ![']', ')'].includes(value.at(-1))) reject('invalid_external_array', 'External numeric arrays need one bracketed file expression.', number, 'invalid');
          const nested = assignments(tokens(value.slice(1, -1), number), number);
          if (nested.length !== 1 || nested[0][0] !== 'file') reject('unsupported_external_array', 'Only one literal UTF-8 File expression is supported.', number);
          reference(nested[0][1], number, 'numeric_array');
        }
      }
    }
    return { policy_version: POLICY, scope: 'file_references_only_not_model_validation', status: 'supported', source_file: sourceFile,
      physical_line_count: physicalLines, dependencies, issues: [] };
  } catch (error) {
    if (!(error instanceof Problem)) throw error;
    return { policy_version: POLICY, scope: 'file_references_only_not_model_validation', status: error.status, source_file: source,
      physical_line_count: physicalLines, dependencies: [], issues: [{ code: error.code, source_file: source, line: error.line, message: error.message }] };
  }
}
