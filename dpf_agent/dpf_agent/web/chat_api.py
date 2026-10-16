"""聊天会话相关 REST API。"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from ..application.service import ApplicationService, TaskBusyError, current_task_lock
from ..config import AgentConfig
from ..domain import diagnosis as _diagnosis
from ..prompts import get_prompt
from ..rag.pipeline import get_pipeline
from ..schemas.enums import OutputFormat, ResultScope, TaskStatus
from ..schemas.task_spec import SolverConfig, UserRequest
from . import case_converters
from . import chat_store as store
from . import batch_scheduler, batch_store
from . import conversion_cache
from . import history_qa  # noqa: E402
from . import xlsx_to_dss

class _SessionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def scoped_handler(request):
            try:
                return await handler(request)
            except store.SessionMissing as exc:
                raise HTTPException(status_code=404, detail="会话已被删除") from exc
        return scoped_handler


router = APIRouter(route_class=_SessionRoute)

# 回答分段标记：主答在前，补充说明放该标记之后（前端渲染为可折叠块）。
# 用于"问什么答什么"——用户没问到的维度不占据主答版面，但仍可展开查看。
SUPP_MARK = "[[SUPP]]"

_RAG_MAX_HITS_IN_CHAT = 3  # 聊天回答里最多嵌入 3 条参考，避免冲淡主答
_RAG_MAX_SNIPPET_CHARS = 160


# 知识型问题的特征词：命中这些说明用户在问"知识/标准/原理"，而不是要跑计算
_KNOWLEDGE_HINTS = (
    "国标", "国家标准", "标准", "限值", "规范", "规程", "导则",
    "gb/t", "gbt", "gb ", "dl/t", "ieee 标准",
    "定义", "是什么", "什么是", "原理", "为什么", "区别", "对比", "优劣",
)
# 明确的"计算命令"特征：算例名 + 动作动词 → 一律走计算，不能被 RAG 抢答
_CASE_NAME_HINTS = ("ieee", "case", ".dss", ".xlsx", ".csv", ".json", ".zip", ".m")
_ACTION_HINTS = ("计算", "算一下", "算一算", "跑一下", "跑一遍", "求解", "执行一遍", "帮我算")


def _looks_like_compute_command(content: str) -> bool:
    """是否是一条明确的"去算"指令（算例名 + 动作）。"""
    c = (content or "").lower().strip()
    if not c:
        return False
    has_case = any(w in c for w in _CASE_NAME_HINTS)
    has_action = any(w in c for w in _ACTION_HINTS)
    return has_case and has_action


def _is_knowledge_query(content: str) -> bool:
    """是否应优先走 RAG 知识库回答。

    判据：含知识/标准类特征词，且不是一条明确的计算命令。
    这样「20kV 三相电压偏差国标限值」会走 RAG，
    而「计算 IEEE33」「ieee33 网损多少」仍走计算/历史结果，不会被 RAG 抢答。
    """
    c = (content or "").strip()
    if not c:
        return False
    if _looks_like_compute_command(c):
        return False
    low = c.lower()
    return any(w in low for w in _KNOWLEDGE_HINTS)


def _rag_answer_sane(text: str, hits: list[dict[str, Any]]) -> str:
    """校验知识库回答是否可用；不可用返回空串（调用方退回"只贴片段"）。

    判据针对**编造**这个最主要的失败模式：
    · 过短 / 纯数字 → 不可用（与结果解释同一类退化）；
    · **必须引用至少一个片段的出处名** —— 这是最低成本、最有效的
      "有没有脱离片段乱说"检测：真按片段作答的回答必然会带来源。
    """
    t = (text or "").strip()
    if len(t) < 20 or re.fullmatch(r"[+-]?\d+(\.\d+)?", t):
        return ""
    srcs = [str(h.get("source") or "") for h in hits]
    srcs = [s for s in srcs if len(s) >= 3]
    if srcs and not any(s in t for s in srcs):
        return ""
    return t


def _rag_answer(query: str, hits: list[dict[str, Any]]) -> str:
    """让 LLM **基于检索片段**作答（P1）。

    边界（与项目红线一致）：只喂片段、只要求归纳、必须标出处、资料不足必须明说；
    模型不可用或输出不合规时返回空串 —— 调用方退回"直接贴片段"的旧行为，功能不退化。
    """
    note = None  # 留痕回填函数；import 放进 try 里，这里先占位以免 UnboundLocalError
    try:
        cfg = AgentConfig()
        if not cfg.llm_enabled:
            return ""
        from ..runtime.model_client import ModelClient, _trace_note_last

        note = _trace_note_last
        client = ModelClient(cfg)
        if not client.available():
            return ""
    except Exception:  # noqa: BLE001 - 取配置/探测失败一律按"不可用"
        return ""

    blocks: list[str] = []
    for i, h in enumerate(hits, 1):
        sn = (h.get("snippet") or "").strip()
        if len(sn) > _RAG_MAX_SNIPPET_CHARS:
            sn = sn[:_RAG_MAX_SNIPPET_CHARS].rstrip() + "…"
        blocks.append(f"[片段{i}] 出处：{h.get('source', '?')}\n{sn}")
    try:
        prompt = (
            get_prompt("rag_answer")
            .replace("{query}", query or "")
            .replace("{snippets}", "\n\n".join(blocks))
        )
        text = client.chat(
            [{"role": "user", "content": prompt}],
            timeout=getattr(cfg, "llm_rag_timeout_s", 20.0),
            json_mode=False,  # 要自然语言，不能带 response_format=json_object
            purpose="rag_answer",
        )
    except Exception:  # noqa: BLE001
        return ""
    sane = _rag_answer_sane(text, hits)
    if note:
        if sane:
            note("accepted", "基于检索片段作答，且标注了出处；片段原文仍照旧附上供核验")
        else:
            note("rejected", "输出未通过 _rag_answer_sane（过短/纯数字/未标注出处），已退回直贴片段")
    return sane


def _build_rag_block(
    query: str,
    *,
    max_hits: int = _RAG_MAX_HITS_IN_CHAT,
    answer_with_llm: bool = False,
) -> str:
    """对用户问题做一次 RAG 检索；命中时返回 markdown 形式的「📚 知识库参考」段落。

    设计：仅当 RAG 启用、且至少 1 条命中超过 min_score 时输出，
    避免在无关查询（如「你好」）后挂上无关参考，干扰阅读。

    `answer_with_llm=True` 时（**知识型问题的主回答**）会在检索之后让模型基于片段
    做一次受约束归纳，并把片段原文照旧附上供核验；模型不可用或回答不合规时自动退回
    "只贴片段"。计算 / 闲聊末尾的"顺便提一下"保持 False —— 不为补充信息多等一次模型。
    """
    cfg = AgentConfig()
    if not cfg.rag_enabled or not (query or "").strip():
        return ""
    try:
        # mode="chat"：短超时 + 关闭 LLM 查询改写，失败自动降级本地 BM25
        result = get_pipeline().search(query, top_k=max_hits, mode="chat")
    except Exception as exc:  # 检索失败不能让整条聊天消息崩
        return f"\n\n<sub>📚 知识库检索暂不可用：{exc}</sub>"
    hits = result.get("hits") or []
    if not hits:
        return ""
    backend = result.get("backend") or ""
    label = "Power-RAG 知识库" if backend == "powerrag" else "本地知识库"

    # 本地 BM25 是原始分（量纲不固定），按"相对最高分"归一化后再判置信度；
    # Power-RAG score 本身就是 0~1，直接判。
    raw_scores = [float(h.get("adjusted_score", h.get("score", 0.0)) or 0.0) for h in hits]
    top = max(raw_scores) if raw_scores else 0.0

    def _confidence(h: dict) -> str:
        s = float(h.get("adjusted_score", h.get("score", 0.0)) or 0.0)
        if backend == "powerrag":
            return "高" if s >= 0.8 else ("中" if s >= 0.6 else "低")
        if top <= 0:
            return "低"
        ratio = s / top
        return "高" if ratio >= 0.7 else ("中" if ratio >= 0.4 else "低")

    def _warnings(h: dict) -> str:
        """把"这条可能不可靠"的原因显式写出来，而不是让用户默认它可信。"""
        tags: list[str] = []
        scope = str(h.get("scope") or "")
        status = str(h.get("status") or "")
        if scope == "case":
            tags.append("某算例结果，非通用结论")
        if status == "draft":
            tags.append("草稿，未经审校")
        elif status == "unknown" and backend != "powerrag":
            tags.append("来源未标注可信度")
        return "、".join(tags)

    def _snippet_lines() -> list[str]:
        out: list[str] = []
        for h in hits:
            snippet = (h.get("snippet") or "").strip()
            if len(snippet) > _RAG_MAX_SNIPPET_CHARS:
                snippet = snippet[:_RAG_MAX_SNIPPET_CHARS].rstrip() + "…"
            score = h.get("score", 0.0)
            source = h.get("source", "")
            warn = _warnings(h)
            tail = f"〔{warn}〕" if warn else ""
            out.append(
                f"- `{source}`（相关度 {score:.2f}，置信 {_confidence(h)}）{tail}：{snippet}"
            )
        return out

    # P1：知识型主回答先给"基于片段的归纳"，再附原文供核验。
    if answer_with_llm:
        answer = _rag_answer(query, hits)
        if answer:
            return "\n".join(
                [
                    f"\n\n📚 **{label}**"
                    "（以下为模型基于检索片段的归纳，仅供参考，请以原始文档为准）",
                    answer,
                    "",
                    "**检索到的原文片段（可核验）**",
                ] + _snippet_lines()
            )
        # 模型不可用 / 回答不合规 → 落到下面的"只贴片段"，与本改动前行为一致
    return "\n".join(
        [f"\n\n📚 **{label}参考**（仅供参考，请以原始文档为准）"] + _snippet_lines()
    )


def _busy_owner_hint() -> str:
    """忙时给出"谁在跑、跑了多久"，让用户知道该等还是该停。"""
    info = current_task_lock()
    owner = info.get("task_id") or "未知任务"
    since = info.get("since")
    if isinstance(since, (int, float)):
        try:
            import time as _t

            return f"{owner}，已运行 {int(_t.time() - since)} 秒"
        except Exception:  # noqa: BLE001
            pass
    return str(owner)


def _get_service() -> ApplicationService:
    return ApplicationService(AgentConfig())


def _extract_summary(artifacts: dict[str, str], question: str = "",
                     case_ref: str | None = None) -> str:
    """从 powerflow_result.json 提取关键指标摘要。

    `question` 是用户本轮原话。传入后会**按问什么答什么**补一段定向数据
    （见 `_focused_answers`）—— 真机实测的缺陷：用户问"delta 内部有没有环流"
    "零序有多大"，回复只有固定的收敛/电压/网损三项，等于没答；
    而这些数在 `powerflow_result.json` 里**早就算好了**，只是没被取用。

    `case_ref` 是算例入口（`Master.dss`）路径。摘要层有几个取数函数需要
    **算例源目录**才能作答（`_redirect_manifest` 要数 ZIP 里的 `.dss`），
    而 `artifacts` 里只有 `*.json`，给不了目录 —— 必须显式传进来。
    """
    path = artifacts.get("powerflow_result.json") or artifacts.get("powerflow_result")
    if not path or not os.path.isfile(path):
        return "报告已生成，可点击下方链接查看。"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ss = data.get("system_summary", {})
        ls = data.get("loss_summary", {})
        solver = data.get("solver_status", {})
        lines = []
        if solver.get("converged") is not None:
            lines.append(history_qa.convergence_summary(solver))
        if ss.get("min_voltage_pu") is not None:
            lines.append(f"- 最低电压：{ss['min_voltage_pu']:.4f} pu（母线 {ss.get('min_voltage_bus', '?')}）")
        if ss.get("max_voltage_pu") is not None:
            lines.append(f"- 最高电压：{ss['max_voltage_pu']:.4f} pu（母线 {ss.get('max_voltage_bus', '?')}）")
        if ls.get("total_loss_kw") is not None:
            pct = ls.get("loss_pct_of_load")
            pct_str = f"，占负荷 {pct:.2f}%" if pct is not None else ""
            lines.append(f"- 总网损：{ls['total_loss_kw']:.2f} kW{pct_str}")

        # ---- 分相算例：把不平衡度与「真实最低相电压」一并给出 ----
        # 反例（真机实测）：用户明确问"各相电压和不平衡度"，摘要只报了平衡等值
        # 口径的三个数 —— 等于没答；而报告第 8 节早就把 ε、分相电压、参考限值
        # 全算好了，只是没被摘要取用。
        ub = data.get("unbalance_summary") or {}
        # `assessed` 只作"这份口径确实有数据"的保险，**不是**分相判据。
        assessed = ub.get("buses_assessed") or 0
        # 判据是**"有没有单相/两相母线"**，不是 buses_assessed ——
        # 平衡等值算例（如 ieee33）每条母线都是三相，同样会给出 33 条"不平衡度"，
        # 但那只是数值噪声（实测 1.1e-8 %）；真正按相别建模的算例才会出现
        # 相数 < 3 的母线（实测 case37：3 条三相 / 3 条单相 / 1 条两相）。
        # 判据写松了，就会在平衡算例的回复里谈"不平衡度" —— 那是误导。
        _partial_phase = any(
            len(_b.get("phase_labels") or []) < 3
            for _b in (data.get("bus_results") or [])
        )
        if assessed and _partial_phase:
            if ss.get("min_phase_voltage_pu") is not None:
                lab = ss.get("min_phase_label")
                lines.append(
                    "- 真实最低相电压：%.4f pu（母线 %s%s）—— 分相口径（上面的"
                    "「最低电压」为平衡等值口径，取每母线三相最大值，偏乐观）"
                    % (ss["min_phase_voltage_pu"], ss.get("min_phase_voltage_bus", "?"),
                       f" 的 {lab} 相" if lab else "")
                )
            worst = ub.get("worst_unbalance_pct")
            if worst is not None:
                lim = ub.get("limit_pct")
                over = ub.get("buses_over_limit_count") or 0
                note = ""
                if lim:
                    note = (f"，**超出参考限值 {lim:g}%**" if over
                            else f"（参考限值 {lim:g}%，未超；仅标注，不作合格判定）")
                lines.append(f"- 不平衡度 ε：最严重母线 {ub.get('worst_bus', '?')}，"
                             f"{worst:.3f}%{note}")
            not_assessed = ub.get("buses_not_assessed") or 0
            lines.append(
                f"- 三相建模：按输入相别建模，{assessed} 条母线可算不平衡度"
                + (f"，{not_assessed} 条相量不足（不平衡度无定义，计「不适用」）"
                   if not_assessed else "")
            )
        if question:
            lines.extend(_focused_answers(data, question, artifacts=artifacts,
                                         case_ref=case_ref))
        return "\n".join(lines) or "报告已生成，可点击下方链接查看。"
    except Exception:
        return "报告已生成，可点击下方链接查看。"


# 用户提问里的主题词 → 该主题对应的取数函数。
# 键是**归一化后**的关键词集合（见 `_focused_answers` 里的匹配方式）。
# ⚠️ 每个取数函数只能读 `powerflow_result.json` 里**已经算好的字段** ——
# 摘要层不做任何新计算，也绝不推测。它做的是"把算好的数按问的维度取出来"。
_FOCUS_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("零序", "zero_seq", "z0"), "zero_seq"),
    (("负序", "negative_seq", "u2", "负序分量"), "negative_seq"),
    (("中性", "零线", "neutral", "a0"), "neutral"),
    (("delta", "三角形", "△", "环流"), "delta"),
    (("环流",), "delta"),
    (("单相变压器", "单相变"), "single_phase_tx"),
    (("变压器",), "transformer"),
    (("接地", "接地方式", "r0", "x0"), "grounding"),
    (("平衡式", "总负荷", "净负荷", "网损", "口径", "残差", "功率平衡", "平衡残差"),
     "balance"),
    (("相电压", "分相电压", "各相", "a 相", "b 相", "c 相", "a相", "b相", "c相"), "phase_v"),
    (("相角", "相位"), "phase_a"),
    (("最严重", "最差", "不平衡最"), "worst"),
    (("环网", "成环", "环形", "闭环", "mesh", "环路", "网格"), "loop"),
    (("孤岛", "分裂", "连通", "连通分量", "解列"), "island"),
    (("过载", "负载率", "载流", "越限", "重载"), "overload"),
    # ⚠️ 关键词必须覆盖**用户的自然说法**，不是只认文件名 ——
    # 第一版只写 `master.dss`，于是「哪些 .dss 被 Master 引用了？」
    # （真人就是这么问的）**一个词都命中不了**，整段依赖分析静默不输出，
    # 症状与"没实现"一模一样。`.dss` 与裸 `master` 才是真人会打的字。
    (("redirect", "master.dss", "master", ".dss", "依赖", "多文件", "解压",
      "场景文件", "scenarios", "未被引用", "哪些文件"), "redirect"),
)


def _redirect_manifest(data: dict[str, Any], artifacts: dict[str, str] | None = None,
                       case_ref: str | None = None) -> list[str]:
    """多文件算例的 Redirect 依赖：哪些被执行、哪些**没有**被引用。

    ## 为什么需要
    真机实测（case32 ZIP 算例）：题目问"ZIP 里有哪些 .dss、哪些是 Master
    真正 Redirect 引用的、哪些不是"，而回复只字未提 —— 而这正是这道题的
    全部考点。ZIP 入口（`conversion_cache._reconvert_from_zip`）解压后
    直接返回 `Master.dss`，**从不解析依赖链**。

    ## 目录从哪来（这里踩过两次坑）
    ⚠️ **不能用 `tasks/pf-xxx/input/` 当"算例目录"**：
    `load_case_bundle` 只把 **Redirect 依赖闭包**复制进 `input/`，
    未被引用的 `scenarios/*.dss` **根本不会出现在那里**。
    在那里数文件得到的是"8 个、全部被引用"—— 恰好把本题的答案 negate 掉，
    比答不出来更坏（它看起来像个正确答案）。
    ⚠️ **也不能用 `powerflow_result.json` 的同级目录**：
    产物写在 workspace 根（`save_json` → `workspace/<name>.json`），
    而 `Master.dss` 在 `workspace/input/`，两者**不同级**。

    唯一正确来源是 **`case_ref`**：它是 `_extract_case_ref` 从**上传附件**
    解析出来的，指向 `chat_data/uploads/<sess>/<名>/Master.dss`，
    即 ZIP 解压后的**源目录**，10 个 `.dss` 全在。
    `case_ref` 不可用时才退回 `artifacts` 推断，且推断失败就**明说取不到**。
    """
    import glob
    import os as _os
    import re as _re

    arts = artifacts or {}
    master: str | None = None
    if case_ref and _os.path.isfile(case_ref):
        master = case_ref
    if not master:
        master = arts.get("Master.dss") or arts.get("master.dss")
    if not master or not _os.path.isfile(master):
        # 退化路径：从结果 JSON 所在目录找。产物在 workspace 根、Master 在
        # workspace/input/，所以**两级都要看**（原来只看同级，真机必然取不到）。
        for key in ("powerflow_result.json", "powerflow_result",
                    "raw_opendss_result.json", "validation.json"):
            p = arts.get(key)
            if not p or not _os.path.isfile(p):
                continue
            base = _os.path.dirname(_os.path.abspath(p))
            for cand in (_os.path.join(base, "Master.dss"),
                         _os.path.join(base, "input", "Master.dss")):
                if _os.path.isfile(cand):
                    master = cand
                    break
            if master:
                break
    if not master or not _os.path.isfile(master):
        return ["- 多文件依赖：本次结果里没有记录算例目录（`powerflow_result.json` "
                "等产物旁找不到 `Master.dss`），无法核对 Redirect 依赖链"
                "——不给猜测。若要回答这个问题，请把解压后的算例目录一并提供"]

    root = _os.path.dirname(_os.path.abspath(master))
    all_dss = sorted(glob.glob(_os.path.join(root, "**", "*.dss"), recursive=True))
    if len(all_dss) <= 1:
        return ["- 多文件依赖：目录内只有 1 个 `.dss`（%s），"
                "本次是**单文件算例**，没有 Redirect 依赖链"
                % _os.path.basename(master)]
    if _os.path.basename(master).lower() != "master.dss":
        # 不是从 Master 起的，直接说明并列出目录内容（仍不给引用关系）
        return ["- 多文件依赖：目录内有 %d 个 `.dss`，但转换入口不是 `Master.dss`"
                "（是 %s），无法判定谁 Redirect 谁 —— 不猜"
                % (len(all_dss), _os.path.basename(master))]

    def rel_of(p: str) -> str:
        """相对目录的 posix 路径，去掉 `.dss` 后缀，转小写。"""
        r = _os.path.relpath(p, root).replace("\\", "/")
        return _os.path.splitext(r)[0].lower()

    def stem(p: str) -> str:
        return _os.path.splitext(_os.path.basename(p))[0].lower()

    # 每个文件的两种键：相对路径（`base/circuit`）与裸文件名（`circuit`）
    by_rel = {rel_of(p): p for p in all_dss}
    by_stem: dict[str, str] = {}
    for p in all_dss:
        by_stem.setdefault(stem(p), p)

    # 从Master 出发做**传递闭包**，只解析不执行。
    # ⚠️ 用**递归 + 声明顺序**，不用栈（`list.pop()` 出栈）：栈会输出逆序，
    # 而 OpenDSS 的 Redirect 是**按声明顺序深度优先执行**的，后声明的可以
    # 覆盖前面已定义的同名元件 —— 顺序有语义，打乱就等于报错了一个假的执行序。
    referenced: set[str] = set()
    order: list[str] = []

    def walk(cur: str) -> None:
        key = rel_of(cur)
        if key in referenced:
            return
        referenced.add(key)
        order.append(key)
        try:
            with open(cur, encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except OSError:
            return
        for m in _re.finditer(r"(?im)^[ \t]*Redirect[ \t]+\"?([^\"\r\n]+?)\"?[ \t]*$", txt):
            raw = m.group(1).strip()
            if not raw:
                continue
            # OpenDSS 的 Redirect 目标可带相对目录（`base/Circuit.dss`），
            # 也可只写文件名（`Circuit`）。⚠️ **两种键都要试** ——
            # 只按裸文件名匹配会把 `base/Circuit.dss` 判成"未引用"，
            # 连带把它下面的整条依赖链全部误报（真机 case32 实测：10 个文件
            # 只认出 1 个，9 个全被误判成"未被引用"）。
            raw_key = _os.path.splitext(raw.replace("\\", "/").split()[0])[0].lower()
            tgt = by_rel.get(raw_key) or by_stem.get(stem(raw_key))
            if tgt is not None:
                walk(tgt)

    walk(master)

    unused = [p for p in all_dss if rel_of(p) not in referenced]

    def show(p: str) -> str:
        return _os.path.relpath(p, root).replace("\\", "/")

    out = ["- 多文件依赖：目录内共 %d 个 `.dss`，Master 依赖链上 **%d 个**（%s）"
           % (len(all_dss), len(referenced),
              " → ".join(order) if order else "?")]

    if unused:
        # ⚠️ 未被引用的文件**必须分类**，不能一律说成"独立场景"：
        # 场景文件与"目录里多放了、但网络定义里用不到"的文件是两回事，
        # 一锅端会给出错误的处置建议。
        scen_hit = [p for p in unused
                    if any(t in show(p).lower()
                           for t in ("scenario", "outage", "state", "case"))]
        other = [p for p in unused if p not in scen_hit]
        out.append("- ⚠️ **未被 Redirect 引用、本次未执行**（%d 个）—— "
                   "它们不在 Master 依赖链上，**不能**跟着 Master 一起自动执行："
                   % len(unused))
        if scen_hit:
            out.append("  · **场景文件**（%d 个）：%s —— 这是**独立场景**："
                       "只有显式切换到该场景（改 Redirect 目标或另建算例）时它才生效，"
                       "跟着 Master 自动执行会把两个场景叠在一起，算出来的电压/潮流"
                       "既不是基态也不是该场景的，没有物理意义"
                       % (len(scen_hit),
                          "、".join(show(p) for p in scen_hit)))
        if other:
            out.append("  · **其它未被引用的 `.dss`**（%d 个）：%s —— 目录里存在但"
                       "网络定义没有 Redirect 到它们；请确认是历史遗留、"
                       "还是漏写了一行 Redirect（漏写会让对应元件/曲线**静默缺失**）"
                       % (len(other), "、".join(show(p) for p in other)))
    else:
        out.append("- 目录内所有 `.dss` 都在 Master 依赖链上，"
                   "没有「未被引用却存在」的文件")
    return out


def _edge_set(data: dict[str, Any]) -> tuple[list, list, list[str]]:
    """取出 (母线, 边, 未知端点) —— 拓扑类取数的**共同前置**。

    单点定义：`_loop_topology` 与 `_island_topology` 都必须先过这一关。
    同一逻辑写两遍必然漂移 —— 本轮就发生过：先给环网判据加了校验，
    孤岛判据还是静默把未知端点当新母线。

    第三项是**支路引用了、但母线表里不存在的节点**。这类数据不自洽时，
    拓扑结论（连通分量 / 独立回路数 / 环流）会跟着算错：实测母线 5 个 +
    2 个不存在的端点，分量数被算成 4、凭空报出"成环"。必须报出来。
    """
    buses = [b for b in (data.get("bus_results") or []) if isinstance(b, dict)]
    branches = [b for b in (data.get("branch_results") or []) if isinstance(b, dict)]
    known = {str(b.get("name") or "") for b in buses} - {""}
    unknown: list[str] = []
    edges: list[tuple[str, str, str, float]] = []
    for br in branches:
        fb, tb = str(br.get("from_bus") or ""), str(br.get("to_bus") or "")
        if not (fb and tb):
            continue
        p = br.get("p_kw")
        edges.append((fb, tb, str(br.get("name") or "?"),
                      float(p) if isinstance(p, (int, float)) else 0.0))
        for n in (fb, tb):
            if n not in known and n not in unknown:
                unknown.append(n)
    return buses, edges, unknown


def _pick_source(buses: list) -> str | None:
    """认出电源（平衡）母线 —— 拓扑类判据的**单点定义**。

    为什么必须认真认
    ----------------
    真机实测（case31）：回复上面写"已收敛（迭代 3 次）、网损 3644 kW"，
    下面却写"15 个连通分量、264 个母线是孤岛、潮流无解"—— **自相矛盾**。
    根因是 `sorted(groups)[0]` 拿字典序第一个母线当电源，
    于是真正的电源侧分量反被当成"孤岛"。

    判据（按可靠性排序）：① 名字命中 sourcebus/source/slack/system/grid；
    ② 电压 ≈ 1.0 pu（平衡母线的电气特征）。两条都不命中才返回 None ——
    **不猜**（宁可不给结论，也不给错结论）。
    """
    cand = [b for b in buses
            if str(b.get("name") or "").lower() in
            ("sourcebus", "source", "slack", "system", "grid")]
    if not cand:
        cand = [b for b in buses
                if isinstance(b.get("voltage_pu"), (int, float))
                and abs(float(b["voltage_pu"]) - 1.0) < 1e-3]
    return str(cand[0].get("name") or "") if cand else None


def _loop_topology(data: dict[str, Any]) -> list[str]:
    """环网 / 成环诊断：区分**真实成环**与**数据错误**。

    真机实测的缺陷（`aa_invalid_loop`）：用户问"环在哪几条支路上、是真实成环
    还是数据错误、怎么改数据"，回复只有收敛/电压/网损三项，等于没答。

    判据全部**从产物严格算出来**，不靠推测：
    - 独立回路数 = 支路数 − 母线数 + 连通分量数（图论的基本回路数，
      辐射网为 0，每多一个独立环就 +1）；
    - **同一对端点出现两条及以上支路** → 重复录入，是**数据错误**，
      不是真实成环（真实成环的两条路径端点必然不同）；
    - 环流用**生成树分解**判：弦边上的潮流即环流分量。
      ⚠️ 不能用"潮流方向与支路命名相反"当判据 —— 纯辐射支路就会有负号，
      那是潮流方向问题，与闭环无关（实测曾把 `-222 kW` 的辐射支路误判成环流）。

    取不到支路/母线就明说取不到，不给猜测值。
    """
    buses, edges, unknown = _edge_set(data)
    if not buses or not edges:
        return ["- 环网诊断：本次结果不含母线/支路结果域，无法判断是否成环（不给猜测）"]
    if unknown:
        return ["- 拓扑判据：⚠️ 支路引用了母线结果里**不存在的节点**（%s）—— "
                "母线与支路数据不自洽，独立回路数与环流结论都会算错，"
                "因此本次不给成环判断（这是**数据一致性问题**，不是成环问题）"
                % "、".join(unknown[:8])]

    nodes = [str(b.get("name") or "") for b in buses]

    # 邻接表（忽略方向）
    adj: dict[str, list[str]] = {}
    for fb, tb, _, _ in edges:
        adj.setdefault(fb, []).append(tb)
        adj.setdefault(tb, []).append(fb)

    # 连通分量数（并查集）
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for fb, tb, _, _ in edges:
        for n in (fb, tb):
            parent.setdefault(n, n)
        ra, rb = find(fb), find(tb)
        if ra != rb:
            parent[ra] = rb
    comps = len({find(n) for n in parent})
    n_nodes, n_edges = len(parent), len(edges)
    cyclomatic = n_edges - n_nodes + comps

    out: list[str] = []

    # 重复录入检测：同一对端点（无序）出现多条不同支路 → 数据错误
    seen: dict[frozenset, list[str]] = {}
    for fb, tb, nm, _p in edges:
        seen.setdefault(frozenset((fb, tb)), []).append(nm)
    dup = {k: v for k, v in seen.items() if len(v) > 1}

    # ── 前置自洽检查：求解收敛却算出多个分量，说明支路图不完整 ──
    # 真机实测（case31，12 馈线配电网）：支路图算出 15 个分量 / 回路数 10，
    # 实际各馈线经变压器挂在 grid 上 —— 变压器**不在** `branch_results` 里，
    # 于是每条馈线都成了独立"分量"，凭空报出"10 个独立回路"。
    # 判据：收敛 + 分量数 > 1 ⇒ 分量必然是假的（失电区不可能收敛），
    # 此时**只报支路图层面的事实，不报独立回路数**。
    conv = (data.get("solver_status") or {}).get("converged")
    graph_incomplete = conv is True and comps > 1

    if graph_incomplete:
        out.append("- 拓扑（按支路图）：支路 %d 条、母线 %d 个、支路图分成 %d 段；"
                   "但**本次求解是收敛的**，失电区不可能收敛 ⇒ "
                   "各段之间必然经**不在支路结果里**的元件（多为变压器）互联。"
                   "支路图不完整，**因此不给独立回路数与成环结论**"
                   % (n_edges, n_nodes, comps))
    else:
        head = ("- 拓扑判据：支路 %d 条、母线 %d 个、连通分量 %d 个 → 独立回路数 = "
                "%d − %d + %d = **%d**"
                % (n_edges, n_nodes, comps, n_edges, n_nodes, comps, cyclomatic))
        if cyclomatic <= 0:
            out.append(head + "，即**纯辐射网，不存在任何闭环**"
                       "（无环流、无重复计入负荷的风险）")
        else:
            out.append(head + "，即网络**真实成环**（存在 %d 个独立回路）"
                       % cyclomatic)

    if dup:
        out.append("- ⚠️ 检测到**同一对端点被录入多条支路**（属数据错误，不是真实成环）："
                   + "；".join("%s ⇄ %s 有 %s"
                               % (sorted(k)[0], sorted(k)[1], "、".join(v))
                               for k, v in list(dup.items())[:4]))

    # 环流检测（生成树分解）
    # --------------------------------------------------
    # ⚠️ 判据必须能真正区分"环流"和"潮流方向与支路命名相反"——
    # 真机离线验证时用 BFS 树深度当判据，结果把 `Line.l003(311→262, -222 kW)`
    # 误判成环流；实际上负号只说明负荷在 311 侧、潮流沿 311→262 反向流动，
    # 纯辐射支路就会有这种符号，与闭环无关。
    #
    # 严格判据：任取一棵**生成树**（以电源为根的 BFS 树），把支路集拆成
    # 「树边」与「弦边」两类。**弦边上的潮流就是环流分量**（树上不含它，
    # 它的功率只能靠闭合回路分流而来），它的数值与选哪棵树无关，
    # 符号约定才是唯一约定。树边的潮流是否"反向"没有意义。
    root = _pick_source(buses)
    if root is None:
        out.append("- 环流检测：**认不出电源母线**（无 sourcebus 之类命名、"
                   "也无电压≈1.0 pu 的平衡节点特征），因此不给环流结论 —— "
                   "指定一个母线当电源再算弦边，结论没有依据")
        return out
    seen_b: set[str] = {root}
    order = [root]
    while order:
        cur = order.pop(0)
        for nx in adj.get(cur, ()):
            if nx not in seen_b:
                seen_b.add(nx)
                order.append(nx)
    unreached = set(parent) - seen_b
    # ⚠️ 弦边判据只在**支路图连通**时成立，但"连通"要用**闭合图**口径 ——
    # 忽略 status=open 的支路。若连闭合图上都到不了，那是支路图缺连接
    # （多馈线经变压器互联），弦边集不可信；反之闭合图能到、只是开关跳闸
    # 才断开，那是**真孤岛**，生成树不覆盖它同样算不出弦边 → 两种都不报环流。
    par_c: dict[str, str] = {n: n for n in parent}

    def fc(x: str) -> str:
        while par_c[x] != x:
            par_c[x] = par_c[par_c[x]]
            x = par_c[x]
        return x

    for fb, tb, _nm, _p in edges:
        ra, rb = fc(fb), fc(tb)
        if ra != rb:
            par_c[ra] = rb
    closed_reach = {n for n in parent if fc(n) == fc(root)}
    never = set(parent) - closed_reach

    if unreached and (data.get("solver_status") or {}).get("converged") is not True:
        out.append("- ⚠️ 存在 **%d 个母线**无法从电源 %s 经闭合支路到达：%s"
                   % (len(unreached), root, "、".join(sorted(unreached)[:10])))
    elif unreached:
        out.append("- 连通性提示：%d 个母线不在电源的支路可达范围内，"
                   "但**本次求解收敛**，因此它们不是失电孤岛 —— "
                   "只可能经**变压器**等不在支路结果里的元件接入，"
                   "不做孤岛判断" % len(unreached))
    # 用同一棵 BFS 树分类：两个端点都在树里且已被树边覆盖 → 判为弦边
    tree_pairs = set()
    covered = {root}
    again = [root]
    while again:
        cur = again.pop(0)
        for nx in adj.get(cur, ()):
            if nx not in covered:
                covered.add(nx)
                tree_pairs.add(frozenset((cur, nx)))
                again.append(nx)
    chords = [(nm, fb, tb, p) for fb, tb, nm, p in edges
              if frozenset((fb, tb)) not in tree_pairs]
    # ⚠️ 弦边判据**只有在支路图连通时才成立**。
    # 生成树只能覆盖一个连通分量；若支路图本身分成多段（多馈线经变压器互联），
    # 树外的所有支路都会被误判成"弦边" —— 实测 264 条里 264 条全成弦边、
    # 合计 138 万 kW、占总负荷 963%，一眼荒谬。
    # 判据：树覆盖不全（`unreached` 非空）⇒ 弦边集不可信，不报环流。
    if unreached or never:
        why = ("支路图不完整（%d 个母线在任何支路组合下都到不了电源）"
               % len(never) if never
               else "支路图分成多段（%d 个母线不在电源可达范围内）" % len(unreached))
        out.append("- 环流检测：%s，生成树无法覆盖全网，"
                   "**弦边集不可信**，因此不给环流数值" % why)
        return out
    if chords:
        tot = sum(abs(p) for _, _, _, p in chords)
        out.append("- 环流检测（生成树分解）：以电源 %s 为根构造生成树后，"
                   "%d 条支路为**弦边**（树上不存在这条路径，"
                   "其功率只能靠闭合回路分流而来）—— 这就是环流分量：%s"
                   % (root, len(chords),
                      "；".join("%s（%s→%s，%.2f kW）" % (nm, fb, tb, p)
                              for nm, fb, tb, p in chords[:4])))
        out.append("- 弦边有功绝对值合计 %.2f kW，占总负荷 %.2f%%；"
                   "成环**不等于**故障：配电网闭环运行是常态，"
                   "正确处理是保持拓扑不变继续求解，不要删支路"
                   "（删了会改变供电路径），也不要把环上负荷重复计入"
                   "（每个负荷元件只计一次，本系统按元件端子求和，已避免重复）"
                   % (tot, _pct(tot, data)))
    else:
        out.append("- 环流检测（生成树分解）：所有 %d 条支路均可归入一棵生成树，"
                   "**无弦边即无环流** —— 网络拓扑上不存在闭合回路"
                   % len(edges))
    return out


def _pct(part: float, data: dict[str, Any]) -> float:
    tot = (data.get("system_summary") or {}).get("total_load_kw")
    try:
        return float(part) / float(tot) * 100.0 if tot else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _island_topology(data: dict[str, Any]) -> list[str]:
    """连通性 / 孤岛诊断：从产物严格算连通分量与不可达母线。

    前置校验走 `_edge_set`（单点定义，与 `_loop_topology` 共用）——
    母线与支路数据不自洽时不给结论，否则分量数会凭空虚高。
    """
    buses, edges, unknown = _edge_set(data)
    branches = [b for b in (data.get("branch_results") or []) if isinstance(b, dict)]
    if not buses or not branches:
        return ["- 连通性诊断：结果不含母线/支路结果域，无法判断连通分量（不给猜测）"]
    if unknown:
        return ["- 连通性诊断：⚠️ 支路引用了母线结果里**不存在的节点**（%s）—— "
                "母线与支路数据不自洽，连通分量与孤岛结论都会算错，"
                "因此本次不给判断（这是**数据一致性问题**）"
                % "、".join(unknown[:8])]
    nodes = {str(b.get("name") or "") for b in buses}
    parent = {n: n for n in nodes}
    edge_pairs = {(fb, tb) for fb, tb, _, _ in edges}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    n_closed = 0
    # 闭合图（**忽略** status=open 的支路）的并查集 ——
    # 用来区分两种"不可达"，这是唯一可靠的判别方式：
    #   · 在**闭合图**上也不可达 → 支路图本身就没有通路（多馈线经变压器互联）
    #   · 闭合图上可达、只因断开才不可达 → **真孤岛**（开关跳闸造成的失电区）
    par_closed: dict[str, str] = {n: n for n in nodes}

    def find_c(x: str) -> str:
        while par_closed[x] != x:
            par_closed[x] = par_closed[par_closed[x]]
            x = par_closed[x]
        return x

    for br in branches:
        fb, tb = str(br.get("from_bus") or ""), str(br.get("to_bus") or "")
        st = str(br.get("status") or "").lower()
        if not (fb and tb):
            continue
        if st not in ("open", "disconnected"):
            n_closed += 0
        else:
            n_closed += 1
        parent.setdefault(fb, fb)
        parent.setdefault(tb, tb)
        par_closed.setdefault(fb, fb)
        par_closed.setdefault(tb, tb)
        if st not in ("open", "disconnected"):
            ra, rb = find(fb), find(tb)
            if ra != rb:
                parent[ra] = rb
        ra, rb = find_c(fb), find_c(tb)
        if ra != rb:
            par_closed[ra] = rb

    # ⚠️ **变压器必须并进图里**，否则配电网会被算成"一堆孤立馈线"。
    # 配电网的馈线首端普遍经变压器挂在上级电网节点（如 `grid`）上，
    # 只按线路建图 → 每条馈线各自成一分量 → 与"已收敛"直接矛盾
    # （真机 case31 实测：15 个分量、268 个母线"在任何支路组合下到不了"，
    #  最后只能判成"支路结果域不完整、不给孤岛结论"—— 白白丢一个真结论）。
    # 这不是"多算几条边"，而是拓扑判据的**数据基础**。
    # 变压器没有 `status` 字段语义（`FieldStatus` 指字段可用性，不是开关状态），
    # 故一律按**闭合**处理；端点为空则跳过，不猜。
    _tx_used = 0
    for t in (data.get("transformer_results") or []):
        if not isinstance(t, dict):
            continue
        fb, tb = str(t.get("from_bus") or ""), str(t.get("to_bus") or "")
        if not (fb and tb) or fb == tb:
            continue
        if fb not in nodes or tb not in nodes:
            # 端点不在母线结果里（分相口径下变压器端点可能带 `.1` 后缀）→ 跳过
            continue
        _tx_used += 1
        parent.setdefault(fb, fb)
        parent.setdefault(tb, tb)
        par_closed.setdefault(fb, fb)
        par_closed.setdefault(tb, tb)
        ra, rb = find(fb), find(tb)
        if ra != rb:
            parent[ra] = rb
        ra, rb = find_c(fb), find_c(tb)
        if ra != rb:
            par_closed[ra] = rb

    groups: dict[str, list[str]] = {}
    for n in list(parent):
        groups.setdefault(find(n), []).append(n)
    # ── 根（电源侧）必须**认得出来**，不能靠字典序兜底 ──
    # 详见 `_pick_source` 的说明：真机 case31 曾因此报出
    # "264 个母线是孤岛、潮流无解"，而同一份回复写着"已收敛"——
    # 电源认错 ⇒ 真正的电源侧分量反被当成孤岛 ⇒ 结论自相矛盾。
    root = _pick_source(buses)
    if root is None and groups:
        # 连电源特征都认不出时，只报**分量规模**，不判断谁被隔离 ——
        # 拿最大分量当"电源侧"是猜测，不做。
        big = max(groups, key=lambda k: len(groups[k]))
        return ["- 连通分量：共 %d 个，最大分量含 %d 个母线；"
                "但结果里**认不出电源母线**（无 sourcebus 之类命名、"
                "也无电压≈1.0 pu 的平衡节点特征），因此**不判断孤岛** —— "
                "凭空指定一个母线当电源再宣布其它都是孤岛，那是编结论"
                % (len(groups), len(groups[big]))]

    # ── 自洽性检查：收敛成功的算例不可能有失电孤岛 ──
    # 失电区无电压方程解，潮流不可能收敛。两者同时出现说明
    # **是本判据算错了**（电源认错或结果域不完整），不是网络有问题。
    # 这时必须说"判据不自洽"，而不是硬报孤岛 —— 宁可不给结论，不给错结论。
    #
    # 真机实测（case31，12 馈线配电网）：`grid` 是上级电网节点，只经**变压器**
    # 连到 f1~f12 各馈线首端，本身不是任何线路的端点；而
    # `transformer_results` **不含 from_bus/to_bus**（实测只有 name/p/q/loading），
    # 所以仅凭 `branch_results` 无法把各馈线合并回一个分量 ——
    # 按支路图算是 15 个分量、268 个母线不可达，与"已收敛"直接矛盾。
    # **这不是网络有问题，是本判据的数据基础不足** → 必须明说取不到。
    conv = (data.get("solver_status") or {}).get("converged")
    live = set(groups[find(root)])
    orphan = sorted(set(parent) - live)
    # 闭合图上从电源可达的母线集合 —— **这是判别"真孤岛"的关键**
    reach_closed = {n for n in parent if find_c(n) == find_c(root)}
    cut_off = [n for n in orphan if n in reach_closed]   # 只因断开而失联
    never = [n for n in orphan if n not in reach_closed]  # 闭合图上就没有通路

    # 自洽检查：收敛 + 存在"闭合图上也到不了"的母线 ⇒ 支路图不完整
    #（真机 case31：12 馈线经变压器挂在 grid 上，变压器不在支路结果里）
    if conv is True and never:
        tx = data.get("transformer_results") or []
        tx_linked = sum(
            1 for t in tx
            if isinstance(t, dict) and (t.get("from_bus") or t.get("to_bus")))
        if _tx_used:
            extra = ("（已并入 %d 台变压器的连接母线后仍有母线不可达 —— "
                     "互联元件**不止变压器一种**（如 `Reactor`/跨电压等级 `Line`），"
                     "本次结果域未覆盖，故不给孤岛结论" % _tx_used)
        elif tx and tx_linked == 0:
            extra = ("（各部分之间靠**变压器**等元件互联，但 `transformer_results` "
                     "**不含连接母线**，无法合并）")
        else:
            extra = ""
        return ["- 连通性：电源 %s 经闭合支路可达 %d 个母线，"
                "另有 %d 个母线**在任何支路组合下都到不了**；"
                "但**本次求解是收敛的**（失电区无电压方程解，不可能收敛）"
                "—— 说明是**支路结果域不完整**%s，"
                "而不是网络真有孤岛。**因此不给孤岛结论**"
                % (root, len(live), len(never), extra)]
    if conv is True and orphan:
        return ["- 连通性：按支路图看，电源 %s 可达 %d 个母线，"
                "另有 %d 个不在其范围内；但**本次求解是收敛的**"
                "（失电区无电压方程解，不可能收敛）—— 两者矛盾，"
                "说明是**本判据与结果域不自洽**，因此不给孤岛结论"
                % (root, len(live), len(orphan))]

    out = ["- 连通分量：共 %d 个，最大分量含 %d 个母线（电源 %s 所在分量）"
           % (len(groups), len(live), root)]
    if n_closed:
        out.append("- 已断开支路 %d 条（status=open 计入，不参与连通）" % n_closed)
    # ⚠️ 三种"不在电源侧分量里"的情况，必须分开，不能一律当成孤岛：
    #  ① **闭合图上可达、只因断开而失联**（`cut_off`）—— 真孤岛，
    #     是开关跳闸造成的失电区，潮流无解；
    #  ② **闭合图上就没有通路**（`never`）—— 更可能是结果域不完整
    #     （该段靠变压器等不在 `branch_results` 里的元件接入）；
    #  ③ **在支路结果里根本没出现**（`dangling`）—— 未导出对应支路。
    # 混为一谈会把"产物域不完整"说成"潮流无解"，那是编结论。
    linked = {n for fb, tb, _, _ in edges for n in (fb, tb)}
    island = [n for n in cut_off if n in linked]
    dangling = [n for n in orphan if n not in linked]
    if island:
        out.append("- ⚠️ **孤岛 %d 个母线**：%s —— 这些母线在**所有支路闭合**时"
                   "与电源有通路，是**断开该支路后才被隔离**的失电区，潮流无解；"
                   "正确做法是先做拓扑诊断/恢复供电，"
                   "而不是对奇异网络反复换收敛算法硬跑"
                   % (len(island), "、".join(island[:10])))
    if never:
        out.append("- ⚠️ 另有 %d 个母线**在任何支路组合下都与电源无通路**（%s）—— "
                   "这不是孤岛判定成立的情形，**结果域可能不完整**"
                   "（缺变压器/开关的连接信息），不能断言其失电"
                   % (len(never), "、".join(never[:10])))
    if dangling:
        out.append("- ⚠️ 另有 %d 个母线**在支路结果里完全没有出现**（%s）—— "
                   "这属于**结果域不完整**（未导出对应支路），"
                   "不能据此断言它们是孤岛，也不能断言其潮流无解"
                   % (len(dangling), "、".join(dangling[:10])))
    if not orphan:
        out.append("- 无孤岛：全部 %d 个母线均可从电源 %s 到达，连通性完整"
                   % (len(parent), root))
    return out


def _overload_detail(data: dict[str, Any]) -> list[str]:
    """过载支路：从 `branch_results` 取真实负载率，按严重度排序。

    ⚠️ **`loading_status` 不是过载判据** —— 真机实测踩过：
    `Line.br0001` 的 `loading_pct=237%` 而 `loading_status="ok"`，
    一度把回复写成"负载率 290.2%，判定 ok"，**自相矛盾**。
    查 `result_normalizer.py`：`loading_status` 的语义是
    "这个负载率**算出来了没有**"（`ok` / `unavailable`），与是否超载无关。
    过载只能由 `loading_pct ≥ 100` 判定，判定词自己写，不引用该字段。
    """
    branches = [b for b in (data.get("branch_results") or []) if isinstance(b, dict)]
    rows: list[tuple[float, str, str, str, str]] = []
    n_no_rating = 0
    for br in branches:
        lp = br.get("loading_pct")
        if not isinstance(lp, (int, float)):
            n_no_rating += 1
            continue
        st = str(br.get("loading_status") or "")
        rows.append((float(lp), str(br.get("name") or "?"),
                     str(br.get("from_bus") or "?"), str(br.get("to_bus") or "?"),
                     "额定值不可用" if st == "unavailable" else ""))
    over = [r for r in rows if r[0] >= 100.0]
    over.sort(reverse=True)
    out: list[str] = []
    if n_no_rating:
        out.append("- 负载率不可算：%d 条支路缺额定电流（`loading_status=unavailable`），"
                   "不参与过载判断" % n_no_rating)
    if not over:
        if rows:
            out.append("- 支路负载率：%d 条可算支路**全部未超额定值**"
                       "（最高 %.1f%%，%s）—— 无过载"
                       % (len(rows), max(r[0] for r in rows),
                          max(rows)[1]))
        else:
            out.append("- 支路负载率：本次结果不含可算的负载率，不给过载结论")
        return out
    out.append("- 过载支路 %d 条 / 可算 %d 条（负载率 ≥ 100%%），"
               "按严重度降序前 5：" % (len(over), len(rows)))
    for lp, nm, fb, tb, note in over[:5]:
        sev = "严重过载" if lp >= 200 else ("过载" if lp >= 120 else "轻度超限")
        out.append("  · %s（%s→%s）负载率 %.1f%% —— %s%s"
                   % (nm, fb, tb, lp, sev, ("，" + note) if note else ""))
    return out


def _read_balance_residual(data: dict[str, Any],
                           artifacts: dict[str, str] | None = None
                           ) -> tuple[float | None, float | None]:
    """取功率平衡残差（pu）与容差。

    残差是**独立校验**的产物，只落在 `verification.json`，不在
    `powerflow_result.json` 里 —— 所以 `data` 里通常没有它（case04 实测）。
    取值顺序：
      ① `data` 内嵌（若上层把校验结果并进来了）；
      ② `artifacts["verification.json"]` 指向的产物文件；
      ③ 同目录的 `validation.json`（旧版口径，残差在 checks 里）。

    取不到就返回 (None, None) —— 调用方据此**不说**这句话，
    不用四数凑、不拿网损冒充（那正是本缺陷的形态）。
    """
    import json as _json

    def _pick(d: dict[str, Any]) -> tuple[float | None, float | None]:
        if not isinstance(d, dict):
            return None, None
        for k in ("power_balance_residual_pu", "balance_residual_pu",
                  "power_balance_residual", "residual_pu"):
            v = d.get(k)
            if isinstance(v, (int, float)):
                tol = d.get("power_balance_tolerance")
                return float(v), (float(tol) if isinstance(tol, (int, float)) else None)
        # 旧版口径：残差塞在 checks 列表里
        for c in (d.get("checks") or []):
            if isinstance(c, dict) and "power_balance" in str(c.get("name", "")):
                for v in c.values():
                    if isinstance(v, (int, float)) and abs(float(v)) < 1.0:
                        return float(v), None
        return None, None

    r, t = _pick(data)
    if r is not None:
        return r, t
    r, t = _pick(data.get("verification") or {})
    if r is not None:
        return r, t

    arts = artifacts or {}
    for name in ("verification.json", "validation.json"):
        p = arts.get(name)
        if not p or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                r, t = _pick(_json.load(f))
        except Exception:      # noqa: BLE001 —— 读不到就当没有，不让摘要层抛错
            r, t = None, None
        if r is not None:
            return r, t
    return None, None


def _focused_answers(data: dict[str, Any], question: str,
                      artifacts: dict[str, str] | None = None,
                      case_ref: str | None = None) -> list[str]:
    """按用户提问的维度，从已算好的结果里定向取数作答。

    硬约定：只**取** `powerflow_result.json` 里已有的字段，取不到就不说 ——
    绝不用"通常来说""理论上"补一句。宁可少答一句，不编一句。
    """
    q = (question or "").lower()
    want: set[str] = set()
    for keys, topic in _FOCUS_HINTS:
        if any(k in q for k in keys):
            want.add(topic)
    if not want:
        return []

    out: list[str] = []
    buses = [b for b in (data.get("bus_results") or []) if isinstance(b, dict)]
    partial = [b for b in buses if len(b.get("phase_labels") or []) < 3]
    three = [b for b in buses if len(b.get("phase_labels") or []) == 3]

    if "phase_v" in want or "worst" in want:
        # 最严重（不平衡最大）的母线分相电压：问"各相电压"时给的是它，不是随手一条
        ub = data.get("unbalance_summary") or {}
        wb = ub.get("worst_bus")
        tgt = next((b for b in buses if b.get("name") == wb), None) or (
            min(buses, key=lambda b: b.get("voltage_pu", 9e9)) if buses else None)
        if tgt is not None:
            labs = tgt.get("phase_labels") or []
            puv = tgt.get("phase_voltage_pu") or []
            if labs and puv:
                out.append("- %s 分相电压：%s" % (
                    tgt.get("name", "?"),
                    "，".join("%s %.4f pu" % (labs[i], v)
                              for i, v in enumerate(puv) if i < len(labs))))
            if "worst" in want and wb:
                out.append("- 不平衡最严重母线：%s（ε=%.4f%%）"
                           % (wb, float(ub.get("worst_unbalance_pct") or 0.0)))

    if "phase_a" in want and three:
        b = three[0]
        ang = b.get("phase_angle_deg") or []
        labs = b.get("phase_labels") or []
        if ang and labs:
            out.append("- %s 相角：%s" % (
                b.get("name", "?"),
                "，".join("%s %.2f°" % (labs[i], v)
                          for i, v in enumerate(ang) if i < len(labs))))

    if "negative_seq" in want and three:
        vals = [b.get("negative_seq_pu") for b in three
                if isinstance(b.get("negative_seq_pu"), (int, float))]
        if vals:
            out.append("- 负序电压幅值：三相母线中最大 %.3e pu，最小 %.3e pu"
                       % (max(vals), min(vals)))

    if "zero_seq" in want:
        vals = [b.get("zero_seq_pu") for b in three
                if isinstance(b.get("zero_seq_pu"), (int, float))]
        if vals:
            out.append("- 零序电压幅值：三相母线中最大 %.3e pu，最小 %.3e pu"
                       % (max(vals), min(vals)))
        else:
            out.append("- 零序：本次结果里**没有零序电压字段**，"
                       "因此不给零序数值（凭空补一个 R0/X0 假设等于编数据）")

    if "neutral" in want:
        out.append("- 中性线/零序通路：本系统按四线制与三线制统一处理，"
                   "中性线电流**未单独求解**；零序相关结论请以上面的零序电压为准，"
                   "不要当成中性线电流的结论")

    if "grounding" in want:
        if "zero_seq" in want or "neutral" in want:
            out.append("- 接地方式与 R0/X0：算例未显式给出时，转换器采用 OpenDSS "
                       "Line/R0 默认值；该假设已写进产物头部的相别声明，"
                       "零序相关结论的可靠范围受此限制")

    if "delta" in want:
        out.append("- Delta 接法：相电压 = 线电压 / √3（反之线电压 = √3×相电压）。"
                   "关于**三角形内部环流**：本系统求解的是**节点电压与支路潮流**，"
                   "不计算 delta 绕组内部环流 —— 这是能力边界，不给数值")
        if "loop" in want:
            # 真机实测修正：问"环流"时若同时问到网络拓扑（环网/闭环/成环），
            # 那是**网络环流**而不是绕组环流 —— 直接抛"不计算"会误导用户
            # 以为整个环流都算不了。必须把两种环流分开说清。
            out.append("- ⚠️ **「环流」有两种含义，请勿混淆**："
                       "① delta 绕组内部环流 —— 上面已说明不计算；"
                       "② 网络闭合回路中的功率分流（也叫环流）—— "
                       "**这个能从支路潮流直接看出来**，见下面「拓扑判据」与"
                       "「环流检测」两条，是有数值结论的")

    if "single_phase_tx" in want:
        txs = [t for t in (data.get("transformer_results") or []) if isinstance(t, dict)]
        if txs:
            phases = [t.get("phases") or t.get("num_phases") for t in txs]
            out.append("- 变压器：%d 台，其中单相（phases=1）%d 台"
                       % (len(txs), sum(1 for p in phases if str(p) == "1")))
        else:
            out.append("- 变压器：本次结果未包含变压器结果域，"
                       "无法回答单相/三相变压器的区分（不给猜测值）")

    if "transformer" in want and "single_phase_tx" not in want:
        txs = [t for t in (data.get("transformer_results") or []) if isinstance(t, dict)]
        out.append("- 变压器：%d 台" % len(txs) if txs
                   else "- 变压器：本次结果不含变压器结果域")

    if "balance" in want:
        ss = data.get("system_summary") or {}
        ls = data.get("loss_summary") or {}
        load = ss.get("total_load_kw")
        gen = ss.get("total_generation_kw")
        net = ss.get("net_load_kw")
        loss = ls.get("total_loss_kw")
        stg = ss.get("total_storage_kw")
        parts = []
        if load is not None:
            parts.append("总负荷 %.2f kW" % load)
        if gen is not None:
            parts.append("发电注入 %.2f kW" % gen)
        if stg is not None:
            parts.append("储能 %.2f kW" % stg)
        if loss is not None:
            parts.append("网损 %.2f kW" % loss)
        if net is not None:
            parts.append("净负荷 %.2f kW" % net)
        if parts:
            out.append("- 平衡式（总负荷≠净负荷，两者分列）：" + "，".join(parts))
        # 功率平衡残差：用户问「四数与残差」时必须一起给。
        # 此前只给四数、把残差吞掉 —— 报告第 9 节写着「功率平衡残差 0.000028 pu」，
        # 聊天回复却答不出（真机 case04 实测，判据 "回复覆盖关键要点" 缺「残差」）。
        # 残差是**独立校验**的产物，只落在 `verification.json` 里，
        # `powerflow_result.json`（= 本函数的 `data`）没有这个字段 ——
        # 所以必须经 `artifacts` 读产物文件，不能指望从 `data` 里取到。
        resid, resid_tol = _read_balance_residual(data, artifacts)
        if resid is not None:
            extra = "，容差 %g" % resid_tol if resid_tol is not None else ""
            out.append("- 功率平衡残差 %.3g pu（独立校验%s，见报告第 9 节）" % (resid, extra))

    if partial and ("phase_v" in want or "worst" in want or "zero_seq" in want
                     or "phase_a" in want):
        out.append("- ⚠️ 本算例含相数 < 3 的母线 %d 条（%s）；"
                   "这些母线按缺失相处理，**不平衡度在其上无定义**，"
                   "不可解读为「这三相平衡」"
                   % (len(partial),
                      "、".join(str(b.get("name")) for b in partial[:6])))
        # 逐条点出**缺的是哪一相** —— 真机实测的缺陷（case39）：
        # 用户问"C 相是缺失还是零电流，这两个含义完全不同"，
        # 回复只说"相数 < 3 按缺失相处理"，没说是哪一相，等于没答。
        # 「缺失」与「零电流」的区别必须讲清：缺失相是该相**不存在**
        # （无元件、无电压方程解），不是"解出来是 0"。
        miss: list[str] = []
        for b in partial[:6]:
            labs = [str(x) for x in (b.get("phase_labels") or [])]
            gone = [c for c in ("A", "B", "C") if c not in labs]
            if gone:
                miss.append("%s 缺 %s 相" % (b.get("name"), "、".join(gone)))
        if miss:
            out.append("- 缺失相逐条点明：" + "；".join(miss) + "。"
                       "**「缺失」不等于「零电流」**：缺失相是该相根本没有接入"
                       "元件（不参与节点电压方程，不是一个解出来的 0），"
                       "所以这些母线上的三相不平衡度没有定义；"
                       "若要按三相补充假设，需要你明确给出该相的接入方式")

    if "loop" in want:
        out.extend(_loop_topology(data))
    if "island" in want:
        out.extend(_island_topology(data))
    if "overload" in want:
        out.extend(_overload_detail(data))
    if "redirect" in want:
        out.extend(_redirect_manifest(data, artifacts, case_ref))
    return out


def _parse_solver_config_from_text(content: str) -> SolverConfig:
    """从用户文本中提取显式求解参数（max_iterations / tolerance）。"""
    import re

    sc = SolverConfig()
    # 最大迭代：兼容“把最大迭代设为 30 / 迭代次数改为 50 / max iterations 30”
    m = re.search(r"(?:最大迭代|iterations|iteration|迭代)\D*?(\d+)", content, re.IGNORECASE)
    if m:
        sc.max_iterations = int(m.group(1))
    # 容差：兼容“收敛容差为 0.00001 / 把容差设为 1e-5 / tolerance 0.0001”
    m = re.search(r"(?:收敛)?\s*容差\D*?(1e-?\d+|0?\.\d+)", content, re.IGNORECASE)
    if m:
        sc.tolerance = float(m.group(1))
    m = re.search(r"收敛标准\D*?(1e-?\d+|0?\.\d+)", content, re.IGNORECASE)
    if m:
        sc.tolerance = float(m.group(1))
    m = re.search(r"精度\D*?(1e-?\d+|0?\.\d+)", content, re.IGNORECASE)
    if m:
        sc.tolerance = float(m.group(1))
    # 数字在“精度”前：如“1e-6的精度”“0.0001 精度”
    m = re.search(r"(1e-?\d+|0?\.\d+)\s*的?\s*精度", content, re.IGNORECASE)
    if m:
        sc.tolerance = float(m.group(1))
    m = re.search(r"tolerance\D*?(1e-?\d+|0?\.\d+)", content, re.IGNORECASE)
    if m:
        sc.tolerance = float(m.group(1))
    return sc


# 历史结果查询关键词
_HISTORY_QUERY_KEYWORDS = [
    "是多少", "多少", "几台", "几个", "哪些", "结果", "报告", "电压", "网损", "损耗", "功率",
    "有功", "无功", "负载", "收敛", "迭代", "状态",
]

# 结构化历史查询：只读取已生成的网络画像/验证/结果产物，不应重新求解。
_STRUCTURE_QUERY_KEYWORDS = [
    "辐射", "网状", "环网", "闭环", "联络线", "并联", "拓扑", "支路类型",
    "母线数量", "线路数量", "变压器数量", "设备数量", "对象数量", "电压等级",
    "常开", "停运", "status=0", "inactive", "启用线路", "支路状态",
    "slack", "pv", "pq", "三相", "单相", "pvsystem", "storage", "储能",
    "保真度", "等价", "映射", "branch ratio", "q限值", "无功限值", "PV→PQ", "pv转pq", "强制 q", "字段",
    "N-1", "n-1", "停运 branch", "开断支路", "manifest", "QMAX", "QMIN", "无功上下限", "无功可行性", "无功限值", "强制所有 PV", "电压稳定裕度", "安全运行", "运行可接受", "收敛但不安全", "热过载", "过载", "不可行", "可行性", "极小非零", "tiny-impedance",
    "反向潮流", "反送", "反送功率", "过电压", "电压升高", "负 pd", "负 qd", "净发电", "相移", "shift_deg", "phase shift", "时序", "24 h", "24小时", "全天", "中午", "晚高峰", "高光伏",
    "前5", "前 5", "最低的5", "最低的 5", "最重载", "越限节点", "越限母线",
]

# 参数修改表达（含这些词且涉及容差/迭代/参数/精度时，应重新计算而非查历史）
# 注意：不要放单字词！"变"会命中"变压器"、"换"会命中"换算/切换"，
# 导致「这个算例有多少台变压器」被误判成参数修改而不走历史查询（实测缺陷）。
_PARAM_CHANGE_KEYWORDS = [
    "改变", "修改", "调整", "设置", "设为", "改成", "改为", "设成",
    "调到", "设置为", "重设", "换成", "变更",
]

# 求解参数相关词
_SOLVER_PARAM_KEYWORDS = [
    "容差", "迭代", "参数", "精度", "收敛标准", "最大迭代", "收敛容差",
]

# 用户明确表达“不要计算/不要重算”的否定词
_NEGATION_KEYWORDS = [
    "不要重算", "不要计算", "不要跑", "不要执行", "别重算", "别计算", "别跑", "别执行",
    "不要重新", "不用重算", "不用计算", "不用跑", "不必重算", "不必计算",
]

# 明显闲聊 / 非电力领域词（即使会话有历史任务，也不触发计算）
_CASUAL_BLOCK_KEYWORDS = [
    "天气", "吃饭", "几点", "今天星期", "今天周", "现在时间", "你好", "您好",
    "hello", "hi", "在吗", "谢谢", "再见", "拜拜", "睡觉", "下班", "加班",
]

# 历史结果比较查询词
_COMPARISON_KEYWORDS = ["相比", "比较", "变化", "差异", "差别", "差多少", "增加了", "减少了"]

# 「跨两次结果」的强语义词。只有出现这些词，才认定用户要的是把两次计算拿来比。
# 旧实现只认 _COMPARISON_KEYWORDS，于是「系统总负荷是多少，净负荷跟它差多少」这类
# **单次结果内部的口径之差**也命中「差多少」→ 在 metric 分派之前抢答
# 「目前只有一次成功计算结果，无法做前后对比」，把已经取到的总负荷/净负荷吞掉了。
_COMPARISON_STRONG_KEYWORDS = ("上次", "上一次", "前一次", "之前那次", "两次结果", "历史对比")


def _is_parameter_change(content: str) -> bool:
    """判断用户是否在修改求解参数（应触发重新计算，而不是查历史）。"""
    lower = content.lower().strip()
    # 只要出现“修改/设置/调整 + 求解参数词”，就认为是参数修改
    if any(w in lower for w in _PARAM_CHANGE_KEYWORDS):
        if any(p in lower for p in _SOLVER_PARAM_KEYWORDS):
            return True
    return False


def _is_history_query(content: str, ctx: dict[str, Any]) -> bool:
    """判断用户是否在基于已有计算结果提问（不需要重新跑潮流）。"""
    lower = content.lower().strip()
    # 只要有任意一次成功任务即可；最近一次任务失败不影响查询历史产物。
    if not (ctx.get("last_success") or ctx.get("last_success_task_id")):
        return False
    # 网络画像、设备统计、拓扑、保真度等均是已生成产物的只读查询。
    if any(w in lower for w in _STRUCTURE_QUERY_KEYWORDS):
        if not any(w in lower for w in _CONTEXT_RERUN_KEYWORDS) and not _is_parameter_change(content):
            return True
    # 用户明确说“不要重算/不要计算”：优先按历史查询处理（即使句子里含“重算”二字）
    if any(w in lower for w in _NEGATION_KEYWORDS):
        return any(w in lower for w in _HISTORY_QUERY_KEYWORDS)
    # 必须包含历史查询词
    if not any(w in lower for w in _HISTORY_QUERY_KEYWORDS):
        return False
    # 明确重跑 / 强计算意图：不是查询
    if any(w in lower for w in _CONTEXT_RERUN_KEYWORDS):
        return False
    if _has_strong_intent(lower):
        return False
    # 涉及参数修改：不是查询。用双重条件版（改动词 + 求解参数词），
    # 不能裸扫 _PARAM_CHANGE_KEYWORDS，否则"变压器"里的"变"会误伤查询类问题。
    if _is_parameter_change(content):
        return False
    return True


def _load_history_artifact(ctx: dict[str, Any], name: str) -> dict[str, Any] | None:
    """读取最近一次成功任务的结构化产物；只读，不触发重新计算。"""
    task_id = ctx.get("last_success_task_id") or ctx.get("last_task_id")
    if not task_id:
        return None
    # AgentConfig 默认路径已锚定项目根目录；保留环境变量覆盖能力，
    # 但不再依赖本次请求期间可能变化的 cwd。
    outputs_root = os.path.abspath(AgentConfig().outputs_root)
    path = os.path.join(outputs_root, task_id, name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _answer_by_metric(
    metric: str | None,
    content: str,
    ctx: dict[str, Any],
    slots: dict[str, Any] | None = None,
) -> str | None:
    """按 metric 回答历史结果类问题（只读产物，绝不重算）。

    2026-09-29 重构：触发条件从「关键词匹配」改为「metric 名」。

    背景：旧实现把「答什么」交给本文件里的关键词表，覆盖不到的说法就答不上，
    只能靠不断补关键词维持；而 LLM 在意图判定阶段**已经判出了 metric**
    （见 prompts.intent_classify 的 slots.metric 白名单），却被丢弃后重猜一遍。
    现在回答逻辑集中在 web/history_qa.py，本函数只做转发。

    边界：LLM 只提供「指标名」，数值全部来自权威产物 —— 不违反
    「数值必须来自引擎」的硬约定。
    """
    return history_qa.answer_by_metric(metric, content, ctx, slots)


def _history_fallback(metric: str | None, ctx: dict[str, Any]) -> str:
    """metric 缺失、或该指标在本算例上取不到数据时的明确回执。

    旧实现此时返回 None，最终落到一句泛泛的「还没有可直接引用的计算结果」，
    用户无法判断究竟是「没听懂」还是「确实没有数据」。现在把两种情况分开说清 ——
    缺口要自己冒出来，而不是等用户试出来。
    """
    task_id = ctx.get("last_success_task_id") or ctx.get("last_task_id")
    if not task_id:
        return "还没有可引用的计算结果。先让我算一次，之后再问具体指标。"
    if history_qa.known_metric(metric):
        return (
            f"识别到你在问「{history_qa.metric_title(metric)}」，"
            f"但上一次的结果产物里没有这项数据（任务 {task_id}）。\n"
            "- 可能是该算例不适用这个指标（例如平衡算例的不平衡度），"
            "或该维度未被请求导出。\n"
            "- 可以换一种问法，或明确要我重新计算并导出该结果范围。"
        )
    return (
        f"我没能把这句话对应到一个具体指标（任务 {task_id} 的结果已在）。\n"
        "可以直接问这些：最低/最高电压、电压越限母线、网损、总负荷、源侧供电、"
        "功率平衡残差、收敛状态、负载率排名、过载支路、反向潮流、三相不平衡度、"
        "分相电压、网络拓扑、设备数量、电压等级、求解参数、转换保真度、算例来源。"
    )


def _is_history_comparison_query(content: str, ctx: dict[str, Any]) -> bool:
    """判断用户是否在要求比较多次历史计算结果。"""
    if not (ctx.get("last_success") or ctx.get("last_success_task_id")):
        return False
    lower = content.lower().strip()
    # N-1、Q 限值和指定场景不能使用普通最近两次结果冒充比较。
    if any(w in lower for w in ["n-1", "停运 branch", "开断支路", "qmax", "qmin", "无功限值", "pv→pq", "强制 q", "24 h", "24小时", "全天", "时序", "中午", "晚高峰", "高光伏", "反向潮流", "反送", "过电压", "相移", "shift_deg"]):
        return False
    return any(w in lower for w in _COMPARISON_KEYWORDS)


def _wants_history_comparison(
    content: str, ctx: dict[str, Any], metric: str | None
) -> bool:
    """是否应当按「跨两次结果对比」作答。

    只在两种情况下成立：
    ① 意图层**没有**判出任何指标（无从按 metric 回答，才退化到对比）；
    ② 问题里有跨记录语义词（「上次」「上一次」…）。

    只要意图层已判出 metric 且问题不含跨记录词，就按该指标作答 —— 否则
    「净负荷跟它差多少」这种单次结果内部的口径之差会被误当成跨记录对比，
    抢在 metric 分派之前把「只有一次结果、无法对比」抛给用户。
    """
    if not _is_history_comparison_query(content, ctx):
        return False
    if not metric:
        return True
    lower = content.lower()
    return any(w in lower for w in _COMPARISON_STRONG_KEYWORDS)


def _answer_history_comparison(content: str, ctx: dict[str, Any]) -> str | None:
    """读取最近两次成功的计算结果并做简单对比。"""
    outputs_root = AgentConfig().outputs_root

    # 从历史消息中收集最近两次成功任务
    session = store.get_session(ctx.get("session_id", ""), include_messages=True)
    if not session:
        return None
    success_task_ids: list[str] = []
    for msg in reversed(session.messages):
        if msg.role == "assistant" and msg.task_id and msg.task_status in ("success", "converged"):
            if msg.task_id not in success_task_ids:
                success_task_ids.append(msg.task_id)
        if len(success_task_ids) >= 2:
            break

    if len(success_task_ids) < 2:
        # 只有一次成功结果，只能给出当前结果
        task_id = ctx.get("last_success_task_id") or ctx.get("last_task_id")
        if not task_id:
            return None
        result_path = os.path.join(outputs_root, task_id, "powerflow_result.json")
        if not os.path.isfile(result_path):
            return None
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
        system = data.get("system_summary", {})
        loss = data.get("loss_summary", {})
        lines = [
            "目前只有一次成功计算结果，无法做前后对比。",
            "上一次结果：",
            f"- 最低电压：{system.get('min_voltage_pu', '?'):.4f} pu",
            f"- 最高电压：{system.get('max_voltage_pu', '?'):.4f} pu",
            f"- 总有功网损：{loss.get('total_loss_kw', '?'):.4f} kW",
            f"- 总无功网损：{loss.get('total_loss_kvar', '?'):.4f} kvar",
        ]
        return "\n".join(lines)

    def _load_result(tid: str) -> dict[str, Any] | None:
        p = os.path.join(outputs_root, tid, "powerflow_result.json")
        if not os.path.isfile(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    newer_tid, older_tid = success_task_ids[0], success_task_ids[1]
    newer = _load_result(newer_tid)
    older = _load_result(older_tid)
    if not newer or not older:
        return None

    n_sys = newer.get("system_summary", {})
    o_sys = older.get("system_summary", {})
    n_loss = newer.get("loss_summary", {})
    o_loss = older.get("loss_summary", {})

    lines = [f"对比最近两次成功计算结果（{older_tid} → {newer_tid}）："]

    def _diff(label: str, new_val, old_val, unit: str = ""):
        if new_val is None or old_val is None:
            return None
        delta = new_val - old_val
        sign = "+" if delta > 0 else ""
        unit_s = f" {unit}" if unit else ""
        return f"- {label}：{old_val:.4f}{unit_s} → {new_val:.4f}{unit_s}（变化 {sign}{delta:.4f}{unit_s}）"

    s = _diff("最低电压", n_sys.get("min_voltage_pu"), o_sys.get("min_voltage_pu"), "pu")
    if s:
        lines.append(s)
    s = _diff("最高电压", n_sys.get("max_voltage_pu"), o_sys.get("max_voltage_pu"), "pu")
    if s:
        lines.append(s)
    s = _diff("总有功网损", n_loss.get("total_loss_kw"), o_loss.get("total_loss_kw"), "kW")
    if s:
        lines.append(s)
    s = _diff("总无功网损", n_loss.get("total_loss_kvar"), o_loss.get("total_loss_kvar"), "kvar")
    if s:
        lines.append(s)

    n_iter = newer.get("solver_status", {}).get("iterations")
    o_iter = older.get("solver_status", {}).get("iterations")
    if n_iter is not None and o_iter is not None:
        lines.append(f"- 迭代次数：{o_iter} 次 → {n_iter} 次")

    return "\n".join(lines)


def _try_convert_extracted_dir(extract_dir: str, upload_dir: str) -> str | None:
    """尝试将解压后的目录转换为 OpenDSS；若转换失败且包含 Master.dss，则直接返回。"""
    # 如果 zip 里已有 Master.dss，直接用它
    for root, dirs, files in os.walk(extract_dir):
        for name in files:
            if name.lower() == "master.dss":
                return os.path.join(root, name)

    # 尝试按 csv 算例集转换
    csv_files = [f for f in os.listdir(extract_dir) if f.lower().endswith(".csv")]
    if csv_files:
        try:
            # 产物归属于本 ZIP 的目录，不能让不同附件共享 converted_csv。
            return case_converters.convert_csv(extract_dir, extract_dir)
        except Exception:
            pass

    # 尝试按 json 转换（单个 json 文件）
    json_files = [f for f in os.listdir(extract_dir) if f.lower().endswith(".json")]
    if json_files:
        try:
            return case_converters.convert_json(os.path.join(extract_dir, json_files[0]), extract_dir)
        except Exception:
            pass

    return None


def _diag_workspace() -> str:
    """诊断轨迹的落盘根目录（与任务 workspace 同级，便于统一清理）。"""
    return os.path.join(AgentConfig().workspace_root, "_diagnostics")


def _criterion_hint(detail_text: str) -> str:
    """早期失败时补一句**判据本身**，让用户知道边界在哪。

    为什么需要
    ----------
    真机题库里有一类问题问的是**判据的区别**而不是单纯的对错：
    「检测到零阻抗 —— 这是奇异矩阵问题还是数值容差问题？
      如果阻抗是非常小但非零呢？」

    这类问题在早期失败路径上**答不了**，因为 `_focused_answers` 读的是
    `powerflow_result.json`，而这条路径上根本没有产物 —— 拿不到产物
    就必须明说取不到，不能沉默；但判据是**代码里写死的常量**，
    可以如实报出来（这不是推测，是可核对的实现事实）。

    判据一律从单点常量取（`case_converters.ZERO_IMPEDANCE_TOL`），
    不在这里另写一份数字 —— 写两遍必然漂移。
    """
    d = detail_text or ""
    if "零阻抗" in d:
        from .case_converters import ZERO_IMPEDANCE_TOL
        return ("判据边界：只拦 |r| 与 |x| **双双小于 %g pu（严格为零）**；"
                "阻抗非常小但非零（如 1e-9 pu）属合法数据，本系统**不拒绝**，"
                "由求解器按容差处理并如实报告迭代情况"
                % ZERO_IMPEDANCE_TOL)
    if "重复母线" in d:
        return ("判据边界：同一个 ID 在母线表里**出现两次及以上**即判为重复；"
                "数字串与字符串形式相同（如 '1' 与 '1.0'）会被归一化后合并，"
                "不算重复")
    if "孤岛" in d:
        return ("判据边界：以平衡母线（slack）为起点沿**闭合支路**做可达性搜索，"
                "不可达即判为孤岛；虚拟源节点（SOURCE）会折叠到真实平衡母线，"
                "不会因为命名差异被误判")
    return "如需判据细节，可追问是哪一条判据触发"


def _write_diagnostic_trace(
    session_id: str,
    filenames: list[str],
    detail_text: str,
    stage: str = "convert_case",
) -> str:
    """为「上传/转换阶段即被拒」写一份**可回看**的执行轨迹，返回 task_id。

    ## 为什么必须有这个函数

    实测缺陷：算例在**转换阶段**就被拒（孤岛 / 重复母线 / 零阻抗）时，
    `send_message` 走的是 `except HTTPException` 分支 —— 直接把错误当成
    assistant 消息返回，**不经过 service.run，因此一条 trace 事件都不产生**。
    后果是：负样本算例在界面上思考流**全空**，用户只看到一行
    「文件无法作为算例使用：…」，看不到"系统读了什么、判定依据是什么"。

    而这恰恰是最需要解释的场合 —— 用户传了个坏算例，最想知道的是
    **你看到了什么、为什么判定为坏、下一步该查哪里**。

    ## 为什么落盘而不是直接返回给前端

    轨迹必须与正常任务走**同一条回看通道**（`/api/tasks/{id}/trace` → 前端
    `fetchTrace`），否则就会出现"实时有、回看没有"的第二种不一致。
    事件结构与 `Tracer.emit` 落盘的完全一致，`detail` 里的槽位对应
    `runtime/narration.py` 新增的三个模板（ingest_upload / convert_case /
    diagnose_case），所以思考段仍由模板 + 真实数值生成，可复现、可审计。
    """
    from ..runtime.tracing import Tracer
    from ..schemas.enums import EventStatus, EventType

    task_id = "diag-" + uuid.uuid4().hex[:12]
    ws = os.path.join(_diag_workspace(), task_id)
    os.makedirs(ws, exist_ok=True)
    tracer = Tracer(os.path.join(ws, "trace.json"), cli_callback=None)

    tracer.emit(
        EventType.USER, "ingest_upload", "收到上传文件",
        detail={"files": list(filenames)},
    )
    tracer.emit(
        EventType.TOOL_CALL, "convert_case", "正在转换算例格式",
        detail={"source_format": ", ".join(os.path.splitext(f)[1] or "?" for f in filenames),
                "target": "OpenDSS Master.dss"},
    )
    # 失败态：narrate 对 failed 一律走"这一步没通过 + 不带着未通过的结果继续走"，
    # 错误原文会如实出现在思考段里（不加工、不美化、不吞细节）。
    tracer.emit(
        EventType.TOOL_RESULT, stage, detail_text,
        detail={"reason": detail_text, "layer": "转换阶段",
                "suggestion": "按上面的具体位置修改数据后重新上传"},
        status=EventStatus.FAILED, error_code="CASE_PRODUCT_REJECTED",
    )
    tracer.emit(
        EventType.GUARD, "diagnose_case",
        "已定位到数据问题，不进入求解",
        detail={"reason": detail_text, "layer": "转换阶段",
                "suggestion": "修正后重新上传；本系统不会带着未通过的结果继续求解"},
    )
    # 判据边界单独一段：用户常问"这是奇异问题还是容差问题""极小非零算不算"，
    # 这些答案在代码常量里（可核对），但早期失败路径没有产物可供取数，
    # 不给就是白丢一个已知的正确答案。仍是从常量取，不硬编码数字。
    try:
        tracer.emit(
            EventType.GUARD, "criterion_case",
            "判定依据与边界",
            detail={"reason": detail_text, "layer": "转换阶段",
                    "suggestion": _criterion_hint(detail_text)},
        )
    except Exception:  # noqa: BLE001 - 判据补充失败不该影响主诊断
        pass
    try:
        with open(os.path.join(ws, "state.json"), "w", encoding="utf-8") as f:
            json.dump({"task_id": task_id, "stage": "diagnostics",
                       "error": detail_text, "files": list(filenames)}, f,
                      ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001 - 诊断轨迹写盘失败不该影响回复
        pass
    return task_id


def _process_uploaded_files(session_id: str, files: list[UploadFile]) -> list[dict[str, Any]]:
    """保存上传文件并自动转换为 OpenDSS，返回附件描述列表。

    每个上传文件只保留一条附件记录：filename 保持原始文件名便于前端展示，
    path 指向转换后的目录或原文件，供后续定位 Master.dss 使用。
    """
    # ⚠️ **先整体校验，再逐个处理**：落盘用的是原始文件名
    # （`os.path.join(upload_dir, safe_name)`），同一次上传里出现同名文件会
    # **后者覆盖前者** —— 静默少一个算例，用户还以为都传上去了。
    # 这里宁可整体拒绝（`send_message` 会把它转成 assistant 诊断消息），
    # 也不要在处理到一半时才失败、留下部分写入。
    # 比较用 lower：Windows 文件系统不区分大小写，`A.dss` 与 `a.dss` 同样会互相覆盖。
    names = [os.path.basename(f.filename or "upload") for f in files]
    lowered = [n.lower() for n in names]
    dups = sorted({n for n, l in zip(names, lowered) if lowered.count(l) > 1})
    if dups:
        raise HTTPException(
            status_code=400,
            detail="同一次上传中有同名文件："
            + "、".join(dups)
            + "（先传的会被后传的覆盖）。请分开上传，或先重命名。",
        )

    upload_dir = store.ensure_upload_dir(session_id)
    attachments: list[dict[str, Any]] = []
    saved = []
    # 先落盘全部组成文件；否则bus.csv会在同行的line/system/load尚未保存时转换失败。
    for file in files:
        original_name = os.path.basename(file.filename or "upload")
        target_path = os.path.join(upload_dir, original_name)
        with open(target_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        saved.append((file, original_name, target_path))

    bundle_source: str | None = None
    for primary in ("bus.csv", "line.csv", "branch.csv"):
        path = os.path.join(upload_dir, primary)
        if os.path.isfile(path) and conversion_cache._source_state(path)["source_kind"] == "csv_bundle":
            # 有line时以bus为根；仅branch替代line时，公开branch入口能读取完整组件束。
            if primary != "bus.csv" or os.path.isfile(os.path.join(upload_dir, "line.csv")):
                bundle_source = path
                break
    bundle_master: str | None = None
    component_names = {"bus.csv", "line.csv", "branch.csv", "system.csv", "load.csv", "loads.csv"}
    for file, original_name, target_path in saved:
        safe_name = original_name

        converted_master: str | None = None
        ext = os.path.splitext(safe_name)[1].lower()
        try:
            if ext == ".zip":
                import zipfile
                extract_dir = os.path.join(upload_dir, safe_name[:-4])
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(target_path, "r") as z:
                    z.extractall(extract_dir)
                converted_master = _try_convert_extracted_dir(extract_dir, upload_dir)
                if converted_master:
                    zip_path = target_path  # 赋值前先留住 zip 本身
                    target_path = os.path.dirname(converted_master)
                    # zip 自带 Master.dss 时不经过转换器，需显式补戳：源文件记为 zip 本身，
                    # 这样将来判定"转换器升级需重建"能追溯到 zip 并重新解压转换。
                    try:
                        conversion_cache.write_meta(target_path, "dss_from_tables", zip_path)
                    except Exception:  # noqa: BLE001 - 打戳失败不影响上传
                        pass
            elif ext in (".xlsx", ".csv", ".json", ".m"):
                is_component = bool(bundle_source and safe_name.lower() in component_names)
                source = bundle_source if is_component else target_path
                dss_dir = conversion_cache.conversion_output_dir(source)
                if is_component and bundle_master:
                    converted_master = bundle_master
                else:
                    converted_master = conversion_cache._reconvert_from_source(source, dss_dir, raise_errors=True)
                    if not converted_master:
                        raise ValueError("转换未生成可用的Master.dss")
                    if is_component:
                        bundle_master = converted_master
                target_path = dss_dir
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"{ext or '文件'} 转换失败：{exc}")

        attachments.append({
            "filename": original_name,
            "path": target_path,
            "content_type": file.content_type or "application/octet-stream",
            **({"case_component": True} if ext == ".csv" and bundle_source and safe_name.lower() in component_names else {}),
        })
    return attachments


def _find_master_in(start_dir: str) -> str | None:
    """在目录里（含子目录）找 Master.dss。"""
    for root, _dirs, files in os.walk(start_dir):
        for name in files:
            if name.lower() == "master.dss":
                return os.path.join(root, name)
    return None


def _extract_case_ref(session_id: str, attachments: list[dict[str, Any]]) -> str | None:
    """从附件中定位**第一个**可作为 case_ref 的 Master.dss 路径。

    ⚠️ 任何返回 Master.dss 的分支都必须经过 `_fresh_case_master`。
    这不是风格问题：转换产物会被长期复用，带病产物（例如 1.6.0 给 `New Line`
    写非法 `Conn=`）留在盘上时，跳过校验就等于把已知会卡死引擎的文件再次送进
    compile —— 实测后果是 120s 硬超时、串行锁占满、整机瘫痪数分钟。

    需要**全部**算例时用 `_extract_case_refs()`（本函数只取第一个）。
    """
    session_upload_dir = store.ensure_upload_dir(session_id)

    for att in attachments:
        filename = att.get("filename", "")
        # zip 解压后形成目录
        if att.get("path") and os.path.isdir(att["path"]):
            master = _find_master_in(att["path"])
            if master:
                return _fresh_case_master(master)
        # 单文件 .dss：直投的 .dss 本是最终产物（无源文件可重建），
        # 但仍过一遍校验——`_fresh_case_master` 对这种情况返回 not-applicable
        # 并原样放行，代价可忽略，却堵住了"以后新增转换器忘了接校验"的口子。
        if filename.lower().endswith(".dss"):
            path = att.get("path") or os.path.join(session_upload_dir, filename)
            if os.path.isfile(path):
                return _fresh_case_master(path)
    # 兜底：在整个上传目录里找 Master.dss
    return _fresh_case_master(_find_master_in(session_upload_dir))


def _extract_case_refs(session_id: str, attachments: list[dict[str, Any]]) -> list[str]:
    """收集**每个附件各自**能独立计算的算例根（Master.dss），按附件顺序去重。

    ## 为什么要这个函数

    一次上传多个算例时，用户说"计算这两个算例"，期待的是**都算**。
    而 `_extract_case_ref()` 遇到第一个就返回 —— 结果是**静默只算了第一个**，
    用户以为第二个也被算了（实测就是这么发生的：上传 case39 + case40，
    只得到 case39 一份报告）。

    ## 判据：什么算"一个独立算例"

    * **目录型附件**（zip 解压 / 表格转换的产物）→ 在**它自己的目录**里找
      `Master.dss`，找到就是一个算例；
    * **单文件 `.dss`** → 只有文件名就是 `Master.dss` 才算算例根。
      散落的 `LineCodes.dss` 之类是**同一算例的组成文件**，不能算成两个算例
      （否则"上传 Master.dss + 子文件"会被误判成多算例、重复计算）。
    * 同一路径去重（`normcase`，Windows 不区分大小写）。

    单元素的返回值 = 原来的单算例语义，调用方据此决定走单条还是批量路径。
    """
    session_upload_dir = store.ensure_upload_dir(session_id)
    seen: set[str] = set()
    out: list[str] = []
    for att in attachments:
        path = att.get("path") or ""
        filename = str(att.get("filename") or "")
        master: str | None = None
        if path and os.path.isdir(path):
            master = _find_master_in(path)
        elif filename.lower() == "master.dss":
            master = path if os.path.isfile(path) else os.path.join(
                session_upload_dir, filename
            )
            if not os.path.isfile(master):
                master = None
        # 其它散落的 .dss（子文件）不是算例根，跳过
        if not master:
            continue
        try:
            fresh = _fresh_case_master(master)
        except Exception:  # noqa: BLE001 - 单个附件出问题不该拖垮整批
            continue
        key = os.path.normcase(os.path.abspath(fresh))
        if key not in seen:
            seen.add(key)
            out.append(fresh)
    return out


def _case_display_name(case_ref: str) -> str:
    """给批量结果用的算例显示名：优先取算例目录名（比 Master.dss 有信息量）。"""
    directory = os.path.dirname(os.path.abspath(case_ref))
    meta = conversion_cache.read_meta(directory) or {}
    source_name = os.path.basename(str(meta.get("source") or ""))
    if source_name:
        return source_name
    parent = os.path.basename(directory)
    if parent and parent.lower() not in ("cases", "uploads"):
        return parent
    return os.path.basename(case_ref)


def _run_case_into_message(
    msg: Any,
    session_id: str,
    content: str,
    case_ref: str,
    *,
    result_scopes: list[ResultScope],
    output_formats: list[OutputFormat],
    success_prefix: str = "",
    failure_prefix: str = "",
    on_task_started=None,
    persist_result=None,
    solver_config_override=None,
) -> tuple[str, TaskStatus, str]:
    """跑一个算例，把结果写进**已有的**占位消息。返回 (task_id, status, reply)。

    **单算例与批量共用这一份** —— 复制一套必然漂移（本项目已因"同一逻辑写两遍"
    吃过两次亏：元件求和口径、Compile 后的电路校验）。调用方只决定前缀文案。

    `success_prefix` / `failure_prefix` 分开，是为了**保持单算例路径的原有行为**：
    那边失败时的回复不含"上下文/默认算例/求解参数"提示，只有成功时才带。
    """
    service = _get_service()
    request = UserRequest(
        message=content,
        case_ref=case_ref,
        solver_config=solver_config_override if solver_config_override is not None else _parse_solver_config_from_text(content),
        result_scopes=result_scopes,
        output_formats=output_formats,
    )
    run_options = {"cli_callback": None}
    if on_task_started is not None:
        run_options["on_task_started"] = on_task_started
    response = service.run(request, **run_options)
    status = TaskStatus(response["status"])
    task_id = response["task_id"]
    report_url = f"/api/tasks/{task_id}/report"
    artifacts = response.get("artifacts", {})

    if status == TaskStatus.SUCCESS:
        summary = _extract_summary(artifacts, question=content, case_ref=case_ref)
        reply = f"{success_prefix}任务 **{task_id}** 已完成。\n\n{summary}"
    elif status == TaskStatus.CANCELLED:
        # 主动取消是独立终态，不能因为携带 CANCELLED 码就套用故障诊断。
        error = response.get("error") or {}
        reason = error.get("message") or "任务已被用户取消"
        reply = f"{failure_prefix}任务 **{task_id}** 已取消。\n\n{reason}"
    else:
        error = response.get("error", {})
        reply = (
            f"{failure_prefix}任务 **{task_id}** 未成功："
            f"{error.get('code', status.value)} — {error.get('message', '')}"
        )
        # 失败诊断（P2-b）：说清"往哪个方向查"。
        # 只对**真失败**状态给（不含 not_assessed / partial —— 那是"未成结论"，
        # 不是故障，硬给方向等于无中生有）；且只给方向，不给操作指令。
        #
        # ⚠️ 顺序很重要：先讲**本次**为什么失败（来自本次的重规划记录，
        # 即 `diagnosis`），再给通用排查方向（hint）。反过来的话用户先看到
        # 一堆通用建议，反而找不到"这次到底怎么了"。
        diagnosis = error.get("diagnosis")
        if diagnosis:
            reply += "\n\n" + str(diagnosis)
        hint = _diagnosis.suggest(
            error.get("code") or _diagnosis.code_for_status(status.value),
            error.get("message", ""),
        ) if _diagnosis.is_failure_status(status.value) or error.get("code") else None
        if hint:
            reply += "\n\n" + hint

    (persist_result or store.update_message_task)(
        msg.id,
        task_id=task_id,
        task_case_ref=case_ref,
        task_status=status.value,
        report_url=report_url,
        artifacts={
            k: f"/api/tasks/{task_id}/files/{k}"
            for k, v in artifacts.items()
            if isinstance(v, str)
        },
        content=reply,
    )
    return task_id, status, reply


def _run_case_batch(
    *,
    session_id: str,
    content: str,
    user_msg: Any,
    case_refs: list[str],
    title_text: str,
    session: Any,
    result_scopes: list[ResultScope],
    output_formats: list[OutputFormat],
) -> JSONResponse:
    """Persist immutable input copies and return before any solver finishes."""
    scheduler = batch_scheduler.current(_execute_batch_item)
    bid = None
    try:
        bid, items = batch_scheduler.prepare_inputs(case_refs, _case_display_name, AgentConfig().cases_root)
        batch = batch_store.create(session_id, user_msg.id, content, items,
            [scope.value for scope in result_scopes], [fmt.value for fmt in output_formats], batch_id=bid,
            solver_config=_parse_solver_config_from_text(content).model_dump(mode="json"))
        if batch is None:
            raise KeyError(session_id)
    except Exception as exc:
        if bid:
            batch_scheduler.remove_inputs(bid)
        if isinstance(exc, KeyError):
            raise HTTPException(status_code=404, detail="会话已被删除，批次未提交") from exc
        try:
            message = store.add_message(session_id, "assistant", f"批次未提交：输入副本保存失败：{exc}")
        except KeyError as missing:
            raise HTTPException(status_code=404, detail="会话已被删除，批次未提交") from missing
        return JSONResponse({"user_message": user_msg.to_dict(), "assistant_message": message.to_dict()})
    if session.title == "新聊天":
        store.update_session_title(session_id, title_text or f"{len(items)} 个算例")
    scheduler.notify()
    return JSONResponse({
        "user_message": user_msg.to_dict(),
        "batch_id": bid, "batch": batch, "assistant_message_count": len(items),
    }, status_code=202)


def _execute_batch_item(item, on_started):
    from types import SimpleNamespace

    result = {}
    def capture(_message_id, **fields):
        result.update(fields)
    frozen_solver = json.loads(item["solver_config"])
    _run_case_into_message(SimpleNamespace(id=item["message_id"]), item["session_id"], item["content"], item["case_ref"],
        result_scopes=[ResultScope(value) for value in json.loads(item["result_scopes"])],
        output_formats=[OutputFormat(value) for value in json.loads(item["output_formats"])],
        success_prefix=batch_store._head(item), failure_prefix=batch_store._head(item),
        on_task_started=on_started, persist_result=capture,
        solver_config_override=SolverConfig.model_validate(frozen_solver) if frozen_solver is not None else None)
    return result


def _extract_case_name_from_text(content: str) -> str | None:
    """从用户消息中提取内置算例名，如 ieee33、case4。"""
    lower = content.lower()
    for name in _BUILTIN_CASE_NAMES:
        if name in lower:
            return name
    # 匹配 "ieee 33" -> "ieee33"
    if "ieee" in lower:
        import re
        m = re.search(r"ieee\s*([0-9]+)", lower)
        if m:
            return f"ieee{m.group(1)}"
    return None


def _extract_mentioned_case_name(content: str) -> str | None:
    """检测用户是否显式提到一个算例名（包括可能不存在的自定义名）。"""
    import re
    lower = content.lower()
    # 明确带扩展名的上传文件优先于stem/内置名，区分case.csv、case.json等独立模型。
    filename = re.search(r"([a-z0-9_][a-z0-9_.-]*\.(?:xlsx|csv|json|m|zip|dss))(?![a-z0-9_.-])", lower)
    if filename:
        return filename.group(1)
    # 内置算例
    builtin = _extract_case_name_from_text(content)
    if builtin:
        return builtin
    # 自定义算例：case11_structured_hard_v2 / case_11_x / case17...
    # 注意 case 后可能紧跟数字（无分隔符），旧正则 `case[_\s]+` 要求必须有
    # 分隔符所以匹配不到 —— 导致用户点名的算例被整体忽略、悄悄沿用上一次。
    m = re.search(r"case[_\s]*([0-9]+[a-z0-9_]*)", lower)
    if m:
        return f"case{m.group(1)}"
    return None


class CaseProductRejected(HTTPException):
    """算例产物含已知会让引擎卡死的非法属性，拒绝送入计算。

    继承 `HTTPException` 有两个好处：
    - 上传阶段已有的 `except HTTPException` 分支会自动把它转成 assistant 诊断
      消息（见 send_message 里 `_process_uploaded_files` 的处理），前端不必改；
    - 万一某条调用路径没接住，它仍是一个语义正确的 HTTP 422，而不是 500。

    为什么必须"拒绝"而不是"降级沿用旧产物"：产物里若含 `New Line` 的 `Conn=`
    这类非法属性，OpenDSS COM 引擎会在 compile 阶段死循环，只能靠 120s 硬超时
    脱身；期间串行锁被占满，整个应用失去响应。让这次请求明确失败，远好过让
    应用瘫几分钟。
    """


def _illegal_line_props(master_dss: str) -> list[str]:
    """扫产物里「会让 OpenDSS 引擎卡死」的非法元素属性，返回问题描述列表。

    为什么需要一层内容级校验（而不是只信版本戳）：
    `refresh_master_if_stale` 依赖 `.conversion.json` 的版本号判断新鲜度。但版本戳
    可能缺失（历史产物）、可能记录错误、也可能转换器改了而忘了升号 —— 这些情况下
    校验都会放行，而盘上的文件仍然是带病的。

    已知致命案例（实测）：`New Line` 写 `Conn=`。OpenDSS 的 Line **没有** Conn
    属性（只有 Load / Generator 有），写上会让 COM 引擎在 compile 阶段**死循环**，
    只能靠硬超时脱身（表现为"卡在编译前检"数分钟、串行锁占满、整机失去响应）。

    这里只做"读文件 + 正则"的廉价检查，不加载引擎。任何一项命中都说明该产物
    不可信，调用方应拒绝使用，而不是抱侥幸心理送去 compile。
    """
    issues: list[str] = []
    try:
        with open(master_dss, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        return [f"产物不可读：{exc}"]

    import re  # 与文件内既有写法一致（本模块的 re 均按需局部导入）

    # 元素 → 该元素**确实拥有**的属性白名单（只列会致命的那几个，不做完整校验）
    forbidden: dict[str, tuple[str, ...]] = {
        # OpenDSS 的 Line 没有 Conn / Conn1 / Conn2
        "line.": ("conn", "conn1", "conn2"),
    }
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("!") or line.startswith("//"):
            continue
        low = line.lower()
        if not low.startswith("new "):
            continue
        # 取 "New <Class>." 里的 class
        head = low[4:]
        dot = head.find(".")
        if dot <= 0:
            continue
        cls = head[: dot + 1]  # 例如 "line."
        bad = forbidden.get(cls)
        if not bad:
            continue
        body = line[len("New ") + dot + 1 :].lower()
        for prop in bad:
            if re.search(rf"(?<![a-z0-9_]){re.escape(prop)}\s*=", body):
                issues.append(
                    f"第 {lineno} 行：{line.split()[1] if len(line.split()) > 1 else cls}"
                    f" 写了非法属性 {prop}=（OpenDSS 的 {cls.rstrip('.')} 不支持该属性，"
                    f"会导致引擎 compile 死循环）：{line[:90]}"
                )
    return issues


# 判定"这是不是 OpenDSS 脚本"用的关键字。刻意取得**宽**：命中任一就放行。
# 真实算例不可能一个都没有 —— 最简形式也得是 `New Circuit.x` 或 `Redirect x.dss`。
_OPENSS_SCRIPT_HINTS: tuple[bytes, ...] = (
    b"new", b"compile", b"redirect", b"set", b"edit", b"solve",
    b"linecode", b"vsource", b"circuit", b"transformer", b"loadshape",
)


def _not_opendss_script(path: str) -> str | None:
    """看起来不是 OpenDSS 脚本时返回原因，否则返回 None。

    为什么要有这道检查（真机实测）：上传一个不是 OpenDSS 语法的文件后，COM 引擎
    **既不报错也不返回**，一路挂到 45s 超时才由保护收场 —— 用户白等近一分钟，
    期间还要丢弃并重建引擎实例。这类失败本该发生在**送引擎之前**（"早期失败"）。

    判据刻意极保守：**只要文本里提到任一关键字就放行**（哪怕它只是出现在一句注释里，
    比如"没有 New，也没有 Redirect"）。这是有意的 —— 本函数的目标是挡住"完全不像算例"
    的文件（空文件、随机二进制、纯中文说明），**不是**做语法校验；真正的语法错误交给
    引擎去报。宁可漏判（多花一次超时），不可误伤（把合法算例挡在门外）。

    （实测教训：第一版验证脚本的"垃圾文件"里写了"没有 New，没有 Redirect"，
    于是那两个英文词触发了放行、白跑一轮 —— 判据没错，是测试样本没选干净。）
    """
    try:
        with open(path, "rb") as f:
            blob = f.read(512 * 1024)
    except Exception:  # noqa: BLE001 - 读不了就不管，交给后面的流程
        return None
    if not blob.strip():
        return "文件是空的（0 字节）"
    # 去掉 NUL 再比对：兼容 UTF-16 编码的算例（关键字是 ASCII，在 UTF-16 里字节间夹 \x00）
    probe = blob.replace(b"\x00", b"").lower()
    if any(h in probe for h in _OPENSS_SCRIPT_HINTS):
        return None
    return f"内容里没有任何 OpenDSS 关键字（共 {len(blob)} 字节）"


def _fresh_case_master(master_path: str | None) -> str | None:
    """引用算例前校验转换产物新鲜度：过期（转换器已升级）则自动重建。

    背景：上传算例的转换结果会被长期复用，"换算例"直接拿旧产物去算。
    转换器改进后老算例不会重新转换 → 修复对它无效且毫无提示
    （实测 case12/case09 拿到的是 8/30、9/9 生成的 `pu=1`、`Model=1` 旧产物）。

    失败一律降级为"沿用旧产物"，绝不因为校验/重建失败而让算例不可用。

    ⚠️ 但"沿用旧产物"有一个例外：产物含**已知会让引擎卡死**的非法属性时，
    宁可让这次调用明确失败并说清原因，也不能把盘上那个文件再送进 compile ——
    后者会让整个应用失去响应数分钟（实测 120s 硬超时 × 连续 4 个任务）。
    """
    if not master_path:
        return master_path
    try:
        cfg = AgentConfig()
        if not getattr(cfg, "conversion_cache_enabled", True):
            return master_path
        refreshed, how = conversion_cache.refresh_master_if_stale(
            master_path, force=bool(getattr(cfg, "conversion_force_refresh", False))
        )
        if how.startswith("rebuilt") or how == "no-meta-rebuilt":
            import logging

            logging.getLogger(__name__).warning(
                "[case] 转换产物已按新版转换器重建（%s）：%s", how, refreshed
            )
        target = refreshed or master_path
    except Exception:  # noqa: BLE001 - 新鲜度校验绝不阻塞算例加载
        target = master_path

    # ---- 内容级兜底（两道并列，缺一不可）----
    # ① 这到底是不是 OpenDSS 脚本？不是就别送引擎 —— 实测垃圾文件会让引擎挂到超时。
    _why_not_script = _not_opendss_script(target)
    if _why_not_script:
        import logging

        logging.getLogger(__name__).error(
            "[case] 疑似非 OpenDSS 脚本，拒绝送引擎：%s（%s）", target, _why_not_script
        )
        raise CaseProductRejected(
            status_code=422,
            detail=(
                "这个文件看起来不是 OpenDSS 算例，已拒绝送入计算引擎"
                "（否则会白等数十秒直到超时）。\n"
                f"- {_why_not_script}\n\n"
                "OpenDSS 脚本至少应包含 New / Compile / Redirect / Set 之一，"
                "例如以 `New Circuit.名称 basekv=10` 起头。"
            ),
        )

    # ② 版本戳不可信时，直接看文件本身有没有致命属性
    problems = _illegal_line_props(target)
    if problems:
        import logging

        logging.getLogger(__name__).error(
            "[case] 产物含非法元素属性，拒绝送引擎：%s\n  - %s",
            target,
            "\n  - ".join(problems),
        )
        raise CaseProductRejected(
            status_code=422,
            detail=(
                "算例产物含非法元素属性，已拒绝送入计算引擎（继续执行会让引擎卡死）。\n"
                + "\n".join(f"- {p}" for p in problems[:5])
                + "\n\n处理办法：重新上传该算例的源文件（会自动按当前转换器重建产物）。"
            ),
        )
    return target


def _uploaded_model_entries(root_dir: str) -> list[str]:
    """枚举新完整filename命名空间及旧平铺布局；新布局优先，同源旧副本不抢匹配。"""
    entries = os.listdir(root_dir)
    namespace = conversion_cache.CONVERTED_DIRNAME
    converted = os.path.join(root_dir, namespace)
    result = []
    if os.path.isdir(converted):
        result.extend(os.path.join(namespace, name) for name in os.listdir(converted))
    result.extend(name for name in entries if name != namespace
                  and not name.startswith((".conversion-rebuild-", ".zip-rebuild-")))
    return result


def _resolve_case_name_to_upload(name: str, session_id: str) -> str | None:
    """把「用户点名的算例」解析为本机已存在的 Master.dss 绝对路径。

    背景（真实 bug）：上传算例时只有那条消息携带绝对路径，之后用户按名字提
    ——「用 case17 再算一遍」「算一下 case11」—— 拿到的 case_ref 只是个字符串
    名字。而 load_case_bundle 只在 cases_root 下找这个名字，上传目录不在其内，
    于是抛 DATA_UNAVAILABLE（或更糟：被静默换成 ieee33 去算）。
    这里在 uploads 里回查同名目录/文件，把名字还原成绝对路径。

    返回 None 表示"我解析不了，交给下游按原逻辑处理"。
    """
    # 内置算例 / 已经是路径的情况不干预
    if not name or _is_known_builtin_case(name):
        return None
    if os.path.isfile(name) or os.sep in name or "/" in name:
        return None

    key = name.strip().lower()
    uploads = getattr(store, "UPLOADS_DIR", None)
    if not uploads or not os.path.isdir(uploads):
        return None

    # 当前会话优先，其次其它会话（新的在前）
    search_dirs: list[str] = []
    sess_dir = os.path.join(uploads, session_id)
    if os.path.isdir(sess_dir):
        search_dirs.append(sess_dir)
    others = []
    for entry in os.listdir(uploads):
        full = os.path.join(uploads, entry)
        if os.path.isdir(full) and full not in search_dirs:
            others.append(full)
    # 按修改时间倒序：最近上传的优先匹配
    others.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    search_dirs.extend(others)

    # 先做精确同名，再做前缀/包含匹配
    for exact in (True, False):
        for root_dir in search_dirs:
            try:
                entries = _uploaded_model_entries(root_dir)
            except OSError:
                continue
            matches: dict[str, tuple[str, str]] = {}
            for entry in entries:
                full = os.path.join(root_dir, entry)
                if os.path.isdir(full):
                    master = _find_master_in(full)
                elif os.path.isfile(full) and full.lower().endswith(".dss"):
                    master = full
                else:
                    continue
                if not master:
                    continue
                meta = conversion_cache.read_meta(os.path.dirname(master)) or {}
                source = str(meta.get("source") or "")
                source_name = os.path.basename(source)
                basename = os.path.basename(entry)
                names = {basename.lower(), os.path.splitext(basename)[0].lower()}
                if source_name:
                    names.update((source_name.lower(), os.path.splitext(source_name)[0].lower()))
                source_kind = meta.get("source_kind")
                dependencies = meta.get("source_dependencies") or []
                if not source_kind and source and os.path.exists(source):
                    # 旧目录按源文件推断组件束，和新的canonical根按同一身份去重。
                    state = conversion_cache._source_state(source)
                    source_kind, dependencies = state["source_kind"], state["source_dependencies"]
                if source_kind in {"csv_bundle", "csv_directory"}:
                    for dependency in dependencies:
                        component = os.path.basename(str(dependency.get("path") or "")).lower()
                        if component:
                            names.update((component, os.path.splitext(component)[0]))
                hit = key in names if exact else any(key in alias or alias in key for alias in names if alias)
                if not hit:
                    continue
                identity_source = os.path.dirname(source) if source_kind == "csv_bundle" else source
                identity = os.path.normcase(os.path.abspath(identity_source or master))
                if source_kind in {"csv_bundle", "csv_directory"}:
                    identity = "csv-bundle:" + identity
                matches.setdefault(identity, (master, source_name or basename))
            if len(matches) > 1:
                labels = "、".join(label for _master, label in matches.values())
                raise CaseProductRejected(status_code=422, detail=(
                    f"算例名 {name} 对应多个已上传模型：{labels}。"
                    "请带上完整文件名和扩展名指定要计算的算例。"
                ))
            if matches:
                full, _label = next(iter(matches.values()))
                return _fresh_case_master(full)
    return None


def _list_uploaded_case_names(session_id: str, limit: int = 12) -> list[str]:
    """列出本机已上传的算例名（按最近上传排序），用于找不到算例时给用户提示。"""
    uploads = getattr(store, "UPLOADS_DIR", None)
    if not uploads or not os.path.isdir(uploads):
        return []
    found: list[tuple[float, str]] = []
    try:
        for sess in os.listdir(uploads):
            sess_dir = os.path.join(uploads, sess)
            if not os.path.isdir(sess_dir):
                continue
            for entry in _uploaded_model_entries(sess_dir):
                full = os.path.join(sess_dir, entry)
                basename = os.path.basename(entry)
                name = os.path.splitext(basename)[0] if os.path.isfile(full) else basename
                ext = os.path.splitext(entry)[1].lower() if os.path.isfile(full) else ""
                if os.path.isfile(full):
                    if ext not in (".dss", ".zip", ".xlsx", ".csv", ".json", ".m"):
                        continue
                    # 非DSS格式保留完整filename，让同stem的不同格式可明确区分。
                    if ext != ".dss":
                        name = basename
                else:
                    master = _find_master_in(full)
                    if not master:
                        continue
                    meta = conversion_cache.read_meta(os.path.dirname(master)) or {}
                    name = os.path.basename(str(meta.get("source") or "")) or basename
                if name in found or name in [n for _, n in found]:
                    continue
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    mtime = 0.0
                if name and name not in [n for _, n in found]:
                    found.append((mtime, name))
    except OSError:
        return []
    found.sort(reverse=True)
    return [n for _, n in found[:limit]]


#: 内置算例名的**唯一定义处**。此前它散落在 `_extract_case_name_from_text`
#: 与 `_is_known_builtin_case` 两处硬编码列表里 —— 一旦加了新内置算例而漏改一处，
#: 就会出现"提取得到但判为未知名"这类自相矛盾的路径。现在统一从这里取。
_BUILTIN_CASE_NAMES: tuple[str, ...] = (
    "case4", "case_tx", "case_nonconv", "case_broken", "bus_reference_probe",
    "pvsystem_probe", "storage_discharge_probe",
)


def _is_known_builtin_case(name: str) -> bool:
    """判断算例名是否为内置可用算例。"""
    return name in _BUILTIN_CASE_NAMES and os.path.isfile(
        os.path.join(AgentConfig().cases_root, name, "Master.dss")
    )


def _case_candidates(session_id: str) -> list[dict[str, str]]:
    """汇总"本机真实可用"的算例清单，作为算例消歧的**白名单**。

    ⚠️ 这里放进去的每一个名字，模型都可能原样回给用户。所以只准放**真的能算**的：
    放一个不存在的名字进去，模型就会把它当成"可用算例"推荐出来，
    比不追问更糟。因此三类来源都要先验证文件确实存在（内置名除外，它由
    `cases_root` 解析）。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(name: str, kind: str, hint: str) -> None:
        n = (name or "").strip()
        if not n or n.lower() in seen:
            return
        seen.add(n.lower())
        out.append({"id": n, "kind": kind, "hint": hint})

    # 1) 内置算例（与 _is_known_builtin_case 同源，避免两处白名单漂移）
    for n in _BUILTIN_CASE_NAMES:
        if _is_known_builtin_case(n):
            _add(n, "builtin", "内置算例")

    # 2) cases_root 下真实存在 Master.dss 的目录（用户可能自己放进去的算例）
    try:
        root = AgentConfig().cases_root
        if os.path.isdir(root):
            for n in sorted(os.listdir(root)):
                if os.path.isfile(os.path.join(root, n, "Master.dss")):
                    _add(n, "builtin_dir", "cases 目录下的算例")
    except Exception:  # noqa: BLE001 - 清单构建失败不该影响原有报错路径
        pass

    # 3) 本机已上传过的算例
    try:
        for n in _list_uploaded_case_names(session_id):
            _add(n, "uploaded", "你之前上传过")
    except Exception:  # noqa: BLE001
        pass

    return out


