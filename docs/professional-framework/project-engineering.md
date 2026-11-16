# 项目工程校核与同源报告

日期：2026-10-24。作者与维护者：yangshaopeng（杨绍鹏）。适用版本：专业核心0.13.0-p3，后端/适配器1.8.2。

P3在P2强制数值门之后，对任务绑定的原生观测应用项目明确提供的判据。工程量与规则检查不修改原模型、不重算潮流、不使用旧仿真默认阈值。用户已完成[固定French17的P3/A07限定工程参考认可](p3-reference-status.md)，P3在此范围收口。9个缺依据点与整体工程合规保持未评估，`engineering_ready=false`；参考认可不修改任何原结果的工程状态。Harness、完整离线模型流程及整体切换仍属于P4—P6。

## 请求与项目判据

`RunRequest.criteria`接受旧`EngineeringCriteria`或新`ProjectCriteria`，不能混合两种结构。新结构必须有`schema_version: "2.0"`，且只能用于请求`schema_version: "1.1"`及完整`operating_conditions`。旧判据没有`schema_version`字段，仍按精确Measurement接口解释；旧请求的规范JSON、摘要及幂等身份保持。

以下示例只是字段示范，不是内置标准；`source`、对象、节点、额定量和限值必须换成适用于实际项目的明确依据。

```json
{
  "schema_version": "2.0",
  "criteria_id": "project-limits-001",
  "version": "1",
  "source": "项目运行校核依据及文件定位",
  "confirmed_by": "项目确认人",
  "confirmed_at": "2026-10-24",
  "rules": [
    {
      "rule_id": "bus-load-node1-voltage",
      "kind": "voltage", "object_id": "loadbus",
      "unit": "pu", "measurement_basis": "node_to_ground",
      "node": 1, "reference_node": 0,
      "base_voltage_v": 2400, "lower": 0.95, "upper": 1.05,
      "source": "该母线节点接地电压基准和运行界限确认记录"
    },
    {
      "rule_id": "line-1-terminal1-conductor1",
      "kind": "line_current", "object_id": "Line.line1",
      "unit": "A", "measurement_basis": "terminal_conductor_rms",
      "terminal": 1, "conductor": 1,
      "rated_value": 100, "upper_loading_percent": 100,
      "source": "该端口导体允许电流及运行比例确认记录"
    },
    {
      "rule_id": "network-active-loss",
      "kind": "active_loss", "object_id": "network",
      "unit": "kW", "measurement_basis": "passive_network_active_loss",
      "upper": 20,
      "source": "本工况有功损耗上限确认记录"
    }
  ]
}
```

项目级`criteria_id/version/source/confirmed_by/confirmed_at/rules`及合同版本均必须提供；每条规则另有`source`。规则ID唯一，同一精确目标选择器不能通过换单位或对象大小写重复声明。对象不支持通配符；对象名按原生名称不区分大小写匹配，节点/端口/导体精确匹配，不猜A/B/C。确认字段只保存调用者声明，不认证确认人、权限、原始文件或工程认可资格。

| kind | 目标、单位与measurement_basis | 必要依据与行为 |
| --- | --- | --- |
| `voltage` | 母线`object_id`、`node`及`reference_node`；`V/kV/pu` | 接地参考0使用`node_to_ground`，其他明确节点使用`node_to_node`；复相量作差取模。显式`lower/upper`至少一个；pu还须`base_voltage_v` |
| `line_current` | `Line.*`、`terminal/conductor`；A、`terminal_conductor_rms` | `upper_loading_percent`必须明确；缺`rated_value`接受合同但该项未评估，不取模型默认电流 |
| `transformer_current` | `Transformer.*`、`terminal/conductor`；同上 | 逐端口导体检查，包含实际导体身份；不等同绕组相电流或热模型 |
| `transformer_kva` | `Transformer.*`及`terminal`；kVA、`terminal_complex_power_magnitude` | `abs(sum(P)+j*sum(Q))`，不是逐导体视在功率模之和；需额定量与比例，仅作附加端口指标 |
| `active_loss` | `object_id=network`；kW、`passive_network_active_loss` | 完整网络边界；显式上下限至少一个，不推定损耗率分母 |

负载项上限为`rated_value × upper_loading_percent / 100`，负载率为`measured / rated_value × 100`。`rated_value`按该条规则的A或kVA口径提供。边界包含等号，不加隐藏工程容差。数值、额定量与基值必须有限、适用且类型正确；未评估项不伪造数值。

## 覆盖、未评估及不适用

