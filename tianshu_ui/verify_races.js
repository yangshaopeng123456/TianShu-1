// 用真实 renderer 函数与可控制的响应顺序验证异步行为，不启动 Electron 或后端。
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, 'renderer', 'app.js'), 'utf8');
// 只跳过启动时的轮询、路由和后端连接；所有被测函数及事件订阅保留原代码。
const renderer = source.slice(0, source.indexOf('// 状态栏不是装饰'));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
function harness() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: '', textContent: '', value: '', disabled: false, style: {}, dataset: {},
      children: [], scrollTop: 0, scrollHeight: 0, clientHeight: 0,
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      querySelector() { return null; }, querySelectorAll() { return []; },
      addEventListener() {}, remove() {}, insertBefore() {},
    });
    return elements.get(id);
  }
  const timers = new Map();
  let nextTimer = 0, events;
  const requests = { session: [], progress: [], trace: [], messages: [], streams: [], cancels: [] };
  let streamStops = 0;
  const document = {
    getElementById: element, querySelectorAll: () => [], querySelector: () => null,
    createElement: () => element('created'), body: { appendChild() {} },
  };
  const bridge = {
    minimize() {}, maximize() {}, close() {}, isMaximized() { return false; }, onMaximizeChange() {},
    onTaskEvent(callback) { events = callback; },
    apiRequest(method, url) {
      if (url === '/api/chat/sessions') return Promise.resolve({ sessions: [{ id: 'S', messages: [] }] });
      const job = deferred();
      if (url === '/api/task-progress') requests.progress.push(job);
      else if (url.endsWith('/trace')) requests.trace.push({ ...job, url });
      else if (url.endsWith('/cancel')) { requests.cancels.push(url); return Promise.resolve({}); }
      else if (url.startsWith('/api/chat/sessions/')) requests.session.push({ ...job, id: decodeURIComponent(url.split('/').pop()) });
      else return Promise.resolve({});
      return job.promise;
    },
    sendMessage(sessionId, content) {
      const job = { ...deferred(), sessionId, content };
      requests.messages.push(job);
      return job.promise;
    },
    startTaskStream(streamId) {
      const job = { ...deferred(), streamId };
      requests.streams.push(job);
      return job.promise;
    },
    stopTaskStream() { streamStops++; return Promise.resolve(true); },
  };
  const context = vm.createContext({
    window: { tianshu: bridge, document }, document, console,
    localStorage: { getItem: () => null, setItem() {} },
    requestAnimationFrame: (fn) => fn(),
    setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, ms, repeat: false }); return id; },
    setInterval(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, ms, repeat: true }); return id; },
    clearTimeout(id) { timers.delete(id); }, clearInterval(id) { timers.delete(id); },
  });
  vm.runInContext(renderer, context, { filename: 'renderer/app.js' });
  const state = vm.runInContext('state', context);
  const status = vm.runInContext('statusState', context);
  element('sb-tasks').querySelector = selector => selector === '.status-label' ? element('sb-task-label') : selector === '.dot' ? element('sb-task-dot') : null;
  state.activeSessionId = 'S';
  state.activeSession = { id: 'S', messages: [] };
  element('chat-input').value = '计算当前算例';
  return {
    context, state, status, requests, element, timers,
    emit: (streamId, event, data = {}) => events({ streamId, event, data }),
    streamStops: () => streamStops,
    async runTimeout(ms) {
      for (const [id, timer] of [...timers]) if (!timer.repeat && timer.ms === ms) {
        timers.delete(id); timer.fn();
      }
      await flush();
    },
  };
}

async function completeMessage(h, message, index, taskId, taskStatus) {
  h.requests.messages[index].resolve({ assistant_message: { task_id: taskId, task_status: taskStatus } });
  await flush();
  if (h.requests.session.length) h.requests.session.at(-1).resolve({ id: h.state.activeSessionId, messages: [] });
  await message;
}

const tests = [];
function test(name, run) { tests.push({ name, run }); }

