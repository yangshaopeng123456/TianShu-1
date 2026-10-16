// ============================================================
// 天枢一号 UI 逻辑：导航路由 + 三页内容渲染
// 所有内容基于天枢一号真实能力（OpenDSS 引擎、qwen2.5:7b、8 个工具）
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
  try {
    const data = await t.apiRequest(method, path, body);
    state.apiOnline = true;
    updateBackendStatus(true);
    return data;
  } catch (err) {
    state.apiOnline = false;
    updateBackendStatus(false, err.message);
    throw err;
  }
}

function updateBackendStatus(online, detail = '') {
  renderStatusItem('sb-sidecar', online ? 'ok' : 'err', online ? 'Sidecar 正常' : 'Sidecar 未连接', detail || 'FastAPI 后端健康检查');
}

const STATUS_LABELS = {
  model: ['模型正常', '模型未配置'],
  engine: ['引擎就绪', '引擎不可用'],
  sidecar: ['Sidecar 正常', 'Sidecar 未连接'],
  license: ['许可证有效', '许可证异常'],
};
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
  if (statusRefreshInFlight) return;
  statusRefreshInFlight = true;
  const taskStatusRevision = _taskStatusRevision;
  try {
    // 模型配置改过但当时有任务在跑 → 现在空闲了，补做后端重启
    applyPendingBackendRestart().catch(() => {});
    const results = await Promise.allSettled([
      api('/api/ready'),
      api('/api/task-progress'),
      api('/api/recent-tasks?limit=20'),
    ]);
    const ready = results[0].status === 'fulfilled' ? results[0].value : null;
    const progress = results[1].status === 'fulfilled' ? results[1].value : null;
    const recent = results[2].status === 'fulfilled' ? results[2].value : null;
    const checks = ready?.checks || {};
    for (const key of ['model', 'engine', 'sidecar', 'license']) {
      const c = checks[key] || {};
      const level = c.level || (c.ok ? 'ok' : 'warn');
      const label = c.ok ? STATUS_LABELS[key][0] : STATUS_LABELS[key][1];
      statusState[key] = { level, label, detail: c.detail || '' };
      renderStatusItem(`sb-${key}`, level, label, c.detail || '');
    }
    if (results[0].status === 'rejected' && results[1].status === 'rejected') {
      renderStatusItem('sb-sidecar', 'err', 'Sidecar 未连接', results[0].reason?.message || '无法访问后端');
    }
    applyRefreshedTaskStatus(progress, recent?.tasks || [], taskStatusRevision);
    buildNotifications(recent?.tasks || []);
  } finally {
    statusRefreshInFlight = false;
  }
}
function startStatusBarPolling() {
  if (statusRefreshTimer && typeof clearInterval === 'function') clearInterval(statusRefreshTimer);
  refreshStatusBar().catch(() => {});
  if (typeof setInterval === 'function') {
    statusRefreshTimer = setInterval(() => refreshStatusBar().catch(() => {}), 5000);
  }
}

