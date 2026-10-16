"""RAG 流水线：ingest / query / status 统一入口。"""
from __future__ import annotations

import os
import threading
import time
from collections import Counter
from typing import Any

from .chunker import chunk_text
from .config import RAGConfig
from .loader import load_documents
from .power_rag import PowerRagProvider, PowerRagUnavailable
from .retriever import BM25Retriever, Hit, Retriever
from .store import BuiltIndex, INDEX_VERSION, load_index, load_meta, save_index
from .tokenize import tokenize

# 聊天注入场景最多嵌入的参考条数（Power-RAG 与本地 BM25 共用）
CHAT_MAX_HITS = 3

# 文档身份 → 可信度权重。检索分数会乘上这个系数，让"草稿/未审校"自动靠后，
# 降低用户把过程产物当成权威结论的风险（只是降权，不是删除，仍可追溯）。
STATUS_WEIGHT = {"final": 1.0, "unknown": 0.8, "draft": 0.5}
AUTHORITY_WEIGHT = {"high": 1.0, "unknown": 0.9, "low": 0.7}


def _trust_weight(meta: dict | None) -> float:
    """根据文档身份元数据计算可信度权重（0~1）。"""
    meta = meta or {}
    st = str(meta.get("status", "unknown")).lower()
    au = str(meta.get("authority", "unknown")).lower()
    return STATUS_WEIGHT.get(st, 0.8) * AUTHORITY_WEIGHT.get(au, 0.9)


