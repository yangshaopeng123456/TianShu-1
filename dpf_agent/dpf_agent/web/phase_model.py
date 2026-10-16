"""相别（分相）建模的列名归一化、相标解析与完整性校验。

**为什么需要这个模块**

OpenDSS 是三相不平衡求解器，但本系统的表格/JSON/CSV 算例此前一律按
**平衡三相等值**建模（强制 `Phases=3`、母线不带相号节点），于是：
- 输入里可能的单相负荷、单相支路、零序参数全部被抹掉；
- 结果层拿不到真实分相数据，"三相不平衡"能力在非 `.dss` 输入上失效。

本模块定义**拓展 schema**：输入可以声明每个元件的相数（1/2/3）与相别身份
（接在哪一相），转换器据此生成分相节点与 `Phases=k`。

**三条设计原则（都是踩过坑换来的）**

1. **相数可以默认，相别身份不可以猜。**
   `phases` 列缺失或为空 → 按 3 相处理，但**计入声明**（报告要说明）；
   而 `phases=1` 却缺"哪一相"或给了非法相标 → **硬报错**。
   猜错相 = 静默算出错误的电压分布，比直接失败危险得多。
2. **没有相别信息时，输出与旧版逐字节一致**（纯平衡等值），保证零回归。
3. **所有默认与假设都计数**，写进转换产物头部，报告据此如实声明。

**相标（phase token）约定**（兼容常见写法）

| 输入 | 含义 | 节点 |
|---|---|---|
| `A` / `a` / `1` / `A相` / `AN` | A 相 | `.1` |
| `B` / `2` | B 相 | `.2` |
| `C` / `3` | C 相 | `.3` |
| `AB` / `1.2` / `A,B` | A-B 两相 | `.1.2` |
| `ABC` / `1.2.3` / `abc` | 三相 | `.1.2.3` |
| `A0` / `1.0` / `AN` | A 相 + 中性（四线制） | `.1.0` |
| `0` / `N` / `中性` | 中性点 | `.0` |
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "PhaseSpecError",
    "PhaseDeclaration",
    "PHASE_COUNT_ALIASES",
    "PHASE_ID_ALIASES",
    "CONN_ALIASES",
    "CONN_PRIMARY_ALIASES",
    "CONN_SECONDARY_ALIASES",
    "ZERO_SEQ_PU_ALIASES",
    "ZERO_SEQ_OHM_ALIASES",
    "NEUTRAL_ALIASES",
    "detect_phase_columns",
    "parse_phase_token",
    "node_suffix",
    "node_ref",
    "is_three_phase",
    "load_kv_for_nodes",
    "normalize_conn",
    "sequence_conversion",
]

SQRT3 = math.sqrt(3.0)

# 相数：元件是几相
PHASE_COUNT_ALIASES = (
    "phases", "nphases", "phase_num", "phase_count", "n_phases", "num_phases", "相数",
)
# 相别身份：接在哪一相（单相/两相时必须给）
PHASE_ID_ALIASES = (
    "phase", "connected_phase", "bus_phase", "phase_id", "phases_connected",
    "conn_phase", "node", "nodes", "相别", "相号",
)
# 接线方式
CONN_ALIASES = ("conn", "connection", "winding_conn", "conn_type", "接线", "connect")
# 变压器两侧接线（delta-wye 配变是常态，必须分开给）
CONN_PRIMARY_ALIASES = (
    "conn1", "conn_primary", "conn_hv", "hv_conn", "winding1_conn", "conn_h", "接线_高压",
)
CONN_SECONDARY_ALIASES = (
    "conn2", "conn_secondary", "conn_lv", "lv_conn", "winding2_conn", "conn_l", "接线_低压",
)
# 零序参数（标么，系统基准）
ZERO_SEQ_PU_ALIASES = {
    "r0_pu": ("r0_pu", "r0pu", "r0"),
    "x0_pu": ("x0_pu", "x0pu", "x0"),
    "b0_pu": ("b0_pu", "b0pu", "b0"),
}
# 零序参数（欧姆 / 纳法，直接给物理量）
ZERO_SEQ_OHM_ALIASES = {
    "r0_ohm": ("r0_ohm", "r0_ohms"),
    "x0_ohm": ("x0_ohm", "x0_ohms"),
}
NEUTRAL_ALIASES = ("neutral", "with_neutral", "four_wire", "4wire", "含中性", "四线")

_LETTER_TO_NODE = {"a": 1, "b": 2, "c": 3, "n": 0}
_NODE_TO_LETTER = {1: "A", 2: "B", 3: "C", 0: "N"}

# 中文/别名 → 字母
_TOKEN_ALIASES = {
    "甲": 1, "乙": 2, "丙": 3,
    "a相": 1, "b相": 2, "c相": 3,
    "第1相": 1, "第2相": 2, "第3相": 3,
    "中性": 0, "中线": 0, "地": 0, "ground": 0, "gnd": 0, "earth": 0,
}


class PhaseSpecError(ValueError):
    """相别声明不合法（无法安全推断）。

    这类错误必须**冒泡到转换阶段并报错**，不允许降级为"按三相处理"——
    猜错相的后果是静默错误的电压分布，比直接失败危险。
    """


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return _clean(value) == ""


def parse_phase_token(value: Any, kind: str = "identity") -> tuple[int, ...]:
    """把相标解析为节点号元组（1/2/3 = A/B/C，0 = 中性）。

    `kind="count"`：解析"相数"列（`3` → 三相 → (1,2,3)）。
    `kind="identity"`：解析"相别"列（`3` → C 相 → (3,)）。

    非法或无法判断时抛 `PhaseSpecError`（调用方不得吞掉后默认三相）。
    """
    text = _clean(value)
    if text == "":
        raise PhaseSpecError("相别为空")

    # 单字母/中文别名
    low = text.lower().replace(" ", "").replace("_", "")
    if low in _TOKEN_ALIASES:
        return (_TOKEN_ALIASES[low],)
    if low in _LETTER_TO_NODE:
        return (_LETTER_TO_NODE[low],)

    # 纯数字：count 语义下 3 = 三相；identity 语义下 3 = C 相。
    # 注意两种情况都要能**继续往下走**而不是直接报错：
    #   "1.2" 不是整数 → 是复合写法（A-B 两相）；
    #   "12"  是整数但超出单相编号范围 → 是连写（A、B 两相）。
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None and number.is_integer():
        n = int(number)
        if kind == "count":
            if n == 3:
                return (1, 2, 3)
            if n in (1, 2):
                raise PhaseSpecError(
                    f"相数为 {n} 但未给出相别身份，无法判断接在哪一相"
                )
            raise PhaseSpecError(f"相数不支持：{n}（仅支持 1/2/3）")
        if n in (0, 1, 2, 3):
            return (n,)
        if not all(ch in "0123" for ch in text):
            raise PhaseSpecError(f"相号不支持：{n}（仅支持 0/1/2/3）")

    # 复合写法：abc / 1.2.3 / A,B / a-b / ab0 …
    parts: list[str] = []
    buf = ""
    for ch in low:
        if ch in ".,-、;|/\\+& ":
            if buf:
                parts.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        parts.append(buf)

    if not parts:
        raise PhaseSpecError(f"无法解析的相标：{text!r}")

    nodes: list[int] = []
    for part in parts:
        if part in _TOKEN_ALIASES:
            nodes.append(_TOKEN_ALIASES[part])
            continue
        if part in _LETTER_TO_NODE:
            nodes.append(_LETTER_TO_NODE[part])
            continue
        stripped = part.replace("相", "")
        if stripped in _LETTER_TO_NODE:
            nodes.append(_LETTER_TO_NODE[stripped])
            continue
        # 无分隔符的连写（ab / abc / a0 / ab0 / 12 / 123）：逐字符展开。
        # 必须支持，因为 `abc`、`ab` 是最常见的写法。
        if len(part) > 1 and all(ch in "abcn0123" for ch in part):
            for ch in part:
                node = _LETTER_TO_NODE.get(ch)
                if node is None:
                    node = int(ch)
                    if node not in (0, 1, 2, 3):
                        raise PhaseSpecError(f"相号不支持：{node}（原文 {text!r}）")
                nodes.append(node)
            continue
        try:
            node = int(float(part))
        except ValueError as exc:
            raise PhaseSpecError(f"无法解析的相标片段：{part!r}（原文 {text!r}）") from exc
        if node not in (0, 1, 2, 3):
            raise PhaseSpecError(f"相号不支持：{node}（原文 {text!r}）")
        nodes.append(node)

    # 去重并排序；"三相"写法里多余的 0 不算中性声明
    phases = sorted({n for n in nodes if n != 0})
    has_neutral = 0 in nodes
    if not phases:
        raise PhaseSpecError(f"相标只含中性点，缺少相号：{text!r}")
    if has_neutral and len(phases) < 3:
        phases = phases + [0]
    return tuple(phases)


def node_suffix(nodes: Sequence[int]) -> str:
    """节点后缀：`(2,)` → `.2`；`(1,2,3)` → `.1.2.3`；`(2,0)` → `.2.0`。"""
    return "".join(f".{int(n)}" for n in nodes)


def node_ref(bus: str, nodes: Sequence[int] | None) -> str:
    """母线 + 节点引用。三相（None 或 (1,2,3)）不加后缀，与旧版输出保持一致。"""
    if not nodes or is_three_phase(nodes):
        return bus
    return f"{bus}{node_suffix(nodes)}"


def is_three_phase(nodes: Sequence[int] | None) -> bool:
    return bool(nodes) and tuple(sorted(n for n in nodes if n != 0)) == (1, 2, 3)


def load_kv_for_nodes(base_kv: float, nodes: Sequence[int] | None) -> float:
    """按连接节点数换算 OpenDSS 的 `kV`。

    OpenDSS 的 `kV` 对三相/两相元件是**线电压**，对单相（相—地/相—中）元件是
    **相电压**。输入表的 base_kv 一律是线电压，故单相必须除以 √3——
    此前不分相时不存在这个问题，一旦按相接入就必须换算，否则负荷被高估 √3 倍。
    """
    if not nodes or is_three_phase(nodes):
        return base_kv
    effective = [n for n in nodes if n != 0]
    if len(effective) >= 3:
        return base_kv
    if len(effective) == 2:
        # 两相（相—相）元件：线电压
        return base_kv
    return base_kv / SQRT3


def normalize_conn(value: Any, default: str = "wye") -> str:
    """接线方式归一化为 OpenDSS 取值（wye / delta）。"""
    text = _clean(value).lower()
    if text in {"", "nan", "none"}:
        return default
    if text in {"wye", "y", "star", "星", "星形", "y接"}:
        return "wye"
    if text in {"delta", "d", "三角", "三角形", "δ", "d接"}:
        return "delta"
    raise PhaseSpecError(f"接线方式不支持：{value!r}（仅支持 wye/delta）")


def sequence_conversion(
    *,
    r0_pu: float | None = None,
    x0_pu: float | None = None,
    b0_pu: float | None = None,
    r0_ohm: float | None = None,
    x0_ohm: float | None = None,
    z_base_ohm: float | None = None,
    base_freq_hz: float = 50.0,
) -> dict[str, float]:
    """零序参数换算成 OpenDSS 的 `R0/X0/C0`。

    - 标么值（系统基准）→ 欧姆：`Z0 = z0_pu × Z_base`；
    - 充电电纳 `b0_pu`（系统基准，总对地）→ `C0`（nF 总量）：
      `B0(S) = b0_pu / Z_base`，`C0 = B0/(2πf) × 1e9`。
    """
    out: dict[str, float] = {}
    if r0_ohm is not None:
        out["R0"] = float(r0_ohm)
    elif r0_pu is not None and z_base_ohm:
        out["R0"] = float(r0_pu) * float(z_base_ohm)
    if x0_ohm is not None:
        out["X0"] = float(x0_ohm)
    elif x0_pu is not None and z_base_ohm:
        out["X0"] = float(x0_pu) * float(z_base_ohm)
    if b0_pu is not None and z_base_ohm and base_freq_hz > 0:
        b_siemens = float(b0_pu) / float(z_base_ohm)
        out["C0"] = b_siemens / (2.0 * math.pi * base_freq_hz) * 1e9
    return out


def detect_phase_columns(columns: Iterable[Any]) -> dict[str, str]:
    """在列名集合里找出相别相关的物理列（返回 规范名 → 实际列名）。

    返回空字典表示**输入完全没有相别信息**——此时保持旧的平衡等值行为，
    不产生任何假设。
    """
    normalized = {str(c).strip(): str(c) for c in columns}
    lowered = {str(c).strip().lower().replace(" ", "").replace("_", ""): str(c) for c in columns}
    found: dict[str, str] = {}

    def pick(canonical: str, aliases: Iterable[str]) -> None:
        for alias in aliases:
            key = alias.lower().replace(" ", "").replace("_", "")
            if key in lowered:
                found[canonical] = lowered[key]
                return

    pick("phases", PHASE_COUNT_ALIASES)
    pick("phase", PHASE_ID_ALIASES)
    pick("conn", CONN_ALIASES)
    pick("conn1", CONN_PRIMARY_ALIASES)
    pick("conn2", CONN_SECONDARY_ALIASES)
    for canonical, aliases in ZERO_SEQ_PU_ALIASES.items():
        pick(canonical, aliases)
    for canonical, aliases in ZERO_SEQ_OHM_ALIASES.items():
        pick(canonical, aliases)
    for canonical, aliases in (("neutral", NEUTRAL_ALIASES),):
        pick(canonical, aliases)
    # 排除误命中：`phases` 与 `phase` 指向同一列时，只保留"相数"语义
    if found.get("phases") and found.get("phase") == found["phases"]:
        found.pop("phase", None)
    return found


@dataclass
class PhaseDeclaration:
    """一次转换里相别信息的统计声明（写进产物头部，供报告如实说明）。

    语义分工：
    - `has_phase_columns=False` → 输入无相别信息，按平衡三相等值（与旧版一致）；
    - `has_phase_columns=True`  → 按输入相别建模，但**默认值要计数**
      （`assumed_three_phase`），报告不能把"默认三相"说成"输入声明了三相"。
    """

    table: str = ""
    has_phase_columns: bool = False
    elements_total: int = 0
    elements_with_phase: int = 0
    elements_single_or_partial: int = 0
    assumed_three_phase: int = 0
    neutral_declared: int = 0
    zero_seq_declared: int = 0
    conn_declared: int = 0
    # 线路上的 conn 列被显式忽略的条数。OpenDSS 的 Line **没有 Conn 属性**，
    # 写上会让 COM 引擎在 compile 时死循环（实测 120s 超时才脱身）。
    conn_ignored_on_lines: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def unbalanced(self) -> bool:
        """是否存在真实的不平衡元件（单相/两相）。"""
        return self.elements_single_or_partial > 0

    def flags(self) -> dict[str, str]:
        return {
            "has_phase_columns": "1" if self.has_phase_columns else "0",
            "unbalanced_input": "1" if self.unbalanced else "0",
            "elements_with_phase": str(self.elements_with_phase),
            "assumed_three_phase": str(self.assumed_three_phase),
            "single_or_partial_elements": str(self.elements_single_or_partial),
            "zero_seq_declared": str(self.zero_seq_declared),
            "neutral_declared": str(self.neutral_declared),
            "conn_declared": str(self.conn_declared),
            "conn_ignored_on_lines": str(self.conn_ignored_on_lines),
        }

    def merge(self, other: "PhaseDeclaration") -> None:
        self.table = f"{self.table}+{other.table}" if self.table else other.table
        self.has_phase_columns = self.has_phase_columns or other.has_phase_columns
        self.elements_total += other.elements_total
        self.elements_with_phase += other.elements_with_phase
        self.elements_single_or_partial += other.elements_single_or_partial
        self.assumed_three_phase += other.assumed_three_phase
        self.neutral_declared += other.neutral_declared
        self.zero_seq_declared += other.zero_seq_declared
        self.conn_declared += other.conn_declared
        self.conn_ignored_on_lines += other.conn_ignored_on_lines
        self.notes.extend(other.notes)
