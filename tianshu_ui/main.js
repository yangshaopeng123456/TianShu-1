// 天枢一号 Electron 主进程
const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const { BackendLifecycle } = require('./backend_lifecycle');

// === 早期诊断日志：在 main.js 加载各阶段写文件，便于排错 ===
const EARLOG = path.join(process.env.APPDATA || process.env.HOME || process.cwd(), 'tianshu-1', 'logs', 'tianshu-main.log');
try { fs.mkdirSync(path.dirname(EARLOG), { recursive: true }); } catch (_) {}
const earlyLog = (msg) => { try { fs.appendFileSync(EARLOG, `[${new Date().toISOString()}] ${msg}\n`); } catch (_) {} };
earlyLog(`main.js loaded; pid=${process.pid}; electron.version=${process.versions.electron}; node=${process.versions.node}`);

const DEFAULT_API = 'http://127.0.0.1:8000';
const BACKEND_HOST = '127.0.0.1';
const BACKEND_PORT = 8000;
// 后端看门狗的约定退出码：后端自己判定"计算引擎已无法在进程内恢复"时，
// 以该码退出，交由 Electron 重新拉起一个干净的后端进程。
const WATCHDOG_EXIT_CODE = 70;
// 自动重启的兜底上限：若短时间内反复重启仍起不来（例如 Python 环境被破坏、
// 端口被别的进程长期占用），继续自动重启只会让界面永远转圈。
// 达到上限就停止并明确告知用户去查日志，把问题摆到台面上。
const RESTART_WINDOW_MS = 60000;
const RESTART_MAX = 4;
let backendRestartAt = [];
const SETTINGS_FILE = path.join(app.getPath('userData'), 'settings.json');
let mainWindow = null;

function projectRoot() {
  // 开发模式：tianshu_ui 的上一级是 agent；打包模式：resources/backend
  if (!app.isPackaged) return path.resolve(__dirname, '..', 'dpf_agent');
  return path.join(process.resourcesPath, 'backend');
}

function backendCommand() {
  const root = projectRoot();
  if (process.platform === 'win32') {
    const python = path.join(root, '.venv', 'Scripts', 'python.exe');
    return fs.existsSync(python)
      ? { command: python, args: ['-m', 'dpf_agent.web.server'], cwd: root }
      : { command: 'python', args: ['-m', 'dpf_agent.web.server'], cwd: root };
  }
  const python = path.join(root, '.venv', 'bin', 'python');
  return { command: fs.existsSync(python) ? python : 'python3', args: ['-m', 'dpf_agent.web.server'], cwd: root };
}

function notify(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
}

function backendLog(message) {
  try {
    const file = path.join(app.getPath('logs'), 'tianshu-launcher.log');
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.appendFileSync(file, `[${new Date().toISOString()}] ${message}\n`);
  } catch (_) {}
}

