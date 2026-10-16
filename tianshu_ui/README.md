# 天枢一号桌面端

Electron 桌面界面，通过主进程桥接访问本机 FastAPI 后端。本轮使用 Node.js **24.19.0**，Electron 精确锁定为 **44.8.0**。后端计算需要 Python 依赖和自行安装、注册的 OpenDSS COM；单独安装 npm 依赖不会安装求解引擎。

## 安装与启动

先完成[根目录安装步骤](../README.md)，在本目录执行：

```powershell
npm ci
npm run setup:electron
$env:DPF_ENGINE_BACKEND = 'com'
$env:DPF_LLM_ENABLED = '0'
$env:DPF_RAG_ENABLED = '0'
$env:DPF_POWER_RAG_ENABLED = '0'
npm start
```

`npm run setup:electron` 通过 Node 启动器调用项目后端的 `.venv` Python 执行 `../scripts/install-electron.py`，联网下载锁定版本的 Electron 运行时，并核对 npm 包内提供的 SHA256；已有校验通过的缓存可复用。该版本的 npm 包不再通过安装后钩子自动完成下载，所以仅执行 `npm ci` 不足以启动桌面窗口。下载器使用 Python 标准 HTTPS/本机系统代理支持；失败时按提示检查网络并重试，请勿将代理地址或凭据复制到源码、日志反馈或截图。

`npm start` 调用 `launch.js` 清理干扰 Electron 的宿主环境变量，再启动桌面应用。主进程优先运行 `../dpf_agent/.venv/Scripts/python.exe`，自动启动和等待后端。窗口关闭时结束由本窗口启动的后端。

默认地址为 `http://127.0.0.1:8000`。如果该地址上已有天枢后端，应用会复用现有服务；验收当前源码前先关闭其他版本。自行启动的后端不归桌面窗口所有，关闭窗口不会替你终止该服务。

首次演示在“分析工作台”输入“计算 case4，重点看最低电压和网损”。完成后从任务卡打开报告；不要根据界面文字中的示例数值判断真实求解结果。

## 模型配置

模型不是运行 `case4` 所必需。需要启用时，先退出应用，在新的 PowerShell 中将 `DPF_LLM_ENABLED` 设为 `1`，再运行 `npm start`。随后在“系统配置 → 模型服务”填写自己的兼容接口地址、模型名和密钥，点击“保存并重启后端”。

桌面配置保存到 Electron 的用户数据目录，Windows 默认通常为 `%APPDATA%/tianshu-1/settings.json`。密钥会写入本机配置文件，界面默认遮挡显示；请勿提交配置文件或含密钥的截图。设置中已保存的模型地址、模型名和密钥优先于同名启动环境变量；`DPF_LLM_ENABLED=0` 仍可关闭模型调用。

## 文件与排错

- Electron 配置和日志位于用户数据/日志目录，不需要在源码中填写本机绝对路径。
- 后端会话、上传、任务和报告默认分别在 `../dpf_agent/chat_data/`、`tasks/` 和 `outputs/`；不随源码提供。
- 提示 Python 模块缺失：确认依赖安装到 `../dpf_agent/.venv`，而不是其他解释器。
- 提示 COM 无效类字符串：使用根目录说明中的 `OpenDSSEngine.DSS` 检验，检查 x64 引擎安装和注册。
- 界面连到旧版本：先检查 `127.0.0.1:8000` 是否已有其他天枢服务，再重新启动。
- 不要直接用 `node main.js` 启动 Electron。

## 界面逻辑检查

```powershell
node verify_races.js
node verify_backend_lifecycle.js
node verify_render.js
node verify_batch.js
```

本轮这 4 项 Node 检查均已通过，升级锁定后的 `npm audit` 报告 0 项已知漏洞。检查结果仅对应本轮锁定的依赖版本与当时的公告数据库。

这些脚本使用受控环境验证界面逻辑，不能替代实际桌面交互或 OpenDSS 数值验收。本轮另外在实际 Electron 44.8.0 窗口以空白用户配置启动，由应用自动启动所属后端，完成点击工作台、新建会话、输入并提交 `case4`：任务成功、验证通过、报告 HTTP 200；未附带的 `ieee33` 清楚提示缺失，页面错误数为 0。退出窗口后 Electron 及所属 8000 后台正常回收。

后端 CLI 的 `case4`、`case_tx` 加上桌面 `case4`，合计 3 个真实 COM 完整任务，每项含独立复算。验收仅覆盖当前机器的新建虚拟环境和空白桌面配置，不等于全新 Windows 系统或其他硬件验收。源码未提供已安装的桌面发行包；`npm start` 运行的是本地源码。

更多操作见[使用说明](../docs/使用说明.md)。

文档日期：2026-10-16；整理：codex。
