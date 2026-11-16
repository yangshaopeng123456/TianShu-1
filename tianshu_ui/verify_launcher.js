// Windows launcher regression: execute unmodified BAT copies with inert Node
// children. No Electron package, GUI, backend, COM, install or network is used.
const assert = require('assert/strict');
const { spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

if (process.platform !== 'win32') {
  console.error('[FAIL] verify_launcher requires Windows cmd.exe; no BAT execution was performed.');
  process.exit(1);
}

const artifactRoot = path.resolve(process.env.DPF_TEST_ARTIFACT_ROOT || path.join(os.tmpdir(), 'tianshu-test-artifacts'));
fs.mkdirSync(artifactRoot, { recursive: true });
const workDir = fs.mkdtempSync(path.join(artifactRoot, 'launcher-'));
const source = path.join(__dirname, 'start-tianshu.bat');
const sourceBytes = fs.readFileSync(source);
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const cmd = path.join(process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows', 'System32', 'cmd.exe');
const results = [];

function runCase(name, options) {
  const appDir = path.join(workDir, name, 'app with spaces');
  fs.mkdirSync(appDir, { recursive: true });
  const bat = path.join(appDir, 'start-tianshu.bat');
  const witnessFile = path.join(appDir, 'inert-node-witness.json');
  fs.writeFileSync(bat, sourceBytes);
  assert.equal(digest(fs.readFileSync(bat)), digest(sourceBytes), 'The executed BAT must be an exact source copy');
  if (!options.missingLaunch) {
    fs.writeFileSync(path.join(appDir, 'launch.js'), [
      "const fs = require('fs');",
      "fs.writeFileSync(process.env.DPF_TEST_LAUNCHER_WITNESS, JSON.stringify({",
      "  pid: process.pid, cwd: process.cwd(),",
      "  cleanNodeOptions: !process.env.NODE_OPTIONS,",
      "  cleanElectronRunAsNode: !process.env.ELECTRON_RUN_AS_NODE,",
      "  cleanElectronAttachConsole: !process.env.ELECTRON_NO_ATTACH_CONSOLE",
      "}));",
      `process.exit(${options.childExit || 0});`,
      '',
    ].join('\n'));
  }
  if (!options.missingElectron) fs.mkdirSync(path.join(appDir, 'node_modules', 'electron'), { recursive: true });
  const env = { ...process.env };
  // Windows treats environment names case-insensitively. Avoid duplicated Path
  // names and prevent PATH fallback in the explicit missing-Node case.
  for (const key of Object.keys(env)) if (key.toLowerCase() === 'path' || key.toLowerCase() === 'node_home') delete env[key];
  env.PATH = options.missingNode ? '' : (options.pathOnly ? path.dirname(process.execPath) : '');
  env.NODE_HOME = options.missingNode || options.pathOnly ? '' : path.dirname(process.execPath);
  env.NODE_OPTIONS = '--invalid-host-injected-option';
  env.ELECTRON_RUN_AS_NODE = '1';
  env.ELECTRON_NO_ATTACH_CONSOLE = '1';
  env.DPF_TEST_LAUNCHER_WITNESS = witnessFile;
  const child = spawnSync(cmd, ['/d', '/s', '/c', `""${bat}""`], {
    cwd: workDir, env, input: '\r\n', encoding: 'utf8', windowsHide: true,
    windowsVerbatimArguments: true, timeout: 15000,
  });
  const result = {
    name, expectedExit: options.expectedExit, actualExit: child.status,
    signal: child.signal, error: child.error ? child.error.message : null,
    stdout: child.stdout || '', stderr: child.stderr || '',
    batSha256: digest(fs.readFileSync(bat)),
    witness: fs.existsSync(witnessFile) ? JSON.parse(fs.readFileSync(witnessFile, 'utf8')) : null,
  };
  try {
    assert.ifError(child.error);
    assert.equal(child.signal, null);
    assert.equal(child.status, options.expectedExit, `BAT exit status for ${name}`);
    if (options.errorText) assert.ok(result.stdout.includes(options.errorText), `Expected actionable preflight message: ${options.errorText}`);
    if (options.missingNode || options.missingLaunch || options.missingElectron) {
      assert.equal(result.witness, null, 'Preflight failures must never start the inert child');
    } else {
      assert.ok(result.witness, 'The inert Node launch.js must actually run');
      assert.equal(result.witness.cwd.toLowerCase(), appDir.toLowerCase());
      assert.equal(result.witness.cleanNodeOptions, true);
      assert.equal(result.witness.cleanElectronRunAsNode, true);
      assert.equal(result.witness.cleanElectronAttachConsole, true);
      if (options.childExit) assert.ok(result.stdout.includes(`[ERROR] Exit code: ${options.childExit}`));
      else assert.ok(!result.stdout.includes('[ERROR] Exit code:'));
    }
    result.passed = true;
    console.log(`[OK] ${name}: actual cmd.exe exit ${child.status}`);
  } catch (error) {
    result.passed = false; result.assertion = error.message;
    console.error(`[FAIL] ${name}: ${error.message}`);
  }
  results.push(result);
  fs.writeFileSync(path.join(appDir, 'result.json'), JSON.stringify(result, null, 2));
}

runCase('child_exit_7', { childExit: 7, expectedExit: 7 });
runCase('child_exit_0', { childExit: 0, expectedExit: 0 });
runCase('missing_launch', { missingLaunch: true, expectedExit: 1, errorText: 'launch.js not found' });
runCase('missing_electron', { missingElectron: true, expectedExit: 1, errorText: 'npm ci && npm run setup:electron' });
runCase('missing_node', { missingNode: true, expectedExit: 1, errorText: 'Install Node.js 24.15 or later' });
runCase('path_node_exit_7', { pathOnly: true, childExit: 7, expectedExit: 7 });

const summary = { passed: results.every(result => result.passed), source, sourceSha256: digest(sourceBytes),
  platform: process.platform, nodeVersion: process.version, artifactRoot: workDir, results };
fs.writeFileSync(path.join(workDir, 'results.json'), JSON.stringify(summary, null, 2));
console.log(`Result: ${results.filter(result => result.passed).length}/${results.length} passed. Evidence: ${workDir}`);
process.exit(summary.passed ? 0 : 1);
