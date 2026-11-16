# 原生节点与端口观测 v2.0

日期：2026-10-26。维护：yangshaopeng。适用版本：专业核心`0.13.0-p3`，后端/适配器`1.8.2`；原生观测合同与P2保持兼容。

纯观测builder只负责原始量投影；服务另以domain_observations_verification核对完整请求域与守恒，数值覆盖不等于工程限值验收。
专业观测保留OpenDSS实际节点和导体身份，将可用数值与工程判定分开。它只投影本次实际取得的raw记录，不声称覆盖整个网络，也不把数值收敛当作设备运行合格。

本版为Load、Generator和PVSystem增加独立端口观测段，保留实际逐导体V/I/PQ，并将原模型的功率配置与求解输出分开。原生raw-v2与专业观测2.0保持版本，新增内容通过独立集合及其覆盖合同说明，不重写历史产物。

## 身份与电气口径

| 内容 | 表示及来源 | 使用边界 |
| --- | --- | --- |
| 母线节点 | ActiveBus.Name、Nodes与NumNodes | 节点编号来自实际记录；顺序不推断A/B/C |
| 母线电压 | 原始VMagAngle，V、deg；puVmagAngle | node_to_ground，相地电压；不是线电压，也不是有电位中性点的相中性点电压 |
| 标幺基值 | ActiveBus.kVBase，明确line_to_neutral | V/(LN基值×1000)须与pu一致；不使用旧base_kv字段换算 |
| 设备端口导体 | ActiveCktElement.Name、BusNames、NodeOrder、NumTerminals/NumConductors/NumPhases | terminal_major_conductor_minor排列；每个端口独立保留导体位置与实际节点 |
| 端口电压、电流 | 原始复数数组及由其导出的幅角，V/A/deg | 与母线节点身份及电压交叉核对；电流为该端口导体电流 |
| 端口功率 | 原始Powers，kW/kvar | 正号流入设备，按原生端口导体记录；核对P+jQ与V×conj(I)/1000 |
| Load/Generator/PV端口范围 | `power_conversion_elements`中的class、可信conn与terminal_data 2.0 | 支持本版一端口wye/delta导体语义，不从功率配置、旧摘要或设备类别补出实际数值 |

