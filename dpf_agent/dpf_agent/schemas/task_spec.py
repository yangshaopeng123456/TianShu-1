"""UserRequest / PowerFlowTaskSpec（文档 9.1 / 11.1 / 8.7）。

初始 Prompt 约束分为硬约束（Rule Guard 强制执行）与软约束（解释与报告组织）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .enums import OutputFormat, ResultScope


class SolverConfig(BaseModel):
    """用户显式求解参数覆盖（硬约束）。"""

    max_iterations: int | None = Field(default=None, ge=1, le=10000)
    tolerance: float | None = Field(default=None, gt=0, le=1.0)
    solve_mode: Literal["snapshot"] = "snapshot"  # V1.3 仅稳态单场景


class UserConstraints(BaseModel):
    """初始 Prompt 解析出的约束（文档 8.7）。

    硬约束：result_scopes / solver_config 数值参数 / output_formats / 能力边界。
    软约束：focus_objects（解释重点）、report_preferences（报告表达偏好）。
    """

    hard: dict[str, Any] = Field(default_factory=dict)
    soft: dict[str, Any] = Field(default_factory=dict)


class UserRequest(BaseModel):
    """用户输入（文档 9.1）。"""

    message: str = ""
    case_ref: str | None = None  # Master.dss 路径或内置算例引用
    scenario_ref: str | None = None
    solver_config: SolverConfig | None = None
    result_scopes: list[ResultScope] = Field(default_factory=list)  # 空 → 由任务解析决定
    output_formats: list[OutputFormat] = Field(default_factory=list)  # 空 → 解析默认 json+md
    constraints: UserConstraints = Field(default_factory=UserConstraints)
    focus_objects: list[str] = Field(default_factory=list)  # 软约束：解释重点


class PowerFlowTaskSpec(BaseModel):
    """冻结的任务规格（文档 11.1）。冻结后不可再变，所有工具调用以其为准。"""

    task_id: str
    case_ref: str
    scenario_ref: str | None = None
    user_goal: str = ""
    solver_config: SolverConfig = Field(default_factory=SolverConfig)
    result_scopes: list[ResultScope]
    output_formats: list[OutputFormat]
    constraints: UserConstraints = Field(default_factory=UserConstraints)
    focus_objects: list[str] = Field(default_factory=list)
    capability_snapshot_ref: str = "v1.3"
    frozen: bool = False

    def freeze(self) -> "PowerFlowTaskSpec":
        self.frozen = True
        return self
