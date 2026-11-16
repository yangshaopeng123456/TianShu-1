"""Script and loopback API entry points; no legacy desktop/chat bootstrap."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

from .jobs import TERMINAL
from .service import PROFESSIONAL_VERSION, ProfessionalService, atomic_text


def _write_new_bundle(target: Path, data: bytes):
    """Publish new bytes atomically without overwriting any existing target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tianshu-bundle-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard link publishes complete bytes with no replace.
        # Existing files/symlinks, including racing writers, cause failure.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="天智五号独立专业核心 " + PROFESSIONAL_VERSION)
    parser.add_argument("--state-dir", required=True, help="独立状态目录，不使用旧聊天/任务目录")
    parser.add_argument("--cases-root", help="已注册内置算例根目录")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities")
    model_import = commands.add_parser("import-model")
    import_source = model_import.add_mutually_exclusive_group(required=True)
    import_source.add_argument("--directory", help="用户明确选择的本机模型目录，只读打包")
    import_source.add_argument("--payload", help="UTF-8导入合同JSON；支持原生包或MATPOWER平衡子集")
    model_import.add_argument("--master", help="--directory方式的DSS入口文件名")
    model_inspect = commands.add_parser("inspect-model")
    model_inspect.add_argument("--snapshot-id", required=True, help="已导入的不可变模型ID")
    comparison = commands.add_parser("compare-reference", help="只读比较已提交结果与明确参考，不重新求解")
    comparison.add_argument("--task-id", required=True)
    comparison.add_argument("--reference", required=True, help="UTF-8参考合同JSON；不自动认证工程来源")
    comparison.add_argument("--output", required=True, help="新的比较JSON文件，不能覆盖历史证据")
    collection = commands.add_parser("compare-reference-collection", help="只读汇总明确参考分包；完整覆盖不代表工程认可")
    collection.add_argument("--task-id", required=True)
    collection.add_argument("--collection", required=True, help="UTF-8参考集合JSON；最多8包，总计20000点")
    collection.add_argument("--output", required=True, help="新的比较JSON文件，不能覆盖历史证据")
    bundle = commands.add_parser("export-report-bundle", help="只读导出完整离线ZIP；解压后打开report.html")
    bundle.add_argument("--task-id", required=True)
    bundle.add_argument("--output", required=True, help="新的ZIP文件，不能覆盖历史证据")
    pdf = commands.add_parser("export-report-pdf", help="从已提交HTML报告派生PDF，不重算")
    pdf.add_argument("--task-id", required=True)
    pdf.add_argument("--output", required=True)
    study_run = commands.add_parser("run-study", help="显式曲线驱动的固定控制准静态时序计算")
    study_run.add_argument("--request", required=True)
    study_run.add_argument("--timeout", type=float, default=600)
    study_get = commands.add_parser("study-status", help="读取已停止服务的时序状态；运行中用API")
    study_get.add_argument("--study-id", required=True)
    study_export = commands.add_parser("export-study", help="只读导出已提交时序产物")
    study_export.add_argument("--study-id", required=True)
    study_export.add_argument("--format", choices=["html","json","csv","pdf"], required=True)
    study_export.add_argument("--output", required=True)
    scenario_compare = commands.add_parser("compare-scenarios", help="读取两个已提交任务，按相同物理口径比较")
    scenario_compare.add_argument("--left-task-id", required=True)
    scenario_compare.add_argument("--right-task-id", required=True)
    scenario_compare.add_argument("--output", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", required=True, help="UTF-8结构化JSON请求文件")
    run.add_argument("--timeout", type=float, default=180)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=18002)
    args = parser.parse_args(argv)
    if args.command == "import-model" and ((args.directory is not None) != (args.master is not None)):
        parser.error("--directory requires --master; --payload does not use --master")
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        service = ProfessionalService(args.state_dir, args.cases_root)
        token = secrets.token_urlsafe(32)
        try:
            atomic_text(service.root / "local-service.json", json.dumps({"url":f"http://127.0.0.1:{args.port}", "token":token, "service":"tianshu-professional"}, indent=2))
            uvicorn.run(create_app(service, token, close_on_shutdown=True), host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            service.close()
        return 0
    with ProfessionalService(args.state_dir, args.cases_root, start_worker=args.command in {"run","run-study"}) as service:
        if args.command == "capabilities":
            print(json.dumps(service.capabilities(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "import-model":
            from .models import pack_directory
            if args.payload is not None:
                with Path(args.payload).open("rb") as stream:
                    body = stream.read(32 * 1024 * 1024 + 1)
                if len(body) > 32 * 1024 * 1024:
                    parser.error("import payload exceeds the 32 MiB wire budget")
                payload = json.loads(body.decode("utf-8-sig"))
            else:
                payload = pack_directory(args.directory, args.master)
            result = service.import_model(payload)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "inspect-model":
            print(json.dumps(service.inspect_model(args.snapshot_id), ensure_ascii=False, indent=2))
            return 0
        if args.command == "study-status":
            print(json.dumps(service.get_study(args.study_id),ensure_ascii=False,indent=2))
            return 0
        if args.command in {"export-study","compare-scenarios"}:
            target=Path(args.output)
            if target.exists() or target.is_symlink():parser.error("output already exists")
            if args.command=="compare-scenarios":
                data=json.dumps(service.compare_scenarios(args.left_task_id,args.right_task_id),ensure_ascii=False,indent=2,allow_nan=False).encode("utf-8")
            elif args.format=="pdf":data=service.export_study_pdf(args.study_id)
            else:data=service.studies.artifact(args.study_id,{"html":"report.html","json":"result.json","csv":"points.csv"}[args.format])
            _write_new_bundle(target,data)
            print(json.dumps({"output":str(target),"bytes":len(data),"recomputed":False},ensure_ascii=False))
            return 0
        if args.command == "export-report-pdf":
            target=Path(args.output)
            if target.exists() or target.is_symlink():parser.error("PDF output already exists")
            data=service.export_report_pdf(args.task_id)
            _write_new_bundle(target,data)
            print(json.dumps({"task_id":args.task_id,"output":str(target),"bytes":len(data),"recalculated":False},ensure_ascii=False))
            return 0
        if args.command == "export-report-bundle":
            target = Path(args.output)
            if target.exists() or target.is_symlink():
                parser.error("bundle output already exists; choose a new evidence file")
            data = service.export_report_bundle(args.task_id)
            _write_new_bundle(target, data)
            print(json.dumps({"task_id":args.task_id, "output":str(target), "bytes":len(data), "format":"zip", "engineering_accepted":False}, ensure_ascii=False))
            return 0
        if args.command in {"compare-reference", "compare-reference-collection"}:
            target = Path(args.output)
            if target.exists():
                parser.error("comparison output already exists; choose a new evidence file")
            is_collection = args.command == "compare-reference-collection"
            budget = (8 if is_collection else 4) * 1024 * 1024
            with Path(args.collection if is_collection else args.reference).open("rb") as stream:
                body = stream.read(budget + 1)
            if len(body) > budget:
                parser.error("reference collection exceeds the 8 MiB contract budget" if is_collection else "reference exceeds the 4 MiB contract budget")
            reference = json.loads(body.decode("utf-8-sig"))
            report = (service.compare_reference_collection if is_collection else service.compare_reference)(args.task_id, reference)
            atomic_text(target, json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
            return 0 if report["passed"] else 1
        payload = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))
        is_study=args.command=="run-study"
        task = service.submit_study(payload) if is_study else service.submit(payload)
        ident=task["study_id" if is_study else "task_id"]
        deadline = time.monotonic() + max(args.timeout, 1)
        while task["execution_status"] not in TERMINAL:
            if time.monotonic() >= deadline:
                (service.cancel_study if is_study else service.cancel)(ident)
                print(json.dumps({"id":ident, "status":"wait_timeout_cancel_requested"}))
                return 2
            time.sleep(.1)
            task = (service.get_study if is_study else service.get_task)(ident)
        print(json.dumps(task, ensure_ascii=False, indent=2))
        return 0 if task["execution_status"] == "finished" and (task.get("completion_status")=="complete" if is_study else task["numerical_validity"]=="converged_valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
