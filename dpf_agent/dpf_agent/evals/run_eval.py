# -*- coding: utf-8 -*-
"""天枢一号端到端评估（eval）harness。

用途：把「内置算例 + 硬算例套件」的求解结果与**权威参考值**逐项比对，产出可放进
汇报/论文的对照表。它回答一个关键问题：**这个 Agent 算出来的数，对不对？**

两种模式：
    python -m dpf_agent.evals.run_eval --from-outputs
        # 扫描 outputs/<task_id>/ 里已有的产物的结果，与参考值比对（不重新计算，离线可跑）
    python -m dpf_agent.evals.run_eval --via-api http://127.0.0.1:8000
        # 需要应用已启动：逐个提交算例并等待完成，再比对（真正的端到端）

参考值来源（全部取自本项目之外，可公开追溯）：
    - 公开标准算例 `examples/cases/ieee_official/manifest.json`：元素计数取 OpenDSS
      原生编译结果；电压 / 网损取 OpenDSS 官方随包发布的期望结果文件
      （`*_VLN_Node.Txt` / `*_Losses.Txt` 等），来源与对照见该目录 README
    - 内置 ieee33：公开文献基准（最低电压 0.9131 pu @ bus18，网损约 202.7 kW）
    - 内置 case4 / case_tx：仅规模核对（无权威参考）
    - 开发期自建的难度算例套件（v1~vN）已于 2026-10-04 移出交付包，
      原因见根目录 README「验证状态」

容差口径（写进报告，避免"拿建模差异当错误"）：
    - 规模类（母线/支路/变压器数）：必须精确一致
    - 电压：|Δ| ≤ 0.010 pu 视为一致，≤ 0.020 pu 记为偏差可解释
    - 网损：相对偏差 ≤ 25% 视为一致（本 Agent 冻结调压 controlmode=off，
      与文献中带调压/不同负荷模型的算例天然存在系统差）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DPF_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if DPF_ROOT not in sys.path:          # 允许以脚本方式直接运行（`python dpf_agent/evals/run_eval.py`）
    sys.path.insert(0, DPF_ROOT)
from dpf_agent.paths import case_archive_root, doc_source_dir  # noqa: E402

# 旧版难度算例套件的位置（v1 比 v2+ 多包了一层 `powerflow_agent_hard_cases\`）。
# ⚠️ 该套件已于 2026-10-04 移出交付包（其数据与「预期结果」出自同一次生成，
#    不构成独立验证），因此在交付包内**必然不存在**，会被下面的存在性检查跳过。
#    保留它只是让仍用旧布局的环境（开发机）不必改代码；现行基准见
#    `examples/cases/ieee_official/`。
HARD_CASES_DIR = os.path.join(
    case_archive_root(), "powerflow_agent_hard_cases_v1",
    "powerflow_agent_hard_cases",
)

V_TOL_OK = 0.010
V_TOL_WARN = 0.020
LOSS_TOL_WARN = 0.25


@dataclass
class Reference:
    name: str
    source: str
    buses: int | None = None
    lines: int | None = None          # 纯线路数（manifest lines_expected）
    branch_total: int | None = None   # 含变压器支路的总支路数（manifest branches）
    transformers: int | None = None
    loads: int | None = None
    vmin_pu: float | None = None
    vmax_pu: float | None = None
    loss_kw: float | None = None
    #: 真实最低**相**电压（manifest `vmin_phase_pu`）。与 `vmin_pu` 口径不同：
    #: `vmin_pu` 是"每母线取三相最大值后取最小"，这个是"逐节点(相)的最小值"。
    #: manifest 自己写明了这个对应关系，因此两者必须分别比对、不可互比。
    vmin_phase_pu: float | None = None
    aliases: tuple[str, ...] = ()
    notes: str = ""


def _suite_manifest_paths() -> list[str]:
    """列出所有算例套件的 manifest：自造压力测试 v1~vN + 公开标准算例 ieee_official。

    扫描根 = `paths.case_archive_root()`。2026-09-24 之前这些套件散在桌面
    （`~/Desktop/powerflow_agent_hard_cases_v*`），后整合进 `agent\\算例归档\\`；
    本函数原先硬编码桌面，整合后不改就会**一个套件都找不到**（评测全绿但无数值校验）。

    真实缺陷（历史）：这里原先只读 v1 且只认 `main_cases` 数组，而 v2~v6 的 manifest 用
    `cases` **字典**。结果是 v2~v6 的算例在评测里全部"无匹配参考"——报告显示全绿，
    实际只验了收敛、**没有任何数值校验**。评测口径必须覆盖全部套件。
    """
    base = case_archive_root()
    found: list[str] = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        entries = []
    for entry in entries:
        # 自造压力测试套件（v1~vN）与**公开标准算例**（ieee_official）都要扫。
        # 2026-10-04 起交付包只保留公开标准算例（自造套件的数据与"预期值"出自同一次
        # 生成，不构成独立验证）；若这里只认 v1~vN，评测会静默失去全部数值校验对象。
        if not (entry.startswith("powerflow_agent_hard_cases_v")
                or entry == "ieee_official"):
            continue
        root = os.path.join(base, entry)
        for cand in (
            os.path.join(root, "manifest.json"),
            os.path.join(root, "powerflow_agent_hard_cases", "manifest.json"),
        ):
            if os.path.isfile(cand):
                found.append(cand)
                break
    legacy = os.path.join(HARD_CASES_DIR, "manifest.json")
    if os.path.isfile(legacy) and legacy not in found:
        found.append(legacy)
    return found


def _first_present(d: dict[str, Any], *keys: str) -> Any:
    """按顺序返回第一个**存在且非 None** 的字段值（允许 0 / 0.0 / "" falsey 值）。

    ⚠️ 这里不能用 `d.get(a) or d.get(b)`：`0` 是合法取值（例如一个**确实没有变压器的
    算例** `transformers: 0`），用 `or` 会把它吞成 `None`，于是一条"声明了 0"的参考值
    被读成"没有声明"，对应的比对**静默消失**。这与 `loss_kw` 漏加载是同族缺陷：
    参考值明明声明了，却没能进入比对。
    """
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _reference_from_case(
    case: dict[str, Any], case_id: str, suite: str, file_hint: str = ""
) -> Reference:
    """把一条 manifest 记录归一化为 Reference。

    字段名在 v1 与 v2+ 之间不一致（`buses_expected` vs `bus_count`、
    `nr_validation` vs `validation`、`transformer_branches` vs `transformer_like_branches`），
    两种都要认，否则会静默丢参考值。
    """
    nr = case.get("nr_validation") or case.get("validation") or {}
    if not isinstance(nr, dict):
        # 故障注入用例的 validation 常是说明性字符串（如 "should be rejected"），
        # 没有数值参考；不能假设它一定是 dict。
        nr = {}
    file_name = str(case.get("file") or file_hint or "")
    cid = str(case.get("id") or case_id)
    stem = os.path.splitext(os.path.basename(file_name))[0].lower()
    branches = _first_present(case, "branches", "branch_count")
    transformers = _first_present(
        case, "transformers", "transformer_branches", "transformer_like_branches"
    )
    return Reference(
        name=cid,
        source=f"{suite}/{file_name} (manifest)",
        buses=_first_present(case, "buses_expected", "buses", "bus_count"),
        lines=_first_present(case, "lines_expected"),
        branch_total=branches,
        transformers=transformers,
        loads=_first_present(case, "loads", "load_count"),
        vmin_pu=nr.get("vmin_pu"),
        vmax_pu=nr.get("vmax_pu"),
        # ⚠️ 下面两个字段原先**没有被读取**，而 `score_result` 却在用 `ref.loss_kw`
        #    做比对 —— 结果是 manifest 里声明的网损参考值（13Bus 112.4 kW、
        #    4Bus 567.6 kW）**从未参与过任何比对**，明细里显示"期望 None"，
        #    看起来像"官方没给参考值"，实际是加载端漏了。同族缺陷：
        #    参考值声明了却被静默丢弃，等于没有验证。
        loss_kw=_first_present(case, "loss_kw") or nr.get("loss_kw"),
        vmin_phase_pu=nr.get("vmin_phase_pu"),
        aliases=tuple(dict.fromkeys(a for a in (stem, cid.lower(), case_id.lower()) if a)),
        notes=str(case.get("format") or ""),
    )


def _iter_manifest_cases(m: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """兼容两种 manifest 结构：`main_cases`/`cases` 为数组或字典。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for key in ("main_cases", "cases"):
        block = m.get(key)
        if isinstance(block, dict):
            for cid, case in block.items():
                if isinstance(case, dict):
                    out.append((str(cid), case))
        elif isinstance(block, list):
            for idx, case in enumerate(block):
                if isinstance(case, dict):
                    out.append((str(case.get("id") or f"{key}{idx}"), case))
    # 故障注入用例（应与主用例同口径纳入参考，只是期望"被拦截"）
    for key in ("negative_tests", "negatives"):
        block = m.get(key)
        if isinstance(block, dict):
            for cid, case in block.items():
                if isinstance(case, dict):
                    out.append((str(cid), case))
    return out


