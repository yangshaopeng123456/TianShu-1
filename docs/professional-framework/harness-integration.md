# Harness接入范围与本轮验收

日期：2026-10-26。作者与维护者：yangshaopeng（杨绍鹏）。

目标Harness版本`0.2.1-rc.2`，天智五号插件`0.1.3-p4`。**P4在Windows本机、Harness工作区引用＋显式文件清单输入范围内完成本轮验收。**本轮实际装载、五文件导入、一次新计算、报告打开、只读追问、会话隔离及致谢样本均按这一范围核对。原生附件上传/FileBlock/完整流导入仍未验，不称原计划每项都通过，也不把新建上传UI列为本轮门槛。P5本地模型与断公网闭环、P6整体主入口切换仍未完成。

配置、打包及本地安装见[插件README](../../integrations/deepseek-harness/README.md)。本次只进行一次官方CLI冷升级；12安装文件与d637统一包一致，真实cap回显0.1.3。没有修改官方Harness本体，没有为工作区输入再次安装。个人配置和带本机路径的部署包不随公共源码分发。

## 职责与接口

Harness提供界面、会话、模型和Agent调度；天枢Host插件提供可信归属、授权及生命周期桥接；独立Python核心负责不可变输入、隔离OpenDSS计算、强制验证、工程校核和权威结果。旧桌面保留，新专业调用不经过旧聊天模型。

| 专用预设工具 | 用途 |
| --- | --- |
| `tianshu_capabilities` | 实际插件/核心版本和范围 |
| `tianshu_list_models` | 当前可信归属可用的模型/快照 |
| `tianshu_import_model` | 明确清单导入不可变模型，workspace与native_attachments来源分开 |
| `tianshu_run_power_flow` | 固定模型/工况的一次授权提交及状态等待 |
| `tianshu_get_task` | 原任务、状态和分页事件 |
| `tianshu_cancel_task` | 取消所属任务，区分受理和实际停止 |
| `tianshu_get_result` | 只读结果、工程指标及分页明细 |
| `tianshu_open_report` | 校验已提交产物，提供临时本机回环链接 |
| `tianshu_list_attachments` | 当前会话自有上传文件的元数据和选择键；本轮真实返回空清单，不计原生上传通过 |

接口存在不等于所有输入路径均已现场验收。工具仅在“天智五号”预设挂载。

## 当前正式输入路径

在Harness已有工作区引用文件，使用`source=workspace`，明确包根、主文件及全部依赖文件清单；清单授权后复制为不可变输入。不递归扫描，不搜索其他目录补依赖，不读取Host私有状态。导入快照与工况请求分开，导入成功不等于求解成功。

本轮使用ENWL既有Master、LineCode、Lines、Loads、Transformers共5文件，原件/清单/任务副本字节保持，只提交一次新潮流。实际finished/converged_valid，criteria=null，工程状态not_assessed、engineering_ready=false。6产物HTML/JSON/CSV及明细的HTTP、缓存和提交原件长度/SHA一致，用户确认报告能打开；随后只读追问原结果，没有重算历史。

当前官方桌面对带真实路径的非图片文件可能形成`@`工作区引用；它不等于原生FileBlock上传。`source=native_attachments`仍须使用真实当前Host清单键、显式包路径映射、主文件及依赖，不与workspace字段混用；空清单不能猜键或暗中换源。本轮用户明确选用workspace，原生上传/流导入保持未验；停止新增上传UI/SDK研究，不要求重复上传。

OpenDSS与MATPOWER分别受[原生输入范围](native-input.md)和[平衡等值合同](matpower-input.md)限制。专业入口要求OpenDSS COM及完整四结果域；采集1.8.2要求完整被动元件功率边界，缺证据不回落旧推导，不以收敛代替强制验证。

## 实际验收与边界

| 专项 | 本轮及既有已核对范围 |
| --- | --- |
| 安装与历史 | 0.1.3单次官方冷升级、12安装文件和实际cap；原中断task保持interrupted。历史查询按原版本/原字节读取，升级不重算 |
| 五文件闭环 | 明确workspace导入→固定快照→一次新任务→强制数值验证→6产物HTTP交付及用户打开报告；是软件流程验收，不是新增工程金标准 |
| 指标追问 | 原任务网损1.1448605860801044kW、passive_network_active_loss，对应result.json#/engineering_analysis/metrics/12810；原生LN最低电压0.9718834017417438pu，780/node3/phase3，基值240.17771198288432V，对应metrics/12828。基值来自OpenDSS并含数值归一说明，不是确认工程限值 |
| 真实会话隔离 | 同工作区A隐藏B的ENWL快照，get_task/get_result/open_report/cancel四项均返回不属于当前Host会话/工作区；仅限所述本机专项，不外推全面多用户安全 |
| 意图样本 | 0.1.3纯致谢仅答“不客气，有需要随时叫我。”，明确专业追问仍读取指定原结果；0.1.2历史两样本保留，不保证所有未来消息或未记录的零只读调用 |
| 取消与中断 | 既有ENWL任务级在途取消/worker回收及取消后新健康任务已验；受控后端中断后原任务恢复interrupted、automatic_replay=false，不自动重放。不证明恰在Solution.Solve某条指令内部中断 |

总计9个作业来自case4/ENWL的不同测试目的，本轮只新增1次计算，不能称9个独立工程电网；此前8任务和149历史事件保持。最后只读、隔离和致谢样本不创建新计算；最终核对9个完整任务行、169事件和48份已提交产物逐字不变，request/owner/attempt/scope、模型归属及新旧审批保持。指标指针匹配是对已提交结果的只读核对，不是新增独立求解。

本轮观察到的授权导入/计算/报告软件闭环分层通过，但完整拟发送文本保真未通过：保存的operating_conditions.source少ENWL前缀，实际AX用户输入本已有部分固定值缺失；已提交固定求解/恢复值符合工具schema，但不能认定与完整拟稿逐字一致或确定丢失环节。原独审all_passed=false与初始失败报告保留，不改旧请求、参考或容差。result.input在验证时合法增加runtime_device_declarations，原模型字段、manifest及5文件字节不变，不能把此扩展误称输入模型已改。

早期case4停止样本缺取消事件及准确点击时刻，保持未覆盖；ENWL首轮用户未点停止，保持正常完成；0.1.1纯致谢复述旧任务的失败也保留。提示词规则不作确定性安全隔离，模拟Host/SDK纯检查不代替实际界面；自动截图/点击未计通过，界面由用户操作并结合文字、归属、后台和产物核对。

## 工程与后续范围

本次P4结论仅为上述Windows本机workspace输入范围，原生上传/流导入仍未验，不把原计划全部项目改成通过。当前按用户2026-10-26确认的联网使用范围交付：Harness联网调用DeepSeek，天智五号专业服务在本机计算。本地模型部署与断公网业务验收已按用户要求跳过，不计通过；旧版保留供回退。入口、安装和回退见[联网版交付说明](online-delivery.md)。本机服务可用不等于完整私有化通过。

[P3/A07固定French17限定参考认可](p3-reference-status.md)保持原范围；9个独立缺依据点、整体工程合规未评估、热模型及历史未通过参考保留，engineering_ready=false。ENWL仅作软件验收，criteria:null下数值有效不代表设备运行或工程合格。