test('preload和主进程把streamId原样传递到每个SSE事件及closed事件', async () => {
  let exposed, invocation;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'preload.js'), 'utf8'), {
    require: () => ({
      contextBridge: { exposeInMainWorld(_name, bridge) { exposed = bridge; } },
      ipcRenderer: { invoke(...args) { invocation = args; return Promise.resolve(true); } },
    }),
  });
  await exposed.startTaskStream(42);
  assert.deepEqual(invocation, ['task:stream:start', 42]);
  const main = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
  const begin = main.indexOf("  ipcMain.handle('task:stream:start'");
  const end = main.indexOf("  ipcMain.handle('task:stream:stop'", begin);
  let handler;
  const responseEvents = {}, forwarded = [];
  const context = {
    ipcMain: { handle(_channel, fn) { handler = fn; } },
    readSettings: () => ({}), DEFAULT_API: 'http://127.0.0.1:8000',
    taskStreamReq: null, stopTaskStream() {},
    http: { get(_url, onResponse) {
      onResponse({ setEncoding() {}, on(name, callback) { responseEvents[name] = callback; } });
      return { on() {}, destroy() {} };
    } },
  };
  vm.runInNewContext(main.slice(begin, end), context);
  await handler({ sender: { send(channel, payload) { forwarded.push({ channel, payload }); } } }, 42);
  responseEvents.data('event: trace\ndata: {"stage":"solve"}\n\n');
  responseEvents.end();
  assert.equal(forwarded.length, 2);
  assert.equal(forwarded[0].payload.streamId, 42);
  assert.equal(forwarded[0].payload.event, 'trace');
  assert.equal(forwarded[1].payload.streamId, 42);
  assert.equal(forwarded[1].payload.event, 'closed');
});

test('快速切换会话只展示最后选择，A→B→A也不接受第一次A的响应', async () => {
  const h = harness();
  const a1 = h.context.loadSession('A');
  const b = h.context.loadSession('B');
  const a2 = h.context.loadSession('A');
  h.requests.session[2].resolve({ id: 'A', title: '最新 A', messages: [] }); await a2;
  h.requests.session[1].resolve({ id: 'B', title: 'B', messages: [] }); await b;
  h.requests.session[0].resolve({ id: 'A', title: '旧 A', messages: [] }); await a1;
  assert.equal(h.state.activeSessionId, 'A');
  assert.equal(h.state.activeSession.title, '最新 A');
});

test('停止后发送新任务，旧回复不解锁按钮、清空输入或关闭新流', async () => {
  const h = harness();
  const old = h.context.sendMessage();
  h.requests.streams[0].resolve(true);
  h.requests.progress[0].resolve({ active: true, task_id: 'old', percent: 20 }); await flush();
  await h.context.stopCurrentTask();
  h.element('chat-input').value = '新任务';
  const current = h.context.sendMessage();
  const stream = h.requests.streams[1]; stream.resolve(true);
  h.emit(stream.streamId, 'progress', { task_id: 'new', percent: 30 }); await flush();
  const stops = h.streamStops();
  h.requests.messages[0].resolve({ assistant_message: { task_id: 'old' } }); await old;
  assert.equal(h.state.busy, true);
  assert.equal(h.state.activeTaskId, 'new');
  assert.equal(h.element('chat-input').value, '新任务');
  assert.equal(h.element('send-message').disabled, true);
  assert.equal(h.element('stop-task').disabled, false);
  assert.equal(h.streamStops(), stops);
  // 新请求正常返回仍应解锁并关闭自己的流。
  h.requests.messages[1].resolve({ assistant_message: { task_id: 'new' } }); await flush();
  h.requests.session.at(-1).resolve({ id: 'S', messages: [] }); await current;
  assert.equal(h.state.busy, false);
  assert.equal(h.element('send-message').disabled, false);
  assert.equal(h.streamStops(), stops + 1);
});

test('停止后的迟到轮询和迟到建连不会覆盖新任务或复活旧定时器', async () => {
  const h = harness();
  h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'old', percent: 20 });
  await h.context.stopCurrentTask();
  h.element('chat-input').value = '新任务';
  h.context.sendMessage();
  const stream = h.requests.streams[1]; stream.resolve(true);
  h.emit(stream.streamId, 'progress', { task_id: 'new', percent: 40 });
  h.requests.progress[0].resolve({ active: true, task_id: 'old', percent: 99 });
  h.requests.streams[0].resolve(true); await flush();
  assert.equal(h.state.activeTaskId, 'new');
  assert.equal(h.state.execProgress.percent, 40);
  assert.equal([...h.timers.values()].filter((timer) => timer.repeat).length, 0);
  const stops = h.streamStops();
  await h.context.stopCurrentTask();
  assert.equal(h.streamStops(), stops + 1);
});

