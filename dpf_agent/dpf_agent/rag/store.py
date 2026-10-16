"""索引存储 + 加载。

索引文件（``outputs/rag_index/``）：
- ``chunks.json``  所有 chunk 明文（调试 + 检索回填用）
- ``index.json``   BM25 倒排索引：{n, avgdl, dl, df, postings}
- ``meta.json``    构建时间、源文件指纹、配置快照

设计为可重建：丢任何一份都不影响下次 ``ingest()`` 重建。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .chunker import Chunk


INDEX_VERSION = 1
META_FILE = "meta.json"
INDEX_FILE = "index.json"
CHUNKS_FILE = "chunks.json"


@dataclass
class BuiltIndex:
    n: int
    avgdl: float
    dl: list[int]               # 每个 chunk 的 token 长度
    df: dict[str, int]          # term -> 文档频
    postings: dict[str, list]   # term -> [[doc_id, tf], ...]
    chunks: list[Chunk]


def save_index(index_dir: str, idx: BuiltIndex, *, source_fps: dict[str, float],
               config_snapshot: dict[str, Any]) -> None:
    os.makedirs(index_dir, exist_ok=True)
    with open(os.path.join(index_dir, CHUNKS_FILE), "w", encoding="utf-8") as f:
        json.dump(
            [{"id": c.chunk_id, "source": c.source, "text": c.text,
              "start": c.start, "meta": c.meta} for c in idx.chunks],
            f, ensure_ascii=False,
        )
    with open(os.path.join(index_dir, INDEX_FILE), "w", encoding="utf-8") as f:
        json.dump({
            "version": INDEX_VERSION,
            "n": idx.n,
            "avgdl": idx.avgdl,
            "dl": idx.dl,
            "df": idx.df,
            "postings": idx.postings,
        }, f, ensure_ascii=False)
    with open(os.path.join(index_dir, META_FILE), "w", encoding="utf-8") as f:
        json.dump({
            "version": INDEX_VERSION,
            "source_fps": source_fps,
            "config": config_snapshot,
            "num_chunks": len(idx.chunks),
            "num_sources": len(source_fps),
        }, f, ensure_ascii=False, indent=2)


def load_index(index_dir: str) -> BuiltIndex | None:
    idx_path = os.path.join(index_dir, INDEX_FILE)
    chunks_path = os.path.join(index_dir, CHUNKS_FILE)
    if not (os.path.isfile(idx_path) and os.path.isfile(chunks_path)):
        return None
    try:
        with open(chunks_path, "r", encoding="utf-8") as f:
            raw_chunks = json.load(f)
        with open(idx_path, "r", encoding="utf-8") as f:
            raw_idx = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_idx, dict) or raw_idx.get("version") != INDEX_VERSION:
        return None
    chunks = [
        Chunk(
            chunk_id=int(c["id"]),
            source=str(c.get("source", "")),
            text=str(c.get("text", "")),
            start=int(c.get("start", 0)),
            meta=dict(c.get("meta") or {}),
        )
        for c in raw_chunks
    ]
    return BuiltIndex(
        n=int(raw_idx["n"]),
        avgdl=float(raw_idx["avgdl"]),
        dl=list(raw_idx.get("dl") or []),
        df=dict(raw_idx.get("df") or {}),
        postings=dict(raw_idx.get("postings") or {}),
        chunks=chunks,
    )


def load_meta(index_dir: str) -> dict[str, Any] | None:
    p = os.path.join(index_dir, META_FILE)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
