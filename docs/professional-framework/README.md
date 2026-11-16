# 天智五号独立专业核心

日期：2026-10-28。作者与维护者：yangshaopeng。

专业核心`0.14.4-diagnostics`，后端/适配器`1.8.2`，Harness插件源码`0.2.4-acceptance`。当前功能范围与新增接口见[采购功能补齐](procurement-extensions.md)。实际安装版本由工具返回；接口、真实求解和界面操作分别验收。

专业核心独立供脚本与回环HTTP调用，使用隔离OpenDSS COM计算；Harness负责会话、所选语言模型和工具调度。新增严格显式相别CSV/JSON导入、母线供电连通诊断、固定控制准静态曲线计算、工况比较及PDF；原单工况合同和历史任务保持兼容。Windows本机为正式求解环境，本地语言模型是可选提供方，不能把本机潮流引擎等同于全部数据本机闭环。

P3仍按[P2数值支持清单](p2-scope.md)、[工程判据](project-engineering.md)及[固定French17限定工程参考](p3-reference-status.md)的既定范围收口。9个缺依据点与整体工程合规保持未评估，`engineering_ready=false`。新增软件功能与其他算例不扩大该工程认可。

既有P4验证覆盖工作区文件到报告、历史查询、跨会话隔离、运行中取消、取消后恢复及受控中断后不重放。新增扩展另有接口和真实算例检查；原生上传、模型多轮自然语言及本地模型GUI效果按真实检查记录，不由历史成绩或源码说明代替。

| 能力 | 当前行为 |
| --- | --- |
| 输入 | 注册DSS、不可变[原生DSS支持子集](native-input.md)、严格字面值[MATPOWER平衡PQ子集](matpower-input.md)、[显式相别CSV/JSON](procurement-extensions.md) |
| 固定工况 | 显式频率；核对模型既有状态，不自动改模或恢复 |
| 设备 | 有序声明与接线/绕组/可靠参数核对；全局倍率、model/status、逐导体开断、源参数及编译后被动导纳冻结 |
| PV | 原生CIM模式与应用后nominal P/Q、Q上限/kVA核对；私有请求Q没有直接getter |
| 实际观测 | 母线节点及Line/Transformer/Load/Generator/PV/Vsource/固定补偿端口导体V/I/PQ |
| 强制验证 | 收敛、独立复算、频率/设备/状态门、请求域独立库存、逐量一致性、完整域KCL及复功率守恒 |
| 作业 | 幂等、排队、取消/超时/中断、输入与事件追溯，HTML/JSON/CSV同源提交和hash核验 |
| 工程 | ProjectCriteria 2.0显式项目判据、节点对电压、逐导体Line/TX电流、附加TX端口kVA及有功损耗；缺依据未评估；engineering_ready=false |
| 参考 | 精确指标/输入/工况身份与显式误差合同；只读比较、覆盖及未评估，不认证A07来源 |
| Harness桥接 | 源码0.2.0扩展；实际安装查工具；当前正式路径为已有工作区引用＋明确workspace清单，ENWL五文件导入、一次新计算和6产物HTTP核对已完成；原生附件仍未验，本轮Windows/workspace范围报告/追问/隔离/致谢样本完成验收，原生附件未验单列。默认联网模型；新增可选本机模型接口已完成一次工具往返，完整GUI和断公网验收不据此计通过 |

标称电压、功率配置与实际输出分别记录；Load/Generator部分参数使用double，PV kV及Generator基准P/Q保留六位文本精度限制。静态partial/not_checked不代表全属性验证；实际编译导纳另有冻结门。见[设备语义](device-semantics.md)、[运行期核对](runtime-device-verification.md)和[观测说明](observations.md)。MATPOWER不恢复真实相别，不产生真实phase Measurement。

采集版本`1.8.2`对新结果启用已有的完整被动元件功率边界验证。专业入口要求OpenDSS COM及全部四个结果域；CLI或部分域输出若缺该证据，不再回落旧推导，不得以收敛代替验证通过。历史结果按原版本及原始字节读取，不为升级重算。

## 使用入口

日常主入口使用 Harness 联网版“天智五号”；安装、文件路径输入、迁移与回退见[联网版交付说明](online-delivery.md)。以下为独立核心入口。

在仓库dpf_agent目录执行，选择独立新状态目录；不加载桌面个人模型配置。

```powershell
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' capabilities
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' import-model --directory '<DSS目录>' --master 'Master.dss'
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' import-model --payload '<导入合同JSON>'
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' run --request '<作业请求JSON>'
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' compare-reference --task-id '<已提交task_id>' --reference '<参考合同JSON>' --output '<新的比较JSON>'
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' export-report-bundle --task-id '<已提交task_id>' --output '<新的ZIP路径>'
python -m dpf_agent.professional.cli --state-dir '<独立状态目录>' serve --port 18002
```

导入合同保存模型内容；[作业请求](contracts.md)另使用snapshot_id及明确工况。导入成功只表示输入冻结，queued不表示计算完成。run等待真实终态，数值有效返回0，超时请求取消并返回非零。同request_id及规范请求返回原task_id；内容不同则409冲突；重算使用新request_id。

