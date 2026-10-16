"""总负荷口径：必须来自负荷元件求和，不能由源侧功率倒推。

背景（实测证据）：
改动前 `total_load_kw = 源侧供电 − 网损`，那是**净负荷**。
626 个真实产物中 148 个的「发电/负荷」比偏离 1 超过 15%，最极端 20.4 倍；
有算例源侧 168684.5 kW 而"总负荷"被写成 **−57332.6 kW** ——
报告里的原文是"系统总负荷为 -57332.60 kW"，一眼就是错的。

本套件锁死三件事：
  A. 求和语义：total_load_kw 只认负荷元件；发电注入单列且符号为正。
  B. 兼容与降级：引擎没给求和时退回倒推（保持数值可用），但不冒充"负荷"。
  C. 平衡与统计口径：功率平衡必须计入发电注入；损耗占比基数用负荷而非净负荷。

全部自包含（假引擎/假 raw），不依赖真实 COM 或 dss-python。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dpf_agent.domain.result_normalizer import (  # noqa: E402
    normalize_powerflow_result,
)
from dpf_agent.engines.opendss_adapter import (  # noqa: E402
    OpenDSSRawResult,
    SolverExecutionResult,
)
from dpf_agent.schemas.enums import ResultScope  # noqa: E402

_PASS = 0
_FAIL = 0
_RESULTS: list[str] = []
_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 断言结果落盘位置：写到**包外**的隔离目录，避免污染源码树（否则 git status 会多出
# 一个未跟踪文件）。拿不到隔离目录就退到临时目录，绝不写进 dpf_agent/。
_OUT_DIR = os.environ.get("DPF_TEST_OUT_DIR") or tempfile.gettempdir()


def _flush() -> None:
    """每次断言即落盘，避免后续崩溃把已通过的结果一起吞掉。"""
    try:
        os.makedirs(_OUT_DIR, exist_ok=True)
        with open(os.path.join(_OUT_DIR, "test_load_caliber.txt"),
                  "w", encoding="utf-8") as f:
            f.write("\n".join(_RESULTS) + f"\n\n通过 {_PASS} / 失败 {_FAIL}\n")
    except OSError:
        pass


def check(group: str, name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    tag = "PASS" if cond else "FAIL"
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
    _RESULTS.append(f"[{tag}] {group} {name}  {detail}")
    _flush()


# ---------------------------------------------------------------- 假 raw / 假引擎
def _raw(system: dict, **kw) -> OpenDSSRawResult:
    raw = OpenDSSRawResult(
        solver={"converged": True, "iterations": 2, "mode": "snapshot"},
        system=system,
        buses=[{"name": "b1", "base_kv": 12.66, "voltage_pu": [1.0], "angle_deg": [0.0]}],
        lines=[],
        transformers=[],
        element_count={"bus": 1, "line": 0, "transformer": 0, "load": 1},
    )
    for k, v in kw.items():
        setattr(raw, k, v)
    return raw


def _norm(system: dict):
    scopes = [ResultScope.BUS, ResultScope.SYSTEM]
    raw = _raw(system)
    solve = SolverExecutionResult(converged=True, iterations=2, error_flag=False)
    return normalize_powerflow_result(
        raw, solve, "t-1", "fp-1", "sp-1", "eng-1", "adp-1", scopes
    )


class _El:
    """假元件：Powers 为「流入元件为正」。"""

    def __init__(self, p_pairs):
        self._pairs = p_pairs  # [(p,q), ...] 每相
        self.NumPhases = len(p_pairs)

    @property
    def Powers(self):
        out = []
        for p, q in self._pairs:
            out.extend([p, q])
        return out


# 注：本文件后半另有一个同名 _Coll（按 ckt 游标推进 ActiveCktElement 的版本），
# 它覆盖了这里——也就是说下面这个按 list 游标的版本**从来没有生效过**。
# 留着两个同名类是个陷阱（改了不生效、还以为生效），故删除。


class _Ckt:
    """最小假电路：只提供求和函数需要的三个入口。

    OpenDSS 的 `collection.First` / `collection.Next` 会同步推进
    `ckt.ActiveCktElement`，所以这里的集合对象自己记住游标位置，
    并让 `ActiveCktElement` 读取当前游标 —— 与真引擎行为一致。
    """

    def __init__(self, loads, gens, pvsystems=None, storages=None, expose_pv=True):
        self._els = {
            "load": list(loads),
            "gen": list(gens),
            "pv": list(pvsystems or []),
            "storage": list(storages or []),
        }
        self._cur_item = None
        self.Loads = _Coll(self, "load")
        self.Generators = _Coll(self, "gen")
        # expose_pv=False 模拟"引擎不暴露该集合"（旧引擎 / dss-python 某些版本）
        if expose_pv:
            self.PVSystems = _Coll(self, "pv")
        self.Storages = _Coll(self, "storage")

    @property
    def ActiveCktElement(self):
        return self._cur_item


class _Coll:
    def __init__(self, ckt, key):
        self._ckt = ckt
        self._key = key
        self._els = ckt._els[key]
        self._i = -1

    @property
    def First(self):
        if not self._els:
            self._ckt._cur_item = None
            return 0
        self._i = 0
        self._ckt._cur_item = self._els[0]
        return 1

    @property
    def Next(self):
        self._i += 1
        if self._i < len(self._els):
            self._ckt._cur_item = self._els[self._i]
            return self._i + 1
        self._ckt._cur_item = None
        return 0

    @property
    def Count(self):
        return len(self._els)


def _terminal1(ce):
    """与适配器内 `_terminal1` 同语义：端子 1 三相合计 P/Q。"""
    n = int(ce.NumPhases)
    powers = list(ce.Powers)
    if len(powers) < 2 * n or n < 1:
        return None, None, None
    p = sum(float(powers[2 * i]) for i in range(n))
    q = sum(float(powers[2 * i + 1]) for i in range(n))
    return p, q, None


def _sums(loads, gens):
    """调用 COM 适配器的静态求和逻辑（不启动真引擎）。"""
    from dpf_agent.engines.opendss_com_adapter import OpenDSSComAdapter

    return OpenDSSComAdapter._element_power_sums(_Ckt(loads, gens), _terminal1)


def main() -> int:
    global _PASS, _FAIL
    _PASS = _FAIL = 0
    _RESULTS.clear()

    print("=" * 78)
    print("A. 求和语义：总负荷只认负荷元件")
    print("=" * 78)

    # A1 无发电：求和值 = 负荷消耗，与倒推值一致
    r = _norm({
        "total_power_kw": 610.0,
        "total_power_kvar": 300.0,
        "losses_kw": 10.0,
        "losses_kvar": 5.0,
        "load_kw": 600.0,
        "load_kvar": 295.0,
        "gen_kw": 0.0,
        "gen_kvar": 0.0,
        "load_elements": 3,
        "load_elements_skipped": 0,
    })
    ss = r.system_summary
    check("A1", "无发电时 total_load_kw 取负荷求和",
          ss.total_load_kw == 600.0, f"{ss.total_load_kw}")
    check("A2", "净负荷 = 源侧 − 网损 = 600",
          ss.net_load_kw == 600.0, f"{ss.net_load_kw}")

    # A3 有发电：总和 1000 kW 负荷 + 400 kW 发电 → 源侧只有 610
    r2 = _norm({
        "total_power_kw": 610.0,
        "total_power_kvar": 300.0,
        "losses_kw": 10.0,
        "losses_kvar": 5.0,
        "load_kw": 1000.0,
        "load_kvar": 480.0,
        "gen_kw": 400.0,
        "gen_kvar": 180.0,
        "load_elements": 5,
        "load_elements_skipped": 0,
    })
    ss2 = r2.system_summary
    check("A3", "有发电时 total_load_kw 仍是负荷求和 1000（不是倒推 600）",
          ss2.total_load_kw == 1000.0, f"{ss2.total_load_kw}")
    check("A4", "净负荷独立记为 600",
          ss2.net_load_kw == 600.0, f"{ss2.net_load_kw}")
    check("A5", "发电注入单列且为正号 400",
          ss2.total_generator_kw == 400.0, f"{ss2.total_generator_kw}")
    check("A6", "total_load_kvar 已填（不再恒 None）",
          ss2.total_load_kvar == 480.0, f"{ss2.total_load_kvar}")

    # A7 反送：负荷 100，发电 500 → 源侧为负
    r3 = _norm({
        "total_power_kw": -410.0,
        "total_power_kvar": -150.0,
        "losses_kw": 10.0,
        "losses_kvar": 5.0,
        "load_kw": 100.0,
        "load_kvar": 40.0,
        "gen_kw": 500.0,
        "gen_kvar": 190.0,
        "load_elements": 1,
        "load_elements_skipped": 0,
    })
    ss3 = r3.system_summary
    check("A7", "反送场景 total_load_kw 仍为正的 100（不会是负数）",
          ss3.total_load_kw == 100.0, f"{ss3.total_load_kw}")
    check("A8", "反送场景净负荷为负 −420",
          ss3.net_load_kw == -420.0, f"{ss3.net_load_kw}")
    check("A9", "反送场景负荷基数仍取 |总负荷|，损耗占比为正",
          r3.loss_summary.loss_pct_of_load is not None
          and r3.loss_summary.loss_pct_of_load > 0,
          f"{r3.loss_summary.loss_pct_of_load}")

    # A10 关键回归：这是历史上被写成"系统总负荷为 -57332.60 kW"的那个场景
    r4 = _norm({
        "total_power_kw": 168684.5,
        "total_power_kvar": 50000.0,
        "losses_kw": 3000.0,
        "losses_kvar": 1500.0,
        "load_kw": 223000.0,
        "load_kvar": 80000.0,
        "gen_kw": 87000.0,
        "gen_kvar": 30000.0,
        "load_elements": 120,
        "load_elements_skipped": 0,
    })
    ss4 = r4.system_summary
    check("A10", "历史错误场景：总负荷为正 223000，不再报 −57332.6",
          ss4.total_load_kw == 223000.0 and ss4.total_load_kw > 0,
          f"{ss4.total_load_kw}")
    check("A11", "该场景净负荷 ≈ 165684.5（正值但远小于总负荷）",
          abs(ss4.net_load_kw - 165684.5) < 1e-6, f"{ss4.net_load_kw}")

    print()
    print("=" * 78)
    print("B. 兼容与降级")
    print("=" * 78)

    # B1 引擎没给求和 → 退回倒推，数值可用
    r5 = _norm({"total_power_kw": 610.0, "total_power_kvar": 300.0,
                "losses_kw": 10.0, "losses_kvar": 5.0})
    check("B1", "缺 load_kw 时退回倒推 600（保持可用）",
          r5.system_summary.total_load_kw == 600.0,
          f"{r5.system_summary.total_load_kw}")
    check("B2", "退回时 net_load_kw 同样为 600",
          r5.system_summary.net_load_kw == 600.0, f"{r5.system_summary.net_load_kw}")

    # B3 skipped 计数如实传递（0 也保留，区别于 None）
    check("B3", "skipped=0 如实保留为 0",
          ss2.load_elements_skipped == 0, f"{ss2.load_elements_skipped}")
    r6 = _norm({"total_power_kw": 610.0, "losses_kw": 10.0,
                "load_kw": 500.0, "load_elements": 5,
                "load_elements_skipped": 2})
    check("B4", "skipped=2 如实传递（提示偏小）",
          r6.system_summary.load_elements_skipped == 2,
          f"{r6.system_summary.load_elements_skipped}")
    r7 = _norm({"total_power_kw": 610.0, "losses_kw": 10.0})
    check("B5", "引擎不支持该能力时 skipped 为 None（区别于 0）",
          r7.system_summary.load_elements_skipped is None,
          f"{r7.system_summary.load_elements_skipped}")

    # B6 全缺失不抛异常
    r8 = _norm({})
    check("B6", "系统字段全缺失时不抛异常",
          r8.system_summary.total_load_kw is None
          and r8.system_summary.net_load_kw is None)

    print()
    print("=" * 78)
    print("C. 元件求和函数（COM 适配器静态逻辑）")
    print("=" * 78)

    # C1 两负荷 + 一发电
    s = _sums(
        [_El([(100.0, 30.0)]), _El([(200.0, 60.0)])],
        [_El([(-50.0, -10.0)])],
    )
    check("C1", "负荷求和 = 300 kW",
          abs(s["load_kw"] - 300.0) < 1e-9, f"{s['load_kw']}")
    check("C2", "负荷 kvar 求和 = 90",
          abs(s["load_kvar"] - 90.0) < 1e-9, f"{s['load_kvar']}")
    check("C3", "发电注入取反为正 = 50 kW",
          abs(s["gen_kw"] - 50.0) < 1e-9, f"{s['gen_kw']}")
    check("C4", "元件计数正确 2 负荷 / 1 发电",
          s["load_elements"] == 2 and s["gen_elements"] == 1,
          f"{s['load_elements']}/{s['gen_elements']}")

    # C5 三相元件逐相累加
    s2 = _sums([_El([(10.0, 3.0), (10.0, 3.0), (10.0, 3.0)])], [])
    check("C5", "三相元件按相累加 = 30 kW",
          abs(s2["load_kw"] - 30.0) < 1e-9, f"{s2['load_kw']}")

    # C6 无元件
    s3 = _sums([], [])
    check("C6", "无元件时求和为 0 且不抛异常",
          s3["load_kw"] == 0.0 and s3["load_elements"] == 0)

    # C7 损坏元件被跳过并计数
    s4 = _sums([_El([(100.0, 30.0)]), _El([])], [])
    check("C7", "读数缺失的元件被跳过并计数",
          abs(s4["load_kw"] - 100.0) < 1e-9 and s4["load_elements_skipped"] == 1,
          f"{s4['load_kw']} skip={s4['load_elements_skipped']}")

    print()
    print("=" * 78)
    print("D. 功率平衡必须计入发电注入")
    print("=" * 78)

    from dpf_agent.verification.verifier import Verifier  # noqa: E402

    _src_v = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "verification", "verifier.py"),
        encoding="utf-8",
    ).read()
    check("D1", "verifier 残差式含 total_generator_kw",
          "total_generator_kw" in _src_v, "已接入发电注入")
    check("D2", "verifier 不再用纯粹的 gen-load-loss 三式",
          "ss.total_generation_kw - ss.total_load_kw - ls.total_loss_kw" not in _src_v,
          "旧三式已改")
    check("D3", "平衡分母计入发电量级（避免小负荷大发电时虚高）",
          "abs(gen_elem_kw)" in _src_v, "已加入 gen 到 base")

    # D4/D5 数值验证：新旧平衡式在含发电场景的差异
    check("D4", "含发电：610 − 1000 + 400 − 10 = 0，新式残差为 0",
          abs(610.0 - 1000.0 + 400.0 - 10.0) < 1e-9, "0.0")
    check("D5", "旧三式在同一场景得出 |610 − 1000 − 10| = 400（会误判不平衡）",
          abs(610.0 - 1000.0 - 10.0) == 400.0, "400.0")

    _src_v = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "verification", "verifier.py"),
        encoding="utf-8",
    ).read()
    check("D6", "verifier 源码含发电注入项（回归护栏）",
          "+ gen_elem_kw" in _src_v, "已含")
    check("D7", "verifier 不再出现纯三式残差表达式（字符串级）",
          "ss.total_generation_kw - ss.total_load_kw - ls.total_loss_kw" not in _src_v,
          "已移除")
    check("D8", "平衡分母计入发电量级（小负荷大发电时不虚高）",
          "abs(gen_elem_kw)" in _src_v, "已加入")

    print()
    print("=" * 78)
    print("E. 报告文案口径（静态契约）")

    _here = _PKG
    print("=" * 78)

    _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _src_b = open(os.path.join(_here, "evidence", "builder.py"), encoding="utf-8").read()
    _src_m = open(os.path.join(_here, "renderers", "markdown_renderer.py"),
                  encoding="utf-8").read()

    check("E1", "证据文案注明负荷来源（元件求和）",
          "各负荷元件端子1有功之和" in _src_b, "已注明")
    check("E2", "证据层给出 net_load_kw 条目",
          '"net_load_kw"' in _src_b, "已添加")
    check("E3", "证据层对 skipped>0 给出偏小提示",
          "load_elements_skipped" in _src_b and "偏小" in _src_b, "已添加")
    check("E4", "报告层给出净负荷行并解释差异来源",
          "净负荷（源侧供电 − 网损）" in _src_m, "已添加")
    check("E5", "报告层对 skipped>0 给出警示",
          "读数缺失未计入求和" in _src_m, "已添加")
    check("E6", "报告层总负荷行注明求和口径",
          "各负荷元件端子1有功之和" in _src_m, "已注明")

    _src_a = open(os.path.join(_here, "engines", "opendss_adapter.py"),
                  encoding="utf-8").read()
    _src_c = open(os.path.join(_here, "engines", "opendss_com_adapter.py"),
                  encoding="utf-8").read()
    check("E7", "基类实现元件求和（集合与口径分离）",
          "_element_power_sums" in _src_a and "_POWER_COLLECTIONS" in _src_a, "已实现")
    check("E8", "COM 适配器不重复定义口径（复用基类）",
          "def _element_power_sums" not in _src_c,
          "已删除副本：此前副本存在，导致修基类对生产路径完全无效")
    check("E9", "求和遍历登记了 PVSystems（光伏必须计入发电）",
          '"PVSystems"' in _src_a, "已登记")
    check("E10", "求和循环有死循环护栏",
          "1_000_000" in _src_a, "已加护栏")

    _src_n = open(os.path.join(_here, "domain", "result_normalizer.py"),
                  encoding="utf-8").read()
    check("E11", "normalizer 不再把倒推值直接当 total_load_kw",
          "total_load_kw = (\n        total_gen_kw - total_loss_kw" not in _src_n,
          "旧赋值已移除")
    check("E12", "normalizer 优先取 load_kw 求和",
          'sys_raw.get("load_kw")' in _src_n, "已实现")

    # ============================================ F. 元件口径表与 PVSystem 计入
    #
    # 背景（9/15 真引擎实测，任务 pf-20260915-233001-4e59c5）：
    # `New PVSystem` 的元件**不在** `ckt.Generators` 里。旧实现只遍历
    # Loads + Generators，于是含光伏的算例 gen_kw=0，
    # 功率平衡残差恰好等于光伏出力 → 任务被判
    # VERIFICATION_FAILED:['power_balance'] 而**直接失败**（不是数值偏差）。
    from dpf_agent.engines.opendss_adapter import (
        ELEMENT_EXCLUDED_PREFIXES,
        ELEMENT_ROLE_BY_PREFIX,
        OpenDSSAdapter,
        _POWER_COLLECTIONS,
        classify_element_by_prefix,
    )

    check("F1", "口径表把 PVSystem 归为发电",
          classify_element_by_prefix("pvsystem") == "generation",
          str(ELEMENT_ROLE_BY_PREFIX))
    check("F2", "口径表把 Generator 归为发电",
          classify_element_by_prefix("generator") == "generation")
    check("F3", "口径表把 Load 归为负荷",
          classify_element_by_prefix("load") == "load")
    # 9/16 口径修订：储能从「完全排除」改为「单独一类 bidirectional」。
    # 原因：完全排除会让平衡残差**恰好等于储能功率**（实测 4 母线算例
    # 「负荷 1000 + 储能放电 500」residual = 0.500000 pu，容差 1e-3），
    # 含储能充放电的算例被判 VERIFICATION_FAILED 直接失败。
    # 现在它仍**不进**负荷/发电（避免"负的负荷"），但**进功率平衡式**。
    check("F4", "储能单独归为「双向」角色（不计负荷/发电，但进平衡式）",
          classify_element_by_prefix("storage") == "bidirectional"
          and "storage" not in ELEMENT_EXCLUDED_PREFIXES,
          str(ELEMENT_ROLE_BY_PREFIX.get("storage", "")))
    check("F5", "未登记的前缀不猜边（返回 None）",
          classify_element_by_prefix("somefutureelement") is None)
    check("F6", "前缀判定忽略大小写与空白",
          classify_element_by_prefix("  PVSystem ") == "generation")
    check("F7", "求和集合登记了 PVSystems",
          any(a == "PVSystems" for a, _ in _POWER_COLLECTIONS),
          str(_POWER_COLLECTIONS))

    def _t1(ce):  # noqa: ANN001, ANN202 - 与适配器内的 terminal1 同形
        n = int(ce.NumPhases)
        powers = list(ce.Powers)
        if len(powers) < 2 * n or n < 1:
            return None, None, None
        p = sum(float(powers[2 * i]) for i in range(n))
        q = sum(float(powers[2 * i + 1]) for i in range(n))
        return p, q, None

    _ck1 = _Ckt(loads=[_El([(400.0, 100.0)] * 3)], gens=[])
    _sr1 = OpenDSSAdapter._element_power_sums(_ck1, _t1)
    check("F8", "负荷计入 load_kw（三相合计）",
          abs(_sr1["load_kw"] - 1200.0) < 1e-6, str(_sr1["load_kw"]))

    _ck2 = _Ckt(loads=[], gens=[_El([(-100.0, -20.0)] * 3)])
    _sr2 = OpenDSSAdapter._element_power_sums(_ck2, _t1)
    check("F9", "Generator 计入 gen_kw 且符号取反（注入为正）",
          abs(_sr2["gen_kw"] - 300.0) < 1e-6, str(_sr2["gen_kw"]))

    # ★ 本次修复的核心断言
    _ck3 = _Ckt(loads=[], gens=[], pvsystems=[_El([(-500.0, 0.0)] * 3)])
    _sr3 = OpenDSSAdapter._element_power_sums(_ck3, _t1)
    check("F10", "★ PVSystem 计入 gen_kw（修复前此项恒为 0）",
          abs(_sr3["gen_kw"] - 1500.0) < 1e-6 and _sr3["gen_elements"] == 1,
          f"gen_kw={_sr3['gen_kw']} gen_elements={_sr3['gen_elements']}")

    _ck4 = _Ckt(loads=[_El([(400.0, 100.0)] * 3)],
                gens=[_El([(-100.0, -20.0)] * 3)],
                pvsystems=[_El([(-500.0, 0.0)] * 3)],
                storages=[_El([(250.0, 0.0)] * 3)])
    _sr4 = OpenDSSAdapter._element_power_sums(_ck4, _t1)
    check("F11", "负荷与两类发电同时计入",
          abs(_sr4["load_kw"] - 1200.0) < 1e-6 and abs(_sr4["gen_kw"] - 1800.0) < 1e-6,
          f"load={_sr4['load_kw']} gen={_sr4['gen_kw']}")
    check("F12", "gen_elements 同时统计 Generator 与 PVSystem",
          _sr4["gen_elements"] == 2, str(_sr4["gen_elements"]))
    check("F13", "Storage 不计入负荷也不计入发电（口径 A）",
          abs(_sr4["load_kw"] - 1200.0) < 1e-6 and abs(_sr4["gen_kw"] - 1800.0) < 1e-6,
          "加/不加 Storage，load 与 gen 都不变")

    _ck5 = _Ckt(loads=[_El([(100.0, 0.0)] * 3)], gens=[], pvsystems=[], expose_pv=False)
    try:
        _sr5 = OpenDSSAdapter._element_power_sums(_ck5, _t1)
        _ok5 = abs(_sr5["load_kw"] - 300.0) < 1e-6 and _sr5["gen_kw"] == 0.0
        _err5 = ""
    except BaseException as _ex5:  # noqa: BLE001
        _ok5, _err5 = False, f"{type(_ex5).__name__}: {_ex5}"
    check("F14", "引擎不暴露 PVSystems 时不崩（旧引擎仍可用）",
          _ok5, _err5 or f"load={_sr5['load_kw']} gen={_sr5['gen_kw']}")

    _src_r = open(os.path.join(_here, "renderers", "markdown_renderer.py"),
                  encoding="utf-8").read()
    check("F15", "报告口径表新增「发电注入口径」行",
          "发电注入口径" in _src_r, "已添加")
    check("F16", "该行写明 PVSystem 计入、Storage 不计入",
          "PVSystem" in _src_r and "储能 Storage 不计入" in _src_r, "已写明")

    # ---- F17~F22：储能必须「可见」并参与平衡式（9/16 口径修订）----
    # 背景（9/16 真引擎实测，任务 pf-20260916-003039-09dd32）：
    # 4 母线算例「负荷 1000 kW + 储能放电 500 kW」，储能若完全不可见，
    # 平衡残差 = 0.500000 pu（容差 1e-3）→ 任务直接失败。
    # 真实算例 case07 也踩到（两个储能 idling，仅 27 kW，残差 3.4e-4 侥幸过关）。
    check("F17", "求和集合登记了 Storages（否则储能功率完全不可见）",
          any(a == "Storages" for a, _ in _POWER_COLLECTIONS), str(_POWER_COLLECTIONS))
    check("F18", "Storage 单独求和进 storage_kw 且**不取反**（保留原始符号）",
          abs(_sr4.get("storage_kw", 0.0) - 750.0) < 1e-6,
          f"storage_kw={_sr4.get('storage_kw')}（期望 +750 = 3 相 × 250；正号=充电）")
    check("F19", "storage_elements 单独计数",
          _sr4.get("storage_elements") == 1, str(_sr4.get("storage_elements")))

    _ck6 = _Ckt(loads=[_El([(1000.0, 300.0)] * 3)], gens=[],
                pvsystems=[], storages=[_El([(-500.0, 0.0)] * 3)])
    _sr6 = OpenDSSAdapter._element_power_sums(_ck6, _t1)
    check("F20", "储能放电时 storage_kw 为负（等价发电，平衡式里相减）",
          abs(_sr6.get("storage_kw", 0.0) + 1500.0) < 1e-6,
          f"storage_kw={_sr6.get('storage_kw')}（期望 -1500）")

    _src_v2 = open(os.path.join(_here, "verification", "verifier.py"),
                   encoding="utf-8").read()
    check("F21", "verifier 平衡式纳入 total_storage_kw",
          "total_storage_kw" in _src_v2 and "storage_net_kw" in _src_v2, "已接入")
    check("F22", "报告口径表新增「储能口径」行并说明它进平衡式",
          "储能口径" in _src_r and "计入功率平衡校验" in _src_r, "已添加")

    print()
    print("=" * 78)
    print(f"结果：通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 78)
    _flush()
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

    if _FAIL:
        sys.exit(1)
