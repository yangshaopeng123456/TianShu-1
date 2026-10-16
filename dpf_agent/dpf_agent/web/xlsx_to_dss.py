"""将标准格式 Excel 配电网算例转换为 OpenDSS Master.dss（兼容入口）。"""
from __future__ import annotations

from .case_converters import convert_xlsx


def convert(xlsx_path: str, output_dir: str) -> str:
    """把 xlsx 转成 OpenDSS 算例目录，返回 Master.dss 路径。"""
    return convert_xlsx(xlsx_path, output_dir)