#: 意图层给出的原因码 → 人话。喂给模型当作"要问什么"的依据。
_CLARIFY_REASON_CN: dict[str, str] = {
    "low_confidence": "系统对这句话是要「重新计算」还是「查已有结果」把握不足",
    "missing_case": "判断出用户想执行计算，但没有指明用哪个算例",
    "unknown_intent": "这句话的意图没能对应到系统支持的任何一类（计算/查询/知识/闲聊）",
}


def _clarify_reply(content: str, ir: Any, session_id: str) -> str:
    """意图不明确时的追问：按"缺哪一项"问，问不成就退回既有固定文案。

    ⚠️ 既有固定文案是**兜底**而不是废弃品：模型不可用 / 超时 / 编造算例名时
    必须落到它上面 —— "要不要重算"这种关键确认一旦变成空话，用户就只能瞎猜。
    """
    fallback = (
        "我想确认一下你的意图，避免做错事：\n"
        "- 如果想**重新计算**，请说「重新计算」或先上传算例；\n"
        "- 如果想**查上次结果**，直接问具体指标即可（如「最低电压是多少」）。"
    )
    reason = getattr(ir, "clarify_reason", "") or "unknown_intent"
    reason_cn = _CLARIFY_REASON_CN.get(reason, "意图不明确")
    try:
        cfg = AgentConfig()
        if str(getattr(cfg, "intent_mode", "hybrid") or "hybrid").lower() == "off":
            return fallback
        from ..runtime.model_client import ModelClient

        # 只有"缺算例"这一类才需要候选清单——其余情形给清单反而会诱导模型
        # 把话题往算例上带（比如用户其实只是在寒暄）。
        cands = _case_candidates(session_id) if reason == "missing_case" else []
        text, _why = ModelClient(cfg).ask_clarify(
            content, reason_cn, getattr(ir, "slots", {}) or {}, cands
        )
        return text or fallback
    except Exception:  # noqa: BLE001 - 澄清层任何异常都不许外抛
        return fallback


