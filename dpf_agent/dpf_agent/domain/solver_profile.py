"""SolverProfileBuilder：冻结求解策略（文档 12.2 build_opendss_solver_profile）。

约束优先级：P1 显式用户约束 > P2 任务/算例显式配置 > P3 项目冻结默认。
LLM 推测默认被禁止 —— 每个参数必须记录来源（selection_evidence）。
"""
from __future__ import annotations

import uuid

from ..config import SimDefaults
from ..schemas.solver import (
    IterationConfig,
    OpenDSSSolverProfile,
    SelectionEvidence,
    ToleranceConfig,
)
from ..schemas.task_spec import PowerFlowTaskSpec
from .network_profile import NetworkProfile


def build_solver_profile(
    task_spec: PowerFlowTaskSpec,
    network_profile: NetworkProfile,
    defaults: SimDefaults,
    engine_version: str,
    adapter_version: str,
) -> OpenDSSSolverProfile:
    evidence: list[SelectionEvidence] = []

    # max_iterations
    if task_spec.solver_config.max_iterations is not None:
        max_iter = task_spec.solver_config.max_iterations
        evidence.append(
            SelectionEvidence(source="user_constraint", detail=f"max_iterations={max_iter}（用户显式指定）")
        )
    else:
        max_iter = defaults.max_iterations
        evidence.append(
            SelectionEvidence(
                source="sim_default",
                detail=f"max_iterations={max_iter}（采用仿真默认阈值 {defaults.version}）",
            )
        )

    # tolerance
    if task_spec.solver_config.tolerance is not None:
        tol = task_spec.solver_config.tolerance
        evidence.append(
            SelectionEvidence(source="user_constraint", detail=f"tolerance={tol}（用户显式指定）")
        )
    else:
        tol = defaults.tolerance
        evidence.append(
            SelectionEvidence(
                source="sim_default",
                detail=f"tolerance={tol}（采用仿真默认阈值 {defaults.version}）",
            )
        )

    evidence.append(
        SelectionEvidence(
            source="network_profile",
            detail=(
                f"网络规模: buses={network_profile.bus_count}, lines={network_profile.line_count}, "
                f"transformers={network_profile.transformer_count}, loads={network_profile.load_count}；"
                "稳态确定性 snapshot 求解"
            ),
        )
    )
    evidence.append(
        SelectionEvidence(
            source="sim_default",
            detail="control_behavior=frozen_off：V1.3 不执行自动分接头/无功补偿/重调度/削负荷/拓扑重构",
        )
    )

    profile = OpenDSSSolverProfile(
        profile_id=f"sp-{uuid.uuid4().hex[:8]}",
        solve_mode="snapshot",
        iteration_config=IterationConfig(max_iterations=max_iter, max_control_iterations=1),
        tolerance_config=ToleranceConfig(tolerance=tol),
        control_behavior="frozen_off",
        result_scopes=list(task_spec.result_scopes),
        version_set={
            "engine": "OpenDSS",
            "engine_version": engine_version,
            "adapter_version": adapter_version,
            "defaults_version": defaults.version,
        },
        selection_evidence=evidence,
    )
    return profile.freeze()
