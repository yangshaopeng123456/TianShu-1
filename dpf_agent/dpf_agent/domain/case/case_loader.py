"""算例加载与规范化（文档 12.3 / 5.1）。

- 只读与副本执行：原 DSS 文件只读，分析在任务副本中执行。
- 路径约束：Redirect/Compile 引用不得越出任务目录或批准资源根（CASE_PATH_REJECTED）。
- 身份可追溯：生成 dependency_manifest 与 case_fingerprint。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil

from ...schemas.case import (
    CanonicalDistributionCase,
    DependencyEntry,
    DependencyManifest,
)
from ...schemas.enums import ErrorCode


class CaseLoadError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_REDIRECT_RE = re.compile(r"^\s*(?:redirect|compile)\s+(.+?)\s*$", re.IGNORECASE)
# `BusCoords <文件>` 也引用一个外部文件（母线坐标 CSV），但它**不是** Redirect，
# 早期只认 Redirect/Compile ⇒ 该 CSV 不会被复制进任务副本，OpenDSS 一 Compile 就报
# "Bus Coordinate file: \"...\" not found"。官方 IEEE 13 节点算例正是这种布局
# （IEEETestCases/13Bus/IEEE13Node_BusXY.csv），实测因此整例失败。
_BUSCOORDS_RE = re.compile(r"^\s*buscoords\s+(.+?)\s*$", re.IGNORECASE)
_DATA_FILE_RE = re.compile(
    r'''(?<![\w.])(?:file|csvfile|pqcsvfile|sngfile|dblfile)\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s,\)\]\}]+))''',
    re.IGNORECASE,
)
_NEW_ELEMENT_RE = re.compile(
    r"^\s*new\s+([A-Za-z][A-Za-z0-9_]*)\.([^\s!]+)", re.IGNORECASE
)


def _scan_duplicate_element_names(dss_path: str, seen: set[str]) -> list[str]:
    """扫描 New Class.Name 重复定义；仅按任务副本中的有效命令判断。"""
    duplicates: list[str] = []
    with open(dss_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.split("!")[0].strip()
            if not stripped:
                continue
            match = _NEW_ELEMENT_RE.match(stripped)
            if not match:
                continue
            key = f"{match.group(1).lower()}.{match.group(2).lower()}"
            if key in seen:
                duplicates.append(key)
            else:
                seen.add(key)
    return duplicates


def _has_dependency_cycle(graph: dict[str, set[str]]) -> bool:
    """判断 Redirect/Compile 依赖图是否存在有向环。"""
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for nxt in graph.get(node, set()):
            if visit(nxt):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_within(path: str, root: str) -> bool:
    """`path` 是否位于 `root` 之内。**跨盘返回 False，而不是抛异常。**

    `os.path.commonpath` 在两个路径属于**不同盘**时会抛
    `ValueError: Paths don't have the same drive`。那会把"算例配置在别的盘"
    变成一条**完全看不出原因**的失败 —— 实测表现为
    `ENGINE_ERROR — Paths don't have the same drive`，而被拒的真实理由是路径守卫。

    `DPF_CHAT_DB_DIR` / `DPF_OUTPUTS` / `DPF_WORKSPACE` 都是公开支持的配置项，
    把数据放到别的盘是合法用法，所以跨盘必须按"不在该根之内"处理。
    """
    try:
        a = os.path.abspath(path)
        b = os.path.abspath(root)
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def _strip_quotes(ref: str) -> str:
    ref = ref.strip()
    if (ref.startswith('"') and ref.endswith('"')) or (
        ref.startswith("'") and ref.endswith("'")
    ):
        return ref[1:-1]
    return ref


def _without_comments(text: str) -> str:
    """Keep quoted paths intact; remove DSS line and block comments."""
    out, quote, block, index = [], None, False, 0
    while index < len(text):
        char, pair = text[index], text[index:index + 2]
        if block:
            if pair == "*/":
                block, index = False, index + 2
                continue
            out.append("\n" if char == "\n" else " ")
        elif quote:
            out.append(char)
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            out.append(char)
        elif pair == "/*":
            block, index = True, index + 2
            continue
        elif char == "!" or pair == "//":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _file_references(dss_path: str) -> list[tuple[str, str]]:
    """(relative path, role); binary/CSV dependencies are never parsed as DSS."""
    with open(dss_path, encoding="utf-8-sig", errors="ignore") as stream:
        text = _without_comments(stream.read())
    refs = []
    for stripped in text.splitlines():
        match = _REDIRECT_RE.match(stripped) or _BUSCOORDS_RE.match(stripped)
        if match:
            role = "redirect" if _REDIRECT_RE.match(stripped) else "data"
            ref = _strip_quotes(match.group(1))
            if ref.startswith("[") and ref.endswith("]"):
                ref = _strip_quotes(ref[1:-1])
            refs.append((ref, role))
            continue
        for match in _DATA_FILE_RE.finditer(stripped):
            ref = next(value for value in match.groups() if value is not None)
            if ref.lower() == "%result%":
                raise CaseLoadError(ErrorCode.VALIDATION_FAILED, "动态 %result% 数据依赖不能冻结，请先保存为固定文件")
            refs.append((ref, "data"))
    return list(dict.fromkeys(refs))


def scan_dss_references(dss_path: str) -> list[str]:
    """解析 DSS 文件引用的**外部文件**（忽略注释行）。

    覆盖三类（都必须在装载时一并复制到任务副本，否则 Compile 期才报"文件找不到"）：
      * `Redirect <文件>` / `Compile <文件>` —— 其它 DSS 脚本；
      * `BusCoords <文件>` —— 母线坐标 CSV（非脚本，但 OpenDSS 会读）。
    """
    return [ref for ref, role in _file_references(dss_path)]


def _sanitize_v13_dss(path: str) -> list[str]:
    """在任务副本中禁用当前引擎不支持且 V1.3 不执行的控制迭代命令。

    只处理任务副本，不触碰用户原文件；其余 OpenDSS 指令保持原样。
    返回被替换的原始命令，供验证清单记录。
    """
    changed: list[str] = []
    lines: list[str] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if re.match(r"^\s*set\s+controliterations\s*=", line, re.IGNORECASE):
                changed.append(line.strip())
                lines.append("! [Tianshu V1.3] controliterations disabled; controlmode=off\n")
            else:
                lines.append(line)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    return changed


def load_case_bundle(
    case_ref: str, task_workspace: str, cases_root: str, allowed_roots: list[str] | None = None
) -> CanonicalDistributionCase:
    """把算例复制到任务隔离目录，递归解析依赖，生成清单与指纹。

    case_ref 支持：内置算例名（cases_root 下的目录名）或 Master.dss 绝对/相对路径。
    """
    input_dir = os.path.join(task_workspace, "input")
    os.makedirs(input_dir, exist_ok=True)

    # 1. 定位源算例
    builtin_dir = os.path.join(cases_root, case_ref)
    if os.path.isdir(builtin_dir):
        source_dir = builtin_dir
        master_name = "Master.dss"
        for fn in os.listdir(source_dir):
            if fn.lower() == "master.dss":
                master_name = fn
                break
        master_src = os.path.join(source_dir, master_name)
        if not os.path.isfile(master_src):
            raise CaseLoadError(ErrorCode.DATA_UNAVAILABLE, f"内置算例缺少 Master.dss: {case_ref}")
        source_type = "builtin"
    else:
        master_src = os.path.abspath(case_ref)
        if not os.path.isfile(master_src):
            raise CaseLoadError(ErrorCode.DATA_UNAVAILABLE, f"Master.dss 不存在: {case_ref}")
        source_dir = os.path.dirname(master_src)
        source_type = "user_file"

    # 转换器会在任务入口 Master.dss 中写入不可逆近似提示。把它提升为
    # 算例身份元数据，后续发布门将其标为受限结果，而不是把 converged
    # 错误解释成原始 XLSX/CSV/JSON/MATPOWER 已被等价复现。
    fidelity_status = "native"
    fidelity_warnings: list[str] = []
    source_format = "dss"
    source_metadata: dict[str, object] = {}
    try:
        with open(master_src, "r", encoding="utf-8", errors="ignore") as f:
            # 转换头的逐条警告可能超过4KiB；读到首个Circuit声明，保留所有头部注释。
            header_lines = []
            for line in f:
                header_lines.append(line)
                if re.match(r"^\s*new\s+circuit\.", line, re.IGNORECASE):
                    break
            header = "".join(header_lines)
            if "[Tianshu Conversion]" not in header:
                # 原生文件仍保留原4KiB扫描口径，兼容Circuit后面设置的频率等声明。
                header += f.read(max(0, 4096 - len(header)))
        # 先解析转换器写下的全部口径标记，再据此生成保真度警告——
        # 只有这样才能区分"相别信息真的被建模了"与"相别信息被丢了"，
        # 而不是对所有转换产物套用同一句"相别不平衡未保留"。
        for key in ("approximation", "enforce_q_limits", "load_source", "fidelity_records", "fidelity_sha256",
                    "input_load_records", "generated_load_records", "zero_load_records"):
            m = re.search(rf"{key}=([^;\s]+)", header, re.IGNORECASE)
            if m:
                source_metadata[key] = m.group(1).strip().lower()
        pm_match = re.search(r"phase_model=([^\n\r]*)", header, re.IGNORECASE)
        if pm_match:
            for pair in pm_match.group(1).split(";"):
                key, _, value = pair.strip().partition("=")
                if key.strip():
                    source_metadata[key.strip()] = value.strip()
        note_match = re.search(r"phase_note=([^\n\r]*)", header, re.IGNORECASE)
        if note_match:
            source_metadata["phase_note"] = note_match.group(1).strip()
        m = re.search(r"DefaultBaseFrequency=([0-9.]+)", header, re.IGNORECASE)
        if m:
            try:
                source_metadata["frequency_hz"] = float(m.group(1))
            except ValueError:
                pass

        marker = re.search(r"source_format=([^;\s]+)", header, re.IGNORECASE)
        if marker:
            source_format = marker.group(1)
            fidelity_status = "warning"
            # 相别建模三态：按相别建模 / 平衡等值 / 原生（无转换标记）
            phase_modeled = str(source_metadata.get("has_phase_columns", "0")) == "1"
            unbalanced = str(source_metadata.get("unbalanced_input", "0")) == "1"
            if phase_modeled:
                fidelity_warnings.append(
                    "输入文件由格式转换器映射为 OpenDSS 模型，**相别信息已按输入建模**"
                    f"（显式声明 {source_metadata.get('elements_with_phase', '?')} 个元件，"
                    f"默认三相 {source_metadata.get('assumed_three_phase', '?')} 个）；"
                    "结果仍不能直接视为原始求解器的等价复现。"
                )
                if unbalanced:
                    fidelity_warnings.append(
                        f"存在 {source_metadata.get('single_or_partial_elements', '?')} 个"
                        "单相/两相元件，已按相别接入相号节点，可给出真实分相结果。"
                    )
                remainder = (
                    "PV/PQ 无功限值、控制器和部分基准语义可能未完全保留，需交叉核验。"
                )
            else:
                fidelity_warnings.append(
                    "输入文件由格式转换器映射为平衡三相 OpenDSS 等值模型；"
                    "结果不能直接视为原始求解器的等价复现。"
                )
                remainder = (
                    "PV/PQ 无功限值、相别不平衡、控制器和部分基准语义可能未完全保留，需交叉核验。"
                )
            if "fidelity_sha256" in source_metadata:
                remainder = "无功限值与显式容量按本次转换头声明的策略建模；控制器和原始求解器特有语义仍需交叉核验。"
            fidelity_warnings.append(remainder)
        # 保留转换器的具体降级/忽略/默认值说明，不让通用保真度声明盖掉真实输入问题。
        specific = re.findall(
            r"^\s*(?:!|//)\s*\[Tianshu Conversion\]\s*"
            r"(?:warning|phase_warning|conversion_note)\s*=\s*([^\r\n]*)",
            header, re.IGNORECASE | re.MULTILINE,
        )
        if specific:
            fidelity_status = "warning"
            fidelity_warnings.extend(note.strip() for note in specific if note.strip())
        fidelity_warnings = list(dict.fromkeys(fidelity_warnings))
    except OSError:
        pass

    # 2. 路径安全：源必须位于批准根内（用户目录、内置算例目录或显式批准根）
    approved = [os.path.abspath(cases_root), os.path.abspath(source_dir)]
    for r in allowed_roots or []:
        approved.append(os.path.abspath(r))
    if not any(_is_within(master_src, root) for root in approved):
        raise CaseLoadError(
            ErrorCode.CASE_PATH_REJECTED, f"算例路径越出批准范围: {master_src}"
        )

    # 3. 递归复制 Master + Redirect/Compile 依赖（只读源 → 任务副本）
    manifest_entries: list[DependencyEntry] = []
    visited: set[str] = set()
    queued: set[str] = {os.path.normpath(master_src)}
    seen_elements: set[str] = set()
    redefined_elements: list[str] = []
    dependency_graph: dict[str, set[str]] = {}
    queue: list[tuple[str, str]] = [(master_src, "master")]  # (源绝对路径, role)

    while queue:
        src, role = queue.pop(0)
        src = os.path.normpath(src)
        queued.discard(src)
        src = os.path.normpath(src)
        if src in visited:
            continue
        visited.add(src)
        if not os.path.isfile(src):
            raise CaseLoadError(
                ErrorCode.DATA_UNAVAILABLE, f"DSS 依赖文件缺失: {src}"
            )
        # 依赖路径不得越出算例源目录（防路径逃逸）
        if not _is_within(os.path.realpath(src), os.path.realpath(source_dir)):
            raise CaseLoadError(
                ErrorCode.CASE_PATH_REJECTED, f"DSS 依赖引用越出算例目录: {src}"
            )
        rel = os.path.relpath(src, source_dir)
        dst = os.path.join(input_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        # Redirect/Compile 图必须是 DAG；循环依赖在复制阶段提前终止，
        # 不把无限递归/原生引擎崩溃留给 Web 请求处理。
        # OpenDSS 的 `New Class.Name` 对**已存在的名字**是**重定义**（后者覆盖前者），
        # 这是合法且常见写法 —— 官方 IEEE 测试算例自身就这么用：
        # IEEETestCases/13Bus/IEEE13Nodeckt.dss 在 L76 与 L98 两次定义
        # `linecode.mtx606`（L82 注明是 2016-02-03 RDugan 的修正）。
        # 此前把任何重名都当致命错误拒绝，导致**官方 IEEE 13 节点算例无法装载**。
        # 现在改为：允许重定义，但把重名记入 source_metadata 以便追溯。
        # 注：同一文件被装载两次这类情况由上面的 `visited` 集合挡住，不依赖本检查。
        redefined = _scan_duplicate_element_names(dst, seen_elements) if role != "data" else []
        if redefined:
            redefined_elements.extend(redefined)
        # V1.3 固定关闭自动控制；对任务副本中的控制迭代命令做兼容性降级。
        if role != "data":
            _sanitize_v13_dss(dst)
        manifest_entries.append(
            DependencyEntry(
                path=rel.replace(os.sep, "/"),
                sha256=_sha256_file(dst),
                size_bytes=os.path.getsize(dst),
                role=role,
            )
        )
        refs = _file_references(dst) if role != "data" else []
        dependency_graph.setdefault(src, set())
        for ref, dependency_role in refs:
            ref_path = ref
            if os.path.isabs(ref_path):
                raise CaseLoadError(
                    ErrorCode.CASE_PATH_REJECTED,
                    f"DSS 依赖引用不允许使用绝对路径：{ref_path}",
                )
            ref_path = os.path.normpath(os.path.join(os.path.dirname(src), ref_path))
            if dependency_role != "data":
                dependency_graph[src].add(ref_path)
            if _has_dependency_cycle(dependency_graph):
                raise CaseLoadError(
                    ErrorCode.VALIDATION_FAILED,
                    f"检测到 DSS Redirect 循环依赖：{src} -> {ref_path}",
                )
            if ref_path not in visited and ref_path not in queued:
                queued.add(ref_path)
                queue.append((ref_path, dependency_role))

    # OpenDSS 语义下重定义是合法的（后者覆盖前者），因此不构成拒绝理由；
    # 但要留痕，便于事后核对"这个算例里哪些元件被覆盖过"。
    if redefined_elements:
        source_metadata["redefined_element_names"] = sorted(set(redefined_elements))

    master_rel = os.path.relpath(
        os.path.join(input_dir, os.path.relpath(master_src, source_dir)), input_dir
    ).replace(os.sep, "/")

    # 4. 聚合指纹：排序后的 (path, sha256) 串联再哈希
    agg = hashlib.sha256()
    for e in sorted(manifest_entries, key=lambda x: x.path):
        agg.update(f"{e.path}:{e.sha256}\n".encode())
    fingerprint = agg.hexdigest()[:16]

    manifest = DependencyManifest(master_dss=master_rel, entries=manifest_entries)
    case_id = os.path.splitext(os.path.basename(master_src))[0]

    return CanonicalDistributionCase(
        case_id=case_id,
        case_fingerprint=fingerprint,
        master_dss_ref=master_rel,
        dependency_manifest=manifest,
        source_type=source_type,
        canonical_workspace_ref=task_workspace,
        source_format=source_format,
        source_metadata=source_metadata,
        fidelity_status=fidelity_status,
        fidelity_warnings=fidelity_warnings,
    )