function bindStatusBarActions() {
  const targets = { model: 'model', engine: 'engine', sidecar: 'sidecar' };
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
   首次打开 = 冷启动，加载引擎（20~40s，不可避免）
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
    id: 'health',
    label: '后端状态',
    always: true,                       // 任何页面都查（毫秒级）
    fetch: () => api('/api/health'),
    apply: () => { /* api() 内部已自动 updateBackendStatus */ },
  },
  {
    id: 'ready',
    label: '就绪度',
    visible: () => !!document.getElementById('ready-cards'),
    fetch: () => api('/api/ready'),
    apply: (d) => {
      _overviewCache.data = d;          // 让后续渲染直接用新数据
      route();                          // 就绪度含 detail 文本，整体重排最稳
    },
  },
  {
    id: 'models',
    label: '模型列表',
    visible: () => !!document.getElementById('model-field-slot'),
    fetch: () => api('/api/models'),
    apply: () => refreshModelField(true),
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
  { id: 'overview', label: '总览与环境检测', group: null },
  { id: null, label: '运行环境', group: true },
  { id: 'model', label: '模型服务', group: '运行环境' },
  { id: 'engine', label: '计算引擎中心', group: '运行环境' },
  { id: 'sidecar', label: 'Sidecar 与环境', group: '运行环境' },
  { id: null, label: '智能体', group: true },
  { id: 'budget', label: '执行与预算', group: '智能体' },
  { id: 'policy', label: '安全策略与默认任务合同', group: '智能体' },
  { id: 'skills', label: 'Skill 能力', group: '智能体' },
  { id: 'knowledge', label: '知识与经验', group: '智能体' },
  { id: null, label: '数据', group: true },
  { id: 'storage', label: '样本与本地存储', group: '数据' },
  { id: 'report', label: '报告与导出', group: '数据' },
  { id: 'logs', label: '日志与诊断', group: '数据' },
  { id: null, label: '软件', group: true },
  { id: 'appearance', label: '通知与外观', group: '软件' },
  { id: 'security', label: '安全与凭据', group: '软件' },
  { id: 'about', label: '版本与维护', group: '软件' },
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
function configToolbar(hasSave = true) {
  return `
    <div class="card" style="margin-bottom:16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <input type="text" placeholder="搜索设置…" style="width:220px;flex-shrink:0;" />
      <button class="btn sm" data-config-action="health">一键环境检测</button>
      <button class="btn sm" data-config-action="import">导入配置</button>
      <button class="btn sm" data-config-action="export">导出脱敏配置</button>
      <button class="btn sm" data-config-action="reset">恢复默认</button>
      <span style="margin-left:auto" class="tag gray">普通用户</span>
    </div>`;
}

/* ============================================================
   页面渲染函数
   ============================================================ */

/* 1. 总览与环境检测 */
// 与 bootstrap 中的占位区分开后：让渲染层拿到真实就绪度
const _overviewCache = { data: null };

async function loadReady() {
  if (_overviewCache.data) return _overviewCache.data;
  const data = await api('/api/ready');
  _overviewCache.data = data;
  return data;
}

function backendEndpointLabel() {
  try { return new URL(state.apiBase).origin; }
  catch { return '当前后端'; }
}

async function pageOverview() {
  const checks = await loadReady().catch((e) => ({ checks: {}, error: e.message }));
  const order = [
    ['model', '模型服务'], ['engine', '计算引擎'], ['sidecar', 'Sidecar'],
    ['license', '许可证'], ['storage', '本地存储'], ['skills', 'Skill 与知识库'],
  ];
  const statusCards = order.map(([k, label]) => {
    const c = (checks.checks || {})[k];
    const level = (c && c.level) || 'gray';
    const detail = k === 'sidecar' && c?.ok
      ? `${esc(backendEndpointLabel())} 健康检查通过`
      : (c && c.detail) || (checks.error ? `检测失败：${checks.error}` : '尚未验证');
    return `
    <div class="status-card">
      <div class="label">${label}</div>
      <div style="margin:6px 0">${statusTag(level)}</div>
      <div class="foot">${detail}</div>
    </div>`;
  }).join('');

  const rows = [
    ['模型服务', (checks.checks?.model?.ok ? '已配置' : '待配置'), checks.checks?.model?.ok ? 'ok' : 'warn'],
    ['内置生产引擎', checks.checks?.engine?.ok ? '1 / 1 就绪（OpenDSS）' : '引擎异常', checks.checks?.engine?.ok ? 'ok' : 'err'],
    ['候选引擎', '多引擎接入预留', 'gray'],
    ['Policy 默认模板', '已配置', 'info'],
    ['Skill 注册表', checks.checks?.skills?.ok ? '正常' : '异常', checks.checks?.skills?.ok ? 'ok' : 'warn'],
    ['知识索引', '有 1 项待更新', 'warn'],
    ['存储与备份', checks.checks?.storage?.ok ? '可写' : '存储异常', checks.checks?.storage?.ok ? 'ok' : 'err'],
  ];
  const completeness = rows.map(([a, b, s]) => `
    <tr><td>${a}</td><td>${b}</td><td>${statusTag(s)}</td></tr>`).join('');

  const checkItems = [
    ['凭据配置文件（未加密）', 'warn'],
    ['模型服务可达性与结构化输出', checks.checks?.model?.ok ? 'ok' : 'warn'],
    ['Sidecar 健康', checks.checks?.sidecar?.ok ? 'ok' : 'warn'],
    ['引擎可执行文件与版本', checks.checks?.engine?.ok ? 'ok' : 'warn'],
    ['许可证与检查时效', checks.checks?.license?.ok ? 'ok' : 'warn'],
    ['工作目录读写', checks.checks?.storage?.ok ? 'ok' : 'warn'],
    ['会话数据库与 Checkpoint', 'ok'],
    ['Skill Manifest / Executor / Validator', checks.checks?.skills?.ok ? 'ok' : 'warn'],
    ['知识索引', 'warn'],
    ['报告与 Artifact 目录', checks.checks?.storage?.ok ? 'ok' : 'warn'],
  ];
  const envChecks = checkItems.map(([n, s]) => `
    <tr><td>${n}</td><td>${statusTag(s)}</td><td>${s === 'warn' ? '建议检查或更新' : '通过'}</td></tr>`).join('');

  return `
    ${configToolbar()}
    <div class="section-title">就绪度</div>
    <div class="grid grid-3" id="ready-cards">${statusCards}</div>
    <div class="section-title">配置完成度</div>
    <div class="card"><table><thead><tr><th>模块</th><th>状态</th><th>标签</th></tr></thead>
    <tbody>${completeness}</tbody></table></div>
    <div class="section-title">一键环境检测</div>
    <div class="card"><table><thead><tr><th>检查项</th><th>结果</th><th>建议操作</th></tr></thead>
    <tbody>${envChecks}</tbody></table></div>`;
}

// 可用模型列表（云端走 /api/models，本地走 /api/local-models）
const _modelCache = { fetched: false, models: [], current: '', error: '', backend: '' };

async function fetchModels(force = false, modeOverride) {
  const mode = modeOverride || state.llmMode || 'cloud';
  if (_modelCache.fetched && !force && _modelCache.backend === mode) return _modelCache;
  _modelCache.backend = mode;
  try {
    let url, d;
    if (mode === 'local') {
      const ollamaUrl = state.llmOllamaUrl || 'http://localhost:11434/v1';
      url = `/api/local-models?url=${encodeURIComponent(ollamaUrl)}`;
    } else {
      url = '/api/models';
    }
    d = await api(url);
    _modelCache.models = (d && d.models) || [];
    _modelCache.current = (d && d.current) || '';
    _modelCache.error = d && d.ok ? '' : ((d && d.error) || '拉取失败');
  } catch (e) {
    _modelCache.error = e.message;
    _modelCache.models = [];
  }
  _modelCache.fetched = true;
  return _modelCache;
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
    const errShort = m.error ? '远端不可达' : '未拉到模型';
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
  if (!slot) return;                                   // 不在模型页
  if (_modelCache.fetched && _modelCache.models.length && !force) {
    slot.innerHTML = renderModelField(_modelCache);     // 已有缓存，直接填
    return;
  }
  const typed = document.getElementById('m-model');
  const preferred = typed && typed.value ? typed.value : '';
  const m = await fetchModels(force);
  const slot2 = document.getElementById('model-field-slot');
  if (!slot2) return;                                  // 期间用户切走了
  slot2.innerHTML = renderModelField(m, preferred, state.llmMode || 'cloud');
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
        切换模式后请重新 <b>保存并重启后端</b>，环境变量才会更新。
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
          <div class="foot">填错或泄露时，先在 <b>云控制台禁用 Key</b> 再点 <b>保存并重启后端</b>。Key 与云端地址随云端界面单独保存，切换到本地不会丢失。</div>
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
      <button class="btn primary" data-config-action="model-test">测试连接</button>
      <span id="conn-status" class="tag gray"><span class="dot"></span>未测试（点按钮探测）</span>
    </div>`;

  return `
    ${configToolbar()}
    <div class="card">
      <div class="card-title">基础配置</div>
      ${modeRow}
      <div class="grid grid-2">
        ${field('Provider 类型', `<input type="text" value="${mode === 'local' ? '本地 Ollama（OpenAI 兼容）' : 'OpenAI 兼容（DashScope / b.ai / DeepSeek 等）'}" />`)}
        ${modeSpecific}
        ${modelRow}
      </div>
      ${autoSaveHint}
      ${testRow}
    </div>
    <div class="section-title">能力检测（只读）</div>
    <div class="card"><div class="grid grid-4">
      ${[['JSON Object', 'ok'], ['JSON Schema', 'ok'], ['Tool Call / tool_choice', 'ok'],
         ['流式输出', 'ok'], ['Usage 返回', 'ok'], ['响应耗时', 'ok'],
         ['最近错误分类', 'ok'], ['辅助接入新引擎', 'ok']].map(([n, s]) => `
        <div class="status-card"><div class="label">${n}</div><div style="margin-top:6px">${statusTag(s)}</div></div>`).join('')}
    </div></div>
    <div class="section-title">高级设置（专家模式）</div>
    <div class="card"><div class="grid grid-2">
      ${field('请求超时（秒）', textInput('adv-1', '120'))}
      ${field('最大重试', textInput('adv-2', '3'))}
      ${field('Temperature', textInput('adv-3', '0.3'))}
      ${field('单请求最大输出 Token', textInput('adv-4', '4096'))}
      ${field('上下文窗口声明', textInput('adv-5', '8192'))}
      ${field('上下文压缩阈值', textInput('adv-6', '0.85'))}
    </div></div>
    <div class="section-title">产品边界</div>
    <div class="card" style="color:var(--text-muted);font-size:12.5px;line-height:1.7">
      本软件连接 OpenAI 兼容模型服务，不负责下载、安装、启动或监督本地大模型运行时。
      用户可填写本地服务地址，但本软件不管理其进程、模型目录和显卡。</div>`;
}

/* 3. 计算引擎中心 */
function pageEngine() {
  const summary = [
    ['已注册引擎', '1'], ['内置生产引擎', '1'], ['当前可用', '1'],
    ['待验证', '0'], ['许可证阻断', '0'], ['运行异常', '0'],
  ];
  const summaryCards = summary.map(([a, b]) => `
    <div class="status-card"><div class="label">${a}</div><div class="value">${b}</div></div>`).join('');

  const rows = `
    <tr><td><b>OpenDSS</b></td><td>Python (dss-python)</td><td>0.15+</td>
    <td>进程内库</td><td>${statusTag('ok')}</td><td>8</td><td>${statusTag('ok')}</td><td>—</td><td>0</td><td>是</td><td><button class="btn sm">详情</button></td></tr>`;

  return `
    ${configToolbar()}
    <div class="card" style="margin-bottom:16px;font-size:13px;color:var(--text-muted)">
      当前版本：<b style="color:var(--text)">1 个内置生产引擎，多引擎接入架构预留</b>
    </div>
    <div class="grid grid-3" style="grid-template-columns:repeat(6,1fr);margin-bottom:16px">${summaryCards}</div>
    <div class="section-title">默认引擎策略</div>
    <div class="card"><div class="grid grid-2">
      ${field('默认引擎选择', `<select><option>自动选择</option><option>固定 OpenDSS</option></select>`)}
      ${field('选择偏好', `<select><option>最小充分能力</option><option>优先现代引擎</option></select>`)}
    </div><div style="font-size:12px;color:var(--text-muted)">修改默认值不影响已有会话。</div></div>
    <div class="section-title">引擎注册表</div>
    <div class="card"><table><thead><tr>
      <th>引擎名称</th><th>类型</th><th>版本</th><th>接入方式</th><th>生产状态</th><th>已验证能力</th><th>授权状态</th><th>授权检查</th><th>当前任务</th><th>允许承担 V2</th><th>操作</th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="section-title">引擎详情</div>
    <div class="card">
      <div class="tag" style="margin-bottom:12px">概况｜能力｜运行环境｜文档知识｜关联 Skill｜验证记录｜版本与影响</div>
      <div class="grid grid-2" style="font-size:12.5px;color:var(--text-muted)">
        <div>Engine Profile：dss-python 0.15，AC 稳态单场景</div>
        <div>Capability Registry：8 项已验证能力</div>
        <div>CLI / Core 版本：OpenDSS 内核（Hash 摘要显示）</div>
        <div>适用 Case：DSS / Excel / CSV / JSON / MATPOWER</div>
        <div>允许物理角色：主执行、辅助、V2、跨引擎复核</div>
        <div>并发与 Lease：串行，单一 Circuit</div>
      </div>
    </div>
    <div class="section-title">引擎 × 能力矩阵</div>
    <div class="card"><table><thead><tr><th>能力</th><th>OpenDSS</th><th>候选引擎（预留）</th></tr></thead><tbody>
      ${['数据与格式校验','初始潮流验真','拓扑/孤岛/参考节点','电压—无功','有功—相角','Q-V / P-Q / P-V 分析','DC 有功可行性','候选生成与组合','Policy Guard','独立 V2','报告生成'].map((c, i) => `
      <tr><td>${c}</td><td>${i < 7 ? statusTag('ok') : statusTag('gray')}</td><td>${statusTag('gray')}</td></tr>`).join('')}
    </tbody></table></div>`;
}

/* 4. Sidecar 与环境 */
function pageSidecar() {
  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">Sidecar 运行状态 ${statusTag('ok')}</div>
      <div class="grid grid-2">
        ${field('Sidecar 版本', `<input type="text" value="1.6.0" disabled />`)}
        ${field('启动时间', `<input type="text" value="2026-09-05 10:00:00" disabled />`)}
        ${field('当前健康状态', `<span class="tag ok"><span class="dot"></span>健康</span>`)}
        ${field('活跃任务', `<input type="text" value="0" disabled />`)}
      </div>
      <div style="display:flex;gap:10px"><button class="btn">重启 Sidecar</button><button class="btn">导出诊断摘要</button></div>
    </div>
    <div class="section-title">内部环境（管理员技术审计，脱敏显示）</div>
    <div class="card" style="color:var(--text-muted);font-size:12.5px">
      动态端口、Session Token、启动命令、内部环境变量——仅管理员可查看。</div>`;
}

/* 5. 执行与预算 */
function pageBudget() {
  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">普通模式预设</div>
      <div class="grid grid-2">
        ${field('分析深度', `<select><option>标准</option><option>深入</option></select>`)}
        ${field('计算预算', `<select><option>标准</option><option>扩展</option></select>`)}
        ${field('默认最大恢复轮次', textInput('b-1', '2'))}
        ${field('默认最大求解器调用数', textInput('b-2', '8'))}
        ${field('默认最大并行候选数', textInput('b-3', '1'))}
        ${field('后台最大会话数', textInput('b-4', '4'))}
        ${field('工具调用超时（秒）', textInput('b-5', '60'))}
        ${field('工程故障自动重试次数', textInput('b-6', '2'))}
      </div>
      <div style="font-size:12px;color:var(--text-muted)">这些只是新会话默认值，启动任务前仍在任务合同中确认。</div>
    </div>
    <div class="section-title">高级与管理员模式（只读）</div>
    <div class="card"><div class="grid grid-2">
      ${['完整诊断最大调用次数','Skill 选择模型调用次数','假设重新生成次数','重复证据调用阻断','Evidence Token 预算','自适应预算状态','Checkpoint 存储健康','并行候选调度状态'].map((n) => `
        <div class="status-card"><div class="label">${n}</div><div class="foot" style="margin-top:8px">${statusTag('gray')} 管理员只读</div></div>`).join('')}
    </div></div>`;
}

/* 6. 安全策略与默认任务合同 */
function pagePolicy() {
  const hard = ['保持拓扑','保持负荷','保持发电计划','保持交换功率','禁止切负荷','禁止改变母线类型','最大设备调整数','最大求解器调用数','最大并行候选数','需要审批的风险等级'];
  const soft = ['最少设备变化','最小总调整幅度','优先可逆动作','优先原控制模式','优先区域','优先动作类型'];
  return `
    ${configToolbar()}
    <div class="card" style="margin-bottom:16px;font-size:12.5px;color:var(--text-muted)">
      本页配置「新任务默认模板」，不直接修改运行中的会话。每次任务仍在工作台生成、展示和确认实际 Task Policy。</div>
    <div class="card"><div class="card-title">默认任务模板</div>
      <div class="grid grid-2">
        ${field('默认 Task Profile', `<select><option>标准潮流分析</option><option>深度诊断</option></select>`)}
        ${field('默认任务模式', `<select><option>只读分析</option><option>方案建议</option></select>`)}
        ${field('默认最小验真等级', `<select><option>标准</option><option>严格</option></select>`)}
        ${field('越限处理', `<select><option>仅检测</option></select>`)}
      </div></div>
    <div class="section-title">用户硬约束模板</div>
    <div class="card"><div class="grid grid-2">
      ${hard.map((n) => `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">
        <span>${n}</span><span style="display:flex;gap:8px;align-items:center">${switchInput('h-' + n, n.startsWith('保持'))}${switchInput('h2-' + n, false)}</span></div>`).join('')}
    </div></div>
    <div class="section-title">用户软偏好</div>
    <div class="card"><div class="grid grid-2">
      ${soft.map((n) => `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span>${n}</span>${switchInput('s-' + n, false)}</div>`).join('')}
    </div></div>
    <div class="section-title">验真策略（只读）</div>
    <div class="card"><div class="grid grid-2">
      ${[['最低验真等级','标准'],['是否必须 V2','否'],['当前容差版本','1e-4'],['当前工程标准','复现性 + 功率平衡']].map(([a,b]) => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span>${a}</span><b>${b}</b></div>`).join('')}
    </div><div style="font-size:12px;color:var(--text-muted);margin-top:8px">物理硬边界、Engine Safety 和 V2 证据门不提供关闭开关。</div></div>`;
}

/* 7. Skill 能力 */
function pageSkills() {
  const groups = ['数据与格式校验','初始潮流验真','拓扑/孤岛/参考节点','初始化与数值路径','电压—无功','有功—相角','候选生成与组合','Policy Guard','Revision 与回滚','独立 V2','因果复核','报告生成'];
  const tools = [
    ['load_case', '算例装载', 'load_case', 'ok'],
    ['validate_case', '编译预检与静态检查', 'validate_case', 'ok'],
    ['profile_network', '网络画像提取', 'build_network_profile', 'ok'],
    ['build_solver_profile', '求解策略冻结', 'build_opendss_solver_profile', 'ok'],
    ['run_powerflow', '潮流求解', 'run_opendss_powerflow', 'ok'],
    ['extract_powerflow_result', '结果归一化', 'normalize_results', 'ok'],
    ['verify_power_balance', '独立验证与功率平衡', 'verify_result_integrity_and_power_balance', 'ok'],
    ['export_artifacts', '产物清单导出', 'export_artifacts', 'ok'],
  ];
  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">能力分组</div>
      <div class="grid grid-3" style="grid-template-columns:repeat(4,1fr)">
        ${groups.map((g) => `<div class="status-card"><div class="label">${g}</div><div style="margin-top:6px">${statusTag('info')} 已注册</div></div>`).join('')}
      </div></div>
    <div class="section-title">Skill 注册表（8 个标准工具）</div>
    <div class="card"><table><thead><tr><th>工具名称</th><th>描述</th><th>所属阶段</th><th>状态</th></tr></thead><tbody>
      ${tools.map(([n, d, s, st]) => `<tr><td><b>${n}</b></td><td>${d}</td><td><code>${s}</code></td><td>${statusTag(st)}</td></tr>`).join('')}
    </tbody></table></div>
    <div class="section-title">Skill 与引擎矩阵</div>
    <div class="card" style="font-size:12.5px;color:var(--text-muted)">
      同一个科学 Skill 可由不同引擎适配器承载，避免"一种引擎复制一套 Skill"。当前 8 个 Skill 均由 OpenDSS 适配器承载。</div>`;
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
    <div class="section-title">证据记忆与经验库</div>
    <div class="card"><div class="grid grid-3">
      ${['Working Memory','Episode Memory','Strategy Memory','Verified-State Memory','Safety Memory','Cross-Case Evidence'].map((n) => `
        <div class="status-card"><div class="label">${n}</div><div class="foot" style="margin-top:8px">记录量 0 · ${statusTag('gray')}</div></div>`).join('')}
    </div></div>
    <div class="section-title">Skill 演化</div>
    <div class="card"><div style="display:flex;gap:8px">
      ${['Draft','Candidate','Shadow','Active','Retired'].map((s, i) => `<span class="tag ${i === 3 ? 'ok' : 'gray'}">${s}</span>`).join('')}
    </div><div style="font-size:12px;color:var(--text-muted);margin-top:10px">单 Case 成功只能形成 Episode 或候选，不允许模型在线改写生产 Skill。</div></div>`;
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
      <span>${esc(x.name)}<span class="foot" style="margin-left:8px">${esc(x.note || '')}</span></span>
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
        ${[['应用数据目录','chat_data/'],['Case 库','cases/'],['会话数据库','chat_data/chat.db'],['Checkpoint','tasks/<id>/state.json'],['Reports','outputs/<id>/report.md'],['Artifacts','outputs/<id>/']].map(([a,b]) => `
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
    <div class="foot" style="margin:-4px 0 10px">清理方式为<b>归档</b>（移动到 <code>_archive/&lt;时间戳&gt;/</code>），不直接删除，可回滚。</div>
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

/* 10. 报告与导出 */
function pageReport() {
  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">报告默认设置</div>
      <div class="grid grid-2">
        ${field('默认格式', `<select><option>Markdown / JSON</option></select>`)}
        ${field('默认导出目录', textInput('r-1', 'outputs/'))}
      </div></div>
    <div class="section-title">报告内容项</div>
    <div class="card"><div class="grid grid-3">
      ${['工程结论','最终调整方案','调整前后对比','执行轨迹','Policy 摘要','Skill 和工具贡献','引擎证据','技术附录','脱敏策略'].map((n) => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span>${n}</span>${switchInput('rep-' + n, true)}</div>`).join('')}
    </div><div style="font-size:12px;color:var(--text-muted);margin-top:10px">原始模型输出、内部 ID、原始 State 和未脱敏路径默认不进入普通报告。</div></div>`;
}

/* 11. 日志与诊断 */
function pageLogs() {
  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">日志与可观测性</div>
      <div class="grid grid-2">
        ${field('Sidecar 日志状态', `<span class="tag ok"><span class="dot"></span>正常</span>`)}
        ${field('最近错误摘要', `<input type="text" value="无" disabled />`)}
      </div>
      <div style="display:flex;gap:10px"><button class="btn">打开日志目录</button><button class="btn">导出脱敏诊断包</button></div></div>
    <div class="section-title">管理员</div>
    <div class="card"><div class="grid grid-2">
      ${[['日志级别','信息'],['日志保留','30 天'],['原始工具 stdout/stderr 保留','否'],['失败现场保留','是']].map(([a,b]) => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span>${a}</span><b>${b}</b></div>`).join('')}
    </div><div style="font-size:12px;color:var(--text-muted);margin-top:10px">失败分类规则不可配置为「把工具失败视为物理不收敛」。</div></div>`;
}

/* 12. 通知与外观 */
function pageAppearance() {
  const opts = ['浅色','深色','跟随系统'];
  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">通知与外观</div>
      <div class="grid grid-2">
        ${field('主题', `<select>${opts.map((o) => `<option>${o}</option>`).join('')}</select>`)}
        ${field('语言', `<select><option>简体中文</option></select>`)}
        ${field('字体大小', `<select><option>标准</option></select>`)}
        ${field('页面缩放', `<select><option>100%</option></select>`)}
      </div></div>
    <div class="section-title">通知</div>
    <div class="card"><div class="grid grid-2">
      ${['后台任务完成通知','任务失败通知','等待审批通知','声音提示','启动恢复上次会话','自动生成会话标题'].map((n, i) => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span>${n}</span>${switchInput('noti-' + i, true)}</div>`).join('')}
    </div></div>`;
}

/* 13. 安全与凭据 */
function pageSecurity() {
  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">安全与凭据</div>
      <div class="grid grid-2">
        ${field('凭据保存方式', `<span class="tag warn">本机配置文件（未加密）</span>`)}
        ${field('API Key 配置', `<span class="tag ${state.llmApiKeySet ? 'info' : 'gray'}">${state.llmApiKeySet ? '已配置' : '未配置'}</span>`)}
        ${field('敏感日志脱敏检查', `<span class="tag gray">未执行独立检查</span>`)}
        ${field('后端连接地址', esc(backendEndpointLabel()))}
      </div></div>
    <div class="card" style="margin-top:14px;font-size:12.5px;color:var(--text-muted);line-height:1.7">
      当前未接入 Windows 安全存储。API Key 写入本机 settings.json，密码输入框只遮挡界面显示，不加密磁盘文件。
      请勿提交配置文件或含密钥的截图；后端健康状态不代表凭据已安全保存。</div>`;
}

