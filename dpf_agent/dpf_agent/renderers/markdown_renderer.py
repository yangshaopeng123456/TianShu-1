"""Markdown / HTML / CSV 报告渲染（文档 16）。

报告生成规则：
- 数值结果必须来自 OpenDSS 或工具返回值，不允许大模型编造。
- 报告层不重算潮流、不重新聚合覆盖权威值。
- unavailable / not_assessed 使用状态标签，不用 0 或空值替代。
- 未经专业审批的限值必须记录判据来源（"按当前仿真判据"）。
"""
from __future__ import annotations

import csv
import html
import os
from typing import Any

from ..config import SimDefaults
from ..domain import diagnosis as _diag
from ..schemas.case import CanonicalDistributionCase
from ..schemas.enums import TaskStatus
from ..schemas.evidence import EvidenceItem
from ..schemas.result import CanonicalDistributionPowerFlowResult
from ..schemas.solver import OpenDSSSolverProfile
from ..schemas.task_spec import PowerFlowTaskSpec
from ..schemas.verification import VerificationResult

_NA = "unavailable"
_NAS = "not_assessed"


def _fmt(v: float | None, unit: str = "", nd: int = 4) -> str:
    if v is None:
        return _NA
    return f"{v:.{nd}f} {unit}".strip()


def _fmt_phase_list(values: list[float], nd: int = 5) -> str:
    """把逐相数值格式化成 A: x / B: y / C: z。"""
    if not values:
        return "—"
    labels = ("A", "B", "C", "D")
    return " / ".join(
        f"{labels[i] if i < len(labels) else i}: {v:.{nd}f}" for i, v in enumerate(values[:4])
    )


def _status_tag(status: str) -> str:
    return {"ok": "ok", "unavailable": _NA, "not_assessed": _NAS}.get(status, status)


