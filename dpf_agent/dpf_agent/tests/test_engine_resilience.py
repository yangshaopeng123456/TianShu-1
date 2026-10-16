# -*- coding: utf-8 -*-
"""OpenDSS COM 引擎「中毒自愈」回归测试（自包含，不依赖真实引擎）。

运行：`.venv/Scripts/python.exe -m dpf_agent.tests.test_engine_resilience`

## 锁定的真实缺陷

2026-09-13 22:48–23:01，连续 4 个任务（case44 / case53 / case54 / case55）在
`validate_case` 各卡 **120.0s** 并失败，整机会话瘫痪 4 分钟以上，只能重启后端。

根因（改动前）：
1. `_ComWorker._loop()` 里 `engine` 是 `while True` 的局部变量，**只 Dispatch 一次、永不重建**；
2. `call()` 超时后只 `raise`，那次卡住的 `fn(engine)` **仍占着引擎**；
3. 于是后续每次调用都排在已卡死的引擎后面 → 必然再次超时。

## 本测试的策略

真实 COM 引擎在 CI / 沙箱里不可用，因此**注入一个假的 Dispatch**：
假引擎在收到特定请求时"永久卡住"，用来复现中毒；随后断言自愈机制生效。

覆盖点：
- A. 超时后 worker 的"代"递增（引擎被重建）
- B. 超时后**下一次调用能正常返回**（这是修复的核心价值：不再连续中毒）
- C. 重建不会把异常泄漏成新的异常类型
- D. 正常调用**不触发**重建（零额外开销）
- E. `dss_ctx` 不再保存会被替换掉的 worker 引用
- F. 诊断信息可读（供指标观测）
"""

from __future__ import annotations

import sys
import threading
import time

from ..engines.opendss_com_adapter import OpenDSSComAdapter, _ComWorker
from ..engines.opendss_adapter import AdapterError
from ..schemas.enums import ErrorCode

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 假引擎：用 monkeypatch 替换 win32com.client.Dispatch
# ---------------------------------------------------------------------------
class _FakeEngine:
    """行为可编程的假引擎。

    - `hang_on(pred)`：当 pred(tag) 为真时，该次调用永久阻塞（模拟引擎卡死）
    - 其余调用立即返回 tag，便于断言"通道是否干净"
    """

    def __init__(self) -> None:
        self.version = "fake-9.9"
        self.calls: list[str] = []
        self._hang_pred = None
        self._lock = threading.Lock()
        # 用于证明"旧引擎确实卡住"：卡死调用进入后置位
        self.hung_event = threading.Event()

    def hang_on(self, pred) -> None:
        self._hang_pred = pred

    def invoke(self, tag: str):
        with self._lock:
            self.calls.append(tag)
        if self._hang_pred is not None and self._hang_pred(tag):
            self.hung_event.set()
            # 永久阻塞：模拟 COM 调用不返回。测试结束由 daemon 线程收尾。
            threading.Event().wait()
        return f"ok:{tag}"


def _make_worker_with_fake_engine(monkeypatch_dispatch) -> _ComWorker:
    """构造一个使用假引擎的 _ComWorker。

    _ComWorker._loop 内部 `from win32com.client import Dispatch` 后 Dispatch(prog_id)。
    这里通过替换 sys.modules 里的 win32com.client 来注入假实现，
    避免依赖真实 pywin32（沙箱/CI 上通常没有）。
    """
    import types

    fake_client = types.ModuleType("win32com.client")
    fake_client.Dispatch = lambda prog_id: monkeypatch_dispatch(prog_id)

    win32com = types.ModuleType("win32com")
    win32com.client = fake_client
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None

    sys.modules["win32com"] = win32com
    sys.modules["win32com.client"] = fake_client
    sys.modules["pythoncom"] = pythoncom

    w = _ComWorker()
    # 给线程一点时间完成 Dispatch
    time.sleep(0.15)
    return w


