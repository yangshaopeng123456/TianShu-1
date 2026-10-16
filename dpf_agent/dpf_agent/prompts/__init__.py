"""提示词集中仓库（P2：提示词版本化）。

为什么集中管理：
1. **可追溯**：每个任务在 state.version_set 里记录本次用到的提示词版本，
   事后能回答"这个结果是哪一版提示词产生的"——论文复现与线上排障都需要。
2. **可回滚**：提示词改动集中在 prompts/ 目录，改坏了能立刻对照上一版回滚。
3. **可 A/B**：同一 id 可并行保留多版本（见 PROMPTS_BY_ID 的列表形式扩展点）。

约定：
- 每个提示词有稳定 id（如 task_parse）、人工版本号 version、以及自动内容指纹 digest。
- digest 由正文计算，任何人改了正文而忘记升版本号，digest 也会变化，避免"同名不同文"。
- 使用方一律通过 get_prompt(id) 取正文，不要在业务代码里内联长提示词。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    id: str
    version: str
    text: str
    description: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:8]

    @property
    def label(self) -> str:
        return f"{self.id}@{self.version}({self.digest})"


# ---------------------------------------------------------------------------
# 1) 任务解析：自然语言 → 严格 JSON 任务规格
# ---------------------------------------------------------------------------
TASK_PARSE = PromptSpec(
    id="task_parse",
    version="v1",
    description="把用户自然语言解析为任务规格 JSON（算例、结果域、求解参数）",
    text="""你是天枢一号的任务解析器。把用户自然语言解析为严格 JSON，不要输出任何其他文字。
JSON schema:
{
  "user_goal": string,               // 固定为"执行配电网稳态潮流计算"，除非用户明确只是咨询
  "case_ref": string|null,           // 用户提到的 DSS 文件路径或算例名
  "result_scopes": ["bus","branch","transformer","system"] 的子集,
  "output_formats": ["json","md","html","csv"] 的子集,
  "solver_config": {"max_iterations": int|null, "tolerance": number|null},
  "focus_objects": [string],         // 用户关注的解释重点，如"最低电压","网损"
  "ambiguities": [string]            // 影响专业结果的歧义
}
规则：result_scopes 默认 ["bus","branch","transformer","system"]；output_formats 默认 ["json","md"]；
用户用"只要/只看"等限定词时，result_scopes 只保留用户明确提到的范围；
用户没提的 solver 参数填 null；绝对不要编造数值结果。""",
)

# ---------------------------------------------------------------------------
# 2) 结果解释：只允许复述事实中的数值（禁止新增数值）
# ---------------------------------------------------------------------------
RESULT_EXPLAIN_TEMPLATE = PromptSpec(
    id="result_explain",
    version="v3",
    description="证据化表达：只能使用给定事实 JSON 中的数值，禁止新增；必须成段自然语言",
    text="""你是配电网潮流结果的解释器。只允许使用下面「事实」中的数值，禁止新增任何数值、状态或排名。
要求：
1) 用 3-6 句通顺的中文写成一段话；**不要输出 JSON、不要只输出一个数值、不要罗列字段名**；
2) 至少复述一两个关键数值（例如最低电压及其所在母线、系统网损）；
3) 不要下"安全 / 合格"结论 —— 判据是仿真默认阈值，不是工程审批限值。
事实：{facts}""",
)

# ---------------------------------------------------------------------------
# 3) 意图分类：语义判定用户意图（替代字面关键词匹配）
# ---------------------------------------------------------------------------
INTENT_CLASSIFY = PromptSpec(
    id="intent_classify",
    version="v3",
    description="把用户消息分为 compute/query_history/param_change/knowledge/capability/casual/reject，并做多轮指代消解",
    text="""你是"天枢一号"配电网潮流计算智能体的意图分类器。
只输出一个 JSON 对象，不要任何解释文字或代码块。格式：
{"intent":"...","confidence":0.0,"slots":{...},"reason":"..."}

intent 取值与含义：
- compute：用户要求**执行**潮流计算（首次计算、换算例、改参数后重算）
- query_history：查询**已有**计算结果，**不重新计算**。例如问母线数、变压器台数、
  最低/最高电压、网损、收敛状态、收敛容差、最大迭代、负载率、越限清单
- param_change：修改求解参数并要求重新计算（改容差、改迭代次数等）
- knowledge：问标准/规程/定义/原理/概念类知识（如国标电压偏差限值）
- capability：问"你能不能做到 X"
- casual：问候、寒暄、感谢
- reject：要求篡改已有计算结果、凭空编造数据、或要求给出明显不存在的结果

