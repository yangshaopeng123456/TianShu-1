// Real renderer functions with controlled bridge timing, explicitly not Electron/solver validation.
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, 'renderer', 'app.js'), 'utf8');
const renderer = source.slice(0, source.indexOf('// 状态栏不是装饰'));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function flush() { for (let i = 0; i < 15; i++) await Promise.resolve(); }
function batch(status = 'queued', options = {}) {
  const itemStatus = options.itemStatus || (status === 'paused' ? 'queued' : status);
  return { id: options.id || 'batch-A', session_id: options.sid || 'S', status,
    can_retry: options.canRetry || false,
    counts: { total: 1, finished: ['queued', 'running', 'cancelling', 'paused'].includes(status) ? 0 : 1, success: status === 'success' ? 1 : 0 },
    items: [{ id: 'item-A', display_name: options.name || 'caseA.csv', status: itemStatus,
      task_id: options.taskId || null, error: options.error || '', message: options.message || null }] };
}
function harness() {
  const elements = new Map(), timers = new Map(), calls = [], messages = [];
  let id = 0, streamStops = 0;
  const element = name => {
    if (!elements.has(name)) elements.set(name, {
      innerHTML: '', textContent: '', value: '', disabled: false, style: {}, dataset: {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      children: [], scrollTop: 0, scrollHeight: 0, clientHeight: 0,
      querySelector() { return null; }, querySelectorAll() { return []; }, handlers: new Map(),
      addEventListener(type, handler) { this.handlers.set(type, handler); },
      remove() {}, insertBefore() {}, focus() {}, setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
    });
    return elements.get(name);
  };
  const document = { getElementById: element, querySelectorAll: () => [], querySelector: () => null,
    createElement: () => element('created'), body: { appendChild() {} }, activeElement: null };
  const bridge = {
    minimize() {}, maximize() {}, close() {}, isMaximized() { return false; }, onMaximizeChange() {}, onTaskEvent() {},
    apiRequest(method, url) {
      if (url === '/api/chat/sessions') return Promise.resolve({ sessions: [{ id: 'S' }] });
      const request = { ...deferred(), method, url }; calls.push(request); return request.promise;
    },
    sendMessage(sessionId, content) { const request = { ...deferred(), sessionId, content }; messages.push(request); return request.promise; },
    startTaskStream() { return Promise.resolve(true); },
    stopTaskStream() { streamStops++; return Promise.resolve(true); },
  };
  const context = vm.createContext({ window: { tianshu: bridge, document }, document, console, URL,
    localStorage: { getItem: () => null, setItem() {} }, requestAnimationFrame: fn => fn(),
    setTimeout(fn, ms) { const n = ++id; timers.set(n, { fn, ms }); return n; },
    setInterval(fn, ms) { const n = ++id; timers.set(n, { fn, ms, repeat: true }); return n; },
    clearTimeout(n) { timers.delete(n); }, clearInterval(n) { timers.delete(n); },
  });
  vm.runInContext(renderer, context, { filename: 'renderer/app.js' });
  const state = vm.runInContext('state', context);
  state.activeSessionId = 'S'; state.activeSession = { id: 'S', messages: [], batches: [] };
  const select = (sid, batches = []) => {
    context.stopBatchMonitoring();
    state.activeSessionId = sid; state.activeSession = { id: sid, messages: [], batches };
    context.startBatchMonitoring(state.activeSession);
  };
  return { context, state, calls, messages, timers, document, element, select, streamStops: () => streamStops };
}
const tests = [];
function test(name, run) { tests.push({ name, run }); }

test('持久化批次卡恢复暂停/重试/报告入口并转义文件名和错误', async () => {
  const h = harness();
  const paused = batch('paused', { name: '<img src=x onerror=alert(1)>', error: '<script>bad</script>' });
  const ended = batch('completed_with_errors', { id: 'batch-B', itemStatus: 'system_failed', canRetry: true,
    taskId: 'real-task-id', message: { report_url: '/api/tasks/real-task-id/report' } });
  const html = h.context.renderBatchCards({ batches: [paused, ended] });
  assert.ok(html.includes('继续未开始项') && html.includes('重试失败或中断项') && html.includes('打开报告'));
  assert.ok(html.includes('等待继续') && html.includes('执行失败'));
  assert.ok(!html.includes('<img') && !html.includes('<script>'));
  assert.ok(html.includes('&lt;img'));
});

test('会话GET真实结构包含batches时恢复监视，旧后端无batches保持兼容', async () => {
  const h = harness();
  const loading = h.context.loadSession('S');
  h.calls[0].resolve({ id: 'S', messages: [], batches: [batch()] }); await loading;
  assert.ok([...h.timers.values()].some(timer => timer.ms === 1000));
  const legacy = h.context.loadSession('T');
  h.calls[1].resolve({ id: 'T', messages: [] }); await legacy;
  assert.equal([...h.timers.values()].filter(timer => timer.ms === 1000).length, 0);
});

test('A→B→A切换丢弃第一次A的迟到轮询', async () => {
  const h = harness(); h.select('A', [batch('running', { sid: 'A', id: 'latest-A' })]);
  const old = h.context.pollBatches();
  h.select('B', []); h.select('A', [batch('running', { sid: 'A', id: 'latest-A' })]);
  h.calls[0].resolve({ batches: [batch('success', { sid: 'A', id: 'obsolete-A' })] }); await old;
  assert.equal(h.state.activeSession.batches[0].id, 'latest-A');
});

test('取消ACK保留cancelling直到权威轮询终态且不会重跑', async () => {
  const h = harness(); h.select('S', [batch('running', { taskId: 'owned-task' })]);
  const cancelling = h.context.runBatchAction('batch-A', 'cancel');
  assert.equal(h.calls[0].method, 'POST');
  assert.ok(h.calls[0].url.endsWith('/batch-A/cancel'));
  h.calls[0].resolve(batch('cancelling', { taskId: 'owned-task' })); await cancelling;
  assert.equal(h.state.activeSession.batches[0].status, 'cancelling');
  assert.ok(h.context.renderBatchCards(h.state.activeSession).includes('正在停止'));
  const poll = h.context.pollBatches();
  h.calls[1].resolve({ batches: [batch('cancelled', { taskId: 'owned-task' })] }); await poll;
  assert.equal(h.state.activeSession.batches[0].status, 'cancelled');
  assert.equal(h.messages.length, 0);
});

test('取消后旧running轮询不能回灌，单项取消按sid/batch/item路由', async () => {
  const h = harness(); h.select('S', [batch('running')]);
  const old = h.context.pollBatches();
  const action = h.context.runBatchAction('batch-A', 'cancel-item', 'item-A');
  assert.equal(h.calls[1].url, '/api/chat/sessions/S/batches/batch-A/items/item-A/cancel');
  h.calls[1].resolve(batch('cancelled')); await action;
  h.calls[0].resolve({ batches: [batch('running')] }); await old;
  assert.equal(h.state.activeSession.batches[0].status, 'cancelled');
});

test('旧会话的resume/retry响应不写入另一会话', async () => {
  const h = harness(); h.select('S', [batch('completed_with_errors', { canRetry: true, itemStatus: 'interrupted' })]);
  const action = h.context.runBatchAction('batch-A', 'retry-failed');
  assert.ok(h.calls[0].url.endsWith('/retry-failed'));
  h.select('T', [batch('success', { sid: 'T', id: 'batch-T' })]);
  h.calls[0].resolve(batch('queued')); await action;
  assert.equal(h.state.activeSessionId, 'T');
  assert.equal(h.state.activeSession.batches[0].id, 'batch-T');
});

test('批次轮询保留输入草稿、光标与已选择附件标签', async () => {
  const h = harness(); h.select('S', [batch('running')]);
  const input = h.element('chat-input'); input.value = '下一条尚未发送的任务';
  input.selectionStart = 2; input.selectionEnd = 5; h.document.activeElement = input;
  h.element('selected-file').textContent = '已选择：next.csv'; h.state.selectedFiles = ['next.csv'];
  const poll = h.context.pollBatches();
  h.calls[0].resolve({ batches: [batch('success', { message: { id: 'result', role: 'assistant', content: '完成', created_at: '2026' } })] }); await poll;
  assert.equal(h.element('chat-input').value, '下一条尚未发送的任务');
  assert.equal(h.element('chat-input').selectionEnd, 5);
  assert.equal(h.element('selected-file').textContent, '已选择：next.csv');
  assert.equal(h.state.selectedFiles[0], 'next.csv');
  assert.equal(h.state.activeSession.messages[0].id, 'result');
});

test('断线保留最后持久化状态并继续轮询，不伪造完成', async () => {
  const h = harness(); h.select('S', [batch('running')]);
  const poll = h.context.pollBatches(); h.calls[0].reject(new Error('offline')); await poll;
  assert.equal(h.state.activeSession.batches[0].status, 'running');
  assert.ok([...h.timers.values()].some(timer => timer.ms === 1000));
});

test('202提交解除发送等待但保留正在排队的批次与监视', async () => {
  const h = harness(); h.element('chat-input').value = '计算这两个算例';
  h.state.selectedFiles = ['a.zip', 'b.zip'];
  const sending = h.context.sendMessage(); await flush();
  h.messages[0].resolve({ batch_id: 'batch-A', batch: batch(), user_message: { id: 'user' } }); await flush();
  const sessionRequest = h.calls.find(request => request.url === '/api/chat/sessions/S');
  assert.ok(sessionRequest);
  sessionRequest.resolve({ id: 'S', messages: [], batches: [batch()] }); await sending;
  assert.equal(h.state.busy, false);
  assert.equal(h.state.execProgress, null);
  assert.equal(h.state.activeTaskId, null);
  assert.equal(h.state.selectedFiles.length, 0);
  assert.equal(h.state.activeSession.batches[0].status, 'queued');
  assert.ok(h.streamStops() > 0);
  assert.ok([...h.timers.values()].some(timer => timer.ms === 1000));
});

test('其他单任务执行时批次终态不清其busy/task/progress或收流', async () => {
  const h = harness(); h.element('chat-input').value = '计算当前单任务';
  h.context.sendMessage(); await flush();
  h.state.activeTaskId = 'new-single-task'; h.state.execProgress = { active: true, task_id: 'new-single-task', percent: 25 };
  h.select('S', [batch('running', { taskId: 'old-batch-task' })]);
  const stops = h.streamStops();
  const poll = h.context.pollBatches(); h.calls.at(-1).resolve({ batches: [batch('success', { taskId: 'old-batch-task' })] }); await poll;
  assert.equal(h.state.busy, true);
  assert.equal(h.state.activeTaskId, 'new-single-task');
  assert.equal(h.state.execProgress.task_id, 'new-single-task');
  assert.equal(h.streamStops(), stops);
});

function bindActualTraceButton(h) {
  const button = h.element('batch-trace-button'); button.dataset.batchTrace = 'task-A';
  h.document.querySelectorAll = selector => selector === '[data-batch-trace]' ? [button] : [];
  h.context.bindWorkbenchEvents();
  h.element('inline-trace').open = false;
  return () => button.handlers.get('click')();
}
test('实际查看过程click处理器在完成任务响应后展开自己的卡片', async () => {
  const h = harness(), click = bindActualTraceButton(h);
  click(); assert.equal(h.calls[0].url, '/api/tasks/task-A/trace');
  h.calls[0].resolve({ stages: [{ stage: 'report', title: '报告已生成' }], events: [] }); await flush();
  assert.equal(h.state.traceTaskId, 'task-A');
  assert.equal(h.element('inline-trace').open, true);
});
test('查看过程中手动折叠后迟到响应和普通刷新都不强行展开', async () => {
  const h = harness(), click = bindActualTraceButton(h);
  click(); h.element('inline-trace').open = false;
  h.element('inline-trace').handlers.get('toggle')();
  h.calls[0].resolve({ stages: [{ stage: 'report' }], events: [] }); await flush();
  h.context.updateTracePanel();
  assert.equal(h.state.traceOpen, false); assert.equal(h.element('inline-trace').open, false);
});
test('旧会话查看过程响应不展开新会话卡片', async () => {
  const h = harness(), click = bindActualTraceButton(h);
  click(); const loading = h.context.loadSession('T');
  h.calls[1].resolve({ id: 'T', messages: [], batches: [] }); await loading;
  h.calls[0].resolve({ stages: [{ stage: 'report' }], events: [] }); await flush();
  assert.equal(h.state.activeSessionId, 'T'); assert.equal(h.state.traceTaskId, null);
  assert.equal(h.element('inline-trace').open, false);
});
test('健康卡显示当前端点并隐藏URL凭据、路径和查询参数', async () => {
  const h = harness(); h.state.apiBase = 'http://private-user:private-secret@127.0.0.1:18016/private?api_key=hidden';
  vm.runInContext(`_overviewCache.data={checks:{sidecar:{ok:true,level:'ok',detail:'FastAPI 8000 健康检查自检通过'}}}`, h.context);
  const html = await h.context.pageOverview();
  assert.ok(html.includes('http://127.0.0.1:18016 健康检查通过'));
  assert.ok(!html.includes('FastAPI 8000') && !html.includes('private-secret') && !html.includes('api_key'));
});

(async () => {
  let failed = 0;
  for (const { name, run } of tests) {
    try { await run(); process.stdout.write(`[OK] ${name}\n`); }
    catch (error) { failed++; process.stderr.write(`[FAIL] ${name}\n${error.stack}\n`); }
  }
  process.stdout.write(`结果: ${tests.length - failed}/${tests.length} 通过；真实renderer函数/受控bridge时序，非真Electron或COM测试\n`);
  process.exitCode = failed ? 1 : 0;
})();
