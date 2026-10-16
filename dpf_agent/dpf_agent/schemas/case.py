"""CanonicalDistributionCase 与依赖清单（文档 11.2 / 5.1）。

不复制 OpenDSS 原生 Circuit 对象，只保存任务可追溯所需的身份、依赖、摘要与引用。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class DependencyEntry(BaseModel):
    path: str           # 任务副本内相对路径
    sha256: str
    size_bytes: int
    role: str = "dependency"  # master | redirect | compile | general


class DependencyManifest(BaseModel):
    master_dss: str
    entries: list[DependencyEntry] = Field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.entries)


class CanonicalDistributionCase(BaseModel):
    """算例规范化身份契约（文档 11.2）。"""

    case_id: str
    case_fingerprint: str  # 全部依赖文件 sha256 聚合指纹
    master_dss_ref: str    # 任务副本内 Master.dss 相对路径
    dependency_manifest: DependencyManifest
    source_type: str = "user_file"  # builtin | user_file | simulation
    object_summary: dict[str, int] = Field(default_factory=dict)   # bus/line/load/transformer...
    base_voltage_summary: dict[str, float] = Field(default_factory=dict)
    canonical_workspace_ref: str = ""  # 任务隔离工作目录
    validation_ref: str = ""           # validation.json 引用
    source_format: str = ""            # xlsx / csv / json / matpower / dss
    source_metadata: dict[str, object] = Field(default_factory=dict)
    fidelity_status: str = "not_assessed"  # native / converted / warning / not_assessed
    fidelity_warnings: list[str] = Field(default_factory=list)
