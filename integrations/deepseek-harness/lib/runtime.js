// Copyright (c) yangshaopeng. Licensed under Apache-2.0.
// Own only our Python service and explicitly published, immutable report files.
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, readFile, writeFile, rename, stat, realpath, lstat, readdir } from 'node:fs/promises';
import { createWriteStream } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { TianshuBridge } from './bridge.js';
import { loopbackFetch } from './loopback-http.js';

const terminal = new Set(['finished', 'failed', 'cancelled', 'interrupted']);
const allowedFiles = new Set(['report.html', 'report.pdf', 'result.json', 'points.csv', 'checks.csv', 'observations.json', 'observations.csv', 'engineering.csv']);
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const contained = (root, candidate) => {
  const relative = path.relative(root, candidate);
  return relative !== '' && !path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep);
};

async function connection(file) {
  const info = await lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 64 * 1024) throw new Error('connection file must be a bounded regular file');
  const data = JSON.parse(await readFile(file, 'utf8'));
  const url = new URL(data.url);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('professional service must use a plain 127.0.0.1 HTTP endpoint');
  }
  if (data.service !== 'tianshu-professional' || typeof data.token !== 'string' || data.token.length < 24) throw new Error('invalid local service identity');
  return data;
}

async function probe(file, expectedPid, expectedInstance, timeoutMs = 1500) {
  try {
    const data = await connection(file);
    if (expectedPid !== undefined && ((data.pid !== expectedPid && data.parent_pid !== expectedPid) || data.integration_instance !== expectedInstance)) return null;
    const response = await loopbackFetch(data.url + '/v1/health', {
      headers: { Authorization: 'Bearer ' + data.token }, signal: AbortSignal.timeout(timeoutMs), redirect: 'error',
    });
    if (!response.ok) return null;
    const body = await response.json();
    return body.service === 'tianshu-professional' && body.version === '0.14.4-diagnostics' && body.ready === true ? data : null;
  } catch { return null; }
}

async function coreIdentity(coreRoot, helper) {
  const codeRoot = path.join(coreRoot, 'dpf_agent', 'dpf_agent');
  const entries = [];
  async function walk(folder) {
    for (const item of await readdir(folder, { withFileTypes: true })) {
      if (item.name === '__pycache__') continue;
      const p = path.join(folder, item.name);
      if (item.isSymbolicLink()) throw new Error('core code must not contain linked entries');
      if (item.isDirectory()) await walk(p);
      else if (item.isFile() && item.name.endsWith('.py')) entries.push([path.relative(codeRoot, p).split(path.sep).join('/'), digest(await readFile(p))]);
    }
  }
  await walk(codeRoot);
  entries.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  return { core_python_sha256: digest(JSON.stringify(entries)), host_script_sha256: digest(await readFile(helper)) };
}