def _prune_shared_aliases(refs: list[Reference]) -> None:
    """把**被多个参考共用**的别名一律丢弃 —— 共用别名不携带识别信息。

    这是实测出来的硬约束：manifest 里四个官方算例的 `file` 都是 `Master.dss`，
    于是除算例 id 外，它们的别名里都多了一个 `master`。而匹配是拿别名去撞
    case_ref 的，任何以 `Master.dss` 结尾的上传算例都会命中**第一个**带该别名的
    参考 —— 实测 **266 条**互不相干的产物（母线数 5 / 50 / 180 / 200…）被挂到了
    官方 13Bus（16 母线）上，报告里因此多出 266 条**根本不成立的"失败"**。

    与其硬编码一份"通用文件名清单"，不如用**冲突检测**：一个别名若同时属于 ≥2 个
    参考，它对"是哪一个"这个问题就是零信息，删掉。这样以后新增算例也不会再犯。
    """
    counts: dict[str, int] = {}
    for r in refs:
        for a in set(r.aliases):
            if a:
                counts[a] = counts.get(a, 0) + 1
    shared = {a for a, n in counts.items() if n >= 2}
    if not shared:
        return
    for r in refs:
        r.aliases = tuple(a for a in r.aliases if a not in shared)


def load_references() -> list[Reference]:
    refs: list[Reference] = [
        Reference(
            name="ieee33", source="公开文献基准（IEEE 33-bus）",
            vmin_pu=0.9131, loss_kw=202.7,
            aliases=("ieee33",),
            notes="最低电压位于 18 号母线；本 Agent 冻结调压，网损偏低属已知系统差",
        ),
        Reference(name="case4", source="内置算例（无权威参考）", aliases=("case4",)),
        Reference(name="case_tx", source="内置算例（变压器算例，无权威参考）", aliases=("case_tx",)),
    ]
    for manifest_path in _suite_manifest_paths():
        try:
            with open(manifest_path, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"⚠ 读取 manifest 失败（{manifest_path}）：{exc}")
            continue
        suite = str(m.get("suite") or os.path.basename(os.path.dirname(manifest_path)))
        for case_id, case in _iter_manifest_cases(m):
            refs.append(_reference_from_case(case, case_id, suite))
    _prune_shared_aliases(refs)
    return refs


