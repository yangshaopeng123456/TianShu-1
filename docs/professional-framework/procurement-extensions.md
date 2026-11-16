# 天智五号采购功能补齐

日期：2026-10-28。作者与维护者：yangshaopeng（杨绍鹏）。

专业核心 `0.14.4-diagnostics`，Harness 插件源码 `0.2.4-acceptance`。这是本地新增能力说明；实际安装版本以能力查询返回值为准。历史 P3/P4 验收记录、固定 French17 工程参考与原任务不改写。工程认可范围没有扩大。

| 功能 | 本版范围 |
|---|---|
| CSV/JSON模型 | 新增严格 `tianzhi-grid/1.0`，显式母线节点、设备连接和频率；不是任意表格自动识别 |
| 孤岛诊断 | 输入声明及原生读回的母线级供电可达性；明确无电源、孤岛母线/设备、部分相开断和不完整诊断 |
| 曲线计算 | 1–1000个显式时点，固定控制、独立快照的准静态潮流；保留每点子任务、失败和未运行项 |
| 工况比较 | 读取两个本会话已提交的有效结果；相同对象、单位、基值、节点/端口与测量口径才计算差值 |
| PDF | 从已提交且哈希校验通过的HTML派生，支持单工况和时序；不重算、不修改原报告 |
| 本地模型 | 可选本机 OpenAI 兼容模型接口和启动/探测脚本；模型能力及目标电脑资源须实际验证 |

## CSV和JSON模型

例子在 [examples](examples/)。JSON根字段必须完整：

```json
{
  "schema_version":"tianzhi-grid/1.0",
  "frequency_hz":50,
  "voltage_bases_kv":[12.47],
  "buses":[{"id":"source","nodes":[1,2,3]},{"id":"loadbus","nodes":[1,2,3]}],
  "objects":[]
}
```

以上仅展示结构，完整可算设备见例子。`objects`包含 `kind`、`id`、`properties`；支持 circuit、linecode、line、transformer、load、generator、pvsystem、vsource、capacitor、reactor和xycurve的既有稳态支持子集。设备属性沿用 [原生支持范围](native-input.md) 和 [设备语义](device-semantics.md)，不是无条件支持所有OpenDSS功能。

- 电压kV，功率kW/kvar，变压器额定kVA；线路长度须明确units。矩阵用二维数值数组，接线/绕组用显式数组。
- 母线连接必须写明节点，如 `loadbus.1.0`、`source.1.2.3`。校验重复编号、未声明节点、连接关系及参数，不把平衡等值假装成实测三相。
- circuit在首位且唯一，明确电源序阻抗；非库对象明确phases。负荷/发电机明确model、conn、kv、kw、kvar或pf；PV明确固定运行点。变压器明确绕组数组。
- CSV采用 `kind,id,properties` 三列，properties为CSV正确引用的JSON对象。system行给frequency_hz与voltage_bases_kv，bus行给nodes，其余为设备行。
- 单个CSV/JSON源文件不超过2MiB。源文件、导入请求、转换版本与哈希保留在不可变快照的 `__tianshu_input__` 中；派生工作副本不会修改源件。

普通用户在Harness选择例子文件后说“帮我计算这个文件”即可。格式不符应说明具体缺项，不猜接线、限值或额定值。原生文件授权仍适用。

## 曲线计算与工况比较

“按这个负荷曲线计算一天，再比较峰谷时刻”可以触发时序流程。须明确模型、实际曲线及时间含义；不要由模型编造96个点。曲线和电网模型是两种文件。

CSV曲线：首列 `at`，另有 `load_multiplier` 和/或 `generator_multiplier`；时间须带时区且严格递增。JSON为点数组，还可提供 `setpoints`。示例：

```json
[{"at":"2026-10-28T00:00:00+08:00","load_multiplier":0.5},
 {"at":"2026-10-28T01:00:00+08:00","load_multiplier":1.0}]
```

全局倍率采用OpenDSS既有LoadMult/GenMult语义；省略时保留基础值。GenMult不代替PV irradiance。单设备运行设定限已有Load/Generator的kw、kvar或pf，PV的irradiance、temperature、kvar或pf；不支持在曲线中修改拓扑、设备额定、相别或控制策略。

整条明确曲线和基础请求绑定一次原生计算授权及SHA256。每点生成独立不可变工作快照、独立请求及真实求解/验证记录。失败策略显式选择stop或continue；普通工作可选择遇错停止，并告知用户。缺判据保持未评估。计算状态、数值有效性及工程状态分开记录，失败点没有伪造的零电压/零网损。

停止时取消在途子任务，确认真实终态后停止后续点；已完成结果保留。重启把未完成父任务标interrupted，`automatic_replay=false`，不自动重算。请求响应未知时只找回原请求。后续比较读取现有子任务结果，差值为右侧减左侧；缺值或基值不同标不可比较。

这是准静态逐点计算。时间是用户曲线标签，不表示动态状态连续性；不声称自动调压、储能调度、故障暂态或有状态控制的时序仿真。

时序基础模型目前限原生OpenDSS及本版显式相别CSV/JSON；MATPOWER转换的合成平衡嵌入继续支持原有单工况，不进入派生时序，以免把合成相别误当物理分相。

## 工具与接口

新增 `tianshu_inspect_topology`、`tianshu_run_time_series`、`tianshu_get_time_series`、`tianshu_cancel_time_series`、`tianshu_open_time_series_report`、`tianshu_compare_scenarios`；`tianshu_open_report`新增format=pdf。时序报告同样可选PDF。

专业API保持Bearer认证与127.0.0.1监听：

