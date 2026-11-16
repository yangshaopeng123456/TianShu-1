// ============================================================
// 天枢一号 UI 逻辑：导航路由 + 三页内容渲染
// 配置页仅保留当前可操作功能与实际状态，数值来自后端产物。
// ============================================================

/* ---------- 窗口控制 ---------- */
const t = window.tianshu;
const state = {
  sessions: [],
  activeSessionId: null,
  activeSession: null,
  apiOnline: false,
  apiBase: 'http://127.0.0.1:8000',
  busy: false,
  // 外接大模型配置（按模式分开存，切换互不覆盖）
  llmApiKey: '',          // 云端 API Key
  llmBaseUrl: '',         // 当前生效的 base url（后端环境变量来源）
  llmCloudBaseUrl: '',    // 云端模式专用地址
  llmCloudModel: '',      // 云端模式专用模型名
  llmLocalModel: '',      // 本地模式专用模型名
  llmModel: '',           // 当前生效的模型名
  llmApiKeySet: false,
  // 连接方式：cloud（云端 API-Key）/ local（本地 Ollama）
  llmMode: 'cloud',
  // 当前前端发起的任务 ID；后端真正开始后由 task-progress 回填。
  activeTaskId: null,
  execProgress: null,
  // ---- 真实执行轨迹 ----
  // 由后端 SSE 逐条推来的阶段与事件（progress / trace 事件），**不是前端估算的**。
  // 任务结束后仍保留，并可调 /api/tasks/{id}/trace 回看历史。
  liveStages: [],        // [{stage, label, percent}]  按首次到达顺序
  liveEvents: [],        // 原始 trace 事件（含 stage / event_type / message / status）
  traceTaskId: null,     // liveStages/liveEvents 对应的任务
  traceLoading: false,
  traceOpen: false,      // 用户对"执行过程"卡片的展开/收起选择（切会话时重置）
  revealCount: 0,        // 已"揭示"（画到界面上）的事件条数，见 scheduleReveal
  // 本地 Ollama 专属字段
  llmOllamaUrl: 'http://localhost:11434/v1',
  llmLocalModels: [],          // /api/local-models 的结果缓存
  llmLocalBackend: '',         // 'ollama-openai' / 'ollama-native'
};

// 在途请求只能收尾自己的状态：停止后重试、切换会话都不会把旧响应当成当前响应。
let _messageGeneration = 0;
let _activeMessageRequest = null;
let _stoppedMessageRequest = null;
let _sessionLoadGeneration = 0;
let _sessionListGeneration = 0;
let _traceViewGeneration = 0;
let _batchMonitorGeneration = 0;
let _batchMonitor = null;
let _batchPollTimer = null;
const _retiredTaskIds = new Set();
function ownsMessageRequest(request) {
  return Boolean(request && request === _activeMessageRequest);
}
function displaysMessageRequest(request) {
  return ownsMessageRequest(request) && request.sessionId === state.activeSessionId;
}
function retireTask(taskId) {
  if (!taskId) return;
  _retiredTaskIds.add(taskId);
  if (_retiredTaskIds.size > 30) _retiredTaskIds.delete(_retiredTaskIds.values().next().value);
}

// API Key 明文显示开关：默认密文（防止截图泄露），点 👁 才展开。
// 仅存于内存，不写盘；每次冷启或 F5 重载后自动回到密文状态。
let _apikeyRevealed = false;

async function api(path, method = 'GET', body = undefined) {
  const generation = _overviewCache.generation;
  const endpoint = state.apiBase;
  try {
    return await t.apiRequest(method, path, body);
  } catch (err) {
    if (generation === _overviewCache.generation && endpoint === state.apiBase) _lastApiError = err.message || String(err);
    throw err;
  }
}

const statusState = {
  model: { level: 'gray', label: '模型检查中…', detail: '' },
  engine: { level: 'gray', label: '引擎检查中…', detail: '' },
  sidecar: { level: 'gray', label: 'Sidecar 检查中…', detail: '' },
  license: { level: 'gray', label: '许可证检查中…', detail: '' },
  tasks: { count: 0, active: false, detail: '' },
};
let statusNotifications = [];
let statusRefreshTimer = null;
let statusRefreshInFlight = false;

function renderStatusItem(id, level, label, detail = '') {
  const el = document.getElementById(id);
  if (!el) return;
  const dot = el.querySelector('.dot');
  const text = el.querySelector('.status-label');
  if (dot) dot.className = `dot ${level || 'gray'}`;
  if (text) text.textContent = label;
  el.title = detail || label;
  el.classList.toggle('is-running', level === 'running' || id === 'sb-tasks' && statusState.tasks.active);
}

function renderTaskStatus(progress, tasks) {
  const active = Boolean(progress && progress.active);
  const count = active ? 1 : 0;
  const pct = progress && Number.isFinite(Number(progress.percent)) ? Math.max(0, Math.min(100, Number(progress.percent))) : 0;
  const label = active ? `计算中 ${pct}%` : '后台任务';
  const detail = active
    ? `${progress.label || '正在执行…'}${progress.task_id ? ` · ${progress.task_id}` : ''}`
    : '当前没有正在执行的任务';
  statusState.tasks = { count, active, detail, taskId: progress?.task_id || '' };
  const el = document.getElementById('sb-tasks');
  if (el) {
    const dot = el.querySelector('.dot');
    const text = el.querySelector('.status-label');
    if (dot) dot.className = `dot ${active ? 'warn' : 'ok'}`;
    if (text) text.textContent = label;
    const num = document.getElementById('sb-bg-tasks');
    if (num) num.textContent = String(count);
    el.title = detail;
    el.classList.toggle('is-running', active);
  }
}

const TERMINAL_TASK_STATUSES = new Set([
  'success', 'partial', 'not_assessed', 'non_converged', 'invalid_request',
  'data_unavailable', 'validation_failed', 'cancelled', 'system_failed', 'interrupted',
  'failed', 'converged',
]);
const _completedTaskIds = new Set();
let _taskStatusRevision = 0;

function observeOwnedTaskStatus(request, progress) {
  if (!ownsMessageRequest(request) || !progress?.active || !progress.task_id
      || request.taskId !== progress.task_id || _completedTaskIds.has(progress.task_id)) return;
  _taskStatusRevision += 1;
  renderTaskStatus(progress);
}

function synchronizeTerminalTaskStatus(request, taskId, taskStatus, sessionId = null) {
  const ownsStop = Boolean(request && request === _stoppedMessageRequest && request.stopGeneration === _messageGeneration);
  if ((!ownsMessageRequest(request) && !ownsStop) || !taskId || !TERMINAL_TASK_STATUSES.has(taskStatus)) return;
  if (sessionId && sessionId !== request.sessionId) return;
  _completedTaskIds.add(taskId);
  if (_completedTaskIds.size > 64) _completedTaskIds.delete(_completedTaskIds.values().next().value);
  // 全局状态可能已经跟到另一个任务；结束旧任务只更新它自己的那一帧。
  if (statusState.tasks.taskId !== taskId) return;
  _taskStatusRevision += 1;
  renderTaskStatus({ task_id: taskId, active: false, done: true, percent: 100 });
}

function applyRefreshedTaskStatus(progress, tasks, revision) {
  if (revision !== _taskStatusRevision) return;
  if (progress?.task_id && _completedTaskIds.has(progress.task_id)) {
    if (statusState.tasks.active && statusState.tasks.taskId !== progress.task_id) return;
    progress = { ...progress, active: false, done: true };
  }
  renderTaskStatus(progress, tasks);
}

function readNotificationIds() {
  try { return JSON.parse(window.localStorage?.getItem('tianshu.readTaskNotifications') || '[]'); }
  catch (e) { return []; }
}
function writeNotificationIds(ids) {
  try { window.localStorage?.setItem('tianshu.readTaskNotifications', JSON.stringify(ids.slice(-100))); }
  catch (e) { /* 无持久化能力时只保留本次会话 */ }
}
function buildNotifications(tasks) {
  const read = new Set(readNotificationIds());
  const list = (tasks || []).filter((t) => t && t.task_id && ['success', 'converged', 'failed', 'system_failed', 'non_converged'].includes(t.task_status));
  statusNotifications = list.slice(0, 12).map((t) => {
    const failed = ['failed', 'system_failed', 'non_converged'].includes(t.task_status);
    return {
      id: t.task_id,
      unread: !read.has(t.task_id),
      level: failed ? 'err' : 'ok',
      title: `${failed ? '任务失败' : '任务完成'}：${t.task_case_ref || '未命名算例'}`,
      detail: `${t.task_status || 'unknown'} · ${t.created_at || ''}`,
      task: t,
    };
  });
  renderNotificationButton();
}
function renderNotificationButton() {
  const unread = statusNotifications.filter((n) => n.unread).length;
  const badge = document.getElementById('sb-notification-count');
  if (badge) { badge.textContent = String(unread); badge.hidden = unread === 0; }
  const el = document.getElementById('sb-notifications');
  if (el) el.title = unread ? `${unread} 条未读任务通知` : '没有未读任务通知';
}
function renderNotificationPanel() {
  const panel = document.getElementById('sb-notification-panel');
  if (!panel) return;
  const items = statusNotifications.length
    ? statusNotifications.map((n) => `<div class="status-notice" data-notice-id="${esc(n.id)}"><div class="notice-title"><span class="dot ${n.level}"></span> ${esc(n.title)}${n.unread ? ' · 未读' : ''}</div><div class="notice-detail">${esc(n.detail)}</div></div>`).join('')
    : '<div class="status-empty">暂无任务通知</div>';
  panel.innerHTML = `<div class="status-popover-title"><span>任务通知</span><button class="btn sm" id="sb-mark-read" type="button">全部标为已读</button></div>${items}`;
  panel.hidden = false;
  panel.querySelectorAll('[data-notice-id]').forEach((el) => {
    el.onclick = () => {
      const item = statusNotifications.find((n) => n.id === el.dataset.noticeId);
      if (!item) return;
      item.unread = false;
      const ids = new Set(readNotificationIds()); ids.add(item.id); writeNotificationIds([...ids]);
      renderNotificationButton(); renderNotificationPanel();
      if (item.task?.session_id) { currentPage = 'workbench'; state.activeSessionId = item.task.session_id; route(); loadSession(item.task.session_id).catch(() => {}); }
    };
  });
  const mark = document.getElementById('sb-mark-read');
  if (mark) mark.onclick = () => {
    const ids = new Set(readNotificationIds()); statusNotifications.forEach((n) => { n.unread = false; ids.add(n.id); });
    writeNotificationIds([...ids]); renderNotificationButton(); renderNotificationPanel();
  };
}
function toggleNotificationPanel() {
  const panel = document.getElementById('sb-notification-panel');
  if (!panel) return;
  if (panel.hidden) renderNotificationPanel(); else panel.hidden = true;
}

async function refreshStatusBar() {
  if (statusRefreshInFlight) return statusRefreshInFlight;
  const revision = _taskStatusRevision;
  const generation = _overviewCache.generation;
  const endpoint = state.apiBase;
  const pending = (async () => {
    applyPendingBackendRestart().catch(() => {});
    const results = await Promise.allSettled([fetchReadySnapshot({ force: true }), api('/api/task-progress'), api('/api/recent-tasks?limit=20')]);
    if (generation !== _overviewCache.generation || endpoint !== state.apiBase) return;
    if (results[1].status === 'fulfilled') applyRefreshedTaskStatus(results[1].value, results[2].value?.tasks || [], revision);
    if (results[2].status === 'fulfilled') buildNotifications(results[2].value?.tasks || []);
  })();
  statusRefreshInFlight = pending;
  try { return await pending; } finally { if (statusRefreshInFlight === pending) statusRefreshInFlight = null; }
}

function startStatusBarPolling() {
  if (statusRefreshTimer && typeof clearInterval === 'function') clearInterval(statusRefreshTimer);
  refreshStatusBar().catch(() => {});
  if (typeof setInterval === 'function') {
    statusRefreshTimer = setInterval(() => refreshStatusBar().catch(() => {}), 5000);
  }
}

function bindStatusBarActions() {
  const targets = { model: 'model', engine: 'overview', sidecar: 'overview' };
  Object.entries(targets).forEach(([key, sub]) => {
    const el = document.getElementById(`sb-${key}`);
    if (el) el.onclick = () => { currentPage = 'config'; currentSub = sub; route(); };
  });
  const license = document.getElementById('sb-license');
  if (license) license.onclick = () => { currentPage = 'config'; currentSub = 'overview'; route(); };
  const tasks = document.getElementById('sb-tasks');
  if (tasks) tasks.onclick = () => { currentPage = 'workbench'; route(); if (state.activeSessionId) loadSession(state.activeSessionId).catch(() => {}); };
  const notices = document.getElementById('sb-notifications');
  if (notices) notices.onclick = toggleNotificationPanel;
}

function showToast(message, kind = 'info') {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.style.cssText = 'position:fixed;right:22px;bottom:22px;z-index:99;padding:10px 14px;border-radius:8px;background:#001432;color:#fff;font-size:12px;max-width:420px;box-shadow:0 4px 16px rgba(0,20,50,.2)';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.style.background = kind === 'error' ? '#E04030' : '#001432';
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.remove(), 4000);
}

document.getElementById('btn-min').onclick = () => t.minimize();
document.getElementById('btn-max').onclick = () => t.maximize();
document.getElementById('btn-close').onclick = () => t.close();
document.getElementById('btn-collapse').onclick = () => {
  document.getElementById('nav').classList.toggle('hidden');
};

// 帮助按钮：弹帮助/关于面板
document.getElementById('btn-help').onclick = () => {
  document.getElementById('help-modal').hidden = false;
};
document.getElementById('help-close').onclick = () => {
  document.getElementById('help-modal').hidden = true;
};
document.getElementById('help-modal').onclick = (ev) => {
  // 点遮罩关闭，点卡片不关
  if (ev.target.id === 'help-modal') {
    ev.currentTarget.hidden = true;
  }
};

/* ---------- 增量刷新（用户要的刷新语义）----------
   首次打开等待本机后端；求解引擎由任务流程按需加载。
   之后点 🔄 = 先扫描「哪些数据变了」，只重画变了的那一块，不做整页重载
   ------------------------------------------------ */
const _syncCache = Object.create(null);   // regionId -> 上次见到的数据签名

function _sig(v) {
  try { return JSON.stringify(v); } catch (e) { return String(v); }
}

// 每个区域：先判断当前界面是否看得见（看不见就不查，省时间），
// 再比对签名；只有签名变了才 apply（局部重绘）。
const SYNC_REGIONS = [
  {
    id: 'ready',
    label: '环境状态',
    always: true,                       // 各页面共用同一批就绪快照
    fetch: () => fetchReadySnapshot({ force: true }),
    apply: () => {},
  },
  {
    id: 'models',
    label: '模型列表',
    visible: () => !!document.getElementById('model-field-slot'),
    fetch: () => fetchModels(true),
    apply: () => refreshModelField(false),
  },
  {
    id: 'kb',
    label: '知识库',
    visible: () => !!document.getElementById('kb-card'),
    fetch: () => api('/api/rag/status'),
    apply: () => route(),
  },
];

async function scanAndRefresh() {
  const btn = document.getElementById('btn-refresh-ui');
  if (btn) { btn.disabled = true; btn.classList.add('is-refreshing'); }
  const checked = [];
  const updated = [];
  const failed = [];
  try {
    for (const r of SYNC_REGIONS) {
      const visible = r.always || (typeof r.visible === 'function' && r.visible());
      if (!visible) continue;                       // 不在当前界面 → 跳过，不浪费时间
      const pageRevision = routeToken;
      let data = null;
      try {
        data = await r.fetch();
      } catch (e) {
        failed.push(`${r.label}（${e.message}）`);
        continue;
      }
      checked.push(r.label);
      const sig = _sig(data);
      if (_syncCache[r.id] === sig) continue;       // 已是最新 → 不动
      _syncCache[r.id] = sig;
      try {
        if (pageRevision !== routeToken) continue;
        await r.apply(data);
        updated.push(r.label);
      } catch (e) {
        failed.push(`${r.label}刷新失败`);
      }
    }
    if (!checked.length && !failed.length) {
      showToast('当前界面没有可刷新的内容');
    } else if (updated.length) {
      showToast(`检查 ${checked.length} 项，更新 ${updated.length} 项：${updated.join('、')}`);
    } else {
      showToast(`检查 ${checked.length} 项，全部已是最新${failed.length ? `（${failed.join('；')}）` : ''}`);
    }
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('is-refreshing'); }
  }
}

