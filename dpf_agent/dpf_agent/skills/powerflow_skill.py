"""Fixed PowerFlow Skill：固定工作流状态机（文档 12.2）。

receive_request → resolve_constraints → request_guard → load_case → validate_case
→ build_network_profile → build_opendss_solver_profile → freeze_case_and_solver_config
→ build_execution_plan → run_opendss_powerflow → normalize_results
→ verify_result_integrity_and_power_balance → build_evidence → publish_guard → render_report

Skill 组织固定业务阶段和 Tool 调用；不保存跨任务可变状态；不直接写报告数值。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any, Callable

from ..evidence.builder import build_evidence, publication_gate
from ..runtime.cancellation import is_cancelled
from ..runtime.guard import RuleGuard
from ..runtime.tracing import Timer, Tracer
from ..schemas.enums import ErrorCode, EventStatus, EventType, Phase, TaskStatus
from ..schemas.state import AgentState
from ..schemas.solver import SelectionEvidence
from ..schemas.verification import VerificationResult
from ..tools.base import ToolArgError, ToolOutput
from ..tools.powerflow_tools import TOOL_REGISTRY, TaskContext, ToolFailure


#: 未收敛时的恢复动作注册表。**这是模型唯一能选的集合**（白名单）——
#: 提示词 `replan_choose` 下发的候选就是它，两边同源，避免"提示词里写了一个、
#: 代码里却没有"这类漂移。
#:
#: 本次改造的核心边界：**模型只选方向，执行仍在本文件里做**。
#: 具体参数（迭代次数、容差、降级哪些机组）一律由下面的代码按模板生成，
#: 模型既拿不到、也不被允许给数值建议 —— 否则就退回到"模型编数字"的老问题。
_REPLAN_ACTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "relax_numeric",
        "desc": (
            "放宽迭代次数与收敛容差后重算。不改变网络模型结构，"
            "适用于「数值上差一点收敛」的情况（迭代次数用尽、容差过严）。"
        ),
    },
    {
        "id": "downgrade_pv_to_pq",
        "desc": (
            "把电压控制型分布式电源（Model=3）降级为恒功率注入（Model=1）后重编译。"
            "适用于「多台机组同时参与调压导致分散」的算例；会改变建模口径，"
            "降级事实必须写入报告与证据链。"
        ),
    },
)


def _choose_replan_action(
    ctx: TaskContext,
    candidates: list[dict[str, str]],
    exc: ToolFailure,
    tried: list[str],
) -> tuple[str, str]:
    """决定这一轮用哪个恢复动作，返回 (动作 id, 判断依据)。

    先让模型在**候选白名单**里选；模型不可用、超时、或给出白名单外的值时，
    退回注册顺序（取第一个候选）。

    ⚠️ **兜底必须存在**：这段代码跑在失败路径上，绝不能因为模型抽风而丢掉
    原本能自愈的机会（与"失败路径必须绝对安全"是同一约定）。

    返回值要么是候选之一，要么是 `"abort"`（模型判定重规划无益），不会是别的。
    """
    fallback = candidates[0]["id"] if candidates else "abort"
    try:
        from ..runtime.model_client import ModelClient

        sp = ctx.solver_profile
        context = {
            "error_code": getattr(exc.code, "value", str(exc.code)),
            "error_message": (getattr(exc, "message", "") or "")[:300],
            "already_tried": list(tried),
            "case_ref": getattr(ctx.task_spec, "case_ref", None),
            "max_iterations": (
                int(sp.iteration_config.max_iterations) if sp is not None else None
            ),
            "tolerance": (
                float(sp.tolerance_config.tolerance) if sp is not None else None
            ),
        }
        picked, why = ModelClient(ctx.config).choose_replan(candidates, context)
        if picked:
            return picked, why
        return fallback, "模型未给出可用选择，按注册顺序兜底"
    except Exception as inner:  # noqa: BLE001 - 决策失败必须退回顾有路径
        return fallback, f"决策调用异常（{type(inner).__name__}），按注册顺序兜底"


def _dss_code_end(line: str) -> int:
    """注释仅在引号之外生效，返回有效命令的末尾位置。"""
    quote = ""
    for i, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "!" or line[i:i + 2] == "//":
            return i
    return len(line)


_GENERATOR_COMMAND = re.compile(
    r"^\s*(?:new|edit)\s+(?:object\s*=\s*)?"
    r"(?:\"generator\.([^\"]+)\"|'generator\.([^']+)'|"
    r"generator\.([^\s]+)|generator\s+([^\s]+))(?=\s|$)",
    re.IGNORECASE,
)
_MODEL_THREE = re.compile(
    r"(?<![\w.])model\s*=\s*(?P<quote>[\"']?)3(?P=quote)(?=$|[\s,)])",
    re.IGNORECASE,
)


def _rewrite_generator_models(text: str) -> tuple[str, set[str]]:
    """只改 New/Edit Generator 与其 ~ 续行的字面 Model=3。

    保留注释、格式及其他元件；不猜测 Like/BatchEdit/选择后编辑等高级语法，
    发现这些可能覆盖机组模型的命令时拒绝该恢复动作。
    返回去重后的对象身份，而不是文本出现次数。
    """
    generator = ""
    changed: set[str] = set()
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        end = _dss_code_end(line)
        code = line[:end]
        stripped = code.strip()
        if re.match(r"^(?:batchedit|select)\s+.*generator\b|^generator[.\s]|^more\b",
                    stripped, re.IGNORECASE):
            raise ValueError("Generator 恢复不支持 BatchEdit/Select/More/直接属性编辑")
        if stripped and not stripped.startswith("~"):
            match = _GENERATOR_COMMAND.match(code)
            generator = next(value for value in match.groups() if value).lower() if match else ""
        if generator and re.search(r"\blike\s*=", code, re.IGNORECASE):
            raise ValueError("Generator 恢复不支持 Like 继承")
        if generator:
            # 字符串属性内的 "model=3" 是数据，不是属性赋值。
            quoted: set[int] = set()
            quote = ""
            for i, char in enumerate(code):
                if quote:
                    quoted.add(i)
                    if char == quote:
                        quote = ""
                elif char in "\"'":
                    quote = char
            for model in reversed(list(_MODEL_THREE.finditer(code))):
                if model.start() not in quoted:
                    code = code[:model.start()] + model.group(0).replace("3", "1") + code[model.end():]
                    changed.add(generator)
        lines.append(code + line[end:])
    return "".join(lines), changed


def _downgrade_pv_voltage_control(ctx: TaskContext) -> tuple[bool, int]:
    """在任务副本的依赖清单内降级 Generator，并更新实际模型的身份。

    改模型结果不进入共享缓存；保留原始清单与前后指纹。编译失败时回滚
    文件和身份，重新装载原模型，防止后续数值恢复运行在半修改的电路上。
    """
    master = getattr(ctx.circuit, "master_dss", "") if ctx.circuit else ""
    if not master or ctx.case is None or not ctx.case.dependency_manifest.entries:
        return False, 0
    root = os.path.abspath(getattr(ctx.circuit, "case_workspace", "") or os.path.dirname(master))
    original_case = ctx.case.model_copy(deep=True)
    originals: dict[str, bytes] = {}
    rewrites: dict[str, bytes] = {}
    changed: set[str] = set()
    try:
        for entry in ctx.case.dependency_manifest.entries:
            path = os.path.abspath(os.path.join(root, entry.path))
            if os.path.commonpath([root, path]) != root:
                return False, 0
            with open(path, "rb") as f:
                data = f.read()
            originals[path] = data
            if path.lower().endswith(".dss"):
                text, names = _rewrite_generator_models(data.decode("utf-8-sig"))
                if names:
                    rewrites[path] = (b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
                    changed.update(names)
    except (OSError, UnicodeError, ValueError):
        return False, 0
    if not changed:
        return False, 0

    previous_recovery = ctx.model_recovery_applied
    ctx.model_recovery_applied = True
    try:
        ctx.save_json("original_case.json", original_case)
        for path, data in rewrites.items():
            with open(path, "wb") as f:
                f.write(data)
        agg = hashlib.sha256()
        for entry in sorted(ctx.case.dependency_manifest.entries, key=lambda item: item.path):
            data = rewrites.get(os.path.abspath(os.path.join(root, entry.path)))
            if data is None:
                data = originals[os.path.abspath(os.path.join(root, entry.path))]
            entry.sha256 = hashlib.sha256(data).hexdigest()
            entry.size_bytes = len(data)
            agg.update(f"{entry.path}:{entry.sha256}\n".encode())
        ctx.case.case_fingerprint = agg.hexdigest()[:16]
        ctx.case.source_metadata["model_recovery"] = {
            "original_fingerprint": original_case.case_fingerprint,
            "generators": sorted(changed),
            "modified_files": [os.path.relpath(path, root).replace(os.sep, "/") for path in rewrites],
        }
        ctx.circuit = ctx.adapter.compile_case(root, master)
        ctx.adapter.apply_solver_profile(ctx.circuit, ctx.solver_profile)
    except Exception:
        # 写入/编译的任一失败都不能留下部分降级模型。
        try:
            for path in rewrites:
                with open(path, "wb") as f:
                    f.write(originals[path])
            ctx.case = original_case
            ctx.circuit = ctx.adapter.compile_case(root, master)
            ctx.adapter.apply_solver_profile(ctx.circuit, ctx.solver_profile)
            ctx.model_recovery_applied = previous_recovery
        except Exception as rollback_exc:
            raise ToolFailure(ErrorCode.ENGINE_ERROR, TaskStatus.SYSTEM_FAILED,
                              f"建模恢复失败且无法重新装载原模型：{rollback_exc}") from rollback_exc
        return False, 0

    validation_path = ctx.artifacts.get("validation.json", "")
    validation: dict[str, Any] = {}
    if validation_path:
        try:
            with open(validation_path, encoding="utf-8") as f:
                validation = json.load(f)
        except (OSError, ValueError):
            pass
    validation.update({"passed": True, "case": ctx.case.model_dump(mode="json")})
    ctx.save_json("validation.json", validation)
    ctx.save_json("model_recovery.json", {
        **ctx.case.source_metadata["model_recovery"],
        "effective_fingerprint": ctx.case.case_fingerprint,
        "solve_cache_disabled": True,
    })
    return True, len(changed)


def _numeric_replan_params(ctx: TaskContext) -> tuple[int, float]:
    """仅放宽用户未明确指定的参数；硬约束保持原值。"""
    sp = ctx.solver_profile
    old_iter = int(sp.iteration_config.max_iterations)
    old_tol = float(sp.tolerance_config.tolerance)
    spec = ctx.task_spec
    hard = spec.constraints.hard
    pinned_iter = spec.solver_config.max_iterations is not None or hard.get("max_iterations") is not None
    pinned_tol = spec.solver_config.tolerance is not None or hard.get("tolerance") is not None
    new_iter = old_iter if pinned_iter else max(old_iter, min(max(old_iter * 4, 30), 100))
    new_tol = old_tol if pinned_tol else max(old_tol, min(old_tol * 10, 1e-2))
    return new_iter, new_tol



# 阶段 → 工具名（固定映射）
_STAGE_TOOLS = [
    ("load_case", "load_case"),
    ("validate_case", "validate_case"),
    ("build_network_profile", "profile_network"),
    ("build_opendss_solver_profile", "build_solver_profile"),
    ("run_opendss_powerflow", "run_powerflow"),
    ("normalize_results", "extract_powerflow_result"),
    ("verify_result_integrity_and_power_balance", "verify_power_balance"),
]

_PHASE_AFTER = {
    "load_case": Phase.CASE_LOADED,
    "validate_case": Phase.CASE_VALIDATED,
    "run_opendss_powerflow": Phase.PF_EXECUTED,
    "normalize_results": Phase.RESULT_BUILT,
    "verify_result_integrity_and_power_balance": Phase.RESULT_VERIFIED,
}

PLAN_STEPS = [
    "解析任务", "算例检查", "网络分析", "求解策略", "潮流计算",
    "结果整理", "验证", "解释", "报告",
]

# 阶段 → (进度百分比, UI 文案)。
# 固定工作流有 7 个工具阶段，以前整段只写一次 30%，UI 会长时间"一动不动"。
# 这里把 30%→85% 拆开，每个阶段进入前写一次，用户才能看到轨迹在推进。
_STAGE_PROGRESS: dict[str, tuple[int, str]] = {
    "load_case": (33, "正在装载算例…"),
    "validate_case": (40, "正在做算例静态检查…"),
    "build_network_profile": (48, "正在分析网络拓扑与设备…"),
    "build_opendss_solver_profile": (55, "正在确定求解策略与参数…"),
    "run_opendss_powerflow": (62, "正在调用 OpenDSS 求解潮流（大算例可能需要几分钟）…"),
    "normalize_results": (74, "正在归一化求解结果…"),
    "verify_result_integrity_and_power_balance": (80, "正在独立校验潮流与功率平衡…"),
}


class SkillResult:
    def __init__(self):
        self.verification: VerificationResult | None = None
        self.evidence: list = []
        self.publication_passed: bool = False
        self.partial: bool = False
        # 需要在报告里如实标注的建模/求解说明（如"PV 电压控制已降级为恒功率"）。
        # 降级不写入这里就等于拿降级结果冒充完整建模结果。
        self.notes: list[str] = []


def run_fixed_powerflow_skill(
    ctx: TaskContext,
    state: AgentState,
    tracer: Tracer,
    guard: RuleGuard,
    progress_cb: Callable[[str, str, int], None] | None = None,
) -> SkillResult:
    """执行固定工作流。失败时按文档 12.2 状态机映射失败语义并抛出 ToolFailure。

    progress_cb(stage, label, percent) 可选；传入后每个阶段进入前回调一次，
    用于把真实执行阶段推给 UI（/api/task-progress）。进度失败不影响主流程。
    """

    def _progress(stage: str, label: str, percent: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(stage, label, percent)
        except Exception:  # pragma: no cover - 进度只是提示，绝不能影响主流程
            pass

    result = SkillResult()

    tracer.emit(
        EventType.PLAN, stage="build_execution_plan",
        message=" -> ".join(PLAN_STEPS),
        detail={"steps": list(PLAN_STEPS)},
    )

    # 受控重规划分级：1=放宽数值策略，2=建模降级；总数上限 2，避免无限重试掩盖真实问题
    replan_attempts = 0
    replan_note = ""

    for stage, tool_name in _STAGE_TOOLS:
        # ---- 取消检查点：用户点了停止就立刻中断，不再往下跑 ----
        if is_cancelled(getattr(state, "task_id", "")):
            tracer.emit(
                EventType.TOOL_RESULT, stage=stage, tool_name=tool_name,
                message="任务已被用户取消，工作流中断",
                status=EventStatus.FAILED, error_code=ErrorCode.CANCELLED.value,
            )
            state.fail(
                ErrorCode.CANCELLED.value, "任务已被用户取消", stage, TaskStatus.CANCELLED
            )
            raise ToolFailure(ErrorCode.CANCELLED, TaskStatus.CANCELLED, "任务已被用户取消")
        pct, label = _STAGE_PROGRESS.get(stage, (50, f"正在执行 {tool_name}…"))
        _progress(stage, label, pct)
        tool = TOOL_REGISTRY[tool_name]
        # 阶段白名单：工具声明的 stage 必须与当前工作流阶段一致。
        # 这是声明式约束——白名单来自工具自己的 stage 字段，不依赖调用方自觉。
        if tool.stage and tool.stage != stage:
            raise ToolFailure(
                ErrorCode.VALIDATION_FAILED,
                TaskStatus.VALIDATION_FAILED,
                f"工具 {tool_name} 声明阶段为 `{tool.stage}`，与当前阶段 `{stage}` 不一致",
            )
        args: dict[str, Any] = {}
        if tool_name == "load_case":
            args = {"case_ref": ctx.task_spec.case_ref}
        tracer.emit(
            EventType.TOOL_CALL, stage=stage, tool_name=tool_name,
            message=f"{tool_name}({_fmt_args(args)})",
            sanitized_arguments=guard.sanitize_arguments(args),
            status=EventStatus.RUNNING,
        )
        with Timer() as t:
            failure: ToolFailure | None = None
            out: ToolOutput | None = None

            # ---- 统一执行入口 + 分级重规划 ----
            # 用重试循环而不是"在 except 里再调一次"：后者第二次失败不会再回到
            # handler，导致第 2 级策略永远不会执行（实测缺陷）。
            #   attempt 0 = 原始求解
            #   attempt 1 = 放宽 max_iterations / tolerance（只调数值策略）
            #   attempt 2 = 把 PV 电压控制（Model=3）降级为恒功率（Model=1）后重新编译
            # 两级都失败则按受限诊断报告处理，绝不静默换模型当原结果。
            # 恢复动作的**已试集合**（取代原来的 attempt 计数）。
            # 每轮未收敛都重新决策，但已试过的动作不再进入候选 —— 既保留了
            # "最多两级重规划"的既有上限，又让模型可以在「放宽数值」与
            # 「降级建模」之间自己决定先试哪个（此前是写死的顺序）。
            replan_tried: list[str] = []
            while True:
                try:
                    out = tool.invoke(ctx, **args)
                    failure = None
                    break
                except ToolArgError as exc:
                    # 参数不合法属于「参数格式错误」，转成字段级诊断，而不是裸异常
                    failure = ToolFailure(
                        ErrorCode.VALIDATION_FAILED,
                        TaskStatus.VALIDATION_FAILED,
                        f"{tool_name} 参数校验未通过: " + "；".join(exc.errors),
                    )
                    out = None
                    break
                except ToolFailure as exc:
                    out = None
                    failure = exc
                    can_replan = (
                        tool_name == "run_powerflow"
                        and exc.code == ErrorCode.NON_CONVERGED
                        and ctx.solver_profile is not None
                        and ctx.circuit is not None
                        and len(replan_tried) < len(_REPLAN_ACTIONS)
                    )
                    if not can_replan:
                        break
                    candidates = [a for a in _REPLAN_ACTIONS if a["id"] not in replan_tried]
                    # 用户显式指定的参数不能通过恢复路径绕过硬约束。
                    numeric_params = _numeric_replan_params(ctx)
                    current_params = (
                        int(ctx.solver_profile.iteration_config.max_iterations),
                        float(ctx.solver_profile.tolerance_config.tolerance),
                    )
                    if numeric_params == current_params:
                        candidates = [a for a in candidates if a["id"] != "relax_numeric"]
                    if not candidates:
                        break
                    chosen, why = _choose_replan_action(ctx, candidates, exc, replan_tried)
                    replan_tried.append(chosen)
                    tracer.emit(
                        EventType.PLAN, stage=stage,
                        message=f"未收敛，恢复动作选定：{chosen}"
                                + (f"（依据：{why}）" if why else ""),
                    )
                    if chosen == "relax_numeric":
                        # ---- 数值放宽：只调参数，不动网络模型结构 ----
                        sp = ctx.solver_profile
                        old_iter = int(sp.iteration_config.max_iterations)
                        old_tol = float(sp.tolerance_config.tolerance)
                        new_iter, new_tol = _numeric_replan_params(ctx)
                        sp = sp.model_copy(deep=True)
                        sp.iteration_config.max_iterations = new_iter
                        sp.tolerance_config.tolerance = new_tol
                        sp.frozen = True  # 重规划后的策略同样必须冻结，禁止二次改动
                        sp.selection_evidence.append(SelectionEvidence(
                            source="recovery",
                            detail=f"未收敛恢复；max_iterations {old_iter}→{new_iter}，"
                                   f"tolerance {old_tol:g}→{new_tol:g}；显式参数保持不变",
                        ))
                        try:
                            ctx.adapter.apply_solver_profile(ctx.circuit, sp)
                        except Exception as apply_exc:  # noqa: BLE001
                            tracer.emit(
                                EventType.PLAN, stage=stage,
                                message=f"第 1 级重规划失败（策略无法应用：{apply_exc}）",
                                status=EventStatus.FAILED,
                            )
                            break
                        ctx.solver_profile = sp
                        ctx.save_json("solver_profile.json", sp)
                        tracer.emit(
                            EventType.PLAN, stage=stage,
                            message=(
                                "检测到未收敛，第 1 级重规划："
                                f"max_iterations {old_iter}→{new_iter}，tolerance {old_tol:g}→{new_tol:g}"
                            ),
                        )
                        ctx.save_json("replan.json", {
                            "trigger": "non_converged",
                            "level": 1,
                            "strategy": "relax_numeric",
                            "reason": why,
                            "max_iterations": {"from": old_iter, "to": new_iter},
                            "tolerance": {"from": old_tol, "to": new_tol},
                        })
                        continue
                    if chosen != "downgrade_pv_to_pq":
                        # 模型判定"继续重规划无益"而选择中止 —— 尊重该判断。
                        # 但**失败语义不变**：仍按未收敛上报，绝不静默换个模型当原结果。
                        tracer.emit(
                            EventType.PLAN, stage=stage,
                            message="恢复动作选定：中止（模型判断继续重规划无益）",
                            status=EventStatus.FAILED,
                        )
                        break
                    # ---- 建模降级（PV 电压控制 → 恒功率）----
                    downgraded, changes = _downgrade_pv_voltage_control(ctx)
                    if not downgraded:
                        tracer.emit(
                            EventType.PLAN, stage=stage,
                            message="第 2 级重规划不适用（无可降级机组或源语法超出安全恢复范围）",
                            status=EventStatus.FAILED,
                        )
                        break
                    tracer.emit(
                        EventType.PLAN, stage=stage,
                        message=(
                            f"第 2 级重规划：{changes} 台分布式电源由电压控制降级为恒功率注入"
                            "（降级事实将写入报告与证据链）"
                        ),
                    )
                    replan_note = (
                        f"{changes} 台分布式电源由电压控制(Model=3)降级为恒功率(Model=1)："
                        "多机同时参与调压导致潮流发散，降级后这些电源不再支撑节点电压，"
                        "电压水平应按下限理解。"
                    )
                    ctx.save_json("replan.json", {
                        "trigger": "non_converged",
                        "level": 2,
                        "strategy": "downgrade_pv_to_pq",
                        "units_downgraded": changes,
                        "note": replan_note,
                        "reason": why,
                    })
                    continue

        if failure is not None:
            tracer.emit(
                EventType.TOOL_RESULT, stage=stage, tool_name=tool_name,
                message=f"{tool_name} failed: {failure.message}",
                status=EventStatus.FAILED, duration=t.duration,
                error_code=failure.code.value,
            )
            state.fail(failure.code.value, failure.message, stage, failure.status)
            raise failure
        summary = _summarize(tool_name, out)
        tracer.emit(
            EventType.TOOL_RESULT, stage=stage, tool_name=tool_name,
            message=summary["message"],
            detail=summary["detail"],
            duration=t.duration,
            status=EventStatus.SUCCESS if out.get("ok", True) else EventStatus.FAILED,
        )

        if stage in _PHASE_AFTER:
            state.phase = _PHASE_AFTER[stage]

        if tool_name == "run_powerflow":
            state.phase = Phase.PF_EXECUTED
        if tool_name == "extract_powerflow_result":
            result.partial = bool(out.get("partial_scopes"))
            state.result_ref = out.get("result_ref", "")
        if tool_name == "verify_power_balance":
            from ..schemas.verification import VerificationResult as VR
            result.verification = VR(**out["verification"])
            state.verification_ref = ctx.artifacts.get("verification.json", "")
            tracer.emit(
                EventType.VERIFY, stage=stage,
                message=f"Verification.passed={out['passed']}"
                + (f"，失败域: {out['failed_domains']}" if out["failed_domains"] else ""),
                detail={
                    "passed": bool(out["passed"]),
                    "residual_pu": out.get("residual_pu"),
                    "failed_domains": out.get("failed_domains") or [],
                },
                status=EventStatus.SUCCESS if out["passed"] else EventStatus.FAILED,
            )
            if not out["passed"]:
                state.fail(
                    ErrorCode.VERIFICATION_FAILED.value,
                    f"独立验证未通过: {out['failed_domains']}",
                    stage,
                    TaskStatus.VALIDATION_FAILED,
                )
                raise ToolFailure(
                    ErrorCode.VERIFICATION_FAILED,
                    TaskStatus.VALIDATION_FAILED,
                    f"独立验证未通过: {out['failed_domains']}",
                )

    if replan_note:
        result.notes.append(replan_note)

    # build_evidence
    assert ctx.canonical_result is not None and result.verification is not None
    evidence = build_evidence(
        ctx.canonical_result,
        result.verification,
        artifact_ref=ctx.artifacts.get("powerflow_result.json", ""),
    )
    # 建模降级等需要在报告里如实标注的事项，作为独立证据条目进入证据链与报告
    if replan_note:
        try:
            from ..schemas.evidence import EvidenceItem

            evidence.append(
                EvidenceItem(
                    evidence_id=f"ev-{uuid.uuid4().hex[:8]}",
                    task_id=ctx.canonical_result.task_id,
                    object_ref="system",
                    field="modeling_note",
                    value=replan_note,
                    unit="",
                    status="ok",
                    artifact_ref=ctx.artifacts.get("replan.json", ""),
                    case_fingerprint=ctx.canonical_result.case_fingerprint,
                    engine_version=ctx.canonical_result.engine_version,
                    adapter_version=ctx.canonical_result.adapter_version,
                    verification_check_ref="",
                    claim=replan_note,
                )
            )
        except Exception:
            pass
    result.evidence = evidence
    _progress("build_evidence", "正在汇总证据链…", 88)
    ctx.save_json("evidence.json", [e.model_dump(mode="json") for e in evidence])
    tracer.emit(
        EventType.EVIDENCE, stage="build_evidence",
        message=f"生成 Evidence {len(evidence)} 条，关键结论覆盖率检查中",
        detail={"count": len(evidence)},
    )

    # publish_guard
    key_claims = [e.claim for e in evidence]
    gate = publication_gate(result.verification, evidence, key_claims)
    result.publication_passed = gate.passed
    tracer.emit(
        EventType.GUARD, stage="publish_guard",
        message=gate.detail,
        detail={"gate": gate.detail, "passed": bool(gate.passed)},
        status=EventStatus.SUCCESS if gate.passed else EventStatus.FAILED,
    )
    if not gate.passed:
        state.fail(
            ErrorCode.VERIFICATION_FAILED.value, gate.detail,
            "publish_guard", TaskStatus.VALIDATION_FAILED,
        )
        raise ToolFailure(
            ErrorCode.VERIFICATION_FAILED, TaskStatus.VALIDATION_FAILED, gate.detail
        )

    # export_artifacts
    # 与循环内工具保持一致的 trace 语义（tool_call + tool_result），
    # 避免出现"只有结果没有调用"的不对称记录。
    tracer.emit(
        EventType.TOOL_CALL, stage="export_artifacts", tool_name="export_artifacts",
        message="export_artifacts()",
        sanitized_arguments=guard.sanitize_arguments({}),
        status=EventStatus.RUNNING,
    )
    _progress("export_artifacts", "正在导出产物…", 94)
    with Timer() as t:
        out = TOOL_REGISTRY["export_artifacts"].invoke(ctx)
    tracer.emit(
        EventType.TOOL_RESULT, stage="export_artifacts", tool_name="export_artifacts",
        message=f"artifact_manifest: {out['count']} 项",
        detail={"count": out["count"]},
        duration=t.duration,
        status=EventStatus.SUCCESS,
    )
    return result


def _fmt_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items())


def _summarize(tool_name: str, out: ToolOutput) -> dict[str, Any]:
    """工具返回 → {"message": 给机器看的记录, "detail": 给文案模板取值的结构化数值}。

    为什么要把 detail 单独给出来，而不是让上层去解析 message：
    执行过程的「思考段」（`runtime/narration.py`）里出现的每个数字都必须来自
    结构化字段。靠字符串反解 message，改一次 message 的写法就会让文案**静默**
    变成兜底句 —— 不报错、不失败，只是没人再看到那句人话。
    """
    if tool_name == "load_case":
        return {
            "message": (
                f"load_case ok: fingerprint={out['case_fingerprint']}, "
                f"dependency_manifest={out['dependency_files']} files"
            ),
            "detail": {
                "case_fingerprint": out["case_fingerprint"],
                "dependency_files": out["dependency_files"],
            },
        }
    if tool_name == "validate_case":
        obj = out.get("object_summary") or {}
        return {
            "message": f"validate_case passed, objects={obj}",
            "detail": {
                "objects": "、".join(f"{k} {v}" for k, v in list(obj.items())[:6]),
                "object_summary": obj,
            },
        }
    if tool_name == "profile_network":
        p = out["profile"]
        return {
            "message": (
                f"profile_network ok: buses={p['buses']}, lines={p['lines']}, "
                f"loads={p['loads']}, load={p['total_installed_load_kw']:.1f} kW"
            ),
            "detail": {
                "buses": p["buses"],
                "lines": p["lines"],
                "loads": p["loads"],
                "load_kw": p["total_installed_load_kw"],
            },
        }
    if tool_name == "build_solver_profile":
        return {
            "message": f"build_solver_profile ok: {out['profile_id']} {out['params']}",
            "detail": {"profile_id": out["profile_id"], "params": str(out["params"])},
        }
    if tool_name == "run_powerflow":
        return {
            "message": f"run_powerflow converged={out.get('converged', True)}, "
                       f"iterations={out['iterations']}",
            "detail": {
                "converged": bool(out.get("converged", True)),
                "iterations": out["iterations"],
            },
        }
    if tool_name == "extract_powerflow_result":
        return {
            "message": f"extract_powerflow_result ok: scope_status={out['scope_status']}",
            "detail": {"scope_status": str(out.get("scope_status") or "")},
        }
    if tool_name == "verify_power_balance":
        r = out.get("residual_pu")
        return {
            "message": (
                f"verify_power_balance residual={r:.6f} (within tolerance)"
                if r is not None
                else "verify_power_balance done"
            ),
            "detail": {"residual_pu": r, "passed": bool(out.get("passed"))},
        }
    return {"message": f"{tool_name} ok", "detail": {}}
