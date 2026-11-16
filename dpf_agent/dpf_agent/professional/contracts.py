"""Versioned, dependency-free contracts for the professional power-flow service.

Copyright (c) yangshaopeng. Contract support does not imply runtime capability.
No legacy model, chat service, solver, or filesystem is imported here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .project_criteria import ProjectCriteria


SCHEMA_VERSION = "1.0"
LATEST_SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, LATEST_SCHEMA_VERSION})
NUMERICAL_VALIDITIES = frozenset({
    "not_run", "converged_valid", "non_converged", "validation_failed",
})
EXECUTION_STATES = frozenset({
    "created", "queued", "running", "cancelling", "finished", "failed",
    "cancelled", "interrupted",
})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SCOPES = frozenset({"bus", "branch", "transformer", "system"})
_FORMATS = frozenset({"json", "html", "csv"})
_METRIC_UNITS = {
    "voltage": frozenset({"pu", "V", "kV"}),
    "current": frozenset({"A"}),
    "apparent_power": frozenset({"kVA"}),
}
_METRIC_BASES = {
    "voltage": frozenset({"phase_to_ground", "phase_to_neutral", "line_to_line"}),
    "current": frozenset({"terminal_conductor_rms"}),
    "apparent_power": frozenset({"terminal_phase_apparent"}),
}


class ContractError(ValueError):
    """Invalid input, with a stable code and field path for API adapters."""

    def __init__(self, field: str, message: str, code: str = "invalid_contract"):
        self.field = field
        self.code = code
        super().__init__(f"{field}: {message}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": str(self)}


def _object(value: Any, allowed: set[str], required: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(path, "must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ContractError(path, "object keys must be strings")
    # JSON permits escaped lone surrogates. Reject them before including any
    # supplied key in an error; otherwise the API error itself cannot be UTF-8
    # encoded and turns a client contract error into a server error.
    try:
        for key in value:
            key.encode("utf-8")
    except UnicodeError:
        raise ContractError(path, "object keys must be valid Unicode encodable as UTF-8") from None
    unknown = set(value) - allowed
    if unknown:
        raise ContractError(path, f"unknown fields: {', '.join(sorted(unknown))}")
    missing = required - set(value)
    if missing:
        raise ContractError(path, f"missing fields: {', '.join(sorted(missing))}")
    return dict(value)


def _text(value: Any, path: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractError(path, "must be a nonempty string without outer whitespace")
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ContractError(path, "too long or contains control characters")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ContractError(path, "must be valid Unicode encodable as UTF-8") from None
    return value


def _identifier(value: Any, path: str) -> str:
    value = _text(value, path, 128)
    if not _ID.fullmatch(value):
        raise ContractError(path, "must be a bounded identifier, not a file path")
    return value


def _integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(path, f"must be an integer from {minimum} to {maximum}")
    return value


def _number(value: Any, path: str, *, minimum: float = 0.0,
            exclusive_minimum: bool = False, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(path, "must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ContractError(path, "must be a finite number") from None
    if not math.isfinite(number):
        raise ContractError(path, "must be a finite number")
    if number < minimum or (exclusive_minimum and number == minimum):
        raise ContractError(path, f"must be {'greater than' if exclusive_minimum else 'at least'} {minimum}")
    if maximum is not None and number > maximum:
        raise ContractError(path, f"must not exceed {maximum}")
    return 0.0 if number == 0 else number


def _choice(value: Any, choices: frozenset[str] | set[str], path: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ContractError(path, f"must be one of {', '.join(sorted(choices))}")
    return value


def _string_tuple(value: Any, allowed: frozenset[str], path: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ContractError(path, "must be a nonempty array")
    result = tuple(_choice(item, allowed, path) for item in value)
    if len(set(result)) != len(result):
        raise ContractError(path, "duplicates are not allowed")
    return result


class _Serializable:
    def to_dict(self) -> dict[str, Any]:
        # JSON round-trip produces independent plain objects and arrays.
        return json.loads(self.canonical_json())

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelReference(_Serializable):
    case_id: str | None = None
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if (self.case_id is None) == (self.snapshot_id is None):
            raise ContractError("model", "provide exactly one of case_id or snapshot_id")
        if self.case_id is not None:
            _identifier(self.case_id, "model.case_id")
            if ":" in self.case_id:
                raise ContractError("model.case_id", "must not contain a drive or path prefix")
        if self.snapshot_id is not None:
            _identifier(self.snapshot_id, "model.snapshot_id")
            if ":" in self.snapshot_id:
                raise ContractError("model.snapshot_id", "must not contain a drive or path prefix")

    @classmethod
    def from_dict(cls, value: Any) -> "ModelReference":
        return cls(**_object(value, {"case_id", "snapshot_id"}, set(), "model"))


@dataclass(frozen=True)
class ScenarioReference(_Serializable):
    scenario_id: str
    mode: str = "existing_snapshot"

    def __post_init__(self) -> None:
        _identifier(self.scenario_id, "scenario.scenario_id")
        _choice(self.mode, {"existing_snapshot"}, "scenario.mode")

    @classmethod
    def from_dict(cls, value: Any) -> "ScenarioReference":
        return cls(**_object(value, {"scenario_id", "mode"}, {"scenario_id", "mode"}, "scenario"))


@dataclass(frozen=True)
class SolverParameters(_Serializable):
    max_iterations: int
    tolerance: float
    engine: str = "OpenDSS"
    transport: str = "com"
    solve_mode: str = "snapshot"

    def __post_init__(self) -> None:
        _choice(self.engine, {"OpenDSS"}, "solver.engine")
        _choice(self.transport, {"com"}, "solver.transport")
        _choice(self.solve_mode, {"snapshot"}, "solver.solve_mode")
        _integer(self.max_iterations, "solver.max_iterations", 1, 10000)
        object.__setattr__(self, "tolerance", _number(
            self.tolerance, "solver.tolerance", exclusive_minimum=True, maximum=1.0))

    @classmethod
    def from_dict(cls, value: Any) -> "SolverParameters":
        fields = {"max_iterations", "tolerance", "engine", "transport", "solve_mode"}
        return cls(**_object(value, fields, fields, "solver"))


@dataclass(frozen=True)
class RecoveryBudget(_Serializable):
    policy: str = "none"
    max_retries: int = 0
    retry_solver: SolverParameters | None = None

    def __post_init__(self) -> None:
        _choice(self.policy, {"none", "numeric_only"}, "recovery.policy")
        _integer(self.max_retries, "recovery.max_retries", 0, 1)
        if self.policy == "none":
            if self.max_retries != 0 or self.retry_solver is not None:
                raise ContractError("recovery", "none policy requires zero retries and no retry_solver")
        elif self.max_retries != 1 or not isinstance(self.retry_solver, SolverParameters):
            raise ContractError("recovery", "numeric_only requires one retry and an explicit retry_solver")

    @classmethod
    def from_dict(cls, value: Any) -> "RecoveryBudget":
        data = _object(value, {"policy", "max_retries", "retry_solver"},
                       {"policy", "max_retries"}, "recovery")
        if data.get("retry_solver") is not None:
            data["retry_solver"] = SolverParameters.from_dict(data["retry_solver"])
        return cls(**data)


def _measurement_identity(metric: Any, unit: Any, basis: Any, phase: Any,
                          terminal: Any, path: str) -> None:
    _choice(metric, frozenset(_METRIC_UNITS), f"{path}.metric")
    _choice(unit, _METRIC_UNITS[metric], f"{path}.unit")
    _choice(basis, _METRIC_BASES[metric], f"{path}.measurement_basis")
    phases = {"AB", "BC", "CA"} if basis == "line_to_line" else {"A", "B", "C", "1", "2", "3"}
    _choice(phase, phases, f"{path}.phase")
    if terminal is not None:
        _integer(terminal, f"{path}.terminal", 1, 32)
    elif metric != "voltage":
        raise ContractError(f"{path}.terminal", "is required for a device current or power measurement")


@dataclass(frozen=True)
class LimitRule(_Serializable):
    rule_id: str
    object_id: str
    metric: str
    unit: str
    measurement_basis: str
    phase: str
    terminal: int | None = None
    lower: float | None = None
    upper: float | None = None

    def __post_init__(self) -> None:
        _identifier(self.rule_id, "rule.rule_id")
        _text(self.object_id, "rule.object_id", 256)
        _measurement_identity(self.metric, self.unit, self.measurement_basis,
                              self.phase, self.terminal, "rule")
        if self.lower is None and self.upper is None:
            raise ContractError("rule", "at least one explicit limit is required")
        for key in ("lower", "upper"):
            if getattr(self, key) is not None:
                object.__setattr__(self, key, _number(getattr(self, key), f"rule.{key}"))
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ContractError("rule", "lower must not exceed upper")

    @classmethod
    def from_dict(cls, value: Any) -> "LimitRule":
        required = {"rule_id", "object_id", "metric", "unit", "measurement_basis", "phase"}
        return cls(**_object(value, required | {"terminal", "lower", "upper"}, required, "rule"))


@dataclass(frozen=True)
class EngineeringCriteria(_Serializable):
    criteria_id: str
    version: str
    source: str
    confirmed_by: str
    confirmed_at: str
    rules: tuple[LimitRule, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.criteria_id, "criteria.criteria_id")
        for key in ("version", "source", "confirmed_by", "confirmed_at"):
            _text(getattr(self, key), f"criteria.{key}")
        try:
            datetime.fromisoformat(self.confirmed_at.replace("Z", "+00:00"))
        except ValueError:
            raise ContractError("criteria.confirmed_at", "must be an ISO date or datetime") from None
        if not isinstance(self.rules, (list, tuple)) or not all(isinstance(r, LimitRule) for r in self.rules):
            raise ContractError("criteria.rules", "must be an array of LimitRule objects")
        object.__setattr__(self, "rules", tuple(self.rules))
        if len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ContractError("criteria.rules", "rule_id must be unique")

    @classmethod
    def from_dict(cls, value: Any) -> "EngineeringCriteria":
        fields = {"criteria_id", "version", "source", "confirmed_by", "confirmed_at", "rules"}
        data = _object(value, fields, fields, "criteria")
        if not isinstance(data["rules"], (list, tuple)):
            raise ContractError("criteria.rules", "must be an array")
        data["rules"] = tuple(LimitRule.from_dict(rule) for rule in data["rules"])
        return cls(**data)


@dataclass(frozen=True)
class Measurement(_Serializable):
    """A provenance-backed value; creation is the trusted extractor's duty."""

    object_id: str
    metric: str
    unit: str
    measurement_basis: str
    phase: str
    value: float
    evidence_ref: str
    terminal: int | None = None

    def __post_init__(self) -> None:
        _text(self.object_id, "measurement.object_id", 256)
        _measurement_identity(self.metric, self.unit, self.measurement_basis,
                              self.phase, self.terminal, "measurement")
        object.__setattr__(self, "value", _number(self.value, "measurement.value"))
        _text(self.evidence_ref, "measurement.evidence_ref", 2048)

    @classmethod
    def from_dict(cls, value: Any) -> "Measurement":
        fields = {"object_id", "metric", "unit", "measurement_basis", "phase", "value", "evidence_ref"}
        return cls(**_object(value, fields | {"terminal"}, fields, "measurement"))


