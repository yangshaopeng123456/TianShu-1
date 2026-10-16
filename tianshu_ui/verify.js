// 渲染验证脚本：无头加载三页，检查渲染与导航，捕获 JS 错误
const { app, BrowserWindow } = require('electron');
const path = require('path');

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 1440, height: 900, show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false,
    },
  });
  const errors = [];
  win.webContents.on('console-message', (e, level, message) => {
    if (level >= 2) errors.push('console[' + level + '] ' + message);
  });
  win.webContents.on('render-process-gone', (e, d) => errors.push('render-gone: ' + JSON.stringify(d)));

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const evaljs = (code) => win.webContents.executeJavaScript(code);

  try {
    await win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
    await sleep(600);

    const cfg = await evaljs(`({
      nav: document.querySelectorAll('#nav .nav-item').length,
      title: document.querySelector('.tb-center').textContent.trim(),
      subnav: document.querySelectorAll('#subnav .subnav-item').length,
      savebar: !document.getElementById('savebar').classList.contains('hidden'),
      crumb: document.getElementById('sb-crumb').textContent
    })`);
    console.log('系统配置:', JSON.stringify(cfg));

    await evaljs(`document.querySelector('#nav .nav-item[data-page="workbench"]').click()`);
    await sleep(300);
    const wb = await evaljs(`({
      sessions: document.querySelectorAll('.session-item').length,
      trace: document.querySelectorAll('.trace-step').length,
      chats: document.querySelectorAll('.chat-bubble').length,
      crumb: document.getElementById('sb-crumb').textContent
    })`);
    console.log('分析工作台:', JSON.stringify(wb));

    await evaljs(`document.querySelector('#nav .nav-item[data-page="results"]').click()`);
    await sleep(300);
    const rs = await evaljs(`({
      cards: document.querySelectorAll('.status-card').length,
      tables: document.querySelectorAll('table').length,
      crumb: document.getElementById('sb-crumb').textContent
    })`);
    console.log('结果展示:', JSON.stringify(rs));

    // 回到配置页，抽查两个子页面
    await evaljs(`document.querySelector('#nav .nav-item[data-page="config"]').click()`);
    await sleep(200);
    await evaljs(`document.querySelector('#subnav .subnav-item[data-sub="engine"]').click()`);
    await sleep(200);
    const eng = await evaljs(`document.querySelector('#page-body table') ? document.querySelectorAll('#page-body table').length : 0`);
    console.log('引擎中心子页表格数:', eng);

    console.log('JS 错误数:', errors.length);
    errors.forEach((e) => console.log('  ERR', e));
  } catch (err) {
    console.log('执行异常:', err.message);
  }
  app.quit();
});
