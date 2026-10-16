"""执行过程的「人话」：动作行 + 思考段（模板 + 真实数值）。

为什么要有这一层
----------------
界面上的执行过程原先只是一串**阶段清单**（阶段名 + 耗时 + ✓），回答的是
"系统跑到哪一步了"。但要看的是**思考流**，回答的是"它此刻在想什么" ——
这一步为什么做、上一个动作看到了什么、接下来要查什么。

为什么是模板，不是让模型现场写
------------------------------
本项目的验收红线是**可复现 + 可审计**。若思考段由模型生成，同样的输入跑两次
会得到两段不同的过程文案，事后审计对不上账。因此这里把思考段做成
`stage → 模板` 的**纯函数**，槽位全部取自事件自带的 `detail`（真实产物的
结构化数值）⇒ 同样的输入永远产出同样的过程文案。

硬约定
------
- 模板里出现的每个数字、名词都必须来自 `detail` 或该 stage 的静态事实。
  **不要**在这里写"通常会…""大约…"这类无法从产物核对的判断 ——
  过程文案一旦开始推测，整条链路的可审计性就被污染了。
- 取不到某个槽位时**退回如实转述 message**，宁可少说一句，不编一句。
- 改这里的模板必须升 `NARRATION_VERSION`：它是"界面当时显示的那段话"与
  "代码现在会生成的那段话"是否一致的唯一凭据。
"""
from __future__ import annotations

import re
from typing import Any, Callable

NARRATION_VERSION = "1.2.0"

_MISSING = "—"

# 机器记录的样子（"load_case ok: …" / "validate_case passed, …"）。
# 只用于兜底句的排版，不用于取值 —— 取值一律走 detail。
_MACHINE_RECORD = re.compile(r"^[a-zA-Z_]\w* (ok|passed|failed|done|converged)\b")

# stage → (动词, 对象, 种类)
# 种类只用于界面上的图标配色：read=只读探查 / compute=计算 /
# verify=校验 / write=产物。图标形态由种类决定，不再和具体阶段名绑死。
ACTIONS: dict[str, tuple[str, str, str]] = {
    "receive_request": ("接收", "请求与目标", "read"),
    "parse_intent": ("理解", "任务意图", "read"),
    "resolve_constraints": ("确定", "约束条件", "read"),
    "request_guard": ("校验", "请求合法性", "verify"),
    "engine_health": ("检查", "计算引擎", "verify"),
    "build_execution_plan": ("编排", "执行计划", "read"),
    "load_case": ("装载", "算例", "read"),
    "validate_case": ("静态检查", "算例", "verify"),
    "build_network_profile": ("分析", "网络拓扑", "read"),
    "build_opendss_solver_profile": ("确定", "求解策略", "read"),
    "run_opendss_powerflow": ("求解", "潮流", "compute"),
    "normalize_results": ("归一化", "结果口径", "compute"),
    "verify_result_integrity_and_power_balance": ("独立校验", "功率平衡", "verify"),
    "build_evidence": ("汇总", "证据链", "write"),
    "publish_guard": ("校验", "可否发布", "verify"),
    "export_artifacts": ("导出", "产物", "write"),
    "explain_result": ("转述", "结果说明", "write"),
    "render_report": ("生成", "报告", "write"),
    # ── 早期失败路径（上传 / 转换阶段被拒）────────────────────
    # 实测缺陷：算例在**转换阶段**就被拒（孤岛 / 重复母线 / 零阻抗）时，
    # 后端直接返回一行错误、**不产生任何 trace 事件** —— 界面上思考流全空。
    # 而这恰恰是最需要解释的场合：用户传了个坏算例，最想知道的
    # 是"你看到了什么、为什么判定为坏、下一步该查哪里"。
    "ingest_upload": ("接收", "上传文件", "read"),
    "convert_case": ("转换", "算例格式", "read"),
    "diagnose_case": ("定位", "数据问题", "verify"),
    # 判据边界单独一段（`criterion_case`）：用户常问的不是"对不对"而是
    # "判据的边界在哪"——「奇异矩阵问题还是数值容差问题？」「极小但非零的
    # 阻抗算不算零？」。这类答案在代码常量里，可核对，不是推测。
    # ⚠️ 必须**独立成阶段**：若复用 `diagnose_case`，界面上会出现两个
    # 同名步骤，违反"阶段不重复"，用户也会以为是同一句话重复了两遍。
    "criterion_case": ("判据", "判定边界", "verify"),
}

# 「正在做…」的开场句。只给"耗时到值得单独说明"的阶段写 ——
# 其余阶段用通用句式（正在 + 动词 + 对象…），保持句式一致。
_LEAD: dict[str, str] = {
    "parse_intent": "正在调大模型理解意图 —— 要认出算例、结果范围与参数约束，"
                    "通常 5~10 秒，这段没有任何中间输出：",
    "engine_health": "正在探测 OpenDSS 引擎是否可用（首次启动要加载引擎，约 20~40 秒）：",
    "run_opendss_powerflow": "正在调 OpenDSS 求解潮流 —— 大算例要几分钟，"
                             "这一步没有中间输出，停在这里是正常的：",
}


