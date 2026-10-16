"""归档目录的**单点定义**。

仓库存在两种布局，本模块同时适配：

    交付包（tianshu-1\\）—— 现行布局
        examples\\cases\\         算例根（只收公开标准算例，见 examples\\cases\\ieee_official\\）
        docs\\03-验证报告\\       报告类 .md（eval / 基线 / 看板脚本的默认输出位置）

    开发树（agent\\）—— 开发机布局，目录名不同，由下面的候选表适配
        <算例根>                对应 examples\\cases\\（开发期为自建套件，已移出交付包）
        <文档源>                对应 docs\\03-验证报告\\

设计取舍：**目录被移走时不报错、只回退**——这些都是辅助数据，缺失时脚本仍应能跑
（评测会自然报"无匹配参考"），而不该在 import 期就崩。
"""
from __future__ import annotations

import os

#: 包所在目录：`<repo>\\dpf_agent`
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
#: 仓库根：`<...>\\agent` 或 `<...>\\tianshu-1`
REPO_ROOT = os.path.abspath(os.path.join(PROJECT_ROOT, os.pardir))

#: 难度算例套件的候选归档目录（按顺序取第一个存在的）
CASE_ARCHIVE_CANDIDATES = (
    os.path.join(REPO_ROOT, "算例归档"),
    os.path.join(REPO_ROOT, "examples", "cases"),
)

#: 报告类 .md 的候选目录（按顺序取第一个存在的）
DOC_SOURCE_CANDIDATES = (
    os.path.join(REPO_ROOT, "天枢一号_文档", "源文档"),
    os.path.join(REPO_ROOT, "docs", "03-验证报告"),
    os.path.join(REPO_ROOT, "docs"),
)

#: 兼容旧引用：指向各自的首选位置
CASE_ARCHIVE = CASE_ARCHIVE_CANDIDATES[0]
DOC_ARCHIVE = os.path.join(REPO_ROOT, "天枢一号_文档")
DOC_SOURCE = DOC_SOURCE_CANDIDATES[0]

#: 上述目录都不存在时的回退位置（公开版限定在仓库内，不扫描用户桌面）
_CASE_FALLBACK = os.path.join(REPO_ROOT, "examples", "cases")
_DOC_FALLBACK = os.path.join(REPO_ROOT, "docs")


def _first_existing(candidates: tuple[str, ...]) -> str | None:
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def case_archive_root() -> str:
    """算例套件的扫描根：按候选顺序取第一个存在的目录，都缺失时回退仓库内的候选位置。"""
    return _first_existing(CASE_ARCHIVE_CANDIDATES) or _CASE_FALLBACK


def doc_source_dir() -> str:
    """报告类 .md 的目录（eval 报告 / 不平衡基线 / 指标看板的默认输出位置）。"""
    return _first_existing(DOC_SOURCE_CANDIDATES) or _DOC_FALLBACK
