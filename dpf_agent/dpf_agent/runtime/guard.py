"""Rule Guard：三类防护门与约束优先级（文档 13.2 / 13.4）。

- 请求门：TaskSpec Schema、能力声明、DSS 路径、约束冲突、保真度。
- 执行门：Tool 白名单、参数 schema、引擎健康、只读边界、版本冻结、敏感信息。
- 发布门：见 verification/publication.py（Verification.passed、Evidence 100%、
  模型不得新增专业数值、not_assessed 不得显示为 0/正常/安全）。
"""
from __future__ import annotations

import re
from typing import Any

from ..schemas.enums import CAPABILITY_STATUS, Capability, ErrorCode
from ..schemas.task_spec import PowerFlowTaskSpec


class GuardRejection(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# V1.3 已注册工具白名单（执行门）
REGISTERED_TOOLS = {
    "load_case",
    "validate_case",
    "profile_network",
    "build_solver_profile",
    "run_powerflow",
    "extract_powerflow_result",
    "verify_power_balance",
    "export_artifacts",
}

# V1.3 正式发布能力（Capability Gate）
ENABLED_CAPABILITIES = {
    Capability.STEADY_STATE_POWERFLOW,
    Capability.DSS_CASE_LOADING,
    Capability.RESULT_NORMALIZATION,
    # 2026-09-18 启用：分相建模 + 不对称度量（对称分量法）已落地并回归通过
    # —— 8 个分相算例真实引擎端到端通过、case07（含 18 个 PVSystem）真实回归通过，
    # 报告口径也早已写成「三相不平衡已启用」。此前只有请求门还停在 later_phase，
    # 导致「能力在、但用户一旦点名『三相/不平衡』反而算不了」。
    Capability.THREE_PHASE_UNBALANCED,
}

# 请求中不得出现的禁用能力关键词 → 对应能力
#
# ⚠️ 「三相」「不平衡」已于 2026-09-18 从本表移出。
# 当时的现象：求解层能按三相原生模型算、聊天层答「能做」、报告口径写「已启用」，
# 但用户只要在指令里写「三相不平衡」就会被请求门判为未启用能力并拒绝执行 ——
# 属「能力口径与实现不一致」。现在本表只保留**确实未启用**的能力。
# 「三相不平衡」的边界改由 CAPABILITY_STATUS = "conditional" 表达：
# 能做，但转换类算例按平衡三相等值建模、限值判定仅供参考（报告会显式声明）。
_FORBIDDEN_CAPABILITY_KEYWORDS: dict[str, Capability] = {
    "概率潮流": Capability.PROBABILISTIC_POWERFLOW,
    "数据驱动": Capability.DATA_DRIVEN_POWERFLOW,
    "自动分接头": Capability.AUTO_CONTROL,
    "无功补偿控制": Capability.AUTO_CONTROL,
    "拓扑重构": Capability.AUTO_CONTROL,
    "削减负荷": Capability.AUTO_CONTROL,
    "重调度": Capability.AUTO_CONTROL,
}


class RuleGuard:
    """规则校验与安全兜底。LLM 输出必须经过本 Guard 才能执行。"""

    # ------------------------------------------------------------- 请求门
    def check_request(self, task_spec: PowerFlowTaskSpec, raw_message: str = "") -> list[str]:
        """请求门校验。通过返回空问题列表，否则抛 GuardRejection。"""
        notes: list[str] = []

        # 1. TaskSpec Schema 完整性
        if not task_spec.case_ref:
            raise GuardRejection(ErrorCode.REQ_SCHEMA_INVALID, "缺少 case_ref")

        # 2. 能力声明：用户原始请求 / 目标 / 约束中请求未启用能力
        goal_text = (
            raw_message
            + " "
            + task_spec.user_goal
            + " "
            + " ".join(str(v) for v in task_spec.constraints.hard.values())
        )
        for kw, cap in _FORBIDDEN_CAPABILITY_KEYWORDS.items():
            if kw in goal_text:
                status = CAPABILITY_STATUS[cap]
                if status in ("later_phase", "research_only"):
                    code = (
                        ErrorCode.FIDELITY_NOT_APPROVED
                        if status == "research_only"
                        else ErrorCode.CAPABILITY_UNAVAILABLE
                    )
                    raise GuardRejection(
                        code, f"请求的能力「{kw}」在 V1.3 未启用（{cap.value} / {status}）"
                    )

        # 3. 用户约束冲突：显式参数与强制安全规则冲突
        sc = task_spec.solver_config
        if sc.max_iterations is not None and sc.max_iterations < 1:
            raise GuardRejection(ErrorCode.CONSTRAINT_CONFLICT, "max_iterations 必须 >= 1")
        if sc.tolerance is not None and not (0 < sc.tolerance <= 1.0):
            raise GuardRejection(ErrorCode.CONSTRAINT_CONFLICT, "tolerance 必须在 (0, 1] 区间")

        return notes

    # ------------------------------------------------------------- 执行门
    def check_tool_call(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        """执行门：工具白名单 + 参数 schema 粗检。"""
        if tool_name not in REGISTERED_TOOLS:
            raise GuardRejection(
                ErrorCode.REQ_SCHEMA_INVALID, f"工具未注册: {tool_name}"
            )

    def check_llm_decision(
        self,
        requested_tool: str | None,
        tool_args: dict[str, Any],
        task_spec: PowerFlowTaskSpec,
    ) -> None:
        """对 LLM 产出的工具调用请求做硬约束校验（文档 8.7 / 10.2）。"""
        if requested_tool is None:
            return
        self.check_tool_call(requested_tool, tool_args)

        # 硬约束：LLM 不得修改 result_scopes / solver_config 数值参数 / output_formats
        hard = task_spec.constraints.hard
        if "result_scopes" in tool_args and "result_scopes" in hard:
            if set(map(str, tool_args["result_scopes"])) != set(map(str, hard["result_scopes"])):
                raise GuardRejection(
                    ErrorCode.CONSTRAINT_CONFLICT,
                    "工具调用试图修改初始 Prompt 硬约束 result_scopes",
                )
        for key in ("max_iterations", "tolerance"):
            if key in tool_args and key in hard:
                if tool_args[key] != hard[key]:
                    raise GuardRejection(
                        ErrorCode.CONSTRAINT_CONFLICT,
                        f"工具调用试图修改初始 Prompt 硬约束 {key}",
                    )

    # ------------------------------------------------------------- 敏感信息
    _SENSITIVE_PAT = re.compile(r"(password|secret|token|api[_-]?key)", re.IGNORECASE)

    def sanitize_arguments(self, args: dict[str, Any]) -> dict[str, Any]:
        """Trace 只记录 sanitized_arguments（执行门，文档 13.4）。"""
        clean: dict[str, Any] = {}
        for k, v in args.items():
            if self._SENSITIVE_PAT.search(str(k)):
                clean[k] = "***"
            else:
                clean[k] = v
        return clean