#: 这些片段在算例路径里是**通用名**，不携带任何识别信息，不能用来匹配。
#: 主要就是官方算例的 `Master.dss`（四个算例全叫这个名）。
_GENERIC_SEGMENTS = {"master", "main", "circuit", "network"}


def _case_ref_candidates(case_ref: str) -> list[str]:
    """从 case_ref 提取可用于匹配的**路径片段**（各级目录名 + 文件名词干）。

    case_ref 实为 `.../uploads/<sess>/<算例名>/Master.dss`，直接取 basename 会得到
    "master" —— 于是大量已上传算例被判成"无匹配参考"，报告显示全绿却没有任何
    数值校验（实测 77 个任务因此漏检）。这里同时取父目录名与文件名。

    ⚠️ 返回的是**片段集合**，匹配时必须要求**片段与算例名精确相等**。
    早先用的是子串匹配（`alias in 整条路径`），后果是
    `'case4' in '.../case42_single_phase_transformer/Master.dss'` 为真 ——
    18 条 `case42_*` 的产物被判成对 `case4` 的比对。
    """
    low = (case_ref or "").lower().replace("\\", "/").strip()
    if not low:
        return []
    parts = [p for p in low.split("/") if p]
    cands: list[str] = []
    for i, p in enumerate(parts):
        seg = os.path.splitext(p)[0] if i == len(parts) - 1 else p
        seg = seg.strip()
        if seg and seg not in _GENERIC_SEGMENTS:
            cands.append(seg)
    return list(dict.fromkeys(cands))


def match_reference(case_ref: str, refs: list[Reference]) -> Reference | None:
    """按**算例名**匹配参考：路径片段必须与算例名 / 别名**精确相等**。

    两条硬约束，都是从实测反推出来的（各自造成过一批虚假结论）：

    1. **不许子串匹配**。`alias in 整条路径` 会让 `case4` 命中
       `case42_single_phase_transformer`（实测 18 条）。
    2. **不许按母线数推定**。母线数相同**不等于**是同一个算例：
       `case_ref` 为空的产物曾"按母线数"被挂到官方 4Bus 算例上（实测 8 条），
       于是报告里出现了一批**根本不成立的比对失败**。
       没有名称证据时，正确结论是**未评估**，不是"推定一致 / 推定失败"。
    """
    cands = set(_case_ref_candidates(case_ref))
    if not cands:
        return None
    for r in refs:
        if r.name.lower() in cands:
            return r
        if any(a and a in cands for a in r.aliases):
            return r
    return None


@dataclass
class Check:
    item: str
    actual: Any
    expected: Any
    verdict: str  # ok / warn / fail / na
    detail: str = ""
    #: 是否是**与参考值的比对**。与"自证项"（如「收敛」）区分开 ——
    #: 这个区分是 `overall()` 能不能判 ok 的前提，见下面的说明。
    is_reference: bool = False


