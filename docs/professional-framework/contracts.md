# 专业框架数据合同 v1.0 / v1.1

日期：2026-10-26。维护：yangshaopeng。适用版本：专业核心0.13.0-p3。

本合同面向独立脚本、作业API及已接入的Harness插件。结构校验使用Python标准库，不导入聊天、模型服务或求解器。合同已定义的字段与某个服务版本已接通的能力分别登记；调用方不能以字段存在推断功能已验收。

## 兼容求解请求 v1.0

```json
{
  "schema_version": "1.0",
  "request_id": "run-demo-001",
  "model": {"case_id": "case4"},
  "scenario": {"scenario_id": "original-operating-point", "mode": "existing_snapshot"},
  "solver": {
    "engine": "OpenDSS", "transport": "com", "solve_mode": "snapshot",
    "max_iterations": 30, "tolerance": 0.0001
  },
  "recovery": {"policy": "none", "max_retries": 0},
  "result_scopes": ["bus", "branch", "transformer", "system"],
  "output_formats": ["json", "html", "csv"]
}
```

`RunRequest.from_dict(payload)`拒绝未知字段、错误类型、布尔值冒充数字、NaN/Infinity和不支持的枚举。`schema_version`、`request_id`、`model`、`scenario`、`solver`必须显式提供；`scenario`和`solver`必须包括示例中的完整字段。缺省`recovery`为不恢复；缺省结果域和格式为上述四域、三格式。格式字段表示请求，服务仍须检查实际支持范围。

| 字段 | 合同与执行边界 |
| --- | --- |
| `request_id` | 调用方稳定请求身份；不等于服务分配的`task_id`。重复请求是否返回同一作业由持久化作业服务原子保证 |
| `model` | `case_id`、`snapshot_id`二选一；仅为注册引用，不能填写任意磁盘路径。引用解析、授权、依赖快照及内容哈希由服务核对 |
| `scenario` | 首轮只表示使用该模型快照已有运行点；`scenario_id`是追溯标签，不会修改负荷、电源、开关或其他工况 |
| `solver` | 首轮合同限定OpenDSS/COM/snapshot。`max_iterations`为1～10000的整数；`tolerance`为大于0且不超过1的有限数。参数合法不代表数值适用或计算有效 |
| `recovery` | `none`要求零重试且没有`retry_solver`；`numeric_only`要求一次重试和完整显式`retry_solver`。后者目前仅定义合同，未接通的服务必须明确拒绝，不得偷偷套用旧恢复模板 |
| `criteria` | 可缺省或为null；旧EngineeringCriteria保留。新ProjectCriteria 2.0带schema_version且只配请求1.1，必须有确认来源、确认记录和规则；缺省不套用仿真默认阈值 |
| `result_scopes`、`output_formats` | 非空、不重复；不能用未知值自动回落为默认值 |

合同不包含模型编辑指令、不可信会话身份、任意文件路径、提示词或“自动放宽约束”开关。模型变更与工况修改后续需独立授权记录和派生快照合同。

`to_dict()`返回独立JSON对象；`canonical_json()`使用排序对象键、UTF-8、紧凑分隔和禁止非有限数；`digest()`返回该规范JSON的SHA-256。数值字段按合同转换为浮点表示，整数迭代上限保持整数。摘要包含`request_id`及完整请求，数组顺序保留；它是提交身份摘要，不是模型内容哈希，也不宣称不同数组顺序的请求语义等价。未提供的可选字段先按合同补齐，因此与相同显式缺省值得到相同摘要。

## 导入模型与明确工况请求 v1.1

保持上方结构，将schema_version改为1.1，model改为导入返回的snapshot_id，并增加完整operating_conditions：

```json
{
  "frequency_hz": 50,
  "source": "本项目模型及运行条件确认记录",
  "confirmed_by": "项目确认人",
  "confirmed_at": "2026-10-19",
  "mode": "validate_model_operating_point"
}
```

这五个字段必须显式提供。频率是0至1000Hz内的有限正数，范围只约束接口，不是电网工程标准。mode仅允许核对原模型运行点，不能借字段修改频率、负荷、电源或开关。新导入snapshot作业必须用1.1；注册case也可用1.1。source与确认信息保存调用方声明，结构校验不证明确认人的身份或权限。