def _restore_modules(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


# ---------------------------------------------------------------------------
# A + B + C：超时后引擎重建，且下一次调用恢复正常（修复的核心）
# ---------------------------------------------------------------------------
def case_timeout_then_recover() -> None:
    saved = {k: sys.modules.get(k) for k in ("win32com", "win32com.client", "pythoncom")}
    engines: list[_FakeEngine] = []

    def dispatch(_prog_id):
        e = _FakeEngine()
        # 第 1 个引擎：收到 "poison" 就卡死
        if not engines:
            e.hang_on(lambda tag: tag == "poison")
        engines.append(e)
        return e

    try:
        w = _make_worker_with_fake_engine(dispatch)

        # 先跑一次正常调用，确认基线可用
        r0 = w.call(lambda eng: eng.invoke("baseline"), 2.0)
        check("A0 正常调用可用", r0 == "ok:baseline", f"返回 {r0!r}")

        gen_before = w.diagnostics()["generation"]

        # 触发中毒：这次调用会卡死并超时
        t0 = time.time()
        raised_type = None
        try:
            w.call(lambda eng: eng.invoke("poison"), 0.6)
            check("A1 中毒调用应超时抛异常", False, "竟然正常返回了")
        except AdapterError as exc:
            raised_type = exc.code
            elapsed = time.time() - t0
            check("A1 中毒调用超时抛 AdapterError(TIMEOUT)",
                  exc.code == ErrorCode.TIMEOUT, f"code={exc.code}, 耗时 {elapsed:.2f}s")
            # 超时必须"按设定时间"返回，不能等满默认 120s
            check("A2 超时按设定时长返回（未退化为默认长超时）",
                  elapsed < 3.0, f"实际 {elapsed:.2f}s")
        except Exception as exc:  # noqa: BLE001
            check("A1 中毒调用超时抛 AdapterError(TIMEOUT)", False,
                  f"抛了非预期异常 {type(exc).__name__}: {exc}")

        # ---- 核心断言：引擎已重建 ----
        gen_after = w.diagnostics()["generation"]
        check("A3 超时后引擎代次递增（发生重建）",
              gen_after > gen_before, f"{gen_before} -> {gen_after}")
        check("A4 重建计数被记录",
              w.diagnostics()["respawn_count"] >= 1,
              f"respawn={w.diagnostics()['respawn_count']}")
        check("A5 重建后创建了新的引擎实例（Dispatch 被再次调用）",
              len(engines) >= 2, f"引擎实例数 {len(engines)}")

        # ---- 核心价值：下一次调用不再中毒 ----
        t1 = time.time()
        try:
            r1 = w.call(lambda eng: eng.invoke("after_heal"), 2.0)
            ok = r1 == "ok:after_heal"
            check("B1 重建后下一次调用正常返回（不再连续中毒）", ok, f"返回 {r1!r}")
        except Exception as exc:  # noqa: BLE001
            check("B1 重建后下一次调用正常返回（不再连续中毒）", False,
                  f"仍然失败：{type(exc).__name__}: {exc}")
        check("B2 恢复耗时正常（未再等一个超时周期）",
              (time.time() - t1) < 2.0, f"实际 {time.time()-t1:.2f}s")

        # ---- C：连续多次调用都正常（陈旧请求没有污染新通道）----
        seq_ok = True
        for i in range(3):
            try:
                r = w.call(lambda eng, i=i: eng.invoke(f"seq{i}"), 2.0)
                if r != f"ok:seq{i}":
                    seq_ok = False
            except Exception:  # noqa: BLE001
                seq_ok = False
        check("C1 恢复后连续 3 次调用均正常", seq_ok)

        check("C2 抛出的仍是 AdapterError（未泄漏内部异常类型）",
              raised_type == ErrorCode.TIMEOUT)
    finally:
        _restore_modules(saved)


# ---------------------------------------------------------------------------
# D：正常调用不触发重建（零额外开销）
# ---------------------------------------------------------------------------
def case_no_respawn_on_success() -> None:
    saved = {k: sys.modules.get(k) for k in ("win32com", "win32com.client", "pythoncom")}
    engines: list[_FakeEngine] = []

    def dispatch(_prog_id):
        e = _FakeEngine()
        engines.append(e)
        return e

    try:
        w = _make_worker_with_fake_engine(dispatch)
        for i in range(5):
            w.call(lambda eng, i=i: eng.invoke(f"n{i}"), 2.0)
        d = w.diagnostics()
        check("D1 全部成功时重建次数为 0", d["respawn_count"] == 0,
              f"respawn={d['respawn_count']}")
        check("D2 只创建了一个引擎实例", len(engines) == 1, f"实例数 {len(engines)}")
        check("D3 代次保持为 0", d["generation"] == 0, f"generation={d['generation']}")
    finally:
        _restore_modules(saved)


# ---------------------------------------------------------------------------
# E：dss_ctx 不再保存会被替换掉的 worker 引用
# ---------------------------------------------------------------------------
def case_dss_ctx_stability() -> None:
    saved = {k: sys.modules.get(k) for k in ("win32com", "win32com.client", "pythoncom")}

    def dispatch(_prog_id):
        return _FakeEngine()

    try:
        ad = OpenDSSComAdapter()
        w = ad._worker
        # 设计意图：**worker 对象保持稳定**（可安全被 handle 持有），
        # 被替换的是它内部的"通道"（队列 + 工作线程 + 引擎实例）。
        # 因此正确断言是：对象 id 稳定，但通道已换新。
        q_before, t_before = w._req, w._thread
        ad._worker._reset()
        q_after, t_after = w._req, w._thread
        check("E1 worker 对象保持稳定（可安全对外持有）", id(ad._worker) == id(w))
        check("E2 重建后内部队列已换成新对象", q_before is not q_after)
        check("E3 重建后工作线程已换成新线程", t_before is not t_after)
        check("E4 适配器提供 engine_diagnostics 观测入口",
              hasattr(ad, "engine_diagnostics")
              and "respawn_count" in ad.engine_diagnostics())
    finally:
        _restore_modules(saved)


# ---------------------------------------------------------------------------
# F：诊断信息可读
# ---------------------------------------------------------------------------
def case_diagnostics_shape() -> None:
    saved = {k: sys.modules.get(k) for k in ("win32com", "win32com.client", "pythoncom")}

    def dispatch(_prog_id):
        return _FakeEngine()

    try:
        w = _make_worker_with_fake_engine(dispatch)
        d = w.diagnostics()
        for key in ("generation", "respawn_count", "last_error", "init_error"):
            check(f"F1 诊断含字段 {key}", key in d)
        check("F2 未重建时 last_error 为空", d["last_error"] == "", repr(d["last_error"]))
    finally:
        _restore_modules(saved)


# ---------------------------------------------------------------------------
# G：旧线程不得污染新代的诊断信息（竞态回归）
# ---------------------------------------------------------------------------
def case_no_cross_generation_pollution() -> None:
    """旧线程在重建后才完成初始化时，不能把 init_error 写进新代。

    改动前的写法是 `self._init_error = init_error`（无条件写实例字段），
    重建会重置该字段，旧线程延迟写回就会污染新代诊断。
    """
    saved = {k: sys.modules.get(k) for k in ("win32com", "win32com.client", "pythoncom")}
    gate = threading.Event()  # 用来人为拉长"第 1 代线程的初始化耗时"

    def dispatch(_prog_id):
        # 第 1 代：让 Dispatch 阻塞，从而制造"旧线程晚于重建才写完 init_error"
        if not gate.is_set():
            gate.wait(2.0)
        return _FakeEngine()

    try:
        w = _make_worker_with_fake_engine(dispatch)
        # 此时第 1 代线程可能仍在 gate 上等待
        w._reset()  # 立即重建，制造"旧线程晚写"的场景
        time.sleep(0.1)
        gate.set()  # 放行第 1 代线程，让它去写 init_error
        time.sleep(0.3)
        d = w.diagnostics()
        check("G1 旧线程晚写不会污染新代 init_error", d["init_error"] == "",
              f"init_error={d['init_error']!r}")
    finally:
        gate.set()
        _restore_modules(saved)


def case_replan_choice() -> None:
    """未收敛时的恢复动作：模型只在白名单内选，选不了就退回固定顺序。

    2026-10-09 加。这是本项目**第一个"让模型参与决策"的入口** ——
    此前的 4 个提示词（task_parse / result_explain / intent_classify /
    rag_answer）全都只做"翻译"。既然引入了决策，边界就必须锁死：
    模型只选**方向**，参数一律由代码按模板生成 —— 否则就退回到
    "模型编数字"的老问题上，而那是这个项目最不能接受的一类缺陷。
    """
    import inspect

    from ..prompts import get_prompt, prompt_versions
    from ..runtime import model_client as mc
    from ..skills import powerflow_skill as ps

    # ---- A. 注册表与提示词 ----
    acts = getattr(ps, "_REPLAN_ACTIONS", ())
    check("R1 恢复动作注册表非空且每项有 id/desc",
          bool(acts) and all(a.get("id") and a.get("desc") for a in acts),
          str([a.get("id") for a in acts]))

    prompt = get_prompt("replan_choose")
    check("R2 提示词 replan_choose 已注册且含候选/上下文占位符",
          "{candidates}" in prompt and "{context}" in prompt)
    check("R3 明文禁止给出数值建议（这是本次的关键边界）",
          "不要给出任何数值建议" in prompt)
    check("R4 明文要求只从候选里选、并允许 abort",
          "只能从这里面选" in prompt and "abort" in prompt)
    check("R5 replan_choose 进入提示词版本集（事后可追溯是哪一版）",
          "replan_choose" in prompt_versions())

    # ---- B. 白名单校验（行为级：用 stub 顶掉 _chat） ----
    class _Stub(mc.ModelClient):
        def __init__(self, reply):
            self.config = None  # choose_replan 会读 config 上的超时项
            self._reply = reply

        def available(self):
            return True

        def _chat(self, messages, timeout=None, json_mode=True, purpose="chat"):
            return self._reply

    cands = [{"id": a["id"], "desc": a["desc"]} for a in acts]
    picked, why = _Stub('{"strategy":"%s","reason":"r"}' % acts[-1]["id"]).choose_replan(cands, {})
    check("R6 候选内的选择被接受并回报依据",
          picked == acts[-1]["id"] and why == "r", f"picked={picked!r} why={why!r}")

    picked, _ = _Stub('{"strategy":"invented_by_model"}').choose_replan(cands, {})
    check("R7 白名单外的选择被拒绝（模型不能自己发明动作）", picked is None, repr(picked))

    picked, _ = _Stub('{"strategy":"abort"}').choose_replan(cands, {})
    check("R8 abort 是合法选择（模型可判定继续重规划无益）", picked == "abort", repr(picked))

    picked, _ = _Stub("这不是 JSON").choose_replan(cands, {})
    check("R9 非 JSON 输出被拒绝", picked is None, repr(picked))

    # ---- C. 兜底：模型不可用/崩溃时必须退回顾有路径 ----
    class _Dead(mc.ModelClient):
        def __init__(self):
            pass

        def available(self):
            return False

    check("R10 模型不可用时返回 None（调用方据此退回注册顺序）",
          _Dead().choose_replan(cands, {})[0] is None)

    class _Boom(mc.ModelClient):
        def __init__(self):
            self.config = None

        def available(self):
            return True

        def _chat(self, *a, **k):
            raise RuntimeError("boom")

    check("R11 模型抛异常时返回 None（失败路径绝不外抛）",
          _Boom().choose_replan(cands, {})[0] is None)

    # ---- D. 防回归：写死的两级顺序不得回来 ----
    src = inspect.getsource(ps)
    check("R12 不再用 attempt 计数写死两级顺序",
          "attempt < 2" not in src and "attempt == 1" not in src)
    check("R13 候选由注册表按已试集合过滤",
          'a["id"] not in replan_tried' in src)
    check("R14 决策依据写进 replan.json（两处策略各一处）",
          src.count('"reason": why') >= 2, str(src.count('"reason": why')))
    check("R15 中止分支保留失败语义（不静默换模型当原结果）",
          "恢复动作选定：中止" in src)


def case_llm_proxy_bypass() -> None:
    """本机 LLM 请求必须绕过系统代理。

    2026-10-09 实测：本机 http_proxy=http://127.0.0.1:13336，而 llm_base_url 
    默认指向 http://localhost:11434/v1。urllib 的**默认 opener 会把连 localhost
    的请求也转发给代理** —— 代理连不上目标就统一回 502，把真实的"连接被拒绝"
    盖掉。后果很隐蔽：available() 恒为 False、LLM 调用静默降级成规则模板，
    报告照常生成、界面上看不出任何异常（知识问答会一直停在"只贴片段"模式）。

    这正是 PITFALLS 里"探测本机端口一律绕过代理"在**产品代码**里的落地。
    行为级验证（真请求、比对 502 与 10061 的差异）由真机脚本承担；
    这里断言的是**结构**：包装存在、且没有别处再裸调 urlopen。
    """
    import inspect

    from ..runtime import model_client as mc

    src_mc = inspect.getsource(mc)
    check("R16 提供本机绕代理的 urlopen 包装",
          hasattr(mc, "_urlopen") and hasattr(mc, "_LOCAL_HOSTS"))
    check("R17 本机名单覆盖 127.0.0.1 与 localhost",
          "127.0.0.1" in mc._LOCAL_HOSTS and "localhost" in mc._LOCAL_HOSTS,
          str(mc._LOCAL_HOSTS))
    bare = src_mc.count("urllib.request.urlopen(")
    check("R18 除包装内部的兜底分支外，不再有裸 urlopen（否则又会被代理吞掉）",
          bare == 1, "裸 urlopen 调用数=%d（应为 1）" % bare)
    check("R19 包装内对本机地址使用空 ProxyHandler",
          "ProxyHandler({})" in inspect.getsource(mc._urlopen))


def case_llm_decisions_bounded() -> None:
    """新增的三处 LLM 用途：边界必须和 replan_choose 一样硬。

    2026-10-10 加。改动内容：
      ① 算例名消歧（case_disambiguate）—— 用户点名的算例不存在时，在**真实可用
         清单**里找最近的，产出确认问句；**不自动改算例去算**。
      ② 失败诊断（failure_diagnose）—— 把"为什么没收敛、系统做了什么"讲成人话；
         这段文字不进证据链、不改数值。
      ③ 澄清反问（clarify_ask）—— 按触发原因（置信不足/缺算例/意图对不上）追问。

    三者共同的红线：**面向用户的文案里不得出现候选外的算例名**，
    以及**不得冒出事实里没有的小数**（模型编数字是本项目第一类不可接受缺陷）。
    """
    import inspect
    import types
    from unittest import mock

    from ..application.service import ApplicationService
    from ..prompts import get_prompt, prompt_versions
    from ..runtime import intent as it
    from ..runtime import model_client as mc
    from ..web import chat_api as ca

    # ---- A. 三个提示词都注册且带占位符 ----
    for pid, holders in (
        ("case_disambiguate", ("{candidates}", "{message}")),
        ("failure_diagnose", ("{facts}",)),
        ("clarify_ask", ("{reason}", "{slots}", "{candidates}", "{message}")),
    ):
        p = get_prompt(pid)
        check(f"D1 提示词 {pid} 已注册且占位符齐全",
              all(h in p for h in holders), pid)
        check(f"D2 {pid} 进入版本集（事后可追溯）", pid in prompt_versions())

    check("D3 消歧提示词明文禁止编造算例名",
          "绝对不许编造算例名" in get_prompt("case_disambiguate"))
    check("D4 诊断提示词明文禁止新增事实与数值",
          "只允许使用下面「事实」里出现的内容" in get_prompt("failure_diagnose"))
    check("D5 澄清提示词明文要求不执行/不报数",
          "不要执行、不要下结论、不要报任何数字" in get_prompt("clarify_ask"))

    # ---- B. 结构：内部/上传/内置三类候选同源，不再两处硬编码 ----
    check("D6 内置算例名单只有一处定义（避免两处白名单漂移）",
          isinstance(ca._BUILTIN_CASE_NAMES, tuple)
          and all(ca._is_known_builtin_case(n) for n in ca._BUILTIN_CASE_NAMES),
          str(ca._BUILTIN_CASE_NAMES))
    check("D7 chat_api 已调用消歧与澄清入口（防回归：不能被改回写死文案）",
          "_case_not_found_reply(" in inspect.getsource(ca)
          and "_clarify_reply(content, ir, session_id)" in inspect.getsource(ca))

    # ---- C. 算例消歧：白名单是硬约束 ----
    cands = [{"id": "ieee33", "kind": "builtin", "hint": "内置算例"},
             {"id": "case_tx", "kind": "builtin", "hint": "内置算例"}]

    class _Stub(mc.ModelClient):
        def __init__(self, reply, up=True):
            self.config = types.SimpleNamespace(llm_intent_timeout_s=8)
            self._reply = reply
            self._up = up

        def available(self):
            return self._up

        def _chat(self, messages, timeout=None, json_mode=True, purpose="chat"):
            if isinstance(self._reply, Exception):
                raise self._reply
            return self._reply

    picked, conf, q, _why = _Stub(
        '{"match":"ieee33","confidence":0.9,"question":"你要找的是 ieee33 吗？"}'
    ).disambiguate_case("算天枢33", cands)
    check("D8 白名单内的匹配被接受并回报置信度",
          picked == "ieee33" and abs(conf - 0.9) < 1e-6, f"{picked!r}/{conf}")

    picked, _, _, _ = _Stub(
        '{"match":"case_999","confidence":0.99,"question":"x"}'
    ).disambiguate_case("算 case_999", cands)
    check("D9 白名单外的匹配被拒绝（模型不能自己发明算例）", picked is None, repr(picked))

    picked, _, q, _ = _Stub(
        '{"match":null,"confidence":0.2,"question":"你要找的是 case_999 吗？"}'
    ).disambiguate_case("算 case_999", cands)
    check("D10 问句里出现候选外的算例名 → 问句整段丢弃",
          picked is None and q == "", repr(q))

    picked, _, _, _ = _Stub("这不是 JSON").disambiguate_case("x", cands)
    check("D11 非 JSON 输出被拒绝", picked is None)
    picked, _, _, _ = _Stub("{}", up=False).disambiguate_case("x", cands)
    check("D12 模型不可用时返回空（调用方退回确定性文案）", picked is None)
    picked, _, _, _ = _Stub(RuntimeError("boom")).disambiguate_case("x", cands)
    check("D13 模型抛异常时返回空（失败路径绝不外抛）", picked is None)
    picked, _, _, _ = _Stub('{"match":"ieee33"}').disambiguate_case("x", [])
    check("D14 没有候选时直接返回空（不浪费一次调用）", picked is None)

    # ---- D. 失败诊断：不许编造小数，且必须有兜底 ----
    facts = {"error_code": "NON_CONVERGED", "final_status": "validation_failed",
             "replan": {"strategy": "downgrade_pv_to_pq", "reason": "多机争抢调压",
                        "tolerance": {"from": 0.0001, "to": 0.001}}}
    good = "本次求解未一次收敛（NON_CONVERGED），系统把电压控制型电源降级为恒功率注入。降级后这些电源不再支撑电压，电压水平应按下限理解。"
    txt, src = _Stub(good).diagnose_failure(facts, "FALLBACK")
    check("D15 通过校验的诊断被采用并标注来源", txt == good and src == "llm", repr(src))

    bad = good.replace("电压水平应按下限理解。", "最低电压可能低到 0.8765 pu。")
    txt, src = _Stub(bad).diagnose_failure(facts, "FALLBACK")
    check("D16 出现事实里没有的小数（编造数值）→ 退回确定性模板",
          txt == "FALLBACK" and src == "rule", repr(txt[:40]))

    txt, src = _Stub("补" * 700).diagnose_failure(facts, "FALLBACK")
    check("D17 诊断写成长篇（超 600 字）→ 视为不可用（说明不该淹没结论）",
          txt == "FALLBACK" and src == "rule", "len=%d" % len(txt))
    txt, src = _Stub("模型不可用", up=False).diagnose_failure(facts, "FALLBACK")
    check("D18 模型不可用 → 退回确定性模板", txt == "FALLBACK" and src == "rule")

    # 兜底文案必须把 replan.reason（模型当初的判断依据）说出来 ——
    # 改动前它只写进 replan.json，用户界面里完全看不到。
    fb = ApplicationService._diagnosis_fallback(None, facts["replan"])
    check("D19 兜底文案包含降级事实与模型当初的判断依据",
          "降级" in fb and "多机争抢调压" in fb, fb[:60])

    # ---- E. 澄清反问：只做追问，编造算例名即退回 ----
    txt, src = _Stub("你是想重新计算，还是查上次的结果？").ask_clarify("再弄一下", "意图不明确", {}, [])
    check("D20 正常追问被采用", txt and src == "llm", repr(txt))
    txt, _ = _Stub("").ask_clarify("x", "r", {}, [])
    check("D21 过短输出被拒绝（退回固定文案）", txt is None)
    txt, _ = _Stub("你要算的是 case_777 吗？").ask_clarify("x", "missing_case", {}, cands)
    check("D22 追问里出现候选外算例名 → 退回固定文案", txt is None, repr(txt))
    txt, _ = _Stub("你要算的是 ieee33 吗？").ask_clarify("x", "missing_case", {}, cands)
    check("D23 追问引用候选内的算例名 → 允许", txt is not None)

    # ---- F. 澄清原因码必须由 decide() 填出来（否则澄清层无从"按缺项问"） ----
    cfg = types.SimpleNamespace(intent_confirm_threshold=0.55)
    cases = [
        (it.IntentResult(intent=it.INTENT_COMPUTE, confidence=0.9, source="llm"),
         it.CLARIFY_MISSING_CASE, "想算但没说算例"),
        (it.IntentResult(intent=it.INTENT_COMPUTE, confidence=0.2, source="llm"),
         it.CLARIFY_LOW_CONFIDENCE, "置信不足"),
        (it.IntentResult(intent="no_such_intent", confidence=0.9, source="llm"),
         it.CLARIFY_UNKNOWN_INTENT, "意图对不上"),
    ]
    for ir_stub, want, label in cases:
        with mock.patch.object(it, "classify", return_value=ir_stub):
            action, out = it.decide("帮我算", {}, cfg)
        check(f"D24 澄清原因码正确：{label}",
              action == it.ACTION_CLARIFY and out.clarify_reason == want,
              f"action={action} reason={out.clarify_reason!r}")

    # ---- G. 边界：诊断只是"说明"，不得进入证据链（否则会变成一条结论） ----
    src_diag = inspect.getsource(ApplicationService._diagnose)
    src_fb = inspect.getsource(ApplicationService._diagnosis_fallback)
    check("D25 诊断实现不触碰证据链 / 发布门",
          "EvidenceItem" not in src_diag and "EvidenceItem" not in src_fb
          and "publication_gate" not in src_diag and "publication_gate" not in src_fb)
    src_run = inspect.getsource(ApplicationService.run)
    check("D26 诊断在终态落盘之前生成（否则 state.json 里没有它）",
          0 <= src_run.find("self._diagnose(ctx, state)") < src_run.find('_persist_state(state, workspace, "final")'),
          "diagnose=%d persist=%d" % (
              src_run.find("self._diagnose(ctx, state)"),
              src_run.find('_persist_state(state, workspace, "final")')))


def main() -> int:
    print("=" * 78)
    print("OpenDSS COM 引擎「中毒自愈」回归测试")
    print("=" * 78)
    case_timeout_then_recover()
    case_no_respawn_on_success()
    case_dss_ctx_stability()
    case_diagnostics_shape()
    case_no_cross_generation_pollution()
    case_replan_choice()
    case_llm_proxy_bypass()
    case_llm_decisions_bounded()
    print("-" * 78)
    print(f"=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