判定要点（务必遵守）：
1. 句子若是在"问已有结果的某个数/某个状态"，判为 query_history，**不是** compute。
2. 只有明确出现"算/计算/重算/跑一遍/求解/改参数后计算"等执行意图，才是 compute 或 param_change。
3. 技术词（电压、网损、收敛、容差、变压器、迭代）本身**不代表**要重算。
4. 同一句话既问结果又含"重新"等重算词时，判 compute。
5. confidence 反映你对判定的把握：语义明确给 0.85 以上，模糊给 0.5 左右。

指代消解（会话上下文里的 recent_turns）：
用户追问时常用"它 / 这个 / 那个 / 刚才的 / 上面那条 / 该 / 其"等指代，单看这一句无法理解。
recent_turns 给了**最近两轮的问答摘要**，先据此判断"这一轮在问什么"，再据此选 metric：
- 上一轮在问网损，这一轮说"它占负荷多少" → 仍是 query_history + losses；
- 上一轮刚答过某条母线的电压，这一轮说"那它正常吗" → 仍是 query_history + bus_voltage，
  母线沿用上一条里点名的那条；
- 上一轮刚算完，这一轮说"再算一次" → compute，算例以上一轮为准。
含指代时，额外在 slots 里给出 resolved_query：把指代补全成一句**能独立看懂**的问题
（例："系统网损占总负荷的比例是多少"）。不含指代就不要填 resolved_query。
**严禁借上下文编造用户没提过的算例名、母线名或数值**；上下文与本轮消息冲突时，以本轮消息为准。

⚠️ 若 recent_turns 为空（新会话、确实没有可承接的上下文），**不要凭猜测去消解指代**：
此时遇到含指代词的消息，confidence 不得高于 0.5，并且**不要**给出 resolved_query
（宁可让下游反问"你指的是哪一项"，也不要猜错还说得斩钉截铁）。

slots 可选字段：
- case_ref：算例名。
- metric：**被问的指标**。当 intent 为 query_history 时必填，取值只能是下列之一：
  min_voltage 最低电压 ｜ max_voltage 最高电压 ｜ voltage_overview 电压概况（最低+最高）
  voltage_lowest_list 电压最低的若干个母线 ｜ voltage_violations 电压越限母线清单
  losses 网损（有功/无功/占负荷比） ｜ generation 源侧供电与发电注入
  load_total 总负荷与净负荷 ｜ power_balance 功率平衡残差 ｜ convergence 收敛状态与迭代次数
  branch_overload 过载支路数量 ｜ branch_loading_top 负载率排名 ｜ reverse_flow 反向潮流
  unbalance 三相不平衡度 ｜ phase_voltage 指定相的最低电压 ｜ topology 网络拓扑（辐射/环网）
  device_counts 设备数量 ｜ voltage_levels 电压等级 ｜ solver_config 求解参数（容差/迭代/模式）
  fidelity 转换保真度与等价性 ｜ case_info 算例来源与指纹
  result_overview 结果概况 ｜ case_statistics 算例全量统计
  bus_voltage **问句点名了某条具体母线**时的该母线电压（点位查询）
  capability_timeseries 24h 时序扫描（未实现）｜ capability_n1 N-1 开断（未实现）
  capability_q_limits 发电机无功限值/PV→PQ（未实现）｜ capability_phase_shift 变压器相移（未实现）
  capability_tiny_impedance 极小阻抗 ｜ capability_safety 运行安全性判定
  **判定要点：按语义选最贴近的一项。同一件事换措辞问，必须给出同一个 metric。**
  例：「最大电压是多少」「电压最高多少」「哪条母线电压最高」→ 一律 max_voltage；
     「变压器几台」「有多少变压器」「变压器数量」→ 一律 device_counts + device=transformer；
     「母线 F3_10_3 的电压是多少」「F3_10_3 这条母线电压正常吗」→ 一律 bus_voltage
     （**判据：问句里点名了一条具体母线**；只是问"最低/最高在哪条母线"仍归 min/max_voltage）。
     「最低电压那条母线属于哪个电压等级」「最高电压在哪一级电压」→ 仍归 min_voltage /
     max_voltage（**等级归属会随最低/最高电压一起给出**，不要因此改判 device_counts）。
     注意：「这个算例有几个电压等级」「最高最低差几倍」→ voltage_levels，与上面不同。
  确实无法对应到任何一项时填 null。
- device：metric=device_counts 时指明设备种类，取值 transformer / bus / line / load /
  generator / pvsystem / storage / capacitor / regulator。
- top_n：metric 为 voltage_lowest_list 或 branch_loading_top 时，用户要求的条数（整数）。
- phase：metric=phase_voltage 时取值 A / B / C。
- solver_field：metric=solver_config 时取值 tolerance / max_iterations / solve_mode。
- params：{"max_iterations":..,"tolerance":..}。
- resolved_query：**仅当**用户消息含指代（它 / 这个 / 那个 / 刚才 / 上面 / 该 / 其）时给出，
  为把指代补全后的完整问题。它只用于留痕与排查，**不参与动作选择** —— 动作仍由 intent 决定。
