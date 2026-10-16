# -*- coding: utf-8 -*-
"""CLI 通道适配器（`engines/opendss_cli_adapter.py`）：解析 / 口径 / 契约。

## 分组

* **A 解析** —— `export` 产物 → 结构。全部是纯函数，用合成文本覆盖边界。
* **B 口径** —— 按元件名前缀求和，且**必须复用** `opendss_adapter` 那张表。
  这一组是"两条通道口径由构造保证一致"的直接证据。
* **C 契约** —— 接口形态的硬约束（下面每一条都对应一个曾踩过的坑）。
* **D 引擎无关** —— 真 CLI 未安装时也能全绿（本套件不启动任何进程）。
* **E 超时预算** —— 慢算例必须由 CLI 强杀，而不是先把后端重启了（见下）。

真引擎端到端（含与 COM 产物逐字段对照）需要另做原生引擎验收，
不属于本套件的离线覆盖范围。
"""

from __future__ import annotations

import inspect
import io
import os
import re
import sys
import tempfile

_PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from dpf_agent.engines import opendss_cli_adapter as M  # noqa: E402
from dpf_agent.engines.opendss_adapter import (  # noqa: E402
    ELEMENT_ROLE_BY_PREFIX,
    OpenDSSAdapter,
    classify_element_by_prefix,
)

_PASS = 0
_FAIL = 0
_LINES: list[str] = []


def _p(msg: str = "") -> None:
    _LINES.append(msg)
    print(msg)