def _case_not_found_reply(session_id: str, content: str, name: str) -> str:
    """用户点名的算例在本机不存在时的回复。

    先让模型在**真实可用清单**里找最近的一个，再决定措辞。设计取舍：
    **模型选中的算例不会自动拿去计算**。既有原则写得很清楚 ——
    "用户点名了算例就必须算那个，找不到要显式报错，不许悄悄换别的算例来算"，
    因为那等于拿另一个算例的结果冒充用户要的结果（伪造证据）。
    所以这里把匹配结果转成一句**明确的确认问句**："你要找的是不是 X"，
    由用户点头后再算。模型在这里做的是"把话问准"，不是"替用户改主意"。
    """
    known = _list_uploaded_case_names(session_id)
    cands = _case_candidates(session_id)
    picked, conf, question, why = "", 0.0, "", "跳过"
    try:
        cfg = AgentConfig()
        if str(getattr(cfg, "intent_mode", "hybrid") or "hybrid").lower() != "off":
            from ..runtime.model_client import ModelClient

            picked, conf, question, why = ModelClient(cfg).disambiguate_case(content, cands)
    except Exception:  # noqa: BLE001 - 消歧失败就用下面的确定性文案
        pass

    # 只列真实可用的候选，避免给出"看起来可选、其实点不了"的名字
    listing = "、".join(f"`{c['id']}`" for c in cands[:10])
    lines = [f"你提到的算例 `{name}` 在本机找不到，我没有用别的算例代替来算。", ""]
    if picked:
        kind_cn = next((c.get("hint", "") for c in cands if c.get("id") == picked), "")
        lines.append(
            f"你要找的是不是 `{picked}`"
            + (f"（{kind_cn}）" if kind_cn else "")
            + f"？如果是，回一句「算 {picked}」我就开始。"
        )
    elif question:
        lines.append(question)
    if listing:
        lines.append(f"本机可用的算例：{listing}")
    if known:
        lines.append("（其中 `uploaded` 那批是你之前上传过的。）")
    lines.append("也可以重新上传 `.xlsx`、`.csv`、`.json`、`.m`、`.dss` 或 `.zip` 算例文件。")
    return "\n".join(lines)


