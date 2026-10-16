# -*- coding: utf-8 -*-
"""OpenDSS CLI 适配器（第三条通道）：把 `export` 产物解析成与 COM 通道等价的接口。

## 定位（重要）

**默认通道仍是 `com`**。本适配器是**增量新增**的第三条通道，用途有三：

1. **离线可验证** —— 命令行就能起真引擎，不必依赖"应用正在运行"这个前提。
   此前所有数值层面的验证都必须借运行中的应用（Step 0 / 0.5 都是这么绕的）。
2. **第二把尺子** —— 独立数据通路，能校验我们的 CSV/属性解析与**口径定义**。
   ⚠️ 注意边界：三个前端底下是**同一份引擎代码**（同为 EPRI OpenDSS 11.0.0.1），
   所以它**不能**验证"引擎算错了"，只能验证"我们读错了 / 归类错了"。
3. **进程隔离** —— `OpenDSScmd.exe` 本身就是独立进程，挂死可强杀。

## 与另两条通道的接口差异（刻意为之）

* **不继承 `OpenDSSAdapter`**，也**不提供 `run_with_engine()`**。
  CLI 是独立进程，执行不了任意 Python 闭包（这正是 A-1 要把画像拆成
  「观察量 + 纯数据组装」的原因）。
  ⚠️ 这条还带来一个**正确性**上的好处：看门狗的复检靠
  `hasattr(adapter, "run_with_engine")` 判断能否复检，不能就按**三态里的
  `None`（无法判定）**处理、**绝不重启**。若这里硬凑一个抛异常的
  `run_with_engine`，复检会把异常误读成"引擎无响应" → **假阳性重启**。

* **批处理模型**：CLI 无法像 COM 那样分步交互（`compile` → `set` → `solve` →
  逐个读属性），因为它是"喂一段命令流 → 退出"。所以：
  - `compile_case()` 跑一次**静态批处理**（compile + summary + loads + show buses），
    用于校验与画像；
  - `solve()` 跑一次**完整批处理**（含 solve 与全部 export），结果缓存供
    `extract_raw_result()` 使用；
  - **每次 `solve()` 都真的重跑**（不缓存复用）—— 这样"复现性检查"
    （同配置二次求解比对）是真的在跑，而不是拿缓存冒充。

## 口径复用

元件"算负荷还是算发电"**不在这里定义** —— 直接用 `opendss_adapter` 的口径表
（`classify_element_by_prefix`）。CLI 只是把 `export powers` 的
`Element` 名字前缀喂进同一张表，所以两条通道的口径**由构造保证一致**。
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .. import ADAPTER_VERSION
from ..schemas.enums import ErrorCode, ResultScope
from ..schemas.solver import OpenDSSSolverProfile
from .circuit_inventory import CircuitInventory
from .circuit_validation import (
    CircuitObservation,
    normalize_bus_name,
    validate_compiled_circuit,
)
from .cli_runner import (
    CliError,
    CliRunResult,
    build_export_commands,
    discover_cli_path,
    mirror_case,
    parse_banner_version,
    run_cli_commands,
)
from .dss_source import DssSourceFacts, parse_dss_directory
from .opendss_adapter import (
    AdapterError,
    CircuitHandle,
    EngineHealth,
    OpenDSSRawResult,
    SolverExecutionResult,
    classify_element_by_prefix,
)

#: 角色 → (输出前缀, 是否取反)。与 `opendss_adapter._element_power_sums` 内
#: 同名映射**语义必须一致**：generation 取反（注入为正）；bidirectional 不取反
#: （符号本身就是语义：>0 充电、<0 放电）。
_ROLE_OUTPUT: dict[str, tuple[str, bool]] = {
    "load": ("load", False),
    "generation": ("gen", True),
    "bidirectional": ("storage", False),
}

#: OpenDSS 的默认求解参数。**与默认值相同就不下发 `set` 命令** ——
#: 实测（Step 0.5）某些求解设置组合会让 CLI 显著变慢甚至挂死，
#: 而最常见的路径（默认 profile）根本不需要设它们。
_DEFAULT_MAX_ITERATIONS = 15
_DEFAULT_TOLERANCE = 1e-4


# =========================================================================== CSV 解析
#
# 全部是**纯函数**（输入文本、输出结构），因此可以离线测试 —— 这也是本模块
# 能被单测覆盖的主要部分。真实引擎调用需要另行运行本机集成验收脚本，
# 用真 CLI 验证。


def _split_csv_line(line: str) -> list[str]:
    """按逗号切分并剥掉引号与空白。

    不做完整 CSV 状态机：OpenDSS 的 export 产物里没有含逗号的字段
    （元件名、母线名都不含），且所有行都是单行记录。
    """
    out: list[str] = []
    for piece in line.split(","):
        p = piece.strip()
        if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
            p = p[1:-1].strip()
        out.append(p)
    return out


def table(text: str | None) -> tuple[list[str], list[list[str]]]:
    """把产物文本拆成 (表头, 数据行)。空文本返回 ([], [])。"""
    rows = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not rows:
        return [], []
    return _split_csv_line(rows[0]), [_split_csv_line(r) for r in rows[1:]]


def _num(value: str | None) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _col(header: list[str], *names: str) -> int | None:
    """按候选列名（大小写不敏感）找下标。"""
    lowered = [h.strip().lower() for h in header]
    for want in names:
        w = want.strip().lower()
        if w in lowered:
            return lowered.index(w)
    return None


def parse_summary(text: str | None) -> dict[str, str]:
    """`export summary` → 字典（保留原始字符串，取值时再转换）。

    关键列：`Status`（SOLVED / UnSolved）、`Iterations`、`NumBuses`、
    `NumDevices`、`TotalMW`、`MWLosses`、`TotalMvar`、`MvarLosses`、
    `MaxPuVoltage`、`MinPuVoltage`、`pctLosses`。

    ⚠️ **必须取最后一行**，不能取首行。原因（2026-09-17 实测）：
    `export summary` 对同名产物是**追加**语义，而本通道的「编译批」与「求解批」
    共用同一个工作目录、各导一次，于是文件里出现**两行**：

    | 行 | 状态 | Iterations | TotalMW | MinPuVoltage |
    |---|---|---|---|---|
    | 第 1 行 | compile 后尚未 solve | 0 | 2.197e-09 | 1.0 |
    | 第 2 行 | 求解后 | 2 | 3.80117 | 0.91465 |

    取第 1 行 → 「源侧供电」与「网损」双双为 0 → 平衡式
    `0 − 负荷 ≈ −负荷` → 残差 **1.000000 pu**（容差 1e-3）→
    任务被判 `validation_failed`。实测 ieee33 连跑 6 个任务全部失败，
    而母线电压表（另一份产物）明明是正确解（0.99679 → 0.91465）。

    算例若**自带 `Solve`**（如 `cases/pvsystem_probe/Master.dss`），
    compile 时就已解一次，第 1 行恰好是真解 —— 取首行才"侥幸"正确。
    **这正是该缺陷长期未被发现的原因**，不要因为"探针算例能过"而改回首行。
    """
    header, rows = table(text)
    if not header or not rows:
        return {}
    row = rows[-1]
    return {h.strip(): (row[i] if i < len(row) else "") for i, h in enumerate(header)}


def parse_voltages(text: str | None) -> list[dict[str, Any]]:
    """`export voltages` → 逐母线分相电压。

    列形如 `Bus, BasekV, Node1, Magnitude1, Angle1, pu1, Node2, ...`。
    **相数不固定**，按出现顺序收集。
    """
    header, rows = table(text)
    if not header:
        return []
    i_bus = _col(header, "Bus")
    i_base = _col(header, "BasekV")
    out: list[dict[str, Any]] = []
    for r in rows:
        if i_bus is None or i_bus >= len(r):
            continue
        pu: list[float] = []
        ang: list[float] = []
        nodes: list[int] = []
        node = 1
        while True:
            i_nd = _col(header, f"Node{node}")
            i_pu = _col(header, f"pu{node}")
            i_an = _col(header, f"Angle{node}")
            if i_nd is None and i_pu is None and i_an is None:
                break
            nd = _num(r[i_nd]) if (i_nd is not None and i_nd < len(r)) else None
            v = _num(r[i_pu]) if (i_pu is not None and i_pu < len(r)) else None
            a = _num(r[i_an]) if (i_an is not None and i_an < len(r)) else None
            # ⚠️ `Node{n}=0` 表示**该位置没有节点**（OpenDSS 对缺相母线用 0 占位；
            #    实测 IEEE13 的 645 是 `2,p,3,p,0,0,0,0`，只有 node2/3）。
            #    必须按**节点号**收集并跳过 0：否则"不存在的相"会被当成 0 V 的真实相，
            #    既让相别标签错位（第 2 个值被当成 B 相），又污染最低相电压与不平衡度
            #    （实测 684 因此算出 ε=196%）。相别身份不可猜 —— 项目硬约定六。
            if nd is not None and int(nd) >= 1 and v is not None:
                nodes.append(int(nd))
                pu.append(v)
                ang.append(a if a is not None else 0.0)
            node += 1
            if node > 12:  # 防御：列名异常时不死循环
                break
        out.append({
            "name": r[i_bus],
            "base_kv": _num(r[i_base]) if (i_base is not None and i_base < len(r)) else None,
            "nodes": nodes,
            "voltage_pu": pu,
            "angle_deg": ang,
        })
    return out


def parse_powers(text: str | None) -> dict[str, dict[int, tuple[float | None, float | None]]]:
    """`export powers` → {元件全名: {端子号: (P_kW, Q_kvar)}}。

    注意**每个元件会有多行**（每个端子一行），所以不能按行直接累加 ——
    求和必须先按元件归并，再只取端子 1（与 COM 侧 `terminal1` 的语义一致）。
    """
    header, rows = table(text)
    if not header:
        return {}
    i_el = _col(header, "Element")
    i_te = _col(header, "Terminal")
    i_p = _col(header, "P(kW)", "P (kW)", "PkW")
    i_q = _col(header, "Q(kvar)", "Q (kvar)", "Qkvar")
    if i_el is None:
        return {}
    out: dict[str, dict[int, tuple[float | None, float | None]]] = {}
    for r in rows:
        if i_el >= len(r):
            continue
        name = r[i_el].strip()
        if not name:
            continue
        t = 1
        if i_te is not None and i_te < len(r):
            tv = _num(r[i_te])
            t = int(tv) if tv is not None else 1
        p = _num(r[i_p]) if (i_p is not None and i_p < len(r)) else None
        q = _num(r[i_q]) if (i_q is not None and i_q < len(r)) else None
        out.setdefault(name, {})[t] = (p, q)
    return out


def parse_loads(text: str | None) -> list[dict[str, Any]]:
    """`export loads` → 逐负荷**额定**值与相数。

    ⚠️ 这里的 kW 是**额定值**，不是实际消耗。实测 case07：`loads` 合计 85276 kW
    而 `powers` 合计 75911 kW —— 差 11% 是因为节点电压低（Vminpu=0.95 时恒功率
    负荷自动转恒阻抗）。**画像用额定值，结果口径用 powers 的实际端子功率。**
    """
    header, rows = table(text)
    if not header:
        return []
    i_load = _col(header, "Load", "Name")
    i_kw = _col(header, "kW")
    i_kvar = _col(header, "kvar")
    i_ph = _col(header, "Phases")
    out: list[dict[str, Any]] = []
    for r in rows:
        if i_load is None or i_load >= len(r):
            continue
        out.append({
            "name": r[i_load],
            "kw": _num(r[i_kw]) if (i_kw is not None and i_kw < len(r)) else None,
            "kvar": _num(r[i_kvar]) if (i_kvar is not None and i_kvar < len(r)) else None,
            "phases": int(_num(r[i_ph])) if (
                i_ph is not None and i_ph < len(r) and _num(r[i_ph]) is not None) else None,
        })
    return out


def parse_keyed_table(text: str | None, key_col: str = "Name") -> dict[str, dict[str, str]]:
    """通用：把 `export capacity` / `export currents` 这类"首个列是名字"的表
    解析成 {名字: {列名: 值}}。"""
    header, rows = table(text)
    if not header:
        return {}
    i_key = _col(header, key_col, "Element")
    if i_key is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        if i_key >= len(r):
            continue
        row = {h.strip(): (r[i] if i < len(r) else "") for i, h in enumerate(header)}
        out[r[i_key].strip()] = row
    return out


def parse_show_buses(text: str | None) -> list[dict[str, Any]]:
    """解析 `show buses` 的文本产物（**不是 CSV**，是固定宽度表格）。

    形如：
        "sourcebus"  12.470          (   NA,   NA )     No   3   1 2 3
    `compile` 之后即可执行（不需要 solve），所以用于 `compile_case` 阶段的
    母线名与基准电压收集。
    """
    out: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("BUSES", "Bus ", "Coord", "Number")):
            continue
        if not line.startswith('"'):
            continue
        # 取第一段带引号的名字
        end = line.find('"', 1)
        if end < 0:
            continue
        name = line[1:end].strip()
        rest = line[end + 1:].strip()
        base = None
        piece = rest.split()[0] if rest.split() else ""
        base = _num(piece)
        out.append({"name": name, "kv_base": base})
    return out


# =========================================================================== 口径求和


def element_power_sums(powers: dict[str, dict[int, tuple[float | None, float | None]]]) -> dict[str, Any]:
    """按**元件名前缀**归类求和 —— 与 COM 通道的 `_element_power_sums` 同口径。

    口径表**不在这里**：`classify_element_by_prefix` 单点定义在
    `opendss_adapter`，本函数只是把前缀喂进去。这正是设计意图 ——
    换通道时口径不需要重新想一遍，也不会漂移。

    只取**端子 1**（与 COM 侧一致）；元件数按元件名去重（每个元件的多个端子
    只算一个元件）。
    """
    out: dict[str, Any] = {
        "load_kw": 0.0, "load_kvar": 0.0, "load_elements": 0, "load_elements_skipped": 0,
        "gen_kw": 0.0, "gen_kvar": 0.0, "gen_elements": 0, "gen_elements_skipped": 0,
        "storage_kw": 0.0, "storage_kvar": 0.0, "storage_elements": 0,
        "storage_elements_skipped": 0,
    }
    for name, terminals in powers.items():
        prefix = str(name).split(".", 1)[0].strip().lower()
        role = classify_element_by_prefix(prefix)
        if role is None:
            continue  # 口径外元件（Line / Transformer / Capacitor / Vsource …）
        spec = _ROLE_OUTPUT.get(role)
        if spec is None:
            continue
        pfx, invert = spec
        pq = terminals.get(1)
        if pq is None:
            out[f"{pfx}_elements_skipped"] += 1
            continue
        p, q = pq
        if p is None or q is None:
            out[f"{pfx}_elements_skipped"] += 1
            continue
        sign = -1.0 if invert else 1.0
        out[f"{pfx}_kw"] += sign * float(p)
        out[f"{pfx}_kvar"] += sign * float(q)
        out[f"{pfx}_elements"] += 1
    return out


def count_by_prefix(names: list[str]) -> dict[str, int]:
    """按元件名前缀计数（用于元件清单）。"""
    out: dict[str, int] = {}
    for n in names:
        p = str(n).split(".", 1)[0].strip().lower()
        if p:
            out[p] = out.get(p, 0) + 1
    return out


# =========================================================================== 会话状态


@dataclass
class _CliSession:
    """一次 `compile_case` 对应的全部现场（引擎无关，纯数据 + 路径）。"""

    workdir: str
    master_dss: str          # 调用方给的原始路径
    shadow_master: str       # 镜像到工作目录后的路径（产物落在这里）
    source_facts: DssSourceFacts
    #: compile_case 阶段的静态产物（不含 solve）
    static_run: CliRunResult | None = None
    #: solve 阶段的完整产物（每次 solve 都会刷新）
    full_run: CliRunResult | None = None
    #: 当前求解设置（来自 apply_solver_profile；CLI 是批处理，设置在这里攒着）
    max_iterations: int = _DEFAULT_MAX_ITERATIONS
    tolerance: float = _DEFAULT_TOLERANCE
    solve_count: int = 0

    @property
    def best_run(self) -> CliRunResult | None:
        """优先用完整产物（含 solve 的状态），退化到静态产物。"""
        return self.full_run or self.static_run


# =========================================================================== 适配器


class OpenDSSCliAdapter:
    """OpenDSS CLI 通道适配器（增量新增；默认通道仍是 com）。

    ⚠️ **刻意不实现 `run_with_engine`** —— 见模块文档。
    """

    def __init__(
        self,
        solve_timeout_s: float = 60.0,
        cli_timeout_s: float = 120.0,
        cli_quick_timeout_s: float = 45.0,
        cli_exe: str | None = None,
    ):
        self.solve_timeout_s = solve_timeout_s
        #: 求解阶段（`solve` + 全部 export）的单次硬超时
        self.cli_timeout_s = cli_timeout_s
        #: 静态阶段（`compile` + `show buses`，不求解）的单次硬超时。
        #: ⚠️ 与 `cli_timeout_s` 之和必须显著小于看门狗的任务锁兜底阈值（见 config.py）。
        self.cli_quick_timeout_s = cli_quick_timeout_s
        self.cli_exe = cli_exe
        self._last_health: EngineHealth | None = None
        self._health_runs = 0
        #: 已产生但**不删除**的工作目录（CLI 通道零删除；由任务收尾或人工回收）
        self._leftovers: list[str] = []

    # --------------------------------------------------------------- 内部工具
    def _run(
        self,
        cmds: list[str],
        workdir: str,
        artifact_dirs: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> CliRunResult:
        """跑一段命令流。

        ⚠️ `artifact_dirs` 必须带上**镜像算例所在目录** —— `export` 的产物落在
        「算例文件所在目录」（`compile` 会切换 OpenDSS 的数据目录），而不是
        CLI 进程的启动目录。实测：stdout 里会直接打出产物绝对路径
        `>>…\\case\\<算例名>_EXP_Summary.CSV`，而 `workdir` 根目录始终是空的。

        `timeout_s=None` 用求解阶段的长超时；静态阶段应显式传 `cli_quick_timeout_s`。
        """
        try:
            return run_cli_commands(
                cmds,
                workdir,
                exe=self.cli_exe,
                timeout_s=self.cli_timeout_s if timeout_s is None else timeout_s,
                artifact_dirs=artifact_dirs,
            )
        except CliError as exc:
            raise AdapterError(ErrorCode.DATA_UNAVAILABLE, str(exc)) from exc

    @staticmethod
    def _session(circuit: CircuitHandle) -> _CliSession:
        sess = getattr(circuit, "dss_ctx", None)
        if not isinstance(sess, _CliSession):
            raise AdapterError(ErrorCode.ENGINE_ERROR, "CLI 会话未初始化（请先 compile_case）")
        return sess

    def _static_commands(self, shadow: str) -> list[str]:
        # compile 之后即可拿到母线表与元件清单，**不需要 solve**。
        # 这样 compile_case 阶段不必猜求解设置（profile 那时还没冻结）。
        return (
            ["clear", f'compile "{shadow}"']
            + build_export_commands(["summary", "loads"])
            + ["show buses"]
        )

    def _full_commands(
        self, shadow: str, max_iterations: int, tolerance: float
    ) -> list[str]:
        cmds = ["clear", f'compile "{shadow}"']
        # ⚠️ 只在**偏离默认值**时才下发 set —— 实测某些设置组合会让 CLI 显著
        #    变慢甚至挂死（Step 0.5），而默认路径本就不需要设它们。
        if int(max_iterations) != _DEFAULT_MAX_ITERATIONS:
            cmds.append(f"set maxiterations={int(max_iterations)}")
        if float(tolerance) != _DEFAULT_TOLERANCE:
            cmds.append(f"set tolerance={float(tolerance):g}")
        # controlmode=off 是 V1.3 的冻结控制行为，与是否偏离默认值无关，必须下发。
        cmds.append("set controlmode=off")
        cmds.append("solve")
        cmds += build_export_commands(
            ["summary", "voltages", "powers", "loads", "capacity", "currents"]
        )
        return cmds

    # ------------------------------------------------------------------ health
    def health(self) -> EngineHealth:
        """起一次 CLI 读 banner 版本。**结果缓存**（一次进程生命周期内不会变）。"""
        if self._last_health is not None and self._last_health.available:
            return self._last_health
        exe, why = discover_cli_path()
        if not exe or (self.cli_exe and not os.path.isfile(self.cli_exe)):
            self._last_health = EngineHealth(
                available=False, detail=f"OpenDSS CLI 不可用：{why}"
            )
            return self._last_health
        try:
            import tempfile

            probe_dir = os.path.join(tempfile.gettempdir(), "tianshu_cli_health")
            res = self._run(["exit"], probe_dir)
            self._health_runs += 1
            version = parse_banner_version(res.stdout) or "unknown"
            self._last_health = EngineHealth(
                available=True,
                engine_version=f"OpenDSS CLI {version}",
                detail=f"可执行文件：{exe}",
            )
        except AdapterError as exc:
            self._last_health = EngineHealth(
                available=False, detail=f"OpenDSS CLI 探测失败：{exc.message}"
            )
        return self._last_health

    # ------------------------------------------------------------ compile_case
    def compile_case(self, case_workspace: str, master_dss: str) -> CircuitHandle:
        handle = CircuitHandle(
            handle_id=uuid.uuid4().hex[:12],
            case_workspace=case_workspace,
            master_dss=master_dss,
        )
        if not os.path.isfile(master_dss):
            raise AdapterError(ErrorCode.VALIDATION_FAILED, f"算例文件不存在：{master_dss}")

        # ⚠️ `case_workspace` 是**算例所在目录**（主链路传的是 `<workspace>/input`），
        #    不是"任务工作区"。所以工作目录必须建在它**外面** —— 否则镜像时会被
        #    当成源目录的一部分而无限递归（实测：4247 层嵌套 / 2124 个文件，
        #    任务卡死 300s 直到看门狗重启进程）。
        #    放在上一级后，工作目录与源目录是兄弟关系，天然无包含。
        ws_parent = os.path.dirname(os.path.abspath(case_workspace)) or case_workspace
        workdir = os.path.join(ws_parent, "cli_work")
        try:
            shadow = mirror_case(master_dss, workdir, subdir="case")
        except CliError as exc:
            raise AdapterError(ErrorCode.DATA_UNAVAILABLE, str(exc)) from exc
        self._leftovers.append(workdir)

        facts = parse_dss_directory(os.path.dirname(os.path.abspath(master_dss)))
        sess = _CliSession(
            workdir=workdir,
            master_dss=master_dss,
            shadow_master=shadow,
            source_facts=facts,
        )
        sess.static_run = self._run(
            self._static_commands(shadow),
            workdir,
            [os.path.dirname(shadow)],
            timeout_s=self.cli_quick_timeout_s,
        )

        # 超时是**明确失败**，不是"算例慢" —— 让上层按 TIMEOUT 处理。
        if sess.static_run.timed_out:
            raise AdapterError(
                ErrorCode.TIMEOUT,
                f"OpenDSS CLI 编译/结构扫描超时（>{self.cli_quick_timeout_s:.0f}s）：{master_dss}",
            )
        # compile 失败判定：stdout 里出现引擎错误标记，或 summary 缺失。
        _raise_if_compile_failed(sess.static_run)

        # 校验规则**单点定义**在 `circuit_validation`，与 COM / dss-python 共用。
        try:
            observation = self._observation(sess)
        except CliError as exc:
            # 产物检索自身失败 —— 典型是多份同类产物（工作目录被复用）。
            # 转成明确失败，**不要**让它冒泡成"系统异常"（那会掩盖真实原因）。
            raise AdapterError(ErrorCode.DATA_UNAVAILABLE, str(exc)) from exc
        detail = validate_compiled_circuit(observation)
        if detail:
            raise AdapterError(ErrorCode.VALIDATION_FAILED, detail)

        handle.compiled = True
        handle.dss_ctx = sess
        # 适配器每个任务新建一次，所以"当前会话"就是最后一个。
        self._last_session = sess
        return handle

    def _observation(self, sess: _CliSession) -> CircuitObservation:
        """从 CLI 产物 + 源文件事实组装电路观察量（判定见 `circuit_validation`）。"""
        run = sess.best_run
        buses = parse_show_buses(run.artifact("_Buses") if run else None)
        bus_names = frozenset(normalize_bus_name(b["name"]) for b in buses if b.get("name"))
        if not bus_names:
            # show buses 没解析出来时退回源文件（宁可少信息也不要空集）
            bus_names = frozenset(
                normalize_bus_name(b)
                for e in sess.source_facts.elements
                for b in e.buses
                if b
            )
        bus_base_kv: list[tuple[str, Any]] = []
        for b in buses:
            if b.get("name"):
                bus_base_kv.append((b["name"], b.get("kv_base")))
        endpoints: list[tuple[str, str]] = []
        for e in sess.source_facts.elements:
            for bus in e.buses:
                endpoints.append((e.full_name, bus))
        return CircuitObservation(
            bus_count=self._bus_count(sess),
            bus_names=bus_names,
            endpoints=tuple(endpoints),
            bus_base_kv=tuple(bus_base_kv),
            vsource_buses=sess.source_facts.vsource_buses(),
        )

    @staticmethod
    def _bus_count(sess: _CliSession) -> int:
        run = sess.best_run
        summary = parse_summary(run.artifact("Summary") if run else None)
        n = _num(summary.get("NumBuses"))
        if n is not None:
            return int(n)
        # 退化：用 show buses / 源文件里的母线数
        buses = parse_show_buses(run.artifact("_Buses") if run else None)
        if buses:
            return len(buses)
        seen = {b for e in sess.source_facts.elements for b in e.buses if b}
        return len(seen)

    # -------------------------------------------------------- circuit_summary
    def circuit_summary(self, circuit: CircuitHandle) -> tuple[dict[str, int], dict[str, float]]:
        """算例对象统计与母线基准电压表（供 validate_case 写 validation.json）。"""
        sess = self._session(circuit)
        run = sess.best_run
        summary = parse_summary(run.artifact("Summary") if run else None)
        facts = sess.source_facts
        counts = {
            "bus": self._bus_count(sess),
            # line / transformer 用**源文件**计数：引擎集合不含 Enabled=no 的对象，
            # 而画像与校验都需要"定义了多少台"。
            "line": facts.count("line") or count_by_prefix(
                list(parse_powers(run.artifact("POWERS") if run else None).keys())
            ).get("line", 0),
            "transformer": facts.count("transformer"),
            "load": facts.count("load"),
        }
        base_kv: dict[str, float] = {}
        for b in parse_show_buses(run.artifact("_Buses") if run else None):
            kv = b.get("kv_base")
            if b.get("name") and kv:
                base_kv[b["name"]] = round(float(kv), 3)
        # 无母线表时留空，不编造
        from .fidelity_mapping import read_fidelity_mapping, restore_summary
        return restore_summary(counts, base_kv, read_fidelity_mapping(circuit.case_workspace))

    # ---------------------------------------------- circuit inventory（网络画像）
    def collect_circuit_inventory(self) -> CircuitInventory:
        """收集网络画像所需的观察量。

        CLI 没有"引擎对象"，所以这里从**源文件事实 + export 产物**凑齐。
        组装侧（`build_network_profile`）是纯数据、单点定义的，一行都不用改 ——
        这正是 A-1 做收集/组装分离的目的。
        """
        sess = self._current_session()
        return self._inventory(sess)

    def _current_session(self) -> _CliSession:
        """取"当前会话"。CLI 适配器每个任务新建一次，所以只可能有一个。"""
        sess = getattr(self, "_last_session", None)
        if not isinstance(sess, _CliSession):
            raise AdapterError(ErrorCode.ENGINE_ERROR, "CLI 会话不存在（请先 compile_case）")
        return sess

    def _inventory(self, sess: _CliSession) -> CircuitInventory:
        run = sess.best_run
        facts = sess.source_facts
        buses = parse_show_buses(run.artifact("_Buses") if run else None)
        loads_dev = parse_loads(run.artifact("LOADS") if run else None)
        powers = parse_powers(run.artifact("POWERS") if run else None)
        prefixes = count_by_prefix(list(powers.keys()))
        # 无 powers（未 solve）时退回源文件计数
        def _cnt(kind: str) -> int:
            return prefixes.get(kind, 0) or facts.count(kind)
        return CircuitInventory(
            bus_count=self._bus_count(sess),
            line_count=facts.count("line") or _cnt("line"),
            transformer_count=facts.count("transformer") or _cnt("transformer"),
            load_count=_cnt("load"),
            generator_count=_cnt("generator"),
            pv_system_count=_cnt("pvsystem"),
            storage_count=_cnt("storage"),
            capacitor_count=_cnt("capacitor"),
            # RegControl **不在** export 产物里（实测），只能读源文件。
            regulator_count=facts.count("regcontrol"),
            bus_kv_base=tuple(
                (b["name"], b.get("kv_base")) for b in buses if b.get("name")
            ),
            loads=tuple(
                (float(d["kw"] or 0.0), int(d["phases"] or 3))
                for d in loads_dev if d.get("name")
            ),
            # 端点母线必须来自源文件：export 产物里**没有任何母线列**。
            line_endpoints=facts.line_endpoints(),
        )

    # ------------------------------------------------------ apply_solver_profile
    def apply_solver_profile(
        self, circuit: CircuitHandle, solver_profile: OpenDSSSolverProfile
    ) -> None:
        """记录求解设置。真正生效在 `solve()` —— CLI 是批处理，无法分步下发。

        与 COM 一致的约束：未冻结的 profile 禁止应用。
        """
        if not solver_profile.frozen:
            raise AdapterError(ErrorCode.REQ_SCHEMA_INVALID, "SolverProfile 未冻结，禁止应用")
        sess = self._session(circuit)
        sess.max_iterations = int(solver_profile.iteration_config.max_iterations)
        sess.tolerance = float(solver_profile.tolerance_config.tolerance)

    # --------------------------------------------------------------------- solve
    def solve(self, circuit: CircuitHandle) -> SolverExecutionResult:
        """跑一次**完整批处理**并返回求解状态。

        **每次都真的重跑**（不使用上一次的产物）：CLI 的"一次运行"本来就是一个
        原子批处理，重跑是它最自然的语义；同时也让上游的**复现性检查**
        （同配置二次求解比对）名副其实，而不是拿缓存冒充。
        """
        sess = self._session(circuit)
        cmds = self._full_commands(sess.shadow_master, sess.max_iterations, sess.tolerance)
        t0 = time.time()
        run = self._run(cmds, sess.workdir, [os.path.dirname(sess.shadow_master)])
        sess.solve_count += 1
        if run.timed_out:
            # 超时是这条通道**能强杀**的地方：进程已被 kill，这里明确上报。
            raise AdapterError(
                ErrorCode.TIMEOUT,
                f"OpenDSS CLI 求解超时（>{self.cli_timeout_s:.0f}s）；进程已强制终止",
            )
        _raise_if_compile_failed(run)
        sess.full_run = run

        summary = parse_summary(run.artifact("Summary"))
        status = str(summary.get("Status", "")).strip().lower()
        # ⚠️ 这个版本的 `Status` 在部分病态算例上会报得过于乐观（Step 0.5 实测：
        #    电压崩到 0.38 pu 也报 SOLVED）。所以先取它，再让下游的独立验证兜底。
        converged = status not in ("unsolved", "")
        iterations = int(_num(summary.get("Iterations")) or 0)
        duration = time.time() - t0
        return SolverExecutionResult(
            converged=converged,
            iterations=iterations,
            error_flag=not converged,
            error_text="" if converged else f"CLI 报告 Status={summary.get('Status')}",
            duration_s=duration,
        )

    # --------------------------------------------------------- extract_raw_result
    def extract_raw_result(
        self, circuit: CircuitHandle, result_scopes: list[ResultScope]
    ) -> OpenDSSRawResult:
        sess = self._session(circuit)
        if sess.full_run is None:
            raise AdapterError(ErrorCode.ENGINE_ERROR, "尚未 solve，无法提取结果")
        raw = OpenDSSRawResult()
        run = sess.full_run
        summary = parse_summary(run.artifact("Summary"))
        powers = parse_powers(run.artifact("POWERS"))
        capacity = parse_keyed_table(run.artifact("CAPACITY"), "Name")
        currents = parse_keyed_table(run.artifact("CURRENTS"), "Element")

        converged = str(summary.get("Status", "")).strip().lower() not in ("unsolved", "")
        raw.solver = {
            "converged": converged,
            "iterations": int(_num(summary.get("Iterations")) or 0),
            "mode": "snapshot",
        }

        # 系统级：与 COM 侧同口径。
        #   total_power_* 取 summary 的 TotalMW/TotalMvar（= 源侧供电，正值为受电侧）
        #   元件求和**必须**来自 powers 的元素表，不能用 (源侧 − 损耗) 倒推 ——
        #   那是**净负荷**，含发电时会写成负数（本项目曾因此写出「总负荷 -57332 kW」）。
        raw.system = {
            "total_power_kw": (_num(summary.get("TotalMW")) or 0.0) * 1000.0,
            "total_power_kvar": (_num(summary.get("TotalMvar")) or 0.0) * 1000.0,
            "losses_kw": (_num(summary.get("MWLosses")) or 0.0) * 1000.0,
            "losses_kvar": (_num(summary.get("MvarLosses")) or 0.0) * 1000.0,
        }
        raw.system.update(element_power_sums(powers))
        # 越限相关的现成数据（CLI 比 COM 强的地方）
        for key, col in (("max_pu_voltage", "MaxPuVoltage"), ("min_pu_voltage", "MinPuVoltage"),
                         ("loss_pct", "pctLosses")):
            v = _num(summary.get(col))
            if v is not None:
                raw.system[key] = v

        if ResultScope.BUS in result_scopes or ResultScope.SYSTEM in result_scopes:
            raw.buses = parse_voltages(run.artifact("VOLTAGES"))

        if ResultScope.BRANCH in result_scopes:
            endpoints = {e.full_name: e.buses for e in sess.source_facts.elements}
            for name, terminals in powers.items():
                prefix = str(name).split(".", 1)[0].strip().lower()
                if prefix != "line":
                    continue
                p, q = terminals.get(1, (None, None))
                cur = currents.get(name) or {}
                cur_vals = [_num(cur.get(f"I1_{i}")) for i in range(1, 5)]
                cur_vals = [c for c in cur_vals if c is not None]
                cap = capacity.get(name) or {}
                # 源文件里的元件名是小写全名（`line.l1`），export 里是引擎原始大小写。
                buses = endpoints.get(str(name).lower(), ())
                rating = _num(cap.get("Imax"))
                raw.lines.append({
                    "name": name if "." in name else f"Line.{name}",
                    # ⚠️ 端点母线名**只能来自源文件**：所有 export 产物的列名里
                    #    都没有 bus 字段（9/16 实测逐命令核对）。缺了就留空，
                    #    不编造 —— 拓扑画像会因为端点缺失而少算一条，不会算错。
                    "bus1": buses[0] if len(buses) > 0 else "",
                    "bus2": buses[1] if len(buses) > 1 else "",
                    "p_kw": p,
                    "q_kvar": q,
                    "current_a": max(cur_vals) if cur_vals else None,
                    "rating_a": rating,
                })

        if ResultScope.TRANSFORMER in result_scopes:
            kva_map = {
                e.full_name: _num(e.get("kva"))
                for e in sess.source_facts.by_kind("transformer")
            }
            for name, terminals in powers.items():
                prefix = str(name).split(".", 1)[0].strip().lower()
                if prefix != "transformer":
                    continue
                p, q = terminals.get(1, (None, None))
                short = str(name).split(".", 1)[-1].lower()
                # 与线路同等待遇：端点母线**只能来自源文件**（`endpoints`），
                # 拿不到就留空。漏采集会让拓扑判据把每条馈线算成独立分量
                # （真机 case31 实测：15 个分量 / 268 个母线"不可达"，
                #  与"已收敛"自相矛盾）—— 详见 com 适配器同处注释。
                tbuses = endpoints.get(str(name).lower(), ())
                raw.transformers.append({
                    "name": name if "." in name else f"Transformer.{name}",
                    "bus1": tbuses[0] if len(tbuses) > 0 else "",
                    "bus2": tbuses[1] if len(tbuses) > 1 else "",
                    "p_kw": p,
                    "q_kvar": q,
                    # 源文件里读 kVA（export capacity 无 kVA 列）
                    "rated_kva": kva_map.get(f"transformer.{short}"),
                })

        raw.element_count = {
            "bus": len(raw.buses),
            "line": len(raw.lines),
            "transformer": len(raw.transformers),
            "load": len(parse_loads(run.artifact("LOADS"))),
        }
        # 与 COM 通道**同一个字段**、同一个含义（约定：口径单点定义，
        # 两条通道不得各自解释）。两路都写，产物形状才一致。
        raw.collector_version = ADAPTER_VERSION
        from .fidelity_mapping import read_fidelity_mapping, apply_fidelity_mapping
        measured = {str(k).lower(): v for k, v in powers.items()}
        apply_fidelity_mapping(raw, read_fidelity_mapping(circuit.case_workspace),
                               lambda element, terminal: measured.get(element.lower(), {}).get(terminal), result_scopes)
        return raw

    # --------------------------------------------------------------------- close
    def close(self, circuit: CircuitHandle) -> None:
        """结束会话。

        ⚠️ **不删除工作目录** —— 本项目的运行环境对删除动作敏感（批量删除阈值），
        且产物本身是诊断留痕。目录位于任务工作目录下，随任务产物一并管理。
        """
        try:
            circuit.compiled = False
            circuit.dss_ctx = None
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- 诊断
    def engine_diagnostics(self) -> dict[str, Any]:
        """CLI 通道的诊断信息（与 COM 的 `engine_diagnostics` 对应）。"""
        exe, why = discover_cli_path()
        sess = getattr(self, "_last_session", None)
        return {
            "backend": "cli",
            "cli_path": exe,
            "cli_origin": why,
            "health_runs": self._health_runs,
            "session_ready": isinstance(sess, _CliSession),
            "solve_count": sess.solve_count if isinstance(sess, _CliSession) else 0,
            # 工作目录刻意不删（零删除），列出来供任务收尾或人工回收
            "leftover_workdirs": list(self._leftovers),
        }


def _raise_if_compile_failed(run: CliRunResult) -> None:
    """从 CLI 输出里判定 compile 失败并抛出。

    判定依据（实测）：
      * `rc` **不可靠**（非 0 也可能产物齐全）→ **不看 rc**；
      * 引擎错误会同时打到 stdout，形如 `Error! ...` / `(xxx) OpenDSS`；
      * 最硬的判据是**产物是否生成** —— compile 成功才会有母线表
        （`*_Buses.Txt`，来自 `show buses`）或 summary。
    """
    text = (run.stdout or "") + (run.stderr or "")
    lower = text.lower()
    if "error!" in lower or "not found" in lower:
        # 取出第一行错误信息，便于上层写进报告
        for raw in text.splitlines():
            if "error" in raw.lower() or "not found" in raw.lower():
                raise AdapterError(
                    ErrorCode.VALIDATION_FAILED,
                    f"OpenDSS Compile 失败: {raw.strip()[:200]}",
                )
        raise AdapterError(ErrorCode.VALIDATION_FAILED, "OpenDSS Compile 失败（引擎报错）")
    if not (run.has("Summary") or run.has("_Buses")):
        raise AdapterError(
            ErrorCode.VALIDATION_FAILED,
            "OpenDSS Compile 未产出任何电路产物（母线表与 summary 都缺失）"
            "；可能算例无效、引擎未执行，或产物收集目录不对",
        )