class RagPipeline:
    def __init__(self, config: RAGConfig | None = None):
        self.config = config or RAGConfig()
        self._lock = threading.RLock()
        self._retriever: Retriever | None = None
        self._index: BuiltIndex | None = None
        self._meta: dict[str, Any] | None = None
        self._source_fps: dict[str, float] = {}
        # 外部 Power-RAG 服务（配置取自 AgentConfig）
        self._power: PowerRagProvider | None = None

    @property
    def power(self) -> PowerRagProvider:
        if self._power is None:
            self._power = PowerRagProvider()
        return self._power

    def _meta_for(self, chunk_id: int) -> dict[str, Any]:
        """按 chunk_id 取文档身份元数据（status/scope/version/authority）。"""
        idx = self._index
        if idx is None:
            return {}
        for c in idx.chunks:
            if getattr(c, "chunk_id", None) == chunk_id:
                return dict(getattr(c, "meta", {}) or {})
        return {}

    # ---------------- index lifecycle ----------------
    def _build_index(self) -> BuiltIndex:
        knowledge_dir, _ = self.config.resolved_paths()
        docs = load_documents(knowledge_dir, max_file_bytes=self.config.max_file_bytes)
        # 来源指纹：相对路径 -> mtime
        self._source_fps = {d.path: float(os.path.getmtime(d.abs_path)) for d in docs}
        chunks: list = []
        next_id = 0
        for d in docs:
            # 把文档身份元数据（status/version/scope/authority）透传到每个 chunk，
            # 供后续加权与"是否可信"标注使用。
            doc_meta = {k: v for k, v in d.meta.items() if k in
                        ("ext", "size", "status", "version", "scope", "authority", "title")}
            pieces = chunk_text(
                d.content, d.path,
                chunk_size=self.config.chunk_size,
                overlap=self.config.chunk_overlap,
                base_id=next_id,
                meta=doc_meta,
            )
            chunks.extend(pieces)
            next_id += len(pieces)
        # BM25 索引
        df: Counter = Counter()
        dl: list[int] = []
        postings: dict[str, list] = {}
        for chunk in chunks:
            tokens = tokenize(chunk.text, drop_stop=True)
            dl.append(len(tokens))
            counts = Counter(tokens)
            for term, tf in counts.items():
                df[term] += 1
                postings.setdefault(term, []).append([chunk.chunk_id, int(tf)])
        n = len(chunks)
        avgdl = (sum(dl) / n) if n else 0.0
        return BuiltIndex(n=n, avgdl=avgdl, dl=dl, df=dict(df),
                          postings=postings, chunks=chunks)

    def _needs_rebuild(self) -> bool:
        if self._index is None or not self._source_fps:
            return True
        meta = load_meta(self.config.index_dir)
        if not meta or meta.get("version") != INDEX_VERSION:
            return True
        prev = dict(meta.get("source_fps") or {})
        if set(prev.keys()) != set(self._source_fps.keys()):
            return True
        for k, v in self._source_fps.items():
            if abs(prev.get(k, -1.0) - v) > 1e-3:
                return True
        return False

    def ensure_ready(self) -> None:
        """确保索引已就绪：优先加载持久化索引，必要时重建。"""
        with self._lock:
            if self._retriever is not None and not self._needs_rebuild():
                return
            knowledge_dir, index_dir = self.config.resolved_paths()
            loaded = None
            if os.path.isdir(index_dir):
                loaded = load_index(index_dir)
            self._meta = load_meta(index_dir) if os.path.isdir(index_dir) else None
            if loaded is not None and not self._needs_rebuild_fresh():
                self._index = loaded
                self._retriever = BM25Retriever(loaded)
                return
            # 重建
            os.makedirs(index_dir, exist_ok=True)
            idx = self._build_index()
            cfg_snap = {
                "knowledge_dir": knowledge_dir,
                "chunk_size": self.config.chunk_size,
                "chunk_overlap": self.config.chunk_overlap,
                "version": INDEX_VERSION,
            }
            save_index(index_dir, idx, source_fps=self._source_fps, config_snapshot=cfg_snap)
            self._index = idx
            self._retriever = BM25Retriever(idx)
            self._meta = {"version": INDEX_VERSION, "source_fps": self._source_fps,
                          "config": cfg_snap, "num_chunks": len(idx.chunks),
                          "num_sources": len(self._source_fps)}

    def _needs_rebuild_fresh(self) -> bool:
        """比 _needs_rebuild 更激进：用于决定是否复用已加载的持久化索引。"""
        return self._needs_rebuild()

    def ingest(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if force:
                self._retriever = None
                self._index = None
                self._meta = None
            idx = self._build_index()
            index_dir = self.config.index_dir
            os.makedirs(index_dir, exist_ok=True)
            knowledge_dir, _ = self.config.resolved_paths()
            cfg_snap = {
                "knowledge_dir": knowledge_dir,
                "chunk_size": self.config.chunk_size,
                "chunk_overlap": self.config.chunk_overlap,
                "version": INDEX_VERSION,
            }
            save_index(index_dir, idx, source_fps=self._source_fps, config_snapshot=cfg_snap)
            self._index = idx
            self._retriever = BM25Retriever(idx)
            self._meta = {"version": INDEX_VERSION, "source_fps": self._source_fps,
                          "config": cfg_snap, "num_chunks": len(idx.chunks),
                          "num_sources": len(self._source_fps)}
            return {
                "num_chunks": len(idx.chunks),
                "num_sources": len(self._source_fps),
                "knowledge_dir": knowledge_dir,
                "index_dir": os.path.abspath(index_dir),
                "built_at": time.time(),
            }

    # ---------------- query ----------------
    def query(self, q: str, top_k: int | None = None) -> list[Hit]:
        if not self.config.enabled:
            return []
        self.ensure_ready()
        k = top_k if (top_k and top_k > 0) else self.config.top_k
        with self._lock:
            if self._retriever is None:
                return []
            hits = self._retriever.query(q, top_k=k)
        # 过滤低于阈值的（阈值 = 0 时全部返回；阈值 > 0 时按 BM25 原始分过滤）
        if self.config.min_score > 0:
            hits = [h for h in hits if h.score >= self.config.min_score]
        return hits

    def search(self, q: str, top_k: int | None = None, mode: str = "api") -> dict[str, Any]:
        """统一检索入口：Power-RAG 优先，本地 BM25 兜底。

        ``mode="chat"``：聊天注入场景。短超时、关闭 LLM 查询改写，
        失败即静默降级本地 BM25，绝不拖慢/阻塞回答。
        ``mode="api"``（默认）：专用检索接口，允许长超时与完整三阶段流水线。
        """
        knowledge_dir, index_dir = self.config.resolved_paths()
        is_chat = mode == "chat"
        hits: list[dict[str, Any]] = []
        backend = None
        degraded = None

        # ---- 1) Power-RAG（主检索）----
        if self.power.enabled:
            try:
                hits = self.power.search(
                    q,
                    top_k=top_k or (CHAT_MAX_HITS if is_chat else self.power.config.power_rag_top_k),
                    timeout=(
                        self.power.config.power_rag_chat_timeout_s if is_chat
                        else self.power.config.power_rag_timeout_s
                    ),
                    use_rewrite=False if is_chat else None,
                )
                if hits:
                    # 相关度门槛：跨领域弱命中（如拿"变电站二次系统"库答配电网问题）
                    # 一律丢弃，让下面回落到本地库，避免把噪声贴进回答。
                    min_score = self.power.config.power_rag_min_score
                    hits = [h for h in hits if float(h.get("score", 0.0)) >= min_score]
                if hits:
                    backend = "powerrag"
                else:
                    degraded = degraded or f"Power-RAG 命中均低于相关度门槛 {self.power.config.power_rag_min_score}"
            except PowerRagUnavailable as exc:
                degraded = str(exc)

        # ---- 2) 本地 BM25 兜底 ----
        # 触发条件：Power-RAG 未启用 / 调用失败 / 零命中，且配置了回落。
        if not hits and self.config.enabled and self.power.config.power_rag_fallback_local:
            local_hits = self.query(q, top_k=top_k or (CHAT_MAX_HITS if is_chat else self.config.top_k))
            scored: list[dict[str, Any]] = []
            for i, h in enumerate(local_hits, 1):
                meta = self._meta_for(h.chunk_id)
                w = _trust_weight(meta)
                scored.append({
                    "rank": i,
                    "score": round(h.score, 4),
                    "adjusted_score": round(h.score * w, 4),
                    "trust_weight": round(w, 3),
                    "status": meta.get("status", "unknown"),
                    "scope": meta.get("scope", "general"),
                    "version": meta.get("version", ""),
                    "authority": meta.get("authority", "unknown"),
                    "source": h.source,
                    "snippet": h.snippet,
                    "text": h.text,
                    "chunk_id": h.chunk_id,
                    "backend": "local-bm25",
                })
            # 按"加权后分数"排序：草稿/低权威自动沉到后面
            scored.sort(key=lambda d: d["adjusted_score"], reverse=True)
            for i, d in enumerate(scored, 1):
                d["rank"] = i
            hits = scored
            if hits:
                backend = "local-bm25"

        # 统一补 backend 字段（Power-RAG 侧已带，这里兜底）
        for i, h in enumerate(hits, 1):
            h.setdefault("rank", i)
            h.setdefault("backend", backend or "unknown")

        return {
            "query": q,
            "mode": mode,
            "backend": backend,
            "degraded_from": degraded,
            "top_k": top_k or self.config.top_k,
            "min_score": self.config.min_score,
            "knowledge_dir": knowledge_dir,
            "index_dir": index_dir,
            "num_hits": len(hits),
            "hits": hits,
        }

    def status(self) -> dict[str, Any]:
        knowledge_dir, index_dir = self.config.resolved_paths()
        has_dir = os.path.isdir(knowledge_dir)
        has_index = os.path.isfile(os.path.join(index_dir, "index.json"))
        meta = self._meta or load_meta(index_dir)
        num_chunks = len(self._index.chunks) if self._index else (meta or {}).get("num_chunks", 0)
        num_sources = len(self._source_fps) or (meta or {}).get("num_sources", 0)
        # Power-RAG（外部服务）：健康检查走短超时，失败不阻塞
        try:
            power_health = self.power.health(timeout=6)
        except Exception as exc:  # noqa: BLE001
            power_health = {"ok": False, "detail": f"Power-RAG 健康检查异常：{exc}", "raw": {}}

        return {
            "enabled": self.config.enabled,
            "knowledge_dir": knowledge_dir,
            "knowledge_exists": has_dir,
            "index_dir": index_dir,
            "index_exists": has_index,
            "num_chunks": num_chunks,
            "num_sources": num_sources,
            "top_k": self.config.top_k,
            "min_score": self.config.min_score,
            "chunk_size": self.config.chunk_size,
            "chunk_overlap": self.config.chunk_overlap,
            "ready": bool(self._retriever is not None or (has_index and has_dir)),
            # 双路检索后端
            "power_rag": {
                "enabled": self.power.enabled,
                "base_url": self.power.config.power_rag_base_url,
                "ok": bool(power_health.get("ok")),
                "detail": power_health.get("detail", ""),
                "points_count": (power_health.get("raw") or {}).get("pointsCount"),
                "collection": (power_health.get("raw") or {}).get("collectionName"),
            },
        }


# --------- 进程级单例 ---------
_LOCK = threading.Lock()
_PIPELINE: RagPipeline | None = None


def get_pipeline() -> RagPipeline:
    global _PIPELINE
    if _PIPELINE is None:
        with _LOCK:
            if _PIPELINE is None:
                _PIPELINE = RagPipeline()
    return _PIPELINE


def reset_pipeline() -> None:
    """测试 / 配置变更时清空单例。"""
    global _PIPELINE
    with _LOCK:
        _PIPELINE = None