test('旧SSE进度、trace、done、closed全部丢弃，新流仍接受事件', async () => {
  const h = harness();
  h.context.sendMessage();
  const old = h.requests.streams[0].streamId;
  h.emit(old, 'progress', { task_id: 'old', percent: 10 });
  await h.context.stopCurrentTask();
  h.context.sendMessage();
  const current = h.requests.streams[1].streamId;
  h.emit(current, 'progress', { task_id: 'new', percent: 40 });
  const stops = h.streamStops();
  h.emit(old, 'progress', { task_id: 'old', percent: 90 });
  h.emit(old, 'trace', { stage: 'old-stage', message: '旧事件' });
  h.emit(old, 'done', { task_id: 'old' }); h.emit(old, 'closed');
  assert.equal(h.state.activeTaskId, 'new');
  assert.equal(h.state.execProgress.percent, 40);
  assert.equal(h.state.liveEvents.length, 0);
  assert.equal(h.streamStops(), stops);
  h.emit(current, 'trace', { stage: 'new-stage', message: '新事件' });
  assert.equal(h.state.liveEvents.length, 1);
  assert.equal(h.state.liveEvents[0].message, '新事件');
});

test('旧完成进度的延时清理不会删除新任务的进度', async () => {
  const h = harness();
  h.context.sendMessage();
  h.requests.progress[0].resolve({ active: true, task_id: 'old', percent: 20 }); await flush();
  const tick = [...h.timers.values()].find((timer) => timer.repeat && timer.ms === 1000);
  tick.fn(); h.requests.progress[1].resolve({ active: false, done: true, task_id: 'old', percent: 100 }); await flush();
  await h.context.stopCurrentTask();
  h.context.sendMessage();
  h.emit(h.requests.streams[1].streamId, 'progress', { task_id: 'new', percent: 35 });
  await h.runTimeout(1500); await h.runTimeout(4000);
  assert.equal(h.state.execProgress.task_id, 'new');
  assert.equal(h.state.execProgress.percent, 35);
  assert.match(h.element('exec-progress').innerHTML, /35%/);
});

test('新SSE连接先跟到正在退出的旧任务时，不接收其无task_id的trace', async () => {
  const h = harness();
  h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'old', percent: 10 });
  await h.context.stopCurrentTask();
  h.context.sendMessage();
  const current = h.requests.streams[1].streamId;
  h.emit(current, 'progress', { task_id: 'old', percent: 90 });
  h.emit(current, 'trace', { stage: 'old-stage', message: '旧任务收尾' });
  assert.equal(h.state.liveEvents.length, 0);
  assert.equal(h.state.activeTaskId, null);
  h.emit(current, 'progress', { task_id: 'new', percent: 25 });
  h.emit(current, 'trace', { stage: 'new-stage' });
  assert.equal(h.state.activeTaskId, 'new');
  assert.equal(h.state.liveEvents.length, 1);
});

test('切到其他会话后，任务回复和轨迹回看不覆盖当前内容', async () => {
  const h = harness();
  const message = h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'old', percent: 20 });
  const trace = h.context.fetchTrace('old');
  const other = h.context.loadSession('B');
  h.requests.session[0].resolve({ id: 'B', messages: [] }); await other;
  h.element('chat-input').value = 'B 的草稿';
  h.requests.trace[0].resolve({ stages: [{ stage: 'old-stage' }], events: [{ stage: 'old-stage' }] }); await trace;
  h.emit(h.requests.streams[0].streamId, 'trace', { stage: 'old-stage' });
  h.requests.messages[0].resolve({ assistant_message: { task_id: 'old' }, session: { id: 'S' } }); await message;
  assert.equal(h.state.activeSessionId, 'B');
  assert.equal(h.state.activeSession.id, 'B');
  assert.equal(h.state.liveEvents.length, 0);
  assert.equal(h.element('chat-input').value, 'B 的草稿');
});

