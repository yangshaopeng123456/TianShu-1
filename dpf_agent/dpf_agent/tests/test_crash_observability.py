# -*- coding: utf-8 -*-
"""缺口 2 回归测试：崩溃可观测 + 任务状态增量落盘 + interrupted 终态。

全程离线、不依赖引擎、不依赖网络、不修改真实产物。用临时目录做沙箱。

## 为什么这些断言值得写

本项目的教训是"改动生效了但没人验证"（例如转换器改了却因老产物不重建而看不出）。
崩溃可观测尤其难验：正常跑一遍**根本不会触发**，只有真崩了才有数据。
所以这里不靠"跑一遍看看"，而是**主动构造**崩溃场景并断言现场被留下来了：

- A 组：crashlog 记录/格式化/按 kind 计数
- B 组：真钩子确实接住了主线程异常，且**原钩子仍被调用**（不吞异常）
- C 组：子线程钩子、unraisable 钩子
- D 组：有界日志轮转
- E 组：记录本身失败（目录不可写）时静默降级、不抛
- F 组：state 增量落盘：原子写、含"遗言"字段、能反推阶段
- G 组：TaskStatus.INTERRUPTED / ErrorCode.INTERRUPTED 存在且被 AgentState.interrupt 用上
- H 组：run_metrics 把"有 trace 无 state.json / 无 status"判为 interrupted 并统计阶段
- I 组：service._persist_state / _mark_progress_stage 的静态契约
- J 组：server 安装钩子的时机（在 create_app 之前）与 /api/diagnostics 存在
- K 组：执行进度僵尸（陈旧 done=False）的判据单点、两个消费者共用、启动清理
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile
import threading
import time

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"  {detail}" if detail else ""))


def main() -> int:  # noqa: C901 - 测试集合，线性展开最易读
    global PASS, FAIL
    PASS = FAIL = 0

    from dpf_agent.runtime import crashlog

    tmp = tempfile.mkdtemp(prefix="crashtest_")
    try:
        # ================================================== A. crashlog 基本行为
        print("--- A. 崩溃记录与格式化 ---")
        log_path = os.path.join(tmp, "a", "crash.log")
        crashlog._reset_for_tests()
        installed = crashlog.install(log_path, max_bytes=1 << 20)
        check("A1 install 返回规范化绝对路径", os.path.isabs(installed), installed)

        try:
            raise ValueError("模拟引擎异常")
        except ValueError as exc:
            crashlog.record(exc, kind="manual", extra={"task_id": "pf-test-0001"})

        check("A2 日志文件已创建", os.path.isfile(log_path), log_path)
        text = open(log_path, encoding="utf-8").read()
        check("A3 含异常类型名", "ValueError" in text)
        check("A4 含异常消息", "模拟引擎异常" in text)
        check("A5 含完整 traceback（文件行号）", "test_crash_observability.py" in text)
        check("A6 含 kind 标记", "kind=manual" in text)
        check("A7 含自定义 extra 字段", "pf-test-0001" in text)
        check("A8 含 pid", f"pid={os.getpid()}" in text)

        diag = crashlog.diagnostics()
        check("A9 diagnostics 计数已递增", diag["records_written"] == 1, str(diag["records_written"]))
        check("A10 by_kind 归类正确", diag["by_kind"].get("manual") == 1, str(diag["by_kind"]))

        # 多来源计数互不干扰
        crashlog.record(KeyError("k1"), kind="thread")
        crashlog.record(KeyError("k2"), kind="thread")
        crashlog.record(KeyError("k3"), kind="excepthook")
        diag = crashlog.diagnostics()
        check("A11 多来源分别计数",
              diag["by_kind"].get("thread") == 2 and diag["by_kind"].get("excepthook") == 1,
              str(diag["by_kind"]))
        check("A12 未知 kind 归入 manual（不产生新类别）",
              crashlog.record(RuntimeError("x"), kind="not-a-real-kind") is not None
              and "notareal" not in "".join(crashlog.diagnostics()["by_kind"].keys()),
              str(crashlog.diagnostics()["by_kind"]))

        # record_text 通道
        before = crashlog.diagnostics()["records_written"]
        crashlog.record_text("Traceback (most recent call last):\n  fake\nBoom",
                             kind="manual", extra={"where": "unit"})
        after = crashlog.diagnostics()["records_written"]
        check("A13 record_text 也计数并落盘", after == before + 1, f"{before}→{after}")
        check("A14 record_text 内容已写入",
              "Boom" in open(log_path, encoding="utf-8").read())

        # ============================================== B. 真钩子：主线程
        print("--- B. 全局钩子（主线程） ---")
        log_b = os.path.join(tmp, "b", "crash.log")
        crashlog._reset_for_tests()
        crashlog.install(log_b, max_bytes=1 << 20)

        called = {"orig": 0}
        prev = sys.excepthook

        def _spy(exc_type, exc, tb):  # noqa: ANN001
            called["orig"] += 1

        sys.excepthook = _spy
        # 重新安装：install 会包装"当前的"钩子（即我们的 spy）
        crashlog._reset_for_tests()
        crashlog.install(log_b, max_bytes=1 << 20)

        try:
            raise RuntimeError("主线程炸了")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)

        text_b = open(log_b, encoding="utf-8").read()
        check("B1 主线程钩子写入日志", "主线程炸了" in text_b)
        check("B2 报告 kind=excepthook", "kind=excepthook" in text_b)
        check("B3 原始钩子仍被调用（不吞异常）", called["orig"] == 1, str(called))
        check("B4 含 argv 线索", "argv" in text_b)
        sys.excepthook = prev

        # ============================================== C. 真钩子：子线程 / unraisable
        print("--- C. 全局钩子（子线程 / 析构期） ---")
        log_c = os.path.join(tmp, "c", "crash.log")
        crashlog._reset_for_tests()
        crashlog.install(log_c, max_bytes=1 << 20)

        def _boom() -> None:
            raise IndexError("子线程炸了")

        t = threading.Thread(target=_boom, name="unit-thread")
        t.start()
        t.join(5)

        text_c = open(log_c, encoding="utf-8").read()
        check("C1 子线程钩子写入日志", "子线程炸了" in text_c)
        check("C2 报告 kind=thread", "kind=thread" in text_c)
        check("C3 含线程名线索", "unit-thread" in text_c)

        # unraisablehook：构造一个 __del__ 抛异常的类
        if hasattr(sys, "unraisablehook"):
            before = crashlog.diagnostics()["records_written"]
            class _Bad:
                def __del__(self):  # noqa: D105
                    raise OSError("析构炸了")
            b = _Bad()
            del b
            import gc

            gc.collect()
            after = crashlog.diagnostics()["records_written"]
            text_c2 = open(log_c, encoding="utf-8").read()
            check("C4 析构期异常被记录（不再被静默吞掉）",
                  after > before and "析构炸了" in text_c2, f"{before}→{after}")
        else:
            check("C4 析构期异常被记录（不再被静默吞掉）", True, "本 Python 无 unraisablehook，跳过")

        # ============================================== D. 有界日志
        print("--- D. 有界日志（轮转） ---")
        log_d = os.path.join(tmp, "d", "crash.log")
        crashlog._reset_for_tests()
        crashlog.install(log_d, max_bytes=2000)
        for i in range(40):
            crashlog.record(RuntimeError(f"充数 {i} " + "x" * 50), kind="manual")
        check("D1 超过上限后产生 .1 轮转文件", os.path.isfile(log_d + ".1"), log_d + ".1")
        size = os.path.getsize(log_d)
        check("D2 轮转后当前文件不失控", size <= 2000 + 4000, f"{size} 字节")
        check("D3 只保留一代（不无限堆积）", not os.path.isfile(log_d + ".2"))
        check("D4 轮转过程未产生写失败", crashlog.diagnostics()["write_errors"] == 0)

        # ============================================== E. 写失败静默降级
        print("--- E. 记录本身失败时静默降级 ---")
        # 用一个"父路径是文件"的路径模拟不可创建目录
        blocker = os.path.join(tmp, "e")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("我是一个文件，不是目录")
        bad_path = os.path.join(blocker, "sub", "crash.log")
        crashlog._reset_for_tests()
        crashlog.install(bad_path, max_bytes=1 << 20)
        raised = None
        try:
            for i in range(3):
                crashlog.record(RuntimeError(f"写不进去 {i}"), kind="manual")
        except Exception as exc:  # noqa: BLE001
            raised = exc
        check("E1 写失败不抛异常（观测设施不成为故障源）", raised is None, repr(raised))
        diag_e = crashlog.diagnostics()
        check("E2 失败被计数（可观测）", diag_e["write_errors"] >= 3, str(diag_e["write_errors"]))
        check("E3 计数仍然累加（记录意图没丢）", diag_e["records_written"] == 3,
              str(diag_e["records_written"]))

        # ============================================== F. 状态增量落盘
        print("--- F. 任务状态增量落盘 ---")
        from dpf_agent.application.service import ApplicationService
        from dpf_agent.config import AgentConfig
        from dpf_agent.schemas.enums import TaskStatus
        from dpf_agent.schemas.state import AgentState

        ws = os.path.join(tmp, "f", "pf-x")
        os.makedirs(ws, exist_ok=True)
        svc = ApplicationService(AgentConfig(workspace_root=os.path.join(tmp, "f", "tasks"),
                                             outputs_root=os.path.join(tmp, "f", "out")))
        st = AgentState(task_id="pf-x")
        svc._persist_state(st, ws, "solve")

        sp = os.path.join(ws, "state.json")
        check("F1 state.json 已写出", os.path.isfile(sp), sp)
        payload = json.load(open(sp, encoding="utf-8"))
        check("F2 含「最后落盘阶段」", payload.get("_persisted_stage") == "solve",
              str(payload.get("_persisted_stage")))
        check("F3 含落盘时间戳", isinstance(payload.get("_persisted_at"), (int, float)))
        check("F4 含可读时间串", str(payload.get("_persisted_at_str", "")).count(":") == 2,
              str(payload.get("_persisted_at_str")))
        check("F5 不含临时文件残留", not os.path.isfile(sp + ".tmp"))

        # 增量覆盖：后写的阶段应取代先写的
        svc._persist_state(st, ws, "verify")
        payload2 = json.load(open(sp, encoding="utf-8"))
        check("F6 增量写覆盖前一阶段", payload2.get("_persisted_stage") == "verify",
              str(payload2.get("_persisted_stage")))

        # 原子性：不允许出现半截 JSON（这里验证正常路径没有 .tmp 残留即可）
        check("F7 重复写不累积临时文件",
              len([n for n in os.listdir(ws) if n.endswith(".tmp")]) == 0,
              str(os.listdir(ws)))

        # _mark_progress_stage 成对推进：UI 进度与 state.json 必须同步
        svc._mark_progress_stage(st, ws, "pf-x", "report", "正在生成报告…", 92)
        prog_path = os.path.join(svc.config.workspace_root, "_current_progress.json")
        check("F8 UI 进度文件已写", os.path.isfile(prog_path), prog_path)
        prog = json.load(open(prog_path, encoding="utf-8"))
        check("F9 UI 进度阶段一致", prog.get("stage") == "report", str(prog.get("stage")))
        payload3 = json.load(open(sp, encoding="utf-8"))
        check("F10 state 阶段与 UI 同步", payload3.get("_persisted_stage") == "report",
              str(payload3.get("_persisted_stage")))

        # ============================================== G. interrupted 终态
        print("--- G. interrupted 终态与错误码 ---")
        check("G1 TaskStatus 含 interrupted",
              TaskStatus.INTERRUPTED.value == "interrupted", TaskStatus.INTERRUPTED.value)
        from dpf_agent.schemas.enums import ErrorCode

        check("G2 ErrorCode 含 INTERRUPTED", ErrorCode.INTERRUPTED.value == "INTERRUPTED")
        st2 = AgentState(task_id="pf-y")
        st2.interrupt("solve", "进程在 solve 阶段被终止")
        check("G3 interrupt() 设置终态", st2.status == TaskStatus.INTERRUPTED, str(st2.status))
        check("G4 interrupt() 错误码正确",
              (st2.error or {}).get("code") == "INTERRUPTED", str(st2.error))
        check("G5 interrupt() 记录阶段",
              (st2.error or {}).get("stage") == "solve", str(st2.error))
        check("G6 interrupt() 与 fail() 语义可区分",
              st2.status != TaskStatus.SYSTEM_FAILED)

        # ============================================== H. 指标口径
        print("--- H. 指标把「结论丢失」判为 interrupted ---")
        from dpf_agent.evals import run_metrics

        tasks_root = os.path.join(tmp, "h", "tasks")
        outputs_root = os.path.join(tmp, "h", "outputs")
        # 任务 1：完整成功（有 state.json）
        d1 = os.path.join(tasks_root, "pf-ok")
        os.makedirs(d1, exist_ok=True)
        json.dump([{"event_type": "result", "timestamp": 1000.0},
                   {"event_type": "result", "timestamp": 1002.0}],
                  open(os.path.join(d1, "trace.json"), "w", encoding="utf-8"))
        json.dump({"status": "success"}, open(os.path.join(d1, "state.json"), "w", encoding="utf-8"))
        # 任务 2：崩溃 —— 有 trace、有 state.json 但无 status、带 _persisted_stage
        d2 = os.path.join(tasks_root, "pf-crash")
        os.makedirs(d2, exist_ok=True)
        json.dump([{"event_type": "tool_call", "timestamp": 2000.0},
                   {"event_type": "tool_call", "timestamp": 2001.0}],
                  open(os.path.join(d2, "trace.json"), "w", encoding="utf-8"))
        json.dump({"task_id": "pf-crash", "status": None, "_persisted_stage": "solve"},
                  open(os.path.join(d2, "state.json"), "w", encoding="utf-8"))
        # 任务 3：更惨 —— 有 trace、完全没有 state.json
        d3 = os.path.join(tasks_root, "pf-vanish")
        os.makedirs(d3, exist_ok=True)
        json.dump([{"event_type": "tool_call", "timestamp": 3000.0},
                   {"event_type": "user", "timestamp": 3000.5}],
                  open(os.path.join(d3, "trace.json"), "w", encoding="utf-8"))

        os.makedirs(outputs_root, exist_ok=True)
        m = run_metrics.collect(tasks_root, outputs_root)
        check("H1 任务总数正确", m["tasks_total"] == 3, str(m["tasks_total"]))
        check("H2 unknown 已被消除", "unknown" not in m["status_distribution"],
              str(m["status_distribution"]))
        check("H3 崩溃任务计入 interrupted",
              m["status_distribution"].get("interrupted") == 2,
              str(m["status_distribution"]))
        check("H4 成功任务仍为 success",
              m["status_distribution"].get("success") == 1,
              str(m["status_distribution"]))
        check("H5 interrupted 单独计数", m["interrupted_tasks"] == 2, str(m["interrupted_tasks"]))
        check("H6 记录「死在哪一步」", m["interrupted_stages"].get("solve") == 1,
              str(m["interrupted_stages"]))
        check("H7 无痕迹的任务被显式标注",
              m["interrupted_stages"].get("(无痕迹)") == 1, str(m["interrupted_stages"]))
        # 成功率分母：interrupted 不该被算成成功
        # 注意：success_rate 由 collect() 按 4 位小数舍入后再返回，
        # 所以容差必须放到 1e-4 以上，否则断言的是"浮点精度"而不是"口径"。
        check("H8 成功率不含 interrupted（分母含中断任务）",
              abs(m["success_rate"] - 1 / 3) < 1e-4, str(m["success_rate"]))
        md = run_metrics.render_markdown(m)
        check("H9 看板渲染中断统计", "进程中断" in md)
        check("H10 看板含中断阶段表", "中断任务最后落盘阶段" in md)
        check("H11 看板不出现半角竖线污染表格的未转义值",
              all(row.count("|") >= 3 for row in md.splitlines() if row.startswith("| ")))

        # ============================================== I. service 静态契约
        print("--- I. service 增量落盘契约 ---")
        src_svc = inspect.getsource(ApplicationService)
        check("I1 存在 _persist_state", "_persist_state" in src_svc)
        check("I2 存在 _mark_progress_stage 配对方法", "_mark_progress_stage" in src_svc)
        check("I3 使用原子写 os.replace", "os.replace" in inspect.getsource(
            ApplicationService._persist_state))
        check("I4 落盘前 fsync（崩溃不丢缓冲区）", "fsync" in inspect.getsource(
            ApplicationService._persist_state))
        check("I5 落盘异常被吞（不阻塞主流程）",
              "except Exception" in inspect.getsource(ApplicationService._persist_state))
        # run() 里不应再有裸的 _write_progress（必须走配对方法以保证 state 同步）
        src_run = inspect.getsource(ApplicationService.run)
        bare = [
            ln.strip() for ln in src_run.splitlines()
            if "self._write_progress(" in ln
        ]
        check("I6 run() 内不再直接调 _write_progress（防漏同步）", not bare, str(bare[:3]))
        check("I7 异常分支落盘 runtime 阶段", 'self._persist_state(state, workspace, "runtime")'
              in src_run)
        check("I8 收尾落盘 final 阶段", '"final"' in src_run)
        check("I9 失败分支上报崩溃日志", "crashlog.record" in src_run)
        # 起步落盘必须在获取串行锁之前（否则锁等待期间崩溃又没痕迹）
        i_init = src_run.find('"init"')
        i_lock = src_run.find("_TASK_LOCK.acquire")
        check("I10 init 落盘早于获取串行锁", 0 < i_init < i_lock, f"init@{i_init} lock@{i_lock}")

        # ============================================== J. server 侧
        print("--- J. server 安装时机与诊断接口 ---")
        from dpf_agent.web import server as web_server

        src_server = inspect.getsource(web_server)
        check("J1 提供 _install_crash_observability", "_install_crash_observability" in src_server)
        src_main = inspect.getsource(web_server.main)
        i_hook = src_main.find("_install_crash_observability")
        i_app = src_main.find("create_app()")
        check("J2 钩子装在 create_app 之前（覆盖启动期崩溃）",
              0 < i_hook < i_app, f"hook@{i_hook} app@{i_app}")
        check("J3 create_app 失败也记日志", "crashlog.record" in src_main)
        check("J4 uvicorn 失败也记日志",
              src_main.count("crashlog.record") >= 2, str(src_main.count("crashlog.record")))
        check("J5 存在 /api/diagnostics 接口", "/api/diagnostics" in src_server)
        # 诊断接口不得因单点失败而整体 500
        src_create = inspect.getsource(web_server.create_app)
        check("J6 诊断接口分段 try（never-fail）",
              src_create.count("except Exception") >= 3, str(src_create.count("except Exception")))

        # 真正构造一次 app 并打诊断接口
        try:
            app = web_server.create_app()
            routes = [getattr(r, "path", "") for r in getattr(app, "routes", [])]
            check("J7 构造 app 成功且已挂载诊断路由",
                  "/api/diagnostics" in routes,
                  str([r for r in routes if "diagn" in r]))
            # 直接调用处理函数（避免起服务器）
            fn = None
            for r in app.routes:
                if getattr(r, "path", "") == "/api/diagnostics":
                    fn = getattr(r, "endpoint", None)
                    break
            got = fn() if fn else {}
            check("J8 诊断返回 ok 且含 crashlog 段",
                  bool(got.get("ok")) and isinstance(got.get("crashlog"), dict),
                  str({k: type(v).__name__ for k, v in (got or {}).items()}))
            check("J9 诊断含任务锁段", isinstance(got.get("task_lock"), dict),
                  str(got.get("task_lock"))[:80])
            check("J10 诊断含进程段", isinstance(got.get("process"), dict),
                  str(got.get("process"))[:80])
            # ⚠️ 路由顺序回归：`/api/tasks/stream` 必须排在 `/api/tasks/{task_id}` **之前**。
            # 顺序错误时 FastAPI 会拿 `{task_id}` 去匹配字面量 "stream"，
            # SSE 请求于是返回普通 JSON（实测 {"task_id":"stream","status":"unknown",...}），
            # 前端仍按 SSE 解析 → 拿不到任何 `event:`/`data:` 行 → **静默失效**，
            # 界面上只剩轮询在兜底，看不出异常。2026-09-17 实测发现并修复。
            #
            # 判据说明：本版 FastAPI 的 `include_router` 生成的是惰性 `_IncludedRouter`
            # 对象，子路由**不会**展开进 `app.routes`（实测其 `path` 为 `?`），
            # 因此不能靠遍历路由表判断顺序 —— 这里对**源码里的注册语句**做顺序断言。
            # 行为级验证（真起后端 + 真请求，断言 content-type 为 text/event-stream
            # 且能收到事件）由真机脚本承担，与本项目"套件不碰网络/引擎"的分工一致。
            i_stream = src_create.find("include_router(stream_api.router)")
            i_api = src_create.find("include_router(api.router)")
            i_chat = src_create.find("include_router(chat_api.router)")
            check("J12 stream_api 注册在 api.router 之前（否则 SSE 被 /api/tasks/{task_id} 吞掉）",
                  i_stream >= 0 and i_api >= 0 and i_stream < i_api,
                  "stream@%s api@%s chat@%s" % (i_stream, i_api, i_chat))
        except Exception as exc:  # noqa: BLE001
            check("J7 构造 app 成功且已挂载诊断路由", False, f"{type(exc).__name__}: {exc}")
            check("J8 诊断返回 ok 且含 crashlog 段", False, "app 构造失败")
            check("J9 诊断含任务锁段", False, "app 构造失败")
            check("J10 诊断含进程段", False, "app 构造失败")
            check("J12 stream_api 注册在 api.router 之前（否则 SSE 被 /api/tasks/{task_id} 吞掉）",
                  False, "app 构造失败")

        # 诊断接口里的 crashlog 段应反映真实计数
        crashlog._reset_for_tests()
        crashlog.install(os.path.join(tmp, "j", "crash.log"), max_bytes=1 << 20)
        crashlog.record(RuntimeError("j-probe"), kind="manual")
        d = crashlog.diagnostics()
        check("J11 诊断反映真实崩溃计数", d["records_written"] >= 1, str(d["records_written"]))

        # ============================================== K. 进度僵尸清理
        print("--- K. 执行进度僵尸（_current_progress.json） ---")
        from dpf_agent.application import progress as P
        from dpf_agent.web import api as web_api
        from dpf_agent.web import stream_api

        check("K1 单点模块导出判据与清理入口",
              all(hasattr(P, n) for n in ("is_finished", "retire_stale", "STALE_AFTER_S")),
              str([n for n in ("is_finished", "retire_stale", "STALE_AFTER_S")
                   if not hasattr(P, n)]))

        # 行为级：阈值两侧都要测。只测一侧的话，把阈值写成 0 或写成无穷
        # 都能蒙混过关 —— 而两种错法都会造成真实故障（前者误杀正在跑的任务，
        # 后者正是本次要修的"僵尸被永久认领"）。
        _now = time.time()
        check("K2 超过阈值未更新 -> 判为已结束",
              P.is_finished({"done": False, "updated_at": _now - P.STALE_AFTER_S - 1},
                            now=_now) is True)
        check("K3 未超阈值 -> 仍判为在跑（不误杀正在运行的任务）",
              P.is_finished({"done": False, "updated_at": _now - 1}, now=_now) is False)
        check("K4 已 done -> 判为已结束",
              P.is_finished({"done": True, "updated_at": _now}, now=_now) is True)
        check("K5 无时间戳且未 done -> 不判死",
              P.is_finished({"done": False}, now=_now) is False)

        # 两个消费者必须共用同一判据：各写一份早晚漂移，症状就是
        # "轮询说空闲、实时流还在推旧任务"（2026-10-09 实测）。
        src_api = inspect.getsource(web_api)
        src_stream = inspect.getsource(stream_api)
        check("K6 轮询端点用单点判据", "is_finished(" in src_api)
        check("K7 实时流用同一单点判据", "is_finished(" in src_stream)
        check("K8 两处都不再各写一份阈值字面量",
              "> 600" not in src_api and "> 600" not in src_stream,
              "api=%s stream=%s" % ("> 600" in src_api, "> 600" in src_stream))

        # 启动清理：僵尸只在**进程被 Kill / 原生库段错误**时产生
        #（`run()` 三个 except 分支都不 return，会走到收尾写 done=True），
        # 所以"启动这一刻见到 done=False 就一定是僵尸"这个前提成立。
        check("K9 main 启动时清理僵尸进度", "retire_stale" in src_main)
        _kdir = os.path.join(tmp, "k")
        os.makedirs(_kdir, exist_ok=True)
        with open(os.path.join(_kdir, P.PROGRESS_FILENAME), "w", encoding="utf-8") as f:
            json.dump({"task_id": "pf-stale", "stage": "validate_case",
                       "label": "正在做算例静态检查…", "percent": 40,
                       "updated_at": _now - 86400, "done": False}, f)
        _retired = P.retire_stale(_kdir)
        _after = P.read_progress(_kdir)
        check("K10 僵尸被置终态并回报 task_id",
              _retired == "pf-stale" and bool(_after and _after.get("done")),
              "retired=%r done=%r" % (_retired, _after and _after.get("done")))
        check("K11 对已终态的进度再清理是幂等的（返回 None）",
              P.retire_stale(_kdir) is None)

    finally:
        # 还原钩子，避免污染同进程内其它测试套件
        try:
            sys.excepthook = sys.__excepthook__
        except Exception:  # noqa: BLE001
            pass
        crashlog._reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== 结果：通过 {PASS} / 失败 {FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
