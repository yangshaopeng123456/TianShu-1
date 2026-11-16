# 天智五号 Harness 原生插件

日期：2026-10-26。作者与维护者：yangshaopeng（杨绍鹏）。

项目产品名称为“天智五号”。计算接口和工具标识沿用tianshu_*，用于兼容已有任务。界面品牌由[品牌扩展](../tianzhi-brand/README.md)提供。

本插件针对 DeepSeek Harness **0.2.1-rc.2**，通过原生Host工具和“天智五号”预设调用独立专业服务。源码版本 **0.2.4-acceptance**。实际装载版本以`tianshu_capabilities.harness_plugin_version`为准；目标兼容值不冒称探测版本。插件不启动旧天枢桌面或旧聊天模型。

`0.1.2-p4`已调整模式提示词的本轮意图边界，`0.1.3-p4`延续这些规则：纯致谢/问候只作简短回应，不查专业工具或复述旧任务；含专业问题或明确操作确认的混合消息继续按专业请求处理。此为交互规则修正，不改变求解、验证或任务接口；实际装载后仍需分别验收纯闲聊及专业追问，不将提示词规则当成确定性安全隔离。

## 日常简短请求与入口依赖

用户选择工作区模型入口后可以说“帮我计算这个文件”。0.1.5-daily修复了0.1.4只打包Master而遗漏同包引用、最终只显示HTTP422的问题。日常workspace_files默认在原生授权范围内沿明确字面引用补齐同包依赖，不要求用户手工列清单；旧package_root/files默认严格清单，include_dependencies:false保留用户明确限制。不会扫描目录、执行脚本、改模型或读取配置凭据；原生附件分支仍只能读取已上传的附件，不能据入口附件猜本机路径。

完整清单限500文件/20MiB，检查目录/链接/私有状态、持久化来源审计并重新核对原字节，再交核心最终校验。缺少实际引用文件会给相对文件名；受控422会给安全的合同字段和中文原因，认证/5xx/未知原文仍隐藏。Compile和其他不支持结构不会自动改写。

源文件的明确Set frequency作为预期声明保留，CalcVoltageBases初始化不要求用户重复填写；核心每次求解前后仍读取实际频率，冲突则拒绝。默认策略、Host声明与原生决定时间区分、同调用不重算规则见[日常文件计算](../../docs/professional-framework/daily-use.md)。同一ENWL单Master已通过隔离真实核心/COM/报告闭环；其中Host授权为测试替身，不能据此称真实DSH短句GUI已验收。原始用户失败记录保留，不改写为成功。

## 职责与当前范围

- Harness 提供界面、会话、模型和 Agent 工具调度。
- 本插件提供可信会话/轮次/调用归属、输入清单授权、固定请求计算授权、持久幂等、取消等待、结果分页和报告桥接。
- Python 专业核心提供不可变模型、OpenDSS 隔离计算、强制验证、项目判据、权威结果和产物。

工具仅挂载在专用预设中，不进入标准或创造模式的通用工具列表。0.1.3候选新增当前Session自身用户FileBlock的Host私有清单、逐项包路径映射和授权流读取；工作区明确文件方式保持兼容。原生附件实际上传、冷重建和完整界面导入仍须另行验收，候选不等于已通过。工具不会递归扫描工作区，也不接受模型提供raw ref或宿主路径。

## 本机配置与打包

公共源码中的 `cordis.patch.yml` 将以下子插件配置保留为空：

```yaml
- insert:
    - id: preset-tianshu-distribution
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: tianshu-distribution
        # name、description、order 等沿用源文件
        plugins:
          - id: tianshu-native-tools
            name: '@yangshaopeng/tianshu-harness'
            config: {}
          # presentation 和 ask-user 子插件沿用源文件
```

在单独的**本机安装准备副本**中，仅替换 `preset-tianshu-distribution.config.plugins` 中 `id: tianshu-native-tools` 的 `config`：

