"""恢复与复现性回归：只用临时算例和假适配器，不启动原生引擎。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from ..config import AgentConfig, SimDefaults
from ..domain.case.case_loader import load_case_bundle
from ..engines.opendss_adapter import CircuitHandle, OpenDSSRawResult, SolverExecutionResult
from ..runtime.guard import RuleGuard
from ..runtime.tracing import Tracer
from ..schemas.enums import ErrorCode, OutputFormat, ResultScope, TaskStatus
from ..schemas.result import (BranchResult, BusResult, CanonicalDistributionPowerFlowResult,
                              LossSummary, SolverStatus, SystemSummary, TransformerResult)
from ..schemas.solver import IterationConfig, OpenDSSSolverProfile, ToleranceConfig
from ..schemas.state import AgentState
from ..schemas.task_spec import PowerFlowTaskSpec, SolverConfig, UserConstraints
from ..skills import powerflow_skill as workflow
from ..tools.base import ToolOutput
from ..tools import powerflow_tools as tools
from ..verification.verifier import Verifier


class FakeAdapter:
    def __init__(self):
        self.compile_calls = 0
        self.fail_first_compile = False
        self.solve_calls = 0
        self.converged = True
        self.error_flag = False
        self.raw = OpenDSSRawResult(
            solver={"converged": True, "iterations": 2},
            buses=[{"name": "b", "base_kv": 10, "nodes": [1, 2, 3],
                    "voltage_pu": [1, 1, 1], "angle_deg": [0, -120, 120]}],
            system={"total_power_kw": 110, "load_kw": 100, "gen_kw": 0,
                    "storage_kw": 0, "losses_kw": 10},
            element_count={"bus": 1},
        )

    def compile_case(self, root, master):
        self.compile_calls += 1
        if self.fail_first_compile and self.compile_calls == 1:
            raise RuntimeError("compile failed")
        return CircuitHandle("fake", root, master, True)

    def apply_solver_profile(self, circuit, profile):
        self.applied_params = profile.param_dict()

    def solve(self, circuit):
        self.solve_calls += 1
        return SolverExecutionResult(self.converged, 2, self.error_flag)

    def extract_raw_result(self, circuit, scopes):
        return self.raw


def result() -> CanonicalDistributionPowerFlowResult:
    return CanonicalDistributionPowerFlowResult(
        task_id="test", case_fingerprint="fp", solver_profile_ref="profile",
        solver_status=SolverStatus(converged=True),
        bus_results=[BusResult(name="b", base_kv=10, voltage_pu=1, voltage_kv=10,
                               phase_labels=["A", "B", "C"], phase_voltage_pu=[1, 1, 1],
                               phase_voltage_kv=[10, 10, 10], phase_angle_deg=[0, -120, 120])],
        branch_results=[BranchResult(name="Line.L1", from_bus="b", to_bus="c", p_kw=100,
                                     q_kvar=20, current_a=10, rating_a=100, loading_pct=10)],
        transformer_results=[TransformerResult(name="Transformer.T1", from_bus="grid", to_bus="b",
                                                p_kw=110, q_kvar=25, loading_pct=15)],
        system_summary=SystemSummary(total_generation_kw=110, total_load_kw=100,
                                     total_generator_kw=0, total_storage_kw=0),
        loss_summary=LossSummary(total_loss_kw=10),
    )


class RecoveryVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dpf-recovery-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name

    def context(self, master="New Generator.DG Model=3\n", dependencies=None, solver_config=None):
        source = os.path.join(self.root, "source")
        work = os.path.join(self.root, "task")
        os.makedirs(source, exist_ok=True)
        for name, body in {"Master.dss": master, **(dependencies or {})}.items():
            path = os.path.join(source, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        case = load_case_bundle(os.path.join(source, "Master.dss"), work, source)
        case.object_summary = {"bus": 1}
        config = AgentConfig(outputs_root=os.path.join(self.root, "outputs"), workspace_root=work)
        config.solve_cache_enabled = True
        spec = PowerFlowTaskSpec(task_id="test", case_ref="test", result_scopes=[ResultScope.BUS, ResultScope.SYSTEM],
                                 output_formats=[OutputFormat.JSON], solver_config=solver_config or SolverConfig())
        profile = OpenDSSSolverProfile(profile_id="profile", result_scopes=spec.result_scopes,
                                       iteration_config=IterationConfig(max_iterations=15),
                                       tolerance_config=ToleranceConfig(tolerance=1e-4))
        if spec.solver_config.max_iterations is not None:
            profile.iteration_config.max_iterations = spec.solver_config.max_iterations
        if spec.solver_config.tolerance is not None:
            profile.tolerance_config.tolerance = spec.solver_config.tolerance
        ctx = tools.TaskContext(spec, work, config, FakeAdapter(), case=case, solver_profile=profile,
                                circuit=CircuitHandle("fake", os.path.join(work, "input"),
                                                      os.path.join(work, "input", "Master.dss"), True))
        ctx.save_json("validation.json", {"passed": True, "case": case.model_dump(mode="json")})
        ctx.save_json("solver_profile.json", profile)
        return ctx

    def test_only_generators_and_continuations_change(self):
        text = ("NeW Generator.DG bus1=b\n! Model=3 stays\n\n"
                "~ MoDeL = '3' ! Model=3 stays\n"
                "Edit Generator.DG model=3 // comment Model=3\n"
                "New Load.motor Model=3\n~ model = 3\n"
                "New Generator.other model=30\n")
        changed, names = workflow._rewrite_generator_models(text)
        self.assertEqual(names, {"dg"})
        self.assertIn("~ MoDeL = '1' ! Model=3 stays", changed)
        self.assertIn("Edit Generator.DG model=1 // comment Model=3", changed)
        self.assertIn("New Load.motor Model=3\n~ model = 3", changed)
        self.assertIn("New Generator.other model=30", changed)

    def test_quoted_object_and_comment_markers(self):
        text = 'New "Generator.DG" bus1="a!b//c" spectrum="model=3 stays" model="3" // model=3\n'
        changed, names = workflow._rewrite_generator_models(text)
        self.assertEqual(names, {"dg"})
        self.assertEqual(changed, text.replace('model="3"', 'model="1"'))
        for unsupported in ("BatchEdit Generator..* model=3\n", "New Generator.dg Like=template model=3\n"):
            with self.assertRaises(ValueError):
                workflow._rewrite_generator_models(unsupported)

    def test_unsupported_recovery_syntax_leaves_files_unchanged(self):
        ctx = self.context("New Generator.DG Model=3\nBatchEdit Generator..* Model=3\n")
        before = ctx.case.case_fingerprint
        with open(ctx.circuit.master_dss, "rb") as f:
            original = f.read()
        self.assertEqual(workflow._downgrade_pv_voltage_control(ctx), (False, 0))
        self.assertEqual(ctx.case.case_fingerprint, before)
        with open(ctx.circuit.master_dss, "rb") as f:
            self.assertEqual(f.read(), original)

    def test_dependency_recovery_refreshes_identity_and_disables_cache(self):
        ctx = self.context("Redirect DG.dss\nNew Load.motor Model=3\n", {"DG.dss": "New Generator.DG\n~ model = 3\n"})
        original = ctx.case.case_fingerprint
        key_before = tools.RunPowerFlowTool()._solve_cache_key(ctx)
        self.assertEqual(workflow._downgrade_pv_voltage_control(ctx), (True, 1))
        self.assertTrue(ctx.model_recovery_applied)
        self.assertNotEqual(original, ctx.case.case_fingerprint)
        self.assertNotEqual(key_before, tools.RunPowerFlowTool()._solve_cache_key(ctx))
        agg = hashlib.sha256()
        for entry in sorted(ctx.case.dependency_manifest.entries, key=lambda e: e.path):
            with open(os.path.join(ctx.circuit.case_workspace, entry.path), "rb") as f:
                content = f.read()
            self.assertEqual(entry.sha256, hashlib.sha256(content).hexdigest())
            self.assertEqual(entry.size_bytes, len(content))
            agg.update(f"{entry.path}:{entry.sha256}\n".encode())
        self.assertEqual(ctx.case.case_fingerprint, agg.hexdigest()[:16])
        with open(ctx.artifacts["validation.json"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["case"]["case_fingerprint"], ctx.case.case_fingerprint)
        with open(ctx.artifacts["original_case.json"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["case_fingerprint"], original)
        with open(os.path.join(self.root, "source", "DG.dss"), encoding="utf-8") as f:
            self.assertIn("model = 3", f.read())

    def test_recovery_cache_neither_reads_nor_writes_shared_file(self):
        ctx = self.context()
        workflow._downgrade_pv_voltage_control(ctx)
        cache = os.path.join(self.root, "cached.json")
        payload = {"solver": {"converged": True, "iterations": 99}}
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with patch.object(tools.RunPowerFlowTool, "_cache_path", return_value=cache):
            out = tools.RunPowerFlowTool().run(ctx)
        self.assertFalse(out["cache_hit"])
        self.assertEqual(ctx.adapter.solve_calls, 1)
        with open(cache, encoding="utf-8") as f:
            self.assertEqual(json.load(f), payload)

    def test_available_cache_refreshes_live_first_solve_and_commits_after_verification(self):
        ctx = self.context()
        cache = os.path.join(self.root, "verified-cache.json")
        stale = {"solver": {"converged": True, "iterations": 99}, "system": {"load_kw": 999}}
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(stale, f)
        with patch.object(tools.RunPowerFlowTool, "_cache_path", return_value=cache), \
             patch.object(tools, "AgentConfig", return_value=ctx.config):
            out = tools.RunPowerFlowTool().run(ctx)
            self.assertEqual(ctx.adapter.solve_calls, 1)
            self.assertEqual(ctx.solve_result.iterations, 2)
            self.assertEqual(ctx.raw_result.system["load_kw"], 100)
            self.assertFalse(out["cache_hit"])
            self.assertTrue(out["cache_available"])
            with open(ctx.artifacts["raw_opendss_result.json"], encoding="utf-8") as f:
                metadata = json.load(f)["cache"]
            self.assertEqual(metadata["result_source"], "live_solve")
            self.assertTrue(metadata["engine_synchronized"])
            self.assertIsNotNone(ctx.pending_solve_cache)
            with open(cache, encoding="utf-8") as f:
                self.assertEqual(json.load(f), stale)
            tools.ExtractPowerFlowResultTool().run(ctx)
            verified = tools.VerifyPowerBalanceTool().run(ctx)
            self.assertTrue(verified["passed"])
            self.assertEqual(ctx.adapter.solve_calls, 2)
            self.assertIsNone(ctx.pending_solve_cache)
            with open(cache, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["system"]["load_kw"], 100)

    def test_cached_success_does_not_hide_real_first_solve_nonconvergence_or_error(self):
        for converged, error_flag, expected_code in (
            (False, False, ErrorCode.NON_CONVERGED),
            (False, True, ErrorCode.NON_CONVERGED),
            (True, True, ErrorCode.ENGINE_ERROR),
        ):
            with self.subTest(converged=converged, error_flag=error_flag):
                ctx = self.context()
                ctx.adapter.converged = converged
                ctx.adapter.error_flag = error_flag
                cache = os.path.join(self.root, "existing-cache.json")
                original = {"solver": {"converged": True, "iterations": 7}}
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(original, f)
                with patch.object(tools.RunPowerFlowTool, "_cache_path", return_value=cache), \
                     patch.object(tools, "AgentConfig", return_value=ctx.config):
                    with self.assertRaises(tools.ToolFailure) as caught:
                        tools.RunPowerFlowTool().run(ctx)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(ctx.adapter.solve_calls, 1)
                self.assertIsNone(ctx.pending_solve_cache)
                with open(cache, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), original)

    def test_integrity_failure_never_commits_pending_cache(self):
        ctx = self.context()
        cache = os.path.join(self.root, "must-not-exist.json")
        with patch.object(tools.RunPowerFlowTool, "_cache_path", return_value=cache), \
             patch.object(tools, "AgentConfig", return_value=ctx.config):
            tools.RunPowerFlowTool().run(ctx)
            self.assertFalse(os.path.isfile(cache))
            tools.ExtractPowerFlowResultTool().run(ctx)
            ctx.canonical_result.system_summary.total_load_kw = 999
            out = tools.VerifyPowerBalanceTool().run(ctx)
        self.assertFalse(out["passed"])
        self.assertIn("power_balance", out["failed_domains"])
        self.assertFalse(os.path.isfile(cache))
        self.assertIsNone(ctx.pending_solve_cache)

    def test_second_solve_failure_never_commits_pending_cache(self):
        ctx = self.context()
        cache = os.path.join(self.root, "must-not-exist-after-second-failure.json")
        with patch.object(tools.RunPowerFlowTool, "_cache_path", return_value=cache), \
             patch.object(tools, "AgentConfig", return_value=ctx.config):
            tools.RunPowerFlowTool().run(ctx)
            tools.ExtractPowerFlowResultTool().run(ctx)
            ctx.adapter.converged = False
            out = tools.VerifyPowerBalanceTool().run(ctx)
        self.assertFalse(out["passed"])
        self.assertIn("reproducibility", out["failed_domains"])
        self.assertEqual(ctx.adapter.solve_calls, 2)
        self.assertFalse(os.path.isfile(cache))
        self.assertIsNone(ctx.pending_solve_cache)

    def test_failed_recovery_rolls_back_files_and_identity(self):
        ctx = self.context()
        before = ctx.case.model_dump()
        with open(ctx.circuit.master_dss, "rb") as f:
            data = f.read()
        ctx.adapter.fail_first_compile = True
        self.assertEqual(workflow._downgrade_pv_voltage_control(ctx), (False, 0))
        self.assertEqual(ctx.case.model_dump(), before)
        self.assertFalse(ctx.model_recovery_applied)
        self.assertEqual(ctx.adapter.compile_calls, 2)
        with open(ctx.circuit.master_dss, "rb") as f:
            self.assertEqual(f.read(), data)

    def run_numeric_recovery(self, ctx):
        class Solver:
            stage = "run_opendss_powerflow"
            calls = 0
            def invoke(self, ctx, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise tools.ToolFailure(ErrorCode.NON_CONVERGED, TaskStatus.NON_CONVERGED, "test")
                return ToolOutput(True, "run_powerflow", {"converged": True, "iterations": 2})
        class Stop:
            stage = "normalize_results"
            def invoke(self, ctx, **kwargs):
                raise tools.ToolFailure(ErrorCode.ENGINE_ERROR, TaskStatus.SYSTEM_FAILED, "stop-after-recovery")
        solver = Solver()
        with patch.object(workflow, "_STAGE_TOOLS", [("run_opendss_powerflow", "run_powerflow"),
                                                    ("normalize_results", "extract_powerflow_result")]), \
             patch.dict(workflow.TOOL_REGISTRY, {"run_powerflow": solver, "extract_powerflow_result": Stop()}), \
             patch.object(workflow, "_choose_replan_action", return_value=("relax_numeric", "test")):
            with self.assertRaises(tools.ToolFailure):
                workflow.run_fixed_powerflow_skill(ctx, AgentState(task_id="test"), Tracer(cli_callback=None), RuleGuard())
        self.assertEqual(solver.calls, 2)

    def test_unpinned_recovery_persists_actual_profile(self):
        ctx = self.context()
        self.run_numeric_recovery(ctx)
        self.assertEqual(ctx.solver_profile.iteration_config.max_iterations, 60)
        self.assertAlmostEqual(ctx.solver_profile.tolerance_config.tolerance, 1e-3)
        with open(ctx.artifacts["solver_profile.json"], encoding="utf-8") as f:
            self.assertEqual(json.load(f), ctx.solver_profile.model_dump(mode="json"))
        self.assertEqual(ctx.adapter.applied_params, ctx.solver_profile.param_dict())

    def test_explicit_iteration_is_preserved_during_recovery(self):
        ctx = self.context(solver_config=SolverConfig(max_iterations=1))
        self.run_numeric_recovery(ctx)
        self.assertEqual(ctx.solver_profile.iteration_config.max_iterations, 1)
        self.assertAlmostEqual(ctx.solver_profile.tolerance_config.tolerance, 1e-3)

    def test_both_explicit_parameters_remove_numeric_recovery(self):
        ctx = self.context(solver_config=SolverConfig(max_iterations=1, tolerance=1e-6))
        self.assertEqual(workflow._numeric_replan_params(ctx), (1, 1e-6))
        ctx.task_spec.solver_config = SolverConfig()
        ctx.task_spec.constraints = UserConstraints(hard={"max_iterations": 1, "tolerance": 1e-6})
        self.assertEqual(workflow._numeric_replan_params(ctx), (1, 1e-6))

    def test_pinned_tolerance_and_large_existing_values_never_tighten(self):
        ctx = self.context(solver_config=SolverConfig(tolerance=1e-6))
        self.assertEqual(workflow._numeric_replan_params(ctx), (60, 1e-6))
        ctx.task_spec.solver_config = SolverConfig()
        ctx.solver_profile.iteration_config.max_iterations = 200
        ctx.solver_profile.tolerance_config.tolerance = 0.1
        self.assertEqual(workflow._numeric_replan_params(ctx), (200, 0.1))

    def test_frozen_explicit_parameters_are_not_offered_to_replanner(self):
        ctx = self.context(solver_config=SolverConfig(max_iterations=1, tolerance=1e-6))
        class Solver:
            stage = "run_opendss_powerflow"
            calls = 0
            def invoke(self, ctx, **kwargs):
                self.calls += 1
                raise tools.ToolFailure(ErrorCode.NON_CONVERGED, TaskStatus.NON_CONVERGED, "test")
        def choose(ctx, candidates, failure, tried):
            self.assertEqual([item["id"] for item in candidates], ["downgrade_pv_to_pq"])
            return "downgrade_pv_to_pq", "test"
        solver = Solver()
        with patch.object(workflow, "_STAGE_TOOLS", [("run_opendss_powerflow", "run_powerflow")]), \
             patch.dict(workflow.TOOL_REGISTRY, {"run_powerflow": solver}), \
             patch.object(workflow, "_choose_replan_action", side_effect=choose), \
             patch.object(workflow, "_downgrade_pv_voltage_control", return_value=(False, 0)):
            with self.assertRaises(tools.ToolFailure) as caught:
                workflow.run_fixed_powerflow_skill(ctx, AgentState(task_id="test"), Tracer(cli_callback=None), RuleGuard())
        self.assertEqual(caught.exception.code, ErrorCode.NON_CONVERGED)
        self.assertEqual(solver.calls, 1)
        self.assertEqual(ctx.solver_profile.iteration_config.max_iterations, 1)
        self.assertEqual(ctx.solver_profile.tolerance_config.tolerance, 1e-6)

    def test_second_solve_must_be_converged_and_error_free(self):
        for converged, error_flag in ((False, False), (True, True)):
            with self.subTest(converged=converged, error_flag=error_flag):
                ctx = self.context()
                ctx.canonical_result = result()
                ctx.canonical_result.case_fingerprint = ctx.case.case_fingerprint
                ctx.canonical_result.branch_results = []
                ctx.canonical_result.transformer_results = []
                ctx.adapter.converged = converged
                ctx.adapter.error_flag = error_flag
                out = tools.VerifyPowerBalanceTool().run(ctx)
                self.assertFalse(out["passed"])
                self.assertIn("reproducibility", out["failed_domains"])

    def test_second_valid_solve_passes(self):
        from ..domain.result_normalizer import normalize_powerflow_result
        from .. import ADAPTER_VERSION
        ctx = self.context()
        ctx.canonical_result = normalize_powerflow_result(
            ctx.adapter.raw, ctx.adapter.solve(ctx.circuit), ctx.task_spec.task_id,
            ctx.case.case_fingerprint, ctx.solver_profile.profile_id, "", ADAPTER_VERSION,
            ctx.task_spec.result_scopes,
        )
        self.assertTrue(tools.VerifyPowerBalanceTool().run(ctx)["passed"])

    def test_reproducibility_rejects_phase_power_identity_and_missing_status(self):
        changes = [
            lambda r: r.bus_results[0].phase_voltage_pu.__setitem__(0, 0.3),
            lambda r: setattr(r.branch_results[0], "p_kw", 300),
            lambda r: setattr(r.transformer_results[0], "q_kvar", 100),
            lambda r: setattr(r.system_summary, "total_load_kw", 500),
            lambda r: setattr(r.loss_summary, "total_loss_kw", 50),
            lambda r: setattr(r.bus_results[0], "name", "other"),
            lambda r: setattr(r.bus_results[0], "status", "unavailable"),
            lambda r: setattr(r.solver_status, "converged", False),
            lambda r: r.bus_results[0].phase_angle_deg.__setitem__(0, float("nan")),
            lambda r: setattr(r.branch_results[0], "p_kw", None),
        ]
        first = result()
        verifier = Verifier(SimDefaults())
        for i, change in enumerate(changes):
            with self.subTest(change=i):
                second = first.model_copy(deep=True)
                change(second)
                self.assertFalse(verifier.check_reproducibility(first, second).passed)

    def test_reproducibility_accepts_reordering_roundoff_and_angle_wrap(self):
        first = result()
        first.bus_results.append(first.bus_results[0].model_copy(update={"name": "c"}))
        second = first.model_copy(deep=True)
        second.bus_results.reverse()
        bus = second.bus_results[0]
        bus.phase_labels = ["C", "A", "B"]
        bus.phase_voltage_pu = [1.000001, 1, 1]
        bus.phase_angle_deg = [480, 360, 240]
        second.branch_results[0].p_kw += 0.001
        self.assertTrue(Verifier(SimDefaults()).check_reproducibility(first, second).passed)

    def test_reproducibility_empty_optional_domains_does_not_crash(self):
        first = CanonicalDistributionPowerFlowResult(task_id="t", case_fingerprint="fp",
                                                     solver_status=SolverStatus(converged=True))
        self.assertTrue(Verifier(SimDefaults()).check_reproducibility(first, first.model_copy(deep=True)).passed)


def main() -> int:
    outcome = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RecoveryVerificationTests))
    return 0 if outcome.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
