# -*- coding: utf-8 -*-
"""算例上传：多选能力与同名拦截。

## 背景

「上传算例」原先只能选一个文件（`showOpenDialog` 用 `properties: ['openFile']`
且只取 `filePaths[0]`）。但一个算例常由多个 `.dss` 组成
（`Master.dss` + `Redirect` 引用的子文件），且**后端本来就是多文件接口**
（`files: list[UploadFile] = File(default=[])`，`_process_uploaded_files` 遍历全部）
—— 也就是说缺的只是前端这一层。

## 分组

* **A 前端契约** —— 主进程 / preload / 渲染层三处的形态约束。
* **C 后端同名拦截** —— 落盘按**原始文件名**，同名会静默覆盖；
  校验必须在写盘**之前**（这一组因此零副作用，不创建任何目录）。

本套件覆盖静态接口契约与真实文件依赖冻结；网络上传的端到端验证
应另起本地测试服务，检查 multipart body 的文件内容。

运行：python -m dpf_agent.tests.test_case_upload
"""
from __future__ import annotations

import io
import os
import re
import sys

_PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

# UI 与后端同属一个仓库根：本项目根 = _PROJ 的上一级（`_PROJ` 是含 .venv 的那层）。
_REPO = os.path.abspath(os.path.join(_PROJ, ".."))
_UI = os.path.join(_REPO, "tianshu_ui")

_PASS = 0
_FAIL: list[str] = []
_LINES: list[str] = []


def _flush() -> None:
    import tempfile

    try:
        io.open(
            os.path.join(tempfile.gettempdir(), "test_case_upload.txt"),
            "w",
            encoding="utf-8",
        ).write("\n".join(_LINES) + f"\n\nPASS={_PASS} FAIL={len(_FAIL)}\n")
    except OSError:
        pass


def _p(msg: str = "") -> None:
    _LINES.append(msg)
    print(msg)


def check(tag: str, ok: bool, detail: str = "") -> None:
    global _PASS
    if ok:
        _PASS += 1
    else:
        _FAIL.append(tag)
    _p("  [%s] %s%s" % ("PASS" if ok else "FAIL", tag, ("  " + detail) if detail else ""))


def section(title: str) -> None:
    _p("")
    _p("-" * 78)
    _p(title)
    _p("-" * 78)


