# 天智五号后端

Python 包名为 `dpf_agent`。默认通过 `OpenDSSEngine.DSS` COM 调用自行安装的 OpenDSS，每个完整任务使用所属子进程；同一任务的求解和复算保留原生上下文。

安装和离线首次运行请先看[根目录说明](../README.md)。所有下列命令均在本目录执行，并使用 `./.venv/Scripts/python.exe`。

运行桌面端时，该虚拟环境还供 `npm run setup:electron` 调用 Python 下载脚本，下载并校验锁定版本的 Electron；先完成后端依赖安装，再按[桌面端说明](../tianshu_ui/README.md)执行下载步骤。

## 独立专业核心

结构化脚本/API版本`0.13.0-p3`、后端/适配器`1.8.2`支持持久作业、幂等与取消/中断、不可变DSS及严格MATPOWER平衡子集导入、设备与工况核对、实际逐端导体V/I/PQ、全域库存及守恒检查。新任务冻结全局状态、开关导体状态及编译后的被动元件导纳，逐Solve和最终发布均核查。P3在上述数值门后应用项目明确提供的电压、逐导体Line/Transformer电流、附加变压器端口kVA及有功损耗判据；HTML、JSON与CSV由同一终态结果生成，并可[校验后导出完整离线ZIP](../docs/professional-framework/report-bundle.md)。受限四导体线路及固定支路范围见[原生输入](../docs/professional-framework/native-input.md)。

PV模式由原生CIM100导出绑定，指定PF/Q与原生限幅后的有功、无功分别核对；私有请求Q仍没有直接getter。高精度参数、未支持的逆变器行为、MATPOWER合成相别与历史解释器均有明确边界。旧请求摘要和历史结果保留；ProjectCriteria 2.0只用于明确工况的请求1.1。P3/A07已完成[固定French17限定工程参考认可](../docs/professional-framework/p3-reference-status.md)，9个缺依据点仍未评估；`engineering_ready=false`。Harness目标版本为`0.2.1-rc.2`，天智五号插件`0.1.3-p4`已由官方CLI冷升级，12个安装文件与审查包一致，实际能力回显已核实。插件提供9项工具，其中新增附件元数据清单；当前本轮采用Harness现有工作区文件引用和`source=workspace`明确清单导入作为正式输入路径，不新增上传UI或重复升级。原生附件上传/导入专项仍未验，不将工作区路径成绩改称原生上传通过。既有实际流程已验证能力/模型查询、case4计算与报告、原任务重启后只读追问、明确工作区文件导入至一次新计算/报告、同工作区不同会话的限定隔离、ENWL任务级在途取消与进程回收、取消后新任务以及多目标只读交错查询。ENWL仅用于软件验收，不扩大French17限定工程认可。受控后端中断后，原任务恢复为`interrupted`、无提交数值结果，原请求/尝试不变，恢复事件`automatic_replay=false`；旧任务、事件及产物保持，未自动重算。该结论不证明恰在`Solution.Solve`某条指令内部中断。0.1.3已完成ENWL五文件工作区清单导入、一次新潮流和6产物HTTP字节核对；新任务为finished/converged_valid，criteria:null，工程状态not_assessed、engineering_ready=false。P4已在Windows本机、Harness工作区引用＋显式文件清单输入范围内完成本轮验收：报告打开、原任务指标追问、跨会话快照隐藏/四项拒绝及纯致谢样本已核对。原生附件上传/流导入仍未验，不能称原计划每项都通过，也不作为本轮新建上传UI的门槛。当前按用户2026-10-26确认的联网使用范围交付：Harness联网调用DeepSeek，天智五号专业服务在本机计算。本地模型部署与断公网业务验收已按用户要求跳过，不计通过；旧版保留供回退。见[专业核心说明](../docs/professional-framework/README.md)、[P2范围](../docs/professional-framework/p2-scope.md)、[数据合同](../docs/professional-framework/contracts.md)、[工程判据](../docs/professional-framework/project-engineering.md)及[只读参考对照](../docs/professional-framework/reference-comparison.md)。

Harness接入通过独立回环服务复用本核心，不经过旧聊天入口。0.1.3保留工作区明确文件清单，并提供附件元数据和显式附件键分支；后者未因代码、纯测试或本轮workspace闭环而计原生附件实测通过。9项工具、可信归属、授权、停止与报告的范围见[Harness接入说明](../docs/professional-framework/harness-integration.md)，本机配置见[插件README](../integrations/deepseek-harness/README.md)。

## CLI

```powershell
$env:DPF_ENGINE_BACKEND = 'com'
$env:DPF_LLM_ENABLED = '0'
$env:DPF_RAG_ENABLED = '0'
$env:DPF_POWER_RAG_ENABLED = '0'
& .\.venv\Scripts\python.exe -m dpf_agent --llm off run --dss case4 -m '计算case4，重点看最低电压和网损'
& .\.venv\Scripts\python.exe -m dpf_agent inspect --task '<task_id>'
& .\.venv\Scripts\python.exe -m dpf_agent report --task '<task_id>' --format md,html
```