```yaml
config:
  stateDir: 'C:/Users/Example/AppData/Local/Tianshu/harness-state'
  python: 'C:/Projects/Tianshu/dpf_agent/.venv/Scripts/python.exe'
  coreRoot: 'C:/Projects/Tianshu'
  casesRoot: 'C:/Projects/Tianshu/dpf_agent/cases'
```

四个字段均为可信、绝对本机路径，不作为模型工具参数。`coreRoot` 是包含 `dpf_agent/dpf_agent/professional/service.py` 的**项目根目录**。`stateDir` 存放服务连接凭据、审批、会话绑定和报告缓存，应选私有目录，不应放入公开包或 Git。输入导入工具拒绝读取该目录及其子树。

可选 `connectionFile` 指向明确管理的既有专业服务连接文件。省略时由插件启动并管理自己的 Python 服务；插件不管理旧桌面的进程。可选 `engineeringReference` 是维护者明确配置的限定参考登记摘要，只用于如实展示，不使结果或任意输入自动获得工程认可。

安装准备副本应包含 `index.js`、`schemas.js`、`prompts/`、全部 `lib/`、`service_host.py`、`cordis.patch.yml`、`README.md` 及 `package.json`。用标准 npm tarball 格式打包后，先核对包内文件、配置及 SHA256，再使用当前安装版随附的管理命令安装本地包，例如：

```powershell
& '<Harness安装目录>/resources/runtime/cli/bin/dsh.cmd' plugin --profile desktop add '<已核对的本地插件包.tgz>'
```

也可使用 Harness 原生插件管理界面安装本地 bundle。通过其原生管理入口取得 profile 写锁及更新依赖，不能用全局 pnpm 或手工覆盖正在运行的用户 profile。CLI 成功仅证明安装命令完成；仍须核对 bundle 激活、预设出现和真实工具操作。升级 Harness 后应重新核对接口与兼容性。

公共开发/发布树保留无个人路径的配置；带本机路径的安装包只作为本地部署产物保存。打包不等于上传 GitHub。

### 从0.1.0、0.1.1或0.1.2升级

当前版本不承诺在旧revision活动时热升级：同一私有状态目录只允许一个Bridge owner。应先完整退出Harness，再通过官方CLI安装已核对且与专业核心匹配的本地包，然后重启并返回原会话。已有task与绑定继续只读使用，不为升级重算。临时报告回环地址随进程重启失效；对原task再次调用`tianshu_open_report`取得新链接即可，不创建新计算。

## 专业工具

| 工具 | 行为 |
| --- | --- |
| `tianshu_capabilities` | 查询实际服务能力、桥接状态及限定参考登记，不计算 |
| `tianshu_list_models` | 查询登记算例与本会话导入的模型 |
| `tianshu_list_attachments` | 只列当前Session自有用户FileBlock元数据与Host选择键，不读字节、不启动专业服务 |
| `tianshu_import_model` | 对工作区明确文件或本会话原生附件清单作原生授权、完整校验并复制为不可变输入 |
| `tianshu_run_power_flow` | 对规范请求作原生计算授权，提交并等待真实终态；停止信号转为所属任务取消 |
| `tianshu_get_task` | 读取本会话历史或固定任务状态、分页原始事件，不重算 |
| `tianshu_cancel_task` | 取消固定任务并核实停止；迟到取消不覆写已提交结果 |
| `tianshu_get_result` | 只读摘要、规则判定、工程量或原生观测；明细每页最多20行，工程量证据另有界分页 |
| `tianshu_open_report` | 取经校验的固定任务产物并提供只读回环报告地址，不重算 |

### 工程量追问

`observations`等视图是原生节点/端口观测；网损、负载率和项目基值等指标应从已提交的`engineering_analysis.metrics`读取，不能仅查观测后声称它们不存在。`0.1.1-p4`新增`engineering_metrics`视图，例如：