也可使用ProfessionalService.import_model/submit/get_task/events/result/cancel/compare_reference。状态目录只允许一个活跃owner，重启后未完成任务标interrupted，已提交结果不重算、不升级写回。未知历史转换解释器标interpretation_unavailable，保留原字节；损坏包单独标integrity_error。参考CLI只有完整比较通过才返回0，部分参考全部点通过仍以partial_comparison_passed及非零返回；其输出不能覆盖已有文件。

serve只监听127.0.0.1。凭据保存在状态目录local-service.json，属于私有运行数据；所有接口须Authorization: Bearer凭据，POST须application/json。

| 接口 | 用途 |
| --- | --- |
| GET /v1/capabilities | 真实版本和范围；not_probed不等于引擎就绪 |
| GET /v1/models | 注册及导入模型摘要 |
| POST /v1/models/import | 原生内容包或MATPOWER合同；不接受服务端路径 |
| GET /v1/models/{snapshot_id} | 重验原始字节、清单及hash，不求解 |
| POST /v1/jobs | 持久提交，合同错误422、请求冲突409 |
| GET /v1/jobs/{task_id} | 执行、数值、校核三类状态 |
| GET /v1/jobs/{task_id}/events?after=0 | 该任务递增事件与下一cursor |
| POST /v1/jobs/{task_id}/cancel | 空JSON对象，受理与确认停止分开 |
| GET /v1/jobs/{task_id}/result | 已提交权威结果；未有结果409 |
| POST /v1/jobs/{task_id}/compare-reference | 参考合同JSON→只读比较JSON；不求解、不改原任务、不认证参考来源 |
| GET /v1/jobs/{task_id}/report-bundle | 校验并打包已提交的六份产物，供离线阅读及明细下载 |
| GET /v1/jobs/{task_id}/artifacts/{name} | 按请求生成并已提交的产物 |

新结果按请求格式生成result.json、report.html、checks.csv、observations.json、observations.csv及engineering.csv；后者仅在结果实际含engineering_analysis时提供，旧历史任务不补文件。长度/hash随终态提交；下载返回实际已校验的同一份字节。取消/验证失败不保留有效Measurement；部分结果域不声称全网覆盖。HTML、校核/工程CSV从同一终态结果派生，显示三种状态、判据确认、覆盖、未评估和来源；HTML无外部资源，CSV字符串公式前缀中和但不改数值或JSON原文。

ProjectCriteria 2.0只配请求1.1和完整工况声明；旧EngineeringCriteria及旧请求摘要保留。原生LN基值pu统计与项目明确基值的工程pu分开；最大负载率只描述已评估额定范围。Line/Transformer/Capacitor/Reactor有功损耗与有符号净Q交换分别输出，不将固定补偿Q统称损耗。端口kVA不替代逐导体电流和热模型；缺基值、额定或判据保持未评估。完整口径见[工程说明](project-engineering.md)。

公开参考、解析补充与每次强制验证分别登记。IEEE原始四节点7123V/0.6V失败、浮置delta数值稳定性及IEEE13原表完整严格对照的未通过项继续保留；不修改旧gold或放宽原容差。完整A06器件/工况参考矩阵和A07工程参考按总验收逐项登记，不能由单个算例通过推导任意工程适用。范围与证据口径见[P2清单](p2-scope.md)。

0.12.0-p3增加有界参考集合的完整库存对照和`software_generated`来源类型，并缩减HTML阅读投影；原单参考限制、权威JSON/CSV和工程认可边界保持。公开来源数据也须核清相别、额定及判据适用性，不因可以运行而自动成为工程金标准。

0.13.0-p3增加[完整报告ZIP](report-bundle.md)、显式四导体矩阵线路的受限声明与运行核对，以及按明确节点身份使用已验证电位的工程电压规则。单相双节点固定Reactor可表示给定被动导纳支路；不扩展为多相delta补偿或自动控制。后端1.8.2对新COM结果另记录完整被动元件端口功率边界，避免Circuit.Losses不含部分并联元件时误判功率平衡；原生损耗字段和验证容差保持。


2026-10-29启动修复：插件0.2.3-startup-fix匹配核心0.14.3-startup。新增带原有认证的轻量`GET /v1/health`；能力查询不再隐式遍历和完整校验历史模型。模型列表仍由`/v1/models`显式请求，实际模型检查及求解前完整性验证保持。启动采用20秒总就绪等待预算，避免多次超时探测叠加成数分钟；失败时另做有界进程清理。普通未指定格式的报告仅交付HTML，明确要PDF才派生。


2026-10-29验收收尾：0.2.4-acceptance匹配核心0.14.4-diagnostics，保留轻量启动检查及中文文件交付。CSV/JSON输入错误明确重复设备及两个对象位置、缺失load/generator/PV必填参数及字段；有效输入转换不变。用户要求后台限值校核时使用完整ProjectCriteria请求并保存规则记录，缺确认声明合并询问，不以模型对旧结果的文字比较代替；不把软件测试判据当作工程标准。原已提交任务不重写；实际安装及界面验收须单独核对。
