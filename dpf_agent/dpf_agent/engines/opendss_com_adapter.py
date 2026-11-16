"""OpenDSS COM Adapter —— 基于本机独立安装的 OpenDSS（OpenDSSEngine.DSS COM 组件）。

背景：dss-python（dss_capi.dll）在当前机器环境下 import 即原生崩溃（0xC0000005 /
0xE0465043），且与版本无关。本机独立安装的 OpenDSS v11 引擎验证可用，其 COM 接口
（OpenDSSEngine.DSS）已注册，API 属性名与 dss-python 几乎一致。

实现要点：
1. 专用 COM 工作线程：COM 对象跨线程访问会因套间规则失败，因此引擎实例只在
   工作线程内创建和使用，所有调用经队列转发（等价于把原生调用串行化）。
2. 此内部线程通道超时抛 AdapterError(TIMEOUT)，本身无法杀原生调用。
   ApplicationService 默认由 opendss_process_adapter 包在任务专属进程，
   超时、取消或崩溃才可真正回收该原生进程与其线程。
3. 集合遍历按 COM 风格重写：AllBusNames/AllElementNames + SetActiveBus/
   SetActiveElement + ActiveBus/ActiveCktElement；元素集合用 First/Next。

接口契约与 opendss_adapter.OpenDSSAdapter 完全一致。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from typing import Any, Callable

from ..schemas.enums import ErrorCode, ResultScope
from ..schemas.solver import OpenDSSSolverProfile
from .circuit_inventory import CircuitInventory, collect_circuit_inventory
from .terminal_observations import (
    collect_bus_observation, collect_terminal_data, collect_frequency_context,
    configure_registry_isolation, read_engine_option,
)
from .circuit_validation import (
    CircuitObservation,
    normalize_bus_name,
    validate_compiled_circuit,
)
from .opendss_adapter import (
    ADAPTER_VERSION,
    AdapterError,
    CircuitHandle,
    EngineHealth,
    OpenDSSAdapter,
    OpenDSSRawResult,
    SolverExecutionResult,
)

PROG_ID = "OpenDSSEngine.DSS"

logger = logging.getLogger(__name__)


def _transformer_endpoints_from_source(case_workspace: str) -> dict[str, tuple[str, ...]]:
    """Legacy source-only lookup, retained for compatibility with old tooling.

    Copyright (c) yangshaopeng. The COM collector no longer calls this helper:
    terminal metadata comes from an explicitly selected, identity-checked
    ActiveCktElement. Source text cannot replace observed NodeOrder/BusNames.
    """
    if not case_workspace or not os.path.isdir(case_workspace):
        logger.warning(
            "[transformer-endpoints] 目录不可用，跳过端点采集：%r", case_workspace)
        return {}
    try:
        from .dss_source import parse_dss_directory

        facts = parse_dss_directory(case_workspace)
    except Exception as exc:  # noqa: BLE001 - 端点是"锦上添花"，解析失败不许拖垮求解
        logger.warning("[transformer-endpoints] 源解析失败 %s：%s", case_workspace, exc)
        return {}
    out = facts.transformer_endpoints()
    # ⚠️ 用 warning 而不是 info：这个诊断**只在真机出问题时才看**，
    # 而 info 级在真机后端日志里根本不输出（实测 0 条）—— 留 info 等于没留。
    logger.warning("[transformer-endpoints] %s 解析到 %d 台变压器端点",
                   case_workspace, len(out))
    return out


def _collect_com_observation(eng: Any) -> CircuitObservation:
    """从 COM 引擎收集电路观察量（引擎相关部分；判定见 `circuit_validation`）。

    **为什么这件事必须各写一份**：COM 的属性模型与 dss-python 不同 ——
    它没有 `CktElements` / `Buses` 这类可直接迭代的集合，只能取
    `AllElementNames` / `AllBusNames`，再逐个 `SetActiveElement` /
    `SetActiveBus` 读 `ActiveCktElement` / `ActiveBus`。

    但"收集"的差异不该传染给"判定"：校验规则（母线引用、零基准母线）
    单点定义在 `circuit_validation.validate_compiled_circuit`，
    与 dss-python 通道共用，将来的 CLI 通道也一样复用。
    """
    ckt = eng.ActiveCircuit
    all_bus_names = [str(n) for n in (ckt.AllBusNames or ())]
    bus_names = frozenset(normalize_bus_name(n) for n in all_bus_names)

    endpoints: list[tuple[str, str]] = []
    for name in (ckt.AllElementNames or ()):
        ckt.SetActiveElement(name)
        ce = ckt.ActiveCktElement
        if not ce.Enabled:
            continue
        cname = str(ce.Name)
        for bus in ce.BusNames or ():
            endpoints.append((cname, str(bus)))

    bus_base_kv: list[tuple[str, Any]] = []
    for name in all_bus_names:
        ckt.SetActiveBus(name)
        bus = ckt.ActiveBus
        bus_base_kv.append((str(bus.Name), bus.kVBase))

    vsource_buses: set[str] = set()
    vs = ckt.Vsources
    idx = vs.First
    while idx > 0:
        for b in ckt.ActiveCktElement.BusNames or ():
            base = normalize_bus_name(b)
            if base:
                vsource_buses.add(base)
        idx = vs.Next

    return CircuitObservation(
        bus_count=int(ckt.NumBuses),
        bus_names=bus_names,
        endpoints=tuple(endpoints),
        bus_base_kv=tuple(bus_base_kv),
        vsource_buses=frozenset(vsource_buses),
    )


class _ComWorker:
    """单线程 COM 执行器：引擎实例固定在后台线程，调用按队列串行执行。

    「中毒自愈」机制（重要）：
    引擎调用超时后，卡住的那次 COM 调用**仍在工作线程里执行**，并且它占着当时那个
    引擎实例。如果继续复用同一个线程与引擎，后续每次调用都会排在已卡死的引擎后面，
    次次超时——实测表现为一次异常之后连续多个任务各卡 120s，整机只能靠重启后端恢复。

    因此超时后必须**丢弃并重建**工作线程与引擎实例：
      * 旧线程标记为僵尸，不再往里投递请求（它在 COM 调用返回后自然退出）；
      * 新建线程 + 新 Dispatch 引擎实例，后续调用走干净的通道。

    正常路径（未超时）零额外开销；重建只发生在超时这一异常分支，且代价有界。
    """

    # 重建后等待新引擎就绪的上限（秒）
    RESPAWN_TIMEOUT_S = 30.0

    def __init__(self, prog_id: str = PROG_ID):
        self._prog_id = prog_id
        self._lock = threading.Lock()
        self._req: "queue.Queue[tuple[Callable[[Any], Any], dict]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._init_error: Exception | None = None
        self._generation = 0  # 每次重建 +1，用于日志与断言
        self._respawn_count = 0
        self._last_error: str = ""
        self._start_worker()

    # ------------------------------------------------------------ 线程生命周期
    def _start_worker(self) -> None:
        """（重新）创建队列与工作线程。调用方需自行持有 self._lock。"""
        self._req = queue.Queue()
        self._init_error = None
        self._engine_settings = None
        self._engine_identity = None
        self._thread = threading.Thread(
            target=self._loop,
            name=f"opendss-com-worker-{self._generation}",
            daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:  # pragma: no cover - 线程体
        import pythoncom

        try:
            pythoncom.CoInitialize()
            import win32com.client

            engine: Any | None = win32com.client.Dispatch(self._prog_id)
            # COM defaults to modal error dialogs. In an unattended backend
            # those wait for a desktop click until the parent kills the worker.
            # Surface Error.Number/Description instead, within the same task.
            engine.AllowForms = False
            engine_settings = configure_registry_isolation(engine)
            init_error: Exception | None = None
        except Exception as exc:
            engine, init_error = None, exc
            engine_settings = None
        # 注意：init_error 只写入"本线程自己那一代"的槽位。
        # 不能直接写 self._init_error——重建后该字段属于新线程，
        # 旧线程若延迟写回会污染新代的诊断信息（竞态）。
        req = self._req  # 绑定本次线程自己的队列，重建后旧线程继续消费旧队列
        with self._lock:
            if self._req is req:
                self._init_error = init_error
                self._engine_settings = engine_settings
                self._engine_identity = id(engine) if engine is not None else None
        while True:
            fn, box = req.get()
            try:
                if engine is None:
                    raise AdapterError(
                        ErrorCode.ENGINE_ERROR,
                        f"OpenDSS COM 引擎初始化失败（{self._prog_id}）: {init_error}",
                    )
                box["out"] = fn(engine)
            except BaseException as exc:  # noqa: BLE001 - 原样转交调用线程
                box["err"] = exc
            finally:
                box["evt"].set()

    def _reset(self) -> None:
        """丢弃当前（可能已卡死的）线程与引擎，立即建一套新的。

        必须在超时后调用。旧线程不再接收新请求；它若仍卡在 COM 调用中，
        会随着自身 daemon 属性在进程退出时被回收，不会阻塞后续任务。
        """
        with self._lock:
            old_gen = self._generation
            self._generation += 1
            self._respawn_count += 1
            # 旧线程若卡在 COM 调用里，join 会一直等——这里不 join，直接弃用
            self._start_worker()
            self._last_error = f"引擎在第 {old_gen} 代超时后重建（第 {self._respawn_count} 次）"

    def call(self, fn: Callable[[Any], Any], timeout_s: float | None = None) -> Any:
        # 持锁取"当前代"的队列与线程，保证投递目标与重建动作互斥，
        # 避免请求被投进一条已被判定为僵尸的队列。
        with self._lock:
            req = self._req
        box: dict[str, Any] = {"evt": threading.Event()}
        req.put((fn, box))
        if timeout_s is not None and timeout_s > 0:
            ok = box["evt"].wait(timeout_s)
            if not ok:
                # 关键：超时即判定该线程+引擎不可再用，重建后再抛错。
                # 顺序是先重建、后抛异常：这样调用方捕获异常时，通道已经干净，
                # 下一个任务不会再撞上卡死的引擎。
                self._reset()
                raise AdapterError(
                    ErrorCode.TIMEOUT,
                    f"OpenDSS COM 调用超时 ({timeout_s:.1f}s)，"
                    f"已丢弃并重建引擎实例（第 {self._respawn_count} 次）",
                )
        else:
            box["evt"].wait()
        if "err" in box:
            raise box["err"]
        return box["out"]

    # ---------------------------------------------------------------- 自愈诊断
    def diagnostics(self) -> dict[str, Any]:
        """供健康检查 / 指标看板观测引擎重建情况。"""
        return {
            "generation": self._generation,
            "respawn_count": self._respawn_count,
            "last_error": self._last_error,
            "init_error": str(self._init_error) if self._init_error else "",
        }


class OpenDSSComAdapter(OpenDSSAdapter):
    """基于 OpenDSSEngine.DSS COM 组件的引擎适配器（接口与 dss-python 版一致）。"""

    def __init__(
        self,
        solve_timeout_s: float = 60.0,
        com_timeout_s: float = 120.0,
        com_quick_timeout_s: float = 45.0,
    ):
        super().__init__(solve_timeout_s=solve_timeout_s)
        self.com_timeout_s = com_timeout_s
        # 编译/校验/设属性等"应当很快"的调用用较短超时：
        # 一旦卡住，快速失败并重建引擎，而不是干等 120s（一次卡死的代价直接砍到 1/3）。
        self.com_quick_timeout_s = com_quick_timeout_s
        self._worker = _ComWorker()

    # ------------------------------------------------ 引擎上下文执行（COM 转发）
    def run_with_engine(self, fn: Any, timeout_s: float | None = None) -> Any:
        """把 fn(engine) 转发到 COM 工作线程执行（跨套间安全）。"""
        return self._worker.call(fn, timeout_s if timeout_s is not None else self.com_timeout_s)

    # ------------------------------------------------------------------- 自愈诊断
    def engine_diagnostics(self) -> dict[str, Any]:
        """引擎重建次数等自愈信息（供健康检查 / 指标观测）。"""
        return self._worker.diagnostics()

    # ---------------------------------------------- circuit inventory（网络画像）
    def collect_circuit_inventory(self) -> CircuitInventory:
        """收集网络画像所需的引擎观察量（在 COM 工作线程内执行）。

        ⚠️ 必须经 `_worker.call` 转发：引擎对象只能在创建它的线程 / 套间内访问，
        在调用线程直接读 `ckt.*` 会因套间规则失败，或拿到不可靠的值。
        """
        return self._worker.call(collect_circuit_inventory, self.com_quick_timeout_s)

    def collect_device_connections(self):
        """Read actual device parameters in the owning COM thread only."""
        from .device_connections import collect_device_connections
        return self._worker.call(lambda eng:collect_device_connections(eng,
            evidence_dir=getattr(self,"_native_observation_dir",None)), self.com_quick_timeout_s)

    # ------------------------------------------------------------------ health
    def health(self) -> EngineHealth:
        try:
            version = str(self._worker.call(lambda eng: eng.Version, 30.0)).strip()
            self._last_health = EngineHealth(
                available=True,
                engine_version=f"OpenDSS COM {version}",
            )
        except Exception as exc:  # pragma: no cover - 引擎缺失/注册失败
            self._last_health = EngineHealth(
                available=False, detail=f"OpenDSS COM 引擎不可用: {exc}"
            )
        return self._last_health

    # ------------------------------------------------------------ compile_case
    def compile_case(self, case_workspace: str, master_dss: str) -> CircuitHandle:
        from pathlib import Path
        self._native_observation_dir = Path(case_workspace).absolute().parent / "native_observation_evidence"
        from .fidelity_mapping import read_fidelity_mapping
        self._fidelity_mapping = read_fidelity_mapping(case_workspace)
        handle = CircuitHandle(
            handle_id=uuid.uuid4().hex[:12],
            case_workspace=case_workspace,
            master_dss=master_dss,
        )
        detail = self._worker.call(
            lambda eng: self._compile_and_validate(eng, master_dss),
            self.com_quick_timeout_s,
        )
        if detail:
            low = detail.lower()
            if "linecode" in low or "not found" in low or "does not exist" in low:
                raise AdapterError(
                    ErrorCode.DATA_UNAVAILABLE,
                    f"OpenDSS Compile 依赖不可用：{detail}",
                )
            raise AdapterError(ErrorCode.VALIDATION_FAILED, f"OpenDSS Compile 失败: {detail}")
        handle.compiled = True
        # 复用该字段保存"能够继续操作引擎的入口"。
        # 注意：**不能**存 self._worker——worker 会在超时自愈时被整体替换，
        # 存它等于存一个随时可能失效的引用。存适配器本身，则始终指向当前代引擎。
        handle.dss_ctx = self
        return handle

    def _compile_and_validate(self, eng: Any, master_dss: str) -> str:
        """执行 compile，再按**单点规则**校验电路；返回错误描述（空串=通过）。

        ⚠️ 校验规则**不在本文件** —— 它在 `engines/circuit_validation.py`，
        与 dss-python 通道（以及将来的 CLI 通道）共用同一份判定。
        本方法只负责两件引擎相关的事：跑 compile 命令、收集观察量。

        此前这里和基类各写了一份语义等价、API 形态不同的规则，
        改一份不影响另一份 —— 那正是「口径写两遍」的隐患
        （本项目已在元件求和口径上吃过同样的亏）。
        """
        # Copyright (c) yangshaopeng. Prevent host configuration persistence,
        # without choosing or changing any electrical frequency in the model.
        worker = getattr(self, "_worker", None)
        startup = (getattr(worker, "_engine_settings", None)
                   if getattr(worker, "_engine_identity", None) == id(eng) else None)
        context = {"registry_isolation": {"startup": startup}}
        compiled = False
        compile_error = ""
        try:
            try:
                configure_registry_isolation(eng)
            except Exception as exc:
                raise AdapterError(ErrorCode.ENGINE_ERROR,
                                   "OpenDSS host-setting persistence guard failed before compile") from exc
            eng.ClearAll()
            context["before_compile"] = read_engine_option(eng, "defaultbasefrequency")
            eng.Text.Command = f'compile "{master_dss}"'
            err_num = int(eng.Error.Number)
            # Preserve this before any diagnostic command replaces the engine
            # error description. A failed read-only query consumes its own error.
            if err_num:
                compile_error = eng.Error.Description or f"error {err_num}"
            else:
                compiled = True
        finally:
            context["after_compile"] = read_engine_option(eng, "defaultbasefrequency")
            observed = read_engine_option(eng, "registryupdate", boolean=True)
            try:
                enforced = configure_registry_isolation(eng, readback=compiled)
            except Exception as exc:
                raise AdapterError(ErrorCode.ENGINE_ERROR,
                                   "OpenDSS host-setting persistence guard failed") from exc
            context["registry_isolation"] = {
                **enforced, "startup": startup,
                "post_compile_observed": observed, "post_compile_enforced": enforced,
            }
            contexts = getattr(self, "_frequency_contexts", None)
            if contexts is None:
                contexts = self._frequency_contexts = {}
            contexts[id(eng)] = context
        if compile_error:
            return compile_error
        return validate_compiled_circuit(_collect_com_observation(eng))

    # ------------------------------------------------------ apply_solver_profile
    def apply_solver_profile(
        self, circuit: CircuitHandle, solver_profile: OpenDSSSolverProfile
    ) -> None:
        if not solver_profile.frozen:
            raise AdapterError(ErrorCode.REQ_SCHEMA_INVALID, "SolverProfile 未冻结，禁止应用")

        def _apply(eng: Any) -> None:
            sol = eng.ActiveCircuit.Solution
            sol.Mode = 0  # snapshot：稳态确定性单场景
            sol.MaxIterations = solver_profile.iteration_config.max_iterations
            sol.Tolerance = solver_profile.tolerance_config.tolerance
            eng.Text.Command = "set controlmode=off"

        self._worker.call(_apply, self.com_quick_timeout_s)

    # --------------------------------------------------------------------- solve
    def solve(self, circuit: CircuitHandle, *, iteration_limit: int | None = None,
              timeout_s: float | None = None) -> SolverExecutionResult:
        def _solve(eng: Any) -> SolverExecutionResult:
            sol = eng.ActiveCircuit.Solution
            previous_limit = sol.MaxIterations
            if iteration_limit is not None:
                sol.MaxIterations = min(int(previous_limit), int(iteration_limit))
            t0 = time.time()
            try:
                sol.Solve()
            finally:
                sol.MaxIterations = previous_limit
            duration = time.time() - t0
            err_num = int(eng.Error.Number)
            err_text = eng.Error.Description if err_num else ""
            return SolverExecutionResult(
                converged=bool(sol.Converged),
                iterations=int(sol.Iterations),
                error_flag=err_num != 0,
                error_text=err_text,
                duration_s=duration,
            )

        allowed_time = min(self.solve_timeout_s, timeout_s) if timeout_s is not None else self.solve_timeout_s
        wait_limit = min(self.solve_timeout_s + 30.0, timeout_s) if timeout_s is not None else self.solve_timeout_s + 30.0
        result = self._worker.call(_solve, wait_limit)
        if result.duration_s > allowed_time:
            raise AdapterError(
                ErrorCode.TIMEOUT, f"Solve 超时 ({result.duration_s:.1f}s)"
            )
        return result

    # --------------------------------------------------------- extract_raw_result
    def extract_raw_result(
        self, circuit: CircuitHandle, result_scopes: list[ResultScope], *, timeout_s: float | None = None
    ) -> OpenDSSRawResult:
        return self._worker.call(
            lambda eng: self._extract(eng, result_scopes, circuit.case_workspace),
            min(self.com_timeout_s, timeout_s) if timeout_s is not None else self.com_timeout_s,
        )

    def _extract(self, eng: Any, result_scopes: list[ResultScope],
                 case_workspace: str = "") -> OpenDSSRawResult:
        import math

        ckt = eng.ActiveCircuit
        raw = OpenDSSRawResult()
        sol = ckt.Solution
        raw.solver = {
            "converged": bool(sol.Converged),
            "iterations": int(sol.Iterations),
            "mode": "snapshot",
        }
        from .domain_inventory import collect_domain_inventory
        inventory_before = collect_domain_inventory(eng)
        frequency_context = collect_frequency_context(
            eng, getattr(self, "_frequency_contexts", {}).get(id(eng)))
        raw.solver["frequency_hz"] = frequency_context["solution_frequency_hz"]
        raw.solver["frequency_context"] = frequency_context
        tp = list(ckt.TotalPower or ())
        ls = list(ckt.Losses or ())
        raw.system = {
            "total_power_kw": -float(tp[0]) if len(tp) > 1 else None,
            "total_power_kvar": -float(tp[1]) if len(tp) > 1 else None,
            "losses_kw": float(ls[0]) / 1000.0 if len(ls) > 1 else None,
            "losses_kvar": float(ls[1]) / 1000.0 if len(ls) > 1 else None,
        }

        if any(scope in result_scopes for scope in (ResultScope.BUS, ResultScope.SYSTEM, ResultScope.BRANCH, ResultScope.TRANSFORMER)):
            for name in (ckt.AllBusNames or ()):
                raw.buses.append(collect_bus_observation(ckt, str(name)))

        def _terminal1(ce: Any, include_current: bool = True) -> tuple[float | None, float | None, float | None]:
            n_ph = int(ce.NumPhases)
            n_cond = int(ce.NumConductors)
            powers = list(ce.Powers or ())
            currents = list(ce.CurrentsMagAng or ()) if include_current else []
            if len(powers) < 2 * n_cond or not 1 <= n_ph <= n_cond:
                return None, None, None
            # Powers is terminal-major, conductor-minor. In particular a
            # one-/two-phase delta load has two/three conductors. Truncating
            # to NumPhases drops real terminal power and breaks conservation.
            p = sum(float(powers[2 * i]) for i in range(n_cond))
            q = sum(float(powers[2 * i + 1]) for i in range(n_cond))
            i_max = max(float(currents[2 * i]) for i in range(n_ph)) if currents else None
            return p, q, i_max

        # 按元件求和 —— 「负荷」必须来自负荷元件本身，不能由源侧功率倒推。
        # 源侧供电 − 网损 是**净负荷**，含 PV/发电时 `净负荷 = 负荷 − 发电`，
        # 把它当"总负荷"会让报告写出明显错误的数值（实测出现过 −57332 kW）。
        raw.system.update(self._element_power_sums(ckt, lambda ce: _terminal1(ce, False)))

        def _legacy_terminal1(data):
            # Copyright (c) yangshaopeng. Keep old scalar fields, but obtain
            # their values only from the identity-checked COM observation.
            n_ph = data["num_phases"]
            n_cond = data["num_conductors"]
            powers, currents = data["powers_kw_kvar"], data["currents_mag_a"]
            if (not data["identity_verified"] or not n_ph or not n_cond or n_cond < n_ph
                    or powers is None or len(powers) < 2 * n_cond):
                return None, None, None
            p, q = sum(powers[:2 * n_cond:2]), sum(powers[1:2 * n_cond:2])
            current = max(currents[:n_ph]) if currents is not None and len(currents) >= n_ph else None
            return p, q, current

        def _legacy_number(getter):
            try:
                value = float(getter())
                return value if value and math.isfinite(value) else None
            except Exception:
                return None

        if ResultScope.BRANCH in result_scopes:
            lines = ckt.Lines
            idx = lines.First
            while idx > 0:
                name = f"Line.{lines.Name}"
                terminal_data = collect_terminal_data(ckt, name, engine=eng)
                bus_names = terminal_data["bus_names"] or []
                rating = _legacy_number(lambda: ckt.ActiveCktElement.NormalAmps) if terminal_data["identity_verified"] else None
                p, q, current = _legacy_terminal1(terminal_data)
                raw.lines.append(
                    {
                        "name": name,
                        "bus1": bus_names[0] if bus_names else "",
                        "bus2": bus_names[1] if len(bus_names) > 1 else "",
                        "p_kw": p,
                        "q_kvar": q,
                        "current_a": current,
                        "rating_a": rating,
                        "rating_a_provenance": "OpenDSS.ActiveCktElement.NormalAmps; engineering_confirmation=not_established",
                        "terminal_data": terminal_data,
                    }
                )
                idx = lines.Next

        if ResultScope.TRANSFORMER in result_scopes:
            # Copyright (c) yangshaopeng. Collection iterators do not prove
            # which circuit element is active. Select the full name and verify
            # it before reading every terminal, including multiwinding units.
            trs = ckt.Transformers
            idx = trs.First
            while idx > 0:
                tname = f"Transformer.{trs.Name}"
                terminal_data = collect_terminal_data(ckt, tname, engine=eng)
                p, q, _ = _legacy_terminal1(terminal_data)
                tr_bus = [name.split(".")[0] for name in terminal_data["bus_names"] or []]
                rated = _legacy_number(lambda: trs.kVA) if terminal_data["identity_verified"] else None
                raw.transformers.append(
                    {
                        "name": tname,
                        "bus1": tr_bus[0] if tr_bus else "",
                        "bus2": tr_bus[1] if len(tr_bus) > 1 else "",
                        "p_kw": p,
                        "q_kvar": q,
                        "rated_kva": rated,
                        "rated_kva_provenance": "OpenDSS.Transformers.kVA of selected winding; engineering_confirmation=not_established",
                        "terminal_data": terminal_data,
                    }
                )
                idx = trs.Next

        from .pce_observations import collect_power_conversion_elements
        raw.power_conversion_elements, raw.power_conversion_elements_coverage = collect_power_conversion_elements(
            eng, requested=ResultScope.SYSTEM in result_scopes)
        if ResultScope.SYSTEM in result_scopes:
            original_element = ckt.ActiveCktElement.Name
            try:
                raw.auxiliary_elements = [{"name":str(name), "class":str(name).split(".",1)[0].casefold(),
                    "terminal_data":collect_terminal_data(ckt,str(name),engine=eng)}
                    for name in ckt.AllElementNames
                    if str(name).split(".",1)[0].casefold() in {"vsource","capacitor","reactor"}]
            finally:
                ckt.SetActiveElement(original_element)
        raw.element_count = {
            "bus": int(ckt.NumBuses),
            "line": len(raw.lines),
            "transformer": len(raw.transformers),
            "load": int(ckt.Loads.Count),
        }
        # ⚠️ 把采集侧的适配器版本**写进产物**。
        # 起因：2026-10-02 排查"变压器端点全空"时，无法从产物判断这份结果
        # 是**哪一版采集逻辑**产出的 —— 只能靠比对进程启动时间与文件 mtime，
        # 绕了很远。产物自带版本戳后，一眼可判"这是旧版跑出来的，重新算"。
        raw.collector_version = ADAPTER_VERSION
        raw.solver["domain_inventory"] = {"before":inventory_before, "after":collect_domain_inventory(eng)}
        if ResultScope.SYSTEM in result_scopes:
            # Freeze the complete native passive boundary before conversion
            # mapping folds source objects or separately labels Gs/Bs shunts.
            # Additional reads serve balance only, without expanding display domains.
            from ..domain.passive_power_balance import KINDS, build_passive_power_boundary
            native_records = {row["name"].casefold(): row for row in [*raw.lines, *raw.transformers,
                *(row for row in (raw.auxiliary_elements or []) if row.get("class") in {"capacitor", "reactor"})]}
            selected = ckt.ActiveCktElement.Name
            try:
                names = inventory_before.get("fields", {}).get("element_names", {}).get("value", [])
                for name in names if isinstance(names, list) else []:
                    if str(name).split(".", 1)[0].casefold() in KINDS and str(name).casefold() not in native_records:
                        native_records[str(name).casefold()] = {"name":str(name), "class":str(name).split(".", 1)[0].casefold(),
                            "terminal_data":collect_terminal_data(ckt, str(name), engine=eng)}
            finally:
                ckt.SetActiveElement(selected)
            raw.solver["domain_inventory"]["after"] = collect_domain_inventory(eng)
            raw.system["passive_power_boundary"] = build_passive_power_boundary({"solver":raw.solver,
                "buses":raw.buses, "auxiliary_elements":raw.auxiliary_elements}, list(native_records.values()))
        from .fidelity_mapping import apply_fidelity_mapping, measured_terminal_reader
        apply_fidelity_mapping(raw, getattr(self, "_fidelity_mapping", []),
                               measured_terminal_reader(ckt), result_scopes, ckt.AllBusNames)
        raw.solver["domain_inventory"] = {"before":inventory_before, "after":collect_domain_inventory(eng)}
        return raw

    # ------------------------------------------------- element power summation
    #
    # 元件求和（`_element_power_sums`）与「什么算负荷、什么算发电」的口径表
    # 都由基类 `OpenDSSAdapter` 提供，本适配器**刻意不重复实现**。
    #
    # 9/15 实测教训：这里曾有一份与基类**逐行等价**的副本（当时理由是
    # "避免把 dss-python 侧的导入牵扯进 COM 进程"——但该方法是 @staticmethod、
    # 不导入任何东西，理由不成立）。结果是修基类对生产路径**完全无效**：
    # 含 `New PVSystem` 的算例在 COM 下仍然 gen_kw=0、功率平衡校验失败、
    # 任务直接报 VERIFICATION_FAILED。口径重复定义必然会漂移，故删除副本。

    # --------------------------------------------------------------------- close
    def close(self, circuit: CircuitHandle) -> None:
        try:
            self._worker.call(lambda eng: eng.ClearAll(), 30.0)
        except Exception:
            pass
        circuit.compiled = False
