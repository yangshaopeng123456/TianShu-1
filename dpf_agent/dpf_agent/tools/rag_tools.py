"""RAG 工具：把本地知识库检索包装为 BaseTool。

注意：这些工具**独立于**潮流 8 工具（``TOOL_REGISTRY``）注册——潮流工
具数量（``len == 8``）是 ``/api/ready`` 的语义契约，不应被 RAG 工具污染。
RAG 工具另存于 ``RAG_TOOL_REGISTRY``，仅在 chat 层与专用 API 中调用。
"""
from __future__ import annotations

from typing import Any, ClassVar

from ..rag.pipeline import get_pipeline
from .base import BaseTool


class RagSearchTool(BaseTool):
    """在本地知识库上做 BM25 检索，返回 top-k 命中片段。"""

    name: ClassVar[str] = "rag_search"
    description: ClassVar[str] = (
        "在天枢一号本地知识库上做检索增强（RAG）。"
        "输入 query，返回按相关性排序的 top_k 个片段（含 source/score/snippet）。"
        "适用于「项目里 / 规范里 / 之前测试里 / 文档里有没有……」「怎么解释……」"
        "等需要从知识库回填证据的查询。"
    )
    stage: ClassVar[str] = "rag"
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "description": "检索问题"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20,
                       "description": "返回条数（默认取自配置 DPF_RAG_TOP_K）"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "num_hits": {"type": "integer"},
            "hits": {"type": "array"},
        },
    }

    def run(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        q = kwargs["query"]
        top_k = kwargs.get("top_k")
        return get_pipeline().search(q, top_k=top_k)


class RagStatusTool(BaseTool):
    name: ClassVar[str] = "rag_status"
    description: ClassVar[str] = "查看 RAG 知识库与索引状态（路径、是否就绪、chunk 数等）。"
    stage: ClassVar[str] = "rag"
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object", "properties": {}, "required": [],
        "additionalProperties": False,
    }
    output_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def run(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return get_pipeline().status()


class RagIngestTool(BaseTool):
    name: ClassVar[str] = "rag_ingest"
    description: ClassVar[str] = "重建 RAG 索引（用于新增/修改知识库文档后）。"
    stage: ClassVar[str] = "rag"
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "force": {"type": "boolean", "description": "强制重建（默认 true）"},
        },
        "required": [],
        "additionalProperties": False,
    }
    output_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def run(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        force = bool(kwargs.get("force", True))
        return get_pipeline().ingest(force=force)


RAG_TOOL_REGISTRY: dict[str, BaseTool] = {
    "rag_search": RagSearchTool(),
    "rag_status": RagStatusTool(),
    "rag_ingest": RagIngestTool(),
}