def check(tag: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        _p("  [PASS] %s%s" % (tag, ("  " + detail) if detail else ""))
    else:
        _FAIL += 1
        _p("  [FAIL] %s  <== %s" % (tag, detail))


SUMMARY_HEAD = (
    "DateTime, CaseName, Status, Mode, Number, LoadMult, NumDevices, NumBuses, NumNodes, "
    "Iterations, ControlMode, ControlIterations, MostIterationsDone, Year, Hour, "
    "MaxPuVoltage, MinPuVoltage, TotalMW, TotalMvar, MWLosses, pctLosses, MvarLosses, Frequency\n"
)
SUMMARY_ROW = (
    '"2026/9/16 13:00:00",QP,SOLVED,Snap,100,   1.000, 11, 4, 12, 2, OFF, 1, 2, 0, 0, '
    '1.0085, 0.97961, 0.810974, -0.0668538, 0.00797411, 0.9833, 0.0172079, 50\n'
)
VOLT_CSV = (
    "Bus, BasekV, Node1, Magnitude1, Angle1, pu1, Node2, Magnitude2, Angle2, pu2, "
    "Node3, Magnitude3, Angle3, pu3\n"
    '"SOURCEBUS", 12.47, 1,    7191.84,   -0.2,   0.99893, 2,    7202.15, -120.2,    1.0004, '
    "3,     7203.1,  119.7,    1.0005\n"
    '"BUS2", 4.16, 1,    2392.94,  -31.0,   0.99632, 2,    2400.78, -150.5,   0.99958, '
    "3,    2404.48,   89.4,    1.0011\n"
)
POWERS_CSV = (
    "Element, Terminal, P(kW), Q(kvar), P_Normal, Q_Normal, P_Emergency, Q_Emergency\n"
    '"Load.LD1"        ,   1,      1000.0,        300.0,         0.0,         0.0,         0.0,         0.0\n'
    '"Load.LD1"        ,   2,         0.0,          0.0\n'
    '"PVSystem.PV_A"   ,   1,      -500.0,          0.0\n'
    '"Storage.ST1"     ,   1,       120.0,         10.0\n'
    '"Line.L1"         ,   1,       500.2,        -12.3\n'
    '"Vsource.SOURCE"  ,   1,      -500.2,         12.3\n'
    '"Transformer.T1"  ,   1,        10.0,          1.0\n'
    '"Capacitor.C1"    ,   1,         0.0,       -300.0\n'
)
LOADS_CSV = (
    "Load, Connected KVA, Allocation Factor, Phases, kW, kvar, PF, Model\n"
    "LD1,      0.0, 0.500, 3,   1500.0,    450.0, 0.958, 1\n"
    "LD2,      0.0, 0.500, 1,    200.0,     60.0, 0.958, 1\n"
)
SHOW_BUSES_TXT = (
    "    \n"
    "BUSES AND NODES IN ACTIVE CIRCUIT: qp\n"
    "    \n"
    "                                    Coord                                 Number of     Nodes\n"
    "  Bus          Base kV             (x, y)                      Keep?       Nodes        connected ...\n"
    "    \n"
    '"sourcebus"  12.470          (           NA,            NA )     No           3          1    2    3 \n'
    '"bus2"        4.160          (           NA,            NA )     No           3          1    2    3 \n'
)


def main() -> int:
    _p("=" * 78)
    _p("CLI 通道适配器：解析 / 口径 / 契约")
    _p("=" * 78)

    # ------------------------------------------------------------------ A 解析
    _p()
    _p("-" * 78)
    _p("A. export 产物解析（纯函数）")
    _p("-" * 78)
    s = M.parse_summary(SUMMARY_HEAD + SUMMARY_ROW)
    check("A1 summary 按列名取值（不依赖列顺序）",
          s.get("Status") == "SOLVED" and s.get("NumBuses") == "4", str(s)[:100])
    check("A2 summary 数值列可读", abs(float(s.get("TotalMW", 0)) - 0.810974) < 1e-9)
    check("A3 summary 缺失/空文本返回空字典而不是抛错",
          M.parse_summary(None) == {} and M.parse_summary("") == {}
          and M.parse_summary("only_a_header\n") == {})

    # ---- A3b~A3d 回归：多行 summary 必须取**最后一行** -------------------
    # 2026-09-17 实测缺陷：`export summary` 对同名产物是追加语义，而本通道
    # 「编译批」（compile + summary + loads + show buses）与「求解批」（solve + 全部 export）
    # **共用同一个工作目录**、各导一次 → 文件里有两行。
    # 第 1 行是 compile 后尚未 solve 的状态，第 2 行才是真解。
    # 取首行的后果：源侧供电与网损双双为 0 → 平衡残差 1.000000 pu →
    # 任务被判 validation_failed（实测 ieee33 连跑 6 个任务全失败，
    # 而同一任务的母线电压表明明是正确解）。
    _H = ("DateTime, CaseName, Status, Mode, Number, LoadMult, NumDevices, NumBuses, NumNodes, "
          "Iterations, ControlMode, ControlIterations, MostIterationsDone, Year, Hour, "
          "MaxPuVoltage, MinPuVoltage, TotalMW, TotalMvar, MWLosses, pctLosses, MvarLosses, Frequency\n")
    _UNSOLVED = (
        '"2026/9/17 21:30:36",IEEE33,SOLVED,Snap,100,   1.000, 65, 33, 99, 0, OFF, 0, 0, 0, 0, '
        '1, 1, 2.197E-009, -0.000430822, 1.82596E-009, 83.11, -0.00547834, 50\n'
    )
    _SOLVED = (
        '"2026/9/17 21:30:39",IEEE33,SOLVED,Snap,100,   1.000, 65, 33, 99, 2, OFF, 1, 2, 0, 0, '
        '0.99679, 0.91465, 3.80117, 2.34322, 0.168614, 4.436, 0.106458, 50\n'
    )
    two = M.parse_summary(_H + _UNSOLVED + _SOLVED)
    check("A3b 两行 summary 取**最后一行**（真解），不是未求解那行",
          two.get("Iterations") == "2" and abs(float(two.get("TotalMW", 0)) - 3.80117) < 1e-9
          and abs(float(two.get("MWLosses", 0)) - 0.168614) < 1e-9,
          "Iterations=%s TotalMW=%s MWLosses=%s MinPuVoltage=%s"
          % (two.get("Iterations"), two.get("TotalMW"), two.get("MWLosses"), two.get("MinPuVoltage")))
    check("A3c 单行文本行为不变（静态批场景本来就只有一行）",
          M.parse_summary(_H + _SOLVED).get("Iterations") == "2")
    check("A3d 是「取最后一行」而非「取最优值」—— 顺序颠倒时结论随之改变",
          M.parse_summary(_H + _SOLVED + _UNSOLVED).get("Iterations") == "0",
          "颠倒后 Iterations=%s（应为 0，证明按位置取而非按数值挑）"
          % M.parse_summary(_H + _SOLVED + _UNSOLVED).get("Iterations"))

    v = M.parse_voltages(VOLT_CSV)
    check("A4 母线数与基准电压正确", len(v) == 2 and v[0]["base_kv"] == 12.47, str(v[0])[:80])
    check("A5 分相幅值/相角都取到（节点数不固定）",
          v[0]["voltage_pu"] == [0.99893, 1.0004, 1.0005] and len(v[0]["angle_deg"]) == 3,
          str(v[0]["voltage_pu"]))
    check("A6 电压解析对空文本安全", M.parse_voltages("") == [])

    p = M.parse_powers(POWERS_CSV)
    check("A7 每元件多端子被归并（同一元件不产生多行记录）",
          p["Load.LD1"][1][0] == 1000.0 and 2 in p["Load.LD1"], str(p["Load.LD1"]))
    check("A8 元件名保留原样（含大小写与类前缀）", "PVSystem.PV_A" in p, str(list(p)))
    check("A9 powers 解析对空文本安全", M.parse_powers("") == {})

    ld = M.parse_loads(LOADS_CSV)
    check("A10 export loads 的额定 kW 与相数可读",
          ld[0]["phases"] == 3 and ld[0]["kw"] == 1500.0 and ld[1]["phases"] == 1, str(ld))

    sb = M.parse_show_buses(SHOW_BUSES_TXT)
    check("A11 show buses 是固定宽度文本（不是 CSV），表头行被跳过",
          len(sb) == 2 and sb[0]["name"] == "sourcebus" and sb[0]["kv_base"] == 12.47,
          str(sb))
    check("A12 show buses 解析对空文本安全", M.parse_show_buses(None) == [])

    cap = M.parse_keyed_table(
        "Name, Imax, %normal, %emergency, kW, kvar, NumCustomers, TotalCustomers,"
        " NumPhases, kVBase\n"
        "Line.L1,    169.628,    42.41,    28.27,    809.461,   -74.4244, 0, 0, 3, 2.4 \n",
        "Name",
    )
    check("A13 通用表解析（capacity / currents）可用",
          "Line.L1" in cap and abs(float(cap["Line.L1"]["Imax"]) - 169.628) < 1e-9,
          str(cap)[:100])

    # ------------------------------------------------------------------ B 口径
    _p()
    _p("-" * 78)
    _p("B. 口径求和（必须复用 opendss_adapter 的口径表）")
    _p("-" * 78)
    sums = M.element_power_sums(p)
    _p("  求和结果：%s" % {k: sums[k] for k in sorted(sums)})
    check("B1 负荷 → load_kw（消耗为正）", abs(sums["load_kw"] - 1000.0) < 1e-9)
    check("B2 PVSystem → gen_kw 且**取反**（注入为正）", abs(sums["gen_kw"] - 500.0) < 1e-9)
    check("B3 储能 → storage_kw 且**不取反**（符号本身是语义）",
          abs(sums["storage_kw"] - 120.0) < 1e-9, str(sums["storage_kw"]))
    check("B4 三类元件数按元件名去重",
          sums["load_elements"] == 1 and sums["gen_elements"] == 1
          and sums["storage_elements"] == 1,
          "load=%d gen=%d storage=%d" % (sums["load_elements"], sums["gen_elements"],
                                         sums["storage_elements"]))
    check("B5 口径外元件不参与求和（Line/Transformer/Capacitor/Vsource）",
          abs(sums["load_kw"] + sums["gen_kw"] + sums["storage_kw"] - 1620.0) < 1e-9,
          "合计 %s" % (sums["load_kw"] + sums["gen_kw"] + sums["storage_kw"]))
    check("B6 未登记前缀一律跳过（不猜边）",
          M.element_power_sums({"Fancy.NewThing": {1: (999.0, 0.0)}}).get("load_kw") == 0.0)
    check("B7 缺端子 1 的元件计入 skipped 而不是当成 0",
          M.element_power_sums({"Load.X": {2: (1.0, 1.0)}})["load_elements_skipped"] == 1)
    check("B8 读数缺失（None）也计入 skipped",
          M.element_power_sums({"Load.X": {1: (None, None)}})["load_elements_skipped"] == 1)

    # 与口径表的**一致性**断言：凡是表里登记的前缀，求和都要认
    known = set(ELEMENT_ROLE_BY_PREFIX)
    probe = {f"{k.upper()}.PROBE": {1: (1.0, 1.0)} for k in known}
    got = M.element_power_sums(probe)
    counted = {k for k in known if got.get(
        {"load": "load_elements", "generation": "gen_elements",
         "bidirectional": "storage_elements"}[classify_element_by_prefix(k)]) >= 1}
    check("B9 口径表里登记的每个前缀都被求和覆盖（口径表与实现不脱节）",
          counted == known, "已覆盖 %s / 登记 %s" % (sorted(counted), sorted(known)))

    check("B10 前缀计数按类名前缀归并",
          M.count_by_prefix(["Line.L1", "Line.L2", "Load.LD1"]) == {"line": 2, "load": 1})

    # ------------------------------------------------------------------ C 契约
    _p()
    _p("-" * 78)
    _p("C. 契约：接口形态的硬约束（每条都对应一个踩过的坑）")
    _p("-" * 78)
    adapter = M.OpenDSSCliAdapter()
    methods = {m for m in dir(adapter) if not m.startswith("_") and callable(getattr(adapter, m))}
    need = {"health", "compile_case", "circuit_summary", "collect_circuit_inventory",
            "apply_solver_profile", "solve", "extract_raw_result", "close"}
    check("C1 实现主链路的全部 8 个方法", need <= methods, str(sorted(need - methods)))

    check("C2 ⚠️ **不提供 run_with_engine**（看门狗据此判「无法判定」而非误重启）",
          not hasattr(adapter, "run_with_engine"),
          "若提供抛异常的版本，复检会把异常读成「引擎无响应」→ 假阳性重启")
    check("C3 不继承 OpenDSSAdapter（CLI 没有可长期持有的引擎对象）",
          OpenDSSAdapter not in type(adapter).__mro__,
          str([c.__name__ for c in type(adapter).__mro__]))
    check("C4 构造不触碰引擎（纯字段初始化，实例化即安全）",
          adapter.health.__doc__ is not None and adapter._last_health is None)

    src = io.open(os.path.join(_PROJ, "dpf_agent", "engines",
                              "opendss_cli_adapter.py"), encoding="utf-8").read()
    check("C5 口径表**不为本通道再定义一份**（只 import 复用）",
          "ELEMENT_ROLE_BY_PREFIX" not in src.replace(
              "from .opendss_adapter import", "")
          and "classify_element_by_prefix" in src,
          "口径单点定义在 opendss_adapter，两条通道共用")
    check("C6 运行时**不做删除**（os.remove 不存在）",
          "os.remove(" not in src, "CLI 通道零删除，产物随任务工作目录管理")
    check("C7 产物收集必须带上镜像目录（产物落算例目录，不是 workdir）",
          "artifact_dirs" in src and "os.path.dirname(shadow)" in src,
          "实测 stdout 里直接打出产物路径 …\\case\\*_EXP_Summary.CSV")
    check("C8 端点母线来自源文件（export 产物里没有任何母线列）",
          "line_endpoints" in src and "DssSourceFacts" in src)
    check("C9 只在偏离默认值时才下发 set（默认路径零风险）",
          "_DEFAULT_MAX_ITERATIONS" in src and "_DEFAULT_TOLERANCE" in src)
    check("C10 判定引擎失败时不看 rc（rc 不可靠，判产物）",
          "不可靠" in src or "rc" not in src.split("_raise_if_compile_failed")[-1][:400],
          "实测 rc=24700 时产物全部有效")

    sig = inspect.signature(M.OpenDSSCliAdapter.compile_case)
    check("C11 compile_case 的签名与其它通道一致（case_workspace, master_dss）",
          list(sig.parameters) == ["self", "case_workspace", "master_dss"], str(sig))
    check("C12 工作目录建在 case_workspace **之外**（否则镜像源目录时会递归）",
          "os.path.dirname(os.path.abspath(case_workspace))" in src,
          "实测：建在里面 → os.walk 遍历 source 时把自己复制进去 → 4247 层嵌套")

    # ------------------------------------------------------------------ D 引擎无关
    _p()
    _p("-" * 78)
    _p("D. 本套件不启动任何进程（引擎未安装时也应全绿）")
    _p("-" * 78)
    health = M.OpenDSSCliAdapter().health.__doc__ or ""
    check("D1 health 有说明（缓存语义）", "缓存" in health, health[:60])
    check("D2 模块不 import 引擎原生库（dss / win32com 都不出现）",
          "import dss" not in src and "win32com" not in src)

    # ------------------------------------------------------------------ E 超时预算
    _p()
    _p("-" * 78)
    _p("E. 超时预算：算例变慢时要由 CLI 强杀，而不是先把整个后端重启了")
    _p("-" * 78)

    def _env_default(rel_py: str, env_key: str):
        """从源码里读某个环境变量的**设计**默认值（不读运行值，不受本机环境干扰）。"""
        text = io.open(os.path.join(_PROJ, rel_py), encoding="utf-8").read()
        m = re.search(r'"%s"\s*,\s*"([0-9.]+)"' % re.escape(env_key), text)
        return float(m.group(1)) if m else None

    quick = _env_default(os.path.join("dpf_agent", "config.py"), "DPF_CLI_QUICK_TIMEOUT")
    solve = _env_default(os.path.join("dpf_agent", "config.py"), "DPF_CLI_TIMEOUT")
    stuck = _env_default(
        os.path.join("dpf_agent", "runtime", "watchdog.py"),
        "DPF_WATCHDOG_TASK_STUCK_S",
    )
    _p("    设计默认值：cli_quick=%s  cli_solve=%s  看门狗任务锁兜底=%s"
       % (quick, solve, stuck))
    check("E1 三个默认值都能从源码读到（否则下面的断言是空转的）",
          None not in (quick, solve, stuck), "%s / %s / %s" % (quick, solve, stuck))

    #: 留给 CLI 之外的阶段（健康检查 / 校验 / 编排 / 提取 / 画像 / 渲染）的余量。
    #: 实测正常任务端到端 43s，其中 CLI 只占一部分，90s 是宽裕的。
    MIN_MARGIN_S = 90.0
    if None not in (quick, solve, stuck):
        check("E2 快慢分离：静态阶段超时 < 求解阶段超时",
              quick < solve, "quick=%s solve=%s" % (quick, solve))
        check("E3 ★ 预算关系：quick + solve + 余量 ≤ 看门狗兜底阈值",
              quick + solve + MIN_MARGIN_S <= stuck,
              "%.0f+%.0f+%.0f=%.0f vs 阈值 %.0f；曾经的缺陷 180+180=360>300，"
              "实测真的把后端重启了"
              % (quick, solve, MIN_MARGIN_S,
                 quick + solve + MIN_MARGIN_S, stuck))
        check("E4 单个阶段的超时本身也不能超过兜底阈值",
              solve < stuck, "solve=%s stuck=%s" % (solve, stuck))

    dflt = M.OpenDSSCliAdapter()
    check("E5 适配器默认值 = config 设计默认值（两处不许漂移）",
          dflt.cli_quick_timeout_s == quick and dflt.cli_timeout_s == solve,
          "适配器 quick=%s solve=%s" % (dflt.cli_quick_timeout_s, dflt.cli_timeout_s))
    if None not in (quick, solve, stuck):
        check("E6 适配器默认值自身也满足预算关系",
              dflt.cli_quick_timeout_s + dflt.cli_timeout_s + MIN_MARGIN_S <= stuck,
              "%.0f ≤ %.0f" % (dflt.cli_quick_timeout_s + dflt.cli_timeout_s
                               + MIN_MARGIN_S, stuck))

    cc_src = inspect.getsource(M.OpenDSSCliAdapter.compile_case)
    solve_src = inspect.getsource(M.OpenDSSCliAdapter.solve)
    check("E7 静态阶段显式用 quick 超时",
          "timeout_s=self.cli_quick_timeout_s" in cc_src,
          "否则 compile 会占用求解级长超时，异常分支的代价被放大")
    check("E8 solve 用主超时（不传覆盖值）",
          "self._run(cmds, sess.workdir," in solve_src and "timeout_s=" not in solve_src,
          "求解是唯一值得等长超时的阶段")
    check("E9 两个阶段的超时消息可区分（用户能判断卡在哪一步）",
          "编译/结构扫描超时" in cc_src and "求解超时" in solve_src)

    _p()
    _p("=" * 78)
    _p("结果：通过 %d / 失败 %d" % (_PASS, _FAIL))
    _p("=" * 78)
    try:
        io.open(os.path.join(tempfile.gettempdir(), "test_cli_adapter.txt"), "w",
                encoding="utf-8").write("\n".join(_LINES) + "\n")
    except OSError:
        pass
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
