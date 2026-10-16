"""零依赖分词器：CJK 字符级 + bigram + ASCII 单词。

为什么不用 jieba：项目 venv 不一定装了 jieba；CJK 字符 bigram 在短文档
稀疏语料上召回表现与 jieba 接近（不少基准甚至更好），且零安装成本。
后续如需更高语义召回，可替换 ``Retriever`` 为向量检索而不动本分词器。
"""
from __future__ import annotations

import re
from typing import Iterable

# 常见停用词（极简，避免噪声过强；RAG 检索本身容忍少量停用词）
_STOPWORDS = frozenset(
    """
    the a an of to in on for and or is are be do does did not no
    的 了 和 与 及 或 也 都 就 还 但 而 在 是 有 没 不 这 那 一 个 上 下
    我 你 他 她 它 我们 你们 他们 吗 呢 啊 吧 哦 嗯
    what how why when where which who whom
    """.split()
)

# Unicode CJK 统一表意文字（基本平面 + 扩展 A）
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def tokenize(text: str, drop_stop: bool = True) -> list[str]:
    """把文本切成 token 列表。

    - CJK 字符：每个单字 + 与前一个 CJK 字符组成的 bigram（首字只有单字）
    - ASCII/数字：连续字母数字作为一个 token，转小写
    - 标点、空白：丢弃
    - 停用词：默认丢弃（停用词对 BM25 通常是噪声）
    """
    if not text:
        return []
    tokens: list[str] = []
    prev_cjk: str | None = None
    for ch in text:
        if _is_cjk(ch):
            tokens.append(ch)
            if prev_cjk is not None:
                tokens.append(prev_cjk + ch)
            prev_cjk = ch
        else:
            prev_cjk = None
    # ASCII 词单独再切一遍
    for w in _WORD_RE.findall(text):
        wl = w.lower()
        if len(wl) >= 2 and not wl.isdigit():
            tokens.append(wl)
    if drop_stop:
        tokens = [t for t in tokens if t not in _STOPWORDS and len(t) > 0]
    return tokens


def tokenize_query(text: str) -> list[str]:
    """查询侧分词：不丢弃停用词（短查询里停用词有时是必要信号），但裁长度。"""
    return tokenize(text, drop_stop=False)[:64]
