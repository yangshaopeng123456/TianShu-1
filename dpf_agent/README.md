# 天枢一号后端

Python 包名为 `dpf_agent`。默认通过 `OpenDSSEngine.DSS` COM 调用自行安装的 OpenDSS，每个完整任务使用所属子进程；同一任务的求解和复算保留原生上下文。

安装和离线首次运行请先看[根目录说明](../README.md)。所有下列命令均在本目录执行，并使用 `./.venv/Scripts/python.exe`。

运行桌面端时，该虚拟环境还供 `npm run setup:electron` 调用 Python 下载脚本，下载并校验锁定版本的 Electron；先完成后端依赖安装，再按[桌面端说明](../tianshu_ui/README.md)执行下载步骤。

## CLI

```powershell
$env:DPF_ENGINE_BACKEND = 'com'
$env:DPF_LLM_ENABLED = '0'
$env:DPF_RAG_ENABLED = '0'
$env:DPF_POWER_RAG_ENABLED = '0'
& .\.venv\Scripts\python.exe -m dpf_agent --llm off run --dss case4 -m '计算case4，重点看最低电压和网损'
& .\.venv\Scripts\python.exe -m dpf_agent inspect --task '<task_id>'
& .\.venv\Scripts\python.exe -m dpf_agent report --task '<task_id>' --format md,html
```

把 `<task_id>` 替换为实际任务 ID；`report` 命令定位已经生成的报告。`--llm` 是顶级选项，必须放在 `run` 前。`--dss` 可以是 `cases/` 下的目录名，也可以是用户自己的 `Master.dss` 路径。

正常任务默认会生成报告和结构化验证结果。终端退出码包括：`0` 成功、`20` 请求无效、`21` 数据不可用、`30` 不收敛、`40` 验证失败、`41` 已取消、`50` 系统失败。失败任务可保留诊断材料，不应作为合格计算结果使用。

## Web

```powershell
& .\.venv\Scripts\python.exe -m dpf_agent web --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000/`；API 描述位于 `/docs`，存活检查为 `/api/health`，真实可用案例清单为 `/api/cases`。仅需要后端时可自行选择其他端口；桌面端默认管理的地址固定为 `127.0.0.1:8000`。

当前接口面向本机使用，没有提供公网多用户认证部署方案。需要远程共享时应另行配置访问控制与隔离，不能直接把本机服务当作互联网服务公开。

## 配置

环境变量在启动进程前设置，`.env.example` 是格式示例；程序不会自动把示例文件加载为配置。

| 变量 | 默认行为/用途 |
|---|---|
| `DPF_ENGINE_BACKEND` | `com`；替代通道为 `cli`、`dss-python`，兼容性需另测 |
| `DPF_COM_ISOLATION` | `process`，任务进程隔离 |
| `DPF_LLM_ENABLED` | `1`；离线设置 `0` |
| `DPF_LLM_BASE_URL` | OpenAI 兼容接口，默认本地 Ollama 地址 |
| `DPF_LLM_MODEL` / `DPF_LLM_API_KEY` | 使用者自己的模型名/密钥 |
| `DPF_RAG_ENABLED` / `DPF_POWER_RAG_ENABLED` | 本地/外部检索开关；无知识数据的演示均设 `0` |
| `DPF_CASES` | 默认本目录 `cases/` |
| `DPF_WORKSPACE` / `DPF_OUTPUTS` | 默认本目录 `tasks/`、`outputs/` |
| `DPF_CHAT_DB_DIR` | 默认本目录 `chat_data/`，存放 SQLite 会话与上传副本 |

模型、知识库或外部检索未启用时，对应就绪度卡可能显示警告；独立的 COM 示例仍可运行。模型就绪度主要表示配置存在，真实可达性需实际调用验证。

## 示例与测试

`case4` 是首次运行推荐示例。其他目录为自建的变压器、应力、缺依赖及设备提取探针；可用名字以 `/api/cases` 或实际存在的 `cases/*/Master.dss` 为准。源码版不附带 IEEE 原始模型和第三方知识资料。

```powershell
& .\.venv\Scripts\python.exe -m dpf_agent.tests.run_all
```

本轮在本机新建虚拟环境安装 31 个 Python 包，`pip check` 通过，首轮 37 套后台回归通过。这是离线回归入口，包含受控引擎替身；官方测试馈线缺失时，可选的相关检查会明确跳过。它不能替代真实 COM 计算。本轮另用 CLI 对 `case4`、`case_tx` 完成真实 COM 计算，并在实际 Electron 44.8.0 窗口提交一次 `case4`，合计 3 个完整任务均为 `success`、`verification.passed=true`，每项含独立复算。桌面报告 HTTP 200，缺失案例清楚报错，窗口退出后所属后端正常回收。这些结果只对应当前机器的新建虚拟环境、空白桌面配置和这两个自建模型，不等于全新 Windows 系统验收。

`release_gate --mode preflight` 不构成完整交付验收；最终模式还要求实际原生引擎证据和交付树比对。

`python -m dpf_agent.evals.run_conversion_fidelity` 与 `python -m dpf_agent.evals.run_eval` 是独立评测入口，其中官方数据评测需使用者自行提供获授权的相应算例；源码未附这些资料，未将其作为首轮离线回归已验证内容。

完整的使用、上传及模型配置见[使用说明](../docs/使用说明.md)。

文档日期：2026-10-16；整理：codex。
