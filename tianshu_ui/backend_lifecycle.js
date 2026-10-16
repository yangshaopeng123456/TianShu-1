// 管理 Electron 自己启动的子进程；外部复用服务从不进入停止流程。
const net = require('net');
const { spawn } = require('child_process');

function isManagedBase(base) {
  const url = new URL(base);
  return url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname)
    && (url.port || '80') === '8000' && url.pathname === '/' && !url.search && !url.hash;
}

function endpointLabel(base) {
  const url = new URL(base);
  return `${url.protocol}//${url.hostname}:${url.port || (url.protocol === 'https:' ? '443' : '80')}`;
}

function isEndpointListening(base, timeoutMs = 500) {
  const url = new URL(base);
  return new Promise((resolve, reject) => {
    const socket = net.connect({ host: url.hostname, port: Number(url.port || (url.protocol === 'https:' ? 443 : 80)) });
    const finish = (error, listening) => { socket.destroy(); error ? reject(error) : resolve(listening); };
    socket.once('connect', () => finish(null, true));
    socket.once('error', error => error.code === 'ECONNREFUSED' ? finish(null, false) : finish(error));
    socket.setTimeout(timeoutMs, () => finish(new Error('检测后端监听端口超时')));
  });
}

function terminateProcessTree(child) {
  if (process.platform !== 'win32') {
    if (!child.kill('SIGTERM')) throw new Error(`停止后端进程失败：pid=${child.pid}`);
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const killer = spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
    killer.once('error', reject);
    killer.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`taskkill 失败：pid=${child.pid} code=${code} signal=${signal}`)));
  });
}

function bounded(promise, timeoutMs, message) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), Math.max(1, timeoutMs)); })])
    .finally(() => clearTimeout(timer));
}

class BackendLifecycle {
  constructor({ getSettings, start, probeReady, listening = isEndpointListening, terminate = terminateProcessTree,
    log = () => {}, onUnexpectedExit = () => {}, onProgress = () => {}, managed = isManagedBase,
    ownedBase = 'http://127.0.0.1:8000', stopTimeoutMs = 15000, startTimeoutMs = 60000, pollMs = 100 }) {
    Object.assign(this, { getSettings, start, probeReady, listening, terminate, log, onUnexpectedExit, onProgress,
      managed, ownedBase, stopTimeoutMs, startTimeoutMs, pollMs });
    this.current = null;
    this.records = new Set();
    this.closing = false;
    this.generation = 0;
    this.queue = Promise.resolve();
    this.shutdownPromise = null;
  }

  assertOpen() { if (this.closing) throw new Error('应用正在关闭，已取消后端启动/重启'); }

  enqueue(operation) {
    const next = this.queue.then(() => { this.assertOpen(); return operation(); });
    this.queue = next.catch(() => {});
    return next;
  }

  ensure(reason = 'startup') { return this.enqueue(() => this.ensureOnce(reason)); }

  restart(reason = 'configuration-change') {
    this.generation += 1; // 旧异常退出的恢复请求不能跨过用户的新重启请求。
    if (this.current) this.current.stopRequested = reason;
    // 每次请求排队，并在实际启动时读取最新配置；不吞并后一次配置保存。
    return this.enqueue(async () => {
      this.log(`restart requested reason=${reason} generation=${this.generation}`);
      await this.stopOwned(reason);
      this.assertOpen();
      return this.ensureOnce(reason);
    });
  }

  recover(record) {
    const generation = record.exitGeneration;
    return this.enqueue(async () => {
      if (generation !== this.generation || this.current) {
        this.log(`recovery skipped pid=${record.child.pid} reason=superseded`);
        return null;
      }
      await this.drain(record, 'unexpected-exit');
      this.assertOpen();
      if (generation !== this.generation || this.current) return null;
      return this.ensureOnce('unexpected-exit');
    });
  }

  shutdown(reason = 'application-close') {
    this.closing = true; // 同步置位；正在等健康、已经排队的操作都不能再生成新进程。
    this.generation += 1;
    if (this.current) this.current.stopRequested = reason;
    if (!this.shutdownPromise) this.shutdownPromise = this.stopOwned(reason);
    return this.shutdownPromise;
  }

  track(child, logPath) {
    const record = { child, logPath, base: this.ownedBase, stopRequested: null, exited: false, stopPromise: null };
    let resolveExit;
    record.exitPromise = new Promise(resolve => { resolveExit = resolve; });
    this.current = record;
    this.records.add(record);
    const finish = (code, signal, error = null) => {
      if (record.exited) return;
      record.exited = true;
      record.error = error;
      record.exitGeneration = this.generation;
      resolveExit({ code, signal, error });
      const current = this.current === record;
      this.log(`backend exited pid=${child.pid || 'unassigned'} code=${code} signal=${signal} stop=${record.stopRequested || 'none'} current=${current}${error ? ` error=${error.message}` : ''}`);
      if (!current) return; // 迟到旧进程事件不能清除替代进程，不能安排旧恢复。
      this.current = null;
      if (!record.stopRequested && !this.closing) this.onUnexpectedExit(record, { code, signal, error });
    };
    child.once('exit', (code, signal) => finish(code, signal));
    child.on('error', error => {
      if (record.exited) return;
      if (!child.pid) finish(null, null, error); // 未成功spawn时可能没有exit事件。
      else this.log(`backend process error pid=${child.pid} stop=${record.stopRequested || 'none'} error=${error.message}`);
      // 已生成pid后的发送/kill错误不代表进程已exit，停止仍必须等真实exit。
    });
    return record;
  }

