# -*- coding: utf-8 -*-
"""判据自检门禁：把「验证脚本自己的判据必须被验证过」变成永久的、必经的关卡。

运行：`.venv/Scripts/python.exe -m dpf_agent.tests.test_judgment_selftest`

## 为什么需要这个套件（起因不是设想，是当天发生的）

2026-10-10 写「格式转换保真度实测」工具时，第一版在**同一个小时、同一个脚本内**
同时踩了两个坑：

- **假绿**：五种变体全部求解失败，工具却打出"逐项一致"
  （判据是布尔 + 默认 pass：`None` vs `None` 不落进任何"超差"分支 → 掉进"一致"）
- **假红**：残差 `8.2e-05` vs `1.2e-06` 被判"超差 98%"
  （把"相对差"这个判据从**可比量**套到了**绝对量**上；两者其实都远低于容差）

翻项目记忆后确认：**这不是"不知道"** ——
`PITFALLS.md` A 节（标题即"每条都犯过两次以上"）已明确写着"判据写错 → 要么假红
要么假绿，比不写更危险（假绿就没人发现）"；"假绿"一词在项目记忆里已累计出现
**14 次**；2026-10-04 还专门写过 `fmt_check.py` 做假绿探测并总结成"可复用"。

⇒ **文档型教训对这个错误类别无效**。唯一有效的控制是**可执行、且必经的关卡**。
本套件就是把那个关卡钉进统一测试入口，让它不可能被绕过。

## 覆盖三层

- **A. 判据本身能区分三态**：6 项构造样例（含本次两个坑的回归用例）
- **B. 自检不是空洞的**：注入三种"错误判据"，自检**必须**报错
  （只断言"自检通过"是不够的 —— 一个永远返回"通过"的自检也能过这一关）
- **C. 结构约束**：三态常量存在、`不可判定` 被当作"不允许出结论"、
  残差判据里不得再出现相对差写法
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


#: 参考对象上"不参与比对"的合法字段（身份/出处/别名/备注）。
#: 除此之外，加载器产出而比对器不用的字段就属于"声明了没用"，应当报出来。
_FIELD_ALLOWLIST = {"source", "aliases", "notes"}


def _ref_field_audit(loader_src: str, scorer_src: str) -> dict[str, list[str]]:
    """从源码 AST 对账参考字段的**生产**与**消费**。

    - `missing`：比对器读了 `ref.<字段>`，而加载器从不产出 → 该参考值**必然静默失效**
      （2026-10-10 的 `loss_kw` 就是这一形态：官方网损参考值一次都没比过，
      报告显示"期望 None"，看起来像官方没给值）。
    - `unused`：加载器产出了字段，却没有任何地方消费 → "声明了没用"
      （同一天的 `loads` 就是这一形态）。

    之所以要做成自动对账而不是手写断言：手写只能覆盖写它时想到的字段，
    下一批字段漏掉时它会照常通过。
    """
    def _attr_on(node: ast.AST, var: str) -> set[str]:
        return {n.attr for n in ast.walk(node)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == var}

    consumed = _attr_on(ast.parse(textwrap.dedent(scorer_src)), "ref")
    tree = ast.parse(textwrap.dedent(loader_src))
    produced: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Reference":
            produced |= {kw.arg for kw in n.keywords if kw.arg}
    return {"missing": sorted(consumed - produced), "unused": sorted(produced - consumed)}


def _judge_module():
    from ..evals import run_conversion_fidelity as m

    return m


def case_judgment_selftest() -> None:
    m = _judge_module()

    # ---- A. 判据自检本身必须通过 ----
    failures = m._self_test()
    check("J1 判据自检通过（6 项构造样例全过）",
          not failures, "；".join(failures))

    # 逐条列出六项覆盖，防止有人删样例却仍显示"通过"
    src_st = inspect.getsource(m._self_test)
    for i, (label, needle) in enumerate((
        ("完全相同", "两份相同"),
        ("计数差 1", "bus_count"),
        ("电压差 0.5 pu", "0.40000"),
        ("缺值（假绿回归）", "= None"),
        ("残差容差内（假红回归）", "1e-07"),
        ("残差超容差", "1e-01"),
    ), start=2):
        check(f"J{i} 自检覆盖「{label}」", needle in src_st, label)

    # ---- B. 自检必须不是空洞的：注入错误判据，自检要能报错 ----
    orig_gauge = m._judge_gauge
    orig_res = m._judge_residual
    orig_cnt = m._judge_counts
    try:
        # B1. 今天的假红：残差改用相对差
        def _bad_red(a, b, tol):
            if a is None or b is None:
                return (m.V_UNKNOWN, "x")
            rel = abs(float(b) - float(a)) / max(abs(float(a)), 1e-30)
            return (m.V_OK, "") if rel <= 0.25 else (m.V_BAD, "rel=%.2f" % rel)

        m._judge_residual = _bad_red
        check("J8 注入「残差用相对差」（假红）→ 自检必须报错",
              bool(m._self_test()), "；".join(m._self_test()))
        m._judge_residual = orig_res

        # B2. 今天的假绿：缺值当一致
        def _bad_green(a, b, tol):
            return (m.V_OK, "")

        m._judge_gauge = _bad_green
        check("J9 注入「缺值当一致」（假绿）→ 自检必须报错",
              bool(m._self_test()), "；".join(m._self_test()))
        m._judge_gauge = orig_gauge

        # B3. 反方向：一律判失败（过严），自检同样要报错
        def _always_bad(a, b, tol):
            return (m.V_BAD, "always")

        m._judge_gauge = _always_bad
        check("J10 注入「一律判超差」（过严）→ 自检必须报错",
              bool(m._self_test()), "；".join(m._self_test()))
        m._judge_gauge = orig_gauge

        # B4. 离散量判据被改成"近似相等"也应被抓出
        def _loose_counts(a, b):
            if a is None or b is None:
                return (m.V_UNKNOWN, "x")
            try:
                return (m.V_OK, "") if abs(float(a) - float(b)) <= 2 else (m.V_BAD, "")
            except (TypeError, ValueError):
                return (m.V_OK, "") if a == b else (m.V_BAD, "")

        m._judge_counts = _loose_counts
        check("J11 注入「计数允许 ±2」→ 自检必须报错",
              bool(m._self_test()), "；".join(m._self_test()))
        m._judge_counts = orig_cnt
    finally:
        # 无论断言结果如何都要还原，绝不给后续套件留一个被改过判据的模块
        m._judge_gauge = orig_gauge
        m._judge_residual = orig_res
        m._judge_counts = orig_cnt

    # ---- C. 结构约束 ----
    check("J12 三态常量齐备（一致 / 超差 / 不可判定）",
          m.V_OK == "一致" and m.V_BAD == "**超差**" and m.V_UNKNOWN == "不可判定",
          f"{m.V_OK}/{m.V_BAD}/{m.V_UNKNOWN}")

    # `不可判定` 必须被当作"不允许出结论"，否则缺值又会长回"通过"
    rows = [{"check": "x", "ref": None, "got": None, "verdict": m.V_UNKNOWN, "detail": ""}]
    check("J13 `不可判定` 被计入『不允许出结论』",
          bool(m._bad_rows(rows)))
    rows_ok = [{"check": "x", "ref": 1, "got": 1, "verdict": m.V_OK, "detail": ""}]
    check("J14 `一致` 不被计入『不允许出结论』", not m._bad_rows(rows_ok))

    src_res = inspect.getsource(m._judge_residual)
    check("J15 残差判据用『各自 ≤ 容差』，不再用相对差（本次假红根因）",
          "tol" in src_res and "/ max(" not in src_res)
    check("J16 缺失值一律走『不可判定』而不是任何『一致』分支",
          src_res.count("V_UNKNOWN") >= 1
          and "V_UNKNOWN" in inspect.getsource(m._judge_gauge)
          and "V_UNKNOWN" in inspect.getsource(m._judge_counts))

    # 判据必须按量的性质分派，而不是一套判据套所有量
    src_cmp = inspect.getsource(m.compare)
    check("J17 compare 按量的性质分派判据（离散/可比/绝对各自成函数）",
          all(fn in src_cmp for fn in ("_judge_counts", "_judge_gauge", "_judge_residual")))

    # ---- D. 同类缺陷的横向排查：`evals/run_eval.py` 曾把"没比对"读成"通过" ----
    #
    # 实测：改动前 `run_eval --from-outputs` 报"通过 170 / 未评估 0"，
    # 改动后为"通过 69 / 未评估 101" —— **101 条被算成"通过"的任务，
    # 其实一次参考比对都没发生**（它们只是"收敛了"，而收敛与"结果对不对"无关）。
    # 这是同族假绿在另一个比对器里的实例，必须一并钉住。
    from ..evals import run_eval as ev

    # D1. 没有参考比对 → 不许判 ok
    r = ev.CaseReport(task_id="t1", case_ref="c")
    r.checks.append(ev.Check("收敛", True, True, "ok"))          # 自证项，非参考比对
    check("J18 无参考比对时 overall 必须是『na』（不许靠一条『收敛 ok』判通过）",
          r.overall() == "na", r.overall())

    # D2. 有参考比对且全 ok → 才是 ok
    r2 = ev.CaseReport(task_id="t2", case_ref="c")
    r2.checks.append(ev.Check("母线数", 3, 3, "ok", is_reference=True))
    check("J19 有参考比对且全通过时 overall 才是『ok』", r2.overall() == "ok", r2.overall())

    # D3. 参考比对全部 na（值缺失）→ 同样不得判 ok
    r3 = ev.CaseReport(task_id="t3", case_ref="c")
    r3.checks.append(ev.Check("收敛", True, True, "ok"))
    r3.checks.append(ev.Check("最低电压(pu)", None, None, "na", is_reference=True))
    check("J20 参考比对全部为 na 时 overall 必须是『na』", r3.overall() == "na", r3.overall())

    # D4. 缺值不能再"一条判据都不产生" —— 必须显式留 na 占位
    src_score = inspect.getsource(ev.score_result)
    check("J21 值缺失时显式记 na 占位（不再是静默跳过）",
          src_score.count("参考值或实测值缺失，本次未比对") >= 3,
          str(src_score.count("参考值或实测值缺失，本次未比对")))

    # D5. 只有 na 的算例组，汇总里不得判「一致」（零证据读成通过）
    md = ev.render_markdown([r3])
    check("J22 只有未评估的算例组不得在汇总里判『一致』",
          "未评估" in md and "| 一致 |" not in md and "| 一致（" not in md,
          [ln for ln in md.splitlines() if ln.startswith("| ")][:3])

    # D6. 「未评估」必须出现在报告头部（不能只藏在明细里）
    check("J23 报告头部单列『未评估』计数", "未评估" in md.split("## 按算例汇总")[0])


    # ---- E. 横向排查（续）：`run_eval` 的**参考匹配**与**参考字段加载** ----
    #
    # 同一天、同一族缺陷的另两个实例，危害比"漏比"更大 —— 它们会**伪造结论**：
    #
    # ① 参考错配：`alias in 整条路径` 子串匹配。manifest 里四个官方算例的文件名都是
    #    `Master.dss`，其词干 `master` 被当作别名后，**任何以 Master.dss 结尾的上传
    #    算例都命中第一个官方算例**。实测 266 条互不相干的产物（母线数 5/50/180/200…）
    #    被挂到 13Bus（16 母线）上，报告里凭空多出 266 条"失败"；
    #    另有 18 条 `case42_*` 因 `'case4'` 是子串而被挂到 `case4`；8 条按母线数推定。
    # ② 参考字段漏加载：`score_result` 在用 `ref.loss_kw` 比对，`_reference_from_case`
    #    却**从未读取** manifest 的 `loss_kw` —— 官方声明的网损参考值（13Bus 112.4 kW、
    #    4Bus 567.6 kW）一次都没参与比对，报告里显示"期望 None"，看起来像官方没给值。
    #
    # 两者的共同点：**"没有比对"被呈现成"有结论"**。故一并钉住。
    ev2 = ev

    # E1. 通用文件名不得进入候选片段
    cands = ev2._case_ref_candidates("C:/x/uploads/s1/13Bus/Master.dss")
    check("J24 通用文件名（master）不得作为匹配片段",
          "master" not in cands and "13bus" in cands, str(cands))

    # E2. 子串不得命中（本次 18 条 case42 → case4 的根因）
    refs_e = [
        ev2.Reference(name="case4", source="t", aliases=("case4",)),
        ev2.Reference(name="13Bus", source="t", buses=16, aliases=("13bus",)),
    ]
    got = ev2.match_reference(
        "C:/x/uploads/s1/case42_single_phase_transformer/Master.dss", refs_e)
    check("J25 子串不得命中：`case42_...` 不许匹配到 `case4`", got is None,
          got.name if got else "None")

    # E3. 路径片段精确相等时必须命中（别把真实匹配也误杀）
    got2 = ev2.match_reference("C:/x/uploads/s1/13Bus/Master.dss", refs_e)
    check("J26 路径片段精确相等时按算例名命中",
          got2 is not None and got2.name == "13Bus", got2.name if got2 else "None")

    # E4. 不许按母线数推定（本次 8 条空 case_ref 被推定到官方 4Bus）
    params = list(inspect.signature(ev2.match_reference).parameters)
    check("J27 match_reference 不接受母线数参数（不做母线数推定）",
          params == ["case_ref", "refs"], str(params))
    check("J28 case_ref 为空时一律不匹配（而不是按规模猜）",
          ev2.match_reference("", refs_e) is None)

    # E5. 共用别名必须被丢弃 —— 这是 `master` 被清掉的机制，且对新增算例同样生效
    r_a = ev2.Reference(name="A", source="t", aliases=("master", "a"))
    r_b = ev2.Reference(name="B", source="t", aliases=("master", "b"))
    ev2._prune_shared_aliases([r_a, r_b])
    check("J29 被多个参考共用的别名一律丢弃（零识别信息）",
          "master" not in r_a.aliases and "master" not in r_b.aliases
          and "a" in r_a.aliases and "b" in r_b.aliases,
          f"A={r_a.aliases} B={r_b.aliases}")

    # E6. manifest 声明的参考字段必须**全部**被加载（漏一个就等于该参考值不存在）
    fake = {
        "id": "X", "file": "X/Master.dss", "buses": 2, "lines_expected": 1,
        "transformers": 0, "loads": 3, "loss_kw": 10.0,
        "nr_validation": {"vmin_pu": 0.9, "vmin_phase_pu": 0.8},
    }
    rf = ev2._reference_from_case(fake, "X", "suite")
    check("J30 manifest 声明的参考字段必须全部被加载",
          (rf.buses, rf.lines, rf.transformers, rf.loads, rf.loss_kw,
           rf.vmin_pu, rf.vmin_phase_pu) == (2, 1, 0, 3, 10.0, 0.9, 0.8),
          f"buses={rf.buses} lines={rf.lines} tr={rf.transformers} loads={rf.loads} "
          f"loss={rf.loss_kw} vmin={rf.vmin_pu} vmin_phase={rf.vmin_phase_pu}")

    # E7. 加载了就必须真的比 —— 否则参考值只是躺在 dataclass 里
    ref_x = ev2.Reference(name="X", source="t", buses=2, lines=1, transformers=0,
                          loads=3, loss_kw=10.0, vmin_pu=0.9, vmin_phase_pu=0.8)
    res_x = {
        "bus_results": [{}, {}], "branch_results": [{}], "transformer_results": [],
        "system_summary": {"min_voltage_pu": 0.9, "max_voltage_pu": 1.0,
                           "min_phase_voltage_pu": 0.8},
        "loss_summary": {"total_loss_kw": 10.0},
        "solver_status": {"converged": True, "iterations": 1},
    }
    rep_x = ev2.score_result("t", res_x, "X", [ref_x], profile={"load_count": 3})
    items = {c.item: c for c in rep_x.checks}
    need = ("负荷元件数", "总有功网损(kW)", "真实最低相电压(pu)")
    check("J31 声明了的参考值必须真的产生比对（负荷数 / 网损 / 相电压）",
          all(items.get(k) is not None and items[k].is_reference
              and items[k].verdict == "ok" for k in need),
          str({k: (items.get(k).verdict if items.get(k) else "缺失") for k in need}))

    # E8. 实测值取不到 → na 占位（不是"不产生判据"，那是缺口不可见）
    rep_y = ev2.score_result("t", res_x, "X", [ref_x], profile=None)
    items_y = {c.item: c for c in rep_y.checks}
    check("J32 网络画像取不到时『负荷元件数』记为 na 占位而不是消失",
          items_y.get("负荷元件数") is not None
          and items_y["负荷元件数"].verdict == "na",
          str(items_y.get("负荷元件数").verdict if items_y.get("负荷元件数") else "缺失"))

    # E9. 匹配口径必须写进报告（结论的一部分，不许只藏在代码里）
    md2 = ev2.render_markdown([r2])
    check("J33 报告写明参考匹配口径（不子串匹配 / 不按母线数推定）",
          "不做子串匹配" in md2 and "不按母线数推定" in md2)

    # ---- F. 把"参考字段对账"从手写断言升级为**自动对账** ----
    #
    # J30/J31 是**针对本次漏掉的那三个字段**手写的。手写断言的毛病很明确：
    # 它只覆盖写它时想到的字段，下一批字段漏了它照样通过 —— 这正是"文档型教训
    # 拦不住人"在测试里的翻版。这里改成从源码 AST 自动对账，新增字段自动纳入。
    audit = _ref_field_audit(
        inspect.getsource(ev2._reference_from_case), inspect.getsource(ev2.score_result))
    check("J34 参考字段自动对账：比对器读的每个 ref.<字段> 都必须能被加载器产出",
          not audit["missing"], f"缺失 {audit['missing']}")
    check("J35 加载器产出却无人消费的字段必须在允许清单内（否则就是『声明了没用』）",
          set(audit["unused"]) <= _FIELD_ALLOWLIST,
          f"越界 {sorted(set(audit['unused']) - _FIELD_ALLOWLIST)}")

    # 关键：证明这个自动对账**不是空洞的** —— 造一个"漏了 loss_kw"的加载器（本次的真实形态），
    # 必须被抓出来。没有这一条，J34 只是"当前恰好对得上"。
    fake_loader = (
        "def _reference_from_case(case, case_id, suite, file_hint=''):\n"
        "    return Reference(name='X', source='s', buses=1)\n"
    )
    bad = _ref_field_audit(fake_loader, inspect.getsource(ev2.score_result))
    check("J36 自动对账非空洞：加载器漏字段时（本次 loss_kw 的形态）必须报出缺口",
          "loss_kw" in bad["missing"] and "vmin_phase_pu" in bad["missing"],
          f"missing={bad['missing']}")


def main() -> int:
    print("=" * 78)
    print("判据自检门禁：验证脚本自己的判据必须被验证过")
    print("=" * 78)
    case_judgment_selftest()
    print("-" * 78)
    print(f"=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    for name in FAIL:
        print(f"  ✗ {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
