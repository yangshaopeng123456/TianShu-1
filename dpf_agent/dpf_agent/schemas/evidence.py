"""EvidenceItem（文档 11.6）：本次任务事实的索引，不是文献引用。

每个关键结论至少绑定 task_id、对象、字段、值、单位、版本、算例指纹与验证引用。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    evidence_id: str
    task_id: str
    object_ref: str           # 如 bus.675 / Line.l1 / system
    field: str                # 如 voltage_pu / total_loss_kw
    value: Any = None
    unit: str = ""
    status: str = "ok"        # ok | unavailable | not_assessed
    artifact_ref: str = ""    # 权威 Artifact 路径
    case_fingerprint: str = ""
    engine_version: str = ""
    adapter_version: str = ""
    verification_check_ref: str = ""
    claim: str = ""           # 该证据支撑的自然语言结论


class KnowledgeItem(BaseModel):
    """可追溯知识条目（文档 15.2）。仅辅助解释，不替代计算与验证。"""

    id: str
    title: str
    source_type: str          # opendss_doc | textbook | expert_rule | user_doc | system_case
    source_ref: str = ""
    applicable_condition: str = ""
    forbidden_condition: str = ""
    recommendation: str = ""
    risk_note: str = ""
    version: str = "1.0"
    updated_at: str = ""
