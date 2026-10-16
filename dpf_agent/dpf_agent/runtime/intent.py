"""意图理解层：把用户的一句话翻译成结构化意图，再由程序决定动作。

与旧规则路由（chat_api 里的关键词表）的本质区别：

1. **语义优先**：由 LLM 判断，不做字面匹配。"变压器"里的"变"再也不会被当成"改参数"。
2. **规则短路**：上传算例、"计算xxx"这类高置信情形直接判定，省一次模型调用。
3. **置信度仲裁**：昂贵动作（重新计算）需要更高置信，不够就反问确认 —— **默认动作从
   "重算"翻转为"查询/确认"**，把误判损失从 40 秒降到 0。
4. **可观测**：返回 reason 与 source，决策可写日志/回归用例，判错可当场修正。

输出契约：
    IntentResult(intent, confidence, slots, reason, source)
    decide(...) -> (action, IntentResult)
    action ∈ compute / history / knowledge / capability / casual / clarify / reject
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..prompts import get_prompt
from typing import Any

INTENT_COMPUTE = "compute"
INTENT_QUERY_HISTORY = "query_history"
INTENT_PARAM_CHANGE = "param_change"
INTENT_KNOWLEDGE = "knowledge"
INTENT_CAPABILITY = "capability"
INTENT_CASUAL = "casual"
INTENT_REJECT = "reject"

VALID_INTENTS = (
    INTENT_COMPUTE,
    INTENT_QUERY_HISTORY,
    INTENT_PARAM_CHANGE,
    INTENT_KNOWLEDGE,
    INTENT_CAPABILITY,
    INTENT_CASUAL,
    INTENT_REJECT,
)

# 意图 → 动作
ACTION_COMPUTE = "compute"
ACTION_HISTORY = "history"
ACTION_KNOWLEDGE = "knowledge"
ACTION_CAPABILITY = "capability"
ACTION_CASUAL = "casual"
ACTION_CLARIFY = "clarify"
ACTION_REJECT = "reject"

# 触发反问的原因码。澄清层据此"按缺什么问什么"，而不是回一段万能文案。
CLARIFY_LOW_CONFIDENCE = "low_confidence"   # 想执行，但置信度不够（可能被误判）
CLARIFY_MISSING_CASE = "missing_case"       # 想计算，但没说是哪个算例
CLARIFY_UNKNOWN_INTENT = "unknown_intent"   # 模型给出了白名单外的意图

# 消息里出现这些片段，说明用户已显式指明算例（内置算例名或上传算例名/文件名后缀），
# 此时"要计算"就有了明确对象，不需要再反问。见 _has_case_context 的说明。
_CASE_TOKEN_HINTS = (
    "ieee", "case", ".dss", ".zip", ".xlsx", ".csv", ".json", ".m",
)


@dataclass
class IntentResult:
    intent: str = INTENT_QUERY_HISTORY
    confidence: float = 0.0
    slots: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    source: str = "rule"  # rule / llm / fallback
    #: 触发反问的**原因码**（`decide()` 填，仅 action=clarify 时有意义）。
    #: 有了它，澄清层才能"按缺什么问什么" —— 否则只能回一段与情形无关的固定文案。
    #: 取值见 CLARIFY_* 常量。
    clarify_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "slots": self.slots,
            "reason": self.reason,
            "source": self.source,
            "clarify_reason": self.clarify_reason,
        }


# 提示词集中在 prompts/ 仓库（版本化），这里只保留 id 引用。
_SYSTEM_PROMPT_ID = "intent_classify"


def _rule_short_circuit(content: str, ctx: dict[str, Any]) -> IntentResult | None:
    """高置信规则短路：省掉一次模型调用。"""
    lower = (content or "").strip().lower()
    if not lower:
        return IntentResult(INTENT_CASUAL, 0.3, reason="空消息", source="rule")

    # 寒暄
    if lower in ("你好", "您好", "hi", "hello", "在吗", "谢谢", "再见", "谢了"):
        return IntentResult(INTENT_CASUAL, 0.9, reason="纯问候", source="rule")

    # 明确重跑 / 执行 / 改参数
    strong_action = ("重算", "再算", "重新计算", "重新跑", "再跑", "跑一遍", "算一遍",
                     "算一下", "帮我算", "帮我跑", "求解", "执行一遍", "复算", "验算")
    if any(w in lower for w in strong_action):
        param_words = ("容差", "迭代", "精度", "tolerance", "最大迭代")
        if any(w in lower for w in param_words) and any(
            w in lower for w in ("改", "设为", "设置为", "调整为", "调到", "换成")
        ):
            return IntentResult(INTENT_PARAM_CHANGE, 0.9, reason="明确改参数后重算", source="rule")
        if any(w in lower for w in ("计算", "算", "跑", "求解", "执行")) and any(
            w in lower for w in ("ieee", "case", ".dss", ".xlsx", ".csv", ".json", ".zip", ".m", "算例")
        ):
            return IntentResult(INTENT_COMPUTE, 0.9, reason="明确要求计算指定算例", source="rule")

    return None


def _fallback_by_rules(content: str, ctx: dict[str, Any]) -> IntentResult:
    """模型不可用时的粗粒度兜底。

    顺序很关键：**先判执行意图，再判查询**。否则"把容差改成 1e-6 重新计算"
    会因为含"容差"被误判成查询。
    """
    lower = (content or "").lower()
    action_words = ("重算", "再算", "重新计算", "重新跑", "再跑", "跑一遍", "算一遍",
                    "算一下", "帮我算", "帮我跑", "求解", "执行", "计算", "复算", "验算")
    param_words = ("容差", "迭代", "精度", "tolerance", "最大迭代")
    change_words = ("改", "设为", "设置为", "调整为", "调到", "换成", "改成")
    if any(w in lower for w in action_words):
        if any(w in lower for w in param_words) and any(w in lower for w in change_words):
            return IntentResult(INTENT_PARAM_CHANGE, 0.5, reason="模型不可用，按改参数重算兜底", source="fallback")
        return IntentResult(INTENT_COMPUTE, 0.5, reason="模型不可用，按计算兜底", source="fallback")
    ask_words = ("多少", "几台", "几个", "几条", "是多少", "哪些", "结果", "状态", "容差", "迭代")
    if any(w in lower for w in ask_words):
        return IntentResult(INTENT_QUERY_HISTORY, 0.45, reason="模型不可用，按查询兜底", source="fallback")
    return IntentResult(INTENT_CASUAL, 0.3, reason="模型不可用，按闲聊兜底", source="fallback")


def _sanitize_slots(slots: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """指代消解的**确定性约束**（代码兜底，不靠提示词自觉）。

    没有上下文（`recent_turns` 为空）时，模型没有可承接的对象，它给出的
    `resolved_query` 只可能是猜的。提示词已明令"不要给"，但实测它照样给 ——
    新会话里问"它占总负荷多少"，置信度只有 0.45，却仍输出了
    "系统网损占总负荷的比例是多少"。

    提示词是**软约束**，这类"给不给、算不算数"的判定必须由代码说了算：
    直接丢掉，猜出来的指代不许流进下游，也不进留痕（免得事后被当成消解成功）。
    """
    if not (ctx.get("recent_turns") or []):
        slots.pop("resolved_query", None)
    return slots


def classify(
    content: str,
    ctx: dict[str, Any] | None = None,
    cfg: Any | None = None,
) -> IntentResult:
    """判定一句话的意图。规则短路 → LLM 语义 → 规则兜底。"""
    ctx = ctx or {}
    if cfg is None:
        from ..config import AgentConfig

        cfg = AgentConfig()

    mode = str(getattr(cfg, "intent_mode", "hybrid") or "hybrid").lower()
    if mode == "off":
        return IntentResult(source="rule", confidence=0.0, reason="意图层已关闭")

    # 1) 规则短路
    if mode in ("hybrid",):
        hit = _rule_short_circuit(content, ctx)
        if hit:
            return hit
    else:  # mode == "llm"
        hit = _rule_short_circuit(content, ctx)
        if hit and hit.confidence >= 0.85 and hit.intent == INTENT_CASUAL:
            return hit

    # 2) LLM 语义判定
    try:
        from .model_client import ModelClient, _trace_note_last

        client = ModelClient(cfg)
        if not client.available():
            return _fallback_by_rules(content, ctx)
        ctx_brief = {
            "has_history_success": bool(ctx.get("last_success")),
            "last_task_id": ctx.get("last_task_id"),
            "last_task_case_ref": ctx.get("last_task_case_ref"),
            "has_attachments": bool(ctx.get("last_attachments")),
            "has_case": bool(ctx.get("last_task_case_ref")),
            "message_count": ctx.get("message_count", 0),
            # 最近两轮的问答摘要（不含本轮）：**指代消解的唯一依据**（P2-a）。
            # 没有它，"它的网损呢"里的"它"就无从落地 —— 模型只能猜，猜错就答非所问。
            "recent_turns": ctx.get("recent_turns") or [],
        }
        user_msg = (
            f"会话上下文：{json.dumps(ctx_brief, ensure_ascii=False)}\n"
            f"用户消息：{content}"
        )
        raw = client.chat(
            [
                {"role": "system", "content": get_prompt(_SYSTEM_PROMPT_ID)},
                {"role": "user", "content": user_msg},
            ],
            timeout=getattr(cfg, "llm_intent_timeout_s", 8.0),
            purpose="intent_classify",
        )
        data = json.loads(raw)
        intent = str(data.get("intent", "")).strip()
        if intent not in VALID_INTENTS:
            _trace_note_last("rejected", f"intent={intent!r} 不在白名单，已退回规则兜底")
            return _fallback_by_rules(content, ctx)
        try:
            conf = float(data.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        conf = max(0.0, min(1.0, conf))
        _raw_slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
        slots = _sanitize_slots(dict(_raw_slots), ctx)
        _dropped_rq = ("resolved_query" in _raw_slots) and ("resolved_query" not in slots)
        # 留痕：记下这次判定到底用了哪条路径、模型给的 metric 是什么。
        # 排查"为什么答非所问"时，这是第一现场（上一次这类问题全靠翻代码猜）。
        _trace_note_last(
            "accepted",
            f"intent={intent} metric={slots.get('metric', '')} conf={conf}"
            # 留痕要能区分"模型说了什么"与"系统采信了什么"：这里记下被硬约束丢掉的猜测。
            # （trace 的 response 字段存的是模型原文，两者对照才能看清兜底有没有生效。）
            + ("｜模型给的 resolved_query 因无上下文已被丢弃" if _dropped_rq else ""),
        )
        return IntentResult(
            intent=intent,
            confidence=conf,
            slots=slots,
            reason=str(data.get("reason", ""))[:200],
            source="llm",
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都退回规则兜底
        try:
            _trace_note_last("rejected", f"判定结果无法使用：{type(exc).__name__}")
        except Exception:  # noqa: BLE001 - 留痕失败不影响兜底
            pass
        fb = _fallback_by_rules(content, ctx)
        fb.reason = f"LLM 判定失败（{type(exc).__name__}），{fb.reason}"
        return fb


def _has_case_context(content: str, ctx: dict[str, Any], ir: IntentResult) -> bool:
    """判断"要计算"这件事是否有算例可算。

    修复的真实缺陷：新会话里说"计算ieee33"曾被判成"需要确认"——
    因为只检查了附件/历史任务，忽略了**内置算例名**与**消息里显式写出的算例引用**
    本身就是充分的算例来源（内置算例 ieee33 无需上传）。
    """
    if ctx.get("last_attachments") or ctx.get("last_task_case_ref") or ctx.get("last_success"):
        return True
    low = (content or "").lower()
    if any(h in low for h in _CASE_TOKEN_HINTS):
        return True
    return bool(str((ir.slots or {}).get("case_ref") or "").strip())


def decide(
    content: str,
    ctx: dict[str, Any] | None = None,
    cfg: Any | None = None,
) -> tuple[str, IntentResult]:
    """返回 (action, IntentResult)。昂贵动作需更高置信，否则反问确认。"""
    ctx = ctx or {}
    if cfg is None:
        from ..config import AgentConfig

        cfg = AgentConfig()

    ir = classify(content, ctx, cfg)
    threshold = float(getattr(cfg, "intent_confirm_threshold", 0.55))

    # 意图 → 动作
    if ir.intent == INTENT_REJECT:
        action = ACTION_REJECT
    elif ir.intent == INTENT_CASUAL:
        action = ACTION_CASUAL
    elif ir.intent == INTENT_CAPABILITY:
        action = ACTION_CAPABILITY
    elif ir.intent == INTENT_KNOWLEDGE:
        action = ACTION_KNOWLEDGE
    elif ir.intent == INTENT_QUERY_HISTORY:
        action = ACTION_HISTORY
    elif ir.intent in (INTENT_COMPUTE, INTENT_PARAM_CHANGE):
        # 昂贵动作：置信不足 → 反问；没有算例也无附件 → 反问
        # 模型不可用走规则兜底时阈值放宽：用户明确说了"计算"，不该只反问。
        eff_threshold = threshold if ir.source == "llm" else 0.4
        if ir.confidence < eff_threshold:
            action = ACTION_CLARIFY
            ir.clarify_reason = CLARIFY_LOW_CONFIDENCE
        elif not _has_case_context(content, ctx, ir):
            action = ACTION_CLARIFY
            ir.clarify_reason = CLARIFY_MISSING_CASE
        else:
            action = ACTION_COMPUTE
    else:
        action = ACTION_CLARIFY
        ir.clarify_reason = CLARIFY_UNKNOWN_INTENT
    return action, ir
