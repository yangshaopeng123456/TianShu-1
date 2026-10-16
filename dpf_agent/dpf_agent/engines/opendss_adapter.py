"""OpenDSS Adapter —— V1.3 唯一正式潮流引擎适配器（文档 7.3）。

隔离 dss-python 的 Text/Circuit/Solution 接口、文件路径、版本差异与原始对象访问，
向上只返回结构化结果。接口契约：

    health() -> EngineHealth
    compile_case(case_workspace, master_dss) -> CircuitHandle
    apply_solver_profile(circuit, solver_profile) -> None
    solve(circuit) -> SolverExecutionResult
    extract_raw_result(circuit, result_scopes) -> OpenDSSRawResult
    close(circuit) -> None

允许的 OpenDSS 命令白名单（执行门，文档 13.4）：compile / redirect / set / solve / show? —
本适配器仅使用受控的必要命令。
"""
from __future__ import annotations

import importlib
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import ADAPTER_VERSION
from ..schemas.enums import ErrorCode, ResultScope
from ..schemas.solver import OpenDSSSolverProfile
from .circuit_inventory import CircuitInventory, collect_circuit_inventory
from .circuit_validation import (
    CircuitObservation,
    normalize_bus_name,
    validate_compiled_circuit,
)


class AdapterError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class EngineHealth:
    available: bool
    engine_version: str = ""
    adapter_version: str = ADAPTER_VERSION
    checked_at: float = field(default_factory=time.time)
    detail: str = ""


@dataclass
class CircuitHandle:
    handle_id: str
    case_workspace: str
    master_dss: str
    compiled: bool = False
    dss_ctx: Any = None  # dss.DSS 上下文（仅后端上下文，不进入 Session/共享状态）


@dataclass
class SolverExecutionResult:
    converged: bool
    iterations: int
    error_flag: bool
    error_text: str = ""
    duration_s: float = 0.0


@dataclass
class OpenDSSRawResult:
    """原始结构化执行结果，不直接供 LLM 使用（文档 4.6）。"""

    solver: dict[str, Any] = field(default_factory=dict)
    buses: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    transformers: list[dict[str, Any]] = field(default_factory=list)
    system: dict[str, Any] = field(default_factory=dict)
    element_count: dict[str, int] = field(default_factory=dict)
    # 采集侧的适配器版本戳。由 COM 通道在 `_extract` 末尾写入。
    #
    # ⚠️ 为什么必须有：排查"结果字段在但值为空"这类问题时，光看产物**无法判断**
    # 它是哪一版采集逻辑产出的（2026-10-02 查变压器端点全空，只能去比对
    # 进程启动时间与文件 mtime，绕了很远，还先后误判成"加载了错误的代码树"
    # 和"日志被 info 级吞掉"）。字段自带版本后，一眼可判"这是旧版跑的，重算"。
    collector_version: str = ""


# ============================================================== 元件口径表
#
# 「什么算负荷、什么算发电」**只在这里定义一次**，两条通道共用：
#   * COM 通道（当前）：遍历引擎集合时按下表归类
#   * CLI 通道（Step 2）：解析 `export powers` 的元件名前缀时用同一张表
#
# 为什么不各写一遍：口径重复定义早晚会漂移。本项目已经吃过一次亏——
# 「总负荷」曾用倒推值，626 个产物里 148 个失真。口径必须单点定义。
ELEMENT_ROLE_BY_PREFIX: dict[str, str] = {
    "load": "load",
    "generator": "generation",
    # ⚠️ PVSystem **必须**在这里。实测（9/15，真引擎）：
    #    `New PVSystem` 定义的元件**不会**出现在 `ckt.Generators` 里
    #    （有 1 个 PVSystem 时 gen_elements=0）。旧实现只遍历 Loads+Generators，
    #    导致含 PVSystem 的算例 gen_kw=0，功率平衡残差 = 光伏出力，
    #    任务被判 VERIFICATION_FAILED:['power_balance'] 而直接失败。
    #    （代码里曾注释"PVSystem 即 Generator 的投影"——那个说法是错的。）
    "pvsystem": "generation",
    # 储能是**双向**元件，角色是第三种：**单独求和**，既不进负荷也不进发电
    # （归进任何一边都会在报告里制造"负的负荷"或"负的发电"，无法解释），
    # 但**必须参与功率平衡** —— 否则平衡残差恰好等于储能功率。
    # 实测（9/16，真引擎）：4 母线算例「负荷 1000 kW + 储能放电 500 kW」，
    # 储能不入平衡式时 residual = 0.500000 pu（容差 1e-3）
    # → 任务直接 VERIFICATION_FAILED:['power_balance']。
    # 求和值保留引擎「流入元件为正」的原始符号：> 0 充电、< 0 放电。
    "storage": "bidirectional",
}

