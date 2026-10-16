"""Result Extractor：OpenDSS 原始结果 → Canonical Result（文档 12.4 / 13.6）。

- 统一单位与方向：电压 pu/kV，功率 kW/kvar（支路首端流出为正），电流 A。
- 缺失域显式标 unavailable / not_assessed；额定值不可用时 loading=unavailable。
- 不改变底层求解结果。
"""
from __future__ import annotations

from ..engines.opendss_adapter import OpenDSSRawResult, SolverExecutionResult
from ..schemas.enums import ErrorCode, ResultScope
from ..schemas.result import (
    BranchResult,
    BusResult,
    CanonicalDistributionPowerFlowResult,
    LossSummary,
    SolverStatus,
    SystemSummary,
    TransformerResult,
    UnbalanceSummary,
    UnbalanceViolation,
)
from .unbalance import UnbalanceScan, compute_metrics

# 相电压不平衡度的**参考**限值。⚠️ 在未与标准原文核对前，这里只用于"标注"，
# 报告必须同时写出 limit_source，不得表述为已被权威认定。
UNBALANCE_LIMIT_PCT = 2.0
UNBALANCE_LIMIT_SOURCE = "参考值（常用限值 2%；短时 4%）—— 待与 GB/T 15543 原文核对后确认"

# OpenDSS 节点顺序默认 A-B-C
_PHASE_LABELS = ("A", "B", "C")
#: OpenDSS 节点号 → 相别标签。**相别身份以此为准**（node1=A/2=B/3=C），
#: 绝不按"出现顺序"猜 —— 缺相母线会让后面的相前移、整套标签错位。
_PHASE_LABEL_BY_NODE = {1: "A", 2: "B", 3: "C"}


class ResultMappingError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = ErrorCode.RESULT_MAPPING_FAILED
        self.message = message


def _is_valid_number(x: float | None) -> bool:
    if x is None:
        return False
    return x == x and x not in (float("inf"), float("-inf"))  # NaN / inf 检查


