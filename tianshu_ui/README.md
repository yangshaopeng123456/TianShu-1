# 天智五号桌面端

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

模型不是运行case4所必需。需要启用时，先退出应用，在新的PowerShell中将DPF_LLM_ENABLED设为1，再运行npm start。在“系统配置→模型服务”填写接口地址、模型名和密钥；字段和云端/本地模式自动保存，任务空闲时尝试重启本窗口所属后端使配置生效。

复用外部服务时明确提示尚未应用，不替外部进程重启。连接检查按当前模式读取模型列表，不等于实际推理或结构化输出验收。桌面配置保存到Electron用户数据目录，Windows默认通常为`%APPDATA%/tianshu-1/settings.json`。密钥写入本机文件且未加密，显示遮挡只影响屏幕；请勿提交配置文件或含密钥的截图。已保存模型配置优先于同名启动环境变量；DPF_LLM_ENABLED=0仍可关闭模型调用。

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
node verify_launcher.js
```

这些脚本使用受控环境检查界面逻辑，部分检查实际启动独立进程；它们不能替代当前源码的真实桌面交互或OpenDSS数值验收。源码启动、全新机器安装和已构建安装包是不同验证范围。

系统配置保留六页：总览、模型服务、运行能力、知识库、本地存储、关于与诊断。状态显示以真实接口为依据；引擎已注册不等于求解通过。未实现的预算/策略/外观/报告偏好和自动更新等控件已移除。工作台及结果页的真实任务操作、报告和下载入口保留。关于页仅打开固定应用日志目录，不将日志原文标为已脱敏。

完整验收应绑定当前版本、输入、任务和实际产物；受控页面渲染、真实窗口操作、真实COM计算分别记录。源码未附已安装的桌面发行包；npm start运行本地源码。

更多操作见[使用说明](../docs/使用说明.md)。

文档日期：2026-10-18；整理：yangshaopeng。