# 喂给意图层的每轮文字上限。上下文是给模型"判断"用的，不是让它"复述"——
# 太长既贵又容易被模型当成素材抄进回答里。
_RECENT_TURN_CHARS = 160


def _get_session_context(session: store.ChatSession) -> dict[str, Any]:
    """从历史消息中提取可用于上下文继承的信息。"""
    ctx: dict[str, Any] = {
        "session_id": session.id if session else None,
        "last_task_id": None,
        "last_task_case_ref": None,
        "last_task_status": None,
        "last_success": False,
        "last_attachments": [],
        "message_count": 0,
        "recent_turns": [],
    }
    if not session or not session.messages:
        return ctx

    ctx["message_count"] = len(session.messages)

    # 最近的上传附件
    for msg in reversed(session.messages):
        if msg.role == "user" and msg.attachments:
            ctx["last_attachments"] = msg.attachments
            break

    # 最近一次 assistant 的计算任务 + 最近一次成功任务。
    # 失败任务（如 non_converged）只是最新状态，不应让历史问答“失忆”：
    # 查询类问题应回落到最近一次成功任务的产物。
    for msg in reversed(session.messages):
        if msg.role == "assistant" and msg.task_id:
            if ctx["last_task_id"] is None:
                ctx["last_task_id"] = msg.task_id
                ctx["last_task_case_ref"] = msg.task_case_ref
                ctx["last_task_status"] = msg.task_status
                ctx["last_success"] = msg.task_status in ("success", "converged")
            if msg.task_status in ("success", "converged"):
                ctx["last_success_task_id"] = msg.task_id
                ctx["last_success_case_ref"] = msg.task_case_ref
                break

    # ---- 最近几轮的「问答摘要」：供意图层消解指代（P2-a）----
    # 意图层此前只能看到 has_history_success 这类**布尔量**，看不到"上一轮究竟问了什么"，
    # 于是"它的网损呢""那这个算例有几台变压器""刚才那条母线电压正常吗"只能靠猜 ——
    # 这是"指代消解"一直缺的那块拼图。
    #
    # ⚠️ 本轮的用户消息在这一步**之前**就已写入会话（见 chat 接口里的 add_message），
    # 所以要把它排除：上下文描述的是"之前发生了什么"，不含本轮，否则模型容易把
    # 本轮消息误当成历史，反而判错。
    msgs = list(session.messages)
    if msgs and msgs[-1].role == "user":
        msgs = msgs[:-1]
    turns: list[dict[str, str]] = []
    for msg in msgs:
        if msg.role not in ("user", "assistant"):
            continue
        text = (msg.content or "").strip()
        if not text:
            continue
        turns.append({"role": msg.role, "text": text[:_RECENT_TURN_CHARS]})
    # 只留最近两轮（4 条）：够消解指代，又不会把上下文撑大
    ctx["recent_turns"] = turns[-4:]

    return ctx