@dataclass
class CaseReport:
    task_id: str
    case_ref: str
    reference: str = ""
    #: 参考是怎么匹配上的。目前只有 "name"（路径片段与算例名精确相等）一种，
    #: 显式写出来是为了**让"排除了哪些匹配方式"在报告里可见** ——
    #: 母线数推定、子串匹配都曾把不相干的产物挂到官方算例上并伪造出失败清单。
    matched_by: str = ""
    checks: list[Check] = field(default_factory=list)

    def compared(self) -> int:
        """真正与**参考值**比过、且有结论的条数（ok/warn/fail）。

        `na` 不计入：它既可能是"信息项"（如三相不平衡度），
        也可能是"缺值未比对"的占位 —— 两者都不构成一次有效比对。
        """
        return sum(1 for c in self.checks
                   if c.is_reference and c.verdict in ("ok", "warn", "fail"))

    def overall(self) -> str:
        """fail > warn > ok > na，**但没有任何参考比对时一律判 na，不许判 ok**。

        ⚠️ 修的是本项目反复出现的那一类**假绿**：原来的实现里，一个"无匹配参考"的
        算例仍会带上一条「收敛 ok」的自证项，于是 `overall()` 返回 ok，
        被顶部"通过率"算成**通过** —— 而实际上**一次参考比对都没发生**。
        这正是 2026-10-04 记录过的"零判据 ⇒ 直接跑必然全绿"。

        区分靠的是 `is_reference`：**自证项（收敛/验证）通过与"结果对不对"无关**，
        没有参考值时结论只能是"未评估"。
        """
        if any(c.verdict == "fail" for c in self.checks):
            return "fail"
        if any(c.verdict == "warn" for c in self.checks):
            return "warn"
        if self.compared() == 0:
            return "na"
        return "ok"


