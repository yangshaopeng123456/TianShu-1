"""Repeatable delivery checks; never sync files or stop an existing service.

Examples (paths are supplied by the caller, not tied to a workstation/date)::

    python -m dpf_agent.release_gate --mode preflight --evidence-dir <new-dir>
    python -m dpf_agent.release_gate --mode final --release-root <release-tree> \
        --evidence-dir <new-dir> --native-command-json '["python", "smoke.py"]'

The native runner receives DPF_GATE_REQUEST and DPF_GATE_NATIVE_RESULT in its
environment. The request specifies run_id, source_manifest_sha256 and versions;
the result must echo these, declare native_executed=true, identify OpenDSS COM,
and provide successful task/verification records with actual evidence files.
An exit code of zero alone is insufficient. Final delivery requires this check;
preflight may explicitly report native coverage as skipped.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
import uuid


SCHEMA_VERSION = 1
DEFAULT_SUITE = "dpf_agent.tests.run_all"
UI_CHECKS = ("verify_races.js", "verify_backend_lifecycle.js", "verify_render.js")
DISCOVERED_UI_CHECKS = ("verify_batch.js",)
SOURCE_SCOPE = {
    "recursive_roots": ["dpf_agent/dpf_agent", "tianshu_ui"],
    "backend_root_files": ["requirements.txt", "pyproject.toml", "setup.cfg", "setup.py", "run.py", "run_cli.py", "start_backend.py"],
    "excluded_directories": [".git", ".venv", "node_modules", "__pycache__", "chat_data", "tasks", "outputs", "logs", "build", "dist"],
    "excluded_files": [".env*", "*credentials*", "settings.json", "*.pyc", "*.pyo", "*.log", "*.tmp"],
}
_EXCLUDED_DIRS = set(SOURCE_SCOPE["excluded_directories"])
_REQUIRED_FILES = ("dpf_agent/dpf_agent/__init__.py", "dpf_agent/dpf_agent/web/conversion_cache.py",
                   "dpf_agent/dpf_agent/tests/run_all.py", "dpf_agent/requirements.txt",
                   "tianshu_ui/package.json", "tianshu_ui/main.js", *[f"tianshu_ui/{name}" for name in UI_CHECKS])


@dataclass(frozen=True)
class GateConfig:
    dev_root: Path
    evidence_dir: Path
    mode: str = "preflight"
    release_root: Path | None = None
    node: str | None = None
    python: str = sys.executable
    native: str = "optional"
    native_command: tuple[str, ...] = ()
    suite_module: str = DEFAULT_SUITE
    timeout_s: float = 900.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _excluded_file(path: Path) -> bool:
    name = path.name.casefold()
    return (name.startswith(".env") or "credentials" in name or name == "settings.json"
            or path.suffix.casefold() in {".pyc", ".pyo", ".log", ".tmp"})


def source_inventory(root: Path) -> dict[str, dict[str, Any]]:
    """Inventory the entire declared delivery scope on either tree.

    Runtime/user data, virtual environments, case archives and secrets are out of
    scope. The exclusions are reported, not an assertion that both whole disks
    are identical. Unexpected extra files inside the scope are compared too.
    """
    root = root.resolve()
    files: list[Path] = []
    for relative in SOURCE_SCOPE["recursive_roots"]:
        directory = root / relative
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"Missing/nonlocal delivery directory: {relative}")
        for folder, directories, names in os.walk(directory, followlinks=False):
            for name in directories:
                if name not in _EXCLUDED_DIRS and (Path(folder) / name).is_symlink():
                    raise ValueError(f"Delivery directory symlink is unsupported: {Path(folder) / name}")
            directories[:] = sorted(name for name in directories if name not in _EXCLUDED_DIRS)
            for name in sorted(names):
                candidate = Path(folder) / name
                if _excluded_file(candidate):
                    continue
                if candidate.is_symlink():
                    raise ValueError(f"Delivery file symlink is unsupported: {candidate}")
                files.append(candidate)
    backend = root / "dpf_agent"
    for name in SOURCE_SCOPE["backend_root_files"]:
        candidate = backend / name
        if candidate.is_file():
            if candidate.is_symlink():
                raise ValueError(f"Delivery file symlink is unsupported: {candidate}")
            files.append(candidate)
    for candidate in backend.glob("start*.bat"):
        if candidate.is_file():
            files.append(candidate)
    inventory = {path.relative_to(root).as_posix(): {"sha256": _sha256(path), "bytes": path.stat().st_size}
                 for path in sorted(set(files))}
    missing = [name for name in _REQUIRED_FILES if name not in inventory]
    if missing:
        raise ValueError("Missing required delivery files: " + ", ".join(missing))
    return inventory


def inventory_fingerprint(inventory: dict[str, dict[str, Any]]) -> str:
    payload = [(name, record["sha256"]) for name, record in sorted(inventory.items())]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _literal_assignments(path: Path) -> dict[str, Any]:
    assignments: dict[str, Any] = {}
    for statement in ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path)).body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"__version__", "ADAPTER_VERSION", "CONVERTER_VERSIONS"}:
                assignments[target.id] = ast.literal_eval(value)
    return assignments


def read_versions(root: Path) -> dict[str, Any]:
    backend = _literal_assignments(root / "dpf_agent/dpf_agent/__init__.py")
    converters = _literal_assignments(root / "dpf_agent/dpf_agent/web/conversion_cache.py").get("CONVERTER_VERSIONS")
    ui = json.loads((root / "tianshu_ui/package.json").read_text(encoding="utf-8-sig")).get("version")
    versions = {"backend": backend.get("__version__"), "adapter": backend.get("ADAPTER_VERSION"),
                "converters": converters, "ui": ui}
    if not versions["backend"] or versions["backend"] != versions["adapter"]:
        raise ValueError("Backend and adapter versions are missing or inconsistent")
    if not isinstance(converters, dict) or not converters or not all(isinstance(value, str) and value for value in converters.values()):
        raise ValueError("Converter version map is missing/invalid")
    if not isinstance(ui, str) or not ui:
        raise ValueError("UI version is missing/invalid")
    return versions


def compare_inventories(dev: dict, release: dict) -> dict[str, Any]:
    return {"missing_in_release": sorted(set(dev) - set(release)),
            "extra_in_release": sorted(set(release) - set(dev)),
            "different": sorted(name for name in set(dev) & set(release)
                                if dev[name]["sha256"] != release[name]["sha256"])}


def _step(name: str, status: str, *, required: bool = True, **details) -> dict[str, Any]:
    return {"name": name, "status": status, "required": required, **details}


def _stop_owned_process(process: subprocess.Popen) -> None:
    """Only terminate the process tree launched by this check."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def _safe_argv(argv: list[str]) -> list[str]:
    result, redact_next = [], False
    for value in argv:
        if redact_next:
            result.append("[redacted]")
            redact_next = False
        elif re.match(r"(?i)^--?(?:api[-_]?key|token|password|secret|credential)(?:=|$)", value):
            key, separator, _ = value.partition("=")
            result.append(key + "=[redacted]" if separator else key)
            redact_next = not bool(separator)
        else:
            result.append(value)
    return result


