"""Registered, read-only professional analysis example.

Author: yangshaopeng. This module never submits jobs or writes core results.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ExtensionError(ValueError):
    """A stable error code; messages do not contain connection secrets."""


def _fail(code):
    raise ExtensionError(code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _json(data):
    try:
        value = json.loads(data, object_pairs_hook=_object,
                           parse_constant=lambda _: _fail("NONFINITE_JSON"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _fail("INVALID_JSON")
    if not isinstance(value, dict):
        _fail("EXPECTED_JSON_OBJECT")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalResultsClient:
    """Read only from one explicitly selected, trusted loopback service."""

    def __init__(self, connection_file):
        path = Path(connection_file)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            _fail("TRUSTED_ABSOLUTE_CONNECTION_FILE_REQUIRED")
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            _fail("CONNECTION_FILE_TOO_LARGE")
        connection = _json(raw)
        url, token = connection.get("url"), connection.get("token")
        if connection.get("service") != "tianshu-professional" or not isinstance(url, str):
            _fail("UNTRUSTED_CONNECTION")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            _fail("LOOPBACK_URL_REQUIRED")
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or port is None or not 1 <= port <= 65535
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            _fail("LOOPBACK_URL_REQUIRED")
        if (not isinstance(token, str) or len(token) < 24
                or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            _fail("INVALID_LOCAL_TOKEN")
        self._base = f"http://127.0.0.1:{port}"
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def get(self, path, limit):
        if not isinstance(path, str) or not re.fullmatch(
                r"/v1/jobs/pf-[A-Za-z0-9_-]+(?:/artifacts/result\.json)?", path):
            _fail("READ_ONLY_ENDPOINT_REQUIRED")
        request = Request(self._base + path,
                          headers={"Authorization": "Bearer " + self._token}, method="GET")
        try:
            with self._opener.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != request.full_url:
                    _fail("UNEXPECTED_HTTP_RESPONSE")
                data = response.read(limit + 1)
        except HTTPError as exc:
            _fail("HTTP_" + str(exc.code))
        except (URLError, TimeoutError, OSError):
            _fail("LOCAL_SERVICE_UNAVAILABLE")
        if len(data) > limit:
            _fail("RESPONSE_TOO_LARGE")
        return data


class ToolRegistry:
    """Add a derived reader without changing ProfessionalService or its writer.

    Handlers receive a private copy of the engineering analysis. This is an
    extension convention, not a sandbox for untrusted Python code.
    """

    def __init__(self):
        self._tools = {}

    def register(self, name, handler):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in self._tools or not callable(handler):
            _fail("INVALID_OR_DUPLICATE_TOOL")
        self._tools[name] = handler

    def run(self, name, analysis):
        if name not in self._tools:
            _fail("UNKNOWN_TOOL")
        return self._tools[name](copy.deepcopy(analysis))

    def names(self):
        return tuple(sorted(self._tools))


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def native_voltage_envelope(analysis):
    """Select existing native extrema; do not recompute or judge compliance."""
    metrics = analysis.get("metrics")
    if not isinstance(metrics, list):
        _fail("METRICS_UNAVAILABLE")
    selected = []
    for metric in ("native_minimum_voltage_pu", "native_maximum_voltage_pu"):
        matches = [(index, row) for index, row in enumerate(metrics)
                   if isinstance(row, dict) and row.get("metric") == metric]
        if len(matches) != 1:
            _fail("METRIC_MISSING_OR_AMBIGUOUS")
        index, row = matches[0]
        refs = row.get("evidence_refs")
        if (row.get("status") != "available" or row.get("unit") != "pu"
                or row.get("measurement_basis") != "node_to_ground_native_ln_base"
                or not _finite(row.get("value")) or row["value"] < 0
                or not _finite(row.get("base_voltage_v")) or row["base_voltage_v"] <= 0
                or not isinstance(row.get("object_id"), str) or not row["object_id"]
                or type(row.get("node")) is not int or row["node"] <= 0
                or not isinstance(row.get("base_source"), str) or not row["base_source"]
                or not isinstance(refs, list) or not refs
                or not all(isinstance(ref, str) and ref for ref in refs)):
            _fail("METRIC_METADATA_INVALID")
        selected.append({key: row.get(key) for key in (
            "metric", "value", "unit", "measurement_basis", "object_id", "node", "phase",
            "base_voltage_v", "base_source", "scope", "evidence_refs")})
        selected[-1]["source_pointer"] = f"result.json#/engineering_analysis/metrics/{index}"
    if selected[0]["value"] > selected[1]["value"]:
        _fail("EXTREMA_INCONSISTENT")
    return {"metrics": selected, "scope": "available_verified_non_ground_standard_nodes_only",
            "interpretation": "Native node-to-ground normalization; no project voltage limit applied."}


TOOLS = ToolRegistry()
TOOLS.register("native_voltage_envelope", native_voltage_envelope)


def _terminal(status, task_id):
    if (status.get("task_id") != task_id or status.get("execution_status") != "finished"
            or status.get("numerical_validity") != "converged_valid"
            or not all(isinstance(status.get(key), str) and status[key]
                       for key in ("request_id", "attempt_id"))
            or "result.json" not in status.get("artifacts", [])):
        _fail("VALID_FINISHED_TASK_REQUIRED")


def run_extension(connection_file, task_id, tool="native_voltage_envelope"):
    if not isinstance(task_id, str) or not re.fullmatch(r"pf-[A-Za-z0-9_-]+", task_id):
        _fail("INVALID_TASK_ID")
    client = LocalResultsClient(connection_file)
    endpoint = "/v1/jobs/" + task_id
    before = _json(client.get(endpoint, 1024 * 1024))
    _terminal(before, task_id)
    # The existing core endpoint verifies the committed artifact hash and size.
    data = client.get(endpoint + "/artifacts/result.json", 128 * 1024 * 1024)
    result = _json(data)
    analysis = result.get("engineering_analysis", {})
    if (not isinstance(analysis, dict)
            or any(result.get(key) != before[key] for key in ("task_id", "request_id", "attempt_id"))
            or result.get("execution_status") != "finished"
            or result.get("numerical_validity") != "converged_valid"
            or result.get("publishable") is not True
            or result.get("verification", {}).get("passed") is not True
            or result.get("verification", {}).get("task_id") != task_id
            or result.get("domain_observations_verification", {}).get("passed") is not True
            or analysis.get("task_id") != task_id
            or analysis.get("numerical_validity") != "converged_valid"
            or analysis.get("coverage", {}).get("trusted_task_bound_observations") is not True):
        _fail("CORE_RESULT_BINDING_OR_VERIFICATION_INVALID")
    output = TOOLS.run(tool, analysis)
    after = _json(client.get(endpoint, 1024 * 1024))
    _terminal(after, task_id)
    if any(after.get(key) != before.get(key) for key in ("task_id", "request_id", "attempt_id", "updated_at")):
        _fail("TASK_CHANGED_DURING_READ")
    return {"kind": "read_only_extension", "tool": tool, "authoritative": False,
            "engineering_acceptance": False,
            "source": {key: result[key] for key in ("task_id", "request_id", "attempt_id")},
            "result_artifact_sha256": hashlib.sha256(data).hexdigest(), "analysis": output}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", required=True, help="Explicit trusted absolute local-service.json path")
    parser.add_argument("--task-id", required=True, help="Existing task in the authorized local service")
    parser.add_argument("--tool", choices=TOOLS.names(), default="native_voltage_envelope")
    args = parser.parse_args(argv)
    try:
        output = run_extension(args.connection_file, args.task_id, args.tool)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except ExtensionError as exc:
        error = str(exc)
    except Exception:
        # Never include connection contents, HTTP bodies, or token-bearing traces.
        error = "EXTENSION_READ_FAILED"
    print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
