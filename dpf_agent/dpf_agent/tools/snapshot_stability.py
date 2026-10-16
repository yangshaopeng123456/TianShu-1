"""同一 snapshot 的有界数值稳定化；验证器仍使用原来的比较标准。

只在首次完整验证通过而复算结果漂移时使用。两对连续结果稳定后，
再执行一次独立复算；不改模型、容差、Mode 或控制方式。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Any

from .. import ADAPTER_VERSION
from ..domain.result_normalizer import normalize_powerflow_result
from ..runtime.cancellation import is_cancelled
from ..schemas.verification import CheckResult

POLICY_VERSION = "bounded-snapshot-v1"


class RefinementCancelled(Exception):
    pass


@dataclass
class StabilityOutcome:
    passed: bool
    check: CheckResult
    raw: Any = None
    solve: Any = None
    canonical: Any = None
    verification: Any = None


def policy(ctx: Any) -> dict[str, Any]:
    pinned = ctx.task_spec.solver_config.max_iterations
    limit = ctx.solver_profile.iteration_config.max_iterations
    return {
        "version": POLICY_VERSION,
        "max_solves": int(ctx.config.stability_max_solves),
        "timeout_s": float(ctx.config.stability_timeout_s),
        "per_solve_max_iterations": limit,
        # 这是额外保守预算，不能把最近一次 Iterations 冒充累计迭代次数。
        "iteration_budget": int(pinned) if pinned is not None else max(30, 2 * limit),
        "budget_source": "explicit_limit" if pinned is not None else "bounded_default",
        "required_stable_pairs": 2,
        "counted_stages": "run/refinement/verification; excludes Compile-internal Solve",
    }


def _identity(result: Any) -> tuple:
    """对象和静态属性始终与原候选配对，不允许把缺失对象的新基线当作稳定。"""
    return (
        result.case_fingerprint, result.solver_profile_ref,
        result.engine_version, result.adapter_version,
        tuple(sorted(result.result_status_by_scope.items())),
        tuple(sorted((b.name.lower(), b.base_kv, tuple(sorted(b.phase_labels)), b.status,
                      b.unbalance_status) for b in result.bus_results)),
        tuple(sorted((b.name.lower(), b.from_bus.lower(), b.to_bus.lower(), b.rating_a,
                      b.status, b.loading_status) for b in result.branch_results)),
        tuple(sorted((t.name.lower(), t.from_bus.lower(), t.to_bus.lower(), t.status,
                      t.loading_status) for t in result.transformer_results)),
        result.system_summary.storage_elements, result.system_summary.load_elements_skipped,
    )


def refine_snapshot(ctx: Any, second_raw: Any, second_solve: Any, second: Any,
                    verifier: Any, rel_tol: float, initial_check: CheckResult) -> StabilityOutcome:
    limits = policy(ctx)
    began = time.monotonic()
    audit = {"policy": limits, "initial_reproducibility": initial_check.model_dump(mode="json"),
             "calls": [], "comparisons": [], "passed": False, "stopped_reason": ""}
    original = ctx.canonical_result
    anchor = _identity(original)
    total = 0

    def context_signature() -> dict:
        if hasattr(ctx.adapter, "context_signature"):
            return ctx.adapter.context_signature(timeout_s=max(0.001, remaining_time()))
        def read(eng):
            sol = eng.ActiveCircuit.Solution
            return {"mode": int(sol.Mode), "control_mode": int(sol.ControlMode),
                    "hour": float(sol.DblHour), "max_iterations": int(sol.MaxIterations),
                    "tolerance": float(sol.Tolerance)}
        return ctx.adapter.run_with_engine(read, timeout_s=max(0.001, remaining_time()))

    def remaining_time() -> float:
        return limits["timeout_s"] - (time.monotonic() - began)

    def cancelled() -> None:
        if is_cancelled(ctx.task_spec.task_id):
            audit["stopped_reason"] = "cancelled"
            raise RefinementCancelled("任务已被用户取消")

    def record(stage: str, raw: Any, solved: Any, canonical: Any, cap: int) -> None:
        nonlocal total
        total += int(solved.iterations)
        number = len(audit["calls"]) + 1
        audit["calls"].append({"number": number, "stage": stage,
                               "iteration_limit": cap, **asdict(solved)})
        ctx.save_json(f"stability_solve_{number:03d}.json", {
            "stage": stage, "solve": asdict(solved), "raw": asdict(raw) if raw is not None else None,
            "canonical": canonical.model_dump(mode="json") if canonical is not None else None,
        })

    def failure(reason: str) -> StabilityOutcome:
        audit["stopped_reason"] = reason
        return StabilityOutcome(False, CheckResult(
            check_id="reproducibility", domain="reproducibility", passed=False,
            detail=f"有界数值稳定化未通过：{reason}；累计迭代 {total}/{limits['iteration_budget']}，"
                   f"实际 Solve {len(audit['calls'])}/{limits['max_solves']}",
        ))

    def normalize(raw: Any, solved: Any) -> Any:
        return normalize_powerflow_result(
            raw, solved, task_id=ctx.task_spec.task_id,
            case_fingerprint=ctx.case.case_fingerprint,
            solver_profile_ref=ctx.solver_profile.profile_id,
            engine_version=ctx.solver_profile.version_set.get("engine_version", ""),
            adapter_version=ADAPTER_VERSION,
            requested_scopes=ctx.task_spec.result_scopes,
        )

    def candidate_check(candidate: Any) -> Any:
        return verifier.verify(ctx.case, candidate, ctx.solver_profile,
                               [s.value for s in ctx.task_spec.result_scopes])

    def next_solve(stage: str, reserve_final: bool) -> tuple | StabilityOutcome:
        cancelled()
        remaining_iterations = limits["iteration_budget"] - total - (2 if reserve_final else 0)
        if len(audit["calls"]) >= limits["max_solves"] - (1 if reserve_final else 0):
            return failure("达到 Solve 次数上限")
        if remaining_iterations < 2:
            return failure("累计迭代预算不足，不再追加求解")
        if remaining_time() <= 0:
            return failure("达到稳定化时间预算")
        cap = min(limits["per_solve_max_iterations"], remaining_iterations)
        solved = ctx.adapter.solve(ctx.circuit, iteration_limit=cap, timeout_s=remaining_time())
        ctx.record_solve(stage, solved, cap)
        # 即使随后的取消、采集或时间检查失败，也记录这次实际完成的 Solve。
        record(stage, None, solved, None, cap)
        cancelled()
        if remaining_time() <= 0:
            return failure("本次求解已用尽稳定化时间预算")
        raw = ctx.adapter.extract_raw_result(ctx.circuit, ctx.solver_profile.result_scopes,
                                             timeout_s=remaining_time())
        candidate = normalize(raw, solved)
        ctx.save_json(f"stability_solve_{len(audit['calls']):03d}.json", {
            "stage": stage, "solve": asdict(solved), "raw": asdict(raw),
            "canonical": candidate.model_dump(mode="json"),
        })
        if not solved.converged or solved.error_flag:
            return failure("追加求解未收敛或返回引擎错误")
        if int(solved.iterations) > cap or total > limits["iteration_budget"]:
            return failure("引擎报告迭代数超过受限预算")
        if remaining_time() <= 0:
            return failure("采集结果后已达到稳定化时间预算")
        if context_signature() != audit["snapshot_context"]:
            return failure("snapshot 时刻、求解参数或控制方式发生变化")
        if _identity(candidate) != anchor:
            return failure("对象身份或静态模型属性发生变化")
        full = candidate_check(candidate)
        if not full.passed:
            audit["invalid_candidate_verification"] = full.model_dump(mode="json")
            return failure("追加求解未通过完整性、物理有效性或功率平衡验证")
        return raw, solved, candidate, full

    try:
        cancelled()
        record("initial", ctx.raw_result, ctx.solve_result, original,
               limits["per_solve_max_iterations"])
        record("initial_recheck", second_raw, second_solve, second,
               limits["per_solve_max_iterations"])
        audit["snapshot_context"] = context_signature()
        if audit["snapshot_context"]["mode"] != 0 or audit["snapshot_context"]["control_mode"] != -1:
            return failure("当前引擎不满足 snapshot/controlmode=off 条件")
        if (audit["snapshot_context"]["max_iterations"] != limits["per_solve_max_iterations"]
                or audit["snapshot_context"]["tolerance"] != ctx.solver_profile.tolerance_config.tolerance):
            return failure("当前原生求解参数与冻结 SolverProfile 不一致")
        if _identity(second) != anchor or not candidate_check(second).passed:
            return failure("初始复算改变了对象身份或未通过完整验证")
        previous = second
        stable_pairs = 0
        while True:
            produced = next_solve("refinement", reserve_final=True)
            if isinstance(produced, StabilityOutcome):
                return produced
            raw, solved, candidate, full = produced
            compare = verifier.check_reproducibility(previous, candidate, rel_tol=rel_tol)
            audit["comparisons"].append(compare.model_dump(mode="json"))
            stable_pairs = stable_pairs + 1 if compare.passed else 0
            previous = candidate
            if stable_pairs < limits["required_stable_pairs"]:
                continue
            # 稳定化结束后才指定权威候选，再做一次独立复算；失败不继续挑选结果。
            final = next_solve("independent_final", reserve_final=False)
            if isinstance(final, StabilityOutcome):
                return final
            _final_raw, _final_solve, final_candidate, _final_full = final
            check = verifier.check_reproducibility(candidate, final_candidate, rel_tol=rel_tol)
            audit["final_reproducibility"] = check.model_dump(mode="json")
            if not check.passed:
                return failure("稳定化后的独立复算仍不一致")
            cancelled()
            full.checks.append(check)
            full.passed = full.passed and check.passed
            task_count = len(ctx.solve_history)
            task_iterations = sum(row["iterations"] for row in ctx.solve_history)
            raw.solver = {**raw.solver, "solution_count": task_count,
                          "total_iterations": task_iterations, "stabilization_policy": POLICY_VERSION}
            candidate.solver_status.raw_solution_count = task_count
            candidate.solver_status.total_iterations = task_iterations
            audit["passed"] = True
            audit["stopped_reason"] = "stable_then_independently_verified"
            audit["authoritative_candidate_call"] = len(audit["calls"]) - 1
            return StabilityOutcome(True, check, raw, solved, candidate, full)
    except Exception as exc:
        if not audit["stopped_reason"]:
            audit["stopped_reason"] = f"exception: {type(exc).__name__}: {exc}"
        raise
    finally:
        audit["total_iterations"] = total
        audit["solution_count"] = len(audit["calls"])
        audit["elapsed_s"] = time.monotonic() - began
        audit["task_solve_history"] = ctx.solve_history
        audit["task_solution_count"] = len(ctx.solve_history)
        audit["task_total_iterations"] = sum(row["iterations"] for row in ctx.solve_history)
        ctx.save_json("numerical_stabilization.json", audit)