# 明确【不】参与任何求和（含功率平衡式）的元件前缀及其原因。
# 原因要能直接写进报告口径声明。**当前为空** —— 机制保留：
# 将来遇到"既不是负荷/发电、也不该进平衡式"的元件时登记在此，
# 而不是把它猜进某一边（猜错的后果是静默的口径错误）。
ELEMENT_EXCLUDED_PREFIXES: dict[str, str] = {}


def classify_element_by_prefix(prefix: str) -> str | None:
    """按元件名前缀判定它在功率求和中的角色。

    返回三种角色之一，或 ``None``（明确不参与任何求和）：
      * ``"load"``          → 累加进 ``load_kw``（消耗为正）
      * ``"generation"``    → 累加进 ``gen_kw``（取反，注入为正）
      * ``"bidirectional"`` → 累加进 ``storage_kw``（**不取反**，保留引擎原始符号）。
        既不进负荷也不进发电，但**必须**参与功率平衡。

    未登记的前缀一律按 None 处理——**宁可漏算一个未登记的类别并被测试发现，
    也不要凭猜测把它归进某一边**（猜错的后果是静默的口径错误）。
    """
    key = str(prefix or "").strip().lower()
    if key in ELEMENT_EXCLUDED_PREFIXES:
        return None
    return ELEMENT_ROLE_BY_PREFIX.get(key)


# 引擎集合属性名 → 元件名前缀。**集合只在这里登记**；
# 每个前缀算负荷、发电还是双向，由上面的口径表决定
# （两件事分开，改一处不影响另一处）。
# 用 getattr 取集合：不同引擎/版本暴露的集合不同，缺了就当它没有，不做假设。
_POWER_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("Loads", "load"),
    ("Generators", "generator"),
    ("PVSystems", "pvsystem"),
    # ⚠️ Storages 必须在这里。不在的话储能功率**完全不可见**，
    #    平衡残差恰好等于储能功率 → 含储能充放电的算例必然判"不平衡"。
    ("Storages", "storage"),
)


def _collect_dss_observation(engine: Any) -> CircuitObservation:
    """从 dss-python 引擎收集电路观察量（引擎相关部分；判定见 `circuit_validation`）。

    只做"取数"，不判断对错 —— 规则单点定义在 `validate_compiled_circuit`，
    与 COM 通道（以及将来的 CLI 通道）共用同一份判定。
    """
    ckt = engine.ActiveCircuit
    bus_names = frozenset(normalize_bus_name(n) for n in (ckt.AllBusNames or ()))

    endpoints: list[tuple[str, str]] = []
    for ce in ckt.CktElements:
        if not ce.Enabled:
            continue
        name = str(ce.Name)
        for bus in ce.BusNames or ():
            endpoints.append((name, str(bus)))

    bus_base_kv = [(str(b.Name), b.kVBase) for b in ckt.Buses]

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
        bus_count=len(ckt.Buses),
        bus_names=bus_names,
        endpoints=tuple(endpoints),
        bus_base_kv=tuple(bus_base_kv),
        vsource_buses=frozenset(vsource_buses),
    )


