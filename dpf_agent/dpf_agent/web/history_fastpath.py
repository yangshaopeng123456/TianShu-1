"""精确历史指标问句：核验成功任务快照后，直接复用已有确定性取数。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from ..config import AgentConfig
from ..schemas.result import CanonicalDistributionPowerFlowResult
from . import history_qa


_TASK_ID = re.compile(r"pf-\d{8}-\d{6}-[0-9a-f]{6}")
_ARTIFACTS = (
    "state.json", "task_spec.json", "validation.json", "solver_profile.json",
    "powerflow_result.json", "verification.json", "network_profile.json",
)
_CHECKS = {
    "input_identity", "solver_state", "completeness", "numeric_validity",
    "physical_sanity", "power_balance", "input_fidelity", "reproducibility",
}
_METRICS = {
    "最低电压是多少": history_qa.METRIC_MIN_VOLTAGE,
    "最低电压多少": history_qa.METRIC_MIN_VOLTAGE,
    "最高电压是多少": history_qa.METRIC_MAX_VOLTAGE,
    "最高电压多少": history_qa.METRIC_MAX_VOLTAGE,
    "总网损是多少": history_qa.METRIC_LOSSES,
    "网损是多少": history_qa.METRIC_LOSSES,
    "总负荷是多少": history_qa.METRIC_LOAD_TOTAL,
    "总负荷多少": history_qa.METRIC_LOAD_TOTAL,
}


@dataclass(frozen=True)
class HistoryAnswer:
    text: str
    task_id: str
    metric: str
    report_url: str | None


def _metric(content: str) -> str | None:
    text = content.strip()
    if text.endswith(("?", "？")):
        text = text[:-1]
    # 只允许明确指向最近结果的两个前缀，不能删除任意计算/参数/对象词。
    for prefix in ("上次的", "刚才的"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return _METRICS.get(text)


def _finite(data: Any) -> bool:
    if isinstance(data, float):
        return math.isfinite(data)
    if isinstance(data, dict):
        return all(_finite(value) for value in data.values())
    if isinstance(data, list):
        return all(_finite(value) for value in data)
    return True


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _same_path(reference: Any, expected: Path) -> bool:
    return isinstance(reference, str) and bool(reference) and Path(reference).resolve() == expected.resolve()


def _snapshot(task_id: str, case_ref: str, config: AgentConfig) -> tuple[dict[str, dict], dict[Path, bytes], Path]:
    """只读同 task 的工作区和发布副本；任何不一致都拒绝快路由。"""
    outputs_root = Path(config.outputs_root).resolve()
    workspace_root = Path(config.workspace_root).resolve()
    output = outputs_root / task_id
    work = workspace_root / task_id
    if not _inside(output, outputs_root) or not _inside(work, workspace_root):
        raise ValueError("任务目录越界")
    blobs: dict[Path, bytes] = {}
    arts: dict[str, dict] = {}
    for name in _ARTIFACTS:
        published, authoritative = output / name, work / name
        if not _inside(published, output) or not _inside(authoritative, work):
            raise ValueError("产物越界")
        data = published.read_bytes()
        original = authoritative.read_bytes()
        if data != original:
            raise ValueError("任务产物副本不同")
        parsed = json.loads(data)
        if not isinstance(parsed, dict) or not _finite(parsed):
            raise ValueError("产物无效")
        blobs[published], blobs[authoritative] = data, original
        arts[name] = parsed

    state, spec = arts["state.json"], arts["task_spec.json"]
    result, verification = arts["powerflow_result.json"], arts["verification.json"]
    validation, solver = arts["validation.json"], arts["solver_profile.json"]
    case = validation.get("case") or {}
    CanonicalDistributionPowerFlowResult.model_validate(result, strict=True)
    if arts["network_profile.json"].get("case_id") != case.get("case_id"):
        raise ValueError("网络画像算例身份不一致")
    if not all(item.get("task_id") == task_id for item in (state, spec, result, verification)):
        raise ValueError("任务身份不一致")
    spec_ref = spec.get("case_ref")
    same_case_ref = spec_ref == case_ref
    if not same_case_ref and case.get("source_type") == "builtin" and isinstance(spec_ref, str):
        # A model may return IEEE33 while the chat entry supplied ieee33. Only
        # registered builtin literals backed by builtin provenance may differ
        # in case; user paths and custom names retain exact identity checks.
        from .chat_api import _is_known_builtin_case

        same_case_ref = (_is_known_builtin_case(case_ref.lower())
                         and spec_ref.lower() == case_ref.lower())
    if (state.get("status") != "success" or state.get("phase") != "RESULT_VERIFIED"
            or state.get("_persisted_stage") != "final" or state.get("error") is not None
            or spec.get("frozen") is not True or not same_case_ref
            or validation.get("passed") is not True or verification.get("passed") is not True):
        raise ValueError("不是已验证成功快照")
    for field, name in (("task_spec_ref", "task_spec.json"), ("result_ref", "powerflow_result.json"),
                        ("verification_ref", "verification.json")):
        if not _same_path(state.get(field), work / name):
            raise ValueError("权威产物引用不一致")
    # 历史成功 state 未填写这两项；若已经填写，不能指向其他任务。
    for field, name in (("case_ref", "validation.json"), ("solver_profile_ref", "solver_profile.json")):
        if state.get(field) and not _same_path(state[field], work / name):
            raise ValueError("可选权威引用不一致")
    if not _same_path(case.get("canonical_workspace_ref"), work) or not _same_path(case.get("validation_ref"), work / "validation.json"):
        raise ValueError("算例快照引用不一致")
    fingerprint = case.get("case_fingerprint")
    profile_id = solver.get("profile_id")
    if (not fingerprint or result.get("case_fingerprint") != fingerprint or not profile_id
            or result.get("solver_profile_ref") != profile_id or solver.get("frozen") is not True):
        raise ValueError("算例/求解策略身份不一致")
    status = result.get("solver_status") or {}
    if status.get("converged") is not True or status.get("error_flag") is not False:
        raise ValueError("结果未有效收敛")
    checks = verification.get("checks")
    if not isinstance(checks, list) or not checks or any(check.get("passed") is not True for check in checks):
        raise ValueError("校验检查不完整")
    if not _CHECKS.issubset({check.get("check_id") for check in checks}):
        raise ValueError("缺少必要校验")
    versions = solver.get("version_set") or {}
    state_versions = state.get("version_set") or {}
    for field, key in (("engine_version", "engine_version"), ("adapter_version", "adapter_version")):
        if not result.get(field) or result[field] != versions.get(key):
            raise ValueError("结果版本不一致")
    if (state_versions.get("adapter") != result["adapter_version"]
            or not state_versions.get("defaults") or state_versions["defaults"] != versions.get("defaults_version")):
        raise ValueError("权威版本不一致")
    if (result.get("result_status_by_scope") or {}).get("system") != "ok":
        raise ValueError("系统结果域不可用")

    # 验证全部输入依赖内容，不能仅凭聊天里的一条 success 信任结果。
    manifest = case.get("dependency_manifest") or {}
    entries = manifest.get("entries")
    if (not isinstance(entries, list) or not entries or not case.get("master_dss_ref")
            or manifest.get("master_dss") != case["master_dss_ref"]):
        raise ValueError("输入清单缺失")
    input_root = work / "input"
    if not _inside(input_root, work):
        raise ValueError("输入目录越界")
    digest = hashlib.sha256()
    paths: set[Path] = set()
    names: set[str] = set()
    for entry in sorted(entries, key=lambda item: item["path"]):
        name = entry["path"]
        relative = Path(name)
        path = input_root / relative
        if relative.is_absolute() or not _inside(path, input_root) or path.resolve() in paths:
            raise ValueError("输入依赖越界/重复")
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha != entry.get("sha256") or len(data) != entry.get("size_bytes"):
            raise ValueError("输入依赖已经改变")
        digest.update(f"{name}:{sha}\n".encode())
        blobs[path] = data
        paths.add(path.resolve())
        names.add(name)
    if case["master_dss_ref"] not in names or digest.hexdigest()[:16] != fingerprint:
        raise ValueError("输入内容指纹不一致")
    return arts, blobs, output


def answer_verified_history(
    content: str, ctx: dict[str, Any], attachments: list | tuple = (),
) -> HistoryAnswer | None:
    """仅精确问句且完整已验证历史快照可答；否则交回原意图/计算链。"""
    metric = _metric(content)
    task_id = ctx.get("last_task_id")
    case_ref = ctx.get("last_task_case_ref")
    if (attachments or not metric or not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id)
            or ctx.get("last_task_status") != "success" or ctx.get("last_success_task_id") != task_id
            or not isinstance(case_ref, str) or not case_ref or ctx.get("last_success_case_ref") != case_ref):
        return None
    try:
        config = AgentConfig()
        arts, blobs, output = _snapshot(task_id, case_ref, config)
        section, field = {
            history_qa.METRIC_MIN_VOLTAGE: ("system_summary", "min_voltage_pu"),
            history_qa.METRIC_MAX_VOLTAGE: ("system_summary", "max_voltage_pu"),
            history_qa.METRIC_LOSSES: ("loss_summary", "total_loss_kw"),
            history_qa.METRIC_LOAD_TOTAL: ("system_summary", "total_load_kw"),
        }[metric]
        value = (arts["powerflow_result.json"].get(section) or {}).get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        text = history_qa.answer_by_metric(metric, content, ctx)
        if not text or Path(AgentConfig().outputs_root).resolve() != Path(config.outputs_root).resolve():
            return None
        # history_qa 复用现有公开入口读取产物；若取数期间有变更，拒绝快答。
        if any(path.read_bytes() != data for path, data in blobs.items()):
            return None
        report_url = None
        for name in ("report.html", "report.md"):
            path = output / name
            if path.exists():
                if not _inside(path, output) or not path.is_file():
                    return None
                report_url = f"/api/tasks/{task_id}/report"
                break
        reply = f"引用已验证任务 **{task_id}** 的既有结果（本次不重新求解）。\n\n{text}"
        return HistoryAnswer(reply, task_id, metric, report_url)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return None