def normalize_powerflow_result(
    raw: OpenDSSRawResult,
    solve_result: SolverExecutionResult,
    task_id: str,
    case_fingerprint: str,
    solver_profile_ref: str,
    engine_version: str,
    adapter_version: str,
    requested_scopes: list[ResultScope],
) -> CanonicalDistributionPowerFlowResult:
    solver_status = SolverStatus(
        converged=solve_result.converged,
        iterations=solve_result.iterations,
        error_flag=solve_result.error_flag,
        raw_solution_count=raw.solver.get("solution_count"),
        total_iterations=raw.solver.get("total_iterations"),
    )

    result = CanonicalDistributionPowerFlowResult(
        task_id=task_id,
        case_fingerprint=case_fingerprint,
        solver_status=solver_status,
        solver_profile_ref=solver_profile_ref,
        engine_version=engine_version,
        adapter_version=adapter_version,
    )

    # --- bus 结果 ---------------------------------------------------------------
    # ⚠️ 这里曾经是"取相电压 pu 最大值作为平衡等值幅值"，即把三相压成一个数 ——
    # 结果是 OpenDSS 原生算出来的不平衡信息在最后一步被丢掉（实测某算例相间差 22.55%，
    # 报告上完全看不出来）。现在**分相数据一律保留**，同时保留一个显式标注的
    # "平衡等值"字段（取三相最大），用于与平衡模型的参考值对比，保证既有口径不漂移。
    bus_results: list[BusResult] = []
    scan = UnbalanceScan()
    for b in raw.buses:
        # ⚠️ 相别身份由 OpenDSS **节点号**决定（见 `_PHASE_LABEL_BY_NODE`）。
        #    历史上这里按"值出现顺序"贴 A/B/C 标签，缺相母线（IEEE13 的 645 只有
        #    node2/3、684 只有 node1/3）会让后面的相前移 —— 实测 645 的 A 标签下装的
        #    是 B 相电压，且"补齐的 0"被当成真实相电压，导致最低相电压=0.0 pu、
        #    684 不平衡度 ε=196%。属硬约定六（相别身份不可猜）的违反。
        raw_nodes = list(b.get("nodes") or [])
        _vals = list(b.get("voltage_pu") or [])
        _angs = list(b.get("angle_deg") or [])
        if raw_nodes and len(raw_nodes) == len(_vals):
            _pairs = [(int(n), v, (_angs[i] if i < len(_angs) else None))
                      for i, (n, v) in enumerate(zip(raw_nodes, _vals))
                      if _is_valid_number(v)]
            labels = [_PHASE_LABEL_BY_NODE.get(n) for n, _, _ in _pairs]
            if any(x is None for x in labels):        # 出现 1~3 以外的节点号：退回位置法
                labels = list(_PHASE_LABELS[:len(_pairs)])
        else:                                          # 旧通道未提供节点号：保持原行为
            _pairs = [(i + 1, v, (_angs[i] if i < len(_angs) else None))
                      for i, v in enumerate(_vals) if _is_valid_number(v)]
            labels = list(_PHASE_LABELS[:len(_pairs)])
        pu_vals = [v for _, v, _ in _pairs]
        angle_vals = [a for _, _, a in _pairs if _is_valid_number(a)]
        if not pu_vals:
            bus_results.append(BusResult(name=b["name"], status="unavailable"))
            continue
        base_kv = b.get("base_kv")
        metrics = compute_metrics(pu_vals, angle_vals)
        n_ph = len(pu_vals)
        bus_results.append(
            BusResult(
                name=b["name"],
                base_kv=base_kv,
                voltage_pu=max(pu_vals),  # 平衡等值：口径见字段注释，勿当作分相值
                voltage_kv=max(pu_vals) * base_kv if base_kv else None,
                angle_deg=angle_vals[0] if angle_vals else None,
                phase_labels=labels,
                phase_voltage_pu=[float(v) for v in pu_vals],
                phase_voltage_kv=[float(v) * base_kv for v in pu_vals] if base_kv else [],
                phase_angle_deg=[float(a) for a in angle_vals[:n_ph]],
                unbalance_pct=metrics.unbalance_pct,
                phase_spread_pct=metrics.phase_spread_pct,
                negative_seq_pu=metrics.negative_seq_pu,
                zero_seq_pu=metrics.zero_seq_pu,
                unbalance_status="ok" if metrics.status == "ok" else "not_assessed",
            )
        )
        # 全网扫描：只在三相齐备且正序非零的母线上参与不平衡排名
        if metrics.status == "ok" and metrics.unbalance_pct is not None:
            scan.assessed += 1
            violation = {
                "name": b["name"],
                "unbalance_pct": metrics.unbalance_pct,
                "phase_voltage_pu": [float(v) for v in pu_vals],
            }
            if metrics.unbalance_pct > UNBALANCE_LIMIT_PCT:
                scan.violations.append(violation)
            if scan.worst_unbalance_pct is None or metrics.unbalance_pct > scan.worst_unbalance_pct:
                scan.worst_unbalance_pct = metrics.unbalance_pct
                scan.worst_bus = b["name"]
                scan.worst_phase_voltage_pu = [float(v) for v in pu_vals]
                scan.worst_phase_angle_deg = [float(a) for a in angle_vals[:n_ph]]
                scan.spread_worst_bus = b["name"]
                scan.spread_worst_pct = metrics.phase_spread_pct
        else:
            scan.not_assessed += 1
    scan.violations.sort(key=lambda item: item["unbalance_pct"], reverse=True)
    result.bus_results = bus_results
    result.unbalance_summary = UnbalanceSummary(**scan.to_summary(UNBALANCE_LIMIT_PCT, UNBALANCE_LIMIT_SOURCE))

    # --- branch 结果 -----------------------------------------------------------
    branch_results: list[BranchResult] = []
    for ln in raw.lines:
        rating = ln["rating_a"]
        current = ln["current_a"]
        if _is_valid_number(rating) and rating and rating > 0 and _is_valid_number(current):
            loading = current / rating * 100.0
            loading_status = "ok"
        else:
            loading = None
            loading_status = "unavailable"  # 额定值不可用 → loading=unavailable（文档 13.6）
        branch_results.append(
            BranchResult(
                p_to_kw=ln.get("p_to_kw"), q_to_kvar=ln.get("q_to_kvar"), source_model=ln.get("source_model", ""),
                name=ln["name"],
                from_bus=ln.get("bus1", ""),
                to_bus=ln.get("bus2", ""),
                p_kw=ln["p_kw"] if _is_valid_number(ln["p_kw"]) else None,
                q_kvar=ln["q_kvar"] if _is_valid_number(ln["q_kvar"]) else None,
                current_a=current if _is_valid_number(current) else None,
                rating_a=rating if _is_valid_number(rating) else None,
                loading_pct=loading,
                loading_status=loading_status,
            )
        )
    result.branch_results = branch_results

    # --- transformer 结果 -------------------------------------------------------
    tr_results: list[TransformerResult] = []
    for tr in raw.transformers:
        p = tr["p_kw"]
        q = tr["q_kvar"]
        rated = tr.get("rated_kva")
        if _is_valid_number(p) and _is_valid_number(q) and _is_valid_number(rated) and rated:
            s_kva = (p**2 + q**2) ** 0.5
            loading = s_kva / rated * 100.0
            loading_status = "ok"
        else:
            loading = None
            loading_status = "unavailable"
        tr_results.append(
            TransformerResult(
                p_to_kw=tr.get("p_to_kw"), q_to_kvar=tr.get("q_to_kvar"), source_model=tr.get("source_model", ""),
                name=tr["name"],
                from_bus=tr.get("bus1", "") or "",
                to_bus=tr.get("bus2", "") or "",
                p_kw=p if _is_valid_number(p) else None,
                q_kvar=q if _is_valid_number(q) else None,
                loading_pct=loading,
                loading_status=loading_status,
            )
        )
    result.transformer_results = tr_results

    # --- 系统汇总 ---------------------------------------------------------------
    sys_raw = raw.system
    ok_buses = [b for b in bus_results if b.status == "ok" and b.voltage_pu is not None]
    min_b = min(ok_buses, key=lambda b: b.voltage_pu) if ok_buses else None
    max_b = max(ok_buses, key=lambda b: b.voltage_pu) if ok_buses else None
    total_gen_kw = sys_raw.get("total_power_kw")
    total_loss_kw = sys_raw.get("losses_kw")

    # --- 总负荷：来自**负荷元件求和**，不是源侧倒推 -----------------------------
    # 曾经的实现是 `total_load_kw = 源侧供电 − 网损`。那是**净负荷**
    # （流入电网的净功率），含 PV/发电时等于 `负荷 − 发电`，数值可正可负。
    # 实测有算例源侧 168684 kW 而"总负荷" −57332 kW，
    # 报告就会写出"系统总负荷为 -57332.60 kW"这种一眼错误的结论。
    # 现在 total_load_kw 只认负荷元件本身；净值另存 net_load_kw。
    load_kw_sum = sys_raw.get("load_kw")
    load_kvar_sum = sys_raw.get("load_kvar")
    load_skipped = sys_raw.get("load_elements_skipped")
    load_n = sys_raw.get("load_elements")
    total_load_kw = load_kw_sum if _is_valid_number(load_kw_sum) else None
    total_load_kvar = load_kvar_sum if _is_valid_number(load_kvar_sum) else None
    # 兼容：引擎（或测试替身）没提供元件求和时，退回旧的倒推口径。
    # 此时 total_load_kw 语义其实偏"净负荷"，但至少保持数值可用；
    # `net_load_kw` 会同时给出，报告据 skipped/缺字段情况自行声明。
    legacy_derived = (
        total_gen_kw - total_loss_kw
        if _is_valid_number(total_gen_kw) and _is_valid_number(total_loss_kw)
        else None
    )
    if total_load_kw is None:
        total_load_kw = legacy_derived
    # 净负荷独立字段：始终按"源侧 − 网损"给出，作为功率平衡的参照量。
    net_load_kw = legacy_derived
    gen_kw_sum = sys_raw.get("gen_kw")
    gen_kvar_sum = sys_raw.get("gen_kvar")
    # 损耗占比的基数必须用**负荷**（正的），而不是可能为负的净负荷；
    # 反送功率场景下用净负荷做分母会把合法网损报成负占比。
    load_base_kw = (
        abs(total_load_kw)
        if _is_valid_number(total_load_kw) and abs(total_load_kw) > 1e-9
        else None
    )
    # 真实最低相电压：取所有母线的所有相里的最小值。分相口径下它比平衡等值的
    # min_voltage_pu 更低，才是真正的电压下限（原实现只看"每母线平衡等值"）。
    worst_pair: tuple[float, str, str] | None = None
    for _b in bus_results:
        for _lbl, _v in zip(_b.phase_labels or [], _b.phase_voltage_pu or []):
            if not _is_valid_number(_v):
                continue
            if worst_pair is None or _v < worst_pair[0]:
                worst_pair = (float(_v), _b.name, _lbl)
    result.system_summary = SystemSummary(
        total_shunt_kw=sys_raw.get("shunt_kw"),
        total_shunt_kvar=sys_raw.get("shunt_kvar"),
        shunt_elements=sys_raw.get("shunt_elements"),
        total_branch_shunt_kw=sys_raw.get("branch_shunt_kw"),
        total_branch_shunt_kvar=sys_raw.get("branch_shunt_kvar"),
        branch_shunt_elements=sys_raw.get("branch_shunt_elements"),
        total_load_kw=total_load_kw,
        total_load_kvar=total_load_kvar,
        total_generation_kw=total_gen_kw if _is_valid_number(total_gen_kw) else None,
        total_generation_kvar=sys_raw.get("total_power_kvar")
        if _is_valid_number(sys_raw.get("total_power_kvar"))
        else None,
        net_load_kw=net_load_kw if _is_valid_number(net_load_kw) else None,
        total_generator_kw=gen_kw_sum if _is_valid_number(gen_kw_sum) else None,
        total_generator_kvar=gen_kvar_sum if _is_valid_number(gen_kvar_sum) else None,
        # 储能净消耗：保留「流入元件为正」的原始符号（>0 充电、<0 放电）。
        # 不计入负荷/发电（双向元件，归任一边都会出现"负的负荷/负的发电"），
        # 但必须参与功率平衡 —— 否则含储能充放电的算例残差 = 储能功率。
        total_storage_kw=(
            sys_raw.get("storage_kw")
            if _is_valid_number(sys_raw.get("storage_kw"))
            else None
        ),
        total_storage_kvar=(
            sys_raw.get("storage_kvar")
            if _is_valid_number(sys_raw.get("storage_kvar"))
            else None
        ),
        storage_elements=(
            int(sys_raw.get("storage_elements"))
            if _is_valid_number(sys_raw.get("storage_elements"))
            else None
        ),
        # 仅当引擎确实报告了跳过计数时才填；0 也要如实写 0（区别于"没这项能力"）
        load_elements_skipped=(
            int(load_skipped)
            if isinstance(load_skipped, int) or (
                isinstance(load_skipped, float) and load_skipped == int(load_skipped)
            )
            else None
        ),
        min_voltage_pu=min_b.voltage_pu if min_b else None,
        min_voltage_bus=min_b.name if min_b else None,
        max_voltage_pu=max_b.voltage_pu if max_b else None,
        max_voltage_bus=max_b.name if max_b else None,
        min_phase_voltage_pu=worst_pair[0] if worst_pair else None,
        min_phase_voltage_bus=worst_pair[1] if worst_pair else None,
        min_phase_label=worst_pair[2] if worst_pair else None,
    )
    result.loss_summary = LossSummary(
        total_loss_kw=total_loss_kw if _is_valid_number(total_loss_kw) else None,
        total_loss_kvar=sys_raw.get("losses_kvar")
        if _is_valid_number(sys_raw.get("losses_kvar"))
        else None,
        loss_pct_of_load=(total_loss_kw / load_base_kw * 100.0)
        if _is_valid_number(total_loss_kw) and load_base_kw
        else None,
    )

    # --- 结果域状态：缺失域显式标记（文档 12.4：缺失域标 unavailable/not_assessed）
    # 网络中不存在该类对象 → not_assessed（该维度本就无法评估）；
    # 网络中存在但未产出 → unavailable（受限发布 partial）。
    scope_data = {
        ResultScope.BUS: bool(bus_results),
        ResultScope.BRANCH: bool(branch_results),
        ResultScope.TRANSFORMER: bool(tr_results),
        ResultScope.SYSTEM: _is_valid_number(total_gen_kw),
    }
    network_count = {
        ResultScope.BUS: raw.element_count.get("bus", 0),
        ResultScope.BRANCH: raw.element_count.get("line", 0),
        ResultScope.TRANSFORMER: raw.element_count.get("transformer", 0),
        ResultScope.SYSTEM: 1,
    }
    for scope in ResultScope:
        if network_count.get(scope, 0) == 0:
            result.result_status_by_scope[scope.value] = "not_assessed"
        elif scope not in requested_scopes:
            result.result_status_by_scope[scope.value] = "not_assessed"
        elif scope_data.get(scope):
            result.result_status_by_scope[scope.value] = "ok"
        else:
            result.result_status_by_scope[scope.value] = "unavailable"

    return result
