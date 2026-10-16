"""文档切片（chunking）。

策略
----
- 先按空行分段（markdown/纯文本都适用），保留段落顺序。
- 相邻段落累积直到 ``chunk_size``；溢出时落盘当前 chunk，并保留最后
  ``chunk_overlap`` 字符作为下一个 chunk 的前缀，保证上下文不丢。
- 极短段落：单独成 chunk；过长段落：硬切 + overlap。
- 文本极小（< chunk_size）：整篇 1 个 chunk。

返回的 ``Chunk`` 携带来源（相对路径）与片段在原文中的字符偏移，便于回溯。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Chunk:
    chunk_id: int
    source: str       # 相对知识库根目录的路径
    text: str
    start: int        # 文本在原文中的字符偏移
    meta: dict


def split_paragraphs(text: str) -> list[str]:
    raw = [p.strip() for p in text.split("\n\n")]
    return [p for p in raw if p]


def chunk_text(text: str, source: str, *, chunk_size: int = 500, overlap: int = 80,
               base_id: int = 0, meta: dict | None = None) -> list[Chunk]:
    """把一整篇文本切成 Chunk 列表。``base_id`` 用于多文件场景下保证 id 唯一。"""
    if not text or not text.strip():
        return []
    chunk_size = max(64, int(chunk_size))
    overlap = max(0, min(int(overlap), chunk_size // 2))
    meta = dict(meta or {})

    if len(text) <= chunk_size:
        return [Chunk(
            chunk_id=base_id,
            source=source,
            text=text.strip(),
            start=0,
            meta=meta,
        )]

    chunks: list[Chunk] = []
    cursor = 0
    cid = base_id
    n = len(text)
    while cursor < n:
        end = min(cursor + chunk_size, n)
        # 尽量在段落/句子边界断开（向前找最多 60 字符）
        if end < n:
            for sep in ("\n\n", "\n", "。", "！", "？", ".", "!", "?"):
                idx = text.rfind(sep, cursor + chunk_size // 2, end)
                if idx > cursor:
                    end = idx + len(sep)
                    break
        piece = text[cursor:end].strip()
        if piece:
            chunks.append(Chunk(
                chunk_id=cid,
                source=source,
                text=piece,
                start=cursor,
                meta=meta,
            ))
            cid += 1
        if end >= n:
            break
        cursor = max(end - overlap, cursor + 1)
    return chunks
