"""NetworkProfile：由确定性程序提取的规模、拓扑、电气与设备画像（文档 11.7）。"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel, Field

from ..engines.circuit_inventory import CircuitInventory
from ..engines.dss_source import parse_dss_directory


class NetworkProfile(BaseModel):
    case_id: str
    bus_count: int = 0
    line_count: int = 0
    transformer_count: int = 0
    load_count: int = 0
    generator_count: int = 0
    pv_system_count: int = 0
    storage_count: int = 0
    slack_bus_count: int = 0
    pv_bus_count: int = 0
    pq_bus_count: int = 0
    capacitor_count: int = 0
    regulator_count: int = 0
    voltage_levels_kv: list[float] = Field(default_factory=list)
    total_installed_load_kw: float | None = None
    is_radial: bool | None = None
    cycle_rank: int = 0
    parallel_line_pairs: int = 0
    active_line_count: int = 0
    inactive_line_count: int = 0
    active_branch_count: int = 0
    inactive_branch_count: int = 0
    three_phase_element_count: int = 0
    single_phase_element_count: int = 0
    has_three_phase_elements: bool = False
    # 结构性不平衡风险：算例里存在单相元件。此时分相口径与平衡等值口径的差异会显著，
    # 报告必须同时给出两个口径，不能只报"平衡等值最低电压"。
    has_single_phase_elements: bool = False
    notes: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "buses": self.bus_count,
            "lines": self.line_count,
            "transformers": self.transformer_count,
            "loads": self.load_count,
            "generators": self.generator_count,
            "pv_systems": self.pv_system_count,
            "storages": self.storage_count,
            "slack_buses": self.slack_bus_count,
            "pv_buses": self.pv_bus_count,
            "pq_buses": self.pq_bus_count,
            "voltage_levels_kv": self.voltage_levels_kv,
            "is_radial": self.is_radial,
            "cycle_rank": self.cycle_rank,
            "parallel_line_pairs": self.parallel_line_pairs,
            "active_line_count": self.active_line_count,
            "inactive_line_count": self.inactive_line_count,
            "active_branch_count": self.active_branch_count,
            "inactive_branch_count": self.inactive_branch_count,
            "three_phase_element_count": self.three_phase_element_count,
            "single_phase_element_count": self.single_phase_element_count,
            "total_installed_load_kw": self.total_installed_load_kw,
        }


def _scan_defined_branch_status(dss_root: str | None, mapping=None) -> tuple[int, int, int, int]:
    """从任务副本 DSS 文本补齐 OpenDSS 集合不会返回的 Enabled=no 对象。"""
    if not dss_root:
        return 0, 0, 0, 0
    from ..engines.fidelity_mapping import read_fidelity_mapping, synthetic_elements
    mapping = read_fidelity_mapping(dss_root) if mapping is None else mapping
    synthetic = synthetic_elements(mapping)
    line_states: dict[str, bool] = {}
    transformer_states: dict[str, bool] = {}
    for root, _, names in __import__("os").walk(dss_root):
        for filename in names:
            if not filename.lower().endswith(".dss"):
                continue
            try:
                with open(__import__("os").path.join(root, filename), encoding="utf-8", errors="ignore") as f:
                    for raw in f:
                        line = raw.split("!")[0].strip().lower()
                        if not line.startswith("new "):
                            continue
                        is_line = line.startswith("new line.")
                        is_transformer = line.startswith("new transformer.")
                        if not (is_line or is_transformer):
                            continue
                        disabled = "enabled=no" in line or "enabled=0" in line
                        parts = line.split(None, 2)
                        if len(parts) < 2 or "." not in parts[1]:
                            continue
                        object_name = parts[1].lower()
                        if object_name in synthetic:
                            continue
                        if is_line:
                            line_states[object_name] = not disabled
                        else:
                            transformer_states[object_name] = not disabled
            except OSError:
                continue
    for r in mapping:
        if r["role"] == "branch":
            target = transformer_states if r.get("original_kind") == "transformer" else line_states
            target[(r.get("original_kind", "line") + "." + str(r["id"])).lower()] = r.get("enabled", True)
    return (
        len(line_states),
        sum(not enabled for enabled in line_states.values()),
        len(transformer_states),
        sum(not enabled for enabled in transformer_states.values()),
    )


def build_network_profile(
    case_id: str,
    inventory: CircuitInventory,
    dss_root: str | None = None,
) -> NetworkProfile:
    """由「引擎观察量 + 任务副本文本」确定性组装网络画像。

    **纯数据推导，不接触引擎** —— 这是刻意的：观察量由 `CircuitInventory` 承载
    （各通道用 `collect_circuit_inventory` 自己收集），组装逻辑只有一份。
    CLI 通道将来只需填一个 `CircuitInventory`，本函数一行都不用改。

    此前的写法是接收 `run_with_engine` 回调、把整段画像逻辑塞进闭包执行。
    那对 COM / dss-python 可行，但 `OpenDSScmd.exe` 是独立进程，只认命令流与
    CSV 输出，**执行不了任意 Python 闭包** —— 这正是当初把它抽象出来的原因。
    """
    from ..engines.fidelity_mapping import read_fidelity_mapping, restore_inventory
    mapping = read_fidelity_mapping(dss_root or "")
    inventory = restore_inventory(inventory, mapping)
    profile = NetworkProfile(case_id=case_id)
    profile.bus_count = inventory.bus_count
    profile.line_count = inventory.line_count
    profile.active_line_count = 0
    profile.inactive_line_count = 0
    profile.transformer_count = inventory.transformer_count
    line_total, line_inactive, transformer_total, transformer_inactive = _scan_defined_branch_status(dss_root, mapping)
    if line_total:
        profile.line_count = line_total
    if transformer_total:
        profile.transformer_count = transformer_total
    profile.active_line_count = max(line_total - line_inactive, 0)
    profile.inactive_line_count = line_inactive
    profile.active_branch_count = profile.active_line_count + max(transformer_total - transformer_inactive, 0)
    profile.inactive_branch_count = profile.inactive_line_count + transformer_inactive
    profile.load_count = inventory.load_count
    profile.generator_count = inventory.generator_count
    profile.pv_system_count = inventory.pv_system_count
    profile.storage_count = inventory.storage_count
    profile.capacitor_count = inventory.capacitor_count
    profile.regulator_count = inventory.regulator_count

    levels: set[float] = set()
    slack_bus_count = 0
    # 松弛节点从**算例结构**取，不按母线名猜。
    # `New Circuit.X … bus1=YYY` 的 bus1 就是源母线（未显式写 bus1 时按 OpenDSS
    # 约定就是 sourcebus），`New Vsource.X` 同理 —— 该判据由 `dss_source` 单点定义。
    # 历史缺陷：原先只按名字猜（source / sourcebus），对**原生 .dss** 会漏：
    # 实测 case31 的源母线名为 GRID → slack_bus_count=0，并连带把 GRID 算进 PQ 母线
    # （对外答「平衡节点 0 个」，属说错话）。
    # 名字兜底保留：转换类算例的源节点会被规范化成 sourcebus，且缺 dss 文本时也要能用。
    source_buses: frozenset[str] = frozenset()
    if dss_root:
        try:
            source_buses = parse_dss_directory(dss_root).vsource_buses()
        except Exception:  # noqa: BLE001 - 源文本不可读不应影响画像主流程
            source_buses = frozenset()
    for name, kv in inventory.bus_kv_base:
        try:
            if kv:
                levels.add(round(float(kv), 3))
        except Exception:
            continue
        norm = str(name).split(".", 1)[0].strip().lower()
        if norm and (norm in source_buses or norm in {"source", "sourcebus"}):
            slack_bus_count += 1
    profile.voltage_levels_kv = sorted(levels)
    profile.slack_bus_count = slack_bus_count
    profile.pv_bus_count = profile.pv_system_count + profile.generator_count
    profile.pq_bus_count = max(profile.bus_count - profile.slack_bus_count - profile.pv_bus_count, 0)

    total_kw = 0.0
    phase_counts: Counter[int] = Counter()
    for kw, n_phases in inventory.loads:
        total_kw += kw
        phase_counts[n_phases] += 1
    profile.total_installed_load_kw = total_kw
    profile.three_phase_element_count = sum(v for k, v in phase_counts.items() if k == 3)
    profile.single_phase_element_count = sum(v for k, v in phase_counts.items() if k == 1)
    profile.has_three_phase_elements = profile.three_phase_element_count > 0
    profile.has_single_phase_elements = profile.single_phase_element_count > 0

    # 仅对已解析的 Line 端点做拓扑画像：cycle_rank > 0 表示至少存在环路。
    edge_counts: Counter[tuple[str, str]] = Counter()
    adjacency: defaultdict[str, set[str]] = defaultdict(set)
    for enabled, bus1, bus2 in inventory.line_endpoints:
        # 有任务副本文本时，启停计数已由 _scan_defined_branch_status 去重统计；
        # OpenDSS Lines 集合通常只枚举启用对象，不能再次累加，否则会出现 2 倍计数。
        if dss_root is None:
            profile.active_line_count += 1 if enabled else 0
            profile.inactive_line_count += 0 if enabled else 1
        if bus1 and bus2:
            edge = tuple(sorted((bus1, bus2)))
            edge_counts[edge] += 1
            adjacency[bus1].add(bus2)
            adjacency[bus2].add(bus1)
    profile.parallel_line_pairs = sum(1 for count in edge_counts.values() if count > 1)
    components = 0
    seen: set[str] = set()
    for node in adjacency:
        if node in seen:
            continue
        components += 1
        stack = [node]
        seen.add(node)
        while stack:
            cur = stack.pop()
            for nxt in adjacency[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    profile.cycle_rank = max(sum(edge_counts.values()) - len(adjacency) + components, 0)
    profile.is_radial = profile.cycle_rank == 0 if adjacency else None
    if profile.cycle_rank:
        profile.notes.append(f"线路拓扑存在环路：cycle_rank={profile.cycle_rank}")
    if profile.parallel_line_pairs:
        profile.notes.append(f"检测到并行线路端点对：{profile.parallel_line_pairs} 对")

    if profile.regulator_count or profile.capacitor_count:
        profile.notes.append(
            "算例含调压器/电容器等控制设备：V1.3 仅识别对象，控制行为已冻结（controlmode=off）"
        )
    if profile.generator_count or profile.pv_system_count:
        profile.notes.append(
            "已识别分布式电源/发电机对象；负荷或支路功率为负可能表示反向潮流/净注入，不应仅因负号判为非法。"
        )
    if profile.has_single_phase_elements:
        profile.notes.append(
            f"算例含 {profile.single_phase_element_count} 个单相元件（结构性不平衡风险）："
            "结果必须同时给出「平衡等值」与「分相」两个口径，后者才是越限判断依据；"
            "转换类算例（xlsx/csv/json/MATPOWER）不保留原始相别信息，其分相结果按平衡三相等值理解。"
        )
    return profile
