# -*- coding: utf-8 -*-
"""三相不平衡度计算回归测试（自包含，不依赖引擎）。

锁定的口径：ε = |U₂| / |U₁| × 100%（对称分量法，负序/正序）。
这些断言是"数字对不对"的唯一防线——不平衡度算错会静默地把所有结论带偏。

运行：`.venv/Scripts/python.exe -m dpf_agent.tests.test_unbalance`
"""

from __future__ import annotations

import math
import sys

from ..domain.unbalance import compute_metrics, sequence_components, phasor

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def approx(a: float | None, b: float, tol: float = 1e-6) -> bool:
    return a is not None and abs(a - b) <= tol


def main() -> int:
    # ① 理想对称 → 不平衡度必须为 0（含幅值非 1.0 的情形，避免把幅值当不平衡）
    m = compute_metrics([1.0, 1.0, 1.0], [0.0, -120.0, 120.0])
    check("① 完全对称 ε=0", m.status == "ok" and approx(m.unbalance_pct, 0.0), f"ε={m.unbalance_pct}")
    m2 = compute_metrics([0.95, 0.95, 0.95], [0.0, -120.0, 120.0])
    check("① 整体降压仍视为平衡", approx(m2.unbalance_pct, 0.0), f"ε={m2.unbalance_pct}")

    # ② 理论可手算的算例：B 相幅值 −5%
    #    |U2| = 0.05/3, |U1| = 2.95/3 → ε = 0.05/2.95 = 1.6949%
    m3 = compute_metrics([1.0, 0.95, 1.0], [0.0, -120.0, 120.0])
    check("② B 相跌 5% → ε=1.6949%（可手算）",
          approx(m3.unbalance_pct, 0.05 / 2.95 * 100.0, 1e-9), f"ε={m3.unbalance_pct}")

    # ③ 幅值三不相同：A=1.0/B=0.8/C=0.9（相角理想）→ U1=0.9, |U2|=0.057735 → ε=6.4150%
    m4 = compute_metrics([1.0, 0.8, 0.9], [0.0, -120.0, 120.0])
    check("③ 三幅值不同 → ε=6.4150%（可手算）",
          approx(m4.unbalance_pct, math.sqrt(0.15**2 + 0.0866**2) / 3 / 0.9 * 100.0, 2e-3),
          f"ε={m4.unbalance_pct}")

    # ④ 纯角度偏差：B 相相角偏 3°
    m5 = compute_metrics([1.0, 1.0, 1.0], [0.0, -117.0, 120.0])
    check("④ 仅相角偏 3° → ε≈1.7457%",
          approx(m5.unbalance_pct, 1.7457, 1e-3), f"ε={m5.unbalance_pct}")

    # ⑤ 相序接反（整组负序）→ 必须判为相序异常，而不是"不平衡度无穷大"
    m6 = compute_metrics([1.0, 1.0, 1.0], [0.0, 120.0, -120.0])
    check("⑤ 相序接反 → sequence_anomaly 且 ε=None",
          m6.status == "sequence_anomaly" and m6.unbalance_pct is None, f"status={m6.status}")

    # ⑥ 相量不足（单相/两相母线）→ 不平衡无定义，不得返回 0
    m7 = compute_metrics([0.9], [0.0])
    check("⑥ 单相母线 → insufficient_phases 且 ε=None",
          m7.status == "insufficient_phases" and m7.unbalance_pct is None, f"status={m7.status}")
    m8 = compute_metrics([0.9, 0.8], [0.0, -120.0])
    check("⑥ 两相母线 → insufficient_phases", m8.status == "insufficient_phases")
    m9 = compute_metrics([], [])
    check("⑥ 空输入 → insufficient_phases", m9.status == "insufficient_phases")

    # ⑦ 旋转不变性：三相整体平移相角不改变不平衡度（说明 ε 不依赖角度参考系）
    base = compute_metrics([1.0, 0.93, 1.04], [0.0, -120.0, 120.0])
    rotated = compute_metrics([1.0, 0.93, 1.04], [37.0, -83.0, 157.0])
    check("⑦ 整体旋转相角不改变 ε",
          approx(base.unbalance_pct, rotated.unbalance_pct, 1e-9),
          f"ε={base.unbalance_pct} vs {rotated.unbalance_pct}")

    # ⑧ 缺相角时的降级：按理想相角算，并且必须在 note 里声明
    m10 = compute_metrics([1.0, 0.95, 1.0], None)
    check("⑧ 缺相角 → 按理想相角计算并声明",
          m10.status == "ok" and "相角" in m10.note, f"note={m10.note!r}")

    # ⑨ 辅助值：最大相间偏差（非标准指标，仅参考）
    m11 = compute_metrics([1.0, 0.9, 1.1], [0.0, -120.0, 120.0])
    check("⑨ 相间偏差 (max-min)/mean = 20%",
          approx(m11.phase_spread_pct, (1.1 - 0.9) / 1.0 * 100.0, 1e-9),
          f"偏差={m11.phase_spread_pct}")

    # ⑩ 对称分量分解自检：正序输入应只落在 U1 上
    u1, u2, u0 = sequence_components([phasor(1.0, 0), phasor(1.0, -120), phasor(1.0, 120)])
    check("⑩ 正序输入 → U1=1, U2=U0=0",
          approx(abs(u1), 1.0, 1e-9) and abs(u2) < 1e-9 and abs(u0) < 1e-9,
          f"|U1|={abs(u1):.6f} |U2|={abs(u2):.2e} |U0|={abs(u0):.2e}")
    u1b, u2b, u0b = sequence_components([phasor(1.0, 0), phasor(1.0, 120), phasor(1.0, -120)])
    check("⑩ 负序输入 → U2=1, U1=0",
          abs(u1b) < 1e-9 and approx(abs(u2b), 1.0, 1e-9),
          f"|U1|={abs(u1b):.2e} |U2|={abs(u2b):.6f}")

    # ⑪ 长度不是 3 时对称分量分解必须报错（不许悄悄算错）
    try:
        sequence_components([phasor(1.0, 0), phasor(1.0, -120)])
        check("⑪ 相量数≠3 时抛错", False, "未抛错")
    except ValueError:
        check("⑪ 相量数≠3 时抛错", True)

    # ⑫ 真实量级自检：以实测母线为样本，ε 应落在合理区间（既不是 0 也不是爆表）
    real = compute_metrics(
        [0.703455, 0.808203, 0.672757], [-31.866, -149.8341, 92.1253]
    )
    check("⑫ 实测母线样本 ε 合理（0.5%~10%）",
          real.status == "ok" and 0.5 < real.unbalance_pct < 10.0,
          f"ε={real.unbalance_pct:.4f}%  偏差={real.phase_spread_pct:.2f}%")

    print(f"\n=== 结果：通过 {len(PASS)} / 失败 {len(FAIL)} ===")
    for name in FAIL:
        print(f"  ✗ {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