// 刷新界面按钮：增量刷新（扫描 → 只更新不同步的部分）。
// 需要"整页重载"时用 F5 / Ctrl+R —— 那才会真正重跑一遍渲染层。
document.getElementById('btn-refresh-ui').onclick = () => scanAndRefresh();

// Esc 关闭帮助弹窗（用 onclick 避免 jsdom 缺 addEventListener）
window.onkeydown = (ev) => {
  if (ev.key === 'Escape' && !document.getElementById('help-modal').hidden) {
    document.getElementById('help-modal').hidden = true;
  }
};

/* ---------- 状态栏面包屑 ---------- */
const crumbMap = {
  'config': '系统配置',
  'workbench': '分析工作台',
  'results': '结果展示',
};
function setCrumb(page, sub) {
  const p = crumbMap[page] || '';
  document.getElementById('sb-crumb').textContent = sub ? `${p} / ${sub}` : p;
}

/* ============================================================
   系统配置 — 二级导航树
   ============================================================ */
const CONFIG_SUBNAV = [
  { id: 'overview', label: '总览', group: null },
  { id: 'model', label: '模型服务', group: null },
  { id: 'skills', label: '运行能力', group: null },
  { id: 'knowledge', label: '知识库', group: null },
  { id: 'storage', label: '本地存储', group: null },
  { id: 'about', label: '关于与诊断', group: null },
];

/* ============================================================
   通用 UI 辅助函数
   ============================================================ */
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function statusTag(state) {
  const map = {
    ok: ['ok', '正常'], warn: ['warn', '条件可用'], info: ['info', '已配置'],
    err: ['err', '异常'], gray: ['gray', '未验证'],
  };
  const [cls, label] = map[state] || map.gray;
  return `<span class="tag ${cls}"><span class="dot"></span>${label}</span>`;
}

function field(label, inner, extra = '') {
  return `<div class="field"><label>${label}${extra}</label>${inner}</div>`;
}
function textInput(id, val, type = 'text', ph = '') {
  return `<input type="${type}" id="${id}" value="${esc(val)}" placeholder="${ph}" />`;
}
function switchInput(id, checked) {
  return `<label class="switch"><input type="checkbox" id="${id}" ${checked ? 'checked' : ''} /><span class="slider"></span></label>`;
}

/* 配置页头部操作栏 */
function configToolbar() { return ''; }

// 所有就绪度读取共用一个快照；重启或地址改变后旧请求不得回写。
const _overviewCache = { data: null, error: '', endpoint: state.apiBase, generation: 0, inFlight: null, signature: '' };
let _lastApiError = '';

function backendEndpointLabel() {
  try { return new URL(state.apiBase).origin; }
  catch { return '当前后端'; }
}
function invalidateReadySnapshot() {
  _overviewCache.generation += 1;
  _overviewCache.data = null;
  _overviewCache.error = '';
  _overviewCache.inFlight = null;
  _overviewCache.signature = '';
  _overviewCache.endpoint = state.apiBase;
}
function readyCheck(key) {
  const c = _overviewCache.data?.checks?.[key];
  if (_overviewCache.error) return { level: 'gray', label: '未检测', detail: `检测失败：${_overviewCache.error}` };
  if (!c) return { level: 'gray', label: '未检测', detail: '尚未取得检测结果' };
  if (key === 'model') {
    if (c.enabled === false) return { level: 'gray', label: '未启用', detail: '模型未启用；规则解析与离线计算仍可使用' };
    return { level: c.configured || c.ok ? 'info' : 'warn', label: c.configured || c.ok ? '已配置' : '未配置', detail: c.detail || '配置状态不代表实际推理通过' };
  }
  if (key === 'knowledge' && c.enabled === false) return { level: 'gray', label: '未启用', detail: c.detail || '检索未启用；不影响离线规则计算' };
  if (key === 'license') return { level: 'info', label: '许可说明', detail: c.detail || '源码与第三方组件各自遵守其许可' };
  const labels = { engine: ['可定位', '不可用'], sidecar: ['可达', '未连接'], storage: ['检测通过', '需检查'], skills: ['已加载', '不可用'], knowledge: ['可用', '未就绪'] };
  return { level: c.ok ? 'ok' : 'warn', label: (labels[key] || ['通过', '需检查'])[c.ok ? 0 : 1], detail: c.detail || '接口未提供详细信息' };
}
function overviewRegionHtml() {
  const order = [['sidecar','后端服务'],['engine','计算引擎'],['model','模型服务'],['storage','本地存储'],['knowledge','知识库'],['skills','工具注册表']];
  return `<div class="grid grid-3" id="ready-cards">${order.map(([key,label]) => {
    const c = readyCheck(key);
    return `<div class="status-card"><div class="label">${label}</div><div style="margin:6px 0"><span class="tag ${c.level}">${c.label}</span></div><div class="foot">${esc(c.detail)}</div></div>`;
  }).join('')}</div>
  <div class="card" style="margin-top:14px"><div class="card-title">本次检测范围</div>
    <div class="foot">后端：${esc(backendEndpointLabel())}；运行版本：${esc(_overviewCache.data?.backend_version || '接口未提供')}。</div>
    <div class="foot" style="margin-top:8px">引擎注册或可定位不代表本次求解通过；模型已配置不代表服务可达或结构化推理通过。离线计算不要求启用模型和知识库。</div>
    <div class="foot" style="margin-top:8px">${esc(readyCheck('license').detail)}</div></div>`;
}
function applyReadySnapshot(data, error = '') {
  state.apiOnline = Boolean(data && !error);
  _overviewCache.data = data;
  _overviewCache.error = error;
  for (const key of ['model','engine','sidecar','license']) {
    const c = readyCheck(key);
    statusState[key] = { ...c };
    renderStatusItem(`sb-${key}`, c.level, `${{model:'模型',engine:'引擎',sidecar:'后端',license:'许可证'}[key]} ${c.label}`, c.detail);
  }
  const signature = _sig({ data, error });
  if (signature === _overviewCache.signature) return;
  _overviewCache.signature = signature;
  if (currentPage === 'config' && currentSub === 'overview') {
    const region = document.getElementById('overview-ready-region');
    if (region) region.innerHTML = overviewRegionHtml();
  }
}
function fetchReadySnapshot({ force = false } = {}) {
  if (_overviewCache.endpoint !== state.apiBase) invalidateReadySnapshot();
  if (_overviewCache.inFlight) return _overviewCache.inFlight;
  if (!force && _overviewCache.data) return Promise.resolve(_overviewCache.data);
  const generation = _overviewCache.generation;
  const endpoint = state.apiBase;
  let pending;
  pending = Promise.resolve().then(() => t.apiRequest('GET','/api/ready')).then(data => {
    if (generation !== _overviewCache.generation || endpoint !== state.apiBase) return fetchReadySnapshot();
    state.apiOnline = true;
    applyReadySnapshot(data);
    return data;
  }).catch(error => {
    if (generation !== _overviewCache.generation || endpoint !== state.apiBase) return fetchReadySnapshot();
    state.apiOnline = false;
    _lastApiError = error.message || String(error);
    applyReadySnapshot(null, _lastApiError);
    throw error;
  }).finally(() => { if (_overviewCache.inFlight === pending) _overviewCache.inFlight = null; });
  _overviewCache.inFlight = pending;
  return pending;
}
async function loadReady() { return fetchReadySnapshot(); }
async function pageOverview() {
  await loadReady().catch(() => {});
  return `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><div class="section-title" style="margin:0">环境状态</div><button class="btn sm" data-config-action="health">刷新环境状态</button></div><div id="overview-ready-region">${overviewRegionHtml()}</div>`;
}

// 可用模型列表（云端走 /api/models，本地走 /api/local-models）
const _modelCache = { fetched: false, models: [], current: '', error: '', backend: '' };

let _modelFetchRevision = 0;
function modelConfigurationPending() {
  return ['draft','saving','pending','external','apply-failed','save-failed'].includes(_modelApplyState);
}
async function fetchModels(force = false, modeOverride) {
  const mode = modeOverride || state.llmMode || 'cloud';
  if (modelConfigurationPending()) return { fetched: true, pending: true, backend: mode, models: [], error: _modelStatus.text || '配置尚未应用' };
  if (_modelCache.fetched && !force && _modelCache.backend === mode) return _modelCache;
  const revision = ++_modelFetchRevision;
  const saveRevision = _modelSaveRevision;
  const backendGeneration = _overviewCache.generation;
  const url = mode === 'local' ? `/api/local-models?url=${encodeURIComponent(state.llmOllamaUrl || 'http://localhost:11434/v1')}` : '/api/models';
  let result;
  try {
    const data = await api(url);
    result = { fetched: true, backend: mode, models: data?.models || [], current: data?.current || '', error: data?.ok ? '' : data?.error || '模型列表不可达' };
  } catch (error) { result = { fetched: true, backend: mode, models: [], current: '', error: error.message }; }
  if (revision === _modelFetchRevision && mode === state.llmMode && saveRevision === _modelSaveRevision && backendGeneration === _overviewCache.generation && !modelConfigurationPending()) Object.assign(_modelCache, result);
  return result;
}

/* 2. 模型服务 */
// ⚠️ 本页原先是 async 且首行 `await fetchModels()`，而 /api/models 会跨网络请求
// 远端 /v1/models —— 整页必须等它返回才写 innerHTML，点进来就会"卡"好几秒。
// 改法：同步渲染（手输兜底，立刻可用）→ 后台拉列表 → 只替换「模型名称」那一个字段。
function renderModelField(m, preferred, mode) {
  mode = mode || state.llmMode || 'cloud';
  const fallback = mode === 'local' ? (state.llmLocalModel || 'qwen2.5:7b') : (state.llmCloudModel || 'qwen-plus');
  const cur = preferred || fallback || state.llmModel || m.current || fallback;
  if (m.models && m.models.length) {
    const list = m.models.slice();
    if (cur && !list.includes(cur)) list.unshift(cur); // 保住用户已填/已选的值
    if (mode === 'local') {
      // 本地模型一般只有几个：<select> 直接下拉最直观
      return `<select id="m-model">
           ${list.map((id) => `<option value="${esc(id)}" ${id === cur ? 'selected' : ''}>${esc(id)}</option>`).join('')}
         </select>
         <div class="foot">本地 Ollama 已安装 ${m.models.length} 个模型${m.error ? `（${esc(m.error)}）` : ''}</div>`;
    }
    // 云端有 249 个模型：用 datalist（输入即过滤的下拉），不用 249 项的 <select>
    return `<input type="text" id="m-model" list="cloud-model-list" value="${esc(cur)}" autocomplete="off"
              placeholder="输入关键字过滤，如 qwen-plus" />
         <datalist id="cloud-model-list">
           ${list.map((id) => `<option value="${esc(id)}"></option>`).join('')}
         </datalist>
         <div class="foot">云端模型库共 ${m.models.length} 个可用，输入关键字即可过滤选择${m.error ? `（${esc(m.error)}）` : ''}</div>`;
  }
  // 没拉到列表：把长长的远端错误**折叠成一个小标签**，避免把「基础配置」整张卡撑爆。
  // - 正在拉取 → 灰色"⟳ 拉取中"标签
  // - 拉取失败 → 黄色"⚠ 远端不可达"标签，hover 显示完整错误；不阻塞填写
  // 用户的当前值（cur）仍能正常输入与保存，与远端是否可达无关。
  if (m.fetched) {
    const errShort = m.pending ? '配置待应用' : (m.error ? '列表不可达' : '未拉到模型');
    const errFull  = m.error ? esc(m.error) : '请稍后重试';
    return `<input type="text" id="m-model" value="${esc(cur)}" autocomplete="off" />
       <div class="foot" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
         <span class="tag warn" title="${errFull}">⚠ ${errShort}</span>
         <span>可手动填写（不影响保存），或点「刷新模型列表」重试</span>
       </div>`;
  }
  return `<input type="text" id="m-model" value="${esc(cur)}" autocomplete="off" />
     <div class="foot" style="display:flex;align-items:center;gap:6px">
       <span class="tag gray">⟳ 拉取中</span><span>可直接手输，不影响</span>
     </div>`;
}

// 后台补齐下拉：只改 #model-field-slot 这一块，不重渲染整页
async function refreshModelField(force = false) {
  const slot = document.getElementById('model-field-slot');
  if (!slot) return;
  const mode = state.llmMode;
  const revision = _modelSaveRevision;
  const backendGeneration = _overviewCache.generation;
  const data = await fetchModels(force, mode);
  if (mode !== state.llmMode || revision !== _modelSaveRevision || backendGeneration !== _overviewCache.generation || currentPage !== 'config' || currentSub !== 'model') return;
  const currentSlot = document.getElementById('model-field-slot');
  if (!currentSlot || currentSlot !== slot) return;
  const preferred = document.getElementById('m-model')?.value || '';
  currentSlot.innerHTML = renderModelField(data, preferred, mode);
  bindModelFieldInput();
}

