# -*- coding: utf-8 -*-
"""
Power-RAG 本地知识库 API 客户端
================================
对接 Power-RAG（FastAPI，http://localhost:5000）的轻量封装。
依赖：requests  （pip install requests）

用法：
    from power_rag_client import PowerRAGClient
    rag = PowerRAGClient()
    res = rag.search("人工接地极散流特性", top_k=5)
"""

from __future__ import annotations

import json
from typing import Any, Dict, Generator, List, Optional

import requests

# ⚠️ 必须**绕开系统代理**。
# 本机若设了 http_proxy / https_proxy（开发机就设了 http://127.0.0.1:12123），
# `requests` 会把**连 localhost 的请求也丢给代理**；代理不可达时 connect 会一路
# 等到超时（实测 8~12 秒）。Power-RAG 就在本机，直连才是对的。
# 另：timeout 用「（连接, 读取）」二元组 —— 单值 timeout 让"连不上"和"响应慢"
# 共用同一个上限，而前者本该更快失败。
_SESSION = requests.Session()
_SESSION.trust_env = False                       # 忽略 *_proxy / no_proxy 环境变量
_NO_PROXY: Dict[str, None] = {"http": None, "https": None}
_CONNECT_TIMEOUT_S = 1.5   # 本机服务的连接上限；连得上就是毫秒级，连不上要快速失败

# 关掉 urllib3 的自动重试：Power-RAG 就在本机，"连不上"等于"服务没起"，
# 重试三次只是把 0.1 秒的失败拖成 4 秒多（实测）。探测路径尤其输不起这个时间。
_HTTP_ADAPTER = requests.adapters.HTTPAdapter(max_retries=0)
_SESSION.mount("http://", _HTTP_ADAPTER)
_SESSION.mount("https://", _HTTP_ADAPTER)


class PowerRAGError(Exception):
    """Power-RAG 接口调用失败（含服务端返回的业务错误）。"""


