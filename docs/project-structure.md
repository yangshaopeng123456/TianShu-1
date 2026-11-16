# 天智五号项目结构与代码导航

日期：2026-10-26。作者与维护者：yangshaopeng（杨绍鹏）。

本文合并原`CODE_MAP.md`与`PROJECT_STRUCTURE.md`的导航职责，以当前实际代码和目录为准。旧说明作为历史资料保留，旧行数、文件数、测试数量和早期开源准备状态不再用来描述当前版本。

## 1. 当前版本与入口

| 部分 | 版本 | 代码位置 |
| --- | --- | --- |
| 后端与OpenDSS适配器 | 1.8.2 | `dpf_agent/dpf_agent/__init__.py`及`engines/` |
| 原桌面界面 | 1.1.2 | `tianshu_ui/` |
| 表格/MATPOWER转换器 | 1.7.0 | `dpf_agent/dpf_agent/web/conversion_cache.py`中的版本声明 |
| 独立专业核心 | 0.13.0-p3 | `dpf_agent/dpf_agent/professional/` |
| Harness原生bundle | 0.1.3-p4，9项工具，已冷升级并核实实际装载；本轮workspace五文件闭环已完成计算/HTTP报告，原生附件仍未验 | `integrations/deepseek-harness/` |

原版桌面功能与独立专业核心共存在同一项目中，复用部分专业计算代码。专业核心版本号不表示整体项目完成比例。Harness目标版本为`0.2.1-rc.2`，天智五号插件`0.1.3-p4`已由官方CLI冷升级，12个安装文件与审查包一致，实际能力回显已核实。插件提供9项工具，其中新增附件元数据清单；当前本轮采用Harness现有工作区文件引用和`source=workspace`明确清单导入作为正式输入路径，不新增上传UI或重复升级。原生附件上传/导入专项仍未验，不将工作区路径成绩改称原生上传通过。实际已验范围包括工作区明确导入、历史只读查询、限定会话隔离、任务级在途取消、中断后恢复且不自动重放；本轮0.1.3五文件workspace导入—一次新计算—6产物HTTP核对已完成，本轮Windows/workspace范围报告/追问/隔离/致谢样本完成验收，native专项仍未验单列。范围见[Harness接入](professional-framework/harness-integration.md)。

首次使用见[根README](../README.md)和[使用说明](使用说明.md)；专业接口及已接通范围见[专业核心说明](professional-framework/README.md)。

## 2. 顶层目录

| 目录或文件 | 当前职责 |
| --- | --- |
| `dpf_agent/` | Python后端工程，包含源码、依赖声明、内置算例、知识资料及本机运行数据 |
| `tianshu_ui/` | Electron桌面界面、后端生命周期管理及界面检查脚本 |
| `integrations/deepseek-harness/` | Harness原生Host工具、专用预设、可信归属/授权/停止及报告桥接；配置见[插件README](../integrations/deepseek-harness/README.md) |
| `scripts/` | Windows启动入口、Electron运行时安装脚本、无模型演示脚本 |
| `docs/` | 当前使用说明、项目结构、已知问题及专业核心合同/语义说明 |
| `README.md` | 项目介绍、安装、启动与能力范围入口 |
| `.env.example` | 参数参考示例；程序不自动加载此文件 |
| `.gitignore`、`.gitattributes` | Git忽略规则及原始字节处理规则 |
| `LICENSE`、`NOTICE` | 项目许可与版权归属声明 |
| `SECURITY.md`、`THIRD_PARTY_NOTICES.md` | 本机数据边界、第三方依赖和资料来源说明 |

开发树还保留以下材料，发布树按开源范围选取必要子集：

