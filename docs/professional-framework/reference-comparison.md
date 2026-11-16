# 已提交工程结果的只读参考比较

日期：2026-10-24。作者与维护者：yangshaopeng（杨绍鹏）。适用版本：专业核心0.13.0-p3。

参考比较读取已提交权威结果的`engineering_analysis.metrics`，按明确身份、输入/工况与误差合同逐点比较。它不启动求解器、不修改任务、不自动调单位/相别/电压基值、不放宽容差。比较输出是新的独立记录，不改写原报告或旧验收结果。

## 使用入口

在后端工程目录调用，状态目录必须是目标作业所属的独立目录，且不能已有其他活跃owner：

```powershell
python -m dpf_agent.professional.cli --state-dir '<目标状态目录>' compare-reference --task-id '<已提交task_id>' --reference '<参考合同JSON>' --output '<新的比较结果JSON>'
```

`--output`不能覆盖已有文件。CLI返回0要求比较集合完整且所有点通过；稀疏参考即使每个指定点通过，也返回非零并以`partial_comparison_passed`表达，不能当成失败求解或完整覆盖。

已启动专业服务时，可使用带相同Bearer凭据的`POST /v1/jobs/{task_id}/compare-reference`，请求体是参考合同JSON，返回比较JSON。接口仅比较，不将参考写入原任务；POST须`application/json`，参考体限4MiB，最多10000点。也可调用`ProfessionalService.compare_reference(task_id, reference)`或纯函数`compare_engineering_reference(result, reference)`。

## 参考合同1.0

顶层必须显式提供：`schema_version/reference_id/version/source/confirmed_by/confirmed_at/reference_kind/input_fingerprint/scenario_digest/points`。`schema_version`为`1.0`；两个摘要为小写SHA256。参考值、误差与适用边界须在观察比较差异前冻结，保留独立来源及版本证据；接口的结构校验不证明实际冻结时间或资料真实性。

`reference_kind`可选：`public`（公开参考）、`analytic_fixture`（独立解析测试）、`recognized_engineering_declared`（调用者声明为认可工程参考）、`software_generated`（软件生成结果）。任何来源类型和确认人字段**不会认证参考来源或自动通过A07**。用户已另行认可[固定French17审核包v3作为P3/A07限定工程参考](p3-reference-status.md)；认可绑定其具体模型、工况、判据、适配近似与对照结果，不改变比较API的来源认证边界或原结果工程状态。

`input_fingerprint`须与目标任务冻结输入指纹一致。工况摘要由下列纯函数生成；不能只按相同工况名称假定相同运行条件：

```python
from dpf_agent.professional.reference_comparison import scenario_digest

# request是权威结果中保存的完整request或RunRequest对象。
digest = scenario_digest(request)
```

它对`{"scenario": request["scenario"], "operating_conditions": ...}`规范JSON求SHA256，历史缺省条件记为null，不改数值或单位。参考合同同时绑定该摘要；服务另核对权威请求摘要、任务身份、数值有效性及工程量来源，取消/失败/缺门或身份不符均不能比较通过。

每个`points`元素必须包含：

| 字段 | 行为 |
| --- | --- |
| `id` | 有界唯一点ID |
| `metric/object_id/unit/measurement_basis` | 与权威工程指标精确匹配；对象名不区分大小写，其他值不自动转换 |
| `node/reference_node/terminal/conductor` | 目标指标有值的选择器必须一致；无值时省略或null，不用相别标签或数组位置代替 |
| `value` | 独立参考的有限值，保留P/Q符号和原始JSON数值表示 |
| `absolute_tolerance/relative_tolerance/near_zero_floor` | 三个非负有限数，分别记录绝对、相对和近零合同；没有软件推定的容差 |

同一目标选择器不能通过换单位或对象名大小写重复声明。推荐先按[工程量说明](project-engineering.md)明确物理口径，再准备参考包。不能把本次待验输出复制成“独立参考”用于证明数值正确。