def render_markdown(
    task_spec: PowerFlowTaskSpec,
    case: CanonicalDistributionCase,
    solver_profile: OpenDSSSolverProfile,
    result: CanonicalDistributionPowerFlowResult | None,
    verification: VerificationResult | None,
    evidence: list[EvidenceItem],
    status: TaskStatus,
    defaults: SimDefaults,
    explanation: str = "",
    error: dict[str, Any] | None = None,
) -> str:
    L: list[str] = []
    L.append(f"# 配电网潮流计算报告\n")
    L.append(f"- 任务 ID：`{task_spec.task_id}`")
    L.append(f"- 任务状态：**{status.value}**")
    L.append(f"- 用户目标：{task_spec.user_goal or '执行配电网稳态潮流计算'}\n")

    # 1. 任务摘要
    L.append("## 1. 任务摘要\n")
    L.append(f"- 结果范围：{', '.join(s.value for s in task_spec.result_scopes)}")
    L.append(f"- 输出格式：{', '.join(f.value for f in task_spec.output_formats)}")
    if task_spec.focus_objects:
        L.append(f"- 解释重点（软约束）：{', '.join(task_spec.focus_objects)}")
    if error:
        L.append(f"- 失败信息：`{error.get('code')}` — {error.get('message')}")
    # 失败诊断（P2-b）：说清"往哪个方向查"，且**只给方向、不给操作指令**。
    # 放在第 1 节内而不是新开一节，是为了不打乱既有节号（其它地方按节号引用）。
    # 判据：有 error 用它；error 为空但状态本身是失败态时，从状态兜底取码。
    _diag_code = (error or {}).get("code")
    if not _diag_code and _diag.is_failure_status(status.value):
        _diag_code = _diag.code_for_status(status.value)
    if _diag_code:
        _hint = _diag.suggest(str(_diag_code), (error or {}).get("message", ""))
        if _hint:
            L.append("")
            L.extend(_hint.splitlines())
    L.append("")

    # 2. 算例来源与数据校验
    L.append("## 2. 算例来源与依赖\n")
    L.append(f"- 算例：`{case.master_dss_ref}`（来源类型：{case.source_type}）")
    L.append(f"- 算例指纹：`{case.case_fingerprint}`")
    L.append(f"- 输入格式：`{case.source_format or 'dss'}`；保真度状态：**{case.fidelity_status or 'not_assessed'}**")
    if case.fidelity_warnings:
        L.append("- 保真度警告：")
        for warning in case.fidelity_warnings:
            L.append(f"  - {warning}")
    L.append(f"- 依赖文件：{case.dependency_manifest.file_count} 个（任务副本只读执行，未覆盖原文件）")
    L.append("")
    L.append("| 依赖文件 | SHA-256（前 12 位） | 大小 (B) |")
    L.append("|---|---|---|")
    for e in case.dependency_manifest.entries:
        L.append(f"| {e.path} | {e.sha256[:12]} | {e.size_bytes} |")
    L.append("")

    # 3. 引擎与版本
    L.append("## 3. 求解配置与版本\n")
    L.append(f"- 引擎：OpenDSS {solver_profile.version_set.get('engine_version', '')}")
    L.append(f"- Adapter 版本：{solver_profile.version_set.get('adapter_version', '')}")
    L.append(f"- SolverProfile：`{solver_profile.profile_id}`（已冻结）")
    L.append(f"- 求解模式：{solver_profile.solve_mode}（稳态确定性单场景）")
    L.append(f"- 最大迭代：{solver_profile.iteration_config.max_iterations}；"
             f"收敛容差：{solver_profile.tolerance_config.tolerance}")
    L.append(f"- 控制行为：{solver_profile.control_behavior}（V1.3 不执行任何自动控制动作）")
    L.append("")
    L.append("参数来源（不得只记录零散参数）：")
    for ev in solver_profile.selection_evidence:
        L.append(f"- [{ev.source}] {ev.detail}")
    L.append("")

    # 3b. 求解口径声明：回答「这个数是在什么口径下算出来的」。
    # 口径不同的数值不可直接比较，因此这一节的地位与结果本身相同。
    L.append("### 本次求解口径声明\n")
    _native = (case.fidelity_status or "native") != "warning"
    _meta = case.source_metadata or {}
    _q = str(_meta.get("enforce_q_limits", ""))
    _freq = _meta.get("frequency_hz")
    # 三相建模三态（顺序不可颠倒）：原生分相 → 按输入相别建模 → 平衡三相等值。
    # 只有"输入真的带相别信息"时才敢说按相别建模；否则必须承认是平衡等值。
    _phase_modeled = str(_meta.get("has_phase_columns", "0")) == "1"
    L.append("| 项 | 本次口径 |")
    L.append("|---|---|")
    L.append(f"| 求解模式 | {solver_profile.solve_mode}（稳态确定性单场景，非时序） |")
    if _native:
        _phase_text = "原生分相模型（母线与支路按相建模，分相结果有效）"
    elif _phase_modeled:
        _unbalanced = str(_meta.get("unbalanced_input", "0")) == "1"
        _phase_text = (
            "**按输入相别建模**：显式声明相别 "
            f"{_meta.get('elements_with_phase', '?')} 个元件、默认三相 "
            f"{_meta.get('assumed_three_phase', '?')} 个"
            + (
                f"；其中单相/两相元件 {_meta.get('single_or_partial_elements', '?')} 个"
                "已按相号节点接入，分相结果有效"
                if _unbalanced
                else "；未发现单相/两相元件，等效为三相"
            )
        )
    else:
        _phase_text = (
            f"**平衡三相等值模型**（由 `{case.source_format}` 转换而来，"
            "输入未提供相别信息，故原始相别不平衡无法表达）"
        )
    L.append(f"| 三相建模 | {_phase_text} |")
    L.append(f"| 控制行为 | {solver_profile.control_behavior}（调压器/电容器等只识别、不动作） |")
    L.append(
        "| 负荷模型 | 名义恒功率（P/Q 恒定）；OpenDSS 默认 `Vminpu=0.95`，"
        "母线电压低于 0.95 pu 时负荷自动转为恒阻抗，故**实际供出负荷可能低于名义值** |"
    )
    # 负荷口径必须声明：读者要能判断"总负荷"这个数到底代表什么。
    # 「负荷元件求和」与「源侧供电−网损（净负荷）」在含发电时完全不同。
    L.append(
        "| 总负荷口径 | 各负荷元件端子1有功之和（反映负荷自身消耗）；"
        "**不等于**源侧供电。含 PV/发电时另列「净负荷 = 源侧供电 − 网损」 |"
    )
    # 发电口径同样必须声明：读者要能判断"发电注入"这个数含哪些元件类型。
    L.append(
        "| 发电注入口径 | 各发电元件端子1有功之和（含 Generator 与 PVSystem）；"
        "**储能 Storage 不计入**（双向元件，见下一行） |"
    )
    # 储能必须**单独一行**说明。只写"不计入"会让人以为它被完全忽略 ——
    # 实际上它不进负荷/发电，却**进功率平衡式**（不进的话残差恰好等于储能功率，
    # 含储能充放电的算例会直接判"不平衡"，实测 residual 可达 0.5 pu）。
    L.append(
        "| 储能口径 | 各储能元件端子1有功之和，保留符号（**正=充电**，等价负荷；"
        "**负=放电**，等价发电）；**不计入**总负荷与发电注入，但**计入功率平衡校验** |"
    )
    if _q == "1":
        L.append("| 发电机无功 | 已施加文件给定的 Qmin/Qmax（PV 机可能因无功触限退化为 PQ） |")
    elif _q == "0":
        L.append("| 发电机无功 | 默认**不施加** Q 限值（与参考解 unconstrained PV-Q 口径一致） |")
    else:
        L.append("| 发电机无功 | 未经过格式转换；是否含 Q 限值取决于算例本身 |")
    L.append(f"| 系统频率 | {f'{_freq:g}' if isinstance(_freq, (int, float)) else '50'} Hz |")
    # 注意：表格单元格内不能出现半角竖线，故 ε 公式用 ÷ 表达而非 |…|。
    L.append("| 不平衡度定义 | ε = 幅值(U₂) ÷ 幅值(U₁) × 100%（对称分量法，与相角参考系无关）；相间偏差仅为辅助指标 |")
    L.append(
        "| 不平衡限值 | 2% 为**参考值，待与 GB/T 15543 原文核对**；只作标注，"
        "**不构成「合格 / 不合格」判定** |"
    )
    L.append("| 未包含 | N-1 场景生成与对比、24 h 时序逐时扫描、保护/自动装置动作 |")
    L.append("")
    if not _native:
        if _phase_modeled:
            L.append(
                f"> ⚠️ 本算例由 `{case.source_format}` 转换为 OpenDSS 模型："
                "**相别信息已按输入建模**（单相/两相元件接入相号节点），"
                "但 **PV/PQ 无功限值、控制器与部分基准语义可能未完全保留**，"
                "仍建议与原求解器交叉核验。\n"
            )
        else:
            L.append(
                f"> ⚠️ 本算例由 `{case.source_format}` 转换为 OpenDSS 模型："
                "**输入未提供相别信息，按平衡三相等值建模**，"
                "原始算例的相别不平衡无法表达；"
                "结果不应直接视为原始求解器的等价复现，需交叉核验。\n"
            )
    L.append(
        "> 「最低电压」在报告中给出两个口径：**平衡等值最低电压**（取每条母线三相最大值，"
        "用于与既有口径衔接）与**真实最低相电压**（分相口径，才是越限判断依据）；"
        "含不平衡算例上后者更低。\n"
    )

    # 4. 潮流求解状态
    L.append("## 4. 潮流求解状态\n")
    if result is not None:
        st = result.solver_status
        L.append(f"- converged：**{st.converged}**；迭代次数：{st.iterations}；"
                 f"引擎错误标志：{st.error_flag}")
        if st.total_iterations is not None:
            L.append(f"- 求解与独立复算（含此前失败的注册工具求解）：共 {st.raw_solution_count} 次 Solve，累计 "
                     f"{st.total_iterations} 次迭代；上面的迭代次数属于权威候选的单次求解。"
                     "累计不包含编译脚本内的隐式 Solve。稳定化阶段保留当前模型和冻结容差，"
                     "单次迭代上限不提高，可按剩余预算临时降低；此前恢复见求解策略和重规划记录。")
        if not st.converged:
            L.append("- ⚠️ 未收敛仅表示数值求解状态，不得解释为“电网不安全”；本报告为受限诊断报告。")
    else:
        L.append("- 未执行求解（前置阶段失败）。")
    L.append("")

    if result is not None and result.solver_status.converged:
        # 5. 母线结果摘要
        L.append("## 5. 母线结果摘要\n")
        ok_buses = [b for b in result.bus_results if b.status == "ok"]
        L.append(f"- 母线总数：{len(result.bus_results)}；有效结果：{len(ok_buses)}")
        L.append("")
        L.append("| 母线 | 基准电压 (kV) | 电压 (pu, 平衡等值) | 电压 (kV) | 相角 (°) | 不平衡度 ε (%) | 电压判据* |")
        L.append("|---|---|---|---|---|---|---|")
        for b in ok_buses:
            judge = ""
            if b.voltage_pu is not None:
                if b.voltage_pu < defaults.voltage_min_pu:
                    judge = "低于下限"
                elif b.voltage_pu > defaults.voltage_max_pu:
                    judge = "高于上限"
                else:
                    judge = "区间内"
            L.append(
                f"| {b.name} | {_fmt(b.base_kv, nd=3)} | {_fmt(b.voltage_pu)} | "
                f"{_fmt(b.voltage_kv, nd=3)} | {_fmt(b.angle_deg, nd=2)} | "
                f"{_fmt(b.unbalance_pct, nd=3) if b.unbalance_pct is not None else '—'} | {judge} |"
            )
        bad = [b for b in result.bus_results if b.status != "ok"]
        for b in bad:
            L.append(f"| {b.name} | — | {_status_tag(b.status)} | — | — | — | — |")
        L.append("")
        L.append(
            "\\* 电压判据按当前仿真判据（仿真默认阈值 "
            f"{defaults.version}：{defaults.voltage_min_pu}~{defaults.voltage_max_pu} pu），"
            "非经专业审批的工程限值。\n"
        )
        L.append(
            "> 口径：「电压（平衡等值）」取该母线**三相电压的最大值**，仅用于与平衡模型参考值对比；\n"
            "> **分相电压与不平衡度见第 8 节**。不平衡度 ε = |U₂|/|U₁|×100%（对称分量法）。\n"
        )

        # 6. 线路 / 变压器结果摘要
        if result.branch_results:
            L.append("## 6. 线路 / 支路结果摘要\n")
            L.append("| 支路 | 起点 | 终点 | P (kW) | Q (kvar) | 电流 (A) | 额定 (A) | 负载率 |")
            L.append("|---|---|---|---|---|---|---|---|")
            for br in result.branch_results:
                loading = (
                    f"{br.loading_pct:.1f}%" if br.loading_pct is not None else _NA
                )
                L.append(
                    f"| {br.name} | {br.from_bus} | {br.to_bus} | {_fmt(br.p_kw, nd=2)} | "
                    f"{_fmt(br.q_kvar, nd=2)} | {_fmt(br.current_a, nd=2)} | "
                    f"{_fmt(br.rating_a, nd=1)} | {loading} |"
                )
            L.append("")
        if result.transformer_results:
            L.append("### 变压器结果\n")
            L.append("| 变压器 | P (kW) | Q (kvar) | 负载率 |")
            L.append("|---|---|---|---|")
            for tr in result.transformer_results:
                loading = (
                    f"{tr.loading_pct:.1f}%" if tr.loading_pct is not None else _NA
                )
                L.append(
                    f"| {tr.name} | {_fmt(tr.p_kw, nd=2)} | {_fmt(tr.q_kvar, nd=2)} | {loading} |"
                )
            L.append("")

        # 7. 系统汇总与功率平衡
        L.append("## 7. 系统负荷、供电、网损与功率平衡\n")
        ss = result.system_summary
        ls = result.loss_summary
        L.append(f"- 总负荷：{_fmt(ss.total_load_kw, 'kW', 2)}"
                 + ("（各负荷元件端子1有功之和）" if ss.total_load_kw is not None else ""))
        L.append(f"- 源侧供电：{_fmt(ss.total_generation_kw, 'kW', 2)} / "
                 f"{_fmt(ss.total_generation_kvar, 'kvar', 2)}")
        if ss.shunt_elements:
            L.append(f"- 固定并联导纳净消耗：{_fmt(ss.total_shunt_kw, 'kW', 2)} / "
                     f"{_fmt(ss.total_shunt_kvar, 'kvar', 2)}（Gs/Bs 随电压平方变化；正为消耗，负为注入；单独参与功率平衡）")
        if ss.branch_shunt_elements:
            L.append(f"- 支路 π 充电净消耗：{_fmt(ss.total_branch_shunt_kw, 'kW', 2)} / "
                     f"{_fmt(ss.total_branch_shunt_kvar, 'kvar', 2)}（负无功为容性注入；已包含在原支路两端功率内，引擎网损不含该对地导纳）")
        # 净负荷与总负荷分列。只有无发电时两者才相等；含 PV/发电时
        # 净负荷 = 负荷 − 发电，可能远小于总负荷甚至为负（反送）。
        # 不写这一行，读者会把源侧供电当负荷，或把负值当异常。
        if ss.net_load_kw is not None and ss.total_load_kw is not None:
            if abs(ss.net_load_kw - ss.total_load_kw) > max(
                abs(ss.total_load_kw) * 0.01, 1.0
            ):
                gen_txt = (
                    f"，其中发电注入 {_fmt(ss.total_generator_kw, 'kW', 2)}"
                    if ss.total_generator_kw is not None
                    else ""
                )
                L.append(
                    f"- 净负荷（源侧供电 − 网损）：{_fmt(ss.net_load_kw, 'kW', 2)}"
                    f"，与总负荷的差异来自发电、储能或固定并联导纳{gen_txt}"
                )
        if ss.load_elements_skipped:
            L.append(
                f"- ⚠️ 有 {int(ss.load_elements_skipped)} 个负荷元件读数缺失未计入求和，"
                "总负荷可能偏小"
            )
        L.append(f"- 总网损：{_fmt(ls.total_loss_kw, 'kW', 2)} / "
                 f"{_fmt(ls.total_loss_kvar, 'kvar', 2)}"
                 + (f"（占负荷 {ls.loss_pct_of_load:.2f}%）" if ls.loss_pct_of_load is not None else ""))
        L.append(f"- 最低电压：{_fmt(ss.min_voltage_pu, 'pu')}（母线 {ss.min_voltage_bus or _NA}）")
        L.append(f"- 最高电压：{_fmt(ss.max_voltage_pu, 'pu')}（母线 {ss.max_voltage_bus or _NA}）")
        # 分相口径下的真实电压下限：比「平衡等值最低电压」更低，才是真正的越限判断依据。
        if ss.min_phase_voltage_pu is not None:
            L.append(
                f"- **真实最低相电压：{_fmt(ss.min_phase_voltage_pu, 'pu')}**"
                f"（{ss.min_phase_voltage_bus or _NA} 的 {ss.min_phase_label or '?'} 相）"
                "—— 分相口径；上面的「平衡等值最低电压」取每母线三相最大值，偏乐观"
            )
        L.append("")

    # 8. 三相不平衡（OpenDSS 原生分相能力）
    #
    # ⚠️ 本节必须自己判 `result is None`：第 4~7 节都写在
    # `if result is not None and converged:` 块里，而本节在块外。
    # 早期失败（compile 校验拒绝、依赖缺失等）时 result 恒为 None，
    # 少了这个判断就会在 `result.unbalance_summary` 上抛 AttributeError，
    # 使整个请求降级成 HTTP 500 —— 前端只看到"服务器错误"，拿不到失败原因。
    # （实测踩过：坏引用算例被校验规则**正确拒绝**，却回了 500。）
    L.append("## 8. 三相不平衡\n")
    ub = result.unbalance_summary if result is not None else None
    if ub is None:
        L.append(f"- {_NA}：本次任务未产出求解结果，无法评估三相不平衡。\n")
    elif ub.buses_assessed:
        L.append(f"- 计算方法：{ub.method}")
        L.append(
            f"- 参与评估母线：{ub.buses_assessed}（不适用 {ub.buses_not_assessed}：相数不足或相序异常）"
        )
        if ub.worst_bus:
            L.append(
                f"- **最严重母线：{ub.worst_bus}，不平衡度 ε = {_fmt(ub.worst_unbalance_pct, '%', 3)}**"
            )
            L.append(f"  - 该母线分相电压：{_fmt_phase_list(ub.worst_phase_voltage_pu)} (pu)")
            L.append(f"  - 该母线分相相角：{_fmt_phase_list(ub.worst_phase_angle_deg, nd=2)} (°)")
        if ub.spread_worst_pct is not None:
            L.append(
                f"- 最大相间偏差（辅助指标，非标准值）：{_fmt(ub.spread_worst_pct, '%', 3)}"
                f"（母线 {ub.spread_worst_bus}）"
            )
        L.append(f"- 参考限值：{_fmt(ub.limit_pct, '%', 1)} —— {ub.limit_source}")
        if ub.buses_over_limit_count:
            L.append("")
            L.append(f"**超过参考限值的母线（{ub.buses_over_limit_count} 个）**")
            L.append("")
            L.append("| 母线 | ε (%) | A/B/C 相电压 (pu) |")
            L.append("|---|---|---|")
            for v in ub.buses_over_limit:
                L.append(
                    f"| {v.name} | {_fmt(v.unbalance_pct, nd=3)} | {_fmt_phase_list(v.phase_voltage_pu)} |"
                )
        L.append("")
        L.append(
            "> 说明：ε 为负序/正序分量之比，与相角参考系无关（三相整体旋转不改变 ε）；\n"
            "> 相量不足 3 个的母线（单相/两相）不平衡度无定义，计入「不适用」而非记 0。\n"
        )
    else:
        L.append("- 本算例无可评估的三相母线（全部相数不足或相序异常）。\n")

    # 8. Verification
    L.append("## 9. 独立验证（Verification）\n")
    if verification is not None:
        L.append(f"- 总体结论：**{'通过' if verification.passed else '未通过'}**")
        L.append("")
        L.append("| 验证域 | 结果 | 说明 |")
        L.append("|---|---|---|")
        for c in verification.checks:
            L.append(f"| {c.domain} | {'✅' if c.passed else '❌'} | {c.detail} |")
        L.append("")
        L.append("判据来源：")
        for k, v in verification.criteria_sources.items():
            L.append(f"- {k}：{v}")
        L.append("")
    else:
        L.append("- 未执行验证。\n")

    # 9. Evidence
    L.append("## 10. Evidence 与关键结论来源\n")
    if evidence:
        L.append("| 证据 ID | 对象 | 字段 | 值 | 单位 | 支撑结论 |")
        L.append("|---|---|---|---|---|---|")
        for ev in evidence:
            L.append(
                f"| {ev.evidence_id} | {ev.object_ref} | {ev.field} | {ev.value} | "
                f"{ev.unit or '—'} | {ev.claim} |"
            )
        L.append("")
    else:
        L.append("- 无（未进入发布阶段）。\n")

    # 10. 解释与限制
    L.append("## 11. 结果解释\n")
    if explanation:
        L.append(explanation + "\n")
    L.append("## 12. 限制与后续建议\n")
    L.append("- 本结果为仿真计算结果，不是生产调度命令。")
    L.append(
        "- V1.3 能力边界：稳态、确定性、单场景潮流计算。"
        "**三相不平衡已启用**（原生分相建模 + 不平衡度指标，见第 8 节）；"
        "**自动控制动作**（调压器/电容器投切、有载调压）与**概率潮流**未启用；"
        "发电机无功限值默认不施加（见「本次求解口径声明」）。"
    )
    L.append(f"- 电压/负载率限值按当前仿真判据（{defaults.version}），未经专业审批，不作为“安全/合格”标准。")
    if case.fidelity_status == "warning":
        L.append("- ⚠️ 这是格式转换后的近似等值模型：本次 OpenDSS 收敛不等于原始 XLSX/CSV/JSON/MATPOWER 模型已等价复现；请用原始求解器或独立基准交叉核验。")
    if status in (TaskStatus.PARTIAL, TaskStatus.NOT_ASSESSED, TaskStatus.NON_CONVERGED, TaskStatus.VALIDATION_FAILED):
        L.append("- ⚠️ 本任务验证受限或求解未收敛，**要求人工复核**。")
    L.append("")
    return "\n".join(L)


