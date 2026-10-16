"""知识库文档加载器。

支持：.md / .markdown / .txt / .csv / .json。
- 跳过隐藏文件、目录、过大文件（> ``max_file_bytes``）。
- 相对路径以 ``knowledge_dir`` 为根。
- JSON：自动提取 ``text`` / ``content`` / ``markdown`` 字段，或把整文档 stringify。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


_TEXT_EXTS = {".md", ".markdown", ".txt", ".csv"}
_JSON_EXTS = {".json"}

# 受支持的 front-matter 键（简单 key: value，不引入 yaml 依赖）
_FM_KEYS = ("status", "version", "scope", "authority", "title", "source")
# 默认身份：未标注的文档按"未审校 + 通用域"处理，宁可保守
_DEFAULT_META = {"status": "unknown", "version": "", "scope": "general", "authority": "unknown"}


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """解析文档开头的 YAML-ish front-matter。

    返回 (meta, 正文)。没有 front-matter 时返回 ({}, 原文)。
    只用标准库实现（不引入 pyyaml），支持形如 ``key: value`` 的简单行。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
        raw = lines[i].strip()
        if not raw or raw.startswith("#"):
            continue
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        k = k.strip().lower()
        v = v.strip().strip('"').strip("'")
        if k in _FM_KEYS and v:
            meta[k] = v
    if end is None:  # 没有闭合，视作无 front-matter
        return {}, text
    return meta, "\n".join(lines[end + 1:])


@dataclass
class Document:
    path: str       # 相对 knowledge_dir
    abs_path: str
    content: str
    meta: dict


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _read_json(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("text", "content", "markdown", "body", "answer"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v
        # 兜底：把所有标量值拼成可读文本
        lines = []
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool)):
                lines.append(f"{k}: {v}")
        return "\n".join(lines)
    if isinstance(data, list):
        lines = []
        for i, item in enumerate(data):
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or item.get("question")
                a = item.get("answer")
                if isinstance(t, str):
                    line = t
                    if isinstance(a, str):
                        line += "\n" + a
                    lines.append(line)
                else:
                    lines.append(json.dumps(item, ensure_ascii=False))
            elif isinstance(item, str):
                lines.append(item)
        return "\n\n".join(lines)
    return str(data)


def load_documents(knowledge_dir: str, *, max_file_bytes: int = 2 * 1024 * 1024) -> list[Document]:
    if not os.path.isdir(knowledge_dir):
        return []
    docs: list[Document] = []
    for root, dirs, files in os.walk(knowledge_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        for name in sorted(files):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in _TEXT_EXTS and ext not in _JSON_EXTS:
                continue
            abs_path = os.path.join(root, name)
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                continue
            if size > max_file_bytes or size == 0:
                continue
            try:
                if ext in _JSON_EXTS:
                    content = _read_json(abs_path)
                    fm: dict = {}
                else:
                    raw = _read_text(abs_path)
                    fm, content = _parse_front_matter(raw)
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            content = (content or "").strip()
            if not content:
                continue
            rel = os.path.relpath(abs_path, knowledge_dir).replace(os.sep, "/")
            # 身份元数据：未声明的字段用保守默认值
            meta: dict = {"ext": ext, "size": size, **_DEFAULT_META}
            meta.update(fm)
            # 正文里若没有标题，用文件名兜底
            meta.setdefault("title", rel)
            docs.append(Document(
                path=rel,
                abs_path=abs_path,
                content=content,
                meta=meta,
            ))
    return docs
