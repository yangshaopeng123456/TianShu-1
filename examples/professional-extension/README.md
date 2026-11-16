# 专业只读工具扩展示例

维护：yangshaopeng。日期：2026-10-26。

本示例通过独立 `ToolRegistry` 注册 `native_voltage_envelope`，读取已完成任务的原生节点最低、最高电压及来源，证明添加专业分析工具无需改写专业核心。它使用已有专业服务的只读 API，不依赖旧桌面界面或 Harness 聊天，也不创建任务、调用求解器、修改模型或写入正式结果。

## 运行

先通过既有入口正常完成一个包含原生节点电压的潮流任务，并保持该本机专业服务运行。使用明确可信的服务连接文件和该服务所属任务编号；不要选择他人提供的连接文件，也不要公开连接文件中的密钥。以下路径和任务编号是需要替换的占位符。

在项目根目录的 PowerShell 中执行（仅使用 Python 标准库）：

```powershell
.\dpf_agent\.venv\Scripts\python.exe .\examples\professional-extension\read_only_extension.py `
  --connection-file 'C:\可信的本机任务目录\professional\local-service.json' `
  --task-id 'pf-请替换为已有任务编号'
```

只有显式指定的 `http://127.0.0.1:端口` 服务可以访问；程序禁用代理和 HTTP 重定向，密钥仅用于本机 Bearer 请求，不出现在输出中。程序不扫描 Harness 配置、凭据、历史或模型文件。需要读取的是当前运行服务生成的 `local-service.json`，并非手工填写模型 API 密钥的配置。

读取顺序是任务状态、`/v1/jobs/{task_id}/artifacts/result.json`、任务状态。工件接口由核心验证已提交文件的 SHA256 和字节数；示例另外核对终态、数值有效状态、任务/请求/尝试身份、核心验证标记及所需指标元数据。未完成、取消、未验证、错误任务绑定、缺失或重复指标均拒绝输出分析。已有结果被修改时，应由核心工件校验拒绝读取。

成功时 stdout 是 JSON，退出码为 0；失败时 stderr 返回稳定错误码，退出码为 2。示例不提供文件写入选项。输出包含：

- `kind=read_only_extension`、`authoritative=false`、`engineering_acceptance=false`。
- 来源 `task_id`、`request_id`、`attempt_id` 和本次读取工件的 SHA256。
- 最低/最高电压的原始值、`pu`、`measurement_basis`、节点位置、基值、基值来源、证据引用及 JSON 指针。

`node_to_ground_native_ln_base` 使用 OpenDSS 原生节点对地电压基准。该结果只覆盖核心实际提供的有效非接地标准节点；节点号和返回的 `phase` 不自动解释为 A/B/C，也不代替线电压或三角形绕组电压。示例不采用默认电压限值，不给出工程合格结论。正式项目判据、强制验证与报告仍由专业核心掌握。

## 新增另一个只读工具

在 `read_only_extension.py` 中定义接收 `engineering_analysis` 字典的函数，然后在 `TOOLS` 中独立注册：

```python
def existing_metric_names(analysis):
    return {"names": sorted({row["metric"] for row in analysis["metrics"]})}

TOOLS.register("existing_metric_names", existing_metric_names)
```

随后使用相同命令加 `--tool existing_metric_names`。函数应只读已经验证的专业指标，明确单位、基准、缺项与来源；不得覆盖核心状态、绕过验证、补造量测或写回权威结果。注册器传入分析数据的独立副本，派生内容放在固定非权威输出的 `analysis` 内。此约定不构成运行不可信 Python 的安全沙箱。

本目录不含真实潮流结果或合成工程样本。测试夹具属于开发检查，不能作为工程参考或工程认可。接口合同与正式扩展约束见 [专业框架合同](../../docs/professional-framework/contracts.md)；项目判据和正式校核见 [工程校核说明](../../docs/professional-framework/project-engineering.md)。
