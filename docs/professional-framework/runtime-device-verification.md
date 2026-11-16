# 运行期设备与绕组核对

日期：2026-10-24。维护：yangshaopeng。适用版本：专业核心`0.10.0-p2`，后端/适配器`1.8.0`。

本门用于新导入原生DSS模型的结构化作业：核对输入中可可靠解释的显式设备声明是否与实际OpenDSS上下文一致。它与[静态声明解释](device-semantics.md)、数值验证和工程限值校核分别记录，不表示完整设备模型或工程合格。

## 生效位置

1. 从该任务的不可变DSS快照重新派生声明，保留文件/行号来源；绑定规范JSON的SHA256。新声明1.3带`ordered_replay` 1.1及`power_operating_points` 1.0，按权威路径覆盖旧解释（包括不确定项），并核对来源及隐式源映射，禁止旧值回退；Load/Generator/PVSystem的显式Enabled也按New/Edit/续行顺序覆盖，功率部分必须保留完整对象身份和字段合同。同一数值上下文不能替换已绑定的声明，不能用请求中的自报通过标志替代检查。
2. 编译并应用求解配置后做首次原生读回，随后每次主Solve、复算或复核的Solve前再次核对。可靠字段不符或读取缺失时拒绝继续，不修改相数、接线、绕组、电气额定值或开关来取得匹配。
3. 提取最终原始结果后，在同一受控上下文中重新采集实际设备信息；发布前由纯比较器重新计算结论。缺少最终读回时，早先成功不能替代它；较晚的拒绝也不能被早先匹配结果覆盖。

频率门仍独立保留。单次Solve的频率读取、设备读取和求解共用总时间预算；结果提取和提取后设备读取也共用该次预算。取消及超时保持`CANCELLED`/`TIMEOUT`原码，不伪装为设备匹配失败。首次读回发生在主潮流Solve前，不代表编译和`CalcVoltageBases`等初始化没有内部数值操作。

旧注册算例schema 1.0请求不新增设备读取门，规范请求及幂等身份保持兼容；历史已提交结果不会被重写或补造本轮证据。

设备声明版本与请求schema分开：比较器仍接受旧声明1.0、1.1、1.2，维持各自原覆盖边界；旧1.1/1.2配套重放1.0。新声明1.3不能通过删除或破坏功率/顺序解释、停用标记及电压读回来退回旧比较。历史存盘manifest及已提交结果不会升级写回。原生设备读回合同仍为schema 1.0，在相关设备行包含独立版本1.0的`power_setpoints`和`voltage_rating`。

## 可信读回与比较边界

采集器读取`ActiveCktElement`实际名称、启用状态、相数、导体/端口数、BusNames及NodeOrder；读取负荷/发电/PV接线和kV等原生属性，以及变压器各Wdg的接线、kV、kVA、Tap和百分比电阻。每个值记录精确getter来源、状态和错误；缺失值为null，不用0或源声明代替。对象选择前后及绕组选择前后核对真实身份，最后恢复原对象与绕组选择器，并消费原生错误码。

新1.3的Load/Generator/PVSystem必须有可信原生Enabled布尔值，以区分启用设备的电气映射和停用设备。仅有有序源声明明确false、原生状态也可信false、NodeOrder具有合法null/not_applicable标记时，才允许不要求该数组。仍核对身份、库存、维度、BusNames、接线、标称电压及可靠功率参数。停用BusNames只做实际文本与显式节点比较，电气节点映射写为`not_checked`；这不产生有效的零电流或零功率观测。未声明源false、状态不符、坏标记以及伪造available NodeOrder均阻止通过。该例外不扩展到Line、Transformer、Vsource或旧声明。

功率读回另选择并验证Loads、Generators或PVSystems集合，读取前后同时核对集合名称与真实活动元件，并恢复原选择器。Load基准kW/kvar、名义PF和kVA采用原生double；Generator名义PF/kVArated采用double，基准kW/kvar使用明确的`ActiveCktElement.Properties(kw/kvar).Val`文本；PV的Pmpp、Irradiance、kVArated、名义PF采用double。Generator类型化kW/kvar读取的是受工况影响的名义输出，不能普遍代替基准配置。

标称电压另有`voltage_rating`身份与精度合同：Load固定读`Loads.kV`、Generator固定读`Generators.kV`的double，rel=1e-10、abs=1e-12；typed getter缺失或失败不得降级为通用属性文本。PV固定读`ActiveCktElement.Properties(kv).Val`，只按官方6位有效数字显示范围核对，保留raw_text和精度限制；不能假设存在`PVSystems.kV`或伪造binary-double来源。原生状态及电压读取过程中也要持续核对身份，重新选回正确对象不能掩盖先前读取时的漂移。