async function startOwnedService({ stateDir, coreRoot, python, casesRoot }) {
  if (!coreRoot || !python || !path.isAbsolute(coreRoot) || !path.isAbsolute(python)) throw new Error('trusted absolute coreRoot and python are required');
  await stat(path.join(coreRoot, 'dpf_agent', 'dpf_agent', 'professional', 'service.py'));
  await stat(python);
  const serviceDir = path.join(stateDir, 'professional');
  await mkdir(serviceDir, { recursive: true });
  if (await realpath(serviceDir) !== serviceDir) throw new Error('owned service directory must not be a link');
  const connectionFile = path.join(serviceDir, 'local-service.json');
  const helper = fileURLToPath(new URL('../service_host.py', import.meta.url));
  const expected = { ...await coreIdentity(coreRoot, helper),
    core_root: await realpath(coreRoot), state_dir: await realpath(serviceDir),
    cases_root: await realpath(casesRoot || path.join(coreRoot, 'dpf_agent', 'cases')) };
  // Reconnect after a plugin reload only if the existing endpoint is the owned
  // integration host; an unrelated service is never adopted or stopped.
  const existing = await probe(connectionFile);
  if (existing && existing.integration_instance && Number.isInteger(existing.pid)) {
    let live = false;
    try { process.kill(existing.pid, 0); live = true; } catch {}
    const response = await loopbackFetch(existing.url + '/_integration/identity', {
      headers: { Authorization: 'Bearer ' + existing.token }, signal: AbortSignal.timeout(timeoutMs), redirect: 'error',
    });
    const identity = response.ok ? await response.json() : null;
    const equal = (a, b) => process.platform === 'win32' ? String(a).toLowerCase() === String(b).toLowerCase() : a === b;
    if (!live || !identity || identity.pid !== existing.pid || identity.integration_instance !== existing.integration_instance ||
        Object.entries(expected).some(([key, value]) => !equal(identity[key], value))) {
      throw new Error('existing integration service uses different code or configuration; stop its owner before reconnecting');
    }
    return { connectionFile, child: null, async close() {} };
  }
  const instance = randomUUID();
  const args = ['-X', 'utf8', '-B', helper, '--core-root', coreRoot, '--state-dir', serviceDir, '--instance-id', instance];
  if (casesRoot) args.push('--cases-root', casesRoot);
  const log = createWriteStream(path.join(stateDir, 'professional-service.log'), { flags: 'a' });
  const child = spawn(python, args, { cwd: path.join(coreRoot, 'dpf_agent'), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.pipe(log, { end: false });
  child.stderr.pipe(log, { end: false });
  let spawnError;
  child.once('error', error => { spawnError = error; log.end(); });
  const exited = new Promise(resolve => child.once('exit', (code, signal) => { log.end(); resolve({ code, signal }); }));
  let ready;
  const startupDeadline = Date.now() + 20000;
  while (Date.now() < startupDeadline) {
    if (spawnError || child.exitCode !== null) break;
    ready = await probe(connectionFile, child.pid, instance, Math.max(1, Math.min(1500, startupDeadline - Date.now())));
    if (ready) break;
    await delay(100);
  }
  if (!ready) {
    // A Windows venv launcher can own a different Python PID. Never kill just
    // the launcher and assume that the actual service has also stopped.
    const cleanup = { integration_instance: instance, launcher_pid: child.pid ?? null,
      python_pid: null, shutdown_accepted: false, exit_confirmed: Boolean(spawnError) };
    try {
      const current = await connection(connectionFile);
      if (current.integration_instance === instance && (current.pid === child.pid || current.parent_pid === child.pid)) {
        cleanup.python_pid = current.pid;
        const response = await loopbackFetch(current.url + '/_integration/shutdown', {
          method: 'POST', headers: { Authorization: 'Bearer ' + current.token, 'X-Tianshu-Control': current.shutdown_token, 'Content-Type': 'application/json' },
          body: '{}', signal: AbortSignal.timeout(3000), redirect: 'error',
        });
        cleanup.shutdown_accepted = response.ok;
        if (response.ok) cleanup.exit_confirmed = Boolean(await Promise.race([exited, delay(15000, null, { ref: false })]));
      } else if (child.exitCode !== null) cleanup.exit_confirmed = true;
    } catch { if (child.exitCode !== null) cleanup.exit_confirmed = true; }
    await writeFile(path.join(stateDir, 'service-startup-failure-' + instance + '.json'), JSON.stringify(cleanup, null, 2) + '\n', { flag: 'wx' });
    throw new Error(cleanup.exit_confirmed
      ? 'owned professional service did not become ready; its startup process has exited; see the local service log'
      : 'owned professional service startup is unresolved; connection identity retained for recovery, no force termination attempted');
  }
  return {
    connectionFile, child,
    async close() {
      if (child.exitCode !== null) return;
      const current = await connection(connectionFile);
      if (current.pid !== ready.pid || (current.pid !== child.pid && current.parent_pid !== child.pid) || current.integration_instance !== instance) throw new Error('owned service identity changed; refusing process control');
      const response = await loopbackFetch(current.url + '/_integration/shutdown', {
        method: 'POST', headers: { Authorization: 'Bearer ' + current.token, 'X-Tianshu-Control': current.shutdown_token, 'Content-Type': 'application/json' },
        body: '{}', signal: AbortSignal.timeout(3000), redirect: 'error',
      });
      if (!response.ok) throw new Error('owned service did not accept graceful shutdown');
      const outcome = await Promise.race([exited, delay(30000, null, { ref: false })]);
      if (!outcome) throw new Error('owned service has not confirmed shutdown; no force termination attempted');
    },
  };
}

export async function createReportPublisher({ bridge, stateDir }) {
  const root = path.join(stateDir, 'report-cache');
  await mkdir(root, { recursive: true });
  const canonicalRoot = await realpath(root);
  const published = new Map();
  let closePromise;
  const server = createServer(async (request, response) => {
    try {
      if (!['GET', 'HEAD'].includes(request.method)) { response.writeHead(405); response.end(); return; }
      if (request.headers.host !== '127.0.0.1:' + server.address().port) { response.writeHead(403); response.end(); return; }
      if (request.headers.origin && request.headers.origin !== 'http://' + request.headers.host) { response.writeHead(403); response.end(); return; }
      const url = new URL(request.url, 'http://' + request.headers.host);
      const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
      if (url.search || parts.length !== 3 || parts[0] !== 'reports' || !/^[a-f0-9]{64}$/.test(parts[1]) || !allowedFiles.has(parts[2])) {
        response.writeHead(404); response.end(); return;
      }
      const item = published.get(parts[1])?.get(parts[2]);
      if (!item || (await lstat(item.path)).isSymbolicLink() || (await lstat(path.dirname(item.path))).isSymbolicLink() || !contained(canonicalRoot, await realpath(item.path))) {
        response.writeHead(404); response.end(); return;
      }
      const bytes = await readFile(item.path);
      if (bytes.length !== item.bytes || digest(bytes) !== item.sha256) { response.writeHead(409); response.end('report integrity failure'); return; }
      response.writeHead(200, {
        'Content-Type': parts[2].endsWith('.html') ? 'text/html; charset=utf-8' : parts[2].endsWith('.pdf') ? 'application/pdf' : parts[2].endsWith('.csv') ? 'text/csv; charset=utf-8' : 'application/json; charset=utf-8',
        'Content-Length': bytes.length, 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        ...(parts[2].endsWith('.html') ? {} : { 'Content-Disposition': 'attachment; filename="' + parts[2] + '"' }),
      });
      response.end(request.method === 'HEAD' ? undefined : bytes);
    } catch { if (!response.headersSent) response.writeHead(404); response.end(); }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  return {
    async publish(scope, taskId, { study = false, format = 'html' } = {}) {
      if (!['html','pdf'].includes(format)) throw new Error('unsupported report format');
      const task = study ? await bridge.studyStatus(scope,taskId) : await bridge.status(scope, taskId);
      if (!terminal.has(task.execution_status) || !Array.isArray(task.artifacts) || !task.artifacts.includes('report.html')) throw new Error('task has no committed HTML report');
      const key = digest(JSON.stringify([scope.workspaceId, scope.sessionId, taskId]));
      const directory = path.join(canonicalRoot, key);
      await mkdir(directory, { recursive: true });
      if (await realpath(directory) !== directory) throw new Error('report cache directory must not be a link');
      const entries = new Map(published.get(key) ?? []);
      let total = 0;
      for (const name of format==='pdf' ? ['report.pdf'] : task.artifacts) {
        if (!allowedFiles.has(name)) throw new Error('unsupported committed artifact name');
        const artifact = format==='pdf' ? await bridge.pdf(scope,taskId,{study}) : study ? await bridge.studyArtifact(scope,taskId,name) : await bridge.artifact(scope, taskId, name);
        const bytes = artifact.bytes;
        if (!Buffer.isBuffer(bytes) || bytes.length > 128 * 1024 * 1024 || digest(bytes) !== artifact.sha256) throw new Error('invalid artifact bytes');
        total += bytes.length;
        if (total > 256 * 1024 * 1024) throw new Error('report exceeds total byte budget');
        if (name === 'result.json' && JSON.parse(bytes.toString('utf8'))[study?'study_id':'task_id'] !== taskId) throw new Error('result task identity mismatch');
        const destination = path.join(directory, name);
        try { if ((await lstat(destination)).isSymbolicLink()) throw new Error('report file must not be a link'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
        const temporary = path.join(directory, '.' + randomUUID() + '.tmp');
        await writeFile(temporary, bytes, { flag: 'wx' });
        await rename(temporary, destination);
        entries.set(name, { path: destination, bytes: bytes.length, sha256: artifact.sha256 });
      }
      // HTML and PDF tools may complete concurrently. Merge at the no-await
      // commit point so the later publication cannot discard earlier links.
      const merged = new Map(published.get(key) ?? []);
      for (const [name, entry] of entries) merged.set(name, entry);
      published.set(key, merged);
      return { ...(study?{study_id:taskId}:{task_id:taskId}), url: `http://127.0.0.1:${server.address().port}/reports/${key}/report.${format}`,
        files: [...merged].map(([name, entry]) => ({ name, bytes: entry.bytes, sha256: entry.sha256 })), source: format==='pdf'?'derived_from_verified_committed_html':'verified_committed_artifacts', recomputed: false };
    },
    async close() {
      if (!closePromise) {
        closePromise = (async () => {
          if (!server.listening) return;
          server.closeIdleConnections();
          await new Promise((resolve, reject) => server.close(error => error && error.code !== 'ERR_SERVER_NOT_RUNNING' ? reject(error) : resolve()));
        })();
        closePromise.catch(() => { closePromise = undefined; });
      }
      return closePromise;
    },
  };
}

export async function createRuntime(options) {
  if (!options.stateDir || !path.isAbsolute(options.stateDir)) throw new Error('absolute private stateDir is required');
  await mkdir(options.stateDir, { recursive: true });
  const stateDir = await realpath(options.stateDir);
  let service;
  let bridge;
  let publisher;
  try {
    service = options.connectionFile ? { connectionFile: options.connectionFile, close: async () => {} } : await startOwnedService({ ...options, stateDir });
    bridge = new TianshuBridge({ stateDir: path.join(stateDir, 'bindings'), connectionFile: service.connectionFile });
    await bridge.ready();
    publisher = await createReportPublisher({ bridge, stateDir });
    let closed = false;
    let closing;
    return {
      bridge, publishReport: (scope, taskId, options) => publisher.publish(scope, taskId, options),
      async close() {
        if (closed) return;
        if (!closing) {
          closing = (async () => {
            const cancelled = await bridge.cancelOwnedPending();
            if (!Array.isArray(cancelled) || cancelled.some(item => item.confirmed_stopped !== true)) {
              throw new Error('owned task stop is not confirmed; service and bindings remain available for recovery');
            }
            await publisher.close();
            await service.close();
            await bridge.close();
            closed = true;
          })();
          closing.catch(() => { closing = undefined; });
        }
        return closing;
      },
    };
  } catch (error) {
    if (publisher) await publisher.close().catch(() => {});
    if (bridge) await bridge.close().catch(() => {});
    if (service) await service.close().catch(() => {});
    throw error;
  }
}
