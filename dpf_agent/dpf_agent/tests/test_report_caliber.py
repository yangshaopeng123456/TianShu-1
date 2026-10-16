"""口径声明回归测试（自包含，不需要引擎）。

锁住三件事，防止"静默改变口径"复发：

1. **能力回答不得否认已实现的能力**。历史缺陷：系统明明按三相求解并保留分相结果，
   却对外答「三相不平衡潮流求解（不能做）」——这是对外说错话。
2. **报告必须声明本次求解口径**：三相建模方式、控制行为、负荷模型（含 OpenDSS
   默认 Vminpu=0.95 的隐性转恒阻抗）、无功限值是否施加、限值来源、未包含项。
3. **转换类算例必须显式声明"平衡三相等值"**，不能与原生 .dss 的结果混为一谈。

运行：python -m dpf_agent.tests.test_report_caliber
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)))

from dpf_agent.config import AgentConfig  # noqa: E402
from dpf_agent.domain.case.case_loader import load_case_bundle  # noqa: E402
from dpf_agent.domain.network_profile import NetworkProfile  # noqa: E402
from dpf_agent.engines.opendss_adapter import (  # noqa: E402
    OpenDSSRawResult,
    SolverExecutionResult,
)
from dpf_agent.schemas.enums import ResultScope, TaskStatus  # noqa: E402
from dpf_agent.schemas.solver import OpenDSSSolverProfile  # noqa: E402
from dpf_agent.schemas.task_spec import PowerFlowTaskSpec  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))


# 最小可编译算例：一个源 + 一个负荷 + 一条线路。仅用于报告渲染，不调用引擎。
NATIVE_DSS = """Clear
New Circuit.demo basekv=10 pu=1.0 phases=3
Set DefaultBaseFrequency=50
New Line.L1 Bus1=b1 Bus2=b2 R1=0.01 X1=0.02 Length=1 Units=none Phases=3
New Load.LD1 Bus1=b2 Phases=3 kW=100 kvar=30
"""

# 转换类算例：头部带转换器标记（与 case_converters 实际写法一致）
CONVERTED_DSS = """Clear
! [Tianshu Conversion] source_format=case_json; approximation=true
! [Tianshu Conversion] warning=原始表格/矩阵已转换为平衡三相 OpenDSS 等值模型。
! [Tianshu Conversion] enforce_q_limits=0
Set DefaultBaseFrequency=60
New Circuit.demo basekv=10 pu=1.04 phases=3
New Line.L1 Bus1=b1 Bus2=b2 R1=0.01 X1=0.02 Length=1 Units=none Phases=3
New Load.LD1 Bus1=b2 Phases=3 kW=100 kvar=30
"""


def _make_case(tmp: str, name: str, content: str):
    src_dir = os.path.join(tmp, f"src_{name}")
    os.makedirs(src_dir, exist_ok=True)
    master = os.path.join(src_dir, "Master.dss")
    with open(master, "w", encoding="utf-8") as f:
        f.write(content)
    ws = os.path.join(tmp, f"ws_{name}")
    os.makedirs(ws, exist_ok=True)
    cfg = AgentConfig()
    return load_case_bundle(master, ws, cfg.cases_root), cfg


def _demo_result(task_id: str):
    """构造一个含分相数据的最小结果对象（不经引擎）。"""
    from dpf_agent.domain.result_normalizer import normalize_powerflow_result

    raw = OpenDSSRawResult()
    raw.solver = {"converged": True, "iterations": 3, "mode": "snapshot"}
    raw.system = {"total_power_kw": 303.0, "total_power_kvar": 96.0}
    raw.element_count = {"bus": 2, "line": 1, "load": 1}
    raw.buses = [
        {"name": "b1", "base_kv": 10.0, "voltage_pu": [1.0, 1.0, 1.0], "angle_deg": [0.0, -120.0, 120.0]},
        # 明显不平衡：B 相偏低
        {"name": "b2", "base_kv": 10.0, "voltage_pu": [0.96, 0.90, 0.95], "angle_deg": [0.0, -118.0, 121.0]},
    ]
    raw.lines = []
    raw.transformers = []
    return normalize_powerflow_result(
        raw,
        SolverExecutionResult(True, 3, False, "", 0.05),
        task_id,
        "fp-demo",
        "",
        "OpenDSS 11",
        "com-test",
        [ResultScope.BUS, ResultScope.BRANCH, ResultScope.SYSTEM],
    )


def main() -> int:
    cfg = AgentConfig()
    tmp = tempfile.mkdtemp(prefix="caliber_")

    # ---------- ① 能力回答：不得否认已实现的能力 ----------
    from dpf_agent.web import chat_api as ca

    ans = ca._answer_capability_inquiry("你能处理三相不平衡吗", {}) or ""
    check("① 能力回答明确写出「能做」", "能做" in ans, ans[:80])
    for banned in ("不能做", "做不到", "不执行完整三相不平衡"):
        check(f"① 能力回答不再出现「{banned}」", banned not in ans, "")
    check("① 能力回答给出不平衡度定义（对称分量法）", "对称分量" in ans and "U₂" in ans)
    check("① 能力回答声明限值待核对", "GB/T 15543" in ans and "核对" in ans)
    check("① 能力回答区分原生/转换算例", "原生" in ans and "转换" in ans)
    # 通用能力清单里也不得把三相不平衡列为「不做」。
    # 注意：只取「暂不支持」段内的条目（到第一个空行为止）——
    # 「能做」段是刻意包含三相不平衡的（说明它能算）。
    # 段落标题随能力清单改版过（原「目前**不能**做」→ 现「**暂不支持**（不会伪装）」），
    # 断言的**语义**不变：三相不平衡属于"能做"那一栏。
    generic = ca._answer_capability_inquiry("你有什么能力", {}) or ""
    cannot_block: list[str] = []
    in_cannot = False
    for line in generic.splitlines():
        if line.startswith("**暂不支持**"):
            in_cannot = True
            continue
        if in_cannot:
            if not line.strip():
                break
            cannot_block.append(line)
    cannot_text = "\n".join(cannot_block)
    check("① 通用清单「暂不支持」段不再含三相不平衡",
          cannot_block and "三相不平衡" not in cannot_text,
          cannot_text.replace("\n", " | ")[:120])
    check("① 通用清单「暂不支持」段仍有内容（解析有效）", len(cannot_block) >= 3,
          f"{len(cannot_block)} 条")
    # 清单用**条目标题**列举（标题写的是"三相不平衡（分相建模与不对称度）"），
    # 所以这里查标题里的特征词，而不是正文里的"不平衡度"字样。
    _supported_text = generic.split("**暂不支持**")[0]
    check("① 通用清单「能做」段含分相与三相不平衡",
          "三相不平衡" in _supported_text and "分相" in _supported_text,
          _supported_text[-120:].replace("\n", " | "))

    # ---------- ② 报告口径声明 ----------
    from dpf_agent.renderers.markdown_renderer import render_markdown

    from dpf_agent.schemas.enums import OutputFormat

    res = _demo_result("pf-demo-caliber")
    ts = PowerFlowTaskSpec(
        task_id="pf-demo-caliber",
        case_ref="demo",
        user_goal="口径测试",
        result_scopes=list(res.bus_results and [ResultScope.BUS, ResultScope.BRANCH, ResultScope.SYSTEM]),
        output_formats=[OutputFormat.MD],
    )
    sp = OpenDSSSolverProfile(profile_id="pf-demo-caliber-profile")
    sp.freeze()

    native_case, _ = _make_case(tmp, "native", NATIVE_DSS)
    conv_case, _ = _make_case(tmp, "conv", CONVERTED_DSS)

    check("② 原生算例保真度标记为 native", native_case.fidelity_status == "native",
          native_case.fidelity_status)
    check("② 转换算例保真度标记为 warning", conv_case.fidelity_status == "warning",
          conv_case.fidelity_status)
    check("② 转换算例解析出 source_format", conv_case.source_format == "case_json",
          conv_case.source_format)
    check("② 转换算例解析出 enforce_q_limits=0",
          str((conv_case.source_metadata or {}).get("enforce_q_limits")) == "0",
          str(conv_case.source_metadata))
    check("② 转换算例解析出频率 60 Hz",
          (conv_case.source_metadata or {}).get("frequency_hz") == 60.0,
          str(conv_case.source_metadata))

    md_native = render_markdown(ts, native_case, sp, res, None, [], TaskStatus.SUCCESS,
                                cfg.defaults, explanation="")
    md_conv = render_markdown(ts, conv_case, sp, res, None, [], TaskStatus.SUCCESS,
                              cfg.defaults, explanation="")

    for label, md in (("原生", md_native), ("转换", md_conv)):
        check(f"②{label} 报告含「本次求解口径声明」", "本次求解口径声明" in md)
        for key in ("求解模式", "三相建模", "控制行为", "负荷模型", "总负荷口径",
                    "发电机无功", "不平衡度定义", "不平衡限值", "未包含"):
            check(f"②{label} 口径表含「{key}」", f"| {key} |" in md)
        check(f"②{label} 声明 OpenDSS 默认 Vminpu 隐性转恒阻抗", "Vminpu=0.95" in md)
        # 总负荷口径必须写明是「元件求和」，且点明与源侧供电的区别。
        # 缺这一行，读者会把"总负荷"当成源侧供电（含发电时两者差一个发电量）。
        check(f"②{label} 声明总负荷为元件求和",
              "各负荷元件端子1有功之和" in md)
        check(f"②{label} 声明总负荷不等于源侧供电",
              "**不等于**源侧供电" in md)
        check(f"②{label} 声明含发电时另列净负荷",
              "净负荷" in md)
        check(f"②{label} 限值声明为待核对参考值", "GB/T 15543" in md and "待与" in md)
        check(f"②{label} 给出两个电压口径说明", "真实最低相电压" in md and "平衡等值最低电压" in md)
        check(f"②{label} 能力边界不再写「三相不平衡未启用」",
              "三相不平衡未启用" not in md and "三相不平衡已启用" in md)

    check("② 原生报告声明「原生分相模型」", "原生分相模型" in md_native)
    check("② 原生报告不出现转换警告", "转换而来，原始相别信息未保留" not in md_native)
    check("② 转换报告声明「平衡三相等值模型」", "平衡三相等值模型" in md_conv)
    check("② 转换报告给出转换警告", "不应直接视为原始求解器的等价复现" in md_conv)
    check("② 转换报告频率按算例写 60 Hz", "| 系统频率 | 60 Hz |" in md_conv)
    check("② 原生报告频率写 50 Hz", "| 系统频率 | 50 Hz |" in md_native)
    # 表格单元格内不得出现半角竖线（会破坏 markdown 表格列数）
    caliber = md_native.split("本次求解口径声明")[1].split("## 4.")[0]
    bad_rows = [
        ln for ln in caliber.splitlines()
        if ln.startswith("|") and ln.count("|") not in (3,)
    ]
    check("② 口径表列数一致（ε 公式未用半角竖线）", not bad_rows, str(bad_rows[:2]))

    # ---------- ②b 三相建模三态（原生 / 按输入相别 / 平衡等值）----------
    # 这三态一旦混淆，报告就会把"平衡等值"说成"按相别建模"（反之亦然），
    # 属于最典型的"口径与实现不一致"，必须锁住。
    phase_dss = (
        "Clear\n"
        "! [Tianshu Conversion] source_format=case_xlsx; approximation=true\n"
        "! [Tianshu Conversion] warning=相别信息已按输入建模\n"
        "! [Tianshu Conversion] phase_model=has_phase_columns=1; unbalanced_input=1; "
        "elements_with_phase=3; assumed_three_phase=0; single_or_partial_elements=2\n"
        "Set DefaultBaseFrequency=50\n"
        "New Circuit.demo basekv=10 pu=1.0 phases=3\n"
        "New Load.LD1 Bus1=b2.2 Phases=1 kV=5.77 kW=100 kvar=30\n"
    )
    phase_case, _ = _make_case(tmp, "phase", phase_dss)
    check("②b 按相别建模：保真度警告称「相别信息已按输入建模」",
          any("相别信息已按输入建模" in w for w in phase_case.fidelity_warnings),
          " | ".join(phase_case.fidelity_warnings)[:120])
    check("②b 按相别建模：不再声称「相别不平衡未保留」",
          not any("相别不平衡" in w for w in phase_case.fidelity_warnings))
    check("②b 按相别建模：source_metadata 记录 unbalanced_input",
          str((phase_case.source_metadata or {}).get("unbalanced_input")) == "1",
          str(phase_case.source_metadata))
    md_phase = render_markdown(ts, phase_case, sp, res, None, [], TaskStatus.SUCCESS,
                               cfg.defaults, explanation="")
    check("②b 报告口径表写「按输入相别建模」", "按输入相别建模" in md_phase)
    check("②b 报告列出单相/两相元件数", "单相/两相元件 2 个" in md_phase)
    check("②b 报告警告说明相别已建模", "相别信息已按输入建模" in md_phase)

    # 三态互斥：原生不说「平衡等值」，平衡等值不说「按输入相别建模」
    check("②b 三态互斥（原生 ≠ 平衡等值）",
          "平衡三相等值模型" not in md_native and "按输入相别建模" not in md_native)
    check("②b 三态互斥（平衡等值 ≠ 按相别建模）",
          "平衡三相等值模型" in md_conv and "按输入相别建模" not in md_conv)

    # ---------- ③ 结构性不平衡标记 ----------
    p1 = NetworkProfile(case_id="x")
    check("③ 新增字段默认关闭（历史产物可加载）", p1.has_single_phase_elements is False)

    import glob

    loaded = failed = 0
    for path in glob.glob(os.path.join(cfg.outputs_root, "pf-*", "network_profile.json")):
        try:
            NetworkProfile(**json.load(open(path, encoding="utf-8")))
            loaded += 1
        except Exception:  # noqa: BLE001
            failed += 1
    check("③ 历史 network_profile.json 全部可加载", failed == 0,
          f"成功 {loaded} / 失败 {failed}")

    # ---------- ④ 早期失败路径：result 为 None 时报告必须还能渲染 ----------
    #
    # 背景（9/16 实测）：任务在 compile 校验阶段就被拒绝时，`ctx.canonical_result`
    # 恒为 None。报告渲染曾假设它非空 —— 第 8 节（三相不平衡）直接读
    # `result.unbalance_summary`，抛 AttributeError，整个请求降级成 HTTP 500，
    # 前端只看到"服务器错误"，拿不到任何失败原因。
    # 触发场景：坏引用算例被校验规则**正确拒绝**，却回了 500。
    # 第 4~7 节本来有 `if result is not None` 保护，第 8 节漏了。
    # 用**别名**导入：函数内的 import 会让该名字在整个函数作用域内变成本地变量，
    # 若与文件顶部的同名导入冲突，会在此前的使用点抛 UnboundLocalError（踩过）。
    from dpf_agent.renderers.markdown_renderer import render_markdown as _render_md
    from dpf_agent.schemas.case import CanonicalDistributionCase as _CDCase
    from dpf_agent.schemas.enums import TaskStatus as _TS
    from dpf_agent.schemas.solver import OpenDSSSolverProfile as _OSProfile
    from dpf_agent.schemas.task_spec import PowerFlowTaskSpec as _PFTS

    _ts = _PFTS(task_id="render-probe", case_ref="probe.dss",
                result_scopes=[], output_formats=[])
    _case = _CDCase(
        case_id="unknown", case_fingerprint="", master_dss_ref="probe.dss",
        dependency_manifest={"master_dss": "", "entries": []},
    )
    _prof = _OSProfile(profile_id="n/a")
    _err_msg = "OpenDSS Compile 失败: 发现被元件使用但缺少电压基准的母线：nosuchbus"
    _md = ""
    _err = ""
    try:
        _md = _render_md(
            _ts, _case, _prof, None, None, [], _TS.VALIDATION_FAILED,
            cfg.defaults, error={"code": "VERIFICATION_FAILED", "message": _err_msg},
        )
        _ok = True
    except BaseException as exc:  # noqa: BLE001
        _ok, _err = False, f"{type(exc).__name__}: {exc}"
    check("④ result 为 None 时渲染不抛异常（早期失败路径）", _ok, _err)
    check("④b 报告里带出失败原因（不是空白页）",
          _ok and "缺少电压基准" in _md, "" if _ok else _err)
    check("④c 三相不平衡章节显式标注无结果",
          _ok and "无法评估三相不平衡" in _md, "" if _ok else _err)
    check("④d 报告仍保留前 3 节（任务/算例/配置）",
          _ok and "## 1. 任务摘要" in _md and "## 3. 求解配置与版本" in _md,
          "" if _ok else _err)

    # 静态契约：service._render 必须把 render_markdown 包在 try 内。
    # 本方法的既有约定是「渲染失败降级为 REPORT_ERROR + 保留已验证 JSON 产物」
    # （文档 9.4），render_csv 也确实在 try 里；只有 render_markdown 曾漏在外面。
    _pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _svc = open(os.path.join(_pkg, "application", "service.py"), encoding="utf-8").read()
    _i_def = _svc.find("def _render")
    _i_call = _svc.find("md_text = render_markdown(", _i_def)
    _i_try = _svc.rfind("try:", _i_def, _i_call)
    check("④e service._render 把 render_markdown 包在 try 内（渲染失败不冒泡成 500）",
          0 < _i_def < _i_try < _i_call, f"def@{_i_def} try@{_i_try} call@{_i_call}")

    # ---------- ⑤ 请求门能力口径：已实现的能力必须放行，未启用的必须仍拦住 ----------
    # 背景（2026-09-18 实测）：分相建模与不对称度量早已实现，聊天层答「能做」、
    # 报告口径写「已启用」，但请求门 `_FORBIDDEN_CAPABILITY_KEYWORDS` 仍含「三相」「不平衡」，
    # 于是用户只要在指令里点名这两个词，就被判 CAPABILITY_UNAVAILABLE 拒绝执行
    # ——「能力在，但话里点名反而算不了」，属能力口径与实现不一致。
    #
    # 这条断言必须**双向**：既证明已实现的能力不再被拦，也证明门没被拆掉。
    # 只断言"能过"是不够的：把整个关键词表清空也能过。
    # ⚠️ 全部用别名导入：本函数在更早处已用到 `PowerFlowTaskSpec` 等模块级名字，
    # 直接 `import` 同名会把它变成局部变量 → UnboundLocalError（此坑已踩过一次，见 2.1 节）。
    from dpf_agent.runtime.guard import (
        ENABLED_CAPABILITIES as _ENABLED_CAPS,
        GuardRejection as _GuardRejection,
        RuleGuard as _RuleGuard,
        _FORBIDDEN_CAPABILITY_KEYWORDS as _FORBIDDEN_KW,
    )
    from dpf_agent.schemas.enums import CAPABILITY_STATUS as _CAP_STATUS
    from dpf_agent.schemas.enums import Capability as _Capability
    from dpf_agent.schemas.task_spec import PowerFlowTaskSpec as _PFTaskSpec

    for _kw in ("三相", "不平衡"):
        check(f"⑤ 「{_kw}」已从禁用关键词表移出",
              _kw not in _FORBIDDEN_KW, str(list(_FORBIDDEN_KW)))
    check("⑤ 三相不平衡已列入 ENABLED_CAPABILITIES",
          _Capability.THREE_PHASE_UNBALANCED in _ENABLED_CAPS)
    _st = _CAP_STATUS[_Capability.THREE_PHASE_UNBALANCED]
    check("⑤ 三相不平衡状态不再是 later_phase / research_only",
          _st not in ("later_phase", "research_only"), _st)

    _guard = _RuleGuard()

    def _probe_spec(goal: str) -> "_PFTaskSpec":
        return _PFTaskSpec(task_id="caliber-probe", case_ref="ieee33",
                           user_goal=goal, result_scopes=[], output_formats=[])

    for _msg in ("计算这个三相不平衡算例", "帮我算一下不平衡度", "看看三相电压分布",
                 "算这个算例，重点看三相不平衡和最低电压"):
        try:
            _guard.check_request(_probe_spec(_msg), _msg)
            check(f"⑤ 请求门放行「{_msg}」", True)
        except _GuardRejection as _exc:
            check(f"⑤ 请求门放行「{_msg}」", False,
                  f"{getattr(_exc.code, 'value', _exc.code)}: {_exc.message}")

    # 反向守卫：真正未启用的能力必须仍然被拒，否则等于把门拆了
    for _msg, _expect in (("跑一下概率潮流", "FIDELITY_NOT_APPROVED"),
                          ("做一个数据驱动的潮流预测", "FIDELITY_NOT_APPROVED"),
                          ("自动分接头调压", "CAPABILITY_UNAVAILABLE"),
                          ("做无功补偿控制", "CAPABILITY_UNAVAILABLE"),
                          ("拓扑重构后的潮流", "CAPABILITY_UNAVAILABLE"),
                          ("削减负荷方案", "CAPABILITY_UNAVAILABLE"),
                          ("重调度方案", "CAPABILITY_UNAVAILABLE")):
        try:
            _guard.check_request(_probe_spec(_msg), _msg)
            check(f"⑤ 未启用能力仍被拒「{_msg}」", False, "被放行了")
        except _GuardRejection as _exc:
            _got = getattr(_exc.code, "value", _exc.code)
            check(f"⑤ 未启用能力仍被拒「{_msg}」", _got == _expect,
                  f"{_got} ≠ {_expect}")

    # ---------- ⑥ 历史问答：设备表结构与分派优先级（2026-09-29 实测挖出的两处静默失效） ----------
    # 缺陷一：`_DEVICE_MAP` 原为 **5 元组**（多一列重构前遗留的「关键词」），而
    # `_h_device_counts` 的整表分支与 `_h_case_statistics` 按 **4 元组**解包，
    # 于是「无 device 槽位」时必然抛 `ValueError`；该异常被 `answer_by_metric`
    # 的兜底 `except` 吞掉，对外表现为「上一次的结果产物里没有这项数据」——
    # **「设备数量」的整表列举与「算例统计」两个指标从未真正工作过**。
    from dpf_agent.web import history_qa as _hq

    _shapes = {len(x) for x in _hq._DEVICE_MAP} | {
        len(v) for v in _hq._DEVICE_ALIASES.values()
    }
    check("⑥ 设备表两张表元组长度同构（曾因 5 vs 4 静默失效）",
          _shapes == {4}, f"长度集合={sorted(_shapes)}")

    _tmp = tempfile.mkdtemp(prefix="caliber-qa-")
    _task = "pf-caliber-probe"
    os.makedirs(os.path.join(_tmp, _task), exist_ok=True)
    with open(os.path.join(_tmp, _task, "validation.json"), "w", encoding="utf-8") as _fh:
        json.dump({"passed": True, "case": {
            "object_summary": {"bus": 11, "line": 10, "transformer": 2, "load": 9},
        }}, _fh, ensure_ascii=False)
    with open(os.path.join(_tmp, _task, "network_profile.json"), "w", encoding="utf-8") as _fh:
        json.dump({"pv_system_count": 3, "generator_count": 0, "storage_count": 0,
                   "capacitor_count": 0, "regulator_count": 0,
                   "voltage_levels_kv": [10.0, 35.0]}, _fh, ensure_ascii=False)
    # 母线结果里 B1 / B10 / B2 故意同前缀，用来锁「点位查询必须按词边界匹配」
    with open(os.path.join(_tmp, _task, "powerflow_result.json"), "w", encoding="utf-8") as _fh:
        json.dump({
            "system_summary": {
                "min_voltage_pu": 0.93, "min_voltage_bus": "B1",
                "max_voltage_pu": 1.01, "max_voltage_bus": "B2",
                "total_generation_kw": 100.0, "net_load_kw": 99.0, "total_load_kw": 100.0,
            },
            "loss_summary": {"total_loss_kw": 1.0, "loss_pct_of_load": 1.0},
            "solver_status": {"converged": True, "iterations": 3},
            "bus_results": [
                {"name": "B1", "base_kv": 10.0, "voltage_pu": 0.93, "voltage_kv": 9.3,
                 "phase_labels": ["A", "B", "C"], "phase_voltage_pu": [0.93, 0.92, 0.94]},
                {"name": "B10", "base_kv": 10.0, "voltage_pu": 1.0, "voltage_kv": 10.0,
                 "phase_labels": ["A", "B", "C"], "phase_voltage_pu": [1.0, 1.0, 1.0]},
                {"name": "B2", "base_kv": 10.0, "voltage_pu": 1.01, "voltage_kv": 10.1,
                 "phase_labels": ["A", "B", "C"], "phase_voltage_pu": [1.01, 1.01, 1.01]},
            ],
        }, _fh, ensure_ascii=False)

    _old_out = os.environ.get("DPF_OUTPUTS")
    os.environ["DPF_OUTPUTS"] = _tmp
    try:
        _ctx = {"last_success_task_id": _task, "last_task_id": _task,
                "session_id": "caliber-probe"}
        _arts = _hq._Arts(_ctx)

        for _fn_name, _fn in (("设备数量整表列举", _hq._h_device_counts),
                              ("算例统计", _hq._h_case_statistics)):
            try:
                _txt = _fn(_arts, "probe", {}) or ""
                check(f"⑥ {_fn_name} 在无 device 槽位时可返回内容",
                      "线路" in _txt and "10" in _txt, _txt.replace("\n", " / ")[:90])
            except Exception as _exc:  # noqa: BLE001
                check(f"⑥ {_fn_name} 在无 device 槽位时可返回内容", False,
                      f"{type(_exc).__name__}: {_exc}")

        _one = _hq._h_device_counts(_arts, "probe", {"device": "line"}) or ""
        check("⑥ 指定 device 槽位仍返回单项（防止修一处坏一处）",
              "线路：10 条" in _one, _one.replace("\n", " / ")[:80])

        # 问了几类就答几类：实测同一句话意图层有时给 device="line"、有时不给，
        # 只信槽位会导致"问三类只答一类"且不提示。
        _multi_q = "线路、变压器、负荷各有多少，哪个最多"
        check("⑥ 文本线索能识别出问了三类设备",
              len(_hq._mentioned_devices(_multi_q)) == 3,
              str(_hq._mentioned_devices(_multi_q)))
        _multi = _hq._h_device_counts(_arts, _multi_q, {"device": "line"}) or ""
        check("⑥ 问三类设备时即使槽位只给一类也答全",
              all(k in _multi for k in ("变压器", "母线", "线路", "负荷")),
              _multi.replace("\n", " / ")[:110])
        check("⑥ 只问一类时仍只答一类（不因新增判据而泛滥）",
              "变压器" not in (
                  _hq._h_device_counts(_arts, "一共有多少条线路", {"device": "line"}) or ""))
        check("⑥ 设备文本线索表与别名表键一致",
              set(_hq._DEVICE_TEXT_HINTS) == set(_hq._DEVICE_ALIASES))

        check("⑥ answer_by_metric 对两个指标都取到数据（非 None）",
              _hq.answer_by_metric(_hq.METRIC_DEVICE_COUNTS, "probe", _ctx, {}) is not None
              and _hq.answer_by_metric(_hq.METRIC_CASE_STATISTICS, "probe", _ctx, {}) is not None)

        # ---------- ⑦ 批 1 能力扩展：点位查询 / 单位换算 / 等级归属 / 多分母 / 倍率 / 支路 ----------
        _bv = _hq.answer_by_metric(_hq.METRIC_BUS_VOLTAGE, "母线 B1 的电压是多少伏", _ctx, {}) or ""
        check("⑦ 点位查询给出 pu 与折合 kV/V",
              "B1" in _bv and "0.9300" in _bv and "9.3000" in _bv and "9300.0 V" in _bv,
              _bv.replace("\n", " / ")[:110])
        check("⑦ 点位查询带分相电压与越限判断（越限要给方向）",
              "分相电压" in _bv and "越限（低于下限）" in _bv,
              _bv.replace("\n", " / ")[:110])
        _bv10 = _hq.answer_by_metric(_hq.METRIC_BUS_VOLTAGE, "母线 B10 电压多少", _ctx, {}) or ""
        check("⑦ 同前缀母线名不误命中（B10 不被 B1 截胡）",
              "B10" in _bv10 and "1.0000" in _bv10, _bv10.replace("\n", " / ")[:90])
        check("⑦ 单字母名不参与匹配（避免命中英文单词）",
              _hq.answer_by_metric(_hq.METRIC_BUS_VOLTAGE, "母线 B 电压多少", _ctx, {}) is None)

        _lv = _hq.answer_by_metric(_hq.METRIC_VOLTAGE_LEVELS, "差多少倍", _ctx, {}) or ""
        check("⑦ 电压等级给出个数与倍率", "电压等级 2 个" in _lv and "3.5 倍" in _lv,
              _lv.replace("\n", " / ")[:90])

        _lo = _hq.answer_by_metric(_hq.METRIC_LOSSES, "网损占源侧供电的比例", _ctx, {}) or ""
        check("⑦ 网损给多分母（负荷 / 源侧 / 净负荷）",
              "占总负荷" in _lo and "占源侧供电" in _lo and "占净负荷" in _lo,
              _lo.replace("\n", " / ")[:110])

        _mn = _hq.answer_by_metric(_hq.METRIC_MIN_VOLTAGE, "最低电压是多少", _ctx, {}) or ""
        check("⑦ 最低电压附该母线的基准电压（等级归属）",
              "基准 10.000 kV" in _mn, _mn.replace("\n", " / ")[:90])

        _vl = _hq.answer_by_metric(_hq.METRIC_VOLTAGE_LOWEST_LIST, "电压最低的母线", _ctx, {}) or ""
        check("⑦ 电压最低清单也带基准电压（不依赖意图层判对指标）",
              "基准 10.000 kV" in _vl, _vl.replace("\n", " / ")[:110])

        _br = _hq.answer_by_metric(_hq.METRIC_DEVICE_COUNTS, "一共有多少条支路", _ctx, {}) or ""
        check("⑦ 「支路」= 线路 + 变压器（不是线路的别名）",
              "支路合计：12" in _br and "线路 10" in _br and "变压器 2" in _br,
              _br.replace("\n", " / ")[:90])

        # ---------- ⑧ 结果解释：调用成功 ≠ 内容正确 ----------
        # 历史缺陷：`_chat` 硬编码 `response_format=json_object`，与提示词「用 3-6 句中文
        # 总结」冲突 → 模型返回一个裸数值（报告第 11 节长期只有 `1.364491…` 一行），
        # 而**这次调用状态是成功的**，所以旧写法（只看有没有抛异常）把它当有效解释收下了。
        from dpf_agent.runtime.model_client import _explanation_sane as _sane

        _facts = {"min_voltage_pu": 0.93, "min_voltage_bus": "B1", "total_loss_kw": 1.0}
        check("⑧ 纯数字输出被判为不可用（本缺陷的直接判据）",
              _sane("1.36449100000000025348295571901582", _facts) is False)
        check("⑧ 空 / 过短输出被判为不可用",
              not _sane("", _facts) and not _sane("ok", _facts))
        check("⑧ 复述了事实数值的正常解释被接受",
              _sane("全网最低电压为 0.9300 pu，出现在母线 B1；系统总网损 1.00 kW。",
                    _facts) is True)
        check("⑧ 不含任何事实数值的长文本被判为不可用",
              _sane("这段文字长度足够，但通篇没有涉及任何真实数值与母线名称，属于空话。",
                    _facts) is False)

        # 用**行为断言**而不是查源码字样：关键字断言会被注释/docstring 里的同样字样
        # 误伤（本文件所在项目已踩过两次）。
        from dpf_agent.runtime import model_client as _mc

        class _StubLLM(_mc.ModelClient):
            def __init__(self, reply: str):
                super().__init__(AgentConfig())
                self.reply_text = reply
                self.json_modes: list[bool] = []
                self.purposes: list[str] = []

            def available(self) -> bool:
                return True

            # ⚠️ 夹具签名必须跟随 `ModelClient._chat` 的真实签名。
            # 漏了 `purpose` 时，`explain` 的新调用形式会抛 TypeError 并被
            # `except` 吞成"退回模板"，表现为**两条断言莫名失败**——
            # 夹具滞后是"产品没坏、测试在喊狼来了"，改夹具而不是改产品。
            def _chat(self, messages, timeout=None, json_mode=True, purpose="chat"):  # noqa: ANN001
                self.json_modes.append(json_mode)
                self.purposes.append(purpose)
                return self.reply_text

        _ok_stub = _StubLLM("全网最低电压 0.9300 pu，出现在母线 B1；系统总网损 1.00 kW。")
        _txt, _src = _ok_stub.explain(_facts, "FALLBACK")
        check("⑧ explain 生成自然语言时确实关闭了 json_mode",
              _ok_stub.json_modes == [False], str(_ok_stub.json_modes))
        check("⑧ 正常解释被采纳（来源记为 llm）",
              _src == "llm" and "0.9300" in _txt, f"src={_src} text={_txt[:40]}")
        check("⑧ explain 调用带 purpose=result_explain（否则留痕无法按用途归类）",
              _ok_stub.purposes == ["result_explain"], str(_ok_stub.purposes))

        _bad_stub = _StubLLM("1.36449100000000025348295571901582")
        _t2, _s2 = _bad_stub.explain(_facts, "FALLBACK")
        check("⑧ LLM 返回裸数值时退回确定性模板（而不是写进报告）",
              _s2 == "rule" and _t2 == "FALLBACK", f"src={_s2} text={_t2[:40]}")

        # ---------- ⑨ 知识库问答：基于片段作答，且必须带出处 ----------
        from dpf_agent.prompts import prompt_versions as _pv
        from dpf_agent.web.chat_api import _rag_answer_sane as _ras

        check("⑨ rag_answer 提示词已注册（否则生成分支会静默降级）",
              "rag_answer" in _pv(), str(sorted(_pv())))

        _hits = [
            {"source": "GB_T 45906.5-2025 变电站二次系统 第5部分.md", "snippet": "…"},
            {"source": "配电网潮流计算-算法与原理.md", "snippet": "…"},
        ]
        check("⑨ 未标注出处的回答被判为不可用（防脱离片段乱说）",
              _ras("继电保护应满足可靠性、选择性、灵敏性和速动性要求。", _hits) == "")
        check("⑨ 带出处的回答被接受",
              _ras("继电保护应满足可靠性与选择性要求（出处：GB_T 45906.5-2025 "
                   "变电站二次系统 第5部分.md）。", _hits) != "")
        check("⑨ 过短 / 纯数字的回答被判为不可用",
              _ras("1.36", _hits) == "" and _ras("太短", _hits) == "")

        # ---------- ⑩ LLM 调用留痕（P3）：回答"说了什么、有没有被采信" ----------
        # 与 llm_usage.json 的分工：那份是**数量**（几次、多少 token），
        # 这份是**内容**（入参指纹 + 模型原文 + 采信判定）。没有它，报告里那句话、
        # 聊天里那段归纳，事后分不清是模型说的还是被规则模板顶掉的。
        _old_trace_log = os.environ.get("DPF_LLM_TRACE_LOG")
        os.environ["DPF_LLM_TRACE_LOG"] = "0"  # 别在测试里往 logs/ 写运行日志
        try:
            check("⑩ 留痕基础设施齐备（mark / since / summary / reset）",
                  all(hasattr(_mc, n) for n in
                      ("trace_mark", "trace_since", "trace_summary", "reset_trace")))

            _mc.reset_trace()
            _mark = _mc.trace_mark()
            _mc._record_trace(
                purpose="intent_classify", model="stub", json_mode=True,
                messages=[{"role": "user", "content": "hi"}],
                response="ok", ok=True, duration_s=0.01,
            )
            _since = _mc.trace_since(_mark)
            check("⑩ mark/since 精确圈出本次调用（不误伤聊天层更早的记录）",
                  len(_since) == 1 and _since[0]["purpose"] == "intent_classify",
                  f"{len(_since)} 条")
            check("⑩ 每条留痕含 入参指纹 / 模型原文 / 耗时 / 判定",
                  all(k in _since[0] for k in
                      ("prompt_digest", "response", "duration_s", "verdict")),
                  str(sorted(_since[0].keys()))[:110])
            _sum = _mc.trace_summary(_since)
            check("⑩ 汇总按用途给出调用数与采信数",
                  _sum["by_purpose"]["intent_classify"]["calls"] == 1,
                  str(_sum["by_purpose"]))

            # 采信判定：只回填"本线程刚发生的那次调用"，且取用即作废
            _mc.reset_trace()
            _mc._record_trace(
                purpose="p", model="stub", json_mode=False,
                messages=[{"role": "user", "content": "x"}],
                response="y", ok=True, duration_s=0.0,
            )
            _mc._trace_note_last("rejected", "不合规，已退回模板")
            check("⑩ 判定回填到刚发生的调用上",
                  _mc.trace_records()[-1]["verdict"] == "rejected")
            _mc._trace_note_last("accepted", "不该生效")
            check("⑩ 令牌一次性：二次回填被忽略（防止判定写到别的记录上）",
                  _mc.trace_records()[-1]["verdict"] == "rejected")
            _mc.reset_trace()
        finally:
            if _old_trace_log is None:
                os.environ.pop("DPF_LLM_TRACE_LOG", None)
            else:
                os.environ["DPF_LLM_TRACE_LOG"] = _old_trace_log

        # ---------- ⑪ 多轮指代消解：喂给意图层的"料"从哪来（P2-a）----------
        # 指代消解能不能生效，取决于意图层拿不拿得到"上一轮究竟问了什么"。
        # 这里锁住两件容易悄悄坏掉的事：① 必须**排除本轮消息**（本轮消息在进入
        # `_get_session_context` 之前就已写入会话，不排除的话模型会把本轮当成历史，
        # 反而判错）；② 必须**截断**（一段长回答会把上下文撑爆，既贵又容易被抄进回答）。
        from types import SimpleNamespace as _NS

        from dpf_agent.prompts import get_prompt as _gp
        from dpf_agent.prompts import get_spec as _gs
        from dpf_agent.web import chat_api as _ca

        def _msg(role: str, content: str, task_id=None, status=None):
            return _NS(role=role, content=content, attachments=[], task_id=task_id,
                       task_case_ref=None, task_status=status)

        _sess = _NS(id="s1", messages=[
            _msg("user", "这个算例的网损是多少"),
            _msg("assistant", "总网损 168.61 kW", task_id="pf-1", status="success"),
            _msg("user", "它占总负荷多少"),  # ← 本轮：已在进入函数前写入会话
        ])
        _rt = _ca._get_session_context(_sess).get("recent_turns") or []
        check("⑪ 上下文带上了 recent_turns（指代消解的唯一依据）", len(_rt) == 2, str(_rt)[:90])
        check("⑪ ★ 不含本轮消息（否则模型会把本轮当历史，反而判错）",
              all(t["text"] != "它占总负荷多少" for t in _rt), str(_rt)[:90])
        check("⑪ 保留角色，模型能分清谁说的",
              [t["role"] for t in _rt] == ["user", "assistant"], str(_rt)[:60])

        _long = "长" * 500
        _sess2 = _NS(id="s2", messages=[
            _msg("user", _long), _msg("assistant", _long),
            _msg("user", _long), _msg("assistant", _long),
            _msg("user", _long), _msg("assistant", _long),
            _msg("user", "本轮"),
        ])
        _rt2 = _ca._get_session_context(_sess2)["recent_turns"]
        check("⑪ 只留最近两轮（4 条），上下文不会越滚越大", len(_rt2) == 4, str(len(_rt2)))
        check("⑪ 单条截断到 _RECENT_TURN_CHARS（上下文是给模型判断用的，不是让它复述）",
              all(len(t["text"]) <= _ca._RECENT_TURN_CHARS for t in _rt2),
              str([len(t["text"]) for t in _rt2]))

        from dpf_agent.runtime import intent as _it

        # 代码兜底（比提示词可靠）：实测无上下文时模型照样给 resolved_query（conf 0.45），
        # 所以"猜出来的指代"必须在**入口**就被丢掉，而不是指望模型自觉。
        check("⑪ ★ 无上下文时模型猜出的 resolved_query 被硬丢掉",
              _it._sanitize_slots({"resolved_query": "系统网损占总负荷的比例是多少",
                                   "metric": "losses"}, {}) == {"metric": "losses"})
        check("⑪ 有上下文时 resolved_query 正常保留",
              _it._sanitize_slots({"resolved_query": "网损占总负荷多少"},
                                  {"recent_turns": [{"role": "user", "text": "网损是多少"}]})
              == {"resolved_query": "网损占总负荷多少"})

        _ic = _gp("intent_classify")
        check("⑪ 意图提示词含指代消解说明（否则上下文白喂）",
              "指代" in _ic and "recent_turns" in _ic)
        check("⑪ resolved_query 契约在提示词里，且写明不参与动作选择",
              "resolved_query" in _ic and "不参与动作选择" in _ic)
        check("⑪ 意图提示词版本已升到 v3（改正文必须升版本）",
              _gs("intent_classify").version == "v3", _gs("intent_classify").version)

        # ---------- ⑫ 能力问答的单一事实来源（P2-c）----------
        # 历史缺陷：能力回答是 5 段手写关键词分支 + 一段硬编码总览，同一条能力事实
        # 在代码里散成多处；且**清单外的主题问不到** —— "支持储能吗" 只会拿到一份
        # 不含储能的总览，既没回答问题也没说清缺口。
        from dpf_agent.domain import capabilities as _caps
        from dpf_agent.schemas.enums import CAPABILITY_STATUS as _CS  # noqa: F401
        from dpf_agent.schemas.enums import Capability as _Capability
        from dpf_agent.web.chat_api import _answer_capability_inquiry as _Capability_inquiry_answer

        # ★ 最要紧的一条：枚举类条目的对外状态必须**跟着 CAPABILITY_STATUS 走**，
        # 不许另写一份 —— 否则改了枚举却忘了改口径，就会对外说错话。
        _expect = {
            "steady_powerflow": _Capability.STEADY_STATE_POWERFLOW,
            "case_loading": _Capability.DSS_CASE_LOADING,
            "result_normalization": _Capability.RESULT_NORMALIZATION,
            "three_phase": _Capability.THREE_PHASE_UNBALANCED,
            "voltage_dependent_load": _Capability.VOLTAGE_DEPENDENT_LOAD,
            "auto_control": _Capability.AUTO_CONTROL,
            "probabilistic": _Capability.PROBABILISTIC_POWERFLOW,
            "data_driven": _Capability.DATA_DRIVEN_POWERFLOW,
        }
        _drift = [k for k, cap in _expect.items()
                  if _caps.get(k) is None
                  or _caps.get(k).status != _caps._status_from_enum(cap)]
        check("⑫ ★ 能力清单的状态与 CAPABILITY_STATUS 同源（改枚举不会留下过期的对外口径）",
              not _drift, str(_drift))
        check("⑫ 枚举里的每项能力都有对应条目（不漏登记）",
              all(_caps.get(k) for k in _expect), str(len(_caps.entries())))

        # 主题匹配：以前问不到的几类，现在要能落到具体条目上
        _cases = {
            "支持储能吗": "der_pv_storage",
            "能算光伏接入吗": "der_pv_storage",
            "你能做配电网重构吗": "auto_control",
            "能算短路电流吗": "short_circuit",
            "支持孤岛运行吗": "topology",
            "支持 24 小时时序吗": "timeseries",
            "你有 N-1 能力吗": "n1",
            "算例出错能定位吗": "diagnosis",
            "支持哪些算例格式": "case_loading",
        }
        _wrong = [(q, _caps.match(q).key if _caps.match(q) else None, want)
                  for q, want in _cases.items()
                  if (_caps.match(q) is None or _caps.match(q).key != want)]
        check("⑫ 以前问不到的主题现在能落到具体条目（储能/重构/短路/孤岛/时序…）",
              not _wrong, str(_wrong)[:150])

        # 越界词防护：不能因为共享一个词就点头
        check("⑫ ★ 越界词不被裸别名吃掉（「量子潮流」不许答成「稳态潮流：能做」）",
              _caps.match("支持量子潮流计算吗") is None)
        check("⑫ 清单外的主题要**承认缺口**，而不是拿总览含糊过去",
              "没有专门的能力条目" in _caps.overview(unmatched="量子潮流"))

        # 「能做」与「暂不支持」两组不能互相串（否则等于自相矛盾）
        _sup = {e.key for e in _caps.entries() if e.status in ("supported", "conditional")}
        _uns = {e.key for e in _caps.entries() if e.status in ("not_supported", "research_only")}
        check("⑫ 同一能力不会同时出现在「能做」与「暂不支持」", not (_sup & _uns))

        # 回答层：能力问询返回的每一段都必须带状态词，不能只说一段介绍
        _ans = _Capability_inquiry_answer("支持储能吗", {})
        check("⑫ 能力回答带状态词（能做 / 暂不支持 …）",
              any(w in _ans for w in ("能做", "暂不支持", "仅为研究形态", "状态未登记")),
              _ans[:60])

        # ---------- ⑬ 失败诊断建议（P2-b）：只给方向，不给操作指令 ----------
        import re as _re

        from dpf_agent.domain import diagnosis as _dg
        from dpf_agent.schemas.enums import ErrorCode as _EC

        _no_diag = {"CANCELLED"}  # 用户主动取消不是故障，刻意不给诊断
        _missing = [c.value for c in _EC
                    if c.value not in _dg.known_codes() and c.value not in _no_diag]
        check("⑬ 每个错误码都登记了排查方向（除用户主动取消）", not _missing, str(_missing))

        _s_gen = _dg.suggest("NON_CONVERGED", "迭代达上限未收敛") or ""
        check("⑬ 未收敛的诊断先声明：未收敛不等于电网不安全",
              "不等于电网不安全" in _s_gen)
        check("⑬ 每段诊断都带免责声明（结构性保障，不靠自觉）",
              "不构成操作建议" in _s_gen and "不构成操作建议" in (_dg.suggest("ENGINE_ERROR", "") or ""))

        # ★ 边界判据用**形态**而不是关键词：文案里本来就会写「改哪条支路、改成多少
        # 属于工程决定」，用裸关键词查「改成」会误伤自己（本项目已两次踩过关键词断言）。
        _instr = _re.compile(r"(把|将)[^。；\n]{0,12}(改为|设为|调到|改成|增加到|设置为)\s*[0-9]")
        _bad = [c for c in _dg.known_codes()
                if _instr.search(_dg.suggest(c, "") or "")
                or _instr.search(_dg.suggest(c, "矩阵求逆 孤岛 nan") or "")]
        check("⑬ ★ 诊断文案不出现「把 X 改成 <数字>」这类操作指令", not _bad, str(_bad))

        check("⑬ 同码不同因：消息含『矩阵求逆』时给零阻抗方向",
              "零阻抗" in (_dg.suggest("NON_CONVERGED", "Matrix Inversion Error") or ""))
        check("⑬ 同码不同因：消息含『孤岛』时给不连通方向",
              "不连通" in (_dg.suggest("VALIDATION_FAILED", "检测到供电孤岛") or ""))

        check("⑬ 未成结论 ≠ 故障：not_assessed / partial 不出诊断",
              not _dg.is_failure_status("not_assessed")
              and not _dg.is_failure_status("partial")
              and not _dg.is_failure_status("cancelled")
              and _dg.is_failure_status("non_converged"))
        check("⑬ 状态→诊断码不是拿状态值直接当码用（小写 snake 与大写码不同名）",
              _dg.code_for_status("non_converged") == "NON_CONVERGED"
              and _dg.code_for_status("non_converged") != "non_converged")

        # 报告层：失败报告要带诊断，成功报告不许带
        _md_fail = render_markdown(
            ts, native_case, sp, res, None, [], TaskStatus.NON_CONVERGED, cfg.defaults,
            explanation="",
            error={"code": "NON_CONVERGED", "message": "迭代达上限 15 次未收敛"})
        check("⑬ 失败报告含「排查方向」", "排查方向" in _md_fail)
        check("⑬ 诊断写在第 1 节内，不新增节号（避免打乱既有节号引用）",
              "## 2." in _md_fail
              and _md_fail.index("排查方向") < _md_fail.index("## 2."))
        check("⑬ 成功报告不含诊断（不无中生有）", "排查方向" not in md_native)

        # ---------- ⑭ 送引擎前的前置预检（P2-b 附带）----------
        # 背景：上传非 OpenDSS 语法的文件后，COM 引擎**既不报错也不返回**，
        # 一路挂到 45s 超时才由保护收场 —— 用户白等近一分钟。这类失败本该发生在
        # 送引擎之前（"早期失败"）。判据刻意极保守：宁可漏判，不可误伤。
        from dpf_agent.web.chat_api import _not_opendss_script as _pre

        _pd = tempfile.mkdtemp(prefix="precheck_")

        def _wfile(name: str, data, enc: str | None = None) -> str:
            p = os.path.join(_pd, name)
            if enc:
                open(p, "w", encoding=enc).write(data)
            elif isinstance(data, bytes):
                open(p, "wb").write(data)
            else:
                open(p, "w", encoding="utf-8").write(data)
            return p

        check("⑭ 空文件被拒（0 字节算例不可能算出来）",
              _pre(_wfile("empty.dss", b"")) is not None)
        check("⑭ 毫无 OpenDSS 关键字的内容被拒",
              _pre(_wfile("junk.dss", "这不是 OpenDSS 语法，随便写的一段中文。")) is not None)
        check("⑭ 随机二进制被拒",
              _pre(_wfile("bin.dss", bytes(range(256)) * 4)) is not None)

        # ★ 防误伤：合法算例必须一律放行。判据宁可漏判（让引擎自己报错），
        # 也不能挡住合法算例 —— 挡错的代价是"能算的算不了"，比白等更难解释。
        check("⑭ ★ 最小合法算例放行（New Circuit.x basekv=10）",
              _pre(_wfile("min.dss", "New Circuit.x basekv=10")) is None)
        check("⑭ ★ 纯 Redirect 入口放行（工程里很常见）",
              _pre(_wfile("redir.dss", "Clear\nRedirect base.dss")) is None)
        check("⑭ ★ UTF-16 编码的合法算例放行（剥离 NUL 后再比对关键字）",
              _pre(_wfile("u16.dss", "New Circuit.x basekv=10", enc="utf-16")) is None)
        check("⑭ 读不了的文件不拦（校验绝不阻塞算例加载）",
              _pre(os.path.join(_pd, "不存在的文件.dss")) is None)
    finally:
        if _old_out is None:
            os.environ.pop("DPF_OUTPUTS", None)
        else:
            os.environ["DPF_OUTPUTS"] = _old_out

    # 缺陷二：对比分支判据过宽 —— 「净负荷跟它差多少」里的「差多少」命中
    # `_COMPARISON_KEYWORDS`，于是在 metric 分派**之前**抢答
    # 「目前只有一次成功计算结果，无法做前后对比」，把已经取到的总负荷/净负荷吞掉。
    # 修正后：意图层已判出 metric 时，只有含「上次/上一次」这类跨记录词才走对比。
    from dpf_agent.web import chat_api as _chat

    _cmp_ctx = {"last_success_task_id": "pf-probe", "last_task_id": "pf-probe"}
    check("⑥ 单次结果内部的口径差（差多少）不再被当成跨记录对比",
          _chat._wants_history_comparison(
              "系统总负荷是多少，净负荷跟它差多少", _cmp_ctx, "load_total") is False)
    check("⑥ 明确要与上次对比时仍走对比分支",
          _chat._wants_history_comparison(
              "和上一次相比网损变化多少", _cmp_ctx, "losses") is True)
    check("⑥ 意图层未判出指标时保持旧的对比行为",
          _chat._wants_history_comparison("比较一下两次结果", _cmp_ctx, None) is True)

    # ---------- 汇总 ----------
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed_n = len(RESULTS) - passed
    print("=" * 66)
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}  {detail[:110]}")
    print(f"=== 结果：通过 {passed} / 失败 {failed_n} ===")
    return 1 if failed_n else 0


if __name__ == "__main__":
    raise SystemExit(main())
