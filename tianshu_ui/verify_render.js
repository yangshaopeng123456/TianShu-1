// 用 Node + mock DOM 验证 app.js 的渲染逻辑（不依赖 Electron GUI）
const vm = require('vm');
const fs = require('fs');
const path = require('path');

function makeEl() {
  const s = new Set();
  return {
    onclick: null, innerHTML: '', textContent: '', dataset: {}, value: '', style: {},
    classList: {
      add: (c) => s.add(c), remove: (c) => s.delete(c),
      contains: (c) => s.has(c),
      toggle: (c, f) => { (f === undefined ? !s.has(c) : f) ? s.add(c) : s.delete(c); },
    },
    querySelectorAll: () => [], querySelector: () => makeEl(),
  };
}
const els = {};
const document = {
  getElementById: (id) => (els[id] || (els[id] = makeEl())),
  querySelectorAll: () => [],
  querySelector: () => makeEl(),
};
let storageAvailable = true;
// 与 /api/storage 的响应结构一致，验证当前的盘点及归档界面。
const storageSnapshot = {
  total_human: '1 KB', total_files: 1, total_bytes: 1024,
  db: { path: 'chat_data/chat.db', size_bytes: 1024 }, dirs: [], aux_dirs: [], aux_bytes: 0,
  groups: {}, counts: {}, sizes: {}, advice: {}, kept: {},
  never_touch: ['chat_data/uploads/ 中的算例原件', 'chat_data/chat.db'],
};
const window = {
  tianshu: {
    minimize(){}, maximize(){}, close(){}, isMaximized(){ return false; }, onMaximizeChange(){},
    getSettings: async () => ({}),
    apiRequest: async (_method, url) => {
      if (url === '/api/storage' && storageAvailable) return storageSnapshot;
      throw new Error('mock backend unavailable');
    },
  },
  document,
};

const code = fs.readFileSync(path.join(__dirname, 'renderer', 'app.js'), 'utf8');
const sandbox = { window, document, console, setTimeout, clearTimeout };
vm.createContext(sandbox);
try {
  vm.runInContext(code, sandbox);
} catch (e) {
  console.log('执行 app.js 异常:', e.message);
  process.exit(1);
}

const checks = [
  ['overview', sandbox.pageOverview, ['就绪度', '配置完成度', '一键环境检测']],
  // 沙箱里 state.llmMode 默认 cloud → 默认模型名 qwen-plus；改用 mode 后字段变成「连接方式」
  ['model', sandbox.pageModel, ['基础配置', '连接方式', '云端 API', '本地 Ollama', 'qwen-plus', '能力检测']],
  ['engine', sandbox.pageEngine, ['OpenDSS', '引擎注册表', '引擎 × 能力矩阵', 'dss-python']],
  ['sidecar', sandbox.pageSidecar, ['Sidecar 运行状态', '重启 Sidecar']],
  ['budget', sandbox.pageBudget, ['分析深度', '求解器调用数', '高级与管理员模式']],
  ['policy', sandbox.pagePolicy, ['默认任务模板', '用户硬约束', '验真策略']],
  ['skills', sandbox.pageSkills, ['load_case', 'run_powerflow', 'verify_power_balance', '8 个标准工具']],
  ['knowledge', sandbox.pageKnowledge, ['RAG 知识库', '重建索引', 'Power-RAG', 'Skill 演化']],
  ['storage', sandbox.pageStorage, ['chat_data', '归档选中项', '重新盘点', '不可清理项']],
  ['report', sandbox.pageReport, ['Markdown / JSON', '报告内容项']],
  ['logs', sandbox.pageLogs, ['日志与可观测性', '导出脱敏诊断包']],
  ['appearance', sandbox.pageAppearance, ['通知与外观', '浅色', '深色']],
  ['security', sandbox.pageSecurity, ['Windows 安全存储', 'API Key']],
  ['about', sandbox.pageAbout, ['版本信息', '软件版本', '恢复默认配置']],
  ['workbench', sandbox.pageWorkbench, ['会话队列', '执行过程', 'chat-messages', 'chat-input', 'trace-steps', 'stop-task']],
  // 无后端时走"还没跑过任务"空态；有真实任务时才渲染 工程结论/多引擎证据分层/模块贡献
  ['results', sandbox.pageResults, ['结果展示', '还没有跑过任务', '分析工作台']],
];

let fail = 0;
(async () => {
for (const [name, fn, keywords] of checks) {
  let html = '';
  try { html = await fn(); } catch (e) { console.log(`  [FAIL] ${name}: 抛异常 ${e.message}`); fail++; continue; }
  if (html == null || typeof html !== 'string') { console.log(`  [SKIP] ${name}: 返回非字符串（需 mock api）`); continue; }
  const missing = keywords.filter((k) => !html.includes(k));
  const ok = html.length > 100 && missing.length === 0;
  if (!ok) { console.log(`  [FAIL] ${name}: 缺 ${missing.join(',')} 或内容过短(${html.length})`); fail++; }
  else console.log(`  [OK]   ${name} (${html.length} 字符)`);
}
storageAvailable = false;
vm.runInContext('_storageCache.data = null', sandbox);
const storageError = await sandbox.pageStorage();
if (storageError.includes('盘点失败：mock backend unavailable') && storageError.includes('storage-rescan')) {
  console.log('  [OK]   storage offline error and retry');
} else {
  console.log('  [FAIL] storage offline: 缺盘点失败原因或重试按钮'); fail++;
}
console.log('\n结果: ' + (fail === 0 ? '全部通过' : fail + ' 个失败'));
process.exit(fail === 0 ? 0 : 1);
})();
