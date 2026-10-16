# -*- coding: utf-8 -*-
"""执行过程「思考流」文案回归测试（自包含，不依赖引擎/网络）。

锁定的是 `runtime/narration.py` 的契约 —— 它是界面上那句"人话"的**唯一**来源：
  · 每个已知阶段都必须有动作行与思考段，且**不许出现空文案**；
  · 思考段必须由「模板 + 事件自带的结构化 detail」拼出，
    取不到值才允许退化成如实转述 message，**任何情况下不许编内容**；
  · 同样的输入必须产出同样的文案（这是"可复现、可审计"的一部分：
    若过程文案由模型现场生成，跑两次的过程记录就对不上账）。

⚠️ 改动 `narration.py` 的模板后必须同步升 `NARRATION_VERSION`，
   并让本测试里"版本号非空"的断言继续通过。

运行：`.venv/Scripts/python.exe -m dpf_agent.tests.test_narration`
"""

from __future__ import annotations

import re
import sys

from ..runtime import narration as N

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


# 一个"样样齐全"的完整任务：每个阶段的最后一个事件（即界面上真正显示的那条）
SAMPLE: list[tuple[str, str, str, dict, str]] = [
    ("receive_request", "understand", "success",
     {"user_goal": "计算这个三相不平衡算例", "result_scopes": ["bus", "system"], "parser": "llm"}, ""),
    ("parse_intent", "understand", "running", {}, "正在理解…"),
    ("parse_intent", "understand", "success",
     {"parser": "llm", "user_goal": "计算这个三相不平衡算例", "result_scopes": ["bus"]}, ""),
    ("resolve_constraints", "constraints", "success",
     {"hard": "result_scopes=['bus']", "soft": "电压"}, ""),
    ("request_guard", "guard", "success", {}, "请求门校验通过"),
    ("engine_health", "verify", "running", {}, "正在检查计算引擎可用性…"),
    ("engine_health", "verify", "success", {}, "计算引擎可用，可以开始求解"),
    ("build_execution_plan", "plan", "success", {"steps": ["解析任务", "算例检查", "潮流计算"]}, ""),
    ("load_case", "tool_result", "success",
     {"case_fingerprint": "de211a53b575fb63", "dependency_files": 1}, ""),
    ("validate_case", "tool_result", "success", {"objects": "bus 12、line 11"}, ""),
    ("build_network_profile", "tool_result", "success",
     {"buses": 12, "lines": 11, "loads": 11, "load_kw": 1093.0}, ""),
    ("build_opendss_solver_profile", "tool_result", "success",
     {"profile_id": "sp-8a50974b", "params": "{'max_iterations': 15}"}, ""),
    ("run_opendss_powerflow", "tool_call", "running", {}, "run_powerflow()"),
    ("run_opendss_powerflow", "tool_result", "success",
     {"converged": True, "iterations": 2}, ""),
    ("normalize_results", "tool_result", "success", {"scope_status": "{'bus': 'ok'}"}, ""),
    ("verify_result_integrity_and_power_balance", "verify", "success",
     {"passed": True, "residual_pu": 0.0}, ""),
    ("build_evidence", "evidence", "success", {"count": 6}, ""),
    ("publish_guard", "guard", "success",
     {"gate": "关键结论 Evidence 覆盖率=100%（要求 100%）", "passed": True}, ""),
    ("export_artifacts", "tool_result", "success", {"count": 8}, ""),
    ("explain_result", "result", "success", {"source": "llm"}, ""),
    ("render_report", "result", "success", {"out_dir": "C:/x/y"}, ""),
]


