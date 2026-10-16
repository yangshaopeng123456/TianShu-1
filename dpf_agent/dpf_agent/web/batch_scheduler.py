"""One durable FIFO worker; the application's engine lock remains authoritative.

No engine is created here. The web layer injects the normal single-case runner.
Input dependency copies live beneath chat storage and never reference mutable
future uploads. Recovery is explicit: queued items pause, active items interrupt.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import uuid

from ..application.service import TaskBusyError
from ..runtime.cancellation import request_cancel
from . import batch_store, chat_store

logger = logging.getLogger(__name__)


def remove_inputs(batch_id):
    if not re.fullmatch(r"batch-[0-9a-f]{16}", batch_id):
        raise ValueError("无效批次目录")
    base = os.path.realpath(os.path.join(chat_store.DB_DIR, "batches"))
    target = os.path.realpath(os.path.join(base, batch_id))
    if os.path.normcase(os.path.commonpath([base, target])) != os.path.normcase(base) or target == base:
        raise ValueError("批次目录超出会话存储范围")
    shutil.rmtree(target, ignore_errors=True)


def prepare_inputs(case_refs, display_name, cases_root):
    from ..domain.case.case_loader import load_case_bundle

    bid = "batch-" + uuid.uuid4().hex[:16]
    root = os.path.abspath(os.path.join(chat_store.DB_DIR, "batches", bid))
    items = []
    try:
        for ref in case_refs:
            iid = "item-" + uuid.uuid4().hex[:16]
            workspace = os.path.join(root, iid, "source")
            case = load_case_bundle(ref, workspace, cases_root)
            items.append({"id": iid, "case_ref": os.path.join(workspace, "input", case.master_dss_ref),
                          "original_case_ref": ref, "display_name": display_name(ref)})
    except Exception:
        remove_inputs(bid)
        raise
    return bid, items


class BatchScheduler:
    def __init__(self, execute, *, cancel_task=request_cancel, idle_seconds=.25):
        self.execute = execute
        self.cancel_task = cancel_task
        self.idle_seconds = idle_seconds
        self.owner = uuid.uuid4().hex
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._active_task = None
        self._guard = threading.RLock()

    def start(self, *, recover=True):
        with self._guard:
            if self._thread and self._thread.is_alive():
                return self
            if self._stop.is_set():
                raise RuntimeError("批次调度器已关闭")
            if recover:
                batch_store.recover()
            self._thread = threading.Thread(target=self._loop, name="chat-batch-worker", daemon=True)
            self._thread.start()
        return self

    def notify(self):
        self._wake.set()

    def _started(self, item, task_id):
        with self._guard:
            cancelled = batch_store.bind_task(item, self.owner, task_id)
            self._active_task = task_id
            if cancelled or self._stop.is_set():
                self.cancel_task(task_id)

    def cancel(self, sid, bid, item_id=None):
        with self._guard:
            task_ids = batch_store.cancel(sid, bid, item_id)
            for tid in task_ids:
                self.cancel_task(tid)
            self.notify()

    def _loop(self):
        while not self._stop.is_set():
            item = None
            try:
                item = batch_store.claim(self.owner)
                if item is None:
                    self._wake.wait(self.idle_seconds)
                    self._wake.clear()
                    continue
                if self._stop.is_set():
                    batch_store.interrupt_owner(self.owner)
                    break
                result = self.execute(item, lambda tid: self._started(item, tid))
                batch_store.complete(item, self.owner, result)
            except TaskBusyError:
                if item:
                    batch_store.requeue_busy(item, self.owner)
                self._stop.wait(self.idle_seconds)
            except Exception as exc:  # one item cannot discard the rest of a batch
                logger.exception("[batch] item execution failed: %s", item and item["id"])
                if item:
                    try:
                        batch_store.complete(item, self.owner, {
                            "task_status": "system_failed", "error": f"{type(exc).__name__}: {exc}",
                            "content": batch_store._head(item) + f"执行异常：{type(exc).__name__}: {exc}",
                        })
                    except Exception:
                        # A database failure must stop scheduling rather than
                        # repeatedly run work whose result cannot be committed.
                        logger.exception("[batch] cannot persist item failure; worker stopped")
                        self._stop.set()
                else:
                    self._stop.wait(self.idle_seconds)
            finally:
                with self._guard:
                    self._active_task = None

    def close(self, timeout=1):
        """Stop claiming immediately. A process shutdown is not user cancellation."""
        with self._guard:
            self._stop.set()
            self._wake.set()
            # Fence late message/result publication before asking the solver to
            # stop. Shutdown is interruption, even if cancellation is its cleanup.
            batch_store.interrupt_owner(self.owner)
            # Cooperative interruption lets the normal task cleanup run. The
            # durable batch outcome remains interrupted if no result arrives.
            if self._active_task:
                self.cancel_task(self._active_task)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)


_scheduler = None
_scheduler_guard = threading.Lock()
_closing = False


def start(execute):
    global _scheduler, _closing
    with _scheduler_guard:
        _closing = False
        if _scheduler is None:
            _scheduler = BatchScheduler(execute).start()
        return _scheduler


def current(execute):
    global _scheduler
    with _scheduler_guard:
        if _closing:
            raise RuntimeError("后端正在关闭，批次未启动")
        if _scheduler is None:
            _scheduler = BatchScheduler(execute).start()
        return _scheduler


def close():
    global _scheduler, _closing
    with _scheduler_guard:
        _closing = True
        scheduler, _scheduler = _scheduler, None
    if scheduler:
        scheduler.close()
