"""历史结果问答：**按 metric 分派**，不做关键词匹配。

为什么重构（2026-09-29）
------------------------
旧实现把「答什么」交给 `chat_api.py` 里的关键词表（`if "变压器" in lower ...`）。
后果：

1. 覆盖不到的说法就答不上 —— 只能靠不断补关键词维持，
   于是形成「换种问法 → 答不上 → 补一条 → 再换又问不上」的循环；
2. 同一逻辑在文件里重复出现（「N-1」写了 3 份、「Q 限值」2 份、「安全判定」4 份），
   改一处不影响另一处；
3. 真正在做语义判断的是 LLM（`runtime.intent`），它**已经判出了 metric**
   （见 `prompts.intent_classify` 的 slots.metric），但下游把这信息丢了，
   又用字符串匹配把「问的是哪个指标」重猜一遍。

现改为：**LLM 只输出指标名，本模块按指标名取数与组织答案。**
数值仍然全部来自权威产物（`powerflow_result.json` / `network_profile.json` /
`solver_profile.json` / `validation.json`），LLM 不产生任何数值 ——
因此不违反「数值必须来自引擎」的硬约定，也不引入幻觉。
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from ..config import AgentConfig

SUPP_MARK = "[[SUPP]]"

# ---------------------------------------------------------------- metric 名
# 结果指标（有真实数据可取）
METRIC_MIN_VOLTAGE = "min_voltage"
METRIC_MAX_VOLTAGE = "max_voltage"
METRIC_VOLTAGE_OVERVIEW = "voltage_overview"
METRIC_VOLTAGE_LOWEST_LIST = "voltage_lowest_list"
METRIC_VOLTAGE_VIOLATIONS = "voltage_violations"
METRIC_LOSSES = "losses"
METRIC_GENERATION = "generation"
METRIC_LOAD_TOTAL = "load_total"
METRIC_POWER_BALANCE = "power_balance"
METRIC_CONVERGENCE = "convergence"
METRIC_BRANCH_OVERLOAD = "branch_overload"
METRIC_BRANCH_LOADING_TOP = "branch_loading_top"
METRIC_REVERSE_FLOW = "reverse_flow"
METRIC_UNBALANCE = "unbalance"
METRIC_PHASE_VOLTAGE = "phase_voltage"
METRIC_TOPOLOGY = "topology"
METRIC_DEVICE_COUNTS = "device_counts"
METRIC_VOLTAGE_LEVELS = "voltage_levels"
METRIC_SOLVER_CONFIG = "solver_config"
METRIC_FIDELITY = "fidelity"
METRIC_CASE_INFO = "case_info"
METRIC_RESULT_OVERVIEW = "result_overview"
METRIC_CASE_STATISTICS = "case_statistics"
# 点位查询：问「母线 X 的电压是多少」。与 min/max_voltage 的区别是**由问句指定对象**，
# 所以需要从话里定位母线名（确定性匹配），并给出 pu / kV / V 多单位与越限判断。
METRIC_BUS_VOLTAGE = "bus_voltage"

# 能力边界类（尚未实现 → 固定话术，不含数值）
METRIC_CAP_TIMESERIES = "capability_timeseries"
METRIC_CAP_N1 = "capability_n1"
METRIC_CAP_Q_LIMITS = "capability_q_limits"
METRIC_CAP_PHASE_SHIFT = "capability_phase_shift"
METRIC_CAP_TINY_IMPEDANCE = "capability_tiny_impedance"
METRIC_CAP_SAFETY = "capability_safety"

#: 全部合法 metric（提示词白名单、测试、兜底提示共用同一份定义）
METRIC_NAMES: tuple[str, ...] = (
    METRIC_MIN_VOLTAGE, METRIC_MAX_VOLTAGE, METRIC_VOLTAGE_OVERVIEW,
    METRIC_VOLTAGE_LOWEST_LIST, METRIC_VOLTAGE_VIOLATIONS,
    METRIC_LOSSES, METRIC_GENERATION, METRIC_LOAD_TOTAL, METRIC_POWER_BALANCE,
    METRIC_CONVERGENCE, METRIC_BRANCH_OVERLOAD, METRIC_BRANCH_LOADING_TOP,
    METRIC_REVERSE_FLOW, METRIC_UNBALANCE, METRIC_PHASE_VOLTAGE,
    METRIC_TOPOLOGY, METRIC_DEVICE_COUNTS, METRIC_VOLTAGE_LEVELS,
    METRIC_SOLVER_CONFIG, METRIC_FIDELITY, METRIC_CASE_INFO,
    METRIC_RESULT_OVERVIEW, METRIC_CASE_STATISTICS, METRIC_BUS_VOLTAGE,
    METRIC_CAP_TIMESERIES, METRIC_CAP_N1, METRIC_CAP_Q_LIMITS,
    METRIC_CAP_PHASE_SHIFT, METRIC_CAP_TINY_IMPEDANCE, METRIC_CAP_SAFETY,
)

#: metric → 中文名（用于「识别到你在问 X，但还没实现」这类提示）
METRIC_TITLES: dict[str, str] = {
    METRIC_MIN_VOLTAGE: "最低电压",
    METRIC_MAX_VOLTAGE: "最高电压",
    METRIC_VOLTAGE_OVERVIEW: "电压概况",
    METRIC_VOLTAGE_LOWEST_LIST: "电压最低的母线清单",
    METRIC_VOLTAGE_VIOLATIONS: "电压越限母线",
    METRIC_LOSSES: "网损",
    METRIC_GENERATION: "源侧供电",
    METRIC_LOAD_TOTAL: "负荷",
    METRIC_POWER_BALANCE: "功率平衡",
    METRIC_CONVERGENCE: "收敛状态",
    METRIC_BRANCH_OVERLOAD: "过载支路",
    METRIC_BRANCH_LOADING_TOP: "负载率排名",
    METRIC_REVERSE_FLOW: "反向潮流",
    METRIC_UNBALANCE: "三相不平衡度",
    METRIC_PHASE_VOLTAGE: "分相电压",
    METRIC_TOPOLOGY: "网络拓扑",
    METRIC_DEVICE_COUNTS: "设备数量",
    METRIC_VOLTAGE_LEVELS: "电压等级",
    METRIC_SOLVER_CONFIG: "求解参数",
    METRIC_FIDELITY: "转换保真度",
    METRIC_CASE_INFO: "算例来源",
    METRIC_RESULT_OVERVIEW: "结果概况",
    METRIC_CASE_STATISTICS: "算例统计",
    METRIC_BUS_VOLTAGE: "指定母线的电压",
    METRIC_CAP_TIMESERIES: "24 小时时序扫描",
    METRIC_CAP_N1: "N-1 开断分析",
    METRIC_CAP_Q_LIMITS: "发电机无功限值与 PV→PQ",
    METRIC_CAP_PHASE_SHIFT: "变压器相移",
    METRIC_CAP_TINY_IMPEDANCE: "极小阻抗",
    METRIC_CAP_SAFETY: "运行安全性判定",
}


# ---------------------------------------------------------------- 产物读取
def load_artifact(ctx: dict[str, Any], name: str) -> dict[str, Any]:
    """读取最近一次成功任务的结构化产物；只读，不触发重新计算。"""
    task_id = ctx.get("last_success_task_id") or ctx.get("last_task_id")
    if not task_id:
        return {}
    outputs_root = os.path.abspath(AgentConfig().outputs_root)
    path = os.path.join(outputs_root, task_id, name)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


class _Arts:
    """一次问答用到的全部产物（惰性载入，缺失即空字典）。"""

    def __init__(self, ctx: dict[str, Any]) -> None:
        self.ctx = ctx
        self.result = load_artifact(ctx, "powerflow_result.json")
        self.profile = load_artifact(ctx, "network_profile.json")
        self.validation = load_artifact(ctx, "validation.json")
        self.solver = load_artifact(ctx, "solver_profile.json")
        self.case = self.validation.get("case") or {}
        self.sys = self.result.get("system_summary") or {}
        self.loss = self.result.get("loss_summary") or {}
        self.solver_status = self.result.get("solver_status") or {}
        self.branches = [
            b for b in (self.result.get("branch_results") or [])
            if b.get("loading_pct") is not None
        ]
        self.buses = [
            b for b in (self.result.get("bus_results") or [])
            if b.get("voltage_pu") is not None
        ]
        # 母线名 → 该母线结果。点位查询（「母线 X 电压多少」）与电压等级归属都靠它，
        # 也让这两处不必各自去遍历 bus_results。
        self.bus_by_name: dict[str, dict[str, Any]] = {}
        for _b in self.buses:
            _nm = str(_b.get("name") or "").strip().lower()
            if _nm:
                self.bus_by_name.setdefault(_nm, _b)

    @property
    def has_any(self) -> bool:
        return bool(self.result or self.profile or self.case)


HEAD = "根据上一次已解析的算例产物（本次不重算）："
HEAD_SOLVER = "根据上一次的求解策略（solver_profile，本次不重算）："
HEAD_RESULT = "根据上一次的计算结果："


def _f(v: Any, digits: int = 4) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------- 结果指标
def _bus_level_suffix(a: _Arts, bus_name: Any) -> str:
    """母线 → 「｜基准 X kV」后缀。

    实测痛点：问「最低电压那条母线属于哪个电压等级」时只答得出母线名与 pu，
    而等级信息本就在产物里（`bus_results[].base_kv` —— 实测 case31 有 269 条）。
    这里顺带把等级带上，不必让用户再追问一次。
    """
    nm = str(bus_name or "").strip().lower()
    if not nm:
        return ""
    b = a.bus_by_name.get(nm)
    if not b:
        return ""
    base = b.get("base_kv")
    if base is None:
        return ""
    return f"　｜基准 {_f(base, 3)} kV"


def _h_min_voltage(a: _Arts, content: str, slots: dict) -> str | None:
    if a.sys.get("min_voltage_pu") is None:
        return None
    bus = a.sys.get("min_voltage_bus", "?")
    return (
        f"{HEAD}\n- 最低电压：{_f(a.sys['min_voltage_pu'])} pu（母线 {bus}）"
        f"{_bus_level_suffix(a, bus)}"
    )


def _h_max_voltage(a: _Arts, content: str, slots: dict) -> str | None:
    if a.sys.get("max_voltage_pu") is None:
        return None
    bus = a.sys.get("max_voltage_bus", "?")
    return (
        f"{HEAD}\n- 最高电压：{_f(a.sys['max_voltage_pu'])} pu（母线 {bus}）"
        f"{_bus_level_suffix(a, bus)}"
    )


def _voltage_lines(a: _Arts) -> list[str]:
    out: list[str] = []
    if a.sys.get("min_voltage_pu") is not None:
        _b = a.sys.get("min_voltage_bus", "?")
        out.append(
            f"- 最低电压：{_f(a.sys['min_voltage_pu'])} pu（母线 {_b}）"
            f"{_bus_level_suffix(a, _b)}"
        )
    if a.sys.get("max_voltage_pu") is not None:
        _b = a.sys.get("max_voltage_bus", "?")
        out.append(
            f"- 最高电压：{_f(a.sys['max_voltage_pu'])} pu（母线 {_b}）"
            f"{_bus_level_suffix(a, _b)}"
        )
    return out


def _h_voltage_overview(a: _Arts, content: str, slots: dict) -> str | None:
    body = _voltage_lines(a)
    if not body:
        return None
    lo = AgentConfig().defaults.voltage_min_pu
    hi = AgentConfig().defaults.voltage_max_pu
    body.append(f"- 判据区间：{lo:.2f}~{hi:.2f} pu（仿真默认阈值，非工程审批限值）")
    return "\n".join([HEAD] + body)


def _h_voltage_lowest_list(a: _Arts, content: str, slots: dict) -> str | None:
    try:
        n = int(slots.get("top_n") or 5)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(n, 50))
    valid = sorted(a.buses, key=lambda b: float(b["voltage_pu"]))
    if not valid:
        return None
    lines = [HEAD, f"- 电压最低的 {n} 个母线："]
    lines += [
        f"  {i}. {b.get('name', '?')}：{float(b['voltage_pu']):.4f} pu"
        # 每条都带基准电压：实测「最低电压那条母线属于哪个电压等级」被意图层判成
        # 「电压最低清单」，于是等级信息不在答案里 —— 这里**不依赖意图层判对**，
        # 清单本身就把等级带上。
        f"{_bus_level_suffix(a, b.get('name'))}"
        for i, b in enumerate(valid[:n], 1)
    ]
    return "\n".join(lines)


def _h_voltage_violations(a: _Arts, content: str, slots: dict) -> str | None:
    lo = AgentConfig().defaults.voltage_min_pu
    hi = AgentConfig().defaults.voltage_max_pu
    violations = [
        b for b in a.buses if float(b["voltage_pu"]) < lo or float(b["voltage_pu"]) > hi
    ]
    lines = [
        HEAD,
        f"- 按默认电压阈值 {lo:.2f}~{hi:.2f} pu，越限母线 {len(violations)} 个。",
    ]
    lines += [
        f"  - {b.get('name', '?')}：{float(b['voltage_pu']):.4f} pu" for b in violations[:20]
    ]
    if not violations:
        lines.append("- 没有母线超出该区间。")
    return "\n".join(lines)


def _h_losses(a: _Arts, content: str, slots: dict) -> str | None:
    body: list[str] = []
    loss_kw = a.loss.get("total_loss_kw")
    if loss_kw is not None:
        body.append(f"- 系统总有功损耗（网损）：{_f(loss_kw, 4)} kW")
        # 多分母一起给出：实测「网损占源侧供电的比例」原先只答出「占总负荷」，
        # 分母没换 —— 而源侧供电与净负荷本来就在产物里，不必让用户再问一次。
        pct = a.loss.get("loss_pct_of_load")
        if pct is not None:
            body.append(f"  - 占总负荷：{_f(pct, 2)}%")
        gen = a.sys.get("total_generation_kw")
        if gen:
            body.append(
                f"  - 占源侧供电：{float(loss_kw) / float(gen) * 100.0:.2f}%"
                f"（源侧 {_f(gen, 3)} kW）"
            )
        net = a.sys.get("net_load_kw")
        if net:
            body.append(
                f"  - 占净负荷：{float(loss_kw) / float(net) * 100.0:.2f}%"
                f"（净负荷 {_f(net, 3)} kW）"
            )
    if a.loss.get("total_loss_kvar") is not None:
        body.append(f"- 系统总无功损耗：{_f(a.loss['total_loss_kvar'], 4)} kvar")
    if not body:
        return None
    return "\n".join([HEAD] + body)


def _h_generation(a: _Arts, content: str, slots: dict) -> str | None:
    body: list[str] = []
    if a.sys.get("total_generation_kw") is not None:
        body.append(f"- 源侧总供电：{_f(a.sys['total_generation_kw'], 3)} kW")
    if a.sys.get("total_generator_kw") is not None:
        body.append(f"- 发电注入：{_f(a.sys['total_generator_kw'], 3)} kW")
    if not body:
        return None
    return "\n".join([HEAD] + body)


def _h_load_total(a: _Arts, content: str, slots: dict) -> str | None:
    body: list[str] = []
    if a.sys.get("total_load_kw") is not None:
        body.append(
            f"- 总负荷：{_f(a.sys['total_load_kw'], 3)} kW（各负荷元件端子1有功之和）"
        )
    if a.sys.get("net_load_kw") is not None:
        body.append(f"- 净负荷（源侧供电 − 网损）：{_f(a.sys['net_load_kw'], 3)} kW")
    if a.sys.get("load_elements_skipped"):
        body.append(
            f"- 有 {int(a.sys['load_elements_skipped'])} 个负荷元件读数缺失未计入求和，总负荷可能偏小"
        )
    if not body:
        return None
    body.append("- 注：总负荷与净负荷口径不同，含发电/反送时两者不相等。")
    return "\n".join([HEAD] + body)


def _h_power_balance(a: _Arts, content: str, slots: dict) -> str | None:
    verification = load_artifact(a.ctx, "verification.json")
    residual = verification.get("power_balance_residual_pu")
    if residual is None:
        return None
    tol = verification.get("power_balance_tolerance")
    return (
        f"{HEAD}\n- 功率平衡残差：{_f(residual, 6)} pu"
        f"（冻结容差 {tol}）"
    )


def convergence_summary(solver: dict[str, Any]) -> str:
    """区分当前解的迭代记录和累计显式求解计数，兼容历史产物缺少累计字段。"""
    parts = [f"当前解迭代 {solver.get('iterations', '?')} 次"]
    total = solver.get("total_iterations")
    calls = solver.get("raw_solution_count")
    if total is not None:
        parts.append(f"累计迭代 {total} 次")
    if calls is not None:
        parts.append(f"显式 Solve 调用 {calls} 次")
    if total is not None or calls is not None:
        parts.append("累计/调用次数不含 Compile 内部的隐式 Solve")
    return f"- 收敛状态：{'已收敛' if solver.get('converged') else '未收敛'}（{'；'.join(parts)}）"


def _h_convergence(a: _Arts, content: str, slots: dict) -> str | None:
    if not a.solver_status:
        return None
    lines = [
        HEAD,
        convergence_summary(a.solver_status),
        "- 解释：潮流收敛只表示方程存在数值解，不代表低压、热过载或无功约束均满足。",
    ]
    return "\n".join(lines)


def _h_branch_overload(a: _Arts, content: str, slots: dict) -> str | None:
    if not a.branches:
        return None
    limit = AgentConfig().defaults.loading_limit_pct
    over = [b for b in a.branches if float(b["loading_pct"]) > limit]
    return (
        f"{HEAD}\n- 超过 {limit:.0f}% 负载率的支路：{len(over)} 条。"
        f"\n- 判据：仿真默认热限值 {limit:.0f}%，非工程审批限值。"
    )


def _h_branch_loading_top(a: _Arts, content: str, slots: dict) -> str | None:
    if not a.branches:
        return None
    try:
        n = int(slots.get("top_n") or 3)
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 20))
    ranked = sorted(a.branches, key=lambda b: float(b["loading_pct"]), reverse=True)
    lines = [HEAD, f"- 负载率最高的 {n} 条支路："]
    lines += [
        f"  {i}. {b.get('name', '?')}：{float(b['loading_pct']):.2f}%"
        for i, b in enumerate(ranked[:n], 1)
    ]
    return "\n".join(lines)


def _h_reverse_flow(a: _Arts, content: str, slots: dict) -> str | None:
    all_branches = a.result.get("branch_results") or []
    reverse = [
        b for b in all_branches if b.get("p_kw") is not None and float(b["p_kw"]) < 0
    ]
    high_v = [b for b in a.buses if float(b["voltage_pu"]) > 1.05]
    lines = [
        HEAD,
        f"- 首端有功为负的反向潮流支路：{len(reverse)} 条。负号表示反送/净注入方向，不代表数据非法。",
        f"- 电压超过 1.05 pu 的母线：{len(high_v)} 个。",
        "- 反向潮流需结合支路首端 p_kw<0、系统净功率与分布式电源结构判断；"
        "当前结果来自转换后的 OpenDSS 等值模型，不能宣称与原始 AC NR 等价。",
    ]
    return "\n".join(lines)


def _h_unbalance(a: _Arts, content: str, slots: dict) -> str | None:
    ub = a.result.get("unbalance_summary") or {}
    if not ub or not ub.get("buses_assessed"):
        return (
            f"{HEAD}\n"
            "- 该算例没有可评估的三相母线（相数不足或相序异常），不平衡度无法给出。\n"
            + SUPP_MARK
            + "\n- 说明：不平衡度按对称分量法 ε=|U₂|/|U₁| 计算，需要三相齐备。"
        )
    out = [HEAD]
    if ub.get("worst_bus"):
        out.append(
            f"- **不平衡最严重：{ub['worst_bus']}，ε = {_f(ub['worst_unbalance_pct'], 3)}%**"
        )
        phv = ub.get("worst_phase_voltage_pu") or []
        if phv:
            out.append(
                "- 该母线三相电压（pu）："
                + " / ".join(f"{'ABC'[i]} {_f(v, 5)}" for i, v in enumerate(phv[:3]))
            )
    if a.sys.get("min_phase_voltage_pu") is not None:
        out.append(
            f"- 全网真实最低相电压：**{_f(a.sys['min_phase_voltage_pu'])} pu**"
            f"（{a.sys.get('min_phase_voltage_bus')} 的 {a.sys.get('min_phase_label')} 相）"
        )
    tail: list[str] = []
    if ub.get("buses_over_limit_count"):
        tail.append(
            f"- 超过参考限值 {ub.get('limit_pct')}% 的母线：{ub['buses_over_limit_count']} 个"
        )
        for v in (ub.get("buses_over_limit") or [])[:5]:
            phv = v.get("phase_voltage_pu") or []
            ph_txt = " / ".join(f"{'ABC'[i]} {_f(x, 5)}" for i, x in enumerate(phv[:3]))
            tail.append(f"  - {v.get('name')}：ε={_f(v.get('unbalance_pct'), 3)}%（{ph_txt}）")
    if ub.get("spread_worst_pct") is not None:
        tail.append(
            f"- 最大相间偏差（辅助指标，非标准值）：{_f(ub['spread_worst_pct'], 3)}%"
            f"（{ub.get('spread_worst_bus')}）"
        )
    tail.append(f"- 计算方法：{ub.get('method')}")
    tail.append(f"- 限值口径：{ub.get('limit_source')}")
    tail.append(
        "- 口径：报告里「电压（平衡等值）」取三相最大值，仅用于与平衡模型参考值对比；"
        "不平衡判断请以分相数据为准。"
    )
    out.append(SUPP_MARK)
    out += tail
    return "\n".join(out)


_PHASE_LABELS = ("a相", "b相", "c相", "a 相", "b 相", "c 相")


def _h_phase_voltage(a: _Arts, content: str, slots: dict) -> str | None:
    lower = content.lower()
    asked = slots.get("phase")
    if not asked:
        asked = next((w[0].upper() for w in _PHASE_LABELS if w in lower), None)
    if asked and str(asked).upper() in "ABC":
        idx = "ABC".index(str(asked).upper())
        worst: tuple[float, str] | None = None
        for b in a.result.get("bus_results") or []:
            ph = b.get("phase_voltage_pu") or []
            if idx < len(ph) and isinstance(ph[idx], (int, float)):
                if worst is None or ph[idx] < worst[0]:
                    worst = (float(ph[idx]), str(b.get("name")))
        if worst:
            label = str(asked).upper()
            return (
                f"{HEAD}\n"
                f"- 全网最低 **{label} 相**电压：{_f(worst[0])} pu（母线 {worst[1]}）"
            )
    # 未指明相别 → 退化为不平衡/分相概况
    return _h_unbalance(a, content, slots)


def _h_topology(a: _Arts, content: str, slots: dict) -> str | None:
    cycle_rank = a.profile.get("cycle_rank")
    parallel = a.profile.get("parallel_line_pairs", 0)
    lines = [HEAD]
    if cycle_rank is not None:
        topology = "网状/含环路" if cycle_rank > 0 else "辐射型（未检测到环路）"
        lines.append(f"- 拓扑判断：{topology}；cycle_rank={cycle_rank}")
    else:
        lines.append("- 拓扑判断：当前产物未保存可发布的环路指标，不能仅凭是否收敛推断。")
    lines.append(f"- 并行线路端点对：{parallel} 对")
    if a.profile.get("notes"):
        lines.append(f"- 结构证据：{'；'.join(a.profile['notes'])}")
    return "\n".join(lines)


# (展示名, 数据键, 数据源, 量词)
#
# 2026-09-29 修正：本表原为 5 元组（多一列「关键词」，是重构前关键词匹配的遗留），
# 而两处消费点（_h_device_counts 的全量分支、_h_case_statistics）按 4 元组解包，
# 于是无 `device` 槽位时必然抛 `ValueError: too many values to unpack` —— 该异常被
# `answer_by_metric` 的兜底 except 吞掉，对外表现为"上一次的结果产物里没有这项数据"。
# 即：**「设备数量」的整表列举与「算例统计」两个指标从未真正工作过**。
# 现统一为 4 元组，与 _DEVICE_ALIASES 的 value 同构，并在下方加结构守卫防止再漂移。
_DEVICE_MAP = (
    ("变压器", "transformer", "obj", "台"),
    ("母线", "bus", "obj", "个"),
    ("线路", "line", "obj", "条"),
    ("负荷", "load", "obj", "个"),
    ("发电机", "generator_count", "profile", "台"),
    ("光伏", "pv_system_count", "profile", "个"),
    ("储能", "storage_count", "profile", "个"),
    ("电容器", "capacitor_count", "profile", "个"),
    ("调压器", "regulator_count", "profile", "台"),
)
_DEVICE_ALIASES = {
    "transformer": ("变压器", "transformer", "obj", "台"),
    "bus": ("母线", "bus", "obj", "个"),
    "line": ("线路", "line", "obj", "条"),
    "load": ("负荷", "load", "obj", "个"),
    "generator": ("发电机", "generator_count", "profile", "台"),
    "pvsystem": ("光伏", "pv_system_count", "profile", "个"),
    "storage": ("储能", "storage_count", "profile", "个"),
    "capacitor": ("电容器", "capacitor_count", "profile", "个"),
    "regulator": ("调压器", "regulator_count", "profile", "台"),
}

# 用户话里「提到了哪几类设备」的文本线索（键必须与 _DEVICE_ALIASES 的键一致）。
#
# 为什么需要它：意图层对同一句话给出的 slots 并不稳定 ——
# 「线路、变压器、负荷各有多少」实测两次，一次给 `metric=device_counts`（无 device，
# 走整表列举），另一次给 `metric=device_counts, device="line"`（只答线路）。
# 后者会让用户问了三类只得到一类，而且**没有任何"只答了一部分"的提示**。
# 因此这里以「话里实际提到几类」为准，而不是只信意图层给的那一个槽位。
_DEVICE_TEXT_HINTS: dict[str, tuple[str, ...]] = {
    "transformer": ("变压器",),
    "bus": ("母线", "节点"),
    # 注意：「支路」**不在此列**。工程口径的"支路"= 线路 + 变压器，
    # 把它当作"线路"的别名会让答案漏掉变压器（实测答"支路"给的是"线路 264 条"）。
    # 它由 `_h_device_counts` 开头单独处理。
    "line": ("线路", "馈线"),
    "load": ("负荷",),
    "generator": ("发电机",),
    "pvsystem": ("光伏",),
    "storage": ("储能",),
    "capacitor": ("电容",),
    "regulator": ("调压", "分接头"),
}

# 结构守卫：两张设备表必须同构，且文本线索表必须覆盖同一批设备。
# 历史缺陷：_DEVICE_MAP 曾为 5 元组、_DEVICE_ALIASES 为 4 元组，按 4 元组解包的两处
# 消费点在「无 device 槽位」时必然抛 ValueError，又被兜底 except 吞成"没有这项数据"。
# 这类"定义漂移 → 静默失效"必须在 import 期就炸出来，不能等用户试出来。
_DEVICE_SHAPE = {len(x) for x in _DEVICE_MAP} | {len(v) for v in _DEVICE_ALIASES.values()}
if len(_DEVICE_SHAPE) != 1:
    raise RuntimeError(
        "设备表结构不一致（元组长度 %s）：_DEVICE_MAP 与 _DEVICE_ALIASES 必须同构，"
        "否则消费点解包会抛 ValueError 并被静默吞掉。" % sorted(_DEVICE_SHAPE)
    )
if set(_DEVICE_TEXT_HINTS) != set(_DEVICE_ALIASES):
    raise RuntimeError(
        "设备文本线索表与别名表键不一致：缺少 %s，多余 %s"
        % (sorted(set(_DEVICE_ALIASES) - set(_DEVICE_TEXT_HINTS)),
           sorted(set(_DEVICE_TEXT_HINTS) - set(_DEVICE_ALIASES)))
    )


def _mentioned_devices(content: str) -> list[str]:
    """用户话里提到了哪几类设备（按 _DEVICE_TEXT_HINTS 的声明序返回设备键）。"""
    lower = (content or "").lower()
    return [
        k for k, words in _DEVICE_TEXT_HINTS.items()
        if any(w.lower() in lower for w in words)
    ]


def _h_device_counts(a: _Arts, content: str, slots: dict) -> str | None:
    obj = a.case.get("object_summary") or {}
    asked = str(slots.get("device") or "").strip().lower()
    mentioned = _mentioned_devices(content)
    # 「支路」= 线路 + 变压器（工程口径的合计），不是"线路"的别名。
    # 实测问「这个算例一共有多少条支路」原先答「线路 264 条」，漏掉 16 台变压器。
    if "支路" in (content or "") and not mentioned:
        line_n = obj.get("line")
        tr_n = obj.get("transformer")
        if line_n is None:
            line_n = a.profile.get("line_count")
        if tr_n is None:
            tr_n = a.profile.get("transformer_count")
        if line_n is not None or tr_n is not None:
            return "\n".join([
                HEAD,
                f"- 支路合计：{(line_n or 0) + (tr_n or 0)} 条"
                f"（线路 {line_n} + 变压器 {tr_n}）",
            ])
    rows: list[tuple[str, str, str, str]] = []
    if len(mentioned) >= 2:
        # 问了几类就答几类。实测：同一句「线路、变压器、负荷各有多少」，意图层有时
        # 给 device="line"、有时不给 device —— 若只信槽位，用户问三类只会得到一类，
        # 且没有任何"只答了一部分"的提示。整表列举既覆盖所问，也便于回答"哪个最多"。
        rows = list(_DEVICE_MAP)
    elif asked and asked in _DEVICE_ALIASES:
        rows = [_DEVICE_ALIASES[asked]]
    elif len(mentioned) == 1:
        rows = [_DEVICE_ALIASES[mentioned[0]]]
    else:
        rows = list(_DEVICE_MAP)
    lines = [HEAD]
    for zh, key, src, unit in rows:
        value = obj.get(key) if src == "obj" else a.profile.get(key)
        if value is not None:
            lines.append(f"- {zh}：{value} {unit}")
    if len(lines) == 1:
        return None
    if not asked:
        row_keys = {(k, s) for _, k, s, _ in rows}
        extra: list[str] = []
        for label, key in (
            ("Slack 母线", "slack_bus_count"), ("PV 母线（画像口径）", "pv_bus_count"),
            ("PQ 母线（画像口径）", "pq_bus_count"),
        ):
            if key in a.profile:
                extra.append(f"- {label}：{a.profile[key]}")
        single_n = a.profile.get("single_phase_element_count")
        if single_n is not None:
            extra.append(f"- 单相元件：{single_n} 个（结构性不平衡的来源）")
            if single_n:
                extra.append(
                    "- **存在单相元件**：三相电压将有差异，越限判断应看**分相**口径，"
                    "而不是「平衡等值」口径。"
                )
        if extra:
            lines.append(SUPP_MARK)
            lines += extra
        _ = row_keys
    return "\n".join(lines)


def _h_voltage_levels(a: _Arts, content: str, slots: dict) -> str | None:
    vls = sorted(float(v) for v in (a.profile.get("voltage_levels_kv") or []))
    if not vls:
        return None
    lines = [HEAD, f"- 电压等级 {len(vls)} 个：" + " / ".join(f"{v:g} kV" for v in vls)]
    # 实测「最高和最低电压等级差多少倍」原先只列出等级、没算倍率 —— 算术本就能做。
    if len(vls) >= 2 and vls[0] > 0:
        lines.append(
            f"- 最高与最低相差：{vls[-1] / vls[0]:.4g} 倍（{vls[-1]:g} / {vls[0]:g} kV）"
        )
    return "\n".join(lines)


def _h_solver_config(a: _Arts, content: str, slots: dict) -> str | None:
    if not a.solver:
        return None
    tol = (a.solver.get("tolerance_config") or {}).get("tolerance")
    maxit = (a.solver.get("iteration_config") or {}).get("max_iterations")
    mode = a.solver.get("solve_mode")
    control = a.solver.get("control_behavior")
    field = str(slots.get("solver_field") or "").strip().lower()
    main: list[str] = []
    if field in ("tolerance", "容差") and tol is not None:
        main.append(f"- 收敛容差：{tol}")
    elif field in ("max_iterations", "迭代", "迭代上限") and maxit is not None:
        main.append(f"- 最大迭代次数：{maxit}")
    elif field in ("solve_mode", "模式", "求解模式") and mode:
        main.append(f"- 求解模式：{mode}（控制行为：{control}）")
    else:
        if tol is not None:
            main.append(f"- 收敛容差：{tol}")
        if maxit is not None:
            main.append(f"- 最大迭代次数：{maxit}")
        if mode:
            main.append(f"- 求解模式：{mode}（控制行为：{control}）")
    if not main:
        return None
    out = [HEAD_SOLVER] + main
    supp: list[str] = []
    for e in a.solver.get("selection_evidence") or []:
        detail = str(e.get("detail", ""))
        if not field or field in detail:
            supp.append("- 参数依据：" + detail)
    if supp:
        out.append(SUPP_MARK)
        out += supp
    return "\n".join(out)


def _h_fidelity(a: _Arts, content: str, slots: dict) -> str | None:
    source_format = a.case.get("source_format")
    if not source_format:
        return None
    lines = [
        HEAD,
        f"- 源格式：{source_format}；保真度状态：**{a.case.get('fidelity_status') or 'not_assessed'}**",
    ]
    for w in a.case.get("fidelity_warnings") or []:
        lines.append(f"- 保真度告警：{w}")
    lines += [
        "- 结论：当前转换结果不能等价替代原始求解器结果。",
        "- PV 母线电压控制、发电机无功上下限及 PV→PQ 切换：未完整等价保留。",
        "- branch ratio / 变压器参数：已尝试映射为 OpenDSS 等值对象，仍需逐项核对。",
    ]
    return "\n".join(lines)


def _h_case_info(a: _Arts, content: str, slots: dict) -> str | None:
    master = a.case.get("master_dss_ref")
    if not master:
        return None
    lines = [
        HEAD,
        f"- 算例：`{master}`（来源类型：{a.case.get('source_type', '?')}）",
        f"- 算例指纹：`{a.case.get('case_fingerprint', '?')}`",
    ]
    if a.case.get("source_format"):
        lines.append(f"- 输入格式：`{a.case['source_format']}`")
    return "\n".join(lines)


def _h_result_overview(a: _Arts, content: str, slots: dict) -> str | None:
    body = _voltage_lines(a)
    if a.solver_status.get("converged") is not None:
        body.insert(
            0,
            f"- 收敛状态：{'已收敛' if a.solver_status['converged'] else '未收敛'}"
            f"（迭代 {a.solver_status.get('iterations', '?')} 次）",
        )
    if a.loss.get("total_loss_kw") is not None:
        body.append(f"- 系统总有功损耗：{_f(a.loss['total_loss_kw'], 4)} kW")
    if a.sys.get("total_load_kw") is not None:
        body.append(f"- 总负荷：{_f(a.sys['total_load_kw'], 3)} kW")
    if not body:
        return None
    return "\n".join([HEAD_RESULT] + body)


def _h_case_statistics(a: _Arts, content: str, slots: dict) -> str | None:
    lines = [HEAD]
    obj = a.case.get("object_summary") or {}
    seen: set[tuple[str, str]] = set()
    for zh, key, src, unit in _DEVICE_MAP:
        if (key, src) in seen:
            continue
        seen.add((key, src))
        value = obj.get(key) if src == "obj" else a.profile.get(key)
        if value is not None:
            lines.append(f"- {zh}：{value} {unit}")
    vls = a.profile.get("voltage_levels_kv") or []
    if vls:
        lines.append("- 电压等级：" + " / ".join(f"{float(v):g} kV" for v in vls))
    if len(lines) == 1:
        return None
    return "\n".join(lines)


def _find_bus(a: _Arts, content: str) -> dict[str, Any] | None:
    """从问句里定位具体母线（**确定性匹配**，不依赖意图层槽位）。

    匹配规则（两条都是实测踩出来的）：
    * **要求词边界**：纯子串匹配会让「f1_1」命中「f1_18」、「source」命中
      「sourcebus」类前缀误判 —— 所以两侧不能紧邻 `[0-9a-z_]`。
      （中文相邻是允许的：「母线F3_10_3的电压」也要命中。）
    * **长名优先**：`f1_10` 比 `f1_1` 更具体，先试长的。
    """
    lower = (content or "").lower()
    for nm in sorted(a.bus_by_name, key=len, reverse=True):
        if len(nm) < 2:
            continue
        if re.search(r"(?<![0-9a-z_])" + re.escape(nm) + r"(?![0-9a-z_])", lower):
            return a.bus_by_name[nm]
    return None


def _h_bus_voltage(a: _Arts, content: str, slots: dict) -> str | None:
    """点位查询：问句里点名了某条母线时，答该母线电压（多单位 + 分相 + 越限）。

    实测痛点：问「母线 F3_10_3 现在的电压是多少伏」原先落到 min/max 概览 ——
    既没有该母线的分相数据，也没有 pu → kV/V 换算。
    """
    b = _find_bus(a, content)
    if b is None:
        return None
    name = b.get("name", "?")
    lines = [HEAD, f"- 母线 {name}："]
    pu = b.get("voltage_pu")
    base = b.get("base_kv")
    if pu is not None:
        lines.append(f"  - 电压（平衡等值口径，取三相最大值）：{_f(pu)} pu")
    kv = b.get("voltage_kv")
    if kv is None and pu is not None and base is not None:
        kv = float(pu) * float(base)
    if kv is not None:
        lines.append(f"  - 折合线电压：{_f(kv, 4)} kV（{_f(float(kv) * 1000.0, 1)} V）")
    if base is not None:
        lines.append(f"  - 该母线基准电压：{_f(base, 3)} kV")
    labels = list(b.get("phase_labels") or [])
    ph = list(b.get("phase_voltage_pu") or [])
    if ph:
        pairs = "；".join(
            f"{labels[i] if i < len(labels) else '?'} 相 {_f(v)} pu" for i, v in enumerate(ph)
        )
        lines.append(f"  - 分相电压（pu）：{pairs}")
    if pu is not None:
        lo = AgentConfig().defaults.voltage_min_pu
        hi = AgentConfig().defaults.voltage_max_pu
        v = float(pu)
        state = ("在判据区间内" if lo <= v <= hi
                 else ("越限（低于下限）" if v < lo else "越限（高于上限）"))
        lines.append(f"  - 判据 {lo:.2f}~{hi:.2f} pu：{state}（仿真默认阈值，非工程审批限值）")
    if len(lines) <= 2:
        return None
    return "\n".join(lines)


# ---------------------------------------------------------------- 能力边界
_CAP_TEXT: dict[str, str] = {
    METRIC_CAP_TIMESERIES: (
        "根据当前能力边界：当前版本可以解析时序对象和 LoadShape 元数据，"
        "但尚未执行完整 24 h Daily 逐时潮流，因此不能给出反向潮流最强时刻、"
        "最高母线电压时刻或中午/晚高峰的实测差值。"
        "当前已有结果是单场景 snapshot，不应冒充全天扫描。"
    ),
    METRIC_CAP_N1: (
        "根据当前能力边界：当前版本可以读取已有任务结果，但尚未实现自动按 manifest "
        "指定支路执行完整 N-1 场景、保存场景身份并与基态做受控对比。"
        "当前历史结果不能冒充 N-1 结果；需要明确停运元件后发起独立场景任务。"
    ),
    METRIC_CAP_Q_LIMITS: (
        "根据当前结果：基态潮流可以收敛，但当前版本尚未完整执行发电机 Q 限值和 "
        "PV→PQ 外层迭代，因此不能声称已完成 Q 限值可行性验证。"
        "若强制限值后发散，应优先判断无功可行域/电压稳定裕度不足，"
        "并同时排查数值条件，而不是简单归因于算法失败。"
    ),
    METRIC_CAP_PHASE_SHIFT: (
        "- MATPOWER 的 branch angle/shift_deg 单位是度（degree），不是弧度。\n"
        "- 若忽略相移或把度误当弧度，会改变变压器复数变比，从而影响两端相角和有功潮流分布。\n"
        "- 当前版本已识别该字段，但转换模型尚未完整执行复数变比等价映射，"
        "不能伪造相移后的精确差值。"
    ),
    METRIC_CAP_TINY_IMPEDANCE: (
        "当前版本不会把极小但非零阻抗自动等同于精确零阻抗；"
        "不过该类支路可能恶化 Ybus 条件数。"
        "是否数值稳定应以求解收敛、复现性、残差和物理合理性联合判断。"
    ),
    METRIC_CAP_SAFETY: (
        "运行安全性需要多项联合判断，潮流收敛不是充分条件：\n"
        "- 潮流收敛只表示方程存在数值解，不代表低压、热过载或无功约束均满足；\n"
        "- 热过载属于运行约束问题，应按额定容量/额定电流判断；\n"
        "- 无功可行性需要实际执行发电机 Q 限值与 PV→PQ 切换，当前版本未完成该外层求解。"
    ),
}


def _make_cap_handler(metric: str) -> Callable[[_Arts, str, dict], str | None]:
    def _h(a: _Arts, content: str, slots: dict) -> str | None:
        return _CAP_TEXT.get(metric)

    return _h


# ---------------------------------------------------------------- 分派表
_HANDLERS: dict[str, Callable[[_Arts, str, dict], str | None]] = {
    METRIC_MIN_VOLTAGE: _h_min_voltage,
    METRIC_MAX_VOLTAGE: _h_max_voltage,
    METRIC_VOLTAGE_OVERVIEW: _h_voltage_overview,
    METRIC_VOLTAGE_LOWEST_LIST: _h_voltage_lowest_list,
    METRIC_VOLTAGE_VIOLATIONS: _h_voltage_violations,
    METRIC_LOSSES: _h_losses,
    METRIC_GENERATION: _h_generation,
    METRIC_LOAD_TOTAL: _h_load_total,
    METRIC_POWER_BALANCE: _h_power_balance,
    METRIC_CONVERGENCE: _h_convergence,
    METRIC_BRANCH_OVERLOAD: _h_branch_overload,
    METRIC_BRANCH_LOADING_TOP: _h_branch_loading_top,
    METRIC_REVERSE_FLOW: _h_reverse_flow,
    METRIC_UNBALANCE: _h_unbalance,
    METRIC_PHASE_VOLTAGE: _h_phase_voltage,
    METRIC_TOPOLOGY: _h_topology,
    METRIC_DEVICE_COUNTS: _h_device_counts,
    METRIC_VOLTAGE_LEVELS: _h_voltage_levels,
    METRIC_SOLVER_CONFIG: _h_solver_config,
    METRIC_FIDELITY: _h_fidelity,
    METRIC_CASE_INFO: _h_case_info,
    METRIC_RESULT_OVERVIEW: _h_result_overview,
    METRIC_CASE_STATISTICS: _h_case_statistics,
    METRIC_BUS_VOLTAGE: _h_bus_voltage,
}
for _m in (
    METRIC_CAP_TIMESERIES, METRIC_CAP_N1, METRIC_CAP_Q_LIMITS,
    METRIC_CAP_PHASE_SHIFT, METRIC_CAP_TINY_IMPEDANCE, METRIC_CAP_SAFETY,
):
    _HANDLERS[_m] = _make_cap_handler(_m)


def known_metric(metric: str | None) -> bool:
    return bool(metric) and str(metric) in _HANDLERS


def answer_by_metric(
    metric: str | None,
    content: str,
    ctx: dict[str, Any],
    slots: dict[str, Any] | None = None,
) -> str | None:
    """按 metric 取数与组织答案。metric 缺失或未知时返回 None（由调用方提示）。"""
    if not known_metric(metric):
        return None
    arts = _Arts(ctx)
    if not arts.has_any:
        return None
    handler = _HANDLERS[str(metric)]
    try:
        text = handler(arts, content, slots or {})
    except Exception:  # noqa: BLE001 - 问答失败不应让整条消息崩
        return None
    if not text:
        return None
    # 兜底：handler 返回的文本里若没有任何数字且不是能力边界话术，
    # 说明这个指标在该算例上确实没有数据 —— 交给调用方给"取不到"的提示。
    return text


def metric_title(metric: str | None) -> str:
    return METRIC_TITLES.get(str(metric), str(metric or ""))
