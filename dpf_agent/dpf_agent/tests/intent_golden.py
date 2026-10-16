# -*- coding: utf-8 -*-
"""意图路由回归测试集（golden cases）。

运行：
    python -m dpf_agent.tests.intent_golden            # 语义模式（调 LLM，较慢）
    python -m dpf_agent.tests.intent_golden rule       # 仅规则短路模式（离线，秒回）

每条用例：(问法, 期望动作, 说明, 期望 metric 或 None)
动作取值见 runtime.intent.ACTION_*；metric 取值见 web.history_qa.METRIC_NAMES。

⚠️ metric 由 LLM 判定，仅在语义模式（默认）下校验；rule 模式下跳过。

⚠️ **必须带上 `DPF_LLM_BASE_URL` / `DPF_LLM_API_KEY` / `DPF_LLM_MODEL` 再跑语义模式**。
`AgentConfig()` 的默认值指向本地 Ollama（`localhost:11434`），本机没装 Ollama 时
`available()` 为假，整轮会静默走 `_fallback_by_rules` —— 表现为**大面积红**，
但那是"没接上模型"，不是路由坏了。上一次就差点因此误判（13/51，失败项全是 fallback）。
本项目的模型配置在 `%APPDATA%/tianshu-1/settings.json`（正斜杠写法，避免 `\t` 被当转义）。

新增/修改任何路由规则或提示词后，必须全绿再提交。
"""
from __future__ import annotations

import sys
from typing import Any

from ..config import AgentConfig
from ..runtime import intent as it
from ..web import history_qa as hq

# 新会话上下文：没有任何历史任务、没有附件。
# ⚠️ 必须有这组用例：真实缺陷是"计算ieee33"在**新会话**里被判成"需要确认"，
# 而原有用例全部带历史上下文，所以漏检了。
CTX_FRESH: dict[str, Any] = {
    "last_success": False,
    "last_success_task_id": None,
    "last_task_id": None,
    "last_task_case_ref": None,
    "last_attachments": [],
    "message_count": 1,
}

# 会话上下文：已有一次成功任务、未带新附件
CTX_WITH_HISTORY: dict[str, Any] = {
    "last_success": True,
    "last_success_task_id": "pf-demo",
    "last_task_id": "pf-demo",
    "last_task_case_ref": "case01",
    "last_attachments": [],
    "message_count": 6,
}