首个主Solve及后续复算前核对OpenDSS实际Solution.Frequency；频率不符或未取得时拒绝继续主Solve。发布前再次检查原始结果的实际频率及来源，缺失/矛盾/不可信时不发布有效数值或Measurement。Compile中的CalcVoltageBases等初始化计算不属于这里所说的主潮流Solve。

结果operating_conditions_verification记录declaration、expected_frequency_hz、actual_frequency_hz、source、frequency_modified=false和status：not_declared、matched、mismatch、not_available或rejected。matched仅描述实际频率与声明一致；其余已有运行点参数仍来自冻结模型，不表示所有工程条件已逐项审核。

1.0缺省或显式null的operating_conditions在规范JSON中省略，旧摘要与幂等身份保持不变；1.0提供非null条件会被拒绝。1.1缺完整条件会被拒绝，不自动升级或填频率。结果外层schema_version仍为1.0，新增字段随professional_version登记；请求版本与结果版本分开。

## 项目判据2.0与兼容旧判据

P3将新`ProjectCriteria`作为`criteria.schema_version=2.0`接入请求1.1；它不是把请求版本改为2.0。请求1.0提供新项目判据会拒绝；1.1仍必须提供完整工况声明。新合同含`criteria_id/version/source/confirmed_by/confirmed_at/rules`，每项明确kind、对象、节点对或端口/导体、物理基准与来源。电压、Line/Transformer逐导体电流、附加Transformer端口kVA和有功损耗的全部字段、未评估及覆盖规则见[项目工程校核](project-engineering.md)。

新判据不接受通配符和同目标换单位/大小写重复；缺pu基值或负载额定不回填默认值。确认记录是调用者声明，不认证工程资格。旧判据不添加schema_version，仍用下方原结构、摘要与精确Measurement匹配，不能当作具有新全库存覆盖合同。历史已提交结果不补算、不重评或升级写回。

## 兼容EngineeringCriteria

以下限值仅示范字段，**不是软件内置工程标准**。实际使用时必须替换为项目确认的依据。

```json
{
  "criteria_id": "project-confirmed-001",
  "version": "1",
  "source": "项目限值确认记录及原始文件定位",
  "confirmed_by": "项目确认人",
  "confirmed_at": "2026-10-18",
  "rules": [{
    "rule_id": "bus1-a-voltage",
    "object_id": "bus1",
    "metric": "voltage",
    "unit": "pu",
    "measurement_basis": "phase_to_ground",
    "phase": "A",
    "terminal": null,
    "lower": 0.95,
    "upper": 1.05
  }]
}
```

`EngineeringCriteria`要求唯一的`rule_id`、非空来源/确认人/版本及ISO日期或时间；一条规则至少有明确上限或下限。上下限包含边界，不添加隐含容差。确认字段用于保存调用方提交的记录，纯合同校验不能替代确认人权限核验或标准原文审核。

| metric | unit | measurement_basis | 相别/端口 |
| --- | --- | --- | --- |
| `voltage` | `pu`、`V`、`kV` | `phase_to_ground`、`phase_to_neutral`、`line_to_line` | 前两者为A/B/C或原生节点1/2/3；线间为AB/BC/CA；母线电压可无设备端口 |
| `current` | `A` | `terminal_conductor_rms` | 明确相别及设备端口，不能把全端口最大电流当作某相电流 |
| `apparent_power` | `kVA` | `terminal_phase_apparent` | 明确相别及设备端口，不能把三相总量当作某相量 |

本轮合同没有三相汇总容量、完整变压器热校核、不平衡度标准或自动单位换算。确认的允许电流/容量可按适用口径直接作为上限；额定量、运行限值及其来源必须先由专业适配层核对，不能取模型默认额定值直接宣称工程依据。

## 工程观测与纯规则接口

