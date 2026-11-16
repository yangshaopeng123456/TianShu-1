# 已提交报告的完整离线包

日期：2026-10-24。作者与维护者：yangshaopeng（杨绍鹏）。适用版本：专业核心0.13.0-p3。

带本机Bearer凭据调用`GET /v1/jobs/{task_id}/report-bundle`，或在任务所属状态目录上使用：

```powershell
python -m dpf_agent.professional.cli --state-dir '<任务状态目录>' export-report-bundle --task-id '<task_id>' --output '<新的ZIP文件>'
```

ZIP包含manifest.json、report.html、result.json、checks.csv、engineering.csv、observations.json和observations.csv。解压到同一目录后打开report.html，其明细相对链接可直接读取同目录完整文件。HTML阅读预览与完整JSON/CSV分别保留；浏览器直接打开带Bearer认证的在线HTML时，普通链接不会自动携带认证凭据，因此完整交付使用该离线包。

服务逐份核对已提交账本中的长度及SHA256，并核对任务、请求、输入绑定、版本和观测身份，然后打包原始字节。单文件限128MiB，合计含manifest限256MiB；缺少任何一份、hash不符、版本/身份冲突或超限会明确拒绝。未请求全部三种格式或没有engineering.csv的历史任务不能补齐导出；旧任务保留自身专业版本，不重算、不升级或重生成文件。CLI拒绝覆盖已有文件，并在同目录原子发布新文件。

此包不调用求解器、模型服务或报告渲染器。manifest的complete表示六份文件齐全且校验通过，不代表工程合格或参考已经认可。失败诊断任务也须按自身状态解释，不能因成功打包变成有效工程结论。
