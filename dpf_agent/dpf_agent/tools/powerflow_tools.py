"""V1.3 八个注册工具（文档 9.3 工具接口契约）。

自 V1.4 起，工具从「裸函数」升级为 ``BaseTool`` 子类：

- 每个工具显式声明 ``name`` / ``description`` / ``stage`` / ``input_schema`` /
  ``output_schema``，契约可读、可被 Prompt 生成器直接导出；
- 统一入口 ``invoke(ctx, **kwargs) -> ToolOutput``，执行前按 ``input_schema``
  校验参数（对应"保证工具参数不出错"中的格式校验与必填检查）；
- 保留 ``__call__`` 与模块级同名别名，既有调用点无需一次性改写。

所有工具返回结构化结果（dict），禁止只返回自然语言文本；
工具不直接面向用户输出结论；执行后结果写回 AgentState 与 trace.json。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..config import AgentConfig
from ..domain.case.case_loader import CaseLoadError, load_case_bundle
from ..domain.network_profile import NetworkProfile, build_network_profile
from ..domain.result_normalizer import normalize_powerflow_result
from ..domain.solver_profile import build_solver_profile
from ..engines.opendss_adapter import (
    AdapterError,
    CircuitHandle,
    OpenDSSAdapter,
    OpenDSSRawResult,
    SolverExecutionResult,
)
from ..schemas.case import CanonicalDistributionCase
from ..schemas.enums import ErrorCode, ResultScope, TaskStatus
from ..schemas.result import CanonicalDistributionPowerFlowResult
from ..schemas.solver import OpenDSSSolverProfile
from ..schemas.task_spec import PowerFlowTaskSpec
from ..verification.verifier import Verifier
from .base import BaseTool


def _raw_payload(raw: OpenDSSRawResult, cache: dict[str, Any]) -> dict[str, Any]:
    return {"solver": raw.solver, "system": raw.system, "element_count": raw.element_count,
            "collector_version": raw.collector_version, "buses": raw.buses,
            "lines": raw.lines, "transformers": raw.transformers, "cache": cache}

__all__ = [
    "ToolFailure",
    "TaskContext",
    "TOOL_REGISTRY",
    "tool_schemas",
    "tools_by_stage",
    # 兼容别名
    "load_case", "validate_case", "profile_network", "build_solver_profile_tool",
    "run_powerflow", "extract_powerflow_result", "verify_power_balance_tool",
    "export_artifacts",
]


class ToolFailure(Exception):
    """工具失败：携带错误码与任务状态语义。"""

    def __init__(self, code: ErrorCode, status: TaskStatus, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


@dataclass
class TaskContext:
    """任务后端上下文（仅存于后端，不进入浏览器 Session）。"""

    task_spec: PowerFlowTaskSpec
    workspace: str
    config: AgentConfig
    adapter: OpenDSSAdapter
    case: CanonicalDistributionCase | None = None
    network_profile: NetworkProfile | None = None
    solver_profile: OpenDSSSolverProfile | None = None
    circuit: CircuitHandle | None = None
    solve_result: SolverExecutionResult | None = None
    raw_result: OpenDSSRawResult | None = None
    canonical_result: CanonicalDistributionPowerFlowResult | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    # 改变物理模型的恢复结果只属于本次任务，禁止读写共享求解缓存。
    model_recovery_applied: bool = False
    # 仅在完整独立验证通过后提交缓存，防止收敛但无效的结果污染后续任务。
    pending_solve_cache: tuple[str, OpenDSSRawResult, SolverExecutionResult] | None = None
    # 服务可延迟到解释等后续阶段完成后提交；独立工具默认仍在验证通过后提交。
    defer_cache_commit: bool = False
    # 记录注册工具实际收到的每次 Solve 返回，包含恢复前的失败；不含 Compile 内部求解。
    solve_history: list[dict[str, Any]] = field(default_factory=list)

    def record_solve(self, stage: str, solved: SolverExecutionResult, iteration_limit: int) -> None:
        from dataclasses import asdict
        self.solve_history.append({"stage": stage, "iteration_limit": iteration_limit,
                                   "case_fingerprint": self.case.case_fingerprint if self.case else "",
                                   "profile_id": self.solver_profile.profile_id if self.solver_profile else "",
                                   **asdict(solved)})
        self.save_json("solve_history.json", {
            "calls": self.solve_history, "solution_count": len(self.solve_history),
            "total_iterations": sum(row["iterations"] for row in self.solve_history),
            "counted_stages": "registered run/refinement/verification; excludes Compile-internal Solve",
        })

    def save_json(self, name: str, data: Any) -> str:
        path = os.path.join(self.workspace, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            if hasattr(data, "model_dump"):
                json.dump(data.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
            else:
                json.dump(data, f, ensure_ascii=False, indent=2)
        self.artifacts[name] = path
        return path


def _check_task_cancelled(ctx: TaskContext, error: Exception | None = None) -> None:
    """原生调用也能返回取消，不能把它降格成数值验证失败。"""
    from ..runtime.cancellation import is_cancelled

    native_cancelled = isinstance(error, (AdapterError, ToolFailure)) and error.code == ErrorCode.CANCELLED
    if native_cancelled or is_cancelled(ctx.task_spec.task_id):
        ctx.pending_solve_cache = None
        raise ToolFailure(ErrorCode.CANCELLED, TaskStatus.CANCELLED,
                          str(error) if native_cancelled else "任务已被用户取消") from error


# 多数工具不接受外部参数：显式声明为空对象，避免"意外传入参数"被静默忽略
_NO_ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# 1. load_case
# ---------------------------------------------------------------------------
class LoadCaseTool(BaseTool):
    name: ClassVar[str] = "load_case"
    description: ClassVar[str] = "复制算例到任务只读副本，解析依赖，生成清单与指纹"
    stage: ClassVar[str] = "load_case"
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "case_ref": {
                "type": "string",
                "description": "算例引用路径（本地文件、目录或内置算例名）",
                "minLength": 1,
            }
        },
        "required": ["case_ref"],
        "additionalProperties": False,
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "case_fingerprint": {"type": "string"},
            "dependency_files": {"type": "integer"},
            "master_dss": {"type": "string"},
            "source_type": {"type": "string"},
        },
    }

    def run(self, ctx: TaskContext, **kwargs: Any) -> dict[str, Any]:
        case_ref = kwargs["case_ref"]
        try:
            case = load_case_bundle(case_ref, ctx.workspace, ctx.config.cases_root)
        except CaseLoadError as exc:
            status = (
                TaskStatus.DATA_UNAVAILABLE
                if exc.code == ErrorCode.DATA_UNAVAILABLE
                else TaskStatus.VALIDATION_FAILED
                if exc.code == ErrorCode.VALIDATION_FAILED
                else TaskStatus.INVALID_REQUEST
            )
            raise ToolFailure(exc.code, status, exc.message)
        ctx.case = case
        path = ctx.save_json("validation.json", {"case": case.model_dump(mode="json")})
        case.validation_ref = path
        return {
            "case_fingerprint": case.case_fingerprint,
            "dependency_files": case.dependency_manifest.file_count,
            "master_dss": case.master_dss_ref,
            "source_type": case.source_type,
        }


# ---------------------------------------------------------------------------
# 2. validate_case
# ---------------------------------------------------------------------------
class ValidateCaseTool(BaseTool):
    name: ClassVar[str] = "validate_case"
    description: ClassVar[str] = "受控 Compile 预检 + 静态检查；validation_failed 时停止，不调用 Solve"
    stage: ClassVar[str] = "validate_case"
    input_schema: ClassVar[dict[str, Any]] = dict(_NO_ARGS_SCHEMA)
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "object_summary": {"type": "object"},
        },
    }

    def run(self, ctx: TaskContext, **kwargs: Any) -> dict[str, Any]:
        assert ctx.case is not None
        master = os.path.join(ctx.workspace, "input", ctx.case.master_dss_ref)
        try:
            circuit = ctx.adapter.compile_case(os.path.join(ctx.workspace, "input"), master)
        except AdapterError as exc:
            failure_status = (
                TaskStatus.DATA_UNAVAILABLE
                if exc.code == ErrorCode.DATA_UNAVAILABLE
                else TaskStatus.VALIDATION_FAILED
            )
            raise ToolFailure(exc.code, failure_status, exc.message)
        ctx.circuit = circuit
        object_summary, base_kv = ctx.adapter.circuit_summary(circuit)
        ctx.case.object_summary = object_summary
        ctx.case.base_voltage_summary = base_kv
        ctx.save_json("validation.json", {
            "passed": True,
            "case": ctx.case.model_dump(mode="json"),
        })
        return {"passed": True, "object_summary": object_summary}


# ---------------------------------------------------------------------------
# 3. profile_network
# ---------------------------------------------------------------------------
class ProfileNetworkTool(BaseTool):
    name: ClassVar[str] = "profile_network"
    description: ClassVar[str] = "确定性提取网络画像（母线/线路/负荷/电压等级/拓扑特征）"
    stage: ClassVar[str] = "build_network_profile"
    input_schema: ClassVar[dict[str, Any]] = dict(_NO_ARGS_SCHEMA)
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"profile": {"type": "object"}},
    }

    def run(self, ctx: TaskContext, **kwargs: Any) -> dict[str, Any]:
        assert ctx.circuit is not None and ctx.case is not None
        # 观察量由适配器收集（引擎相关，各通道各写一份）；
        # 画像组装是纯数据、单点定义。这样画像逻辑不再依赖
        # 「能执行 Python 闭包」这一前提 —— CLI 通道也能复用同一套组装。
        profile = build_network_profile(
            ctx.case.case_id,
            ctx.adapter.collect_circuit_inventory(),
            os.path.join(ctx.workspace, "input"),
        )
        ctx.network_profile = profile
        ctx.save_json("network_profile.json", profile)
        return {"profile": profile.summary()}


# ---------------------------------------------------------------------------
# 4. build_solver_profile
# ---------------------------------------------------------------------------
class BuildSolverProfileTool(BaseTool):
    name: ClassVar[str] = "build_solver_profile"
    description: ClassVar[str] = "冻结求解策略；约束优先级 P1 用户 > P2 算例 > P3 默认"
    stage: ClassVar[str] = "build_opendss_solver_profile"
    input_schema: ClassVar[dict[str, Any]] = dict(_NO_ARGS_SCHEMA)
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "profile_id": {"type": "string"},
            "params": {"type": "object"},
        },
    }

    def run(self, ctx: TaskContext, **kwargs: Any) -> dict[str, Any]:
        assert ctx.network_profile is not None
        health = ctx.adapter.health()
        from .. import ADAPTER_VERSION

        profile = build_solver_profile(
            ctx.task_spec,
            ctx.network_profile,
            ctx.config.defaults,
            engine_version=health.engine_version,
            adapter_version=ADAPTER_VERSION,
        )
        ctx.solver_profile = profile
        ctx.adapter.apply_solver_profile(ctx.circuit, profile)
        ctx.save_json("solver_profile.json", profile)
        return {"profile_id": profile.profile_id, "params": profile.param_dict()}


# ---------------------------------------------------------------------------
# 5. run_powerflow
# ---------------------------------------------------------------------------
class RunPowerFlowTool(BaseTool):
    name: ClassVar[str] = "run_powerflow"
    description: ClassVar[str] = (
        "执行 OpenDSS Solve 并采集原始结果；"
        "non_converged → 仅生成受限诊断报告，不发布正式结果"
    )
    stage: ClassVar[str] = "run_opendss_powerflow"
    input_schema: ClassVar[dict[str, Any]] = dict(_NO_ARGS_SCHEMA)
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "converged": {"type": "boolean"},
            "iterations": {"type": "integer"},
            "duration_s": {"type": "number"},
            "elements": {"type": "integer"},
        },
    }

    def run(self, ctx: TaskContext, **kwargs: Any) -> dict[str, Any]:
        assert ctx.solver_profile is not None and ctx.circuit is not None

        # 缓存只记录已验证产物。引擎的迭代状态没有随 JSON 缓存保存，不能跳过
        # 首次真实 Solve 后把冷启动计算冒充“二次复算”。即使找到缓存，也采用
        # 本次真实首次求解作为权威结果，再做独立二次验证；不宣称缓存加速。
        ctx.pending_solve_cache = None
        cache_key = self._solve_cache_key(ctx)
        cached = self._load_solve_cache(ctx, cache_key)
        try:
            solve_result = ctx.adapter.solve(ctx.circuit)
        except AdapterError as exc:
            failure_status = (
                TaskStatus.DATA_UNAVAILABLE
                if exc.code == ErrorCode.DATA_UNAVAILABLE
                else TaskStatus.VALIDATION_FAILED
                if exc.code == ErrorCode.VALIDATION_FAILED
                else TaskStatus.SYSTEM_FAILED
            )
            raise ToolFailure(exc.code, failure_status, exc.message)
        ctx.solve_result = solve_result
        ctx.record_solve("run", solve_result, ctx.solver_profile.iteration_config.max_iterations)
        raw = ctx.adapter.extract_raw_result(ctx.circuit, ctx.solver_profile.result_scopes)
        ctx.raw_result = raw
        ctx.save_json("raw_opendss_result.json", _raw_payload(raw, {
                "hit": False,
                "available": cached is not None,
                "key": cache_key,
                "result_source": "live_solve",
                "engine_synchronized": solve_result.converged and not solve_result.error_flag,
                "disabled_for_model_recovery": bool(getattr(ctx, "model_recovery_applied", False)),
        }))
        if not solve_result.converged:
            # 不收敛的结果不写缓存（重规划后会得到不同的参数与结果）
            raise ToolFailure(
                ErrorCode.NON_CONVERGED,
                TaskStatus.NON_CONVERGED,
                f"OpenDSS Solve 未收敛（iterations={solve_result.iterations}），仅生成受限诊断报告",
            )
        if solve_result.error_flag:
            raise ToolFailure(
                ErrorCode.ENGINE_ERROR,
                TaskStatus.SYSTEM_FAILED,
                f"OpenDSS Solve 返回引擎错误：{solve_result.error_text or 'error_flag=True'}",
            )
        if not getattr(ctx, "model_recovery_applied", False):
            ctx.pending_solve_cache = (cache_key, raw, solve_result)
        return {
            "converged": True,
            "iterations": solve_result.iterations,
            "duration_s": round(solve_result.duration_s, 3),
            "elements": raw.element_count,
            "cache_hit": False,
            "cache_available": cached is not None,
            "cache_key": cache_key,
            "engine_synchronized": True,
        }

    # ---- 求解缓存辅助 ----
    #
    # ⚠️ 缓存键的**唯一职责**是「回答同一个问题的所有输入是否完全一致」。
    # 它必须是**内容决定的**，绝不能包含任何**位置信息**。
    #
    # 实测过的真实缺陷（已修）：旧实现把 `Master.dss` 的**绝对路径**也哈希了进去。
    # 而算例的落盘位置天然会变 —— 上传算例每次落在新的 session 目录，
    # 且 case_loader 还会把整份依赖**复制**到 `tasks/<task_id>/input/`。
    # 于是同一个算例、同样的参数，每次计算都得到一个全新的键，
    # 缓存**永不命中**（线上 37 个缓存文件彼此孤立，命中数恒为 0）。
    #
    # 「以路径为身份」在工程上很常见但在这里是错的：路径是**存放位置**，
    # 不是**内容身份**。用内容哈希才有跨目录、跨会话的复用能力。
    #
    # 现在改用 case_loader 已经算好的 **`case_fingerprint`**：
    # 它把整份依赖清单按「相对路径 : 文件内容 sha256」排序后聚合哈希，
    # 天然满足两点 —— 内容变了必然变（哪怕只改一个字符）、
    # 存到哪都不变（相对路径参与，绝对路径不参与）。
    # 单文件算例没有清单时，退回直接哈希 Master.dss 内容。
    def _solve_cache_key(self, ctx: TaskContext) -> str:
        import hashlib

        sp = ctx.solver_profile
        h = hashlib.sha256()

        # ---- 1. 算例身份：内容指纹优先，内容哈希兜底，都不含绝对路径 ----
        fingerprint = ""
        master_ref = ""
        if ctx.case is not None:
            fingerprint = str(getattr(ctx.case, "case_fingerprint", "") or "")
            # master_dss_ref 是**任务副本内的相对路径**（如 "Master.dss"），
            # 与 session 目录无关，可以安全参与键计算。
            master_ref = str(getattr(ctx.case, "master_dss_ref", "") or "")
        if fingerprint:
            h.update(b"fingerprint:")
            h.update(fingerprint.encode("utf-8", errors="ignore"))
            # 同指纹不同入口文件名（如 Master.dss vs case01.dss）意味着
            # 清单里的相对路径不同 → 指纹其实已不同。这里再带上相对路径
            # 作为显式声明，避免读者误以为指纹覆盖不到这一点。
            h.update(b"|ref:")
            h.update(master_ref.encode("utf-8", errors="ignore"))
        else:
            # 兜底：没有 CanonicalCase（例如早期调用路径）时，
            # 直接哈希 Master.dss 的**内容**。注意不含路径。
            h.update(b"content:")
            master = ctx.circuit.master_dss if ctx.circuit else ""
            try:
                if master and os.path.isfile(master):
                    with open(master, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            h.update(chunk)
            except OSError:
                pass

        # ---- 2. 求解策略：必须覆盖**全部影响数值**的维度 ----
        # 旧实现只放了 max_iterations/tolerance/scopes/mode/engine，
        # 漏了 control_behavior 与 version_set —— 引擎或适配器版本一变，
        # 同样的算例可能算出不同的数，却会命中旧缓存（静默错误结果）。
        h.update(
            repr({
                "max_iterations": int(sp.iteration_config.max_iterations),
                "tolerance": float(sp.tolerance_config.tolerance),
                "scopes": sorted(s.value for s in sp.result_scopes),
                "mode": sp.solve_mode,
                "engine": sp.engine,
                # 控制行为：V1.3 固定 frozen_off，但一旦将来放开自动控制，
                # 不同取值会算出不同结果，必须参与键。
                "control": getattr(sp, "control_behavior", ""),
                # 版本集：引擎/适配器/默认阈值版本。换引擎或改默认值后
                # 结果可能不同，旧缓存不应再被复用。
                "versions": dict(sorted((sp.version_set or {}).items())),
            }).encode("utf-8", errors="ignore")
        )
        if getattr(getattr(ctx, "adapter", None), "supports_snapshot_refinement", False):
            from .snapshot_stability import policy
            h.update(repr(policy(ctx)).encode("utf-8"))

        # ---- 3. 元件求和口径 ----
        # 口径（什么算负荷、什么算发电）直接决定 raw.system 里的数值。
        # 口径一变，旧缓存里的数就是错的，必须让它失效——否则会出现
        # "代码改了、结果没变"的假象（实测踩过：修好 PVSystem 计入后重跑，
        # 缓存命中旧口径结果，读数仍是 gen_kw=0）。
        #
        # 这里用**口径表自身的内容**做指纹，而不是维护一个人工版本号：
        # 改表即失效，不必指望谁记得「改了要升号」。本项目已有教训——
        # 转换器改过却忘了升版本号，导致老算例沿用旧产物，修复看起来无效。
        from ..engines.opendss_adapter import (
            ELEMENT_EXCLUDED_PREFIXES,
            ELEMENT_ROLE_BY_PREFIX,
        )

        h.update(b"caliber:")
        h.update(repr(sorted(ELEMENT_ROLE_BY_PREFIX.items())).encode("utf-8", errors="ignore"))
        h.update(repr(sorted(ELEMENT_EXCLUDED_PREFIXES.items())).encode("utf-8", errors="ignore"))
        return h.hexdigest()[:24]

    def _cache_path(self, ctx: TaskContext, key: str) -> str:
        return os.path.join(AgentConfig().outputs_root, "_cache", "solve", f"{key}.json")

    def _load_solve_cache(self, ctx: TaskContext, key: str) -> dict[str, Any] | None:
        if getattr(ctx, "model_recovery_applied", False):
            return None
        if not AgentConfig().solve_cache_enabled:
            return None
        path = self._cache_path(ctx, key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) and data.get("solver") else None
        except (OSError, json.JSONDecodeError):
            return None

    def _save_solve_cache(
        self, ctx: TaskContext, key: str, raw: OpenDSSRawResult, solve_result: Any
    ) -> None:
        _check_task_cancelled(ctx)
        if getattr(ctx, "model_recovery_applied", False):
            return
        if not AgentConfig().solve_cache_enabled:
            return
        path = self._cache_path(ctx, key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = {
                "cache_key": key,
                "created_at": time.time(),
                # 只记录**内容身份**与**相对引用**，不记录绝对路径。
                #
                # 原因：绝对路径会随会话/任务变化（且任务工作区可被清理），
                # 写进来既没有复用价值，又会让排查的人误以为"路径不同所以不该命中"。
                # 真正决定身份的是 case_fingerprint。
                "case_fingerprint": (
                    str(getattr(ctx.case, "case_fingerprint", "") or "") if ctx.case else ""
                ),
                "master_dss_ref": (
                    str(getattr(ctx.case, "master_dss_ref", "") or "") if ctx.case else ""
                ),
                "solver": raw.solver,
                "system": raw.system,
                "element_count": raw.element_count,
                # 采集侧版本戳：让"这份缓存是哪一版逻辑写的"可追溯。
                # 老缓存没这个键（读出来是空串）—— 空即"旧版"，可据此判断该不该清。
                "collector_version": raw.collector_version,
                "buses": raw.buses,
                "lines": raw.lines,
                "transformers": raw.transformers,
                "duration_s": round(float(solve_result.duration_s), 3),
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            try:
                _check_task_cancelled(ctx)
            except ToolFailure:
                # 取消时只撤销本次尚未发布的临时文件，保留已有成功缓存。
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            os.replace(tmp, path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 6. extract_powerflow_result
# ---------------------------------------------------------------------------
class ExtractPowerFlowResultTool(BaseTool):
    name: ClassVar[str] = "extract_powerflow_result"
    description: ClassVar[str] = "Result Extractor：OpenDSS 原始结果 → Canonical Result"
    stage: ClassVar[str] = "normalize_results"
    input_schema: ClassVar[dict[str, Any]] = dict(_NO_ARGS_SCHEMA)
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "result_ref": {"type": "string"},
            "scope_status": {"type": "object"},
            "partial_scopes": {"type": "array", "items": {"type": "string"}},
        },
    }

    def run(self, ctx: TaskContext, **kwargs: Any) -> dict[str, Any]:
        from .. import ADAPTER_VERSION

        assert ctx.raw_result is not None and ctx.solve_result is not None
        assert ctx.case is not None and ctx.solver_profile is not None
        result = normalize_powerflow_result(
            ctx.raw_result,
            ctx.solve_result,
            task_id=ctx.task_spec.task_id,
            case_fingerprint=ctx.case.case_fingerprint,
            solver_profile_ref=ctx.solver_profile.profile_id,
            engine_version=ctx.solver_profile.version_set.get("engine_version", ""),
            adapter_version=ADAPTER_VERSION,
            requested_scopes=ctx.task_spec.result_scopes,
        )
        ctx.canonical_result = result
        path = ctx.save_json("powerflow_result.json", result)
        partial_scopes = [
            s for s, st in result.result_status_by_scope.items()
            if st == "unavailable" and s in [x.value for x in ctx.task_spec.result_scopes]
        ]
        return {
            "result_ref": path,
            "scope_status": result.result_status_by_scope,
            "partial_scopes": partial_scopes,
        }


# ---------------------------------------------------------------------------
# 7. verify_power_balance
# ---------------------------------------------------------------------------
class VerifyPowerBalanceTool(BaseTool):
    name: ClassVar[str] = "verify_power_balance"
    description: ClassVar[str] = (
        "独立 Verifier：完整性、数值有效性、功率平衡、复现性；"
        "未通过则不发布正式结果"
    )
    stage: ClassVar[str] = "verify_result_integrity_and_power_balance"
    input_schema: ClassVar[dict[str, Any]] = dict(_NO_ARGS_SCHEMA)
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "failed_domains": {"type": "array", "items": {"type": "string"}},
            "residual_pu": {"type": "number"},
            "verification": {"type": "object"},
        },
    }

    def run(self, ctx: TaskContext, **kwargs: Any) -> dict[str, Any]:
        assert ctx.canonical_result is not None and ctx.case is not None
        assert ctx.solver_profile is not None
        _check_task_cancelled(ctx)
        verifier = Verifier(ctx.config.defaults)
        verification = verifier.verify(
            ctx.case,
            ctx.canonical_result,
            ctx.solver_profile,
            [s.value for s in ctx.task_spec.result_scopes],
        )
        # 复现性：同输入、同版本、同 SolverProfile 二次求解比对
        try:
            solve2 = ctx.adapter.solve(ctx.circuit)
            ctx.record_solve("verification", solve2, ctx.solver_profile.iteration_config.max_iterations)
            _check_task_cancelled(ctx)
            if not solve2.converged or solve2.error_flag:
                raise ValueError(
                    "复现性二次求解未得到有效收敛解："
                    f"converged={solve2.converged}, error_flag={solve2.error_flag}"
                )
            raw2 = ctx.adapter.extract_raw_result(ctx.circuit, ctx.solver_profile.result_scopes)
            _check_task_cancelled(ctx)
            from .. import ADAPTER_VERSION
            second = normalize_powerflow_result(
                raw2, solve2,
                task_id=ctx.task_spec.task_id,
                case_fingerprint=ctx.case.case_fingerprint,
                solver_profile_ref=ctx.solver_profile.profile_id,
                engine_version=ctx.solver_profile.version_set.get("engine_version", ""),
                adapter_version=ADAPTER_VERSION,
                requested_scopes=ctx.task_spec.result_scopes,
            )
            # 复现性容差与求解容差同量级：二次求解从上次解出发继续求精，
            # 差异上限本来就是第一次解的残差（~tolerance 级）。用 1e-5 会把
            # 正常的迭代求精误判为非确定性求解。
            repro_tol = max(1e-4, ctx.solver_profile.tolerance_config.tolerance)
            repro = verifier.check_reproducibility(ctx.canonical_result, second, rel_tol=repro_tol)
            if (verification.passed and not repro.passed
                    and getattr(ctx.adapter, "supports_snapshot_refinement", False)):
                from .snapshot_stability import refine_snapshot
                stabilized = refine_snapshot(ctx, raw2, solve2, second, verifier, repro_tol, repro)
                _check_task_cancelled(ctx)
                if stabilized.passed:
                    cache_meta = {
                        "hit": False, "available": False,
                        "key": RunPowerFlowTool()._solve_cache_key(ctx),
                        "result_source": "live_solve", "engine_synchronized": True,
                        "disabled_for_model_recovery": ctx.model_recovery_applied,
                    }
                    # 保留最初的缓存存在性等审计字段，只换成经最终验证的权威数值。
                    with open(ctx.artifacts["raw_opendss_result.json"], encoding="utf-8") as saved:
                        cache_meta.update(json.load(saved).get("cache") or {})
                    _check_task_cancelled(ctx)
                    ctx.save_json("raw_opendss_result.json", _raw_payload(stabilized.raw, cache_meta))
                    ctx.save_json("powerflow_result.json", stabilized.canonical)
                    _check_task_cancelled(ctx)
                    ctx.raw_result = stabilized.raw
                    ctx.solve_result = stabilized.solve
                    ctx.canonical_result = stabilized.canonical
                    if ctx.pending_solve_cache is not None:
                        key, _, _ = ctx.pending_solve_cache
                        ctx.pending_solve_cache = (key, stabilized.raw, stabilized.solve)
                    verification = stabilized.verification
                else:
                    verification.checks.append(stabilized.check)
                    verification.passed = False
            else:
                verification.checks.append(repro)
                verification.passed = verification.passed and repro.passed
        except Exception as exc:
            from .snapshot_stability import RefinementCancelled
            if isinstance(exc, RefinementCancelled):
                ctx.pending_solve_cache = None
                raise ToolFailure(ErrorCode.CANCELLED, TaskStatus.CANCELLED, str(exc)) from exc
            _check_task_cancelled(ctx, exc)
            from ..schemas.verification import CheckResult

            verification.checks.append(
                CheckResult(
                    check_id="reproducibility",
                    domain="reproducibility",
                    passed=False,
                    detail=f"复现性二次求解失败: {exc}",
                )
            )
            verification.passed = False
        _check_task_cancelled(ctx)
        if verification.passed and ctx.solve_history and ctx.raw_result is not None:
            ctx.raw_result.solver["solution_count"] = len(ctx.solve_history)
            ctx.raw_result.solver["total_iterations"] = sum(row["iterations"] for row in ctx.solve_history)
            ctx.canonical_result.solver_status.raw_solution_count = len(ctx.solve_history)
            ctx.canonical_result.solver_status.total_iterations = ctx.raw_result.solver["total_iterations"]
            with open(ctx.artifacts["raw_opendss_result.json"], encoding="utf-8") as saved:
                cache_meta = json.load(saved).get("cache") or {}
            _check_task_cancelled(ctx)
            ctx.save_json("raw_opendss_result.json", _raw_payload(ctx.raw_result, cache_meta))
            ctx.save_json("powerflow_result.json", ctx.canonical_result)
        ctx.save_json("verification.json", verification)
        _check_task_cancelled(ctx)
        pending = ctx.pending_solve_cache
        if not verification.passed or not ctx.defer_cache_commit:
            ctx.pending_solve_cache = None
        if verification.passed and pending is not None and not ctx.defer_cache_commit:
            _check_task_cancelled(ctx)
            key, raw, first_solve = pending
            RunPowerFlowTool()._save_solve_cache(ctx, key, raw, first_solve)
        return {
            "passed": verification.passed,
            "failed_domains": verification.failed_domains(),
            "residual_pu": verification.power_balance_residual_pu,
            "verification": verification.model_dump(mode="json"),
        }


# ---------------------------------------------------------------------------
# 8. export_artifacts
# ---------------------------------------------------------------------------
class ExportArtifactsTool(BaseTool):
    name: ClassVar[str] = "export_artifacts"
    description: ClassVar[str] = "生成 artifact_manifest.json（权威 JSON、报告与清单）"
    stage: ClassVar[str] = "export_artifacts"
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "extra": {
                "type": "object",
                "description": "额外并入清单的产物（artifact 名 → 路径）",
            }
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "manifest_ref": {"type": "string"},
            "count": {"type": "integer"},
        },
    }

    def run(self, ctx: TaskContext, **kwargs: Any) -> dict[str, Any]:
        extra = kwargs.get("extra")
        manifest = {"task_id": ctx.task_spec.task_id, "artifacts": dict(ctx.artifacts)}
        if extra:
            manifest["artifacts"].update(extra)
        path = ctx.save_json("artifact_manifest.json", manifest)
        return {"manifest_ref": path, "count": len(manifest["artifacts"])}


# ---------------------------------------------------------------------------
# 注册表：名称 → BaseTool 实例
# ---------------------------------------------------------------------------
TOOL_REGISTRY: dict[str, BaseTool] = {
    t.name: t()
    for t in (
        LoadCaseTool,
        ValidateCaseTool,
        ProfileNetworkTool,
        BuildSolverProfileTool,
        RunPowerFlowTool,
        ExtractPowerFlowResultTool,
        VerifyPowerBalanceTool,
        ExportArtifactsTool,
    )
}


def tool_schemas() -> list[dict[str, Any]]:
    """导出 OpenAI function-calling 风格的工具声明（供 Prompt 生成器使用）。"""
    from .base import tool_json_schemas

    return tool_json_schemas(TOOL_REGISTRY)


def tools_by_stage() -> dict[str, list[str]]:
    """阶段 → 该阶段允许调用的工具名（阶段白名单的数据来源）。"""
    mapping: dict[str, list[str]] = {}
    for name, tool in TOOL_REGISTRY.items():
        mapping.setdefault(tool.stage, []).append(name)
    return mapping


def allowed_tools(stage: str) -> list[str]:
    """返回指定阶段允许调用的工具名列表。"""
    return tools_by_stage().get(stage, [])


# ---------------------------------------------------------------------------
# 兼容别名：既有 `from ..tools.powerflow_tools import load_case` 仍可用，
# 调用方式 `load_case(ctx, case_ref=...)` 保持不变（走 BaseTool.__call__）。
# ---------------------------------------------------------------------------
load_case = TOOL_REGISTRY["load_case"]
validate_case = TOOL_REGISTRY["validate_case"]
profile_network = TOOL_REGISTRY["profile_network"]
build_solver_profile_tool = TOOL_REGISTRY["build_solver_profile"]
run_powerflow = TOOL_REGISTRY["run_powerflow"]
extract_powerflow_result = TOOL_REGISTRY["extract_powerflow_result"]
verify_power_balance_tool = TOOL_REGISTRY["verify_power_balance"]
export_artifacts = TOOL_REGISTRY["export_artifacts"]