# 强意图：只要出现这些词，优先认为用户想执行潮流计算（即使句子里带了“你好”）
_POWERFLOW_STRONG = [
    "计算", "跑", "执行", "求解", "潮流", "配电网",
    "ieee", "master", "dss", "重算", "再算", "帮我算", "帮我跑",
    "分析一下", "算一下", "新建算例", "使用算例",
    # 补全常见口语说法。真实缺陷：「用 X 算一遍」此前既不命中 STRONG
    # 也不含任何技术指标词（WEAK），会被误判成闲聊，任务根本不触发。
    "算一遍", "算一算", "跑一遍", "跑一次", "再跑", "复算", "验算", "算算",
]

# 弱意图：单独出现不一定计算，但如果还提到电压/网损/收敛等技术指标，则计算
_POWERFLOW_WEAK = ["电压", "网损", "负载", "收敛", "功率", "损耗", "迭代"]

# 常见问候/闲聊，不触发计算
_CASUAL_KEYWORDS = ["你好", "您好", "hello", "hi", "在吗", "谢谢", "再见"]


def _is_pure_casual(content: str) -> bool:
    """是否仅为问候/闲聊（不含计算意图）。"""
    lower = content.lower().strip()
    # 去掉标点后再判
    cleaned = "".join(c for c in lower if c.isalnum() or c.isspace())
    # 只有问候词和少量 filler
    if cleaned in ("你好", "您好", "hello", "hi", "在吗", "谢谢", "再见", "谢了"):
        return True
    # 如果包含强计算意图，就不是纯闲聊
    for w in _POWERFLOW_STRONG:
        if w in lower:
            return False
    return False