def score_result(
    task_id: str, result: dict[str, Any], case_ref: str, refs: list[Reference],
    profile: dict[str, Any] | None = None,
) -> CaseReport:
    """把一份求解结果与参考值逐项比对。

    `profile` 是同一任务的 `network_profile.json`（可选）。它提供**元件计数**
    （负荷数等）—— `powerflow_result.json` 里没有负荷计数，所以"负荷数"这项参考值
    必须靠它才能真正比上。取不到时记 `na` 占位，不静默跳过。
    """
    system = result.get("system_summary") or {}
    solver = result.get("solver_status") or {}
    buses = result.get("bus_results") or []
    branches = result.get("branch_results") or []
    rep = CaseReport(task_id=task_id, case_ref=case_ref)

    ref = match_reference(case_ref, refs)
    rep.reference = ref.name if ref else "（无匹配参考）"
    rep.matched_by = "name" if ref else ""

    # 收敛
    conv = solver.get("converged")
    rep.checks.append(
        Check("收敛", conv, True, "ok" if conv else "fail",
              f"迭代 {solver.get('iterations')} 次" if conv else "未收敛")
    )

    if ref:
        if ref.buses is not None:
            rep.checks.append(
                Check("母线数", len(buses), ref.buses,
                      "ok" if len(buses) == ref.buses else "fail", is_reference=True)
            )
        # 口径：manifest 中 case01/02 用 lines_expected（纯线路数），
        # case03~06 用 branches（线路 + 变压器支路总数）。两者语义不同，分别比对。
        n_trans = len(result.get("transformer_results") or [])
        if ref.lines is not None:
            rep.checks.append(
                Check("线路数", len(branches), ref.lines,
                      "ok" if len(branches) == ref.lines else "fail", is_reference=True)
            )
        if ref.branch_total is not None:
            total = len(branches) + n_trans
            rep.checks.append(
                Check("总支路数(线路+变压器)", total, ref.branch_total,
                      "ok" if total == ref.branch_total else "warn",
                      f"线路 {len(branches)} + 变压器 {n_trans}", is_reference=True)
            )
        # 判据用 `is not None` 而不是真值判断：`transformers: 0` 是**合法的声明**
        # （该算例确实没有变压器），用 `if ref.transformers:` 会把这条参考值静默跳过。
        if ref.transformers is not None:
            rep.checks.append(
                Check("变压器支路数", n_trans, ref.transformers,
                      "ok" if n_trans == ref.transformers else "warn", is_reference=True)
            )
        # 负荷元件数：参考值来自 manifest 的 `loads`，实测值来自网络画像
        # （`powerflow_result.json` 不含负荷计数）。参考值声明了就必须比到 ——
        # 取不到实测值记为 na 占位，而不是让这条参考值静默失效。
        if ref.loads is not None:
            n_load = (profile or {}).get("load_count")
            rep.checks.append(
                Check("负荷元件数", n_load, ref.loads,
                      "ok" if n_load == ref.loads else "fail" if n_load is not None else "na",
                      "" if n_load is not None else "网络画像缺失，本次未比对",
                      is_reference=True)
            )
        # ⚠️ 下面三处"缺值就跳过"的写法是**同一族缺陷**：值取不到时**一条判据都不产生**，
        #    于是"没比对"在报告里完全看不见，而整体结论仍可能显示 ok。
        #    现在改成：值缺失时**显式记一条 na 占位**，让缺口在报告里可见
        #    （`na` 不计入 `compared()`，因此也不会被当成"通过"）。
        vmin = system.get("min_voltage_pu")
        if ref.vmin_pu is not None and vmin is not None:
            d = abs(float(vmin) - float(ref.vmin_pu))
            rep.checks.append(
                Check("最低电压(pu)", round(float(vmin), 4), ref.vmin_pu,
                      "ok" if d <= V_TOL_OK else "warn" if d <= V_TOL_WARN else "fail",
                      f"Δ={d:.4f}，位置 {system.get('min_voltage_bus')}", is_reference=True)
            )
        else:
            rep.checks.append(
                Check("最低电压(pu)", vmin, ref.vmin_pu, "na",
                      "参考值或实测值缺失，本次未比对", is_reference=True)
            )
        vmax = system.get("max_voltage_pu")
        if ref.vmax_pu is not None and vmax is not None:
            d = abs(float(vmax) - float(ref.vmax_pu))
            rep.checks.append(
                Check("最高电压(pu)", round(float(vmax), 4), ref.vmax_pu,
                      "ok" if d <= V_TOL_OK else "warn" if d <= V_TOL_WARN else "fail",
                      f"Δ={d:.4f}，位置 {system.get('max_voltage_bus')}", is_reference=True)
            )
        else:
            rep.checks.append(
                Check("最高电压(pu)", vmax, ref.vmax_pu, "na",
                      "参考值或实测值缺失，本次未比对", is_reference=True)
            )
        loss = (result.get("loss_summary") or {}).get("total_loss_kw")
        if ref.loss_kw is not None and loss is not None:
            rel = abs(float(loss) - float(ref.loss_kw)) / float(ref.loss_kw)
            rep.checks.append(
                Check("总有功网损(kW)", round(float(loss), 2), ref.loss_kw,
                      "ok" if rel <= LOSS_TOL_WARN else "fail",
                      f"相对偏差 {rel*100:.1f}%", is_reference=True)
            )
        else:
            rep.checks.append(
                Check("总有功网损(kW)", loss, ref.loss_kw, "na",
                      "参考值或实测值缺失，本次未比对", is_reference=True)
            )
        # 真实最低**相**电压：与上面的 `最低电压(pu)` 是**两个口径**，manifest 自己
        # 写明了对应关系（`vmin_phase_note`）—— 前者是"每母线取三相最大值后取最小"，
        # 逐相最小值比它更低是正常的（4Bus：0.87577 vs 0.78218）。
        # 这一项此前从未被加载，等于官方给的"真实最低相电压"参考值一直没被用。
        if ref.vmin_phase_pu is not None:
            vph = system.get("min_phase_voltage_pu")
            if vph is not None:
                d = abs(float(vph) - float(ref.vmin_phase_pu))
                rep.checks.append(
                    Check("真实最低相电压(pu)", round(float(vph), 4), ref.vmin_phase_pu,
                          "ok" if d <= V_TOL_OK else "warn" if d <= V_TOL_WARN else "fail",
                          f"Δ={d:.4f}，位置 {system.get('min_phase_voltage_bus')} 的 "
                          f"{system.get('min_phase_label')} 相", is_reference=True)
                )
            else:
                rep.checks.append(
                    Check("真实最低相电压(pu)", vph, ref.vmin_phase_pu, "na",
                          "参考值或实测值缺失，本次未比对", is_reference=True)
                )

    # 三相不平衡度：作为**信息项**（verdict=na，不影响通过判定），
    # 因为参考值目前没有 ε 基准。它的价值是让评测报告也能看到分相事实，
    # 而不是只看到一个被压平的平衡等值。
    ub = result.get("unbalance_summary") or {}
    sysd = result.get("system_summary") or {}
    if ub.get("buses_assessed"):
        eps = ub.get("worst_unbalance_pct")
        over = ub.get("buses_over_limit_count") or 0
        rep.checks.append(
            Check(
                "三相不平衡度 ε(%)",
                round(float(eps), 3) if eps is not None else None,
                f"参考限值 {ub.get('limit_pct')}",
                "na",
                f"最严重母线 {ub.get('worst_bus')}；超限母线 {over} 个；"
                f"真实最低相电压 {sysd.get('min_phase_voltage_pu'):.4f} pu"
                f"（{sysd.get('min_phase_voltage_bus')} 的 {sysd.get('min_phase_label')} 相）"
                if sysd.get("min_phase_voltage_pu") is not None
                else f"最严重母线 {ub.get('worst_bus')}；超限母线 {over} 个",
            )
        )
    return rep


