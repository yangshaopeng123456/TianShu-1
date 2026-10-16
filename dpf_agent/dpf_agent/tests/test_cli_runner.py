# -*- coding: utf-8 -*-
"""CLI 驱动层（`engines/cli_runner.py`）：白名单 / 版本解析 / 编码 / 镜像 / 产物查找 / 契约。

## 为什么这些断言值得锁住

`OpenDSScmd.exe` 是**官方**的独立可执行文件，但它的行为很糙，踩过的每个坑都
不是直觉能推出来的：

* 直接把算例文件名当参数传 → rc=301 且**不编译**（必须用 stdin 喂命令流）；
* `export` 的产物落在**算例文件所在目录**，不是 cwd → 不镜像就会往用户算例目录写文件；
* `export meters` / `faultstudy` 触发 **Access Violation** → 必须有命令白名单；
* `rc` 不可靠（非 0 也可能产出全部有效 CSV）→ 判定只能看产物；
* 用 PIPE + `communicate()` 时 kill 后仍要等子进程关闭管道（实测 3s 超时 → 18s 才返回）
  → 输出必须落文件。

**本套件不碰引擎**：全部是纯静态检查与纯文件操作。
真实 CLI 的进程、产物和数值精度需要另做原生引擎验收。

**零删除动作**：本项目所在环境对批量删除敏感（实测会触发文件安全策略拦截），
所以本套件不做任何清理，临时目录落在系统 temp 下由用户自行回收。
"""

from __future__ import annotations

import inspect
import io
import os
import shutil
import sys
import tempfile

_PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from dpf_agent.engines.cli_runner import (  # noqa: E402
    BANNED_EXPORTS,
    MISSING_EXPORTS,
    SAFE_EXPORTS,
    CliError,
    CliRunResult,
    _encode_commands,
    build_export_commands,
    discover_cli_path,
    mirror_case,
    parse_banner_version,
    run_cli_commands,
)

_PASS = 0
_FAIL = 0
_LINES: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        _LINES.append("[PASS] %s%s" % (tag, ("  " + detail) if detail else ""))
        print("  [PASS] %s%s" % (tag, ("  " + detail) if detail else ""))
    else:
        _FAIL += 1
        _LINES.append("[FAIL] %s  <== %s" % (tag, detail))
        print("  [FAIL] %s  <== %s" % (tag, detail))


def _print(msg: str = "") -> None:
    _LINES.append(msg)
    print(msg)


