"""REST API 路由。"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..application.progress import is_finished
from ..application.service import ApplicationService, TaskBusyError
from ..config import AgentConfig
from ..runtime import narration as _narration
from ..schemas.enums import OutputFormat, ResultScope, TaskStatus
from ..schemas.task_spec import SolverConfig, UserRequest
from .schemas import RunRequest, RunResponse, TaskInfo

router = APIRouter()

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "static"))


@router.get("/", response_class=HTMLResponse, response_model=None)
def index() -> str:
    """返回前端首页。"""
    p = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read()
    return "<h1>天枢一号 Web 服务</h1><p>static/index.html 未找到</p>"


def _get_service(req: RunRequest | None = None) -> ApplicationService:
    """构造 ApplicationService；支持单次请求覆盖 llm_enabled。"""
    cfg = AgentConfig()
    if req and req.llm_enabled is not None:
        cfg.llm_enabled = req.llm_enabled
    return ApplicationService(cfg)


def _to_scopes(values: list[str]) -> list[ResultScope]:
    return [ResultScope(v.strip()) for v in values]


def _to_formats(values: list[str]) -> list[OutputFormat]:
    return [OutputFormat(v.strip()) for v in values]


@router.get("/api/metrics")
async def metrics() -> JSONResponse:
    """运行指标（成功率 / 耗时分位 / 阶段失败率 / 错误码分布）。

    数据来自既有 trace 与产物，不触发任何计算。结果缓存 30 秒，
    避免前端轮询时反复扫描全部任务目录。
    """
    global _METRICS_CACHE
    now = time.time()
    if _METRICS_CACHE and now - _METRICS_CACHE[0] < 30:
        return JSONResponse(_METRICS_CACHE[1])

    from starlette.concurrency import run_in_threadpool

    from ..evals.run_metrics import collect

    cfg = AgentConfig()

    def _collect():
        return collect(cfg.workspace_root, cfg.outputs_root)

    try:
        data = await run_in_threadpool(_collect)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    _METRICS_CACHE = (now, data)
    return JSONResponse(data)


_METRICS_CACHE: tuple[float, dict] | None = None


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> JSONResponse:
    """请求取消一个正在执行的任务。

    后端工作流在每个阶段入口检查取消标记，命中即中断并以 CANCELLED 终态结束，
    不再把整轮潮流算完（此前没有取消手段，只能杀进程）。
    """
    from ..runtime.cancellation import request_cancel

    request_cancel(task_id)
    # 这里只登记请求。工作流真正停止后由服务收尾更新进度，
    # 避免取消旧任务时改写另一任务，或把仍在求解的任务提前标为结束。
    return JSONResponse({"cancelled": True, "task_id": task_id})


@router.get("/api/storage")
async def storage_overview() -> JSONResponse:
    """本地存储盘点（只读）。

    统计 uploads / tasks / outputs 的占用与文件数，并给出可清理候选清单。
    **不修改任何文件**；清理须另行调用 /api/storage/archive 并显式给出清单。
    """
    from starlette.concurrency import run_in_threadpool

    from . import storage_admin

    cfg = AgentConfig()

    def _scan():
        return storage_admin.scan(cfg)

    try:
        data = await run_in_threadpool(_scan)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(data)


class ArchiveRequest(BaseModel):
    """归档请求：必须显式列出条目，不接受"全部清理"这类模糊指令。"""

    items: list[dict[str, Any]]
    archive_root: str | None = None
    confirm: bool = False


@router.post("/api/storage/archive")
async def storage_archive(payload: ArchiveRequest) -> JSONResponse:
    """把显式指定的条目归档到 `_archive/<时间戳>/`（移动，可回滚）。

    安全约束（在 storage_admin.archive 内强制）：
      * 单批最多 10 条；
      * 拒绝归档 chat.db、tasks/audit/、uploads 下含文件的会话目录；
      * 任一条失败即停止，并如实报告已完成部分。
    """
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="需显式确认（confirm=true）后才执行归档")
    if not payload.items:
        raise HTTPException(status_code=400, detail="未指定任何条目")

    from starlette.concurrency import run_in_threadpool

    from . import storage_admin

    cfg = AgentConfig()

    def _run():
        return storage_admin.archive(cfg, payload.items, payload.archive_root)

    try:
        result = await run_in_threadpool(_run)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(result)


@router.get("/api/task-progress")
async def task_progress() -> JSONResponse:
    """返回当前（最近一个）任务的执行阶段，供 UI 在等待期间轮询显示进度。

    同一时刻只跑一个任务，所以后端写的是单个「当前进度」文件。
    没有在跑的任务时返回 idle，UI 据此停止轮询。
    """
    from starlette.concurrency import run_in_threadpool

    cfg = AgentConfig()
    path = os.path.join(cfg.workspace_root, "_current_progress.json")
    if not os.path.isfile(path):
        return JSONResponse({"active": False, "stage": "idle", "label": "", "percent": 0})

    def _read():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    data = await run_in_threadpool(_read)
    if not data:
        return JSONResponse({"active": False, "stage": "idle", "label": "", "percent": 0})

    # 「已结束」= 已 done，或超过 STALE_AFTER_S 没更新（上次进程被 Kill /
    # 原生库段错误留下的脏状态）。
    # 判据单点定义在 application/progress.py —— 实时流 /api/tasks/stream
    # 用的是同一份；两处各写一份的下场是「这边说空闲、那边还在推旧任务」
    # （2026-10-09 实测）。
    done = is_finished(data)
    return JSONResponse({
        "active": not done,
        "stage": data.get("stage") or "",
        "label": data.get("label") or "",
        "percent": int(data.get("percent") or 0),
        "task_id": data.get("task_id") or "",
        "done": done,
    })


@router.get("/api/models")
async def list_models() -> JSONResponse:
    """拉取当前 LLM 服务上可用的模型列表。

    由后端代理请求，API Key 不暴露给渲染层。
    """
    from starlette.concurrency import run_in_threadpool

    payload = await run_in_threadpool(_fetch_models)
    return JSONResponse(payload)


@router.get("/api/local-models")
async def list_local_models(url: str = "http://localhost:11434/v1") -> JSONResponse:
    """拉取本地 Ollama 上已下载的模型列表。

    渲染层不直连 Ollama（避免暴露主机/端口给前端），由后端代理并做错误归一。
    Ollama 的 OpenAI 兼容 /v1/models 与原生 /api/tags 都试，前者返回更接近 OpenAI 格式。
    """
    from starlette.concurrency import run_in_threadpool
    base = (url or "http://localhost:11434/v1").rstrip("/")
    payload = await run_in_threadpool(_fetch_local_models, base)
    return JSONResponse(payload)


def _fetch_local_models(base: str) -> dict:
    """探测 Ollama 列出本地模型：先试 OpenAI 兼容 /v1/models，再退回原生 /api/tags。"""
    # OpenAI 兼容端点
    try:
        with urllib.request.urlopen(base + "/models", timeout=6) as r:
            d = json.loads(r.read())
        items = d.get("data", []) if isinstance(d, dict) else d
        names = [it.get("id") for it in items if isinstance(it, dict) and it.get("id")]
        if names:
            return {"ok": True, "models": names, "count": len(names), "backend": "ollama-openai", "base": base}
    except Exception:
        pass
    # 原生 /api/tags（更通用）
    try:
        # /v1 → /api 替换
        tags = base[:-3] + "api/tags" if base.endswith("/v1") else base.rstrip("/") + "/api/tags"
        with urllib.request.urlopen(tags, timeout=6) as r:
            d = json.loads(r.read())
        models = d.get("models", []) if isinstance(d, dict) else []
        names = [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]
        if names:
            return {"ok": True, "models": names, "count": len(names), "backend": "ollama-native", "base": base}
    except Exception as exc:
        return {"ok": False, "models": [], "count": 0,
                "error": f"连不上 Ollama（{base}）：{exc}。先双击 Ollama 图标启动服务。"}
    return {"ok": False, "models": [], "count": 0, "error": "Ollama 已连上但没拉任何模型。先在终端跑：ollama pull qwen2.5:7b"}


def _fetch_models() -> dict:
    cfg = AgentConfig()
    base = (cfg.llm_base_url or "").rstrip("/")
    if not base:
        return {"ok": False, "models": [], "count": 0, "error": "未配置 llm_base_url"}

    url = base + "/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {cfg.llm_api_key}",
        "Content-Type": "application/json",
    })
    try:
        # 超时故意设短（8s）：这个接口在前端"模型服务"页会被调用，
        # 网络不通时操作系统要等很久才返回（实测 WinError 10051 要 ~20s），
        # 会拖慢整条链路。宁可快速失败并给出明确提示。
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "models": [], "count": 0, "error": f"HTTP {exc.code}：{exc.reason}"}
    except Exception as exc:
        return {"ok": False, "models": [], "count": 0, "error": f"拉取失败：{exc}"}

    items = data.get("data", []) if isinstance(data, dict) else data
    models: list[str] = []
    for m in items or []:
        mid = m.get("id") if isinstance(m, dict) else m
        if mid:
            models.append(str(mid))

    return {
        "ok": True,
        "models": models,
        "count": len(models),
        "base_url": base,
        "current": cfg.llm_model,
    }


@router.get("/api/health")
def health() -> JSONResponse:
    """轻量存活检查（main.js 启动探活 + bootstrap 都依赖此接口，不能改路径）。"""
    cfg = AgentConfig()
    return JSONResponse({
        "status": "ok",
        "service": "tianshu-1",
        "llm_base_url": cfg.llm_base_url,
        "llm_model": cfg.llm_model,
    })


@router.get("/api/ready")
async def ready() -> JSONResponse:
    """桌面 UI 就绪度检测：异步实现，避开 dss-python 在请求线程首次 import 死锁。
    注意：OpenDSS native 库必须在 `create_app()` 中预加载（见 server.py）。"""
    cfg = AgentConfig()
    cases_root = cfg.cases_root
    case_count = 0
    cases_ok = os.path.isdir(cases_root)
    if cases_ok:
        case_count = sum(
            1 for name in os.listdir(cases_root)
            if os.path.isfile(os.path.join(cases_root, name, "Master.dss"))
        )

    def _probe_rag() -> tuple[bool, str]:
        """知识库就绪度：Power-RAG（外部，主）优先，本地 BM25（兜底）次之。

        只要有一路可用就算就绪——聊天层会自动选路，离线时不会因为没有
        Power-RAG 而判定为不可用。
        """
        from ..rag.pipeline import get_pipeline
        try:
            st = get_pipeline().status()
        except Exception as exc:  # noqa: BLE001
            return False, f"知识库模块异常：{exc}"

        pr = st.get("power_rag") or {}
        local_ready = bool(st.get("index_exists")) and bool(st.get("knowledge_exists"))

        if pr.get("enabled") and pr.get("ok"):
            pts = pr.get("points_count")
            col = pr.get("collection") or "?"
            base = f"Power-RAG 在线：{pts} 个向量片段 / 集合 {col}"
            if local_ready:
                return True, f"{base}；本地兜底库 {st.get('num_chunks')} 片段"
            return True, base

        # Power-RAG 不可用：看本地兜底
        reason = pr.get("detail") or "Power-RAG 未启用"
        if local_ready:
            return True, (
                f"本地知识库就绪：{st.get('num_sources')} 份文档 / "
                f"{st.get('num_chunks')} 个片段（{reason}）"
            )
        if st.get("knowledge_exists"):
            return True, f"知识库目录就绪但未建索引（{reason}；调 /api/rag/ingest）"
        return False, f"无可用知识库：{reason}；本地目录 {st.get('knowledge_dir')} 不存在"

    model_ok = bool(cfg.llm_enabled and cfg.llm_base_url and cfg.llm_model)
    model_detail = f"{cfg.llm_model} @ {cfg.llm_base_url}（实际可达性在首次推理时验证）"

    # ⚠️ 这几个探测内部会做同步 IO（读盘、探测引擎、调知识库）——**必须丢到线程池**。
    # 本端点是 async 的，直接在事件循环里调用它们，就会把整个循环堵住：
    # 实测知识库探测在代理配置下会卡 8~12 秒，期间**同一进程内所有请求（含实时
    # 事件流）全部停摆** —— 界面上表现为"任务跑完过程才一次性出现"。
    # 教训：async 函数里不允许出现任何同步 IO，哪怕是"看起来很快"的探测。
    from starlette.concurrency import run_in_threadpool

    engine_ok, engine_detail = await run_in_threadpool(_probe_engine)
    storage_ok, storage_detail = await run_in_threadpool(_probe_storage, case_count)
    rag_ok, rag_detail = await run_in_threadpool(_probe_rag)

    sidecar_ok = True
    sidecar_detail = "FastAPI 8000 健康检查自检通过"
    license_ok = True
    license_detail = "自有源码采用 Apache-2.0；引擎与依赖遵循各自许可，见 THIRD_PARTY_NOTICES.md"
    skills_ok, skills_detail = await run_in_threadpool(_probe_skills)

    def map_level(ok: bool) -> str:
        return "ok" if ok else "warn"

    return JSONResponse({
        "overall_ok": all([model_ok, engine_ok, sidecar_ok, license_ok, storage_ok, skills_ok]),
        "checks": {
            "model":     {"ok": model_ok,   "level": map_level(model_ok),   "detail": model_detail},
            "engine":    {"ok": engine_ok,  "level": map_level(engine_ok),  "detail": engine_detail},
            "sidecar":   {"ok": sidecar_ok, "level": map_level(sidecar_ok), "detail": sidecar_detail},
            "license":   {"ok": license_ok, "level": map_level(license_ok), "detail": license_detail},
            "storage":   {"ok": storage_ok, "level": map_level(storage_ok), "detail": storage_detail},
            "skills":    {"ok": skills_ok,  "level": map_level(skills_ok),  "detail": skills_detail},
            "knowledge": {"ok": rag_ok,     "level": map_level(rag_ok),     "detail": rag_detail},
        },
    })


def _compute_ready() -> dict:
    cfg = AgentConfig()
    cases_root = cfg.cases_root
    case_count = 0
    cases_ok = os.path.isdir(cases_root)
    if cases_ok:
        case_count = sum(
            1 for name in os.listdir(cases_root)
            if os.path.isfile(os.path.join(cases_root, name, "Master.dss"))
        )

    # 模型：就绪度检查不主动探测 LLM（Ollama 不可达会拖慢或卡住响应），
    # 实际可达性在首次推理时验证。这里只读取配置。
    model_ok = bool(cfg.llm_enabled and cfg.llm_base_url and cfg.llm_model)
    model_detail = f"{cfg.llm_model} @ {cfg.llm_base_url}（实际可达性在首次推理时验证）"

    # 引擎：当前内置 OpenDSS（dss-python）；导入失败即引擎未就绪
    engine_ok, engine_detail = _probe_engine()

    # 本地存储：cases 目录 + outputs 目录可写
    storage_ok, storage_detail = _probe_storage(case_count)

    sidecar_ok = True
    sidecar_detail = "FastAPI 8000 健康检查自检通过"
    license_ok = True
    license_detail = "自有源码采用 Apache-2.0；引擎与依赖遵循各自许可，见 THIRD_PARTY_NOTICES.md"

    skills_ok, skills_detail = _probe_skills()

    def map_level(ok: bool) -> str:
        return "ok" if ok else "warn"

    return {
        "overall_ok": all([model_ok, engine_ok, sidecar_ok, license_ok, storage_ok, skills_ok]),
        "checks": {
            "model":     {"ok": model_ok,   "level": map_level(model_ok),   "detail": model_detail},
            "engine":    {"ok": engine_ok,  "level": map_level(engine_ok),  "detail": engine_detail},
            "sidecar":   {"ok": sidecar_ok, "level": map_level(sidecar_ok), "detail": sidecar_detail},
            "license":   {"ok": license_ok, "level": map_level(license_ok), "detail": license_detail},
            "storage":   {"ok": storage_ok, "level": map_level(storage_ok), "detail": storage_detail},
            "skills":    {"ok": skills_ok,  "level": map_level(skills_ok),  "detail": skills_detail},
        },
    }


def _probe_llm(cfg: AgentConfig) -> tuple[bool, str]:
    """显式探测 LLM 可达性（会真的发一次 /models 请求，最多等 5s）。

    ⚠️ 当前就绪检查（readiness）**不调用本函数** —— 那里只读配置、不主动探测，
    因为 Ollama 不可达时会把接口拖慢甚至卡住。本函数保留给"用户主动点自检"这类场景。

    历史缺陷（本轮发现并修正）：这里原写的是 `client.is_reachable()`，
    而 ModelClient 从来没有这个属性（只有 `available()`）→ 每次调用必抛
    AttributeError，被下面的 except 吞成 "LLM 自检跳过"，即**自检永远不生效**。
    这类"有实现、但从未真正生效"的缺陷在本项目已出现多次，值得当心。
    """
    if not cfg.llm_enabled:
        return True, f"LLM 已禁用，使用规则解析（模型 {cfg.llm_model}）"
    try:
        from ..runtime.model_client import ModelClient
        client = ModelClient(cfg)
        if not client.available():
            return False, f"模型不可达：{cfg.llm_base_url} / {cfg.llm_model}"
        return True, f"{cfg.llm_model} @ {cfg.llm_base_url}"
    except Exception as exc:
        return True, f"LLM 自检跳过：{exc}"


def _probe_engine() -> tuple[bool, str]:
    """检测 OpenDSS 引擎是否可用。

    注意：不能用 `import dss`——dss-python 原生库初始化在 Electron 派生的子进程
    环境里会死锁/崩溃（实测 import 即 0xC0000005）。当前默认引擎后端为
    "com"（本机独立安装的 OpenDSS COM 组件），只检查注册表中的 COM 注册项，
    不实例化引擎、不初始化原生库；实际求解时由 opendss_com_adapter 按需加载。
    """
    try:
        backend = (getattr(AgentConfig(), "engine_backend", "com") or "com").lower()
        if backend == "com":
            import winreg

            winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, r"OpenDSSEngine.DSS\CLSID"
            ).Close()
            return True, "OpenDSS COM 引擎已注册（求解时按需加载）"
        if backend == "cli":
            # CLI 通道：只做**定位**，不启动进程（探测不产生副作用）。
            from ..engines.cli_runner import discover_cli_path

            exe, why = discover_cli_path()
            if exe:
                return True, f"OpenDSS CLI 已就绪（{exe}）"
            return False, f"OpenDSS CLI 不可用：{why}"
        import importlib.util

        spec = importlib.util.find_spec("dss")
        if spec is None:
            return False, "dss-python 未安装"
        return True, "OpenDSS / dss-python 可用（求解时按需加载）"
    except Exception as exc:
        return False, f"OpenDSS 检测失败：{exc}"


def _probe_storage(case_count: int) -> tuple[bool, str]:
    cases_root = AgentConfig().cases_root
    outputs_root = AgentConfig().outputs_root
    cases_ok = os.path.isdir(cases_root)
    out_writable = os.path.isdir(outputs_root) and os.access(outputs_root, os.W_OK)
    if not cases_ok:
        return False, f"cases 目录不可访问：{cases_root}"
    if not out_writable:
        return False, f"outputs 目录不可写：{outputs_root}"
    return True, f"cases={case_count}, outputs 可写"


def _probe_skills() -> tuple[bool, str]:
    try:
        from ..tools.powerflow_tools import TOOL_REGISTRY
        return True, f"{len(TOOL_REGISTRY)} 个标准工具已注册"
    except Exception as exc:
        return False, f"工具注册表加载失败：{exc}"


@router.get("/api/cases")
def list_cases() -> JSONResponse:
    """列出内置算例。"""
    cases_root = AgentConfig().cases_root
    cases: list[dict[str, str]] = []
    if os.path.isdir(cases_root):
        for name in sorted(os.listdir(cases_root)):
            master = os.path.join(cases_root, name, "Master.dss")
            if os.path.isfile(master):
                cases.append({"name": name, "master_dss": master})
    return JSONResponse({"cases": cases})


@router.post("/api/run", response_model=RunResponse)
def run_task(payload: RunRequest, background_tasks: BackgroundTasks) -> RunResponse:
    """同步执行一次潮流计算任务并返回结果。"""
    service = _get_service(payload)

    sc = SolverConfig()
    if payload.max_iterations is not None:
        sc.max_iterations = payload.max_iterations
    if payload.tolerance is not None:
        sc.tolerance = payload.tolerance

    request = UserRequest(
        message=payload.message,
        case_ref=payload.case_ref,
        solver_config=sc if (sc.max_iterations or sc.tolerance) else None,
    )
    if payload.result_scopes:
        request.result_scopes = _to_scopes(payload.result_scopes)
    if payload.output_formats:
        request.output_formats = _to_formats(payload.output_formats)

    try:
        response = service.run(request, cli_callback=None)
    except TaskBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    status = TaskStatus(response["status"])
    task_id = response["task_id"]
    report_url = f"/api/tasks/{task_id}/report"

    return RunResponse(
        task_id=task_id,
        status=status.value,
        output_dir=response.get("output_dir"),
        report_url=report_url,
        error=response.get("error"),
    )


@router.get("/api/tasks/{task_id}", response_model=TaskInfo)
def get_task(task_id: str) -> TaskInfo:
    service = _get_service()
    info = service.inspect(task_id)
    if not info or not os.path.isdir(info.get("workspace") or ""):
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    state = info.get("state") if isinstance(info.get("state"), dict) else {}
    status = state.get("status")
    error = state.get("error")
    if not isinstance(status, str) or not status.strip() or status == "unknown":
        # Incremental state intentionally has status=None until a conclusion.
        # Only this process's current lock owner is still executing; after an
        # abrupt backend restart the same saved state is an interrupted task.
        from ..application.service import current_task_lock

        owner = current_task_lock()
        if owner.get("locked") and owner.get("task_id") == task_id:
            status = "running"
        else:
            status = TaskStatus.INTERRUPTED.value
            if not isinstance(error, dict):
                error = {"code": "INTERRUPTED", "message": "后端执行已中断，未形成最终结论。",
                         "stage": state.get("_persisted_stage") or state.get("phase") or "unknown"}
    artifacts: dict[str, str] = {}
    output_dir = ""
    ws = info.get("workspace", "")
    if ws and os.path.isdir(ws):
        if os.path.basename(os.path.dirname(ws)) == "outputs":
            output_dir = ws
        else:
            output_dir = os.path.join(os.path.dirname(ws).replace("tasks", "outputs"), task_id)
        found = _artifact_paths(task_id, ws)
        artifacts = {name: f"/api/tasks/{task_id}/files/{name}" for name in found}

    return TaskInfo(
        task_id=task_id,
        status=status,
        output_dir=output_dir,
        report_url=f"/api/tasks/{task_id}/report" if "report.md" in artifacts or "report.html" in artifacts else None,
        artifacts=artifacts,
        error=error,
    )


def _artifact_paths(task_id: str, ws: str) -> dict[str, str]:
    """收集任务产物可能存在的路径（workspace/reports 或 outputs/<task_id>）。"""
    candidates: dict[str, str] = {}
    # workspace 根目录：遍历所有 JSON/CSV 文件
    if os.path.isdir(ws):
        for name in os.listdir(ws):
            if name.startswith("."):
                continue
            p = os.path.join(ws, name)
            if os.path.isfile(p):
                candidates[name] = p
    # reports 子目录
    reports_dir = os.path.join(ws, "reports")
    if os.path.isdir(reports_dir):
        for name in os.listdir(reports_dir):
            if name.startswith("."):
                continue
            p = os.path.join(reports_dir, name)
            if os.path.isfile(p):
                candidates[name] = p
    # outputs 发布目录兜底：当 ws 是 workspace 时也查 outputs/<task_id>/
    outputs_dir = ws
    if os.path.basename(os.path.dirname(ws)) != "outputs":
        outputs_dir = os.path.join(os.path.dirname(ws).replace("tasks", "outputs"), task_id)
    if os.path.isdir(outputs_dir) and os.path.abspath(outputs_dir) != os.path.abspath(ws):
        for name in os.listdir(outputs_dir):
            if name.startswith("."):
                continue
            p = os.path.join(outputs_dir, name)
            if os.path.isfile(p):
                candidates.setdefault(name, p)
    return candidates


@router.get("/api/tasks/{task_id}/report", response_model=None)
def get_report(task_id: str) -> HTMLResponse | FileResponse:
    """返回 HTML 报告；若未生成 HTML 则返回 Markdown。"""
    service = _get_service()
    info = service.inspect(task_id)
    if not info or "workspace" not in info:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    artifacts = _artifact_paths(task_id, info["workspace"])
    if "report.html" in artifacts:
        return FileResponse(artifacts["report.html"], media_type="text/html")
    if "report.md" in artifacts:
        with open(artifacts["report.md"], encoding="utf-8") as f:
            return HTMLResponse(f"<pre>{f.read()}</pre>")
    raise HTTPException(status_code=404, detail=f"任务 {task_id} 暂无报告")


@router.get("/api/tasks/{task_id}/trace", response_model=None)
def get_task_trace(task_id: str) -> JSONResponse:
    """某任务的**真实执行轨迹**（只读，不触发任何计算）。

    数据源是任务产物里的 `trace.json` —— 它是运行时**逐条落盘**的，不是事后编的。
    界面上的「执行监控」步骤列表与「执行轨迹」面板都以它为准。

    之所以要有这个接口：此前「执行监控」那十步是前端**硬编码**的、进度按
    `floor(百分比 / 10)` **估算**出来，界面上却写着"真实任务轨迹" —— 名不副实。
    实时过程另有 SSE（`/api/tasks/stream`）在推；本接口负责**任务结束后的回看**。

    返回 `events`（原始事件，含 stage / event_type / tool_name / message / status /
    **action / narration**）与 `stages`（按**首次出现顺序**去重后的阶段序列）。
    `action` 是界面上的**动作行**（动词 + 对象 + 种类），`narration` 是紧跟其后的
    **思考段** —— 两者都由 `runtime/narration.py` 的模板 + 事件里的真实数值生成。
    标题与思考段取该阶段**最后一条**事件：阶段开头往往是"正在…"，收尾那条
    才说清"这一步做成了什么"。
    """
    service = _get_service()
    info = service.inspect(task_id)
    if not info or "workspace" not in info:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    artifacts = _artifact_paths(task_id, info["workspace"])
    path = artifacts.get("trace.json")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 没有执行轨迹")

    try:
        with open(path, encoding="utf-8") as f:
            events = json.load(f)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取轨迹失败：{exc}") from exc
    if not isinstance(events, list):
        events = []

    stages: list[dict] = []
    index: dict[str, dict] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        st = str(e.get("stage") or "").strip()
        if not st:
            continue
        # 事件 → 动作行（动词/对象/种类）+「思考段」（模板 + 该事件里的真实数值）。
        # 之所以在后端算好随事件下发，而不是让前端各自拼：
        # SSE 实时推的那份与这里回看的那份**必然同一段话**，不会出现"跑的时候
        # 一个样、刷新后另一个样"。模板只有一处（runtime/narration.py）。
        action = _narration.action_for(st)
        note = _narration.narrate_from_event(e)
        e["action"] = action
        e["narration"] = note
        row = index.get(st)
        if row is None:
            row = {
                "stage": st,
                "title": str(e.get("message") or st)[:90],
                "action": action,
                "narration": note,
                "first_ts": e.get("timestamp"),
                "last_ts": e.get("timestamp"),
                "event_count": 0,
                "ok": True,
            }
            index[st] = row
            stages.append(row)
        row["last_ts"] = e.get("timestamp")
        row["event_count"] = int(row["event_count"]) + 1
        # 标题与思考段都取该阶段**最后一条**事件：阶段开头往往是"正在…"，
        # 收尾那条才说清"这一步做成了什么"。与前端实时聚合的规则保持一致。
        row["title"] = str(e.get("message") or st)[:90]
        row["action"] = action
        row["narration"] = note
        if e.get("status") == "failed":
            row["ok"] = False

    return JSONResponse({
        "task_id": task_id,
        "count": len(events),
        "narration_version": _narration.NARRATION_VERSION,
        "stages": stages,
        "events": events,
    })


@router.get("/api/tasks/{task_id}/files/{filename}", response_model=None)
def get_file(task_id: str, filename: str) -> FileResponse:
    """下载任务产物文件。"""
    service = _get_service()
    info = service.inspect(task_id)
    if not info or "workspace" not in info:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    safe_name = os.path.basename(filename)
    artifacts = _artifact_paths(task_id, info["workspace"])
    target = artifacts.get(safe_name)
    if not target or not os.path.isfile(target):
        raise HTTPException(status_code=404, detail=f"文件 {safe_name} 不存在")

    media_type = "application/octet-stream"
    if safe_name.endswith(".json"):
        media_type = "application/json"
    elif safe_name.endswith(".md"):
        media_type = "text/markdown"
    elif safe_name.endswith(".html"):
        media_type = "text/html"
    elif safe_name.endswith(".csv"):
        media_type = "text/csv"
    return FileResponse(target, media_type=media_type)
