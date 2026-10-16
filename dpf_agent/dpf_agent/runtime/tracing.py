"""Trace 记录与 CLI 实时事件流（文档 9.2 / 14.2）。

trace.json 是 CLI、Web 和报告复盘的统一来源。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from ..schemas.enums import EventStatus, EventType
from ..schemas.state import TraceEvent

_CLI_TAG = {
    EventType.USER: "user",
    EventType.UNDERSTAND: "understand",
    EventType.CONSTRAINTS: "constraints",
    EventType.PLAN: "plan",
    EventType.TOOL_CALL: "tool-call",
    EventType.TOOL_RESULT: "tool-result",
    EventType.GUARD: "guard",
    EventType.VERIFY: "verify",
    EventType.EVIDENCE: "evidence",
    EventType.RESULT: "result",
}


class Tracer:
    def __init__(
        self,
        trace_path: str | None = None,
        cli_callback: Callable[[str], None] | None = print,
    ):
        self.events: list[TraceEvent] = []
        self.trace_path = trace_path
        self.cli_callback = cli_callback

    def emit(
        self,
        event_type: EventType,
        stage: str = "",
        message: str = "",
        tool_name: str | None = None,
        sanitized_arguments: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        result_ref: str | None = None,
        status: EventStatus = EventStatus.SUCCESS,
        duration: float | None = None,
        error_code: str | None = None,
    ) -> TraceEvent:
        ev = TraceEvent(
            stage=stage,
            event_type=event_type,
            tool_name=tool_name,
            sanitized_arguments=sanitized_arguments or {},
            message=message,
            detail=detail or {},
            result_ref=result_ref,
            status=status,
            duration=duration,
            error_code=error_code,
        )
        self.events.append(ev)
        if self.cli_callback:
            tag = _CLI_TAG[event_type]
            suffix = ""
            if duration is not None:
                suffix += f" ({duration:.2f}s)"
            if status == EventStatus.FAILED:
                suffix += f" [FAILED {error_code or ''}]"
            self.cli_callback(f"[{tag}] {message}{suffix}")
        self.flush()
        return ev

    def flush(self) -> None:
        if self.trace_path:
            os.makedirs(os.path.dirname(self.trace_path), exist_ok=True)
            with open(self.trace_path, "w", encoding="utf-8") as f:
                json.dump(
                    [e.model_dump(mode="json") for e in self.events],
                    f,
                    ensure_ascii=False,
                    indent=2,
                )


class Timer:
    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, *exc):
        self.duration = time.time() - self.t0
