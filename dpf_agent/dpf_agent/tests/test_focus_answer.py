# -*- coding: utf-8 -*-
"""聊天回复「问什么答什么」的定向取数回归（自包含，不依赖引擎/网络）。

## 背景（真机实测发现的缺陷）

用户问的是**复合问题**——「这个算例没有零序数据，无零序参数时你的模型怎么处理的？」
「负荷接在 delta 上，delta 内部有没有环流？」「含单相变压器，高低压侧接在哪一相？」
而聊天回复只有固定的五项摘要（收敛 / 最低电压 / 最高电压 / 网损 / 不平衡度），
**子问题一个都没答**。真机 8 道分相算例题里有 5 道因此判负。

而这些数在 `powerflow_result.json` 里**早就算好了**（`phase_voltage_pu`、
`negative_seq_pu`、`zero_seq_pu`、`transformer_results`、`system_summary`）——
缺的只是"按问的维度把它取出来"这一步。

## 本测试锁定的契约

1. 问里有主题词 → 摘要里必须出现该主题的**真实数值**；
2. **取不到就不说** —— 绝不用"通常/理论上"补一句（编数据比不回答更糟）；
3. 能力边界要**明说**（delta 内部环流算不了就说算不了，不给假数值）；
4. 相数 < 3 的母线必须被点出来，且不许把"相缺失"说成"三相平衡"；
5. 总负荷与净负荷必须**分列**（项目硬约定二）。

运行：`.venv/Scripts/python.exe -m dpf_agent.tests.test_focus_answer`
"""
from __future__ import annotations

import json
import sys

from ..web.chat_api import _focused_answers

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def _bus(name, labels, v=1.02, neg=1e-4, zero=1e-5, ub=None):
    n = len(labels)
    return {
        "name": name,
        "base_kv": 10.0,
        "voltage_pu": v,
        "angle_deg": 0.0,
        "phase_labels": labels,
        "phase_voltage_pu": [v + 0.001 * i for i in range(n)],
        "phase_voltage_kv": [10.2 + 0.01 * i for i in range(n)],
        "phase_angle_deg": [-120.0 * i for i in range(n)],
        "unbalance_pct": ub if ub is not None else 0.003,
        "phase_spread_pct": 0.004,
        "negative_seq_pu": neg,
        "zero_seq_pu": zero,
    }


