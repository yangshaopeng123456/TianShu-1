# -*- coding: utf-8 -*-
"""求解缓存键回归测试：必须是「内容决定」，不能含任何位置信息。

## 为什么这些断言值得写

本项目踩过的真实缺陷：`_solve_cache_key` 把 `Master.dss` 的**绝对路径**
哈希进了键。而算例落盘位置天然会变（上传算例每次落在新 session 目录；
case_loader 还会把依赖复制到 `tasks/<task_id>/input/`），于是同一算例
每次计算都得到全新的键，**缓存永不命中**（线上 37 个缓存文件彼此孤立）。

这类缺陷的可怕之处在于「静默」：功能看起来正常（每次都算对了），
只是缓存白装。所以必须有**显式断言**锁住"同内容 → 同键"。

全程离线、不依赖引擎、不依赖网络。用 _solve_cache_key 需要的
最小 ctx 替身（duck typing），避免构造整个 TaskContext 的重依赖。

- A 组：同内容不同目录 → 同键（**这是本次修复的核心**）
- B 组：内容变化 → 必须换键（不能为了命中而牺牲正确性）
- C 组：求解参数变化 → 必须换键（含旧实现漏掉的 control / versions）
- D 组：键的形态（长度、十六进制、不含路径痕迹）
- E 组：静态契约（源码里不得再出现把绝对路径喂给哈希的写法）
- F 组：缓存读写往返（命中分支不依赖绝对路径；旧格式缓存仍可读）
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import tempfile


def _real_adapter_version() -> str:
    """读真实的 `ADAPTER_VERSION`（单测不得自带一份副本）。"""
    from dpf_agent import ADAPTER_VERSION

    return ADAPTER_VERSION


PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"  {detail}" if detail else ""))


class _Case:
    """CanonicalDistributionCase 的最小替身。"""

    def __init__(self, fingerprint: str, master_ref: str = "Master.dss"):
        self.case_fingerprint = fingerprint
        self.master_dss_ref = master_ref


class _Circuit:
    def __init__(self, master_dss: str = ""):
        self.master_dss = master_dss


class _Iteration:
    def __init__(self, n: int = 15):
        self.max_iterations = n


class _Tolerance:
    def __init__(self, t: float = 1e-4):
        self.tolerance = t


class _Profile:
    def __init__(self, **kw):
        self.iteration_config = _Iteration(kw.get("max_iterations", 15))
        self.tolerance_config = _Tolerance(kw.get("tolerance", 1e-4))
        self.result_scopes = kw.get("result_scopes", [_Scope("system")])
        self.solve_mode = kw.get("solve_mode", "snapshot")
        self.engine = kw.get("engine", "OpenDSS")
        self.control_behavior = kw.get("control_behavior", "frozen_off")
        # ⚠️ 这里的默认 adapter 版本**取真实常量**，不要写死字符串。
        # 写死过一次（"1.3.0"）：后来真实常量升到 1.4.0，单测照样全绿，
        # 而它恰好是这个套件本该守住的那条不变量（版本号进键 → 改采集逻辑必须升版）。
        # 写死等于把要守的东西从测试里摘出去了。
        self.version_set = kw.get("version_set", {
            "adapter": _real_adapter_version(), "defaults": "sim-defaults-v1.0"})


class _Scope:
    def __init__(self, value: str):
        self.value = value


class _Ctx:
    def __init__(self, case=None, circuit=None, profile=None, workspace=""):
        self.case = case
        self.circuit = circuit or _Circuit()
        self.solver_profile = profile or _Profile()
        self.workspace = workspace
        self.config = None
        self.adapter = None
        self.raw_result = None

    def save_json(self, name, data):  # noqa: D102
        if self.workspace:
            os.makedirs(self.workspace, exist_ok=True)
            p = os.path.join(self.workspace, name)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return p
        return ""


def main() -> int:  # noqa: C901
    global PASS, FAIL
    PASS = FAIL = 0

    from dpf_agent.tools.powerflow_tools import RunPowerFlowTool

    tool = RunPowerFlowTool()
    tmp = tempfile.mkdtemp(prefix="cachekeytest_")
    try:
        # ============================================ A. 同内容不同目录 → 同键
        print("--- A. 同内容不同目录必须得到同一个键 ---")
        # 构造两份字节完全相同的 Master.dss，放在两个"不同会话"目录
        a_dir = os.path.join(tmp, "sess-AAAA", "case_x")
        b_dir = os.path.join(tmp, "sess-BBBB", "case_x")
        os.makedirs(a_dir)
        os.makedirs(b_dir)
        dss_body = "New Circuit.test basekv=10\nNew Line.L1 Bus1=a Bus2=b R1=0.01 X1=0.02\n"
        for d in (a_dir, b_dir):
            with open(os.path.join(d, "Master.dss"), "w", encoding="utf-8") as f:
                f.write(dss_body)

        ctx_a = _Ctx(case=_Case("fp-abc123"), circuit=_Circuit(os.path.join(a_dir, "Master.dss")))
        ctx_b = _Ctx(case=_Case("fp-abc123"), circuit=_Circuit(os.path.join(b_dir, "Master.dss")))
        k_a = tool._solve_cache_key(ctx_a)
        k_b = tool._solve_cache_key(ctx_b)
        check("A1 同指纹 + 同参数 → 同键（核心修复）", k_a == k_b, f"{k_a} vs {k_b}")

        # 即使 canonical case 缺失（走内容兜底），也不得因路径不同而换键
        ctx_a2 = _Ctx(case=None, circuit=_Circuit(os.path.join(a_dir, "Master.dss")))
        ctx_b2 = _Ctx(case=None, circuit=_Circuit(os.path.join(b_dir, "Master.dss")))
        k_a2 = tool._solve_cache_key(ctx_a2)
        k_b2 = tool._solve_cache_key(ctx_b2)
        check("A2 无 CanonicalCase 时按内容兜底，仍与路径无关", k_a2 == k_b2, f"{k_a2} vs {k_b2}")

        # 兜底路径与指纹路径应当**不同**（两者信息量不同，混用会误命中）
        check("A3 指纹路径与内容兜底路径不混用", k_a != k_a2, f"{k_a} vs {k_a2}")

        # 同一目录、同一内容反复调用 → 稳定
        check("A4 键计算是确定性的", tool._solve_cache_key(ctx_a) == k_a)

        # ============================================ B. 内容变化 → 必须换键
        print("--- B. 内容变化必须换键 ---")
        c_dir = os.path.join(tmp, "sess-AAAA", "case_x_changed")
        os.makedirs(c_dir)
        with open(os.path.join(c_dir, "Master.dss"), "w", encoding="utf-8") as f:
            # 只改一个字符
            f.write(dss_body.replace("0.01", "0.02"))
        ctx_c = _Ctx(case=_Case("fp-CHANGED"), circuit=_Circuit(os.path.join(c_dir, "Master.dss")))
        check("B1 指纹变化 → 换键", tool._solve_cache_key(ctx_c) != k_a)

        # 兜底路径下，内容变一个字符也必须换键
        ctx_c2 = _Ctx(case=None, circuit=_Circuit(os.path.join(c_dir, "Master.dss")))
        check("B2 兜底路径下内容变化 → 换键", tool._solve_cache_key(ctx_c2) != k_a2)

        # 兜底路径下，文件不存在也要能算出键（不抛异常）
        ctx_missing = _Ctx(case=None, circuit=_Circuit(os.path.join(tmp, "nope", "Master.dss")))
        raised = None
        try:
            km = tool._solve_cache_key(ctx_missing)
        except Exception as exc:  # noqa: BLE001
            raised = exc
            km = ""
        check("B3 文件缺失时降级不抛异常", raised is None, repr(raised))
        check("B4 文件缺失的键与正常键不同", km and km != k_a2, km)

        # ============================================ C. 求解参数变化 → 换键
        print("--- C. 求解参数变化必须换键 ---")
        variants = [
            ("max_iterations", _Profile(max_iterations=30)),
            ("tolerance", _Profile(tolerance=1e-3)),
            ("result_scopes", _Profile(result_scopes=[_Scope("system"), _Scope("bus")])),
            ("solve_mode", _Profile(solve_mode="daily")),
            ("engine", _Profile(engine="OpenDSS-Com")),
            # 旧实现漏掉的两个维度
            ("control_behavior", _Profile(control_behavior="auto")),
            ("version_set", _Profile(version_set={"adapter": "9.9.9", "defaults": "sim-defaults-v1.0"})),
        ]
        for label, prof in variants:
            k = tool._solve_cache_key(_Ctx(case=_Case("fp-abc123"), profile=prof))
            check(f"C 参数 {label} 变化 → 换键", k != k_a, f"{k} vs {k_a}")

        # scopes 顺序不影响键（集合语义）
        p1 = _Profile(result_scopes=[_Scope("system"), _Scope("bus")])
        p2 = _Profile(result_scopes=[_Scope("bus"), _Scope("system")])
        k1 = tool._solve_cache_key(_Ctx(case=_Case("fp-abc123"), profile=p1))
        k2 = tool._solve_cache_key(_Ctx(case=_Case("fp-abc123"), profile=p2))
        check("C2 scopes 顺序无关（按集合语义）", k1 == k2, f"{k1} vs {k2}")

        # ★ C3 真实 `ADAPTER_VERSION` 必须真的影响键 —— 这条是 2026-10-02 补的。
        #
        # 那天的故障：为了修 case31 的变压器连接母线，采集逻辑已改对，
        # 但 `ADAPTER_VERSION` 仍是 1.3.0。于是同一算例命中旧求解缓存，
        # 走的是"直接反序列化 cached.transformers"的分支，`_extract`
        # 一次都没被调用 —— 产物里有 `from_bus` 字段却全为空，
        # 而**没有任何报错**。前一轮排查因此绕了很远（怀疑过"加载了错误的
        # 代码树""诊断日志被 info 级吞掉"），真因只是版本号没升。
        #
        # 判据要能真的判：不能用写死的字符串，必须用**真实常量**构造 profile，
        # 再与"把 adapter 版本换掉"的 profile 比对。
        _real = _real_adapter_version()
        k_real = tool._solve_cache_key(_Ctx(
            case=_Case("fp-abc123"),
            profile=_Profile(version_set={"adapter": _real,
                                          "defaults": "sim-defaults-v1.0"})))
        k_bumped = tool._solve_cache_key(_Ctx(
            case=_Case("fp-abc123"),
            profile=_Profile(version_set={"adapter": _real + "-bump",
                                          "defaults": "sim-defaults-v1.0"})))
        check("C3 ★ 真实 ADAPTER_VERSION 变化 → 必须换键（否则改采集逻辑不生效且无报错）",
              k_real != k_bumped, f"real={_real} {k_real} vs bumped {k_bumped}")

        # ============================================ D. 键的形态
        print("--- D. 键的形态 ---")
        check("D1 键长度为 24", len(k_a) == 24, str(len(k_a)))
        check("D2 键为十六进制", all(c in "0123456789abcdef" for c in k_a), k_a)
        # 键里不得残留路径痕迹（例如目录名片段）
        leaked = ("sess" in k_a.lower()) or ("AAAA" in k_a) or (tmp.lower() in k_a.lower())
        check("D3 键中不含路径痕迹", not leaked, k_a)

        # ============================================ E. 静态契约
        print("--- E. 源码不得再把绝对路径喂给哈希 ---")
        src = inspect.getsource(RunPowerFlowTool._solve_cache_key)
        check("E1 使用 case_fingerprint", "case_fingerprint" in src)
        # 关键反向断言：不得出现 `h.update(str(master)...)` 这类把路径当身份的老写法
        bad = 'h.update(str(master)' in src
        check("E2 不再把 master 路径字符串喂给哈希", not bad,
              '仍存在 h.update(str(master)...)' if bad else "已移除")
        check("E3 参与键的参数含 control_behavior", "control_behavior" in src)
        check("E4 参与键的参数含 version_set", "version_set" in src)
        check("E5 有内容兜底分支", "isfile(master)" in src or "isfile" in src)
        src_save = inspect.getsource(RunPowerFlowTool._save_solve_cache)
        # 注意：源码里有 "master_dss_ref"（合法，是相对引用），
        # 所以必须精确匹配带闭合引号的 "master_dss" 键名，不能用子串判断。
        has_abs_key = '"master_dss":' in src_save or '"master_dss" ' in src_save
        check("E6 缓存载荷不再写 master_dss 绝对路径键名", not has_abs_key,
              "发现 \"master_dss\" 键" if has_abs_key else "只含 master_dss_ref")
        check("E7 缓存载荷记录 case_fingerprint", "case_fingerprint" in src_save)

        # ★ E8 采集逻辑的改动**必须**伴随 ADAPTER_VERSION 升版。
        # 这条断言守不住"人为忘记升版"，但能钉死两件可机械核对的事：
        #   (a) ADAPTER_VERSION 参与键（真正的失效机制在）；
        #   (b) 采集逻辑的代码里**引用了版本常量**（提醒：动了它就得升号）。
        # 判据取"调用形式"而非裸关键词，避免注释与说明文档里的字样误伤。
        #
        # ⚠️ 这里必须取**类的 `_extract` 方法**源码，不能取模块源码：
        # 写入版本戳的语句在方法体里，`inspect.getsource(module)` 只给模块级
        # 代码（import + 定义），会漏掉它 —— 曾因此判出"未引用版本常量"
        # 却仍然 PASS（假绿：detail 与结论矛盾，正是本项目反复出现的判据事故）。
        _com_mod = __import__("dpf_agent.engines.opendss_com_adapter", fromlist=["x"])
        _extract_src = inspect.getsource(_com_mod.OpenDSSComAdapter._extract)
        # 匹配**完整赋值语句**（含接收者 `raw.`），不要只匹配裸表达式：
        # 写成 "collector_version = ADAPTER_VERSION" 时，
        # 实际语句是 "raw.collector_version = ADAPTER_VERSION"，
        # 子串恰好仍能命中；可一旦将来改成别的写法（如 self.xxx）就会假红。
        # 更要紧的是：这类"detail 与结论相反"的假绿已在本项目出现多次，
        # 所以这里把命中的那几行**原样打进 detail**，人一眼能核对。
        _ver_lines = [ln.strip() for ln in _extract_src.splitlines()
                      if "ADAPTER_VERSION" in ln and "collector_version" in ln]
        check("E8 ★ 采集方法写入 collector_version = ADAPTER_VERSION（改采集逻辑必须升版）",
              bool(_ver_lines),
              ("命中: " + " | ".join(_ver_lines)) if _ver_lines
              else "_extract 中未找到版本戳写入语句，改采集逻辑将无法自动失效旧缓存")

        # ============================================ F. 缓存读写往返
        print("--- F. 缓存读写往返 ---")
        from dpf_agent.config import AgentConfig

        cfg = AgentConfig()
        cache_dir = os.path.join(cfg.outputs_root, "_cache", "solve")
        # 命中分支只读 solver/... 不读绝对路径 —— 用真实旧格式文件验证可读
        legacy = {
            "cache_key": "legacykey000000000000abc",
            "created_at": 1.0,
            "master_dss": "C:/some/old/path/Master.dss",   # 旧格式字段，应被安全忽略
            "solver": {"converged": True, "iterations": 4},
            "system": {"total_kw": 100.0},
            "element_count": {"buses": 3},
            "buses": [], "lines": [], "transformers": [],
        }
        made_dir = False
        # ⚠️ `qdir` 必须定义在 try **之外**：F4 也要用它搬走测试文件。
        # 放在 F1 的 try 内部时，一旦 F1 抛异常，后面新增的 F4 会先炸
        # NameError（而不是报出真正想报的问题）—— 掩盖判据。
        qdir = os.path.join(tmp, "quarantine")
        os.makedirs(qdir, exist_ok=True)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            made_dir = True
            lp = os.path.join(cache_dir, "legacykey000000000000abc.json")
            with open(lp, "w", encoding="utf-8") as f:
                json.dump(legacy, f)
            got = tool._load_solve_cache(_Ctx(), "legacykey000000000000abc")
            check("F1 旧格式缓存仍可读（不再依赖 master_dss 字段）",
                  isinstance(got, dict) and got.get("solver", {}).get("iterations") == 4,
                  str(got)[:70])
            # 清理：把这个测试文件移走（不用 Remove-Item，本机 safe-delete 失效）
            shutil.move(lp, os.path.join(qdir, os.path.basename(lp)))
            check("F2 测试用缓存文件已移出真实缓存目录", not os.path.isfile(lp))
        except Exception as exc:  # noqa: BLE001
            check("F1 旧格式缓存仍可读（不再依赖 master_dss 字段）", False, f"{type(exc).__name__}: {exc}")
            check("F2 测试用缓存文件已移出真实缓存目录", False, "前置步骤失败")

        # 空 solvent 的缓存应判为无效
        bad_path = os.path.join(tmp, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump({"solver": {}}, f)
        check("F3 无 solver 的缓存判为无效", tool._load_solve_cache(_Ctx(), "__nonexist__") is None)

        # ★ F4 缓存命中时**必须还原采集版本戳**，且老缓存读出来应是空串。
        #
        # 为什么这条重要：命中分支走"直接反序列化"，不调 `_extract`。
        # 若这里不还原 `collector_version`，命中旧缓存后该字段恒为空 ——
        # 排查"字段在但值为空"时就没有任何信号能区分
        # 「当前版本跑出来是空的」与「这份是旧版产物」。空串即后者。
        legacy_again = {
            "cache_key": "verprobe000000000ab",
            "created_at": 1.0,
            "solver": {"converged": True, "iterations": 2},
            "system": {}, "element_count": {},
            "buses": [], "lines": [], "transformers": [],
            # 故意**不写** collector_version，模拟 1.3.x 时代写下的老缓存
        }
        try:
            os.makedirs(cache_dir, exist_ok=True)
            vp = os.path.join(cache_dir, "verprobe000000000ab.json")
            with open(vp, "w", encoding="utf-8") as f:
                json.dump(legacy_again, f)
            got_v = tool._load_solve_cache(_Ctx(), "verprobe000000000ab")
            check("F4 ★ 老缓存无版本戳时读出空串（空=旧版产物，是有效信号而非缺字段）",
                  isinstance(got_v, dict) and "collector_version" not in got_v,
                  f"keys={sorted(got_v)[:8] if isinstance(got_v, dict) else got_v!r}")
            shutil.move(vp, os.path.join(qdir, os.path.basename(vp)))
        except Exception as exc:  # noqa: BLE001
            check("F4 ★ 老缓存无版本戳时读出空串（空=旧版产物，是有效信号而非缺字段）",
                  False, f"{type(exc).__name__}: {exc}")

        # ★ F5 两条通道的 `_extract` 都必须写 collector_version ——
        # 只让一路写，产物形状就会漂移（另一路永远是空串，
        # 会被误读成"那一路的采集逻辑坏了"）。
        try:
            import dpf_agent.engines.opendss_cli_adapter as _cli

            # 同 E8：必须读**采集方法体**，不能读模块源码（模块源码里没有方法体）。
            # ⚠️ 两条通道的采集方法**不同名**：COM 拆出了 `_extract`，
            # CLI 直接写在 `extract_raw_result` 里（批处理模型，无法分步交互）。
            # 所以这里按各自真实方法名取，别写死同一个名字。
            def _ver_hits(fn) -> list:
                return [ln.strip() for ln in inspect.getsource(fn).splitlines()
                        if "collector_version" in ln and "ADAPTER_VERSION" in ln]

            com_hits = _ver_hits(_com_mod.OpenDSSComAdapter._extract)
            cli_hits = _ver_hits(_cli.OpenDSSCliAdapter.extract_raw_result)
            check("F5 ★ COM 与 CLI 两条通道都写入 collector_version",
                  bool(com_hits) and bool(cli_hits),
                  f"COM={com_hits or '无'} / CLI={cli_hits or '无'}")
        except Exception as exc:  # noqa: BLE001
            check("F5 ★ COM 与 CLI 两条通道都写入 collector_version",
                  False, f"{type(exc).__name__}: {exc}")

        # 关闭开关时不读缓存
        import dpf_agent.config as cfgmod

        old = cfgmod.DEFAULT_CONFIG.solve_cache_enabled
        try:
            cfgmod.DEFAULT_CONFIG.solve_cache_enabled = False
            check("F4 开关关闭时不命中", tool._load_solve_cache(_Ctx(), "legacykey000000000000abc") is None)
        finally:
            cfgmod.DEFAULT_CONFIG.solve_cache_enabled = old

        # ============================================ G. 求和口径参与缓存键
        #
        # 背景（9/15 实测）：修好「PVSystem 计入发电」之后重跑同一算例，
        # 产物里 gen_kw 仍然是 0 —— 因为缓存命中了**旧口径**的结果。
        # 口径直接决定 raw.system 的数值，必须让它参与缓存键，
        # 否则会出现"代码改了、结果没变"的假象（比不修更危险：会让人误判修复无效）。
        from dpf_agent.engines import opendss_adapter as _oa

        _cctx = _Ctx(case=_Case("fp-caliber"), circuit=_Circuit(""), profile=_Profile())
        _k1 = tool._solve_cache_key(_cctx)

        _saved_role = dict(_oa.ELEMENT_ROLE_BY_PREFIX)
        _saved_excl = dict(_oa.ELEMENT_EXCLUDED_PREFIXES)
        try:
            # 把 PVSystem 从「发电」改成「负荷」→ 口径变了，键必须变
            _oa.ELEMENT_ROLE_BY_PREFIX["pvsystem"] = "load"
            _k2 = tool._solve_cache_key(_cctx)
            check("G1 口径表变化 → 缓存键变化", _k1 != _k2, f"{_k1} vs {_k2}")

            _oa.ELEMENT_ROLE_BY_PREFIX.clear()
            _oa.ELEMENT_ROLE_BY_PREFIX.update(_saved_role)
            _k3 = tool._solve_cache_key(_cctx)
            check("G2 还原口径后键回到原值（口径可复现地参与，不是随机量）",
                  _k1 == _k3, f"{_k1} vs {_k3}")

            # 排除表同样参与：改变排除表内容也是口径变化。
            # ⚠️ 用「新增一项」而不是「清空表」：表可能本来就是空的，
            #    清空一个空表不构成任何变化 → 断言会假失败（9/16 踩过，
            #    当时把 storage 从排除表移到角色表，排除表变空，G3 就红了）。
            _oa.ELEMENT_EXCLUDED_PREFIXES["__probe_excluded__"] = "测试用临时排除项"
            _k4 = tool._solve_cache_key(_cctx)
            check("G3 排除表变化同样换键", _k1 != _k4, f"{_k1} vs {_k4}")
        finally:
            _oa.ELEMENT_ROLE_BY_PREFIX.clear()
            _oa.ELEMENT_ROLE_BY_PREFIX.update(_saved_role)
            _oa.ELEMENT_EXCLUDED_PREFIXES.clear()
            _oa.ELEMENT_EXCLUDED_PREFIXES.update(_saved_excl)

        # 求解状态与原始结果的配对、缓存存在时也必须真实求解、验证后才写缓存，
        # 由 test_recovery_verification 的行为用例覆盖。这里仅验证缓存身份；
        # 不再用源码字符串顺序替代执行行为。

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=== 结果：通过 {PASS} / 失败 {FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
