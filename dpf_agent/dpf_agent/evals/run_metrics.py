# -*- coding: utf-8 -*-
"""运行指标采集与看板生成（P1）。

数据来源全部是既有产物，不触发任何计算：
    tasks/<task_id>/trace.json      —— 事件流（工具调用/结果/阶段/耗时/错误码）
    tasks/<task_id>/state.json      —— 终态与阶段
    outputs/<task_id>/raw_opendss_result.json —— 求解缓存命中标记

产出：
    控制台表格 + 桌面《天枢一号_指标看板.md》 + outputs/_metrics/metrics.json

用法：
    python -m dpf_agent.evals.run_metrics
    python -m dpf_agent.evals.run_metrics --since 2026-09-11   # 只看某天之后的
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from typing import Any

DPF_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if DPF_ROOT not in sys.path:          # 允许以脚本方式直接运行
    sys.path.insert(0, DPF_ROOT)
from dpf_agent.paths import doc_source_dir  # noqa: E402


def _load(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return vals[idx]


def collect(tasks_root: str, outputs_root: str, since: float | None = None) -> dict[str, Any]:
    trace_files = sorted(glob.glob(os.path.join(tasks_root, "pf-*", "trace.json")))
    total = 0
    status_count: Counter[str] = Counter()
    error_codes: Counter[str] = Counter()
    fail_stages: Counter[str] = Counter()
    tool_fail: Counter[str] = Counter()
    tool_calls: Counter[str] = Counter()
    tool_durations: dict[str, list[float]] = defaultdict(list)
    e2e_durations: list[float] = []
    e2e_by_family: dict[str, list[float]] = defaultdict(list)
    replan_tasks = 0
    cache_hits = 0
    guard_events = 0
    interrupted_tasks = 0
    # 最后落盘阶段分布：崩溃任务"死在哪一步"的直接线索（增量落盘后才有）
    interrupted_stages: Counter[str] = Counter()
    llm_tasks = 0
    llm_calls = 0
    llm_tokens = 0
    llm_duration = 0.0
    llm_by_model: dict[str, dict[str, int]] = {}
    daily: dict[str, Counter] = defaultdict(Counter)

    for tf in trace_files:
        task_dir = os.path.dirname(tf)
        task_id = os.path.basename(task_dir)
        events = _load(tf) or []
        if not isinstance(events, list) or not events:
            continue
        stamps = [e.get("timestamp") for e in events if e.get("timestamp")]
        if not stamps:
            continue
        first, last = min(stamps), max(stamps)
        if since is not None and last < since:
            continue
        total += 1

        state = _load(os.path.join(task_dir, "state.json")) or {}
        status = str(state.get("status") or "unknown")
        if status == "unknown":
            # 有 trace 但没有可用 state.json → 进程没走完生命周期（崩溃/被杀）。
            #
            # 以前这里直接记 "unknown"，等于把**基础设施问题**混进业务分布：
            # 指标看板 21 个 unknown（5.5%）全部源于此，而 unknown 既不是
            # 成功也不在失败集合里，成功率的分母被凭空放大。
            # 现在归入 interrupted（实测口径），并且**单独计数**，
            # 便于一眼看出"有多少任务是因进程死掉而丢失结论"。
            status = "interrupted"
            interrupted_tasks += 1
            # 增量落盘留下的"遗言"：这个任务最后成功到达哪个阶段
            st = state.get("_persisted_stage")
            if st:
                interrupted_stages[str(st)] += 1
            elif state.get("phase"):
                interrupted_stages[str(state["phase"])] += 1
            else:
                interrupted_stages["(无痕迹)"] += 1
        status_count[status] += 1
        error = (state.get("error") or {}) if isinstance(state.get("error"), dict) else {}
        if error.get("code"):
            error_codes[str(error["code"])] += 1
        if error.get("stage"):
            fail_stages[str(error["stage"])] += 1

        dur = last - first
        e2e_durations.append(dur)
        day = dt.datetime.fromtimestamp(last).strftime("%Y-%m-%d")
        daily[day][status] += 1

        spec = _load(os.path.join(task_dir, "task_spec.json")) or {}
        case_ref = str(spec.get("case_ref") or "")
        family = "其他"
        low = case_ref.lower()
        idx = low.find("case")
        if idx >= 0:
            family = low[idx:idx + 14].split("\\")[0].split("/")[0]
        elif "ieee" in low:
            family = "ieee"
        e2e_by_family[family].append(dur)

        if any(e.get("event_type") == "plan" and "重规划" in str(e.get("message") or "") for e in events):
            replan_tasks += 1
        guard_events += sum(1 for e in events if e.get("event_type") == "guard")

        raw = _load(os.path.join(outputs_root, task_id, "raw_opendss_result.json"))
        if isinstance(raw, dict) and (raw.get("cache") or {}).get("hit"):
            cache_hits += 1

        # LLM 消耗（P2）：任务收尾写入 llm_usage.json
        usage = _load(os.path.join(task_dir, "llm_usage.json"))
        if isinstance(usage, dict):
            llm_tasks += 1
            llm_calls += int(usage.get("calls") or 0)
            llm_tokens += int(usage.get("total_tokens") or 0)
            llm_duration += float(usage.get("duration_s") or 0.0)
            for model, m in (usage.get("models") or {}).items():
                bucket = llm_by_model.setdefault(model, {"calls": 0, "tokens": 0})
                bucket["calls"] += int(m.get("calls") or 0)
                bucket["tokens"] += int(m.get("prompt_tokens") or 0) + int(
                    m.get("completion_tokens") or 0
                )

        # 工具耗时：tool_call → 同工具 tool_result 配对（用 result 自带 duration）
        for e in events:
            if e.get("event_type") == "tool_result":
                name = str(e.get("tool_name") or "?")
                tool_calls[name] += 1
                if e.get("status") == "failed":
                    tool_fail[name] += 1
                if isinstance(e.get("duration"), (int, float)):
                    tool_durations[name].append(float(e["duration"]))

    def _p(vals: list[float], q: float) -> float | None:
        v = _pct(vals, q)
        return round(v, 2) if v is not None else None

    ok_statuses = {"success", "converged"}
    success = sum(n for s, n in status_count.items() if s in ok_statuses)
    metrics = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks_total": total,
        "status_distribution": dict(status_count.most_common()),
        "success_rate": round(success / total, 4) if total else None,
        "e2e_seconds": {
            "p50": _p(e2e_durations, 0.50),
            "p90": _p(e2e_durations, 0.90),
            "p95": _p(e2e_durations, 0.95),
            "max": _p(e2e_durations, 1.0),
            "avg": round(statistics.fmean(e2e_durations), 2) if e2e_durations else None,
        },
        "e2e_by_family": {
            fam: {"n": len(vals), "p50": _p(vals, 0.50), "p95": _p(vals, 0.95)}
            for fam, vals in sorted(e2e_by_family.items(), key=lambda kv: -len(kv[1]))[:12]
        },
        "stage_seconds": {
            name: {
                "n": len(vals),
                "p50": _p(vals, 0.50),
                "p95": _p(vals, 0.95),
                "max": _p(vals, 1.0),
                "fail": tool_fail.get(name, 0),
                "fail_rate": round(tool_fail.get(name, 0) / len(vals), 4) if vals else None,
            }
            for name, vals in sorted(tool_durations.items())
        },
        "error_codes": dict(error_codes.most_common()),
        "fail_stages": dict(fail_stages.most_common()),
        "replan_tasks": replan_tasks,
        "cache_hits": cache_hits,
        "guard_events": guard_events,
        # 进程中断（崩溃/被杀）导致结论丢失的任务数，以及它们"死在哪一步"。
        # 这是缺口 2 修复后的关键可观测信号：以前只有 unknown 一个数字，
        # 看不出原因，也看不出规律。
        "interrupted_tasks": interrupted_tasks,
        "interrupted_stages": dict(interrupted_stages.most_common()),
        "llm_usage": {
            "tasks_with_usage": llm_tasks,
            "calls": llm_calls,
            "total_tokens": llm_tokens,
            "duration_s": round(llm_duration, 2),
            "avg_tokens_per_task": round(llm_tokens / llm_tasks, 1) if llm_tasks else None,
            "by_model": llm_by_model,
        },
        "daily": {d: dict(c) for d, c in sorted(daily.items())},
    }
    return metrics


def render_markdown(m: dict[str, Any]) -> str:
    out: list[str] = ["# 天枢一号 · 运行指标看板\n", f"生成时间：{m['generated_at']}\n"]
    total = m["tasks_total"]
    sr = m["success_rate"]
    out.append(f"**任务总数 {total}｜成功率 {'—' if sr is None else f'{sr*100:.1f}%'}**\n")

    out.append("## 终态分布\n")
    out.append("| 终态 | 数量 | 占比 |")
    out.append("|---|---|---|")
    for s, n in m["status_distribution"].items():
        out.append(f"| {s} | {n} | {n/total*100:.1f}% |" if total else f"| {s} | {n} | — |")
    out.append("")

    out.append("## 端到端耗时（秒）\n")
    e = m["e2e_seconds"]
    out.append(f"P50 {e['p50']}｜P90 {e['p90']}｜P95 {e['p95']}｜最大 {e['max']}｜均值 {e['avg']}\n")

    out.append("## 阶段耗时与失败率\n")
    out.append("| 阶段/工具 | 次数 | P50 | P95 | 最大 | 失败 | 失败率 |")
    out.append("|---|---|---|---|---|---|---|")
    for name, s in m["stage_seconds"].items():
        fr = "—" if s["fail_rate"] is None else f"{s['fail_rate']*100:.1f}%"
        out.append(f"| {name} | {s['n']} | {s['p50']} | {s['p95']} | {s['max']} | {s['fail']} | {fr} |")
    out.append("")

    if m["error_codes"]:
        out.append("## 错误码分布\n")
        out.append("| 错误码 | 次数 |")
        out.append("|---|---|")
        for code, n in m["error_codes"].items():
            out.append(f"| {code} | {n} |")
        out.append("")
    if m["fail_stages"]:
        out.append("## 失败发生阶段\n")
        out.append("| 阶段 | 次数 |")
        out.append("|---|---|")
        for st, n in m["fail_stages"].items():
            out.append(f"| {st} | {n} |")
        out.append("")

    out.append("## 新能力使用情况\n")
    out.append(f"- 触发受控重规划的任务：{m['replan_tasks']}")
    out.append(f"- 求解缓存命中：{m['cache_hits']}")
    out.append(f"- 执行门（guard）事件：{m['guard_events']}")
    out.append(f"- 进程中断（崩溃/被杀，结论丢失）：{m.get('interrupted_tasks', 0)}")
    stages = m.get("interrupted_stages") or {}
    if stages:
        out.append("")
        out.append("### 中断任务最后落盘阶段（排查线索）\n")
        out.append("| 最后阶段 | 任务数 |")
        out.append("|---|---|")
        for st, n in stages.items():
            out.append(f"| {st} | {n} |")
    lu = m.get("llm_usage") or {}
    out.append(
        f"- LLM 调用：{lu.get('calls', 0)} 次 / {lu.get('total_tokens', 0)} tokens，"
        f"覆盖 {lu.get('tasks_with_usage', 0)} 个任务，"
        f"平均每任务 {lu.get('avg_tokens_per_task')} tokens，"
        f"累计耗时 {lu.get('duration_s', 0)}s"
    )
    out.append("")

    if m["e2e_by_family"]:
        out.append("## 按算例族耗时（秒）\n")
        out.append("| 算例族 | 任务数 | P50 | P95 |")
        out.append("|---|---|---|---|")
        for fam, s in m["e2e_by_family"].items():
            out.append(f"| {fam} | {s['n']} | {s['p50']} | {s['p95']} |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="只看该日期(YYYY-MM-DD)之后完成的任务")
    # 默认输出到「天枢一号_文档\源文档\」，与归档的报告同一份（详见 paths.py）
    ap.add_argument("--out", default=os.path.join(doc_source_dir(), "天枢一号_指标看板.md"))
    args = ap.parse_args()

    since = None
    if args.since:
        since = dt.datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    m = collect(
        os.path.join(DPF_ROOT, "tasks"),
        os.path.join(DPF_ROOT, "outputs"),
        since=since,
    )
    md = render_markdown(m)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    metrics_dir = os.path.join(DPF_ROOT, "outputs", "_metrics")
    os.makedirs(metrics_dir, exist_ok=True)
    with open(os.path.join(metrics_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    print(md)
    print(f"\n看板已写入：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