DATA = {
    "solver_status": {"converged": True, "iterations": 2},
    "bus_results": [
        # 母线名与下面 `branch_results` 的端点**必须完全一致** ——
        # 不一致时拓扑判据会因数据不自洽而拒绝给结论（这本身是对的），
        # 但那样就测不到"环网判据算得对不对"。所以这里按真实产物
        # `pf-20261002-203021-bcb638`（aa_invalid_loop，**合法闭环**算例）
        # 的 8 个母线建：8 支路 − 8 母线 + 1 分量 = 独立回路数 1，
        # 弦边恰为闭环支路 Line.l002。
        _bus("sourcebus", ["A", "B", "C"], 1.02, 5.8e-8, 1.1e-10, 0.0),
        _bus("lv", ["A", "B", "C"], 1.0199, 3.0e-4, 3.0e-5, 0.003),
        _bus("a1", ["A", "B", "C"], 1.0196),
        _bus("a2", ["A", "B", "C"], 1.0197),
        _bus("b1", ["A", "B"], 1.0198),
        _bus("b2", ["A", "B", "C"], 1.0197),
        _bus("c1", ["A", "C"], 1.0195),
        _bus("c2", ["A", "B", "C"], 1.0194),
    ],
    "branch_results": [
        # 取自真实产物，数值是**真的**（206.18 kW / 133.66% 都不是编的）
        {"name": "Line.loop01", "from_bus": "sourcebus", "to_bus": "lv",
         "p_kw": 424.23, "loading_pct": 55.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.l001", "from_bus": "sourcebus", "to_bus": "b2",
         "p_kw": 199.89, "loading_pct": 30.0, "loading_status": "ok", "status": "ok"},
        # 弦边 = 环流分量所在
        {"name": "Line.l002", "from_bus": "lv", "to_bus": "b2",
         "p_kw": 206.18, "loading_pct": 33.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.l006", "from_bus": "lv", "to_bus": "a1",
         "p_kw": 58.00, "loading_pct": 12.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.l004", "from_bus": "lv", "to_bus": "c2",
         "p_kw": 69.00, "loading_pct": 14.0, "loading_status": "ok", "status": "ok"},
        # ↓ 这两条是**负号潮流**（潮流方向与支路命名相反）——
        # 纯辐射支路就会有，与闭环无关，绝不能被误判成环流。
        {"name": "Line.l003", "from_bus": "a2", "to_bus": "b2",
         "p_kw": -222.02, "loading_pct": 45.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.l005", "from_bus": "b1", "to_bus": "b2",
         "p_kw": -77.00, "loading_pct": 20.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.la1", "from_bus": "a1", "to_bus": "lv",
         "p_kw": 120.04, "loading_pct": 133.66,
         "loading_status": "overload", "status": "ok"},
        # ↓ 这两条补齐母线连接。**每个母线都必须被支路引用** ——
        # 否则"无支路连接的母线"会被算成孤岛，那是数据没造全，不是网络问题。
        {"name": "Line.l007", "from_bus": "a2", "to_bus": "a1",
         "p_kw": 102.0, "loading_pct": 25.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.l008", "from_bus": "c1", "to_bus": "c2",
         "p_kw": 41.0, "loading_pct": 10.0, "loading_status": "ok", "status": "ok"},
    ],
    "transformer_results": [
        {"name": "T1", "phases": 3},
        {"name": "T2", "phases": 1},
    ],
    "system_summary": {
        "total_load_kw": 190.0, "total_generation_kw": 190.04,
        "net_load_kw": 190.0, "total_storage_kw": 12.5,
        "min_voltage_pu": 1.0196, "min_voltage_bus": "an",
        "max_voltage_pu": 1.02, "max_voltage_bus": "sourcebus",
    },
    "loss_summary": {"total_loss_kw": 0.04, "loss_pct_of_load": 0.02},
    "unbalance_summary": {
        "buses_assessed": 2, "buses_not_assessed": 3,
        "worst_bus": "lv", "worst_unbalance_pct": 0.003,
        "limit_pct": 2.0, "buses_over_limit_count": 0,
    },
}