核心检查集合由已验证库存生成：全部非接地母线节点电压、Line与Transformer每端每导体电流，以及网络有功损耗。变压器端口kVA是附加指标，不能替代逐导体电流集合。

每个实际目标须有适用且已评估规则，或精确、有来源的不适用声明。缺项目判据、电压基值、额定量、目标观测、可靠元数据或完整网络域时标`not_assessed`并写原因。部分请求域、稀疏规则或少数通过项不能冒充全网覆盖。

可用`applicability: "not_applicable"`及非空`exclusion_reason`明确排除一个精确目标；仍须填写其kind、对象、物理口径、选择器和source，且不能带上下限、基值或额定量等数值。拼错或不存在的目标仍为未评估，不能借排除绕过库存。可靠禁用设备可标不适用；缺失电气值不补零。

每项状态为`pass/violation/not_assessed/not_applicable`，摘要保留计数及`coverage.expected_points/covered_points/missing_points/complete`。`evaluated_rules_passed`只描述已评估规则；`declared_scope_checks_passed`须同时满足完整覆盖和无越限，仍不表示工程认可。`overall_compliance`保持`not_assessed`，`engineering_accepted=false`、`thermal_model_assessed=false`。

## 工程量与网损口径

`engineering_analysis.schema_version=1.0`记录指标、公式、输入证据、覆盖与限制，和`assessment`分开。原生`native_voltage_pu`及其极值采用OpenDSS母线LN基值，仅描述可用、已验证节点；不当作项目基值或电压合格结论。`project_voltage_pu`来自明确节点对及项目`base_voltage_v`，两者不合并。最大负载率只对有明确额定且实际已评估的目标取极值，范围为`evaluated_project_ratings_only`；遗漏/排除项仍保留在覆盖明细。

有功网损汇总Line、Transformer、固定Capacitor和Reactor所有端口及导体流入元件的有符号P；不计Vsource、Load、Generator、PVSystem的净注入/消费。逐元件与线路/变压器、固定补偿分组另列。Q列为`net_reactive_absorption`净无功吸收/交换，可能含补偿负值，不能统称热损耗或无功网损。完整库存、各端口功率及网络守恒缺失时，不给完整网络损耗结论。

MATPOWER平衡等值模型不提供真实分相映射，不将合成节点当工程相别验收；满足完整边界时可提供平衡等值有功损耗。P2模型族、原生读回精度和未通过参考继续按[P2范围](p2-scope.md)保留。

## HTML、JSON与CSV

终态事务先处理取消竞争及全部数值门，再生成工程分析、校核和报告。`result.json`是权威结果；`observations.json`同源保存原生事实，`checks.csv`逐校核项、`engineering.csv`逐工程指标，`observations.csv`保留逐节点/导体事实，`report.html`清晰展示执行、数值和校核三种状态、确认依据、未评估、覆盖及证据。只生成请求格式；任务清单决定实际产物，历史P2任务不凭空增加新文件或重评。

HTML所有动态文字转义，使用内嵌样式、无外部资源及脚本。CSV字符串公式前缀被中和，数值和JSON原文不改；CSV中的文本前缀不当作原始证据。产物长度与SHA256随终态提交，下载返回实际已核验的同一份字节并标记附件及`nosniff`。服务状态目录和作业账本仍属于使用者管理的本机可信边界，不宣称防御拥有全部本机文件权限者的改写。

`engineering_publishable`仅表示数值允许发布且确有已评估项目项；其范围固定为`evaluated_project_checks_only_not_engineering_acceptance`。进行参考对照见[只读参考比较](reference-comparison.md)；读取报告或比较不会自动重算历史任务。


### 大结果HTML阅读投影

报告投影1.1将HTML作为有界阅读入口，完整权威JSON、逐项CSV及观测文件仍按任务实际产物提供。HTML不再嵌入整份权威JSON或完整运行期证明；显示真实计数、来源与覆盖摘要、关键限制和明细预览。越限与未评估优先展示；超出预览的各状态数量明确列出，少量可见通过项不改变完整校核状态。

只显示任务请求格式实际生成的产物入口；未生成某格式会明确说明。完整输入和证据仍保留在已提交权威结果及机器文件，HTML的省略不是删除证据或新的工程判定。历史已提交报告不自动重渲染；新投影也可以在独立输出目录读取历史权威结果验证，保留原件。

显式节点电压规则可选择节点编号大于3的已验证有限电位，包括明确节点对的电压差。其物理相别仍保持未指定，观测中的not_assessed不被改写；仅有可信metadata并通过整体数值门的电位才可参与该规则。默认A/B/C统计继续只使用标准节点，节点4不自动成为中性点。