| 开发树目录 | 当前用途 |
| --- | --- |
| `算例归档/ieee_official/` | 公开参考算例、原始DSS及参考输出；当前包含13Bus、37Bus、4Bus-GrdYD-Bal、IEEE 30 Bus相关资料 |
| `天枢一号_文档/` | 原有源文档、办公文档与内部台账；其中历史结论应按其日期理解 |
| `开发归档/2026-10-21_历史资料整理/` | 完整保留旧助手记录、原发布树独有资料和两份旧结构说明；路径映射由同目录JSON清单记录。该目录不进入公开源码范围 |

`tianshu-development`承担完整开发职责；相邻的`tianshu-release`承担精简的本地公开发行准备职责。日常只同步功能及必要说明，向GitHub发布须由用户明确提出。资料归档不是另一套独立维护的业务源码。

## 3. 后端工程的两层同名目录

```text
dpf_agent/                         后端工程根
├─ dpf_agent/                      实际Python包
│  ├─ application/                 计算事务编排
│  ├─ domain/                      领域数据、能力边界与归一化
│  ├─ engines/                     OpenDSS适配、进程隔离和原生采集
│  ├─ professional/                独立专业服务
│  ├─ runtime/                     意图、模型、守卫、取消及诊断
│  ├─ schemas/                     任务、结果、证据及验证合同
│  ├─ skills/、tools/              原有流程与专业工具
│  ├─ verification/、evidence/     强制数值验证与证据
│  ├─ renderers/                   原报告渲染
│  ├─ web/、static/                原HTTP入口及兼容网页
│  ├─ rag/、prompts/、knowledge/   原检索组件、提示词和知识元数据
│  ├─ cli/                        原命令行入口
│  └─ tests/、evals/               自动回归与离线评测
├─ cases/                          内置算例及功能/故障探针
├─ knowledge/                      领域Markdown资料
├─ docs/、project_docs/             后端既有接口与项目材料
├─ requirements*.txt               依赖声明及Windows锁定清单
└─ .venv/、tasks/、outputs/等        安装或运行后存在的本机目录
```

`dpf_agent/knowledge/`存放领域资料；包内`dpf_agent/dpf_agent/knowledge/knowledge_items.json`存放知识条目元数据，两者用途不同。发布树不因开发树保留材料就自动附带全部知识资料。

## 4. 修改功能时去哪里找

以下路径相对于`dpf_agent/dpf_agent/`，桌面路径单独列出。

| 功能 | 主要文件或模块 |
| --- | --- |
| 旧聊天、上传、历史查询 | `web/chat_api.py`、`web/chat_store.py`及`web/`中的接口 |
| XLSX/CSV/JSON/MATPOWER转换 | `web/case_converters.py`、`web/conversion_cache.py` |
| 原计算事务与结构化复用入口 | `application/service.py` |
| 原固定工作流与工具 | `skills/powerflow_skill.py`、`tools/powerflow_tools.py` |
| 网络画像、结果归一、能力与诊断 | `domain/` |
| COM求解与任务所属进程 | `engines/opendss_com_adapter.py`、`engines/opendss_process_adapter.py` |
| 原生端口、设备和功率配置读回 | `engines/terminal_observations.py`、`engines/device_connections.py`、`engines/power_setpoints.py`、`engines/pce_observations.py`、`engines/voltage_ratings.py` |
| 数值验证、复算及证据 | `verification/verifier.py`、`evidence/builder.py` |
| 原报告 | `renderers/markdown_renderer.py` |
| 旧意图、模型调用、过程说明 | `runtime/intent.py`、`runtime/model_client.py`、`runtime/narration.py` |
| 取消和守卫 | `runtime/cancellation.py`、`runtime/guard.py` |
| 批次与存储管理 | `web/batch_scheduler.py`、`web/batch_store.py`、`web/storage_admin.py` |
| 原知识检索 | `rag/`、`tools/rag_tools.py` |
| 配置与归档路径候选 | `config.py`、`paths.py` |
| 桌面启动与后端管理 | `tianshu_ui/main.js`、`tianshu_ui/backend_lifecycle.js` |
| 桌面页面、按钮和样式 | `tianshu_ui/renderer/app.js`、`index.html`、`styles.css`；均位于`renderer/`中 |

