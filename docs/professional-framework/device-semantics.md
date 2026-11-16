# 原生设备与连接声明

日期：2026-10-24。维护：yangshaopeng。

独立核心`0.10.0-p2`为导入的原生DSS包返回独立的`device_semantics`解释。新声明版本1.3配套`ordered_replay` 1.1和既有`power_operating_points` 1.0，按全局New/Edit/续行命令及同一命令内的属性顺序重放列明的赋值效果，并增加Load、Generator、PVSystem的显式Enabled状态。它不执行OpenDSS、不补数值默认参数，也不证明工程合格。原模型ID、原字节及存盘manifest保留；新的解释不加入旧manifest，不重写历史结果。新导入模型作业另有[运行期原生核对门](runtime-device-verification.md)，静态解释与真实读回的职责分开；运行期仍接受旧声明1.0、1.1、1.2，并保留各自原覆盖边界，其中旧1.1/1.2仍使用重放1.0。

## 读取与状态

模型导入、按snapshot_id检查的响应增加`device_semantics`，对象列表中的`device_semantics_status`与文件完整性状态分开。新作业在`input.device_semantics`保存提交时的解释；运行时从该任务冻结的DSS文件重新派生声明，在`input.runtime_device_declarations`保留最终所用内容并绑定其摘要；`source_refs`记录文件、行号及属性，便于定位。

| 状态 | 含义 | 当前行为 |
| --- | --- | --- |
| complete | 本解释子集内所需声明齐备 | 仍须编译、数值验证及工程校核 |
| partial | 缺少明确声明，或存在尚未重放的默认/副作用/语义 | 保留问题及缺项，满足运行期可靠字段门才可继续；不判工程合格 |
| invalid | 发现本子集内明确矛盾 | 新导入返回422且不落盘；历史有效哈希包仍能检查，但新提交拒绝使用 |

状态同时存在于整个解释和每个对象。顺序重放只覆盖列明的字段与副作用，因此全包通常为partial；不能把某个绕组或线路complete写成整个网络参数已审核。

字段状态为`declared`、`not_declared`或`uncertain`，分别表示明确源声明、缺声明、受到未完整解释的副作用影响。缺声明值为null，不用OpenDSS默认量替代。确认字段及本报告不构成设备工程额定值获项目批准的证据。

`ordered_replay`保留逐步事件、来源和未处理属性，其`replayed_fields`指定权威覆盖路径，仅接受`declared`与`uncertain`字段。比较前用这些路径覆盖原静态解释，不能因新解释不确定而退回旧的`declared`值；未声明项保留缺口，不允许用空覆盖项消除旧断言。运行期还核对来源及隐式源映射关系。命令顺序缺失/有歧义时保留不确定状态，不从最终库表猜历史。

## 设备范围

- Transformer：保留相数、绕组数及逐绕组bus、conn、kV、kVA、tap和%r。明确两绕组模型中，任一绕组的标量kVA赋值使两侧同值；`Kvas`数组独立逐项赋值，后续标量再联动。三绕组的明确数组保留各项，直接修改第二/第三绕组只更新所选项；第一绕组kVA对其他绕组的副作用暂标uncertain，不套用两绕组规则。后置维度、%loadloss等未完整重放副作用及不明确selector仍保守标不确定，不补默认绕组数。
- Line/LineCode：引用时复制当时明确的`nphases`为线路相数；在原生允许的支持子集内，同一命令中后面的`phases`或`linecode`继续覆盖。之后编辑库不会自动传播，重新指定`linecode`才再次复制。只重放列明的相数效果；完整矩阵、阻抗、单位及额定电流复制仍未覆盖。导体尺寸变化后的旧节点绑定不猜截断或默认值。
- Load/Generator/PVSystem：保留明确相数、bus、conn、kV及Enabled等字段和来源，按声明的连接检查导体数量边界。Enabled按New/Edit/续行顺序覆盖，未声明保持null，不填默认true；输入包支持yes/no/true/false。另在`power_operating_points`中解释下述有界功率配置；未填写额定量不会被补成工程限值。
- Circuit及隐式Vsource：将`New Circuit`及其创建续行的支持字段映射到`Vsource.Source`，后续明确Vsource编辑按顺序覆盖。普通`Edit Circuit`在原生执行器中无操作，保留忽略事件，不回灌电源；其后`~`可能编辑之前真正活动的源或其他类，本版明确拒绝此歧义组合。未声明的源默认量及阻抗不制造。XYCurve不是电气端口对象。