function pageModel() {
  const mode = state.llmMode || 'cloud';

  // ====== 模式选择 ======
  const modeRow = `
    <div class="form-row" style="margin-bottom:14px">
      <div class="form-label" style="margin-bottom:6px">连接方式</div>
      <div style="display:flex;gap:18px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="radio" name="llm-mode" value="cloud" ${mode === 'cloud' ? 'checked' : ''} />
          <span><b>云端 API</b>（API-Key 接入 DashScope/OpenAI/DeepSeek 等）</span>
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="radio" name="llm-mode" value="local" ${mode === 'local' ? 'checked' : ''} />
          <span><b>本地 Ollama</b>（无需 Key，断网可用）</span>
        </label>
      </div>
      <div class="foot" style="margin-top:6px">
        切换模式会自动保存；请 <b>等待自动保存及空闲后生效</b>，环境变量才会更新。
      </div>
    </div>`;

  // ====== 模式专属字段（两套界面各自独立取值，互不串台）======
  let modeSpecific = '';
  if (mode === 'cloud') {
    // ── 界面一：云端 API ──
    // Base URL 必须读云端专用值：llmBaseUrl 在本地模式下会被覆盖成 Ollama 地址
    const cloudUrl = state.llmCloudBaseUrl || '';
    modeSpecific = `
      ${field('Base URL', `<input type="text" id="m-url" value="${esc(cloudUrl)}" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" autocomplete="off" />`)}
      ${field('API Key', `
        <div style="display:flex;flex-direction:column;gap:6px">
          <div style="display:flex;gap:6px;align-items:center">
            <input type="${_apikeyRevealed ? 'text' : 'password'}" id="m-apikey" value="${esc(state.llmApiKey || '')}"
              placeholder="sk-…  （留空会判为未配置）" autocomplete="off"
              style="flex:1;font-family:ui-monospace,Consolas,monospace;letter-spacing:0" />
            <button class="btn sm" type="button" data-config-action="apikey-toggle"
              title="${_apikeyRevealed ? '隐藏明文（防止截图泄露）' : '显示明文（用于核对）'}"
              style="min-width:72px">${_apikeyRevealed ? '🙈 隐藏' : '👁 显示'}</button>
          </div>
          <div class="foot">填错或泄露时，先在 <b>云控制台禁用 Key</b> 再点 <b>等待自动保存及空闲后生效</b>。Key 与云端地址随云端界面单独保存，切换到本地不会丢失。</div>
        </div>`)}
    `;
  } else {
    // ── 界面二：本地模型 ──
    modeSpecific = `
      ${field('Ollama 地址', `
        <div style="display:flex;flex-direction:column;gap:6px">
          <input type="text" id="m-ollama-url" value="${esc(state.llmOllamaUrl || 'http://localhost:11434/v1')}" placeholder="http://localhost:11434/v1" autocomplete="off" />
          <div class="foot">默认 <code>http://localhost:11434/v1</code>。需先双击 Ollama 图标启动服务；模型用 <code>ollama pull &lt;name&gt;</code> 拉取。</div>
        </div>`)}
      ${field('API Key', `
        <div style="padding:8px 10px;border:1px dashed var(--border);border-radius:6px;color:var(--text-muted);font-size:12.5px">
          本地 Ollama <b>不需要</b> API Key。你的云端 Key 已单独保存，切回云端界面原样还在。
        </div>`)}
    `;
  }

  // ====== 模型名称（两模式都有，但拉取源不同）======
  const modelRow = `
    <div class="form-row" style="margin-top:8px">
      <div class="form-label">模型名称</div>
      <div id="model-field-slot">${renderModelField(_modelCache, null, mode)}</div>
    </div>`;

  // ====== 自动保存提示（不再放手动按钮：改完即存，任务结束后自动生效）======
  const autoSaveHint = `
    <div class="foot" style="margin-top:14px;display:flex;align-items:center;gap:6px">
      <span id="model-autosave-status" class="tag gray"><span class="dot"></span>自动保存</span>
      <span>修改任一项后自动保存；后端会在任务空闲时自动重启使配置生效。</span>
    </div>`;

  // ====== 测试连接 ======
  const testRow = `
    <div style="display:flex;gap:10px;align-items:center;margin-top:14px">
      <button class="btn primary" data-config-action="model-test">检查连接</button>
      <span id="conn-status" class="tag gray"><span class="dot"></span>未测试（点按钮探测）</span>
    </div>`;

  return `
    ${configToolbar()}
    <div class="card">
      <div class="card-title">基础配置</div>
      ${modeRow}
      <div class="grid grid-2">
        ${field('接口类型', `<span>${mode === 'local' ? '本地 Ollama（OpenAI 兼容）' : 'OpenAI 兼容云端 API'}</span>`)}
        ${modeSpecific}
        ${modelRow}
      </div>
      ${autoSaveHint}
      ${testRow}
      <button class="btn sm" style="margin-top:10px" data-config-action="models-refresh">刷新模型列表</button>
    </div>
    <div class="section-title">本地凭据</div>${credentialInfoHtml()}
    <div class="section-title">产品边界</div>
    <div class="card" style="color:var(--text-muted);font-size:12.5px;line-height:1.7">
      本软件连接 OpenAI 兼容模型服务，不负责下载、安装、启动或监督本地大模型运行时。
      用户可填写本地服务地址，但本软件不管理其进程、模型目录和显卡。</div>`;
}

// 与本版本后端 TOOL_REGISTRY 对应维护的只读工具清单。
function pageSkills() {
  const tools = [['load_case','装载算例'],['validate_case','输入与编译检查'],['profile_network','网络画像'],['build_solver_profile','冻结求解策略'],['run_powerflow','潮流求解'],['extract_powerflow_result','结果归一化'],['verify_power_balance','完整性、功率平衡与同引擎复算'],['export_artifacts','导出报告与证据']];
  return `<div class="card"><div class="card-title">固定流程与8个标准工具</div><table><thead><tr><th>工具</th><th>作用</th></tr></thead><tbody>${tools.map(([name,detail])=>`<tr><td><code>${name}</code></td><td>${detail}</td></tr>`).join('')}</tbody></table><div class="foot" style="margin-top:12px">工具清单与本版本代码共同维护；加载状态见总览。模型在受约束节点参与理解、解释和有限恢复选择，数值来自OpenDSS。</div></div>
  <div class="card" style="margin-top:14px"><div class="card-title">支持边界</div><p>单场景稳态潮流，支持规定结构的DSS、ZIP、XLSX、CSV、JSON和静态MATPOWER输入。原生DSS按模型分相；有明确相别的表格可分相，无相别按三相等值。Q限额按输入策略有条件支持。</p><div class="foot">独立验证使用同一引擎复算，不是第二求解器认证。不提供自动调度、重构、短路、概率潮流、时序优化或跨会话自主学习。详细限制以本次报告为准。</div></div>`;
}

/* 8. 知识与经验 */
// RAG 知识库状态改为读取后端真实数据（/api/rag/status），
// 并提供"重建索引"刷新按钮——新增/修改 knowledge/ 里的文档后点它即可重建。
async function pageKnowledge() {
  let st = null;
  let err = '';
  try {
    st = await api('/api/rag/status');
  } catch (e) {
    err = e.message || String(e);
  }
  const pr = (st && st.power_rag) || {};
  const localReady = Boolean(st && st.ready);
  const prOk = Boolean(pr.ok);

  const kbCard = `
    <div class="card" id="kb-card">
      <div class="card-title" style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <span>RAG 知识库</span>
        <button class="btn sm" data-config-action="kb-refresh" title="重建本地知识库索引">
          <span class="refresh-icon" aria-hidden="true">⟳</span><span class="btn-label">重建索引</span>
        </button>
      </div>
      ${err ? `<div class="foot" style="color:#b42318;margin:6px 0">状态获取失败：${esc(err)}</div>` : ''}
      <table><thead><tr><th>检索后端</th><th>状态</th><th>文档 / 片段</th><th>说明</th></tr></thead><tbody>
        <tr>
          <td><b>本地知识库</b>（BM25）</td>
          <td>${statusTag(localReady ? 'ok' : 'warn')}</td>
          <td>${(st && st.num_sources != null) ? `${st.num_sources} 份 / ${st.num_chunks} 片段` : '—'}</td>
          <td class="foot">${esc((st && st.knowledge_dir) || '—')}</td>
        </tr>
        <tr>
          <td><b>Power-RAG</b>（外部服务）</td>
          <td>${statusTag(pr.enabled ? (prOk ? 'ok' : 'warn') : 'gray')}</td>
          <td>${pr.points_count != null ? `${pr.points_count} 个向量片段` : '—'}</td>
          <td class="foot">${esc(pr.detail || (pr.enabled ? pr.base_url || '' : '未启用'))}</td>
        </tr>
      </tbody></table>
      <div class="foot" style="margin-top:8px">
        新增或修改 <code>knowledge/</code> 下的文档后，点右上角「重建索引」使其生效；
        跨领域弱命中会自动回落或被门槛滤除，不会污染回答。
      </div>
    </div>`;

  return `
    ${configToolbar()}
    ${kbCard}
    <div class="foot">知识库可选；检索就绪不代表回答质量已验收。未启用时不影响规则计算。</div>`;
}

/* 9. 样本与本地存储 */
// 真实数据来自 /api/storage（只读盘点）。清理走 /api/storage/archive，归档而非删除。
const _storageCache = { data: null };

const STORAGE_GROUPS = [
  { key: 'empty_session_dirs', label: '空会话目录', risk: 'low', defaultOn: true },
  { key: 'failed_task_residue', label: '失败残留任务', risk: 'low', defaultOn: true },
  { key: 'orphan_outputs', label: '孤儿产物（过程记录已丢失）', risk: 'medium', defaultOn: false },
  { key: 'legacy_outputs', label: '旧口径产物（汇报勿用）', risk: 'medium', defaultOn: false },
];

function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

async function pageStorage() {
  let d = null;
  let err = '';
  if (!_storageCache.data) {
    try {
      _storageCache.data = await api('/api/storage');
    } catch (e) {
      err = e.message || String(e);
    }
  }
  d = _storageCache.data;
  if (!d) {
    return `${configToolbar()}
      <div class="card"><div class="card-title">本地存储</div>
      <div class="foot" style="color:#b42318">盘点失败：${esc(err)}</div>
      <button class="btn sm" data-config-action="storage-rescan" style="margin-top:10px">重新盘点</button></div>`;
  }

  const dirRow = (x) => `
    <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">
      <span title="${esc(x.path || '')}">${esc(x.name)}<span class="foot" style="margin-left:8px">${esc(x.note || '')}</span></span>
      <span>${x.file_count} 文件 · ${fmtBytes(x.size_bytes)}</span>
    </div>`;

  const groupCard = (g) => {
    const items = (d.groups[g.key] || []);
    const size = d.sizes[g.key] || 0;
    const riskTag = g.risk === 'low' ? statusTag('ok') : statusTag('warn');
    const body = items.length === 0
      ? `<div class="foot" style="margin-top:8px">暂无此类条目。</div>`
      : `<div style="max-height:190px;overflow:auto;margin-top:8px;border:1px solid var(--border);border-radius:6px">
          ${items.slice(0, 200).map((it) => `
            <label style="display:flex;gap:8px;align-items:center;padding:5px 9px;border-bottom:1px solid var(--border);cursor:pointer">
              <input type="checkbox" class="stg-item" data-kind="${g.key}" value="${esc(it.path)}" ${g.defaultOn ? 'checked' : ''} />
              <span style="flex:1;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(it.rel_path)}">${esc(it.rel_path)}</span>
              <span class="foot">${fmtBytes(it.size_bytes)}</span>
            </label>`).join('')}
        </div>
        ${items.length > 200 ? `<div class="foot" style="margin-top:6px">仅显示前 200 条，共 ${items.length} 条。</div>` : ''}
        <div class="foot" style="margin-top:8px">${esc(d.advice[g.key] || '')}</div>`;
    return `
      <div class="card" style="margin-bottom:12px">
        <div class="card-title" style="display:flex;align-items:center;gap:8px">
          <span>${esc(g.label)}</span>${riskTag}
          <span class="foot" style="margin-left:auto">${items.length} 个 · ${fmtBytes(size)}</span>
        </div>
        ${body}
      </div>`;
  };

  const aux = (d.aux_dirs || []);
  const kept = d.kept || {};
  const keptList = Object.keys(kept);

  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">状态与位置</div>
      <div class="grid grid-2" style="font-size:12.5px;color:var(--text-muted)">
        ${[...(d.dirs || []).map(item => [item.name, item.path]), ['会话数据库', d.db.path]].map(([a,b]) => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span>${a}</span><span>${esc(b)}</span></div>`).join('')}
      </div>
      <div class="grid grid-2" style="margin-top:12px">
        <div class="status-card"><div class="label">当前占用空间</div><div class="value">${esc(d.total_human)}</div></div>
        <div class="status-card"><div class="label">文件与记录数量</div><div class="value">${d.total_files}</div></div>
      </div>
      <div style="margin-top:12px">
        ${(d.dirs || []).map(dirRow).join('')}
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">
          <span>chat.db<span class="foot" style="margin-left:8px">会话与消息数据库</span></span>
          <span>1 文件 · ${fmtBytes(d.db.size_bytes)}</span>
        </div>
      </div>
    </div>

    <div class="section-title">可清理项（只读盘点，需勾选后归档）</div>
    <div class="foot" style="margin:-4px 0 10px">清理方式为<b>归档</b>（移动到 <code>_archive/&lt;时间戳&gt;/</code>），不直接删除，归档后可手动移回。</div>
    ${STORAGE_GROUPS.map(groupCard).join('')}

    <div class="card" style="position:sticky;bottom:0">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <button class="btn sm" data-config-action="storage-rescan">重新盘点</button>
        <button class="btn sm" data-config-action="storage-archive">归档选中项</button>
        <span class="foot" id="stg-summary">未选中任何条目</span>
      </div>
      <div class="foot" style="margin-top:8px">
        单批最多 10 条。<b>uploads 中的算例原件、chat.db、审计日志永不参与清理。</b>
      </div>
    </div>

    ${aux.length ? `
    <div class="section-title">outputs 下的辅助数据（非任务产物，不参与清理）</div>
    <div class="card" style="font-size:12.5px;color:var(--text-muted)">
      ${aux.map((x) => `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)"><span>${esc(x.name)}</span><span>${x.file_count} 文件 · ${fmtBytes(x.size_bytes)}</span></div>`).join('')}
      <div class="foot" style="margin-top:8px">合计 ${fmtBytes(d.aux_bytes)}。含缓存、审计归档、PPT 素材与 RAG 索引，由各自功能管理。</div>
    </div>` : ''}

    ${keptList.length ? `
    <div class="section-title">保守保留项（含源文件，未纳入清理）</div>
    <div class="card" style="font-size:12.5px;color:var(--text-muted)">
      ${keptList.map((k) => `<div style="padding:5px 0;border-bottom:1px solid var(--border)"><b>${esc(k)}</b> —— ${esc(kept[k])}</div>`).join('')}
    </div>` : ''}

    <div class="section-title">不可清理项</div>
    <div class="card" style="font-size:12.5px;color:var(--text-muted)">
      ${(d.never_touch || []).map((t) => `<div style="padding:4px 0">· ${esc(t)}</div>`).join('')}
    </div>`;
}

function credentialInfoHtml() {
  return `<div class="card"><div class="grid grid-2">${field('凭据保存方式','<span class="tag warn">本地配置文件（未加密）</span>')}${field('API Key 配置',`<span class="tag ${state.llmApiKeySet ? 'info' : 'gray'}">${state.llmApiKeySet ? '已配置' : '未配置'}</span>`)}</div><div class="foot" style="margin-top:10px">API Key写入本机settings.json；隐藏字符只遮挡显示，不加密磁盘文件。日志未经过独立脱敏检查，请勿公开配置或原始日志。</div></div>`;
}
async function pageAbout() {
  const info = await Promise.resolve().then(() => t.getAppInfo()).catch(() => ({}));
  const ownership = { owned: '本窗口所属后端', external: '复用外部后端', none: '未启动所属后端' }[info.backend?.ownership] || '接口未提供';
  const rows = [['软件版本',info.appVersion || '接口未提供'],['后端代码版本',info.backendCodeVersion || '接口未提供'],['实际后端运行版本',_overviewCache.data?.backend_version || '未检测'],['后端归属',ownership],['后端地址',info.backend?.base || backendEndpointLabel()],['作者与维护者',info.author || 'yangshaopeng'],['源码许可',info.license || 'Apache-2.0']];
  return `<div class="card"><div class="card-title">版本与运行信息</div>${rows.map(([label,value])=>`<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span>${label}</span><b>${esc(value)}</b></div>`).join('')}<div class="foot" style="margin-top:10px">代码版本与当前连接的后端运行版本分别展示；尚未检测时不推定一致。第三方组件遵守各自许可。</div></div>
  <div class="card" style="margin-top:14px"><div class="card-title">诊断</div><div class="foot">最近界面API错误：${esc(_lastApiError || '本次界面尚未记录错误')}</div><button class="btn sm" data-config-action="open-logs" style="margin-top:12px">打开应用日志目录</button><div class="foot" style="margin-top:8px">${info.logs?.available ? '本应用日志目录可用。' : '日志目录尚未确认存在，打开时将返回实际结果。'} 日志为原始本机资料，未经脱敏。</div></div>`;
}

/* ============================================================
   分析工作台：真实接入 FastAPI 会话 / 消息 / 上传 / 任务
   ============================================================ */
/* ------------------------------------------------------------
   执行过程卡片（聊天流内）：显示**真实到达过**的阶段
   ------------------------------------------------------------
   数据两个来源，都来自后端：
     ① 实时 —— SSE 的 progress 事件，已累积在 state.liveStages；
     ② 回看 —— /api/tasks/{id}/trace，打开历史会话时拉一次。
   这里是**纯展示**，不再做任何推断：阶段名与说明都用后端给的原文。
   （原先此处是前端硬编码十步 + floor(百分比/10) 估算，
     界面上却写着"真实任务轨迹" —— 已废弃。）
   ------------------------------------------------------------ */
let _traceFetchingFor = null;
// 已经与后端"对齐"过的任务（见 reconcileTrace）。同一个任务只对齐一次。
let _traceReconciledFor = null;

// 换会话/新会话时清空轨迹状态。不清的话，旧任务的阶段会挂在新会话的
// 消息流里 —— 看起来就像"这段对话执行过这些步骤"，属于伪造过程。
function resetTraceState() {
  _traceViewGeneration += 1;
  state.traceLoading = false;
  state.liveStages = [];
  state.liveEvents = [];
  state.traceTaskId = null;
  state.execProgress = null;
  state.traceOpen = false;   // 回看历史时默认收起，要用再点开
  state.revealCount = 0;
  if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
  _traceFetchingFor = null;
  _traceReconciledFor = null;
}

// 用某个任务的完整轨迹覆盖界面状态。**只读**，不触发任何计算。
async function fetchTrace(tid, request = null) {
  const viewGeneration = _traceViewGeneration;
  const sessionId = state.activeSessionId;
  const res = await api(`/api/tasks/${encodeURIComponent(tid)}/trace`);
  if (viewGeneration !== _traceViewGeneration || sessionId !== state.activeSessionId
      || (request && !displaysMessageRequest(request))) return;
  const stages = (res && res.stages) || [];
  state.liveStages = stages.map((s) => ({
    stage: s.stage,
    label: s.title || s.stage,
    // 动作行与思考段都由后端算好下发（模板只有一处：runtime/narration.py）
    action: s.action || null,
    narration: s.narration || '',
    first_ts: s.first_ts,
    last_ts: s.last_ts,
    percent: null,
    events: s.event_count,
    ok: s.ok !== false,
  }));
  state.liveEvents = (res && res.events) || [];
  state.traceTaskId = tid;
  // 回看历史：直接给成品，不必再逐条演一遍
  state.revealCount = state.liveEvents.length;
  updateTracePanel();
}

