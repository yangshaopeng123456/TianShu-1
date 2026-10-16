"""FastAPI 应用构造与启动入口。"""
from __future__ import annotations

import os
import sys
import traceback

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from . import api
from . import chat_api
from . import chat_store
from . import stream_api


def _install_crash_observability() -> str:
    """安装进程级崩溃钩子，返回崩溃日志路径。

    ## 为什么必须在**创建 app 之前**做

    `create_app()` 会 `chat_store.init_db()`（建库/建表）、挂载路由、初始化静态目录，
    任何一步都可能抛异常。若钩子装在其后，这段窗口期的崩溃就仍然无日志——
    而这恰恰是"启动即死"最常见的形态（`tianshu-launcher.log` 里
    `backend exited code=1` 多数发生在启动阶段）。

    钩子本身绝不抛异常：它是观测设施，不能成为故障源。
    """
    from ..runtime import crashlog

    try:
        path = crashlog.install()
    except Exception:  # noqa: BLE001
        return ""
    return path


def _install_watchdog() -> bool:
    """启动引擎中毒看门狗（方案 B），返回是否已启用。

    与崩溃钩子同理，必须在**开始服务之前**装好：一旦第一个任务把引擎用坏，
    看门狗必须已经在盯着了。看门狗自身绝不抛异常——它是观测设施，
    不能成为故障源。
    """
    from ..runtime import watchdog

    try:
        return bool(watchdog.install())
    except Exception:  # noqa: BLE001
        return False


def create_app() -> FastAPI:
    """构造 FastAPI 应用。"""
    # 注意：不要在这里 `import dss`。dss-python 原生库初始化在 Electron 派生的
    # 子进程里会死锁（实测后端永远起不来，60s 超时），且项目本身通过
    # opendss_adapter 用 importlib 按需导入，无需预加载。
    chat_store.init_db()
    app = FastAPI(
        title="天枢一号",
        description="配电网潮流计算智能体“天枢一号”的 Web 服务层（V1.3）",
        version=__version__,
    )
    # ⚠️ **注册顺序有语义**：FastAPI 按注册顺序匹配路由。
    # `/api/tasks/stream` 必须排在 `api.router` 之前，否则会被
    # `/api/tasks/{task_id}` 抢先匹配（task_id 取到字面量 "stream"）。
    # 2026-09-17 实测该顺序错误时的表现：SSE 请求返回
    # `{"task_id":"stream","status":"unknown",...}` 的普通 JSON，
    # 前端按 SSE 解析拿不到任何 `event:`/`data:` 行 → **静默失效**，
    # 只有轮询在兜底，界面上看不出问题。
    # 同类坑的另一个已知案例：`/api/recent-tasks` 刻意不用 `/api/tasks/recent`
    # （见 chat_api.py 的注释）——同一类冲突，换个写法避开。
    app.include_router(stream_api.router)
    app.include_router(api.router)
    app.include_router(chat_api.router)

    @app.on_event("startup")
    def _start_chat_batches():
        from . import batch_scheduler
        batch_scheduler.start(chat_api._execute_batch_item)

    @app.on_event("shutdown")
    def _close_chat_batches():
        from . import batch_scheduler
        batch_scheduler.close()

    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ---- 崩溃可观测性 + 诊断接口 ----
    #
    # 背景（实测）：`tianshu-backend.log` 有 7525 行、36 次启动、多次
    # `backend exited code=1`，却**一个 Traceback 都没有** —— 因为 uvicorn 的
    # access log 只记请求，进程级异常没有任何落点。崩了十几次，现场全部丢失。
    #
    # 这里补上：① 全局异常钩子落盘到独立日志；② `/api/diagnostics` 暴露
    # 崩溃计数与最后落盘阶段，便于"崩了没有"这种问题不必翻文件就能回答。
    crash_path = _install_crash_observability()

    @app.get("/api/diagnostics")
    def _diagnostics() -> dict:
        """运行时诊断快照：崩溃记录、任务锁、引擎代次。

        刻意做成 `never-fail`：诊断接口本身报错会掩盖被诊断的问题，
        所以每一段都独立 try，取不到就返回 null 而不是 500。
        """
        from ..runtime import crashlog

        out: dict = {"ok": True}
        try:
            out["crashlog"] = crashlog.diagnostics()
        except Exception as exc:  # noqa: BLE001
            out["crashlog"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            from ..application.service import current_task_lock

            out["task_lock"] = current_task_lock()
        except Exception as exc:  # noqa: BLE001
            out["task_lock"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            from ..runtime import watchdog

            out["watchdog"] = watchdog.diagnostics()
        except Exception as exc:  # noqa: BLE001
            out["watchdog"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            import platform
            import time as _t

            out["process"] = {
                "pid": os.getpid(),
                "python": platform.python_version(),
                "started_hint": _t.strftime("%Y-%m-%d %H:%M:%S"),
                "argv": sys.argv[:6],
            }
        except Exception as exc:  # noqa: BLE001
            out["process"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    host = os.environ.get("DPF_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("DPF_WEB_PORT", "8000"))
    # 进程 cwd 可能被 OpenDSS 改写；配置模块已锚定项目根目录，这里仅记录启动配置。
    #
    # uvicorn 的日志配置会重置 root logger，但**不会**动 sys.excepthook，
    # 所以钩子在 uvicorn.run 之前装好即可覆盖整个进程生命周期。
    crash_path = _install_crash_observability()
    if crash_path:
        print(f"崩溃日志：{crash_path}")
    # 引擎中毒看门狗：必须在服务开始前启动，否则第一个任务就可能把引擎
    # 用坏而没人盯着（看门狗触发时以非 0 退出码退出，由 Electron 自动重启）。
    if _install_watchdog():
        print("引擎看门狗：已启用（判定进程无法自愈时自动重启）")
    try:
        app = create_app()
    except BaseException as exc:  # noqa: BLE001 - 启动期崩溃是最需要留证的一类
        from ..runtime import crashlog

        crashlog.record(
            exc, kind="manual",
            extra={"stage": "create_app", "where": "server.main"},
        )
        print(f"create_app 失败，已记录崩溃日志：{crash_path}")
        traceback.print_exc()
        return 1
    # ---- 启动清理：上一次进程被 Kill / 原生库段错误留下的脏进度 ----
    # 那种情况下 `_current_progress.json` 停在 done=False（`finally` 都跑不到）。
    # 它是全局复用的：界面连上实时流会把它当成「当前任务」——2026-10-09 实测，
    # 发一句「你好」，右侧执行过程面板却显示 10-04 卡在 validate_case 的旧任务。
    # 启动这一刻旧任务必然已结束（同一时刻只跑一个任务），直接置终态。
    # 清理失败不影响启动（retire_stale 内部吞异常）。
    try:
        from ..application.progress import retire_stale
        from ..config import AgentConfig

        _retired = retire_stale(AgentConfig().workspace_root)
    except Exception:  # noqa: BLE001
        _retired = None
    if _retired:
        print(f"已清理上次未正常结束的进度残留：{_retired}")
    print(f"启动 Web 服务：http://{host}:{port}/")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except BaseException as exc:  # noqa: BLE001 - 端口占用/绑定失败等
        from ..runtime import crashlog

        crashlog.record(
            exc, kind="manual",
            extra={"stage": "uvicorn.run", "where": "server.main",
                   "host": host, "port": port},
        )
        print(f"uvicorn 启动失败，已记录崩溃日志：{crash_path}")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
