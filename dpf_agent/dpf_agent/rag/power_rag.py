"""Power-RAG 外部知识库服务适配层（localhost:5000）。

为什么单独一层
--------------
1. **隔离第三方客户端**：``power_rag_client.py`` 是对接包原样引入的，
   项目内所有调用都走本模块的 ``PowerRagProvider``，便于替换/升级客户端。
2. **超时与降级**：三阶段检索（改写 → 混合检索 → 精排）慢且依赖网络。
   聊天注入必须设短超时，超时一律降级到本地 BM25，不能拖住回答。
3. **归一化**：把 Power-RAG 的片段结构统一成本项目 Hit 结构
   ``{rank, score, source, snippet, text, backend}``，
   使 chat 渲染层无需区分检索后端。
"""
from __future__ import annotations

import threading
from typing import Any

BACKEND_NAME = "powerrag"


class PowerRagUnavailable(Exception):
    """Power-RAG 不可用（服务离线 / 超时 / 业务错误）。"""


class PowerRagProvider:
    def __init__(self, config: Any | None = None):
        if config is None:
            from ..config import AgentConfig
            config = AgentConfig()
        self.config = config
        self._client = None
        self._lock = threading.RLock()

    # ---------------- 客户端 ----------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "power_rag_enabled", False))

    def _get_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from .power_rag_client import PowerRAGClient
                    self._client = PowerRAGClient(
                        base_url=self.config.power_rag_base_url,
                        timeout=int(self.config.power_rag_timeout_s),
                    )
        return self._client

    # ---------------- 健康检查 ----------------
    def health(self, timeout: float | None = None) -> dict[str, Any]:
        """返回 {"ok": bool, "detail": str, "raw": {...}}。不抛异常。"""
        if not self.enabled:
            return {"ok": False, "detail": "Power-RAG 未启用（DPF_POWER_RAG_ENABLED=0）", "raw": {}}
        try:
            from .power_rag_client import PowerRAGClient, PowerRAGError
            client = PowerRAGClient(
                base_url=self.config.power_rag_base_url,
                timeout=int(timeout or 8),
            )
            raw = client.kb_status()
        except Exception as exc:  # noqa: BLE001 - 健康检查绝不抛出
            return {"ok": False, "detail": f"Power-RAG 不可达：{exc}", "raw": {}}
        running = raw.get("qdrantStatus") == "running"
        points = raw.get("pointsCount", 0)
        detail = (
            f"Power-RAG 在线：{points} 个向量片段 / 集合 {raw.get('collectionName', '?')}"
            f" / 状态 {raw.get('status', '?')}"
            if running else f"Power-RAG 向量库未运行（{raw.get('qdrantStatus')}）"
        )
        return {"ok": running, "detail": detail, "raw": raw}

    # ---------------- 检索 ----------------
    def search(
        self,
        query: str,
        top_k: int | None = None,
        *,
        timeout: float | None = None,
        use_rewrite: bool | None = None,
        use_reranker: bool | None = None,
    ) -> list[dict[str, Any]]:
        """检索并返回归一化 hits。失败/超时抛 PowerRagUnavailable，由上层决定降级。"""
        if not self.enabled:
            raise PowerRagUnavailable("Power-RAG 未启用")
        q = (query or "").strip()
        if not q:
            return []
        k = int(top_k or self.config.power_rag_top_k)
        tmo = int(timeout or self.config.power_rag_timeout_s)
        rewrite = self.config.power_rag_use_rewrite if use_rewrite is None else use_rewrite
        rerank = self.config.power_rag_use_reranker if use_reranker is None else use_reranker

        try:
            from .power_rag_client import PowerRAGClient, PowerRAGError
            client = PowerRAGClient(base_url=self.config.power_rag_base_url, timeout=tmo)
            raw = client.search(
                q,
                top_k=k,
                use_reranker=rerank,
                use_query_rewrite=rewrite,
            )
        except PowerRAGError as exc:
            raise PowerRagUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise PowerRagUnavailable(f"Power-RAG 调用异常：{exc}") from exc

        results = (raw or {}).get("results") or []
        hits: list[dict[str, Any]] = []
        for i, item in enumerate(results, 1):
            text = (item.get("text") or "").strip()
            if not text:
                continue
            hits.append({
                "rank": item.get("rank", i),
                "score": float(item.get("score", 0.0) or 0.0),
                "source": item.get("source_file") or item.get("relative_path") or "Power-RAG",
                "snippet": text,
                "text": text,
                "backend": BACKEND_NAME,
                "chunk_index": item.get("chunk_index"),
            })
        return hits