/* 14. 版本与维护 */
function pageAbout() {
  return `
    ${configToolbar()}
    <div class="card"><div class="card-title">版本信息</div>
      <div class="grid grid-2" style="font-size:12.5px;color:var(--text-muted)">
        ${[['软件版本','1.6.0'],['Sidecar 版本','1.6.0'],['构建版本','—'],['Repository Commit 摘要','—'],['发布签名状态','未签名']].map(([a,b]) => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)"><span>${a}</span><b>${b}</b></div>`).join('')}
      </div></div>
    <div class="section-title">维护操作</div>
    <div class="card"><div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn">检查更新</button><button class="btn">重启 Sidecar</button>
      <button class="btn">环境诊断</button><button class="btn danger">恢复默认配置</button></div></div>`;
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

  // RAG 是否真的接上了（不再写死"否"）
  let ragOn = false;
  try {
    const st = await api('/api/rag/status');
    ragOn = Boolean(st && (st.enabled || (st.power_rag && st.power_rag.enabled)));
  } catch (e) { /* 拉不到就当没接 */ }

  if (!task) {
    return `
      <div class="card" style="margin-bottom:16px">
        <div class="card-title">结果展示</div>
        <div style="padding:18px 0;text-align:center;color:var(--text-muted)">
          <div style="font-size:15px;margin-bottom:6px">还没有跑过任务</div>
          <div style="font-size:12.5px">去 <b>分析工作台</b> 说一句「计算 IEEE33」，跑完这里就会显示真实结果。</div>
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
  const tabs = ['工程结果','恢复过程','假设与证据','引擎与能力','Policy 与版本','经验沉淀','技术审计'];

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
    <div class="card" style="margin-bottom:16px">
      <div style="display:flex;gap:6px">${tabs.map((t, i) => `<span class="tag ${i === 0 ? 'info' : 'gray'}">${t}</span>`).join('')}</div>
      <div class="foot" style="margin-top:8px">后 6 个页签为后续规划，暂未接入。</div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-title">关键指标</div>
        <table><thead><tr><th>指标</th><th>结果</th></tr></thead><tbody>
          ${metrics.map(([a,b]) => `<tr><td>${a}</td><td><b>${esc(b)}</b></td></tr>`).join('')}
        </tbody></table>
      </div>
      <div class="card">
        <div class="card-title">多引擎证据分层</div>
        <table><thead><tr><th>角色</th><th>引擎</th><th>状态</th></tr></thead><tbody>
          ${[['主执行引擎','OpenDSS','ok'],['辅助计算引擎','—','gray'],['独立复算引擎','OpenDSS（V2）','ok'],['跨引擎复核','预留','gray']].map(([a,b,st]) => `<tr><td>${a}</td><td>${b}</td><td>${statusTag(st)}</td></tr>`).join('')}
        </tbody></table>
        <div class="foot" style="margin-top:8px">辅助引擎强制标记"仅辅助"，不能冒充权威 V2。</div>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <div class="card-title">模块贡献</div>
      <table><thead><tr><th>模块</th><th>存在</th><th>被采用</th><th>对最终决策有贡献</th></tr></thead><tbody>
        ${[
          ['模型', '是', '是', '理解与解释'],
          ['Skill', '是', '是', '8 个工具执行'],
          ['Memory', '否', '—', '—'],
          ['RAG', ragOn ? '是' : '否', ragOn ? '是' : '—', ragOn ? '知识库检索补充证据' : '—'],
          ['Guard', '是', '是', '请求/执行/发布门'],
          ['引擎', '是', '是', '潮流计算'],
        ].map(([a,b,c,d]) => `<tr><td>${a}</td><td>${b}</td><td>${c}</td><td>${d}</td></tr>`).join('')}
      </tbody></table>
      <div class="foot" style="margin-top:8px">有记录不等于有贡献。</div>
    </div>`;
}

/* ============================================================
   路由
   ============================================================ */
const PAGE_RENDER = {
  overview: pageOverview, model: pageModel, engine: pageEngine, sidecar: pageSidecar,
  budget: pageBudget, policy: pagePolicy, skills: pageSkills, knowledge: pageKnowledge,
  storage: pageStorage, report: pageReport, logs: pageLogs, appearance: pageAppearance,
  security: pageSecurity, about: pageAbout,
};

let currentPage = 'config';
let currentSub = 'overview';

function renderSubnav() {
  const nav = document.getElementById('subnav');
  const savebar = document.getElementById('savebar');
  if (currentPage !== 'config') {
    nav.classList.add('hidden');
    savebar.classList.add('hidden');
    return;
  }
  nav.classList.remove('hidden');
  savebar.classList.remove('hidden');
  let html = '';
  for (const item of CONFIG_SUBNAV) {
    if (item.group === true) {
      html += `<div class="subnav-group-title">${item.label}</div>`;
    } else {
      const active = item.id === currentSub ? 'active' : '';
      html += `<div class="subnav-item ${active}" data-sub="${item.id}">${item.label}</div>`;
    }
  }
  nav.innerHTML = html;
  nav.querySelectorAll('.subnav-item').forEach((el) => {
    el.onclick = () => { currentSub = el.dataset.sub; route(); };
  });
  savebar.innerHTML = `
    <span class="hint">有 0 项未保存更改</span>
    <span style="margin-left:auto" class="hint"></span>
    <div class="right">
      <button class="btn" data-save-action="changes">查看变更</button>
      <button class="btn" data-save-action="discard">放弃更改</button>
      <button class="btn primary" data-save-action="save">保存并应用</button>
    </div>`;
  savebar.querySelectorAll('[data-save-action]').forEach((el) => el.addEventListener('click', async () => {
    if (el.dataset.saveAction === 'save') {
      await t.saveSettings({ apiBase: state.apiBase });
      showToast('配置已保存（当前可持久化项：后端 API 地址）');
    } else if (el.dataset.saveAction === 'discard') showToast('已放弃当前页面修改');
    else showToast('当前没有待保存变更');
  }));
}

// ---- 模型配置自动保存 ----
// 字段一改就防抖保存；后端重启推迟到任务空闲时执行（避免杀掉正在跑的任务）。
let _modelAutoSaveTimer = null;
let _modelAutoSaveBusy = false;
let _pendingBackendRestart = false;

function setAutoSaveStatus(cls, text) {
  const el = document.getElementById('model-autosave-status');
  if (!el) return;
  el.className = `tag ${cls}`;
  el.innerHTML = `<span class="dot"></span>${text}`;
}

function scheduleModelAutoSave() {
  setAutoSaveStatus('warn', '有修改…');
  clearTimeout(_modelAutoSaveTimer);
  _modelAutoSaveTimer = setTimeout(() => { autoSaveModelConfig().catch(() => {}); }, 1000);
}

function buildModelPatch() {
  const patch = {};
  const modelEl = document.getElementById('m-model');
  const pickedModel = modelEl && modelEl.value ? modelEl.value.trim() : '';
  patch.llmMode = state.llmMode;
  if (state.llmMode === 'local') {
    const ollamaUrlEl = document.getElementById('m-ollama-url');
    patch.llmOllamaUrl = (ollamaUrlEl && ollamaUrlEl.value.trim()) || 'http://localhost:11434/v1';
    patch.llmLocalModel = pickedModel || state.llmLocalModel || 'qwen2.5:7b';
    // 后端环境变量取 llmBaseUrl/llmModel，这里同步成当前生效值
    patch.llmBaseUrl = patch.llmOllamaUrl;
    patch.llmModel = patch.llmLocalModel;
    // ⚠️ 不清空 llmApiKey：云端 Key 随云端界面单独保存，切模式不能丢
  } else {
    const keyEl = document.getElementById('m-apikey');
    const urlEl = document.getElementById('m-url');
    patch.llmApiKey = keyEl ? keyEl.value.trim() : state.llmApiKey;
    patch.llmCloudBaseUrl = (urlEl && urlEl.value.trim()) || state.llmCloudBaseUrl || 'https://dashscope.aliyuncs.com/compatible-mode/v1';
    patch.llmCloudModel = pickedModel || state.llmCloudModel || 'qwen-plus';
    patch.llmBaseUrl = patch.llmCloudBaseUrl;
    patch.llmModel = patch.llmCloudModel;
  }
  return patch;
}

function patchEqualsState(patch) {
  return patch.llmBaseUrl === (state.llmBaseUrl || '')
    && patch.llmModel === (state.llmModel || '')
    && (patch.llmApiKey === undefined || patch.llmApiKey === state.llmApiKey)
    && (patch.llmOllamaUrl === undefined || patch.llmOllamaUrl === state.llmOllamaUrl)
    && (patch.llmCloudBaseUrl === undefined || patch.llmCloudBaseUrl === state.llmCloudBaseUrl)
    && (patch.llmCloudModel === undefined || patch.llmCloudModel === state.llmCloudModel)
    && (patch.llmLocalModel === undefined || patch.llmLocalModel === state.llmLocalModel);
}

async function autoSaveModelConfig() {
  if (_modelAutoSaveBusy) return;
  // 字段已经不在页面上（用户切走了）就不保存，避免用过期 DOM 覆盖配置
  if (!document.getElementById('m-model')) return;
  const patch = buildModelPatch();
  if (patchEqualsState(patch)) return;          // 没有实际变化 → 不动
  _modelAutoSaveBusy = true;
  setAutoSaveStatus('warn', '保存中…');
  try {
    await t.saveSettings(patch);
    state.llmApiKeySet = Boolean(patch.llmApiKey) || state.llmApiKeySet;
    if (patch.llmApiKey !== undefined) state.llmApiKey = patch.llmApiKey;
    state.llmBaseUrl = patch.llmBaseUrl || '';
    if (patch.llmCloudBaseUrl) state.llmCloudBaseUrl = patch.llmCloudBaseUrl;
    if (patch.llmCloudModel) state.llmCloudModel = patch.llmCloudModel;
    if (patch.llmLocalModel) state.llmLocalModel = patch.llmLocalModel;
    state.llmModel = patch.llmModel || '';
    if (patch.llmOllamaUrl) state.llmOllamaUrl = patch.llmOllamaUrl;
    _modelCache.fetched = false;                // 配置变了，模型下拉要重新拉
    // 任务执行中不重启后端（会杀掉正在跑的任务），标记稍后补做
    if (state.busy || (state.execProgress && state.execProgress.active)) {
      _pendingBackendRestart = true;
      setAutoSaveStatus('warn', '已保存，任务结束后生效');
    } else {
      await t.restartBackend();
      setAutoSaveStatus('ok', '已保存并生效');
      showToast('模型配置已保存并生效');
      if (currentPage === 'config' && currentSub === 'model') refreshModelField(true);
    }
  } catch (e) {
    setAutoSaveStatus('err', '保存失败');
    showToast(`保存失败：${e.message}`, 'error');
  } finally {
    _modelAutoSaveBusy = false;
  }
}

// 任务结束/空闲后补做被推迟的后端重启（状态栏轮询每 5s 调一次）
async function applyPendingBackendRestart() {
  if (!_pendingBackendRestart) return;
  if (state.busy || (state.execProgress && state.execProgress.active)) return;
  _pendingBackendRestart = false;
  try {
    await t.restartBackend();
    _modelCache.fetched = false;
    setAutoSaveStatus('ok', '已保存并生效');
    showToast('任务已结束，模型配置自动生效');
    if (currentPage === 'config' && currentSub === 'model') refreshModelField(true);
  } catch (e) { /* 下次轮询再试 */ _pendingBackendRestart = true; }
}

function bindConfigActions() {
  // 模型服务页：页面已用缓存/手输即时渲染，这里只后台补齐下拉（不阻塞）
  if (currentSub === 'model') refreshModelField();

  document.querySelectorAll('[data-config-action="model-test"]').forEach((el) => el.addEventListener('click', async () => {
    el.disabled = true; el.textContent = '测试中…';
    const status = document.getElementById('conn-status');
    const setStatus = (cls, label) => {
      if (!status) return;
      status.className = `tag ${cls}`;
      status.innerHTML = `<span class="dot"></span>${label}`;
    };
    setStatus('gray', '测试中…');
    try {
      // 1) 本地后端（毫秒级）
      const health = await api('/api/health');
      // 2) 远端 LLM（/api/models，后端 8s 超时）
      let llmLabel = '远端 LLM 通';
      let llmCls = 'ok';
      try {
        const m = await api('/api/models');
        if (m.ok) {
          llmLabel = `远端 LLM 通（${m.count} 个模型）`;
        } else {
          llmCls = 'warn';
          llmLabel = '远端 LLM 不可达（仅规则对话可用）';
        }
      } catch (e) {
        llmCls = 'warn';
        llmLabel = '远端 LLM 探测失败（仅规则对话可用）';
      }
      setStatus(llmCls, `后端 ok；${llmLabel}`);
      showToast(`后端 ok；${llmLabel}`);
    } catch (e) {
      setStatus('err', `后端失败：${e.message}`);
      showToast(`后端失败：${e.message}`, 'error');
    } finally { el.disabled = false; el.textContent = '测试连接'; }
  }));
  // 重新向服务端拉取可用模型列表（换 Key / 换 Base URL 后使用）
  document.querySelectorAll('[data-config-action="models-refresh"]').forEach((el) => el.addEventListener('click', async () => {
    el.disabled = true; el.textContent = '拉取中…';
    try {
      const m = await fetchModels(true);
      // 只替换模型字段，不重渲染整页（避免再次卡住）
      await refreshModelField(true);
      if (m.models.length) {
        showToast(`已拉取 ${m.models.length} 个模型`);
      } else {
        showToast(`未拉到模型：${m.error || '未知原因'}`, 'error');
      }
    } catch (e) {
      showToast(`刷新失败：${e.message}`, 'error');
    } finally { el.disabled = false; el.textContent = '刷新模型列表'; }
  }));

  // ---- 知识库：重建本地 RAG 索引（刷新标志 = ⟳ 旋转动画）----
  // 新增/修改 knowledge/ 下的文档后点它，索引才会更新。
  document.querySelectorAll('[data-config-action="kb-refresh"]').forEach((el) => el.addEventListener('click', async () => {
    el.disabled = true;
    el.classList.add('is-refreshing');
    const label = el.querySelector('.btn-label');
    if (label) label.textContent = '重建中…';
    try {
      const r = await api('/api/rag/ingest?force=true', 'POST');
      showToast(`索引已重建：${r.num_sources} 份文档 / ${r.num_chunks} 个片段`);
      if (currentPage === 'config') route();
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
    el.disabled = true; el.textContent = '盘点中…';
    try {
      _storageCache.data = await api('/api/storage');
      showToast('盘点完成');
      route();
    } catch (e) {
      showToast(`盘点失败：${e.message}`, 'error');
      el.disabled = false; el.textContent = '重新盘点';
    }
  }));

  document.querySelectorAll('[data-config-action="storage-archive"]').forEach((el) => el.addEventListener('click', async () => {
    const items = _stgSelected();
    if (!items.length) { showToast('请先勾选要归档的条目', 'error'); return; }
    const total = items.reduce((s, it) => {
      const g = (_storageCache.data && _storageCache.data.groups[it.kind]) || [];
      const hit = g.find((x) => x.path === it.path);
      return s + (hit ? hit.size_bytes : 0);
    }, 0);
    if (!window.confirm(`将归档 ${items.length} 项（约 ${fmtBytes(total)}）到 _archive/。\n\n这是移动操作，不删除，可回滚。确认继续？`)) return;
    el.disabled = true; el.textContent = '归档中…';
    try {
      const r = await api('/api/storage/archive', 'POST', { items, confirm: true });
      showToast(r.message || `已归档 ${r.moved.length} 项`);
      if (r.failed && r.failed.length) {
        showToast(`有 ${r.failed.length} 项未处理：${r.failed[0].error}`, 'error');
      }
      _storageCache.data = null;
      _storageCache.data = await api('/api/storage');
      route();
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

  // ---- 模式切换（云端 / 本地 Ollama）----
  document.querySelectorAll('input[name="llm-mode"]').forEach((el) => el.addEventListener('change', () => {
    const next = el.value === 'local' ? 'local' : 'cloud';
    if (next === state.llmMode) return;
    state.llmMode = next;
    // 切模式后模型列表要重新拉
    _modelCache.fetched = false;
    _modelCache.models = [];
    if (currentPage === 'config' && currentSub === 'model') route();
  }));

  // ---- 外接大模型配置：API Key / Base URL / 模型名 ----
  // 自动保存：字段改动 → 防抖保存 → 任务空闲时自动重启后端生效（不再需要手动按钮）
  ['m-apikey', 'm-url', 'm-model', 'm-ollama-url'].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const ev = el.tagName === 'SELECT' ? 'change' : 'input';
    el.addEventListener(ev, scheduleModelAutoSave);
  });

  document.querySelectorAll('[data-config-action="health"]').forEach((el) => el.addEventListener('click', async () => {
    el.disabled = true; el.textContent = '检测中…';
    try {
      await api('/api/cases');
      showToast('后端可达，算例目录读取正常');
    } catch (e) { showToast(`环境检测失败：${e.message}`, 'error'); }
    finally { el.disabled = false; el.textContent = '一键环境检测'; }
  }));
  document.querySelectorAll('[data-config-action="export"]').forEach((el) => el.addEventListener('click', async () => {
    const settings = await t.getSettings();
    const blob = new Blob([JSON.stringify({ apiBase: settings.apiBase }, null, 2)], { type: 'application/json' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'tianshu-settings-redacted.json'; a.click(); URL.revokeObjectURL(a.href);
    showToast('已导出脱敏配置');
  }));
  document.querySelectorAll('[data-config-action="reset"]').forEach((el) => el.addEventListener('click', async () => {
    await t.saveSettings({ apiBase: 'http://127.0.0.1:8000' }); state.apiBase = 'http://127.0.0.1:8000'; showToast('已恢复默认后端地址');
  }));
  document.querySelectorAll('[data-config-action="import"]').forEach((el) => el.addEventListener('click', () => showToast('导入配置将在下一步接入文件选择器')));
  document.querySelectorAll('#savebar .btn').forEach((el) => el.addEventListener('click', () => showToast('当前页面无待保存的后端配置')));
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
        <div style="font-size:12.5px">其它页面不受影响；也可以到「日志与诊断」查看具体情况。</div>
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
    const fn = PAGE_RENDER[currentSub] || pageOverview;
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
  try {
    await api('/api/cases');
    hideBootOverlay();
    if (currentPage === 'workbench') await loadSessions();
  } catch (e) {
    // 后端还没起来是首次启动的正常中间状态，不弹错误遮罩，等 backend:ready 再补加载
    updateBackendStatus(false, e.message);
    hideBootOverlay();
  }
})();

// 后端就绪后补加载，避免首次启动时会话列表为空
t.onBackendReady?.(async (info) => {
  setBootMessage('后端已就绪，正在加载数据…');
  if (info?.restarted) showToast('后端已自动重启，之前的任务已中断，请重新发送');
  try {
    await api('/api/cases');
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