def render_html(markdown_text: str, task_id: str) -> str:
    """轻量 HTML 包装（不引入外部依赖）。"""
    lines = []
    for raw in markdown_text.split("\n"):
        line = html.escape(raw)
        if line.startswith("# "):
            line = f"<h1>{line[2:]}</h1>"
        elif line.startswith("## "):
            line = f"<h2>{line[3:]}</h2>"
        elif line.startswith("### "):
            line = f"<h3>{line[4:]}</h3>"
        elif line.startswith("- "):
            line = f"<li>{line[2:]}</li>"
        elif line.startswith("|"):
            line = f"<pre>{line}</pre>"
        elif line.strip():
            line = f"<p>{line}</p>"
        line = line.replace("**", "")  # 简化处理粗体标记
        lines.append(line)
    body = "\n".join(lines)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>配电网潮流计算报告 {html.escape(task_id)}</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1em; color: #222; }}
h1 {{ border-bottom: 2px solid #1a5fb4; padding-bottom: .3em; }}
h2 {{ color: #1a5fb4; margin-top: 1.6em; }}
pre {{ background: #f6f8fa; padding: .4em .8em; border-radius: 4px; margin: 0; overflow-x: auto; }}
li {{ margin: .2em 0; }}
</style></head><body>
{body}
</body></html>"""


def render_csv(result: CanonicalDistributionPowerFlowResult, path: str) -> str:
    """results.csv：母线 / 支路 / 变压器结果合并导出。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["# bus_results"])
        w.writerow(["name", "base_kv", "voltage_pu", "voltage_kv", "angle_deg", "status"])
        for b in result.bus_results:
            w.writerow([b.name, b.base_kv, b.voltage_pu, b.voltage_kv, b.angle_deg, b.status])
        w.writerow([])
        w.writerow(["# branch_results"])
        w.writerow(
            ["name", "from_bus", "to_bus", "p_kw", "q_kvar", "current_a", "rating_a", "loading_pct", "loading_status"]
        )
        for br in result.branch_results:
            w.writerow(
                [br.name, br.from_bus, br.to_bus, br.p_kw, br.q_kvar, br.current_a,
                 br.rating_a, br.loading_pct, br.loading_status]
            )
        if result.transformer_results:
            w.writerow([])
            w.writerow(["# transformer_results"])
            w.writerow(["name", "p_kw", "q_kvar", "loading_pct", "loading_status"])
            for tr in result.transformer_results:
                w.writerow([tr.name, tr.p_kw, tr.q_kvar, tr.loading_pct, tr.loading_status])
        w.writerow([])
        w.writerow(["# system_summary"])
        ss = result.system_summary
        ls = result.loss_summary
        w.writerow(["total_load_kw", ss.total_load_kw])
        w.writerow(["total_generation_kw", ss.total_generation_kw])
        w.writerow(["total_loss_kw", ls.total_loss_kw])
        w.writerow(["min_voltage_pu", ss.min_voltage_pu])
        w.writerow(["min_voltage_bus", ss.min_voltage_bus])
    return path