| API | 用途 |
|---|---|
| POST /v1/studies | 完整基础RunRequest 1.1及明确曲线；独立父请求ID |
| GET /v1/studies/{id} | 父子状态与真实指标 |
| GET /v1/studies/by-request/{request_id} | 原请求找回；不提交新任务 |
| POST /v1/studies/{id}/cancel | 空对象；请求取消与确认停止分开 |
| GET /v1/studies/{id}/artifacts/{name} | report.html、result.json、points.csv |
| GET /v1/studies/{id}/report.pdf | 已提交时序HTML派生PDF |
| POST /v1/compare-scenarios | left_task_id、right_task_id；只读比较 |
| GET /v1/jobs/{id}/report.pdf | 已提交单工况HTML派生PDF |

时序请求字段：request_id、mode=`quasistatic_fixed_controls`、curve_source、base_request、on_point_error及points或curve_file二选一。curve_file包含format、name、content_utf8，最大1MiB。base_request完整合同见[contracts.md](contracts.md)。

```powershell
python -m dpf_agent.professional.cli --state-dir '<独立目录>' run-study --request '<时序合同.json>' --timeout 600
python -m dpf_agent.professional.cli --state-dir '<已停止服务的目录>' study-status --study-id '<study-id>'
python -m dpf_agent.professional.cli --state-dir '<已停止服务的目录>' export-study --study-id '<study-id>' --format pdf --output '<新报告.pdf>'
python -m dpf_agent.professional.cli --state-dir '<已停止服务的目录>' compare-scenarios --left-task-id '<task1>' --right-task-id '<task2>' --output '<新比较.json>'
```

同一状态目录只允许一个服务所有者，运行中查询使用API或Harness，不另开CLI抢占。PDF要求已安装Edge/Chromium；缺渲染器明确不可用，不伪造文件。PDF是阅读版，完整精度与追溯明细仍随JSON/CSV交付。

## 可选本地模型

专业计算核心不调用大语言模型；解释、规划及工具调用由Harness所选模型承担。部署本地/单位内网模型时要选择真实支持工具调用的模型和兼容接口，单纯添加URL不算验收。

本机已用现有Qwen2.5:7B、官方Ollama运行器完成OpenAI兼容聊天及一次真实专业能力工具往返；这证明该组合的接口可用，不证明其覆盖全部专业意图，也不冒称Harness本地模型端到端界面或断公网验收通过。默认联网模型不被自动替换。

```powershell
python scripts/local-model.py serve --exe '<官方ollama.exe>' --models-root '<已有.ollama/models>'
python scripts/local-model.py probe --model qwen2.5:7b
```

Harness自定义模型：API地址 `http://127.0.0.1:11435/v1`，OpenAI Chat Completions协议，真实模型ID `qwen2.5:7b`。显示名可设“本地Qwen2.5 7B”。仅本机回环监听，脚本不改防火墙、不下载模型、不设置云回退。其他电脑应准备其自己的运行器、模型及算力；模型和第三方程序遵循各自许可证。

## 验证范围

已完成严格输入/曲线/对比合同测试、三相不平衡实算、96个15分钟时点实算、实际Host桥接接口的时序/报告/比较/取消/重启/会话隔离检查。96点成绩来自合成两节点模型，不能用作907节点馈线性能承诺。新增能力安装及界面操作单独核对，不把接口测试叫作全部界面验收。

母线级孤岛诊断对信息缺失或部分相开路返回partial；不把部分诊断当“无孤岛”的完整证明。工程额定/限值缺失仍未评估；数值有效、软件验收和工程适用性互不替代。


2026-10-28验收修正：报告主链接使用Harness原生Session文档预览；所选文件已保存到工作区Codex工作实践/日期_天智五号报告，PDF可直接复制或另存。HTTP保留备用。HTML/PDF并行发布保留两项登记。时序查询从原持久请求只读回显原始曲线设置，派生HTML/PDF补充输入和计算设置，不修改已提交原件，不重算。显式导出文件按工作区文件权限访问，原任务API的会话权限不变。


报告链接显示：使用标准工作区文件路径作Markdown目标，由Harness转换为文件预览；内部资源协议不直接作为Markdown链接。每种格式仅展示一次可点击文件入口。


2026-10-29集中验收修正：`tianshu_get_time_series(history=true, offset=0, limit=10)`无需事先知道父任务ID，只列本会话已有绑定，limit最大20；其他会话不可见。响应未知按原请求ID只读找回，不能以查询之名重算。`history=true`不可同时指定study_id。正常查询、取消与报告仍可用明确study_id。节点错误会定位具体设备和连接，缺失状态保持unknown。有效模型转换及已提交历史数值不改写。


2026-10-29启动修复：插件0.2.3-startup-fix匹配核心0.14.3-startup。新增带原有认证的轻量`GET /v1/health`；能力查询不再隐式遍历和完整校验历史模型。模型列表仍由`/v1/models`显式请求，实际模型检查及求解前完整性验证保持。启动采用20秒总就绪等待预算，避免多次超时探测叠加成数分钟；失败时另做有界进程清理。普通未指定格式的报告仅交付HTML，明确要PDF才派生。


2026-10-29验收收尾：0.2.4-acceptance匹配核心0.14.4-diagnostics，保留轻量启动检查及中文文件交付。CSV/JSON输入错误明确重复设备及两个对象位置、缺失load/generator/PV必填参数及字段；有效输入转换不变。用户要求后台限值校核时使用完整ProjectCriteria请求并保存规则记录，缺确认声明合并询问，不以模型对旧结果的文字比较代替；不把软件测试判据当作工程标准。原已提交任务不重写；实际安装及界面验收须单独核对。
