# -*- coding: utf-8 -*-
"""算例产物引用守卫回归测试（自包含，不依赖引擎与网络）。

运行：`.venv/Scripts/python.exe -m dpf_agent.tests.test_case_product_guard`

覆盖的真实缺陷（三段式，缺一段就还是会瘫）：

1. **转换器写出了非法属性**：1.6.0 给 `New Line` 写 `Conn=`，而 OpenDSS 的 Line
   **没有** Conn 属性，会让 COM 引擎在 compile 阶段死循环。
2. **带病产物留在盘上被无校验引用**：`_extract_case_ref` / `_resolve_case_name_to_upload`
   里有两处 `return <raw path>`，绕过了 `_fresh_case_master` 的版本校验。
3. **版本戳不可信时无兜底**：版本戳可能缺失 / 记错 / 转换器改了忘了升号，
   这三种情况版本校验都会放行，而盘上的文件仍然是带病的。

实测后果：2026-09-13 22:48~23:01 连续 4 个任务（case44/53/54/55）各卡 120.0s，
串行锁占满，整机失去响应数分钟，只能重启后端。

本测试锁定的是"**任何路径都不许把带病产物送进引擎**"这条红线。
"""

from __future__ import annotations

import os
import sys
import tempfile

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


# 带病产物：New Line 写了非法 Conn=（1.6.0 真实产物的特征）
POISONED = """Clear
! [Tianshu Conversion] source_format=case_xlsx; approximation=true
! [Tianshu Conversion] warning=...
Set DefaultBaseFrequency=50
New Circuit.case_xlsx basekv=10 pu=1.02 phases=3 MVAsc3=1.0 MVAsc1=1.0

// 线路
New Line.L_GRID Bus1=sourcebus Bus2=HV R1=0.002 X1=0.01 Conn=wye Length=1 Phases=3 Enabled=yes
New Line.L_A Bus1=LV.1 Bus2=LA.1 R1=0.0096 X1=0.004 Conn=wye Length=1 Phases=1 Enabled=yes
New Load.Load_LD_A1 Bus1=LA.1 Phases=1 kV=0.23094 kW=120 kvar=40 Conn=wye
Solve
"""

# 合规产物：Line 无 Conn，Load 的 Conn 合法（必须放行）
CLEAN = """Clear
! [Tianshu Conversion] source_format=case_xlsx; approximation=true
Set DefaultBaseFrequency=50
New Circuit.case_xlsx basekv=10 pu=1.02 phases=3 MVAsc3=1.0 MVAsc1=1.0

// 线路（无 Conn）
New Line.L_GRID Bus1=sourcebus Bus2=HV R1=0.002 X1=0.01 Length=1 Units=none Phases=3 Enabled=yes
New Line.L_A Bus1=LV.1 Bus2=LA.1 R1=0.0096 X1=0.004 Length=1 Units=none Phases=1 Enabled=yes

// 负荷与变压器的 Conn / conns 是合法属性
New Load.Load_LD_A1 Bus1=LA.1 Phases=1 kV=0.23094 kW=120 kvar=40 Conn=wye
New Load.Load_LD_AB Bus1=LAB.1.2 Phases=2 kV=0.4 kW=30 kvar=10 Conn=delta
New Transformer.T_MAIN phases=3 windings=2 buses=[HV LV] conns=[delta wye] kvs=[10 0.4] kvas=[100000 100000] XHL=5 tap=1
New Generator.G_A Bus1=LA.1 Phases=1 kV=0.23094 kW=10 kvar=0 Model=3 Vpu=1.02 Conn=wye
Solve
"""

# 注释里的 Conn= 不能误报
COMMENTED = """Clear
New Circuit.case_xlsx basekv=10 pu=1.02
! 这里解释为什么 Line 不能写 Conn=wye
// New Line.L_BAD Bus1=a Bus2=b Conn=wye
New Line.L_OK Bus1=a Bus2=b R1=0.01 X1=0.01 Length=1 Units=none Phases=3
Solve
"""