## 误差、覆盖与结论

非近零参考采用：

```text
abs(actual - reference) <= absolute_tolerance + relative_tolerance * abs(reference)
```

当`abs(reference) <= near_zero_floor`时，相对分量严格为0，仅用绝对容差；相对误差不制造除零值。边界包含等号，计算溢出、非有限误差或容差不能判通过。逐点输出实际值、参考值、绝对误差、允许误差、相对分量及证据。

每项为`pass/mismatch/not_assessed`。缺量、重复目标、损坏量、输入/工况不符或数值门未通过时未评估。`all_reference_points_passed=true`仅表示所列点均通过；`passed=true`还要求所有可用工程指标均被唯一覆盖，且无损坏/歧义指标或未匹配记录。只对可用指标集合完整也不代表全网工程适用性，校核覆盖仍单独读取。

摘要为`passed`、`partial_comparison_passed`、`mismatch`或`not_assessed`，同时列`gating_issues/metric_issues/unmatched_metrics`及逐点覆盖。`engineering_accepted=false`、`reference_recognition=caller_declared_not_independently_authenticated`保持。公开直接数据、独立推导数据、解析补充和实际工程参考应分层保存，不能合并为一种“官方已通过”。

P2历史IEEE4的7123V/0.6V超差、IEEE13原表完整严格对照及浮置YD数值问题保持原结论；新比较接口不更改旧gold、误差或档案。P3的软件比较工具不把这些失败关闭，也不代替P4—P6的Harness、离线与整体验收。


## 软件生成参考

`software_generated`用于通过明确软件运行、导出或独立采集程序生成的参考值。`source`须如实记录软件、版本、模型与工况来源、采集或转换方式；接口仍只保存调用者声明，不核验这些来源。该类型不代表原作者公开gold，也不自动代表第二独立求解器。OpenDSS COM与CLI、不同Python绑定或不同采集程序仍可能共用同一数值引擎。

## 有界参考集合1.0

大结果可使用多个合规分包组成一份明确参考集合。旧单参考合同仍为最多10000点、HTTP/CLI 4MiB；没有提高或绕过这一限制。新集合顶层字段严格为`schema_version/collection_id/version/references`，版本为`1.0`，`references`是1至8个完整参考合同。每包非空且不超过10000点，合计不超过20000点，HTTP/CLI输入上限8MiB。

各包的`schema_version/version/source/confirmed_by/confirmed_at/reference_kind/input_fingerprint/scenario_digest`必须完全一致；包ID、点ID跨包唯一。精确目标选择器跨包也唯一，不能通过改单位或对象名大小写重复覆盖。集合自己的`version`和参考数据的`version`分别记录组织版本及数值版本，不能据此推定物理工况改变。

服务先核所有分包合同，再对并集执行一次完整的任务、输入、工况、有限值、误差和覆盖验证。它没有将几个partial布尔值相与后改成完整通过。缺量、重复实际量、失败任务、身份或来源不一致不能给出完整通过。完整比较依旧只覆盖当前权威工程指标集合，`engineering_accepted=false`保持。

```powershell
python -m dpf_agent.professional.cli --state-dir '<目标状态目录>' compare-reference-collection --task-id '<已提交task_id>' --collection '<集合JSON>' --output '<新的比较JSON>'
```

HTTP入口为`POST /v1/jobs/{task_id}/compare-reference-collection`，须相同Bearer及`application/json`；输入读取期间检查8MiB预算。直接服务入口为`ProfessionalService.compare_reference_collection(task_id, collection)`，纯入口为`compare_engineering_reference_collection(result, collection)`。

结果保留原单参考比较的逐点状态与完整库存覆盖字段，并增加`collection_id/collection_version/collection_digest/reference_parts/collection_limits`。`collection_digest`绑定完整集合及各分包；`reference_parts`列各包摘要与点数；`reference_digest`绑定用于一次覆盖检查的合并参考表示。读取或比较不写原任务，也不保存参考到原任务目录。
