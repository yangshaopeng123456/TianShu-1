"""AgentState 与 TraceEvent（文档 11.5 / 9.2）。

AgentState 是当前任务唯一权威状态；TraceEvent 是执行审计链，
trace.json 是 CLI、Web 和报告复盘的统一来源。
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from .enums import ErrorCode, EventStatus, EventType, Phase, TaskStatus


class TraceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = Field(default_factory=time.time)
    stage: str = ""
    event_type: EventType
    tool_name: str | None = None
    sanitized_arguments: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    # 与 message 并存的**结构化数值**（message 是给人看的记录，这里是给模板取值的槽位）。
    # 用途：执行过程的「思考段」由 `runtime/narration.py` 的模板 + 这里的真实数值拼装 ——
    # 让文案是数据的函数，而不是把 message 当字符串反解（反解一改格式就静默失效）。
    detail: dict[str, Any] = Field(default_factory=dict)
    result_ref: str | None = None
    status: EventStatus = EventStatus.SUCCESS
    duration: float | None = None
    error_code: str | None = None


class AgentState(BaseModel):
    """任务唯一权威状态（文档 11.5）。"""

    task_id: str
    phase: Phase = Phase.INIT
    status: TaskStatus | None = None
    task_spec_ref: str = ""         # tasks/<id>/task_spec.json
    case_ref: str = ""              # CanonicalCase 引用
    solver_profile_ref: str = ""
    result_ref: str = ""
    verification_ref: str = ""
    error: dict[str, Any] | None = None  # {code, message, stage}
    version_set: dict[str, str] = Field(default_factory=dict)
    rejected_actions: list[dict[str, Any]] = Field(default_factory=list)

    def fail(self, code: str, message: str, stage: str, status: TaskStatus) -> None:
        self.phase = Phase.FAILED
        self.status = status
        self.error = {"code": code, "message": message, "stage": stage}

    def interrupt(self, stage: str, message: str) -> None:
        """标记任务被进程级中断（崩溃 / 被杀），未能形成业务结论。

        与 `fail()` 的区别是语义性的：`fail()` 表示"执行到了并判定为失败"，
        本方法表示"没来得及判定"。因此错误码固定为 `INTERRUPTED`，
        便于指标看板把基础设施问题与业务失败分开统计。
        """
        self.phase = Phase.FAILED
        self.status = TaskStatus.INTERRUPTED
        self.error = {
            "code": ErrorCode.INTERRUPTED.value,
            "message": message,
            "stage": stage,
        }