比较器要求来源、字段状态与身份可信，并将完整`AllElementNames`电气对象清单与快照中的对象及Circuit产生的隐式Vsource对应。LineCode、XYCurve是库对象，不从电气对象枚举中补造它们的运行期属性。

| 内容 | 本版处理 |
| --- | --- |
| 可靠显式相数、端口及导体结构 | 在有明确适用依据时比较；不把缺省参数解释成用户声明 |
| 母线及显式节点 | 启用设备核对BusNames与NodeOrder及实际端口次序；节点0保留，省略节点不补默认相别；符合新1.3停用合同的PCE仅核BusNames文本，电气节点映射未评估 |
| Load/Generator/PV的Enabled | 新1.3比较有来源的显式布尔状态；无声明不填true，也不能因此取得停用例外 |
| Load/Generator/PV的conn | 比较可靠显式声明与可信原生接线getter |
| Load/Generator的kV | 新1.3使用各自typed double读取，保留参数比较容差，无文本fallback |
| PV的kV | 新1.3固定6位有效数字文本一致性，保留原始文本及精度限制，不声称完整精度相等 |
| Load基准P/Q、名义PF和kVA | 按New/Edit/续行命令结束时的规格重算解释，使用可信double参数读回核对；不当作实际端口功率 |
| Generator基准P/Q | 按逐属性即时同步解释，仅核对官方6位有效数字属性文本的一致性；保留原文本与精度，不宣称完整精度相等 |
| Generator名义PF和额定kVA | 使用可信double读回；额定容量与运行视在功率不同，不等于工程认可值 |
| PV的Pmpp、irradiance、额定kVA、名义PF | 比较可靠配置与可信double读回；名义PF不证明当前模式或实际端口PF |
| PV源选择模式和请求Q | 旧power_setpoints中仍not_checked；新增pv_fixed_points以原生CIM模式及限幅后nominal P/Q作独立验证，不冒称私有请求Q直接读回 |
| Transformer逐绕组conn、kV、Tap、%R | 按真实Wdg选择后比较；明确且不受未重放副作用影响的其他字段按逐项结果查看 |
| LineCode相数复制与覆盖 | 比较引用当时明确相数及后续属性覆盖；库后续编辑不自动传播。尺寸改变后的旧节点绑定等不确定项标`not_checked`，不猜截断 |
| Circuit与隐式Vsource覆盖 | 比较Circuit创建时映射及后续明确Vsource编辑的支持字段；简单Circuit Edit按原生无操作处理，其后歧义续行明确拒绝 |
| Transformer kVA顺序 | 比较明确两绕组的任一侧标量同值联动、独立Kvas数组及后续覆盖；三绕组只比较已可靠重放的自身/数组值，其余副作用标`not_checked` |
| 容量、额定电流、矩阵及其他耦合属性 | 不保证完整一致性检查；不可靠或不在范围内的字段标`not_checked`，即使已采集原生值也不冒充完整校核 |

明确两绕组Transformer的标量kVA联动和数组独立赋值已在有界重放中处理；本版增加了上表的功率配置核对，不扩展为所有容量语义保证。LineCode完整电气矩阵/单位复制、电源阻抗和矩阵的源语义、三绕组第一侧kVA对其他侧的副作用仍未完整重放；编译后实际被动导纳另有冻结门，PV常规固定点另有独立核对门。个别kVA项matched不能推导出全设备容量一致或容量越限已评估。原生NormalAmps、kVA等默认或设置值也不自动成为项目批准的工程判据。

Generator基准P/Q和新1.3的PV标称kV文本比较以预期量级的6位显示区间核对，标明`precision_limited=true`、`full_precision_equality=false`；同一舍入区间内的细小差异无法发现。无效文本、虚报精度及非零绝对值低于正常double下限的非正规数不能取得可信一致性读数；可靠声明要求此读回时拒绝发布，不能用固定绝对容差把极小数全部判为0。此限制与Load/Generator标称kV等原生double参数核对分别记录。

配置基准与实际功率有独立对照：源基准有功80 kW，在明确倍率下可观测到Load端口40 kW或Generator端口-32 kW。PV输出还可能被限幅；其`kvar`读数是输出Q，不能验证源请求Q。参数读回只确认配置职责。本版另设system结果域的Load/Generator/PVSystem实际端口V/I/PQ观测及库存门，见[观测说明](observations.md)；不证明全网其他设备或全部运行语义已覆盖。

## 阅读结果

