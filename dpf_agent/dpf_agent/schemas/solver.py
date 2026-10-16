"""OpenDSSSolverProfile（文档 11.3）：本任务冻结的求解策略、参数、结果域与版本集合。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .enums import ResultScope


class IterationConfig(BaseModel):
    max_iterations: int = 15
    max_control_iterations: int = 10  # 记录值；实际控制由 controlmode=off 冻结


class ToleranceConfig(BaseModel):
    tolerance: float = 1e-4


class SelectionEvidence(BaseModel):
    """SolverProfile 选择依据（文档 13.6：不得只记录零散参数）。"""

    source: str  # user_constraint | case_field | sim_default | network_profile
    detail: str


class OpenDSSSolverProfile(BaseModel):
    profile_id: str
    engine: str = "OpenDSS"
    solve_mode: str = "snapshot"  # V1.3 稳态确定性单场景
    iteration_config: IterationConfig = Field(default_factory=IterationConfig)
    tolerance_config: ToleranceConfig = Field(default_factory=ToleranceConfig)
    control_behavior: str = "frozen_off"  # V1.3：不执行任何自动控制动作
    result_scopes: list[ResultScope] = Field(default_factory=list)
    version_set: dict[str, str] = Field(default_factory=dict)  # engine/adapter/defaults 版本
    selection_evidence: list[SelectionEvidence] = Field(default_factory=list)
    frozen: bool = False

    def freeze(self) -> "OpenDSSSolverProfile":
        self.frozen = True
        return self

    def param_dict(self) -> dict[str, Any]:
        return {
            "max_iterations": self.iteration_config.max_iterations,
            "tolerance": self.tolerance_config.tolerance,
            "solve_mode": self.solve_mode,
            "control_behavior": self.control_behavior,
        }
