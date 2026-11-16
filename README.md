# 天智五号（Tianzhi-5）

项目显示名称已于2026-10-28统一为“天智五号”。第三方运行框架与求解器署名保留；原目录、工具标识及GitHub仓库地址维持兼容。界面品牌插件见[integrations/tianzhi-brand](integrations/tianzhi-brand/README.md)。本次公开更新日期：2026-11-16。GitHub仓库地址维持兼容。

作者与维护者：**yangshaopeng（杨绍鹏）**

版本：后端/适配器 **1.8.2**，桌面界面 **1.1.2**，转换器 **1.7.0**。

配电网潮流计算 Agent：以 Harness 为专业任务主入口，使用独立 Python 专业核心和 OpenDSS 求解引擎。旧 Python/FastAPI、Electron 应用及其转换、批量与历史功能保留。

独立专业核心`0.13.0-p3`在P2不可变输入、设备/工况核对、逐节点/端口V/I/PQ及强制验证基础上，接入项目确认电压、逐导体负载及有功网损判据，生成同源HTML/JSON/CSV报告，并可只读对照冻结参考、导出校验后的完整离线报告ZIP。缺电压基值、设备额定或判据的项目明确未评估，不套用工程默认值；数值采集仍使用后端/适配器`1.8.2`。PV原生CIM及P2模型族、参数精度和失败参考边界保持。P3/A07已由用户认可[固定French17限定工程参考](docs/professional-framework/p3-reference-status.md)，P3在此范围收口；9个缺依据点仍未评估，`engineering_ready=false`。Harness0.1.3已完成实际装载，并在本轮workspace输入范围内核实导入、计算、报告及只读追问；原生附件专项仍未验；当前转入联网范围交付，本地模型与断公网流程按用户要求跳过。范围及操作见[专业核心说明](docs/professional-framework/README.md)、[项目工程校核](docs/professional-framework/project-engineering.md)及[参考对照](docs/professional-framework/reference-comparison.md)。

Harness目标版本为`0.2.1-rc.2`，天智五号插件`0.1.3-p4`已由官方CLI冷升级，12个安装文件与审查包一致，实际能力回显已核实。插件提供9项工具，其中新增附件元数据清单；当前本轮采用Harness现有工作区文件引用和`source=workspace`明确清单导入作为正式输入路径，不新增上传UI或重复升级。原生附件上传/导入专项仍未验，不将工作区路径成绩改称原生上传通过。

既有实际流程已验证能力/模型查询、case4计算与报告、原任务重启后只读追问、明确工作区文件导入至一次新计算/报告、同工作区不同会话的限定隔离、ENWL任务级在途取消与进程回收、取消后新任务以及多目标只读交错查询。ENWL仅用于软件验收，不扩大French17限定工程认可。受控后端中断后，原任务恢复为`interrupted`、无提交数值结果，原请求/尝试不变，恢复事件`automatic_replay=false`；旧任务、事件及产物保持，未自动重算。该结论不证明恰在`Solution.Solve`某条指令内部中断。原生附件候选、授权与分层验收见[Harness接入范围](docs/professional-framework/harness-integration.md)，配置及本地安装见[插件说明](integrations/deepseek-harness/README.md)。0.1.3已完成ENWL五文件工作区清单导入、一次新潮流和6产物HTTP字节核对；新任务为finished/converged_valid，criteria:null，工程状态not_assessed、engineering_ready=false。P4已在Windows本机、Harness工作区引用＋显式文件清单输入范围内完成本轮验收：报告打开、原任务指标追问、跨会话快照隐藏/四项拒绝及纯致谢样本已核对。原生附件上传/流导入仍未验，不能称原计划每项都通过，也不作为本轮新建上传UI的门槛。当前按用户2026-10-26确认的联网使用范围交付：Harness联网调用DeepSeek，天智五号专业服务在本机计算。本地模型部署与断公网业务验收已按用户要求跳过，不计通过；旧版保留供回退。

本源码版以 **Windows x64、Python 3.13、Node.js 24、OpenDSS COM** 为运行基线。本轮实际使用 Node.js **24.19.0**，桌面运行时锁定为 Electron **44.8.0**。以下 Node/Electron 基线用于旧版应用；Harness 专业入口的模型与安装步骤见联网版说明。

## 日常交互修复（0.1.5-daily）

