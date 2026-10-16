"""三相不平衡度计算（对称分量法）。

## 口径（必须与报告声明一致，改这里就要同步改声明）

- 输入是**相对地/中性点**的相电压相量：幅值(pu) + 相角(deg)，相序按 A-B-C。
- 不平衡度 `ε = |U₂| / |U₁| × 100%`（负序/正序），逐母线计算。
  这与 IEC 61000-4-30 / GB/T 15543 对"电压不平衡度"的定义口径一致；
  **限值本身需与标准原文核对**，本模块只负责算，不负责判定限值。
- 另给一个工程辅助值 `phase_spread_pct = (max−min)/mean × 100%`（最大相间偏差）。
  它**不是**标准指标，只用于直觉判断，报告中必须与 ε 分开标注。

## 为什么不用单一相电压

`max(三相)` 这种"平衡等值"会把不平衡直接抹平：实测某算例母线 `lv04_3`
三相电压相差 18.6%，被压成一个数之后，报告上完全看不出来。

## 边界（宁可说"不适用"，不臆测）

- **相量不足 3 个**（单相/两相母线）：不平衡没有定义 → 返回 `insufficient_phases`，
  此时 ε 为 None，而不是 0。
- **正序接近 0**（例如相序接反，整组呈负序）：`|U₂|/|U₁|` 会发散，
  这不是"不平衡度极大"，而是**相序异常** → 返回 `sequence_anomaly`，ε 置 None。
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field
from typing import Sequence

# 旋转算子 a = 1∠120°
_A = cmath.exp(complex(0.0, 2.0 * math.pi / 3.0))

# 认为"正序可忽略"的阈值：低于此值不再做除法，改判相序异常。
_POSITIVE_SEQ_EPS = 1e-6

METHOD = "symmetrical components: |U2|/|U1|"


@dataclass(frozen=True)
class UnbalanceMetrics:
    """单个母线的三相不平衡度量结果。"""

    status: str = "ok"  # ok / insufficient_phases / sequence_anomaly
    phase_count: int = 0
    unbalance_pct: float | None = None
    phase_spread_pct: float | None = None
    positive_seq_pu: float | None = None
    negative_seq_pu: float | None = None
    zero_seq_pu: float | None = None
    note: str = ""


def phasor(magnitude: float, angle_deg: float) -> complex:
    """幅值 + 相角(度) → 复数相量。"""
    return cmath.rect(magnitude, math.radians(angle_deg))


def sequence_components(voltages: Sequence[complex]) -> tuple[complex, complex, complex]:
    """对称分量分解，返回 (正序 U1, 负序 U2, 零序 U0)。

    要求长度为 3（A/B/C 相）。长度不为 3 时抛 ValueError ——
    调用方应先判断相数，而不是让这里悄悄算出错误结果。
    """
    if len(voltages) != 3:
        raise ValueError(f"对称分量分解需要 3 个相量，收到 {len(voltages)} 个")
    va, vb, vc = voltages
    u0 = (va + vb + vc) / 3.0
    u1 = (va + _A * vb + _A * _A * vc) / 3.0
    u2 = (va + _A * _A * vb + _A * vc) / 3.0
    return u1, u2, u0


def compute_metrics(
    magnitudes: Sequence[float],
    angles_deg: Sequence[float] | None = None,
) -> UnbalanceMetrics:
    """按相电压计算不平衡度。

    `angles_deg` 省略时按理想对称（A:0°, B:−120°, C:120°）处理 —— 这会让
    不平衡度偏小，因此仅在调用方明确没有相角数据时使用，并在 note 里标注。
    """
    mags = [float(m) for m in magnitudes if isinstance(m, (int, float)) and math.isfinite(float(m))]
    if not mags:
        return UnbalanceMetrics(status="insufficient_phases", phase_count=0, note="无有效相电压")
    if len(mags) < 3:
        return UnbalanceMetrics(
            status="insufficient_phases",
            phase_count=len(mags),
            phase_spread_pct=_spread(mags),
            note="相量少于 3 个，不平衡度无定义",
        )

    angles_note = ""
    if angles_deg and len(angles_deg) >= 3:
        angles = [float(a) for a in angles_deg[:3]]
    else:
        angles = [0.0, -120.0, 120.0]
        angles_note = "缺相角数据，按理想对称相角计算（不平衡度偏小）"

    m3 = mags[:3]
    volts = [phasor(m3[i], angles[i]) for i in range(3)]
    u1, u2, u0 = sequence_components(volts)

    if abs(u1) < _POSITIVE_SEQ_EPS:
        return UnbalanceMetrics(
            status="sequence_anomaly",
            phase_count=3,
            phase_spread_pct=_spread(m3),
            positive_seq_pu=abs(u1),
            negative_seq_pu=abs(u2),
            zero_seq_pu=abs(u0),
            note="正序分量接近 0，疑为相序接反；不平衡度不适用",
        )

    return UnbalanceMetrics(
        status="ok",
        phase_count=3,
        unbalance_pct=abs(u2) / abs(u1) * 100.0,
        phase_spread_pct=_spread(m3),
        positive_seq_pu=abs(u1),
        negative_seq_pu=abs(u2),
        zero_seq_pu=abs(u0),
        note=angles_note,
    )


def _spread(magnitudes: Sequence[float]) -> float | None:
    """最大相间偏差 (max−min)/mean × 100%。非标准指标，仅作辅助。"""
    vals = [v for v in magnitudes if isinstance(v, (int, float))]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    if abs(mean) < 1e-12:
        return None
    return (max(vals) - min(vals)) / abs(mean) * 100.0


@dataclass
class UnbalanceScan:
    """全网扫描结果（供汇总与报告使用）。"""

    method: str = METHOD
    assessed: int = 0
    not_assessed: int = 0
    worst_bus: str | None = None
    worst_unbalance_pct: float | None = None
    worst_phase_voltage_pu: list[float] = field(default_factory=list)
    worst_phase_angle_deg: list[float] = field(default_factory=list)
    violations: list[dict] = field(default_factory=list)
    spread_worst_bus: str | None = None
    spread_worst_pct: float | None = None

    def to_summary(self, limit_pct: float | None, limit_source: str, max_violations: int = 20) -> dict:
        return {
            "method": self.method,
            "limit_pct": limit_pct,
            "limit_source": limit_source,
            "buses_assessed": self.assessed,
            "buses_not_assessed": self.not_assessed,
            "worst_bus": self.worst_bus,
            "worst_unbalance_pct": self.worst_unbalance_pct,
            "worst_phase_voltage_pu": self.worst_phase_voltage_pu,
            "worst_phase_angle_deg": self.worst_phase_angle_deg,
            "buses_over_limit": self.violations[:max_violations],
            "buses_over_limit_count": len(self.violations),
            "spread_worst_bus": self.spread_worst_bus,
            "spread_worst_pct": self.spread_worst_pct,
        }