const backendLifecycle = new BackendLifecycle({
  getSettings: readSettings,
  ownedBase: DEFAULT_API,
  log: backendLog,
  probeReady: async (base) => {
    try { await requestJson('GET', `${base}/api/health`, undefined, {}, 500); return true; }
    catch (_) { return false; }
  },
  onProgress: (seconds) => notify('backend:progress', { seconds, message: `正在启动后端服务… 已等待 ${seconds} 秒` }),
  start: (settings) => {
    notify('backend:progress', { seconds: 0, message: '正在启动天枢一号后端服务…' });
    const spec = backendCommand();
    if (!fs.existsSync(spec.cwd)) throw new Error(`找不到后端目录：${spec.cwd}`);
    backendLog(`spec.command=${spec.command} spec.cwd=${spec.cwd}`);
    const child = spawn(spec.command, spec.args, {
      cwd: spec.cwd,
      env: {
        ...process.env,
        DPF_WEB_HOST: BACKEND_HOST,
        DPF_WEB_PORT: String(BACKEND_PORT),
        PYTHONUNBUFFERED: '1',
        ...(settings.llmApiKey ? { DPF_LLM_API_KEY: settings.llmApiKey } : {}),
        ...(settings.llmBaseUrl ? { DPF_LLM_BASE_URL: settings.llmBaseUrl } : {}),
        ...(settings.llmModel ? { DPF_LLM_MODEL: settings.llmModel } : {}),
      },
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const logPath = path.join(app.getPath('logs'), 'tianshu-backend.log');
    const log = fs.createWriteStream(logPath, { flags: 'a' });
    child.stdout?.pipe(log);
    child.stderr?.pipe(log);
    return { child, logPath };
  },
  onUnexpectedExit: (record, { code, signal, error }) => {
    const now = Date.now();
    backendRestartAt = backendRestartAt.filter((time) => now - time < RESTART_WINDOW_MS);
    if (backendRestartAt.length >= RESTART_MAX) {
      backendLog(`restart suppressed: ${backendRestartAt.length} times in ${RESTART_WINDOW_MS}ms pid=${record.child.pid}`);
      notify('backend:error', `后端反复启动失败（1 分钟内 ${backendRestartAt.length} 次），已停止自动重启。请查看日志：${path.join(app.getPath('logs'), 'tianshu-backend.log')}`);
      return;
    }
    backendRestartAt.push(now);
    const why = error ? error.message : signal ? `被信号 ${signal} 终止` : `退出码 ${code}`;
    notify('backend:error', code === WATCHDOG_EXIT_CODE
      ? '检测到计算引擎异常，正在自动恢复…'
      : `后端进程意外退出（${why}），正在自动重启…`);
    backendLifecycle.recover(record).then((backend) => {
      if (backend && !backendLifecycle.closing) notify('backend:ready', { ...backend, restarted: true });
    }).catch((err) => {
      backendLog(`recovery failed pid=${record.child.pid} error=${err.message}`);
      if (!backendLifecycle.closing) notify('backend:error', String(err.message || err));
    });
  },
});

function ensureBackend() { return backendLifecycle.ensure(); }
function stopBackend(reason) { return backendLifecycle.shutdown(reason); }
function stopBackendInBackground(reason) {
  void stopBackend(reason).catch((err) => backendLog(`shutdown failed reason=${reason} error=${err.message}`));
}

function readSettings() {
  try {
    let raw = fs.readFileSync(SETTINGS_FILE, 'utf8');
    // 去掉 UTF-8 BOM：PowerShell 的 Set-Content -Encoding UTF8 等工具会写入 BOM，
    // 带 BOM 会让 JSON.parse 直接失败（配置被静默丢弃）。
    if (raw.charCodeAt(0) === 0xFEFF) raw = raw.slice(1);
    return JSON.parse(raw);
  } catch (_) { return { apiBase: DEFAULT_API }; }
}

function writeSettings(value) {
  fs.mkdirSync(path.dirname(SETTINGS_FILE), { recursive: true });
  fs.writeFileSync(SETTINGS_FILE, JSON.stringify(value, null, 2), 'utf8');
  return value;
}

function requestJson(method, url, body, headers = {}, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const transport = target.protocol === 'https:' ? https : http;
    const payload = body == null ? null : JSON.stringify(body);
    const req = transport.request({
      method, hostname: target.hostname, port: target.port || (target.protocol === 'https:' ? 443 : 80),
      path: target.pathname + target.search,
      headers: { ...(payload ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } : {}), ...headers },
      timeout: timeoutMs,
    }, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { raw += chunk; });
      res.on('end', () => {
        let data;
        try { data = raw ? JSON.parse(raw) : {}; } catch (_) { data = { raw }; }
        if (res.statusCode >= 400) reject(new Error(data.detail || data.error || `HTTP ${res.statusCode}`));
        else resolve({ status: res.statusCode, data });
      });
    });
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

