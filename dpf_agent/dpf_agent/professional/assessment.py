"""Pure engineering assessment; never substitutes for numerical verification.

Copyright (c) yangshaopeng. Missing criteria or observations are not passes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .contracts import (
    ContractError, EngineeringCriteria, LimitRule, Measurement,
    NUMERICAL_VALIDITIES, _Serializable, _choice,
)


@dataclass(frozen=True)
class AssessmentItem(_Serializable):
    check_id: str
    metric: str
    status: str
    reason: str
    object_id: str | None = None
    phase: str | None = None
    terminal: int | None = None
    unit: str | None = None
    measurement_basis: str | None = None
    measured: float | None = None
    lower: float | None = None
    upper: float | None = None
    criteria_source: str | None = None
    evidence_ref: str | None = None


@dataclass(frozen=True)
class AssessmentReport(_Serializable):
    numerical_validity: str
    status: str
    criteria_id: str | None
    criteria_version: str | None
    criteria_digest: str | None
    items: tuple[AssessmentItem, ...]
    scope: str = "explicit_confirmed_rules_only"
    overall_compliance: str = "not_assessed"

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result["counts"] = {
            status: sum(item.status == status for item in self.items)
            for status in ("pass", "violation", "not_assessed", "not_applicable")
        }
        return result


def _identity(value: Measurement | LimitRule) -> tuple[Any, ...]:
    return (value.object_id, value.metric, value.unit, value.measurement_basis,
            value.phase, value.terminal)


def evaluate_assessment(
    observations: Iterable[Measurement],
    criteria: EngineeringCriteria | None,
    numerical_validity: str,
) -> AssessmentReport:
    """Evaluate only exact, unambiguous measurement identities.

    Rules include explicit limits and confirmation provenance. Bounds are
    inclusive; no implicit standards, unit conversion, phase inference, default
    equipment rating, or scope-wide all-clear conclusion is introduced.
    """
    _choice(numerical_validity, NUMERICAL_VALIDITIES, "numerical_validity")
    if criteria is not None and not isinstance(criteria, EngineeringCriteria):
        raise ContractError("criteria", "must be EngineeringCriteria or null")
    try:
        values = tuple(observations)
    except TypeError:
        raise ContractError("observations", "must be an iterable of Measurement") from None
    if not all(isinstance(value, Measurement) for value in values):
        raise ContractError("observations", "must contain only Measurement objects")
    indexed: dict[tuple[Any, ...], list[Measurement]] = {}
    for value in values:
        indexed.setdefault(_identity(value), []).append(value)

    items: list[AssessmentItem] = []
    rules = criteria.rules if criteria is not None else ()
    for rule in rules:
        matches = indexed.get(_identity(rule), [])
        measured = None
        evidence_ref = None
        if numerical_validity != "converged_valid":
            status, reason = "not_assessed", "numerical_result_not_validated"
        elif not matches:
            status, reason = "not_assessed", "matching_measurement_missing"
        elif len(matches) != 1:
            status, reason = "not_assessed", "matching_measurement_ambiguous"
        else:
            observation = matches[0]
            measured, evidence_ref = observation.value, observation.evidence_ref
            violation = ((rule.lower is not None and measured < rule.lower)
                         or (rule.upper is not None and measured > rule.upper))
            status = "violation" if violation else "pass"
            reason = "outside_confirmed_limits" if violation else "within_confirmed_limits"
        items.append(AssessmentItem(
            check_id=rule.rule_id, metric=rule.metric, status=status, reason=reason,
            object_id=rule.object_id, phase=rule.phase, terminal=rule.terminal,
            unit=rule.unit, measurement_basis=rule.measurement_basis,
            measured=measured, lower=rule.lower, upper=rule.upper,
            criteria_source=criteria.source, evidence_ref=evidence_ref,
        ))

    # Visible minimum coverage: an empty rule list must never look like a pass.
    for metric in ("voltage", "current", "apparent_power"):
        if not any(rule.metric == metric for rule in rules):
            items.append(AssessmentItem(
                check_id=f"coverage:{metric}", metric=metric, status="not_assessed",
                reason="confirmed_criteria_missing", criteria_source=criteria.source if criteria else None,
            ))

    if any(item.status == "violation" for item in items):
        status = "violations_found"
    elif any(item.status == "not_assessed" for item in items):
        status = "not_assessed" if all(item.status == "not_assessed" for item in items) else "partially_assessed"
    else:
        status = "evaluated_rules_passed"
    return AssessmentReport(
        numerical_validity=numerical_validity, status=status,
        criteria_id=criteria.criteria_id if criteria else None,
        criteria_version=criteria.version if criteria else None,
        criteria_digest=criteria.digest() if criteria else None,
        items=tuple(items),
    )