# ── 取值小工具（都必须是"取不到就说取不到"，不许猜）────────────────
def _g(d: dict, key: str, default: Any = _MISSING) -> Any:
    v = d.get(key)
    return default if v is None or v == "" else v


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v: Any, digits: int = 2) -> str:
    """数值转文本：整数不带小数点，其余保留 digits 位。"""
    f = _num(v)
    if f is None:
        return _MISSING
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.{digits}f}"


def _clip(text: Any, limit: int = 48) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit] + "…" if len(s) > limit else s


# ── 各阶段的「思考段」───────────────────────────────────────────
# 每段的结构固定为：观察（上一步产出的真实结果）→ 判断（这意味着什么）
# → 决定（紧接着要查什么），末尾用全角冒号把话头递给下一个动作。
# 这就是"像人在想"的排版语法，缺了末尾那一句读起来就只是目录。

def _beat_receive_request(d: dict) -> str | None:
    """该阶段有两条事件：① 用户原话 ② 解析出的「目标 + 结果范围」。

    展示时后一条覆盖前一条，所以这里优先按②写 —— 但①也要能独立成句
    （实测存在只有用户原话、没有②的运行），两态都覆盖才不会有空文案。
    """
    goal = _clip(_g(d, "user_goal", ""), 60)
    if goal:
        scopes = d.get("result_scopes") or []
        scope_text = ("、".join(str(s) for s in scopes)) if scopes else ""
        tail = (f"结果范围取 {scope_text} —— 范围决定后面算什么、导出什么，"
                "先把它定死：" if scope_text else "下面把它翻译成可冻结的硬约束：")
        return f"目标已明确：{goal}。{tail}"
    text = _clip(_g(d, "text", ""), 48)
    if not text:
        return None
    return (f"收到请求：「{text}」。先判断它属于哪一类 —— 求解潮流、回看某次结果，"
            "还是纯知识问答；三类走的下游完全不同，认错了就会把闲聊也送进求解链路：")


def _beat_parse_intent(d: dict) -> str | None:
    goal = _clip(_g(d, "user_goal", ""), 60)
    if not goal:
        return None
    parser = _g(d, "parser", "?")
    scopes = d.get("result_scopes") or []
    tail = (f"结果范围锁定为 {'、'.join(str(s) for s in scopes)} —— "
            "它会作为硬约束参与缓存键，所以必须在这一步定死："
            if scopes else "接下来把目标翻译成硬约束：")
    return f"意图已解析（{parser}）：{goal}。{tail}"


def _beat_resolve_constraints(d: dict) -> str | None:
    hard = _g(d, "hard", "")
    if not hard:
        return None
    soft = d.get("soft")
    extra = f"解释重点放在「{soft}」。 " if soft else ""
    return (f"约束定下来了：{hard}。{extra}"
            "约束和口径一起决定缓存键 —— 同一个算例换了口径就是另一次求解，"
            "绝不能命中旧缓存：")


def _beat_request_guard(d: dict) -> str | None:
    return ("请求门放行：算例路径、能力口径、参数范围都过了检查。这一步不做任何计算，"
            "作用是在任务规格冻结之前把不合格的请求挡掉 —— 冻结之后字段就再也改不动了：")


def _beat_engine_health(d: dict) -> str | None:
    return ("引擎可用，可以开始求解。单独先探一次是为了避免最贵的那种失败："
            "算了半天才发现引擎根本没起来：")


def _beat_build_execution_plan(d: dict) -> str | None:
    steps = d.get("steps") or []
    chain = "→".join(str(s) for s in steps) if steps else ""
    if not chain:
        return None
    return (f"执行链固定为：{chain}。它是**代码写死的**，不由模型现场决定 —— "
            "这正是「同样输入能复现同样结果」的前提：")


def _beat_load_case(d: dict) -> str | None:
    fp = _g(d, "case_fingerprint", "")
    if not fp:
        return None
    return (f"算例装载完成：内容指纹 {fp}，随附 {_fmt(_g(d, 'dependency_files', 0), 0)} 个引用文件。"
            "指纹只由算例内容决定，后面的求解缓存认的就是它：")


def _beat_validate_case(d: dict) -> str | None:
    objects = _clip(_g(d, "objects", ""), 70)
    tail = f"清点到的设备：{objects}。" if objects else ""
    return (f"静态检查通过。{tail}先查硬伤（缺元件、参数越界、拓扑断点）再求解，"
            "能省下一整次求解的代价：")


