"""全局配置与仿真默认阈值.

约束优先级（设计文档 13.2）：
  P0 安全与 Capability Gate > P1 显式用户约束 > P2 任务/算例显式配置 > P3 项目冻结默认
本模块只承载 P3 级默认值；任何默认阈值在报告中必须标注 "采用仿真默认阈值 sim-defaults-v1.1"。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULTS_VERSION = "sim-defaults-v1.1"

# 配置默认路径必须锚定项目根目录，不能依赖进程当前工作目录。
# OpenDSS 及部分 Web 启动方式可能改变 cwd，使用 cwd 会导致同一任务的
# workspace/outputs 在不同目录之间漂移，历史产物因此无法被聊天层读取。
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


@dataclass(frozen=True)
class SimDefaults:
    """仿真默认阈值（P3，仅在算例字段与用户配置均未提供时使用）。"""

    version: str = DEFAULTS_VERSION
    # 电压合格区间 (pu)
    voltage_min_pu: float = 0.95
    voltage_max_pu: float = 1.05
    # OpenDSS 官方默认求解参数
    max_iterations: int = 15
    tolerance: float = 1e-4
    # 功率平衡 residual 容差（相对总负荷比例）
    power_balance_tol_rel: float = 1e-3
    # Absolute roundoff allowance (kW), not permission for negative physical
    # loss. Zero-load phase-bank COM produces about -2e-7 kW by cancellation.
    loss_roundoff_kw: float = 1e-3
    # 负载率限值 (%)
    loading_limit_pct: float = 100.0


@dataclass
class AgentConfig:
    """运行配置。可用环境变量覆盖。"""

    # 本地大模型（Ollama，OpenAI 兼容接口）
    llm_base_url: str = field(
        default_factory=lambda: os.environ.get("DPF_LLM_BASE_URL", "http://localhost:11434/v1")
    )
    llm_model: str = field(default_factory=lambda: os.environ.get("DPF_LLM_MODEL", "qwen2.5:7b"))
    llm_api_key: str = field(default_factory=lambda: os.environ.get("DPF_LLM_API_KEY", "ollama"))
    llm_timeout_s: float = 120.0
    # 「结果解释」专用超时，远小于 llm_timeout_s。
    #
    # 原因（实测）：一次潮流计算的核心流程只要 3 秒，但 _explain() 调 LLM
    # 把结果"换个说法"时，遇到远端不可达会干等满 120 秒才超时，用户看到的就是
    # "界面卡住两分钟"。而 explain 失败时会退回 fallback——一个确定性模板，
    # 里面已经包含 Vmin/网损/判据等全部关键数值（prompt 也明令禁止 LLM 新增数值）。
    # 也就是说等满 2 分钟拿到的答案，和立刻 fallback 拿到的完全一样。
    # 所以这里单独给一个短超时：等不到就走模板，用户几乎无感。
    llm_explain_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DPF_LLM_EXPLAIN_TIMEOUT", "12"))
    )
    llm_enabled: bool = field(
        default_factory=lambda: os.environ.get("DPF_LLM_ENABLED", "1") not in ("0", "false")
    )
    llm_max_retries: int = 3  # 文档 10.2：连续 3 次失败后进入 fallback

    # 路径
    workspace_root: str = field(
        default_factory=lambda: os.environ.get("DPF_WORKSPACE", os.path.join(PROJECT_ROOT, "tasks"))
    )
    outputs_root: str = field(
        default_factory=lambda: os.environ.get("DPF_OUTPUTS", os.path.join(PROJECT_ROOT, "outputs"))
    )
    cases_root: str = field(
        default_factory=lambda: os.environ.get(
            "DPF_CASES", os.path.join(os.path.dirname(os.path.dirname(__file__)), "cases")
        )
    )

    defaults: SimDefaults = field(default_factory=SimDefaults)

    # 引擎健康检查有效期 (s)
    engine_health_ttl_s: float = 300.0
    # 已验证求解产物缓存：同输入可发现历史产物；原生迭代状态无法序列化，
    # 每次仍真实求解并独立复算，完整验证通过后更新产物，不宣称缓存加速。
    solve_cache_enabled: bool = field(
        default_factory=lambda: os.environ.get("DPF_SOLVE_CACHE", "1") not in ("0", "false")
    )
    # 转换产物新鲜度：引用老算例时校验转换器版本，不一致则自动重建（防"转换器改了但老算例不重转"）
    conversion_cache_enabled: bool = field(
        default_factory=lambda: os.environ.get("DPF_CONVERSION_CACHE", "1") not in ("0", "false")
    )
    # 强制重建：无视版本一致性，每次引用都重新转换（调试转换器时用，平时保持关闭）
    conversion_force_refresh: bool = field(
        default_factory=lambda: os.environ.get("DPF_CONVERSION_FORCE", "0") in ("1", "true")
    )
    # 意图理解层（根治"字面关键词误判"）：
    #   off    = 完全沿用旧规则路由（兜底开关）
    #   hybrid = 规则短路 + LLM 语义判定（默认）
    #   llm    = 全部走 LLM 判定
    intent_mode: str = field(
        default_factory=lambda: os.environ.get("DPF_INTENT_MODE", "hybrid")
    )
    # 意图判定的超时：远小于任务解析，避免拖慢交互
    llm_intent_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DPF_LLM_INTENT_TIMEOUT", "8"))
    )
    # 昂贵动作（重新计算）所需的最低置信度，低于此值改为反问确认而非直接重算
    intent_confirm_threshold: float = field(
        default_factory=lambda: float(os.environ.get("DPF_INTENT_CONF", "0.55"))
    )
    # 求解超时 (s)
    solve_timeout_s: float = 60.0
    # 仅结果复算仍漂移时继续同一 snapshot；总调用数包括初次、二次与最终独立复算。
    stability_max_solves: int = 16
    stability_timeout_s: float = 60.0

    # COM 引擎单次调用超时 (s)。
    # 背景：曾出现一次引擎调用卡死后，因复用同一引擎实例导致后续任务连续各卡 120s。
    # 默认COM超时终止任务专属原生进程，下个任务创建新上下文；关闭回收另有有界等待。
    # 编译/校验类调用远快于求解，用同一上限会让异常分支的代价被放大——
    # 故拆为两个：编译校验用较短上限，求解保留较长上限。
    com_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DPF_COM_TIMEOUT", "120"))
    )
    # 编译 / 结构校验 / 属性设置类调用的超时（正常 < 10s）
    com_quick_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DPF_COM_QUICK_TIMEOUT", "45"))
    )
    # Production COM uses a task-scoped process. The legacy thread channel is
    # explicit opt-in for diagnostic comparisons, not an automatic fallback.
    com_isolation: str = field(default_factory=lambda: os.environ.get("DPF_COM_ISOLATION", "process"))

    # 潮流引擎后端选择（三通道）：
    #   "com"  —— 本机独立安装的 OpenDSS（OpenDSSEngine.DSS COM 组件，需 pywin32）。
    #             **默认**。原因：dss-python 在当前机器环境不可用（import 即挂死/崩溃，
    #             与版本无关，根因在系统层）；而 CLI 通道的 CSV 只有 5~6 位有效数字，
    #             达不到本项目的验收精度。
    #   "dss-python" —— 原内嵌引擎，保留作为回退（本机切了跑不起来）。
    #   "cli"  —— 官方 `OpenDSScmd.exe`（本身即独立进程）。**定位是对照 / 离线验证**，
    #             不是生产默认：可独立命令行对照；生产COM现在也有任务进程隔离。
    #             （离线可复现，不依赖"应用正在运行"）；代价是精度、速度，
    #             以及 3 个字段要靠在源文件里扫描补齐（端点母线 / RegControl / 变压器 kVA）。
    engine_backend: str = field(
        default_factory=lambda: os.environ.get("DPF_ENGINE_BACKEND", "com")
    )
    # CLI 通道专用：可执行文件路径（留空则自动探测：环境变量 → COM 注册表推导 → 常见路径）
    opendss_cli_path: str = field(
        default_factory=lambda: os.environ.get("DPF_OPENDSS_CLI", "")
    )
    # CLI 通道的硬超时（快慢分离，与上面的 COM 通道同一思路）。
    # 实测某些求解设置组合会让 CLI 显著变慢甚至挂死，超时会强制 kill 进程
    # COM默认通道也在独立进程内；CLI对照仍有导出精度和连续迭代上下文差异。
    #
    # ⚠️ **预算约束（改动前必读）**：一个任务最多会跑两次 CLI 批处理 ——
    # 静态阶段（compile + show buses）+ 求解阶段（solve + 全部 export）。
    # 两者的超时之和**必须显著小于**看门狗的「任务锁被占」兜底阈值
    # （`runtime/watchdog.py` 的 `_TASK_STUCK_S`，默认 300s）。
    # 否则：算例一慢就先把后端重启了，而 CLI 的强杀机制根本没机会生效。
    # 曾经的缺陷：两处都用 180 → 最坏 360s > 300s，实测真的触发了重启。
    # 当前预算：45（静态）+ 120（求解）= 165s，余量 135s。
    # 该关系由 `tests/test_cli_adapter.py` 的 E 组断言锁住。
    #
    # 对比 COM 通道：那边各调用叠加最坏可到 405s（> 300s），但**不需要也不应该**
    # 用同样的约束去改——进程内的 COM 线程**杀不掉**，等看门狗兜底重启是它唯一
    # 可行的失败路径。CLI 不同：它跑在独立进程里，**「能强杀」正是这条通道的
    # 存在理由**，所以其超时必须留在兜底阈值之下，让强杀真正生效、后端不受牵连。
    cli_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DPF_CLI_TIMEOUT", "120"))
    )
    # 静态阶段（compile 后只做 show/export 结构信息，不求解）的超时。
    # 正常远快于求解；给同样的长超时会让异常分支的代价被放大。
    cli_quick_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DPF_CLI_QUICK_TIMEOUT", "45"))
    )

    # RAG（本地知识库检索）。详见 dpf_agent/rag/config.py。
    # 默认即开箱即用，指向 <project>/knowledge 与 outputs/rag_index。
    rag_enabled: bool = field(
        default_factory=lambda: os.environ.get("DPF_RAG_ENABLED", "1") not in ("0", "false")
    )
    rag_knowledge_dir: str = field(
        default_factory=lambda: os.environ.get(
            "DPF_RAG_KNOWLEDGE_DIR",
            os.path.abspath(os.path.join(PROJECT_ROOT, "knowledge")),
        )
    )
    rag_index_dir: str = field(
        default_factory=lambda: os.environ.get(
            "DPF_RAG_INDEX_DIR",
            os.path.abspath(os.path.join(PROJECT_ROOT, "outputs", "rag_index")),
        )
    )
    rag_top_k: int = field(
        default_factory=lambda: int(os.environ.get("DPF_RAG_TOP_K", "4"))
    )
    rag_min_score: float = field(
        default_factory=lambda: float(os.environ.get("DPF_RAG_MIN_SCORE", "0.6"))
    )
    rag_chunk_size: int = field(
        default_factory=lambda: int(os.environ.get("DPF_RAG_CHUNK_SIZE", "500"))
    )
    rag_chunk_overlap: int = field(
        default_factory=lambda: int(os.environ.get("DPF_RAG_CHUNK_OVERLAP", "80"))
    )

    # 外部 Power-RAG 知识库服务（localhost:5000，已装载 GB/T 45906 离线知识包）。
    # 作为 RAG 的"主检索"，本地 BM25（knowledge/ 目录）作为离线兜底。
    power_rag_enabled: bool = field(
        default_factory=lambda: os.environ.get("DPF_POWER_RAG_ENABLED", "1") not in ("0", "false")
    )
    power_rag_base_url: str = field(
        default_factory=lambda: os.environ.get("DPF_POWER_RAG_URL", "http://localhost:5000")
    )
    # 专用检索接口（/api/rag/search）的超时：三阶段流水线较慢，给足 60s
    power_rag_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DPF_POWER_RAG_TIMEOUT", "60"))
    )
    # 聊天注入场景的超时：必须远小于用户可感知延迟，超时即降级本地 BM25
    power_rag_chat_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("DPF_POWER_RAG_CHAT_TIMEOUT", "12"))
    )
    power_rag_top_k: int = field(
        default_factory=lambda: int(os.environ.get("DPF_POWER_RAG_TOP_K", "5"))
    )
    # LLM 查询改写 + HyDE 需联网且慢；聊天场景默认关闭以提速
    power_rag_use_rewrite: bool = field(
        default_factory=lambda: os.environ.get("DPF_POWER_RAG_REWRITE", "1") not in ("0", "false")
    )
    power_rag_use_reranker: bool = field(
        default_factory=lambda: os.environ.get("DPF_POWER_RAG_RERANK", "1") not in ("0", "false")
    )
    # 相关度门槛（Power-RAG 的 score 为 0~1）。低于此分的片段视为"领域不相关"，
    # 直接丢弃并回落本地库——避免把跨领域噪声硬贴进回答。
    #
    # 0.70 是实测标定的，不是拍脑袋：当前库是《变电站二次系统》，
    #   同域提问（变电站二次系统/通信报文/保护控制…）得分 0.82 ~ 0.97
    #   跨域提问（配电网潮流/网损/负载率/电压偏差…）得分 0.40 ~ 0.55
    # 门槛卡在两峰之间的谷底。换知识库后必须重新标定。
    power_rag_min_score: float = field(
        default_factory=lambda: float(os.environ.get("DPF_POWER_RAG_MIN_SCORE", "0.70"))
    )
    # Power-RAG 不可用/超时时，是否回落到本地 BM25 知识库
    power_rag_fallback_local: bool = field(
        default_factory=lambda: os.environ.get("DPF_POWER_RAG_FALLBACK", "1") not in ("0", "false")
    )


DEFAULT_CONFIG = AgentConfig()