async function ensureTraceLoaded(session) {
  const msg = [...((session && session.messages) || [])].reverse().find((m) => m.task_id);
  const tid = msg && msg.task_id;
  if (!tid || _traceFetchingFor === tid) return;
  _traceFetchingFor = tid;
  const viewGeneration = _traceViewGeneration;
  state.traceLoading = true;
  try {
    await fetchTrace(tid);
  } catch (e) {
    // 该任务没有轨迹（或已清理）——保持空态，不编内容
  } finally {
    if (viewGeneration === _traceViewGeneration) state.traceLoading = false;
  }
}

/* 任务结束后与后端**对齐一次**。
   为什么必须有这一步：SSE 的最后一条事件（「生成报告」）落在 `tracer.flush()`
   之后，而前端在 HTTP 响应回来的 `finally` 里就会关掉事件流 —— 两者是赛跑关系，
   那一两条事件可能**永远没被推出来**。实测：跑的时候卡片 17 步、切走再切回来
   18 步，而两条路径都宣称"与后端一致"。
   对齐之后，"实时看到的"与"回看看到的"同源同源，不会再差一条。 */
async function reconcileTrace(tid, request = null) {
  if (request && !displaysMessageRequest(request)) return;
  tid = tid || state.traceTaskId;
  if (!tid || _traceReconciledFor === tid) return;
  _traceReconciledFor = tid;
  try {
    await fetchTrace(tid, request);
  } catch (e) {
    // 取不到就保持现状 —— 已经推出来的那部分仍然是真的，绝不编内容补上
  }
}

function renderTrace(session) {
  const stages = state.liveStages || [];
  if (!stages.length) {
    if (!state.traceLoading && !state.busy) ensureTraceLoaded(session);
    const live = isTaskRunning();
    return `<div class="et-idle">${live ? '正在执行，等待第一个阶段…' : '暂无执行记录'}</div>`;
  }
  return stages.map((s, i) => traceStepHtml(s, i, stages.length)).join('');
}

/* 一条步骤 = 动作行（图标 + 动词 + 对象 + 耗时）+ 紧随其后的思考段。
   这是"思考流"而不是"阶段清单"的关键：动作行只说**做了什么**，
   思考段回答**为什么做、看到了什么、接下来查什么**。
   思考段由后端按模板 + 真实数值生成（见 runtime/narration.py），
   这里只负责排版，不做任何推断。 */
function stepMetaText(s) {
  const dur = (s.first_ts && s.last_ts && s.last_ts > s.first_ts)
    ? `${(s.last_ts - s.first_ts).toFixed(2)}s` : '';
  const n = s.events > 1 ? `${s.events} 条` : '';
  return [dur, n].filter(Boolean).join(' · ');
}

// 思考段允许极少量行内标记：**粗体** 与 `代码`。
// ⚠️ 必须先转义再替换 —— 顺序反了等于给自己开一个注入口。
function richText(text) {
  return esc(text || '')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

// 单条步骤节点。抽出来是为了"增量追加"——逐条揭示时只 append 新节点，
// 不重画整块（整块重画会把内部滚动弹回顶部，还会让所有条目重播淡入）。
function traceStepHtml(s, i, total) {
  const running = isTaskRunning();
  const cls = (!running || i < total - 1) ? 'done' : 'running';
  const a = s.action || { verb: '执行', object: s.stage, kind: 'compute' };
  const note = s.narration || s.label || '';
  return `<div class="trace-step ${cls} k-${esc(a.kind || 'compute')}"
      data-stage="${esc(s.stage)}" data-note="${esc(note)}">
      <span class="ts-dot"></span>
      <div class="ts-action">
        <span class="ts-verb">${esc(a.verb || '')}</span>
        <span class="ts-obj">· ${esc(a.object || '')}</span>
        <span class="ts-meta">${esc(stepMetaText(s))}</span>
      </div>
      <div class="ts-note">${richText(note)}</div>
    </div>`;
}

// 增量渲染步骤列表：多出来的追加，少了的（切会话/清空）整块重画。
function renderTraceInto(box, session) {
  if (!box) return;
  const stages = state.liveStages || [];
  const have = box.querySelectorAll('.trace-step').length;
  if (have > stages.length) {           // 变少了：只能重画
    box.innerHTML = renderTrace(session);
    return;
  }
  for (let i = have; i < stages.length; i += 1) {
    box.insertAdjacentHTML('beforeend', traceStepHtml(stages[i], i, stages.length));
  }
  // 新条目一出现，先前那条就不再是"进行中"了 —— 黄点改蓝点
  const all = box.querySelectorAll('.trace-step');
  for (let i = 0; i < all.length - 1; i += 1) {
    if (all[i].classList.contains('running')) {
      all[i].classList.remove('running');
      all[i].classList.add('done');
    }
  }
  // ⚠️ 每一条都要对一次文案，**不能只刷最后一条**。
  // 实测缺陷：`export_artifacts` 在跑的时候先显示"正在导出产物…："，
  // 拿到结果后应变成"导出 8 项产物…"；可这条一旦不再是最后一条，
  // 就再也没人刷新它 —— 跑完看到的是那句"正在…"，切走再切回才变对。
  // 只改文本、不替换节点（替换会重播淡入，逐条揭示时就是一直在闪）。
  for (let i = 0; i < Math.min(all.length, stages.length); i += 1) {
    const el = all[i];
    const s = stages[i];
    const note = s.narration || s.label || '';
    if (el.dataset.note !== note) {
      const noteEl = el.querySelector('.ts-note');
      if (noteEl) noteEl.innerHTML = richText(note);
      el.dataset.note = note;
    }
    const metaEl = el.querySelector('.ts-meta');
    if (metaEl) {
      const m = stepMetaText(s);
      if (metaEl.textContent !== m) metaEl.textContent = m;
    }
  }
}

// 「任务是否还在跑」的**单一判定**，各处共用。
// 不能只看 state.busy：请求已返回但后端可能仍在收尾；
// 也不能只看 execProgress.active：任务结束后手上还是上一轮轮询的旧值
// （active=true / 96%），卡片会继续显示"执行中"。故辅以后端进度里的 done。
function isTaskRunning() {
  const p = state.execProgress;
  return Boolean(state.busy || (p && p.active && !p.done));
}

// 执行过程卡片标题行右侧的状态：任务跑起来后每秒都会变，
// 让用户一眼看出到底卡在哪一步、是不是还在动。
function traceStatusText() {
  const p = state.execProgress;
  const n = (state.liveStages || []).length;
  if (isTaskRunning()) {
    const pct = p && p.percent != null ? `${Math.max(0, Math.min(100, Number(p.percent)))}%` : '';
    const label = (p && p.label) || '正在准备任务…';
    return `执行中 · ${label}${pct ? `（${pct}）` : ''}`;
  }
  return n ? `已完成 · ${n} 步` : '';
}

// 后端用 [[SUPP]] 把"用户问到的主答"与"补充说明"分开，
// 补充说明默认收起，避免回答显得冗余。
const SUPP_MARK = '[[SUPP]]';

function renderContent(text) {
  const raw = text || '';
  const idx = raw.indexOf(SUPP_MARK);
  if (idx < 0) return esc(raw).replace(/\n/g, '<br/>');
  const main = raw.slice(0, idx).replace(/\s+$/, '');
  const supp = raw.slice(idx + SUPP_MARK.length).replace(/^\s+/, '');
  if (!supp) return esc(main).replace(/\n/g, '<br/>');
  const n = supp.split('\n').filter((l) => l.trim().startsWith('-')).length || 1;
  return `${esc(main).replace(/\n/g, '<br/>')}`
    + `<details class="supp-block"><summary>补充说明（${n} 项，点击展开）</summary>`
    + `<div class="supp-body">${esc(supp).replace(/\n/g, '<br/>')}</div></details>`;
}

function renderMessage(m) {
  const cls = m.role === 'user' ? 'chat-user' : 'chat-agent';
  const attachments = (m.attachments || []).map((a) => `<div class="chat-card">附件：${esc(a.filename || a.name || '算例文件')}</div>`).join('');
  const task = m.task_id ? `<div class="chat-card"><b>任务 ${esc(m.task_id)}</b><br/>状态：${esc(m.task_status || 'unknown')}${m.report_url ? `<br/><button class="btn sm primary js-open-report" data-url="${esc(m.report_url)}">打开报告</button>` : ''}</div>` : '';
  return `<div class="chat-bubble ${cls}">${renderContent(m.content)}${attachments}${task}</div>`;
}

async function loadSessions(request = null) {
  const listGeneration = ++_sessionListGeneration;
  const selectedId = state.activeSessionId;
  const loadGeneration = _sessionLoadGeneration;
  const data = await api('/api/chat/sessions');
  if (listGeneration !== _sessionListGeneration || (request && !ownsMessageRequest(request))) return;
  state.sessions = data.sessions || [];
  if (!state.activeSessionId && state.sessions[0]) state.activeSessionId = state.sessions[0].id;
  // 列表请求等待期间用户已切换：让那次显式选择负责加载，避免抢占其请求。
  if (loadGeneration !== _sessionLoadGeneration || (selectedId && selectedId !== state.activeSessionId)) return;
  if (request && !displaysMessageRequest(request)) return;
  if (state.activeSessionId) await loadSession(state.activeSessionId, request);
  else renderWorkbench();
}

async function loadSession(sessionId, request = null) {
  stopBatchMonitoring();
  const loadGeneration = ++_sessionLoadGeneration;
  if (sessionId !== state.activeSessionId || (state.activeSession && state.activeSession.id !== sessionId)) {
    resetTraceState();
    state.activeSession = null;
  }
  state.activeSessionId = sessionId;
  const session = await api(`/api/chat/sessions/${encodeURIComponent(sessionId)}`);
  if (loadGeneration !== _sessionLoadGeneration || sessionId !== state.activeSessionId
      || (request && !ownsMessageRequest(request))) return;
  state.activeSession = session;
  renderWorkbench();
  startBatchMonitoring(session);
}

async function createSession() {
  const loadGeneration = ++_sessionLoadGeneration;
  try {
    const session = await api('/api/chat/sessions', 'POST', { title: '新聊天' });
    state.sessions.unshift(session);
    if (loadGeneration !== _sessionLoadGeneration) return;
    state.activeSessionId = session.id;
    state.activeSession = session;
    stopBatchMonitoring();
    resetTraceState();
    renderWorkbench();
  } catch (e) { showToast(`新建会话失败：${e.message}`, 'error'); }
}

function buildWorkbenchShell() {
  const session = state.activeSession || { messages: [] };
  const queue = state.sessions.length ? state.sessions.map((s) => `
    <div class="session-item ${s.id === state.activeSessionId ? 'active' : ''}" data-session="${esc(s.id)}">
      <div class="s-title">${esc(s.title || '新聊天')}</div>
      <div class="s-meta">${esc(s.updated_at || '')}</div>
    </div>`).join('') : `<div style="font-size:12px;color:var(--text-muted);padding:12px">暂无会话</div>`;
  const msgs = session.messages || [];
  const runningNow = isTaskRunning();
  let chat;
  if (!msgs.length) {
    // ⚠️ 空会话也必须带卡片：新会话发任务时首条回复还没落库，
    // 若这时候页面上没有卡片元素，整场执行过程就没地方可显示
    // （实测：卡片直到跑完、消息落库后才冒出来 —— 等于过程全程看不见）。
    chat = `<div class="chat-bubble chat-agent">请选择或新建一个会话，然后输入任务；也可以上传 .dss / .zip / .xlsx / .csv / .json / .m 算例。</div>`
      + traceCardShell(session);
  } else {
    // 过程卡片落在**最后一条回复之前**：先看过程、再看结论，和对话式助手一致。
    // 任务刚发出、回复还没落库时（runningNow）就落在末尾 —— 那才是"当前正在发生的事"。
    // 卡片带固定 id，全页只允许存在一个实例。
    let lastAgent = -1;
    for (let i = msgs.length - 1; i >= 0; i -= 1) {
      if (msgs[i].role !== 'user') { lastAgent = i; break; }
    }
    const card = traceCardShell(session);
    chat = (runningNow || lastAgent < 0)
      ? msgs.map(renderMessage).join('') + card
      : msgs.map((m, i) => (i === lastAgent ? card + renderMessage(m) : renderMessage(m))).join('');
  }
  return `
    <div style="display:flex;gap:14px;height:calc(100vh - 132px);min-height:520px">
      <div style="width:200px;flex-shrink:0;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:12px;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px"><b style="font-size:13px">会话队列</b><button class="btn sm primary" id="new-session">+ 新建</button></div>
        ${queue}
      </div>
      <div style="flex:1;display:flex;flex-direction:column;min-width:0">
        <div id="chat-messages" style="flex:1;overflow-y:auto;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:16px">${chat}${renderBatchCards(session)}</div>
        <div style="margin-top:10px;display:flex;gap:8px">
          <input id="chat-input" type="text" placeholder="输入任务目标，或上传算例文件…" style="flex:1" ${state.activeSessionId && !state.busy ? '' : 'disabled'} />
          <button class="btn" id="pick-file" ${state.activeSessionId && !state.busy ? '' : 'disabled'}>上传算例</button>
          <button class="btn primary" id="send-message" ${state.activeSessionId && !state.busy ? '' : 'disabled'}>${state.busy ? '执行中…' : '发送'}</button>
          <button class="btn" id="stop-task" title="中断当前潮流计算任务" ${state.busy ? '' : 'disabled'}>停止</button>
        </div>
        <div id="exec-progress"></div>
        <div id="selected-file" style="font-size:11px;color:var(--text-muted);padding:4px 2px"></div>
      </div>
    </div>`;
}

const BATCH_STATUS_LABELS = {
  queued: '等待计算', running: '计算中', cancelling: '正在停止', paused: '等待继续',
  success: '已完成', completed_with_errors: '部分未成功', cancelled: '已取消',
  interrupted: '已中断', partial: '部分结果', not_assessed: '未形成结论',
  non_converged: '未收敛', invalid_request: '请求无效', data_unavailable: '数据不可用',
  validation_failed: '校验未通过', system_failed: '执行失败',
};
function renderBatchCards(session) {
  const batches = session.batches || [];
  if (!batches.length) return '';
  return `<div id="batch-list">${batches.map(batch => {
    const active = ['queued', 'running', 'cancelling', 'paused'].includes(batch.status);
    const counts = batch.counts || {};
    const button = (action, title, item = '') => `<button class="btn sm" data-batch-action="${action}" data-batch-id="${esc(batch.id)}" data-item-id="${esc(item)}">${title}</button>`;
    return `<div class="chat-card batch-card" data-batch="${esc(batch.id)}">
      <b>批量计算 · ${esc(BATCH_STATUS_LABELS[batch.status] || batch.status)}</b>
      <span> ${counts.finished || 0}/${counts.total || (batch.items || []).length} 项已结束，${counts.success || 0} 项成功</span>
      <div style="margin:8px 0;display:flex;gap:8px">
        ${active && batch.status !== 'cancelling' ? button('cancel', '取消批次') : ''}
        ${batch.status === 'paused' ? button('resume', '继续未开始项') : ''}
        ${batch.can_retry ? button('retry-failed', '重试失败或中断项') : ''}
      </div>
      ${(batch.items || []).map(item => `<div style="padding:6px 0;border-top:1px solid var(--border)">
        <b>${esc(item.display_name)}</b> · ${esc(BATCH_STATUS_LABELS[item.status] || item.status)}
        ${item.task_id ? `<span style="font-size:11px"> ${esc(item.task_id)}</span>` : ''}
        ${['queued', 'running'].includes(item.status) ? button('cancel-item', '取消此项', item.id) : ''}
        ${item.task_id ? `<button class="btn sm" data-batch-trace="${esc(item.task_id)}">查看过程</button>` : ''}
        ${item.message?.report_url ? `<button class="btn sm primary js-open-report" data-url="${esc(item.message.report_url)}">打开报告</button>` : ''}
        ${item.error ? `<div style="font-size:12px">${esc(item.error)}</div>` : ''}
      </div>`).join('')}
    </div>`;
  }).join('')}</div>`;
}
function stopBatchMonitoring() {
  _batchMonitorGeneration += 1;
  _batchMonitor = null;
  if (_batchPollTimer) clearTimeout(_batchPollTimer);
  _batchPollTimer = null;
}
function ownsBatchMonitor(monitor) {
  return Boolean(monitor && monitor === _batchMonitor && monitor.generation === _batchMonitorGeneration
    && monitor.sessionId === state.activeSessionId && state.activeSession?.id === monitor.sessionId);
}
function startBatchMonitoring(session) {
  stopBatchMonitoring();
  if (!session || session.id !== state.activeSessionId || !Array.isArray(session.batches)) return;
  const monitor = { sessionId: session.id, generation: _batchMonitorGeneration };
  _batchMonitor = monitor;
  scheduleBatchPoll(monitor);
}
function scheduleBatchPoll(monitor) {
  if (!ownsBatchMonitor(monitor)) return;
  const active = (state.activeSession.batches || []).some(batch => ['queued', 'running', 'cancelling'].includes(batch.status));
  if (active) _batchPollTimer = setTimeout(() => pollBatches(monitor), 1000);
}
function applyBatchSnapshot(batches) {
  state.activeSession.batches = batches;
  const messages = new Map((state.activeSession.messages || []).map(message => [message.id, message]));
  for (const batch of batches) for (const item of batch.items || []) {
    if (item.message) messages.set(item.message.id, item.message);
  }
  state.activeSession.messages = [...messages.values()].sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
  // Polling updates results while the user can keep composing the next message.
  const input = document.getElementById('chat-input');
  const draft = input?.value || '';
  const focused = input && document.activeElement === input;
  const caret = input?.selectionStart;
  const selectionEnd = input?.selectionEnd;
  const chosenText = document.getElementById('selected-file')?.textContent || '';
  renderWorkbench();
  const replacement = document.getElementById('chat-input');
  if (replacement) {
    replacement.value = draft;
    if (focused && replacement.focus) {
      replacement.focus();
      if (Number.isInteger(caret) && replacement.setSelectionRange) replacement.setSelectionRange(caret, selectionEnd ?? caret);
    }
  }
  const chosen = document.getElementById('selected-file');
  if (chosen) chosen.textContent = chosenText;
}
async function pollBatches(monitor = _batchMonitor) {
  if (!ownsBatchMonitor(monitor)) return;
  try {
    const result = await api(`/api/chat/sessions/${encodeURIComponent(monitor.sessionId)}/batches`);
    if (!ownsBatchMonitor(monitor)) return;
    applyBatchSnapshot(result.batches || []);
  } catch (error) {
    // Preserve durable state during a temporary disconnect; never invent completion.
  } finally {
    if (ownsBatchMonitor(monitor)) scheduleBatchPoll(monitor);
  }
}
async function runBatchAction(batchId, action, itemId = '') {
  const sessionId = state.activeSessionId;
  if (!sessionId || !state.activeSession?.batches?.some(batch => batch.id === batchId)) return;
  stopBatchMonitoring();
  const generation = _batchMonitorGeneration;
  const suffix = action === 'cancel-item' ? `items/${encodeURIComponent(itemId)}/cancel` : action;
  try {
    const batch = await api(`/api/chat/sessions/${encodeURIComponent(sessionId)}/batches/${encodeURIComponent(batchId)}/${suffix}`, 'POST', {});
    if (generation !== _batchMonitorGeneration || sessionId !== state.activeSessionId) return;
    applyBatchSnapshot((state.activeSession.batches || []).map(current => current.id === batch.id ? batch : current));
  } catch (error) {
    if (generation === _batchMonitorGeneration && sessionId === state.activeSessionId) showToast(`批次操作失败：${error.message}`, 'error');
  } finally {
    if (generation === _batchMonitorGeneration && sessionId === state.activeSessionId) startBatchMonitoring(state.activeSession);
  }
}

/* ------------------------------------------------------------
   执行过程卡片（嵌在聊天流里的可折叠块）
   ------------------------------------------------------------
   形态对齐对话式助手：过程跟在消息流里，跑的时候自动展开、跑完可收起，
   而不是占用右侧一整栏常驻。
   数据仍全部来自后端（SSE 实时 + /api/tasks/{id}/trace 回看），纯展示。
   ------------------------------------------------------------ */
function traceCardShell(session) {
  const stages = state.liveStages || [];
  // 跑的时候自动展开（过程要看得见）；跑完保持用户上一次的选择
  // （不主动收起 —— 别在人家正看的时候把内容折叠掉）。
  const running = isTaskRunning();
  return `
    <details id="inline-trace" class="exec-trace" ${stages.length ? '' : 'hidden'} ${running || state.traceOpen ? 'open' : ''}>
      <summary class="et-summary">
        <span class="et-chev">▸</span>
        <b class="et-title">执行过程</b>
        <span class="et-status" id="trace-status">${esc(traceStatusText())}</span>
      </summary>
      <div class="trace-steps">${renderTrace(session)}</div>
      <details class="et-raw">
        <summary class="et-raw-sum">原始事件 <b id="et-raw-count">${(state.liveEvents || []).length}</b> 条</summary>
        <div class="et-raw-lines" id="et-raw-lines">${renderRawEvents()}</div>
      </details>
    </details>`;
}

// 逐条原始事件（阶段聚合之下的细节）。跑的时候默认收起，
// 想看"到底调了什么、返回什么"时点开 —— 阶段是概览，这里是原始记录。
function renderRawEvents() {
  // 与时间线一致：只画已经"揭示"到的那部分，保持两者同步
  const evs = (state.liveEvents || []).slice(0, state.revealCount || 0);
  if (!evs.length) return `<div class="et-raw-empty">暂无原始事件</div>`;
  return evs.map((e) => {
    const t = eventTimeText(e);
    return `<div class="et-raw-line"><span class="et-raw-t">${esc(t)}</span>`
      + `<span>${esc(describeTraceEvent(e))}</span></div>`;
  }).join('');
}

if (window.tianshu && window.tianshu.onTaskEvent) {
  window.tianshu.onTaskEvent(({ event, data, streamId }) => handleTaskStreamEvent(event, data, streamId));
}

function bindWorkbenchEvents() {
  // 记录用户对过程卡片的展开/收起选择：重新渲染工作台时按它还原，
  // 不然每次刷新都会弹回默认态、把用户的意图冲掉。
  const traceCard = document.getElementById('inline-trace');
  traceCard?.addEventListener('toggle', () => { state.traceOpen = traceCard.open; });
  document.getElementById('new-session')?.addEventListener('click', createSession);
  document.querySelectorAll('[data-session]').forEach((el) => el.addEventListener('click', () => loadSession(el.dataset.session)));
  document.querySelectorAll('[data-batch-action]').forEach(el => el.addEventListener('click', () => {
    el.disabled = true;
    runBatchAction(el.dataset.batchId, el.dataset.batchAction, el.dataset.itemId);
  }));
  document.querySelectorAll('[data-batch-trace]').forEach(el => el.addEventListener('click', () => {
    if (state.busy) return showToast('请先等待当前消息返回');
    resetTraceState();
    state.traceOpen = true;
    const generation = _traceViewGeneration;
    const sessionId = state.activeSessionId;
    const taskId = el.dataset.batchTrace;
    const ownsView = () => generation === _traceViewGeneration && sessionId === state.activeSessionId;
    fetchTrace(taskId).then(() => {
      // This explicit "view" click opens its own result. Ordinary polling still
      // preserves a user's collapsed card, including a fold while loading.
      if (!ownsView() || state.traceTaskId !== taskId || !state.traceOpen) return;
      const card = document.getElementById('inline-trace');
      if (card) {
        card.open = true;
        card.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
      }
    }).catch(error => {
      if (ownsView()) showToast(`过程读取失败：${error.message}`, 'error');
    });
  }));
  document.querySelectorAll('.js-open-report').forEach((el) => el.addEventListener('click', () => {
    const base = state.apiBase.replace(/\/$/, '');
    t.openExternal(base + el.dataset.url);
  }));
  const picker = document.getElementById('pick-file');
  const input = document.getElementById('chat-input');
  const send = document.getElementById('send-message');
  // 选中的算例文件（可多选）。放在 state 上而不是闭包变量里：`sendMessage` 提交后
  // 要能把它清空 —— 否则"清空了提示文字、变量还留着"会让下一次发送重复上传同一批文件。
  state.selectedFiles = state.selectedFiles || [];
  picker?.addEventListener('click', async () => {
    const picked = await t.chooseFile();
    // 主进程统一返回数组；这里再兜一层，兼容旧形态（null / 单个字符串）。
    const arr = Array.isArray(picked) ? picked : (picked ? [picked] : []);
    const label = document.getElementById('selected-file');
    // ⚠️ 同名文件必须拦住：后端按**原始文件名**落盘到同一个上传目录
    // （`os.path.join(upload_dir, safe_name)`），同名会**后者覆盖前者**，
    // 静默少一个算例。宁可让用户分开传，也不要悄悄丢文件。
    const names = arr.map((p) => p.replace(/^.*[\\/]/, ''));
    const dup = Array.from(new Set(names.filter((n, i) => names.indexOf(n) !== i)));
    if (dup.length) {
      state.selectedFiles = [];
      if (label) label.textContent = '';
      return showToast(`所选文件中有同名文件（${dup.join('、')}），后一个会覆盖前一个。请分开上传。`, 'error');
    }
    state.selectedFiles = arr;
    if (label) {
      label.textContent = arr.length === 0
        ? ''
        : (arr.length === 1
          ? `已选择：${arr[0]}`
          : `已选择 ${arr.length} 个文件：${names.join('、')}`);
    }
  });
  send?.addEventListener('click', () => sendMessage(state.selectedFiles));
  document.getElementById('stop-task')?.addEventListener('click', stopCurrentTask);
  input?.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(state.selectedFiles); } });
}

