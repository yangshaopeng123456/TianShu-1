"""能力问答的**单一事实来源**（P2-c）。

为什么要有这个模块
------------------
"能不能做 X" 是用户最常问、也最容易答错的一类问题 —— 答错就是**对外说错话**
（本项目的硬约定之一：能力口径必须与实现一致）。此前这类回答散落在
`web/chat_api.py` 的若干条 `if 关键词` 分支里，且只覆盖 5 个主题；用户问
"支持储能吗""能算短路电流吗"时，得到的是一份**不含该主题**的通用总览 ——
既没回答问题，也没说清"是没做还是没有这项"。

本模块把能力事实收敛到一处：

1. **枚举类能力的状态从 `CAPABILITY_STATUS` 读**（`_status_from_enum`），不另写一份
   —— 同一条事实写两遍迟早漂移，症状是"改一处无效"。
2. 非枚举、但用户确实会问的实务主题（短路电流、孤岛、储能元件语义等）单列，
   并在 `evidence` 里写明依据（哪个适配器/哪条回归），便于事后核对口径。
3. 清单里**没有**的主题，如实说"不在清单内、我不承诺"，而不是拿总览糊弄。

边界：本模块只负责"把清单里的既有事实说给人听"，**不产生任何新的能力声明**。
新增能力时先改实现与 `CAPABILITY_STATUS`，再回来加条目。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas.enums import CAPABILITY_STATUS, Capability

# ---------------------------------------------------------------------------
# 状态口径：把枚举里的原始状态词翻译成"对用户说的话"
# ---------------------------------------------------------------------------
_STATUS_FROM_ENUM: dict[str, str] = {
    "mvp_candidate": "supported",
    "conditional": "conditional",
    "later_phase": "not_supported",
    "research_only": "research_only",
}

_STATUS_LABEL: dict[str, str] = {
    "supported": "能做",
    "conditional": "能做，但有边界",
    "not_supported": "暂不支持",
    "research_only": "仅为研究形态，未纳入正式发布",
    "unknown": "状态未登记",
}


def _status_from_enum(cap: Capability) -> str:
    return _STATUS_FROM_ENUM.get(CAPABILITY_STATUS.get(cap, ""), "unknown")


@dataclass(frozen=True)
class CapabilityEntry:
    key: str
    title: str
    detail: str
    status: str = "supported"
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # 依据：写明"凭什么这么说"，让口径事后可核对（代码位置 / 回归记录）
    evidence: str = ""


# ---------------------------------------------------------------------------
# 清单
#
# 状态一律优先取枚举（`Capability` 项）；纯实务主题（无对应枚举）才手写状态。
# ---------------------------------------------------------------------------
CAPABILITIES: tuple[CapabilityEntry, ...] = (
    # 诊断类刻意排在最前：它的别名（"出错"/"不收敛"）与最宽泛的 "算例"
    # （case_loading）常同长度，而 match() 在**同长度时取清单靠前者** ——
    # 排前面才能让「算例出错能定位吗」落到诊断条目，而不是被 "算例" 吸走。
    CapabilityEntry(
        key="diagnosis",
        title="算例出错时的定位能力",
        status="supported",
        aliases=("出错", "不收敛", "失败", "报错", "异常", "定位", "哪里出错", "诊断", "坏数据"),
        detail="**能做**：按四个层级给出**具体文件 / 行号 / 字段名**，而不是一句\"计算失败\"。\n"
               "· 转换阶段：`baseMVA ≤ 0`、负变比、非法 `bus_type`、跨电压等级却标为普通 line、"
               "供电孤岛、重复母线 ID、`NaN`/`Infinity` 负荷、绝对 Redirect 路径阻断、依赖文件缺失；\n"
               "· 编译阶段：缺少 `LineCode` 定义、`Vsource` 基准母线缺失、异常母线引用；\n"
               "· 求解阶段：迭代达上限未收敛（如实返回，不伪装成功）、"
               "精确零阻抗导致矩阵奇异、强制限值后发散；\n"
               "· 验证阶段：电压 ≤ 0 或 > 2.0 pu、负网损、功率平衡残差过大、复现性不一致。\n"
               "想现场看效果，可以上传一个已知会失败的文件（缺 `LineCode` 的 `.dss`、"
               "含 `NaN` 负荷的 `.json`、负变比的 `.m`、零阻抗支路等）。",
        evidence="exceptions/case_errors；validation；verification/physical_sanity",
    ),
    # ---------------- 已启用（mvp_candidate）----------------
    CapabilityEntry(
        key="case_loading",
        title="算例读取与转换",
        status=_status_from_enum(Capability.DSS_CASE_LOADING),
        aliases=("算例", "格式", "读取", "导入", "上传", "dss", "zip", "xlsx", "csv",
                 "json", "matpower", ".m", "文件"),
        detail="能读 6 类算例：`.dss`、`.zip`、`.xlsx`、`.csv`、`.json`、MATPOWER `.m`，"
               "统一转换为 OpenDSS 模型后再求解。转换产物带版本戳，改转换逻辑会升版本，"
               "不会出现\"老算例一直用旧产物\"这种静默问题。",
        evidence="web/case_converters.py；CONVERSION_VERSIONS",
    ),
    CapabilityEntry(
        key="steady_powerflow",
        title="稳态单场景潮流",
        status=_status_from_enum(Capability.STEADY_STATE_POWERFLOW),
        aliases=("潮流", "稳态", "求解", "计算", "single", "snapshot"),
        detail="调用 OpenDSS 求解**单个场景**的稳态潮流，给出收敛状态、迭代次数、"
               "节点电压、支路潮流、网损与功率平衡残差。",
        evidence="engines/opendss_adapter.py；skills/powerflow_skill.py",
    ),
    CapabilityEntry(
        key="result_normalization",
        title="结果规范化与报告产物",
        status=_status_from_enum(Capability.RESULT_NORMALIZATION),
        aliases=("报告", "产物", "导出", "markdown", "html", "规范化"),
        detail="把引擎原始结果规范化为统一口径（总负荷 / 净负荷 / 网损 / 发电注入分列），"
               "并产出 Markdown、HTML、JSON 报告与审计产物（含验证结论、证据链、"
               "本次求解口径声明）。",
        evidence="renderers/markdown_renderer.py；evidence/；verification/",
    ),
    # ---------------- 有条件（conditional）----------------
    CapabilityEntry(
        key="three_phase",
        title="三相不平衡（分相建模与不对称度）",
        status=_status_from_enum(Capability.THREE_PHASE_UNBALANCED),
        aliases=("三相", "分相", "不平衡", "不对称", "unbalance", "负序", "零序", "单相"),
        detail="**能做** —— 这本来就是 OpenDSS 的强项，本系统按三相原生模型求解"
               "（不是正序等值），结果保留**每条母线的分相电压与相角**。\n"
               "· **指标**：用对称分量法给出不平衡度 ε = |U₂| / |U₁| × 100%"
               "（与 IEC 61000-4-30、GB/T 15543 对「电压不平衡度」的口径一致），"
               "并汇总最严重母线、超限清单。\n"
               "· **提问**：算完之后可以直接问「哪个母线不平衡最严重」「A 相电压是多少」"
               "「不平衡度多大」「有没有负序」——读产物秒答，不重算。\n"
               "· **报告**：含独立章节「三相不平衡」（分相电压/相角、最严重母线、超限清单、口径说明）。\n"
               "**边界（必须说清，避免误解）**：\n"
               "· 限值 **2% 是参考值，尚待与 GB/T 15543 原文核对**，只作标注，"
               "不构成「合格 / 不合格」的权威判定；\n"
               "· 若算例由 **xlsx / csv / json / MATPOWER** 转换而来，转换器目前映射为"
               "**平衡三相等值模型**，原始文件里的相别信息（单相负荷接在哪一相、零序参数 R0/X0）"
               "不会被保留 —— 报告会显式声明这一点；\n"
               "· **原生 `.dss` 算例没有上述限制**，其模型本身分相，分相结果完整。",
        evidence="web/phase_model.py；domain/unbalance；8 个分相算例端到端回归",
    ),
    CapabilityEntry(
        key="voltage_dependent_load",
        title="电压相关负荷（ZIP）",
        status=_status_from_enum(Capability.VOLTAGE_DEPENDENT_LOAD),
        aliases=("zip", "恒功率", "恒阻抗", "恒电流", "电压相关", "负荷模型", "vminpu"),
        detail="负荷按算例给定的模型求解。注意一个**隐性行为**：OpenDSS 默认 "
               "`Vminpu=0.95`，电压跌破该值后负荷会被自动按电压折减 —— 报告会把这一点"
               "写进口径声明，不会让它悄悄影响结果。",
        evidence="domain/solver_profile.py（control_behavior / 负荷模型声明）",
    ),
    # ---------------- 暂不支持（later_phase / research_only）----------------
    CapabilityEntry(
        key="auto_control",
        title="自动控制（分接头 / 无功补偿 / 重调度 / 削负荷 / 拓扑重构）",
        status=_status_from_enum(Capability.AUTO_CONTROL),
        aliases=("自动分接头", "有载调压", "无功补偿", "电容器投切", "重调度", "削负荷",
                 "拓扑重构", "网络重构", "重构", "oltc", "调压"),
        detail="**不做**：V1.3 不执行自动分接头调节、无功补偿投切、重调度、削负荷与拓扑重构"
               "（`control_behavior=frozen_off`）。若把这些词写进指令，请求门会直接拒绝并说明原因。\n"
               "需要区分的是：**静态分接头建模是支持的** —— 算例里给定的 tap / 变比会照常参与求解，"
               "不支持的只是\"让程序自己去调它\"。",
        evidence="runtime/guard.py（_FORBIDDEN_CAPABILITY_KEYWORDS）；domain/solver_profile.py",
    ),
    CapabilityEntry(
        key="probabilistic",
        title="概率潮流",
        status=_status_from_enum(Capability.PROBABILISTIC_POWERFLOW),
        aliases=("概率潮流", "随机潮流", "蒙特卡洛", "概率"),
        detail="**不做**：不提供概率潮流 / 随机潮流 / 蒙特卡洛抽样（研究形态，未纳入正式发布）。",
        evidence="schemas/enums.py（CAPABILITY_STATUS: research_only）",
    ),
    CapabilityEntry(
        key="data_driven",
        title="数据驱动潮流",
        status=_status_from_enum(Capability.DATA_DRIVEN_POWERFLOW),
        aliases=("数据驱动", "神经网络潮流", "机器学习潮流", "ai潮流"),
        detail="**不做**：不提供数据驱动的潮流近似（研究形态，未纳入正式发布）；"
               "本系统的数值一律来自确定性求解器，不由模型生成。",
        evidence="schemas/enums.py（CAPABILITY_STATUS: research_only）",
    ),
    # ---------------- 实务主题（无对应枚举，依据写在 evidence）----------------
    CapabilityEntry(
        key="der_pv_storage",
        title="分布式电源：光伏 / 发电 / 储能",
        status="supported",
        aliases=("光伏", "pv", "pvsystem", "分布式电源", "der", "发电", "风机", "储能",
                 "storage", "蓄电池", "电池"),
        detail="**能做**：`Generator` 与 `PVSystem` 都计入发电注入（`gen_kw`，取反后为上网功率）"
               "；`Storage` 单独统计（`storage_kw`，保留原始符号、不取反），并**必须进功率平衡式**"
               "—— 否则含储能的算例会直接判失败。\n"
               "边界：储能只做**元件级建模与功率统计**，不做充放电调度、不做时序优化"
               "（那是自动控制/时序能力的范畴，见对应条目）。",
        evidence="engines/opendss_adapter.py（ELEMENT_ROLE_BY_PREFIX / _POWER_COLLECTIONS）；"
                 "回归样本 cases/pvsystem_probe/、cases/storage_discharge_probe/；case07 含 18 个 PVSystem",
    ),
    CapabilityEntry(
        key="topology",
        title="网络拓扑",
        status="conditional",
        aliases=("拓扑", "辐射", "环网", "开环", "馈线", "多馈线", "互联", "孤岛", "孤岛运行"),
        detail="**能做**：解析并报告网络拓扑（辐射 / 环网判定、馈线归属、设备挂接关系）。\n"
               "边界：**不支持孤岛运行** —— 与平衡母线不连通的节点会在转换阶段被判为"
               "「供电孤岛」并明确报错，而不是硬算出一个无意义的结果；多馈线之间默认不互联。",
        evidence="web/case_converters.py（供电孤岛检测）；domain/network_profile.py",
    ),
    CapabilityEntry(
        key="short_circuit",
        title="短路电流计算",
        status="not_supported",
        aliases=("短路", "短路电流", "故障电流", "短路容量", "scc"),
        detail="**不做**：本系统只做**稳态潮流**，不提供短路电流 / 故障电流计算。"
               "（转换器里出现的\"短路容量\"只是给等值电源设内阻抗用的一个参数，"
               "不代表具备短路计算能力。）",
        evidence="无短路计算实现；web/case_converters.py 中 MVAsc 仅作等值参数",
    ),
    CapabilityEntry(
        key="timeseries",
        title="24 小时时序扫描",
        status="not_supported",
        aliases=("时序", "24小时", "24h", "全天", "逐时", "daily", "profile", "日内"),
        detail="**不做**：已有结果是**单场景 snapshot**，不会冒充全天扫描。"
               "若需要不同时段的对比，请分别发起独立计算，系统会如实标注各自的口径。",
        evidence="skills/powerflow_skill.py（单场景）；报告口径声明",
    ),
    CapabilityEntry(
        key="n1",
        title="N-1 开断扫描",
        status="not_supported",
        aliases=("n-1", "n1", "停运", "开断", "故障扫描", "预想事故"),
        detail="**不做**：不自动生成 N-1 开断场景、也不做批量对比。"
               "如需某个开断方式的结果，可以显式构造该算例后单独计算。",
        evidence="无 N-1 场景生成实现",
    ),
    CapabilityEntry(
        key="q_limits",
        title="发电机无功限值（Q 限值与 PV→PQ）",
        status="not_supported",
        aliases=("q限值", "qmax", "qmin", "无功限值", "pv转pq", "pv→pq", "无功约束",
                 "无功优化", "无功"),
        detail="**尚未完整执行**：当前版本不施加发电机 Q 限值约束、也不做 PV→PQ 外层迭代。"
               "基态潮流可以收敛，但**不能据此声称已完成无功可行性验证**。"
               "若强制限值后发散，应先判断无功可行域 / 电压稳定裕度不足，同时排查数值条件，"
               "而不是简单归因于算法失败。",
        evidence="域模型无 PV→PQ 外循环；报告口径声明未包含项",
    ),
)

_BY_KEY: dict[str, CapabilityEntry] = {e.key: e for e in CAPABILITIES}


def entries() -> tuple[CapabilityEntry, ...]:
    return CAPABILITIES


def get(key: str) -> CapabilityEntry | None:
    return _BY_KEY.get(key)


# 越界修饰词：出现这些词时，说明用户问的是**另一个东西**，不能因为共享了某个词
# 就当成清单里的条目。实测："支持量子潮流计算吗" 会被裸别名「潮流」吃掉，
# 于是答成"稳态潮流：能做" —— 那是**说错话**（对着没做的事点头）。
# 这批词属于"有限枚举"：本清单没有覆盖的领域，不硬猜，一律承认缺口。
_QUALIFIER_GUARD: tuple[str, ...] = (
    "量子", "人工智能", "深度学习", "神经网络", "强化学习", "区块链",
    "联邦", "大模型", "数字孪生", "元宇宙",
)


def match(text: str) -> CapabilityEntry | None:
    """按别名匹配用户问的能力主题。

    命中规则刻意保守：**取最长命中的别名所属条目**（"自动分接头" 应命中 auto_control，
    而不是被 "分接" 之类的短别名抢走），并且只认清单里的主题 —— 猜不出来的宁可说不知道。
    """
    compact = "".join((text or "").lower().split())
    if not compact:
        return None
    if any(w in compact for w in _QUALIFIER_GUARD):
        return None
    best: tuple[int, CapabilityEntry] | None = None
    for e in CAPABILITIES:
        for a in e.aliases:
            al = "".join(a.lower().split())
            if al and al in compact:
                if best is None or len(al) > best[0]:
                    best = (len(al), e)
    if best is None:
        return None
    # 同一长度下多条目命中（例如"无功"同时属于 q_limits 与 auto_control）时，
    # 取清单中**更靠前**的条目 —— 清单顺序即优先级，避免结果随字典顺序漂移。
    hit_len = best[0]
    for e in CAPABILITIES:
        for a in e.aliases:
            al = "".join(a.lower().split())
            if al and al in compact and len(al) == hit_len:
                return e
    return best[1]


def render(entry: CapabilityEntry) -> str:
    label = _STATUS_LABEL.get(entry.status, entry.status)
    lines = [
        "这是能力说明，本次不触发潮流计算。",
        "",
        f"**{entry.title}** —— {label}",
        "",
        entry.detail,
    ]
    return "\n".join(lines)


def _supported_list() -> list[str]:
    return [f"- {e.title}" for e in CAPABILITIES
            if e.status in ("supported", "conditional")]


def _unsupported_list() -> list[str]:
    out = []
    for e in CAPABILITIES:
        if e.status in ("not_supported", "research_only"):
            out.append(f"- {e.title}")
    return out


def overview(*, unmatched: str = "") -> str:
    """整体能力清单。`unmatched` 非空时，**先承认"你问的这项我没有专门条目"**。

    这是本模块存在的意义所在：与其拿一份不含该主题的总览含糊过去，
    不如把缺口摆到台面上（"缺口要自己冒出来，而不是等用户试出来"）。
    """
    lines = ["这是能力说明，本次不触发潮流计算。", ""]
    if unmatched:
        lines.append(
            f"你问的「{unmatched}」我**没有专门的能力条目** —— 不在下面的清单里，"
            "所以我不能对它做任何承诺（可能尚未实现，也可能只是没登记）。"
            "下面是完整清单，若其中确实没有你想问的项，请按\"尚未支持\"理解。"
        )
        lines.append("")
    lines.append("**能做**（含\"能做但有边界\"）：")
    lines.extend(_supported_list())
    lines.append("")
    unsup = _unsupported_list()
    if unsup:
        lines.append("**暂不支持**（不会伪装）：")
        lines.extend(unsup)
        lines.append("")
    lines.append(
        "想问某个具体能力，直接说能力名（如「三相」「储能」「N-1」「时序」「Q 限值」），"
        "我会给出明确边界；也可以说「全部能力」看这份清单。"
    )
    return "\n".join(lines)


def answer(text: str) -> str:
    """能力问询的统一入口：命中主题给该主题的说明，否则给总览并承认未收录。"""
    entry = match(text)
    if entry is not None:
        return render(entry)
    compact = "".join((text or "").lower().split())
    # 「你能做什么」「支持哪些能力」这类总览请求：不必承认缺口，直接给清单
    if any(w in compact for w in ("能力", "做什么", "能做什么", "会什么", "哪些功能",
                                  "功能列表", "全部能力", "支持什么")):
        return overview()
    return overview(unmatched=(text or "").strip()[:40])