def _has_strong_intent(content_lower: str) -> bool:
    return any(w in content_lower for w in _POWERFLOW_STRONG)


def _should_run_powerflow(content: str, attachments: list[dict[str, Any]]) -> bool:
    """判断用户是否想执行潮流计算（单条消息层面）。"""
    # 上传了算例文件，默认想计算
    if attachments:
        for att in attachments:
            filename = att.get("filename", "").lower()
            if filename.endswith(".dss") or os.path.isdir(att.get("path", "")):
                return True
    lower = content.lower()

    # 明显闲聊 / 否定计算：不触发
    if any(w in lower for w in _CASUAL_BLOCK_KEYWORDS):
        return False
    if any(w in lower for w in _NEGATION_KEYWORDS):
        return False

    # 强意图优先
    if _has_strong_intent(lower):
        return True

    # 修改求解参数，也应触发重新计算
    if _is_parameter_change(content):
        return True

    # 弱意图 + 技术指标词，认为想计算
    weak_hit = any(w in lower for w in _POWERFLOW_WEAK)
    return weak_hit


# ---------------------------------------------------------------------------
# 能力询问（capability inquiry）：用户在问"你能不能做到 X"，而不是要执行 X。
# 例：「如果算例出错导致不收敛，能具体给出哪里出错吗」
# 这类问题常包含"收敛""电压"等技术词，会被 _POWERFLOW_WEAK / _CONTEXT_QUERY_KEYWORDS
# 字面命中而误触发重算，因此必须在路由最前面单独拦截。
# ---------------------------------------------------------------------------
_CAPABILITY_HYPOTHETICAL = ["如果", "假如", "假设", "一旦", "要是", "万一", "若是", "若"]  # 假设/条件
_CAPABILITY_MODAL = [  # 能力询问
    "能不能", "能否", "可不可以", "可以吗", "会不会", "能具体", "能定位", "能指出",
    "能给出", "能告诉", "能说明", "能查到", "支持吗", "支持什么", "有什么能力",
    "会怎么处理", "怎么处理", "如何处理", "怎样处理", "怎么定位",
    "如何定位", "怎么判断", "如何判断", "怎么发现", "如何发现", "会怎么",
    # "能/可以 + 动词" 形态，如「你能处理三相不平衡吗」
    "能处理", "能算", "能做", "能支持", "能识别", "能判断", "能发现", "能看出",
    "可以处理", "可以算", "可以做", "能做什么", "会什么",
]
# 出现这些词说明用户是真的要执行/修改，不算能力询问
_CAPABILITY_EXCLUDE = [
    "重算", "再算", "再跑", "跑一遍", "算一遍", "重新计算", "重新跑",
    "容差", "迭代次数", "最大迭代", "tol", "改成", "改为", "设置为",
    "帮我算", "帮我跑", "执行", "求解一下",
]