test('看门狗超时后重发，旧请求失败也不清理新请求', async () => {
  const h = harness();
  const old = h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'old', percent: 10 });
  await h.runTimeout(180000);
  assert.equal(h.state.busy, false);
  h.context.sendMessage();
  const current = h.requests.streams[1].streamId;
  h.emit(current, 'progress', { task_id: 'old', percent: 90 });
  h.emit(current, 'trace', { stage: 'old-stage' });
  assert.equal(h.state.liveEvents.length, 0);
  h.requests.messages[0].reject(new Error('旧连接中断')); await old;
  assert.equal(h.state.busy, true);
  assert.equal(h.element('stop-task').disabled, false);
});

for (const status of ['success', 'validation_failed', 'cancelled']) {
  test(`${status}权威回复立即更新匹配的顶部状态，不等5秒定时器`, async () => {
    const h = harness();
    const message = h.context.sendMessage();
    h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'top-task', percent: 40 });
    assert.equal(h.status.tasks.active, true);
    await completeMessage(h, message, 0, 'top-task', status);
    assert.equal(h.status.tasks.active, false);
    assert.equal(h.element('sb-bg-tasks').textContent, '0');
    assert.equal(h.element('sb-task-label').textContent, '后台任务');
  });

  test(`${status}权威回复到达时清除同任务的迟到活动进度，不等待下次轮询`, async () => {
    const h = harness();
    h.element('inline-trace').querySelector = selector => selector === '#trace-status' ? h.element('trace-status') : null;
    const message = h.context.sendMessage();
    h.requests.progress[0].resolve({ active: true, task_id: 'terminal-task', percent: 96 }); await flush();
    h.emit(h.requests.streams[0].streamId, 'trace', { stage: 'report', message: '真实报告事件' });
    await h.runTimeout(0);
    h.requests.messages[0].resolve({ assistant_message: { task_id: 'terminal-task', task_status: status } }); await flush();
    h.requests.session.at(-1).resolve({ id: 'S', messages: [{ role: 'assistant', task_id: 'terminal-task', task_status: status }] });
    await message;
    assert.equal(h.state.execProgress, null);
    assert.match(h.element('trace-status').textContent, /^已完成/);
    assert.equal([...h.timers.values()].filter(timer => timer.repeat).length, 0);
  });

  test(`真实轨迹先到、消息回复迟到时，${status}回复后标题立即显示完成`, async () => {
    const h = harness();
    h.element('inline-trace').querySelector = selector => selector === '#trace-status' ? h.element('trace-status') : null;
    const message = h.context.sendMessage();
    h.requests.progress[0].resolve({ active: true, task_id: 'finished-task', percent: 20 }); await flush();
    const tick = [...h.timers.values()].find(timer => timer.repeat && timer.ms === 1000);
    tick.fn(); h.requests.progress[1].resolve({ active: false, done: true, task_id: 'finished-task', percent: 100 }); await flush();
    await h.runTimeout(1500);
    h.emit(h.requests.streams[0].streamId, 'done', { task_id: 'finished-task' });
    h.requests.trace[0].resolve({ stages: [{ stage: 'report', title: '报告' }], events: [{ stage: 'report' }] }); await flush();
    assert.match(h.element('trace-status').textContent, /^执行中/);
    h.requests.messages[0].resolve({ assistant_message: { task_id: 'finished-task', task_status: status } }); await flush();
    h.requests.session.at(-1).resolve({ id: 'S', messages: [{ role: 'assistant', task_id: 'finished-task', task_status: status }] });
    await message;
    assert.equal(h.state.busy, false);
    assert.equal(h.state.execProgress, null);
    assert.match(h.element('trace-status').textContent, /^已完成/);
  });
}

test('取消ACK只解锁输入，收到实际cancelled回复才清顶部状态', async () => {
  const h = harness();
  const message = h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'cancel-task', percent: 10 });
  await h.context.stopCurrentTask();
  assert.equal(h.status.tasks.active, true);
  assert.equal(h.element('sb-bg-tasks').textContent, '1');
  await completeMessage(h, message, 0, 'cancel-task', 'cancelled');
  assert.equal(h.status.tasks.active, false);
  assert.equal(h.element('sb-bg-tasks').textContent, '0');
});

test('已停止旧请求的cancelled回复不能清除后来新任务的顶部状态', async () => {
  const h = harness();
  const old = h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'old-task', percent: 10 });
  await h.context.stopCurrentTask();
  h.context.sendMessage();
  h.emit(h.requests.streams[1].streamId, 'progress', { task_id: 'new-task', percent: 30 });
  await completeMessage(h, old, 0, 'old-task', 'cancelled');
  assert.equal(h.status.tasks.taskId, 'new-task');
  assert.equal(h.status.tasks.active, true);
  assert.equal(h.element('sb-bg-tasks').textContent, '1');
});