# (问法, 期望动作, 说明, 期望 metric)
# metric 这一段是 2026-09-29 重构后新增的判据：
# 旧实现只测「动作对不对」，不测「指标认没认出来」，所以「换种问法就答不上」
# 这类缺陷一路漏到用户那里（本文件里"曾答不出"那条就是历史痕迹）。
CASES: list[tuple[str, str, str, str | tuple[str, ...] | None]] = [
    # ---- 查询已有结果（绝不能重算）----
    ("这个算例有多少台变压器", it.ACTION_HISTORY, "设备计数", hq.METRIC_DEVICE_COUNTS),
    ("这个算例有多少母线", it.ACTION_HISTORY, "母线计数", hq.METRIC_DEVICE_COUNTS),
    ("这个算例有几条线路", it.ACTION_HISTORY, "线路计数", hq.METRIC_DEVICE_COUNTS),
    ("负荷和光伏分别多少个", it.ACTION_HISTORY, "多设备计数", hq.METRIC_DEVICE_COUNTS),
    ("最低电压是多少", it.ACTION_HISTORY, "最低电压", hq.METRIC_MIN_VOLTAGE),
    ("最高电压是多少", it.ACTION_HISTORY, "最高电压", hq.METRIC_MAX_VOLTAGE),
    # ★ 同一个指标换措辞 —— 必须收敛到同一个 metric，否则又会退化成"补关键词"
    ("电压最高的是哪条母线", it.ACTION_HISTORY, "最高电压（换措辞）", hq.METRIC_MAX_VOLTAGE),
    ("全网电压最高多少", it.ACTION_HISTORY, "最高电压（换措辞 2）", hq.METRIC_MAX_VOLTAGE),
    ("哪条母线电压最低", it.ACTION_HISTORY, "最低电压（换措辞）",
     (hq.METRIC_MIN_VOLTAGE, hq.METRIC_VOLTAGE_LOWEST_LIST)),
    ("网损有多少", it.ACTION_HISTORY, "网损", hq.METRIC_LOSSES),
    ("网损占负荷的比例是多少", it.ACTION_HISTORY, "网损（换措辞）", hq.METRIC_LOSSES),
    ("源侧一共供了多少电", it.ACTION_HISTORY, "源侧供电", hq.METRIC_GENERATION),
    ("系统总负荷是多少", it.ACTION_HISTORY, "总负荷", hq.METRIC_LOAD_TOTAL),
    ("功率平衡残差是多少", it.ACTION_HISTORY, "功率平衡", hq.METRIC_POWER_BALANCE),
    ("收敛了吗", it.ACTION_HISTORY, "收敛状态", hq.METRIC_CONVERGENCE),
    ("收敛容差是多少", it.ACTION_HISTORY, "容差（曾答不出）", hq.METRIC_SOLVER_CONFIG),
    ("最大迭代次数设的上限是多少", it.ACTION_HISTORY, "迭代上限", hq.METRIC_SOLVER_CONFIG),
    ("用的什么求解模式", it.ACTION_HISTORY, "求解模式", hq.METRIC_SOLVER_CONFIG),
    ("有哪些母线电压越限", it.ACTION_HISTORY, "越限清单", hq.METRIC_VOLTAGE_VIOLATIONS),
    ("电压不合规的母线有哪些", it.ACTION_HISTORY, "越限清单（换措辞）", hq.METRIC_VOLTAGE_VIOLATIONS),
    ("负载率最高的支路是哪些", it.ACTION_HISTORY, "支路排名", hq.METRIC_BRANCH_LOADING_TOP),
    ("最重载的线路是哪条", it.ACTION_HISTORY, "支路排名（换措辞）", hq.METRIC_BRANCH_LOADING_TOP),
    ("有没有支路超过额定负载", it.ACTION_HISTORY, "过载支路", hq.METRIC_BRANCH_OVERLOAD),
    ("这个算例有哪些电压等级", it.ACTION_HISTORY, "电压等级", hq.METRIC_VOLTAGE_LEVELS),
    ("三相不平衡度是多少", it.ACTION_HISTORY, "不平衡度", hq.METRIC_UNBALANCE),
    ("网络是辐射型还是环网", it.ACTION_HISTORY, "拓扑", hq.METRIC_TOPOLOGY),
    ("算例文件叫什么", it.ACTION_HISTORY, "算例来源", hq.METRIC_CASE_INFO),
    ("这个算例的转换保真度如何", it.ACTION_HISTORY, "保真度", hq.METRIC_FIDELITY),
    ("统计一下这个算例", it.ACTION_HISTORY, "全量统计", hq.METRIC_CASE_STATISTICS),
    ("刚才那次计算的结果怎么样", it.ACTION_HISTORY, "结果概况", hq.METRIC_RESULT_OVERVIEW),
    # ---- 明确重算 / 执行 ----
    ("计算ieee33", it.ACTION_COMPUTE, "显式计算", None),
    ("帮我算一下 case01", it.ACTION_COMPUTE, "显式计算", None),
    ("重新算一遍", it.ACTION_COMPUTE, "明确重跑", None),
    ("再跑一次看看", it.ACTION_COMPUTE, "明确重跑", None),
    ("把容差改成 1e-6 重新计算", it.ACTION_COMPUTE, "改参数后重算", None),
    ("把最大迭代次数调到 50 再算一次", it.ACTION_COMPUTE, "改参数后重算", None),
    # ---- 知识 / 能力 / 闲聊 ----
    ("20kV 三相电压偏差的国标限值是多少", it.ACTION_KNOWLEDGE, "标准查询", None),
    ("什么是 PV 节点", it.ACTION_KNOWLEDGE, "概念查询", None),
    ("你能处理三相不平衡吗", it.ACTION_CAPABILITY, "能力询问", None),
    ("如果算例出错导致不收敛，能定位哪里出错吗", it.ACTION_CAPABILITY, "能力询问", None),
    ("你好", it.ACTION_CASUAL, "问候", None),
    ("谢谢", it.ACTION_CASUAL, "致谢", None),
]


