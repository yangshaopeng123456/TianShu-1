# 原生DSS模型导入与运行条件

日期：2026-10-26。维护：yangshaopeng。

`0.13.0-p3`（后端/适配器`1.8.2`）可从显式本机目录或HTTP文件内容包保存原生DSS支持子集。原生DSS导入不调用OpenDSS，不转换格式，不改写原始模型。另有独立的[MATPOWER平衡子集适配](matpower-input.md)，会保留原始源字节及显式转换清单。静态检查通过只表示输入符合本版子集，编译、收敛、数值验证和工程校核分别判定。

## 输入子集

入口为包根目录下一个DSS文件，可使用任意文件名。根入口可Redirect叶DSS脚本，叶可以放在子目录；叶不得再次引用脚本或外部数据。内部Compile、嵌套重定向、求解/导出/DOS/目录操作、动态模型、自动控制器和二进制依赖不在本版范围。未支持内容明确拒绝，不能静默删除后继续计算。

静态输入允许精确属性白名单内的Circuit、Vsource、Line、LineCode、Transformer、Load、Generator、PVSystem、XYCurve及受限固定wye Capacitor/Reactor定义/编辑和同文件续行，以及snapshot设置。静态输入中的Generator/PVSystem model1/2可解析；运行期已验模型族仅Load1/2/5、Generator/PVSystem1，其他模型会在主Solve前拒绝。可静态保存不表示数值能力已验收。根入口可使用BusCoords、XYCurve的CSV或File数值依赖；保留原生单位、节点和连接，不补物理相别或工程限值。命令/属性缩写、位置参数、隐式Edit及一般DSS脚本不支持。

Enabled等布尔属性入口保留yes/no/true/false白名单；当前本机导入与HTTP包不因此增加1/0等拼写。新声明1.3对Load、Generator、PVSystem的显式Enabled按New/Edit/续行顺序保存，不填默认true。只有源明确false、运行期可信原生状态也为false，且有合法null/not_applicable NodeOrder标记时，才允许该停用PCE不提供电气NodeOrder；其他可靠配置仍须校验，BusNames文本可核对，电气节点映射保持未评估。停用标记不等于有效的零电流、零功率或工程合格。

`Edit Circuit`本身在原生执行器中无操作，不能用它修改源参数，应明确编辑`Vsource.Source`。`Edit Circuit`后接`~`的实际目标可能仍是之前活动的源或其他类，本版明确拒绝这一歧义组合，包括空`Edit Circuit`后的续行；不通过重排命令消除歧义。

路径使用相对POSIX形式，拒绝越界、盘符、UNC、链接/reparse point、Windows保留名及大小写/组件别名冲突。包不超过20 MiB UTF-8源字节、500个文件和500个子目录；活动DSS闭包不超过100000行，CSV不超过10000行、每行65536字符，派生manifest不超过20 MiB。HTTP另有32 MiB线流量上限。非有限数、缺依赖、重复定义、重复include和不支持的维度会明确拒绝。

未引用文件仍保存原字节并参与模型指纹，列在inspection.unused_files；它们不算已解析或已执行的电网模型。选择本机目录时应只选择本次需要的模型资料。