// 停止当前任务：先把任务号报给后端（工作流会在下一个阶段检查点中断），
// 再立刻解锁界面，避免用户点完还在等。
// 取不到任务号时兜底从 /api/task-progress 里取（后端写的就是当前任务的 id）。
async function stopCurrentTask() {
  const request = _activeMessageRequest;
  if (!ownsMessageRequest(request) || !state.busy) return;
  const btn = document.getElementById('stop-task');
  if (btn) { btn.disabled = true; btn.textContent = '停止中…'; }
  let taskId = request.taskId;
  try {
    if (!taskId) {
      const p = await api('/api/task-progress');
      if (!ownsMessageRequest(request)) return;
      taskId = (p && p.task_id) || '';
      if (_retiredTaskIds.has(taskId)) taskId = '';
    }
    if (taskId) {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, 'POST', {});
    }
  } catch (e) {
    // 后端不可达也要解锁界面：宁可让用户重试，也不要留下"死界面"
  }
  if (!ownsMessageRequest(request)) return;
  retireTask(taskId);
  state.busy = false;
  state.activeTaskId = null;
  state.execProgress = null;
  stopProgressPolling(request);
  stopTaskStream(request);
  clearBusyWatchdog(request);
  _activeMessageRequest = null;
  const stoppedGeneration = ++_messageGeneration;
  _stoppedMessageRequest = request;
  request.stopGeneration = stoppedGeneration;
  applyBusyUI();
  revealAll();   // 用户主动中断：已到的事件一次补齐，别继续慢慢演
  updateTracePanel();
  const box = document.getElementById('exec-progress');
  if (box) {
    box.innerHTML = taskId
      ? `<div style="margin-top:8px;font-size:12px;color:var(--text-muted)">已请求停止任务 ${esc(taskId)}（已完成的阶段结果仍会保留）</div>`
      : '<div style="margin-top:8px;font-size:12px;color:var(--text-muted)">已停止等待（后端未在运行任务）</div>';
    setTimeout(() => {
      if (stoppedGeneration !== _messageGeneration) return;
      const b = document.getElementById('exec-progress'); if (b) b.innerHTML = '';
    }, 4000);
  }
}

// 统一维护"执行中"状态。
// ⚠️ 关键：sendMessage 期间 loadSessions() → loadSession() → renderWorkbench() 会
// 整个重建 #page-body 的 DOM，此前缓存的 send/input 节点会变成"孤儿"。如果还在
// 旧节点上设 disabled，界面上就锁死了（按钮永远点不动）。
// 所以：① 每次都重新取 DOM；② DOM 重建时按钮文案/disabled 由 state.busy 决定。
function applyBusyUI() {
  const send = document.getElementById('send-message');
  const input = document.getElementById('chat-input');
  const pick = document.getElementById('pick-file');
  const stop = document.getElementById('stop-task');
  const usable = Boolean(state.activeSessionId) && !state.busy;
  if (input) input.disabled = !usable;
  if (pick) pick.disabled = !usable;
  if (stop) {
    stop.disabled = !state.busy;
    stop.textContent = '停止';
  }
  if (send) {
    send.disabled = !usable;
    send.textContent = state.busy ? '执行中…' : '发送';
  }
}

// ── 实时事件流（SSE，经主进程转发）───────────────────────────────
// 轮询只能给出"百分比"，事件流能给出"每一步在做什么、花了多久"。
// 两者并存：事件流失败时自动回退到轮询（不阻塞主流程）。
let _esRunning = false;
let _esOwner = null;

function describeTraceEvent(d) {
  const tool = d.tool_name ? `${d.tool_name}` : (d.stage || 'stage');
  const dur = typeof d.duration === 'number' ? ` (${d.duration.toFixed(2)}s)` : '';
  const msg = (d.message || '').slice(0, 90);
  return `${tool}${dur} ${msg}`;
}

// 阶段时间线 = trace 事件按 stage 聚合。
// 规则与后端 /api/tasks/{id}/trace 的 stages **完全一致**：按首次出现顺序去重，
// 动作行的标题与思考段都取该阶段**最后一条**事件 —— 阶段开头往往是"正在…"，
// 收尾那条才说清"这一步做成了什么"，实时与回看因此是同一套东西。
// ⚠️ 去重必须**按 stage 在整个数组里找**，不能只看数组最后一项：
//    实测同一个 stage 会在隔了别的阶段之后再次出现（`receive_request` 就是
//    先出用户原话、解析完再出一次目标），只看最后一项会多长出一条重复步骤 ——
//    跑的时候 19 条、刷新后 18 条，而两条路径都宣称"与后端一致"。
function appendStageFromEvent(e) {
  const stage = e && e.stage;
  if (!stage) return;
  const arr = state.liveStages;
  let row = arr.find((x) => x && x.stage === stage);
  if (!row) {
    row = { stage: stage, events: 0 };
    arr.push(row);
  }
  row.events += 1;
  if (e.action) row.action = e.action;
  if (e.narration) row.narration = e.narration;
  if (e.message) row.label = e.message;
  const ts = Number(e.ts);
  if (isFinite(ts)) {
    if (!row.first_ts) row.first_ts = ts;
    row.last_ts = ts;
  }
}

/* ------------------------------------------------------------
   逐条揭示：让过程"一条条长出来"，而不是"啪一下全出现"
   ------------------------------------------------------------
   为什么要这一层：后端一次任务的后半段（装载→求解→校验→导出）常常只花
   1~2 秒就写完全部事件，即便把推送间隔压到 0.08s，也仍有二十多条挤在
   同一瞬间到达 —— 直接画出来就是一堵墙。
   这里把"到达"与"画出"分开，按节奏逐条放出：积压多就快放（别拖），
   积压少就慢放（看得清）。
   ⚠️ 每条左边的**时间戳是真实的**（来自事件自身），被平滑的只是
      "它出现在屏幕上的时刻" —— 不是把假数据演成真的。
   ------------------------------------------------------------ */
let _revealTimer = null;

function revealGapMs(backlog) {
  if (backlog > 15) return 70;
  if (backlog > 5) return 140;
  return 260;
}

function scheduleReveal() {
  if (_revealTimer) return;
  const step = () => {
    _revealTimer = null;
    const total = (state.liveEvents || []).length;
    if (state.revealCount >= total) return;
    appendStageFromEvent(state.liveEvents[state.revealCount]);
    state.revealCount += 1;
    updateTracePanel();
    _revealTimer = setTimeout(step, revealGapMs(total - state.revealCount));
  };
  _revealTimer = setTimeout(step, 0);
}

