"""CLI 入口（文档 14）。

python -m dpf_agent run --dss ./Master.dss --solver auto
python -m dpf_agent run --dss ./Master.dss --solver-config ./solver.json --result-scope bus,branch,system
python -m dpf_agent run --dss case4 --message "计算这个配电网，重点看最低电压和网损"
python -m dpf_agent inspect --task <task_id>
python -m dpf_agent report --task <task_id> --format md,html
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ..application.service import ApplicationService
from ..config import AgentConfig
from ..schemas.enums import EXIT_CODES, OutputFormat, ResultScope, TaskStatus
from ..schemas.task_spec import SolverConfig, UserRequest


def _build_config(args: argparse.Namespace) -> AgentConfig:
    cfg = AgentConfig()
    if getattr(args, "workspace", None):
        cfg.workspace_root = args.workspace
    if getattr(args, "outputs", None):
        cfg.outputs_root = args.outputs
    if getattr(args, "llm", None) == "off":
        cfg.llm_enabled = False
    return cfg


def cmd_run(args: argparse.Namespace) -> int:
    service = ApplicationService(_build_config(args))

    solver_config = SolverConfig()
    if args.solver_config:
        with open(args.solver_config, encoding="utf-8") as f:
            data = json.load(f)
        solver_config = SolverConfig(**data)
    if args.max_iterations is not None:
        solver_config.max_iterations = args.max_iterations
    if args.tolerance is not None:
        solver_config.tolerance = args.tolerance

    scopes = (
        [ResultScope(s.strip()) for s in args.result_scope.split(",")]
        if args.result_scope
        else None
    )
    formats = (
        [OutputFormat(f.strip()) for f in args.output_format.split(",")]
        if args.output_format
        else None
    )

    request = UserRequest(
        message=args.message or "",
        case_ref=args.dss,
        solver_config=solver_config if (solver_config.max_iterations or solver_config.tolerance) else None,
    )
    if scopes:
        request.result_scopes = scopes
    if formats:
        request.output_formats = formats

    response = service.run(request, cli_callback=print)
    status = TaskStatus(response["status"])
    print(f"\n[task] task_id={response['task_id']}")
    print(f"[task] 输出目录: {response['output_dir']}")
    if response.get("error"):
        print(f"[task] 失败: {response['error']['code']} — {response['error']['message']}")
    return EXIT_CODES[status]


def cmd_inspect(args: argparse.Namespace) -> int:
    service = ApplicationService(_build_config(args))
    info = service.inspect(args.task)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    # 报告已在 run 时生成；此处定位并打印路径
    for root in (cfg.workspace_root, cfg.outputs_root):
        candidate = os.path.join(root, args.task, "reports")
        if os.path.isdir(candidate):
            for fn in sorted(os.listdir(candidate)):
                print(os.path.join(candidate, fn))
            return 0
        candidate2 = os.path.join(root, args.task)
        if os.path.isdir(candidate2):
            for fn in sorted(os.listdir(candidate2)):
                if fn.startswith("report") or fn.endswith(".csv"):
                    print(os.path.join(candidate2, fn))
            return 0
    print(f"未找到任务 {args.task} 的报告", file=sys.stderr)
    return 1


def cmd_web(args: argparse.Namespace) -> int:
    from ..web.server import main as web_main

    os.environ.setdefault("DPF_WEB_HOST", args.host)
    os.environ.setdefault("DPF_WEB_PORT", str(args.port))
    # Web 服务运行期间当前目录可能被引擎副作用改变，固定到项目根目录，
    # 不依赖调用者从哪个 cwd 启动，确保聊天历史与任务产物始终使用同一处。
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    os.environ.setdefault("DPF_WORKSPACE", os.path.join(project_root, "tasks"))
    os.environ.setdefault("DPF_OUTPUTS", os.path.join(project_root, "outputs"))
    os.environ.setdefault("DPF_CASES", os.path.join(project_root, "cases"))
    return web_main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dpf_agent", description="天枢一号 V1.3 — 配电网潮流计算智能体（CLI-first）"
    )
    parser.add_argument("--workspace", help="任务工作目录根（默认 ./tasks）")
    parser.add_argument("--outputs", help="输出目录根（默认 ./outputs）")
    parser.add_argument("--llm", choices=["auto", "off"], default="auto", help="本地大模型开关")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行潮流计算任务")
    p_run.add_argument("--dss", required=True, help="Master.dss 路径或内置合成算例名（如 case4）")
    p_run.add_argument("--message", "-m", default="", help="自然语言任务描述")
    p_run.add_argument("--solver", default="auto", help="求解策略（V1.3 固定 auto → OpenDSS）")
    p_run.add_argument("--solver-config", help="求解参数 JSON 文件")
    p_run.add_argument("--max-iterations", type=int, default=None)
    p_run.add_argument("--tolerance", type=float, default=None)
    p_run.add_argument("--result-scope", help="bus,branch,transformer,system")
    p_run.add_argument("--output-format", help="json,md,html,csv")
    p_run.set_defaults(func=cmd_run)

    p_ins = sub.add_parser("inspect", help="查看任务状态与验证结果")
    p_ins.add_argument("--task", required=True, help="task_id")
    p_ins.set_defaults(func=cmd_inspect)

    p_rep = sub.add_parser("report", help="定位任务报告文件")
    p_rep.add_argument("--task", required=True, help="task_id")
    p_rep.add_argument("--format", default="md,html")
    p_rep.set_defaults(func=cmd_report)

    p_web = sub.add_parser("web", help="启动 Web 服务")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_web.add_argument("--port", type=int, default=8000, help="监听端口")
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