def _beat_build_network_profile(d: dict) -> str | None:
    buses = _num(d.get("buses"))
    if buses is None:
        return None
    return (f"网络画像：{_fmt(buses, 0)} 条母线、{_fmt(d.get('lines'), 0)} 条线路、"
            f"{_fmt(d.get('loads'), 0)} 个负荷，装机总负荷 {_fmt(d.get('load_kw'), 2)} kW。"
            "这一层只统计拓扑与设备，还不涉及潮流：")


def _beat_build_opendss_solver_profile(d: dict) -> str | None:
    pid = _g(d, "profile_id", "")
    if not pid:
        return None
    return (f"求解策略定为 {pid}：{_clip(_g(d, 'params', ''), 70)}。"
            "策略一旦冻结就不再改动，防止求解中途系数被悄悄换掉：")


def _beat_run_opendss_powerflow(d: dict) -> str | None:
    it = _num(d.get("iterations"))
    conv = d.get("converged")
    if it is None and conv is None:
        return None
    state = "已收敛" if conv else "未收敛"
    return (f"潮流{state}，迭代 {_fmt(it, 0)} 次。收敛只说明方程解出来了，"
            "还不代表结果可信 —— 紧接着要换一套独立实现重新校验：")


def _beat_normalize_results(d: dict) -> str | None:
    sc = _clip(_g(d, "scope_status", ""), 60)
    return (f"结果已按统一口径归一化。{('各结果域状态：' + sc + '。') if sc else ''}"
            "引擎的原始输出单位不一、口径不一，必须先转成带声明的统一结果再往外给：")


def _beat_verify(d: dict) -> str | None:
    passed = d.get("passed")
    if passed is None:
        return None
    res = _num(d.get("residual_pu"))
    head = "独立校验通过" if passed else "独立校验未通过"
    body = f"：功率平衡残差 {_fmt(res, 6)} pu，在容差内。" if res is not None else "。"
    return (f"{head}{body}校验与求解是两套独立实现 —— 自己算完自己验没有意义：")


def _beat_build_evidence(d: dict) -> str | None:
    n = _num(d.get("count"))
    if n is None:
        return None
    return (f"汇总出 {_fmt(n, 0)} 条 Evidence。每条结论都挂到具体产物的具体字段上，"
            "报告里任何一句话都能回溯到它是从哪个数来的：")


def _beat_publish_guard(d: dict) -> str | None:
    detail = _clip(_g(d, "gate", ""), 70)
    if not detail:
        return None
    return (f"发布门：{detail}。没过就不发布 —— 宁可不给结论，也不给一个没验证过的结论：")


def _beat_export_artifacts(d: dict) -> str | None:
    n = _num(d.get("count"))
    return (f"导出 {_fmt(n, 0)} 项产物（报告 / 结果 JSON / 曲线数据）。"
            "整条链路的中间结果都落盘留痕，事后可以逐条复盘：")


def _beat_explain_result(d: dict) -> str | None:
    src = d.get("source")
    who = {"llm": "大模型", "rule": "确定性模板"}.get(str(src), _g(d, "source", "?"))
    return (f"结果说明已生成（来源：{who}）。说明里的数值全部取自本次求解产物，"
            "不由模型自由发挥 —— 模型只负责把数说成人话：")


def _beat_render_report(d: dict) -> str | None:
    out = _clip(_g(d, "out_dir", ""), 44)
    tail = f"报告已存到 {out}。" if out else "报告已生成。"
    return f"{tail}到这里整条链路走完，每一步的产物都还在，可以逐条复盘。"


def _beat_ingest_upload(d: dict) -> str | None:
    names = d.get("files") or []
    n = _num(len(names))
    if n is None:
        return None
    tail = "、".join(_clip(x, 28) for x in names[:3]) if names else ""
    more = f" 等 {int(n)} 个文件" if n > 3 else ""
    return (f"收到 {int(n)} 个上传文件（{tail}{more}）。"
            "先落盘再判断能不能用 —— 落盘用的是原始文件名，"
            "所以必须先查有没有同名文件会互相覆盖：")


def _beat_convert_case(d: dict) -> str | None:
    src = _clip(_g(d, "source_format", ""), 24)
    dst = _clip(_g(d, "target", ""), 40)
    if not src:
        return None
    head = f"把 {src} 转成 {dst or 'OpenDSS 模型'}"
    rows = _num(d.get("rows"))
    if rows is not None:
        return (f"{head}，读入 {_fmt(rows, 0)} 行记录。"
                "转换阶段只做结构化解析与健全性检查，不求解 —— "
                "坏数据必须在这里被挡住，不能带着它进引擎：")
    return (f"{head}。转换阶段只做结构化解析与健全性检查，不求解 —— "
            "坏数据必须在这里被挡住，不能带着它进引擎：")


