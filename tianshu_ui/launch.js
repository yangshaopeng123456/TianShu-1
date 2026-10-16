// 单入口启动器：清理开发环境注入的 NODE_OPTIONS，再启动 Electron。
// WorkBuddy 等宿主可能给 NODE_OPTIONS 注入 Node 专用参数，Electron 不接受这些参数。
const { spawn } = require('child_process');
const path = require('path');
const electron = require('electron');

const env = { ...process.env };
delete env.NODE_OPTIONS;
delete env.ELECTRON_RUN_AS_NODE;
const appDir = __dirname;
// Electron CLI 需要把应用目录作为参数传给 Electron 可执行文件，
// 不能依赖从 Node 入口脚本间接推断，否则 main.js 会被普通 Node 加载。
const electronArgs = [
  '--disable-gpu',
  '--disable-gpu-compositing',
  '--disable-gpu-sandbox',
  appDir,
];
const child = spawn(electron, electronArgs, { cwd: appDir, stdio: 'inherit', env });
child.on('exit', (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});
child.on('error', (err) => {
  console.error('Electron 启动失败:', err.message);
  process.exit(1);
});
