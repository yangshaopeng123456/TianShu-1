"""转换产物新鲜度管理（解决"换算例复用过期转换结果"）。

## 真实缺陷

上传算例的转换产物（`chat_data/uploads/<sess>/<case>/Master.dss`）写入后长期复用。
转换器一旦改进（例如 MATPOWER 母线 Vm 漏读、PV 节点被当恒功率两处修复），
**老算例不会重新转换** —— 于是修复对已有算例完全无效，而且没有任何提示，
用户看到的仍是旧模型的数值。实测：case12/case09 的产物是 8/30、9/9 生成的，
里面还是 `pu=1`、`Model=1`。

## 做法

1. 转换时在产物目录写 `.conversion.json`：转换器 id + 版本 + 源文件路径 + 源文件 sha256。
2. 引用算例时校验版本；不一致且源文件仍在 → **自动重新转换**。
3. 没有 meta 的历史产物视为过期（来源未知），能推断出源文件的同样重转换。
4. 源文件已不存在 / 无法推断 → 保持原样，只记日志（绝不擅自删产物，也不静默换算例）。

## 约定

**改了转换逻辑必须升 `CONVERTER_VERSIONS` 里的版本号**，否则老算例不会被重建。
源文件内容变化也会触发重建；转换逻辑变化仍须升版本号，不能只依赖源文件变化。
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import zipfile
from typing import Any, Callable

logger = logging.getLogger(__name__)

META_NAME = ".conversion.json"
CONVERTED_DIRNAME = "_converted"
_rebuild_locks: dict[str, threading.RLock] = {}
_rebuild_locks_guard = threading.Lock()


def conversion_output_dir(source_path: str) -> str:
    """以完整源文件名隔离产物，保留扩展名以区分同stem的不同输入格式。"""
    source_path = os.path.abspath(source_path)
    return os.path.join(os.path.dirname(source_path), CONVERTED_DIRNAME, os.path.basename(source_path))


def _rebuild_lock(master_path: str) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(master_path))
    with _rebuild_locks_guard:
        return _rebuild_locks.setdefault(key, threading.RLock())

# 转换器版本表。语义：产物里的 meta.version != 这里的值 → 该产物过期。
CONVERTER_VERSIONS: dict[str, str] = {
    # case_converters：xlsx / csv / json / matpower .m（xlsx_to_dss 是它的兼容封装）
    # 1.1.0 = MATPOWER 母线 Vm 参与源电压设定 + PV 节点 Model=3 电压控制建模
    # 1.2.0 = MATPOWER 支路变比 ratio 写入 tap（此前被整体丢弃）+ 变压器
    #         %R/XHL 由系统基准折算到变压器自身 kVA 基准
    # 1.3.0 = 修正变压器阻抗基准：kVA 基准取系统基准（rateA 是热稳限额，不是阻抗
    #         基准），%Rs 双绕组对半拆分（此前电阻翻倍）
    # 1.4.0 = MATPOWER 发电机 Vg 作为本节点电压设定值对 PV 节点生效
    #         （此前只对 Slack 且母线缺 Vm 时才用 Vg）
    # 1.5.0 = ① 表格算例的电压设定列 vm_set_pu 纳入别名（此前源电压回落 1.0）
    #         ② 变压器判定改为以"两端电压等级是否一致"的物理事实为准，
    #            修复 tie / parallel_transformer 等 kind 值被当成普通线路
    #            （case09~11 因此少 2 条变压器且产生跨压"线路"）
    #         ③ 线路充电电纳 b 建模为 C1（此前整列未读）
    #         ④ 频率从算例读取（此前硬编码 50）
    #         ⑤ PV 无功限值改为默认不执行（参考解口径为 unconstrained PV-Q），
    #            需要严格限值语义时用 ENFORCE_Q_LIMITS=1
    # 1.6.0 = 支持分相（三相不平衡）建模：
    #         ① 相别列（phases/phase/conn/conn1/conn2）纳入别名，单相/两相元件
    #            生成相号节点（Bus1=x.2）与 Phases=k，单相负荷 kV 按 √3 折算；
    #         ② 零序参数 r0/x0/b0（标么或欧姆）→ OpenDSS 的 R0/X0/C0；
    #         ③ 可选独立负荷表（xlsx 的 Load 表 / json 的 load 数组 / csv 的
    #            type=load 记录 / 目录下的 load.csv），逐条声明相别；
    #         ④ 变压器支持单相（phases=1）与分侧接线（conn1/conn2 → delta-wye）；
    #         ⑤ 头部写 phase_model= 声明；**无相别列时输出与 1.5.0 逐字节一致**。
    # 1.6.1 = 修复致命 bug：**不再给 `New Line` 写 `Conn=`**。
    #         OpenDSS 的 Line 没有 Conn 属性（只有 Load/Generator 有），写上会让
    #         COM 引擎在 compile 阶段**死循环**（120s 硬超时；命令行版对文件静默不执行，
    #         所以离线发现不了）。最小差异实测：同一模型删掉该词即 5s 内正常收敛。
    #         线路上的 conn 列现改为**显式忽略并计入声明**（接线对线路无意义）。
    # 1.6.2 = 修复连通性误判：虚拟源 `SOURCE` 不再让整条支路被丢弃。
    #         原实现「任一端是 SOURCE 就不进连通图」，于是平衡母线自己成了孤点，
    #         其下游全部母线被判"供电孤岛" —— 实测一个拓扑完全连通的 5 母线
    #         辐射网（SOURCE→B226/B213，B226→BUS_121/LOAD_E04）被整片拒绝。
    #         现在把 SOURCE 折叠到实际平衡母线上再算连通分量。
    #         本版**只改校验**，生成的 Master.dss 与 1.6.1 逐字节一致。
    # 1.6.3 = 修复 System 表形态导致的**静默降级**：
    #         "结构化单行"分支原先要求表里必须含 `base_mva` 或 `base_kv` 列才进入，
    #         于是只声明 `frequency_hz`（或只给电压/短路容量基准）的 System 表
    #         **整行被丢弃**，频率回落硬编码默认 50 Hz —— 60 Hz 算例的线路充电电纳
    #         （B=2πfC）随之整体算错，而产物里连一句提示都没有。
    #         现在：判据改为「**单行且非 key/value 形态**」（这才是"结构化单行"的本义，
    #         `system_df` 已由上游按表名/键识别）；且**系统表给了却一个参数都没读出来时，
    #         在产物头部显式警告**（静默丢弃是最不该有的行为）。
    #         ⇒ 受影响输入的转换产物**会变**（频率、basekv、MVAsc、充电电纳），必须升号。
    # 1.6.4 = MATPOWER 仅聚合在线机组；ZIP 产物隔离，失败重建保留原模型。
    # 1.6.5 = 严格解析/默认值声明、完整文件身份隔离、暂存发布及源内容变更新鲜度。
    "dss_from_tables": "1.7.0",
}

# 扩展名 → (转换器 id, 转换函数名)
_EXT_TO_CONVERTER: dict[str, tuple[str, str]] = {
    ".xlsx": ("dss_from_tables", "convert_xlsx"),
    ".csv": ("dss_from_tables", "convert_csv"),
    ".json": ("dss_from_tables", "convert_json"),
    ".m": ("dss_from_tables", "convert_matpower_m"),
}

_SUPPORTED_SOURCE_EXTS = tuple(_EXT_TO_CONVERTER) + (".zip", ".dss")


# --------------------------------------------------------------------- 基础工具


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _csv_is_mixed(path: str) -> bool:
    """混合记录CSV自身就是整网，不能因文件名叫bus.csv而把它当组成表。"""
    try:
        with open(path, encoding="utf-8-sig", newline="") as stream:
            rows = csv.DictReader(stream)
            type_key = next((key for key in rows.fieldnames or [] if key.strip().lower() == "type"), None)
            if type_key is None:
                return False
            return any(str(row.get(type_key) or "").strip().lower() in
                       {"bus", "node", "line", "branch", "transformer", "system", "load", "loads"}
                       for row in rows)
    except (OSError, UnicodeError, csv.Error):
        return False


def _source_state(source: str) -> dict[str, Any]:
    """源指纹及CSV伴随表清单；新增、修改、缺失依赖都改变清单。"""
    source = os.path.abspath(source)
    dependencies: list[dict[str, str]] = []
    kind = "file"
    if os.path.isdir(source):
        kind = "csv_directory"
        paths = []
        for root, dirs, files in os.walk(source):
            dirs[:] = [name for name in dirs if name != CONVERTED_DIRNAME
                       and not name.startswith((".conversion-rebuild-", ".zip-rebuild-"))]
            paths.extend(os.path.join(root, name) for name in files if name.lower().endswith(".csv"))
        dependencies = [{"path": path, "sha256": _sha256_file(path)} for path in sorted(paths)]
        payload = [{"path": os.path.relpath(item["path"], source), "sha256": item["sha256"]}
                   for item in dependencies]
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    else:
        fingerprint = _sha256_file(source) if os.path.isfile(source) else ""
        if os.path.basename(source).lower() in {"bus.csv", "line.csv", "branch.csv"} and not _csv_is_mixed(source):
            directory = os.path.dirname(source)
            try:
                entries = {name.lower(): os.path.join(directory, name) for name in os.listdir(directory)
                           if os.path.isfile(os.path.join(directory, name))}
            except OSError:
                entries = {}
            if "bus.csv" in entries and ("line.csv" in entries or "branch.csv" in entries):
                kind = "csv_bundle"
                dependencies = [{"path": entries[name], "sha256": _sha256_file(entries[name])}
                                for name in sorted(entries)
                                if name in {"bus.csv", "line.csv", "branch.csv", "system.csv", "load.csv", "loads.csv"}]
    return {"source_sha256": fingerprint, "source_dependencies": dependencies, "source_kind": kind}


def meta_path(output_dir: str) -> str:
    return os.path.join(output_dir, META_NAME)


def write_meta(output_dir: str, converter: str, source_path: str) -> dict[str, Any]:
    """在产物目录写转换元数据。写失败不影响转换结果本身（只记日志）。"""
    meta = {
        "converter": converter,
        "version": CONVERTER_VERSIONS.get(converter, "unknown"),
        "source": os.path.abspath(source_path) if source_path else "",
        **(_source_state(source_path) if source_path else {"source_sha256": "", "source_dependencies": [], "source_kind": "file"}),
        "source_mtime": os.path.getmtime(source_path) if source_path and os.path.isfile(source_path) else None,
        "master_sha256": _sha256_file(os.path.join(output_dir, "Master.dss")),
        "converted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    temporary: str | None = None
    try:
        os.makedirs(output_dir, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".conversion-meta-", dir=output_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, meta_path(output_dir))
        temporary = None
    except OSError as exc:  # pragma: no cover - 磁盘异常
        logger.warning("[conversion] 写入转换元数据失败 %s: %s", output_dir, exc)
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass
    return meta


def read_meta(output_dir: str) -> dict[str, Any] | None:
    p = meta_path(output_dir)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def meta_status(output_dir: str) -> tuple[str, dict[str, Any] | None]:
    """返回 ("fresh" | "stale" | "unknown", meta)。

    - fresh：有 meta 且版本、可验证的源文件内容与记录一致；
    - stale：有 meta 但版本不一致或有指纹的源文件内容已改变；
    - unknown：无 meta（历史产物）或 meta 不可读。
    """
    meta = read_meta(output_dir)
    if not meta:
        return "unknown", None
    converter = str(meta.get("converter") or "")
    recorded = str(meta.get("version") or "")
    current = CONVERTER_VERSIONS.get(converter)
    if not converter or not current:
        return "unknown", meta
    if recorded != current:
        return "stale", meta
    source = str(meta.get("source") or "")
    recorded_sha = str(meta.get("source_sha256") or "")
    if source and os.path.exists(source):
        state = _source_state(source)
        if recorded_sha and state["source_sha256"] != recorded_sha:
            return "stale", meta
        if ("source_dependencies" in meta or state["source_dependencies"]) and meta.get("source_dependencies", []) != state["source_dependencies"]:
            return "stale", meta
    elif meta.get("source_dependencies"):
        return "stale", meta
    return "fresh", meta


# --------------------------------------------------------------------- 源文件定位


def _infer_source(output_dir: str, meta: dict[str, Any] | None) -> str | None:
    """推断产物对应的源文件。

    优先用 meta 记录的路径；缺失时按上传目录约定反推：
    产物在 `<upload_dir>/<stem>/`，源文件是 `<upload_dir>/<stem>.<ext>`。

    歧义保护：若同名源文件存在多个扩展名（如同时有 `case1.xlsx` 与 `case1.m`），
    **不猜**——从错误的源重建会静默换掉整个网络模型，比沿用旧产物危险得多。
    """
    if meta:
        src = str(meta.get("source") or "")
        if src and (os.path.isfile(src) or os.path.isdir(src)):
            return src
    # 新布局保留完整filename，即使历史打戳失败也能精确回查，避免跨格式猜源。
    output_dir = os.path.abspath(output_dir.rstrip(os.sep))
    namespace = os.path.dirname(output_dir)
    if os.path.basename(namespace) == CONVERTED_DIRNAME:
        candidate = os.path.join(os.path.dirname(namespace), os.path.basename(output_dir))
        if os.path.isfile(candidate) and os.path.splitext(candidate)[1].lower() in _SUPPORTED_SOURCE_EXTS:
            return candidate
    parent = os.path.dirname(os.path.abspath(output_dir.rstrip(os.sep)))
    stem = os.path.basename(os.path.abspath(output_dir.rstrip(os.sep)))
    if not parent or not stem:
        return None
    try:
        entries = os.listdir(parent)
    except OSError:
        return None
    candidates: list[str] = []
    for entry in entries:
        name, ext = os.path.splitext(entry)
        if name.lower() != stem.lower() or ext.lower() not in _SUPPORTED_SOURCE_EXTS:
            continue
        full = os.path.join(parent, entry)
        if os.path.isfile(full):
            candidates.append(full)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "[conversion] %s 同名源文件有多个（%s），为避免按错误源重建，沿用旧产物",
            output_dir, ", ".join(os.path.basename(c) for c in candidates),
        )
    return None


def _load_converter(name: str) -> Callable[..., str]:
    from . import case_converters

    return getattr(case_converters, name)


class _PublishRollbackFailed(OSError):
    """发布和回滚均失败时，携带必须保留的原模型备份路径。"""

    def __init__(self, backup_dir: str):
        super().__init__(f"发布失败且原模型回滚失败，备份保留于 {backup_dir}")
        self.backup_dir = backup_dir


def _publish_staged_directory(staged_dir: str, target_dir: str, work: str) -> None:
    backup_dir = os.path.join(work, "previous")
    had_previous = os.path.exists(target_dir)
    if had_previous:
        os.replace(target_dir, backup_dir)
    try:
        os.replace(staged_dir, target_dir)
    except OSError:
        if had_previous:
            try:
                os.replace(backup_dir, target_dir)
            except OSError as exc:
                raise _PublishRollbackFailed(backup_dir) from exc
        raise


def _stage_and_publish(
    source: str, target_dir: str, builder: Callable[[str], str | None], *,
    converter: str = "dss_from_tables", prefix: str = ".conversion-rebuild-",
    preserve_existing: bool = False, raise_errors: bool = False,
) -> str | None:
    """转换和元信息均验证成功后发布完整目录；任何早期失败不触碰旧模型。"""
    source = os.path.abspath(source)
    target_dir = os.path.abspath(target_dir)
    work: str | None = None
    preserve_backup = False
    try:
        parent_dir = os.path.dirname(target_dir)
        os.makedirs(parent_dir, exist_ok=True)
        work = tempfile.mkdtemp(prefix=prefix, dir=parent_dir)
        staged_dir = os.path.join(work, "model")
        os.makedirs(staged_dir)
        if preserve_existing and os.path.isdir(target_dir):
            # 保留旧目录的附属文件/目录内源文件，但不能让旧Master/meta冒充新转换成功。
            def omit_generated(folder: str, names: list[str]) -> list[str]:
                if os.path.normcase(os.path.abspath(folder)) != os.path.normcase(target_dir):
                    return []
                return [name for name in names if name.lower() in {"master.dss", META_NAME.lower()}
                        or name.startswith(".conversion-meta-")]

            shutil.copytree(target_dir, staged_dir, dirs_exist_ok=True, ignore=omit_generated)
        source_state = _source_state(source)
        master = builder(staged_dir)
        if not master or not os.path.isfile(master):
            return None
        master = os.path.abspath(master)
        if os.path.normcase(os.path.commonpath([master, staged_dir])) != os.path.normcase(staged_dir):
            raise ValueError("转换器返回的Master.dss不在暂存目录内")
        master_rel = os.path.relpath(master, staged_dir)
        meta_dir = os.path.dirname(master)
        expected_meta = write_meta(meta_dir, converter, source)
        if read_meta(meta_dir) != expected_meta:
            raise OSError("重建元信息写入失败")
        recorded_state = {key: expected_meta.get(key) for key in source_state}
        if recorded_state != source_state or _source_state(source) != source_state:
            raise OSError("源文件在转换期间发生变化，拒绝发布不一致模型")
        _publish_staged_directory(staged_dir, target_dir, work)
        return os.path.join(target_dir, master_rel)
    except _PublishRollbackFailed as exc:
        preserve_backup = True
        logger.error("[conversion] %s", exc)
        if raise_errors:
            raise
        return None
    except Exception as exc:  # noqa: BLE001 - 失败保留旧模型；调用方沿用旧产物并记录原因
        logger.warning("[conversion] 重建失败 %s: %s", source, exc)
        if raise_errors:
            raise
        return None
    finally:
        if work and not preserve_backup:
            shutil.rmtree(work, ignore_errors=True)


def _reconvert_from_source(source: str, output_dir: str, *, raise_errors: bool = False) -> str | None:
    """按源文件扩展名重新执行转换。返回新的 Master.dss 路径，失败返回 None。"""
    ext = os.path.splitext(source)[1].lower()
    if ext == ".dss":
        return source
    if ext == ".zip":
        return _reconvert_from_zip(source, output_dir)
    mapping = _EXT_TO_CONVERTER.get(".csv" if os.path.isdir(source) else ext)
    if not mapping:
        return None
    _, func_name = mapping
    try:
        fn = _load_converter(func_name)
    except (ImportError, AttributeError) as exc:
        logger.warning("[conversion] 装载转换器失败 %s: %s", func_name, exc)
        if raise_errors:
            raise
        return None
    # 上传重传与引用触发的重建也共享锁；refresh中的同锁可重入。
    with _rebuild_lock(os.path.join(output_dir, "Master.dss")):
        return _stage_and_publish(source, output_dir, lambda staged: fn(source, staged),
                                  converter=mapping[0], preserve_existing=True, raise_errors=raise_errors)


def _reconvert_from_zip(zip_path: str, output_dir: str) -> str | None:
    """在临时目录重建 ZIP，成功后替换；失败时旧模型与元信息不变。

    新布局的产物位于 ZIP 自己的解压目录；历史 converted_csv/json 布局仍在
    原路径更新。嵌套的 Master.dss 必须整包替换，保留其相对路径依赖。
    """
    zip_path = os.path.abspath(zip_path)
    output_dir = os.path.abspath(output_dir)
    extract_dir = os.path.join(os.path.dirname(zip_path),
                               os.path.splitext(os.path.basename(zip_path))[0])
    try:
        in_extract = os.path.normcase(os.path.commonpath([output_dir, extract_dir])) == os.path.normcase(extract_dir)
    except ValueError:  # 跨盘历史路径
        in_extract = False
    target_dir = extract_dir if in_extract else output_dir
    def rebuild(staged_dir: str) -> str | None:
        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(staged_dir)

        master = next((os.path.join(root, name)
                       for root, _dirs, files in os.walk(staged_dir)
                       for name in files if name.lower() == "master.dss"), None)
        if master is None:
            from . import case_converters

            entries = os.listdir(staged_dir)
            if any(name.lower().endswith(".csv") for name in entries):
                master = case_converters.convert_csv(staged_dir, staged_dir)
            else:
                json_files = [name for name in entries if name.lower().endswith(".json")]
                if json_files:
                    master = case_converters.convert_json(
                        os.path.join(staged_dir, json_files[0]), staged_dir)
        return master

    return _stage_and_publish(zip_path, target_dir, rebuild, prefix=".zip-rebuild-")


# --------------------------------------------------------------------- 对外入口


def refresh_master_if_stale(master_path: str | None, force: bool = False) -> tuple[str | None, str]:
    """必要时重建转换产物。

    返回 `(可用的 Master.dss 路径, 说明)`。说明用于落日志/排查：
    - `"fresh"`            产物是最新版本，未改动；
    - `"rebuilt"`          已按新版本重建，且内容与旧产物**不同**；
    - `"rebuilt-same"`     已重建，但内容与旧产物逐字节相同（版本号升了、该算例输出没变）；
    - `"no-meta-rebuilt"`  历史产物（无 meta）已重建；
    - `"stale-unresolved"` 过期但源文件找不到，沿用旧产物；
    - `"not-applicable"`   非转换产物（内置算例、直传 .dss 等），无需处理。
    """
    if not master_path:
        return master_path, "not-applicable"
    # 同一个模型的并发重建串行发布，避免两次重命名/回滚互相争用目录。
    with _rebuild_lock(master_path):
        return _refresh_master_if_stale(master_path, force)


def _refresh_master_if_stale(master_path: str, force: bool) -> tuple[str | None, str]:
    if not os.path.isfile(master_path):
        return master_path, "not-applicable"

    output_dir = os.path.dirname(os.path.abspath(master_path))
    status, meta = meta_status(output_dir)

    if not force:
        if status == "fresh":
            return master_path, "fresh"
        if status == "unknown" and not _infer_source(output_dir, meta):
            # 内置算例 / 直接投递的 .dss：没有源文件可推断，本来就是最终产物
            return master_path, "not-applicable"

    source = _infer_source(output_dir, meta)
    if not source:
        logger.warning("[conversion] %s 判定为过期但找不到源文件，沿用旧产物", output_dir)
        return master_path, "stale-unresolved"

    old_sha = ""
    try:
        old_sha = _sha256_file(master_path)
    except OSError:
        pass
    rebuilt = _reconvert_from_source(source, output_dir)
    if not rebuilt or not os.path.isfile(rebuilt):
        logger.warning("[conversion] %s 重建失败，沿用旧产物", output_dir)
        return master_path, "stale-unresolved"

    verb = "rebuilt" if status == "stale" else "no-meta-rebuilt"
    logger.info(
        "[conversion] %s：%s 已重建（converter=%s 记录版本=%s 当前版本=%s）",
        verb, rebuilt,
        (meta or {}).get("converter") or "inferred",
        (meta or {}).get("version") or "-",
        CONVERTER_VERSIONS.get(str((meta or {}).get("converter") or "") or "dss_from_tables", "-"),
    )
    if old_sha and _sha256_file(rebuilt) == old_sha:
        return rebuilt, "rebuilt-same"
    return rebuilt, verb
