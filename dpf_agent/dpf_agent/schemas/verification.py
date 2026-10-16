"""VerificationResult（文档 12.5 / 13.5）：独立校验结果。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    check_id: str
    domain: str          # input_identity | solver_state | completeness | numeric_validity
                         # | power_balance | reproducibility | evidence_completeness
    passed: bool
    detail: str = ""
    measured: float | None = None
    tolerance: float | None = None


class VerificationResult(BaseModel):
    task_id: str
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    power_balance_residual_pu: float | None = None
    power_balance_tolerance: float | None = None
    criteria_sources: dict[str, str] = Field(default_factory=dict)  # 判据来源与版本
    notes: list[str] = Field(default_factory=list)

    def failed_domains(self) -> list[str]:
        return [c.domain for c in self.checks if not c.passed]