def main() -> int:
    def q(text: str) -> str:
        return "\n".join(_focused_answers(DATA, text))

    # ① 问零序 → 必须给出真实零序数值
    z = q("这个算例没有零序数据，无零序参数时你的模型是怎么处理的？")
    check("① 问零序会答零序（不是无视问题）", "零序" in z, z[:60])
    check("①b 零序给的是产物里的真实数值", "3.000e-05" in z or "3.0e-05" in z, z[:120])

    # ② 问 delta 环流 → 必须**明说算不了**，不许给假数值
    d = q("负荷接在三角形 delta 上，delta 内部是否存在环流？")
    check("② 问 delta 环流会正面回应", "delta" in d.lower() or "三角形" in d, d[:60])
    check("②b 能力边界写明「不计算」而不是编一个数",
          "不计算" in d and "环流" in d, d[:120])

    # ③ 问单相变压器 → 必须报出单相台数（产物里有 1 台）
    t = q("这个算例含单相变压器，高低压侧分别接在哪一相？")
    check("③ 问单相变压器会区分单相/三相台数",
          "单相" in t and "1 台" in t, t[:100])

    # ④ 问相电压 → 给分相电压，且落在最严重母线上
    p = q("各相电压是多少？A 相电压多大？")
    check("④ 问分相电压会给出 A/B/C 三相", all(k in p for k in ("A ", "B ", "C ")), p[:100])

    # ⑤ 平衡式：总负荷与净负荷必须**分列**（硬约定二）
    b = q("报告平衡式四数：总负荷、发电、网损、净负荷")
    check("⑤ 平衡式四数齐全且总负荷≠净负荷被分列",
          all(k in b for k in ("总负荷", "发电", "网损", "净负荷")), b[:140])
    check("⑤b 储能也进平衡式", "储能" in b, b[:140])

    # ⑥ 相数 < 3 的母线必须点出，且不许说成"三相平衡"
    check("⑥ 相缺失母线被显式点出（不混同为三相平衡）",
          "相数 < 3" in (p + z) and "不平衡度在其上无定义" in (p + z), (p + z)[-140:])

    # ⑥b 必须**逐条点明缺的是哪一相**（真机实测缺陷 case39：
    # 用户问"C 相是缺失还是零电流"，回复只说"按缺失相处理"，等于没答）
    check("⑥b 缺失相逐条点明是哪一相",
          "b1 缺 C 相" in (p + z) and "c1 缺 B 相" in (p + z), (p + z)[-200:])
    check("⑥c 「缺失」与「零电流」的区别被讲清（不混同）",
          "缺失相是该相根本没有接入元件" in (p + z), (p + z)[-200:])

    # ⑦ 问不到的主题 → 一句都不加（不许无差别倾泻所有字段）
    plain = q("收敛了吗？")
    check("⑦ 无关问题不追加任何定向数据", plain.strip() == "", repr(plain[:80]))

    # ⑧ 取不到零序字段时**明说没有**，不给假设值
    no_zero = dict(DATA)
    no_zero["bus_results"] = [
        {k: v for k, v in b_.items() if k != "zero_seq_pu"} for b_ in DATA["bus_results"]
    ]
    nz = "\n".join(_focused_answers(no_zero, "零序电流有多大？"))
    check("⑧ 结果里没有零序字段时明说「没有」而不是编数",
          "没有零序电压字段" in nz and "不给" in nz, nz[:120])

    # ⑨ 能力边界句不得出现"通常/理论上"这类推测措辞
    allq = "\n".join(q(t_) for t_ in (
        "零序？", "delta 环流？", "单相变压器？", "平衡式？", "各相电压？", "接地方式？"))
    banned = [w for w in ("通常", "理论上", "一般来说", "估计约", "大概是") if w in allq]
    check("⑨ 定向取数里没有推测措辞（可审计）", not banned, str(banned))

    # ⑩ 同样输入产出同样输出（可复现）
    probe = "各相电压？零序？平衡式？"
    again = _focused_answers(DATA, probe)
    check("⑩ 同样输入产出同样输出（可复现）",
          again == _focused_answers(DATA, probe))
    check("⑩b 输入不同则取数不同（否则说明没在按问题走）",
          _focused_answers(DATA, "零序？") != _focused_answers(DATA, "平衡式？"))

    # ── ⑪ 环网：必须区分「真实成环」与「重复录入的数据错误」──────────
    # 真机实测缺陷（aa_invalid_loop）：用户问"环在哪几条支路上、
    # 是真实成环还是数据错误、怎么改数据"，回复只有收敛/电压/网损三项。
    ring = q("这个算例是坏的（我预期有环形/成环问题）。请找出环在哪几条支路上、"
             "这个环是真实成环还是数据错误？")
    check("⑪ 问成环会用图论判据定性（独立回路数）",
          "独立回路数" in ring and "真实成环" in ring, ring[:130])
    check("⑪b 给出环流分量与占比（真实数值，非推测）",
          "弦边" in ring and "206.18" in ring and "%" in ring, ring[:220])

    # ⑪c 纯辐射网 → 明确说"不存在任何闭环"，不许误报成环
    # 辐射组：4 母线 / 3 支路 / 1 分量 → 独立回路数 = 0，无弦边。
    # （母线名与端点也必须自洽，否则会被前置校验挡掉，测不到判据本身）
    rad_d = json.loads(json.dumps(DATA))
    rad_d["bus_results"] = [
        _bus("sourcebus", ["A", "B", "C"], 1.02, 5.8e-8, 1.1e-10, 0.0),
        _bus("lv", ["A", "B", "C"], 1.0199, 3.0e-4, 3.0e-5, 0.003),
        _bus("a1", ["A", "B", "C"], 1.0196),
        _bus("b1", ["A", "B", "C"], 1.0198),
    ]
    rad_d["branch_results"] = [
        {"name": "Line.r1", "from_bus": "sourcebus", "to_bus": "lv",
         "p_kw": 190.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.r2", "from_bus": "lv", "to_bus": "a1",
         "p_kw": 120.0, "loading_pct": 30.0, "loading_status": "ok", "status": "ok"},
        # 负号潮流：验证它不会被当成环流
        {"name": "Line.r3", "from_bus": "a1", "to_bus": "b1",
         "p_kw": -77.0, "loading_pct": 20.0, "loading_status": "ok", "status": "ok"},
    ]
    rad = "\n".join(_focused_answers(rad_d, "这个算例成环了吗？闭环上的环流有多少？"))
    check("⑪c 辐射网不被误报成环（独立回路数=0）",
          "纯辐射网" in rad and "无弦边" in rad, rad[:170])
    check("⑪c2 辐射网的负号潮流不被当成环流（真机曾误判）",
          "Line.r3" not in rad.split("弦边")[-1] if "弦边" in rad else True, rad[:170])

    # ⑪d 同一对端点被录两次 → 判为**数据错误**（与真实成环严格区分）
    dupd = json.loads(json.dumps(DATA))
    dupd["branch_results"] = [
        {"name": "Line.lv1", "from_bus": "sourcebus", "to_bus": "lv",
         "p_kw": 190.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.lv1_dup", "from_bus": "lv", "to_bus": "sourcebus",
         "p_kw": 190.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
    ]
    dr = "\n".join(_focused_answers(dupd, "是否成环？同一段线路是否被重复录入？"))
    check("⑪d 重复录入被判为数据错误（区别于真实成环）",
          "同一对端点被录入多条支路" in dr and "数据错误" in dr, dr[:200])

    # ⑪e 母线/支路数据不自洽时**拒绝给结论**（不许带着错结论继续答）
    bad = json.loads(json.dumps(DATA))
    bad["branch_results"] = [
        {"name": "Line.x1", "from_bus": "sourcebus", "to_bus": "lv",
         "p_kw": 190.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.x2", "from_bus": "lv", "to_bus": "ghost_bus",
         "p_kw": 12.0, "loading_pct": 5.0, "loading_status": "ok", "status": "ok"},
    ]
    bd = "\n".join(_focused_answers(bad, "是否成环？"))
    check("⑪e 引用不存在的节点时明说数据不自洽（不给错结论）",
          "ghost_bus" in bd and "数据一致性问题" in bd and "真实成环" not in bd, bd[:200])

    # ── ⑫ 连通性 / 孤岛：从产物严格算可达性 ───────────────────────
    isl = q("断线后网络是否分裂成孤岛？")
    check("⑫ 问孤岛会给连通分量结论",
          "连通分量" in isl and "无孤岛" in isl, isl[:150])

    # ⚠️ 这两组必须把 `converged` 置 False ——
    # 断线产生真孤岛时**潮流不可能收敛**（失电区无电压方程解），
    # 若沿用 DATA 里 converged=True，会被自洽检查（"收敛却有不可达母线"）
    # 拦下，测到的就不是"孤岛判定"本身了。
    isld = json.loads(json.dumps(DATA))
    isld["solver_status"] = {"converged": False, "iterations": 0}
    isld["bus_results"] = [
        _bus("sourcebus", ["A", "B", "C"], 1.02, 5.8e-8, 1.1e-10, 0.0),
        _bus("lv", ["A", "B", "C"], 1.0199, 3.0e-4, 3.0e-5, 0.003),
        _bus("c2", ["A", "B", "C"], 1.0194),
    ]
    isld["branch_results"] = [
        {"name": "Line.lv1", "from_bus": "sourcebus", "to_bus": "lv",
         "p_kw": 190.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
        # 这条连着 c2，但**状态是断开** → c2 被隔离 = 真空岛
        {"name": "Line.tie1", "from_bus": "lv", "to_bus": "c2",
         "p_kw": 0.0, "loading_pct": 0.0,
         "loading_status": "ok", "status": "open"},
    ]
    isl2 = "\n".join(_focused_answers(isld, "哪些母线是孤岛？断线后是否分裂？"))
    check("⑫b 被断开支路隔离的母线判为孤岛并点名",
          "孤岛 1 个母线" in isl2 and "c2" in isl2 and "已断开支路 1 条" in isl2,
          isl2[:220])

    # ⑫c 未导出支路的母线不被误判成孤岛（结果域不完整）
    dang = json.loads(json.dumps(DATA))
    dang["solver_status"] = {"converged": False, "iterations": 0}
    dang["bus_results"] = [
        _bus("sourcebus", ["A", "B", "C"], 1.02, 5.8e-8, 1.1e-10, 0.0),
        _bus("lv", ["A", "B", "C"], 1.0199, 3.0e-4, 3.0e-5, 0.003),
        _bus("no_export_bus", ["A", "B", "C"], 1.0195),
    ]
    dang["branch_results"] = [
        {"name": "Line.lv1", "from_bus": "sourcebus", "to_bus": "lv",
         "p_kw": 190.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
    ]
    dg = "\n".join(_focused_answers(dang, "网络是否分裂成孤岛？"))
    check("⑫c 未导出支路的母线不被误判成孤岛（结果域不完整）",
          "结果域不完整" in dg and "no_export_bus" in dg
          and "孤岛 1 个母线" not in dg, dg[:220])

    # ⑫d 收敛 + 多馈线（各段靠变压器互联，变压器不在支路结果里）
    # → 绝不能报"孤岛"或"独立回路数"。真机 case31 曾报 264 个孤岛。
    # 这组是**本轮最重要的回归**：判据曾输出"已收敛"与"潮流无解"并存。
    multi = json.loads(json.dumps(DATA))
    multi["solver_status"] = {"converged": True, "iterations": 3}
    multi["bus_results"] = [
        _bus("grid", ["A", "B", "C"], 1.0, 1e-8, 1e-10, 0.0),
        _bus("f1_01", ["A", "B", "C"], 0.98), _bus("f1_02", ["A", "B", "C"], 0.97),
        _bus("f2_01", ["A", "B", "C"], 0.98), _bus("f2_02", ["A", "B", "C"], 0.97),
    ]
    multi["branch_results"] = [
        {"name": "Line.f1l01", "from_bus": "f1_01", "to_bus": "f1_02",
         "p_kw": 900.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.f2l01", "from_bus": "f2_01", "to_bus": "f2_02",
         "p_kw": 900.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
    ]
    multi["transformer_results"] = [
        {"name": "Transformer.ta", "p_kw": 920.0, "loading_pct": 44.0},
    ]
    # ⑫e 支路图不完整时不报独立回路数与环流数值
    # 题干要**同时问孤岛**：环流那段在 `island` 主题之后才输出，
    # 只问"成环"的话孤岛主题不触发，那一段根本不会执行（那是设计如此）。
    ml = "\n".join(_focused_answers(multi, "这个算例成环了吗？哪些母线是孤岛？闭环环流多大？"))
    isl_only = "\n".join(_focused_answers(multi, "哪些母线是孤岛？"))
    # ⚠️ 判据不能只查"文中有没有'孤岛'二字" ——
    # 正确输出里"孤岛"出现在**否定句**（"而不是网络真有孤岛"）。
    # 必须查"没有下孤岛结论"的标记，否则会被否定句里的字样误判成失败。
    check("⑫d 收敛+多馈线不下孤岛结论（真机 case31 曾误报 264 个）",
          "不给孤岛结论" in isl_only and "而不是网络真有孤岛" in isl_only
          and "孤岛 %d 个母线" not in isl_only and "潮流无解" not in isl_only,
          isl_only[:220])
    check("⑫e 支路图不完整时不报独立回路数与环流数值",
          "不给独立回路数与成环结论" in ml and "不给环流数值" in ml, ml[:260])

    # ⑫f ★★ 本轮修复：变压器**带连接母线**时必须并进拓扑图。
    # 修复前`transformer_results` 只有 name/p/q/loading，配电网的
    # 「grid —变压器→ 各馈线」这条主干就断了 → 每条馈线各自成一分量 →
    # 只能判成"结果域不完整、不给孤岛结论"（真机 case31：15 个分量、
    # 268 个母线"不可达"，白丢一个本来能算出来的结论）。
    # 修复后：grid 经变压器挂到 f1/f2，两条馈线应合并为**同一个**连通分量。
    linked = json.loads(json.dumps(DATA))
    linked["solver_status"] = {"converged": True, "iterations": 3}
    linked["bus_results"] = [
        _bus("grid", ["A", "B", "C"], 1.0, 1e-8, 1e-10, 0.0),
        _bus("f1_01", ["A", "B", "C"], 0.98), _bus("f1_02", ["A", "B", "C"], 0.97),
        _bus("f2_01", ["A", "B", "C"], 0.98), _bus("f2_02", ["A", "B", "C"], 0.97),
    ]
    linked["branch_results"] = [
        {"name": "Line.f1l01", "from_bus": "f1_01", "to_bus": "f1_02",
         "p_kw": 900.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
        {"name": "Line.f2l01", "from_bus": "f2_01", "to_bus": "f2_02",
         "p_kw": 900.0, "loading_pct": 40.0, "loading_status": "ok", "status": "ok"},
    ]
    # 复刻case31 真实结构：grid 只经变压器挂到各馈线首端（母线名大小写按引擎返回）
    linked["transformer_results"] = [
        {"name": "Transformer.t1", "from_bus": "grid", "to_bus": "f1_01",
         "p_kw": 920.0, "loading_pct": 44.0},
        {"name": "Transformer.t2", "from_bus": "grid", "to_bus": "f2_01",
         "p_kw": 920.0, "loading_pct": 44.0},
    ]
    lk = "\n".join(_focused_answers(linked, "网络分裂成孤岛了吗？"))
    check("⑫f ★ 变压器并入拓扑图后应合并为单一连通分量（不给孤岛、不说不完整）",
          "连通分量" in lk and "结果域不完整" not in lk
          and "不给孤岛结论" not in lk and "不可达" not in lk, lk[:260])

    # ⑫g 变压器**没有**连接母线时（老产物/CLI 通道取不到），必须仍明说
    # 数据不足 —— 绝不能因为"并了变压器"就假装图是完整的。
    nolink = json.loads(json.dumps(linked))
    nolink["transformer_results"] = [
        {"name": "Transformer.t1", "p_kw": 920.0, "loading_pct": 44.0},
    ]
    nl = "\n".join(_focused_answers(nolink, "网络分裂成孤岛了吗？"))
    check("⑫g 变压器缺连接母线时明说数据不足（不假装图完整）",
          "不含连接母线" in nl and "不给孤岛结论" in nl, nl[:260])

    # ⑫h ★★ 端点齐全时，**必须给出分量的母线数**（而不只是"不给孤岛"）。
    #
    # ⑫f 只锁住了"不许降级"，但没锁住"降级没了以后该给的东西给了没有"。
    # 真机 case31 的诉求原话是「报告每个连通分量的母线数量」——
    # 只回一句"无孤岛、连通性完整"是**没答完**的：读者仍不知道有多少个分量、
    # 各有多少母线。而这类"该给数字却不给"的半吊子回复，靠 ⑫f 完全测不出来
    #（它只检查了"不出现某句话"）。
    #
    # 判据要卡在**数字**上：分量个数 + 最大分量的母线数，且母线数必须与
    # 传入的母线总数自洽（5 条母线 → 不能报出 285）。
    _n_bus = len(linked.get("bus_results") or [])
    import re as _re

    _m_comp = _re.search(r"连通分量：共\s*(\d+)\s*个", lk)
    _m_size = _re.search(r"最大分量含\s*(\d+)\s*个母线", lk)
    _got_comp = int(_m_comp.group(1)) if _m_comp else -1
    _got_size = int(_m_size.group(1)) if _m_size else -1
    check("⑫h ★ 端点齐全时必须报出分量个数与最大分量母线数（不能只说「无孤岛」）",
          _got_comp >= 1 and _got_size == _n_bus,
          f"分量={_got_comp} 最大分量={_got_size} 母线总数={_n_bus} | {lk[:160]}")

    # ── ⑬ 过载：只报 loading_pct ≥ 100 的支路，且给真实负载率 ──────
    ovl = q("恢复后最大支路负载率是多少？哪些支路过载了？")
    check("⑬ 过载支路按负载率降序列出并给判定",
          "过载支路 1 条" in ovl and "la1" in ovl and "133.7%" in ovl, ovl[:200])

    # ── ⑭ 多文件算例的 Redirect 依赖链 ─────────────────────────────
    # 真机 case32（`case32_bundle_reconfiguration_v6.zip`）的实测缺陷：
    # 用户问"ZIP 里有哪些 .dss、哪些是 Master 真正 Redirect 引用的、哪些不是"，
    # 回复只字未提。根因：ZIP 入口解压后直接返回 `Master.dss`，
    # **从不解析依赖链** —— 而这正是那道题的**全部考点**。
    # 判据全部**从磁盘真实文件算出来**，不预设结论。
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        # ⚠️ 源目录与 workspace 必须是**并列的两棵独立目录树**。
        # 真机：`chat_data/uploads/<sess>/<名>/`（ZIP 解压产物，10 个 `.dss`）
        # 与 `dpf_agent/tasks/pf-xxx/`（产物 + `input/` 闭包，8 个）。
        # 早先版本把 `ws/` 嵌在源目录里，结果 `glob(root/**/*.dss)`
        # 把闭包副本一起数进去（10+8=18）—— 单测自己造出的布局不真，
        # 判据再严也测不对东西。
        td = os.path.join(root, "src")
        ws = os.path.join(root, "ws")
        os.makedirs(td, exist_ok=True)
        os.makedirs(os.path.join(ws, "input"), exist_ok=True)

        def w(rel, txt):
            p = os.path.join(td, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(txt)
            return p

        def wws(rel, txt):
            p = os.path.join(ws, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(txt)
            return p

        # 源目录 = case32 真实结构：Master + base 6 + profiles 1 + scenarios 2 = 10
        master = w("Master.dss", "\n".join([
            "Clear",
            "Redirect base/Circuit.dss",
            "Redirect base/LineCodes.dss",
            "Redirect profiles/LoadShapes.dss",
            "Redirect base/Transformers.dss",
            "Redirect base/Lines.dss",
            "Redirect base/Loads.dss",
            "Redirect base/DER.dss",
            "! scenarios/ 下的文件刻意不在依赖链上",
        ]) + "\n")
        for n in ("Circuit", "LineCodes", "Lines", "Loads", "DER", "Transformers"):
            w("base/%s.dss" % n, "Set MaxIterations=140\n")
        w("profiles/LoadShapes.dss", "New LoadShape=day\n")
        w("scenarios/outage_only.dss", "Open Line.tie1 Status=open\n")
        w("scenarios/restored.dss", "Close Line.tie1\n")

        # artifacts 必须按**真机形态**给：里面只有 `*.json`，**没有**
        # `Master.dss`（`ToolContext.save_json` 只写 JSON）。第一版实现就是
        # 直接 `artifacts["Master.dss"]` —— 单测给了这个键所以通过，
        # 真机上必然取不到。
        # workspace：`powerflow_result.json` 在**根**，`Master.dss` 在 `input/`
        #（`load_case_bundle` 复制的 **Redirect 依赖闭包**，只有 8 个，
        #  scenarios 两个**不在里面**）。第一版单测把 Master 放在结果 JSON
        # 同级 —— 真机上这是错的，所以单测全绿、真机答不出来。
        # 拿 `ws/input/` 当"算例目录"更糟：会得到"8 个、全被引用"，
        # 把本题答案 negate 掉 —— 比答不出来更坏，因为它看着像正确答案。
        pf = os.path.join(ws, "powerflow_result.json")
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump(DATA, fh)
        m_in = wws("input/Master.dss", open(master, encoding="utf-8").read())
        for n in ("Circuit", "LineCodes", "Lines", "Loads", "DER", "Transformers"):
            wws("input/base/%s.dss" % n, "Set MaxIterations=140\n")
        wws("input/profiles/LoadShapes.dss", "New LoadShape=day\n")

        arts = {"powerflow_result.json": pf}
        rmk = "\n".join(_focused_answers(
            DATA, "这个 ZIP 有哪些 .dss？哪些是 Master 真正 Redirect 引用的？"
                  "scenarios 下的文件没被引用的话请说出来",
            artifacts=arts, case_ref=master))

        check("⑭a case_ref 指向源目录 → 报出全部 10 个 .dss",
              "共 10 个 `.dss`" in rmk, rmk[:200])
        check("⑭b 报出依赖链 8 个，且 profiles/LoadShapes 不被漏判",
              "依赖链上 **8 个**" in rmk and "loadshapes" in rmk.lower(), rmk[:260])
        check("⑭c ★ 依赖链顺序 = Master 声明顺序（Redirect 是有序执行，顺序有语义）",
              "master → base/circuit → base/linecodes → profiles/loadshapes → "
              "base/transformers → base/lines → base/loads → base/der"
              in rmk.lower(), rmk[:300])
        check("⑭d scenarios 下 2 个 .dss 判为「未被 Redirect 引用、本次未执行」",
              "未被 Redirect 引用" in rmk and "outage_only" in rmk
              and "restored" in rmk, rmk[:300])
        check("⑭e 未被引用文件**分类**为场景文件，并说明为何不能自动执行",
              "场景文件" in rmk and "独立场景" in rmk and "物理意义" in rmk, rmk[:300])

        # ⑭h ★★ 锁死"不能用 input/ 闭包当算例目录"这个坑：
        # 只给 case_ref=input 里的 Master（闭包 8 个、无 scenarios）时，
        # 必须**明说目录里只有 8 个、scenarios 不在其中**，绝不能把
        # "scenarios 两个文件未被引用"这条结论凭空编出来。
        rclosed = "\n".join(_focused_answers(
            DATA, "哪些 .dss 被 Master 引用了？ZIP 里有哪些 .dss？",
            artifacts=arts, case_ref=m_in))
        check("⑭h ★ 闭包目录(8 个)不得凭空编出 scenarios 结论",
              "共 8 个 `.dss`" in rclosed and "scenarios" not in rclosed,
              rclosed[:240])

        # ⑭i 无 case_ref 时，从 artifacts 退化定位（结果 JSON 同级 + input/ 两级）
        rdeg = "\n".join(_focused_answers(
            DATA, "哪些 .dss 被 Master 引用了？", artifacts=arts))
        check("⑭i 无 case_ref 时退化定位到 input/ 下的 Master",
              "共 8 个 `.dss`" in rdeg, rdeg[:200])

        # ⑭f 取不到算例目录时明说取不到，绝不猜
        rnone = "\n".join(_focused_answers(
            DATA, "哪些 .dss 被 Master 引用了？", artifacts={}))
        check("⑭f 取不到算例目录时明说取不到（不给猜测）",
              "无法核对 Redirect 依赖链" in rnone and "不给猜测" in rnone,
              rnone[:200])

        # ⑭g 单文件算例不虚报依赖链
        solo_dir = os.path.join(td, "solo")
        os.makedirs(solo_dir, exist_ok=True)
        solo = os.path.join(solo_dir, "Master.dss")
        with open(solo, "w", encoding="utf-8") as fh:
            fh.write("Clear\nNew Circuit=t1\n")
        rsolo = "\n".join(_focused_answers(
            DATA, "哪些 .dss 被引用了？", artifacts={"Master.dss": solo}))
        check("⑭g 单文件算例不虚报依赖链",
              "单文件算例" in rsolo and "没有 Redirect 依赖链" in rsolo, rsolo[:200])

    # ⑮ 功率平衡残差必须能取到（真机 case04 实测缺陷）：
    #     报告第 9 节写着「功率平衡残差 0.000028 pu，在容差内」，
    #     聊天回复却只给了四数、没提残差 —— 判据"回复覆盖关键要点"缺「残差」。
    #     残差只落在 verification.json（不在 powerflow_result.json 里），
    #     所以必须经 artifacts 读产物文件。
    import os as _os
    import tempfile as _tf

    td = _tf.mkdtemp(prefix="resid_")
    try:
        vpath = _os.path.join(td, "verification.json")
        with open(vpath, "w", encoding="utf-8") as fh:
            json.dump({"task_id": "pf-x", "passed": True,
                       "power_balance_residual_pu": 2.790776318694996e-05,
                       "power_balance_tolerance": 0.001}, fh)
        rb = "\n".join(_focused_answers(
            DATA, "平衡式四数（总负荷/发电/网损/净负荷）与残差",
            artifacts={"verification.json": vpath}))
        check("⑮ 问「四数与残差」必须给出功率平衡残差",
              "残差" in rb, rb[:200])
        check("⑮b 残差取的是 verification.json 里的真实值（2.79e-05）",
              "2.79e-05" in rb, rb[:260])
        check("⑮c 残差同时带出容差（0.001），便于判断是否通过",
              "容差 0.001" in rb, rb[:260])

        # 单独问残差也要命中（此前「残差」不在 _FOCUS_HINTS 关键词里）
        ronly = "\n".join(_focused_answers(
            DATA, "功率平衡残差是多少？",
            artifacts={"verification.json": vpath}))
        check("⑮d 单独问「功率平衡残差」也能命中", "2.79e-05" in ronly, ronly[:200])

        # 取不到残差时**不许编**，也不许拿四数冒充
        rmiss = "\n".join(_focused_answers(
            DATA, "平衡式四数与残差", artifacts={}))
        check("⑮e 取不到残差时不说这句（不用四数凑、不编数）",
              "残差" not in rmiss, rmiss[:200])
        check("⑮f 取不到残差时四数照给（不能因缺一项就整体不答）",
              "总负荷" in rmiss, rmiss[:200])
    finally:
        import shutil as _sh
        _sh.rmtree(td, ignore_errors=True)

    print()
    print(f"=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
