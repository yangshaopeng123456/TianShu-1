"""实时事件流（SSE）：把任务执行过程边跑边推给界面。

设计取舍（为什么不做"整体流式重构"）：
- 现有的 `/api/chat/sessions/{id}/messages` 同步返回，路由逻辑（意图/历史/知识/计算）
  里积累了大量行为约定，改成生成器会把风险扩散到主流程。
- 因此这里**只新增一个只读的事件流端点**：它跟随当前任务的 trace.json 与进度文件，
  把新出现的事件实时推给前端。老接口一行不改，前端可渐进接入、随时回退轮询。

事件类型：
    event: progress   data: {stage, label, percent, task_id, done}
    event: trace      data: {event_type, stage, tool_name, message, status, duration, ts}
    event: done       data: {task_id, reason}
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..application.progress import is_finished
from ..config import AgentConfig
from ..runtime import narration as _narration

router = APIRouter()

# 轮询间隔。原先 0.4s —— 但一次任务的后半段（装载→求解→校验→导出）往往
# 只花 1~2 秒就写完全部事件，0.4s 一次会把二十多条一起读走，界面看起来
# 就是"啪一下全冒出来"。压到 0.08s，让事件尽可能贴着产生时刻出去。
# （读取的是本地几 KB 的 JSON，代价可忽略。）
_POLL_INTERVAL_S = 0.08
_MAX_STREAM_S = 1800.0  # 硬上限：半小时，防连接泄漏
_MAX_EVENTS_PER_TICK = 40
_MAX_WAIT_NEW_TASK_S = 90.0  # 等"本次任务真正开始"的上限


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_log(cfg: AgentConfig, msg: str) -> None:
    """实时流的关键节点留痕（连接 / 认任务 / 每批推送）。

    只记这几件事 —— 排查"界面上的过程为什么来得晚"时，需要能区分
    "后端没写事件"和"写了但没推出去"，光看客户端计时对不出结论。
    **任何写盘异常都不影响主流程**（整段包在 try 里）。
    """
    try:
        path = os.path.join(cfg.workspace_root, "_sse_debug.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


async def _event_source(task_id: str | None) -> AsyncIterator[str]:
    """跟随任务事件流。task_id 为空时先等它出现（用户刚点发送）。"""
    cfg = AgentConfig()
    _sse_log(cfg, "connect task_id=%s" % task_id)
    # 曾在这里挂过一个"心跳探针"（独立线程往 loop 投回调，用来量事件循环延迟）。
    # 它已经完成使命：定位到根因是**异步端点里做了同步 HTTP**（见下方 stall 处的说明）。
    # 现在不再需要 —— 每条连接都起一个线程的成本远大于它的价值。
    yield "retry: 3000\n\n"
    started = time.monotonic()
    emitted_events = 0
    last_progress_sig: tuple | None = None
    seen_task = task_id
    # 连接刚建立时读到的是哪个任务（用于识别"这是上一次的收尾态"）
    initial_task: str | None = None
    waiting_since: float | None = None
    last_seen_len: int | None = None
    last_tick: float | None = None

    while time.monotonic() - started < _MAX_STREAM_S:
        # 本循环本该每 0.08 秒转一圈。若某一圈隔了很久才回来，说明**事件循环被
        # 别的同步代码占住了** —— 那"过程来得晚"就不是推流的问题，而是 loop 被堵。
        # 这一条留作长期哨兵：它曾在 10 分钟内定位到真凶
        # （`/api/ready` 是 async 却直接做了同步 HTTP 探测知识库）。
        # 真有 stall 时顺手把所有线程的栈记下来 —— 根因通常在**别的线程**，
        # 光看这一侧永远只是在猜。
        _now = time.monotonic()
        if last_tick is not None and _now - last_tick > 1.0:
            _sse_log(cfg, "★ stall %.1fs (loop blocked) at %.1fs"
                     % (_now - last_tick, _now - started))
            try:
                import sys as _sys
                import traceback as _tb

                for _tid, _fr in _sys._current_frames().items():
                    _sse_log(cfg, "--- thread %s ---\n%s"
                             % (_tid, "".join(_tb.format_stack(_fr)[-5:])))
            except Exception:  # noqa: BLE001
                pass
        last_tick = _now
        # ⚠️ 这里**故意同步读**（几 KB 的文件，读一次不到 1ms）：
        # 原先用 `asyncio.to_thread` 走线程池，实测会整整卡住 4~8 秒不返回 ——
        # 线程池被别的同步调用占着时，这个"投递到线程"的动作本身就在排队。
        # 流式输出要的是"尽快把已有内容送出去"，为此把一次廉价读盘交给线程池
        # 并不划算；直接读反而最稳。
        _r0 = time.monotonic()
        progress = _read_json(os.path.join(cfg.workspace_root, "_current_progress.json"))
        _r1 = time.monotonic()
        if _r1 - _r0 > 0.5:
            _sse_log(cfg, "read_progress took %.1fs at %.1fs"
                     % (_r1 - _r0, _r1 - started))

        if not seen_task:
            # ⚠️ 点发送的瞬间，_current_progress.json 里**往往还是上一个任务**（已 done）——
            # 后端要几百毫秒后才开始写本次任务。若照它去读，会把上一轮的整份
            # trace 当成这次的推给界面，而且因为它的 done=true，这条流**立刻收尾**：
            # 本次任务的阶段一个都推不出去，界面只能等跑完从 /api/tasks/{id}/trace 回填。
            # 实测症状：任务全程 liveStages 恒为 0，最后一步才"跳"出全部阶段。
            # 所以：连接时那个任务的**完成态**不算数，要等真正属于本次的任务出现。
            cand = progress.get("task_id") if isinstance(progress, dict) else None
            # 除「已完成」外，「超过 STALE_AFTER_S 没更新」同样算结束 —— 那是上次
            # 进程被 Kill / 原生库段错误留下的脏状态：`finally` 都跑不到，
            # `done` 永远是 False，只认 `done` 会让它被**永久**当成当前任务推给
            # 界面。判据与轮询端点 /api/task-progress 共用同一份
            # （application/progress.py），避免两处漂移。
            # 2026-10-09 实测症状：发一句「你好」（闲聊、不产生任务），右侧执行
            # 过程面板显示的却是 10-04 卡在 validate_case 的旧任务。
            cand_finished = is_finished(progress)
            if initial_task is None and cand:
                initial_task = cand
            if cand and (not cand_finished or cand != initial_task):
                seen_task = cand
                waiting_since = None
                _sse_log(cfg, "track %s (initial=%s) after %.1fs"
                         % (cand, initial_task, time.monotonic() - started))
            else:
                if waiting_since is None:
                    waiting_since = time.monotonic()
                if time.monotonic() - waiting_since > _MAX_WAIT_NEW_TASK_S:
                    yield _sse("done", {"task_id": cand, "reason": "no_task_started"})
                    return
                await asyncio.sleep(_POLL_INTERVAL_S)
                continue
        if isinstance(progress, dict) and progress.get("task_id") == seen_task:
            sig = (
                progress.get("stage"),
                progress.get("label"),
                progress.get("percent"),
                progress.get("done"),
            )
            if sig != last_progress_sig:
                last_progress_sig = sig
                yield _sse("progress", {
                    "task_id": seen_task,
                    "stage": progress.get("stage"),
                    "label": progress.get("label"),
                    "percent": progress.get("percent"),
                    "done": bool(progress.get("done")),
                    "ts": time.time(),
                })

        # trace 增量推送（按已发条数推进，避免重复）。同样同步读，理由见上面。
        events = _read_json(os.path.join(cfg.workspace_root, seen_task, "trace.json"))
        # 读到的条数变化也留痕：-1 表示这一刻读不出来（文件正在被重写 /
        # 还不存在）。有了它才能分清"后端没写"与"写了但读不到"。
        _n = len(events) if isinstance(events, list) else -1
        if _n != last_seen_len:
            last_seen_len = _n
            _sse_log(cfg, "read trace len=%s at %.1fs" % (_n, time.monotonic() - started))
        if isinstance(events, list) and len(events) > emitted_events:
            fresh = events[emitted_events: emitted_events + _MAX_EVENTS_PER_TICK]
            emitted_events += len(fresh)
            _sse_log(cfg, "push %d trace events (total=%d, at %.1fs)"
                     % (len(fresh), emitted_events, time.monotonic() - started))
            for e in fresh:
                if not isinstance(e, dict):
                    continue
                yield _sse("trace", {
                    "event_type": e.get("event_type"),
                    "stage": e.get("stage"),
                    "tool_name": e.get("tool_name"),
                    "message": (e.get("message") or "")[:300],
                    "status": e.get("status"),
                    "duration": e.get("duration"),
                    "ts": e.get("timestamp"),
                    # 动作行 + 思考段。**由后端算好随事件下发**，与
                    # `/api/tasks/{id}/trace` 用的是同一个模板（runtime/narration.py）——
                    # 否则实时一份、回看一份，两套模板早晚漂移。
                    "action": _narration.action_for(e.get("stage") or ""),
                    "narration": _narration.narrate_from_event(e),
                })

        # 必须是**本任务**的完成态才算结束（progress 文件是全局复用的，
        # 不看 task_id 会把别的任务的收尾态当成本次的结束信号）
        done = bool(
            isinstance(progress, dict)
            and progress.get("task_id") == seen_task
            and progress.get("done")
        )
        if done:
            yield _sse("done", {"task_id": seen_task, "reason": "progress_done"})
            return
        if time.monotonic() - started > 10 and emitted_events == 0 and not progress:
            # 既不认识任务、也没有任何事件：多半是本条消息根本不是任务（闲聊/查询）
            yield _sse("done", {"task_id": seen_task, "reason": "no_task_events"})
            return
        await asyncio.sleep(_POLL_INTERVAL_S)


@router.get("/api/tasks/stream")
async def task_stream(request: Request, task_id: str | None = None) -> StreamingResponse:
    """SSE：实时推送当前（或指定）任务的进度与 trace 事件。

    前端用法：
        const es = new EventSource(base + '/api/tasks/stream');
        es.addEventListener('progress', e => ...);
        es.addEventListener('trace', e => ...);
        es.addEventListener('done', e => es.close());
    """
    return StreamingResponse(
        _event_source(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