OpenDSS的Compile与Redirect有不同目录行为，见[官方说明](https://opendss.epri.com/TechNoteCompile.html)。本版按明确子集处理，保留用户脚本，不通过重排或改写依赖来制造成功。

## 本机CLI

在后端`dpf_agent`目录使用已安装依赖的Python环境：

```powershell
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' import-model --directory '<用户选定模型目录>' --master '<根入口.dss>'
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' inspect-model --snapshot-id '<实际返回的snapshot_id>'
```

目录入口只供显式本机CLI调用；HTTP不接受服务器磁盘路径。导入返回snapshot_id、fingerprint、manifest和inspection。同一入口、同一路径与原字节内容返回同一内容寻址ID；换入口或改任何字节会改变ID。

模型存于独立状态目录的models下；每次查看或新作业引用都会核对文件集合、字节哈希、清单及静态策略。列表中的integrity_error项不可当成可用模型。每个新作业还拥有独立输入快照；已经提交的结果不会因后来重新导入而重算。重启只读取已保存身份，不自动重跑。

## HTTP文件包

所有接口沿用本机Bearer认证。POST /v1/models/import接收以下UTF-8 JSON，content_utf8是原文件文本：

```json
{
  "master_file": "Feeder.dss",
  "files": [
    {"path": "Feeder.dss", "content_utf8": "Clear\nNew Circuit.Demo bus1=src basekv=12.47 phases=3 frequency=50 basefreq=50\nSet BaseFrequency=50\nNew Line.L1 bus1=src bus2=end phases=3 r1=0.2 x1=0.1 r0=0.5 x0=0.3 length=1 units=km\nNew Load.L1 bus1=end phases=3 kv=12.47 kw=30 kvar=10 model=1\nSet VoltageBases=[12.47]\nCalcVoltageBases\nSet mode=snapshot\nSet controlmode=off\nSet frequency=50\n"}
  ]
}
```

这只是接口演示算例，不是工程标准或用户电网。GET /v1/models/{snapshot_id}读取该输入身份；GET /v1/models同时列出注册算例及导入模型摘要。导入/查看成功不表示引擎已就绪，更不表示潮流或工程校核通过。

导入另返回声明版本1.3的`device_semantics`，附加`ordered_replay` 1.1和`power_operating_points` 1.0；它独立于原模型ID/manifest。按全局命令及属性次序重放LineCode引用时的相数快照、支持的源映射、绕组容量覆盖及三类PCE的显式Enabled；后续库编辑不自动传播，明确两绕组标量kVA同值联动，Kvas数组逐项独立。未知数值默认、完整矩阵/单位及未重放副作用保留partial/uncertain，不补成可靠声明。

功率解释区分Load命令结束重算与Generator逐属性同步，保留基准P/Q、名义PF及明确容量的来源。PV只解释Pmpp、irradiance、额定kVA、名义PF和源模式/请求无功；名义PF不表示实际模式；旧power_operating_points/power_setpoints字段的源模式与私有请求Q仍为`not_checked`。新增pv_fixed_points通过原生CIM模式、独立限值及应用后nominal P/Q验证常规固定点，不冒称直接读取私有请求Q。原生设备合同仍为1.0，追加`power_setpoints` 1.0；Load采用double参数核对，Generator基准P/Q仅做6位有效数字文本一致性，不能证明完整精度，也不支持非正规极小数的该项核对。

新1.3的`voltage_rating` 1.0读回合同按类别固定精度：Load、Generator分别要求`Loads.kV`、`Generators.kV`的原生double，按rel=1e-10、abs=1e-12比较，typed读取失败不能回退文本。PV使用`Properties(kv).Val`的6位有效数字原文，只检查舍入区间一致性，结果标明`precision_limited=true/full_precision_equality=false`，同一区间内的微小变化不可识别；不能将其伪装为不存在的typed kV接口。Vsource的basekV保持原有通用文本比较，高精度源声明仍可能受到该接口的舍入限制。

初次绑定、每次Solve前及最终发布时，可靠声明字段须与原生读回一致；重放的权威路径连同uncertain状态覆盖旧解释，不回退旧值，来源也须可信。比较器仍接受旧声明1.0、1.1、1.2，并保留各自原比较范围，其中旧1.1/1.2配重放1.0；不因此重写旧manifest或历史结果。新1.3不能通过删除扩展或换旧重放版本来降低要求。详细范围见[设备声明语义](device-semantics.md)及[运行期核对](runtime-device-verification.md)。

## 提交已有运行点

将上一步返回的真实snapshot_id填入以下请求后POST /v1/jobs，或将请求保存为JSON供CLI run使用：

```json
{
  "schema_version": "1.1",
  "request_id": "native-demo-001",
  "model": {"snapshot_id": "<替换为导入返回的snapshot_id>"},
  "scenario": {"scenario_id": "original-point", "mode": "existing_snapshot"},
  "solver": {"engine": "OpenDSS", "transport": "com", "solve_mode": "snapshot", "max_iterations": 100, "tolerance": 0.000001},
  "operating_conditions": {
    "frequency_hz": 50,
    "source": "演示模型显式50Hz；真实项目须填写项目确认记录",
    "confirmed_by": "演示调用者",
    "confirmed_at": "2026-10-19",
    "mode": "validate_model_operating_point"
  }
}
```

frequency_hz仅声明需要核对的实际频率，不能指示程序改频率。主潮流Solve及每次复算前读取真实引擎频率；不一致或无法取得时停止。发布前再核对原始结果的频率与来源，缺失/矛盾则不发布有效数值。Compile中的CalcVoltageBases初始化与主潮流Solve分开记录。

负荷、电源、开关及设备参数均来自原模型，scenario_id不会改变它们。当前显式核对运行频率及设备声明中可可靠比较的字段，不等于已审核全部工程工况。确认字段保存调用者声明，不替代身份/权限审核。缺少工程依据仍是未评估；matched不表示数值有效或工程合格。

原模型还可能包含负荷/发电倍率，配置基准功率与实际端口功率不能混用：倍率对照中基准80 kW分别可对应Load端口40 kW和Generator端口-32 kW。system结果域保留Load/Generator/PVSystem及Vsource、固定补偿支路的实际端口V/I/PQ，与Line/Transformer观测共同接受独立库存和物理一致性门。仅完整请求域全部通过才标记all_network_coverage；逐导体数值覆盖与工程限值验收分别记录。

任意模型修改、通用表格工程语义及自动控制未接通；项目确认判据已在P3接通，Harness入口已在Windows单人workspace明确清单范围接通并限定验收。MATPOWER仅支持已冻结的平衡PQ子集。P3/A07已取得[固定French17限定工程参考认可](p3-reference-status.md)；其他模型工程适用性未据此验收；断公网业务按用户要求跳过，未计通过。固定单步wye Capacitor/Reactor及显式Line Open/Close在本版白名单内，分别冻结额定参数、步状态及逐端导体开断矩阵；多步/自动投切、delta并联支路不在该被动补偿子集。

## P3受限四导体与固定支路扩展

仅显式Line/LineCode允许四导体维度：完整4×4 R/X/C矩阵、线路两端四个明确节点及一致的LineCode继承关系须同时成立。`kron=no/false`保留全部导体；`kron=yes`拒绝。四导体线路使用声明1.4及ordered_replay1.2；普通1/2/3导体输入继续使用声明1.3及ordered_replay1.1。不会把Load、Vsource、Circuit或Transformer的相数上限扩大为4，也不会由节点4推断中性线。

固定单相Reactor可用bus1/bus2各一个明确节点连接不同节点/母线，并显式给定R/X；其参数、端点、导纳和逐导体开断状态均冻结。此扩展不适用于多相delta补偿、自动投切或任意Reactor脚本。Circuit允许明确bus2；其他源语义门继续生效。扩展来自具体参考模型的表示需求，不等于已经支持一般四线DSS模型或通用外部格式转换。
