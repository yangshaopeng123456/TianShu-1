"""天枢一号本地 RAG 模块。

检索增强生成（RAG）= 检索 + 生成。本模块负责「检索」：把本地知识库
（默认 ``<project>/knowledge/``）切成 chunk、建索引、按问题召回相关片段，
供聊天层作为 grounding 注入到回答中。

设计要点
--------
- 零额外依赖：不引入 jieba / rank_bm25 / numpy / faiss。中文用字符 + 相邻
  bigram 切分（在 CJK 语料上效果接近 jieba，零成本），BM25 评分自实现。
- 索引可持久化到 JSON（``outputs/rag_index/``），冷启动后毫秒级加载。
- 检索器抽象为 ``Retriever`` 基类，后续可替换为向量检索（sentence-transformers
  / bge / b.ai embeddings）而不改动上层调用。
- 顶层 ``get_pipeline()`` 惰性单例：第一次调用时构建/加载，后续复用。
"""
from .config import RAGConfig
from .pipeline import RagPipeline, get_pipeline, reset_pipeline
from .retriever import Hit, Retriever, BM25Retriever

__all__ = [
    "RAGConfig",
    "RagPipeline",
    "get_pipeline",
    "reset_pipeline",
    "Hit",
    "Retriever",
    "BM25Retriever",
]
