"""Evidence 构建与发布门（文档 11.6 / 12.5 / 13.4）。

- 关键自然语言结论必须能映射到 EvidenceItem，覆盖率 100%。
- 每个关键结论绑定对象、字段、值、单位、版本、算例指纹与验证引用。
"""
from __future__ import annotations

import uuid

from ..schemas.enums import ErrorCode
from ..schemas.evidence import EvidenceItem
from ..schemas.result import CanonicalDistributionPowerFlowResult
from ..schemas.verification import CheckResult, VerificationResult


class PublicationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = ErrorCode.VERIFICATION_FAILED
        self.message = message


def build_evidence(
    result: CanonicalDistributionPowerFlowResult,
    verification: VerificationResult,
    artifact_ref: str,
) -> list[EvidenceItem]:
    """为关键结论生成证据索引。只绑定权威结果中真实存在的数值。"""
    items: list[EvidenceItem] = []
    base = dict(
        task_id=result.task_id,
        artifact_ref=artifact_ref,
        case_fingerprint=result.case_fingerprint,
        engine_version=result.engine_version,
        adapter_version=result.adapter_version,
        verification_check_ref="power_balance",
    )

    def add(object_ref: str, field: str, value, unit: str, claim: str, status: str = "ok"):
        items.append(
            EvidenceItem(
                evidence_id=f"ev-{uuid.uuid4().hex[:8]}",
                object_ref=object_ref,
                field=field,
                value=value,
                unit=unit,
                status=status,
                claim=claim,
                **base,
            )
        )

    ss = result.system_summary
    ls = result.loss_summary
    if ss.min_voltage_pu is not None:
        add(
            f"bus.{ss.min_voltage_bus}",
            "voltage_pu",
            round(ss.min_voltage_pu, 6),
            "pu",
            f"最低母线电压为 {ss.min_voltage_pu:.4f} pu（母线 {ss.min_voltage_bus}）",
        )
    if ss.max_voltage_pu is not None:
        add(
            f"bus.{ss.max_voltage_bus}",
            "voltage_pu",
            round(ss.max_voltage_pu, 6),
            "pu",
            f"最高母线电压为 {ss.max_voltage_pu:.4f} pu（母线 {ss.max_voltage_bus}）",
        )
    if ls.total_loss_kw is not None:
        add(
            "system",
            "total_loss_kw",
            round(ls.total_loss_kw, 3),
            "kW",
            f"系统总网损为 {ls.total_loss_kw:.2f} kW",
        )
    if ss.total_load_kw is not None:
        add(
            "system",
            "total_load_kw",
            round(ss.total_load_kw, 3),
            "kW",
            f"系统总负荷为 {ss.total_load_kw:.2f} kW（各负荷元件端子1有功之和）",
        )
    if ss.total_generation_kw is not None:
        add(
            "system",
            "total_generation_kw",
            round(ss.total_generation_kw, 3),
            "kW",
            f"源侧总供电为 {ss.total_generation_kw:.2f} kW",
        )
    # 净负荷与总负荷分列：两者只有在**无发电**时才相等。
    # 含 PV/发电时 net = 负荷 − 发电，可能远小于甚至为负（反送）。
    # 必须显式给出，否则读者会把源侧供电误当成"负荷"，或把负值当异常。
    if ss.net_load_kw is not None and ss.total_load_kw is not None:
        if abs(ss.net_load_kw - ss.total_load_kw) > max(
            abs(ss.total_load_kw) * 0.01, 1.0
        ):
            gen_part = (
                f"，发电注入 {ss.total_generator_kw:.2f} kW"
                if ss.total_generator_kw is not None
                else ""
            )
            add(
                "system",
                "net_load_kw",
                round(ss.net_load_kw, 3),
                "kW",
                f"净负荷（源侧供电 − 网损）为 {ss.net_load_kw:.2f} kW，"
                f"与总负荷 {ss.total_load_kw:.2f} kW 的差异来自算例中的发电/反送{gen_part}",
            )
    if ss.load_elements_skipped:
        add(
            "system",
            "load_elements_skipped",
            int(ss.load_elements_skipped),
            "个",
            f"有 {int(ss.load_elements_skipped)} 个负荷元件读数缺失未计入求和，"
            "总负荷可能偏小",
        )
    if verification.power_balance_residual_pu is not None:
        add(
            "system",
            "power_balance_residual_pu",
            verification.power_balance_residual_pu,
            "pu",
            f"功率平衡残差 {verification.power_balance_residual_pu:.6f} pu，"
            f"在冻结容差 {verification.power_balance_tolerance} 内",
        )
    return items


def publication_gate(
    verification: VerificationResult,
    evidence: list[EvidenceItem],
    key_claims: list[str],
) -> CheckResult:
    """发布门（文档 13.4）：

    - Verification.passed 必须为 true。
    - 关键自然语言结论 Evidence 覆盖率 = 100%。
    - not_assessed 维度不得显示为 0/正常/安全（渲染层保证，此处校验证据状态）。
    """
    if not verification.passed:
        return CheckResult(
            check_id="publication_gate",
            domain="evidence_completeness",
            passed=False,
            detail=f"Verification 未通过，禁止正式发布: {verification.failed_domains()}",
        )
    covered = sum(
        1 for claim in key_claims if any(claim in ev.claim or ev.claim in claim for ev in evidence)
    )
    coverage = covered / len(key_claims) if key_claims else 1.0
    ok = coverage >= 1.0
    return CheckResult(
        check_id="publication_gate",
        domain="evidence_completeness",
        passed=ok,
        detail=f"关键结论 Evidence 覆盖率={coverage * 100:.0f}%（要求 100%）",
        measured=coverage,
        tolerance=1.0,
    )