class OpenDSSAdapter:
    """OpenDSS 引擎适配器。所有潮流数值 100% 经本适配器产生（验收门 17.3）。"""

    # 保持同一 snapshot 的原生引擎可继续迭代；CLI 的独立冷批处理不具有此能力。
    supports_snapshot_refinement = True

    def __init__(self, solve_timeout_s: float = 60.0):
        self.solve_timeout_s = solve_timeout_s
        self._last_health: EngineHealth | None = None
        self._dss_module: Any | None = None

    def _dss(self) -> Any:
        """延迟加载 dss-python，避免 Web 应用导入阶段触发原生库初始化。"""
        if self._dss_module is None:
            self._dss_module = importlib.import_module("dss")
        return self._dss_module

    # ------------------------------------------------ 引擎上下文执行（引擎通用）
    def run_with_engine(self, fn: Any, timeout_s: float | None = None) -> Any:
        """在引擎对象可安全访问的上下文中执行 fn(engine)。

        dss-python：同进程直接调用；COM 适配器重写为转发到专用工作线程。
        fn 内只允许使用 dss-python 与 COM 共有的 API。
        """
        return fn(self._dss().DSS)

    def circuit_summary(self, circuit: CircuitHandle) -> tuple[dict[str, int], dict[str, float]]:
        """算例对象统计与母线基准电压表（引擎通用，COM 风格 API）。"""

        def _summary(eng: Any) -> tuple[dict[str, int], dict[str, float]]:
            ckt = eng.ActiveCircuit
            summary = {
                "bus": int(ckt.NumBuses),
                "line": int(ckt.Lines.Count),
                "transformer": int(ckt.Transformers.Count),
                "load": int(ckt.Loads.Count),
            }
            base_kv: dict[str, float] = {}
            for name in (ckt.AllBusNames or ()):
                ckt.SetActiveBus(name)
                bus = ckt.ActiveBus
                try:
                    if bus.kVBase:
                        base_kv[str(bus.Name)] = round(float(bus.kVBase), 3)
                except Exception:
                    continue
            return summary, base_kv

        from .fidelity_mapping import restore_summary
        counts, base_kv = self.run_with_engine(_summary)
        return restore_summary(counts, base_kv, getattr(self, "_fidelity_mapping", []))

    # ------------------------------------------------------------------ health
    def health(self) -> EngineHealth:
        try:
            engine = self._dss().DSS
            version = engine.Version
            self._last_health = EngineHealth(available=True, engine_version=version.strip())
        except Exception as exc:  # pragma: no cover - 引擎缺失
            self._last_health = EngineHealth(
                available=False, detail=f"OpenDSS 引擎不可用: {exc}"
            )
        return self._last_health

    # ------------------------------------------------------------ compile_case
    def compile_case(self, case_workspace: str, master_dss: str) -> CircuitHandle:
        from .fidelity_mapping import read_fidelity_mapping
        self._fidelity_mapping = read_fidelity_mapping(case_workspace)
        handle = CircuitHandle(
            handle_id=uuid.uuid4().hex[:12],
            case_workspace=case_workspace,
            master_dss=master_dss,
        )
        engine = self._dss().DSS
        engine.ClearAll()
        # 受控 Compile：只允许任务目录内的 master 文件（路径安全已在 Guard 校验）。
        # dss-python 某些版本会在 Command setter 直接抛出 DSSException，不能只读 Error.Number。
        compile_detail = ""
        try:
            engine.Text.Command = f'compile "{master_dss}"'
        except Exception as exc:
            compile_detail = str(exc)
        err_num = engine.Error.Number
        if compile_detail:
            error_code = ErrorCode.DATA_UNAVAILABLE if (
                "linecode" in compile_detail.lower()
                or "not found" in compile_detail.lower()
                or "does not exist" in compile_detail.lower()
            ) else ErrorCode.VALIDATION_FAILED
            raise AdapterError(
                error_code,
                f"OpenDSS Compile 失败 [{err_num}]: {compile_detail}",
            )
        if err_num != 0:
            detail = engine.Error.Description
            error_code = ErrorCode.VALIDATION_FAILED
            if "LineCode" in detail or "not found" in detail.lower() or "does not exist" in detail.lower():
                error_code = ErrorCode.DATA_UNAVAILABLE
            raise AdapterError(
                error_code,
                f"OpenDSS Compile 失败 [{err_num}]: {detail}",
            )
        # compile 依赖缺失（LineCode 找不到等）单独判为 DATA_UNAVAILABLE，
        # 与"算例本身无效"区分开：前者是数据/环境问题，后者是算例问题。
        if compile_detail and (
            "linecode" in compile_detail.lower()
            or "not found" in compile_detail.lower()
            or "does not exist" in compile_detail.lower()
        ):
            raise AdapterError(
                ErrorCode.DATA_UNAVAILABLE,
                f"OpenDSS Compile 依赖不可用：{compile_detail}",
            )
        # 电路校验规则**单点定义**在 `circuit_validation`（与 COM 通道共用同一份）。
        # 这里只负责"从 dss-python 引擎收集观察量"——即引擎相关的部分；
        # 判定逻辑只写一遍。此前两个适配器各写一份规则，改一份不影响另一份，
        # 早晚漂移（本项目已在元件求和口径上吃过同样的亏）。
        detail = validate_compiled_circuit(_collect_dss_observation(engine))
        if detail:
            raise AdapterError(ErrorCode.VALIDATION_FAILED, detail)
        handle.compiled = True
        handle.dss_ctx = engine
        return handle

    # ------------------------------------------------------ apply_solver_profile
    def apply_solver_profile(
        self, circuit: CircuitHandle, solver_profile: OpenDSSSolverProfile
    ) -> None:
        if not solver_profile.frozen:
            raise AdapterError(ErrorCode.REQ_SCHEMA_INVALID, "SolverProfile 未冻结，禁止应用")
        sol = circuit.dss_ctx.ActiveCircuit.Solution
        sol.Mode = 0  # snapshot：稳态确定性单场景
        sol.MaxIterations = solver_profile.iteration_config.max_iterations
        sol.Tolerance = solver_profile.tolerance_config.tolerance
        # V1.3 冻结控制行为：controlmode=off 禁用一切自动控制动作
        # （不设置 MaxControlIterations=1，OpenDSS 会因此产生 #485 警告）
        circuit.dss_ctx.Text.Command = "set controlmode=off"

    # --------------------------------------------------------------------- solve
    def solve(self, circuit: CircuitHandle, *, iteration_limit: int | None = None,
              timeout_s: float | None = None) -> SolverExecutionResult:
        sol = circuit.dss_ctx.ActiveCircuit.Solution
        previous_limit = sol.MaxIterations
        if iteration_limit is not None:
            sol.MaxIterations = min(int(previous_limit), int(iteration_limit))
        t0 = time.time()
        try:
            sol.Solve()
        finally:
            sol.MaxIterations = previous_limit
        duration = time.time() - t0
        err_num = circuit.dss_ctx.Error.Number
        err_text = circuit.dss_ctx.Error.Description if err_num else ""
        allowed_time = min(self.solve_timeout_s, timeout_s) if timeout_s is not None else self.solve_timeout_s
        if duration > allowed_time:
            raise AdapterError(ErrorCode.TIMEOUT, f"Solve 超时 ({duration:.1f}s)")
        return SolverExecutionResult(
            converged=bool(sol.Converged),
            iterations=int(sol.Iterations),
            error_flag=err_num != 0,
            error_text=err_text,
            duration_s=duration,
        )

    # --------------------------------------------------------- extract_raw_result
    def extract_raw_result(
        self, circuit: CircuitHandle, result_scopes: list[ResultScope], *, timeout_s: float | None = None
    ) -> OpenDSSRawResult:
        ctx = circuit.dss_ctx
        ckt = ctx.ActiveCircuit
        raw = OpenDSSRawResult()

        sol = ckt.Solution
        raw.solver = {
            "converged": bool(sol.Converged),
            "iterations": int(sol.Iterations),
            "mode": "snapshot",
        }

        # 系统级：总功率与损耗（Circuit 级聚合值，单位 kW/kvar）
        raw.system = {
            "total_power_kw": -float(ckt.TotalPower[0]),  # 源侧供电为正
            "total_power_kvar": -float(ckt.TotalPower[1]),
            "losses_kw": float(ckt.Losses[0]) / 1000.0,
            "losses_kvar": float(ckt.Losses[1]) / 1000.0,
        }
        # 按元件求和 —— 「负荷」必须来自负荷元件本身，不能由源侧功率倒推。
        # 原因：源侧供电 − 网损 得到的是**净负荷**（流入电网的净功率）。
        # 算例含 PV/发电机时两者不相等：净负荷 = 负荷 − 发电。
        # 实测有算例源侧 168684 kW、"净负荷" −57332 kW，若拿它当"总负荷"
        # 就会在报告里写出"系统总负荷为 -57332.60 kW"这种明显错误的结论。
        raw.system.update(self._element_power_sums(ckt, _terminal1))


        if ResultScope.BUS in result_scopes or ResultScope.SYSTEM in result_scopes:
            import math

            for bus in ckt.Buses:
                pu_va = list(bus.puVmagAngle) if bus.puVmagAngle is not None else []
                # kVBase 为相对地电压；多相母线换算为线电压
                kv_ln = float(bus.kVBase) if bus.kVBase else None
                n_nodes = len(list(bus.Nodes)) if bus.Nodes is not None else 0
                base_kv = kv_ln * math.sqrt(3) if (kv_ln and n_nodes >= 2) else kv_ln
                raw.buses.append(
                    {
                        "name": bus.Name,
                        "base_kv": base_kv,
                        # 节点号必须带出去：相别身份由它决定（见 result_normalizer
                        # 的 _PHASE_LABEL_BY_NODE），不能按值出现顺序猜相别。
                        "nodes": [int(n) for n in list(bus.Nodes or ())],
                        "voltage_pu": [float(v) for v in pu_va[::2]],
                        "angle_deg": [float(a) for a in pu_va[1::2]],
                    }
                )

        def _terminal1(ce) -> tuple[float | None, float | None, float | None]:
            """首端（terminal 1）三相合计 P/Q（流入元件为正）与最大相电流。"""
            n_ph = int(ce.NumPhases)
            powers = list(ce.Powers)
            currents = list(ce.CurrentsMagAng)
            if len(powers) < 2 * n_ph or n_ph < 1:
                return None, None, None
            p = sum(float(powers[2 * i]) for i in range(n_ph))
            q = sum(float(powers[2 * i + 1]) for i in range(n_ph))
            i_max = max(float(currents[2 * i]) for i in range(n_ph)) if currents else None
            return p, q, i_max

        if ResultScope.BRANCH in result_scopes:
            ce = ckt.ActiveCktElement
            lines = ckt.Lines
            idx = lines.First
            while idx > 0:
                p, q, i_max = _terminal1(ce)
                bus_names = list(ce.BusNames)
                normal_amps = ce.NormalAmps
                raw.lines.append(
                    {
                        "name": f"Line.{lines.Name}",
                        "bus1": bus_names[0] if bus_names else "",
                        "bus2": bus_names[1] if len(bus_names) > 1 else "",
                        "p_kw": p,  # 首端流出母线为正（OpenDSS 流入元件为正，方向定义固定）
                        "q_kvar": q,
                        "current_a": i_max,
                        "rating_a": float(normal_amps) if normal_amps else None,
                    }
                )
                idx = lines.Next

        if ResultScope.TRANSFORMER in result_scopes:
            ce = ckt.ActiveCktElement
            trs = ckt.Transformers
            idx = trs.First
            while idx > 0:
                p, q, _ = _terminal1(ce)
                raw.transformers.append(
                    {
                        "name": f"Transformer.{trs.Name}",
                        "p_kw": p,
                        "q_kvar": q,
                        "rated_kva": float(trs.kVA) if trs.kVA else None,
                    }
                )
                idx = trs.Next

        raw.element_count = {
            "bus": len(raw.buses),
            "line": len(raw.lines),
            "transformer": len(raw.transformers),
            "load": int(ckt.Loads.Count),
        }
        from .fidelity_mapping import apply_fidelity_mapping, measured_terminal_reader
        apply_fidelity_mapping(raw, getattr(self, "_fidelity_mapping", []),
                               measured_terminal_reader(ckt), result_scopes, ckt.AllBusNames)
        return raw

    # ------------------------------------------------- element power summation
    @staticmethod
    def _element_power_sums(ckt: Any, terminal1: Any) -> dict[str, Any]:
        """按元件求和：负荷消耗、发电注入（均为端子 1 三相合计，单位 kW/kvar）。

        ## 为什么不用 Circuit.TotalPower − Losses

        那是**净负荷**（源侧流入 − 网损）。含 PV/发电时
        `净负荷 = 负荷 − 发电`，把它当"总负荷"会在报告里写出明显错误的数值
        （实测出现过 −57332 kW 的"系统总负荷"）。「总负荷」应当忠实反映
        负荷元件自身的消耗，因此必须遍历元件求和。

        ## 符号约定

        OpenDSS 约定 `Powers` 为**流入元件为正**，所以：
          - 负荷元件：正常为正值 → 直接累加即"消耗"。
          - 发电元件：向电网注入，其端子 1 功率为**负**；取相反数得"注入"，
            便于读者按正数理解。两种符号都如实保留，不做绝对值化，
            以免把"反向受电的负荷"（罕见但合法）也算成发电。

        ## 哪些元件参与求和（口径不写死在本函数里）

        由 `classify_element_by_prefix` 按元件名前缀判定，遍历的集合来自
        `_POWER_COLLECTIONS`。口径单点定义，Step 2 的 CLI 通道复用同一张表。

        ⚠️ `PVSystems` **必须**在遍历范围内：实测（9/15，真引擎）证明
        `New PVSystem` 的元件不会出现在 `ckt.Generators` 里，漏掉它会让
        含光伏的算例 gen_kw=0，进而功率平衡校验失败、任务直接报错。

        ⚠️ `Storages` 同理**必须**在遍历范围内，但归类不同：储能是双向元件，
        既不进负荷也不进发电（归任一边都会产生"负的负荷/负的发电"），
        而是单独记进 `storage_kw`，供功率平衡式使用。漏掉它的后果与漏掉
        PVSystem 同类：平衡残差 = 储能功率 → 含储能充放电的算例直接失败。

        ## 容错

        单个元件读数缺失（长度为 0 或相数异常）时跳过该元件，
        不让一处坏数据污染全网汇总；同时记录被跳过的元件数供排查。
        """
        out: dict[str, Any] = {
            "load_kw": 0.0,
            "load_kvar": 0.0,
            "load_elements": 0,
            "load_elements_skipped": 0,
            "gen_kw": 0.0,
            "gen_kvar": 0.0,
            "gen_elements": 0,
            "gen_elements_skipped": 0,
            # 储能：单独一类，**不进** load_kw / gen_kw，但会进功率平衡式。
            # 保留引擎原始符号（流入元件为正）：> 0 充电、< 0 放电。
            "storage_kw": 0.0,
            "storage_kvar": 0.0,
            "storage_elements": 0,
            "storage_elements_skipped": 0,
        }

        # 角色 → (输出前缀, 是否取反)。
        #   generation 取反，是为了"注入为正"便于读者理解；
        #   bidirectional **不取反** —— 它的符号本身就是语义（正=从电网取电、
        #   负=向电网送电），取反反而会让平衡式里的符号变绕。
        _ROLE_OUTPUT: dict[str, tuple[str, bool]] = {
            "load": ("load", False),
            "generation": ("gen", True),
            "bidirectional": ("storage", False),
        }

        def _accumulate(collection: Any, *, prefix: str) -> None:
            role = classify_element_by_prefix(prefix)
            if role is None:
                return  # 口径外元件，明确跳过而非猜一边
            spec = _ROLE_OUTPUT.get(role)
            if spec is None:
                return
            pfx, invert = spec
            try:
                idx = collection.First
            except Exception:
                return
            guard = 0
            while idx > 0:
                guard += 1
                if guard > 1_000_000:  # 防引擎游标异常导致的死循环
                    break
                ce = ckt.ActiveCktElement
                try:
                    p, q, _ = terminal1(ce)
                except Exception:
                    p, q = None, None
                if p is None or q is None:
                    out[f"{pfx}_elements_skipped"] += 1
                else:
                    sign = -1.0 if invert else 1.0
                    out[f"{pfx}_kw"] += sign * float(p)
                    out[f"{pfx}_kvar"] += sign * float(q)
                    out[f"{pfx}_elements"] += 1
                try:
                    idx = collection.Next
                except Exception:
                    break

        for attr, prefix in _POWER_COLLECTIONS:
            try:
                collection = getattr(ckt, attr, None)
            except Exception:  # noqa: BLE001 - 该引擎不暴露此集合
                collection = None
            if collection is None:
                continue
            try:
                _accumulate(collection, prefix=prefix)
            except Exception:  # noqa: BLE001 - 单个集合异常不影响其余集合
                pass
        return out

    # ---------------------------------------------- circuit inventory（网络画像）
    def collect_circuit_inventory(self) -> CircuitInventory:
        """收集网络画像所需的引擎观察量（引擎相关的那一半）。

        组装逻辑（`domain.network_profile.build_network_profile`）是纯数据、单点定义的；
        各通道只负责把**自己引擎里的原始量**读出来。CLI 通道将来自己实现本方法即可，
        画像组装一行都不用改。
        """
        return collect_circuit_inventory(self._dss().DSS)

    # --------------------------------------------------------------------- close
    def close(self, circuit: CircuitHandle) -> None:
        try:
            circuit.dss_ctx.ClearAll()
        except Exception:
            pass
        circuit.compiled = False