// 回看历史 / 收尾时不必再演一遍，直接补齐
function revealAll() {
  if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
  const total = (state.liveEvents || []).length;
  while (state.revealCount < total) {
    appendStageFromEvent(state.liveEvents[state.revealCount]);
    state.revealCount += 1;
  }
}

function eventTimeText(e) {
  const ts = e && e.timestamp;
  if (typeof ts !== 'number' || !isFinite(ts)) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

function handleTaskStreamEvent(evt, data, streamId) {
  const request = _esOwner;
  if (!ownsMessageRequest(request) || (streamId !== undefined && streamId !== request.generation)) return;
  const taskId = data && data.task_id;
  // 一个新连接也可能先跟到正在退出的旧后端任务；没有 task_id 的 trace 继承该流的身份。
  if (evt === 'progress' && taskId) request.streamTaskId = taskId;
  const eventTaskId = taskId || (evt === 'trace' ? request.streamTaskId : null);
  if (eventTaskId && (_retiredTaskIds.has(eventTaskId)
      || (request.taskId && eventTaskId !== request.taskId))) return;
  if (evt === 'progress') {
    if (taskId) { request.taskId = taskId; state.activeTaskId = taskId; }
    if (data && data.done) return;
    if (data && typeof data.percent === 'number') {
      request.sawActive = true;
      observeOwnedTaskStatus(request, { ...data, active: true });
      if (!displaysMessageRequest(request)) return;
      state.execProgress = {
        active: true, stage: data.stage, label: data.label,
        percent: data.percent, task_id: data.task_id,
      };
      if (data.task_id) state.traceTaskId = data.task_id;
      // ⚠️ 阶段时间线**不在这里**拼。progress 写的 stage 与 trace 事件的 stage
      // 不是同一套粒度（实测：整场任务 progress 的 stage 只变了 1 次，
      //  而 trace 事件里能聚合出 15 个真实阶段）。拿它拼时间线只会得到
      // 一条假时间线 —— 真正的时间线见 appendStageFromEvent()。
      // 这里只负责"当前到哪一步、百分之多少"，也就是紧贴输入框那一行。
      renderProgress(state.execProgress);
    }
    return;
  }
  if (evt === 'trace') {
    request.sawActive = true;
    if (!displaysMessageRequest(request)) return;
    // source='sse' 标记实时到达的事件（回看接口写入的没这个标记）。
    // 用途：任务很快时"实时累积"与"回看一次性拉取"画面完全一样，
    // 靠这个标记才能判定实时链路到底有没有在工作 —— 别只看画面。
    state.liveEvents.push(Object.assign({ source: 'sse' }, data || {}));
    // 不直接入画：交给揭示节奏一条条放（见 scheduleReveal 的说明）
    scheduleReveal();
    if (state.execProgress) renderProgress(state.execProgress);
    return;
  }
  if (evt === 'done' || evt === 'closed') {
    stopTaskStream(request);
    reconcileTrace(taskId, request);
    return;
  }
}

// 过程卡片的重画统一走 updateTracePanel()（它同时更新顶部状态行），
// 这里不再另写一份，避免同一逻辑两处实现。

async function startTaskStream(request = _activeMessageRequest) {
  if (!ownsMessageRequest(request)) return;
  // 新任务开始：清空上一条的真实轨迹，避免新旧阶段混在一起
  state.liveStages = [];
  state.liveEvents = [];
  state.traceTaskId = null;
  state.revealCount = 0;
  if (_revealTimer) { clearTimeout(_revealTimer); _revealTimer = null; }
  _traceFetchingFor = null;
  _traceReconciledFor = null;
  if (!t.startTaskStream) return;
  _esOwner = request;
  try {
    const ok = await t.startTaskStream(request.generation);
    if (_esOwner === request && ownsMessageRequest(request)) _esRunning = Boolean(ok);
  } catch (e) {
    if (_esOwner === request) _esRunning = false;
  }
}

function stopTaskStream(request = null) {
  if (request && _esOwner !== request) return;
  // 建连 promise 尚未返回时也要停止；否则它会在取消后继续占着全局事件流。
  if (!_esOwner && !_esRunning) return;
  _esOwner = null;
  _esRunning = false;
  try { t.stopTaskStream && t.stopTaskStream(); } catch (e) { /* 忽略 */ }
}

// ── 执行进度轮询 ───────────────────────────────────────────────
// 大算例端到端要 2~5 分钟，期间 UI 只显示"执行中…"会让人以为卡死。
// 后端跑任务时会写 outputs/_current_progress.json，这里定时拉取并渲染
// 「阶段文字 + 进度条 + 已用时」，让用户看到它确实在动、动到哪一步。
let _progTimer = null;
let _progOwner = null;
let _progStartedAt = 0;

function renderProgress(p) {
  const box = document.getElementById('exec-progress');
  if (!box) return;
  if (!p || !p.active) { box.innerHTML = ''; return; }
  const pct = Math.max(0, Math.min(100, p.percent || 0));
  const secs = Math.max(0, Math.round((Date.now() - _progStartedAt) / 1000));
  const mm = String(Math.floor(secs / 60)).padStart(2, '0');
  const ss = String(secs % 60).padStart(2, '0');
  // 逐条事件不在这里重复显示 —— 它已经在聊天流那张「执行过程」卡片里了。
  // 这里只留"到哪一步、多少、用了多久"这一行，紧贴输入框，随时可见。
  box.innerHTML = `
    <div style="margin-top:8px">
      <div style="display:flex;justify-content:space-between;align-items:center;
                  font-size:12px;color:var(--text-muted);margin-bottom:5px">
        <span>${esc(p.label || '正在执行…')}</span>
        <span style="font-variant-numeric:tabular-nums">${pct}% · ${mm}:${ss}</span>
      </div>
      <div style="height:5px;background:var(--border);border-radius:3px;overflow:hidden">
        <div style="height:100%;width:${pct}%;background:var(--primary);
                    border-radius:3px;transition:width .4s ease"></div>
      </div>
    </div>`;
}

// 聊天流里那张过程卡片的局部重画。只改卡片内部，
// 不动 messages 容器 —— 否则会打断用户正在翻阅的位置。
function updateTracePanel() {
  const card = document.getElementById('inline-trace');
  if (!card) return;
  const stages = state.liveStages || [];
  const events = state.liveEvents || [];
  const running = isTaskRunning();
  // 执行中也显示（哪怕阶段还没到）：意图解析本身要几秒，这段时间若什么都不显示，
  // 用户会以为这次没有过程可看。先让面板出现、并写着"等待第一个阶段…"。
  card.hidden = !running && !stages.length && !events.length;
  if (card.hidden) return;
  // 执行中且用户没有主动收起 → 自动展开。
  // 否则会出现"卡片显示了、过程却在收起状态里看不到在走"。
  if (running && !card.open && state.traceOpen) card.open = true;
  // 逐条揭示会反复重画：先记下内部滚动位置，画完再恢复 ——
  // 否则每来一条就把用户正在看的位置弹回顶部。
  const traceEl = card.querySelector('.trace-steps');
  if (traceEl) {
    const stuck = traceEl.scrollHeight - traceEl.scrollTop - traceEl.clientHeight < 40;
    renderTraceInto(traceEl, state.activeSession);
    if (stuck || traceEl.scrollTop === 0) traceEl.scrollTop = traceEl.scrollHeight;
  }
  const statusEl = card.querySelector('#trace-status');
  if (statusEl) statusEl.textContent = traceStatusText();
  const rawBox = card.querySelector('#et-raw-lines');
  if (rawBox) {
    const raw = card.querySelector('.et-raw');
    const stick = raw && raw.open
      && rawBox.scrollHeight - rawBox.scrollTop - rawBox.clientHeight < 40;
    rawBox.innerHTML = renderRawEvents();
    const cnt = card.querySelector('#et-raw-count');
    if (cnt) cnt.textContent = String(state.revealCount || 0);
    if (stick) requestAnimationFrame(() => { rawBox.scrollTop = rawBox.scrollHeight; });
  }
  followTraceIfPinned();
}

// 任务结束后把卡片归位到"最后一条回复之前"（过程在上、结论在下）。
// 任务进行中它是落在末尾的 —— 那时"当前正在发生的事"就该在最下面。
// 只挪一次 DOM 节点，不重建聊天区（重建会把用户正在看的位置冲掉）。
function repositionTraceCard() {
  if (state.busy) return;
  const card = document.getElementById('inline-trace');
  const box = document.getElementById('chat-messages');
  if (!card || !box) return;
  const kids = Array.from(box.children);
  let lastAgent = null;
  for (let i = kids.length - 1; i >= 0; i -= 1) {
    if (kids[i].classList.contains('chat-agent')) { lastAgent = kids[i]; break; }
  }
  if (!lastAgent || card.nextElementSibling === lastAgent) return;
  box.insertBefore(card, lastAgent);
}

// 过程卡片增长时跟随到底 —— 但只在用户本来就贴着底部时跟随，
// 不然会把正在往回翻历史的人一直拽回来。
function followTraceIfPinned() {
  const box = document.getElementById('chat-messages');
  if (!box) return;
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  if (!nearBottom) return;
  requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
}

async function startProgressPolling(request = _activeMessageRequest) {
  if (!ownsMessageRequest(request)) return;
  stopProgressPolling();
  _progOwner = request;
  _progStartedAt = Date.now();
  let cleared = false;
  const tick = async () => {
    if (!ownsMessageRequest(request) || _progOwner !== request) return;
    try {
      const p = await api('/api/task-progress');
      if (!ownsMessageRequest(request) || _progOwner !== request) return;
      // ⚠️ 关键：任务请求发出后、后端还没开始写本次进度前，
      // 接口读到的仍是"上一次任务的 done 文件"。若此时就认为任务结束，
      // 执行过程卡片会直接跳成上次的全完成态并停止刷新 —— 这就是"一动不动"的根因。
      if (p && p.task_id) {
        if (_retiredTaskIds.has(p.task_id) || (request.taskId && p.task_id !== request.taskId)) return;
        if (p.active) {
          request.taskId = p.task_id;
          state.activeTaskId = p.task_id;
          request.sawActive = true;
          observeOwnedTaskStatus(request, p);
        } else if (!request.taskId && !p.done) {
          // 还没轮到本次任务 —— 但**「后端已明确标记完成」必须放行**：
          // sendMessage 收尾时会把 activeTaskId 清空，若这里一律 return，
          // 这份 done 就被挡掉了：进度条不自清、过程卡片标题永远停在
          // "执行中 · ……（96%）"，看起来像一直在跑。
          return;
        }
      }
      if (displaysMessageRequest(request)) {
        state.execProgress = p;
        updateTracePanel();
        renderProgress(p);
      }
      if (cleared || (p && !p.active && request.sawActive)) {
        cleared = true;
        stopProgressPolling(request);
        const viewGeneration = _traceViewGeneration;
        // 先保留 100% / 最终轨迹，等消息落库后再刷新会话。
        setTimeout(() => {
          if (!displaysMessageRequest(request) || viewGeneration !== _traceViewGeneration) return;
          const latest = document.getElementById('exec-progress');
          if (latest) latest.innerHTML = '';
          // ⚠️ 顺序要紧：**先清状态、再重画**。反过来（先前那版）重画时读到的
          // 还是旧的 execProgress（active=true / 92%），画完才清空 —— 于是卡片
          // 标题永远停在"执行中"，直到下一次有人重画。这类"顺序反了"的 bug
          // 界面单测发现不了，只有真机点完盯着状态看才露出来。
          state.execProgress = null;
          updateTracePanel();
        }, 1500);
        return;
      }
      // 兜底：本次是一条普通问答（后端不会跑任务），最多探 8 秒就收手，
      // 避免定时器一直空转。
      if (!request.sawActive && Date.now() - request.startedAt > 8000) {
        stopProgressPolling(request);
        if (!displaysMessageRequest(request)) return;
        state.execProgress = null;
        const box = document.getElementById('exec-progress');
        if (box) box.innerHTML = '';
        updateTracePanel();
      }
    } catch (e) {
      // 轮询失败时保留当前轨迹，并在下一轮重试；不回退到历史完成态。
      if (displaysMessageRequest(request)) updateTracePanel();
    }
  };
  await tick();
  if (ownsMessageRequest(request) && _progOwner === request) _progTimer = setInterval(tick, 1000);
}

function stopProgressPolling(request = null) {
  if (request && _progOwner !== request) return;
  _progOwner = null;
  if (_progTimer) { clearInterval(_progTimer); _progTimer = null; }
}

// 看门狗：避免请求悬挂导致 state.busy 永久为 true（界面彻底点不动）。
// 超时后强制解锁并提示——宁可让用户重试，也不要出现"死界面"。
let _busyWatchdog = null;
let _busyWatchdogOwner = null;
function armBusyWatchdog(ms = 180000, request = _activeMessageRequest) {
  clearBusyWatchdog();
  _busyWatchdogOwner = request;
  _busyWatchdog = setTimeout(() => {
    if (!ownsMessageRequest(request) || !state.busy) return;
    state.busy = false;
    clearBusyWatchdog(request);
    applyBusyUI();
    showToast('等待超时（3 分钟）。任务可能仍在后端执行，请稍后刷新查看结果。', 'error');
  }, ms);
}
function clearBusyWatchdog(request = null) {
  if (request && _busyWatchdogOwner !== request) return;
  _busyWatchdogOwner = null;
  if (_busyWatchdog) { clearTimeout(_busyWatchdog); _busyWatchdog = null; }
}

async function sendMessage(filePaths = null) {
  if (state.busy || !state.activeSessionId) return;
  const input = document.getElementById('chat-input');
  const content = input?.value?.trim() || '';
  // 支持多选：统一归一化成数组（兼容旧形态的单个路径 / null）
  const files = Array.isArray(filePaths) ? filePaths.filter(Boolean) : (filePaths ? [filePaths] : []);
  if (!content && files.length === 0) return showToast('请输入任务或选择算例文件', 'error');
  // 看门狗也会允许重试；此时上一个请求可能仍在后端执行。
  if (_activeMessageRequest) retireTask(_activeMessageRequest.taskId);
  const request = {
    generation: ++_messageGeneration, sessionId: state.activeSessionId,
    taskId: null, sawActive: false, responseReceived: false, startedAt: Date.now(),
  };
  _activeMessageRequest = request;
  _stoppedMessageRequest = null;
  _taskStatusRevision += 1;
  resetTraceState();
  state.busy = true;
  state.activeTaskId = null;
  // 先给一个可见的初始态：不能等后端回包才动，否则上传大算例的最初几秒
  // 执行过程卡片仍是上一次任务的完成态。
  state.execProgress = { active: true, stage: 'init', label: '正在准备任务…', percent: 5 };
  state.traceOpen = true;   // 开跑就把过程展开，用户能看着它一步步走
  applyBusyUI();
  updateTracePanel();
  renderProgress(state.execProgress);
  armBusyWatchdog(180000, request);
  startProgressPolling(request);
  // SSE 实时流：此前 `startTaskStream()` 只定义了、**全项目没有任何调用点**，
  // 于是 handleTaskStreamEvent 永不触发 —— 「执行过程」卡片在任务执行期间
  // 只能是空的，要等跑完靠 /api/tasks/{id}/trace 回填才显示。
  // 又一个"有实现≠在生效"：路由修好了，调用点却一直缺失。
  startTaskStream(request);
  try {
    let data;
    if (files.length) {
      data = await t.uploadFile(request.sessionId, files, content);
    } else {
      data = await t.sendMessage(request.sessionId, content);
    }
    const result = data.assistant_message ? data : data;
    if (result.batch_id) {
      if (!ownsMessageRequest(request)) return;
      request.batchId = result.batch_id;
      request.taskId = null;
      request.sawActive = false;
      stopProgressPolling(request);
      stopTaskStream(request);
      if (displaysMessageRequest(request)) resetTraceState();
      await loadSessions(request);
      if (ownsMessageRequest(request)) showToast('批次已提交，可在批次卡查看进度');
      return;
    }
    const assistant = result.assistant_message || result.message || {};
    synchronizeTerminalTaskStatus(request, assistant.task_id || result.task_id,
      assistant.task_status || result.task_status || result.status, assistant.session_id || result.session?.id);
    if (!ownsMessageRequest(request)) {
      retireTask(assistant.task_id || result.task_id);
      return;
    }
    request.taskId = assistant.task_id || result.task_id || request.taskId;
    request.responseReceived = true;
    state.activeTaskId = request.taskId;
    await loadSessions(request);
    if (!ownsMessageRequest(request)) return;
    showToast('任务响应已返回');
  } catch (e) {
    if (ownsMessageRequest(request)) showToast(`任务执行失败：${e.message}`, 'error');
  } finally {
    if (ownsMessageRequest(request)) {
      state.busy = false;
      clearBusyWatchdog(request);
      // 同步消息接口已返回该任务的终态，不能让迟到的轮询/SSE继续保留活动进度。
      // HTTP异常时没有这份终态证据，仍保留现有的后端进度语义。
      if (request.responseReceived && request.taskId) {
        stopProgressPolling(request);
        if (displaysMessageRequest(request)
            && (!state.execProgress?.task_id || state.execProgress.task_id === request.taskId)) {
          state.execProgress = null;
          renderProgress(null);
        }
      }
      // 重新取 DOM：loadSessions() 可能已经重建了整个工作台
      const cur = displaysMessageRequest(request) ? document.getElementById('chat-input') : null;
      if (cur) cur.value = '';
      applyBusyUI();
      // 跑完了把过程卡片归位到回复之前（过程在上、结论在下）。
      // 放在这里而不是会话刷新那一步：刷新发生在 busy 仍为 true 时，
      // 那时按"执行中"的规则卡片会落在末尾，之后没人再挪它。
      if (displaysMessageRequest(request)) repositionTraceCard();
      // 与清空输入框同一时机清空文件选择：否则"提示文字清了、选择还留着"
      // 会让下一次发送重复上传同一批文件。
      if (displaysMessageRequest(request)) {
        state.selectedFiles = [];
        const chosen = document.getElementById('selected-file'); if (chosen) chosen.textContent = '';
      }
      // 本次没有真正跑任务（普通问答/能力询问）：立刻收掉进度条和轮询，
      // 不要留一个空转的定时器。
      if (!request.sawActive) {
        stopProgressPolling(request);
        if (displaysMessageRequest(request)) {
          state.execProgress = null;
          const box = document.getElementById('exec-progress');
          if (box) box.innerHTML = '';
          updateTracePanel();
        }
      }
      // SSE 实时流也要收掉：否则它会一直挂着，下一个任务的事件
      // 会和这一次的混在一起（过程卡片会串台）。
      stopTaskStream(request);
      // 回看接口是**权威版本**：收流的动作与后端写完 trace 的那一刻是赛跑的，
      // 最后一条事件可能压根没推出来。这里补一次对齐，保证过程卡片与刷新后一致。
      if (request.taskId) reconcileTrace(request.taskId, request);
      state.activeTaskId = null;
      // 会话刷新可能在 busy=true 时重建标题；轨迹若已对齐，不会再有异步重画。
      updateTracePanel();
      // 进度轮询自己管理完成后的清空（1500ms 让用户看清 100%），这里不再插手
    }
  }
}

function pageWorkbench() {
  return buildWorkbenchShell();
}

// 增量更新工作台：把 buildWorkbenchShell() 渲染到 #page-body 并重新绑定事件。
// loadSessions / loadSession / createSession 都会调它。
function renderWorkbench() {
  const body = document.getElementById('page-body');
  if (!body) return;
  body.innerHTML = buildWorkbenchShell();
  if (typeof bindWorkbenchEvents === 'function') bindWorkbenchEvents();
  scrollChatToBottom();
}

// 渲染后滚到最新消息：默认让最后一条消息进入视野（smooth 过渡）。
// 用户原本在底部 → 等同于滚到底；
// 用户主动翻历史 → 把最后一条滚进视野但不强制拉到底（少打扰）。
function scrollChatToBottom() {
  const el = document.getElementById('chat-messages');
  if (!el) return;
  const last = el.lastElementChild;
  if (!last || !last.scrollIntoView) {
    requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
    return;
  }
  requestAnimationFrame(() => last.scrollIntoView({ behavior: 'smooth', block: 'end' }));
}


/* ============================================================
   结果展示
   ============================================================ */
/* 结果展示：读**真实**任务结果（不再写死示例值）
   真实 task_id 存在聊天消息里（update_message_task 会写 task_id/task_status/report_url），
   所以：拉会话 → 找最后一条带 task_id 的消息 → 解析其中的摘要指标。 */
function parseSummary(content) {
  const out = {};
  const conv = /收敛状态：(已收敛|未收敛)（迭代 (\d+) 次）/.exec(content || '');
  if (conv) { out.converged = conv[1] === '已收敛'; out.iterations = conv[2]; }
  const vmin = /最低电压：([\d.]+)\s*pu（母线\s*([^）]+)）/.exec(content || '');
  if (vmin) { out.vmin = vmin[1]; out.vminBus = vmin[2].trim(); }
  const vmax = /最高电压：([\d.]+)\s*pu（母线\s*([^）]+)）/.exec(content || '');
  if (vmax) { out.vmax = vmax[1]; out.vmaxBus = vmax[2].trim(); }
  const loss = /总网损：([\d.]+)\s*kW(?:，占负荷\s*([\d.]+)%)?/.exec(content || '');
  if (loss) { out.lossKw = loss[1]; out.lossPct = loss[2] || ''; }
  return out;
}

async function findLatestTask() {
  // 一次请求取最近任务（后端已从 messages 表按时间倒序列出），
  // 替代逐会话扫描 —— 会话多时前端扫会很慢且漏旧会话。
  const res = await api('/api/recent-tasks?limit=50');
  const list = (res && res.tasks) || [];
  if (!list.length) return null;
  // 优先展示最近一次成功任务；一次都没成功过才退回最近一次（会显示失败状态）
  const pick = list.find((t) => t.task_status === 'success') || list[0];
  return { ...pick, createdAt: pick.created_at, sessionId: pick.session_id };
}

async function pageResults() {
  let task = null;
  let err = '';
  try {
    task = await findLatestTask();
  } catch (e) {
    err = e.message || String(e);
  }

  if (!task) {
    return `
      <div class="card" style="margin-bottom:16px">
        <div class="card-title">结果展示</div>
        <div style="padding:18px 0;text-align:center;color:var(--text-muted)">
          <div style="font-size:15px;margin-bottom:6px">还没有跑过任务</div>
          <div style="font-size:12.5px">去 <b>分析工作台</b> 说一句「计算 case4」，跑完这里就会显示真实结果。</div>
          ${err ? `<div style="font-size:12px;color:#b42318;margin-top:10px">读取失败：${esc(err)}</div>` : ''}
        </div>
      </div>`;
  }

  const s = parseSummary(task.content || '');
  const status = task.task_status || 'unknown';
  const ok = status === 'success';
  const when = task.createdAt
    ? new Date(task.createdAt).toLocaleString('zh-CN', { hour12: false })
    : '—';
  const concl = [
    ['任务 ID', task.task_id || '—'],
    ['算例', task.task_case_ref || '—'],
    ['状态', ok ? '成功' : status],
    ['收敛', s.converged === undefined ? '—' : (s.converged ? '已收敛' : '未收敛')],
    ['迭代次数', s.iterations || '—'],
    ['完成时间', when],
  ];
  const metrics = [
    ['最低电压', s.vmin ? `${s.vmin} pu（母线 ${s.vminBus}）` : '—'],
    ['最高电压', s.vmax ? `${s.vmax} pu（母线 ${s.vmaxBus}）` : '—'],
    ['总网损', s.lossKw ? `${s.lossKw} kW${s.lossPct ? `，占负荷 ${s.lossPct}%` : ''}` : '—'],
  ];
  return `
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">工程结论（最近一次真实任务）</div>
      <div class="grid grid-3">
        ${concl.map(([a, b]) => `<div class="status-card"><div class="label">${a}</div><div class="value" style="font-size:14px;margin-top:4px">${esc(String(b))}</div></div>`).join('')}
      </div>
      <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${task.report_url ? `<a class="btn sm primary" href="${esc(task.report_url)}" target="_blank">查看完整报告</a>` : ''}
        <span class="tag ${ok ? 'ok' : 'warn'}"><span class="dot"></span>${ok ? '任务成功' : status}</span>
        <span class="foot">数据来源：聊天消息 ${esc(task.sessionId || '')} 的真实任务记录</span>
      </div>
    </div>
    <div class="card">
        <div class="card-title">关键指标</div>
        <table><thead><tr><th>指标</th><th>结果</th></tr></thead><tbody>
          ${metrics.map(([a,b]) => `<tr><td>${a}</td><td><b>${esc(b)}</b></td></tr>`).join('')}
        </tbody></table>
      </div>
    <div class="foot" style="margin-top:10px">完整数值、同引擎独立验证和模型假设见任务报告；本页仅展示上述真实任务记录的摘要。</div>`;
}

/* ============================================================
   路由
   ============================================================ */
const PAGE_RENDER = { overview: pageOverview, model: pageModel, skills: pageSkills, knowledge: pageKnowledge, storage: pageStorage, about: pageAbout };
let currentPage = 'config';
let currentSub = 'overview';
function renderSubnav() {
  const nav = document.getElementById('subnav');
  if (currentPage !== 'config') { nav.classList.add('hidden'); return; }
  nav.classList.remove('hidden');
  nav.innerHTML = CONFIG_SUBNAV.map(item => `<div class="subnav-item ${item.id === currentSub ? 'active' : ''}" data-sub="${item.id}">${item.label}</div>`).join('');
  nav.querySelectorAll('.subnav-item').forEach(el => { el.onclick = () => { currentSub = el.dataset.sub; route(); }; });
}

// ---- 模型配置自动保存 ----
// 字段一改就防抖保存；后端重启推迟到任务空闲时执行（避免杀掉正在跑的任务）。
let _modelAutoSaveTimer = null;
let _modelSaveRevision = 0;
let _modelSaveQueue = Promise.resolve();
let _modelQueuedPatch = null;
let _lastSavedModelPatch = null;
let _pendingBackendRestart = false;
let _modelRestartPromise = null;
let _modelApplyState = 'unchanged';
let _modelStatus = { cls: 'gray', text: '自动保存' };
let _lastModelApplication = { state: 'unchanged', cls: 'gray', text: '配置与已保存值一致' };

function setAutoSaveStatus(cls, text) {
  _modelStatus = { cls, text };
  const el = document.getElementById('model-autosave-status');
  if (!el) return;
  el.className = `tag ${cls}`;
  el.innerHTML = `<span class="dot"></span>${esc(text)}`;
}
function setModelApplicationState(next, cls, text) {
  _modelApplyState = next;
  _lastModelApplication = { state: next, cls, text };
  setAutoSaveStatus(cls, text);
}
function buildModelPatch({ readForm = true } = {}) {
  const mode = state.llmMode;
  const value = id => readForm ? document.getElementById(id)?.value?.trim() : undefined;
  const picked = value('m-model');
  const patch = { llmMode: mode };
  if (mode === 'local') {
    patch.llmOllamaUrl = value('m-ollama-url') || state.llmOllamaUrl || 'http://localhost:11434/v1';
    patch.llmLocalModel = picked || state.llmLocalModel || 'qwen2.5:7b';
    patch.llmBaseUrl = patch.llmOllamaUrl;
    patch.llmModel = patch.llmLocalModel;
  } else {
    const key = value('m-apikey');
    patch.llmApiKey = key === undefined ? state.llmApiKey : key;
    patch.llmCloudBaseUrl = value('m-url') || state.llmCloudBaseUrl || 'https://dashscope.aliyuncs.com/compatible-mode/v1';
    patch.llmCloudModel = picked || state.llmCloudModel || 'qwen-plus';
    patch.llmBaseUrl = patch.llmCloudBaseUrl;
    patch.llmModel = patch.llmCloudModel;
  }
  return patch;
}
function applyModelPatchToState(patch) {
  for (const [key,value] of Object.entries(patch)) if (value !== undefined && value !== null) state[key] = value;
  if (patch.llmApiKey !== undefined && patch.llmApiKey !== null) state.llmApiKeySet = Boolean(patch.llmApiKey);
}
function patchEqualsState(patch) {
  return Boolean(_lastSavedModelPatch && Object.entries(patch).every(([key,value]) => _lastSavedModelPatch[key] === value));
}
function scheduleModelAutoSave() {
  _modelQueuedPatch = buildModelPatch();
  applyModelPatchToState(_modelQueuedPatch);
  _modelSaveRevision += 1;
  _modelApplyState = 'draft';
  setAutoSaveStatus('warn', '有修改，等待保存');
  clearTimeout(_modelAutoSaveTimer);
  _modelAutoSaveTimer = setTimeout(() => autoSaveModelConfig().catch(() => {}), 1000);
}
function bindModelFieldInput() {
  const el = document.getElementById('m-model');
  if (!el || el._modelSaveBound) return;
  el._modelSaveBound = true;
  el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', scheduleModelAutoSave);
}
async function restartSavedModel(revision) {
  if (revision !== _modelSaveRevision) return false;
  if (state.busy || state.execProgress?.active || statusState.tasks.active) {
    _pendingBackendRestart = true;
    setModelApplicationState('pending', 'warn', '已保存，任务结束后生效');
    return false;
  }
  if (_modelRestartPromise) return _modelRestartPromise;
  const pending = (async () => {
    try {
      // 同时检查其他窗口/会话发起的任务，不能仅凭当前输入框不忙就重启。
      const progress = await api('/api/task-progress');
      if (revision !== _modelSaveRevision) return false;
      if (progress?.active || state.busy || state.execProgress?.active) {
        _pendingBackendRestart = true;
        setModelApplicationState('pending', 'warn', '已保存，任务结束后生效');
        return false;
      }
      const result = await t.restartBackend();
      if (revision !== _modelSaveRevision) return false;
      _pendingBackendRestart = false;
      if (result?.reused !== false) {
        setModelApplicationState('external', 'warn', '已保存；外部后端未应用，请自行重启该服务');
        return false;
      }
      setModelApplicationState('applied', 'ok', '已保存并应用到所属后端');
      invalidateReadySnapshot();
      fetchReadySnapshot({ force: true }).catch(() => {});
      if (currentPage === 'config' && currentSub === 'model') refreshModelField(true).catch(() => {});
      return true;
    } catch (error) {
      if (revision === _modelSaveRevision) {
        _pendingBackendRestart = true;
        setModelApplicationState('apply-failed', 'warn', `已保存，应用失败：${error.message}`);
      }
      return false;
    }
  })();
  _modelRestartPromise = pending;
  try { return await pending; } finally { if (_modelRestartPromise === pending) _modelRestartPromise = null; }
}
function autoSaveModelConfig() {
  clearTimeout(_modelAutoSaveTimer);
  const patch = _modelQueuedPatch || (currentPage === 'config' && currentSub === 'model' && document.getElementById('m-model') ? buildModelPatch() : null);
  if (!patch) return _modelSaveQueue;
  _modelQueuedPatch = null;
  const revision = _modelSaveRevision;
  const pending = _modelSaveQueue.catch(() => {}).then(async () => {
    if (patchEqualsState(patch)) {
      if (revision === _modelSaveRevision) {
        _modelApplyState = _lastModelApplication.state;
        setAutoSaveStatus(_lastModelApplication.cls, _lastModelApplication.text);
        if (_pendingBackendRestart) await restartSavedModel(revision);
      }
      return;
    }
    if (revision === _modelSaveRevision) { _modelApplyState = 'saving'; setAutoSaveStatus('warn','保存中…'); }
    try {
      await t.saveSettings(patch);
      _lastSavedModelPatch = { ...(_lastSavedModelPatch || {}), ...patch };
      _pendingBackendRestart = true;
      // 磁盘配置更新后，即使被新草稿代次打断，仍需确认该配置是否实际应用。
      _lastModelApplication = { state: 'pending', cls: 'warn', text: '已保存，等待应用到所属后端' };
      if (revision !== _modelSaveRevision) return;
      applyModelPatchToState(patch);
      _modelCache.fetched = false;
      await restartSavedModel(revision);
    } catch (error) {
      if (revision === _modelSaveRevision) {
        _modelApplyState = 'save-failed';
        setAutoSaveStatus('err', `保存失败：${error.message}`);
        showToast(`保存失败：${error.message}`, 'error');
      }
      throw error;
    }
  });
  _modelSaveQueue = pending;
  return pending;
}
async function applyPendingBackendRestart() {
  if (!_pendingBackendRestart || _modelQueuedPatch || !['pending','apply-failed'].includes(_modelApplyState)) return;
  await restartSavedModel(_modelSaveRevision);
}
async function switchModelMode(next) {
  if (next === state.llmMode) return;
  clearTimeout(_modelAutoSaveTimer);
  const previous = buildModelPatch();
  applyModelPatchToState(previous);
  state.llmMode = next;
  _modelQueuedPatch = { ...previous, ...buildModelPatch({ readForm: false }) };
  applyModelPatchToState(_modelQueuedPatch);
  _modelSaveRevision += 1;
  _modelFetchRevision += 1;
  _modelApplyState = 'draft';
  Object.assign(_modelCache, { fetched: false, models: [], error: '', backend: next });
  setAutoSaveStatus('warn','连接方式已修改，等待保存');
  if (currentPage === 'config' && currentSub === 'model') route();
  await autoSaveModelConfig();
}
async function testModelConnection() {
  await autoSaveModelConfig();
  if (modelConfigurationPending()) throw new Error(_modelStatus.text);
  const mode = state.llmMode;
  const revision = _modelSaveRevision;
  const backendGeneration = _overviewCache.generation;
  const data = await fetchModels(true,mode);
  if (mode !== state.llmMode || revision !== _modelSaveRevision || backendGeneration !== _overviewCache.generation) throw new Error('配置或后端已变化，请重新检查连接');
  if (data.error) throw new Error(data.error);
  return `${mode === 'local' ? '本地' : '云端'}模型列表可达（${data.models.length}项）；未执行推理测试`;
}

function bindConfigActions() {
  if (currentSub === 'model') {
    refreshModelField().catch(() => {});
    setAutoSaveStatus(_modelStatus.cls, _modelStatus.text);
  }
  document.querySelectorAll('[data-config-action="model-test"]').forEach(el => el.addEventListener('click', async () => {
    el.disabled = true;
    el.textContent = '检查中…';
    const status = document.getElementById('conn-status');
    try {
      const message = await testModelConnection();
      if (status) { status.className = 'tag info'; status.textContent = message; }
      showToast(message);
    } catch (error) {
      if (status) { status.className = 'tag warn'; status.textContent = error.message; }
      showToast(`连接检查未通过：${error.message}`, 'error');
    } finally { el.disabled = false; el.textContent = '检查连接'; }
  }));
  document.querySelectorAll('[data-config-action="models-refresh"]').forEach(el => el.addEventListener('click', async () => {
    el.disabled = true;
    try { await autoSaveModelConfig(); if (modelConfigurationPending()) throw new Error(_modelStatus.text); await refreshModelField(true); showToast(_modelCache.error || '模型列表已刷新', _modelCache.error ? 'error' : 'info'); }
    catch (error) { showToast(`刷新失败：${error.message}`, 'error'); }
    finally { el.disabled = false; }
  }));


  // ---- 知识库：重建本地 RAG 索引（刷新标志 = ⟳ 旋转动画）----
  // 新增/修改 knowledge/ 下的文档后点它，索引才会更新。
  document.querySelectorAll('[data-config-action="kb-refresh"]').forEach((el) => el.addEventListener('click', async () => {
    const pageRevision = routeToken;
    el.disabled = true;
    el.classList.add('is-refreshing');
    const label = el.querySelector('.btn-label');
    if (label) label.textContent = '重建中…';
    try {
      const r = await api('/api/rag/ingest?force=true', 'POST');
      showToast(`索引已重建：${r.num_sources} 份文档 / ${r.num_chunks} 个片段`);
      if (pageRevision === routeToken && currentPage === 'config' && currentSub === 'knowledge') route();
    } catch (e) {
      showToast(`重建失败：${e.message}`, 'error');
    } finally {
      el.disabled = false;
      el.classList.remove('is-refreshing');
      if (label) label.textContent = '重建索引';
    }
  }));

  // ---- 存储管理：盘点 / 归档 ----
  // 清理一律走"归档到 _archive/<时间戳>/"，不直接删除；需先勾选条目再执行。
  const _stgSelected = () => Array.from(document.querySelectorAll('.stg-item:checked')).map((el) => ({
    kind: el.getAttribute('data-kind'),
    path: el.value,
  }));

  const _stgUpdateSummary = () => {
    const sel = _stgSelected();
    const box = document.getElementById('stg-summary');
    if (!box) return;
    box.textContent = sel.length ? `已选中 ${sel.length} 项${sel.length > 10 ? '（超过单批上限 10，将只处理前 10 项）' : ''}` : '未选中任何条目';
  };

  document.querySelectorAll('.stg-item').forEach((el) => el.addEventListener('change', _stgUpdateSummary));
  _stgUpdateSummary();

  document.querySelectorAll('[data-config-action="storage-rescan"]').forEach((el) => el.addEventListener('click', async () => {
    const pageRevision = routeToken;
    el.disabled = true; el.textContent = '盘点中…';
    try {
      _storageCache.data = await api('/api/storage');
      showToast('盘点完成');
      if (pageRevision === routeToken && currentPage === 'config' && currentSub === 'storage') route();
    } catch (e) {
      showToast(`盘点失败：${e.message}`, 'error');
      el.disabled = false; el.textContent = '重新盘点';
    }
  }));

  document.querySelectorAll('[data-config-action="storage-archive"]').forEach((el) => el.addEventListener('click', async () => {
    const pageRevision = routeToken;
    const items = _stgSelected();
    if (!items.length) { showToast('请先勾选要归档的条目', 'error'); return; }
    const total = items.reduce((s, it) => {
      const g = (_storageCache.data && _storageCache.data.groups[it.kind]) || [];
      const hit = g.find((x) => x.path === it.path);
      return s + (hit ? hit.size_bytes : 0);
    }, 0);
    if (!window.confirm(`将归档 ${items.length} 项（约 ${fmtBytes(total)}）到 _archive/。\n\n这是移动操作，不删除，归档后可手动移回。确认继续？`)) return;
    el.disabled = true; el.textContent = '归档中…';
    try {
      const r = await api('/api/storage/archive', 'POST', { items, confirm: true });
      showToast(r.message || `已归档 ${r.moved.length} 项`);
      if (r.failed && r.failed.length) {
        showToast(`有 ${r.failed.length} 项未处理：${r.failed[0].error}`, 'error');
      }
      _storageCache.data = null;
      _storageCache.data = await api('/api/storage');
      if (pageRevision === routeToken && currentPage === 'config' && currentSub === 'storage') route();
    } catch (e) {
      showToast(`归档失败：${e.message}`, 'error');
      el.disabled = false; el.textContent = '归档选中项';
    }
  }));

  // ---- API Key 明文显示开关：默认密文，点 👁 才展开 ----
  document.querySelectorAll('[data-config-action="apikey-toggle"]').forEach((el) => el.addEventListener('click', () => {
    _apikeyRevealed = !_apikeyRevealed;
    const input = document.getElementById('m-apikey');
    if (input) input.type = _apikeyRevealed ? 'text' : 'password';
    el.textContent = _apikeyRevealed ? '🙈 隐藏' : '👁 显示';
    el.title = _apikeyRevealed ? '隐藏明文（防止截图泄露）' : '显示明文（用于核对）';
  }));

  document.querySelectorAll('input[name="llm-mode"]').forEach(el => el.addEventListener('change', () => {
    switchModelMode(el.value === 'local' ? 'local' : 'cloud').catch(error => showToast(`连接方式保存失败：${error.message}`, 'error'));
  }));
  ['m-apikey','m-url','m-ollama-url'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input',scheduleModelAutoSave);
  });
  bindModelFieldInput();
  document.querySelectorAll('[data-config-action="health"]').forEach(el => el.addEventListener('click',async () => {
    el.disabled = true; el.textContent = '检测中…';
    try { await fetchReadySnapshot({ force: true }); showToast('环境状态已刷新；检测范围见各项说明'); }
    catch(error) { showToast(`环境检测失败：${error.message}`, 'error'); }
    finally { el.disabled = false; el.textContent = '刷新环境状态'; }
  }));
  document.querySelectorAll('[data-config-action="open-logs"]').forEach(el => el.addEventListener('click',async () => {
    el.disabled = true;
    try {
      const result = await t.openLogsDirectory();
      if (!result?.ok) throw new Error(result?.error || '日志目录未能打开');
      showToast('已打开应用日志目录；原始日志未经脱敏');
    } catch(error) { showToast(`打开日志目录失败：${error.message}`, 'error'); }
    finally { el.disabled = false; }
  }));
}


