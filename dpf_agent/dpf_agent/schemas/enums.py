"""枚举定义：状态语义、错误码、阶段、事件类型、结果域、能力门。

对应设计文档 9.2 / 9.4 / 12.2 / 13.1 / 13.3 / 14.2 / 14.3。
"""
from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    """AgentResponse.status（文档 9.2 / 13.3）。"""

    SUCCESS = "success"                    # 求解完成，Verification 与发布门通过 → 正式发布
    PARTIAL = "partial"                    # 部分请求结果域不可用 → 受限发布
    NOT_ASSESSED = "not_assessed"          # 某维度未形成可发布结果，不得解释为 0/正常/安全
    NON_CONVERGED = "non_converged"        # OpenDSS Solve 未收敛 → 仅受限诊断报告
    INVALID_REQUEST = "invalid_request"    # 请求 schema 或约束错误 → 不发布
    DATA_UNAVAILABLE = "data_unavailable"  # DSS 文件/依赖不可用 → 不发布
    VALIDATION_FAILED = "validation_failed"  # 输入或独立 Verification 失败 → 不发布
    CANCELLED = "cancelled"            # 用户主动取消 → 不发布
    SYSTEM_FAILED = "system_failed"        # 系统/适配器异常 → 不发布
    # 进程被强制终止 / 崩溃，任务没有走完生命周期 → 与"失败"语义不同。
    #
    # 为什么必须单独一个终态，而不是归入 SYSTEM_FAILED 或 unknown：
    #   - SYSTEM_FAILED 意味着"代码执行到了，并且给出了失败结论"；
    #     INTERRUPTED 意味着"根本没人来得及下结论"（后端被 Kill、
    #     原生库段错误、机器断电）。两者的排查动作完全不同：
    #     前者看错误码，后者看崩溃日志与最后落盘的阶段。
    #   - 归入 unknown 更糟：unknown 会让指标看板把"基础设施问题"
    #     悄悄混进"业务分布"，成功率的分子分母都被污染。
    INTERRUPTED = "interrupted"            # 进程中断，未能形成结论 → 不发布


class ErrorCode(str, Enum):
    """错误码（文档 9.4 / 13.4）。"""

    REQ_SCHEMA_INVALID = "REQ_SCHEMA_INVALID"
    CONSTRAINT_CONFLICT = "CONSTRAINT_CONFLICT"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    CASE_PATH_REJECTED = "CASE_PATH_REJECTED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    NON_CONVERGED = "NON_CONVERGED"
    RESULT_MAPPING_FAILED = "RESULT_MAPPING_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REPORT_ERROR = "REPORT_ERROR"
    ENGINE_ERROR = "ENGINE_ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"  # 用户主动取消（新增：支持任务级中断）
    FIDELITY_NOT_APPROVED = "FIDELITY_NOT_APPROVED"
    # 进程级中断：后端崩溃 / 被 Kill / 原生库段错误，任务未形成结论。
    # 与 CANCELLED（用户主动）刻意区分：排查方向完全不同。
    INTERRUPTED = "INTERRUPTED"


class Phase(str, Enum):
    """固定工作流状态机阶段（文档 12.2）。"""

    INIT = "INIT"
    CASE_LOADED = "CASE_LOADED"
    CASE_VALIDATED = "CASE_VALIDATED"
    PF_EXECUTED = "PF_EXECUTED"
    RESULT_BUILT = "RESULT_BUILT"
    RESULT_VERIFIED = "RESULT_VERIFIED"
    REPORTED = "REPORTED"
    FAILED = "FAILED"


class EventType(str, Enum):
    """TraceEvent.event_type（文档 9.2 / 14.2）。"""

    USER = "user"
    UNDERSTAND = "understand"
    CONSTRAINTS = "constraints"
    PLAN = "plan"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    GUARD = "guard"
    VERIFY = "verify"
    EVIDENCE = "evidence"
    RESULT = "result"


class EventStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ResultScope(str, Enum):
    BUS = "bus"
    BRANCH = "branch"
    TRANSFORMER = "transformer"
    SYSTEM = "system"


class OutputFormat(str, Enum):
    JSON = "json"
    MD = "md"
    HTML = "html"
    CSV = "csv"


class Capability(str, Enum):
    """能力门（文档 13.1）。V1.3 仅 mvp_candidate 能力对正式发布开放。"""

    STEADY_STATE_POWERFLOW = "steady_state_powerflow"       # mvp_candidate / V1.3
    DSS_CASE_LOADING = "dss_case_loading"                    # mvp_candidate / V1.3
    RESULT_NORMALIZATION = "result_normalization"            # mvp_candidate / V1.3
    VOLTAGE_DEPENDENT_LOAD = "voltage_dependent_load"        # conditional / V1.3
    AUTO_CONTROL = "auto_control"                            # later_phase / V2（V1.3 只识别对象）
    THREE_PHASE_UNBALANCED = "three_phase_unbalanced"        # conditional（2026-09-18 启用）
    PROBABILISTIC_POWERFLOW = "probabilistic_powerflow"      # research_only
    DATA_DRIVEN_POWERFLOW = "data_driven_powerflow"          # research_only


CAPABILITY_STATUS: dict[Capability, str] = {
    Capability.STEADY_STATE_POWERFLOW: "mvp_candidate",
    Capability.DSS_CASE_LOADING: "mvp_candidate",
    Capability.RESULT_NORMALIZATION: "mvp_candidate",
    Capability.VOLTAGE_DEPENDENT_LOAD: "conditional",
    Capability.AUTO_CONTROL: "later_phase",
    # 2026-09-18：分相建模 + 不对称度量已落地并回归通过，由 later_phase 改为 conditional。
    # 不是 mvp_candidate 的原因：转换类算例按「平衡三相等值」建模（原始相别信息不保留），
    # 且不平衡度限值判定待与 GB/T 15543 原文核对 —— 属"能做但有边界"。
    Capability.THREE_PHASE_UNBALANCED: "conditional",
    Capability.PROBABILISTIC_POWERFLOW: "research_only",
    Capability.DATA_DRIVEN_POWERFLOW: "research_only",
}


# 状态 → CLI 退出码（文档 14.3）
EXIT_CODES: dict[TaskStatus, int] = {
    TaskStatus.SUCCESS: 0,
    TaskStatus.PARTIAL: 10,
    TaskStatus.NOT_ASSESSED: 10,
    TaskStatus.INVALID_REQUEST: 20,
    TaskStatus.DATA_UNAVAILABLE: 21,
    TaskStatus.NON_CONVERGED: 30,
    TaskStatus.CANCELLED: 41,
    TaskStatus.VALIDATION_FAILED: 40,
    TaskStatus.SYSTEM_FAILED: 50,
}