def main() -> int:
    # ① 动作行：每个出现的阶段都必须有动词与对象
    bad_action = []
    for st, *_ in SAMPLE:
        a = N.action_for(st)
        if not (a.get("verb") or "").strip() or not (a.get("object") or "").strip():
            bad_action.append(st)
        if a.get("kind") not in ("read", "compute", "verify", "write"):
            bad_action.append(st + "(kind)")
    check("① 每个阶段都有动作行（动词 + 对象 + 合法种类）", not bad_action, str(bad_action))

    # ② 思考段非空 —— 界面上留一行空白比写错更糟，用户会以为"这一步什么都没做"
    notes = [N.narrate(st, et, status, det, msg) for st, et, status, det, msg in SAMPLE]
    empty = [SAMPLE[i][0] for i, n in enumerate(notes) if not n.strip()]
    check("② 每一步都有思考段（不许空文案）", not empty, str(empty))

    # ③ 带 detail 的阶段必须**不是**把 message 原样搬上来 ——
    #    那说明模板没取到值、退化成了兜底，界面上就看不到"人话"了
    degraded = []
    for st, et, status, det, msg in SAMPLE:
        if not det:
            continue
        note = N.narrate(st, et, status, det, msg)
        if msg and note.strip().startswith(msg.strip()[:20]):
            degraded.append(st)
    check("③ 有 detail 时思考段不退化（不是复述 message）", not degraded, str(degraded))

    # ④ 真实数值必须进到文案里（"模板 + 真实数值"里的那个"数值"）
    by_stage = {st: n for (st, *_rest), n in zip(SAMPLE, notes)}
    check("④ 真实数值进入文案（母线条数 / 迭代次数 / 指纹）",
          all(k in by_stage.get("build_network_profile", "")
              for k in ("12 条母线", "11 条线路", "1093"))
          and "迭代 2 次" in by_stage.get("run_opendss_powerflow", "")
          and "de211a53b575fb63" in by_stage.get("load_case", ""),
          by_stage.get("build_network_profile", "")[:60])

    # ⑤ 末尾把话头递给下一步：全角冒号或句号收尾
    bad_tail = [SAMPLE[i][0] for i, n in enumerate(notes)
                if not n.rstrip().endswith(("：", "。"))]
    check("⑤ 每条思考段都以「：」或「。」收尾", not bad_tail, str(bad_tail))

    # ⑥ 可复现：同样的输入必须产出同样的文案（本项目的验收红线）
    again = [N.narrate(st, et, status, det, msg) for st, et, status, det, msg in SAMPLE]
    check("⑥ 同样输入产出同样文案（可复现）", again == notes)

    # ⑦ 失败态优先：不允许把"没通过"说成"已完成"
    failnote = N.narrate("verify_result_integrity_and_power_balance", "verify", "failed",
                         {"passed": False}, "Verification.passed=False")
    check("⑦ 失败态文案明确说没通过，且不声称继续", "没通过" in failnote, failnote[:40])

    # ⑧ 未知阶段：兜底如实转述，**不编内容**
    unknown = N.narrate("some_new_stage", "tool_result", "success", {}, "some_new_stage ok: 1")
    check("⑧ 未知阶段走兜底且不抛异常", bool(unknown.strip()), unknown[:40])
    check("⑧b 兜底句带上了动作行（不是孤零零一行英文标识符）",
          unknown.startswith(N.action_for("some_new_stage")["verb"]), unknown[:40])

    # ⑨ detail 缺失（老任务的 trace 没有该字段）时仍有文案，不许抛
    legacy = [N.narrate(st, et, status, None, msg or "load_case ok: x")
              for st, et, status, _det, msg in SAMPLE]
    check("⑨ 老 trace（无 detail）也能出文案，且不抛异常",
          all(n.strip() for n in legacy), str([n for n in legacy if not n.strip()]))

    # ⑩ 版本戳：文案模板变了却不升版本，就等于"界面当时显示的那段话"与
    #    "代码现在会生成的那段话"悄悄分叉
    check("⑩ NARRATION_VERSION 非空且形如 x.y.z",
          bool(re.fullmatch(r"\d+\.\d+\.\d+", N.NARRATION_VERSION)),
          N.NARRATION_VERSION)

    # 11 界面对 **粗体 ** 是成对解析的，落单的星号会原样显示出来
    #     （编号用 ASCII：⑪ 及其后的带圈数字不在 GBK 里，单独跑会崩）
    odd = [SAMPLE[i][0] for i, n in enumerate(notes) if n.count("**") % 2]
    check("11 思考段里的 ** 标记成对（否则界面会露出星号）", not odd, str(odd))

    # 12 早期失败路径（上传/转换阶段被拒）也必须有思考段。
    #    实测缺陷：这条路径原先**一条 trace 事件都不产生** ⇒ 负样本算例
    #    （孤岛 / 重复母线 / 零阻抗）在界面上思考流全空，只剩一行错误 ——
    #    而这恰恰是最需要解释的场合。
    early = [
        ("ingest_upload", "user", "success",
         {"files": ["negative_duplicate_bus.csv"]}, ""),
        ("convert_case", "tool_call", "running", {}, "正在转换…"),
        ("convert_case", "tool_result", "failed",
         {"reason": "重复母线 ID：B3", "layer": "转换阶段",
          "suggestion": "修改母线 ID"}, "重复母线 ID：B3"),
        ("diagnose_case", "guard", "success",
         {"reason": "重复母线 ID：B3", "layer": "转换阶段",
          "suggestion": "修正后重新上传"}, "已定位到数据问题"),
    ]
    early_notes = [N.narrate(st, et, s, d, m) for st, et, s, d, m in early]
    check("12 早期失败路径每一步都有思考段",
          all(n.strip() for n in early_notes), str(early_notes))
    check("12b 早期失败路径给出文件名与失败原因",
          "negative_duplicate_bus.csv" in early_notes[0]
          and "重复母线 ID" in "".join(early_notes), early_notes[0][:50])
    check("12c 早期失败明确说「不带着未通过的结果继续」",
          "不会带着未通过的结果继续" in early_notes[2], early_notes[2][:50])
    check("12d 早期失败被定性为数据问题而非数值问题",
          "数据问题" in early_notes[3] and "数值算法" in early_notes[3],
          early_notes[3][:60])
    check("12e 早期失败文案同样可复现",
          [N.narrate(st, et, s, d, m) for st, et, s, d, m in early] == early_notes)

    print()
    print(f"=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
