"""本地存储盘点与历史清理。

设计原则（沿用项目既有约定）：

1. **扫描与执行分离**。`scan()` 只读，不碰任何文件；`archive()` 才动文件，
   且必须显式给出 `items` 清单 —— 不做"扫描到什么就删什么"的隐式操作。
2. **归档而非删除**。所有清理都是 `shutil.move` 到归档目录，可回滚。
   归档目录默认在 outputs_root 的**同级** `_archive/` 下，不与应用数据混放。
3. **不猜、不兜底**。分类只依据可验证的事实（文件数、体积、是否被产物引用），
   拿不准的一律归入 `keep` 并标注原因，绝不因为"看起来没用"就删。
4. **上传原件永不清理**。`chat_data/uploads/` 是算例的唯一原始来源，
   扫描时只统计、不生成清理项。

四类候选（保守排序，风险从低到高）：

| 类别 | 含义 | 风险 |
|---|---|---|
| `empty_session_dirs` | uploads 下 0 文件的会话目录 | 无（删空目录不影响任何数据） |
| `failed_task_residue` | 无 report / 无产物 / outputs 里也没有的任务目录 | 低（任务本身没跑出结果） |
| `orphan_outputs`   | outputs 里对应任务目录已不存在的产物 | 中（结果还在，过程记录丢了） |
| `legacy_outputs`   | 总负荷口径修复前生成的产物 | 中（数值仍有效，仅口径旧） |
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------- 数据模型


@dataclass
class CleanItem:
    """一个可清理项。"""

    kind: str  # empty_session_dirs / failed_task_residue / orphan_outputs / legacy_outputs
    path: str
    rel_path: str
    size_bytes: int = 0
    file_count: int = 0
    reason: str = ""
    risk: str = "low"  # low / medium / high
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class DirStat:
    name: str
    path: str
    file_count: int
    size_bytes: int
    entry_count: int
    note: str = ""


# ---------------------------------------------------------------- 工具

#: 目录结构异常的判据。背景：CLI 适配器的镜像递归缺陷曾产出 **4247 层嵌套**
#: 目录（2124 个文件），这类目录有两个危害：
#:   1. **拖慢扫描** —— 实测单次 `scan()` 从 4s 涨到 268s（每次都要走完全树）；
#:   2. **绕过清理判据** —— 它含 `input/Master.dss`，于是落进「含源文件 → 保守保留」
#:      那一支，永远列不进候选，也就永远无法处置。
#: 正常任务目录最多 4 层（`input/cli_work/case/...`），子目录数远小于 300。
_MAX_TASK_DEPTH = 25
_MAX_TASK_DIRS = 300


def probe_pathological(path: str) -> str:
    """快速判断目录结构是否异常；返回原因（空串 = 正常）。

    **早退**：`os.walk` 是惰性的，一旦超过深度/数量上限就立即返回，
    不需要走完整棵树 —— 这是它敢在扫描里逐个任务调用的前提。
    """
    dirs_seen = 0
    try:
        for dp, dns, _fn in os.walk(path):
            rel = os.path.relpath(dp, path)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth > _MAX_TASK_DEPTH:
                return f"目录嵌套过深（超过 {_MAX_TASK_DEPTH} 层）"
            dirs_seen += len(dns)
            if dirs_seen > _MAX_TASK_DIRS:
                return f"子目录数量异常（超过 {_MAX_TASK_DIRS} 个）"
    except OSError:
        return ""
    return ""


def _dir_stat(path: str, *, max_depth: int | None = None) -> tuple[int, int]:
    """返回 (文件数, 总字节)。失败静默跳过单个文件，不因权限问题整体失败。

    `max_depth` 给定时**只统计到该深度**（超出的子目录被剪枝，不再深入）。
    用于包含病态子目录的场景：那里全量遍历没有意义（数字不准确），
    却要付出每层目录的系统调用代价。**不传则全量统计**（正常路径）。
    """
    n = 0
    total = 0
    for dp, dns, fn in os.walk(path):
        if max_depth is not None:
            rel = os.path.relpath(dp, path)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= max_depth:
                dns[:] = []  # 剪枝：不再深入
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
                n += 1
            except OSError:
                pass
    return n, total


def _list_files(path: str) -> list[str]:
    out: list[str] = []
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            out.append(os.path.relpath(os.path.join(dp, f), path))
    return out


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


# ---------------------------------------------------------------- 扫描


def _uploads_dir() -> str:
    from . import chat_store as store

    return store.UPLOADS_DIR


def _task_ids_with_outputs(outputs_root: str) -> set[str]:
    """outputs/ 下的**任务产物**目录。

    只认 `pf-` 前缀。outputs/ 里还混着系统自己的辅助目录（`_cache`、`_metrics`、
    `converter_audit*`、`hard_cases_*`、`rag_index` 等），
    它们不是任务产物，不得参与"孤儿产物"判定——否则会把审计归档和 PPT 素材
    当成可清理项列出来。

    ⚠️ **注意这套规则的实际后果**：辅助目录**只展示、不清除**，所以它们会一直堆着。
    例：`tianshu_ppt` ~ `tianshu_ppt_v4` 四个 PPT 构建工作区曾累计 **121.5 MB**，
    从 8/31 一直留到 2026-09-24 才由人工清理（`.cache/media`、`.slidep/bak` 占了绝大部分）。
    若再次生成 PPT，它们会在同一位置重新出现 —— 属于同一类辅助目录，同样不会被自动清掉。
    """
    if not os.path.isdir(outputs_root):
        return set()
    out = set()
    for d in os.listdir(outputs_root):
        if d.startswith("pf-") and os.path.isdir(os.path.join(outputs_root, d)):
            out.add(d)
    return out


def _aux_output_dirs(outputs_root: str) -> list[DirStat]:
    """outputs/ 下的非任务目录（辅助数据），单独列出供 UI 展示，不参与清理。"""
    out: list[DirStat] = []
    if not os.path.isdir(outputs_root):
        return out
    for d in sorted(os.listdir(outputs_root)):
        p = os.path.join(outputs_root, d)
        if not os.path.isdir(p) or d.startswith("pf-"):
            continue
        n, s = _dir_stat(p)
        out.append(DirStat(d, p, n, s, 0, "系统辅助数据（非任务产物），不参与清理"))
    return out


def scan(cfg: Any) -> dict[str, Any]:
    """只读盘点。返回结构化的统计 + 候选清单，不修改任何文件。"""
    outputs_root = cfg.outputs_root
    tasks_root = cfg.workspace_root
    uploads = _uploads_dir()

    # ---------- 0. 病态目录探测（必须最先做） ----------
    # 它决定两件事：① 下面的规模统计要不要限深；② 第 3 节的候选判据。
    # 逐个任务早退探测的成本可以忽略：正常任务目录只有几层。
    pathological: dict[str, str] = {}
    if os.path.isdir(tasks_root):
        for tid in sorted(os.listdir(tasks_root)):
            if tid == "audit":
                continue
            tpath = os.path.join(tasks_root, tid)
            if not os.path.isdir(tpath):
                continue
            why = probe_pathological(tpath)
            if why:
                pathological[tid] = why

    # ---------- 1. 目录规模 ----------
    dirs: list[DirStat] = []
    tasks_note = "任务过程记录：trace / state / report / input 快照"
    if pathological:
        # 有异常嵌套时**限深统计**：全量遍历这类目录没有意义（数字不准确），
        # 却会把扫描拖到分钟级。数字会偏小，所以在备注里说明。
        tasks_note += (
            f"；⚠️ 含 {len(pathological)} 个结构异常目录，本项为**限深统计**（下限）"
        )
    for name, p, note, depth in (
        ("uploads", uploads, "各会话上传的算例原件（唯一来源，不参与清理）", None),
        ("tasks", tasks_root, tasks_note,
         _MAX_TASK_DEPTH if pathological else None),
        ("outputs", outputs_root, "任务对外产物：结果 JSON / 报告 / 图表", None),
    ):
        if os.path.isdir(p):
            n, s = _dir_stat(p, max_depth=depth)
            try:
                entries = len(os.listdir(p))
            except OSError:
                entries = 0
            dirs.append(DirStat(name, p, n, s, entries, note))

    # chat.db 单独计
    from . import chat_store as store

    db_size = 0
    if os.path.isfile(store.DB_PATH):
        try:
            db_size = os.path.getsize(store.DB_PATH)
        except OSError:
            pass

    total_bytes = sum(d.size_bytes for d in dirs) + db_size
    total_files = sum(d.file_count for d in dirs) + (1 if db_size else 0)

    # ---------- 2. 候选：空会话目录 ----------
    empty_dirs: list[CleanItem] = []
    if os.path.isdir(uploads):
        for name in sorted(os.listdir(uploads)):
            p = os.path.join(uploads, name)
            if not os.path.isdir(p):
                continue
            try:
                items = os.listdir(p)
            except OSError:
                continue
            if not items:
                empty_dirs.append(
                    CleanItem(
                        kind="empty_session_dirs",
                        path=p,
                        rel_path=os.path.join("chat_data", "uploads", name),
                        reason="会话目录为空，未上传任何文件",
                        risk="low",
                    )
                )

    # ---------- 3. 候选：失败残留任务 ----------
    out_ids = _task_ids_with_outputs(outputs_root)
    failed: list[CleanItem] = []
    kept_reason: dict[str, str] = {}
    if os.path.isdir(tasks_root):
        for tid in sorted(os.listdir(tasks_root)):
            tpath = os.path.join(tasks_root, tid)
            if not os.path.isdir(tpath):
                continue
            if tid == "audit":
                continue  # 审计日志，永不清理

            why = pathological.get(tid)
            if why:
                # 结构异常的任务：**优先于**「含源文件 → 保守保留」处置。
                # 依据：这类目录是异常拷贝的产物，该任务从未跑出结果；而归档是
                # **移动**不是删除，`input/` 里的源文件会随任务目录一起保留 ——
                # 「唯一工程样本」不会丢，所以保守保留在这里失去意义，
                # 反而让它永远卡在盘上、每次扫描都要为它付代价。
                # ⚠️ 这里**不做** `_list_files`：对深目录极慢（实测 4 分钟级）。
                n, size = _dir_stat(tpath, max_depth=_MAX_TASK_DEPTH)
                failed.append(
                    CleanItem(
                        kind="failed_task_residue",
                        path=tpath,
                        rel_path=os.path.join("tasks", tid),
                        size_bytes=size,
                        file_count=n,
                        reason=f"目录结构异常（{why}），疑似异常拷贝产物；"
                               "无报告、无产物结果（统计已限深，为下限）",
                        risk="medium",
                        detail={
                            "pathological": why,
                            "note": "归档为移动操作，input/ 内的源文件会一并保留",
                        },
                    )
                )
                continue

            files = _list_files(tpath)
            has_report = any(f.endswith("report.md") for f in files)
            has_result = any("powerflow_result" in f for f in files)
            in_outputs = tid in out_ids
            if has_report or has_result or in_outputs:
                continue  # 有产出，不是残留
            # 带 .dss 等源文件的保留：可能是独一无二的工程样本
            has_source = any(f.lower().endswith((".dss", ".m", ".json", ".csv", ".xlsx")) for f in files)
            n, size = _dir_stat(tpath)
            if has_source:
                kept_reason[tid] = f"含源文件（{len(files)} 文件），可能为唯一工程样本，保守保留"
                continue
            failed.append(
                CleanItem(
                    kind="failed_task_residue",
                    path=tpath,
                    rel_path=os.path.join("tasks", tid),
                    size_bytes=size,
                    file_count=n,
                    reason="无报告、无产物结果、outputs 中也无对应目录（任务未跑出结果）",
                    risk="low",
                    detail={"files": files[:12], "file_total": len(files)},
                )
            )

    # ---------- 4. 候选：孤儿产物（任务目录已消失） ----------
    # 只针对 pf- 前缀的任务产物；辅助目录已在 _task_ids_with_outputs 里排除。
    orphans: list[CleanItem] = []
    if os.path.isdir(outputs_root):
        for tid in sorted(out_ids):
            tpath = os.path.join(tasks_root, tid)
            if os.path.isdir(tpath):
                continue
            opath = os.path.join(outputs_root, tid)
            files = _list_files(opath)
            # 必须含结果文件才算产物；空壳目录不计入（另行处理也无意义）
            if not any("powerflow_result" in f or f.endswith("report.md") for f in files):
                continue
            n, size = _dir_stat(opath)
            orphans.append(
                CleanItem(
                    kind="orphan_outputs",
                    path=opath,
                    rel_path=os.path.join("outputs", tid),
                    size_bytes=size,
                    file_count=n,
                    reason="tasks/ 下已无同名任务目录（过程记录缺失，结果文件仍在）",
                    risk="medium",
                    detail={"files": files[:12]},
                )
            )

    # ---------- 5. 候选：旧口径产物 ----------
    # 判据：产物里没有 total_load_kw 的元件求和字段（即口径修复前生成）
    legacy: list[CleanItem] = []
    legacy_note = "总负荷口径修复前生成；潮流解本身有效，仅 total_load_kw 采用旧倒推口径"
    if os.path.isdir(outputs_root):
        for tid in sorted(out_ids):
            rp = os.path.join(outputs_root, tid, "powerflow_result.json")
            if not os.path.isfile(rp):
                continue
            try:
                with open(rp, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            # 注意字段名是 system_summary（产物 JSON 的顶层键），不是 system。
            syssum = ((data or {}).get("system_summary") or {})
            if not isinstance(syssum, dict):
                continue
            # 新口径标志：同时有 total_load_kw 与 net_load_kw。
            # 修复前的产物只有 total_load_kw（且为"源侧−网损"倒推值），没有 net_load_kw。
            has_new = (
                syssum.get("total_load_kw") is not None
                and syssum.get("net_load_kw") is not None
            )
            if has_new:
                continue
            # 连 total_load_kw 都没有的残缺产物不归此类，避免误列
            if syssum.get("total_load_kw") is None:
                continue
            opath = os.path.join(outputs_root, tid)
            n, size = _dir_stat(opath)
            legacy.append(
                CleanItem(
                    kind="legacy_outputs",
                    path=opath,
                    rel_path=os.path.join("outputs", tid),
                    size_bytes=size,
                    file_count=n,
                    reason=legacy_note,
                    risk="medium",
                    detail={"total_load_kw": syssum.get("total_load_kw")},
                )
            )

    groups = {
        "empty_session_dirs": empty_dirs,
        "failed_task_residue": failed,
        "orphan_outputs": orphans,
        "legacy_outputs": legacy,
    }

    aux_dirs = _aux_output_dirs(outputs_root)

    return {
        "generated_at": time.time(),
        "dirs": [asdict(d) for d in dirs],
        "aux_dirs": [asdict(d) for d in aux_dirs],
        "aux_bytes": sum(d.size_bytes for d in aux_dirs),
        "db": {"path": store.DB_PATH, "size_bytes": db_size},
        "total_bytes": total_bytes,
        "total_files": total_files,
        "total_human": _human(total_bytes),
        "groups": {k: [asdict(i) for i in v] for k, v in groups.items()},
        "counts": {k: len(v) for k, v in groups.items()},
        "sizes": {k: sum(i.size_bytes for i in v) for k, v in groups.items()},
        "kept": kept_reason,
        # 每类的处置建议，供 UI 直接展示
        "advice": {
            "empty_session_dirs": "可直接清理。空目录不含数据。",
            "failed_task_residue": "可清理。任务未产出任何结果。"
            "（含源文件的已自动排除；**目录结构异常**的除外——那类会被列出，"
            "因为归档是移动操作，源文件会一并保留，不必为它长期拖慢扫描）",
            "orphan_outputs": "清理前请确认：结果文件仍在，但过程记录已丢失，无法重算复现。",
            "legacy_outputs": "建议归档而非清理。潮流解有效，仅口径不同；汇报取数请用新代码重跑。",
        },
        "never_touch": [
            "chat_data/uploads/ 中的算例原件 —— 唯一来源，不可再生",
            "chat_data/chat.db —— 会话与消息数据库",
            "tasks/audit/ —— 审计日志",
            "tasks/ 中含源文件的失败任务 —— 可能是唯一工程样本"
            "（**例外**：目录结构异常的会被列为候选，归档时源文件随目录一并保留）",
        ],
    }


# ---------------------------------------------------------------- 归档


def default_archive_root(cfg: Any) -> str:
    """归档目录。

    默认放在项目根**之外**（`<项目根>/../tianshu_archive`），理由：
      * 归档是"从应用数据里拿走"的动作，放回应用目录会污染源码树、被 git 跟踪；
      * 与 outputs/tasks 同级会让"占用空间"统计把归档也算进去，自欺欺人。
    可用环境变量 `DPF_ARCHIVE_ROOT` 覆盖（例如指到大容量盘）。
    """
    env = os.environ.get("DPF_ARCHIVE_ROOT")
    if env:
        return env
    project_root = os.path.dirname(os.path.abspath(cfg.outputs_root))
    return os.path.join(os.path.dirname(project_root), "tianshu_archive")


def archive(cfg: Any, items: list[dict[str, Any]], archive_root: str | None = None) -> dict[str, Any]:
    """把指定条目归档（移动）到归档目录。

    `items` 必须是调用方显式给出的清单（通常来自 scan 后用户在 UI 上勾选的项），
    本函数**不做任何扫描**，也不接受"全部"这类模糊指令。

    每批最多处理 10 条，逐条核对；任一条失败即停止并如实报告已完成部分。
    """
    if not items:
        return {"ok": True, "moved": [], "failed": [], "message": "未指定任何条目"}

    root = archive_root or default_archive_root(cfg)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    batch_dir = os.path.join(root, stamp)
    os.makedirs(batch_dir, exist_ok=True)

    moved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for i, item in enumerate(items):
        if i >= 10:
            failed.append({"path": "(剩余条目)", "error": "单批上限 10 条，其余未处理"})
            break
        src = item.get("path") or ""
        kind = item.get("kind") or "unknown"
        if not src or not os.path.exists(src):
            failed.append({"path": src, "error": "源路径不存在"})
            continue

        # 安全检查：拒绝归档 uploads 里有内容的目录、chat.db、audit
        ap = os.path.abspath(src)
        if ap.endswith("chat.db"):
            failed.append({"path": src, "error": "拒绝归档会话数据库"})
            continue
        if os.path.basename(ap) == "audit" and os.path.basename(os.path.dirname(ap)) == "tasks":
            failed.append({"path": src, "error": "拒绝归档审计日志"})
            continue
        if os.path.basename(os.path.dirname(ap)) == "uploads" and os.listdir(ap):
            failed.append({"path": src, "error": "拒绝归档含文件的会话目录（原始来源）"})
            continue

        dst = os.path.join(batch_dir, kind, os.path.basename(ap))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.move(ap, dst)
            moved.append({"path": src, "to": dst, "kind": kind})
        except Exception as exc:  # noqa: BLE001
            failed.append({"path": src, "error": str(exc)})
            break  # 出错即停，不继续

    return {
        "ok": not failed,
        "moved": moved,
        "failed": failed,
        "batch_dir": batch_dir,
        "message": f"已归档 {len(moved)} 条；失败 {len(failed)} 条。归档位置：{batch_dir}",
    }
