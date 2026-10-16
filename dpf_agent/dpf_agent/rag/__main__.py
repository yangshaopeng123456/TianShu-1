"""CLI: python -m dpf_agent.rag {ingest|query|status}"""
from __future__ import annotations

import argparse
import json
import sys

from .config import RAGConfig
from .pipeline import get_pipeline, reset_pipeline


def _cmd_ingest(args: argparse.Namespace) -> int:
    pipe = get_pipeline()
    result = pipe.ingest(force=bool(args.force))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    pipe = get_pipeline()
    if not pipe.config.enabled and not args.ignore_disabled:
        print(json.dumps({"error": "RAG 已禁用（DPF_RAG_ENABLED=0）"}, ensure_ascii=False))
        return 2
    result = pipe.search(args.query, top_k=args.top_k)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    pipe = get_pipeline()
    print(json.dumps(pipe.status(), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dpf_agent.rag", description="天枢一号本地 RAG 检索 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="构建/重建 RAG 索引")
    p_ing.add_argument("--force", action="store_true", help="强制重建（忽略已有索引与指纹）")
    p_ing.set_defaults(func=_cmd_ingest)

    p_q = sub.add_parser("query", help="对知识库做一次检索")
    p_q.add_argument("query", help="检索问题")
    p_q.add_argument("--top-k", type=int, default=None, help="返回条数（默认取自配置）")
    p_q.add_argument("--ignore-disabled", action="store_true", help="即使 RAG 关闭也执行")
    p_q.set_defaults(func=_cmd_query)

    p_s = sub.add_parser("status", help="查看知识库与索引状态")
    p_s.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