""",
)

# ---------------------------------------------------------------------------
# 4) 知识库问答：基于检索片段作答（P1）
#
# 此前 RAG 只把检索片段**原样贴出来**，没有生成 —— 即"有检索、没生成"，
# 用户要自己从片段里找答案。这里补上生成侧。
# 边界不变：只喂片段、只要求归纳、必须标出处、资料不足必须明说。
# ---------------------------------------------------------------------------
RAG_ANSWER = PromptSpec(
    id="rag_answer",
    version="v1",
    description="知识库问答：只依据检索片段作答、必须标出处、资料不足则明说",
    text="""你是配电网专业资料助手。下面是从知识库检索到的资料片段，每条都带出处文件名。

硬性要求：
1) **只允许依据这些片段作答**。片段里没有的内容，直接说"给出的资料不足以回答这一点"；
   绝不许用自己的知识补充，更不许编造标准编号、条款号或限值。
2) 每条结论后面用（出处：文件名）标注来源；有多个来源就都标上。
3) 若片段之间互相矛盾，把矛盾指出来，不要自行选一个。
4) 只做资料归纳，**不下"合格 / 安全 / 应当"这类工程结论**。
5) 用 3-6 句通顺的中文写成一段话；不要输出 JSON、不要罗列字段名。

问题：{query}
资料片段：
{snippets}""",
)


# ---------------------------------------------------------------------------
# 5) 故障恢复决策：未收敛时在**候选白名单**里选恢复动作
#
# 这是本项目第一个"让模型参与决策"的入口（此前的 4 个提示词都只做翻译）。
# 边界刻意收得很紧：**模型只选方向，不给参数**。
#   候选动作由调用方（skills/powerflow_skill.py 的 `_REPLAN_ACTIONS`）给出，
#   具体参数（迭代次数/容差/降级对象）一律由代码按模板生成 ——
#   模型拿不到数值，也就不可能编造数值。
# ---------------------------------------------------------------------------
REPLAN_CHOOSE = PromptSpec(
    id="replan_choose",
    version="v1",
    description="未收敛时的恢复动作选择：只在候选白名单内选，不得给出数值建议",
    text="""你是配电网潮流计算的故障恢复决策器。上一次求解**未收敛**，请从「候选动作」中选一个最合适的。

只输出一个 JSON 对象，不要任何解释文字或代码块：
{"strategy":"候选动作的 id","reason":"一句话判断依据"}

候选动作（**只能从这里面选**，不得自创 id）：
{candidates}

失败上下文：
{context}

规则（务必遵守）：
1) strategy 必须是候选里出现过的 id；若你认为这些动作都不适合（重规划无益），填 "abort"。
2) **绝对不要给出任何数值建议** —— 不要提迭代次数、容差、电压或阻抗的取值。
   具体参数由系统按所选策略的模板生成，你只负责选方向。
3) reason 用一句话说明依据，**不得编造上下文里没有的现象**（例如算例里没有的机组类型）。
4) 判断参考：
   - 迭代次数用尽 / 容差过严导致的未收敛 → 放宽数值类动作更合适；
   - 报错指向多台电压控制机组互相争夺调压 → 降级建模类动作更合适；
   - 上下文不足以判断时，选候选里的第一个（系统会在失败后继续给你第二次选择）。""",
)


# ---------------------------------------------------------------------------
# 6) 算例名消歧：用户点名的算例本机没有时，在**真实可用清单**里找最近的
#
# 与 replan_choose 同构（白名单内选），但用途刻意收得更窄：
# 返回值**只用来生成一句确认问句**，绝不用于自动改算例 ——
# "用户点名了算例就必须算那个，找不到要显式报错，不许悄悄换别的算例算"
# 是这个项目的既有硬原则（见 chat_api 的注释）。
# ---------------------------------------------------------------------------
CASE_DISAMBIGUATE = PromptSpec(
    id="case_disambiguate",
    version="v1",
    description="点名算例不存在时，在真实可用清单内做语义匹配，产出确认问句",
    text="""你是"天枢一号"的算例名解析器。用户点名了一个算例，但本机**没有**这个算例。
下面是本机实际可用的算例清单，请判断用户最可能指的是哪一个。

只输出一个 JSON 对象，不要任何解释文字或代码块：
{"match":"候选里的 id","confidence":0.0,"question":"一句追问"}

候选算例（**只能从这里面选**，不得自创名字）：
{candidates}

用户原话：{message}

