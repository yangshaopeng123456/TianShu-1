"""崩溃可观测：把进程级异常落到独立日志，供事后复盘（缺口 2）。

## 为什么需要它（实测证据）

`tianshu-backend.log` 有 **7525 行、36 次后端启动记录、多次 `backend exited code=1`**，
但**一个 Traceback 都没有**——因为 uvicorn 的 access log 只记请求，
进程级异常（主线程未捕获异常、子线程异常、原生库段错误前的 Python 异常）
根本没有落到任何地方。崩了十几次，现场全部丢失，无法复盘。

本模块补齐三件事：

1. **`install()`** —— 安装 `sys.excepthook`（主线程）、`threading.excepthook`（子线程），
   以及 `sys.unraisablehook`（析构期异常）；
2. **`record()`** —— 供业务代码主动上报（例如 uvicorn 入口的 except 分支）；
3. **有界日志** —— 单个文件超过 `max_bytes` 就轮转，避免磁盘被写满。

## 设计原则（重要，勿改错）

- **绝不吞掉原异常**：所有钩子在记录后仍调用原始钩子（`_chain_original`），
  否则会把"崩溃"变成"静默消失"，比没有日志更危险。
- **记录本身绝不抛异常**：磁盘满 / 目录只读 / 权限不足时一律静默降级——
  日志是观测设施，不能成为新的故障源。
- **落盘用 `os.fsync`**：崩溃后进程可能立即消失，缓冲区里的内容会丢。
- **日志目录独立于任务工作区**：进程级崩溃可能发生在任何任务之前/之间，
  挂在某个任务目录下会随任务清理一起丢掉。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import threading
import traceback
from typing import Any, TextIO

# 单条记录分隔线：便于人工阅读，也便于按行切割做统计
_SEP = "=" * 78
_SUB = "-" * 78

_lock = threading.Lock()
_state: dict[str, Any] = {
    "installed": False,
    "path": "",
    "max_bytes": 2 * 1024 * 1024,
    "seq": 0,          # 本进程内已写入的记录数（诊断用）
    "write_errors": 0,  # 写失败次数（诊断用；始终为静默降级）
    "counts": {},      # 按 kind 计数：excepthook / thread / unraisable / manual / watchdog
}

_VALID_KINDS = ("excepthook", "thread", "unraisable", "manual", "watchdog")


def crash_log_path() -> str:
    """当前崩溃日志路径（未安装时返回将要使用的默认路径）。"""
    return _state["path"]


def diagnostics() -> dict[str, Any]:
    """观测入口：已写入条数、失败次数、按来源分类计数。"""
    with _lock:
        return {
            "installed": bool(_state["installed"]),
            "path": _state["path"],
            "max_bytes": int(_state["max_bytes"]),
            "records_written": int(_state["seq"]),
            "write_errors": int(_state["write_errors"]),
            "by_kind": dict(_state["counts"]),
        }


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rotate_if_needed(path: str, max_bytes: int, incoming: int = 0) -> None:
    """超过上限就把当前文件改名为 `.1`（只保留一代），失败则静默忽略。

    只留一代是有意的：崩溃日志是"最近发生了什么"的线索，
    保留历史意义有限，而多代轮转本身会占用磁盘。需要长期留存的
    应当由外部收集（复制走），而不是在这里积累。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size + incoming <= max_bytes:
        return
    try:
        os.replace(path, path + ".1")
    except OSError:
        # 改名失败不能阻塞记录：宁可继续往原文件追加（可能超限），
        # 也不要因为轮转失败把这条崩溃记录丢掉。
        pass


def _append_block(block: str) -> None:
    """把一段已格式化的文本写盘。**这是唯一写盘点**，所有失败都在此静默降级。"""
    path = _state["path"]
    if not path:
        return
    data = block.encode("utf-8", errors="replace")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate_if_needed(path, int(_state["max_bytes"]), incoming=len(data))
        with open(path, "ab") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, ValueError):
                # 某些文件系统/句柄不支持 fsync；已 flush，尽力而为。
                pass
    except Exception:  # noqa: BLE001 - 记录崩溃时绝不能因记录本身再次崩溃
        try:
            _state["write_errors"] = int(_state["write_errors"]) + 1
        except Exception:  # noqa: BLE001
            pass


def _format(
    kind: str,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: Any,
    *,
    thread_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    name = getattr(exc_type, "__name__", None) or (type(exc).__name__ if exc else "Unknown")
    msg = str(exc) if exc is not None else ""
    lines = [
        _SEP,
        f"[{_now()}] 崩溃记录  kind={kind}  pid={os.getpid()}  thread={thread_name or 'main'}",
        f"异常类型：{name}",
        f"异常信息：{msg}",
    ]
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}：{v}")
    lines.append(_SUB)
    if tb is not None:
        # 用 format_exception 而不是 format_tb：前者包含异常类型与消息，
        # 单独一段就能完整还原现场（后者只有栈帧）。
        try:
            lines.append("".join(
                traceback.format_exception(exc_type, exc, tb)
            ).rstrip("\n"))
        except Exception:  # noqa: BLE001 - 某些异常对象的 __str__ 本身会抛
            lines.append("（traceback 格式化失败，退化为栈帧）")
            try:
                lines.append("".join(traceback.format_tb(tb)).rstrip("\n"))
            except Exception:  # noqa: BLE001
                lines.append("（栈帧也不可读）")
    else:
        lines.append("（无 traceback）")
    lines.append(_SEP)
    lines.append("")
    return "\n".join(lines) + "\n"