def _command(name: str, argv: list[str], cwd: Path, env: dict[str, str], evidence: Path,
             timeout_s: float, *, reject_skip: bool = False) -> dict[str, Any]:
    log = evidence / (name + ".log")
    started = time.monotonic()
    process = None
    result = _step(name, "failed", argv=_safe_argv(argv), cwd=str(cwd), log=str(log), exit_code=None)
    try:
        with log.open("wb") as stream:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=(os.name != "nt"))
            result["pid"] = process.pid
            try:
                result["exit_code"] = process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                result["timed_out"] = True
                _stop_owned_process(process)
                result["exit_code"] = process.returncode
        text = log.read_text(encoding="utf-8", errors="replace")
        result["status"] = "passed" if result["exit_code"] == 0 and not result.get("timed_out") else "failed"
        skips = re.findall(r"(?im)^.*\[SKIP\].*$", text)
        if skips:
            result["skip_lines"] = skips
            if reject_skip and result["status"] == "passed":
                result["status"] = "partial"
                result["reason"] = "Required check reported skipped coverage"
    except (KeyboardInterrupt, SystemExit):
        if process is not None and process.poll() is None:
            _stop_owned_process(process)
        raise
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
        if process is not None and process.poll() is None:
            _stop_owned_process(process)
    result["elapsed_s"] = round(time.monotonic() - started, 6)
    if log.exists():
        result["log_sha256"] = _sha256(log)
    return result