最终结果的`device_connections_verification`包含逐项`checks`、覆盖、声明摘要、任务和输入身份、实际证据来源及`publication_authorized`。逐项状态为`matched`、`mismatch`、`not_available`或`not_checked`。可靠声明所需实际值缺失、不可信或不符会阻止有效数值发布；仅缺少源声明或明确不支持的语义保持`not_checked`。没有任何可靠显式比较项时也不授权发布，不能用大量跳过项凑成成功。

`passed=true`只表示本门列明的可靠字段通过；`publication_authorized=true`还要求数值事务及其他发布条件满足，仅针对该任务数值结果。两者都不代表工程限值通过。`engineering_ready=false`、工程校核未评估与Harness/离线完整流程待验边界继续保留。

原生逐字段事实在任务原始结果的`solver.device_connections`中，运行时所用声明保存在最终结果`input.runtime_device_declarations`。内部诊断保存首次检查及最后一次求解前检查，用于说明拒绝原因；它们不替代最终实际读回。用户应通过任务产物及摘要追溯，不直接把旧`solver_result`当完整设备核对报告。

属性依据：[EPRI Transformer Properties](https://opendss.epri.com/Properties16.html)、[Transformer kVA联动说明](https://opendss.epri.com/Transformermodelhasbeenmodified.html)。数值比较容差用于同一原生属性读回的一致性，不是电网运行标准或工程限值。

当前限制：PV标称kV与Generator基准P/Q仍只有6位文本精度；PV模式已由独立CIM证据核对，但私有请求Q仍无直接读回。Vsource的basekV沿用旧通用属性文本合同，高精度声明仍可能因文本舍入而被拒绝。旧1.0—1.2声明保持原比较范围，不因新PCE停用例外自动变更；停用线路、变压器及其他设备也没有获得这一例外。完整设备模型和工程验收仍须按各自门槛完成。

## P2固定状态与导纳门

任务从不可变源文件派生`snapshot_state`与显式PV控制/限值，冻结首次实际读回：LoadMult/GenMult、LoadModel/Algorithm、Mode/ControlMode、DblHour/Frequency、设备Enabled/model/status、Vsource运行参数及每端每导体IsOpen。Capacitor的kV/kvar采用集合身份守卫的typed double；Reactor两值是保留完整字符串的原生存储属性文本，不伪称固定6位显示或内部double。未知、错误或缺失不回填默认值。

`passive_stamp`读取Line/Transformer/Vsource/Capacitor/Reactor的原生Yprim复数double、端口/导体/BusNames/NodeOrder和库存；绑定编译后实际导纳并在每次Solve及最终核对。它检查模型期间的有效电气参数未变，不声称已逐属性重放所有LineCode/矩阵/单位副作用。源原文和编译后事实同时保留。停用支路不补零；首版停用设备的可计算特例仍仅限已验证PCE。

PV的CIM100先在自有短暂存路径导出，再将完全相同的XML字节及hash保存到该作业专属证据目录，不进入模型快照、不改变DataPath或电气参数；身份关联、文件hash、选择器恢复及导出前后状态均须可信。常规模式要求WattPriority/PFPriority/VarFollowInverter均为false、无正Q斜坡且逆变器on/off可确定（高于两阈值上界或低于下界；只拒绝中间/量化不确定状态）。检查源PF/Q经Q上限和kVA约束后的nominal P/Q；不将nominal值当作实际端口功率。未支持或读回不充分时拒绝数值发布，不能靠大量not_checked通过。精确范围见[P2支持清单](p2-scope.md)。

被动门schema1.1另外冻结固定配置文本及各Wdg的rneut/xneut，防止Yprim延迟缓存掩盖求解前参数变化。文本值受原生格式精度限制，完整typed Yprim仍独立核对；缺失配置不能退回旧1.0块。已验模型族限定Load1/2/5、Generator/PV1，其余可解析输入在运行期拒绝，不把静态可解析当数值已验。CIM使用自有短暂存路径导出，原字节及hash留在任务证据目录，不改输入或DataPath。

## P3有界新增范围

声明1.4/重放1.2用于完整显式四导体矩阵线路，运行期保留全部节点与端口次序；普通声明版本保持。单相双节点固定Reactor的两个明确端点、R/X、Yprim及IsOpen仍在源状态冻结门内，不能用跨母线支路跳过核对。后端1.8.2另在fidelity映射之前保留完整被动PD端口P/Q边界，用于系统功率平衡，不依赖Circuit.Losses是否忽略某个并联元件；原生Circuit.Losses和旧转换字段保留，缺库存/端口或不一致时拒绝有效发布。