节点编号保留原生整数，0是参考地。4、11等编号不自动映射为A/B/C，也不自动断言为中性线。省略节点或仅给部分节点时，解释不补引擎默认；重复0允许，非零节点重复可能为有意短接，标partial。明确非法节点词法、超出已知导体数量及非正绕组kV/kVA/tap等矛盾会拒绝。

Transformer每端导体数按OpenDSS为相数+1，包括delta端的额外内部空置导体。Load/Generator/PVSystem的wye通常为相数+1；delta单相/两相/三相分别为2/3/3导体。因此相数不能直接作为端口功率数组长度。

新声明1.3的停用PCE，只有有序源声明明确Enabled=false、可信原生Enabled也为false，且NodeOrder返回约定的null/not_applicable标记时，才允许不要求该电气数组。身份、库存、相数、导体/端口数、BusNames、conn、kV和可靠功率配置仍须核对。停用母线只比较实际BusNames文本及显式节点，电气NodeOrder映射明确`not_checked`，不造默认节点或有效电气观测。未声明/不确定源状态、启用状态矛盾或坏标记均不能使用此例外；Line、Transformer、Vsource也不使用这一PCE例外。

## 功率配置顺序

`power_operating_points`仅包含Load、Generator和PVSystem，字段保留`declared/not_declared/uncertain`及源命令、行号、属性序号。Load/Generator使用`kw_base/kvar_base/pf_nominal/kva`；PV使用`pmpp/irradiance/kva/pf_nominal/reactive_mode/kvar_requested`。缺依赖不补原生数值默认，不从最终属性表反推中间命令。

- Load在每个New/Edit/`~`命令结束时重算；kW、kvar、kVA分别切换P/PF、P/Q、S/PF规格，PF本身不切规格。例如同一命令`kw=80 kvar=20 pf=.8`仍按P/Q，Q为20、名义PF重算约0.9701425。跨命令已可靠派生的PF可以成为后续明确容量命令的依据。
- Generator的kW/PF逐属性即时同步Q，kvar赋值即时更新名义PF。额定kVA不同于运行视在功率：未显式设定容量时同步采用1.2×kW，显式kVA/MVA后保留用户额定值。负kW产生的非正自动容量保持不确定，不当工程额定值。
- PV的Pmpp、irradiance、kVA、名义PF是配置参数。最后一次PF/kvar赋值记录为`source_selected_pf/source_selected_kvar`，仅表示源选择；旧名义PF和旧请求无功可继续存在。它们不证明运行模式，`kvar_requested`也不等于逆变器实际输出Q；两项在旧power_operating_points/power_setpoints合同内仍为`not_checked`；本版另以pv_fixed_points核对CIM原生模式及应用后P/Q，不能混作私有请求Q直接读回。

相关kW/kVA的原生非零解析规则会将绝对值小于1e-8的明确输入规范为正1e-8；这是有来源的输入规范化，不是补数值默认。无效PF、非有限值或不支持的功率副作用保留错误或不确定状态。Generator基准P/Q只具备6位有效数字文本读回，非零绝对值低于正常double下限的非正规数不能获得该比较的可信读数，运行期拒绝授权。

参数与端口功率应分开阅读：实际倍率对照中，源基准80 kW可对应Load端口40 kW或Generator端口-32 kW。名义PF也不是实际端口PF。本版system结果域的实际端口观测独立核对Load/Generator/PVSystem的V/I/PQ；参数读回本身仍不证明输出或全网覆盖。

## 计算侧修正及剩余边界

后端/适配器`1.8.0`保留此前COM按首端**全部导体**汇总P/Q的修正，避免单相/两相delta负荷、发电与非零中性导体功率被截断；多端元件仍仅取首端，最大相电流保留原口径。适配器版本升级使旧求解缓存不与新采集结果混用。原生设备合同1.0包含`power_setpoints` 1.0，并新增`voltage_rating` 1.0；新1.3声明要求其适用的可信读回，不能删除扩展后降级为旧比较。参数子合同与实际Load/Generator/PVSystem端口观测职责分开，详见[观测说明](observations.md)。