```python
from dpf_agent.professional import (
    EngineeringCriteria, Measurement, RunRequest, evaluate_assessment,
)

request = RunRequest.from_dict(payload)
# 以下数值仅展示调用形式；真实观测须来自已核对语义的提取器。
observation = Measurement(
    object_id="bus1", metric="voltage", unit="pu",
    measurement_basis="phase_to_ground", phase="A", terminal=None,
    value=0.98, evidence_ref="raw_result.json#/buses/0",
)
report = evaluate_assessment(
    observations=(observation,), criteria=request.criteria,
    numerical_validity="converged_valid",
).to_dict()
```

观测的对象、物理量、单位、连接口径、相别、端口必须与规则**完全一致**；大小写、A与1、线间与相地等不自动转换。提取器须持有实际节点/端口身份及电压基值来源，`evidence_ref`定位到该数值证据。填写标签不能证明观测语义正确。

旧solver_result含平衡等值、历史相别推断和聚合电流等口径，不能拼造专业观测。`professional_observations`从原生raw-v2提取。P3只把任务身份、数值及来源门通过后的有效Measurement送入兼容EngineeringCriteria；新ProjectCriteria 2.0从同源节点/导体事实进行明确连接与库存覆盖校核。失败、取消或缺可信来源时不留有效观测，也不凭旧聚合量给出工程通过。

## 专业观测增量 v2.0

请求合同支持`1.0/1.1`；专业观测是结果的增量字段，使用独立`schema_version=2.0`，不能与ProjectCriteria的2.0混同。结果包含`observations`（符合Measurement合同的可用值）、`bus_nodes`、`element_terminals`、`issues`、`coverage`和`provenance`。完整语义及限制见[专业观测说明](observations.md)。

从OpenDSS提取的Measurement使用原生节点字符串`1/2/3`，不将其自动改成`A/B/C`；上方A相示例只演示纯合同。项目规则若使用A/B/C，后续必须提供可审计的相别映射，不能凭位置匹配。未知节点保留编号但不生成当前合同不支持的Measurement；接地导体0标不适用。端口导体电流不能冒充绕组相电流或热负载率。

`observations.json`是权威结果中`professional_observations`的同源JSON，`observations.csv`为母线节点和端口导体事实表。只有请求相应格式才生成，任务产物清单是读取依据。字段缺失、映射矛盾或非有限量会形成问题记录并限制可用Measurement；不能以CSV行存在当作已校核通过。

兼容EngineeringCriteria的纯规则输出为`pass`、`violation`或`not_assessed`。没有已确认依据、观测缺失/多义、数值未验证时均为未评估。未列入规则的电压/电流/容量类型保留覆盖占位项，空规则不会得到通过。新ProjectCriteria 2.0使用实际库存覆盖及精确不适用机制，不能用旧类型占位项代替。

摘要状态为`not_assessed`、`partially_assessed`、`violations_found`或`evaluated_rules_passed`。最后一种只描述已声明规则，**不表示全网合格**。`overall_compliance`保持`not_assessed`；即使新项目合同的声明集合覆盖完整，也不据此宣称工程认可或全部热模型已检查。摘要同时提供各状态计数、判据身份及摘要、逐条实际值/界限/来源/证据。

## 三种状态分别记录

| 维度 | 状态 |
| --- | --- |
| 执行 | `created`、`queued`、`running`、`cancelling`、`finished`、`failed`、`cancelled`、`interrupted` |
| 数值 | `not_run`、`converged_valid`、`non_converged`、`validation_failed` |
| 工程 | 每项`pass`、`violation`、`not_assessed`；ProjectCriteria 2.0另支持精确目标`not_applicable`及完整库存覆盖 |

执行完成、文件生成、数值收敛及工程合格互不替代。`converged_valid`须由固定数值事务及其强制验证给出，不能由插件或调用者根据一句模型回答设置。此纯规则模块不替换或放宽已有数值发布门。

P3新增`engineering_analysis.schema_version=1.0`及新项目校核`assessment.schema_version=2.0`；兼容旧判据的assessment保留原合同。HTML/JSON/CSV从同一终态结果派生，新增engineering.csv按实际结果及请求格式列出。参考合同1.0与只读API/CLI见[参考比较](reference-comparison.md)。`engineering_ready=false`保持。P3/A07已取得[固定French17限定工程参考认可](p3-reference-status.md)，以独立审核记录绑定具体包哈希；任何合同字段存在都不意味着工程认证，也不改写原结果。