```json
{"task_id":"<本会话实际返回的任务ID>","view":"engineering_metrics","metric":"network_active_loss_kw","object_id":"network","limit":1}
```

该视图可按`metric/object_id/status/unit/measurement_basis/phase/node/terminal/conductor/criteria_rule_id`作精确过滤，随后按`offset/limit`分页。工程量状态为`available/not_assessed/not_applicable`，不能混用规则判定的`pass/violation`。`assessment`仍用于规则结果；两种数据域不拼接。

返回已记录的原值、单位、测量基准、额定/项目基值（如果原记录存在）及证据。每行默认最多8条证据，用`evidence_offset/evidence_limit`继续读取；证据总量、截断标记和`result.json#/engineering_analysis/metrics/<索引>`同时保留。大记录会缩短当前页而返回可继续的游标，不能把摘录当完整原件。

摘要仅投影原记录中已存在的全域网损、原生电压极值或已评估额定范围最大负载率，不从其他字段暗算补值。未记录的指标、缺值/单位/基准/证据或身份不符的来源保持`not_assessed`，不会补零、猜额定或重算。原生LN基准与项目明确基准不混用；净无功吸收也不当作热损耗。

## 授权与任务身份

会话、工作区、轮次、工具调用及根调用来自 Harness 运行时。模型不能提供 `session_id`、`request_id`、API 地址、凭据或其他会话归属。专业核心的 Bearer 凭据只在 Host 内使用，不进入模型、报告链接或普通工具结果。

计算前，Host 以真实归属和完整规范请求 SHA256 保存私有审批记录。原生审批拒绝时不会提交计算。同一 Host 调用、同一规范请求恢复时复用已记录的授权和原任务；同调用的请求发生变化会被拒绝，新调用则需要新的原生确认。响应未知时只能找回原 `request_id` 和原请求，不自动更换身份重算。只有实际确认的终态才能表述为已停止。

原生 `allowed-once` 只表示本次计算授权；可能由既有用户策略授予，不是手写签名或职业资格认证。工具不改写用户提供的 `confirmed_by`、`confirmed_at` 或项目阈值。缺依据时明确使用 `criteria:null`，保持未评估；计算授权不将整体工程合规改为合格。

## 验收边界

源码检查、模拟 Host 合同测试、真实 Python/COM 接口验证、实际 Harness 模式/界面操作、模型多轮工具调用及断公网闭环分别留证。安装、工具注册、HTTP202、数值收敛或已评估规则通过均不能单独替代后续验收。

P3 的限定参考认可只适用于其绑定的模型、工况、判据、适配近似和对照结果。未评估项、历史未通过参考、热模型及 `engineering_ready` 边界保持；本插件不把它们扩展成任意电网可用或合规的保证。

## 0.1.3原生附件候选边界

`tianshu_list_attachments`只返回当前Session自身用户上传文件的选择键与元数据。`tianshu_import_model`的native_attachments分支必须给出逐项attachment_key/package_path、主文件和完整依赖。原生授权在读流之前；提供者正常EOF及本插件长度/SHA/UTF-8核验全部通过后，保存私有来源审计并沿原Bridge导入，不改网络或工况。原上传目录不会自动恢复，fork继承附件须在当前会话重新上传。缺依赖拒绝，不搜索本机。

本段描述候选实现；实际原生验收未完成前，不宣称P4附件门槛通过。

0.1.3候选修订：投影状态使用显式依赖zod 4.4.3，Config继续使用schemastery；来源清单首次写入或同调用复用均需完成文件sync与关闭，失败不提交导入；附件元数据按JSON预算分页，保留选择键、next_offset及more。实际Host原生上传和冷恢复仍须验收。


### 原生上传与工作区引用的区别

“＋”菜单或`@`搜索中选择的工作区文件可能只生成`@相对路径`引用；界面显示文件名或文件标签，也不能单独证明它已成为本会话的原生上传文件。工作区引用仍可使用原有`source=workspace`方式，由用户明确`package_root`、`master_file`和完整`files`清单并确认授权。

