"""CanonicalDistributionPowerFlowResult（文档 11.4 / 12.4）。

统一对象身份、字段、单位、状态和来源的权威潮流结果。
缺失域必须显式 unavailable / not_assessed，禁止用 0 或空值冒充。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .enums import ResultScope

FieldStatus = Literal["ok", "unavailable", "not_assessed"]


class BusResult(BaseModel):
    """母线结果。

    ⚠️ 口径（勿混用）：
    - `voltage_pu` 是**平衡等值幅值**（历史上取三相最大值），用于与平衡模型参考值对比，
      保持向后兼容；
    - `phase_voltage_pu` 才是**真实的分相电压**，不平衡信息只在这里。
      报告里给分相数据，`voltage_pu` 必须标注为"平衡等值"。
    """

    name: str
    base_kv: float | None = None
    voltage_pu: float | None = None          # 平衡等值幅值 (pu)
    voltage_kv: float | None = None
    angle_deg: float | None = None
    status: FieldStatus = "ok"
    # ---- 分相数据（OpenDSS 原生给出，不允许再被压平）----
    phase_labels: list[str] = Field(default_factory=list)        # ["A","B","C"]
    phase_voltage_pu: list[float] = Field(default_factory=list)  # 逐相电压 (pu)
    phase_voltage_kv: list[float] = Field(default_factory=list)
    phase_angle_deg: list[float] = Field(default_factory=list)
    # ---- 不平衡度量 ----
    unbalance_pct: float | None = None        # ε = |U2|/|U1| × 100%
    phase_spread_pct: float | None = None     # (max−min)/mean ×100%，辅助值
    negative_seq_pu: float | None = None
    zero_seq_pu: float | None = None
    unbalance_status: FieldStatus = "not_assessed"


class BranchResult(BaseModel):
    name: str            # 元件名，如 Line.xxx
    from_bus: str = ""
    to_bus: str = ""
    p_kw: float | None = None      # 首端有功（流出为正，方向定义固定）
    q_kvar: float | None = None
    p_to_kw: float | None = None  # positive into source-model branch at to port
    q_to_kvar: float | None = None
    source_model: str = ""
    current_a: float | None = None
    rating_a: float | None = None  # 额定电流；不可用则 loading 为 unavailable
    loading_pct: float | None = None
    loading_status: FieldStatus = "ok"
    status: FieldStatus = "ok"


class TransformerResult(BaseModel):
    name: str
    # 连接母线：拓扑判据（连通分量 / 孤岛 / 环路）**必须**有它。
    # 配电网馈线首端普遍经变压器挂在上级电网节点（如 `grid`）上，
    # 缺了它就只剩"一堆互不相连的馈线"，与"已收敛"直接矛盾。
    from_bus: str = ""
    to_bus: str = ""
    p_kw: float | None = None
    q_kvar: float | None = None
    p_to_kw: float | None = None
    q_to_kvar: float | None = None
    source_model: str = ""
    loading_pct: float | None = None
    loading_status: FieldStatus = "ok"
    status: FieldStatus = "ok"


class SystemSummary(BaseModel):
    # 总负荷：由**负荷元件端子1有功之和**得到，忠实反映负荷自身消耗。
    # 曾用「源侧供电 − 网损」倒推，那实际是**净负荷**，含 PV/发电时等于
    # 负荷 − 发电，数值可正可负（实测出现过 −57332 kW），不能冠以"总负荷"。
    total_load_kw: float | None = None
    total_load_kvar: float | None = None
    # 源侧供电（Circuit.TotalPower 取反，供电为正）。
    total_generation_kw: float | None = None
    total_generation_kvar: float | None = None
    # 净负荷：源侧供电 − 网损。可作为功率平衡的独立参照，
    # 但**不是**"总负荷"，报告必须与 total_load_kw 分列并注明差异来源。
    net_load_kw: float | None = None
    # 发电元件注入（端子1有功之和取反，注入为正）；用于解释
    # net_load_kw 与 total_load_kw 的差值是否可由发电解释。
    total_generator_kw: float | None = None
    total_generator_kvar: float | None = None
    # 储能净消耗：端子1有功之和，**保留「流入元件为正」的原始符号**
    #   > 0 = 充电（从电网取电，等价于负荷）
    #   < 0 = 放电（向电网送电，等价于发电）
    # 储能**不归入** total_load_kw / total_generator_kw —— 它是双向元件，
    # 归进任何一边都会在报告里产生"负的负荷"或"负的发电"，无法解释。
    # 但它**必须参与功率平衡**。实测（9/16）：4 母线算例「负荷 1000 kW +
    # 储能放电 500 kW」，若平衡式不含储能，残差 = 0.5 pu（容差 1e-3）→
    # 任务被判 VERIFICATION_FAILED:['power_balance'] 而直接失败。
    total_storage_kw: float | None = None
    total_storage_kvar: float | None = None
    storage_elements: int | None = None
    # Converted fixed Gs/Bs admittance: positive consumption, negative injection.
    # Separate from load/generation and excluded from network device losses.
    total_shunt_kw: float | None = None
    total_shunt_kvar: float | None = None
    shunt_elements: int | None = None
    total_branch_shunt_kw: float | None = None
    total_branch_shunt_kvar: float | None = None
    branch_shunt_elements: int | None = None
    # 负荷求和时因读数缺失被跳过的元件数；>0 说明 total_load_kw 偏小，需排查。
    load_elements_skipped: int | None = None
    min_voltage_pu: float | None = None
    min_voltage_bus: str | None = None
    max_voltage_pu: float | None = None
    max_voltage_bus: str | None = None
    # 真实最低相电压（分相口径）：比平衡等值的 min 更低，才是真正的电压下限。
    min_phase_voltage_pu: float | None = None
    min_phase_voltage_bus: str | None = None
    min_phase_label: str | None = None


class UnbalanceViolation(BaseModel):
    name: str
    unbalance_pct: float | None = None
    phase_voltage_pu: list[float] = Field(default_factory=list)


class UnbalanceSummary(BaseModel):
    """全网三相不平衡汇总。

    限值只作**参考标注**：在未与标准原文核对前，`limit_source` 必须写明来源口径，
    不得让读者误以为已被权威认定。
    """

    method: str = ""
    limit_pct: float | None = None
    limit_source: str = ""
    buses_assessed: int = 0
    buses_not_assessed: int = 0
    worst_bus: str | None = None
    worst_unbalance_pct: float | None = None
    worst_phase_voltage_pu: list[float] = Field(default_factory=list)
    worst_phase_angle_deg: list[float] = Field(default_factory=list)
    buses_over_limit: list[UnbalanceViolation] = Field(default_factory=list)
    buses_over_limit_count: int = 0
    spread_worst_bus: str | None = None
    spread_worst_pct: float | None = None
    notes: list[str] = Field(default_factory=list)


class LossSummary(BaseModel):
    total_loss_kw: float | None = None
    total_loss_kvar: float | None = None
    loss_pct_of_load: float | None = None


class SolverStatus(BaseModel):
    converged: bool
    iterations: int | None = None
    error_flag: bool = False
    raw_solution_count: int | None = None
    # 运行、稳定化与独立复算阶段的累计；不包含 Master 内部的隐式 Solve。
    total_iterations: int | None = None


class CanonicalDistributionPowerFlowResult(BaseModel):
    """权威潮流结果（文档 11.4）。"""

    task_id: str
    case_fingerprint: str
    solver_status: SolverStatus
    bus_results: list[BusResult] = Field(default_factory=list)
    branch_results: list[BranchResult] = Field(default_factory=list)
    transformer_results: list[TransformerResult] = Field(default_factory=list)
    system_summary: SystemSummary = Field(default_factory=SystemSummary)
    loss_summary: LossSummary = Field(default_factory=LossSummary)
    unbalance_summary: UnbalanceSummary = Field(default_factory=UnbalanceSummary)
    result_status_by_scope: dict[str, FieldStatus] = Field(default_factory=dict)
    solver_profile_ref: str = ""
    engine_version: str = ""
    adapter_version: str = ""

    def scope_status(self, scope: ResultScope) -> FieldStatus:
        return self.result_status_by_scope.get(scope.value, "not_assessed")