COM进程隔离是当前专业服务使用的计算方式；旧CLI及可选dss-python通道的适用范围和验证情况分别管理，不能因为代码存在就认为所有通道等价验收。

## 5. 独立专业核心

| 文件 | 当前作用 |
| --- | --- |
| `professional/cli.py`、`api.py` | 独立命令行及带令牌的本机回环API |
| `professional/service.py` | 专业作业组织、强制验证及结果提交 |
| `professional/contracts.py` | 版本化请求、工况、观测、结果及判据合同 |
| `professional/jobs.py` | SQLite作业、幂等、事件、取消与中断 |
| `professional/models.py` | 支持范围内的原生DSS文本包检查及不可变模型快照 |
| `professional/device_semantics.py`、`device_replay.py` | 显式设备语义及有限属性顺序重放 |
| `professional/power_point_semantics.py` | Load/Generator命令顺序及PV名义功率配置的纯解释，不重建逆变器输出 |
| `professional/power_point_verification.py` | 功率参数来源/身份与原生读回核对；Generator基准P/Q仅6位文本一致，PV源模式/请求Q不核对 |
| `professional/device_verification.py` | 声明与真实运行期字段核对 |
| `professional/observations.py` | 原生节点、端口和导体观测及覆盖说明 |
| `professional/assessment.py` | 兼容旧EngineeringCriteria的精确Measurement规则 |
| `professional/project_criteria.py`、`engineering.py` | 项目判据2.0、节点/端口精确选择、工程量与覆盖校核 |
| `professional/reporting.py` | 最终权威结果的同源HTML/校核CSV/工程CSV及字符串公式防护 |
| `professional/reference_comparison.py` | 已提交工程量与显式误差合同的只读参考比较，不重新求解 |

专业入口复用`ApplicationService(..., structured_only=True).run_structured()`。该路径不构造旧`ModelClient`，不进行LLM/RAG推理，也不写旧聊天库。求解器、固定事务、守卫、验证和证据仍属于专业后端职责。

设备声明1.3附`ordered_replay` 1.1与`power_operating_points` 1.0；原生schema仍1.0，含`power_setpoints` 1.0和`voltage_rating` 1.0。旧声明1.0/1.1/1.2保留原范围。Load/Generator标称kV采用typed double；PV标称kV及Generator基准P/Q只作原生六位文本一致。PCE启停按源顺序核对，只有可信双false和合法标记才能省略停用电气NodeOrder，其他参数仍核对；system结果域继续独立校验实际V/I/PQ和库存。工程校核、完整设备语义及其他设备电压精度仍有边界，见专业核心说明。

旧版已有的表格转换、历史问答、批次和检索，不代表这些能力已经迁入独立专业接口。当前支持项及拒绝边界以[专业核心说明](professional-framework/README.md)、[请求合同](professional-framework/contracts.md)、[导入说明](professional-framework/native-input.md)为准。

## 6. 启动链路与数据

原桌面链路：`scripts/启动天枢一号.bat` → `tianshu_ui/start-tianshu.bat` → `launch.js` → Electron `main.js`及`backend_lifecycle.js` → Python `dpf_agent.web.server`。桌面后端使用`127.0.0.1:8000`，会复用该地址已有的天枢后端；优先使用本项目`dpf_agent/.venv`。

原命令行：在后端工程目录执行`python -m dpf_agent`，由`cli/main.py`处理。独立专业入口为`python -m dpf_agent.professional.cli --state-dir <独立目录> ...`，其API只监听本机回环地址。安装、启动和示例命令见[根README](../README.md)，无需从历史文档复制旧绝对路径。