/* ------------------------------------------------------------
   异步页面的加载占位 / 失败兜底
   ------------------------------------------------------------
   背景（2026-10-02 真机点击发现）：
   · 「结果展示」要等 8 秒、「知识与经验」要等 10 秒才渲染（首次初始化 RAG）。
     这段时间界面**毫无反应**，用户会以为按钮坏了。
   · 原来这两个分支只有 `.then` 没有 `.catch`，一旦渲染函数 reject，
     异常被静默吞掉、界面停在上一页、面包屑也不变 —— 没有任何提示。
   这里把「立刻给反馈」和「失败要说出来」都补上。
   另：异步是「后发起后到达」可能乱序（连点两个子页时，先发的慢请求会盖掉
   后发的快页面），所以用 routeToken 作废过期回调。
   ------------------------------------------------------------ */
let routeToken = 0;

function routeLoadingHtml(text) {
  return `<div class="card"><div class="route-loading">
      <div class="spin"></div>
      <div class="msg">${esc(text || '正在加载…')}</div>
      <div class="hint">首次打开该页需要初始化检索组件，可能要几秒；之后会快很多。</div>
    </div></div>`;
}

function routeErrorHtml(err) {
  const msg = esc((err && (err.message || String(err))) || '未知错误');
  return `<div class="card">
      <div class="card-title">这个页面没能加载出来</div>
      <div style="padding:12px 0;font-size:13px;color:var(--text-muted)">
        <div style="margin-bottom:8px">出错信息：<code>${msg}</code></div>
        <div style="font-size:12.5px">其它页面不受影响；也可以到「关于与诊断」查看具体情况。</div>
      </div>
      <button class="btn sm primary" data-route-retry="1">重试</button>
    </div>`;
}