  async ensureOnce(reason) {
    this.assertOpen();
    for (const record of [...this.records]) {
      if (record.exited || record.stopRequested) await this.drain(record, reason);
    }
    this.assertOpen();
    const settings = this.getSettings();
    const base = (settings.apiBase || this.ownedBase).replace(/\/$/, '');
    const before = this.current;
    const ready = await this.probeReady(base);
    this.assertOpen();
    if (before && (before.exited || before.stopRequested || this.current !== before)) throw new Error('后端已退出或正在停止，不能复用旧健康响应');
    if (ready) {
      this.log(`backend reused reason=${reason} ownership=${this.current ? 'owned' : 'external'} endpoint=${endpointLabel(base)}`);
      return { reused: true, base };
    }
    if (!this.managed(base)) {
      this.log(`backend start refused reason=external-unreachable endpoint=${endpointLabel(base)}`);
      throw new Error(`配置的外部后端不可达：${endpointLabel(base)}，请启动该服务或检查 API 地址`);
    }
    if (!this.current) {
      this.assertOpen();
      const { child, logPath } = this.start(settings);
      this.track(child, logPath);
      this.log(`backend spawned pid=${child.pid || 'unassigned'} reason=${reason} endpoint=${endpointLabel(this.ownedBase)}`);
    }
    const record = this.current;
    const deadline = Date.now() + this.startTimeoutMs;
    while (true) {
      this.assertOpen();
      if (record.exited || record.stopRequested || this.current !== record) throw record.error || new Error('后端启动期间退出或收到停止请求');
      const readyNow = await bounded(Promise.race([
        this.probeReady(base), record.exitPromise.then(result => { throw result.error || new Error(`后端启动期间退出：code=${result.code} signal=${result.signal}`); }),
      ]), deadline - Date.now(), '天枢一号后端启动超时，请检查 Python 环境和服务日志');
      this.assertOpen();
      if (record.exited || record.stopRequested || this.current !== record) throw new Error('后端已退出或正在停止，不能报告就绪');
      if (readyNow) return { reused: false, base, logPath: record.logPath };
      if (Date.now() >= deadline) throw new Error('天枢一号后端启动超时，请检查 Python 环境和服务日志');
      this.onProgress(Math.floor((this.startTimeoutMs - (deadline - Date.now())) / 1000));
      await new Promise(resolve => setTimeout(resolve, this.pollMs));
    }
  }

  async stopOwned(reason) {
    if (!this.current && !this.records.size) {
      this.log(`backend stop skipped reason=${reason} ownership=external-or-none`);
      return;
    }
    for (const record of [...this.records]) await this.drain(record, reason);
  }

  drain(record, reason) {
    if (record.stopPromise) return record.stopPromise;
    record.stopRequested = record.stopRequested || reason;
    const deadline = Date.now() + this.stopTimeoutMs;
    const operation = (async () => {
      this.log(`backend stop requested pid=${record.child.pid || 'unassigned'} reason=${reason} exited=${record.exited}`);
      if (!record.exited) {
        await bounded(Promise.resolve().then(() => this.terminate(record.child)), deadline - Date.now(), '停止后端命令超时');
        this.log(`backend stop command completed pid=${record.child.pid}`);
        await bounded(record.exitPromise, deadline - Date.now(), '等待后端进程实际退出超时');
      }
      while (await bounded(this.listening(record.base), deadline - Date.now(), '等待后端监听端口释放超时')) {
        if (Date.now() >= deadline) throw new Error('等待后端监听端口释放超时');
        await new Promise(resolve => setTimeout(resolve, Math.min(this.pollMs, Math.max(1, deadline - Date.now()))));
      }
      this.log(`backend stop completed pid=${record.child.pid || 'unassigned'} port-released=true reason=${reason}`);
      this.records.delete(record);
      if (this.current === record) this.current = null;
    })();
    record.stopPromise = operation.catch(error => {
      record.stopPromise = null; // 明确失败后保留记录，允许再次停止；不能当作外部服务复用。
      this.log(`backend stop failed pid=${record.child.pid || 'unassigned'} reason=${reason} error=${error.message}`);
      throw error;
    });
    return record.stopPromise;
  }
}

module.exports = { BackendLifecycle, isManagedBase, isEndpointListening, terminateProcessTree };
