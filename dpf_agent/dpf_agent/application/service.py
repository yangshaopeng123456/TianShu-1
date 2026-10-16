"""Application Service：任务生命周期与唯一业务门面（文档 6 / 7.3）。

所有生产入口必须经过本服务；CLI / Web / API 均不得直接调用 OpenDSS。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from typing import Any, Callable

from .. import ADAPTER_VERSION
from ..config import AgentConfig, DEFAULT_CONFIG
from ..engines.opendss_adapter import OpenDSSAdapter
from ..prompts import prompt_versions
from ..renderers.markdown_renderer import render_csv, render_html, render_markdown
from ..runtime.guard import GuardRejection, RuleGuard
from ..runtime.cancellation import is_cancelled
from ..runtime.model_client import ModelClient, ParsedTask, build_user_constraints
from ..runtime.tracing import Tracer
from ..schemas.enums import (
    ErrorCode,
    EventStatus,
    EventType,
    OutputFormat,
    Phase,
    ResultScope,
    TaskStatus,
)
from ..schemas.state import AgentState
from ..schemas.task_spec import PowerFlowTaskSpec, SolverConfig, UserRequest
from ..skills.powerflow_skill import run_fixed_powerflow_skill
from ..tools.powerflow_tools import TaskContext, ToolFailure


class AgentResponse(dict):
    """结构化响应（文档 9.2）：status / task_id / plan / tool_trace /
    result_ref / verification / evidence / artifacts / report。"""


# dss-python 原生库首次 import 在个别机器上会**永久挂住**（不是报错，是不返回），
# 一旦发生，整个 HTTP 请求会永远不回包，前端执行监控就"一动不动"到天荒地老。
# 这里给引擎健康检查加硬超时：宁可明确失败，也不要无声挂死。
_ENGINE_HEALTH_TIMEOUT_S = float(os.environ.get("DPF_ENGINE_HEALTH_TIMEOUT_S", "90"))
_engine_health_state: dict[str, Any] = {"checked": False, "available": False, "detail": ""}


def probe_engine_health(adapter: Any, timeout_s: float = _ENGINE_HEALTH_TIMEOUT_S) -> Any:
    """带硬超时的 adapter.health()。超时或异常都视为引擎不可用。

    注意：超时后那个线程仍在后台挂着（原生库无法从 Python 层打断），
    因此结果会被缓存，避免每个任务都漏一个线程。
    """
    # A task-owned process already enforces its own hard timeout. A global
    # result from another task must neither suppress its probe nor poison a
    # later healthy worker after an earlier task failed.
    if getattr(adapter, "process_isolated", False):
        return adapter.health()
    if _engine_health_state["checked"]:
        from ..engines.opendss_adapter import EngineHealth
        return EngineHealth(
            available=_engine_health_state["available"],
            detail=_engine_health_state["detail"],
        )

    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["health"] = adapter.health()
        except BaseException as exc:  # noqa: BLE001 - 原生库可能抛任意异常
            box["error"] = exc

    t = threading.Thread(target=_run, name="engine-health", daemon=True)
    t.start()
    t.join(timeout_s)
    if not t.is_alive() or "health" in box or "error" in box:
        health = box.get("health")
        if health is None:
            from ..engines.opendss_adapter import EngineHealth
            health = EngineHealth(available=False, detail=f"OpenDSS 引擎不可用: {box.get('error')}")
        _engine_health_state.update(
            {"checked": True, "available": health.available, "detail": health.detail}
        )
        return health

    from ..engines.opendss_adapter import EngineHealth
    detail = (
        f"OpenDSS 引擎初始化超过 {int(timeout_s)} 秒仍未返回"
        "（dss-python 原生库疑似挂死）。请重启天枢一号后端；"
        "若反复出现，检查 dss-python 安装与系统资源。"
    )
    _engine_health_state.update({"checked": True, "available": False, "detail": detail})
    return EngineHealth(available=False, detail=detail)


class TaskBusyError(RuntimeError):
    """同一时刻只允许一个潮流任务：引擎为单实例，并发会产生静默错误结果。

    正确性优先于吞吐：宁可直接拒绝第二个任务并给出明确提示，
    也不要让两个任务共用引擎导致"都成功但数值是错的"。
    """


# 全进程任务串行锁（模块级）：见 ApplicationService.run 中的说明
_TASK_LOCK = threading.Lock()
_TASK_LOCK_OWNER: dict[str, Any] = {"task_id": None, "since": None}


def current_task_lock() -> dict[str, Any]:
    """诊断用：当前是否有任务在跑、是哪个、什么时候开始的。"""
    return {
        "locked": _TASK_LOCK.locked(),
        "task_id": _TASK_LOCK_OWNER.get("task_id"),
        "since": _TASK_LOCK_OWNER.get("since"),
    }


class ApplicationService:
    def __init__(self, config: AgentConfig | None = None):
        self.config = config or DEFAULT_CONFIG
        self.guard = RuleGuard()
        self.model_client = ModelClient(self.config)
        # 统一规范化为绝对路径，避免调用方 cwd 变化造成任务和历史产物漂移。
        self.config.workspace_root = os.path.abspath(self.config.workspace_root)
        self.config.outputs_root = os.path.abspath(self.config.outputs_root)
        self.config.cases_root = os.path.abspath(self.config.cases_root)
        os.makedirs(self.config.workspace_root, exist_ok=True)
        os.makedirs(self.config.outputs_root, exist_ok=True)

    def _cancelled_verification(self, ctx, state, message="任务已被用户取消"):
        from ..schemas.verification import CheckResult, VerificationResult
        state.fail(ErrorCode.CANCELLED.value, message, "cancelled", TaskStatus.CANCELLED)
        verification = VerificationResult(task_id=state.task_id, passed=False,
            checks=[CheckResult(check_id="task_cancellation", domain="execution", passed=False, detail=message)],
            notes=["取消后保留的原始数值仅供诊断，不作为本任务通过验证的正式结果。"])
        if ctx is not None:
            ctx.pending_solve_cache = None
            ctx.save_json("verification.json", verification)
        return verification

    # ------------------------------------------------------ 故障诊断（看门狗）
    def _report_engine_failure(self, ctx: "TaskContext | None", code: Any, message: str) -> None:
        """把引擎级故障交给看门狗复检，由它决定是否需要重启整个后端。

        ## 为什么要复检，而不是直接重启

        引擎超时既可能是"进程已中毒"——9/15 Step 0 实测：本机
        `OpenDSSEngine.dll` 一旦在进程里挂死，进程内没有任何自愈手段；
        也可能只是"这个算例本身就慢过了超时线"。两者都表现为 TIMEOUT，
        只有再问引擎一句"你还活着吗"才能区分：
        复检健康就继续服务（那只是算例慢），复检无响应才判定中毒。

        ## 边界

        本方法跑在**任务失败路径**上，必须绝对安全：全程 try 包裹，
        任何异常都只是少一次诊断——不允许把诊断本身变成新的故障。
        """
        try:
            if ctx is None or getattr(ctx, "adapter", None) is None:
                return
            # 只对"引擎通道"类错误码上报：算例校验不过、请求非法、约束冲突等
            # 与引擎健康无关，每次都去探测只会让失败路径平白多花十几秒。
            if code not in (ErrorCode.TIMEOUT, ErrorCode.ENGINE_ERROR):
                return
            if getattr(ctx.adapter, "process_isolated", False):
                # The failed native worker belongs to this task and is already
                # reaped. Probing/restarting the whole HTTP backend would harm
                # unrelated sessions and cannot rescue an invalid old circuit.
                ctx.save_json("engine_failure_diagnostics.json", ctx.adapter.engine_diagnostics())
                return
            from ..runtime import watchdog

            watchdog.note_engine_failure(
                ctx.adapter, f"{getattr(code, 'value', code)}: {message}"
            )
        except Exception:  # noqa: BLE001 - 诊断失败不得影响任务收尾
            pass

    # ---------------------------------------------------------------- run
    def _write_progress(self, task_id: str, stage: str, label: str, percent: int) -> None:
        """把当前执行阶段写到 outputs/_current_progress.json，供 UI 轮询显示。

        本服务同一时刻只跑一个任务（HTTP 请求同步等待），所以用「单一当前进度」
        就够了，不必为每个 task_id 建索引。写失败一律忽略——进度只是提示，
        不能影响主流程。
        """
        path = os.path.join(self.config.workspace_root, "_current_progress.json")
        data = {
            "task_id": task_id,
            "stage": stage,
            "label": label,
            "percent": max(0, min(100, int(percent))),
            "updated_at": time.time(),
            "done": False,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError:
            pass

    def _clear_progress(self, task_id: str) -> None:
        path = os.path.join(self.config.workspace_root, "_current_progress.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {"task_id": task_id, "stage": "done", "label": "已完成",
                     "percent": 100, "updated_at": time.time(), "done": True},
                    f, ensure_ascii=False,
                )
        except OSError:
            pass

    # -------------------------------------------------- 状态增量落盘（缺口 2）
    def _write_state_dir(self, workspace: str) -> str:
        return os.path.join(workspace, "state.json")

    def _persist_state(self, state: AgentState, workspace: str, stage: str) -> None:
        """把当前状态写入 `state.json`，附带"最后落盘阶段"。

        ## 为什么必须增量写

        原来 `state.json` 只在**整个任务跑完之后**写一次。于是进程在中途被 Kill
        （崩溃 / 重启 / 原生库段错误）时，磁盘上**根本没有这个任务的 state.json**，
        `run_metrics` 只能记成 `unknown` —— 指标看板 21 个 unknown（5.5%）
        全部源于此。这不是统计口径问题，是**观测数据缺失**。

        现在改成每个阶段入口都写一次。崩溃后磁盘上留下的是"最后成功到达的阶段"，
        这既是排查线索，也让 `run_metrics` 可以把这种任务判为 `interrupted`
        而不是 `unknown`。

        ## 为什么写失败绝不能抛

        状态落盘是观测设施。磁盘满 / 只读 / 权限不足时若抛异常，
        观测设施就变成了新的故障源——比没有观测更糟。故一律静默降级。

        ## 原子写

        用「写临时文件 + os.replace」而不是直接覆盖：直接覆盖时若恰好在此刻
        被 Kill，会留下**半截 JSON**，比没有文件更难排查（解析失败 → 又变 unknown）。
        os.replace 在 Windows/POSIX 上都是原子的。
        """
        path = self._write_state_dir(workspace)
        try:
            payload = state.model_dump(mode="json")
            # 记录最后落盘阶段与时间：崩溃后这就是"遗言"。
            payload["_persisted_stage"] = stage
            payload["_persisted_at"] = time.time()
            payload["_persisted_at_str"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime()
            )
            tmp = path + ".tmp"
            os.makedirs(workspace, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, ValueError):
                    pass
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001 - 观测设施绝不阻塞主流程
            pass

    def _mark_progress_stage(
        self, state: AgentState, workspace: str,
        task_id: str, stage: str, label: str, percent: int,
    ) -> None:
        """一次调用同时更新「UI 进度」与「持久状态」。

        合并成单个方法是有意的：两者必须成对推进。如果分两处调用，
        将来新增阶段时极易只加其中一处，于是崩溃后 state.json 停留的
        阶段与用户看到的进度条对不上——排查时会被误导。
        """
        self._write_progress(task_id, stage, label, percent)
        self._persist_state(state, workspace, stage)

    def run(
        self,
        request: UserRequest,
        cli_callback: Callable[[str], None] | None = print,
        *,
        on_task_started: Callable[[str], None] | None = None,
    ) -> AgentResponse:
        task_id = f"pf-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        workspace = os.path.join(self.config.workspace_root, task_id)
        os.makedirs(workspace, exist_ok=True)

        tracer = Tracer(os.path.join(workspace, "trace.json"), cli_callback)
        state = AgentState(task_id=task_id)
        # "init" 阶段的落盘必须在锁之前：若卡在锁等待/锁获取失败，
        # 磁盘上也应留下这个任务的痕迹（否则又是 unknown）。
        # 锁前只留该任务的独立痕迹；被拒绝的任务不能覆盖活动任务进度。
        self._persist_state(state, workspace, "init")

        # ---- 全进程任务串行锁 ----
        # 默认COM已有任务专属进程，服务仍保留单计算容量：全局进度与模型计量
        # 属于一个活动任务，旧thread诊断通道也必须串行。批量排队交由持久调度器。
        if not _TASK_LOCK.acquire(blocking=False):
            raise TaskBusyError(
                f"已有任务正在执行（{_TASK_LOCK_OWNER.get('task_id') or '未知'}）"
            )
        _TASK_LOCK_OWNER["task_id"] = task_id
        _TASK_LOCK_OWNER["since"] = time.time()
        try:
            if on_task_started is not None:
                on_task_started(task_id)
            self._mark_progress_stage(state, workspace, task_id, "init", "正在准备任务…", 5)

            state.version_set = {
                "adapter": ADAPTER_VERSION,
                "defaults": self.config.defaults.version,
                # 提示词版本化：记录本次任务实际使用的每一版提示词（含内容指纹），
                # 复查"这个结果是哪版提示词产生的"时不必再翻代码。
                "prompts": " ".join(f"{k}={v}" for k, v in prompt_versions().items()),
            }
            # LLM 消耗计量：本任务期间累计，收尾写入 llm_usage.json
            llm_trace_mark: int | None = None
            try:
                from ..runtime.model_client import reset_usage, trace_mark

                reset_usage()
                # LLM 调用留痕起点（P3）：收尾用 trace_since(mark) 精确取本任务的调用。
                # 用 mark/since 而不是 reset，是为了不误伤聊天层——意图判定与知识库问答
                # 发生在任务开始之前，它们不属于任何任务，只能靠进程级 jsonl 日志留痕。
                llm_trace_mark = trace_mark()
            except Exception:
                pass

            if request.message:
                tracer.emit(
                    EventType.USER, stage="receive_request", message=request.message,
                    detail={"text": request.message},
                )

            task_spec: PowerFlowTaskSpec | None = None
            ctx: TaskContext | None = None
            verification = None
            evidence: list = []
            explanation = ""
            explanation_source = "rule"

            try:
                # ---- receive_request + resolve_constraints（任务解析） ----
                self._mark_progress_stage(state, workspace, task_id, "parse", "正在理解任务意图…", 10)
                # ⚠️ 下面这句要调一次大模型（实测 5~11 秒）。此前这段时间**没有任何事件**：
                # 过程面板一直空着，等解析完才把后面所有阶段一次性涌出来 ——
                # 观感就是"过程是一下子生成的"。所以耗时的**开始**也要报一条，
                # 让界面知道"现在卡在哪一步、大约要多久"，而不是干等。
                tracer.emit(
                    EventType.UNDERSTAND, stage="parse_intent",
                    message="正在理解任务意图：识别算例、结果范围与参数约束…",
                    status=EventStatus.RUNNING,
                )
                parsed = self.model_client.parse_request(request)
                task_spec = self._build_task_spec(task_id, parsed, request)
                self._mark_progress_stage(
                    state, workspace, task_id, "constraints", "已确定算例与约束", 20
                )
                tracer.emit(
                    EventType.UNDERSTAND, stage="parse_intent",
                    message=f"意图已解析（解析器：{parsed.parser}）：{task_spec.user_goal}",
                    detail={
                        "parser": parsed.parser,
                        "user_goal": task_spec.user_goal,
                        "result_scopes": [s.value for s in task_spec.result_scopes],
                    },
                )
                tracer.emit(
                    EventType.UNDERSTAND, stage="receive_request",
                    message=(
                        f"目标：{task_spec.user_goal}，结果范围 "
                        f"{', '.join(s.value for s in task_spec.result_scopes)}"
                        f"（解析器: {parsed.parser}）"
                    ),
                    detail={
                        "user_goal": task_spec.user_goal,
                        "parser": parsed.parser,
                        "result_scopes": [s.value for s in task_spec.result_scopes],
                    },
                )
                hard_desc = [f"result_scopes={task_spec.constraints.hard.get('result_scopes')}"]
                for k in ("max_iterations", "tolerance"):
                    if k in task_spec.constraints.hard:
                        hard_desc.append(f"{k}={task_spec.constraints.hard[k]}")
                soft = task_spec.constraints.soft.get("focus_objects")
                tracer.emit(
                    EventType.CONSTRAINTS, stage="resolve_constraints",
                    message=f"硬约束：{'；'.join(hard_desc)}"
                    + (f"；软约束：解释重点={soft}" if soft else ""),
                    detail={"hard": "；".join(hard_desc), "soft": soft},
                )

                # ---- request_guard ----
                self._mark_progress_stage(state, workspace, task_id, "guard", "正在校验请求…", 22)
                self.guard.check_request(task_spec, raw_message=request.message or "")
                tracer.emit(EventType.GUARD, stage="request_guard", message="请求门校验通过")
                task_spec.freeze()
                adapter_backend = (getattr(self.config, "engine_backend", "com") or "com").lower()
                if adapter_backend == "com":
                    isolation = getattr(self.config, "com_isolation", "process")
                    if isolation == "process":
                        from ..engines.opendss_process_adapter import OpenDSSProcessAdapter
                        adapter_class = OpenDSSProcessAdapter
                        adapter_kwargs = {"task_id": task_id}
                    elif isolation == "thread":
                        from ..engines.opendss_com_adapter import OpenDSSComAdapter
                        adapter_class = OpenDSSComAdapter
                        adapter_kwargs = {}
                    else:
                        raise ValueError(f"不支持的 COM 隔离方式：{isolation}")
                    engine_adapter: Any = adapter_class(
                        solve_timeout_s=self.config.solve_timeout_s,
                        com_timeout_s=getattr(self.config, "com_timeout_s", 120.0),
                        com_quick_timeout_s=getattr(self.config, "com_quick_timeout_s", 45.0),
                        **adapter_kwargs,
                    )
                elif adapter_backend == "cli":
                    # 第三条通道（`OpenDSScmd.exe`，官方独立进程）。
                    # 定位：**对照 / 离线验证**，不是生产默认 —— 默认仍是 com。
                    # 它跑在独立进程里，所以挂死可强杀；代价是 CSV 只有 5~6 位有效数字。
                    from ..engines.opendss_cli_adapter import OpenDSSCliAdapter

                    engine_adapter = OpenDSSCliAdapter(
                        solve_timeout_s=self.config.solve_timeout_s,
                        cli_timeout_s=getattr(self.config, "cli_timeout_s", 120.0),
                        cli_quick_timeout_s=getattr(
                            self.config, "cli_quick_timeout_s", 45.0
                        ),
                        cli_exe=getattr(self.config, "opendss_cli_path", None) or None,
                    )
                else:
                    engine_adapter = OpenDSSAdapter(solve_timeout_s=self.config.solve_timeout_s)
                ctx = TaskContext(
                    task_spec=task_spec,
                    workspace=workspace,
                    config=self.config,
                    adapter=engine_adapter,
                )
                # Cache publication belongs to the complete service lifecycle;
                # cancellation during result explanation cannot leave a cache.
                ctx.defer_cache_commit = True
                # 引擎健康检查（执行门）——带硬超时，避免原生库挂死时请求永不返回
                self._mark_progress_stage(
                    state, workspace, task_id, "engine", "正在检查计算引擎…", 25
                )
                # 启动前先报告阶段；冷启动耗时依机器而异，不用旧环境估计冒充本次实测。
                tracer.emit(
                    EventType.VERIFY, stage="engine_health",
                    message="正在检查计算引擎可用性…",
                    status=EventStatus.RUNNING,
                )
                health = probe_engine_health(ctx.adapter)
                if is_cancelled(task_id):
                    raise ToolFailure(ErrorCode.CANCELLED, TaskStatus.CANCELLED, "任务已被用户取消")
                if not health.available:
                    raise ToolFailure(
                        ErrorCode.ENGINE_ERROR, TaskStatus.SYSTEM_FAILED, health.detail
                    )
                tracer.emit(
                    EventType.VERIFY, stage="engine_health", message="计算引擎可用，可以开始求解"
                )
                state.task_spec_ref = ctx.save_json("task_spec.json", task_spec)

                # ---- 固定工作流 ----
                self._mark_progress_stage(
                    state, workspace, task_id,
                    "solve", "正在编译算例并求解潮流（大算例可能需要几分钟）…", 30,
                )
                # 把 7 个工具阶段的真实进度推给 UI：以前整段只写一次 30%，
                # 求解大算例时右侧执行监控会长时间停在同一个位置，看起来像卡死。
                skill_result = run_fixed_powerflow_skill(
                    ctx, state, tracer, self.guard,
                    progress_cb=lambda stage, label, pct: self._mark_progress_stage(
                        state, workspace, task_id, stage, label, pct
                    ),
                )
                if is_cancelled(task_id):
                    raise ToolFailure(ErrorCode.CANCELLED, TaskStatus.CANCELLED, "任务已被用户取消")
                self._mark_progress_stage(
                    state, workspace, task_id, "verify", "正在校验结果与生成报告…", 96
                )
                verification = skill_result.verification
                evidence = skill_result.evidence

                # ---- 状态判定 ----
                if skill_result.partial:
                    state.status = TaskStatus.PARTIAL
                else:
                    state.status = TaskStatus.SUCCESS

                # ---- 结果解释（证据化表达，数值只来自权威结果） ----
                # 这里同样要调一次大模型（超时 12 秒），先把"开始"报出来，
                # 免得报告生成前又是几秒空白。
                tracer.emit(
                    EventType.RESULT, stage="explain_result",
                    message="正在把结果转述成中文说明（数值全部取自本次求解产物）…",
                    status=EventStatus.RUNNING,
                )
                explanation, explanation_source = self._explain(ctx, task_spec)
                if is_cancelled(task_id):
                    raise ToolFailure(ErrorCode.CANCELLED, TaskStatus.CANCELLED, "任务已被用户取消")
                tracer.emit(
                    EventType.RESULT, stage="explain_result",
                    message="结果说明已生成（来源："
                    + ("大模型" if explanation_source == "llm" else "确定性模板")
                    + "）",
                    detail={"source": explanation_source},
                )
                state.phase = Phase.RESULT_VERIFIED

            except GuardRejection as exc:
                tracer.emit(
                    EventType.GUARD, stage="request_guard",
                    message=f"Rule Guard 拒绝: {exc.message}",
                    status=EventStatus.FAILED, error_code=exc.code.value,
                )
                status = (
                    TaskStatus.INVALID_REQUEST
                    if exc.code
                    in (ErrorCode.REQ_SCHEMA_INVALID, ErrorCode.CONSTRAINT_CONFLICT,
                        ErrorCode.CAPABILITY_UNAVAILABLE, ErrorCode.FIDELITY_NOT_APPROVED,
                        ErrorCode.CASE_PATH_REJECTED)
                    else TaskStatus.VALIDATION_FAILED
                )
                state.fail(exc.code.value, exc.message, "request_guard", status)
            except ToolFailure as exc:
                if exc.code == ErrorCode.CANCELLED or is_cancelled(task_id):
                    verification = self._cancelled_verification(ctx, state)
                    explanation = ""
                # 工作流之前的失败（如引擎健康检查）还没有设置权威状态。
                if state.status is None:
                    stage = "engine_health" if not state.task_spec_ref else "runtime"
                    state.fail(exc.code.value, exc.message, stage, exc.status)
                    tracer.emit(
                        EventType.RESULT, stage=stage, message=exc.message,
                        status=EventStatus.FAILED, error_code=exc.code.value,
                    )
                # 引擎级故障（超时/引擎错误）再交给看门狗复检通道，
                # 由它判定这只是"算例慢"还是"进程已中毒、必须重启"。
                self._report_engine_failure(ctx, exc.code, exc.message)
            except Exception as exc:  # 系统级异常兜底
                if getattr(exc, "code", None) == ErrorCode.CANCELLED or is_cancelled(task_id):
                    verification = self._cancelled_verification(ctx, state)
                    explanation = ""
                else:
                    try:
                        from ..runtime import crashlog
                        crashlog.record(
                            exc, kind="manual",
                            extra={"task_id": task_id, "stage": "runtime", "where": "service.run"},
                        )
                    except Exception:
                        pass
                    tracer.emit(
                        EventType.RESULT, stage="runtime",
                        message=f"系统异常: {exc}", status=EventStatus.FAILED,
                        error_code=ErrorCode.ENGINE_ERROR.value,
                    )
                    state.fail(ErrorCode.ENGINE_ERROR.value, str(exc), "runtime", TaskStatus.SYSTEM_FAILED)
                    self._persist_state(state, workspace, "runtime")
                    self._report_engine_failure(ctx, ErrorCode.ENGINE_ERROR, str(exc))
            finally:
                if ctx is not None:
                    try:
                        if ctx.circuit is not None:
                            ctx.adapter.close(ctx.circuit)
                        elif hasattr(ctx.adapter, "shutdown"):
                            ctx.adapter.shutdown()
                        if getattr(ctx.adapter, "process_isolated", False):
                            ctx.save_json("engine_process_diagnostics.json", ctx.adapter.engine_diagnostics())
                    except Exception:
                        # 清理失败不能阻断报告收尾或泄漏任务锁。
                        pass

            # ---- render_report ----
            if state.status in {TaskStatus.SUCCESS, TaskStatus.PARTIAL} and is_cancelled(task_id):
                verification = self._cancelled_verification(ctx, state)
                explanation = ""
            self._mark_progress_stage(state, workspace, task_id, "report", "正在生成报告…", 92)
            artifacts = dict(ctx.artifacts) if ctx else {}

            # ---- 失败/降级诊断（讲人话）----
            # 必须放在 `_persist_state` 之前：诊断文本要随 state.json 一起落盘，
            # 也要进 artifacts 清单，事后复查时能回答"当时到底怎么跟用户说的"。
            try:
                diagnosis_text, diagnosis_source = self._diagnose(ctx, state)
            except Exception:  # noqa: BLE001 - 诊断失败不得影响任务结论
                diagnosis_text, diagnosis_source = "", "rule"
            if diagnosis_text:
                if isinstance(state.error, dict):
                    state.error["diagnosis"] = diagnosis_text
                    state.error["diagnosis_source"] = diagnosis_source
                try:
                    diag_path = os.path.join(workspace, "diagnosis.json")
                    with open(diag_path, "w", encoding="utf-8") as f:
                        json.dump(
                            {"text": diagnosis_text, "source": diagnosis_source},
                            f, ensure_ascii=False, indent=2,
                        )
                    artifacts["diagnosis.json"] = diag_path
                except Exception:  # noqa: BLE001
                    pass
                tracer.emit(
                    EventType.RESULT, stage="diagnose_failure",
                    message=diagnosis_text,
                    detail={"source": diagnosis_source},
                )

            report_paths = self._render(
                task_id, workspace, task_spec, ctx, state, verification, evidence, explanation
            )
            artifacts.update(report_paths)

            # ---- LLM 消耗计量：写入任务工作区与产物清单（P2）----
            try:
                from ..runtime.model_client import usage_snapshot

                usage = usage_snapshot()
                state.version_set["llm_usage"] = (
                    f"calls={usage['calls']} tokens={usage['total_tokens']} "
                    f"duration={usage['duration_s']}s"
                )
                usage_path = os.path.join(workspace, "llm_usage.json")
                with open(usage_path, "w", encoding="utf-8") as f:
                    json.dump(usage, f, ensure_ascii=False, indent=2)
                artifacts["llm_usage.json"] = usage_path
            except Exception:
                pass

            # ---- LLM 调用留痕：写入任务工作区与产物清单（P3）----
            # `llm_usage.json` 只回答"调了几次、花了多少 token"；本文件回答"说了什么、
            # 有没有被采信"。没有它，报告第 11 节那句话、聊天里那段归纳，事后无法复查
            # 是模型原文、还是被规则模板顶掉的（P0 的裸数值正是这样潜进报告的）。
            try:
                from ..runtime.model_client import trace_since, trace_summary

                records = trace_since(llm_trace_mark) if llm_trace_mark is not None else []
                trace_doc = {
                    "task_id": task_id,
                    "summary": trace_summary(records),
                    "records": records,
                    "note": (
                        "LLM 本次任务的原文级留痕。每条含 purpose（用途）、prompt_digest"
                        "（入参指纹，完整提示词可由 prompts 版本号复现）、模型完整输出、"
                        "耗时与 verdict（accepted=已采用；rejected=输出不合规已退回确定性模板；"
                        "error=调用失败）。数值均出自引擎产物，LLM 只做语言进出。"
                    ),
                }
                trace_path = os.path.join(workspace, "llm_outputs.json")
                with open(trace_path, "w", encoding="utf-8") as f:
                    json.dump(trace_doc, f, ensure_ascii=False, indent=2)
                artifacts["llm_outputs.json"] = trace_path
            except Exception:
                pass

            # 终态落盘：走统一的原子写，与增量写保持同一套格式（含 _persisted_stage），
            # 这样 `run_metrics` 不必区分"增量写的中间态"与"收尾写的终态"。
            self._persist_state(state, workspace, "final")
            state_path = self._write_state_dir(workspace)
            artifacts["state.json"] = state_path

            # ---- 汇总输出目录 outputs/<task_id>/（文档 20） ----
            out_dir = self._publish_outputs(task_id, workspace, artifacts)

            if ctx is not None:
                pending, ctx.pending_solve_cache = ctx.pending_solve_cache, None
                if (pending is not None and state.status == TaskStatus.SUCCESS
                        and verification is not None and verification.passed and not is_cancelled(task_id)):
                    from ..tools.powerflow_tools import RunPowerFlowTool
                    key, raw, solved = pending
                    RunPowerFlowTool()._save_solve_cache(ctx, key, raw, solved)

            status = state.status or TaskStatus.SYSTEM_FAILED
            tracer.emit(
                EventType.RESULT, stage="render_report",
                message=f"status={status.value}, task_id={task_id}, 报告已保存 {out_dir}/",
                detail={"out_dir": out_dir, "task_status": status.value},
                status=EventStatus.SUCCESS if status == TaskStatus.SUCCESS else EventStatus.FAILED,
            )
            tracer.flush()

            return AgentResponse(
                status=status.value,
                task_id=task_id,
                plan="解析任务 -> 算例检查 -> 网络分析 -> 求解策略 -> 潮流计算 -> 结果整理 -> 验证 -> 解释 -> 报告",
                tool_trace=[e.model_dump(mode="json") for e in tracer.events],
                result_ref=(ctx.artifacts.get("powerflow_result.json") if ctx else None),
                verification=verification.model_dump(mode="json") if verification else None,
                evidence=[e.model_dump(mode="json") for e in evidence],
                artifacts=artifacts,
                report=report_paths.get("report.md"),
                output_dir=out_dir,
                error=state.error,
                explanation=explanation,
                explanation_source=explanation_source,
            )
        finally:
            # 锁覆盖完整任务生命周期，启动/报告异常也必须释放。
            try:
                self._clear_progress(task_id)
            finally:
                _TASK_LOCK_OWNER["task_id"] = None
                _TASK_LOCK_OWNER["since"] = None
                _TASK_LOCK.release()

    # ---------------------------------------------------------- TaskSpec
    def _build_task_spec(
        self, task_id: str, parsed: ParsedTask, request: UserRequest
    ) -> PowerFlowTaskSpec:
        # 「调用方显式指定的算例」优先于「LLM 从文本里猜的算例」。
        #
        # 真实 bug：聊天入口在 chat_api 里已把算例解析成 Master.dss 绝对路径，
        # 但这里的旧逻辑是 `parsed.case_ref or request.case_ref`，导致
        # LLM 意图解析从消息文本里提取的**纯目录名**（如 "case17_structured_hard_v3"）
        # 会覆盖掉那条正确路径。而 load_case_bundle 只在 cases_root 下按名字找，
        # 上传目录不在其内 → DATA_UNAVAILABLE，或更糟：悄悄退回去算别的算例。
        #
        # 原则：已经拿到可直接打开的文件就不要再让模型猜。
        request_ref = request.case_ref
        request_ref_is_file = bool(
            request_ref and os.path.isabs(request_ref) and os.path.isfile(request_ref)
        )
        if request_ref_is_file:
            case_ref = request_ref
        else:
            case_ref = parsed.case_ref or request_ref
        if not case_ref:
            raise GuardRejection(
                ErrorCode.REQ_SCHEMA_INVALID,
                "缺少算例引用：请通过 --dss 参数或在消息中提供 Master.dss 路径 / 内置算例名",
            )
        sc = SolverConfig()
        if parsed.solver_config.get("max_iterations") is not None:
            sc.max_iterations = int(parsed.solver_config["max_iterations"])
        if parsed.solver_config.get("tolerance") is not None:
            sc.tolerance = float(parsed.solver_config["tolerance"])
        return PowerFlowTaskSpec(
            task_id=task_id,
            case_ref=case_ref,
            scenario_ref=request.scenario_ref,
            user_goal=parsed.user_goal,
            solver_config=sc,
            result_scopes=[ResultScope(s) for s in parsed.result_scopes],
            output_formats=[OutputFormat(f) for f in parsed.output_formats],
            constraints=build_user_constraints(parsed, request),
            focus_objects=parsed.focus_objects,
        )

    # ---------------------------------------------------------- 解释生成
    def _explain(self, ctx: TaskContext, task_spec: PowerFlowTaskSpec) -> tuple[str, str]:
        r = ctx.canonical_result
        assert r is not None
        ss, ls = r.system_summary, r.loss_summary
        facts: dict[str, Any] = {
            "converged": r.solver_status.converged,
            "iterations": r.solver_status.iterations,
            "solution_count": getattr(r.solver_status, "raw_solution_count", None),
            "total_iterations": getattr(r.solver_status, "total_iterations", None),
            "min_voltage_pu": ss.min_voltage_pu,
            "min_voltage_bus": ss.min_voltage_bus,
            "max_voltage_pu": ss.max_voltage_pu,
            "max_voltage_bus": ss.max_voltage_bus,
            "total_load_kw": ss.total_load_kw,
            "total_loss_kw": ls.total_loss_kw,
            "loss_pct_of_load": ls.loss_pct_of_load,
            "focus_objects": task_spec.focus_objects,
            "voltage_criteria": f"仿真默认阈值 {self.config.defaults.version}: "
            f"{self.config.defaults.voltage_min_pu}~{self.config.defaults.voltage_max_pu} pu",
        }
        # 确定性模板（fallback，保证无 LLM 时也有证据化解释）
        parts = []
        if ss.min_voltage_pu is not None:
            if ss.min_voltage_pu < self.config.defaults.voltage_min_pu:
                judge = "低于仿真判据下限"
            elif ss.min_voltage_pu > self.config.defaults.voltage_max_pu:
                judge = "高于仿真判据上限"
            else:
                judge = "在仿真判据区间内"
            parts.append(
                f"全网最低电压出现在母线 {ss.min_voltage_bus}，为 {ss.min_voltage_pu:.4f} pu，{judge}。"
            )
        if ls.total_loss_kw is not None:
            pct = f"，占总负荷 {ls.loss_pct_of_load:.2f}%" if ls.loss_pct_of_load is not None else ""
            parts.append(f"系统总网损 {ls.total_loss_kw:.2f} kW{pct}。")
        parts.append(
            "以上数值均来自 OpenDSS 求解并经独立 Verifier 校验，判据为当前仿真判据，非工程审批限值。"
        )
        fallback = "".join(parts)
        return self.model_client.explain(facts, fallback)

    # ------------------------------------------------------ 失败/降级诊断
    def _read_replan(self, ctx: TaskContext | None) -> dict[str, Any] | None:
        """读取本次任务的重规划记录（未发生过重规划则返回 None）。"""
        if ctx is None:
            return None
        path = (getattr(ctx, "artifacts", {}) or {}).get("replan.json", "")
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 - 读不到就当作没发生过
            return None

    def _diagnosis_fallback(self, replan: dict[str, Any]) -> str:
        """确定性诊断文案（无 LLM 时的兜底，保证"为什么"这句话永远有）。

        注意它已经把 `replan.reason` 一并说出来了 —— 那是**模型在
        `replan_choose` 里给出的判断依据**，改动前只写进 replan.json，
        用户界面里完全看不到。哪怕第二次模型调用不可用，这条信息也不该丢。
        """
        strategy = str(replan.get("strategy") or "")
        action_cn = {
            "relax_numeric": "放宽迭代次数与收敛容差后重算（不改变网络模型结构）",
            "downgrade_pv_to_pq": "把电压控制型分布式电源降级为恒功率注入后重编译",
        }.get(strategy, strategy or "受控重规划")
        parts = [f"本次求解未能一次收敛，系统按受控重规划流程处理：{action_cn}。"]
        note = str(replan.get("note") or "").strip()
        if note:
            parts.append(note if note.endswith(("。", "！", "？")) else note + "。")
        reason = str(replan.get("reason") or "").strip()
        if reason:
            parts.append(f"选择该动作的判断依据：{reason}。")
        return "".join(parts)

    def _diagnose(self, ctx: TaskContext | None, state: AgentState) -> tuple[str, str]:
        """把"未收敛 → 受控重规划"这段既有事实讲成人话。返回 (文本, 来源)。

        为什么需要它：模型在 `replan_choose` 里给出的判断依据此前**只写进
        `replan.json`**，用户看到的永远是同一句写死的模板 —— 等于"模型判了，
        却没告诉用户为什么这么判"。这里补的是**解释**，不是决策：不改任何数值、
        不进证据链、不参与发布门（与 `_explain` 的产物不同，后者会进报告正文）。

        只在**确实发生过重规划**时生成；没发生过就没有"诊断"可讲，返回空串。
        """
        replan = self._read_replan(ctx)
        if not replan:
            return "", "skip"
        err = state.error if isinstance(state.error, dict) else {}
        facts: dict[str, Any] = {
            "case_ref": getattr(getattr(ctx, "task_spec", None), "case_ref", None),
            "final_status": state.status.value if state.status else "",
            "error_code": str(err.get("code") or ""),
            "error_message": str(err.get("message") or "")[:200],
            "replan": replan,
        }
        fallback = self._diagnosis_fallback(replan)
        try:
            return self.model_client.diagnose_failure(facts, fallback)
        except Exception:  # noqa: BLE001 - 诊断失败绝不能影响任务结论
            return fallback, "rule"

    # ---------------------------------------------------------- 报告渲染
    def _render(
        self, task_id, workspace, task_spec, ctx, state, verification, evidence, explanation
    ) -> dict[str, str]:
        paths: dict[str, str] = {}
        reports_dir = os.path.join(workspace, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        status = state.status or TaskStatus.SYSTEM_FAILED
        defaults = self.config.defaults

        # 失败场景：算例/验证失败时报告停止在对应阶段（文档 16.2）
        from ..schemas.case import CanonicalDistributionCase
        from ..schemas.solver import OpenDSSSolverProfile

        case = ctx.case if ctx and ctx.case else CanonicalDistributionCase(
            case_id="unknown", case_fingerprint="",
            master_dss_ref=task_spec.case_ref if task_spec else "",
            dependency_manifest={"master_dss": "", "entries": []},
        )
        solver_profile = (
            ctx.solver_profile
            if ctx and ctx.solver_profile
            else OpenDSSSolverProfile(profile_id="n/a")
        )
        result = ctx.canonical_result if ctx else None

        fmts = task_spec.output_formats if task_spec else [OutputFormat.MD]
        # ⚠️ render_markdown 必须包在 try 内。
        # 它与下面的 HTML/CSV 一样属于"渲染"，而本方法的既有约定是
        # **渲染失败降级为 REPORT_ERROR 并保留已验证的 JSON 产物**（文档 9.4）。
        # 之前它写在 try 之外，于是渲染层任何异常都会直接冒泡成 HTTP 500，
        # 前端只看到"服务器错误"、拿不到失败原因，JSON 产物虽然写好了却无从知晓。
        # 实测踩过：早期失败的算例（compile 校验拒绝）在第 8 节渲染时抛
        # AttributeError，把一次"应当明确失败"的请求变成了 500。
        try:
            md_text = render_markdown(
                task_spec or PowerFlowTaskSpec(
                    task_id=task_id, case_ref="", result_scopes=[], output_formats=[]
                ),
                case, solver_profile, result, verification, evidence, status,
                defaults, explanation=explanation, error=state.error,
            )
            if OutputFormat.MD in fmts or True:  # report.md 始终生成（CLI 摘要 + 复盘）
                p = os.path.join(reports_dir, "report.md")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(md_text)
                paths["report.md"] = p
            if OutputFormat.HTML in fmts:
                p = os.path.join(reports_dir, "report.html")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(render_html(md_text, task_id))
                paths["report.html"] = p
            if OutputFormat.CSV in fmts and result is not None:
                paths["results.csv"] = render_csv(
                    result, os.path.join(reports_dir, "results.csv")
                )
        except Exception as exc:
            # REPORT_ERROR：保留已验证 JSON 等产物（文档 9.4）
            paths["report_error"] = f"{ErrorCode.REPORT_ERROR.value}: {exc}"
        return paths

    # ---------------------------------------------------------- 输出发布
    def _publish_outputs(self, task_id: str, workspace: str, artifacts: dict[str, str]) -> str:
        out_dir = os.path.join(self.config.outputs_root, task_id)
        os.makedirs(out_dir, exist_ok=True)
        copied: set[str] = set()
        # 把 artifacts 字典中指向实际文件的路径全部复制到 outputs/<task_id>/
        for name, src in artifacts.items():
            if not src or not isinstance(src, str) or not os.path.isfile(src):
                continue
            dst = os.path.join(out_dir, os.path.basename(src))
            if os.path.abspath(src) == os.path.abspath(dst):
                continue
            try:
                shutil.copy2(src, dst)
                copied.add(name)
            except Exception:
                continue
        # 兜底：即使 artifacts 字典缺项，也复制核心产物
        for name in (
            "powerflow_result.json", "verification.json", "evidence.json", "trace.json",
            # 两个 LLM 审计文件同样兜底：它们回答"模型介入到什么程度"，
            # 缺了就没法复查某个数值/某句话的来路（P2 计量 + P3 留痕）。
            "llm_usage.json", "llm_outputs.json",
        ):
            if name in copied:
                continue
            src = os.path.join(workspace, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(out_dir, name))
        # 报告目录兜底
        reports_src = os.path.join(workspace, "reports")
        if os.path.isdir(reports_src):
            for fn in os.listdir(reports_src):
                src = os.path.join(reports_src, fn)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(out_dir, fn))
        return out_dir

    # ---------------------------------------------------------- inspect / report
    def inspect(self, task_id: str) -> dict[str, Any]:
        ws = os.path.join(self.config.workspace_root, task_id)
        if not os.path.isdir(ws):
            # 兼容 outputs 目录
            ws = os.path.join(self.config.outputs_root, task_id)
        # ⚠️ 诊断轨迹落在 `workspace_root/_diagnostics/<id>`：早期失败
        # （上传/转换阶段被拒）也要有可回看的执行轨迹，而它没有走过 run()，
        # 不在 workspace 根下。漏了这一分支，前端回看就是 404 ——
        # 表现为"实时也没有、回看也没有"，比不做更糟（会让人以为是竞态）。
        if not os.path.isdir(ws):
            ws = os.path.join(self.config.workspace_root, "_diagnostics", task_id)
        info: dict[str, Any] = {"task_id": task_id, "workspace": ws}
        for name in ("state.json", "task_spec.json", "verification.json"):
            p = os.path.join(ws, name)
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    info[name.replace(".json", "")] = json.load(f)
        return info