原生附件以`tianshu_list_attachments`实际返回的本会话选择键为准。若清单为空，先核对输入类型；不要根据文件标签猜造选择键或宣称读到上传字节。若要走原生附件方式，应使用Harness实际提供的文件上传入口，并在发送后重新读取清单确认；具体按钮名称随安装版本核对，不仅凭“＋”按钮名称判断。两种来源不能混传，也不因出现文件引用而自动导入或计算。


## 0.2.0采购功能扩展

匹配核心0.14.0-extensions，新增严格CSV/JSON模型导入、母线供电孤岛诊断、曲线准静态时序、已提交工况比较及单工况/时序PDF。详见[完整合同与使用边界](../../docs/professional-framework/procurement-extensions.md)。新增工具为tianshu_inspect_topology、tianshu_run_time_series、tianshu_get_time_series、tianshu_cancel_time_series、tianshu_open_time_series_report、tianshu_compare_scenarios；open_report新增format=pdf。旧工具保持兼容，真实安装版本仍须查询。上述早期0.1.x验收记录不自动构成新增功能的界面验收。


## 0.2.1报告交付修复

HTML与PDF并行发布时合并登记，保留两个链接。主报告入口改用Harness官方Session文档资源地址：将经校验的所选报告导出到当前工作区Codex工作实践/日期_天智五号报告目录，再交原生文档预览打开；PDF已保存为本机文件，不依赖内置网页下载，也不更改浏览器安全策略。显式导出文件遵循工作区文件访问权限；原任务API的会话校验不变。HTTP地址保留为备用；原任务与数值不重算。时序查询明确回显原曲线设置，派生阅读报告附原请求设置，原提交产物不改写。


0.2.2显示修正：面向用户使用标准工作区文件Markdown链接，交由Harness原生文档入口处理；内部dsh-resource地址仅保留为元数据，不再放进用户Markdown链接。HTML/PDF各列一次可点击文件入口，移除重复不可点击的“打开报告”文字。数值计算与原提交文件不变。


## 0.2.2集中验收修正

2026-10-29，匹配专业核心0.14.2-diagnostics。报告使用真实工作区文件Markdown链接，HTML/PDF各显示一个入口。CSV/JSON节点校验明确设备、连接、字段及母线声明节点，未改动有效输入的转换内容。`tianshu_get_time_series(history=true)`只读取本会话已有时序记录；工具回复中断后先找回真实状态及完成/取消/未运行点数，不能据中断消息宣称没有执行。响应未知时只按原请求ID执行GET查询，查询失败保持unknown，不提交、不重放。


2026-10-29入口文案补充：报告与数据文件使用中文可见链接名（打开潮流报告、打开时序报告、打开时序PDF、查看逐时结果（CSV）等），实际指向已保存的工作区文件；不再显示重复的英文文件名清单。原文件名、哈希及历史产物不改名。


2026-10-29启动修复：插件0.2.3-startup-fix匹配核心0.14.3-startup。新增带原有认证的轻量`GET /v1/health`；能力查询不再隐式遍历和完整校验历史模型。模型列表仍由`/v1/models`显式请求，实际模型检查及求解前完整性验证保持。启动采用20秒总就绪等待预算，避免多次超时探测叠加成数分钟；失败时另做有界进程清理。普通未指定格式的报告仅交付HTML，明确要PDF才派生。


2026-10-29验收收尾：0.2.4-acceptance匹配核心0.14.4-diagnostics，保留轻量启动检查及中文文件交付。CSV/JSON输入错误明确重复设备及两个对象位置、缺失load/generator/PV必填参数及字段；有效输入转换不变。用户要求后台限值校核时使用完整ProjectCriteria请求并保存规则记录，缺确认声明合并询问，不以模型对旧结果的文字比较代替；不把软件测试判据当作工程标准。原已提交任务不重写；实际安装及界面验收须单独核对。