0.1.4的真实用户单Master输入暴露了依赖未带入及HTTP422无具体原因的问题。当前0.1.5-daily修复版可在明确授权范围内沿入口字面引用收集同包依赖，给出缺文件或安全合同诊断；默认不扫描目录、不改模型。已用同一ENWL入口完成一次隔离真实COM与报告闭环，原文件哈希不变；该软件验证使用模拟Host授权，真实DSH界面中的简短请求仍按实际操作记录。详见[日常文件计算](docs/professional-framework/daily-use.md)。本机已通过官方CLI统一安装，17文件与审查包一致，Harness已重开；未代用户发送新的GUI计算请求，不改写原失败记录。

## 当前日常入口：Harness 联网版

使用 Harness 的“天智五号”模式作为专业任务入口：Harness 负责界面、会话、联网模型推理和工具调度，天枢负责本机 OpenDSS 计算、验证、工程校核及报告。

已配置本机打开桌面“启动天智五号”快捷方式；源码用户可运行 `node scripts/start-harness.js`，或直接打开已安装的 Harness。随后选择项目工作区及“天智五号”。启动器不自动选择模式、不启动旧聊天后端，也不安装或升级插件。首次部署按[联网版安装、使用与回退](docs/professional-framework/online-delivery.md)配置专业服务和插件；当前扩展源码与接口见[采购功能补齐](docs/professional-framework/procurement-extensions.md)，实际安装版本以能力查询为准。

输入采用工作区文件路径和明确依赖清单，导入后使用真实返回的 snapshot_id 计算，按 task_id 查询和打开报告。详见[使用说明](docs/使用说明.md)。默认联网模型可能接收对话、工具摘要和明确提供的数据；新增可选本地模型接口，实际是否闭环取决于所选提供方和验收，不自动宣称断公网通过。

旧 Electron 界面及其历史数据保留，项目回退入口为 `scripts/start-legacy.bat`。旧版表格转换、批量、历史比较和知识问答等不因 Harness 接入而自动迁移；见[验收与迁移清单](docs/professional-framework/online-delivery.md#验收与迁移清单)。以下 Electron 安装和运行说明供旧版使用。

## 旧版安装

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

## 旧版命令行示例（不调用模型）

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

## 打开旧版桌面界面

在上述 PowerShell 中执行，离线设置会传给桌面端启动的后端：

```powershell
Push-Location .\tianshu_ui
npm start
Pop-Location
```

也可在已配置环境的终端调用 `scripts/start-legacy.bat`。在“分析工作台”输入“计算 case4，重点看最低电压和网损”，完成后打开任务报告。

桌面端使用 `127.0.0.1:8000`，会复用该地址上已有的天枢后端。首次验收前先关闭其他版本，避免界面连接到旧服务。后端优先使用本项目 `dpf_agent/.venv`，无需激活虚拟环境。

## 能力与范围

- 原生 `.dss`、包含依赖文件的 `.zip`，以及 `.xlsx/.csv/.json/.m` 表格模型接入。
- 固定求解流程、独立验证、任务所属 COM 子进程、取消与异常回收。
- 批次持久化、逐项状态、失败项重试和重启后的手动恢复。
- 原生DSS按输入模型保留分相；XLSX/CSV/JSON有明确相别时可分相建模，无相别时按平衡三相等值。MATPOWER静态数据按支持的平衡等值口径转换；连续相移仅限平衡正序范围。
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
node verify_launcher.js
Pop-Location
```

自动回归与真实 OpenDSS 计算分别验收，检查结果以对应版本的验收记录为准。

独立的 `dpf_agent.evals.run_conversion_fidelity` / `dpf_agent.evals.run_eval` 官方数据评测需要使用者另行提供获授权的相应算例；这些资料不随源码提供，也不属于上述离线回归的结果。

详见 [项目结构与代码导航](docs/project-structure.md)、[使用说明](docs/使用说明.md)、[后端说明](dpf_agent/README.md) 和 [桌面端说明](tianshu_ui/README.md)。

已知问题与修复状态见[说明](docs/KNOWN_ISSUES.md)。

## 许可证与配置

项目源码采用 [Apache License 2.0](LICENSE)。第三方依赖、引擎、模型服务与用户自行提供的数据分别遵守其许可证或服务条款。

模型密钥由使用者自行配置，不随源码提供；会话、上传内容、日志和报告为本地运行数据。提交 Git 前检查待提交清单，避免将这些内容加入仓库。模型配置方法和数据位置见 [使用说明](docs/使用说明.md)。

文档日期：2026-10-26；整理：yangshaopeng。