function bindRouteRetry() {
  document.querySelectorAll('[data-route-retry]').forEach((el) =>
    el.addEventListener('click', () => route()));
}

/** 异步页统一入口：先落面包屑与占位，再等数据；失败把错误显示出来。 */
function renderAsyncPage(body, label, loader, onDone) {
  const my = ++routeToken;
  setCrumb(currentPage, label);
  body.innerHTML = routeLoadingHtml(label ? `正在加载「${label}」…` : '正在加载…');
  renderSubnav();
  Promise.resolve()
    .then(loader)
    .then((html) => {
      if (my !== routeToken) return; // 已被更新的路由取代，丢弃
      body.innerHTML = typeof html === 'string' ? html : '';
      onDone();
    })
    .catch((e) => {
      if (my !== routeToken) return;
      body.innerHTML = routeErrorHtml(e);
      bindRouteRetry();
    });
}

function route() {
  // 一级导航高亮
  document.querySelectorAll('#nav .nav-item').forEach((el) => {
    el.classList.toggle('active', el.dataset.page === currentPage);
  });
  const body = document.getElementById('page-body');
  if (currentPage === 'config') {
    if (!PAGE_RENDER[currentSub]) currentSub = 'overview';
    const fn = PAGE_RENDER[currentSub];
    const label = CONFIG_SUBNAV.find((i) => i.id === currentSub)?.label || '总览与环境检测';
    renderAsyncPage(body, label, fn, () => {
      bindConfigActions();
      renderSubnav();
    });
  } else if (currentPage === 'workbench') {
    routeToken += 1; // 切到同步页，让在途的异步渲染作废
    body.innerHTML = pageWorkbench();
    bindWorkbenchEvents();
    setCrumb('workbench');
    renderSubnav();
  } else if (currentPage === 'results') {
    // pageResults 会读真实任务数据（异步），需按 Promise 处理
    renderAsyncPage(body, '', pageResults, () => renderSubnav());
  }
  // config 异步分支已自行 renderSubnav；其他分支仍由 route 兜底
  if (currentPage !== 'config') renderSubnav();
}

document.querySelectorAll('#nav .nav-item').forEach((el) => {
  el.onclick = () => {
    currentPage = el.dataset.page;
    if (currentPage === 'config') currentSub = currentSub || 'overview';
    route();
    if (currentPage === 'workbench') loadSessions().catch((e) => showToast(`会话加载失败：${e.message}`, 'error'));
  };
});

function setBootMessage(message, isError = false) {
  const el = document.getElementById('boot-msg');
  if (!el) return;
  el.textContent = message;
  el.className = isError ? 'boot-msg boot-error' : 'boot-msg';
}

function hideBootOverlay() {
  const overlay = document.getElementById('boot-overlay');
  if (overlay) overlay.classList.add('done');
}

// 状态栏不是装饰：启动后立即检查，并持续同步任务、引擎和通知状态。
bindStatusBarActions();
startStatusBarPolling();
route();
(async function bootstrap() {
  const settings = await t.getSettings().catch(() => ({ apiBase: 'http://127.0.0.1:8000' }));
  state.apiBase = settings.apiBase || state.apiBase;
  invalidateReadySnapshot();
  // 主进程回传明文（本机自用场景，用户要求在配置页可见）
  state.llmApiKeySet = Boolean(settings.llmApiKeySet);
  state.llmApiKey = settings.llmApiKey || '';
  state.llmBaseUrl = settings.llmBaseUrl || '';
  state.llmCloudBaseUrl = settings.llmCloudBaseUrl || '';
  state.llmCloudModel = settings.llmCloudModel || '';
  state.llmLocalModel = settings.llmLocalModel || '';
  state.llmModel = settings.llmModel || '';
  state.llmMode = settings.llmMode === 'local' ? 'local' : 'cloud';
  state.llmOllamaUrl = settings.llmOllamaUrl || state.llmOllamaUrl;
  _lastSavedModelPatch = { ...settings, llmMode: state.llmMode };
  if (currentPage === 'config' && currentSub === 'model') route();
  fetchReadySnapshot({ force: true }).catch(() => {});
  try {
    await fetchReadySnapshot();
    hideBootOverlay();
    if (currentPage === 'workbench') await loadSessions();
  } catch (e) {
    // 后端还没起来是首次启动的正常中间状态，不弹错误遮罩，等 backend:ready 再补加载
    hideBootOverlay();
  }
})();

// 后端就绪后补加载，避免首次启动时会话列表为空
t.onBackendReady?.(async (info) => {
  invalidateReadySnapshot();
  statusRefreshInFlight = false;
  setBootMessage('后端已就绪，正在加载数据…');
  if (info?.restarted) showToast('后端已自动重启，之前的任务已中断，请重新发送');
  try {
    await fetchReadySnapshot({ force: true });
    await refreshStatusBar();
    if (currentPage === 'workbench') await loadSessions();
    hideBootOverlay();
    showToast('天枢一号已就绪');
  } catch (e) {
    showToast(`数据加载失败：${e.message}`, 'error');
    hideBootOverlay();
  }
});

t.onBackendProgress?.((info) => {
  if (info && info.message) setBootMessage(info.message);
});

t.onBackendError?.((message) => {
  invalidateReadySnapshot();
  applyReadySnapshot(null, String(message));
  setBootMessage(`后端启动失败：${message}`, true);
  showToast(`后端启动失败：${message}`, 'error');
  // 后端崩了（例如 dss 原生库段错误）：在途请求永远不会返回，
  // 必须主动解锁并停掉轮询，否则界面会一直停在"执行中…"。
  if (state.busy || _activeMessageRequest) {
    const request = _activeMessageRequest;
    retireTask(request && request.taskId);
    state.busy = false;
    clearBusyWatchdog(request);
    stopProgressPolling(request);
    stopTaskStream(request);
    _activeMessageRequest = null;
    _stoppedMessageRequest = null;
    _messageGeneration += 1;
    _traceViewGeneration += 1;
    state.execProgress = null;
    const box = document.getElementById('exec-progress');
    if (box) box.innerHTML = '';
    applyBusyUI();
    updateTracePanel();
  }
});
