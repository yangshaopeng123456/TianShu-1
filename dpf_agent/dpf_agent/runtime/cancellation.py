"""任务取消登记处。

后端在本进程内维护"已请求取消"的任务集合；工作流在每个阶段入口检查，
命中即以 CANCELLED 终态中断，而不是继续把 40 秒的潮流算完。

说明：这是进程内实现（单实例桌面应用足够）。若要支持多实例/分布式，
应改为共享存储（文件或 redis）版本的 is_cancelled。
"""
from __future__ import annotations

import threading

_cancelled: set[str] = set()
_lock = threading.Lock()


def request_cancel(task_id: str) -> None:
    if not task_id:
        return
    with _lock:
        _cancelled.add(task_id)


def is_cancelled(task_id: str | None) -> bool:
    if not task_id:
        return False
    with _lock:
        return task_id in _cancelled


def clear(task_id: str) -> None:
    with _lock:
        _cancelled.discard(task_id)


def pending() -> list[str]:
    with _lock:
        return sorted(_cancelled)