def record(
    exc: BaseException,
    *,
    kind: str = "manual",
    extra: dict[str, Any] | None = None,
    tb: Any = None,
) -> str | None:
    """主动上报一次异常。返回写盘的文本（未安装时返回 None）。

    `kind` 用于区分来源；未知值归入 `manual`，避免把归类错误
    （而不是崩溃本身）变成问题。
    """
    k = kind if kind in _VALID_KINDS else "manual"
    with _lock:
        _state["seq"] = int(_state["seq"]) + 1
        counts = _state["counts"]
        counts[k] = int(counts.get(k, 0)) + 1
        block = _format(
            k,
            type(exc),
            exc,
            tb if tb is not None else exc.__traceback__,
            thread_name=threading.current_thread().name,
            extra=extra,
        )
        _append_block(block)
    return block


def record_text(text: str, *, kind: str = "manual", extra: dict[str, Any] | None = None) -> None:
    """上报一段已经格式化好的文本（用于 `format_exception` 的现成结果）。"""
    k = kind if kind in _VALID_KINDS else "manual"
    with _lock:
        _state["seq"] = int(_state["seq"]) + 1
        counts = _state["counts"]
        counts[k] = int(counts.get(k, 0)) + 1
        head = [
            _SEP,
            f"[{_now()}] 崩溃记录  kind={k}  pid={os.getpid()}  "
            f"thread={threading.current_thread().name}",
        ]
        if extra:
            head.extend(f"{key}：{val}" for key, val in extra.items())
        head.append(_SUB)
        _append_block("\n".join(head) + "\n" + text.rstrip("\n") + "\n" + _SEP + "\n\n")


def install(path: str | None = None, max_bytes: int = 2 * 1024 * 1024) -> str:
    """安装全局异常钩子并返回日志路径。重复调用只更新路径/上限，不重复挂钩子。

    `path` 为空时使用环境变量 `DPF_CRASH_LOG`；再为空则落在项目根的
    `outputs/_logs/backend_crash.log`。
    """
    from ..config import PROJECT_ROOT  # 延迟导入：避免模块导入期依赖配置

    resolved = path or os.environ.get("DPF_CRASH_LOG") or os.path.join(
        PROJECT_ROOT, "outputs", "_logs", "backend_crash.log"
    )
    resolved = os.path.abspath(resolved)

    already = bool(_state["installed"])
    with _lock:
        _state["path"] = resolved
        _state["max_bytes"] = int(max_bytes)
        _state["installed"] = True

    if already:
        return resolved

    # ---- 主线程未捕获异常 ----
    prev_excepthook = sys.excepthook

    def _excepthook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        try:
            record(exc if exc is not None else exc_type(f"{exc_type.__name__}"),  # type: ignore[misc]
                   kind="excepthook", tb=tb,
                   extra={"argv": " ".join(sys.argv[:8])})
        except Exception:  # noqa: BLE001
            pass
        # 关键：仍然交给原始钩子，保证 stderr 上的现场不被吞掉。
        prev_excepthook(exc_type, exc, tb)

    sys.excepthook = _excepthook

    # ---- 子线程未捕获异常 ----
    prev_thread_hook = getattr(threading, "excepthook", None)

    def _thread_hook(args):  # type: ignore[no-untyped-def]
        try:
            if args.exc_value is not None:
                record(
                    args.exc_value, kind="thread", tb=args.exc_traceback,
                    extra={"thread": getattr(args.thread, "name", "?")},
                )
        except Exception:  # noqa: BLE001
            pass
        if callable(prev_thread_hook):
            prev_thread_hook(args)

    if prev_thread_hook is not None:
        threading.excepthook = _thread_hook  # type: ignore[assignment]
    else:
        # 极端老版本 Python：至少保证子线程崩溃不再静默。
        threading.excepthook = _thread_hook  # type: ignore[assignment]

    # ---- 析构期异常（__del__ 里抛，否则会被 "Exception ignored" 吞掉） ----
    prev_unraisable = getattr(sys, "unraisablehook", None)

    def _unraisable_hook(args):  # type: ignore[no-untyped-def]
        try:
            exc = args.exc_value
            if exc is not None:
                record(
                    exc, kind="unraisable", tb=args.exc_traceback,
                    extra={"object": repr(args.object)[:120]},
                )
        except Exception:  # noqa: BLE001
            pass
        if callable(prev_unraisable):
            prev_unraisable(args)

    if prev_unraisable is not None:
        sys.unraisablehook = _unraisable_hook  # type: ignore[assignment]

    return resolved


def _reset_for_tests() -> None:
    """仅供测试：清空内部计数与路径（不还原钩子，避免测试间相互污染更难查）。"""
    with _lock:
        _state["seq"] = 0
        _state["write_errors"] = 0
        _state["counts"] = {}
        _state["installed"] = False
        _state["path"] = ""
