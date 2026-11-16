"""Deterministic, integrity-checked offline professional report bundle.

Copyright (c) yangshaopeng. Packaging never solves, renders, or evaluates data.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import io
import json
import math
import re
from typing import Any
import zipfile


BUNDLE_SCHEMA_VERSION = "1.0"
BUNDLE_FILENAME = "tianshu-professional-report.zip"
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
ARTIFACT_PURPOSES = {
    "report.html": "工程校核阅读入口；明细预览不代替完整结果",
    "result.json": "权威任务结果、输入绑定、校核与数值验证证据",
    "checks.csv": "完整逐项项目判据校核记录",
    "engineering.csv": "完整逐项工程指标及证据引用",
    "observations.json": "完整原生观测合同",
    "observations.csv": "完整原生节点及端口导体观测表",
}
_NAMES = tuple(ARTIFACT_PURPOSES)
_NAME_SET = frozenset(_NAMES)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}\Z")


class ArtifactBundleError(ValueError):
    """Incomplete, inconsistent, unsupported, or unverified bundle input."""


def _fail(message: str):
    raise ArtifactBundleError(message)


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        _fail(field + " must be a lowercase SHA256 digest")
    return value


def _identity(value: Any, field: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 160
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:\-]*", value) is None
            or (field != "request_id" and ":" in value)):
        _fail(field + " must be a bounded identifier without paths")
    return value


def _version(value: Any, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        _fail(field + " must be a bounded version identifier")
    return value


def _json_object(data: bytes, label: str) -> Mapping:
    def nonfinite(_):
        _fail(label + " contains nonfinite JSON")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail(label + " contains duplicate JSON keys")
            value[key] = item
        return value

    def finite_number(text):
        value = float(text)
        if not math.isfinite(value):
            _fail(label + " contains a nonfinite JSON number")
        return value

    try:
        value = json.loads(data, parse_constant=nonfinite, parse_float=finite_number, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ArtifactBundleError):
            raise
        raise ArtifactBundleError(label + " must contain valid finite JSON") from None
    if not isinstance(value, Mapping):
        _fail(label + " must contain a JSON object")
    return value


def _binding(task_id: str, artifacts: Mapping[str, bytes], expected_professional_version: str | None) -> dict:
    result = _json_object(artifacts["result.json"], "result.json")
    if result.get("task_id") != task_id:
        _fail("result.json task identity differs from the requested task")
    attempt_id = _identity(result.get("attempt_id"), "attempt_id")
    if not attempt_id.startswith(task_id + "-a") or not attempt_id[len(task_id) + 2:].isdigit():
        _fail("attempt_id does not belong to the requested task")
    request_id = _identity(result.get("request_id"), "request_id")
    request = result.get("request")
    if not isinstance(request, Mapping) or request.get("request_id") != request_id:
        _fail("authoritative request identity is missing or inconsistent")
    formats = request.get("output_formats")
    if (not isinstance(formats, (list, tuple)) or len(formats) != 3
            or any(not isinstance(value, str) for value in formats)
            or set(formats) != {"html", "json", "csv"}):
        _fail("a complete offline bundle requires the recorded HTML, JSON and CSV formats")
    inputs = result.get("input")
    if not isinstance(inputs, Mapping):
        _fail("authoritative input binding is missing")
    professional_version = _version(result.get("professional_version"), "professional_version")
    if expected_professional_version is not None and professional_version != expected_professional_version:
        _fail("professional version differs from the expected committed version")
    observation = result.get("professional_observations")
    if not isinstance(observation, Mapping) or observation.get("task_id") != task_id:
        _fail("authoritative observation task binding is missing or inconsistent")
    observation_schema = _version(observation.get("schema_version"), "observation_schema_version")
    observation_validity = observation.get("numerical_validity")
    if observation_validity != result.get("numerical_validity"):
        _fail("authoritative observation numerical state is inconsistent")
    engineering = result.get("engineering_analysis")
    if not isinstance(engineering, Mapping) or engineering.get("task_id") != task_id:
        _fail("authoritative engineering task binding is missing or inconsistent")
    binding = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "request_id": request_id,
        "request_digest": _digest(result.get("request_digest"), "request_digest"),
        "input_fingerprint": _digest(inputs.get("fingerprint"), "input_fingerprint"),
        "professional_version": professional_version,
        "result_schema_version": _version(result.get("schema_version"), "result_schema_version"),
        "observation_schema_version": observation_schema,
        "engineering_schema_version": _version(engineering.get("schema_version"), "engineering_schema_version"),
    }
    try:
        request_bytes = json.dumps(request, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ArtifactBundleError("authoritative request must be finite UTF-8 JSON") from None
    request_digest = hashlib.sha256(request_bytes).hexdigest()
    if request_digest != binding["request_digest"]:
        _fail("authoritative request digest differs from its recorded request")
    imported = inputs.get("imported_model_fingerprint")
    if imported is not None:
        binding["imported_model_fingerprint"] = _digest(imported, "imported_model_fingerprint")
    # Release the large result tree before decoding the separate observations.
    del result, request, inputs, observation, engineering
    observations = _json_object(artifacts["observations.json"], "observations.json")
    if (observations.get("task_id") != task_id or observations.get("schema_version") != observation_schema
            or observations.get("numerical_validity") != observation_validity):
        _fail("observations.json identity, schema or numerical state differs from the authority")
    return binding


def build_artifact_bundle(
    task_id: str,
    artifacts: Mapping[str, bytes],
    *,
    expected_artifacts: Mapping[str, Mapping[str, Any]],
    expected_professional_version: str | None = None,
) -> bytes:
    """Package the six committed artifacts unchanged, bound to verified metadata.

    The caller must obtain ``expected_artifacts`` from its trusted committed
    artifact ledger, not a download request. Input metadata authenticates no
    person and creates no engineering recognition. No filesystem, clock, model,
    network, token, raw solver file, or log is consulted or added to the ZIP.
    ``complete`` means this exact six-file deliverable, not engineering approval.
    """
    task_id = _identity(task_id, "task_id")
    if not isinstance(artifacts, Mapping) or set(artifacts) != _NAME_SET:
        _fail("exactly the six fixed professional artifact names are required")
    # Keep the verified immutable byte references even if the caller changes
    # its mapping while the larger JSON documents are being checked.
    artifacts = {name: artifacts[name] for name in _NAMES}
    if not isinstance(expected_artifacts, Mapping) or set(expected_artifacts) != _NAME_SET:
        _fail("trusted size and SHA256 metadata for all six artifacts is required")
    if expected_professional_version is not None:
        _version(expected_professional_version, "expected_professional_version")
    files = []
    total = 0
    for name in _NAMES:
        data = artifacts[name]
        if not isinstance(data, bytes):
            _fail("artifact content must be immutable bytes")
        size = len(data)
        if size == 0 or size > MAX_FILE_BYTES:
            _fail("artifact is empty or exceeds the 128 MiB file budget")
        total += size
        if total > MAX_TOTAL_BYTES:
            _fail("artifacts exceed the 256 MiB total uncompressed budget")
        expected = expected_artifacts[name]
        if (not isinstance(expected, Mapping) or set(expected) != {"sha256", "bytes"}
                or type(expected.get("bytes")) is not int):
            _fail("trusted artifact metadata must contain exactly bytes and sha256")
        digest = hashlib.sha256(data).hexdigest()
        if expected["bytes"] != size or _digest(expected.get("sha256"), "artifact sha256") != digest:
            _fail("artifact size or SHA256 differs from its committed metadata")
        files.append({"name": name, "sha256": digest, "bytes": size, "purpose": ARTIFACT_PURPOSES[name]})
    binding = _binding(task_id, artifacts, expected_professional_version)
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_version": "1.0",
        "bundle_kind": "tianshu_professional_complete_offline_report",
        "complete": True,
        "completeness_scope": "six_committed_professional_artifacts_only",
        "engineering_recognition": "not_assessed_by_bundle",
        "entrypoint": "report.html",
        "usage": "解压全部文件到同一目录后打开 report.html；产物原字节保留，完整包不表示工程验收通过。",
        "total_artifact_bytes": total,
        **binding,
        "artifacts": files,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2).encode("utf-8")
    if len(manifest_bytes) > MAX_FILE_BYTES or total + len(manifest_bytes) > MAX_TOTAL_BYTES:
        _fail("bundle manifest would exceed the uncompressed file or total budget")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name in (*_NAMES, "manifest.json"):
            data = manifest_bytes if name == "manifest.json" else artifacts[name]
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100600 << 16
            with archive.open(info, mode="w", force_zip64=True) as output:
                view = memoryview(data)
                for start in range(0, len(view), _CHUNK_BYTES):
                    output.write(view[start:start + _CHUNK_BYTES])
    return stream.getvalue()