def _is_capability_inquiry(content: str) -> bool:
    """是否为"能力询问"：问系统能不能做到某事，而不是要求执行某事。"""
    lower = content.lower().strip()
    if not lower:
        return False
    # 真的要执行/改参数 → 不是能力询问
    if any(w in lower for w in _CAPABILITY_EXCLUDE):
        return False
    # 去空白后的紧凑文本，避免「Q 限值」「N - 1」这类带空格写法漏匹配
    compact = "".join(lower.split())
    if any(w in compact for w in _CAPABILITY_EXCLUDE):
        return False

    has_hypo = any(w in lower for w in _CAPABILITY_HYPOTHETICAL)
    has_modal = any(w in lower for w in _CAPABILITY_MODAL)
    # 假设句 + 技术/故障词（如"出错""不收敛""失败"）也视为能力询问
    fault_words = ["出错", "不收敛", "失败", "报错", "异常", "错误", "坏了", "有问题"]
    has_fault = any(w in compact for w in fault_words)
    if has_modal or (has_hypo and has_fault):
        return True
    # 「有…能力吗 / 么」形态：如「你有三相不平衡能力么」。
    # 该形态是明确的"问能不能做"，且已排除执行/改参数语义，可安全判定。
    if "能力" in compact and lower.endswith(("吗", "么", "?", "？")):
        return True
    # 「支持 X 吗」「支持 X 么」形态：支持时序吗 / 支持三相吗
    if lower.endswith(("吗", "么", "?")) and "支持" in lower:
        return True
    # 「支持哪些 X」这种**枚举式**问法：实测「你支持哪些算例格式」被判成非能力询问，
    # 于是答非所问。只补"支持哪些"——"有哪些"太宽（"有哪些越限母线"是结果查询），不能收。
    if "支持哪些" in compact:
        return True
    return False


def _answer_capability_inquiry(content: str, ctx: dict[str, Any]) -> str | None:
    """回答能力询问，不触发潮流计算。

    2026-10-01 收敛（P2-c）：此前这里是 5 段手写的 `if 关键词` 分支 + 一段硬编码总览，
    同一条能力事实在代码里散成好几处；而且**清单之外的主题根本问不到** —— 用户问
    "支持储能吗""能算短路电流吗"，拿到的是一份**不含该主题**的总览，
    既没回答问题，也没说清到底是"没做"还是"没登记"。

    现在能力事实统一由 `domain.capabilities` 提供（状态一律取自 CAPABILITY_STATUS，
    依据写在各条目的 evidence 里）。本函数只保留一件清单管不到的事：
    **结合本次会话状态**给出的补充说明。
    """
    from ..domain import capabilities as caps

    ans = caps.answer(content)
    entry = caps.match(content)
    if entry is None:
        return ans

    # 能力清单讲的是"系统能做什么"，与"你这次算过什么"无关；
    # 下面这两段补充都要读**本次会话的产物**，所以留在这一层，不混进清单。
    has_hist = bool(ctx.get("last_task_id")) and bool(ctx.get("last_success"))
    if not has_hist:
        return ans

    if entry.key == "diagnosis":
        ans += (
            "\n\n补充：你前面那个任务是**已收敛**的，"
            "所以「算例出错导致不收敛」这个假设在它身上没有发生，"
            "不能用它的结果来演示诊断能力。"
        )
    elif entry.key == "three_phase":
        art = _load_history_artifact(ctx, "powerflow_result.json") or {}
        ub = art.get("unbalance_summary") or {}
        if ub.get("buses_assessed") and ub.get("worst_bus"):
            task_id = ctx.get("last_success_task_id") or ctx.get("last_task_id")
            ans += (
                f"\n\n补充：你最近一次任务（`{task_id}`）已有分相结果："
                f"**{ub['worst_bus']} 的 ε = "
                f"{float(ub['worst_unbalance_pct']):.3f}%**，"
                f"参与评估母线 {ub.get('buses_assessed')} 条。"
            )
    return ans



# 上下文重跑关键词
_CONTEXT_RERUN_KEYWORDS = ["重算", "再算", "重新", "再跑", "跑一下", "算一次", "跑一次"]
    # 上下文查询关键词（需配合历史成功任务）
_CONTEXT_QUERY_KEYWORDS = ["网损", "电压", "负载", "损耗", "收敛", "功率", "结果", "报告", "怎么样", "多少", "如何", "安全", "可行", "热过载", "过载", "越限", "q限值", "无功限值", "qmax", "qmin", "n-1", "N-1", "停运", "开断"]


def _is_contextual_powerflow_request(content: str, ctx: dict[str, Any]) -> tuple[bool, str]:
    """判断当前消息是否依赖上下文应触发潮流计算。返回 (是否触发, 原因)。"""
    lower = content.lower().strip()

    # 能力询问（"你能不能做到 X"）优先拦截：
    # 这类问法常含"收敛""电压"等技术词，会被下面的 _CONTEXT_QUERY_KEYWORDS 字面命中。
    if _is_capability_inquiry(content):
        return False, ""

    # 必须已有历史成功任务或附件
    if ctx["message_count"] == 0:
        return False, ""

    # 明显闲聊：不触发计算
    if any(w in lower for w in _CASUAL_BLOCK_KEYWORDS):
        return False, ""

    # 用户明确否定计算
    if any(w in lower for w in _NEGATION_KEYWORDS):
        return False, ""

    has_history_task = bool(ctx.get("last_task_id")) and ctx.get("last_success")
    has_history_attachments = bool(ctx.get("last_attachments"))

    if not has_history_task and not has_history_attachments:
        return False, ""

    # 明确重跑
    if any(w in lower for w in _CONTEXT_RERUN_KEYWORDS):
        return True, "上下文：用户要求重跑/再算"

    # 修改求解参数：基于同一算例重新计算
    if _is_parameter_change(content):
        if has_history_task or has_history_attachments:
            return True, "上下文：用户修改求解参数并重新计算"

    # 带了技术指标词，且历史有成功任务或附件，认为是针对同一算例的继续提问
    if any(w in lower for w in _CONTEXT_QUERY_KEYWORDS):
        if has_history_task or has_history_attachments:
            return True, "上下文：用户继续询问同一算例指标"

    return False, ""


def _casual_reply(content: str, session: store.ChatSession | None = None) -> str:
    lower = content.lower()
    has_history = session and len(session.messages) > 2

    if any(w in lower for w in ["你好", "您好", "hello", "hi"]):
        if has_history:
            return "又见面了，继续聊配电网潮流吗？"
        return "你好！我是天枢一号。你可以让我计算内置合成算例（如 case4），或者上传自己的 DSS 算例文件/zip 压缩包。"
    if "谢谢" in lower or "谢了" in lower:
        return "不客气，有问题随时叫我。"
    if "再见" in lower or "拜拜" in lower:
        return "再见！需要时再打开页面继续聊。"
    if "天气" in lower or "吃饭" in lower or "几点" in lower:
        return "这个我暂时回答不了。我只会做配电网潮流计算，你可以说“计算 IEEE33”或上传算例文件。"
    if has_history:
        return "没太懂你的意思。需要计算哪个算例，或者上传 DSS 文件，我直接帮你跑。"
    return "我不太明白你的意思。你可以直接说“计算 IEEE33 配电网”或上传算例文件，我会帮你跑潮流并返回报告。"


class CreateSessionRequest(BaseModel):
    title: str = "新聊天"


class SendMessageRequest(BaseModel):
    content: str


@router.get("/api/chat/sessions")
def list_sessions() -> JSONResponse:
    store.init_db()
    sessions = store.list_sessions()
    return JSONResponse({"sessions": [s.to_dict() for s in sessions]})


@router.post("/api/chat/sessions")
def create_session(payload: CreateSessionRequest) -> JSONResponse:
    store.init_db()
    session = store.create_session(payload.title)
    return JSONResponse(session.to_dict())


@router.get("/api/chat/sessions/{session_id}")
def get_session(session_id: str) -> JSONResponse:
    store.init_db()
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return JSONResponse({**session.to_dict(), "batches": batch_store.list_for_session(session_id)})


@router.delete("/api/chat/sessions/{session_id}")
def delete_session(session_id: str) -> JSONResponse:
    store.init_db()
    batches = batch_store.list_for_session(session_id)
    for batch in batches:
        batch_scheduler.current(_execute_batch_item).cancel(session_id, batch["id"])
    store.delete_session(session_id)
    for batch in batches:
        batch_scheduler.remove_inputs(batch["id"])
    upload_dir = store.ensure_upload_dir(session_id)
    if os.path.isdir(upload_dir):
        shutil.rmtree(upload_dir, ignore_errors=True)
    return JSONResponse({"deleted": session_id})