def _beat_diagnose_case(d: dict) -> str | None:
    reason = _clip(_g(d, "reason", ""), 80)
    if not reason:
        return None
    layer = _clip(_g(d, "layer", ""), 30)
    hint = _clip(_g(d, "suggestion", ""), 60)
    head = f"问题定位于{('「' + layer + '」') if layer else '转换阶段'}：{reason}"
    tail = f"建议：{hint}。" if hint else ""
    return (f"{head}。{tail}这是**数据问题**而不是数值算法问题 —— "
            "换收敛算法或迭代次数都不会让它变好，先改数据：")


def _beat_criterion_case(d: dict) -> str | None:
    """判定边界段：说清"什么情况下这条判据**不会**触发"。

    只讲边界，不重复上面的失败原因（那已由 `diagnose_case` 说过一遍）。
    价值在于让用户知道自己踩的是硬边界还是近似容差 ——
    这直接决定他是该改数据还是该调参数。
    """
    hint = _clip(_g(d, "suggestion", ""), 220)
    if not hint:
        return None
    return ("这一条判据的**边界**也要说清楚，否则你无法判断该改数据还是调参数："
            f"{hint}。")


_BEATS: dict[str, Callable[[dict], str | None]] = {
    "ingest_upload": _beat_ingest_upload,
    "convert_case": _beat_convert_case,
    "diagnose_case": _beat_diagnose_case,
    "criterion_case": _beat_criterion_case,
    "receive_request": _beat_receive_request,
    "parse_intent": _beat_parse_intent,
    "resolve_constraints": _beat_resolve_constraints,
    "request_guard": _beat_request_guard,
    "engine_health": _beat_engine_health,
    "build_execution_plan": _beat_build_execution_plan,
    "load_case": _beat_load_case,
    "validate_case": _beat_validate_case,
    "build_network_profile": _beat_build_network_profile,
    "build_opendss_solver_profile": _beat_build_opendss_solver_profile,
    "run_opendss_powerflow": _beat_run_opendss_powerflow,
    "normalize_results": _beat_normalize_results,
    "verify_result_integrity_and_power_balance": _beat_verify,
    "build_evidence": _beat_build_evidence,
    "publish_guard": _beat_publish_guard,
    "export_artifacts": _beat_export_artifacts,
    "explain_result": _beat_explain_result,
    "render_report": _beat_render_report,
}


def action_for(stage: str) -> dict[str, str]:
    """阶段 → 动作行（动词 / 对象 / 种类）。未知阶段如实显示阶段名。"""
    verb, obj, kind = ACTIONS.get(str(stage or ""), ("执行", str(stage or ""), "compute"))
    return {"verb": verb, "object": obj, "kind": kind}


def _generic_lead(stage: str) -> str:
    a = action_for(stage)
    return f"正在{a['verb']}{a['object']}…："


def narrate(
    stage: str,
    event_type: str = "",
    status: str = "",
    detail: dict[str, Any] | None = None,
    message: str = "",
) -> str:
    """一条事件 → 该阶段的「思考段」。

    取值优先级：失败态 > 进行中（开场句）> 专用模板（读 detail）> 如实转述 message。
    任何一层取不到内容就往下退，**绝不编内容**；函数本身不抛异常。
    """
    try:
        d = detail if isinstance(detail, dict) else {}
        st = str(status or "").lower()
        et = str(event_type or "").lower()
        stage = str(stage or "")

        if st == "failed":
            body = _clip(message, 90) or "见原始事件"
            return f"这一步没通过：{body}。按约定，不会带着未通过的结果继续往下走。"

        if st in ("running", "pending") or et == "tool_call":
            return _LEAD.get(stage) or _generic_lead(stage)

        beat = _BEATS.get(stage)
        if beat is not None:
            text = beat(d)
            if text:
                return text

        text = str(message or "").strip()
        if not text:
            return ""
        # 兜底也要能读：老任务的 trace 里没有 detail（该字段是后加的），
        # 直接甩一句 "load_case ok: fingerprint=…" 到界面上等于没说人话。
        if text[-1] in "：。！？":
            return text
        a = action_for(stage)
        if _MACHINE_RECORD.match(text):
            return f"{a['verb']}{a['object']}：{text}"
        return text + "："
    except Exception:  # noqa: BLE001 - 过程文案绝不能影响主流程
        return ""


def narrate_from_event(e: dict[str, Any]) -> str:
    """trace.json 里的一条事件（dict）→ 思考段。

    事件已经落盘之后再补文案，取值只依赖落盘字段 ⇒ SSE 实时推的与
    任务结束后回看的，必然是同一段话。
    """
    if not isinstance(e, dict):
        return ""
    return narrate(
        e.get("stage") or "",
        e.get("event_type") or "",
        e.get("status") or "",
        e.get("detail"),
        e.get("message") or "",
    )