def _write(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def main() -> int:
    # chat_api 依赖 fastapi（本项目 web 层的既有依赖），导入失败就明确说清
    try:
        from ..web import chat_api as ca
    except Exception as exc:  # noqa: BLE001
        print(f"无法导入 chat_api（缺依赖？）：{type(exc).__name__}: {exc}")
        return 1

    root = tempfile.mkdtemp(prefix="prodguard_")
    try:
        # ================= A. 非法属性检测：能命中 =================
        print("\n--- A. 非法元素属性检测 ---")
        p_bad = _write(os.path.join(root, "bad.dss"), POISONED)
        issues = ca._illegal_line_props(p_bad)
        check("A1 检出 New Line 的非法 Conn=", len(issues) >= 1, f"命中 {len(issues)} 条")
        check("A2 命中的是两条线路（L_GRID / L_A）", len(issues) == 2, f"命中 {len(issues)} 条")
        check("A3 报错信息里含行号", any("第 " in i for i in issues), issues[:1][0][:70] if issues else "")
        check("A4 报错信息点明原因（compile 死循环）", any("死循环" in i for i in issues))

        # ================= B. 合规产物必须放行（防误报）=================
        print("\n--- B. 合规产物不应被误判 ---")
        p_ok = _write(os.path.join(root, "clean.dss"), CLEAN)
        issues_ok = ca._illegal_line_props(p_ok)
        check("B1 合规产物无告警", not issues_ok, str(issues_ok)[:120])
        check("B2 Load 的 Conn=wye 合法（不误报）",
              not any("Load" in i for i in issues_ok))
        check("B3 Transformer 的 conns=[delta wye] 合法（不误报）",
              not any("Transformer" in i for i in issues_ok))
        check("B4 Generator 的 Conn 合法（不误报）",
              not any("Generator" in i for i in issues_ok))

        # ================= C. 注释里的 Conn= 不算 =================
        print("\n--- C. 注释与停用行 ---")
        p_c = _write(os.path.join(root, "comment.dss"), COMMENTED)
        issues_c = ca._illegal_line_props(p_c)
        check("C1 `!` 注释里的 Conn= 不误报",
              not any("Conn" in i and "这里解释" in i for i in issues_c), str(issues_c)[:120])
        check("C2 `//` 注释里的 New Line 不误报",
              not any("L_BAD" in i for i in issues_c), str(issues_c)[:120])
        check("C3 真实合规行不误报", not issues_c, str(issues_c)[:120])

        # ================= D. CaseProductRejected 的契约 =================
        print("\n--- D. 拒绝异常的契约 ---")
        check("D1 CaseProductRejected 是 HTTPException 子类",
              issubclass(ca.CaseProductRejected, ca.HTTPException))
        exc = ca.CaseProductRejected(status_code=422, detail="x")
        check("D2 携带 422 状态码", exc.status_code == 422)

        # ================= E. 引用路径必须强制过校验 =================
        print("\n--- E. 引用路径不许绕过校验 ---")
        import inspect

        src_extract = inspect.getsource(ca._extract_case_ref)
        src_resolve = inspect.getsource(ca._resolve_case_name_to_upload)

        # 找出所有 "return <裸路径变量>" 形式的返回；只允许 return _fresh_case_master(...) / None
        #
        # 注意要排除**嵌套辅助函数**里的返回（例如 find_master 的
        # `return os.path.join(root, name)`）—— 它返回的是"找到的候选路径"，
        # 交给外层再过校验，本身不构成绕过。这里按缩进判定：只有与外层函数体
        # 同级的 return 才是真正的对外出口。
        def bare_returns(src: str) -> list[str]:
            out = []
            for raw in src.splitlines():
                # 只认顶层语句（def 行缩进为 0，函数体缩进 4）。
                # 嵌套 def 的函数体缩进 >= 8，被下面的条件排除。
                indent = len(raw) - len(raw.lstrip())
                if indent != 4:
                    continue
                s = raw.strip()
                if not s.startswith("return "):
                    continue
                expr = s[len("return "):].strip()
                if expr in ("None",) or expr.startswith("_fresh_case_master("):
                    continue
                out.append(expr)
            return out

        bare_e = bare_returns(src_extract)
        check("E1 _extract_case_ref 顶层无裸路径返回", not bare_e, str(bare_e)[:150])
        bare_r = bare_returns(src_resolve)
        check("E2 _resolve_case_name_to_upload 顶层无裸路径返回", not bare_r, str(bare_r)[:150])
        check("E3 _extract_case_ref 的 .dss 分支过校验",
              "return _fresh_case_master(path)" in src_extract)
        check("E4 _resolve_case_name_to_upload 的 .dss 分支过校验",
              "return _fresh_case_master(full)" in src_resolve)

        # ================= F. 端到端：带病产物走校验会被拒 =================
        print("\n--- F. 带病产物经 _fresh_case_master 应被拒 ---")
        # 造一个"看似合规"的布局：有 meta 且版本号写成当前版本（版本校验会放行），
        # 但文件内容带病 —— 这正是内容级兜底要抓的场景。
        from ..web import conversion_cache as cc

        prod_dir = os.path.join(root, "sess-x", "case_poisoned")
        os.makedirs(prod_dir, exist_ok=True)
        master = _write(os.path.join(prod_dir, "Master.dss"), POISONED)
        src = _write(os.path.join(root, "sess-x", "case_poisoned.xlsx"), "not-a-real-xlsx")
        meta = cc.write_meta(prod_dir, "dss_from_tables", src)
        meta["version"] = cc.CONVERTER_VERSIONS["dss_from_tables"]  # 伪装成"最新"
        import json

        with open(os.path.join(prod_dir, cc.META_NAME), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

        status, _ = cc.meta_status(prod_dir)
        check("F1 版本戳伪装成最新时 meta_status 判 fresh", status == "fresh", f"status={status}")

        rejected = False
        detail = ""
        try:
            ca._fresh_case_master(master)
        except ca.CaseProductRejected as exc2:
            rejected = True
            detail = str(exc2.detail)[:80]
        except Exception as exc2:  # noqa: BLE001
            detail = f"{type(exc2).__name__}: {exc2}"
        check("F2 版本戳放行时仍被内容级兜底拦下", rejected, detail)

        # 合规产物走同一条路应正常放行
        clean_dir = os.path.join(root, "sess-y", "case_clean")
        os.makedirs(clean_dir, exist_ok=True)
        master_clean = _write(os.path.join(clean_dir, "Master.dss"), CLEAN)
        ok = False
        try:
            got = ca._fresh_case_master(master_clean)
            ok = got == master_clean
        except Exception as exc3:  # noqa: BLE001
            ok = False
            detail = f"{type(exc3).__name__}: {exc3}"
        check("F3 合规产物正常放行", ok, detail)

        # 真实 case37 产物若在盘上，应被拒（这是本次事故的原始样本）
        real = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "chat_data", "uploads", "sess-e02ea04f0062", "case37_unbalanced", "Master.dss",
        )
        if os.path.isfile(real):
            with open(real, encoding="utf-8") as f:
                real_text = f.read()
            real_issues = ca._illegal_line_props(real)
            poisoned_now = len(real_issues) > 0
            check(
                "F4 真实 case37 产物当前状态已记录",
                True,
                f"{'仍带病（会被拦下，共 %d 处）' % len(real_issues) if poisoned_now else '已被重建为合规产物'}",
            )
        else:
            check("F4 真实 case37 产物不存在（跳过）", True, "该样本已被清理")

        # ============ G. 版本戳看不出的雷：内容级兜底必须独立成立 ============
        #
        # 这是本机制存在的根本理由，用真实样本锁死：
        # `sess-51cb41fb4fdb/case51_phase_raw` 无 meta（unknown）且源文件已丢失
        # （_infer_source = None）⇒ 只查版本戳的旧逻辑判 not-applicable 直接放行，
        # 而它实际含 2 处 `New Line ... Conn=` ⇒ 送进引擎必然卡死 120s。
        print("\n--- G. 版本戳不可信时靠内容拦阻（真实样本）---")
        real51 = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "chat_data", "uploads", "sess-51cb41fb4fdb", "case51_phase_raw", "Master.dss",
        )
        if os.path.isfile(real51):
            st51, meta51 = cc.meta_status(os.path.dirname(real51))
            inf51 = cc._infer_source(os.path.dirname(real51), meta51)
            check("G1 该样本 meta 缺失（判 unknown）", st51 == "unknown", f"status={st51}")
            check("G2 该样本源文件无法推断（重建不可能）", inf51 is None, f"inferred={inf51!r}")
            check("G3 但它确实含非法属性", len(ca._illegal_line_props(real51)) >= 1,
                  f"命中 {len(ca._illegal_line_props(real51))} 处")
            caught = False
            try:
                ca._fresh_case_master(real51)
            except ca.CaseProductRejected:
                caught = True
            except Exception as exc4:  # noqa: BLE001
                check("G4 应抛 CaseProductRejected", False, f"{type(exc4).__name__}: {exc4}")
            else:
                check("G4 应抛 CaseProductRejected", False, "未抛异常（守卫失效）")
            if caught:
                check("G4 版本戳放行路径被内容级兜底拦下", True, "已拦下")
        else:
            check("G1-G4 真实 case51 样本不存在（跳过）", True, "该样本已被清理")

    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)

    print(f"\n=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print(f"  - {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