# ---------------------------------------------------------------------------
# 多轮指代消解用例（P2-a）：每条自带上下文（recent_turns = 最近两轮问答摘要）。
#
# 这组用例的特点是**单看这一句根本无法判断在问什么**——"它占总负荷多少""那线路呢"
# 没有主语。此前意图层只拿得到 has_history_success 这类布尔量，只能猜；
# 现在把最近两轮的文字喂进去，才有判断依据。
# 判据：**有上下文时必须判对**（下面这些）；无上下文时判不出属于正常，不作断言。
# ---------------------------------------------------------------------------
def _turns(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": r, "text": t} for r, t in pairs]


# 上一轮在问网损
CTX_TURNS_LOSSES: dict[str, Any] = {
    **CTX_WITH_HISTORY,
    "recent_turns": _turns(
        ("user", "这个算例的网损是多少"),
        ("assistant", "任务 pf-demo 已完成。总网损：168.61 kW，占负荷 4.64%"),
    ),
}

# 上一轮在问设备台数（变压器）
CTX_TURNS_DEVICE: dict[str, Any] = {
    **CTX_WITH_HISTORY,
    "recent_turns": _turns(
        ("user", "这个算例有多少台变压器"),
        ("assistant", "变压器：2 台"),
    ),
}

# 上一轮答的是最低电压及其所在母线
CTX_TURNS_VOLTAGE: dict[str, Any] = {
    **CTX_WITH_HISTORY,
    "recent_turns": _turns(
        ("user", "最低电压是多少"),
        ("assistant", "最低电压 0.9147 pu，出现在母线 18"),
    ),
}

# (问法, 期望动作, 说明, 期望 metric, 自带上下文)
TURN_CASES: list[tuple[str, str, str, str | tuple[str, ...] | None, dict[str, Any]]] = [
    ("它占总负荷多少", it.ACTION_HISTORY, "指代：它=上一轮问的网损",
     hq.METRIC_LOSSES, CTX_TURNS_LOSSES),
    ("那它占负荷的比例呢", it.ACTION_HISTORY, "指代：承接网损，问占负荷比",
     hq.METRIC_LOSSES, CTX_TURNS_LOSSES),
    ("那线路呢", it.ACTION_HISTORY, "指代：承接上一轮的设备计数，换成线路",
     hq.METRIC_DEVICE_COUNTS, CTX_TURNS_DEVICE),
    ("那它正常吗", it.ACTION_HISTORY, "指代：承接上一轮的最低电压（可能落到该母线点位）",
     (hq.METRIC_MIN_VOLTAGE, hq.METRIC_BUS_VOLTAGE), CTX_TURNS_VOLTAGE),
]


# 新会话场景用例：(问法, 期望动作, 说明, 期望 metric)，用 CTX_FRESH 判定
FRESH_CASES: list[tuple[str, str, str, str | tuple[str, ...] | None]] = [
    ("计算ieee33", it.ACTION_COMPUTE, "新会话+内置算例名 → 应当直接计算", None),
    ("帮我算一下 case4", it.ACTION_COMPUTE, "新会话+内置算例名", None),
    ("计算 case12_matpower_hard_v2", it.ACTION_COMPUTE, "新会话+上传算例名", None),
    ("算一下我上传的 case05_structured_hard.json", it.ACTION_COMPUTE, "新会话+文件名", None),
    ("重新计算", it.ACTION_CLARIFY, "没说算例 → 应反问", None),
]


def run(mode: str = "llm") -> int:
    cfg = AgentConfig()
    cfg.intent_mode = mode
    check_metric = mode != "rule"
    passed = 0
    failed: list[str] = []
    all_cases = [(q, e, n, m, CTX_WITH_HISTORY) for q, e, n, m in CASES] + [
        (q, e, n, m, CTX_FRESH) for q, e, n, m in FRESH_CASES
    ]
    # 指代消解只在**语义模式**下承诺：规则兜底拿不到（也不该拿）上下文，
    # "它占总负荷多少"这类句子在降级路径上必然判不出 —— 那是降级的已知边界，
    # 不是回归。把它塞进 rule 模式只会让套件长期假红（假红久了就没人看了）。
    if check_metric:
        all_cases += [(q, e, n, m, c) for q, e, n, m, c in TURN_CASES]
    else:
        print(f"[SKIP] 规则模式跳过 {len(TURN_CASES)} 条指代消解用例（需语义判定）")
    for q, expect, note, expect_metric, ctx in all_cases:
        action, ir = it.decide(q, ctx, cfg)
        got_metric = (ir.slots or {}).get("metric")
        if action != expect:
            failed.append(
                f"[FAIL] {q!r} 期望={expect} 实际={action} "
                f"({ir.intent}, conf={ir.confidence:.2f}, {ir.source}) {note}"
            )
            continue
        if check_metric and expect_metric:
            expected = expect_metric if isinstance(expect_metric, tuple) else (expect_metric,)
            if got_metric not in expected:
                failed.append(
                    f"[FAIL] {q!r} 动作对但 metric 不符：期望 {expected}，实际 {got_metric} {note}"
                )
                continue
        passed += 1
        print(
            f"[PASS] {q!r} -> {action} ({ir.intent}, conf={ir.confidence:.2f}, "
            f"{ir.source}, metric={got_metric})"
        )
    for f in failed:
        print(f)
    total = len(all_cases)
    print(f"\n通过 {passed}/{total}")
    if failed:
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "llm"))
