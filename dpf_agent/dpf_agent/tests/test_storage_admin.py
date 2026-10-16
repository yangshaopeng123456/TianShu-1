# -*- coding: utf-8 -*-
"""存储清理守卫测试：确保「不可清理的东西永远不会被列出或归档」。

测试对象：`dpf_agent/web/storage_admin.py`

分组：
  A 扫描安全：uploads 原件 / chat.db / audit 绝不出现在候选里
  B 分类正确：旧口径产物判据命中且不误报；辅助目录不被当任务产物
  C 归档安全：四类拒绝规则 + 单批上限 + 出错即停
  D 归档可回滚：移动到位、原位置消失、可原样恢复
  E 静态断言：源码里不得出现直接删除（rmtree/remove/unlink）残留数据的写法
  F 结构异常目录：深嵌套能被识别 / 进候选 / 归档可行，且不误伤正常的保守保留

运行：python -m dpf_agent.tests.test_storage_admin
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

# ---------------------------------------------------------------- 轻量断言框架

_PASS = 0
_FAIL: list[str] = []
_OUT_DIR = os.environ.get("DPF_TEST_OUT_DIR") or tempfile.gettempdir()
_LINES: list[str] = []


def _flush() -> None:
    """断言即落盘，进程被杀也能看到进度。"""
    try:
        with open(os.path.join(_OUT_DIR, "_stg_guard.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(_LINES) + f"\n\nPASS={_PASS} FAIL={len(_FAIL)}\n")
    except OSError:
        pass


def check(cond: bool, label: str, detail: str = "") -> None:
    global _PASS
    if cond:
        _PASS += 1
        _LINES.append(f"[PASS] {label}")
    else:
        _FAIL.append(label)
        _LINES.append(f"[FAIL] {label}" + (f"  <- {detail}" if detail else ""))
    _flush()


def section(t: str) -> None:
    _LINES.append("")
    _LINES.append(f"===== {t} =====")
    _flush()


# ---------------------------------------------------------------- 主体


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    from dpf_agent.config import AgentConfig
    from dpf_agent.web import chat_store as store
    from dpf_agent.web import storage_admin as sa

    cfg = AgentConfig()

    # ---------------- A 扫描安全 ----------------
    section("A 扫描安全：不可清理项不得出现在候选里")

    d = sa.scan(cfg)

    # A1 所有候选路径集合
    all_items = []
    for items in d["groups"].values():
        all_items.extend(items)
    all_paths = [os.path.abspath(i["path"]) for i in all_items]

    # A2 chat.db 不在候选里
    check(
        os.path.abspath(store.DB_PATH) not in all_paths,
        "A2 chat.db 不在候选清单里",
        store.DB_PATH,
    )

    # A3 audit 不在候选里
    audit = os.path.abspath(os.path.join(cfg.workspace_root, "audit"))
    check(audit not in all_paths, "A3 tasks/audit 不在候选清单里", audit)

    # A4 含文件的 uploads 会话目录不在候选里
    nonempty = []
    for n in os.listdir(sa._uploads_dir()):
        p = os.path.join(sa._uploads_dir(), n)
        if os.path.isdir(p) and os.listdir(p):
            nonempty.append(os.path.abspath(p))
    hit = [p for p in all_paths if p in nonempty]
    check(
        not hit,
        "A4 含文件的 uploads 会话目录不在候选清单里",
        f"命中 {len(hit)} 个",
    )

    # A5 outputs 下的辅助目录（非 pf-）不在候选里
    aux = [os.path.abspath(x["path"]) for x in d["aux_dirs"]]
    hit2 = [p for p in all_paths if p in aux]
    check(not hit2, "A5 outputs 辅助目录不在候选清单里", f"命中 {len(hit2)} 个")

    # A6 辅助目录确实被单独列出（不是被丢掉）
    check(len(aux) > 0, "A6 aux_dirs 非空（辅助目录被单独统计）", f"len={len(aux)}")

    # A7 never_touch 文案非空
    check(bool(d.get("never_touch")), "A7 never_touch 声明非空")

    # A8 advice 每类都有说明
    check(
        all(bool(v) for v in d["advice"].values()),
        "A8 advice 每类都有处置说明",
    )

    # A9 kept（保守保留）不含候选里的东西
    kept_paths = set(d.get("kept", {}).keys())
    overlap = kept_paths & {os.path.basename(p) for p in all_paths}
    check(not overlap, "A9 保守保留项与候选清单不重叠", str(overlap))

    # ---------------- B 分类正确 ----------------
    section("B 分类正确")

    # B1 orphan_outputs 只含 pf- 前缀
    bad_orphan = [i["rel_path"] for i in d["groups"]["orphan_outputs"]
                  if not os.path.basename(i["path"]).startswith("pf-")]
    check(not bad_orphan, "B1 orphan_outputs 只含 pf- 任务产物", str(bad_orphan[:3]))

    # B2 orphan_outputs 必须含结果文件
    bad2 = []
    for i in d["groups"]["orphan_outputs"]:
        files = i.get("detail", {}).get("files", [])
        if not any("powerflow_result" in f or f.endswith("report.md") for f in files):
            bad2.append(i["rel_path"])
    check(not bad2, "B2 orphan_outputs 均含结果文件", str(bad2[:3]))

    # B3 legacy 与 orphan 不重叠
    lo = {i["path"] for i in d["groups"]["legacy_outputs"]}
    oo = {i["path"] for i in d["groups"]["orphan_outputs"]}
    check(not (lo & oo), "B3 legacy 与 orphan 不重叠", str(list(lo & oo)[:3]))

    # B4 legacy 判据：产物确实缺 net_load_kw
    import json

    bad3 = []
    for i in d["groups"]["legacy_outputs"][:40]:
        rp = os.path.join(i["path"], "powerflow_result.json")
        try:
            with open(rp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        ss = data.get("system_summary") or {}
        if ss.get("net_load_kw") is not None:
            bad3.append(i["rel_path"])
    check(not bad3, "B4 legacy 条目确实不含 net_load_kw（判据正确）", str(bad3[:3]))

    # B5 反向：含 net_load_kw 的新产物不得被判为 legacy
    new_ok = []
    for i in d["groups"]["legacy_outputs"]:
        rp = os.path.join(i["path"], "powerflow_result.json")
        try:
            with open(rp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        ss = data.get("system_summary") or {}
        if ss.get("net_load_kw") is not None:
            new_ok.append(i["rel_path"])
    check(not new_ok, "B5 新口径产物未被误判为 legacy", str(new_ok[:3]))

    # B6 failed_task_residue 不含源文件
    ext = (".dss", ".m", ".json", ".csv", ".xlsx")
    bad4 = []
    for i in d["groups"]["failed_task_residue"]:
        files = i.get("detail", {}).get("files", [])
        if any(f.lower().endswith(ext) for f in files):
            bad4.append(i["rel_path"])
    check(not bad4, "B6 failed_task_residue 不含源文件", str(bad4[:3]))

    # ---------------- C 归档安全 ----------------
    section("C 归档安全：拒绝规则")

    # C1 chat.db 被拒
    r = sa.archive(cfg, [{"kind": "x", "path": store.DB_PATH}])
    check(bool(r["failed"]) and os.path.isfile(store.DB_PATH),
          "C1 归档 chat.db 被拒且文件仍在", str(r["failed"][:1]))

    # C2 audit 被拒
    if os.path.isdir(audit):
        r = sa.archive(cfg, [{"kind": "x", "path": audit}])
        check(bool(r["failed"]) and os.path.isdir(audit),
              "C2 归档 tasks/audit 被拒且目录仍在", str(r["failed"][:1]))

    # C3 含文件的 uploads 目录被拒
    if nonempty:
        r = sa.archive(cfg, [{"kind": "x", "path": nonempty[0]}])
        check(bool(r["failed"]) and os.path.isdir(nonempty[0]),
              "C3 归档含文件的 uploads 目录被拒", str(r["failed"][:1]))

    # C4 不存在的路径
    r = sa.archive(cfg, [{"kind": "x", "path": os.path.join(tempfile.gettempdir(), "__nope_zzz__")}])
    check(bool(r["failed"]), "C4 归档不存在的路径被拒", str(r["failed"][:1]))

    # C5 空清单
    r = sa.archive(cfg, [])
    check(r.get("ok") and not r["moved"], "C5 空清单不做事且不报错", str(r))

    # C6 单批上限 10：提交 15 条真实存在的小目录，应只处理 10 条并报上限
    with tempfile.TemporaryDirectory() as tmp:
        dirs = []
        for i in range(15):
            p = os.path.join(tmp, f"d{i:02d}")
            os.makedirs(p)
            with open(os.path.join(p, "f.txt"), "w", encoding="utf-8") as fh:
                fh.write("x")
            dirs.append(p)
        arch = os.path.join(tmp, "_a")
        r = sa.archive(cfg, [{"kind": "legacy_outputs", "path": p} for p in dirs], arch)
        n_moved = len(r["moved"])
        has_cap = any("上限" in f["error"] for f in r["failed"])
        check(
            n_moved == 10 and has_cap,
            "C6 单批上限生效（15 条只移 10 条，并报上限）",
            f"moved={n_moved} cap_reported={has_cap}",
        )
        # 剩下 5 条必须仍在原位（没被顺手处理）
        left = [p for p in dirs[10:] if os.path.isdir(p)]
        check(len(left) == 5, "C6b 超出上限的 5 条未被移动", f"left={len(left)}")

    # ---------------- D 归档可回滚 ----------------
    section("D 归档可回滚")

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "fake_output")
        os.makedirs(src)
        with open(os.path.join(src, "powerflow_result.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        with open(os.path.join(src, "report.md"), "w", encoding="utf-8") as fh:
            fh.write("# x")

        arch = os.path.join(tmp, "_arch")
        r = sa.archive(cfg, [{"kind": "legacy_outputs", "path": src}], arch)
        check(r["ok"] and len(r["moved"]) == 1, "D1 归档成功", str(r["failed"]))
        check(not os.path.isdir(src), "D2 原位置已消失")
        dst = r["moved"][0]["to"]
        check(os.path.isdir(dst), "D3 归档目标存在")
        check(
            os.path.isfile(os.path.join(dst, "powerflow_result.json")),
            "D4 归档内容完整",
        )

        # 回滚
        shutil.move(dst, src)
        check(os.path.isdir(src) and os.path.isfile(os.path.join(src, "report.md")),
              "D5 可原样回滚恢复")

    # ---------------- E 静态断言 ----------------
    section("E 静态断言：源码中不得有直接删除残留数据的写法")

    src_file = sa.__file__
    with open(src_file, "r", encoding="utf-8") as fh:
        code = fh.read()

    check("shutil.move" in code, "E1 使用 shutil.move 归档（非删除）")
    check("shutil.rmtree" not in code, "E2 源码中无 shutil.rmtree（不得直接删目录）")
    check("os.remove" not in code, "E3 源码中无 os.remove")
    check("os.unlink" not in code, "E4 源码中无 os.unlink")

    # E5 confirm 语义在 API 层（api.py），不在 storage_admin.py
    from dpf_agent.web import api as api_mod

    with open(api_mod.__file__, "r", encoding="utf-8") as fh:
        api_code = fh.read()
    check("confirm" in api_code and "需显式确认" in api_code,
          "E5 归档接口在 API 层要求显式 confirm")

    # E6 archive() 签名不接受"全部/模糊"指令：必须是显式 items 列表
    import inspect

    sig = inspect.signature(sa.archive)
    check("items" in sig.parameters, "E6 archive() 必须接收显式 items 清单")
    check("all" not in sig.parameters and "everything" not in sig.parameters,
          "E6b archive() 无「全部清理」类模糊参数")

    # ---------------- F 病态目录 ----------------
    section("F 结构异常目录：能被识别 / 可处置 / 不误伤正常保留")

    # 为什么单列一组：CLI 适配器的镜像递归缺陷曾产出 4247 层嵌套目录。这类目录
    # ① 拖慢扫描（实测 4s → 268s）；② 因含 input/Master.dss 而落进「含源文件 →
    # 保守保留」，永远列不进候选。用**合成目录**测，不依赖盘上恰好有这么一个。
    # ⚠️ 沿用本项目的约定：**测试不做删除**，临时目录留在系统 temp 下由用户回收
    # （本项目所在环境对批量删除敏感，加 rmtree 会触发文件安全策略拦截）。
    tmp2 = tempfile.mkdtemp(prefix="storage_patho_")
    f_root = os.path.join(tmp2, "probe")
    # 正常形状：input/cli_work/case（3 层）
    ok_dir = os.path.join(f_root, "normal", "input", "cli_work", "case")
    os.makedirs(ok_dir, exist_ok=True)
    with open(os.path.join(ok_dir, "Master.dss"), "w", encoding="ascii") as fh:
        fh.write("New Circuit.N basekv=12.47\n")
    check(sa.probe_pathological(os.path.join(f_root, "normal")) == "",
          "F1 正常任务目录不被误判为病态")

    # 病态形状：cli_work/case 反复嵌套 30 层（阈值 25）
    bad_dir = os.path.join(f_root, "pathological")
    cur = bad_dir
    os.makedirs(cur, exist_ok=True)
    for _ in range(30):
        cur = os.path.join(cur, "cli_work", "case")
        os.makedirs(cur, exist_ok=True)
        with open(os.path.join(cur, "Master.dss"), "w", encoding="ascii") as fh:
            fh.write("New Circuit.D basekv=12.47\n")
    t_probe = time.time()
    why = sa.probe_pathological(bad_dir)
    probe_cost = time.time() - t_probe
    check(bool(why), "F2 深嵌套目录被识别为病态", str(why))
    check(probe_cost < 2.0, "F3 探测是早退的（30 层未走完）", "%.3fs" % probe_cost)
    fc, _sz = sa._dir_stat(bad_dir, max_depth=sa._MAX_TASK_DEPTH)
    check(fc < 30, "F4 限深统计给出的是下限（不会走完深目录）",
          "看到 %d 层 / 实际 30 层" % fc)

    # scan 的判据（合成工作区，不碰真实数据）
    ws = os.path.join(tmp2, "ws")
    outs = os.path.join(tmp2, "out")
    os.makedirs(outs, exist_ok=True)
    keep_id = "pf-20200101-000000-keepme"
    kd = os.path.join(ws, keep_id, "input")
    os.makedirs(kd, exist_ok=True)
    with open(os.path.join(kd, "Master.dss"), "w", encoding="ascii") as fh:
        fh.write("New Circuit.K basekv=12.47\n")
    bad_id = "pf-20200101-000000-patho"
    bd = os.path.join(ws, bad_id, "input")
    os.makedirs(bd, exist_ok=True)
    cur = bd
    for _ in range(30):
        cur = os.path.join(cur, "cli_work", "case")
        os.makedirs(cur, exist_ok=True)
        with open(os.path.join(cur, "Master.dss"), "w", encoding="ascii") as fh:
            fh.write("New Circuit.D basekv=12.47\n")

    fcfg = AgentConfig(workspace_root=ws, outputs_root=outs)
    d2 = sa.scan(fcfg)
    failed_ids = {os.path.basename(i["path"])
                  for i in d2["groups"]["failed_task_residue"]}
    check(bad_id in failed_ids,
          "F5 结构异常的任务进入候选（不再被无条件保留）", str(sorted(failed_ids)))
    check(bad_id not in d2.get("kept", {}),
          "F6 结构异常的任务不在 kept 里")
    check(keep_id in d2.get("kept", {}),
          "F7 正常形状的含源文件任务仍被保守保留（保护未误伤）",
          str(sorted(d2.get("kept", {}))))
    bad_item = next((i for i in d2["groups"]["failed_task_residue"]
                     if os.path.basename(i["path"]) == bad_id), None)
    check(bad_item is not None and bad_item.get("risk") == "medium",
          "F8 结构异常项标 medium 风险（含源文件，需人看一眼）",
          str(bad_item and bad_item.get("risk")))
    check(bad_item is not None and bool((bad_item.get("detail") or {}).get("pathological")),
          "F9 detail 里带上病态原因（便于 UI 说明）")

    # 归档机制：move 是 rename 语义，与嵌套深度无关
    dst = os.path.join(tmp2, "archive")
    os.makedirs(dst, exist_ok=True)
    t_move = time.time()
    move_ok = True
    try:
        shutil.move(bad_dir, os.path.join(dst, "pathological"))
    except BaseException:  # noqa: BLE001
        move_ok = False
    move_cost = time.time() - t_move
    check(move_ok, "F10 深目录可被归档（move 不是逐文件拷贝）")
    check(move_ok and move_cost < 5.0, "F11 归档耗时与嵌套深度无关",
          "%.3fs" % move_cost)

    # ---------------- 汇总 ----------------
    _LINES.append("")
    _LINES.append(f"RESULT: PASS={_PASS} FAIL={len(_FAIL)}")
    if _FAIL:
        for f in _FAIL:
            _LINES.append(f"  - {f}")
    _flush()

    print(f"PASS={_PASS} FAIL={len(_FAIL)}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
