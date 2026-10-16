"""RAG 配置。

所有字段都有环境变量覆盖，命名空间 ``DPF_RAG_*``。默认即「开箱即用」：
- enabled=True
- knowledge_dir = <project>/knowledge
- index_dir    = <project>/outputs/rag_index
- top_k=4, min_score=0.6, chunk_size=500, chunk_overlap=80
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RAGConfig:
    enabled: bool = field(default_factory=lambda: _env_bool("DPF_RAG_ENABLED", True))
    knowledge_dir: str = field(
        default_factory=lambda: os.environ.get(
            "DPF_RAG_KNOWLEDGE_DIR",
            os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "knowledge")),
        )
    )
    index_dir: str = field(
        default_factory=lambda: os.environ.get(
            "DPF_RAG_INDEX_DIR",
            os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "outputs", "rag_index")),
        )
    )
    top_k: int = field(default_factory=lambda: _env_int("DPF_RAG_TOP_K", 4))
    min_score: float = field(default_factory=lambda: float(os.environ.get("DPF_RAG_MIN_SCORE", "0.6")))
    chunk_size: int = field(default_factory=lambda: _env_int("DPF_RAG_CHUNK_SIZE", 500))
    chunk_overlap: int = field(default_factory=lambda: _env_int("DPF_RAG_CHUNK_OVERLAP", 80))
    max_file_bytes: int = field(default_factory=lambda: _env_int("DPF_RAG_MAX_FILE_BYTES", 2 * 1024 * 1024))

    def resolved_paths(self) -> tuple[str, str]:
        """返回 (knowledge_dir, index_dir) 的绝对路径。"""
        return os.path.abspath(self.knowledge_dir), os.path.abspath(self.index_dir)
