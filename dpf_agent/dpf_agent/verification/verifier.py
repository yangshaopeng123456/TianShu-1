"""独立 Verifier（文档 12.5 / 13.5 / 13.6）。

与 OpenDSS Adapter 权责分离：不修改权威结果来"通过验证"。
验证域：输入身份、求解状态、结果完整性、数值有效性、功率平衡、复现性、Evidence 完整性。
V1.3 不依赖第二潮流引擎；功率平衡由本 Verifier 独立聚合复算。
"""
from __future__ import annotations

import math

from ..config import SimDefaults
from ..schemas.case import CanonicalDistributionCase
from ..schemas.result import CanonicalDistributionPowerFlowResult
from ..schemas.solver import OpenDSSSolverProfile
from ..schemas.verification import CheckResult, VerificationResult


def _num_ok(x: float | None) -> bool:
    return x is not None and x == x and x not in (float("inf"), float("-inf"))


class Verifier:
    def __init__(self, defaults: SimDefaults):
        self.defaults = defaults

    def verify(
        self,
        case: CanonicalDistributionCase,
        result: CanonicalDistributionPowerFlowResult,
        solver_profile: OpenDSSSolverProfile,
        requested_scopes: list[str],
    ) -> VerificationResult:
        checks: list[CheckResult] = []

        # 1. 输入身份：指纹、依赖清单、对象数量存在性
        identity_ok = (
            bool(case.case_fingerprint)
            and case.case_fingerprint == result.case_fingerprint
            and case.dependency_manifest.file_count > 0
        )
        checks.append(
            CheckResult(
                check_id="input_identity",
                domain="input_identity",
                passed=identity_ok,
                detail=f"fingerprint={case.case_fingerprint}, "
                f"dependencies={case.dependency_manifest.file_count} files",
            )
        )

        # 2. 求解状态一致性：非收敛不得伪造结果
        st = result.solver_status
        state_ok = st.converged and not st.error_flag
        checks.append(
            CheckResult(
                check_id="solver_state",
                domain="solver_state",
                passed=state_ok,
                detail=f"converged={st.converged}, iterations={st.iterations}, error_flag={st.error_flag}",
            )
        )

        # 3. 结果完整性：已请求且网络中实际存在的结果域，对象数量与字段完整
        obj = case.object_summary or {}
        incomplete: list[str] = []
        if "bus" in requested_scopes and obj.get("bus", 1) > 0 and not any(
            b.status == "ok" for b in result.bus_results
        ):
            incomplete.append("bus")
        if "branch" in requested_scopes and obj.get("active_line", obj.get("line", 0)) > 0 and not result.branch_results:
            incomplete.append("branch")
        if (
            "transformer" in requested_scopes
            and obj.get("active_transformer", obj.get("transformer", 0)) > 0
            and not result.transformer_results
        ):
            incomplete.append("transformer")
        checks.append(
            CheckResult(
                check_id="completeness",
                domain="completeness",
                passed=not incomplete,
                detail="请求结果域完整" if not incomplete else f"缺失结果域: {incomplete}",
            )
        )

        # 4. 数值有效性：关键字段无未解释 NaN/inf
        bad_fields: list[str] = []
        for b in result.bus_results:
            if b.status == "ok" and not _num_ok(b.voltage_pu):
                bad_fields.append(f"bus.{b.name}.voltage_pu")
        for br in result.branch_results:
            if br.status == "ok" and not _num_ok(br.p_kw):
                bad_fields.append(f"{br.name}.p_kw")
        checks.append(
            CheckResult(
                check_id="numeric_validity",
                domain="numeric_validity",
                passed=not bad_fields,
                detail="关键字段数值有效" if not bad_fields else f"非法数值: {bad_fields[:5]}",
            )
        )

        # 4.5 物理合理性：OpenDSS 可能接受未知 bus 并生成零基准母线；
        # 这类结果即使 Converged=True 也不得作为正常成功结果发布。
        physical_issues: list[str] = []
        ss = result.system_summary
        ls = result.loss_summary
        voltages = [
            float(b.voltage_pu) for b in result.bus_results
            if b.status == "ok" and _num_ok(b.voltage_pu)
        ]
        if any(v <= 0 or v > 2.0 for v in voltages):
            physical_issues.append("存在非正或超过 2.0 pu 的母线电压")
        if _num_ok(ls.total_loss_kw) and ls.total_loss_kw < -self.defaults.loss_roundoff_kw:
            physical_issues.append(f"总有功网损为负值：{ls.total_loss_kw:.6f} kW")
        # 高比例 DG/反送功率时，网损占“净负荷”可能超过 100%；这不是非法数值。
        # 只有无穷大、负网损等已由其他检查覆盖的情况才应阻断。
        if _num_ok(ls.loss_pct_of_load) and ls.loss_pct_of_load > 10000.0:
            physical_issues.append(f"网损占负荷比例异常：{ls.loss_pct_of_load:.4f}%")
        checks.append(
            CheckResult(
                check_id="physical_sanity",
                domain="physical_sanity",
                passed=not physical_issues,
                detail=f"母线电压与网损处于基本物理合理范围（网损绝对舍入容差 {self.defaults.loss_roundoff_kw:g} kW）"
                if not physical_issues else "; ".join(physical_issues),
            )
        )

        # 5. 功率平衡：独立复算 源侧供电 − 负荷 + 发电注入 − 储能净消耗 − 损耗（文档 13.5）
        #
        # ⚠️ 这里必须用**发电元件注入**参与，不能用"源侧供电 − 负荷"。
        # 因为 total_load_kw 现在忠实等于负荷元件消耗（不含发电），
        # 而源侧供电是**净**流入电网的功率。含 PV/发电时：
        #     源侧供电 = 负荷 − 发电注入 + 网损
        # 若沿用旧式 `gen − load − loss`，含发电算例会恒不满足平衡，
        # 把正确的解判成"不平衡"。
        #
        # ⚠️ 储能也必须进入这个式子，尽管它**不计入**负荷或发电。
        # 储能是第三种元件（双向）：充电=从电网取电（等价负荷）、放电=向电网送电
        # （等价发电）。它既不进 total_load_kw 也不进 total_generator_kw——
        # 归进任何一边都会产生"负的负荷/负的发电"，无法向读者解释。
        # 但它**必须**出现在平衡式里，否则残差恰好等于储能功率。
        # 实测（9/16，真引擎）：4 母线算例「负荷 1000 kW + 储能放电 500 kW」，
        # 平衡式不含储能时 residual = 0.500000 pu（容差 1e-3）→ 任务直接
        # VERIFICATION_FAILED:['power_balance']。真实算例 case07 也踩到过
        # （两个储能 state=idling，仅 27 kW，残差 3.4e-4 侥幸过关）。
        # 符号约定「流入元件为正」→ 充电为正、放电为负，故平衡式里**减去**它。
        ss = result.system_summary
        ls = result.loss_summary
        residual_pu: float | None = None
        balance_ok = False
        if _num_ok(ss.total_generation_kw) and _num_ok(ss.total_load_kw) and _num_ok(ls.total_loss_kw):
            gen_elem_kw = ss.total_generator_kw if _num_ok(ss.total_generator_kw) else 0.0
            # 缺该字段时按 0 处理：老产物（口径修复前）本就不含储能项，行为不变。
            storage_net_kw = ss.total_storage_kw if _num_ok(ss.total_storage_kw) else 0.0
            shunt_net_kw = ss.total_shunt_kw if _num_ok(ss.total_shunt_kw) else 0.0
            branch_shunt_kw = ss.total_branch_shunt_kw if _num_ok(ss.total_branch_shunt_kw) else 0.0
            residual_kw = abs(
                ss.total_generation_kw - ss.total_load_kw + gen_elem_kw
                - storage_net_kw - shunt_net_kw - branch_shunt_kw - ls.total_loss_kw
            )
            base = max(abs(ss.total_load_kw), abs(gen_elem_kw), abs(shunt_net_kw), 1.0)
            residual_pu = residual_kw / base
            balance_ok = residual_pu <= self.defaults.power_balance_tol_rel
        checks.append(
            CheckResult(
                check_id="power_balance",
                domain="power_balance",
                passed=balance_ok,
                detail=(
                    f"residual={residual_pu:.6f} pu（容差 {self.defaults.power_balance_tol_rel}）"
                    if residual_pu is not None
                    else "功率平衡所需字段缺失，无法复算"
                ),
                measured=residual_pu,
                tolerance=self.defaults.power_balance_tol_rel,
            )
        )

        # 6. 格式转换保真度：转换模型求解成功不等于原始格式等价复现。
        #    native DSS 可继续作为正式结果；转换来源至少明确标为受限/待交叉核验。
        if case.fidelity_status == "warning":
            checks.append(
                CheckResult(
                    check_id="input_fidelity",
                    domain="input_fidelity",
                    passed=True,
                    detail=(
                        f"已识别受限保真度：源格式 {case.source_format or 'converted'} 已转换为平衡三相 OpenDSS 等值模型；"
                        "该检查通过仅表示限制已被记录，不表示与原始求解器等价复现。"
                    ),
                )
            )
        else:
            checks.append(
                CheckResult(
                    check_id="input_fidelity",
                    domain="input_fidelity",
                    passed=True,
                    detail="原生 OpenDSS 算例，未经过表格/矩阵格式转换。",
                )
            )

        # 7. 复现性：由调用方做二次求解比对（此处记录检查占位，由 run 流程填充）
        #    见 runtime 中的 reproducibility 二次校验。

        passed = all(c.passed for c in checks)
        return VerificationResult(
            task_id=result.task_id,
            passed=passed,
            checks=checks,
            power_balance_residual_pu=residual_pu,
            power_balance_tolerance=self.defaults.power_balance_tol_rel,
            criteria_sources={
                "power_balance_tolerance": f"仿真默认阈值 {self.defaults.version}",
                "voltage_limits": f"仿真默认阈值 {self.defaults.version} "
                f"({self.defaults.voltage_min_pu}~{self.defaults.voltage_max_pu} pu)",
                "loading_limit": f"仿真默认阈值 {self.defaults.version} "
                f"({self.defaults.loading_limit_pct}%)",
            },
        )

    def check_reproducibility(
        self,
        first: CanonicalDistributionPowerFlowResult,
        second: CanonicalDistributionPowerFlowResult,
        rel_tol: float = 1e-4,
    ) -> CheckResult:
        """复现性：同输入、同版本、同 SolverProfile 的两次求解结果一致（文档 13.5）。

        默认容差 1e-4 pu，与 OpenDSS 默认收敛容差同量级：二次求解从上次解
        出发继续迭代求精，差异上限是第一次解的残差，不能按 1e-5 级别的
        “完全确定性”要求。真正的非确定性求解差异通常远大于该量级。"""
        diffs: list[str] = []
        rel_tol = max(float(rel_tol), 1e-8)
        for label, result in (("first", first), ("second", second)):
            if not result.solver_status.converged or result.solver_status.error_flag:
                diffs.append(f"{label}.solver_state")
        for field in ("case_fingerprint", "solver_profile_ref", "engine_version", "adapter_version",
                      "result_status_by_scope"):
            if getattr(first, field) != getattr(second, field):
                diffs.append(field)

        # 功率接近零时使用全网功率规模给出绝对容差，避免近零无功的相对误差发散。
        power_scale = max([1.0] + [
            abs(value) for result in (first, second)
            for field in ("total_generation_kw", "total_load_kw", "total_generator_kw", "total_storage_kw", "total_shunt_kw")
            if _num_ok(value := getattr(result.system_summary, field))
        ])
        power_abs_tol = max(1e-3, 2.0 * rel_tol * power_scale)
        angle_tol = max(0.05, 2.0 * math.degrees(rel_tol))

        def number(path: str, left: float | None, right: float | None,
                   abs_tol: float = 1e-4, angle: bool = False) -> None:
            if left is None and right is None:
                return
            if not _num_ok(left) or not _num_ok(right):
                diffs.append(path)
            elif angle:
                if abs((left - right + 180.0) % 360.0 - 180.0) > angle_tol:
                    diffs.append(path)
            elif not math.isclose(left, right, rel_tol=rel_tol, abs_tol=abs_tol):
                diffs.append(path)

        def objects(label: str, left: list, right: list) -> tuple[dict, dict]:
            a = {item.name.lower(): item for item in left}
            b = {item.name.lower(): item for item in right}
            if len(a) != len(left) or len(b) != len(right) or a.keys() != b.keys():
                diffs.append(f"{label}.identities")
            return a, b

        buses1, buses2 = objects("bus", first.bus_results, second.bus_results)
        for name in buses1.keys() & buses2.keys():
            a, b = buses1[name], buses2[name]
            path = f"bus.{name}"
            if a.status != b.status or a.unbalance_status != b.unbalance_status:
                diffs.append(f"{path}.status")
            for field in ("base_kv", "voltage_pu", "voltage_kv", "negative_seq_pu", "zero_seq_pu"):
                number(f"{path}.{field}", getattr(a, field), getattr(b, field))
            if not a.phase_labels and not b.phase_labels:
                number(f"{path}.angle_deg", a.angle_deg, b.angle_deg, angle=True)
            for field in ("unbalance_pct", "phase_spread_pct"):
                number(f"{path}.{field}", getattr(a, field), getattr(b, field), abs_tol=0.02)
            # 相别身份决定配对；母线/相的遍历顺序不应影响复现性。
            if (len(set(a.phase_labels)) != len(a.phase_labels)
                    or len(set(b.phase_labels)) != len(b.phase_labels)
                    or set(a.phase_labels) != set(b.phase_labels)):
                diffs.append(f"{path}.phase_identities")
            for field in ("phase_voltage_pu", "phase_voltage_kv", "phase_angle_deg"):
                va, vb = getattr(a, field), getattr(b, field)
                if not va and not vb:
                    continue
                if len(va) != len(a.phase_labels) or len(vb) != len(b.phase_labels):
                    diffs.append(f"{path}.{field}.completeness")
                    continue
                da, db = dict(zip(a.phase_labels, va)), dict(zip(b.phase_labels, vb))
                for phase in da.keys() & db.keys():
                    number(f"{path}.{phase}.{field}", da[phase], db[phase], angle=field == "phase_angle_deg")

        for label, left, right in (
            ("branch", first.branch_results, second.branch_results),
            ("transformer", first.transformer_results, second.transformer_results),
        ):
            a_map, b_map = objects(label, left, right)
            for name in a_map.keys() & b_map.keys():
                a, b = a_map[name], b_map[name]
                path = f"{label}.{name}"
                for field in ("status", "loading_status", "from_bus", "to_bus", "source_model"):
                    if str(getattr(a, field)).lower() != str(getattr(b, field)).lower():
                        diffs.append(f"{path}.{field}")
                for field in ("p_kw", "q_kvar", "p_to_kw", "q_to_kvar"):
                    number(f"{path}.{field}", getattr(a, field), getattr(b, field), power_abs_tol)
                number(f"{path}.loading_pct", a.loading_pct, b.loading_pct, abs_tol=0.02)
                if label == "branch":
                    for field in ("current_a", "rating_a"):
                        number(f"{path}.{field}", getattr(a, field), getattr(b, field), abs_tol=0.01)

        for field in type(first.system_summary).model_fields:
            a, b = getattr(first.system_summary, field), getattr(second.system_summary, field)
            if field.endswith(("_kw", "_kvar")):
                number(f"system.{field}", a, b, power_abs_tol)
            elif field.endswith("_pu"):
                number(f"system.{field}", a, b)
            elif field in ("storage_elements", "shunt_elements", "branch_shunt_elements", "load_elements_skipped") and a != b:
                diffs.append(f"system.{field}")
            # 极值母线/相在同值并列时可换位：每个对象的数值已逐项比较。
        for field in ("total_loss_kw", "total_loss_kvar", "loss_pct_of_load"):
            number(f"loss.{field}", getattr(first.loss_summary, field), getattr(second.loss_summary, field),
                   0.02 if field == "loss_pct_of_load" else power_abs_tol)
        return CheckResult(
            check_id="reproducibility",
            domain="reproducibility",
            passed=not diffs,
            detail="两次求解结果一致" if not diffs else f"结果不一致对象: {diffs[:5]}",
        )
