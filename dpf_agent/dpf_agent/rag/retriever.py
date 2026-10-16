"""检索器：抽象基类 + BM25 实现。

BM25 (Okapi) 公式（k1=1.5, b=0.75）：
    idf(t) = ln((N - df + 0.5) / (df + 0.5) + 1)
    score(d, q) = sum_{t in q} idf(t) * tf(t,d) * (k1+1)
                  / (tf(t,d) + k1 * (1 - b + b * |d| / avgdl))

倒排索引结构：``postings[term] = [[doc_id, tf], ...]``。
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .store import BuiltIndex
from .tokenize import tokenize_query


@dataclass
class Hit:
    chunk_id: int
    score: float
    source: str
    text: str
    snippet: str


class Retriever(ABC):
    @abstractmethod
    def query(self, q: str, top_k: int = 4) -> list[Hit]: ...


class BM25Retriever(Retriever):
    """自实现 BM25 检索器。构造时从 ``BuiltIndex`` 预计算每词 IDF。"""

    K1 = 1.5
    B = 0.75

    def __init__(self, idx: BuiltIndex):
        self.idx = idx
        self._idf: dict[str, float] = {}
        for term, df in idx.df.items():
            # Robertson-Sparck Jones IDF（带 +1 平滑，避免负值）
            self._idf[term] = math.log((idx.n - df + 0.5) / (df + 0.5) + 1.0)

    def query(self, q: str, top_k: int = 4) -> list[Hit]:
        if not q or not q.strip() or self.idx.n == 0:
            return []
        tokens = tokenize_query(q)
        if not tokens:
            return []
        scores: dict[int, float] = {}
        for t in tokens:
            idf = self._idf.get(t)
            if idf is None:
                continue
            postings = self.idx.postings.get(t)
            if not postings:
                continue
            for doc_id, tf in postings:
                if doc_id >= len(self.idx.dl):
                    continue
                dl = self.idx.dl[doc_id]
                denom = tf + self.K1 * (1.0 - self.B + self.B * dl / max(self.idx.avgdl, 1.0))
                inc = idf * (tf * (self.K1 + 1.0)) / max(denom, 1e-9)
                scores[doc_id] = scores.get(doc_id, 0.0) + inc
        if not scores:
            return []
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:max(1, int(top_k))]
        hits: list[Hit] = []
        for doc_id, score in top:
            chunk = self.idx.chunks[doc_id]
            text = chunk.text
            snippet = text if len(text) <= 220 else text[:220].rstrip() + "…"
            hits.append(Hit(
                chunk_id=chunk.chunk_id,
                score=round(float(score), 4),
                source=chunk.source,
                text=text,
                snippet=snippet,
            ))
        return hits
