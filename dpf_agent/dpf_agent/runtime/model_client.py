"""ModelClient：本地大模型理解 / 计划 / 解释（文档 10）。

- 默认面向 Qwen 系列量化模型的 OpenAI 兼容本地推理服务（Ollama /v1）。
- 只做结构化解析、最小澄清、受控计划和证据化表达；
  不得生成或修改任何专业数值、solver_status、VerificationResult。
- LLM 输出必须经过 schema 校验与 Rule Guard；连续 3 次失败进入规则 fallback。
- 模型不可用时降级为确定性规则解析（rule_fallback），已确认的结构化任务仍可继续。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Iterable

from pydantic import BaseModel, Field, ValidationError

from ..config import AgentConfig
from ..prompts import get_prompt
from ..schemas.enums import OutputFormat, ResultScope
from ..schemas.task_spec import SolverConfig, UserConstraints, UserRequest

# ---------------------------------------------------------------------------
# LLM 消耗计量（P2）：累计调用次数、token、耗时，供任务产物与看板使用。
# 进程级累计，任务开始前 reset、结束后 snapshot 写盘。
# ---------------------------------------------------------------------------
_USAGE_LOCK = threading.Lock()
_USAGE: dict[str, Any] = {
    "calls": 0,
    "ok_calls": 0,
    "failed_calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "duration_s": 0.0,
    "models": {},
}


def _record_usage(
    *, model: str, duration: float, ok: bool, prompt_tokens: int, completion_tokens: int
) -> None:
    with _USAGE_LOCK:
        _USAGE["calls"] += 1
        _USAGE["ok_calls" if ok else "failed_calls"] += 1
        _USAGE["prompt_tokens"] += prompt_tokens
        _USAGE["completion_tokens"] += completion_tokens
        _USAGE["duration_s"] = round(float(_USAGE["duration_s"]) + duration, 3)
        per_model = _USAGE["models"].setdefault(
            model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        per_model["calls"] += 1
        per_model["prompt_tokens"] += prompt_tokens
        per_model["completion_tokens"] += completion_tokens


def reset_usage() -> None:
    with _USAGE_LOCK:
        _USAGE.update({
            "calls": 0, "ok_calls": 0, "failed_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "duration_s": 0.0, "models": {},
        })


def usage_snapshot() -> dict[str, Any]:
    with _USAGE_LOCK:
        snap = json.loads(json.dumps(_USAGE))
    snap["total_tokens"] = snap["prompt_tokens"] + snap["completion_tokens"]
    snap["cost_note"] = "token 数为模型服务返回的 usage 累计；未做货币换算"
    return snap


# ---------------------------------------------------------------------------
# LLM 调用留痕（P3）：记下"问了什么 → 模型说了什么 → 有没有被采信"。
#
# 与 `_USAGE` 的分工：`_USAGE` 只回答"调了几次、花了多少 token"（数量）；
# 这里是**内容级**审计 —— 没有它，LLM 的每次介入都是黑箱：
# 报告里那句话、聊天里那段归纳，究竟模型原文是什么、是否通过校验、
# 还是被规则模板顶掉了，事后一概查不到（P0 那个"裸数值"就是这么潜进报告的）。
#
# 落盘两处：
#   ① 任务产物 `llm_outputs.json`（本次任务期间的全部调用，随任务归档）；
#   ② 进程级追加日志 `logs/llm_trace.jsonl`（含聊天层调用 —— 它们不属于任何任务，
#      只有这里有痕迹）。可用 `DPF_LLM_TRACE_LOG=0` 关闭 ②。
# ---------------------------------------------------------------------------
_TRACE_LOCK = threading.Lock()
_TRACE: list[dict[str, Any]] = []
_TRACE_LIMIT = 500             # 内存保留条数：够复查一轮会话，又不至于长期运行涨爆
_TRACE_RESPONSE_CHARS = 4000   # 单条响应留档上限（超出截断并标注）
_TRACE_PROMPT_CHARS = 300      # 入参只留头部摘要，完整提示词可由 prompts 版本号复现
_TRACE_SEQ = 0                 # 单调递增序号：环形裁剪后仍能唯一定位一条记录
_TLS = threading.local()       # 记住"本线程最后一次调用"的序号，供回填判定结果


def _sha1_12(text: str) -> str:
    import hashlib

    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def _trace_log_path() -> str:
    from ..config import PROJECT_ROOT

    override = os.environ.get("DPF_LLM_TRACE_PATH")
    if override:
        return os.path.abspath(override)
    return os.path.join(PROJECT_ROOT, "logs", "llm_trace.jsonl")


def _append_trace_jsonl(rec: dict[str, Any]) -> None:
    """进程级追加日志。任何写盘异常都不影响主流程（留痕不该拖垮业务）。

    ⚠️ 整个函数体（含环境变量开关）都在 try 内：写入期间曾因顶层漏 `import os`
    抛 NameError，而当时开关判断恰好在 try 之外，异常直接外泄到业务调用上 ——
    "留痕失败不影响主流程"这句承诺只有当**每一行**都在保护范围内时才成立。
    """
    try:
        if os.environ.get("DPF_LLM_TRACE_LOG", "1") in ("0", "false"):
            return
        path = _trace_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _record_trace(
    *,
    purpose: str,
    model: str,
    json_mode: bool,
    messages: list[dict[str, str]],
    response: str,
    ok: bool,
    duration_s: float,
    error: str = "",
) -> int:
    """写一条留痕，返回其 seq（调用方可用它回填"是否采信"）。"""
    global _TRACE_SEQ
    prompt_text = "\n".join(m.get("content", "") for m in messages)
    resp = response or ""
    truncated = len(resp) > _TRACE_RESPONSE_CHARS
    rec: dict[str, Any] = {
        # jsonl 是**事件流**：调用写一条 event=call，判定再写一条 event=verdict。
        # 一条记录写两行，是因为 verdict 要等调用方校验之后才知道 —— 只写一行的话，
        # 日志里永远停在"模型返回了"，而"到底有没有被采信"恰恰是审计最关心的那项。
        "event": "call",
        "seq": 0,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "purpose": purpose or "chat",
        "model": model,
        "json_mode": bool(json_mode),
        "ok": bool(ok),
        "duration_s": round(duration_s, 3),
        "prompt_digest": _sha1_12(prompt_text),
        "prompt_chars": len(prompt_text),
        "prompt_head": prompt_text[: _TRACE_PROMPT_CHARS].replace("\n", " ⏎ "),
        "response": resp[:_TRACE_RESPONSE_CHARS] + ("…[已截断]" if truncated else ""),
        "response_chars": len(resp),
        "verdict": "returned" if ok else "error",
        "verdict_note": error,
    }
    with _TRACE_LOCK:
        _TRACE_SEQ += 1
        rec["seq"] = _TRACE_SEQ
        _TRACE.append(rec)
        if len(_TRACE) > _TRACE_LIMIT:
            del _TRACE[: len(_TRACE) - _TRACE_LIMIT]
        seq = _TRACE_SEQ
    _TLS.last_trace_seq = seq
    _append_trace_jsonl(rec)
    return seq


def _trace_note_last(verdict: str, note: str = "") -> None:
    """回填"本线程最后一次调用"的采信结论：accepted / rejected / empty / error。

    用线程局部变量而非"最后一条"，是因为 HTTP 服务下多个聊天请求可能并发，
    直接取 `_TRACE[-1]` 会把判定写到别人的记录上。

    取用后立即作废（一次性令牌）：否则一次判定会一直挂在 thread-local 上，
    后续任何没走 `_chat` 的路径（例如测试桩、自造客户端）再调用本函数时，
    就会把结果写到**上一次**调用的记录里 —— 那是错位的证据，比没有证据更糟。
    """
    seq = getattr(_TLS, "last_trace_seq", None)
    if seq is None:
        return
    _TLS.last_trace_seq = None
    with _TRACE_LOCK:
        for rec in reversed(_TRACE):
            if rec.get("seq") == seq:
                rec["verdict"] = verdict
                if note:
                    rec["verdict_note"] = note
                break
    # 判定单独追加一行（事件流），回放时按 seq 与 call 行合并
    _append_trace_jsonl({
        "event": "verdict",
        "seq": seq,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "verdict": verdict,
        "verdict_note": note,
    })


def trace_mark() -> int:
    """取当前序号。任务开始时记下，收尾时用 `trace_since(mark)` 取本任务的调用。"""
    with _TRACE_LOCK:
        return _TRACE_SEQ


def trace_since(mark: int) -> list[dict[str, Any]]:
    with _TRACE_LOCK:
        return [json.loads(json.dumps(r)) for r in _TRACE if r.get("seq", 0) > mark]


def trace_records() -> list[dict[str, Any]]:
    with _TRACE_LOCK:
        return [json.loads(json.dumps(r)) for r in _TRACE]


def reset_trace() -> None:
    global _TRACE_SEQ
    with _TRACE_LOCK:
        _TRACE.clear()
        _TRACE_SEQ = 0
    _TLS.last_trace_seq = None


def trace_summary(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """给产物一份一眼看懂的汇总：各用途调了几次、几次被采信、几次被顶掉。"""
    recs = trace_records() if records is None else list(records)
    by_purpose: dict[str, dict[str, int]] = {}
    for r in recs:
        p = r.get("purpose") or "chat"
        slot = by_purpose.setdefault(p, {"calls": 0, "accepted": 0, "rejected": 0})
        slot["calls"] += 1
        v = r.get("verdict")
        if v == "accepted":
            slot["accepted"] += 1
        elif v in ("rejected", "empty"):
            slot["rejected"] += 1
    return {
        "calls": len(recs),
        "by_purpose": by_purpose,
        "note": (
            "accepted=模型输出通过内容校验并写入回答/报告；"
            "rejected=已调用但输出不合规，已退回确定性模板；"
            "returned=已返回但调用方未做二次判定"
        ),
    }


class ParsedTask(BaseModel):
    """任务解析结果（对应 ModelDecision 的受控子集）。"""

    user_goal: str = "执行配电网稳态潮流计算"
    case_ref: str | None = None
    result_scopes: list[str] = Field(
        default_factory=lambda: ["bus", "branch", "transformer", "system"]
    )
    output_formats: list[str] = Field(default_factory=lambda: ["json", "md"])
    solver_config: dict[str, Any] = Field(default_factory=dict)
    focus_objects: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    parser: str = "rule"  # llm | rule


# 提示词统一来自 dpf_agent/prompts（集中管理 + 版本化，见该模块说明）。
# 解析用系统提示词：PROMPT_TASK_PARSE_ID 对应 prompts.TASK_PARSE


def _explanation_sane(text: str, facts: dict[str, Any]) -> bool:
    """判断 LLM 的解释文本是否可用（挡住"调用成功但内容是垃圾"）。

    实测背景：`response_format={"type":"json_object"}` 与「用 3-6 句中文总结」冲突时，
    模型返回一个**裸数值**（如 `1.3644910000000002`），而 HTTP 调用是成功的 ——
    旧的 `explain` 只看有没有抛异常，于是把它当成有效解释写进报告第 11 节。
    即 **"调用成功" ≠ "内容正确"**。所以这里校验内容本身：
      ① 不能过短；② 不能是纯数字；③ 必须复述 facts 里至少一个数值或标识。
    任一不满足就退回确定性模板（模板里已含全部关键数值，不损失信息）。
    """
    t = (text or "").strip()
    if len(t) < 20:
        return False
    if re.fullmatch(r"[+-]?\d+(\.\d+)?([eE][+-]?\d+)?", t):
        return False
    comparable = False
    for v in facts.values():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            comparable = True
            f = float(v)
            # 覆盖模型可能采用的多种写法（原样 / 四位 / 两位 / 通用 / 一位）
            for cand in (str(v), f"{f:.4f}", f"{f:.2f}", f"{f:g}", f"{f:.1f}"):
                if cand and cand in t:
                    return True
        elif isinstance(v, str) and len(v) >= 3:
            comparable = True
            if v in t:  # 母线名一类的短标识也算"复述了事实"
                return True
    # 有可比对项但一个都没复述 → 不可用；facts 里本来就没有可比对项 → 不因此判死
    return not comparable


#: 小数（如 0.95 / 1e-6 这类工程数值）。诊断说明里最危险的编造就是这种数，
#: 所以只对**小数**做"必须能在事实里找到"的校验；纯整数（"两级重规划"）
#: 在中文里太常见，一律拦会大面积误伤。
_DECIMAL_RE = re.compile(r"\d+\.\d+")


def _diagnosis_sane(text: str, facts: dict[str, Any]) -> bool:
    """失败诊断说明的可用性校验。

    与 `_explanation_sane`（要求"复述了某个事实值"）不同，这里反过来卡：
    **诊断里出现的每一个小数都必须在事实里找得到**。理由是这段文字的用途
    是"解释现象"，不是"报数"——一旦它自己冒出 0.95、1e-6 这类数，就是编的，
    而"模型编数字"正是这个项目最不能接受的一类缺陷。

    另加长度上限：诊断是**说明**不是报告，写成一整篇反而把结论淹没。
    """
    t = (text or "").strip()
    if len(t) < 20 or len(t) > 600:
        return False
    if re.fullmatch(r"[+-]?\d+(\.\d+)?([eE][+-]?\d+)?", t):
        return False
    flat = json.dumps(facts, ensure_ascii=False)
    for m in _DECIMAL_RE.finditer(t):
        raw = m.group(0)
        variants = {raw, raw.rstrip("0").rstrip(".")}
        try:
            f = float(raw)
            variants |= {f"{f:g}", f"{f:.1f}", f"{f:.2f}", f"{f:.3f}", f"{f:.4f}"}
        except ValueError:  # pragma: no cover - 正则已保证可转
            pass
        if not any(v and v in flat for v in variants):
            return False
    return True


#: 文本里"看起来像算例名"的片段。用于**拦截模型编造算例名** ——
#: 提示词是软约束，实测模型照样会写出候选里没有的算例名，
#: 所以"提到的名字必须真实存在"这条判定必须由代码说了算。
_CASE_TOKEN_RE = re.compile(r"(?:ieee\s*[0-9]+|[a-z0-9_]*case[0-9a-z_]+)", re.IGNORECASE)


def _norm_case_token(token: str) -> str:
    """把算例名归一化到可比较的形式：忽略大小写与空格/下划线/连字符。"""
    return re.sub(r"[\s_\-]+", "", (token or "").lower())


def _unknown_case_mentions(text: str, allowed: Iterable[str]) -> list[str]:
    """返回 `text` 里出现的、**不在 allowed 内**的算例名片段（应为空）。

    用在"模型生成面向用户的文案"之后：候选清单是真实存在的算例，
    超出它的名字只可能是模型编的，必须拦下来。
    """
    allowed_norm = {_norm_case_token(a) for a in allowed}
    bad: list[str] = []
    for m in _CASE_TOKEN_RE.finditer(text or ""):
        token = m.group(0)
        if _norm_case_token(token) not in allowed_norm:
            bad.append(token)
    return bad


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


def _urlopen(req, timeout):
    """发请求；**对本机地址绕过系统代理**。

    本机存在 `http_proxy` 时（实测 `http://127.0.0.1:13336`），urllib 的默认 opener
    会把连 `localhost` 的请求**也转发给代理** —— 代理连不上目标就统一回 502，
    把真实的"连接被拒绝 / 超时"盖掉。后果很隐蔽：`available()` 恒为 False、
    LLM 调用静默降级成规则模板，而界面上看不出任何异常（知识问答会一直停在
    "只贴片段"模式，报告也照常生成）。

    这正是 PITFALLS 里那条"探测本机端口一律绕过代理"在**产品代码**里的落地：
    当 `llm_base_url` 指向本机（如默认的 `http://localhost:11434/v1`）时必须绕。
    指向外网时仍走代理（那是用户显式配置的，应当尊重）。
    """
    host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


class ModelClient:
    def __init__(self, config: AgentConfig):
        self.config = config

    # ------------------------------------------------------------ LLM 调用
    def chat(
        self,
        messages: list[dict[str, str]],
        timeout: float | None = None,
        json_mode: bool = True,
        purpose: str = "chat",
    ) -> str:
        """对外暴露的通用对话入口（供意图理解等轻量场景复用）。

        `json_mode=True`（默认）会带 `response_format={"type":"json_object"}` ——
        这是给「把话变成结构化字段」的场景（意图分类、任务解析）用的。
        **要生成自然语言时必须传 `False`**：这两个要求冲突时，模型会退化成一个
        裸数值（实测 `explain` 因此把 `1.364491…` 写进了报告第 11 节）。

        `purpose` 只用于留痕归类（intent_classify / rag_answer / result_explain / …），
        填了之后 `llm_outputs.json` 才能按用途给出"调了几次、几次被采信"。
        """
        return self._chat(messages, timeout=timeout, json_mode=json_mode, purpose=purpose)

    def _chat(
        self,
        messages: list[dict[str, str]],
        timeout: float | None = None,
        json_mode: bool = True,
        purpose: str = "chat",
    ) -> str:
        url = self.config.llm_base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.config.llm_model,
            "messages": messages,
            "temperature": 0.0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.llm_api_key}",
            },
        )
        t0 = time.time()
        try:
            with _urlopen(
                req, timeout=timeout if timeout is not None else self.config.llm_timeout_s
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            dur = time.time() - t0
            # 失败也要计入计量：否则"失败重试"的消耗会被漏统计
            _record_usage(
                model=self.config.llm_model, duration=dur, ok=False,
                prompt_tokens=0, completion_tokens=0,
            )
            # 失败同样留痕：超时/连接拒绝在报告里表现为"用了规则模板"，
            # 只有留痕能区分"模型不可用"与"模型答得不好"。
            _record_trace(
                purpose=purpose, model=self.config.llm_model, json_mode=json_mode,
                messages=messages, response="", ok=False, duration_s=dur,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        dur = time.time() - t0
        _record_usage(
            model=self.config.llm_model, duration=dur, ok=True,
            prompt_tokens=int(((data.get("usage") or {}).get("prompt_tokens")) or 0),
            completion_tokens=int(((data.get("usage") or {}).get("completion_tokens")) or 0),
        )
        content = data["choices"][0]["message"]["content"]
        _record_trace(
            purpose=purpose, model=self.config.llm_model, json_mode=json_mode,
            messages=messages, response=content, ok=True, duration_s=dur,
        )
        return content

    def available(self) -> bool:
        if not self.config.llm_enabled:
            return False
        try:
            url = self.config.llm_base_url.rstrip("/") + "/models"
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.config.llm_api_key}"}
            )
            with _urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------ 任务解析
    def parse_request(self, request: UserRequest) -> ParsedTask:
        """解析用户请求。LLM 优先；失败/不可用则规则降级。最多重试 llm_max_retries 次。"""
        if request.message.strip() and self.available():
            for _ in range(self.config.llm_max_retries):
                try:
                    return self._parse_with_llm(request)
                except Exception as exc:
                    # 留痕：重试本身也要有痕迹，否则"为什么最后走了规则解析"无从追溯。
                    _trace_note_last("rejected", f"任务解析结果不可用：{type(exc).__name__}: {exc}")
                    continue
        return self._parse_with_rules(request)

    def _parse_with_llm(self, request: UserRequest) -> ParsedTask:
        content = self._chat(
            [
                {"role": "system", "content": get_prompt("task_parse")},
                {"role": "user", "content": request.message},
            ],
            purpose="task_parse",
        )
        data = json.loads(content)
        parsed = ParsedTask(**data, parser="llm")
        self._validate_scopes(parsed)  # schema 之外的业务校验
        _trace_note_last(
            "accepted",
            f"scope={parsed.result_scopes} formats={parsed.output_formats} "
            f"solver={parsed.solver_config}",
        )
        # CLI / API 显式约束优先于 LLM 推断（文档 8.7 约束优先级）
        if request.result_scopes:
            parsed.result_scopes = [s.value for s in request.result_scopes]
        if request.output_formats:
            parsed.output_formats = [f.value for f in request.output_formats]
        if request.solver_config:
            if request.solver_config.max_iterations is not None:
                parsed.solver_config["max_iterations"] = request.solver_config.max_iterations
            if request.solver_config.tolerance is not None:
                parsed.solver_config["tolerance"] = request.solver_config.tolerance
        return parsed

    # ------------------------------------------------------- 规则降级解析
    _SCOPE_KEYWORDS = {
        "bus": ("母线", "节点电压", "bus"),
        "branch": ("支路", "线路", "branch", "line"),
        "transformer": ("变压器", "transformer"),
        "system": ("系统", "汇总", "网损", "损耗", "负荷", "供电", "system"),
    }
    _FORMAT_KEYWORDS = {
        "json": ("json",),
        "md": ("markdown", "md"),
        "html": ("html", "网页"),
        "csv": ("csv", "表格"),
    }

    def _parse_with_rules(self, request: UserRequest) -> ParsedTask:
        text = request.message or ""
        parsed = ParsedTask(parser="rule")

        # case_ref
        if request.case_ref:
            parsed.case_ref = request.case_ref
        else:
            m = re.search(r"([\w./\\:-]+\.dss)", text, re.IGNORECASE)
            parsed.case_ref = m.group(1) if m else None

        # result_scopes：默认全量；用户明确限定"只要/只看"时收窄到提及范围
        scopes: list[str] = []
        restrict = bool(re.search(r"(只要|只看|仅需|仅仅)", text))
        for scope, kws in self._SCOPE_KEYWORDS.items():
            if any(kw in text for kw in kws):
                scopes.append(scope)
        if restrict and scopes:
            parsed.result_scopes = scopes
        if request.result_scopes:  # CLI / API 显式指定优先
            parsed.result_scopes = [s.value for s in request.result_scopes]

        # output_formats
        fmts = [f for f, kws in self._FORMAT_KEYWORDS.items() if any(k in text.lower() for k in kws)]
        if fmts:
            parsed.output_formats = sorted(set(fmts) | {"json"})
        if request.output_formats:
            parsed.output_formats = [f.value for f in request.output_formats]

        # solver_config：兼容“把最大迭代设为 30 / 迭代次数改为 50 / 收敛容差为 0.00001 / 容差 1e-5 / tolerance 0.0001”等表达
        # 使用惰性匹配，允许关键字与数值之间有任意非数字字符
        m = re.search(r"(?:最大迭代|iterations|iteration|迭代)\D*?(\d+)", text, re.IGNORECASE)
        if m:
            parsed.solver_config["max_iterations"] = int(m.group(1))
        m = re.search(r"(?:收敛)?\s*容差\D*?(1e-?\d+|0?\.\d+)", text, re.IGNORECASE)
        if m:
            parsed.solver_config["tolerance"] = float(m.group(1))
        m = re.search(r"收敛标准\D*?(1e-?\d+|0?\.\d+)", text, re.IGNORECASE)
        if m:
            parsed.solver_config["tolerance"] = float(m.group(1))
        m = re.search(r"精度\D*?(1e-?\d+|0?\.\d+)", text, re.IGNORECASE)
        if m:
            parsed.solver_config["tolerance"] = float(m.group(1))
        # 数字在“精度”前：如“1e-6的精度”“0.0001 精度”
        m = re.search(r"(1e-?\d+|0?\.\d+)\s*的?\s*精度", text, re.IGNORECASE)
        if m:
            parsed.solver_config["tolerance"] = float(m.group(1))
        m = re.search(r"tolerance\D*?(1e-?\d+|0?\.\d+)", text, re.IGNORECASE)
        if m:
            parsed.solver_config["tolerance"] = float(m.group(1))
        if request.solver_config:
            if request.solver_config.max_iterations is not None:
                parsed.solver_config["max_iterations"] = request.solver_config.max_iterations
            if request.solver_config.tolerance is not None:
                parsed.solver_config["tolerance"] = request.solver_config.tolerance

        # focus_objects（软约束：解释重点）
        for kw in ("最低电压", "最高电压", "网损", "损耗", "负载率", "电压"):
            if kw in text:
                parsed.focus_objects.append(kw)
        parsed.focus_objects = sorted(set(parsed.focus_objects))

        if not parsed.case_ref:
            parsed.ambiguities.append("缺少算例引用（case_ref）")
        return parsed

    def _validate_scopes(self, parsed: ParsedTask) -> None:
        valid_scopes = {s.value for s in ResultScope}
        valid_fmts = {f.value for f in OutputFormat}
        parsed.result_scopes = [s for s in parsed.result_scopes if s in valid_scopes] or [
            "bus",
            "branch",
            "transformer",
            "system",
        ]
        parsed.output_formats = [f for f in parsed.output_formats if f in valid_fmts] or [
            "json",
            "md",
        ]

    # ------------------------------------------------------------- 解释生成
    def explain(
        self, facts: dict[str, Any], fallback_text: str
    ) -> tuple[str, str]:
        """证据化表达：只能复述 facts 中存在的数值。

        返回 (文本, 来源)。LLM 不可用、超时、或**输出不可用**时返回 fallback_text
        （由确定性模板生成）。

        ⚠️ 两个必须同时满足的条件（都是实测踩出来的）：
        ① 传 `json_mode=False` —— 否则 `response_format=json_object` 会与「用中文总结」
           的提示词冲突，模型退化成一个裸数值；
        ② 用 `_explanation_sane` 校验内容 —— 那种裸数值**调用状态是成功的**，
           只看"有没有抛异常"会把它当成有效解释收下。
        """
        if not self.available():
            return fallback_text, "rule"
        prompt = get_prompt("result_explain").replace(
            "{facts}", json.dumps(facts, ensure_ascii=False)
        )
        try:
            # 结果解释只是"换个说法"，不值得为它干等。超时就走确定性模板
            # （fallback 里已含全部关键数值，答案质量等价）。
            text = self._chat(
                [{"role": "user", "content": prompt}],
                timeout=getattr(self.config, "llm_explain_timeout_s", 12),
                json_mode=False,
                purpose="result_explain",
            )
        except Exception:
            return fallback_text, "rule"
        if not _explanation_sane(text, facts):
            # 留痕区分两种"退回模板"：调用失败（上面已记 error）与内容不合规。
            # P0 的裸数值事故正属于后者 —— 调用状态是成功的。
            _trace_note_last("rejected", "输出未通过 _explanation_sane（过短/纯数字/未复述 facts），已退回确定性模板")
            return fallback_text, "rule"
        _trace_note_last("accepted", "通过内容校验，写为报告解释")
        return text.strip(), "llm"

    # --------------------------------------------------------- 恢复动作选择
    def choose_replan(
        self,
        candidates: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> tuple[str | None, str]:
        """未收敛时，让模型在**已注册的候选动作**里选一个。返回 (动作 id, 依据)。

        与 `explain` 同一套取舍：LLM 不可用 / 超时 / **返回不在候选内的值**
        一律返回 `(None, 原因)`，由调用方退回固定顺序 —— 这段代码跑在**失败路径**上，
        绝不能因为模型抽风而丢掉原本能自愈的机会。

        白名单由**调用方**给出（`powerflow_skill._REPLAN_ACTIONS`），模型只负责选、
        不负责发明：这就是"决策自由、执行受控"的边界。返回值额外允许 `"abort"`
        （模型判定重规划无益），由提示词明文约定。

        ⚠️ 用 `json_mode=True`：这里要的就是结构化字段，与 `explain` 的场景相反
        （那边要自然语言，传 True 会让模型退化成一个裸数值）。
        """
        if not self.available():
            return None, "模型不可用"
        prompt = (
            get_prompt("replan_choose")
            .replace("{candidates}", json.dumps(candidates, ensure_ascii=False, indent=1))
            .replace("{context}", json.dumps(context, ensure_ascii=False, indent=1))
        )
        try:
            text = self._chat(
                [{"role": "user", "content": prompt}],
                timeout=getattr(self.config, "llm_intent_timeout_s", 8),
                json_mode=True,
                purpose="replan_choose",
            )
        except Exception as exc:  # noqa: BLE001 - 决策失败不得影响恢复流程
            return None, f"调用失败：{type(exc).__name__}"
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None, "输出不是合法 JSON"
        if not isinstance(data, dict):
            return None, "输出不是 JSON 对象"
        picked = data.get("strategy")
        reason = str(data.get("reason") or "").strip()
        if picked == "abort":
            _trace_note_last("accepted", "模型判定不宜重规划，选择中止")
            return "abort", reason
        allowed = {str(c.get("id")) for c in candidates}
        if not isinstance(picked, str) or picked not in allowed:
            _trace_note_last(
                "rejected",
                "恢复动作选择不在候选白名单内（%r），已退回固定顺序" % (picked,),
            )
            return None, "选择不在白名单内"
        _trace_note_last("accepted", "模型选定恢复动作：%s" % picked)
        return picked, reason

    # --------------------------------------------------------- 算例名消歧
    def disambiguate_case(
        self,
        message: str,
        candidates: list[dict[str, Any]],
    ) -> tuple[str | None, float, str, str]:
        """用户点名的算例本机不存在时，让模型在**真实可用清单**里找最近的那个。

        返回 `(候选 id | None, 置信度, 确认问句, 依据)`。

        与 `choose_replan` 同构（白名单内选），但用途刻意收得更窄：返回值**只用于
        生成一句确认问句**，绝不用于自动改算例。项目既有硬原则是"用户点名了算例
        就必须算那个，找不到要显式报错，不许悄悄换别的算例来算"——因为那等于
        拿另一个算例的结果冒充用户要的结果。模型在这里的价值是"把话问准"，
        不是"替用户改主意"。

        白名单由调用方给出（chat_api 汇总的内置算例 + 本会话已上传算例）；
        `more` 里出现的算例名超出白名单时，问句整段丢弃（宁可用确定性文案）。
        """
        if not candidates:
            return None, 0.0, "", "没有可用的算例候选"
        if not self.available():
            return None, 0.0, "", "模型不可用"
        allowed = [str(c.get("id")) for c in candidates]
        prompt = (
            get_prompt("case_disambiguate")
            .replace("{candidates}", json.dumps(candidates, ensure_ascii=False, indent=1))
            .replace("{message}", message or "")
        )
        try:
            text = self._chat(
                [{"role": "user", "content": prompt}],
                timeout=getattr(self.config, "llm_intent_timeout_s", 8),
                json_mode=True,
                purpose="case_disambiguate",
            )
        except Exception as exc:  # noqa: BLE001 - 消歧失败不得影响既有报错路径
            return None, 0.0, "", f"调用失败：{type(exc).__name__}"
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None, 0.0, "", "输出不是合法 JSON"
        if not isinstance(data, dict):
            return None, 0.0, "", "输出不是 JSON 对象"

        question = str(data.get("question") or "").strip()
        bad = _unknown_case_mentions(question, allowed)
        if bad:
            # 硬约束：面向用户的文案里出现候选外的算例名 → 一律丢弃问句。
            # 否则用户会看到一个本机根本不存在的算例名，比不追问更糟。
            _trace_note_last(
                "rejected", f"追问里出现候选外的算例名 {bad!r}，问句已丢弃"
            )
            question = ""

        try:
            conf = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        picked = data.get("match")
        if not isinstance(picked, str) or picked not in allowed:
            _trace_note_last("rejected", f"消歧结果 {picked!r} 不在候选白名单内")
            return None, 0.0, question, "匹配不在白名单内"
        _trace_note_last("accepted", f"算例消歧命中 {picked}（conf={conf}）")
        return picked, conf, question, "模型在真实可用清单里选定"

    # ----------------------------------------------------- 失败/降级诊断
    def diagnose_failure(
        self, facts: dict[str, Any], fallback_text: str
    ) -> tuple[str, str]:
        """把"为什么没收敛 / 系统做了什么"讲成人话。返回 `(文本, 来源)`。

        与 `explain` 同一套取舍：`json_mode=False`（要自然语言）、内容校验
        （挡"调用成功但内容是垃圾"）、不可用/超时/不合规一律退回确定性模板。

        与 `explain` 的关键区别：这段文字**不进证据链、不参与发布门**，
        只是"说明"。所以它既不能改数值，也不能被当成结论 —— 这一点由
        提示词与调用点双重约束（调用方只把它挂到说明字段上）。
        """
        if not self.available():
            return fallback_text, "rule"
        prompt = get_prompt("failure_diagnose").replace(
            "{facts}", json.dumps(facts, ensure_ascii=False)
        )
        try:
            text = self._chat(
                [{"role": "user", "content": prompt}],
                timeout=getattr(self.config, "llm_explain_timeout_s", 12),
                json_mode=False,
                purpose="failure_diagnose",
            )
        except Exception:  # noqa: BLE001
            return fallback_text, "rule"
        if not _diagnosis_sane(text, facts):
            _trace_note_last(
                "rejected", "输出未通过 _diagnosis_sane（过短/过长/纯数字/未复述事实），"
                "已退回确定性模板"
            )
            return fallback_text, "rule"
        _trace_note_last("accepted", "通过内容校验，写为失败/降级说明")
        return text.strip(), "llm"

    # ------------------------------------------------------------ 澄清反问
    def ask_clarify(
        self,
        message: str,
        reason: str,
        slots: dict[str, Any] | None = None,
        candidates: list[dict[str, Any]] | None = None,
    ) -> tuple[str | None, str]:
        """意图不明确时，按"缺哪一项"生成一句针对性追问。返回 `(文本 | None, 依据)`。

        纯粹是"把澄清问得更贴题"：不执行、不报数、不选路。返回值只用于回给用户，
        不参与任何分支判断 —— 所以它失败时返回 `None`，由调用方退回既有固定文案。

        与 `disambiguate_case` 共用同一条硬约束：**问句里不得出现候选外的算例名**。
        """
        if not self.available():
            return None, "模型不可用"
        cands = candidates or []
        allowed = [str(c.get("id")) for c in cands]
        prompt = (
            get_prompt("clarify_ask")
            .replace("{reason}", reason or "未指明")
            .replace("{slots}", json.dumps(slots or {}, ensure_ascii=False))
            .replace("{candidates}", json.dumps(cands, ensure_ascii=False, indent=1) if cands else "（本次与算例无关）")
            .replace("{message}", message or "")
        )
        try:
            text = self._chat(
                [{"role": "user", "content": prompt}],
                timeout=getattr(self.config, "llm_intent_timeout_s", 8),
                json_mode=False,
                purpose="clarify_ask",
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"调用失败：{type(exc).__name__}"
        t = (text or "").strip()
        if len(t) < 8 or len(t) > 400:
            _trace_note_last("rejected", f"追问长度不合适（{len(t)} 字），已退回固定文案")
            return None, "追问长度不合适"
        bad = _unknown_case_mentions(t, allowed)
        if bad:
            _trace_note_last("rejected", f"追问里出现候选外的算例名 {bad!r}，已退回固定文案")
            return None, "追问含候选外算例名"
        _trace_note_last("accepted", "生成针对性澄清问句")
        return t, "llm"


def build_user_constraints(parsed: ParsedTask, request: UserRequest) -> UserConstraints:
    """构造硬/软约束（文档 8.7）。"""
    hard: dict[str, Any] = {
        "result_scopes": list(parsed.result_scopes),
        "output_formats": list(parsed.output_formats),
    }
    for k, v in parsed.solver_config.items():
        if v is not None:
            hard[k] = v
    soft: dict[str, Any] = {}
    if parsed.focus_objects:
        soft["focus_objects"] = list(parsed.focus_objects)
    return UserConstraints(hard=hard, soft=soft)