class PowerRAGClient:
    def __init__(self, base_url: str = "http://localhost:5000", timeout: int = 60):
        # `localhost` 在 Windows 上通常先解析到 IPv6（::1）。Power-RAG 只监听
        # IPv4 时，连 ::1 属于**被丢弃**（不是被拒绝）—— 于是要干等一个连接
        # 超时，实测每次探测白花 3 秒。规范成 127.0.0.1 直接走 IPv4，
        # 服务没起就是立刻失败（WinError 10061）。
        base_url = base_url.replace("://localhost", "://127.0.0.1")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ────────────────────────────────────────────────
    # 内部工具
    # ────────────────────────────────────────────────
    def _timeout(self, timeout: Optional[int]) -> tuple[float, float]:
        total = float(timeout or self.timeout)
        return (min(_CONNECT_TIMEOUT_S, total), total)

    def _post(self, path: str, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = _SESSION.post(url, json=payload, timeout=self._timeout(timeout),
                                 proxies=_NO_PROXY)
        except requests.RequestException as e:
            raise PowerRAGError(f"无法连接 Power-RAG 服务（{url}）：{e}") from e
        return self._handle(resp, url)

    def _get(self, path: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = _SESSION.get(url, timeout=self._timeout(timeout), proxies=_NO_PROXY)
        except requests.RequestException as e:
            raise PowerRAGError(f"无法连接 Power-RAG 服务（{url}）：{e}") from e
        return self._handle(resp, url)

    @staticmethod
    def _handle(resp: requests.Response, url: str) -> Dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            raise PowerRAGError(f"服务返回非 JSON（HTTP {resp.status_code}，{url}）")
        if resp.status_code >= 400:
            detail = data.get("detail") or data.get("error") or resp.text[:200]
            raise PowerRAGError(f"接口错误（HTTP {resp.status_code}）：{detail}")
        if isinstance(data, dict) and data.get("error"):
            raise PowerRAGError(f"服务端业务错误：{data['error']}")
        return data

    # ────────────────────────────────────────────────
    # 健康检查
    # ────────────────────────────────────────────────
    def kb_status(self) -> Dict[str, Any]:
        """返回知识库状态。关键字段：qdrantStatus / pointsCount / status / llmConfigured。"""
        return self._get("/api/kb/status", timeout=20)

    def is_ready(self, expected_points: Optional[int] = None) -> bool:
        """服务是否可用；可选校验向量数量（当前 GB/T 45906 知识库应为 498）。"""
        try:
            s = self.kb_status()
        except PowerRAGError:
            return False
        if s.get("qdrantStatus") != "running":
            return False
        if expected_points is not None and int(s.get("pointsCount", 0)) != expected_points:
            return False
        return True

    # ────────────────────────────────────────────────
    # 核心：检索
    # ────────────────────────────────────────────────
    def search(
        self,
        query: str,
        top_k: int = 20,
        use_reranker: bool = True,
        use_query_rewrite: bool = True,
    ) -> Dict[str, Any]:
        """
        三阶段混合检索。
        返回 {"results": [{rank, score, source_file, text, ...}], "searchMode": ..., "searchInfo": ...}
        离线环境请把 use_query_rewrite 设为 False（向量嵌入仍需联网）。
        """
        if not query or not query.strip():
            raise ValueError("query 不能为空")
        return self._post("/api/search", {
            "query": query.strip(),
            "topK": top_k,
            "useReranker": use_reranker,
            "useQueryRewrite": use_query_rewrite,
        })

    # ────────────────────────────────────────────────
    # 核心：综述 / 文章生成
    # ────────────────────────────────────────────────
    FORMATS = ("ieee", "csee", "paper", "book_chapter",
               "tech_report", "literature_review", "general")

    def generate_review(
        self,
        search_results: List[Dict[str, Any]],
        query: str = "",
        language: str = "zh",
        fmt: str = "tech_report",
        custom_title: str = "",
        custom_rules: str = "",
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """
        基于检索结果一次性生成文章（需联网调千问 API）。
        返回 {"content": 正文, "reasoning": ..., "usage": ..., "format": ..., "formatName": ...}
        """
        if not search_results:
            raise ValueError("search_results 至少需要 1 条检索结果")
        if fmt not in self.FORMATS:
            raise ValueError(f"format 必须是 {self.FORMATS} 之一")
        return self._post("/api/review/generate", {
            "query": query,
            "searchResults": search_results,
            "language": language,
            "format": fmt,
            "customTitle": custom_title,
            "customRules": custom_rules,
            "stream": False,
        }, timeout=timeout)

    def generate_review_stream(
        self,
        search_results: List[Dict[str, Any]],
        query: str = "",
        language: str = "zh",
        fmt: str = "tech_report",
        custom_title: str = "",
        custom_rules: str = "",
        timeout: int = 300,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        SSE 流式生成。逐事件 yield：
          {"type": "content",   "text": "..."}   正文增量
          {"type": "reasoning", "text": "..."}   推理增量
          {"type": "done", ...}                  完毕
        出错时抛出 PowerRAGError。
        """
        if not search_results:
            raise ValueError("search_results 至少需要 1 条检索结果")
        url = f"{self.base_url}/api/review/generate"
        payload = {
            "query": query,
            "searchResults": search_results,
            "language": language,
            "format": fmt,
            "customTitle": custom_title,
            "customRules": custom_rules,
            "stream": True,
        }
        try:
            with _SESSION.post(url, json=payload, timeout=self._timeout(timeout),
                               stream=True, proxies=_NO_PROXY) as resp:
                if resp.status_code >= 400:
                    raise PowerRAGError(f"接口错误（HTTP {resp.status_code}）：{resp.text[:200]}")
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw or not raw.startswith("data: "):
                        continue
                    try:
                        event = json.loads(raw[6:])
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "error":
                        raise PowerRAGError(f"流式生成出错：{event.get('message')}")
                    yield event
                    if event.get("type") == "done":
                        return
        except requests.RequestException as e:
            raise PowerRAGError(f"连接中断：{e}") from e

    # ────────────────────────────────────────────────
    # 辅助：浏览 / 图谱
    # ────────────────────────────────────────────────
    def browse(self, mode: str = "all", category: str = "",
               files: Optional[List[str]] = None, limit: int = 50) -> Dict[str, Any]:
        """按条件浏览文献片段。mode: all / category / files。"""
        return self._post("/api/browse", {
            "mode": mode, "category": category,
            "files": files or [], "limit": limit,
        })

    def graph_stats(self) -> Dict[str, Any]:
        """实体图谱统计（实体/关系总数、类型分布、Top20 高频实体）。"""
        return self._get("/api/graph/stats")

    def graph_search(self, query: str, top_k: int = 10) -> Dict[str, Any]:
        """搜索实体图谱，返回相关实体及其关系、来源文件。"""
        if not query or not query.strip():
            raise ValueError("query 不能为空")
        return self._post("/api/graph/search", {"query": query.strip(), "topK": top_k})

    def formats(self) -> Dict[str, Any]:
        """可用写作格式列表。"""
        return self._get("/api/formats")


if __name__ == "__main__":
    # 简单自检：python power_rag_client.py
    rag = PowerRAGClient()
    print("服务可用：" , rag.is_ready())
    status = rag.kb_status()
    print(f"向量片段数: {status.get('pointsCount')}  状态: {status.get('status')}")