def _load_profile(task_dir: str) -> dict[str, Any] | None:
    """读同一任务的 `network_profile.json`（元计数的来源）。取不到返回 None。"""
    p = os.path.join(task_dir, "network_profile.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return None


def scan_outputs(outputs_root: str, refs: list[Reference]) -> list[CaseReport]:
    reports: list[CaseReport] = []
    for path in sorted(glob.glob(os.path.join(outputs_root, "pf-*", "powerflow_result.json"))):
        task_dir = os.path.dirname(path)
        task_id = os.path.basename(task_dir)
        try:
            with open(path, encoding="utf-8") as f:
                result = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        case_ref = ""
        spec_path = os.path.join(task_dir, "task_spec.json")
        if os.path.isfile(spec_path):
            try:
                with open(spec_path, encoding="utf-8") as f:
                    case_ref = str((json.load(f) or {}).get("case_ref") or "")
            except (OSError, json.JSONDecodeError):
                pass
        reports.append(score_result(task_id, result, case_ref, refs, _load_profile(task_dir)))
    return reports


def run_via_api(base_url: str, refs: list[Reference], case_names: list[str]) -> list[CaseReport]:
    """通过后端接口逐个提交算例并等待完成（应用需已启动）。"""
    base = base_url.rstrip("/")

    def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    reports: list[CaseReport] = []
    for name in case_names:
        sess = _post_json(f"{base}/api/chat/sessions", {"title": f"eval-{name}"})
        sid = sess.get("id")
        if not sid:
            print(f"✗ 创建会话失败：{name}")
            continue
        # multipart 表单提交（接口要求 Form 字段）
        boundary = "----tsEval"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"\r\n\r\n"
            f"计算{name}\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/chat/sessions/{sid}/messages", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        task_id = (payload.get("assistant_message") or {}).get("task_id") or ""
        print(f"· {name}: task={task_id} 用时 {time.time()-t0:.1f}s")
        if not task_id:
            continue
        path = os.path.join(DPF_ROOT, "outputs", task_id, "powerflow_result.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                reports.append(score_result(
                    task_id, json.load(f), name, refs,
                    _load_profile(os.path.join(DPF_ROOT, "outputs", task_id)),
                ))
    return reports


