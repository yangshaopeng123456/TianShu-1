# -*- coding: utf-8 -*-
"""三相不平衡基线扫描：离线重放历史产物，量化"被抹平的不平衡"。

为什么需要它：结果层此前用 `max(三相)` 把不平衡压成一个数，
所以**过去的报告看不出任何不平衡**。本工具不触发任何求解，只读
`outputs/<task>/raw_opendss_result.json`（内含原生分相数据），
按对称分量法重算不平衡度，给出：

1. 全网基线：多少任务/母线存在不平衡；
2. **被抹平的代价**：旧口径的"平衡等值最低电压" vs 真实最低**相**电压之差；
3. 超参考限值清单（含母线、三相电压）。

运行：`.venv/Scripts/python.exe -m dpf_agent.evals.run_unbalance_scan [--out 路径.md]`
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
from typing import Any

from ..config import AgentConfig
from ..domain.unbalance import METHOD, compute_metrics
from ..paths import doc_source_dir

LIMIT_PCT = 2.0


def _load(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def scan_task(task_dir: str) -> dict[str, Any] | None:
    """重放单个任务的原始产物，返回其不平衡统计。"""
    raw = _load(os.path.join(task_dir, "raw_opendss_result.json"))
    if not isinstance(raw, dict):
        return None
    buses = raw.get("buses") or []
    if not buses:
        return None

    worst_eps: float | None = None
    worst_bus = ""
    worst_phases: list[float] = []
    worst_spread: float | None = None
    assessed = 0
    not_assessed = 0
    over: list[dict[str, Any]] = []
    # 旧口径：每母线取三相最大值 → 全网最低
    legacy_min: float | None = None
    legacy_bus = ""
    # 新口径：所有母线的所有相 → 全网最低
    true_min: float | None = None
    true_bus = ""
    true_label = ""

    for b in buses:
        mags = [v for v in (b.get("voltage_pu") or []) if isinstance(v, (int, float))]
        angs = [a for a in (b.get("angle_deg") or []) if isinstance(a, (int, float))]
        if not mags:
            continue
        name = str(b.get("name"))
        m = compute_metrics(mags, angs)
        if m.status == "ok" and m.unbalance_pct is not None:
            assessed += 1
            if worst_eps is None or m.unbalance_pct > worst_eps:
                worst_eps, worst_bus, worst_phases = m.unbalance_pct, name, list(mags[:3])
                worst_spread = m.phase_spread_pct
            if m.unbalance_pct > LIMIT_PCT:
                over.append({"name": name, "unbalance_pct": m.unbalance_pct, "phases": list(mags[:3])})
        else:
            not_assessed += 1
        legacy = max(mags)
        if legacy_min is None or legacy < legacy_min:
            legacy_min, legacy_bus = legacy, name
        labels = ("A", "B", "C", "D")
        for i, v in enumerate(mags[:4]):
            if true_min is None or v < true_min:
                true_min, true_bus, true_label = v, name, labels[i] if i < 4 else str(i)

    spec = _load(os.path.join(task_dir, "task_spec.json")) or {}
    return {
        "task_id": os.path.basename(task_dir),
        "case_ref": os.path.basename(str(spec.get("case_ref") or "")),
        "bus_count": len(buses),
        "assessed": assessed,
        "not_assessed": not_assessed,
        "worst_bus": worst_bus,
        "worst_eps": worst_eps,
        "worst_phases": worst_phases,
        "worst_spread": worst_spread,
        "over_limit": sorted(over, key=lambda x: x["unbalance_pct"], reverse=True),
        "legacy_min": legacy_min,
        "legacy_min_bus": legacy_bus,
        "true_min": true_min,
        "true_min_bus": true_bus,
        "true_min_phase": true_label,
    }


def _case_label(case_ref: str, task_id: str) -> str:
    """算例展示名：case_ref 常是 `.../<算例名>/Master.dss`，取父目录更可读。"""
    ref = (case_ref or "").replace("\\", "/").strip()
    if not ref:
        return task_id
    parts = [p for p in ref.split("/") if p]
    base = parts[-1]
    if base.lower() == "master.dss":
        # 老任务只记了文件名、没记父目录 → 退回任务号，避免报告里出现一排 "Master.dss"
        return parts[-2] if len(parts) > 1 else task_id
    return base


def collect(outputs_root: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in sorted(glob.glob(os.path.join(outputs_root, "pf-*"))):
        r = scan_task(d)
        if r:
            r["case_label"] = _case_label(r.get("case_ref", ""), r["task_id"])
            rows.append(r)
    return rows


def _dedupe(rows: list[dict[str, Any]], key_fn) -> list[tuple[dict[str, Any], int]]:
    """按"算例 + 结果指纹"合并重复运行，返回 (代表行, 次数)。

    同一算例被反复运行时，这也能顺带证明**确定性求解的可复现性**：
    指纹相同即数值逐位一致。
    """
    seen: dict[Any, tuple[dict[str, Any], int]] = {}
    for r in rows:
        k = key_fn(r)
        if k in seen:
            row, n = seen[k]
            seen[k] = (row, n + 1)
        else:
            seen[k] = (r, 1)
    return sorted(seen.values(), key=lambda t: t[1], reverse=True)


def render(rows: list[dict[str, Any]]) -> str:
    L: list[str] = []
    L.append("# 天枢一号 · 三相不平衡基线报告\n")
    L.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    L.append(
        "口径：ε = |U₂|/|U₁| × 100%（对称分量法）。数据来源为历史任务的"
        "`raw_opendss_result.json`（OpenDSS 原生**分相**结果），本报告**不触发任何求解**。\n"
    )
    L.append(
        f"参考限值：{LIMIT_PCT}% —— 常用限值，**待与 GB/T 15543 原文核对后确认**，"
        "本报告只作参考标注。\n"
    )

    total = len(rows)
    with_eps = [r for r in rows if r["worst_eps"] is not None]
    over_tasks = [r for r in with_eps if r["worst_eps"] > LIMIT_PCT]
    uniq_cases = {r["case_label"] for r in rows}
    L.append("## 一、总体\n")
    L.append(f"- 可重放任务：**{total}**（涉及 **{len(uniq_cases)}** 个算例，含重复运行）")
    L.append(f"- 含可评估三相母线的任务：**{len(with_eps)}**")
    L.append(f"- 不平衡度超过参考限值 {LIMIT_PCT}% 的任务：**{len(over_tasks)}**")
    if with_eps:
        worst = max(with_eps, key=lambda r: r["worst_eps"])
        L.append(
            f"- 最严重：**{worst['case_label']}** — 母线 "
            f"`{worst['worst_bus']}`，ε = **{worst['worst_eps']:.3f}%**"
        )
    L.append("")

    # 被抹平的代价（按算例+指纹去重）
    deltas = [
        (r["legacy_min"] - r["true_min"], r) for r in rows
        if r["legacy_min"] is not None and r["true_min"] is not None
    ]
    deltas.sort(key=lambda x: x[0], reverse=True)
    L.append("## 二、旧口径的代价（被抹平的不平衡）\n")
    L.append(
        "旧结果层取每母线**三相最大值**作为「平衡等值电压」，再用它求全网最低电压。"
        "下表对比它与**真实最低相电压**的差距——差值越大，说明过去的报告越乐观。"
        "（同一算例多次运行结果逐位一致，故按结果指纹合并，`运行次数` 列即为可复现性证据。）\n"
    )
    L.append("| 算例 | 旧口径最低电压 (pu) | 真实最低相电压 (pu) | 差值 (pu) | 真实最低位置 | 运行次数 |")
    L.append("|---|---|---|---|---|---|")
    delta_rows = [r for delta, r in deltas if delta > 1e-9]
    delta_unique = _dedupe(
        delta_rows,
        lambda r: (r["case_label"], round(r["legacy_min"] or 0, 4), round(r["true_min"] or 0, 4),
                   r["true_min_bus"], r["true_min_phase"]),
    )
    delta_unique.sort(key=lambda t: (t[0]["legacy_min"] - t[0]["true_min"]), reverse=True)
    for r, n in delta_unique[:12]:
        L.append(
            f"| {r['case_label']} | {r['legacy_min']:.4f} | "
            f"{r['true_min']:.4f} | **{r['legacy_min'] - r['true_min']:+.4f}** | "
            f"{r['true_min_bus']} 的 {r['true_min_phase']} 相 | {n} |"
        )
    L.append("")

    L.append("## 三、超参考限值的算例（按算例去重）\n")
    L.append("| 算例 | 母线 | ε (%) | A/B/C 相电压 (pu) | 相间偏差 (%) | 超限母线数 | 运行次数 |")
    L.append("|---|---|---|---|---|---|---|")
    over_unique = _dedupe(
        over_tasks,
        lambda r: (r["case_label"], r["worst_bus"], round(r["worst_eps"] or 0, 3)),
    )
    over_unique.sort(key=lambda t: t[0]["worst_eps"], reverse=True)
    for r, n in over_unique[:15]:
        ph = " / ".join(f"{v:.5f}" for v in (r["worst_phases"] or [])[:3])
        spread = f"{r['worst_spread']:.2f}" if r["worst_spread"] is not None else "—"
        L.append(
            f"| {r['case_label']} | {r['worst_bus']} | **{r['worst_eps']:.3f}** | "
            f"{ph} | {spread} | {len(r['over_limit'])} | {n} |"
        )
    L.append("")

    L.append("## 四、结论\n")
    L.append(
        "1. OpenDSS 一直在按**三相不平衡**求解，分相结果从未缺失——"
        "缺的是结果层把它保留下来（旧实现 `max(三相)`）。"
    )
    L.append(
        "2. 因此过去报告中的「最低电压」在含不平衡的算例上**偏乐观**，"
        "上表第二列的差值即为被抹掉的量。"
    )
    L.append(
        "3. 本报告为**基线**：修复后（P0）新任务的产物会直接带 `unbalance_summary` 与分相电压，"
        "可用同一套口径复算对比。"
    )
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="三相不平衡基线扫描（离线，不触发求解）")
    # 默认输出到「天枢一号_文档\源文档\」，与归档的报告同一份（详见 paths.py）
    ap.add_argument("--out", default=os.path.join(doc_source_dir(), "天枢一号_三相不平衡基线报告.md"))
    # 产物根目录以 AgentConfig 为唯一来源，避免相对层级算错（曾因此扫到空目录）
    ap.add_argument("--outputs", default=AgentConfig().outputs_root, help="产物根目录")
    args = ap.parse_args()

    rows = collect(args.outputs)
    if not rows:
        print("未找到可重放的原始产物。")
        return 1
    md = render(rows)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    with_eps = [r for r in rows if r["worst_eps"] is not None]
    over = [r for r in with_eps if r["worst_eps"] > LIMIT_PCT]
    print(f"方法：{METHOD}")
    print(f"可重放任务 {len(rows)}；含三相母线 {len(with_eps)}；超 {LIMIT_PCT}% 的任务 {len(over)}")
    if with_eps:
        w = max(with_eps, key=lambda r: r["worst_eps"])
        print(f"最严重：{w['case_ref'] or w['task_id']} 母线 {w['worst_bus']} ε={w['worst_eps']:.3f}%")
    print(f"报告已写入：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