test('自己的任务终态不能清除全局状态栏正在显示的其他任务', async () => {
  const h = harness();
  const message = h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'own-task', percent: 20 });
  h.context.renderTaskStatus({ task_id: 'other-task', active: true, percent: 80 });
  await completeMessage(h, message, 0, 'own-task', 'success');
  assert.equal(h.status.tasks.taskId, 'other-task');
  assert.equal(h.status.tasks.active, true);
  assert.equal(h.element('sb-task-label').textContent, '计算中 80%');
});

test('HTTP错误或没有终态的响应不清除顶部活动状态', async () => {
  for (const failure of ['http', 'no-terminal']) {
    const h = harness();
    const message = h.context.sendMessage();
    h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'uncertain-task', percent: 60 });
    if (failure === 'http') { h.requests.messages[0].reject(new Error('请求超时')); await message; }
    else await completeMessage(h, message, 0, 'uncertain-task', 'running');
    assert.equal(h.status.tasks.active, true);
    assert.equal(h.element('sb-bg-tasks').textContent, '1');
  }
});

test('响应明确属于其他会话时，不能用它的终态清除自己的顶部任务', async () => {
  const h = harness();
  const message = h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'session-task', percent: 45 });
  h.requests.messages[0].resolve({ assistant_message: { task_id: 'session-task', task_status: 'success', session_id: 'other-session' } });
  await flush();
  h.requests.session.at(-1).resolve({ id: 'S', messages: [] }); await message;
  assert.equal(h.status.tasks.active, true);
  assert.equal(h.element('sb-bg-tasks').textContent, '1');
});

test('终态先到时，已在等待的旧refreshStatusBar活动帧不能回灌', async () => {
  const h = harness();
  const message = h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'finished-top', percent: 90 });
  const refresh = h.context.refreshStatusBar();
  await completeMessage(h, message, 0, 'finished-top', 'success');
  h.requests.progress[1].resolve({ task_id: 'finished-top', active: true, percent: 90 }); await refresh;
  assert.equal(h.status.tasks.active, false);
  assert.equal(h.element('sb-bg-tasks').textContent, '0');
});

test('终态后的新refresh也不能复活该task活动帧或误清其他task', async () => {
  const h = harness();
  const message = h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'finished-top', percent: 90 });
  await completeMessage(h, message, 0, 'finished-top', 'validation_failed');
  const firstRefresh = h.context.refreshStatusBar();
  h.requests.progress[1].resolve({ task_id: 'finished-top', active: true, percent: 90 }); await firstRefresh;
  assert.equal(h.status.tasks.active, false);
  h.context.renderTaskStatus({ task_id: 'other-top', active: true, percent: 25 });
  const secondRefresh = h.context.refreshStatusBar();
  h.requests.progress[2].resolve({ task_id: 'finished-top', active: true, percent: 90 }); await secondRefresh;
  assert.equal(h.status.tasks.taskId, 'other-top');
  assert.equal(h.status.tasks.active, true);
});

test('旧refresh在新发送代次开始后返回不能覆盖新任务顶部状态', async () => {
  const h = harness();
  h.context.sendMessage();
  h.emit(h.requests.streams[0].streamId, 'progress', { task_id: 'old-top', percent: 10 });
  const refresh = h.context.refreshStatusBar();
  await h.context.stopCurrentTask();
  h.context.sendMessage();
  h.emit(h.requests.streams[1].streamId, 'progress', { task_id: 'new-top', percent: 35 });
  h.requests.progress[1].resolve({ task_id: 'old-top', active: true, percent: 10 }); await refresh;
  assert.equal(h.status.tasks.taskId, 'new-top');
  assert.equal(h.element('sb-task-label').textContent, '计算中 35%');
});

(async () => {
  let failed = 0;
  for (const { name, run } of tests) {
    try { await run(); console.log(`[OK] ${name}`); }
    catch (error) { failed++; console.error(`[FAIL] ${name}\n${error.stack}`); }
  }
  console.log(`结果: ${tests.length - failed}/${tests.length} 通过`);
  process.exitCode = failed ? 1 : 0;
})();