def main() -> int:
    main_js = io.open(os.path.join(_UI, "main.js"), encoding="utf-8").read()
    preload_js = io.open(os.path.join(_UI, "preload.js"), encoding="utf-8").read()
    app_js = io.open(
        os.path.join(_UI, "renderer", "app.js"), encoding="utf-8"
    ).read()

    # ---------------------------------------------------------------- A 前端
    section("A 前端契约：多选链路（主进程 → preload → 渲染层）")

    check("A1 打开多选（multiSelections）", "'multiSelections'" in main_js)
    check(
        "A2 选择器统一返回数组（取消 = 空数组，调用方不用区分三种形态）",
        "result.canceled ? [] : result.filePaths" in main_js,
        "曾返回 filePaths[0]（单值）",
    )
    check(
        "A3 multipartRequest 按数组遍历、逐个建 part",
        "Array.isArray(filePaths)" in main_js and "for (const one of list)" in main_js,
    )
    check(
        "A4 每个 part 用自己的文件名（不是共用第一个）",
        "const name = path.basename(String(one))" in main_js,
        "共用文件名会让服务端看到的全是一个名字",
    )
    check(
        "A5 字段名是 files（复数）—— 与后端 list[UploadFile] 对齐",
        'name="files"' in main_js,
        "后端形参就叫 files，改成单数会 422",
    )
    check(
        "A6 单个文件读不出来时明确报错，不静默少传",
        "读取文件失败" in main_js and "reject(" in main_js,
    )
    check(
        "A7 preload 透传 filePaths（不再传 filename 参数）",
        "filePaths" in preload_js and "filename" not in preload_js,
    )
    check(
        "A8 渲染层把选择放在 state 上（这样 sendMessage 能清空它）",
        "state.selectedFiles" in app_js,
    )
    check(
        "A9 没有 selectedFile（单数）残留",
        not re.search(r"\bselectedFile\b", app_js),
    )
    check("A10 多选时提示显示数量与文件名", "个文件：" in app_js)
    check(
        "A11 提交后清空选择（否则会重复上传同一批文件）",
        "state.selectedFiles = [];" in app_js,
    )
    check(
        "A12 前端拦住同名文件（后端按原始名落盘，同名会互相覆盖）",
        "所选文件中有同名文件" in app_js and "请分开上传" in app_js,
    )

    # ---------------------------------------------------------------- C 后端
    section("C 后端：同名文件整体拒绝（校验在写盘之前 → 本组零副作用）")

    from fastapi import HTTPException  # noqa: E402
    from starlette.datastructures import UploadFile as _SUF  # noqa: E402

    from dpf_agent.web import chat_api as capi  # noqa: E402

    def _uf(name: str):
        return _SUF(
            file=io.BytesIO(b"New Circuit.T basekv=12.47\n"), filename=name
        )

    probe_sess = "probe-dup-not-created"
    raised = None
    try:
        capi._process_uploaded_files(probe_sess, [_uf("Master.dss"), _uf("master.DSS")])
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    check(
        "C1 同名被拒绝（大小写不同也算 —— Windows 文件系统不区分）",
        isinstance(raised, HTTPException) and "同名" in str(raised.detail),
        str(raised)[:150],
    )
    check(
        "C2 提示给出可操作做法（分开上传 / 重命名）",
        "分开上传" in str(getattr(raised, "detail", "")),
        str(getattr(raised, "detail", ""))[:130],
    )
    uploads_root = getattr(capi.store, "UPLOADS_DIR", "")
    check(
        "C3 拒绝发生在**写盘之前**（会话上传目录未被创建）",
        not (uploads_root and os.path.isdir(os.path.join(uploads_root, probe_sess))),
    )
    # 用模块自己的 __file__，避免手工拼路径时数错层级
    src_api = io.open(capi.__file__, encoding="utf-8").read()
    i_dup = src_api.index("同一次上传中有同名文件")
    i_dir = src_api.index(
        "upload_dir = store.ensure_upload_dir(session_id)",
        src_api.index("def _process_uploaded_files"),
    )
    check("C4 源码里校验在处理之前（先整体校验，不留部分写入）", i_dup < i_dir)
    check("C5 后端接口仍是 list[UploadFile]（多文件是它的原生形态）",
        "files: list[UploadFile] = File(default=[])" in src_api
        or "files: list[UploadFile] = File(...)" in src_api,
    )

    # ---------------------------------------------------------------- D 多算例收集
    section("D 多算例收集：一个附件 = 一个独立算例（子文件不算）")

    import tempfile  # noqa: E402
    from dpf_agent.web import chat_store as _store  # noqa: E402

    d1 = tempfile.mkdtemp(prefix="upload_refs_")
    old_uploads = _store.UPLOADS_DIR
    try:
        # 让 ensure_upload_dir 落在临时目录里（不改项目数据）
        _store.UPLOADS_DIR = os.path.join(d1, "uploads")
        os.makedirs(_store.UPLOADS_DIR, exist_ok=True)
        sess = "s-probe"

        def mk_case(name: str, body: str = "New Circuit.T basekv=12.47\n") -> str:
            """造一个算例目录（含 Master.dss），返回目录路径。"""
            d = os.path.join(_store.UPLOADS_DIR, sess, name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "Master.dss"), "w", encoding="ascii") as fh:
                fh.write(body)
            return d

        a_dir = mk_case("caseA")
        b_dir = mk_case("caseB")

        # D1：两个目录型附件 → 两个算例
        refs = capi._extract_case_refs(sess, [
            {"filename": "caseA.xlsx", "path": a_dir},
            {"filename": "caseB.xlsx", "path": b_dir},
        ])
        check("D1 两个目录型附件 → 收到 2 个算例", len(refs) == 2, str(len(refs)))

        # D2：顺序与附件顺序一致
        check("D2 顺序与附件顺序一致（用户选的第一个排在前）",
              os.path.basename(os.path.dirname(refs[0])) == "caseA"
              and os.path.basename(os.path.dirname(refs[1])) == "caseB",
              str([os.path.basename(os.path.dirname(r)) for r in refs]))

        # D3：同一算例被两个附件指向（目录 + 其中的 Master.dss）→ 去重为 1
        refs3 = capi._extract_case_refs(sess, [
            {"filename": "caseA.xlsx", "path": a_dir},
            {"filename": "Master.dss",
             "path": os.path.join(a_dir, "Master.dss")},
        ])
        check("D3 同一算例被两个附件指向时去重", len(refs3) == 1, str(len(refs3)))

        # D4：散落的子文件不算独立算例（Master.dss + LineCodes.dss = 1 个算例）
        loose = os.path.join(_store.UPLOADS_DIR, sess, "loose")
        os.makedirs(loose, exist_ok=True)
        m_path = os.path.join(loose, "Master.dss")
        sub_path = os.path.join(loose, "LineCodes.dss")
        for p in (m_path, sub_path):
            with open(p, "w", encoding="ascii") as fh:
                fh.write("New Circuit.T basekv=12.47\n")
        refs4 = capi._extract_case_refs(sess, [
            {"filename": "Master.dss", "path": m_path},
            {"filename": "LineCodes.dss", "path": sub_path},
        ])
        check("D4 子文件（非 Master.dss）不算独立算例 → 1 个",
              len(refs4) == 1, str(len(refs4)))

        # D5：`_extract_case_ref` 只取第一个 —— 这正是"传两个只算一个"的根源
        first = capi._extract_case_ref(sess, [
            {"filename": "caseA.xlsx", "path": a_dir},
            {"filename": "caseB.xlsx", "path": b_dir},
        ])
        check("D5 旧的单值函数仍只返回第一个（批量必须用 refs 版本）",
              first is not None
              and os.path.basename(os.path.dirname(first)) == "caseA",
              str(first))

        # D6：目录名作为展示名（Master.dss 没有信息量）
        check("D6 展示名取算例目录名",
              capi._case_display_name(refs[0]) == "caseA",
              capi._case_display_name(refs[0]))
    finally:
        _store.UPLOADS_DIR = old_uploads

    # ---------------------------------------------------------------- E 批量契约
    section("E 批量契约：异步持久队列 / 单点执行体 / 每项结果")

    check("E1 存在批量分支，且判据是「可独立计算的算例多于一个」",
          "len(_cands) > 1" in src_api and "_run_case_batch(" in src_api)
    check("E2 批量分支要求「用户没有点名算例」（点名优先，不批量）",
          "if attachments and not named_case:" in src_api)
    # Use actual SQLite/ASGI/worker behavior instead of pinning the old synchronous
    # loop's spelling. These tests deliberately do not call an engine or model.
    import unittest
    from dpf_agent.tests.test_batch_scheduler import BatchSchedulerTests
    batch_behavior = {}
    for method in ("test_fifo_failure_continues_and_retry_only_failed_keeps_old_attempt",
                   "test_real_multipart_returns_202_before_runner_finishes_and_snapshots_inputs"):
        result = unittest.TestResult()
        BatchSchedulerTests(method).run(result)
        batch_behavior[method] = result.wasSuccessful()
        if not result.wasSuccessful():
            for _test, problem in result.errors + result.failures:
                _p(problem)
    fifo_ok = batch_behavior["test_fifo_failure_continues_and_retry_only_failed_keeps_old_attempt"]
    ack_ok = batch_behavior["test_real_multipart_returns_202_before_runner_finishes_and_snapshots_inputs"]
    check("E3 单worker按FIFO执行，最大同时执行数为1（真实SQLite/受控runner）", fifo_ok)
    check("E4 单项失败继续其他项，重试只执行失败项并保留旧attempt", fifo_ok)
    check("E5 ★ 单算例与批量**共用同一个执行体**（防「同一逻辑写两遍」漂移）",
          src_api.count("_run_case_into_message(") >= 2
          and src_api.count("def _run_case_into_message(") == 1,
          "调用 %d 次 / 定义 1 处" % (src_api.count("_run_case_into_message(") - 1))
    check("E6 每项具有独立assistant消息，任务号/真实终态与本会话绑定", ack_ok)
    check("E7 multipart立即202返回batch，轮询与会话GET能恢复进度", ack_ok)

    # ---------------------------------------------------------------- G 跨盘守卫
    section("G 路径守卫：跨盘必须按「不在范围内」处理，而不是抛异常")

    from dpf_agent.domain.case.case_loader import _is_within  # noqa: E402

    same = os.path.join(_REPO, "dpf_agent")
    check("G1 同盘且在内 → True", _is_within(os.path.join(same, "chat_api.py"), same))
    check("G2 同盘但在外 → False", not _is_within(os.path.join(_REPO, "nope.txt"), same))
    raised = None
    cross = None
    try:
        # 手工构造跨盘对：只要两个盘符不同就必然触发 commonpath 的 ValueError
        cross = _is_within(r"D:\probe\case\Master.dss", r"C:\probe\root")
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    check("G3 ★ 跨盘**不抛异常**（曾抛 ValueError: Paths don't have the same drive，"
          "并冒泡成 ENGINE_ERROR）", raised is None, repr(raised))
    check("G4 跨盘返回 False（视为不在该根内）", cross is False, str(cross))
    src_loader = io.open(
        os.path.join(_REPO, "dpf_agent", "dpf_agent", "domain", "case",
                     "case_loader.py"),
        encoding="utf-8",
    ).read()
    check("G5 路径守卫不再直接用 os.path.commonpath（防回归）",
          "os.path.commonpath(" not in src_loader.replace(
              "return os.path.commonpath([a, b]) == b", ""),
          "只允许出现在 _is_within 内部")

    # ---- H. 合成算例的重定义容错与 BusCoords 依赖冻结 ----
    # 直接构造能触发回归的最小语法，不依赖第三方算例或开发机目录。
    _p("")
    _p("=" * 78)
    _p("H. 合成算例装载（重定义容错 + BusCoords 依赖）")
    _p("=" * 78)
    import tempfile as _tf  # noqa: E402
    import shutil as _sh  # noqa: E402
    from dpf_agent.domain.case.case_loader import (  # noqa: E402
        load_case_bundle, scan_dss_references, _scan_duplicate_element_names,
    )

    hd = _tf.mkdtemp(prefix="synthetic_case_")
    try:
        # H1：BusCoords 必须被识别为外部文件依赖
        m1 = os.path.join(hd, "A.dss")
        io.open(m1, "w", encoding="utf-8").write(
            "Redirect sub.dss\nBusCoords BusXY.csv\n! 注释里的 BusCoords B.csv 不算\n")
        refs = [r.lower() for r in scan_dss_references(m1)]
        check("H1 BusCoords 的 CSV 被当作依赖（此前漏读）",
              "busxy.csv" in refs, str(refs))
        check("H1b Redirect 仍被识别（未回归）", "sub.dss" in refs, str(refs))
        check("H1c 注释行里的 BusCoords 不误判",
              "b.csv" not in refs, str(refs))

        # H2：同一元件合法重定义 → 记录下来，但**不**拒绝
        m2 = os.path.join(hd, "B.dss")
        io.open(m2, "w", encoding="utf-8").write(
            "New Linecode.synthetic nphases=3 R1=0.1 X1=0.2\n"
            "! Synthetic redefinition with a changed resistance\n"
            "New Linecode.synthetic nphases=3 R1=0.2 X1=0.2 Units=km\n")
        seen: set[str] = set()
        dup = _scan_duplicate_element_names(m2, seen)
        check("H2 重定义被检出并记录（供追溯）",
              dup == ["linecode.synthetic"], str(dup))
        src_loader2 = io.open(
            os.path.join(_REPO, "dpf_agent", "dpf_agent", "domain", "case",
                         "case_loader.py"), encoding="utf-8").read()
        check("H2b 重定义不再抛「检测到重复 OpenDSS 元件名称」（防回归）",
              "检测到重复 OpenDSS 元件名称" not in src_loader2,
              "OpenDSS 的 New 对已存在名字是重定义（后者覆盖前者），合法")
        check("H2c 重定义留痕字段存在", "redefined_element_names" in src_loader2)

        # H3：真实装载一个原创最小依赖图，并检查冻结副本独立于源文件。
        source = os.path.join(hd, "source")
        os.makedirs(os.path.join(source, "sub"))
        fixtures = {
            "Master.dss": b"Clear\nNew Circuit.synthetic basekv=20\nRedirect sub/Lines.dss\nBusCoords BusXY.csv\n",
            "sub/Lines.dss": b"New Line.L1 bus1=sourcebus bus2=b2 phases=3 r1=0.1 x1=0.2 length=1 units=km\n",
            "BusXY.csv": b"sourcebus,0,0\nb2,1,0\n",
        }
        for name, payload in fixtures.items():
            with open(os.path.join(source, name), "wb") as stream:
                stream.write(payload)
        task = os.path.join(hd, "task")
        case = load_case_bundle(os.path.join(source, "Master.dss"), task,
                                os.path.join(hd, "cases"))
        frozen = {entry.path for entry in case.dependency_manifest.entries}
        check("H3 Redirect 和 BusCoords 依赖均进入任务清单",
              frozen == set(fixtures), str(sorted(frozen)))
        check("H3b 所有冻结文件的字节与源一致",
              all(io.open(os.path.join(task, "input", name), "rb").read() == payload
                  for name, payload in fixtures.items()))
        with open(os.path.join(source, "BusXY.csv"), "wb") as stream:
            stream.write(b"source modified after submission\n")
        check("H3c 修改源 CSV 不改变已提交的冻结副本",
              io.open(os.path.join(task, "input", "BusXY.csv"), "rb").read()
              == fixtures["BusXY.csv"])
    finally:
        _sh.rmtree(hd, ignore_errors=True)

    _p("")
    _p("=" * 78)
    _p(f"RESULT: PASS={_PASS} FAIL={len(_FAIL)}")
    if _FAIL:
        for f in _FAIL:
            _p(f"  - {f}")
    _p("=" * 78)
    _flush()
    print(f"PASS={_PASS} FAIL={len(_FAIL)}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
