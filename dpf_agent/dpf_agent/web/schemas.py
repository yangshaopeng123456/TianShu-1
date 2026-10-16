"""Web API 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..schemas.enums import OutputFormat, ResultScope, TaskStatus


class RunRequest(BaseModel):
    """提交潮流计算任务。"""

    message: str = Field(default="", description="自然语言任务描述")
    case_ref: str = Field(..., description="算例引用，如 ieee33 / case4 / case_tx 等")
    result_scopes: list[str] = Field(default=[], description="结果范围：bus,branch,transformer,system")
    output_formats: list[str] = Field(default=["md", "html"], description="输出格式：json,md,html,csv")
    max_iterations: int | None = Field(default=None, ge=1, le=10000)
    tolerance: float | None = Field(default=None, gt=0, le=1.0)
    llm_enabled: bool | None = Field(default=None, description="None 表示跟随 CLI/环境默认")


class RunResponse(BaseModel):
    """任务提交响应。"""

    task_id: str
    status: str
    output_dir: str | None = None
    report_url: str | None = None
    error: dict | None = None


class TaskInfo(BaseModel):
    """任务状态查询。"""

    task_id: str
    status: str
    output_dir: str | None = None
    report_url: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    error: dict | None = None