标称kV按设备类别固定来源：Load使用`Loads.kV`、Generator使用`Generators.kV`的原生double，以rel=1e-10、abs=1e-12核对，不在typed读取失败时退回通用文本。官方PV COM接口没有同等typed kV getter，PV固定使用`Properties(kv).Val`的6位有效数字文本一致性检查，保留原文、量化间隔及`precision_limited=true/full_precision_equality=false`；它不能识别同一舍入区间内的细小差异。Vsource的basekV仍使用原有通用属性文本合同，较高精度源声明仍可能因文本舍入而被拒绝。上述容差及精度均为参数读回边界，不是工程限值。

旧A/B/C结果投影仅保留原有节点1/2/3约定；其他节点从此投影排除，原始及专业观测继续保留完整节点数据。对称分量按节点1/2/3次序计算，避免引擎节点返回顺序改变产生假差异。仅有其他编号的母线在旧投影中为unavailable，不能据此声称新框架已支持所有任意编号网络的完整数值事务。

生产环境已为新导入模型接通逐设备原生属性读回和发布门，详见[运行期核对](runtime-device-verification.md)。可靠相数、端点及节点、接线、kV、绕组Tap/%R与本次可靠重放项按逐字段结果比较；其余副作用为`not_checked`。Load功率配置用原生double比较；Generator基准P/Q明确只做6位文本一致性，不宣称完整精度相等；旧有界属性解释器不重放完整PV逆变器输出、控制行为或默认量；新增独立固定点门对明确常规子集核对模式及限幅后的P/Q，不扩张成任意控制行为保证。额定量被读回或列明字段matched，不等于完整设备校核或工程认可。一般DSS、全部设备语义、工程判据、Harness及离线Agent仍未完成。公开及解析参考按各自模型/工况逐项登记，历史IEEE失败不被新增参考覆盖；P3/A07已取得[固定French17限定工程参考认可](p3-reference-status.md)，其他模型、工况及未覆盖设备不随之通过，详见[P2范围](p2-scope.md)。

语法依据：[EPRI Transformer](https://opendss.epri.com/Transformer1.html)、[Transformer Properties](https://opendss.epri.com/Properties16.html)、[两绕组kVA联动](https://opendss.epri.com/Transformermodelhasbeenmodified.html)、[线路属性顺序](https://opendss.epri.com/LineModelRevision.html)、[官方执行器源码](https://svn.code.sf.net/p/electricdss/code/trunk/Version8/Source/Executive/ExecHelper.pas)、[Load Properties](https://opendss.epri.com/Properties7.html)、[Phases and Other Conductors](https://opendss.epri.com/PhasesandOtherConductors.html)。本项目的保守解释边界不等于OpenDSS引擎完整语法范围。

功率顺序依据：[官方Load源码](https://svn.code.sf.net/p/electricdss/code/trunk/Version8/Source/PCElements/Load.pas)、[Generator源码](https://svn.code.sf.net/p/electricdss/code/trunk/Version8/Source/PCElements/generator.pas)、[PVSystem源码](https://svn.code.sf.net/p/electricdss/code/trunk/Version8/Source/PCElements/PVsystem.pas)。源码语义说明与本机实际引擎逐项验收分别管理，不因源码存在就视为全部版本行为已验证。

## P2固定工况扩展

专业核心0.10增加任务内`snapshot_state`、`passive_stamp`及`pv_fixed_points`独立门；既有声明1.3、ordered_replay1.1与power_operating_points1.0的旧字段范围保持原义。原power_setpoints未检查的私有PV请求Q没有被改写成getter成功；CIM模式与应用后P/Q通过另一合同验证。新门按任务冻结并纳入最终发布，不回填历史manifest或已提交结果。详见[运行期核对](runtime-device-verification.md)及[P2范围](p2-scope.md)。

## P3显式四导体声明

专业核心0.13.0-p3、后端1.8.2新增受限声明1.4与ordered_replay1.2，仅覆盖显式四导体Line/LineCode的完整矩阵及节点次序。普通模型仍生成1.3/1.1，旧解释与历史manifest不升级写回。四导体不是四相负荷，也不自动指定任何节点为中性线；完整矩阵/节点缺失或继承维度不一致则拒绝导入。