def main() -> int:
    _print("=" * 78)
    _print("CLI 驱动层：白名单 / 版本 / 编码 / 镜像 / 产物查找 / 契约")
    _print("=" * 78)

    # ------------------------------------------------- A. exe 定位与白名单
    _print()
    _print("-" * 78)
    _print("A. exe 定位与命令白名单")
    _print("-" * 78)
    exe, why = discover_cli_path()
    _print("  定位结果：%s" % (exe or "<未找到>"))
    _print("  依据：%s" % why)
    if exe:
        check("A1 定位到的可执行文件确实存在且名为 OpenDSScmd.exe",
              os.path.isfile(exe) and exe.lower().endswith("opendsscmd.exe"),
              str(exe))
        check("A2 找到时说明定位依据（可追溯）", bool(why), why)
    else:
        # 本机没装 OpenDSS 时不该让套件红：定位是"环境事实"，不是代码正确性
        _print("  [SKIP] 本机未安装 OpenDSScmd.exe，跳过 A1")
        check("A2 未找到时给出可操作提示", "DPF_OPENDSS_CLI" in why, why)

    cmds = build_export_commands()
    check("A3 默认导出序列非空且都带 export 前缀",
          bool(cmds) and all(c.startswith("export ") for c in cmds),
          "%d 条" % len(cmds))
    check("A4 默认序列覆盖适配器必需的四类",
          all(f"export {k}" in cmds for k in ("summary", "voltages", "powers", "loads")),
          str(cmds))
    check("A5 白名单外的名字被过滤",
          build_export_commands(["summary", "not_a_real_export"]) == ["export summary"],
          str(build_export_commands(["summary", "not_a_real_export"])))
    check("A6 危险命令被跳过（不冒险触发 Access Violation）",
          build_export_commands(["meters", "faultstudy", "summary"]) == ["export summary"],
          "危险清单：%s" % sorted(BANNED_EXPORTS))
    check("A7 不存在的子命令被跳过（而不是让引擎报错再排查）",
          build_export_commands(["transformers", "elementcurrents", "summary"])
          == ["export summary"],
          "不存在清单：%s" % sorted(MISSING_EXPORTS))
    check("A8 三类清单互不重叠",
          not (set(SAFE_EXPORTS) & set(BANNED_EXPORTS))
          and not (set(SAFE_EXPORTS) & set(MISSING_EXPORTS)),
          "safe=%d banned=%d missing=%d"
          % (len(SAFE_EXPORTS), len(BANNED_EXPORTS), len(MISSING_EXPORTS)))
    check("A9 白名单里不含危险命令",
          not any(k in SAFE_EXPORTS for k in BANNED_EXPORTS))

    # ------------------------------------------------- B. banner 与编码
    _print()
    _print("-" * 78)
    _print("B. banner 版本解析与命令编码")
    _print("-" * 78)
    real = ("**********************EPRI OpenDSS simulator**********************\n"
            "* OpenDSS console Version 11.0.0.1 (64-bit build) - Charlottesville *\n"
            "*Copyright (c) 2008-2021, Electric Power Research Institute, Inc.*\n")
    got = parse_banner_version(real)
    _print("  真实 banner 解析结果：%r" % got)
    check("B1 真实 banner 解析出版本号", got == "11.0.0.1 (64-bit build)", repr(got))
    check("B2 版本号不含版权行内容", "Copyright" not in got, repr(got))
    check("B3 大小写不敏感",
          parse_banner_version("* opendss console VERSION 9.9.9 *") == "9.9.9")
    check("B4 无 banner 时返回空串（降级而非抛错）",
          parse_banner_version("nothing here") == "")
    check("B5 多行里取第一条含 version 的行",
          parse_banner_version("junk\n* OpenDSS Version 1.2.3 *\njunk") == "1.2.3",
          repr(parse_banner_version("junk\n* OpenDSS Version 1.2.3 *\njunk")))

    check("B6 ASCII 命令原样编码", _encode_commands("solve") == b"solve")
    cn = _encode_commands('compile "D:\\中文目录\\算例.dss"')
    check("B7 中文路径退化为 ANSI 代码页而不是抛异常", len(cn) > 20, "%d 字节" % len(cn))
    check("B8 中文确实经过了非 ASCII 编码",
          cn != 'compile "D:\\中文目录\\算例.dss"'.encode("ascii", "ignore"),
          "走的是 mbcs 分支")

    # ------------------------------------------------- C. 产物查找与判定
    _print()
    _print("-" * 78)
    _print("C. CliRunResult 的产物查找与判定")
    _print("-" * 78)
    r = CliRunResult(
        rc=0, timed_out=False, duration_s=0.5, stdout="", stderr="", workdir="<mem>",
        artifacts={
            "CASEX_EXP_Summary.CSV": '"Status","Iterations"\n"SOLVED",2\n',
            "CASEX_EXP_VOLTAGES.CSV": '"Bus","BasekV"\n"B1",12.47\n',
            "CASEX_Buses.Txt": "BUSES AND NODES\n",
        },
    )
    check("C1 按关键词找到产物（大小写不敏感）",
          r.has("voltages") and r.has("VOLTAGES") and r.has("Summary"))
    check("C2 找不到时返回 False 而不是抛错", not r.has("LOSSES"))
    check("C3 artifact 返回全文", "SOLVED" in (r.artifact("Summary") or ""))
    check("C4 artifact_lines 分行", len(r.artifact_lines("VOLTAGES")) == 2,
          str(r.artifact_lines("VOLTAGES")))
    check("C5 exported 解析出 export 类别",
          r.exported == ["summary", "voltages"], str(r.exported))
    check("C6 非 _EXP_ 的产物不混进 exported 列表",
          "buses" not in r.exported, str(r.exported))
    check("C7 summary() 给出可留痕的摘要",
          r.summary()["artifact_count"] == 3, str(r.summary()))

    r_to = CliRunResult(rc=None, timed_out=True, duration_s=3.0, stdout="", stderr="",
                        workdir="<mem>")
    check("C8 超时时 rc 为 None（区分「被杀」与「正常非零退出」）",
          r_to.rc is None and r_to.timed_out)
    check("C9 超时结果里 exported 为空且不抛错", r_to.exported == [])

    # ------------------------------------------------- D. 镜像（纯文件操作）
    _print()
    _print("-" * 78)
    _print("D. mirror_case：镜像到专用目录（源目录零写入）")
    _print("-" * 78)
    tmp = tempfile.mkdtemp(prefix="cli_runner_test_")
    src = os.path.join(tmp, "src")
    os.makedirs(os.path.join(src, "sub"), exist_ok=True)
    io.open(os.path.join(src, "Master.dss"), "w", encoding="ascii").write(
        'New Circuit.T basekv=12.47\nRedirect sub\\extra.dss\n'
    )
    io.open(os.path.join(src, "sub", "extra.dss"), "w", encoding="ascii").write(
        "New Line.L1 bus1=A bus2=B\n"
    )
    before = {}
    for dp, _d, fns in os.walk(src):
        for f in fns:
            fp = os.path.join(dp, f)
            before[os.path.relpath(fp, src)] = os.path.getmtime(fp)

    dest_root = os.path.join(tmp, "work")
    shadow = mirror_case(os.path.join(src, "Master.dss"), dest_root)
    check("D1 返回镜像后的 master 路径（在工作目录内）",
          os.path.isfile(shadow) and dest_root in os.path.abspath(shadow), shadow)
    check("D2 master 内容一致",
          io.open(shadow, encoding="ascii").read()
          == io.open(os.path.join(src, "Master.dss"), encoding="ascii").read())
    check("D3 依赖子目录一并镜像（Redirect 目标不能丢）",
          os.path.isfile(os.path.join(os.path.dirname(shadow), "sub", "extra.dss")))
    after = {}
    for dp, _d, fns in os.walk(src):
        for f in fns:
            fp = os.path.join(dp, f)
            after[os.path.relpath(fp, src)] = os.path.getmtime(fp)
    check("D4 源目录零写入（文件集合与 mtime 都不变）",
          before == after,
          "新增=%s 改动=%s" % (sorted(set(after) - set(before)),
                              sorted(f for f in set(before) & set(after)
                                     if before[f] != after[f])))

    raised = None
    try:
        mirror_case(shadow, dest_root, subdir=".")
    except CliError as exc:
        raised = exc
    check("D5 镜像目标在源目录内时报错（防递归拷贝）", raised is not None, str(raised))

    raised2 = None
    try:
        mirror_case(os.path.join(tmp, "no_such_dir", "Master.dss"), dest_root)
    except CliError as exc:
        raised2 = exc
    check("D6 源目录不存在时报错", raised2 is not None, str(raised2))

    raised3 = None
    try:
        run_cli_commands(["clear"], os.path.join(tmp, "w2"),
                         exe=os.path.join(tmp, "no_such.exe"), timeout_s=5)
    except CliError as exc:
        raised3 = exc
    check("D7 exe 不存在时抛 CliError（而不是静默失败）", raised3 is not None, str(raised3))
    check("D8 CliError 提示里带环境变量名（可操作）",
          raised3 is not None and "DPF_OPENDSS_CLI" in str(raised3), str(raised3))

    # ⚠️ D9/D10：**反向**包含 —— dest 落在 source **内部**。
    # 本轮实测踩到：CLI 适配器曾把工作目录建在算例目录里，于是 `os.walk`
    # 一边遍历 source 一边复制正在写入的 dest → **无限递归**
    # （一次产生 4247 层嵌套 / 2124 个文件，任务卡死 300s 被看门狗重启进程）。
    # 保护必须**双向**：只防"source ⊂ dest"是不够的。
    outer = os.path.join(tmp, "outer")
    os.makedirs(outer, exist_ok=True)
    io.open(os.path.join(outer, "Master.dss"), "w", encoding="ascii").write(
        "New Circuit.O basekv=12.47\n"
    )
    raised4 = None
    try:
        mirror_case(os.path.join(outer, "Master.dss"), outer, subdir="work")
    except CliError as exc:
        raised4 = exc
    check("D9 dest 落在 source 内部时报错（否则无限递归 —— 本轮实测踩到）",
          raised4 is not None, str(raised4))
    check("D10 报错信息点明是包含关系（便于定位）",
          raised4 is not None and "包含关系" in str(raised4), str(raised4))

    # ⚠️ D11~D16：**工作目录复用** = 静默读到上一个算例的产物。
    # 本轮实测踩到：同一个 `cli_work` 下连着跑两个算例，第二次的
    # `artifact("_Buses")` 取到了**上一个算例的母线表**，于是报出
    # 「发现未解析的母线引用：line.l1->sourcebus…」—— 错误信息把人引去查算例，
    # 真实原因却是目录复用。两道防线缺一不可：入口拒绝 + 检索拒绝多匹配。
    reuse_root = os.path.join(tmp, "reuse")
    src_a = os.path.join(reuse_root, "a")
    src_b = os.path.join(reuse_root, "b")
    for _d in (src_a, src_b):
        os.makedirs(_d, exist_ok=True)
        io.open(os.path.join(_d, "Master.dss"), "w", encoding="ascii").write(
            "New Circuit.C basekv=12.47\n"
        )
    work = os.path.join(tmp, "reuse_work")
    mirror_case(os.path.join(src_a, "Master.dss"), work)
    raised5 = None
    try:
        mirror_case(os.path.join(src_b, "Master.dss"), work)
    except CliError as exc:
        raised5 = exc
    check("D11 镜像目录非空时报错（工作目录被复用 → 会读到上一个算例的产物）",
          raised5 is not None, str(raised5))
    check("D12 报错信息点明是目录复用（便于定位）",
          raised5 is not None and "复用" in str(raised5), str(raised5))
    ok_existing = True
    try:
        mirror_case(os.path.join(src_b, "Master.dss"), work,
                    allow_existing=True)
    except CliError:
        ok_existing = False
    check("D13 只有 allow_existing=True 才放行（显式声明，不是默认）", ok_existing)

    dup = CliRunResult(
        rc=0, timed_out=False, duration_s=0.0, stdout="", stderr="", workdir=tmp,
        artifacts={"STPROBE_Buses.Txt": "2 相", "CASE07_Buses.Txt": "134 相"},
    )
    raised6 = None
    try:
        dup.artifact("_Buses")
    except CliError as exc:
        raised6 = exc
    check("D14 产物多匹配时明确失败（不静默取第一个）",
          raised6 is not None, str(raised6))
    check("D15 has() 只看存在性，多匹配不报错（职责分离）",
          dup.has("_Buses") is True)
    lone = CliRunResult(
        rc=0, timed_out=False, duration_s=0.0, stdout="", stderr="", workdir=tmp,
        artifacts={"STPROBE_Buses.Txt": "2 相"},
    )
    check("D16 单匹配时正常返回（断言没把正常路径也拦了）",
          lone.artifact("_Buses") == "2 相")

    # ------------------------------------------------- E. 契约（防回归）
    _print()
    _print("-" * 78)
    _print("E. 契约：驱动层不许引入删除动作 / 不许绕过白名单")
    _print("-" * 78)
    sig = list(inspect.signature(run_cli_commands).parameters)
    _print("  run_cli_commands 参数：%s" % sig)
    check("E1 驱动层没有清理开关（零删除动作）",
          not any("clear" in p.lower() and "workdir" in p.lower() for p in sig), str(sig))
    check("E2 必需参数是 commands 与 workdir",
          sig[:2] == ["commands", "workdir"], str(sig))

    src_text = io.open(
        os.path.join(_PROJ, "dpf_agent", "engines", "cli_runner.py"),
        encoding="utf-8",
    ).read()
    check("E3 运行时**不做任何删除**（os.remove 不存在）",
          "os.remove(" not in src_text,
          "清理曾被做成 clear_workdir 开关，实测既容易漏又会被安全策略拦")
    check("E4 驱动层**完全没有**目录删除（rmtree 不存在，零删除是结构性保证）",
          "shutil.rmtree(" not in src_text and "os.rmdir(" not in src_text,
          "曾有无调用点的 cleanup_workdir 与 mirror 的 reset 分支，都带着 rmtree；"
          "⚠️ 断言要看**调用形式**，只匹配 'rmtree' 会被说明文档里的字样误伤")
    check("E4b mirror_case 不再有 reset 参数（目录复用只许明确失败，不许悄悄清空）",
          "reset" not in inspect.signature(mirror_case).parameters,
          str(inspect.signature(mirror_case)))
    check("E5 输出落文件（不再用 PIPE + communicate，避免 kill 后干等）",
          "_cli_stdout.txt" in src_text and "communicate(input=" not in src_text,
          "PIPE 会让 kill 后一直等子进程关闭管道（实测 3s 超时→18s 返回）")
    check("E6 驱动层留痕不混进产物清单",
          'name.startswith("_cli_")' in src_text)
    check("E7 超时会 kill 并 wait（不留僵尸进程）",
          "proc.kill()" in src_text and "proc.wait(" in src_text)
    check("E8 产物收集支持额外目录（镜像模式下产物不在 workdir 根）",
          "artifact_dirs" in sig, str(sig))

    _print()
    _print("=" * 78)
    _print("结果：通过 %d / 失败 %d" % (_PASS, _FAIL))
    _print("=" * 78)
    try:
        io.open(os.path.join(tempfile.gettempdir(), "test_cli_runner.txt"), "w",
                encoding="utf-8").write("\n".join(_LINES) + "\n")
    except OSError:
        pass
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