def _environment(evidence: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name, relative in {"TEMP": "temporary", "TMP": "temporary", "TMPDIR": "temporary",
                           "DPF_CHAT_DB_DIR": "chat_data", "DPF_OUTPUTS": "outputs",
                           "DPF_WORKSPACE": "tasks", "DPF_TEST_ARTIFACT_ROOT": "test_artifacts"}.items():
        directory = evidence / relative
        directory.mkdir(parents=True, exist_ok=True)
        env[name] = str(directory)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    # The selected tree, rather than an inherited installation/PYTHONPATH, owns imports.
    env.pop("PYTHONPATH", None)
    return env


def _validate_native_result(path: Path, request: dict[str, Any], evidence: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("schema_version", "run_id", "source_manifest_sha256", "versions"):
        if result.get(key) != request[key]:
            raise ValueError(f"Native evidence identity mismatch: {key}")
    if result.get("status") == "skipped":
        if result.get("native_executed") is not False or not result.get("reason"):
            raise ValueError("Skipped native coverage requires an explicit reason and native_executed=false")
        return {"status": "skipped", "native_executed": False, "reason": str(result["reason"]),
                "result_sha256": _sha256(path)}
    if result.get("passed") is not True or result.get("native_executed") is not True:
        raise ValueError("Native runner did not prove an executed successful native check")
    engine = result.get("engine") or {}
    if str(engine.get("backend", "")).casefold() != "opendss com" or not engine.get("version"):
        raise ValueError("Native check did not identify OpenDSS COM and its version")
    cases = result.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Native check has no task evidence")
    evidence_files = []
    for case in cases:
        if (not isinstance(case, dict) or not case.get("task_id") or case.get("status") != "success"
                or case.get("verification_passed") is not True
                or not isinstance(case.get("registered_solve_count"), int)
                or isinstance(case.get("registered_solve_count"), bool)
                or case["registered_solve_count"] < 2):
            raise ValueError("Native task must succeed with independent verification and actual Solve records")
        files = case.get("evidence_files")
        if not isinstance(files, list) or not files:
            raise ValueError("Native task has no actual evidence files")
        for name in files:
            candidate = Path(name)
            candidate = (candidate if candidate.is_absolute() else evidence / candidate).resolve()
            if not candidate.is_relative_to(evidence.resolve()) or not candidate.is_file():
                raise ValueError("Native task evidence must exist inside this isolated gate directory")
            evidence_files.append({"path": str(candidate), "sha256": _sha256(candidate)})
    return {"engine": engine, "case_count": len(cases), "evidence_files": evidence_files,
            "result_sha256": _sha256(path)}


def run_gate(config: GateConfig) -> dict[str, Any]:
    """Execute the gate and return the same structured record written to disk."""
    evidence = config.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    if any(evidence.iterdir()):
        raise ValueError("Evidence directory must be new/empty; prior evidence is never overwritten")
    dev = config.dev_root.resolve()
    ui_checks = UI_CHECKS + tuple(name for name in DISCOVERED_UI_CHECKS if (dev / "tianshu_ui" / name).is_file())
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "run_id": uuid.uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(), "mode": config.mode,
        "dev_root": str(dev), "release_root": str(config.release_root.resolve()) if config.release_root else None,
        "scope": SOURCE_SCOPE, "steps": [], "passed": False, "delivered": False,
        "required_ui_checks": list(ui_checks),
        "standard_offline_suite": config.suite_module == DEFAULT_SUITE}
    steps = report["steps"]
    initial_inventory = None
    try:
        if config.mode not in {"preflight", "final"} or config.native not in {"off", "optional", "required"}:
            raise ValueError("Unsupported gate/native mode")
        if not math.isfinite(config.timeout_s) or config.timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        initial_inventory = source_inventory(dev)
        versions = read_versions(dev)
        fingerprint = inventory_fingerprint(initial_inventory)
        report.update(versions=versions, source_manifest_sha256=fingerprint, source_file_count=len(initial_inventory))
        _json_write(evidence / "development_manifest.json", initial_inventory)
        steps.append(_step("development_source", "passed", file_count=len(initial_inventory), fingerprint=fingerprint))
        env = _environment(evidence)
        if config.mode == "final" and config.suite_module != DEFAULT_SUITE:
            steps.append(_step("offline_suite_contract", "failed", reason="Final gate must run the standard dynamic run_all entry"))
        else:
            steps.append(_command("backend_offline", [config.python, "-B", "-X", "utf8", "-m", config.suite_module],
                                  dev / "dpf_agent", env, evidence, config.timeout_s, reject_skip=True))
        node = config.node or shutil.which("node")
        for script in ui_checks:
            name = Path(script).stem
            if not node:
                steps.append(_step(name, "failed", reason="Node.js is unavailable; required UI coverage was not run"))
            else:
                steps.append(_command(name, [node, str(dev / "tianshu_ui" / script)],
                                      dev / "tianshu_ui", env, evidence, config.timeout_s, reject_skip=True))

        request = {"schema_version": SCHEMA_VERSION, "run_id": report["run_id"],
                   "source_manifest_sha256": fingerprint, "versions": versions,
                   "dev_root": str(dev), "evidence_dir": str(evidence),
                   "native_result": str(evidence / "native_result.json"), "required_backend": "OpenDSS COM"}
        _json_write(evidence / "gate_request.json", request)
        native_required = config.mode == "final" or config.native == "required"
        if config.native == "off" or not config.native_command:
            steps.append(_step("native_com", "skipped", required=native_required,
                               reason="Native COM check explicitly disabled" if config.native == "off" else "No native runner configured",
                               native_executed=False))
        else:
            native_env = env.copy()
            native_env.update(DPF_GATE_REQUEST=str(evidence / "gate_request.json"), DPF_GATE_NATIVE_RESULT=request["native_result"])
            native_step = _command("native_com", list(config.native_command), dev / "dpf_agent", native_env, evidence, config.timeout_s)
            native_step["required"] = native_required
            native_step["native_executed"] = False
            if native_step["status"] == "passed":
                try:
                    native_step.update(_validate_native_result(Path(request["native_result"]), request, evidence))
                    native_step["native_executed"] = native_step["status"] == "passed"
                except Exception as exc:
                    native_step.update(status="failed", reason=f"Invalid native evidence: {type(exc).__name__}: {exc}")
            steps.append(native_step)

        if config.mode == "preflight":
            steps.append(_step("release_sync", "skipped", required=False,
                               reason="Development preflight does not certify a delivered release"))
        elif not config.release_root:
            steps.append(_step("release_sync", "failed", reason="Final gate requires --release-root"))
        else:
            release = config.release_root.resolve()
            if release == dev:
                raise ValueError("Development and release roots must be distinct")
            release_inventory = source_inventory(release)
            release_versions = read_versions(release)
            _json_write(evidence / "release_manifest.json", release_inventory)
            comparison = compare_inventories(initial_inventory, release_inventory)
            report["release_versions"] = release_versions
            version_match = versions == release_versions
            steps.append(_step("release_sync", "passed" if version_match and not any(comparison.values()) else "failed",
                               versions_equal=version_match, file_count=len(release_inventory), **comparison))
    except (KeyboardInterrupt, SystemExit) as exc:
        report["interrupted"] = True
        steps.append(_step("gate_interrupted", "failed", reason=f"{type(exc).__name__}: gate interrupted"))
    except Exception as exc:
        steps.append(_step("gate_setup_or_audit", "failed", reason=f"{type(exc).__name__}: {exc}"))
    finally:
        if initial_inventory is not None:
            try:
                after = source_inventory(dev)
                comparison = compare_inventories(initial_inventory, after)
                steps.append(_step("development_unchanged", "passed" if not any(comparison.values()) else "failed", **comparison))
            except Exception as exc:
                steps.append(_step("development_unchanged", "failed", reason=f"{type(exc).__name__}: {exc}"))
        required_names = {"development_source", "backend_offline", "native_com", "release_sync", "development_unchanged",
                          *[Path(script).stem for script in ui_checks]}
        missing = sorted(required_names - {step["name"] for step in steps})
        if missing:
            steps.append(_step("gate_incomplete", "failed", missing_steps=missing,
                               reason="Required checks did not all finish"))
        # Optional absence is visible; an attempted failure is never ignored.
        report["passed"] = bool(steps) and all(step["status"] == "passed" or
            (step["status"] == "skipped" and not step["required"]) for step in steps)
        report["delivered"] = report["passed"] and config.mode == "final"
        report["status"] = "delivered" if report["delivered"] else "preflight_passed" if report["passed"] else "failed"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _json_write(evidence / "gate_result.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("preflight", "final"), default="preflight")
    parser.add_argument("--dev-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--node")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--native", choices=("off", "optional", "required"), default="optional")
    parser.add_argument("--native-command-json", default="[]", help="JSON argv array; no shell interpolation. Runner reads DPF_GATE_REQUEST/DPF_GATE_NATIVE_RESULT.")
    parser.add_argument("--suite-module", default=DEFAULT_SUITE, help="Preflight-only override; final always requires dynamic run_all")
    parser.add_argument("--timeout-s", type=float, default=900)
    args = parser.parse_args(argv)
    try:
        command = json.loads(args.native_command_json)
        if not isinstance(command, list) or not all(isinstance(value, str) and value for value in command):
            raise ValueError("native-command-json must be an array of nonempty strings")
        result = run_gate(GateConfig(dev_root=args.dev_root, evidence_dir=args.evidence_dir, mode=args.mode,
                         release_root=args.release_root, node=args.node, python=args.python, native=args.native,
                         native_command=tuple(command), suite_module=args.suite_module, timeout_s=args.timeout_s))
    except Exception as exc:
        print(f"Delivery gate could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": result["status"], "passed": result["passed"], "delivered": result["delivered"],
                      "evidence": str(args.evidence_dir.resolve() / "gate_result.json")}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