@dataclass(frozen=True)
class OperatingConditions(_Serializable):
    """Declared operating-point check; never authorizes model modification.

    Confirmation metadata records the caller's declaration. Valid structure
    does not verify a confirmatory person's identity or access permissions.
    """

    frequency_hz: float
    source: str
    confirmed_by: str
    confirmed_at: str
    mode: str = "validate_model_operating_point"

    def __post_init__(self) -> None:
        object.__setattr__(self, "frequency_hz", _number(
            self.frequency_hz, "operating_conditions.frequency_hz", exclusive_minimum=True, maximum=1000.0))
        _choice(self.mode, {"validate_model_operating_point"}, "operating_conditions.mode")
        for key in ("source", "confirmed_by", "confirmed_at"):
            _text(getattr(self, key), f"operating_conditions.{key}")
        try:
            datetime.fromisoformat(self.confirmed_at.replace("Z", "+00:00"))
        except ValueError:
            raise ContractError("operating_conditions.confirmed_at", "must be an ISO date or datetime") from None

    @classmethod
    def from_dict(cls, value: Any) -> "OperatingConditions":
        fields = {"frequency_hz", "source", "confirmed_by", "confirmed_at", "mode"}
        return cls(**_object(value, fields, fields, "operating_conditions"))


@dataclass(frozen=True)
class RunRequest(_Serializable):
    request_id: str
    model: ModelReference
    scenario: ScenarioReference
    solver: SolverParameters
    schema_version: str = SCHEMA_VERSION
    recovery: RecoveryBudget = field(default_factory=RecoveryBudget)
    criteria: EngineeringCriteria | ProjectCriteria | None = None
    result_scopes: tuple[str, ...] = ("bus", "branch", "transformer", "system")
    output_formats: tuple[str, ...] = ("json", "html", "csv")
    operating_conditions: OperatingConditions | None = None

    def __post_init__(self) -> None:
        _identifier(self.request_id, "request_id")
        _choice(self.schema_version, SUPPORTED_SCHEMA_VERSIONS, "schema_version")
        for key, kind in (("model", ModelReference), ("scenario", ScenarioReference),
                          ("solver", SolverParameters), ("recovery", RecoveryBudget)):
            if not isinstance(getattr(self, key), kind):
                raise ContractError(key, f"must be a {kind.__name__}")
        from .project_criteria import ProjectCriteria
        if self.criteria is not None and not isinstance(self.criteria, (EngineeringCriteria, ProjectCriteria)):
            raise ContractError("criteria", "must be EngineeringCriteria, ProjectCriteria or null")
        if isinstance(self.criteria, ProjectCriteria) and self.schema_version != "1.1":
            raise ContractError("criteria", "project criteria 2.0 requires request 1.1 with explicit operating conditions")
        if self.schema_version == "1.0":
            if self.operating_conditions is not None:
                raise ContractError("operating_conditions", "non-null conditions require request schema 1.1")
        elif not isinstance(self.operating_conditions, OperatingConditions):
            raise ContractError("operating_conditions", "request schema 1.1 requires explicit OperatingConditions")
        object.__setattr__(self, "result_scopes", _string_tuple(self.result_scopes, _SCOPES, "result_scopes"))
        object.__setattr__(self, "output_formats", _string_tuple(self.output_formats, _FORMATS, "output_formats"))

    def canonical_json(self) -> str:
        data = asdict(self)
        if self.schema_version == "1.0":
            # A null new field must not alter persisted 1.0 requests, stable
            # request digests or idempotency after an upgrade.
            data.pop("operating_conditions")
        return json.dumps(data, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Any) -> "RunRequest":
        required = {"schema_version", "request_id", "model", "scenario", "solver"}
        data = _object(value, required | {"recovery", "criteria", "result_scopes", "output_formats", "operating_conditions"},
                       required, "request")
        _choice(data["schema_version"], SUPPORTED_SCHEMA_VERSIONS, "schema_version")
        if data["schema_version"] == "1.0":
            if data.get("operating_conditions") is not None:
                raise ContractError("operating_conditions", "non-null conditions require request schema 1.1")
        else:
            if data.get("operating_conditions") is None:
                raise ContractError("operating_conditions", "request schema 1.1 requires explicit OperatingConditions")
            data["operating_conditions"] = OperatingConditions.from_dict(data["operating_conditions"])
        data["model"] = ModelReference.from_dict(data["model"])
        data["scenario"] = ScenarioReference.from_dict(data["scenario"])
        data["solver"] = SolverParameters.from_dict(data["solver"])
        if "recovery" in data:
            data["recovery"] = RecoveryBudget.from_dict(data["recovery"])
        if data.get("criteria") is not None:
            from .project_criteria import ProjectCriteria
            if isinstance(data["criteria"], Mapping) and "schema_version" in data["criteria"]:
                data["criteria"] = ProjectCriteria.from_dict(data["criteria"])
            else:
                data["criteria"] = EngineeringCriteria.from_dict(data["criteria"])
        return cls(**data)
