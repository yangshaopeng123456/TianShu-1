"""Explicit project criteria for engineering checks. Copyright yangshaopeng.

Schema 2.0 is separate from the historical EngineeringCriteria contract.
Confirmation metadata records the caller's declaration; it does not verify
identity, authority, the source document, or engineering acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any

from .contracts import (
    ContractError, _Serializable, _choice, _identifier, _integer, _object, _text,
)


PROJECT_CRITERIA_SCHEMA_VERSION = "2.0"
_KINDS = frozenset({
    "voltage", "line_current", "transformer_current", "transformer_kva", "active_loss",
})
_NUMERIC_FIELDS = frozenset({
    "lower", "upper", "rated_value", "upper_loading_percent", "base_voltage_v",
})
_SELECTORS = frozenset({"node", "reference_node", "terminal", "conductor"})
_RULE_REQUIRED = frozenset({"rule_id", "kind", "object_id", "unit", "measurement_basis", "source"})
_RULE_FIELDS = _RULE_REQUIRED | _NUMERIC_FIELDS | _SELECTORS | {
    "applicability", "exclusion_reason",
}


def _finite(value: Any, path: str, *, nonnegative: bool = False,
            positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(path, "must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ContractError(path, "must be a finite number") from None
    if not math.isfinite(number):
        raise ContractError(path, "must be a finite number")
    if (positive and number <= 0) or (nonnegative and number < 0):
        raise ContractError(path, "must be positive" if positive else "must be nonnegative")
    return 0.0 if number == 0 else number


@dataclass(frozen=True)
class ProjectRule(_Serializable):
    """One exact native-node, terminal/conductor, or network check.

    No wildcard, phase-label inference, equipment default rating, automatic
    voltage base, or implicit engineering limit is part of this contract.
    """

    rule_id: str
    kind: str
    object_id: str
    unit: str
    measurement_basis: str
    source: str
    node: int | None = None
    reference_node: int | None = None
    terminal: int | None = None
    conductor: int | None = None
    lower: float | None = None
    upper: float | None = None
    rated_value: float | None = None
    upper_loading_percent: float | None = None
    base_voltage_v: float | None = None
    applicability: str = "evaluate"
    exclusion_reason: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.rule_id, "project_rule.rule_id")
        _choice(self.kind, _KINDS, "project_rule.kind")
        _text(self.object_id, "project_rule.object_id", 256)
        if any(char in self.object_id for char in "*?[]"):
            raise ContractError("project_rule.object_id", "must identify one exact object without wildcard syntax")
        _text(self.source, "project_rule.source", 4096)
        _choice(self.applicability, {"evaluate", "not_applicable"}, "project_rule.applicability")

        if self.kind == "voltage":
            _choice(self.unit, {"V", "kV", "pu"}, "project_rule.unit")
            _integer(self.node, "project_rule.node", 1, 2147483647)
            _integer(self.reference_node, "project_rule.reference_node", 0, 2147483647)
            if self.node == self.reference_node:
                raise ContractError("project_rule.reference_node", "must differ from node")
            expected_basis = "node_to_ground" if self.reference_node == 0 else "node_to_node"
            _choice(self.measurement_basis, {expected_basis}, "project_rule.measurement_basis")
            self._forbid({"terminal", "conductor", "rated_value", "upper_loading_percent"})
            if self.unit != "pu":
                self._forbid({"base_voltage_v"})
        elif self.kind in {"line_current", "transformer_current"}:
            _choice(self.unit, {"A"}, "project_rule.unit")
            _choice(self.measurement_basis, {"terminal_conductor_rms"}, "project_rule.measurement_basis")
            _integer(self.terminal, "project_rule.terminal", 1, 32)
            _integer(self.conductor, "project_rule.conductor", 1, 64)
            self._forbid({"node", "reference_node", "lower", "upper", "base_voltage_v"})
        elif self.kind == "transformer_kva":
            _choice(self.unit, {"kVA"}, "project_rule.unit")
            _choice(self.measurement_basis, {"terminal_complex_power_magnitude"}, "project_rule.measurement_basis")
            _integer(self.terminal, "project_rule.terminal", 1, 32)
            self._forbid({"node", "reference_node", "conductor", "lower", "upper", "base_voltage_v"})
        else:
            _choice(self.object_id, {"network"}, "project_rule.object_id")
            _choice(self.unit, {"kW"}, "project_rule.unit")
            _choice(self.measurement_basis, {"passive_network_active_loss"}, "project_rule.measurement_basis")
            self._forbid(_SELECTORS | {"rated_value", "upper_loading_percent", "base_voltage_v"})

        if self.applicability == "not_applicable":
            _text(self.exclusion_reason, "project_rule.exclusion_reason", 4096)
            self._forbid(_NUMERIC_FIELDS)
        else:
            self._forbid({"exclusion_reason"})
            if self.kind in {"voltage", "active_loss"}:
                if self.lower is None and self.upper is None:
                    raise ContractError("project_rule", "at least one explicit lower or upper limit is required")
                for name in ("lower", "upper"):
                    value = getattr(self, name)
                    if value is not None:
                        object.__setattr__(self, name, _finite(
                            value, "project_rule." + name, nonnegative=self.kind == "voltage"))
                if self.lower is not None and self.upper is not None and self.lower > self.upper:
                    raise ContractError("project_rule", "lower must not exceed upper")
            if self.kind in {"line_current", "transformer_current", "transformer_kva"}:
                object.__setattr__(self, "upper_loading_percent", _finite(
                    self.upper_loading_percent, "project_rule.upper_loading_percent", positive=True))
                if self.rated_value is not None:
                    object.__setattr__(self, "rated_value", _finite(
                        self.rated_value, "project_rule.rated_value", positive=True))
            if self.base_voltage_v is not None:
                object.__setattr__(self, "base_voltage_v", _finite(
                    self.base_voltage_v, "project_rule.base_voltage_v", positive=True))

    def _forbid(self, names: set[str] | frozenset[str]) -> None:
        for name in sorted(names):
            if getattr(self, name) is not None:
                raise ContractError("project_rule." + name, "is not applicable to this rule and must be null or omitted")

    def selector_identity(self) -> tuple[Any, ...]:
        """Unit-independent identity prevents duplicate checks or exclusions."""
        return (self.kind, self.object_id.casefold(), self.node, self.reference_node,
                self.terminal, self.conductor, self.measurement_basis)

    @classmethod
    def from_dict(cls, value: Any) -> "ProjectRule":
        return cls(**_object(value, set(_RULE_FIELDS), set(_RULE_REQUIRED), "project_rule"))


@dataclass(frozen=True)
class ProjectCriteria(_Serializable):
    """Caller-confirmed criteria declaration, not an authenticated approval."""

    criteria_id: str
    version: str
    source: str
    confirmed_by: str
    confirmed_at: str
    rules: tuple[ProjectRule, ...] = ()
    schema_version: str = PROJECT_CRITERIA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _choice(self.schema_version, {PROJECT_CRITERIA_SCHEMA_VERSION}, "project_criteria.schema_version")
        _identifier(self.criteria_id, "project_criteria.criteria_id")
        for name in ("version", "source", "confirmed_by", "confirmed_at"):
            _text(getattr(self, name), "project_criteria." + name, 4096 if name == "source" else 1024)
        try:
            datetime.fromisoformat(self.confirmed_at.replace("Z", "+00:00"))
        except ValueError:
            raise ContractError("project_criteria.confirmed_at", "must be an ISO date or datetime") from None
        if (not isinstance(self.rules, (list, tuple)) or len(self.rules) > 10000
                or not all(isinstance(rule, ProjectRule) for rule in self.rules)):
            raise ContractError("project_criteria.rules", "must contain at most 10000 ProjectRule objects")
        object.__setattr__(self, "rules", tuple(self.rules))
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ContractError("project_criteria.rules", "rule_id must be unique")
        if len({rule.selector_identity() for rule in self.rules}) != len(self.rules):
            raise ContractError("project_criteria.rules", "exact target selectors must be unique irrespective of units or object-name case")

    @classmethod
    def from_dict(cls, value: Any) -> "ProjectCriteria":
        fields = {"schema_version", "criteria_id", "version", "source", "confirmed_by", "confirmed_at", "rules"}
        data = _object(value, fields, fields, "project_criteria")
        _choice(data["schema_version"], {PROJECT_CRITERIA_SCHEMA_VERSION}, "project_criteria.schema_version")
        if not isinstance(data["rules"], (list, tuple)) or len(data["rules"]) > 10000:
            raise ContractError("project_criteria.rules", "must be an array of at most 10000 rules")
        data["rules"] = tuple(ProjectRule.from_dict(rule) for rule in data["rules"])
        return cls(**data)
