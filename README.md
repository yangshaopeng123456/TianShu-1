# 天枢一号（Tianshu-1）

作者与维护者：**yangshaopeng（杨绍鹏）**

配电网潮流计算智能体：Python/FastAPI 后端、Electron 桌面界面和 OpenDSS 求解引擎。支持自然语言任务、算例上传、批量计算、任务取消、历史查询，以及带验证结果的 HTML/Markdown/JSON/CSV 报告。

本源码版以 **Windows x64、Python 3.13、Node.js 24、OpenDSS COM** 为运行基线。本轮实际使用 Node.js **24.19.0**，桌面运行时锁定为 Electron **44.8.0**。模型服务可选；建议先完成下面的离线示例，再配置模型。

## 安装

1. 自行安装 [OpenDSS](https://sourceforge.net/projects/electricdss/) 的 Windows x64 发行版，并确保 `OpenDSSEngine.DSS` COM 组件已注册。引擎安装包、DLL 和运行环境不随源码提供。
2. 安装 Python 3.13 x64 和 Node.js 24；确认 PowerShell 可执行 `py -3.13`、`node` 和 `npm`。
3. 在本项目根目录打开 PowerShell，执行：

```powershell
py -3.13 -m venv dpf_agent/.venv
& .\dpf_agent\.venv\Scripts\python.exe -m pip install -r .\dpf_agent\requirements-windows-py313.lock.txt
& .\dpf_agent\.venv\Scripts\python.exe -c "import win32com.client; dss=win32com.client.Dispatch('OpenDSSEngine.DSS'); print(dss.Version)"
Push-Location .\tianshu_ui
npm ci
npm run setup:electron
Pop-Location
```

`npm ci` 安装 JavaScript 依赖；`npm run setup:electron` 另行联网下载 Electron 运行时，不能省略。该命令通过 Node 启动器调用前面创建的 Python 虚拟环境运行 [下载脚本](scripts/install-electron.py)，按本机 npm 包的锁定版本从 Electron 官方 GitHub Release 下载，并校对包内提供的 SHA256；已有校验通过的缓存可复用。Python 标准 HTTPS 会使用本机系统代理设置。下载失败时按脚本提示检查访问 GitHub 的网络/代理后重试，无需把代理地址或凭据写进源码、命令记录或问题反馈。首次安装 Python/npm 依赖和桌面运行时均需可用的下载网络；完成后离线示例不需要模型服务。

锁定文件用于 Windows x64/Python 3.13 的依赖安装；其他平台或 Python 版本需重新验证。需要自行选择依赖版本时，可安装 `dpf_agent/requirements-com.txt`；`requirements.txt` 另含 `dss-python` 替代引擎依赖，首次运行无需安装它。

## 先跑一个离线示例

继续在项目根目录执行：

```powershell
$env:DPF_ENGINE_BACKEND = 'com'
$env:DPF_COM_ISOLATION = 'process'
$env:DPF_LLM_ENABLED = '0'
$env:DPF_RAG_ENABLED = '0'
$env:DPF_POWER_RAG_ENABLED = '0'
Push-Location .\dpf_agent
& .\.venv\Scripts\python.exe -m dpf_agent --llm off run --dss case4 -m '计算case4，重点看最低电压和网损'
Pop-Location
```

`case4` 是自建的四节点径向演示模型。成功时终端输出任务 ID 和产物目录，报告位于 `dpf_agent/outputs/<task_id>/`。报告中的 `success` 表示计算与相应验证完成，电压是否合格仍需查看具体判据。

## 打开桌面界面

在上述 PowerShell 中执行，离线设置会传给桌面端启动的后端：

```powershell
Push-Location .\tianshu_ui
npm start
Pop-Location
```

也可在已配置环境的终端调用 `scripts/启动天枢一号.bat`。在“分析工作台”输入“计算 case4，重点看最低电压和网损”，完成后打开任务报告。

桌面端使用 `127.0.0.1:8000`，会复用该地址上已有的天枢后端。首次验收前先关闭其他版本，避免界面连接到旧服务。后端优先使用本项目 `dpf_agent/.venv`，无需激活虚拟环境。

## 能力与范围

- 原生 `.dss`、包含依赖文件的 `.zip`，以及 `.xlsx/.csv/.json/.m` 表格模型接入。
- 固定求解流程、独立验证、任务所属 COM 子进程、取消与异常回收。
- 批次持久化、逐项状态、失败项重试和重启后的手动恢复。
- 原生 DSS 可描述三相不平衡模型；表格模型转换采用声明的平衡三相等值口径。连续相移限于平衡正序支持范围。
- 不提供概率潮流、自动重构、自动调度或生产电网安全认证。收敛、验证通过与设备运行合格是不同判据。

本仓库附带 7 个自建演示/故障探针目录；`case_broken` 用于验证缺失依赖，`case_nonconv` 为应力模型，均不应当作正常网络基准。**不附带 IEEE 原始馈线资料、原始知识库、第三方教材 PDF、个人办公文档或历史任务数据。** 自带示例不代表通过 IEEE 标准算例认证。

## 验证与进一步使用

```powershell
Push-Location .\dpf_agent
& .\.venv\Scripts\python.exe -m dpf_agent.tests.run_all
Pop-Location
Push-Location .\tianshu_ui
node verify_races.js
node verify_backend_lifecycle.js
node verify_render.js
node verify_batch.js
Pop-Location
```

本轮已在当前机器新建虚拟环境，安装 31 个 Python 包并通过 `pip check`；首轮 37 套后台回归和上面 4 项 Node 检查通过。Electron 44.8.0 依赖锁定后的 `npm audit` 在本轮检查中报告 0 项已知漏洞；这是检查当时的依赖结果，不代表所有安全风险均已排除。

后台回归包含受控替身和实际操作系统子进程检查；上述回归通过不等于已运行真实 OpenDSS 数值验收。本轮另用 CLI 完成 `case4`、`case_tx` 两项计算，并从全新空白用户配置启动实际 Electron 44.8.0 窗口，点击新建会话、输入并提交 `case4`。共 **3 个真实 COM 完整任务**均为 `success`、验证通过，每项包含独立复算；桌面任务报告 HTTP 200，显式指定未附带的 `ieee33` 得到缺失提示，无页面错误，关闭窗口后所属后台正常回收。

这些是当前机器新建虚拟环境及空白桌面配置的结果，不能替代全新 Windows 系统、其他硬件和其他引擎版本的验证。

独立的 `dpf_agent.evals.run_conversion_fidelity` / `dpf_agent.evals.run_eval` 官方数据评测需要使用者另行提供获授权的相应算例；这些资料不随源码提供，也不属于上述离线回归的结果。

详见 [使用说明](docs/使用说明.md)、[后端说明](dpf_agent/README.md) 和 [桌面端说明](tianshu_ui/README.md)。

## 许可证与配置

项目源码采用 [Apache License 2.0](LICENSE)。第三方依赖、引擎、模型服务与用户自行提供的数据分别遵守其许可证或服务条款。

模型密钥由使用者自行配置，不随源码提供；会话、上传内容、日志和报告为本地运行数据。提交 Git 前检查待提交清单，避免将这些内容加入仓库。模型配置方法和数据位置见 [使用说明](docs/使用说明.md)。

文档日期：2026-10-16；整理：yangshaopeng。