def _require_batch(session_id, batch_id):
    store.init_db()
    batch = batch_store.get(session_id, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


@router.get("/api/chat/sessions/{session_id}/batches")
def list_batches(session_id: str) -> JSONResponse:
    store.init_db()
    if not store.get_session(session_id, include_messages=False):
        raise HTTPException(status_code=404, detail="会话不存在")
    return JSONResponse({"batches": batch_store.list_for_session(session_id)})


@router.get("/api/chat/sessions/{session_id}/batches/{batch_id}")
def get_batch(session_id: str, batch_id: str) -> JSONResponse:
    return JSONResponse(_require_batch(session_id, batch_id))


def _batch_action(session_id, batch_id, action, item_id=None):
    _require_batch(session_id, batch_id)
    scheduler = batch_scheduler.current(_execute_batch_item)
    try:
        if action == "cancel":
            scheduler.cancel(session_id, batch_id, item_id)
        elif action == "resume":
            batch_store.resume(session_id, batch_id)
        else:
            batch_store.retry_failed(session_id, batch_id)
        scheduler.notify()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次或项目不存在") from exc
    except batch_store.BatchConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(batch_store.get(session_id, batch_id))


@router.post("/api/chat/sessions/{session_id}/batches/{batch_id}/cancel")
def cancel_batch(session_id: str, batch_id: str) -> JSONResponse:
    return _batch_action(session_id, batch_id, "cancel")


@router.post("/api/chat/sessions/{session_id}/batches/{batch_id}/items/{item_id}/cancel")
def cancel_batch_item(session_id: str, batch_id: str, item_id: str) -> JSONResponse:
    return _batch_action(session_id, batch_id, "cancel", item_id)


@router.post("/api/chat/sessions/{session_id}/batches/{batch_id}/resume")
def resume_batch(session_id: str, batch_id: str) -> JSONResponse:
    return _batch_action(session_id, batch_id, "resume")


@router.post("/api/chat/sessions/{session_id}/batches/{batch_id}/retry-failed")
def retry_batch_failed(session_id: str, batch_id: str) -> JSONResponse:
    return _batch_action(session_id, batch_id, "retry")


@router.post("/api/chat/sessions/{session_id}/messages")
def send_message(
    session_id: str,
    content: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    store.init_db()
    session = store.get_session(session_id, include_messages=False)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    content = (content or "").strip()
    attachments: list[dict[str, Any]] = []
    if files:
        try:
            attachments = _process_uploaded_files(session_id, files)
        except HTTPException as exc:
            # 转换阶段检出的结构错误（重复母线/孤岛/零阻抗等）：作为 assistant
            # 诊断消息保存在会话中，而不是 HTTP 400 让前端只看到一个失败弹窗。
            user_msg = store.add_message(session_id, "user", content or f"上传了 {len(files)} 个文件")
            detail_text = str(exc.detail)
            reply = f"文件无法作为算例使用：{detail_text}"
            # ⚠️ 早期失败也必须留下**可回看的执行轨迹**。否则界面上思考流全空，
            # 用户只看到一行错误，看不到"读到了什么、判定依据是什么"。
            # 轨迹与正常任务走同一条回看通道，保证实时与回看不会分叉。
            diag_id = _write_diagnostic_trace(
                session_id,
                [os.path.basename(f.filename or "upload") for f in files],
                detail_text,
            )
            assistant_msg = store.add_message(
                session_id, "assistant", reply,
                task_id=diag_id, task_status="failed",
            )
            if session.title == "新聊天":
                store.update_session_title(session_id, (content[:16] or "文件诊断"))
            return JSONResponse({
                "user_message": user_msg.to_dict(),
                "assistant_message": assistant_msg.to_dict(),
                "task_id": diag_id,
            })

    if not content and not attachments:
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 用于会话标题的摘要文本
    title_text = content[:16] or (attachments[0]["filename"] if attachments else "")

    # 保存用户消息（文字与附件合并为一条）
    user_msg = store.add_message(session_id, "user", content, attachments=attachments)

    # 获取当前会话完整上下文
    full_session = store.get_session(session_id, include_messages=True)
    ctx = _get_session_context(full_session)
    last_attachments = ctx.get("last_attachments", [])

    # 意图判断：单条消息意图 + 上下文依赖意图
    # 注意：_should_run_powerflow 只应基于本次消息自身内容/附件判断，不能基于历史附件
    single_intent = _should_run_powerflow(content, attachments)
    contextual_intent, contextual_reason = _is_contextual_powerflow_request(content, ctx)
    # 新上传的文件本身就是本轮计算依据，不要误显示“沿用前面对话”。
    if attachments:
        contextual_intent = False

    # 优先处理"基于历史结果的查询/比较"，避免重复跑潮流
    # 如果本次消息带有新附件，优先按新算例处理，不走历史查询
    _intent_metric: str | None = None
    _intent_slots: dict[str, Any] = {}

    if not attachments:
        # 精确的已验证历史读数无需模型判意图；新计算仍走完整求解/复现性链。
        from .history_fastpath import answer_verified_history

        history_answer = answer_verified_history(content, ctx)
        if history_answer is not None:
            am = store.add_message(
                session_id, "assistant", history_answer.text,
                report_url=history_answer.report_url,
            )
            return JSONResponse({
                "user_message": user_msg.to_dict(), "assistant_message": am.to_dict(),
                "answer_source": "verified_history", "source_task_id": history_answer.task_id,
            })
        # ---- 意图理解层（语义优先，规则链降级为兜底）----
        # 目标：问什么答什么；查询类绝不触发重算；拿不准就反问，而不是默认重算。
        cfg_intent = AgentConfig()
        if str(getattr(cfg_intent, "intent_mode", "hybrid") or "hybrid").lower() != "off":
            try:
                from ..runtime.intent import (
                    ACTION_CASUAL,
                    ACTION_CAPABILITY,
                    ACTION_CLARIFY,
                    ACTION_COMPUTE,
                    ACTION_HISTORY,
                    ACTION_KNOWLEDGE,
                    ACTION_REJECT,
                    decide,
                )

                action, ir = decide(content, ctx, cfg_intent)
                # 意图层已判出「问的是哪个指标」，这里收下来传给回答层 ——
                # 旧实现把它丢掉了，下游只好用关键词重猜（已修正）。
                _intent_slots = dict(ir.slots or {})
                _intent_metric = _intent_slots.get("metric")
                # 可观测：意图决策写入日志，便于回归与纠错
                try:
                    _rq = _intent_slots.get("resolved_query")
                    print(
                        f"[intent] {content[:40]!r} -> {action} ({ir.intent}, "
                        f"conf={ir.confidence:.2f}, src={ir.source}) "
                        # 指代消解结果只在日志里露头：它不进任何取数路径（见 prompts 里的约束）
                        f"{f'resolved={_rq!r} ' if _rq else ''}{ir.reason[:60]}"
                    )
                except Exception:
                    pass

                def _reply(text: str, title: str) -> JSONResponse:
                    am = store.add_message(session_id, "assistant", text)
                    if session.title == "新聊天":
                        store.update_session_title(session_id, title_text or title)
                    return JSONResponse({
                        "user_message": user_msg.to_dict(),
                        "assistant_message": am.to_dict(),
                    })

                if ir.source in ("llm", "rule") and ir.confidence >= 0.6:
                    if action == ACTION_HISTORY:
                        # 走既有历史产物问答链（只读，不重算）
                        reply = (
                            (_wants_history_comparison(content, ctx, _intent_metric)
                             and _answer_history_comparison(content, ctx))
                            or _answer_by_metric(_intent_metric, content, ctx, _intent_slots)
                            or _history_fallback(_intent_metric, ctx)
                        )
                        return _reply(reply, "历史结果查询")
                    if action == ACTION_CLARIFY:
                        # 澄清层升级：不再是"一段万能文案"，而是按触发原因
                        # （置信不足 / 缺算例 / 意图对不上）生成针对性追问。
                        # 生成失败时 `_clarify_reply` 内部退回原固定文案。
                        return _reply(_clarify_reply(content, ir, session_id), "意图确认")
                    if action == ACTION_REJECT:
                        return _reply(
                            "这个请求我不能执行：已生成的计算结果不允许被修改或凭空编造；"
                            "如需不同参数下的结果，请改参数后发起一次新的独立计算。",
                            "拒绝说明",
                        )
                    if action == ACTION_CASUAL:
                        return _reply(_casual_reply(content, full_session), "闲聊")
                    if action == ACTION_CAPABILITY:
                        return _reply(
                            _answer_capability_inquiry(content, ctx)
                            or _casual_reply(content, full_session),
                            "能力说明",
                        )
                    if action == ACTION_KNOWLEDGE:
                        # 知识型问题的主回答：让模型基于片段归纳（P1）
                        rag_block = _build_rag_block(content, answer_with_llm=True)
                        if rag_block:
                            return _reply("知识库中检索到以下内容：" + rag_block, "知识库问答")
                    # ACTION_COMPUTE：落到下面的计算链路
            except Exception:  # noqa: BLE001 - 意图层任何异常都退回旧规则链
                pass

        # 知识型问题（标准/限值/原理/定义）优先走 RAG 知识库。
        # 否则「20kV 三相电压偏差国标限值」会因为含"三相"被判成能力说明，
        # 而真正该给的是国标条款。
        # 必须放在能力询问之前：这类问题语义明确，不该被后续分支抢走。
        if _is_knowledge_query(content):
            # 同上：这是知识型问题的主回答路径，需要生成（不是"顺便提一下"）
            rag_block = _build_rag_block(content, answer_with_llm=True)
            if rag_block:
                reply = "知识库中检索到以下内容：" + rag_block
                assistant_msg = store.add_message(session_id, "assistant", reply)
                if session.title == "新聊天":
                    store.update_session_title(session_id, title_text or "知识库问答")
                return JSONResponse({
                    "user_message": user_msg.to_dict(),
                    "assistant_message": assistant_msg.to_dict(),
                })
            # 知识库无命中 → 继续走下面的常规路由（计算/历史/闲聊）

        # 能力询问（"你能不能做到 X"）优先于历史查询与重算，
        # 避免"如果算例出错导致不收敛，能给出哪里出错吗"被字面命中"收敛"而触发计算。
        if _is_capability_inquiry(content):
            reply = _answer_capability_inquiry(content, ctx) or _casual_reply(content, full_session)
            assistant_msg = store.add_message(session_id, "assistant", reply)
            if session.title == "新聊天":
                store.update_session_title(session_id, title_text or "能力说明")
            return JSONResponse({
                "user_message": user_msg.to_dict(),
                "assistant_message": assistant_msg.to_dict(),
            })

        if _wants_history_comparison(content, ctx, _intent_metric):
            reply = _answer_history_comparison(content, ctx) or (
                "我联系了前面的对话，但还没有足够的历史计算结果可以做对比。"
            )
            assistant_msg = store.add_message(session_id, "assistant", reply)
            if session.title == "新聊天":
                store.update_session_title(session_id, title_text or "历史结果对比")
            return JSONResponse({
                "user_message": user_msg.to_dict(),
                "assistant_message": assistant_msg.to_dict(),
            })

        if _is_history_query(content, ctx):
            reply = (
                _answer_by_metric(_intent_metric, content, ctx, _intent_slots)
                or _history_fallback(_intent_metric, ctx)
            )
            # 历史结果分支不再注入 RAG：用户问的是"这次结果是什么"，
            # 知识库检索对它毫无意义，还会让答案被无关片段污染。
            assistant_msg = store.add_message(session_id, "assistant", reply)
            if session.title == "新聊天":
                store.update_session_title(session_id, title_text or "新聊天")
            return JSONResponse({
                "user_message": user_msg.to_dict(),
                "assistant_message": assistant_msg.to_dict(),
            })

    # 上传了不支持的文件格式：给出明确提示，而不是当作闲聊
    case_ref_from_attachments = _extract_case_ref(session_id, attachments) if attachments else None
    explicit_case = _extract_case_name_from_text(content)
    if attachments and not case_ref_from_attachments and not explicit_case:
        reply = (
            "你上传的文件格式暂时不支持直接计算。天枢一号目前支持：\n"
            "- OpenDSS 文件：`*.dss`、`*.zip`（含 Master.dss 或多 csv）\n"
            "- Excel 算例：`*.xlsx`\n"
            "- CSV 算例：`*.csv`（含 type 列或 bus.csv + line.csv）\n"
            "- JSON 算例：`*.json`\n"
            "- MATPOWER 算例：`*.m`\n\n"
            "你也可以直接说“计算 IEEE33”使用内置算例。"
        )
        assistant_msg = store.add_message(session_id, "assistant", reply)
        if session.title == "新聊天":
            store.update_session_title(session_id, title_text or "新聊天")
        return JSONResponse({
            "user_message": user_msg.to_dict(),
            "assistant_message": assistant_msg.to_dict(),
        })

    if not single_intent and not contextual_intent:
        reply = _casual_reply(content, full_session)
        rag_block = _build_rag_block(content)
        if rag_block:
            # 之前：RAG 有命中就**整段覆盖** casual_reply 并替换为"规则引擎无法直接作答…"，
            # 导致"问漏号"被贴上一坨完全不相关的国标表格——典型的"无关内容硬贴"bug。
            # 现在：casual_reply 永远是主答案；RAG 只在末尾做"📚 顺便提一下"的软补充。
            # 只有在 casual_reply 本身没给出有用信息（很短或纯兜底）时，
            # 才退回到"知识库里有这些可能相关"的措辞。
            if len(reply.strip()) < 12 or "不太明白" in reply or "试试" in reply:
                reply = "知识库中检索到以下内容（仅供参考）：" + rag_block
            else:
                reply = reply + "\n\n📚 顺便提一下（知识库里也有相关内容）：\n" + rag_block
        assistant_msg = store.add_message(session_id, "assistant", reply)
        if session.title == "新聊天":
            store.update_session_title(session_id, title_text or "新聊天")
        return JSONResponse({
            "user_message": user_msg.to_dict(),
            "assistant_message": assistant_msg.to_dict(),
        })

    def _reject_reply(exc: "CaseProductRejected") -> JSONResponse:
        """把模型拒绝/同名歧义显示为对话回答，保留可操作的具体原因。"""
        am = store.add_message(session_id, "assistant", str(exc.detail))
        return JSONResponse({
            "user_message": user_msg.to_dict(),
            "assistant_message": am.to_dict(),
        })

    # 用户显式指定了一个不存在的算例名（如 case_999）：给出明确提示，不要默默回退历史算例
    # ⚠️ **本轮已经上传了附件时，正文里提到的算例名不算"点名"**。
    # 实测缺陷：用户上传 `case_35bus_random_radial.xlsx` 并问
    # 「同目录已有 case_35bus_random_radial_result.xlsx 参考结果，但不要读它」，
    # 系统把**参考文件名**当成了点名算例，回「算例 case35bus..._result 找不到」——
    # 上传的算例被完全忽略。附件是本轮唯一的计算依据，它的优先级高于正文里的
    # 任何文件名（正文提到别的文件，通常是在说明"别用它"或"拿它做对照"）。
    mentioned_case = None if attachments else _extract_mentioned_case_name(content)
    if mentioned_case and not _is_known_builtin_case(mentioned_case):
        # ⚠️ 不能只按"内置白名单"拒绝。用户之前上传过的自定义算例
        # （case17_structured_hard_v3 等）同样合法，这里会误杀——必须对
        # 已上传算例放行，让下面的 case_ref 决策正常处理。
        try:
            resolved_mentioned = _resolve_case_name_to_upload(mentioned_case, session_id)
        except CaseProductRejected as exc:
            return _reject_reply(exc)
        if not resolved_mentioned:
            # 找不到时不只报错：让模型在真实清单里找最近的，并给出一句确认问句。
            # **不会自动改用别的算例去算**（见 _case_not_found_reply 的说明）。
            reply = _case_not_found_reply(session_id, content, mentioned_case)
            assistant_msg = store.add_message(session_id, "assistant", reply)
            if session.title == "新聊天":
                store.update_session_title(session_id, title_text or "算例不存在")
            return JSONResponse({
                "user_message": user_msg.to_dict(),
                "assistant_message": assistant_msg.to_dict(),
            })

    # 确定本次使用的 case_ref：新上传附件优先，其次才继承历史算例。
    # 不能先查 last_attachments，否则上传新算例后会被错误绑定到旧算例。
    try:
        case_ref = _extract_case_ref(session_id, attachments)
        if not case_ref:
            case_ref = _extract_case_ref(session_id, last_attachments)
    except CaseProductRejected as exc:
        return _reject_reply(exc)

    # 用户点名了算例却在本机找不到时要显式报错，绝不许悄悄退回 ieee33——
    # 那样会出现"我要算 case17，它却给出了 ieee33 的结果"，属于伪造证据。
    unresolved_named_case: str | None = None

    # 点名优先级最高：既认内置名（ieee33/case4），也认上传的自定义名
    # （case11_structured_hard_v2）。缺这一步，用户点名的算例会被静默忽略、
    # 沿用上一次的算例 —— 表面 success，实则是答非所问。
    # ⚠️ 与上面同理：**本轮上传了附件时，正文里的文件名不算点名**。
    # 判据与理由见上一处注释（实测：把"参考结果文件"当成了要算的算例）。
    named_case = None if attachments else (_extract_mentioned_case_name(content) or explicit_case)

    if named_case:
        try:
            resolved = _resolve_case_name_to_upload(named_case, session_id)
        except CaseProductRejected as exc:
            return _reject_reply(exc)
        if resolved:
            case_ref = resolved
        elif os.path.isdir(os.path.join(store.UPLOADS_DIR, session_id, named_case)) or os.path.isfile(named_case):
            case_ref = case_ref
        elif explicit_case and _is_known_builtin_case(named_case):
            # 内置算例交给 cases_root 正常解析
            case_ref = named_case
        else:
            unresolved_named_case = named_case
    elif not case_ref and ctx.get("last_task_case_ref"):
        case_ref = ctx["last_task_case_ref"]

    if unresolved_named_case:
        # 同上：报错之外再给一句"是不是要说 X"的确认问句（绝不自动换算例）。
        assistant_msg = store.add_message(
            session_id,
            "assistant",
            _case_not_found_reply(session_id, content, unresolved_named_case),
        )
        return JSONResponse({"user_message": user_msg.to_dict(), "assistant_message": assistant_msg.to_dict()})

    # 聊天模式默认保存完整可追问结果域，避免后续询问支路/变压器时只能返回“未保存”。
    # 用户仍可通过 CLI/API 显式缩小结果范围；聊天追问则直接读取本次任务产物。
    result_scopes = [
        ResultScope.BUS,
        ResultScope.BRANCH,
        ResultScope.TRANSFORMER,
        ResultScope.SYSTEM,
    ]
    output_formats = [OutputFormat.MD, OutputFormat.HTML]

    # ---- 批量分支：一次上传了**多个独立算例** ----
    # 判据：附件里能各自定位出 Master.dss 的**不止一个**，且用户没有点名算例。
    # 为什么必须有这一步：`_extract_case_ref()` 遇到第一个就返回，于是
    # "传了两个算例"只算第一个，而且**没有任何提示** —— 用户以为两个都算了。
    # （实测：上传 case39 + case40 并说"计算这两个算例"，只得到 case39 一份报告。）
    if attachments and not named_case:
        _cands = _extract_case_refs(session_id, attachments)
        if len(_cands) > 1:
            return _run_case_batch(
                session_id=session_id,
                content=content,
                user_msg=user_msg,
                case_refs=_cands,
                title_text=title_text,
                session=session,
                result_scopes=result_scopes,
                output_formats=output_formats,
            )

    # 生成系统运行消息（占位，后续更新结果）
    assistant_msg = store.add_message(
        session_id, "assistant", "正在理解任务并执行潮流计算，请稍候..."
    )

    # 执行任务
    try:
        effective_case_ref = case_ref or "case4"  # 公开版默认使用自建合成算例
        # 回复前缀（只在成功时拼接，与既有行为一致）。执行体与批量路径共用。
        context_note = ""
        if contextual_intent:
            context_note = "我联系了前面的对话，基于同一算例继续分析。\n\n"
        elif ctx.get("last_task_case_ref") and effective_case_ref == ctx["last_task_case_ref"]:
            context_note = "我联系了前面的对话，沿用上次的算例继续计算。\n\n"
        default_note = ""
        if not case_ref and not ctx.get("last_task_case_ref"):
            default_note = "你未指定算例，我默认使用内置合成算例 `case4` 进行计算。\n\n"
        solver_note = ""
        _sc = _parse_solver_config_from_text(content)
        if _sc and (_sc.max_iterations is not None or _sc.tolerance is not None):
            _parts = []
            if _sc.max_iterations is not None:
                _parts.append(f"最大迭代 {_sc.max_iterations}")
            if _sc.tolerance is not None:
                _parts.append(f"收敛容差 {_sc.tolerance}")
            solver_note = f"已按你指定的求解参数：{', '.join(_parts)}。\n\n"

        task_id, status, _reply = _run_case_into_message(
            assistant_msg,
            session_id,
            content,
            effective_case_ref,
            result_scopes=result_scopes,
            output_formats=output_formats,
            success_prefix=f"{context_note}{default_note}{solver_note}",
        )

        # 更新会话标题（首次用户消息的前 8 个字）
        if session.title == "新聊天":
            store.update_session_title(session_id, title_text or "潮流计算")

        return JSONResponse({
            "user_message": user_msg.to_dict(),
            "assistant_message": store.get_message(assistant_msg.id, session_id).to_dict(),
        })
    except TaskBusyError as exc:
        # 单实例引擎：忙时直接拒绝并说明，不排队、不产生半成品结果。
        reply = (
            f"⚠️ 系统正在执行另一个任务（{_busy_owner_hint()}），本次请求**未启动**。\n\n"
            "为避免两个任务共用同一个求解引擎导致数值错乱，同一时刻只允许执行一个任务。\n"
            "请等当前任务结束（右侧执行监控会显示进度），或点「停止」中断它后再发起。"
        )
        store.update_message_task(assistant_msg.id, task_status="system_failed", content=reply)
        return JSONResponse({
            "user_message": user_msg.to_dict(),
            "assistant_message": store.get_message(assistant_msg.id, session_id).to_dict(),
        })
    except Exception as exc:
        store.update_message_task(
            assistant_msg.id,
            task_status="system_failed",
            content=f"执行失败：{exc}",
        )
        return JSONResponse({
            "user_message": user_msg.to_dict(),
            "assistant_message": store.get_message(assistant_msg.id, session_id).to_dict(),
        })


@router.post("/api/chat/sessions/{session_id}/upload")
def upload_file(session_id: str, files: list[UploadFile] = File(...)) -> JSONResponse:
    store.init_db()
    session = store.get_session(session_id, include_messages=False)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    attachments = _process_uploaded_files(session_id, files)
    file_list = ", ".join(a["filename"] for a in attachments)
    msg = store.add_message(
        session_id,
        "user",
        f"上传了文件：`{file_list}`。",
        attachments=attachments,
    )
    return JSONResponse({"message": msg.to_dict(), "attachments": attachments})


# --------------------------------------------------------------------------
# RAG：本地知识库检索 API
# --------------------------------------------------------------------------
class _RagSearchRequest(BaseModel):
    query: str
    top_k: int | None = None


@router.post("/api/rag/search")
def rag_search(payload: _RagSearchRequest) -> JSONResponse:
    """对本地知识库做一次检索，返回 top-k 命中片段。"""
    cfg = AgentConfig()
    if not cfg.rag_enabled:
        raise HTTPException(status_code=400, detail="RAG 已禁用（DPF_RAG_ENABLED=0）")
    q = (payload.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="query 不能为空")
    return JSONResponse(get_pipeline().search(q, top_k=payload.top_k))


@router.get("/api/rag/status")
def rag_status() -> JSONResponse:
    """返回 RAG 配置 / 知识库 / 索引状态。"""
    return JSONResponse(get_pipeline().status())


@router.post("/api/rag/ingest")
def rag_ingest(force: bool = True) -> JSONResponse:
    """重建 RAG 索引。默认 force=True；新增/修改知识库文档后调用。"""
    return JSONResponse(get_pipeline().ingest(force=bool(force)))


@router.get("/api/powerrag/status")
def powerrag_status() -> JSONResponse:
    """外部 Power-RAG 服务（localhost:5000）健康状态与知识库统计。"""
    return JSONResponse(get_pipeline().power.health(timeout=8))


@router.post("/api/powerrag/search")
def powerrag_search(payload: _RagSearchRequest) -> JSONResponse:
    """直接检索 Power-RAG（不降级本地 BM25），用于确认外部服务命中情况。"""
    q = (payload.query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="query 不能为空")
    try:
        hits = get_pipeline().power.search(q, top_k=payload.top_k)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Power-RAG 检索失败：{exc}") from exc
    return JSONResponse({"query": q, "backend": "powerrag", "num_hits": len(hits), "hits": hits})


# --------------------------------------------------------------------------
# 任务列表：真实 task_id 存在聊天消息里（update_message_task 会写 task_id），
# 结果展示页用它找最近一次真实任务，替代前端逐会话扫描。
# 路径用 /api/recent-tasks 而非 /api/tasks/recent，
# 避免与 api.py 的 /api/tasks/{task_id} 路由参数匹配冲突（"recent" 会被当成 task_id）。
# --------------------------------------------------------------------------
@router.get("/api/recent-tasks")
def recent_tasks(limit: int = 20) -> JSONResponse:
    """按时间倒序列出带 task_id 的任务记录（结果展示页数据源）。"""
    store.init_db()
    n = max(1, min(int(limit or 20), 200))
    with store.get_conn() as conn:
        rows = conn.execute(
            """SELECT task_id, task_case_ref, task_status, report_url, content, created_at, session_id
               FROM messages
               WHERE task_id IS NOT NULL
               ORDER BY created_at DESC
               LIMIT ?""",
            (n,),
        ).fetchall()
    return JSONResponse({
        "tasks": [
            {
                "task_id": r["task_id"],
                "task_case_ref": r["task_case_ref"],
                "task_status": r["task_status"],
                "report_url": r["report_url"],
                "content": r["content"],
                "created_at": r["created_at"],
                "session_id": r["session_id"],
            }
            for r in rows
        ]
    })