| 位置 | 保存内容与处理原则 |
| --- | --- |
| `dpf_agent/cases/` | 内置模型和探针；`case4`为自建演示，故障探针不能当正常标准基准 |
| `dpf_agent/tasks/`、`outputs/` | 任务输入、中间产物和报告；历史任务可能包含唯一原始算例，不能整目录当缓存清空 |
| `dpf_agent/chat_data/`、`logs/` | 会话、上传副本和日志，属于本机运行数据 |
| 专业核心指定的独立状态根 | 作业库、模型/快照、运行目录、已提交产物及服务启动资料，不混用旧聊天或任务目录 |
| `dpf_agent/.venv/`、`tianshu_ui/node_modules/` | 本机依赖；可重建，但删除后需重新安装才能恢复相应运行 |
| `__pycache__/`、`*.pyc` | Python可再生缓存，与原始模型和结果资料区分 |

`paths.py`按存在性选择开发树的`算例归档/`与`天枢一号_文档/源文档/`，并兼容发布布局的`examples/cases/`及`docs/03-验证报告/`等候选。移动这些仍使用的输入/输出目录须同步处理路径；2026-10-21的目录整理只涉及旧助手记录、补充资料和结构说明，本轮功率配置更新也未改变这些计算路径。

## 7. 验证和能力状态

自动回归入口为`python -m dpf_agent.tests.run_all`；前端检查脚本及安装基线见[根README](../README.md)。回归、实际OpenDSS求解、桌面点击和DSH端到端使用分别验收，目录整理或链接检查不替代这些验收。

当前专业核心仍为`engineering_ready=false`。既有实际流程已验证能力/模型查询、case4计算与报告、原任务重启后只读追问、明确工作区文件导入至一次新计算/报告、同工作区不同会话的限定隔离、ENWL任务级在途取消与进程回收、取消后新任务以及多目标只读交错查询。ENWL仅用于软件验收，不扩大French17限定工程认可。受控后端中断后，原任务恢复为`interrupted`、无提交数值结果，原请求/尝试不变，恢复事件`automatic_replay=false`；旧任务、事件及产物保持，未自动重算。该结论不证明恰在`Solution.Solve`某条指令内部中断。0.1.2两个意图边界样本保留其原版本限定回归结论，当前实际装载为0.1.3；0.1.1纯致谢复述旧任务的失败记录保留，提示词规则不作为确定性安全隔离。0.1.3已完成ENWL五文件工作区清单导入、一次新潮流和6产物HTTP字节核对；新任务为finished/converged_valid，criteria:null，工程状态not_assessed、engineering_ready=false。P4已在Windows本机、Harness工作区引用＋显式文件清单输入范围内完成本轮验收：报告打开、原任务指标追问、跨会话快照隐藏/四项拒绝及纯致谢样本已核对。原生附件上传/流导入仍未验，不能称原计划每项都通过，也不作为本轮新建上传UI的门槛。当前按用户2026-10-26确认的联网使用范围交付：Harness联网调用DeepSeek，天智五号专业服务在本机计算。本地模型部署与断公网业务验收已按用户要求跳过，不计通过；旧版保留供回退。

P3的`engineering_publishable`仅表示存在可发布的已评估项目项，范围固定为`evaluated_project_checks_only_not_engineering_acceptance`；不代表全网、热模型或工程验收通过。缺依据、缺额定或覆盖不完整继续未评估。IEEE4/13及浮置YD历史具体失败保持；A07已取得[固定French17限定工程参考认可](professional-framework/p3-reference-status.md)，P3在该范围收口；9个缺依据点及历史失败保留。ENWL只用于软件验收，不扩大French17工程认可。项目判据、报告与比较入口见[工程校核](professional-framework/project-engineering.md)、[参考对照](professional-framework/reference-comparison.md)和[Harness接入](professional-framework/harness-integration.md)。

已知修复与限制见[已知问题说明](KNOWN_ISSUES.md)，专业版本范围以[专业核心说明](professional-framework/README.md)为准。DSH入口继续保留专业后端的强制检查、任务身份、取消和证据，通用界面与模型职责通过插件和预设接入；界面操作与数值/接口验证分别留证。
