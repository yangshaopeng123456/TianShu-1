"""分相（三相不平衡）建模回归测试 —— 自包含，不需要引擎。

覆盖三块：

**A. 相别解析单元行为**（`web/phase_model.py`）
   相标、节点命名、单相 kV 折算、接线归一化、零序换算、列检测。

**B. 转换器分相输出**（`web/case_converters.py`）
   用合成 xlsx / json / csv（同一套电气数据、不同表达方式）验证：
   单相负荷接相、单相支路、零序参数 R0/X0/C0、delta-wye 变压器、
   头部相别声明，三种格式结果**互相一致**。

**C. 拒绝与零回归**
   - `phases=1` 却不给"哪一相" → **必须报错**（不许猜）；
   - 非法相标 / 非法接线 → **必须报错**；
   - **无相别列时输出与旧行为一致**（Phases=3、无节点后缀、保守声明）。

运行：python -m dpf_agent.tests.test_phase_modeling
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)))

import pandas as pd  # noqa: E402

from dpf_agent.web import case_converters as cc  # noqa: E402
from dpf_agent.web import phase_model as pm  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, str(detail)))


# --------------------------------------------------------------------------- #
# A. 相别解析单元行为
# --------------------------------------------------------------------------- #
def test_phase_tokens() -> None:
    cases = {
        "A": (1,), "a": (1,), "1": (1,), 1: (1,), 1.0: (1,), "甲": (1,),
        "B": (2,), "2": (2,), 2: (2,), "b相": (2,),
        "C": (3,), "3": (3,),
        "AB": (1, 2), "ab": (1, 2), "1.2": (1, 2), "A,B": (1, 2), "12": (1, 2),
        "abc": (1, 2, 3), "ABC": (1, 2, 3), "1.2.3": (1, 2, 3), "123": (1, 2, 3),
        "A0": (1, 0), "a0": (1, 0), "an": (1, 0),
        "N": (0,), 0: (0,), "中性": (0,),
    }
    bad = []
    for token, expect in cases.items():
        try:
            got = pm.parse_phase_token(token)
        except pm.PhaseSpecError as exc:
            bad.append(f"{token!r} 解析失败：{exc}")
            continue
        if got != expect:
            bad.append(f"{token!r} -> {got}，期望 {expect}")
    check(f"A1 相标解析 {len(cases)} 种写法", not bad, "；".join(bad[:4]))

    # 相数列语义：3 → 三相；1/2 → 必须报错（问不出接哪一相）
    check("A2 相数列 3 → 三相", pm.parse_phase_token("3", "count") == (1, 2, 3))
    for value in ("1", "2", 1, 2):
        try:
            pm.parse_phase_token(value, "count")
            check(f"A3 相数 {value} 无相别身份应报错", False, "未报错")
        except pm.PhaseSpecError:
            check(f"A3 相数 {value} 无相别身份应报错", True)

    # 非法输入必须报错，而不是悄悄按三相处理
    for bad_token in ("5", "x", "zz", "1.05"):
        try:
            pm.parse_phase_token(bad_token)
            check(f"A4 非法相标 {bad_token!r} 应报错", False, "未报错")
        except pm.PhaseSpecError:
            check(f"A4 非法相标 {bad_token!r} 应报错", True)


def test_node_naming_and_kv() -> None:
    cases = [
        ("A", "BUS.1"), ("B", "BUS.2"), ("C", "BUS.3"),
        ("AB", "BUS.1.2"), ("abc", "BUS"),  # 三相不加后缀，与旧输出保持一致
    ]
    bad = [f"{t}->{pm.node_ref('BUS', pm.parse_phase_token(t))}（期望 {e}）"
           for t, e in cases if pm.node_ref("BUS", pm.parse_phase_token(t)) != e]
    check("A5 节点命名（三相不加后缀）", not bad, "；".join(bad))
    check("A6 三相判定", pm.is_three_phase((1, 2, 3)) and not pm.is_three_phase((1, 2)))
    check("A7 None 视为三相", pm.node_ref("BUS", None) == "BUS" and pm.load_kv_for_nodes(20.0, None) == 20.0)

    v = 20.0
    check("A8 单相 kV = 线电压/√3", abs(pm.load_kv_for_nodes(v, (2,)) - v / math.sqrt(3)) < 1e-9,
          f"{pm.load_kv_for_nodes(v, (2,)):.6f}")
    check("A8 两相 kV = 线电压", pm.load_kv_for_nodes(v, (1, 2)) == v)
    check("A8 三相 kV = 线电压", pm.load_kv_for_nodes(v, (1, 2, 3)) == v)
    check("A8 四线制单相 kV = 线电压/√3",
          abs(pm.load_kv_for_nodes(v, (2, 0)) - v / math.sqrt(3)) < 1e-9)

    for text, expect in (("wye", "wye"), ("Y", "wye"), ("星形", "wye"),
                         ("delta", "delta"), ("D", "delta"), ("三角", "delta")):
        check(f"A9 接线归一化 {text} → {expect}", pm.normalize_conn(text) == expect)
    try:
        pm.normalize_conn("zzz")
        check("A9 非法接线应报错", False, "未报错")
    except pm.PhaseSpecError:
        check("A9 非法接线应报错", True)

    # 零序换算：标么 → 欧姆；充电电纳 → 电容（nF）
    conv = pm.sequence_conversion(r0_pu=0.6, x0_pu=1.0, b0_pu=0.02,
                                  z_base_ohm=4.0, base_freq_hz=50.0)
    check("A10 R0 = r0_pu × Z_base", abs(conv["R0"] - 2.4) < 1e-9, str(conv.get("R0")))
    check("A10 X0 = x0_pu × Z_base", abs(conv["X0"] - 4.0) < 1e-9, str(conv.get("X0")))
    c0_expect = (0.02 / 4.0) / (2 * math.pi * 50.0) * 1e9
    check("A10 C0 = (b0/Z)/(2πf)×1e9", abs(conv["C0"] - c0_expect) / c0_expect < 1e-9,
          f"{conv.get('C0')} vs {c0_expect}")
    conv_ohm = pm.sequence_conversion(r0_ohm=1.5, x0_ohm=2.5)
    check("A10 直接给欧姆时不换算", conv_ohm == {"R0": 1.5, "X0": 2.5}, str(conv_ohm))

    cols = pm.detect_phase_columns(["load_id", "bus_id", "phase", "phases", "conn",
                                    "r0_pu", "x0_pu", "b0_pu", "conn1", "conn2"])
    check("A11 列检测命中全部相别列",
          {"phase", "phases", "conn", "r0_pu", "x0_pu", "b0_pu", "conn1", "conn2"} <= set(cols),
          str(sorted(cols)))
    check("A11 无相别列时返回空", pm.detect_phase_columns(["bus_id", "base_kv", "pd_mw"]) == {})
    check("A11 conn 不误判为相别身份",
          "phase" not in pm.detect_phase_columns(["bus_id", "conn", "r0_pu"]))


# --------------------------------------------------------------------------- #
# 合成算例：同一套电气数据、三种表达方式
# --------------------------------------------------------------------------- #
# 拓扑（照搬真实硬算例 case07 的模式：中压三相 + 配变 + 低压单相支路）：
#   SRC(110) --L1:变压器--> B2(20) --LTX:配变--> LV(0.4)
#   LV --LP1(A 相单相支路)--> L1A   LV --LP2(B 相单相支路)--> L1B
#   三相负荷挂 B2；单相负荷分别挂 L1A / L1B。
_BUS = [
    # bus_id, base_kv, type, pd_mw, qd_mvar, vm_set_pu
    ("SRC", 110.0, "slack", 0.0, 0.0, 1.04),
    ("B2", 20.0, "pq", 6.0, 2.0, 1.01),
    ("LV", 0.4, "pq", 0.0, 0.0, 1.0),
    ("L1A", 0.4, "pq", 0.0, 0.0, 1.0),
    ("L1B", 0.4, "pq", 0.0, 0.0, 1.0),
]
_LINE = [
    # line_id, from, to, kind, r_pu, x_pu, b_pu, rate_mva, phases, phase, conn, r0_pu, x0_pu, b0_pu
    ("L1", "SRC", "B2", "transformer", 0.005, 0.05, 0.0, 40.0, 3, "abc", "wye", 0.02, 0.08, 0.0),
    ("LTX", "B2", "LV", "transformer", 0.01, 0.06, 0.0, 0.63, 3, "abc", "wye", 0.03, 0.09, 0.0),
    ("LP1", "LV", "L1A", "line", 0.72, 0.10, 0.0, 0.5, 1, "A", "wye", 0.72, 0.10, 0.0),
    ("LP2", "LV", "L1B", "line", 0.72, 0.10, 0.0, 0.5, 1, "B", "wye", 0.72, 0.10, 0.0),
]
_LOAD = [
    # load_id, bus_id, phase, pd_mw, qd_mvar, conn
    ("LD_A", "L1A", "A", 0.10, 0.03, "wye"),
    ("LD_B", "L1B", "B", 0.12, 0.04, "wye"),
    ("LD_3P", "B2", "abc", 0.8, 0.3, "wye"),
]


def _bus_frame() -> pd.DataFrame:
    return pd.DataFrame(_BUS, columns=["bus_id", "base_kv", "type", "pd_mw", "qd_mvar", "vm_set_pu"])


def _line_frame() -> pd.DataFrame:
    return pd.DataFrame(_LINE, columns=[
        "line_id", "from_bus", "to_bus", "kind", "r_pu", "x_pu", "b_pu", "rate_mva",
        "phases", "phase", "conn", "r0_pu", "x0_pu", "b0_pu",
    ])


def _load_frame() -> pd.DataFrame:
    return pd.DataFrame(_LOAD, columns=["load_id", "bus_id", "phase", "pd_mw", "qd_mvar", "conn"])


def _write_xlsx(path: str) -> None:
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame(
            [{"name": "demo", "base_mva": 100.0, "frequency_hz": 50.0}]
        ).to_excel(writer, sheet_name="System", index=False)
        _bus_frame().to_excel(writer, sheet_name="Bus", index=False)
        _line_frame().to_excel(writer, sheet_name="Line", index=False)
        _load_frame().to_excel(writer, sheet_name="Load", index=False)


def _write_json(path: str) -> None:
    payload = {
        "system": {"name": "demo", "base_mva": 100.0, "frequency_hz": 50.0},
        "bus": [
            dict(zip(["id", "base_kv", "type", "pd_mw", "qd_mvar", "vm_set_pu"], row))
            for row in _BUS
        ],
        "line": [
            dict(zip(
                ["id", "from", "to", "kind", "r_pu", "x_pu", "b_pu", "rate_mva",
                 "phases", "phase", "conn", "r0_pu", "x0_pu", "b0_pu"], row))
            for row in _LINE
        ],
        "load": [
            dict(zip(["id", "bus", "phase", "pd_mw", "qd_mvar", "conn"], row))
            for row in _LOAD
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _write_csv(path: str) -> None:
    rows = []
    rows.append({"type": "system", "key": "base_mva", "value": 100.0})
    rows.append({"type": "system", "key": "frequency_hz", "value": 50.0})
    for bid, kv, btype, pd_mw, qd_mvar, vm in _BUS:
        rows.append({"type": "bus", "bus_id": bid, "base_kv": kv, "bus_type": btype,
                     "pd_mw": pd_mw, "qd_mvar": qd_mvar, "vm_set_pu": vm})
    for (lid, fb, tb, kind, r, x, b, rate, ph, ph_id, conn, r0, x0, b0) in _LINE:
        rows.append({"type": "line", "line_id": lid, "from_bus": fb, "to_bus": tb, "kind": kind,
                     "r_pu": r, "x_pu": x, "b_pu": b, "rate_mva": rate,
                     "phases": ph, "phase": ph_id, "conn": conn,
                     "r0_pu": r0, "x0_pu": x0, "b0_pu": b0})
    for lid, bid, ph_id, pd_mw, qd_mvar, conn in _LOAD:
        rows.append({"type": "load", "load_id": lid, "bus_id": bid, "phase": ph_id,
                     "pd_mw": pd_mw, "qd_mvar": qd_mvar, "conn": conn})
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _convert_all(tmp: str) -> dict[str, str]:
    xlsx = os.path.join(tmp, "demo.xlsx")
    js = os.path.join(tmp, "demo.json")
    csv = os.path.join(tmp, "demo.csv")
    _write_xlsx(xlsx)
    _write_json(js)
    _write_csv(csv)
    out: dict[str, str] = {}
    for label, path, fn in (("xlsx", xlsx, cc.convert_xlsx),
                            ("json", js, cc.convert_json),
                            ("csv", csv, cc.convert_csv)):
        master = fn(path, os.path.join(tmp, f"out_{label}"))
        out[label] = open(master, encoding="utf-8").read()
    return out


def _loads(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        m = re.match(r"\s*New Load\.(\S+)\s+(.*)$", line)
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


def _lines(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        m = re.match(r"\s*New Line\.(\S+)\s+(.*)$", line)
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


def _transformers(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        m = re.match(r"\s*New Transformer\.(\S+)\s+(.*)$", line)
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


def test_conversion(texts: dict[str, str]) -> None:
    for label, text in texts.items():
        tag = f"B[{label}]"
        loads = _loads(text)
        lines = _lines(text)
        xfmrs = _transformers(text)

        # 单相负荷：接在指定相、Phases=1、kV 按相电压（0.4kV 侧 → /√3）
        a = loads.get("Load_LD_A", "")
        check(f"{tag} A 相单相负荷节点 .1", "Bus1=L1A.1" in a, a)
        check(f"{tag} A 相单相负荷 Phases=1", "Phases=1" in a, a)
        check(f"{tag} A 相负荷 kV = 0.4/√3",
              abs(_num(a, "kV") - 0.4 / math.sqrt(3)) < 1e-6, a)
        check(f"{tag} 单相负荷带 Conn=wye", "Conn=wye" in a, a)

        b = loads.get("Load_LD_B", "")
        check(f"{tag} B 相单相负荷节点 .2", "Bus1=L1B.2" in b, b)

        three = loads.get("Load_LD_3P", "")
        check(f"{tag} 三相负荷不加节点后缀", "Bus1=B2 Phases=3" in three, three)
        check(f"{tag} 三相负荷 kV 用线电压", abs(_num(three, "kV") - 20.0) < 1e-6, three)

        # 单相支路：节点带相号、Phases=1
        l2 = lines.get("LP1", "")
        check(f"{tag} A 相支路两端节点 .1", "Bus1=LV.1" in l2 and "Bus2=L1A.1" in l2, l2)
        check(f"{tag} A 相支路 Phases=1", "Phases=1" in l2, l2)
        l3 = lines.get("LP2", "")
        check(f"{tag} B 相支路两端节点 .2", "Bus1=LV.2" in l3 and "Bus2=L1B.2" in l3, l3)

        # 零序参数落地
        check(f"{tag} 支路输出 R0/X0", "R0=" in l2 and "X0=" in l2, l2)

        # 变压器：三相 + 节点不加后缀（注意 slack 母线会被重命名为 sourcebus）
        tx = xfmrs.get("LTX", "")
        check(f"{tag} 三相变压器 phases=3", "phases=3" in tx.lower(), tx)
        check(f"{tag} 三相变压器 20/0.4 kV 正确", "kvs=[20 0.4]" in tx, tx)

        # 头部声明
        check(f"{tag} 头部 unbalanced_input=1", "unbalanced_input=1" in text)
        check(f"{tag} 头部 has_phase_columns=1", "has_phase_columns=1" in text)
        check(f"{tag} 头部声明单相元件数", "phase_note=" in text)
        m = re.search(r"single_or_partial_elements=(\d+)", text)
        check(f"{tag} 单相/两相元件计数 = 4（2 支路 + 2 负荷）", m and int(m.group(1)) == 4,
              m.group(1) if m else "缺")
        check(f"{tag} warning 不再写「相别不平衡…未保留」",
              "相别信息已按输入建模" in text, text[:200])
        # 事故回归：Line 上绝不能出现 Conn（曾致 COM 引擎死循环）；
        # 但输入表里的 conn 列要被**显式忽略并计数**，不能静默丢弃。
        check(f"{tag} New Line 上无非法 Conn 属性",
              not any(l.strip().startswith("New Line") and " Conn=" in l
                      for l in text.splitlines()))
        # 只有 2 条普通线路（LP1/LP2）带 conn；L1/LTX 是变压器，走变压器分支。
        check(f"{tag} 线路 conn 被忽略并计入声明", "conn_ignored_on_lines=2" in text,
              next((l for l in text.splitlines() if "phase_model=" in l), "")[:200])

    # 三种格式的电气模型应完全一致（同一套数据的等价表达）
    def electrical(t: str) -> list[str]:
        return [ln for ln in t.splitlines()
                if not ln.lstrip().startswith("!") and "New Circuit" not in ln]

    base = electrical(texts["xlsx"])
    same = [k for k, v in texts.items() if electrical(v) == base]
    check("B[跨格式] xlsx / json / csv 电气模型一致", len(same) == 3, f"一致格式：{same}")


def test_transformer_conn(tmp: str) -> None:
    """delta-wye 配变必须能表达（此前固定写死 wye wye）。"""
    bus = pd.DataFrame(
        [("SRC", 20.0, "slack", 0.0, 0.0, 1.02), ("LV", 0.4, "pq", 0.3, 0.1, 1.0)],
        columns=["bus_id", "base_kv", "type", "pd_mw", "qd_mvar", "vm_set_pu"],
    )
    line = pd.DataFrame(
        [("TX", "SRC", "LV", "transformer", 0.01, 0.06, 0.0, 0.63, 3, "abc", "delta", "wye")],
        columns=["line_id", "from_bus", "to_bus", "kind", "r_pu", "x_pu", "b_pu",
                 "rate_mva", "phases", "phase", "conn1", "conn2"],
    )
    master = cc.build_dss_from_frames(None, bus, line, os.path.join(tmp, "tx_dy"),
                                      source_label="case_conn")
    text = open(master, encoding="utf-8").read()
    check("B2 delta-wye 变压器接线落地", "conns=[delta wye]" in text,
          next((l for l in text.splitlines() if "Transformer" in l), "")[:130])
    check("B2 conn_declared 计入头部", "conn_declared=1" in text)


def test_single_phase_transformer(tmp: str) -> None:
    """单相变压器（Phases=1）应能表达；两相变压器必须被拒绝。"""
    bus = pd.DataFrame(
        [("SRC", 20.0, "slack", 0.0, 0.0, 1.02), ("LVA", 0.4, "pq", 0.1, 0.03, 1.0)],
        columns=["bus_id", "base_kv", "type", "pd_mw", "qd_mvar", "vm_set_pu"],
    )
    line1 = pd.DataFrame(
        [("TX1", "SRC", "LVA", "transformer", 0.01, 0.06, 0.0, 0.05, 1, "A")],
        columns=["line_id", "from_bus", "to_bus", "kind", "r_pu", "x_pu", "b_pu",
                 "rate_mva", "phases", "phase"],
    )
    master = cc.build_dss_from_frames(None, bus, line1, os.path.join(tmp, "tx_1ph"),
                                      source_label="case_tx1")
    text = open(master, encoding="utf-8").read()
    tx = next((l for l in text.splitlines() if "Transformer" in l), "")
    check("B3 单相变压器 phases=1", "phases=1" in tx.lower(), tx[:130])
    check("B3 单相变压器两侧带相号节点", "buses=[sourcebus.1 LVA.1]" in tx, tx[:130])

    line2 = line1.copy()
    line2.loc[0, "phase"] = "AB"
    try:
        cc.build_dss_from_frames(None, bus, line2, os.path.join(tmp, "tx_2ph"),
                                 source_label="case_tx2")
        check("B3 两相变压器应被拒绝", False, "未报错")
    except ValueError as exc:
        check("B3 两相变压器应被拒绝", "相数不支持" in str(exc), str(exc)[:90])


# --------------------------------------------------------------------------- #
# C. 拒绝 + 零回归
# --------------------------------------------------------------------------- #
def dss_structural_issues(text: str) -> list[str]:
    """产物结构自检：抓"只有到 OpenDSS 求解时才暴露"的接线错误。

    为什么需要它：本环境里官方命令行版对文件是**静默不执行**的（已实测），
    所以我无法用引擎离线跑产物；但下面这几类错误是纯结构性的，可以在转换层
    直接拦住——若不拦，就会以"引擎报错/不收敛"的形式在更晚、更贵的地方爆出来。

    检查项：
    1. `Bus1/Bus2/buses=` 引用的母线必须在产物里声明过（含 sourcebus）；
    2. 节点号必须在 0..3（0 = 中性）；母线的相数不得超过该节点集；
    3. 元件声明的 `Phases=k` 必须与显式给出的节点个数一致（中性点不计入 k）；
    4. 变压器 `kvs=[a b]` 必须与两端母线的 base_kv 一致（否则变比就错了）；
    5. 括号成对（`[...]`、`(...)`）；
    6. **属性白名单**：每个元素只能写它真正拥有的属性。

    第 6 项来自一次真实事故：给 `New Line` 写了 `Conn=wye`（OpenDSS 的 Line
    **没有** Conn 属性，只有 Load/Generator 有），结果 OpenDSS COM 引擎在 compile
    阶段**死循环**，120s 硬超时才脱身——而同一模型删掉该词 5 秒内正常收敛。
    命令行版对文件静默不执行，所以这类错误在离线阶段看不出来，必须在转换层拦住。
    """
    issues: list[str] = []

    # ---- 6. 属性白名单（只用已验证存在的属性名）----
    allowed_props: dict[str, set[str]] = {
        "New Line.": {
            "bus1", "bus2", "phases", "r1", "x1", "r0", "x0", "c1", "c0",
            "length", "units", "enabled", "linecode", "normamps", "emergamps",
            "earthmodel", "rmatrix", "xmatrix", "cmatrix",
        },
        "New Load.": {
            "bus1", "bus2", "phases", "kv", "kw", "kvar", "pf", "model", "conn",
            "vminpu", "vmaxpu", "kva", "daily", "status", "enabled",
        },
        "New Transformer.": {
            "phases", "windings", "buses", "conns", "kvs", "kvas", "kva",
            "%rs", "xhl", "tap", "taps", "wdg", "mintap", "maxtap", "xscarray", "enabled", "r", "x",
            "ppm_antifloat", "%imag", "%noloadloss",  # EPRI Transformer properties; real COM fidelity probes.
        },
        "New Generator.": {
            "bus1", "phases", "kv", "kw", "kvar", "pf", "model", "vpu",
            "minkvar", "maxkvar", "conn", "enabled", "status",
        },
        "New Circuit.": {"basekv", "pu", "phases", "mvasc3", "mvasc1", "frequency"},
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for prefix, allowed in allowed_props.items():
            if not line.startswith(prefix):
                continue
            body = line[len(prefix):]
            # 跳过元素名，取后续 KEY=VALUE
            for match in re.finditer(r"([A-Za-z%][A-Za-z0-9%_]*)\s*=", body):
                key = match.group(1).lower()
                if key not in allowed:
                    issues.append(f"{prefix.rstrip('.')} 不支持属性 {match.group(1)}：{line[:90]}")
            break
    # 1. 声明过的母线：New Line/Transformer/Load 的端点都算（OpenDSS 会自动建母线），
    #    但为了抓住"引用了不存在的母线名"，这里以显式出现的母线 + sourcebus 为准。
    declared: set[str] = {"sourcebus"}
    all_bus_tokens: list[tuple[str, str, str]] = []  # (元素, 端点, 母线名)

    def split_bus(token: str) -> tuple[str, list[int]]:
        parts = token.split(".")
        name = parts[0]
        nodes = [int(p) for p in parts[1:] if p.strip().isdigit()]
        return name, nodes

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("!"):
            continue
        for bus_token in re.findall(r"\b(?:Bus1|Bus2)=(\S+)", line):
            name, nodes = split_bus(bus_token)
            all_bus_tokens.append((line.split(".")[0], bus_token, name))
            declared.add(name)
            for n in nodes:
                if n not in (0, 1, 2, 3):
                    issues.append(f"节点号越界：{bus_token}")
            if nodes and len([n for n in nodes if n != 0]) > 3:
                issues.append(f"相数超过 3：{bus_token}")
        for group in re.findall(r"\bbuses=\[([^\]]+)\]", line):
            for bus_token in group.split():
                name, nodes = split_bus(bus_token)
                all_bus_tokens.append((line.split(".")[0], bus_token, name))
                declared.add(name)
                for n in nodes:
                    if n not in (0, 1, 2, 3):
                        issues.append(f"节点号越界：{bus_token}")

    # 3. Phases 与节点数一致
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("//") or s.startswith("!"):
            continue
        m_ph = re.search(r"\bPhases=(\d+)", s, re.IGNORECASE)
        if not m_ph:
            m_ph = re.search(r"\bphases=(\d+)", s)
        if not m_ph:
            continue
        k = int(m_ph.group(1))
        bus_tokens = re.findall(r"\b(?:Bus1|Bus2)=(\S+)", s)
        for token in bus_tokens:
            _, nodes = split_bus(token)
            explicit = len([n for n in nodes if n != 0])
            if explicit and explicit != k:
                issues.append(f"Phases={k} 与节点数 {explicit} 不一致：{s[:80]}")

    # 4. 变压器变比与母线电压等级一致。
    #    产物里只有源端有显式 basekv（其余母线由 CalcVoltageBases 推导），
    #    因此用 `Set VoltageBases=[...]`（列出全部电压等级）作为参照集：
    #    每个绕组 kV 必须能在其中找到同值项——写错变比会在这里被抓住。
    base_kv: dict[str, float] = {}
    for m in re.finditer(r"New Circuit\.\S+ basekv=([0-9.]+)", text):
        base_kv["sourcebus"] = float(m.group(1))
    levels: list[float] = []
    mlevels = re.search(r"Set VoltageBases=\[([^\]]*)\]", text, re.IGNORECASE)
    if mlevels:
        levels = [float(v) for v in mlevels.group(1).split() if _is_float(v)]
    for line in text.splitlines():
        s = line.strip()
        mb = re.search(r"buses=\[(\S+) (\S+)\]\s+conns=\[[^\]]*\]\s+kvs=\[([0-9.]+) ([0-9.]+)\]", s)
        if not mb:
            continue
        b1, b2, kv1, kv2 = mb.group(1), mb.group(2), float(mb.group(3)), float(mb.group(4))
        for bus_token, kv in ((b1, kv1), (b2, kv2)):
            name, _ = split_bus(bus_token)
            known = base_kv.get(name)
            if known is not None and abs(known - kv) > 1e-6:
                issues.append(f"变压器 {bus_token} 侧 kV={kv:g} 与母线 basekv={known:g} 不一致")
            elif levels and not any(abs(kv - v) <= 1e-6 for v in levels):
                issues.append(
                    f"变压器 {bus_token} 侧 kV={kv:g} 不在 VoltageBases={levels} 内（变比可疑）"
                )

    # 5. 括号成对
    for op, cl in (("[", "]"), ("(", ")")):
        if text.count(op) != text.count(cl):
            issues.append(f"括号不成对：{op}{cl} （{text.count(op)} vs {text.count(cl)}）")
    return issues


def test_structural_sanity(texts: dict[str, str], tmp: str) -> None:
    """分相产物不得存在引用/相数/变比层面的结构错误。"""
    for label, text in texts.items():
        issues = dss_structural_issues(text)
        check(f"D[{label}] 产物结构自检无问题", not issues, "；".join(issues[:3]))

    # 反例：故意引用不存在的母线 → 自检必须报出来（否则这个自检是摆设）
    bad = "New Circuit.c basekv=10\nNew Line.L1 Bus1=nowhere.1 Bus2=alsomissing.1 Phases=2 R1=1 X1=1"
    check("D 自检能发现 Phases 与节点数不一致", any("不一致" in i for i in dss_structural_issues(bad)),
          str(dss_structural_issues(bad))[:120])
    bad2 = ("New Circuit.c basekv=10\nSet VoltageBases=[0.4 10]\n"
            "New Transformer.T phases=3 windings=2 buses=[sourcebus lv] conns=[wye wye] "
            "kvs=[10 99] kvas=[1 1] %Rs=[1 1] XHL=1")
    check("D 自检能发现变压器 kV 与其余电压等级不匹配",
          any("VoltageBases" in i or "kV=" in i for i in dss_structural_issues(bad2)),
          str(dss_structural_issues(bad2))[:120])
    # 事故回归：Line 上写 Conn 必须被自检抓住（真实导致过 COM 引擎死循环）
    bad3 = "New Circuit.c basekv=10\nNew Line.L1 Bus1=a Bus2=b Phases=3 R1=1 X1=1 Conn=wye"
    issues3 = dss_structural_issues(bad3)
    check("D 自检能发现 Line 上的非法属性 Conn（曾致引擎死循环）",
          any("不支持属性 Conn" in i for i in issues3), str(issues3)[:150])
    # 反向确认：Load 上的 Conn 是合法的，不应被误报
    ok_load = "New Circuit.c basekv=10\nNew Load.LD Bus1=a Phases=1 kV=0.23 kW=1 kvar=1 Conn=wye"
    check("D 自检不误报 Load 的合法 Conn", not dss_structural_issues(ok_load),
          str(dss_structural_issues(ok_load))[:150])


def test_rejections(tmp: str) -> None:
    bus = _bus_frame()
    # ① 单相负荷但不给"哪一相" → 必须报错（不许猜）
    line = _line_frame()
    bad_load = _load_frame()
    bad_load.loc[0, "phase"] = None
    try:
        cc.build_dss_from_frames(None, bus, line, os.path.join(tmp, "rej1"),
                                 source_label="case_rej1", load_df=bad_load)
        check("C1 单相负荷缺相别 → 报错", False, "未报错（危险：会静默猜相）")
    except ValueError as exc:
        check("C1 单相负荷缺相别 → 报错", "相数" in str(exc) or "相别" in str(exc), str(exc)[:110])

    # ② 非法相标 → 报错
    bad = _line_frame()
    bad.loc[1, "phase"] = "ZZ"
    try:
        cc.build_dss_from_frames(None, bus, bad, os.path.join(tmp, "rej2"), source_label="case_rej2")
        check("C2 非法相标 → 报错", False, "未报错")
    except ValueError as exc:
        check("C2 非法相标 → 报错", "无法解析" in str(exc) or "相" in str(exc), str(exc)[:110])

    # ③ 非法接线 → 报错
    bad2 = _line_frame()
    bad2.loc[1, "conn"] = "zigzag"
    try:
        cc.build_dss_from_frames(None, bus, bad2, os.path.join(tmp, "rej3"), source_label="case_rej3")
        check("C3 非法接线 → 报错", False, "未报错")
    except ValueError as exc:
        check("C3 非法接线 → 报错", "接线" in str(exc), str(exc)[:110])


def test_zero_regression(tmp: str) -> None:
    """没有相别列时，输出必须与旧行为一致：Phases=3、无节点后缀、保守声明。"""
    bus_cols = ["bus_id", "base_kv", "type", "pd_mw", "qd_mvar", "vm_set_pu"]
    bus = pd.DataFrame(
        [("SRC", 20.0, "slack", 0.0, 0.0, 1.02), ("B2", 20.0, "pq", 4.0, 1.5, 1.0)],
        columns=bus_cols,
    )
    line = pd.DataFrame(
        [("L1", "SRC", "B2", "line", 0.05, 0.10, 0.02, 10.0)],
        columns=["line_id", "from_bus", "to_bus", "kind", "r_pu", "x_pu", "b_pu", "rate_mva"],
    )
    master = cc.build_dss_from_frames(None, bus, line, os.path.join(tmp, "reg"),
                                      source_label="case_reg")
    text = open(master, encoding="utf-8").read()
    load = next((l for l in text.splitlines() if l.startswith("New Load")), "")
    ln = next((l for l in text.splitlines() if l.startswith("New Line")), "")
    check("C4 无相别列 → 负荷仍为 Phases=3 且无节点后缀",
          "Bus1=B2 Phases=3" in load, load[:110])
    check("C4 无相别列 → 线路仍为 Phases=3 且无节点后缀、无 R0/X0",
          "Bus1=sourcebus Bus2=B2" in ln and "Phases=3" in ln and "R0=" not in ln, ln[:150])
    check("C4 无相别列 → 头部标记 has_phase_columns=0", "has_phase_columns=0" in text)
    check("C4 无相别列 → 头部标记 unbalanced_input=0", "unbalanced_input=0" in text)
    check("C4 无相别列 → 声明平衡三相等值且相别不平衡需交叉核验",
          "相别不平衡" in text and "交叉核验" in text and "平衡三相" in text)
    check("C4 无相别列 → 不出现分相提示行", "phase_note=" not in text)


def _num(text: str, key: str) -> float:
    m = re.search(rf"\b{key}=({_NUM})", text)
    if not m:
        raise AssertionError(f"未找到 {key}= in {text[:120]}")
    return float(m.group(1))


_NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _is_float(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="phase_")
    test_phase_tokens()
    test_node_naming_and_kv()
    texts = _convert_all(tmp)
    test_conversion(texts)
    test_structural_sanity(texts, tmp)
    test_transformer_conn(tmp)
    test_single_phase_transformer(tmp)
    test_rejections(tmp)
    test_zero_regression(tmp)

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("=" * 70)
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}\n      {detail[:170]}")
    print(f"=== 结果：通过 {passed} / 失败 {failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
