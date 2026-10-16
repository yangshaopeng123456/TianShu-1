# -*- coding: utf-8 -*-
"""格式转换保真度实测（对照物 = 公开标准算例）。

## 要回答的问题

**xlsx / csv / json / m 转出来的 OpenDSS 模型，忠实于它所要表达的网络吗？**

## 为什么以前答不了，现在能答

此前只能用"自建硬算例"，而那是**自己出题自己判卷**（算例数据与预期结果出自同一次
生成），不构成独立验证 —— 该套件已于 2026-10-04 移出交付包。

本工具换成**从公开标准算例里取对照物**：

1. 从 OpenDSS 官方安装包自带的 IEEE 30 Bus（IEEETestCases，公开、未修改）
   中抽取 **132 kV 子网**：9 条母线、12 条线路、6 个 PQ 负荷、1 个等值电源。
2. 该子网完全落在表格格式的**声明能力**内：显式 R/X、单一电压等级（不会触发
   "跨压等级自动升变压器"）、无 PV、无控制器、无分相。因此"转换是否忠实"
   这件事**可以被逐位验证**，不会被已知近似项搅浑。
3. 同一个网络分别编码成 .xlsx / .csv(混合行) / .csv(目录=zip 内三文件) / .json / .m，
   各自经**产品自己的转换器**转回 Master.dss。
   （目录形态走的是 `_convert_csv_dir`，与混合行 CSV **是两条不同的代码分支**。）
4. 另手工写一份**原生 .dss**（同网络，不走转换器）作对照物。
5. 五者走**同一条完整求解链路**（CLI 通道 + ApplicationService），比对元件计数、
   Vmin/Vmax、总网损、功率平衡残差。

## 判据（沿用 evals/run_eval.py 的口径）

| 类别 | 判据 |
|---|---|
| 元件计数 | 必须**精确一致** |
| 电压 | \\|Δ\\| ≤ 0.010 pu 一致；≤ 0.020 pu 可解释 |
| 网损 | 相对偏差 ≤ 25% 一致 |
| 残差 | 必须通过（≤ 容差） |

## ⚠️ 本工具证明什么、不证明什么

**证明**：在上述能力范围内，五种编码（xlsx / csv 混合行 / csv 目录（=zip）/ json / m）
的转换—求解是**保真**的（差异仅来自
浮点/有效位数）。

**不证明、也不声称**：转换后的模型与"表格所代表的原始求解器"（如 MATPOWER
自己的结果）数值等价。这一条在本项目里从一开始就不成立，原因是三条已声明的
建模近似：

1. OpenDSS 负荷默认 `Vminpu=0.95`，电压低于此值会自动转恒阻抗，而 MATPOWER
   参考解是恒功率；
2. PV 无功限值默认**不执行**（`enforce_q_limits`，见转换产物头声明）；
3. 表格/矩阵类算例被转成**平衡三相**等值，相别不平衡信息不保留。

因此正确表述是「**受限保真度 + 口径可追溯**」，不是「数值等价」。
详见根目录 README「验证状态」。

运行（**必须用 `-m`**）：

    .venv\\Scripts\\python.exe -m dpf_agent.evals.run_conversion_fidelity

退出码：0 = 全部一致；1 = 存在超容差差异（可用于 CI）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

DPF_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if DPF_ROOT not in sys.path:
    sys.path.insert(0, DPF_ROOT)

from dpf_agent.paths import case_archive_root  # noqa: E402

# 132 kV 子网的母线集合。依据（可直接核对官方文件）：
#   * `Master.dss`：`~ BasekV=132 Bus1=B1 pu=1.06`  —— 电源在 B1，基准 132 kV
#   * `Transformers.DSS`：`Wdg=1 kV=132` 出现在 B6 / B4 / B28  —— 这三条是 132 kV 侧
#   * `Lines.DSS`：B1~B8 之间互相连通，B8/B6 再经 8-28 / 6-28 接到 B28
# 下面的 `_check_subnet_is_closed()` 会断言这个集合"要么整条线在内、要么整条线在外"，
# 只要官方文件里出现"一半在内一半在外"的线路就会立刻报错——避免集合写错还照样出结论。
SUBNET_132KV = ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B28")
SLACK_BUS = "B1"
SLACK_PU = 1.06
BASE_KV = 132.0
BASE_FREQ_HZ = 60.0          # IEEE 30 Bus 是 60 Hz 系统（OpenDSS 默认 50，必须显式给）
S_BASE_MVA = 100.0           # 与转换器默认一致；它决定等值电源的短路容量
MVA_SC = S_BASE_MVA * 1e6    # 转换器写的是 MVAsc3 = s_base_mva*1e6，参照物必须同值

V_TOL_OK = 0.010
V_TOL_WARN = 0.020
LOSS_TOL_WARN = 0.25

# 三态判定。**`不可判定` 必须与 `一致` 分开**：
# 判据的输入缺值/状态异常时，正确结论是"不知道"，绝不能让"没有反例"滑成"通过"。
V_OK = "一致"
V_BAD = "**超差**"
V_UNKNOWN = "不可判定"


def _judge_counts(a: Any, b: Any) -> tuple[str, str]:
    if a is None or b is None:
        return V_UNKNOWN, "缺值"
    return (V_OK, "") if a == b else (V_BAD, "")


def _judge_gauge(a: Any, b: Any, abs_tol: float) -> tuple[str, str]:
    """可比量（电压、网损）→ 用**绝对差**判。"""
    if a is None or b is None:
        return V_UNKNOWN, "缺值"
    d = abs(float(a) - float(b))
    return (V_OK if d <= abs_tol else V_BAD, f"|Δ|={d:.6f}")


def _judge_residual(a: Any, b: Any, tol: float) -> tuple[str, str]:
    """功率平衡残差是**绝对量**（离 0 有多远），判据只能是"各自是否低于容差"。

    ⚠️ 不要用相对差 —— 8.2e-05 与 1.2e-06 的相对差高达 98%，但两者都远低于容差，
    本属"都通过"。这是本工具实测踩过的**假红**（判据形式与量的性质不匹配）。
    """
    if a is None or b is None:
        return V_UNKNOWN, "缺值"
    ok = float(a) <= tol and float(b) <= tol
    return (V_OK if ok else V_BAD,
            f"两者均 ≤ 容差 {tol:g}" if ok else f"容差 {tol:g}，ref={a:g} got={b:g}")


def _self_test() -> list[str]:
    """**判据自检**：用构造样例确认判据本身能区分 通过 / 不通过 / 不可判定。

    为什么必须有它：本项目 `PITFALLS.md` 早就写着"判据写错 → 要么假红要么假绿，
    比不写更危险（假绿就没人发现）"，2026-10-04 还专门做过判据自检脚本。
    但**文档拦不住写代码的那一刻** —— 本工具第一版照样同时踩了假绿与假红。
    所以这一条不能只写在注释里，必须**可执行、且在出结论之前强制跑**。

    覆盖六种构造：
      ① 两份完全相同          → 必须全 一致
      ② 元件计数差 1          → 必须抓出 不一致（离散量用精确相等）
      ③ 电压差 0.5 pu         → 必须抓出 超差
      ④ 参照物缺值            → 必须判 **不可判定**，不得判 一致   ← 假绿实例
      ⑤ 两残差都低于容差但相差 100 倍 → 必须判 一致              ← 假红实例
      ⑥ 有一个残差超出容差    → 必须抓出 超差
    """
    failures: list[str] = []
    base = {
        "status": "success", "vmin_pu": 0.90000, "vmax_pu": 1.00000,
        "total_loss_kw": 100.0, "converged": True, "passed": True,
        "residual_pu": 1e-05, "residual_tol": 1e-03,
        "counts": {"bus_count": 3}, "total_installed_load_kw": 1000.0,
    }

    def _v(rows: list[dict[str, Any]], check_key: str) -> str:
        for r in rows:
            if r["check"] == check_key:
                return r["verdict"]
        return "(缺项)"

    # ① 完全相同
    rows = compare(base, dict(base))
    if any(r["verdict"] != V_OK for r in rows):
        failures.append("① 两份相同却出现非『一致』：" +
                        str([r for r in rows if r["verdict"] != V_OK]))

    # ② 计数差 1
    bad_counts = json.loads(json.dumps(base))
    bad_counts["counts"]["bus_count"] = 4
    if _v(compare(base, bad_counts), "计数·bus_count") != V_BAD:
        failures.append("② 计数差 1 未被判『超差』")

    # ③ 电压差 0.5 pu
    bad_v = json.loads(json.dumps(base))
    bad_v["vmin_pu"] = 0.40000
    if _v(compare(base, bad_v), "最低电压 pu") != V_BAD:
        failures.append("③ 电压差 0.5 pu 未被判『超差』")

    # ④ 参照物缺值 —— 今天的假绿就是这个形态
    missing = json.loads(json.dumps(base))
    missing["vmin_pu"] = None
    if _v(compare(base, missing), "最低电压 pu") != V_UNKNOWN:
        failures.append("④ 缺值未被判『不可判定』（假绿风险）")
    # 并且缺值不能让整轮被判成"一致"
    if not any(r["verdict"] == V_UNKNOWN for r in compare(base, missing)):
        failures.append("④-2 缺值没有在任何一条上体现为『不可判定』")

    # ⑤ 两残差都远低于容差、但彼此相差 100 倍 —— 必须判"一致"（假红回归）
    loose = json.loads(json.dumps(base))
    loose["residual_pu"] = 1e-07
    if _v(compare(base, loose), "功率平衡残差 pu") != V_OK:
        failures.append("⑤ 残差都在容差内却判了『超差』（假红风险）")

    # ⑥ 残差超容差
    over = json.loads(json.dumps(base))
    over["residual_pu"] = 1e-01
    if _v(compare(base, over), "功率平衡残差 pu") != V_BAD:
        failures.append("⑥ 残差超出容差未被判『超差』")

    return failures


# --------------------------------------------------------------------------
# 1) 从公开算例抽取子网
# --------------------------------------------------------------------------
@dataclass
class Subnet:
    buses: list[str] = field(default_factory=list)
    lines: list[tuple[str, str, float, float, str]] = field(default_factory=list)  # fb,tb,r,x,id
    loads: dict[str, tuple[float, float]] = field(default_factory=dict)           # bus -> (kW,kvar)

    def as_dict(self) -> dict[str, Any]:
        return {
            "buses": self.buses,
            "lines": [
                {"id": i, "from": f, "to": t, "r_ohm": r, "x_ohm": x}
                for f, t, r, x, i in self.lines
            ],
            "loads_kw_kvar": {k: list(v) for k, v in sorted(self.loads.items())},
        }


def official_case_dir() -> str:
    d = os.path.join(case_archive_root(), "ieee_official", "IEEE 30 Bus")
    if not os.path.isdir(d):
        raise FileNotFoundError(
            f"找不到公开算例目录：{d}\n（它来自 OpenDSS 官方安装包 IEEETestCases，"
            "属于交付包的 examples/cases/ieee_official/）"
        )
    return d


_NUM = r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


def _parse_lines(text: str) -> list[tuple[str, str, float, float, str]]:
    out = []
    for m in re.finditer(
        rf"New\s+line\.(?P<lid>\S+)\s+Bus1=(?P<fb>\S+)\s+Bus2=(?P<tb>\S+)\s+"
        rf"R1=(?P<r>{_NUM})\s+X1=(?P<x>{_NUM})",
        text,
        re.IGNORECASE,
    ):
        out.append((m.group("fb"), m.group("tb"), float(m.group("r")),
                    float(m.group("x")), m.group("lid")))
    return out


def _parse_loads(text: str) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for m in re.finditer(
        rf"New\s+Load\.\S+\s+Bus1=(?P<b>\S+)\s+kV=(?P<kv>{_NUM})\s+"
        rf"kW=(?P<p>{_NUM})\s+kvar=(?P<q>{_NUM})",
        text,
        re.IGNORECASE,
    ):
        out[m.group("b")] = (float(m.group("p")), float(m.group("q")))
    return out


def extract_subnet(case_dir: str) -> Subnet:
    lines_all = _parse_lines(_read(os.path.join(case_dir, "Lines.DSS")))
    loads_all = _parse_loads(_read(os.path.join(case_dir, "Loads.DSS")))
    keep = set(SUBNET_132KV)

    # —— 自证：子网必须"闭合"（不存在一半在内一半在外的线路），否则集合定义就是错的
    half = [(f, t) for f, t, *_ in lines_all if (f in keep) != (t in keep)]
    if half:
        raise AssertionError(
            f"132 kV 子网定义有误：存在跨子网边界的线路 {half}。"
            "请核对 SUBNET_132KV —— 这个断言就是为了防止集合写错还照样出结论。"
        )

    subnet = Subnet()
    subnet.buses = sorted(keep, key=lambda b: int(re.sub(r"\D", "", b) or 0))
    subnet.lines = [ln for ln in lines_all if ln[0] in keep and ln[1] in keep]
    subnet.loads = {b: v for b, v in loads_all.items() if b in keep}

    # —— 自证：选出来的子网必须连通且含电源母线
    adj: dict[str, set[str]] = {b: set() for b in subnet.buses}
    for f, t, *_ in subnet.lines:
        adj[f].add(t)
        adj[t].add(f)
    seen, stack = {SLACK_BUS}, [SLACK_BUS]
    while stack:
        for nb in adj[stack.pop()]:
            if nb not in seen:
                seen.add(nb)
                stack.append(nb)
    if seen != set(subnet.buses):
        raise AssertionError(
            f"子网不连通：从 {SLACK_BUS} 只能到达 {sorted(seen)}，"
            f"而定义里有 {subnet.buses}"
        )
    return subnet


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


# --------------------------------------------------------------------------
# 2) 把同一份网络编码成五种形式
# --------------------------------------------------------------------------
def _bus_rows(net: Subnet) -> list[dict[str, Any]]:
    rows = []
    for b in net.buses:
        p, q = net.loads.get(b, (0.0, 0.0))
        row: dict[str, Any] = {
            "bus_id": b,
            "base_kv": BASE_KV,
            "bus_type": "3" if b == SLACK_BUS else "1",
            "p_kw": p,
            "q_kvar": q,
        }
        if b == SLACK_BUS:
            row["vm_set_pu"] = SLACK_PU
        rows.append(row)
    return rows


def _line_rows(net: Subnet) -> list[dict[str, Any]]:
    return [
        {"line_id": lid, "from_bus": f, "to_bus": t, "r": r, "x": x}
        for f, t, r, x, lid in net.lines
    ]


def _system_row() -> dict[str, Any]:
    """系统参数行 —— 只写真实参数键，**不写任何"触发器列"**。

    这里原先额外塞了 `base_kv` / `base_mva` 两个触发器列，因为当时 `_read_system_params()`
    只有当 System 表含这两个列名时才进入"结构化单行"分支去读其它字段 —— 否则整行参数被
    **静默忽略**，`FREQUENCY_HZ` 回落 50 Hz（IEEE 30 Bus 是 60 Hz 系统，线路充电电纳会整体算错）。

    该缺陷已于 2026-10-11 修掉（判据改为"单行且非 key/value 形态"，且读不出参数时在产物里
    显式警告）。**绕行已移除** —— 于是本工具顺带成了该缺陷的回归测试：若哪天又退回旧行为，
    这里的频率会变回 50 Hz，各格式对照立刻不一致。

    键名大小写：参数键读取时统一转大写，所以 `V_BASE_KV` / `S_BASE_MVA` / `FREQUENCY_HZ`
    必须原样大写出现，转换器才取得到。
    """
    return {
        "V_BASE_KV": BASE_KV,
        "S_BASE_MVA": S_BASE_MVA,
        "FREQUENCY_HZ": BASE_FREQ_HZ,
    }


def write_reference_dss(net: Subnet, path: str) -> None:
    """手工写原生 OpenDSS —— 对照物。不走任何转换器。

    ⚠️ 源母线的写法必须与官方 `IEEE 30 Bus/Master.dss` 一致：
    `New Circuit.<名>` 会创建一个名为 `sourcebus` 的内部母线，**必须再用
    `~ Bus1=<母线名>` 把电源挂到真实母线上**。不写这一句的后果是
    "源在 sourcebus、线路接在 B1"，两边各是一个游离母线 —— 网络被整体孤岛，
    求解出来电压全 0。本工具第一版正是这么错的，表现为"参照物 validation_failed"。
    """
    out = [
        "Clear",
        f"Set DefaultBaseFrequency={BASE_FREQ_HZ:g}",
        "! 对照物：同一份网络的原生 OpenDSS 写法（不经转换器）",
        f"New Circuit.conv_fidelity_ref",
        f"~ BasekV={BASE_KV:g} Bus1={SLACK_BUS} pu={SLACK_PU:g} "
        f"MVAsc3={MVA_SC:.1f} MVAsc1={MVA_SC:.1f}",
        "",
    ]
    out += [
        f"New Line.{lid} Bus1={f} Bus2={t} R1={r:.9f} X1={x:.9f} Length=1 Units=none Phases=3 Enabled=yes"
        for f, t, r, x, lid in net.lines
    ]
    out.append("")
    for b in net.buses:
        p, q = net.loads.get(b, (0.0, 0.0))
        if abs(p) > 1e-12 or abs(q) > 1e-12:
            out.append(f"New Load.Load_{b} Bus1={b} Phases=3 kV={BASE_KV:g} kW={p:.6f} kvar={q:.6f}")
    out += ["", "Set Voltagebases=[132]", "Calcvoltagebases", ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def write_xlsx(net: Subnet, path: str) -> None:
    import pandas as pd

    with pd.ExcelWriter(path, engine="openpyxl") as w:
        pd.DataFrame(_bus_rows(net)).to_excel(w, sheet_name="Bus", index=False)
        pd.DataFrame(_line_rows(net)).to_excel(w, sheet_name="Line", index=False)
        pd.DataFrame([_system_row()]).to_excel(w, sheet_name="System", index=False)


def write_mixed_csv(net: Subnet, path: str) -> None:
    """混合行 CSV：type 列区分 bus / line / system 记录。"""
    import pandas as pd

    rows: list[dict[str, Any]] = []
    for r in _bus_rows(net):
        rows.append({"type": "bus", **r})
    for r in _line_rows(net):
        rows.append({"type": "line", **r})
    rows.append({"type": "system", **_system_row()})
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def write_bundle(net: Subnet, out_dir: str) -> str:
    """目录形态：`bus.csv` + `line.csv` + `system.csv`（等价于 zip 内三文件）。

    ⚠️ 这条路径**不是**"混合行 CSV"的另一种写法，而是转换器里**另一条代码分支**
    （`_convert_csv_dir`，zip 会先解包再走它）。它此前既没进本工具的对照，
    也没有任何测试覆盖 —— 即"文档里说支持，但没人验过"。既然要回答"转换对不对"，
    就不能把一条独立分支留在外面。
    """
    import pandas as pd

    os.makedirs(out_dir, exist_ok=True)
    pd.DataFrame(_bus_rows(net)).to_csv(
        os.path.join(out_dir, "bus.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(_line_rows(net)).to_csv(
        os.path.join(out_dir, "line.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame([_system_row()]).to_csv(
        os.path.join(out_dir, "system.csv"), index=False, encoding="utf-8-sig")
    return out_dir


def write_json(net: Subnet, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"bus": _bus_rows(net), "line": _line_rows(net), "system": _system_row()},
            f, ensure_ascii=False, indent=2,
        )


def write_matpower_m(net: Subnet, path: str) -> None:
    """MATPOWER 格式。注意：MATPOWER 的 r/x 是**标幺值**，需按 Z_base 换算，
    这是 .m 与表格格式（直接给欧姆）的固有差异，差异会体现在结果里。"""
    z_base = BASE_KV ** 2 / S_BASE_MVA
    bus_lines, gen_lines, br_lines = [], [], []
    idx = {b: i + 1 for i, b in enumerate(net.buses)}
    for b in net.buses:
        p_mw, q_mvar = (v / 1000.0 for v in net.loads.get(b, (0.0, 0.0)))
        if b == SLACK_BUS:
            bus_lines.append(
                f"\t{idx[b]}\t3\t{p_mw:.6f}\t{q_mvar:.6f}\t0\t0\t1\t{SLACK_PU:.4f}\t0\t{BASE_KV:g}\t1\t1.1\t0.9;"
            )
            gen_lines.append(f"\t{idx[b]}\t0\t0\t999\t-999\t{SLACK_PU:.4f}\t{S_BASE_MVA:g}\t1\t999\t-999;")
        else:
            bus_lines.append(
                f"\t{idx[b]}\t1\t{p_mw:.6f}\t{q_mvar:.6f}\t0\t0\t1\t1.0000\t0\t{BASE_KV:g}\t1\t1.1\t0.9;"
            )
    for fb, tb, r, x, _lid in net.lines:
        br_lines.append(
            f"\t{idx[fb]}\t{idx[tb]}\t{r / z_base:.12f}\t{x / z_base:.12f}\t0\t"
            f"{S_BASE_MVA:g}\t{S_BASE_MVA:g}\t{S_BASE_MVA:g}\t0\t0\t1;"
        )
    text = "\n".join([
        "function mpc = conv_fidelity",
        "mpc.version = '2';",
        f"mpc.baseMVA = {S_BASE_MVA:g};",
        "",
        "% bus data",
        "%bus_i type Pd Qd Gs Bs area Vm Va baseKV zone Vmax Vmin",
        "mpc.bus = [",
        *bus_lines,
        "];",
        "",
        "% generator data",
        "%bus Pg Qg Qmax Qmin Vg mBase status Pmax Pmin",
        "mpc.gen = [",
        *gen_lines,
        "];",
        "",
        "% branch data",
        "%fbus tbus r x b rateA rateB rateC ratio angle status",
        "mpc.branch = [",
        *br_lines,
        "];",
        "",
    ])
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# --------------------------------------------------------------------------
# 3) 求解（完整链路，CLI 通道）
# --------------------------------------------------------------------------
def solve_case(master_dss: str, work_root: str) -> dict[str, Any]:
    from dpf_agent.application.service import ApplicationService
    from dpf_agent.config import AgentConfig
    from dpf_agent.schemas.enums import OutputFormat, ResultScope
    from dpf_agent.schemas.task_spec import UserRequest

    cfg = AgentConfig()
    cfg.engine_backend = "cli"          # COM 只能在 Electron 进程内用（loader lock）
    cfg.llm_enabled = False             # 纯数值对照，不让 LLM 参与
    cfg.workspace_root = os.path.join(work_root, "tasks")
    cfg.outputs_root = os.path.join(work_root, "outputs")
    cfg.rag_enabled = False

    svc = ApplicationService(cfg)
    t0 = time.time()
    resp = svc.run(
        UserRequest(
            message="conversion fidelity check",
            case_ref=master_dss,
            result_scopes=list(ResultScope),
            output_formats=[OutputFormat.JSON, OutputFormat.MD],
        ),
        cli_callback=None,
    )
    # ⚠️ `AgentResponse` 是 **dict 子类**（见 application/service.py），不是 dataclass ——
    # 用属性访问会抛 AttributeError，而且会被下面的 except 吞成"缺值"。
    arts = resp.get("artifacts") or {}
    out: dict[str, Any] = {
        "status": resp.get("status"),
        "task_id": resp.get("task_id"),
        "duration_s": round(time.time() - t0, 2),
        "error": resp.get("error"),
    }
    # 计数与装机容量（来自网络画像）
    try:
        prof = json.loads(_read(arts["network_profile.json"]))
        out["counts"] = {
            k: prof.get(k) for k in
            ("bus_count", "line_count", "transformer_count", "load_count", "generator_count")
        }
        out["total_installed_load_kw"] = prof.get("total_installed_load_kw")
    except Exception:
        out["counts"] = {}
    # 数值
    try:
        r = json.loads(_read(arts["powerflow_result.json"]))
        ss, ls, sol = r.get("system_summary", {}), r.get("loss_summary", {}), r.get("solver_status", {})
        out["vmin_pu"] = ss.get("min_voltage_pu")
        out["vmin_bus"] = ss.get("min_voltage_bus")
        out["vmax_pu"] = ss.get("max_voltage_pu")
        out["total_load_kw"] = ss.get("total_load_kw")
        out["total_gen_kw"] = ss.get("total_gen_kw")
        out["total_loss_kw"] = ls.get("total_loss_kw")
        out["converged"] = sol.get("converged")
        out["iterations"] = sol.get("iterations")
    except Exception as exc:
        out["metrics_error"] = f"{type(exc).__name__}: {exc}"
    # 独立验证
    try:
        v = resp.get("verification") or {}
        out["residual_pu"] = v.get("power_balance_residual_pu")
        out["residual_tol"] = v.get("power_balance_tolerance")
        out["passed"] = v.get("passed")
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# 4) 比对
# --------------------------------------------------------------------------
def compare(ref: dict[str, Any], got: dict[str, Any]) -> list[dict[str, Any]]:
    """逐项比对，每条给出三态判定之一：`一致` / `**超差**` / `不可判定`。

    ⚠️ 判据一律**按量的性质选形式**（见 `_judge_*`）：离散量用精确相等、可比量用
    绝对差、残差这类绝对量用"各自是否低于容差"。不要用同一套判据套所有量。
    """
    rows: list[dict[str, Any]] = []

    def add(name: str, a: Any, b: Any, verdict: str, detail: str = "") -> None:
        rows.append({"check": name, "ref": a, "got": b, "verdict": verdict, "detail": detail})

    # 元件计数：离散量，必须精确一致
    rk = {k: v for k, v in (ref.get("counts") or {}).items() if v}
    gk = {k: v for k, v in (got.get("counts") or {}).items() if v}
    if not rk or not gk:
        add("计数", rk or None, gk or None, V_UNKNOWN, "取不到元件计数")
    for k in sorted(set(rk) | set(gk)):
        v, d = _judge_counts(rk.get(k), gk.get(k))
        add(f"计数·{k}", rk.get(k), gk.get(k), v, d)

    # 装机容量：离散量（同值即一致）
    v, d = _judge_counts(ref.get("total_installed_load_kw"), got.get("total_installed_load_kw"))
    add("装机负荷 kW", ref.get("total_installed_load_kw"), got.get("total_installed_load_kw"), v, d)

    # 电压：可比量，绝对差
    for key, label in (("vmin_pu", "最低电压 pu"), ("vmax_pu", "最高电压 pu")):
        v, d = _judge_gauge(ref.get(key), got.get(key), V_TOL_OK)
        add(label, ref.get(key), got.get(key), v, d)

    # 网损：可比量，用相对偏差（量级大，绝对差不合适）
    a, b = ref.get("total_loss_kw"), got.get("total_loss_kw")
    if a is None or b is None:
        add("总网损 kW", a, b, V_UNKNOWN, "缺值")
    else:
        rel = abs(float(b) - float(a)) / abs(float(a)) if abs(float(a)) > 1e-9 else 0.0
        add("总网损 kW", a, b, V_OK if rel <= LOSS_TOL_WARN else V_BAD,
            f"相对偏差 {rel * 100:.4f}%")

    # 状态与残差
    add("收敛", ref.get("converged"), got.get("converged"),
        *_judge_counts(ref.get("converged"), got.get("converged")))
    add("验证通过", ref.get("passed"), got.get("passed"),
        *_judge_counts(ref.get("passed"), got.get("passed")))
    tol = max(ref.get("residual_tol") or 1e-3, got.get("residual_tol") or 1e-3)
    v, d = _judge_residual(ref.get("residual_pu"), got.get("residual_pu"), tol)
    add("功率平衡残差 pu", ref.get("residual_pu"), got.get("residual_pu"), v, d)
    return rows


def _bad_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """既不是"一致"的条目（超差 或 不可判定）——两者都不允许出"保真"结论。"""
    return [r for r in rows if r["verdict"] != V_OK]


# --------------------------------------------------------------------------
# 5) 主流程
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="格式转换保真度实测（对照物=公开标准算例）")
    ap.add_argument("--out", default="", help="报告输出目录（默认临时目录）")
    ap.add_argument("--keep", action="store_true", help="保留中间产物便于人工核对")
    args = ap.parse_args(argv)

    print("=" * 88)
    print("格式转换保真度实测 —— 对照物：公开标准算例 IEEE 30 Bus 的 132 kV 子网")
    print("=" * 88)

    # ---- 第 0 步：判据自检（不通过就**拒绝运行**，不出任何结论）----
    # 这一步是本次最重要的改动：教训写进注释拦不住人，只有可执行的前置关卡才行。
    st_failures = _self_test()
    if st_failures:
        print("✗ 判据自检未通过 —— 拒绝出结论（工具本身不可信时，它的输出一律无效）。")
        for f in st_failures:
            print(f"    · {f}")
        print("=" * 88)
        return 3
    print("判据自检 : 通过（6 项构造样例：计数差/电压超差/缺值/残差容差内/残差超容差/完全相同）")
    print("-" * 88)

    case_dir = official_case_dir()
    net = extract_subnet(case_dir)
    print(f"算例来源 : {case_dir}")
    print(f"子网规模 : {len(net.buses)} 母线 / {len(net.lines)} 线路 / {len(net.loads)} 负荷")
    print(f"母线     : {', '.join(net.buses)}")
    print(f"自证通过 : 子网闭合（无跨边界线路）且从 {SLACK_BUS} 全连通")
    print("-" * 88)

    work = tempfile.mkdtemp(prefix="conv_fidelity_")
    src = os.path.join(work, "src")
    os.makedirs(src, exist_ok=True)

    write_reference_dss(net, os.path.join(src, "ref", "Master.dss"))
    write_xlsx(net, os.path.join(src, "tables.xlsx"))
    write_mixed_csv(net, os.path.join(src, "tables_mixed.csv"))
    write_json(net, os.path.join(src, "tables.json"))
    write_matpower_m(net, os.path.join(src, "tables.m"))
    bundle_dir = write_bundle(net, os.path.join(src, "bundle"))

    from dpf_agent.web import case_converters as cv

    variants: list[tuple[str, str]] = [("参照(原生 .dss)", os.path.join(src, "ref", "Master.dss"))]
    for label, conv, path in (
        (".xlsx", cv.convert_xlsx, os.path.join(src, "tables.xlsx")),
        (".csv(混合)", cv.convert_csv, os.path.join(src, "tables_mixed.csv")),
        (".csv(目录)", cv.convert_csv, bundle_dir),
        (".json", cv.convert_json, os.path.join(src, "tables.json")),
        (".m(MATPOWER)", cv.convert_matpower_m, os.path.join(src, "tables.m")),
    ):
        out_dir = os.path.join(work, "converted", label.replace(".", "").replace("(", "_").replace(")", ""))
        os.makedirs(out_dir, exist_ok=True)
        try:
            variants.append((f"{label} 转换产物", conv(path, out_dir)))
        except Exception as exc:
            print(f"[转换失败] {label}: {type(exc).__name__}: {exc}")

    results: dict[str, dict[str, Any]] = {}
    for label, master in variants:
        print(f"[求解] {label} …", end="", flush=True)
        try:
            results[label] = solve_case(master, os.path.join(work, "run"))
            r = results[label]
            print(f" {r['status']}  Vmin={r.get('vmin_pu')}  网损={r.get('total_loss_kw')}  {r['duration_s']}s")
        except Exception as exc:
            results[label] = {"status": f"异常:{type(exc).__name__}", "error": str(exc)[:200]}
            print(f" 异常 {type(exc).__name__}: {exc}")
    print("-" * 88)

    ref_label = variants[0][0]
    ref = results[ref_label]

    # ⚠️ **假绿防线**：参照物自己都没跑成时，整轮结论是"不可判定"，不是"一致"。
    #    （本工具第一版就踩了这个坑：五种变体全部因异常没跑起来，比较结果全是
    #     "缺值"，而缺值当时不触发失败判定 → 打出了"逐项一致"。缺值必须判无效。）
    #
    #    注意：这里**不提前 return** —— 即使不可判定，也要把中间产物与明细落盘，
    #    否则排查时手里什么都没有（本工具第一版正是因为提前 return，
    #    `--keep` 打了空，连出错现场都拿不到）。
    ref_invalid = ref.get("status") != "success" or ref.get("vmin_pu") is None
    if ref_invalid:
        print(f"⚠ 参照物未成功（status={ref.get('status')}），本轮**不可判定**，不出保真结论。")
        print(f"  参照物详情：{json.dumps(ref, ensure_ascii=False)[:600]}")
        print("  （仍会写出明细与中间产物，便于排查）")
        print("-" * 88)

    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "case_source": case_dir,
        "subnet": net.as_dict(),
        "reference": ref_label,
        "verdict": "invalid" if ref_invalid else "compared",
        "results": results,
        "comparisons": {},
    }
    all_ok = True
    any_invalid = False
    for label, _ in variants[1:]:
        if results[label].get("status") != "success":
            any_invalid = True
        rows = compare(ref, results[label])
        report["comparisons"][label] = rows
        if _bad_rows(rows):
            all_ok = False

    # ---- 控制台表格 ----
    print(f"对照基准：{ref_label}  (Vmin={ref.get('vmin_pu')} pu, 网损={ref.get('total_loss_kw')} kW)")
    print()
    head = f"{'格式':<16}{'状态':<12}{'Vmin(pu)':>12}{'ΔV':>10}{'网损(kW)':>14}{'网损偏差':>10}  判定"
    print(head)
    print("-" * len(head))
    for label, _ in variants[1:]:
        r = results[label]
        rows = {x["check"]: x for x in report["comparisons"][label]}
        dv = rows.get("最低电压 pu", {}).get("detail", "")
        dl = rows.get("总网损 kW", {}).get("detail", "")
        bad = _bad_rows(report["comparisons"][label])
        verdict = "一致 ✓" if not bad else f"有差异({len(bad)})"
        print(f"{label:<16}{str(r.get('status')):<12}{str(r.get('vmin_pu')):>12}{dv:>10}"
              f"{str(r.get('total_loss_kw')):>14}{dl:>10}  {verdict}")
    print()
    if ref_invalid:
        print("结论：**参照物未成功 ⇒ 本轮不可判定**（未验证任何东西；明细已落盘待排查）。")
    elif any_invalid:
        print("结论：**存在未能完成求解的变体，不能判定保真**（见上表状态列）。")
    elif all_ok:
        print(f"结论：全部 {len(variants) - 1} 种编码经转换—求解与原生态逐项一致（差异仅在容差内）。")
    else:
        print("结论：存在非『一致』条目，逐条明细如下 ——")
        for label, rows in report["comparisons"].items():
            for x in _bad_rows(rows):
                print(f"  [{label}] {x['check']}：{x['verdict']} "
                      f"ref={x['ref']} got={x['got']} {x['detail']}")

    out_dir = args.out or work
    os.makedirs(out_dir, exist_ok=True)
    jpath = os.path.join(out_dir, "conversion_fidelity.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print()
    print(f"明细 JSON：{jpath}")
    if args.keep:
        print(f"中间产物：{work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    print("=" * 88)
    if ref_invalid:
        return 2
    return 0 if (all_ok and not any_invalid) else 1


if __name__ == "__main__":
    raise SystemExit(main())