把 `<task_id>` 替换为实际任务 ID；`report` 命令定位已经生成的报告。`--llm` 是顶级选项，必须放在 `run` 前。`--dss` 可以是 `cases/` 下的目录名，也可以是用户自己的 `Master.dss` 路径。

正常任务默认会生成报告和结构化验证结果。终端退出码包括：`0` 成功、`20` 请求无效、`21` 数据不可用、`30` 不收敛、`40` 验证失败、`41` 已取消、`50` 系统失败。失败任务可保留诊断材料，不应作为合格计算结果使用。

## Web

```powershell
& .\.venv\Scripts\python.exe -m dpf_agent web --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000/`；API 描述位于 `/docs`，存活检查为 `/api/health`，真实可用案例清单为 `/api/cases`。仅需要后端时可自行选择其他端口；桌面端默认管理的地址固定为 `127.0.0.1:8000`。

当前接口面向本机使用，没有提供公网多用户认证部署方案。需要远程共享时应另行配置访问控制与隔离，不能直接把本机服务当作互联网服务公开。

## 配置

环境变量在启动进程前设置，`.env.example` 是格式示例；程序不会自动把示例文件加载为配置。

| 变量                                          | 默认行为/用途                               |
| ------------------------------------------- | ------------------------------------- |
| `DPF_ENGINE_BACKEND`                        | `com`；替代通道为 `cli`、`dss-python`，兼容性需另测 |
| `DPF_COM_ISOLATION`                         | `process`，任务进程隔离                      |
| `DPF_LLM_ENABLED`                           | `1`；离线设置 `0`                          |
| `DPF_LLM_BASE_URL`                          | OpenAI 兼容接口，默认本地 Ollama 地址            |
| `DPF_LLM_MODEL` / `DPF_LLM_API_KEY`         | 使用者自己的模型名/密钥                          |
| `DPF_RAG_ENABLED` / `DPF_POWER_RAG_ENABLED` | 本地/外部检索开关；无知识数据的演示均设 `0`              |
| `DPF_CASES`                                 | 默认本目录 `cases/`                        |
| `DPF_WORKSPACE` / `DPF_OUTPUTS`             | 默认本目录 `tasks/`、`outputs/`             |
| `DPF_CHAT_DB_DIR`                           | 默认本目录 `chat_data/`，存放 SQLite 会话与上传副本  |

模型、知识库或外部检索未启用时，对应就绪度卡可能显示警告；独立的 COM 示例仍可运行。模型就绪度主要表示配置存在，真实可达性需实际调用验证。

## 示例与测试

`case4`是首次运行推荐示例。其他自建目录用于变压器、应力、缺依赖及设备提取探针；可用名字以/api/cases或实际存在的cases/*/Master.dss为准。开发树和精简发布树的资料范围不同，IEEE和知识资料按实际目录提供，不因公共代码相同而自动进入发布范围。

```powershell
& .\.venv\Scripts\python.exe -m dpf_agent.tests.run_all
```

这是离线回归入口，包含受控引擎替身及部分实际操作系统进程；可选官方资料缺失时，相关检查会明确跳过。真实COM、实际桌面交互及真实模型调用须分别验证，记录对应版本、任务、输入身份和产物。通过命令行入口执行默认COM，不代表OpenDSS CLI替代引擎已验收。

`release_gate --mode preflight` 不构成完整交付验收；最终模式还要求实际原生引擎证据和交付树比对。

`python -m dpf_agent.evals.run_conversion_fidelity` 与 `python -m dpf_agent.evals.run_eval` 是独立评测入口，其中官方数据评测需使用者自行提供获授权的相应算例；源码未附这些资料，未将其作为首轮离线回归已验证内容。

完整的使用、上传及模型配置见[使用说明](../docs/使用说明.md)。

文档日期：2026-10-26；整理：yangshaopeng。

本版在system结果域增加Load、Generator、PVSystem原生端口观测。AllElementNames与三类Count核对库存，逐导体核对V/I/PQ及母线节点；P/Q正号流入设备，保留中性线/非标准节点事实，不将设定值或绕组额定量混作输出。当前system请求缺失、矛盾或不完整时不发布有效数值结果；旧已提交任务不重写。停用PCE只输出无电气数值摘要。新1.3仅在显式源false与可信原生false一致且NodeOrder为合法不适用标记时免读电气节点映射，其余参数仍须核对；Load/Generator标称kV采用typed double，PV仅六位文本一致，不能宣称全精度。详见[观测说明](../docs/professional-framework/observations.md)。