def official_case_files() -> list[tuple[str, str]]:
    """官方标准算例清单：`[(算例名, Master.dss 绝对路径)]`，保持 manifest 顺序。

    用途是驱动**离线评测**（见 `run_official_offline`）：参考值再权威，若从不把算例
    跑一遍，评测就等于没有对象。历史产物里只有内置 `ieee33` 有运行记录，四个官方
    算例的参考值一直没有被检验过。
    """
    out: list[tuple[str, str]] = []
    for manifest_path in _suite_manifest_paths():
        try:
            with open(manifest_path, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if str(m.get("suite_name") or "") != "ieee_official" and \
                os.path.basename(os.path.dirname(manifest_path)) != "ieee_official":
            continue
        base = os.path.dirname(manifest_path)
        for case_id, case in _iter_manifest_cases(m):
            rel = str(case.get("file") or "")
            if not rel:
                continue
            out.append((case_id, os.path.join(base, *rel.split("/"))))
    return out


def run_official_offline(
    refs: list[Reference], names: list[str] | None = None, work_root: str | None = None
) -> list[CaseReport]:
    """**离线**跑官方标准算例并与 manifest 参考比对（不需要应用启动）。

    为什么需要它：`--from-outputs` 只能扫历史产物，而历史产物里几乎没有官方算例的
    运行记录 —— 参考错配修掉之后，444 条里只有 69 条（内置 ieee33）真的比过，
    四个官方算例的参考值**从未被检验**。这个模式把"有参考值"变成"有比对发生"。

    走的是与产品同一条求解链路（`ApplicationService` + CLI 通道），产物落在临时
    工作区，不污染历史 `outputs/`；比对复用 `score_result`，不另写一套判据。
    """
    from dpf_agent.application.service import ApplicationService
    from dpf_agent.config import AgentConfig
    from dpf_agent.schemas.enums import OutputFormat, ResultScope
    from dpf_agent.schemas.task_spec import UserRequest

    if work_root is None:
        import tempfile

        work_root = tempfile.mkdtemp(prefix="ts_eval_official_")

    cases = official_case_files()
    if names:
        wanted = {n.lower() for n in names}
        cases = [(n, p) for n, p in cases if n.lower() in wanted]
    if not cases:
        print("⚠ 未找到官方算例（检查 examples/cases/ieee_official/manifest.json）")
        return []

    cfg = AgentConfig()
    cfg.engine_backend = "cli"      # COM 只能在 Electron 进程内用（loader lock）
    cfg.llm_enabled = False         # 数值对照不让 LLM 参与
    cfg.rag_enabled = False
    cfg.workspace_root = os.path.join(work_root, "tasks")
    cfg.outputs_root = os.path.join(work_root, "outputs")
    svc = ApplicationService(cfg)

    reports: list[CaseReport] = []
    for name, master in cases:
        if not os.path.isfile(master):
            print(f"✗ {name}: 算例文件不存在 {master}")
            continue
        t0 = time.time()
        resp = svc.run(
            UserRequest(
                message=f"eval {name}",
                case_ref=master,          # 传绝对路径：路径片段含算例名，按名匹配参考
                result_scopes=list(ResultScope),
                output_formats=[OutputFormat.JSON, OutputFormat.MD],
            ),
            cli_callback=None,
        )
        task_id = str(resp.get("task_id") or "")
        pf = (resp.get("artifacts") or {}).get("powerflow_result.json")
        print(f"· {name}: task={task_id} status={resp.get('status')} 用时 {time.time()-t0:.1f}s")
        if not pf or not os.path.isfile(pf):
            print(f"  ✗ 无结果产物，跳过（error={str(resp.get('error'))[:120]}）")
            continue
        with open(pf, encoding="utf-8") as f:
            prof = None
            ppath = os.path.join(os.path.dirname(pf), "network_profile.json")
            if os.path.isfile(ppath):
                try:
                    with open(ppath, encoding="utf-8") as gp:
                        prof = json.load(gp)
                except (OSError, json.JSONDecodeError):
                    prof = None
            reports.append(score_result(task_id, json.load(f), master, refs, prof))
    print(f"（离线工作区：{work_root}）")
    return reports


def render_markdown(reports: list[CaseReport], title: str = "端到端评估报告",
                    scope: str = "") -> str:
    out: list[str] = []
    out.append(f"# 天枢一号 · {title}\n")
    out.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    if scope:
        out.append(scope + "\n")
    out.append(
        "口径说明：规模类必须精确一致；电压 |Δ|≤0.010 pu 记一致、≤0.020 pu 记可解释偏差；"
        "网损相对偏差 ≤25% 记一致（本 Agent 冻结调压 controlmode=off，与带调压的文献算例存在系统差）。\n"
    )
    out.append(
        "参考匹配口径：**仅当算例路径片段与算例名精确相等时才匹配**；不做子串匹配、"
        "不按母线数推定（母线数相同不等于同一算例）。未匹配到参考的任务一律记为「未评估」，"
        "不参与通过率 —— 宁可不评，也不把不相干的产物挂到某个官方算例上。\n"
    )
    ok = sum(1 for r in reports if r.overall() == "ok")
    warn = sum(1 for r in reports if r.overall() == "warn")
    fail = sum(1 for r in reports if r.overall() == "fail")
    na = sum(1 for r in reports if r.overall() == "na")
    out.append(
        f"**任务总数 {len(reports)} — 通过 {ok} / 偏差 {warn} / 失败 {fail} / 未评估 {na}**\n"
    )
    if na:
        # 「未评估」必须单独出现在头部 —— 它以前是**默认为零**的：没有参考值的算例
        # 靠一条「收敛 ok」被算成"通过"，于是"通过率"里混着没比过的东西。
        out.append(
            f"> ⚠️ **未评估 {na} 条**：这些任务没有匹配到参考值（或参考值缺失），"
            "**一次参考比对都没有发生**，因此不计入通过率。"
            "要让它们可评估，请补 `examples/cases/ieee_official/manifest.json` 之类的参考值。\n"
        )

    # ---- 按算例汇总（同一算例的多轮任务合并统计，便于看系统性差异）----
    grouped: dict[str, dict[str, int]] = {}
    for r in reports:
        key = r.reference if r.reference and not r.reference.startswith("（") else "未匹配参考"
        if key == "未匹配参考":
            low = (r.case_ref or "").lower()
            for token in ("case0", "ieee33", "case4", "case_tx", "case1"):
                idx = low.find(token)
                if idx >= 0:
                    # 前缀「未匹配·」是必须的：不加时这一组的名字（如 case01_opendss）
                    # 看起来像一个**参考名**，会被读成"对 case01_opendss 做了比对且未评估"。
                    # 实际是"没匹配到任何参考，只是按名字聚个类"。
                    key = "未匹配·" + low[idx:idx + 14].split("\\")[0].split("/")[0]
                    break
        g = grouped.setdefault(key, {"ok": 0, "warn": 0, "fail": 0, "na": 0})
        g[r.overall()] = g.get(r.overall(), 0) + 1
    out.append("## 按算例汇总\n")
    out.append("| 算例 | 通过 | 偏差 | 失败 | 未评估 | 结论 |")
    out.append("|---|---|---|---|---|---|")
    for key in sorted(grouped):
        g = grouped[key]
        # ⚠️ 判定必须把"未评估"算进去：一个**只有 na** 的组曾被打成「一致」——
        #    零证据被读成"通过"，是同族假绿里最刺眼的一种。
        if g.get("na", 0) and not (g.get("ok", 0) or g.get("warn", 0) or g.get("fail", 0)):
            verdict = "未评估"
        elif g.get("fail", 0) == 0 and g.get("warn", 0) == 0 and g.get("ok", 0) == 0:
            verdict = "未评估"
        elif g.get("fail", 0) == 0 and g.get("warn", 0) == 0:
            verdict = "一致" + ("（部分未评估）" if g.get("na", 0) else "")
        elif g.get("fail", 0):
            verdict = "存在系统性偏差"
        else:
            verdict = "个别偏差"
        out.append(f"| {key} | {g.get('ok',0)} | {g.get('warn',0)} | {g.get('fail',0)} "
                   f"| {g.get('na',0)} | {verdict} |")
    out.append("")

    # ---- 失败特征分布：把"哪些指标对不上"聚合成可行动的问题清单 ----
    # 只统计 warn/fail —— 「未评估」不是失败，混进来会把问题清单稀释成噪声。
    signatures: dict[str, int] = {}
    for r in reports:
        if r.overall() in ("ok", "na"):
            continue
        sig = "；".join(c.item for c in r.checks
                        if c.verdict != "ok" and c.verdict != "na")
        signatures[sig] = signatures.get(sig, 0) + 1
    if signatures:
        out.append("## 失败特征分布（按出现次数）\n")
        out.append("| 对不上的指标组合 | 出现次数 |")
        out.append("|---|---|")
        for sig, n in sorted(signatures.items(), key=lambda kv: -kv[1]):
            out.append(f"| {sig} | {n} |")
        out.append("")

    out.append("## 明细\n")
    out.append("| 任务 | 算例 | 参考 | 结论 | 明细 |")
    out.append("|---|---|---|---|---|")
    for r in reports:
        detail = "；".join(
            f"{c.item}: {c.actual}（期望 {c.expected}）{('，' + c.detail) if c.detail else ''}"
            for c in r.checks if c.verdict != "ok"
        ) or ("**无参考比对，未评估**" if r.overall() == "na" else "全部一致")
        # 把"怎么匹配上的"写进明细：匹配依据本身就是结论的一部分，
        # 早先错配的 266 条正是因为依据不可见才一直没人发现。
        ref_cell = f"{r.reference}（按算例名）" if r.matched_by else r.reference
        out.append(f"| {r.task_id} | {os.path.basename(str(r.case_ref))} | {ref_cell} | {r.overall()} | {detail} |")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-outputs", action="store_true", help="离线扫描已有产物")
    ap.add_argument("--official", action="store_true",
                    help="离线跑官方标准算例并与 manifest 参考比对（不需应用启动）")
    ap.add_argument("--via-api", metavar="BASE_URL", help="通过后端接口端到端跑")
    ap.add_argument("--cases", nargs="*", default=None,
                    help="限定算例名列表（--official / --via-api）")
    # 默认输出到「天枢一号_文档\源文档\」，与归档的报告同一份 —— 避免重跑又在桌面
    # 生成一份副本，导致归档件变旧、设计文档全集里的数字与实际不符。
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    refs = load_references()
    outputs_root = os.path.join(DPF_ROOT, "outputs")
    src = doc_source_dir()

    if args.official:
        # 报告分开写：这份回答"官方算例跑出来对不对"，历史产物扫描回答"攒下来的
        # 产物说明什么"，两个问题混在一张表里会让人分不清哪些是刚跑出来的。
        out_path = args.out or os.path.join(src, "天枢一号_官方算例评测报告.md")
        reports = run_official_offline(refs, args.cases)
        md = render_markdown(
            reports, title="官方标准算例评测报告",
            scope=("范围：**本次离线跑出来的官方标准算例**（走产品同一条求解链路，"
                   "CLI 通道 + ApplicationService）。每条都按算例名匹配到 manifest 中的"
                   "官方参考值 —— 参考值取自 OpenDSS 官方随包发布的期望结果文件，"
                   "原始算例文件未做修改。"),
        )
    elif args.via_api:
        out_path = args.out or os.path.join(src, "天枢一号_eval报告.md")
        reports = run_via_api(args.via_api, refs, args.cases or ["case4"])
        md = render_markdown(reports, title="端到端评估报告（经后端接口）")
    else:
        out_path = args.out or os.path.join(src, "天枢一号_eval报告.md")
        reports = scan_outputs(outputs_root, refs)
        md = render_markdown(
            reports, title="端到端评估报告（历史产物扫描）",
            scope=("范围：**扫描 `outputs/` 下已积累的历史产物**，不重新计算。"
                   "它们大多是 2026-08~10 开发期留下的运行记录，其中不少算例"
                   "（自建难度套件等）已随交付包移出，**没有参考值可比** —— "
                   "修掉参考错配后，这类任务一律记「未评估」而不是硬挂到某个官方算例上。"
                   "要看官方算例的真实结果，请用 `--official`。"),
        )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    print(f"\n报告已写入：{out_path}")
    # Historical scans are report viewing, not a claim that an acceptance run
    # succeeded. Execution modes must have actual assessed objects and propagate
    # failures rather than returning success for an empty/missing case set.
    if args.official or args.via_api:
        if not reports:
            print("评测未完成：没有生成任何任务评测记录。", file=sys.stderr)
            return 2
        if any(report.overall() == "fail" for report in reports):
            print("评测失败：存在失败任务或参考比对失败。", file=sys.stderr)
            return 1
        if all(report.overall() == "na" for report in reports):
            print("评测未完成：所有任务均无有效参考比对，不能判为验收通过。", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
