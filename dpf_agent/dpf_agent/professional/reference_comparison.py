"""Read-only engineering reference comparison. Copyright yangshaopeng.

A declared reference kind or confirmer never establishes recognized real
engineering acceptance. This module runs no solver and authenticates nobody.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import math
import re

from .contracts import ContractError, _choice, _identifier, _integer, _object, _text


REFERENCE_SCHEMA_VERSION = "1.0"
REFERENCE_KINDS = frozenset({"public", "analytic_fixture", "recognized_engineering_declared", "software_generated"})
MAX_COLLECTION_PARTS = 8
MAX_COLLECTION_POINTS = 20000
_SELECTORS = ("node", "reference_node", "terminal", "conductor")
_POINT_REQUIRED = {"id", "metric", "object_id", "unit", "measurement_basis", "value",
                   "absolute_tolerance", "relative_tolerance", "near_zero_floor"}
_REFERENCE_FIELDS = {"schema_version", "reference_id", "version", "source", "confirmed_by",
                     "confirmed_at", "reference_kind", "input_fingerprint", "scenario_digest", "points"}
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def _canonical(value, path):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise ContractError(path, "must be a finite UTF-8 JSON value") from None


def _digest(value, path):
    return hashlib.sha256(_canonical(value, path)).hexdigest()


def scenario_digest(request):
    """SHA256 of the exact declared scenario and operating-conditions JSON.

    The canonical object always includes operating_conditions; missing legacy
    conditions become null. No scenario values, units, or numbers are changed.
    A RunRequest instance or its plain serialized object is accepted.
    """
    from .contracts import RunRequest
    if isinstance(request, RunRequest):
        request = request.to_dict()
    if not isinstance(request, Mapping) or not isinstance(request.get("scenario"), Mapping):
        raise ContractError("request.scenario", "must be an object")
    conditions = request.get("operating_conditions")
    if conditions is not None and not isinstance(conditions, Mapping):
        raise ContractError("request.operating_conditions", "must be an object or null")
    return _digest({"scenario": dict(request["scenario"]),
                    "operating_conditions": dict(conditions) if conditions is not None else None}, "request.scenario")


def _finite(value, path, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(path, "must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ContractError(path, "must be a finite number") from None
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise ContractError(path, "must be finite and nonnegative" if nonnegative else "must be finite")
    return 0.0 if number == 0 else number


def _hash(value, path):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ContractError(path, "must be a lowercase SHA256 hex digest")
    return value


def _identity(row, path):
    for key, maximum in (("metric", 128), ("object_id", 256), ("unit", 32), ("measurement_basis", 256)):
        _text(row.get(key), path + "." + key, maximum)
    if any(char in row["object_id"] for char in "*?[]"):
        raise ContractError(path + ".object_id", "must identify one exact object")
    for key in _SELECTORS:
        value = row.get(key)
        if value is not None:
            _integer(value, path + "." + key, 0 if key in {"node", "reference_node"} else 1,
                     32 if key == "terminal" else 64 if key == "conductor" else 2147483647)
    # Native identity is exact; no phase label, suffix, wildcard or unit
    # conversion is inferred. All selectors present on a metric must match.
    return (row["metric"], row["object_id"].casefold(), row["unit"], row["measurement_basis"],
            *(row.get(key) for key in _SELECTORS))


def _selector(identity):
    return identity[:2] + identity[3:]


def _where(row):
    return {key: row.get(key) for key in ("metric", "object_id", "unit", "measurement_basis", *_SELECTORS)}


def _reference(value):
    data = _object(value, _REFERENCE_FIELDS, _REFERENCE_FIELDS, "reference")
    _choice(data["schema_version"], {REFERENCE_SCHEMA_VERSION}, "reference.schema_version")
    _identifier(data["reference_id"], "reference.reference_id")
    for name in ("version", "source", "confirmed_by", "confirmed_at"):
        _text(data[name], "reference." + name, 4096 if name == "source" else 1024)
    try:
        datetime.fromisoformat(data["confirmed_at"].replace("Z", "+00:00"))
    except ValueError:
        raise ContractError("reference.confirmed_at", "must be an ISO date or datetime") from None
    _choice(data["reference_kind"], REFERENCE_KINDS, "reference.reference_kind")
    _hash(data["input_fingerprint"], "reference.input_fingerprint")
    _hash(data["scenario_digest"], "reference.scenario_digest")
    if not isinstance(data["points"], (list, tuple)) or len(data["points"]) > 10000:
        raise ContractError("reference.points", "must be an array of at most 10000 points")
    ids, selectors, points = set(), set(), []
    for index, supplied in enumerate(data["points"]):
        path = "reference.points[" + str(index) + "]"
        point = _object(supplied, _POINT_REQUIRED | set(_SELECTORS), _POINT_REQUIRED, path)
        _identifier(point["id"], path + ".id")
        identity = _identity(point, path)
        if point["id"] in ids:
            raise ContractError(path + ".id", "must be unique")
        if _selector(identity) in selectors:
            raise ContractError(path, "target selector must be unique irrespective of unit or object-name case")
        ids.add(point["id"]); selectors.add(_selector(identity))
        # Validate finite numbers without replacing their original JSON number
        # representation: the reference digest identifies exactly that input.
        _finite(point["value"], path + ".value")
        for name in ("absolute_tolerance", "relative_tolerance", "near_zero_floor"):
            _finite(point[name], path + "." + name, nonnegative=True)
        points.append(point)
    data["points"] = points
    _canonical(data, "reference")
    return data


def compare_engineering_reference(result, reference):
    """Compare frozen explicit points with validated same-task metrics only.

    Invalid reference contracts raise ContractError. Task/input/condition gaps
    and missing or ambiguous numerical facts produce explicit not_assessed
    points. A sparse passing reference is a partial comparison, not full
    coverage or recognized engineering acceptance. Neither input is mutated.
    """
    return _compare_validated_reference(result, _reference(reference))


def compare_engineering_reference_collection(result, collection):
    """Compare a bounded collection without weakening single-part validation.

    Each part retains the existing 10000-point contract. All parts must bind
    the same inputs, scenario and declared provenance; no overlapping targets
    or repeated point/part identities are allowed. Coverage is evaluated once
    against the complete task metric inventory, never by AND-ing subset passes.
    This function does not solve, store references, or approve engineering use.
    """
    fields = {"schema_version", "collection_id", "version", "references"}
    data = _object(collection, fields, fields, "collection")
    _choice(data["schema_version"], {"1.0"}, "collection.schema_version")
    _identifier(data["collection_id"], "collection.collection_id")
    _text(data["version"], "collection.version", 1024)
    supplied = data["references"]
    if not isinstance(supplied, (list, tuple)) or not 1 <= len(supplied) <= MAX_COLLECTION_PARTS:
        raise ContractError("collection.references", "must contain 1 to 8 bounded reference parts")
    shared = ("schema_version", "version", "source", "confirmed_by", "confirmed_at",
              "reference_kind", "input_fingerprint", "scenario_digest")
    parts, points, part_ids, point_ids, selectors = [], [], set(), set(), set()
    first = None
    for index, value in enumerate(supplied):
        ref = _reference(value)
        path = "collection.references[" + str(index) + "]"
        if not ref["points"]:
            raise ContractError(path + ".points", "collection parts must contain reference points")
        if ref["reference_id"] in part_ids:
            raise ContractError(path + ".reference_id", "must be unique across the collection")
        part_ids.add(ref["reference_id"])
        if first is None:
            first = ref
        elif any(ref[key] != first[key] for key in shared):
            raise ContractError(path, "all parts must declare identical input, scenario and provenance metadata")
        if len(points) + len(ref["points"]) > MAX_COLLECTION_POINTS:
            raise ContractError("collection.references", "collection exceeds the 20000-point total budget")
        for point in ref["points"]:
            if point["id"] in point_ids:
                raise ContractError(path + ".points", "point ids must be unique across the collection")
            selector = _selector(_identity(point, path + ".point"))
            if selector in selectors:
                raise ContractError(path + ".points", "target selectors must be unique across the collection")
            point_ids.add(point["id"])
            selectors.add(selector)
            points.append(point)
        parts.append({"reference_id": ref["reference_id"], "reference_digest": _digest(ref, path),
                      "reference_points": len(ref["points"])})
    # All points have passed the original single-part validation above. Only
    # the bounded union enters the shared numerical/task/coverage checks.
    merged = {key: first[key] for key in _REFERENCE_FIELDS if key not in {"reference_id", "points"}}
    merged.update(reference_id=data["collection_id"], points=points)
    report = _compare_validated_reference(result, merged)
    report.update(comparison_scope="explicit_frozen_reference_collection",
                  collection_id=data["collection_id"], collection_version=data["version"],
                  collection_digest=_digest(data, "collection"), reference_parts=parts,
                  collection_limits={"max_parts": MAX_COLLECTION_PARTS, "max_points_per_part": 10000,
                                     "max_total_points": MAX_COLLECTION_POINTS})
    report["limitations"].append("Collection coverage is evaluated over the exact union; numerical agreement does not authenticate any declared reference source.")
    _canonical(report, "comparison_report")
    return report


def _compare_validated_reference(result, ref):
    if not isinstance(result, Mapping):
        raise ContractError("result", "must be an object")
    analysis = result.get("engineering_analysis")
    if not isinstance(analysis, Mapping):
        analysis = {}
    request = result.get("request")
    gates = []
    actual_scenario = actual_request_digest = None
    try:
        actual_scenario = scenario_digest(request)
        actual_request_digest = _digest(request, "result.request")
    except ContractError:
        gates.append("request_scenario_or_operating_conditions_invalid")
    supplied_request_digest = result.get("request_digest")
    if supplied_request_digest != actual_request_digest or actual_request_digest is None:
        gates.append("request_digest_missing_or_mismatched")
    if (result.get("publishable") is not True or result.get("execution_status") != "finished"
            or result.get("numerical_validity") != "converged_valid"
            or not isinstance(result.get("verification"), Mapping)
            or result["verification"].get("passed") is not True
            or result["verification"].get("task_id") != result.get("task_id")
            or not isinstance(result.get("domain_observations_verification"), Mapping)
            or result["domain_observations_verification"].get("passed") is not True):
        gates.append("numerical_task_not_validated")
    coverage = analysis.get("coverage")
    if (analysis.get("schema_version") != "1.0" or not isinstance(coverage, Mapping)
            or coverage.get("trusted_task_bound_observations") is not True
            or analysis.get("task_id") != result.get("task_id")
            or not isinstance(result.get("task_id"), str) or not result["task_id"]
            or analysis.get("numerical_validity") != "converged_valid"):
        gates.append("engineering_metrics_not_task_bound_and_validated")
    input_data = result.get("input")
    actual_input = input_data.get("fingerprint") if isinstance(input_data, Mapping) else None
    if actual_input != ref["input_fingerprint"]:
        gates.append("input_fingerprint_mismatch")
    if actual_scenario != ref["scenario_digest"]:
        gates.append("scenario_digest_mismatch")

    indexed, metric_rows, metric_issues = defaultdict(list), [], []
    metrics = analysis.get("metrics")
    if not isinstance(metrics, (list, tuple)) or len(metrics) > 100000:
        metrics = []
        gates.append("engineering_metric_array_missing_or_invalid")
    for index, metric in enumerate(metrics):
        try:
            if not isinstance(metric, Mapping):
                raise ContractError("metric", "must be an object")
            identity = _identity(metric, "metric")
            value = _finite(metric.get("value"), "metric.value")
            if metric.get("status") != "available":
                raise ContractError("metric.status", "must be available")
            metric_rows.append((index, metric, identity, value))
            indexed[identity].append((index, metric, value))
        except ContractError as exc:
            metric_issues.append({"index": index, "reason": "metric_not_available_or_invalid", "detail": str(exc)})

    items, matched = [], set()
    for point in ref["points"]:
        identity = _identity(point, "reference.point")
        matches = indexed.get(identity, [])
        value = _finite(point["value"], "reference.point.value")
        floor = _finite(point["near_zero_floor"], "reference.point.near_zero_floor", nonnegative=True)
        near_zero = abs(value) <= floor
        item = {"point_id": point["id"], **_where(point), "reference_value": value,
                "absolute_tolerance": point["absolute_tolerance"], "relative_tolerance": point["relative_tolerance"],
                "near_zero_floor": floor, "near_zero_reference": near_zero,
                "relative_component_applied": not near_zero, "relative_component": None,
                "allowed_absolute_error": None, "actual_value": None, "absolute_error": None,
                "relative_error": None, "status": "not_assessed", "reason": None,
                "evidence_refs": []}
        if gates:
            item["reason"] = "task_or_reference_identity_not_validated"
        elif not matches:
            item["reason"] = "exact_engineering_metric_missing_or_invalid"
        elif len(matches) != 1:
            item["reason"] = "exact_engineering_metric_ambiguous"
        else:
            index, metric, actual = matches[0]
            matched.add(index)
            error = abs(actual - value)
            relative = 0.0 if near_zero else point["relative_tolerance"] * abs(value)
            allowed = point["absolute_tolerance"] + relative
            if not all(math.isfinite(number) for number in (error, relative, allowed)):
                item["reason"] = "error_or_tolerance_computation_not_finite"
            else:
                item.update(actual_value=actual, absolute_error=error, relative_component=relative,
                            allowed_absolute_error=allowed, status="pass" if error <= allowed else "mismatch",
                            reason="within_explicit_reference_tolerance" if error <= allowed else "outside_explicit_reference_tolerance")
                if not near_zero:
                    relative_error = error / abs(value)
                    item["relative_error"] = relative_error if math.isfinite(relative_error) else None
                refs = metric.get("evidence_refs")
                if isinstance(refs, (list, tuple)):
                    item["evidence_refs"] = [value for value in refs if isinstance(value, str)]
        items.append(item)

    counts = Counter(item["status"] for item in items)
    duplicate_metrics = [identity for identity, rows in indexed.items() if len(rows) != 1]
    unmatched = [{"metric_index": index, **_where(metric), "reason": "not_selected_by_exact_reference_point"}
                 for index, metric, identity, value in metric_rows if index not in matched]
    points_passed = bool(items and not gates and counts["pass"] == len(items))
    complete = bool(points_passed and not unmatched and not metric_issues and not duplicate_metrics
                    and len(matched) == len(metric_rows) and metric_rows)
    status = ("mismatch" if counts["mismatch"] else "not_assessed" if gates or counts["not_assessed"] or not items
              else "passed" if complete else "partial_comparison_passed")
    report = {"schema_version": "1.0", "comparison_scope": "explicit_frozen_reference_points_only",
              "status": status, "passed": complete, "all_reference_points_passed": points_passed,
              "task_id": result.get("task_id"), "numerical_validity": result.get("numerical_validity"),
              "reference_id": ref["reference_id"], "reference_version": ref["version"],
              "reference_kind": ref["reference_kind"], "reference_source": ref["source"],
              "confirmed_by": ref["confirmed_by"], "confirmed_at": ref["confirmed_at"],
              "reference_digest": _digest(ref, "reference"), "request_digest": actual_request_digest,
              "recorded_request_digest": supplied_request_digest,
              "input_fingerprint": actual_input, "reference_input_fingerprint": ref["input_fingerprint"],
              "scenario_digest": actual_scenario, "reference_scenario_digest": ref["scenario_digest"],
              "comparison_equation": "abs(actual-reference) <= absolute_tolerance + relative_tolerance*abs(reference); relative component=0 when abs(reference)<=near_zero_floor",
              "counts": {key: counts[key] for key in ("pass", "mismatch", "not_assessed")},
              "items": items, "gating_issues": gates, "metric_issues": metric_issues,
              "coverage": {"reference_points": len(items), "available_metric_records": len(metric_rows),
                           "matched_metric_records": len(matched), "unmatched_metric_records": len(unmatched),
                           "ambiguous_metric_identities": len(duplicate_metrics), "complete": complete,
                           "scope": "all_available_engineering_analysis_metrics_only_not_engineering_acceptance"},
              "unmatched_metrics": unmatched, "engineering_accepted": False,
              "reference_recognition": "caller_declared_not_independently_authenticated",
              "limitations": ["Reference kind and confirmation are caller declarations, not proof of A07 recognition.",
                              "A passing subset does not establish complete metric coverage or network engineering compliance.",
                              "No solver is run; no unit conversion, phase identity, base or tolerance is inferred."]}
    if ref["reference_kind"] == "software_generated":
        report["limitations"].append("Software-generated values are not automatically a published reference or an independent numerical solver; record the actual software/version and derivation in source.")
    _canonical(report, "comparison_report")
    return report