function multipartRequest(url, filePaths, content) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const transport = target.protocol === 'https:' ? https : http;
    const boundary = `----TianshuBoundary${Date.now()}`;
    const parts = [];
    if (content) parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="content"\r\n\r\n${content}\r\n`));
    // 支持一次上传多个文件：后端 /api/chat/sessions/{id}/messages 收的是
    // `files: list[UploadFile]`（字段名本来就是复数），每个文件一个 part、
    // 同一个 name="files"，服务端会按顺序全部收下。
    const list = Array.isArray(filePaths) ? filePaths.filter(Boolean) : (filePaths ? [filePaths] : []);
    for (const one of list) {
      let data;
      try {
        data = fs.readFileSync(one);
      } catch (err) {
        reject(new Error(`读取文件失败：${path.basename(String(one))}（${err.message}）`));
        return;
      }
      const name = path.basename(String(one));
      parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="${name}"\r\nContent-Type: application/octet-stream\r\n\r\n`));
      parts.push(data);
      parts.push(Buffer.from(`\r\n`));
    }
    parts.push(Buffer.from(`--${boundary}--\r\n`));
    const body = Buffer.concat(parts);
    const req = transport.request({
      method: 'POST', hostname: target.hostname, port: target.port || (target.protocol === 'https:' ? 443 : 80),
      path: target.pathname + target.search,
      // 上传/计算的等待上限。取 10 分钟：一次可以上传**多个算例**，后端是
      // 串行依次计算，耗时随算例数成倍增长；120s（原值）连两个算例都撑不住
      // （实测正好卡在 120.2s 超时）。
      // 注意与渲染层 `armBusyWatchdog(180s)` 的分工：看门狗先给用户"还在跑"的
      // 反馈并解锁 UI，这里的超时是**放弃等待**，理应比看门狗宽裕。
      headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}`, 'Content-Length': body.length }, timeout: 600000,
    }, (res) => {
      let raw = ''; res.setEncoding('utf8'); res.on('data', (c) => { raw += c; });
      res.on('end', () => {
        let parsed; try { parsed = JSON.parse(raw); } catch (_) { parsed = { raw }; }
        if (res.statusCode >= 400) reject(new Error(parsed.detail || `HTTP ${res.statusCode}`)); else resolve(parsed);
      });
    });
    req.on('timeout', () => req.destroy(new Error('上传请求超时')));
    req.on('error', reject); req.write(body); req.end();
  });
}

const isWindows = process.platform === 'win32';

function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1180,
    minHeight: 720,
    frame: false,                    // 无边框，自绘标题栏
    backgroundColor: '#001432',      // 与侧边栏一致，避免白闪
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow = win;
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  win.once('ready-to-show', () => win.show());
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
    stopTaskStream();
    stopBackendInBackground('window-closed');
  });


// ---------- 任务事件流（SSE）：经主进程转发，绕开渲染层 CSP 限制 ----------
let taskStreamReq = null;
function stopTaskStream() {
  if (taskStreamReq) {
    try { taskStreamReq.destroy(); } catch (_) {}
    taskStreamReq = null;
  }
}
  // ---------- 受控后端 API 桥接 ----------
  ipcMain.handle('api:request', async (_event, { method = 'GET', path: apiPath, body } = {}) => {
    const settings = readSettings();
    const base = (settings.apiBase || DEFAULT_API).replace(/\/$/, '');
    return (await requestJson(method, base + apiPath, body)).data;
  });
  ipcMain.handle('api:upload', async (_event, { sessionId, filePaths, content }) => {
    const settings = readSettings();
    const base = (settings.apiBase || DEFAULT_API).replace(/\/$/, '');
    return multipartRequest(`${base}/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`, filePaths, content);
  });
  ipcMain.handle('api:message', async (_event, { sessionId, content }) => {
    const settings = readSettings();
    const base = (settings.apiBase || DEFAULT_API).replace(/\/$/, '');
    return multipartRequest(`${base}/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`, [], content);
  });
  ipcMain.handle('task:stream:start', async (ipcEvent, streamId) => {
    const settings = readSettings();
    const base = (settings.apiBase || DEFAULT_API).replace(/\/$/, '');
    stopTaskStream();
    return await new Promise((resolve) => {
      try {
        taskStreamReq = http.get(`${base}/api/tasks/stream`, (res) => {
          let buf = '';
          res.setEncoding('utf8');
          res.on('data', (chunk) => {
            buf += chunk;
            let idx;
            while ((idx = buf.indexOf('\n\n')) >= 0) {
              const raw = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              let evt = 'message';
              const dataLines = [];
              raw.split('\n').forEach((line) => {
                if (line.startsWith('event:')) evt = line.slice(6).trim();
                else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
              });
              if (!dataLines.length) continue;
              try {
                ipcEvent.sender.send('task:event', { event: evt, data: JSON.parse(dataLines.join('\n')), streamId });
              } catch (_) {}
            }
          });
          res.on('end', () => {
            try { ipcEvent.sender.send('task:event', { event: 'closed', data: {}, streamId }); } catch (_) {}
          });
          resolve(true);
        });
        taskStreamReq.on('error', () => resolve(false));
      } catch (_) { resolve(false); }
    });
  });
  ipcMain.handle('task:stream:stop', async () => { stopTaskStream(); return true; });
  ipcMain.handle('file:choose', async () => {
    // 自动化验收用，两个注入点（都不设时行为完全不变）：
    //
    //   DPF_TEST_PICK_LIST  —— 指向一个**清单文件**，**每次点击都重读**。
    //     为什么必须"每次重读"：一个 Electron 实例要连着测完一整个批次，
    //     每题换算例。若路径在进程启动时就固定，一批只能测一个文件，
    //     测 18 题就得重启 18 次 Electron（每次 ~15s 启动 + 等后端就绪）。
    //     框架 `_ui_batch.py` 每题先写清单再点上传，靠的就是这个。
    //
    //   DPF_TEST_PICK_FILES —— 旧写法，`;` 分隔，**进程启动时固定**。
    //     保留兼容（单题调试方便），但不支持连续多题。
    const listFile = (process.env.DPF_TEST_PICK_LIST || '').trim();
    if (listFile) {
      try {
        const raw = fs.readFileSync(listFile, 'utf-8');
        const files = raw.split(/[\r\n;]+/).map((s) => s.trim()).filter(Boolean);
        if (files.length) {
          earlyLog(`[file:choose] 注入清单 ${files.length} 个文件（清单=${listFile}）`);
          return files;
        }
        earlyLog(`[file:choose] 清单为空：${listFile}`);
        return [];
      } catch (err) {
        // 清单读不到就回落，不让验收因环境问题静默失败。
        earlyLog(`[file:choose] 读清单失败：${String(err)}（清单=${listFile}）`);
        return [];
      }
    }
    const injected = (process.env.DPF_TEST_PICK_FILES || '').trim();
    if (injected) {
      return injected.split(';').map((s) => s.trim()).filter(Boolean);
    }
    const result = await dialog.showOpenDialog(win, {
      // multiSelections：一个算例常由多个 .dss 组成（Master.dss + Redirect 的子文件），
      // 一次选完比反复点上传更贴近真实用法。后端 `files: list[UploadFile]` 本就收多个。
      properties: ['openFile', 'multiSelections'],
      filters: [{ name: '潮流算例', extensions: ['dss', 'zip', 'xlsx', 'csv', 'json', 'm'] }, { name: '全部文件', extensions: ['*'] }],
    });
    // 统一返回**数组**（取消 = 空数组），避免调用方要区分 null / string / array 三种形态。
    return result.canceled ? [] : result.filePaths;
  });
  // 用户要求配置页能直接看到已保存的 Key（本机自用场景），故回传明文。
  ipcMain.handle('settings:get', () => {
    const s = readSettings();
    return { ...s, llmApiKeySet: Boolean(s.llmApiKey) };
  });
  ipcMain.handle('settings:save', (_event, value) => {
    const current = readSettings();
    const merged = { ...current, ...value };
    // llmApiKey 语义：字符串=设置，''=清除，undefined/null=不改（保持原值）
    if (value && (value.llmApiKey === undefined || value.llmApiKey === null)) {
      merged.llmApiKey = current.llmApiKey;
    }
    return writeSettings(merged);
  });
  // 修改模型配置（Key / Base URL / 模型名）后重启后端，使新环境变量生效
  ipcMain.handle('backend:restart', async () => {
    const backend = await backendLifecycle.restart('configuration-change');
    backendLifecycle.assertOpen();
    notify('backend:ready', backend);
    return backend;
  });
  ipcMain.handle('external:open', (_event, url) => shell.openExternal(url));

  // 标题栏与状态栏间的窗口控制
  ipcMain.on('window:minimize', () => win.minimize());
  ipcMain.on('window:maximize', () => {
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  });
  ipcMain.on('window:close', () => win.close());
  ipcMain.on('window:is-maximized', (e) => {
    e.returnValue = win.isMaximized();
  });

  win.on('maximize', () => win.webContents.send('window:maximized', true));
  win.on('unmaximize', () => win.webContents.send('window:maximized', false));

  // 外部链接用系统浏览器打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

app.whenReady().then(async () => {
  earlyLog('app ready');
  // 先创建窗口：后端首次启动需要 20~40 秒（要导入 OpenDSS），
  // 若等后端就绪再开窗，用户会看到长时间无界面，误以为没反应。
  createWindow();
  earlyLog('createWindow returned');
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
  try {
    const backend = await ensureBackend();
    backendLifecycle.assertOpen();
    earlyLog('backend ready');
    notify('backend:ready', backend);
  } catch (err) {
    earlyLog(`backend failed: ${err.message}`);
    if (backendLifecycle.closing) return;
    notify('backend:error', err.message);
    dialog.showErrorBox('天枢一号启动失败', `${err.message}\n\n请检查后端 Python 环境。`);
  }
}).catch((err) => { earlyLog(`whenReady error: ${err.message}`); });

let quitCleanupFinished = false;
let quitCleanupStarted = false;
app.on('before-quit', (event) => {
  if (quitCleanupFinished) return;
  event.preventDefault();
  if (quitCleanupStarted) return;
  quitCleanupStarted = true;
  stopBackend('before-quit').catch((err) => backendLog(`shutdown failed reason=before-quit error=${err.message}`))
    .finally(() => { quitCleanupFinished = true; app.quit(); });
});
app.on('window-all-closed', () => {
  stopBackendInBackground('window-all-closed');
  if (!isWindows) app.quit();
});