规则（务必遵守）：
1) match 必须是候选里出现过的 id；若没有任何一个像是用户想要的，填 null。
2) **绝对不许编造算例名**。question 里提到的算例名，也必须是候选里出现过的。
3) 不得因为"名字里都有 case"就硬凑 —— 毫无把握时填 null，confidence 低于 0.5。
4) confidence 反映把握：只是大小写/分隔符差异给 0.9 以上；语义接近
   （如"上次那个样例"）给 0.5~0.7；完全没线索给 0.0~0.3。
5) question 用一句话向用户确认（必要时把候选列出来让用户挑）。
   **不要给出任何计算参数建议，也不要下任何结论**。""",
)


# ---------------------------------------------------------------------------
# 7) 失败/降级诊断：把"为什么没收敛、系统做了什么"讲成人话
#
# 此前模型的判断依据（replan.json 里的 reason）**只落盘、用户看不到**，
# 用户界面里只有一句写死的模板。这里补的是"把已有事实说清楚"，
# 属于**解释**而非**决策**：不改数值、不进证据链、不给参数建议。
# ---------------------------------------------------------------------------
FAILURE_DIAGNOSE = PromptSpec(
    id="failure_diagnose",
    version="v1",
    description="把求解失败/受控重规划的既有事实转成 3-5 句中文说明（禁新增事实）",
    text="""你是配电网潮流计算的故障诊断解说员。本次求解经历过**未收敛与受控重规划**
（或最终仍未成功）。请把下面的事实转成 3-5 句通顺的中文，说清三件事：
① 为什么会这样 —— 只能依据事实里的错误码、错误信息与已采用过的恢复动作；
② 系统为此做了什么 —— 采用了哪个恢复动作、改变了什么口径；
③ 用户该怎么理解这次结果的边界（例如"降级后这些电源不再支撑电压，电压水平应按下限理解"）。

要求：
1) **只允许使用下面「事实」里出现的内容**，禁止新增任何数值、元件名或算例名。
2) 不要输出 JSON、不要只输出一个数值、不要罗列字段名；写成一段话。
3) 不要给出"把参数改成 X 就能收敛"这类具体操作建议 —— 参数由系统按模板决定。
4) 不要下"结果不可用 / 必须重跑 / 安全 / 合格"这类结论性判断，只描述现象与边界。
事实：{facts}""",
)


# ---------------------------------------------------------------------------
# 8) 澄清反问：意图不够明确时，按"缺哪一项"生成针对性追问
#
# 此前的澄清是一段写死的文案，不管缺的是算例、参数还是指标，话都一样。
# 这里让模型只做一件事：把"该问什么"问准。不执行、不报数、不选路。
# ---------------------------------------------------------------------------
CLARIFY_ASK = PromptSpec(
    id="clarify_ask",
    version="v1",
    description="意图不明确时按触发原因生成一句针对性追问（禁编造算例名）",
    text="""你是"天枢一号"配电网潮流计算智能体的澄清助手。系统判断用户这句话
**意图不够明确**，不适合直接执行。请生成 1-2 句针对性的追问，帮用户把话说清楚。

触发原因：{reason}
已知信息：{slots}
可用的算例候选（仅当原因与算例有关时才用得上；**提到算例名必须从这里选**）：
{candidates}
用户原话：{message}

规则（务必遵守）：
1) 只做追问，**不要执行、不要下结论、不要报任何数字**。
2) 不要反问用户已经说清楚的事 —— 按"触发原因"只问真正缺的那一项。
3) **绝对不许编造算例名、指标名或参数名**；提到的算例名必须在候选清单里。
4) 用 1-2 句中文；必要时给 2-3 个可选项让用户挑
   （例如"是想重新计算，还是查上次的结果？"）。
5) 不要输出 JSON、不要用代码块。""",
)


_REGISTRY: dict[str, PromptSpec] = {
    p.id: p for p in (
        TASK_PARSE, RESULT_EXPLAIN_TEMPLATE, INTENT_CLASSIFY, RAG_ANSWER, REPLAN_CHOOSE,
        CASE_DISAMBIGUATE, FAILURE_DIAGNOSE, CLARIFY_ASK,
    )
}


def get_prompt(prompt_id: str) -> str:
    spec = _REGISTRY.get(prompt_id)
    if spec is None:
        raise KeyError(f"未注册的提示词 id：{prompt_id}（可用：{', '.join(sorted(_REGISTRY))}）")
    return spec.text


def get_spec(prompt_id: str) -> PromptSpec:
    spec = _REGISTRY.get(prompt_id)
    if spec is None:
        raise KeyError(f"未注册的提示词 id：{prompt_id}")
    return spec


def prompt_versions() -> dict[str, str]:
    """返回 {id: "version(digest)"}，用于写入任务版本集。"""
    return {pid: f"{spec.version}({spec.digest})" for pid, spec in sorted(_REGISTRY.items())}


def prompt_version_line() -> str:
    return " ".join(f"{pid}={v}" for pid, v in prompt_versions().items())
