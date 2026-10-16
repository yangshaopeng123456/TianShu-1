"""失败诊断建议：算例/任务失败时，给出「往哪个方向查」。

边界（与使用方约定，写死在这里以免日后走样）
------------------------------------------
本模块**只回答"往哪个方向查"**，绝不给出操作指令。

- 可以说：「达到迭代上限，常见三类原因：无功支撑不足、存在极小阻抗导致条件数
  恶化、或平衡母线电压与网络不匹配」——这是**排查方向**。
- 不可以说：「把容差改成 1e-3」「把迭代加到 100 就能收敛」——这是**让系统替用户
  改参数**。改了收敛、结果对不对，责任说不清，也越过了"数值必须来自引擎、
  系统不替用户做工程决定"这条线。

因此这里**刻意不接大模型**：诊断方向属于领域事实，且"不给操作指令"是一条硬边界 ——
确定性文案能 100% 守住它，而生成式输出需要额外的（且不完备的）校验才能拦。
（能力问答也是同理，见 `domain/capabilities.py`。）

另外一条纪律：**不把"未收敛"说成"电网不安全"**。未收敛只是数值求解状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 免责声明放在最前，避免读者把"方向"当成"处方"
DISCLAIMER = "以下仅为排查方向提示，不构成操作建议，也不代表系统会替任何参数做决定。"


@dataclass(frozen=True)
class Hint:
    """一条诊断提示。`keywords` 命中错误消息时优先采用（用于区分同码不同因）。"""

    code: str
    headline: str
    directions: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""
    keywords: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 通用表：按错误码
# ---------------------------------------------------------------------------
BY_CODE: dict[str, Hint] = {
    "NON_CONVERGED": Hint(
        code="NON_CONVERGED",
        headline="迭代达到上限仍未收敛",
        directions=(
            "**无功支撑不足**：负荷水平偏高、或电源无功出力受限，远离平衡点的电压支撑不够；",
            "**电压稳定裕度不足**：重载馈线末端电压已接近崩溃点，迭代在两点之间震荡；",
            "**条件数恶化**：存在极小阻抗 / 接近零阻抗的支路或联络线，Y 矩阵病态；",
            "**平衡母线设定与网络不匹配**：基准电压、相角或短路容量与网络实际相差较大；",
            "**负荷模型的非线性**：电压跌破 OpenDSS 默认 `Vminpu` 后负荷被按电压折减，"
            "与初始假设不一致。",
        ),
        note="未收敛只是数值求解状态，**不等于电网不安全**；此时不应引用任何电压/负载率结论。",
    ),
    "DATA_UNAVAILABLE": Hint(
        code="DATA_UNAVAILABLE",
        headline="编译阶段失败：算例缺少可解析的定义",
        directions=(
            "**缺少 `LineCode` 定义**：引用了未声明的线型；",
            "**基准母线缺失**：`Vsource`/`Circuit` 指向的母线在模型中不存在；",
            "**异常母线引用**：`Bus1`/`Bus2`/`Bus3` 里有拼写不一致或未定义的节点；",
            "**依赖文件缺失**：`Redirect` 指向的子文件不在交付目录内。",
        ),
    ),
    "VALIDATION_FAILED": Hint(
        code="VALIDATION_FAILED",
        headline="算例校验未通过",
        directions=(
            "**拓扑**：跨电压等级却标为普通 `Line`（应表达为变压器语义）、存在供电孤岛"
            "（节点无法从平衡母线到达）、重复母线 ID；",
            "**数据**：负荷 / 阻抗里出现 `NaN` 或 `Infinity`、相移 `shift_deg` 非数值；",
            "**参数**：`baseMVA ≤ 0`、负变比，或三相相关字段与声明的相数不一致。",
        ),
    ),
    "CASE_PATH_REJECTED": Hint(
        code="CASE_PATH_REJECTED",
        headline="算例路径被拒绝",
        directions=(
            "**绝对路径的 `Redirect`**：指向了交付目录之外的绝对路径；",
            "**越界引用**：算例引用了允许目录之外的文件；",
            "**Master 缺失或命名不符**：目录里没有可识别的入口文件。",
        ),
    ),
    "VERIFICATION_FAILED": Hint(
        code="VERIFICATION_FAILED",
        headline="物理合理性校验未通过（结果自相矛盾）",
        directions=(
            "**若是电压异常**（≤ 0 或 > 2.0 pu）：多为量测口径或单位问题（p.u. 与 kV 混用）、"
            "或源端等值参数不合理；",
            "**若是负网损**：支路潮流方向与损耗口径不一致，或存在未标注的发电注入；",
            "**若是功率平衡残差过大**：元件口径（负荷/发电/储能的归类）与实际不符，"
            "或引擎返回的元件集合不完整；",
            "**若是复现性不一致**：同一算例两次求解结果不同，指向缓存键或算例产物被改写。",
        ),
        note="这是「结果不可信」的信号，比「算不出来」更值得警惕 —— 数值已产出但不能用。",
    ),
    "RESULT_MAPPING_FAILED": Hint(
        code="RESULT_MAPPING_FAILED",
        headline="求解完成，但结果无法映射为统一口径",
        directions=(
            "**元件集合不完整**：引擎返回的对象与预期不符（例如某类元件未被遍历到）；",
            "**字段缺失或单位不符**：结果里缺少必需的量（电压 / 功率 / 损耗）；",
            "**相别信息不足**：需要分相结果，但算例是平衡等值模型。",
        ),
    ),
    "ENGINE_ERROR": Hint(
        code="ENGINE_ERROR",
        headline="计算引擎通道异常（与算例本身可能无关）",
        directions=(
            "**先判断可复现性**：同一算例再算一次 —— 若结果不同，倾向引擎进程状态问题；"
            "若稳定复现，倾向算例或数值条件；",
            "**引擎健康**：原生库 / 引擎进程可能已被破坏，此时任何算例都会失败；",
            "**环境干扰**：同一时刻是否有其它进程占用引擎资源。",
        ),
    ),
    "TIMEOUT": Hint(
        code="TIMEOUT",
        headline="求解或引擎调用超时",
        directions=(
            # 这条来自真机实测：上传一个不是 OpenDSS 语法的文件后，引擎**不报错、
            # 直接挂住**，白等 45s 才由超时保护收场 —— 所以"算例内容非法"必须列进来，
            # 否则用户只会去查"规模/参数"，方向完全不对。
            "**算例内容非法**：内容不是有效的 OpenDSS 脚本时，引擎可能既不报错也不返回"
            "（一路挂到超时）；可用命令行通道做对照验证；",
            "**算例规模**：节点 / 支路数量是否远超常规；",
            "**引擎无响应**：进程可能卡在原生库调用上（而非计算量大）；",
            "**异常参数**：极端迭代次数或不合理容差也可能导致长时间不返回。",
        ),
    ),
    "CAPABILITY_UNAVAILABLE": Hint(
        code="CAPABILITY_UNAVAILABLE",
        headline="请求了当前版本未启用的能力",
        directions=(
            "这是**能力边界**而非算例缺陷：请求里包含了未启用的能力关键词"
            "（自动分接头、无功补偿控制、拓扑重构、重调度、削负荷等）；",
            "可用「能力说明」类问题查看完整的能力清单与边界。",
        ),
    ),
    "FIDELITY_NOT_APPROVED": Hint(
        code="FIDELITY_NOT_APPROVED",
        headline="保真度未获批准（结果不得作为正式结论）",
        directions=(
            "**格式转换等值**：算例来自 xlsx / csv / json / MATPOWER，"
            "转换后是近似等值模型，未与原始求解器做过等价性验证；",
            "**交叉核验**：若需要正式结论，应以原始求解器的结果为准或做独立基准对比。",
        ),
    ),
    "REQ_SCHEMA_INVALID": Hint(
        code="REQ_SCHEMA_INVALID",
        headline="请求本身不完整",
        directions=(
            "**缺少算例引用**：既没有上传算例，也没有指定内置算例名或 `Master.dss` 路径；",
            "**字段缺失**：必需的任务字段未提供。",
        ),
    ),
    "CONSTRAINT_CONFLICT": Hint(
        code="CONSTRAINT_CONFLICT",
        headline="约束之间互相冲突",
        directions=(
            "**参数越界**：如迭代次数 < 1、容差不在 (0, 1] 区间；",
            "**与强制安全规则冲突**：用户显式参数与系统硬约束不一致。",
        ),
    ),
    "INTERRUPTED": Hint(
        code="INTERRUPTED",
        headline="任务被进程级中断（后端崩溃或被强制结束）",
        directions=(
            "**非用户取消**：与主动取消刻意区分 —— 这是后端进程异常终止；",
            "**查崩溃日志**：看 `outputs/_logs/` 下的崩溃记录与后端日志尾部；",
            "**是否可复现**：重算一次；若稳定复现，需定位到具体阶段。",
        ),
    ),
    "REPORT_ERROR": Hint(
        code="REPORT_ERROR",
        headline="报告渲染失败",
        directions=(
            "**已验证的结构化产物仍然有效**：JSON / 验证结论不受影响，可继续引用；",
            "**多为渲染层问题**（字段缺失 / 编码 / 模板），而非计算问题。",
        ),
    ),
}

# 同码不同因：错误消息里出现这些词时，用更具体的条目
BY_KEYWORD: tuple[Hint, ...] = (
    Hint(
        code="NON_CONVERGED",
        headline="收敛失败，且与极小阻抗有关",
        keywords=("零阻抗", "极小阻抗", "矩阵求逆", "inversion", "奇异", "singular"),
        directions=(
            "**精确零阻抗支路**会让 Y 矩阵奇异**或**接近奇异：检查是否存在长度为 0 的线路、"
            "未带阻抗的理想开关；",
            "**近似零阻抗**（比其余支路小若干数量级）同样会恶化条件数；",
            "排查时可先看**是否所有算例都失败**：只有个别算例失败 → 该算例的支路参数；"
            "普遍失败 → 求解设置或引擎状态。",
        ),
        note="这里只说往哪查；改哪条支路、改成多少，属于工程决定，系统不做。",
    ),
    Hint(
        code="DATA_UNAVAILABLE",
        headline="编译失败，指向线型定义缺失",
        keywords=("linecode", "线型"),
        directions=(
            "`Line` 引用了未定义的 `LineCode`：确认线型定义的名称拼写、"
            "以及定义是否在 `Redirect` 的文件里被真正加载。",
        ),
    ),
    Hint(
        code="VALIDATION_FAILED",
        headline="校验未通过，指向供电孤岛",
        keywords=("孤岛", "island"),
        directions=(
            "**不连通**：部分节点无法从平衡母线到达，通常是支路缺失或母线名写错；",
            "**本就是孤岛设计**：本系统不支持孤岛运行，此类算例应改造为并网模型。",
        ),
    ),
    Hint(
        code="VALIDATION_FAILED",
        headline="校验未通过，指向非法数值",
        keywords=("nan", "infinity", "inf"),
        directions=(
            "**坏值来源**：原始表格里的空单元格、除零结果、或单位换算中间值；",
            "**定位方式**：按字段名在源文件中查找该记录。",
        ),
    ),
    # ↓ 这三条来自真机实测：`Compile 后 Circuit 无母线` 原本落到通用方向里，
    #   给的是拓扑/数据/参数一整套 —— 与实际情况（算例压根没被解析出来）对不上。
    Hint(
        code="VALIDATION_FAILED",
        headline="校验未通过：算例里没有任何母线",
        keywords=("无母线", "没有母线", "母线数为", "no bus", "no buses"),
        directions=(
            "**入口文件没被正确加载**：最常见 —— 内容是空的或不是有效的 OpenDSS 脚本，"
            "于是 `Circuit` 建起来了却没有任何母线；",
            "**`New` 语句被整条忽略**：语法有误时 OpenDSS 会跳过该行，元件与母线随之不存在；",
            "**确认入口与 `Redirect`**：目录里是否确有所指的入口文件，"
            "它引用的子文件是否都在交付目录内。",
        ),
    ),
    Hint(
        code="VALIDATION_FAILED",
        headline="校验未通过：存在未解析的母线引用",
        keywords=("bad_reference", "未解析", "母线引用"),
        directions=(
            "**拼写不一致**：`Bus1`/`Bus2`/`Bus3` 与定义处的母线名不完全一致"
            "（包括大小写与相别后缀 `b1.1.2` 这类写法）；",
            "**元件被跳过**：该母线原本由另一条 `New` 语句定义，而那一条没生效；",
            "**跨文件引用**：母线定义在被 `Redirect` 的文件里，但该文件未被加载。",
        ),
    ),
    Hint(
        code="VALIDATION_FAILED",
        headline="校验未通过：没有可用的基准（源）母线",
        keywords=("基准母线", "无源", "vsource", "source bus"),
        directions=(
            "**缺少源定义**：算例里没有 `Vsource` / `Circuit` 的电源设置，"
            "潮流没有起点；",
            "**源指向的母线不存在**：`bus1=` 写了一个未被定义的母线；",
            "**转换产物丢源**：转换类算例的电源等值没被正确写出。",
        ),
    ),
)


def _pick(code: str, message: str) -> Hint:
    msg = (message or "").lower()
    for h in BY_KEYWORD:
        if h.code == code and any(k.lower() in msg for k in h.keywords):
            return h
    return BY_CODE.get(code) or Hint(code=code, headline="未登记的错误码")


def suggest(code: str | None, message: str = "") -> str | None:
    """按错误码 + 消息给出排查方向；空码或用户取消不属于故障诊断。"""
    if not code or str(getattr(code, "value", code)).upper() == "CANCELLED":
        return None
    h = _pick(str(code), message)
    lines = [f"**排查方向**（{h.headline}）", ""]
    if h.directions:
        lines.extend(f"- {d}" for d in h.directions)
    else:
        lines.append("- 该错误码尚未登记具体方向，请结合上面的失败信息与阶段定位。")
    if h.note:
        lines.append("")
        lines.append(f"> {h.note}")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    return "\n".join(lines)


# 任务状态 → 诊断码（`error` 为空时的兜底）。
# ⚠️ `TaskStatus` 的取值是小写 snake_case，与 `ErrorCode` 的大写形式**不同名**，
# 不能直接拿 status 当 code 用（拼错就会静默落到"未登记的错误码"）。
_STATUS_TO_CODE: dict[str, str] = {
    "non_converged": "NON_CONVERGED",
    "validation_failed": "VALIDATION_FAILED",
    "data_unavailable": "DATA_UNAVAILABLE",
    "invalid_request": "REQ_SCHEMA_INVALID",
    "system_failed": "ENGINE_ERROR",
    "interrupted": "INTERRUPTED",
}

# 只有这些状态才配"失败诊断"。
# ⚠️ 刻意不含 `not_assessed` / `partial`：它们的语义是"某维度未形成结论 / 受限发布"，
# 不是故障。把它们当失败来诊断，会凭空给出一堆与实际情况无关的排查方向
# （例如把"未评估"说成"物理校验未通过"）—— 那比不给建议更糟。
_FAILURE_STATUSES: frozenset[str] = frozenset(_STATUS_TO_CODE)


def is_failure_status(status_value: str | None) -> bool:
    return str(status_value or "").lower() in _FAILURE_STATUSES


def code_for_status(status_value: str | None) -> str | None:
    return _STATUS_TO_CODE.get(str(status_value or "").lower())


def known_codes() -> tuple[str, ...]:
    return tuple(sorted(BY_CODE))