OpenDSS的NodeOrder对应Voltages、Currents和Powers的导体顺序；线路两端的节点顺序可以不同。节点0表示接地，其他节点可以采用任意编号，三相设备还可能包含额外导体。见官方[NodeOrder说明](https://opendss.epri.com/NodeOrderPropertyAddedtoCktEleme.html)与[相别及其他导体](https://opendss.epri.com/PhasesandOtherConductors.html)。

母线基值采用LN口径，见[基值检查说明](https://opendss.epri.com/TechNoteShowkVBaseMismatch.html)；功率正方向见[Powers定义](https://opendss.epri.com/Powers.html)。母线相地量不能直接与线间发布值比较，比较线间量时必须按复相量作差并记录实际节点映射。

## 可用性检查

采集器核对请求对象与实际激活对象，在读取前后确认身份；保留原始数组、完整维度和错误记录。纯观测层再检查唯一身份、数组长度、非有限数、复数/幅角/功率一致性、端口与母线的关联及LN基值。无可信来源或缺元数据时保留问题，不能用旧标量、默认零或数组下标补出Measurement。

母线记录未请求或不可用时，相关端口不能视为已完成跨记录映射核验。需要完整端口观测的调用方应同时请求bus与相应设备结果域，并读取issues和coverage，而不是只看作业finished。缺少记录不表示该对象不存在。

仅在数值状态converged_valid、求解器原始converged为true且元数据一致时生成Measurement。目前只将原生节点1/2/3投影为字符串相别；这不是A/B/C物理相别认证。其他节点保留原值并标未评估；节点0对应的接地导体保留原值并标不适用。取消、失败、非收敛或验证失败时不生成有效Measurement。

最终结果写入前还重新核验原始`solver.converged`必须严格为布尔true，`solver.error_flag`按兼容缺省false处理、提供时必须严格为布尔false。上游先前的成功状态不能覆盖最终未收敛、错误标志或状态类型冲突；发生冲突时不发布有效结果，也不保留有效Measurement。

Load/Generator/PV另按可信conn确认导体角色：wye的最后一根导体作为中性导体保留实际值，即使其原生节点号恰好为2，也不生成phase="2"电流Measurement。delta的导体电流、功率是端口导体量，不等于三角形支路或绕组相量。接地、禁用、中性导体及节点身份有歧义的记录不用于制造有效相电流；原生节点1/2/3以外也不猜物理相别。

PCE只核对BusNames中明确给出的前NumConductors个节点位置；未给出的连接位置直接依照可信NodeOrder，再与已验证母线同节点电压核对。单相delta的两个导体不能从NumPhases=1推断为“一个相导体加地”。旧线路/变压器连接核对规则保留。

表示一致性检查采用浮点比较容差，记录在provenance；这些容差只检查同一批数据的表示一致性，不是工程电压限值，也不是公开参考对照的误差标准。

## Load、Generator与PV覆盖门

COM采集把新增范围挂在`system`结果域。专业服务请求system时要求该段完整；未请求system时显式记录`not_requested`，不凭空生成空网络证明。当前system结果缺少集合、覆盖清单或独立计数时拒绝发布有效数值，不能退回旧raw边界。

`power_conversion_elements_coverage`版本1.0列明`expected_names`、各类`class_counts`、精确来源、状态与错误。名称来自`OpenDSS.ActiveCircuit.AllElementNames`，数量另从Loads.Count、Generators.Count和PVSystems.Count取得，包含禁用设备。纯适配层核对名称唯一、类别一致、独立计数与名称分类相符，再与每一条实际记录对应；遗漏、重复、读取失败或类别矛盾使整个新增段不可用，不能用其他正常PCE或大量跳过项凑成通过。

集合中各行保留`name/class/connection/terminal_data`。connection须有可信原生getter来源；逐导体数据核对活动对象身份、terminal-major/conductor-minor顺序、数组维度、复数/幅角一致性、同节点母线V及`P+jQ=V×conj(I)/1000`。负荷消费与电源注入的正负号均按原生“流入设备为正”保留，不为报表统一改成正值。

若同一raw另含最终`solver.device_connections`原生读回，新增段还须与其可信全量元件名称、Enabled、conn、端口/导体/相数及BusNames逐字段交叉核对；启用设备还核NodeOrder。每个字段必须有精确getter来源、可用状态及正确类型，两份记录的值与次序必须一致。缺失、不可信或矛盾均拒绝，不能把实际启用设备伪装成禁用摘要，也不能把单相wye伪装成同维度delta，使中性节点2错误成为phase2电流Measurement。禁用设备只跳过不可读的NodeOrder，其余身份、Enabled、conn、维度与BusNames仍须一致。旧注册路径未采集该独立frame时保持原边界；存在frame但损坏时不静默跳过。

`coverage.power_conversion_elements`单独记录`status/passed/required`、库存核验及类别/导体/禁用摘要计数。新增段失败时不产生该段有效Measurement；P/Q仍是带适用性和证据的端口导体事实，不新增功率Measurement或工程结论。旧采集版本的raw缺少该段时保留`not_requested`，历史结果不补算；新采集版本不能靠删去两项新增字段伪装成旧结果。

## 禁用设备

实际OpenDSS仍将禁用Load/Generator/PV保留在元件枚举与集合数量中，但其NodeOrder、Voltages等电气读取可能失败。采集器在可信`enabled=false`时保留身份、维度和BusNames，跳过电气getter；状态为`disabled`，原因固定为`disabled_device_no_electrical_observations`，NodeOrder及全部V/I/PQ数组显式为null，不将失败转换为零。

专业观测输出一条`disabled_element_summary`，状态为`not_applicable`，`enabled=false`、`numerically_valid=false`；terminal、conductor、node、phase均为null。不制造导体事实或Measurement，禁用摘要与实际导体数分别统计。

新声明1.3可对显式停用PCE继续配置校验：源Enabled=false、原生Enabled=false且参数读回NodeOrder为合法的null/not_applicable标记时，才省略电气NodeOrder绑定。仍核对身份、维度、BusNames中显式节点文本、conn、标称kV和功率配置，报告将电气节点映射标为not_checked；源状态缺失、不确定或相矛盾，或标记被伪造成available数组，均不能借此跳过。旧声明1.0—1.2保留原比较边界，不自动获得新例外。

参数读回与此处端口观测分别管理。新`voltage_rating` 1.0对Load/Generator采用真实typed double kV，失败不回退通用文本；PV接口没有typed kV，只可核对原生Properties(kv).Val六位文本及其量化区间，保留raw_text与precision_limited，不宣称完整精度。求解端口电压不能反推或代替输入标称kV。
## 频率与引擎环境

实际求解频率、设备阻抗基频和引擎默认基频分别记录。它们不能相互替代：相同DSS文本在不同隐含默认设置下可能得到不同结果，只有输入文件哈希相同不足以证明运行条件相同。未提供或未读到的频率不推断为50Hz或60Hz，也不当作项目已确认的运行条件。

自有COM工作线程关闭RegistryUpdate，防止正常模型执行将默认频率等会话设置持久化到Windows配置；专业注册输入拒绝显式修改RegistryUpdate。该措施不修改原模型电气频率，也不构成任意DSS脚本的安全沙箱。执行前后实际状态与读取失败随结果保存。

OpenDSS的DefaultBaseFrequency会影响后续电路；BaseFrequency及单个设备BaseFreq、Solution.Frequency有不同用途，见官方[BaseFrequency说明](https://opendss.epri.com/BaseFrequency.html)和[RegistryUpdate选项](https://opendss.epri.com/Options.html)。需要可复现对照时，应核对这些实际值及明确来源，不能只比较界面显示的电压摘要。

## 输出与当前限制

结果字段professional_observations包含：

- observations：可用的相地标幺电压和端口导体电流Measurement；每项附raw证据定位。
- bus_nodes、element_terminals：原生节点/导体的幅值、角度、基值及有符号P/Q事实，附身份和适用状态；禁用PCE以明确的disabled_element_summary表示。
- issues、coverage：缺失、矛盾、未知节点与实际记录覆盖；仅当完整bus/branch/transformer/system域、PCE、辅助源/补偿支路及全网守恒均通过时all_network_coverage=true；这不代表工程验收。
- provenance：采集版本、物理口径、raw源哈希及本次输入快照指纹。

JSON与CSV从同一终态结果生成，长度和SHA256随作业记录提交。只读API核验已提交清单及文件完整性。历史P1任务维持原产物，不把重新读取当作重新计算。

观测层本身不生成变压器绕组相电流、热负载率、不平衡度或工程合格结论。P3另从已验证、任务绑定的节点复相量与端口事实生成`engineering_analysis`，按ProjectCriteria 2.0核对明确节点对电压、Line/Transformer逐端逐导体电流及附加变压器端口kVA。三角形端口导体电流不当作绕组相电流；模型额定值不自动变成项目依据。工程标幺电压必须使用项目明确的`base_voltage_v`，与原生LN基值统计分开。工程量、覆盖、未评估机制与同源报告见[工程校核](project-engineering.md)。P3/A07已取得[固定French17限定工程参考认可](p3-reference-status.md)；该认可不扩张观测层能力，也不改变原结果的未评估项或整体工程合规状态。

本轮端口连接来自实际BusNames/NodeOrder，完整绕组接线组别、接头与额定参数的模型语义合同还待扩充。频率逐元件记录覆盖实际采集的线路、变压器及新增PCE，不等于其他设备或控制行为均已独立审核。

配置核对的`power_operating_points/power_setpoints`与实际端口量分别管理：Generator基准P/Q及PV标称kV仍有文本精度限制。PV新增`pv_fixed_points`以原生CIM模式、独立nominal P/Q及限值核对常规指定运行点，私有请求Q未直接读回；支持边界见[P2范围](p2-scope.md)。

新`domain_observations_verification`以独立AllBusNames/NumBuses、AllElementNames及设备Count核对请求域完整性。system附带Vsource、固定Capacitor/Reactor的`auxiliary_elements`，与PCE、Line、Transformer共同验证非接地节点KCL和所有端口复功率总和。数值阈值为电流.001A+1e-4倍通过电流、功率.001kVA+1e-4倍通过视在功率，不是工程标准；任何错误、截断或库存矛盾都会阻止有效发布、清除Measurement，保留诊断事实。仅请求部分域时不声称全网守恒或完整覆盖。

MATPOWER输入没有真实相别：逐导体事实保留`equivalent_phase_label`、合成相别来源和`physical_phase_mapping=false`，不生成真实phase Measurement；CSV亦如此。P3不会借合成相别发布真实分相电压/负载合格结论；满足完整边界时可统计平衡等值有功损耗。旧solver_result只用于诊断，不能作为真实相别证明。所有新CSV字符串单元格均进行公式前缀防护，JSON原始数据与数值不改；下载返回同一份已检查长度和SHA256的字节。历史产物不重写。Harness已在Windows单人workspace明确清单范围接通并限定验收；本地模型与断公网业务按用户要求跳过，未计通过。
