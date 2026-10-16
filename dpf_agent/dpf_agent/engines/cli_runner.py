# -*- coding: utf-8 -*-
"""OpenDSS CLI 子进程驱动：喂命令流、收产物、硬超时强杀。

## 这个模块存在的意义

`OpenDSScmd.exe` 是 **EPRI 官方**发布的独立可执行文件（不是我们写的引擎），
本身就是一个独立进程。用它做计算通道，进程隔离是**免费获得**的 ——
而进程内 COM 引擎挂死后无法回收（Python 杀不掉卡在原生调用里的线程）。

价值不在"算得更准"（两者是**同一份引擎代码**，同一版本号），而在于：

* **离线可验证** —— 命令行就能起真引擎，不必依赖"应用正在运行"这个前提；
* **可强杀** —— 挂死时能真正回收资源；
* **第二把尺子** —— 独立数据通路，能校验我们的 CSV/属性解析与口径定义。

## 实测得到的硬约束（全部来自 Step 0.5 与本轮探针，不要凭直觉改动）

1. **喂命令流，不是传文件名**。直接 `OpenDSScmd.exe Master.dss` → rc=301 且不编译。
   正确姿势是 stdin 喂 `compile "<master>"` → `solve` → `export ...` → `exit`。
2. **产物落「算例文件所在目录」，不是 cwd** —— `compile` 会把 OpenDSS 的数据目录
   切到该算例所在目录，`export` 随即输出到那里（9/16 实测：cwd 与数据目录都空，
   算例目录里出现全部 CSV。stdout 里直接打出产物绝对路径）。
   ⇒ **绝不能在用户的算例目录里直接跑**，会往那里写一堆 CSV 污染用户数据。
   正确做法是 `mirror_case()` 把算例镜像到专用工作目录，在镜像上 compile，
   产物自然落在工作目录里，用完整个删除。
   ⚠️ **banner 里打印的 `Data Directory` / `Output Directory` 是上一次运行的残留**，
   不能拿它判断产物落点；**必须以实际文件是否出现为准**。
   （早先"产物落 cwd"的结论是实验设计缺陷：当时 cwd 恰好就是算例目录，
   两个假设无法区分。）
3. **`rc` 不可靠**：非 0 也可能已产出全部有效 CSV（实测 rc=24700 时产物齐全）。
   判定看**产物是否齐全**，不看 rc。
4. **某些 export 会触发 Access Violation**（`meters` / `faultstudy`，实测 rc=303）
   → 见 `SAFE_EXPORTS` / `BANNED_EXPORTS`，命令必须走白名单。
5. **某些求解设置组合会极慢甚至挂死**：四条求解设置全设时开销从 2s 涨到 19.7s，
   把 `maxiterations` 调大直接 HANG。**所以硬超时与强制 kill 是必须的** ——
   这正是 CLI 通道相对进程内 COM 的最大优势，必须用上。
6. **输入必须能编码**：命令流按 ASCII 喂；算例路径含非 ASCII（中文目录名）时
   退化为系统 ANSI 代码页，否则 `.encode("ascii")` 直接抛异常。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

#: 实测安全的 export 命令（白名单）。未在此列的命令一律不执行。
SAFE_EXPORTS: tuple[str, ...] = (
    "summary",
    "voltages",
    "powers",
    "currents",
    "losses",
    "capacity",
    "loads",
    "overloads",
    "buscoords",
    "yprim",
    "seqvoltages",
    "genmeters",
)

#: 会让引擎触发 Access Violation 的命令（实测 rc=303，进程进入异常状态）。
BANNED_EXPORTS: dict[str, str] = {
    "meters": "export meters 触发 Access Violation（实测 rc=303）",
    "faultstudy": "export faultstudy 触发 Access Violation（实测 rc=303）",
}

#: 不存在的 export 子命令（实测报 rc=24713）。列在这里是为了给出明确诊断，
#: 而不是让调用方从一堆 rc 里猜。
MISSING_EXPORTS: dict[str, str] = {
    "transformers": "无此 export 子命令（实测报错）；变压器数据用 powers 的 Terminal 行",
    "elementcurrents": "无此 export 子命令（实测报错）",
}

_DEFAULT_TIMEOUT_S = 120.0


class CliError(Exception):
    """CLI 驱动层的失败（定位/启动/超时）。上层据此转 AdapterError。"""

    def __init__(self, message: str, *, timed_out: bool = False):
        super().__init__(message)
        self.message = message
        self.timed_out = timed_out


# --------------------------------------------------------------------------- 定位 exe


def discover_cli_path() -> tuple[str | None, str]:
    """定位 `OpenDSScmd.exe`，返回 (路径, 说明)。

    优先级：环境变量 → COM 注册表推导（**最可靠**：COM 引擎已验证可用，
    同目录必有 CLI）→ 常见安装路径。
    """
    env = os.environ.get("DPF_OPENDSS_CLI")
    if env:
        if os.path.isfile(env):
            return env, "来自环境变量 DPF_OPENDSS_CLI"
        return None, f"环境变量 DPF_OPENDSS_CLI 指向的文件不存在：{env}"

    # 从 COM 注册表反推：InprocServer32 指向 ...\x64\OpenDSSEngine.dll
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"OpenDSSEngine.DSS\CLSID") as k:
            clsid = winreg.QueryValueEx(k, "")[0]
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\InprocServer32"
        ) as k:
            dll = winreg.QueryValueEx(k, "")[0]
        cand = os.path.join(os.path.dirname(os.path.expandvars(str(dll))), "OpenDSScmd.exe")
        if os.path.isfile(cand):
            return cand, f"由 COM 注册表推导（{dll}）"
    except Exception:  # noqa: BLE001 - 注册表缺失/非 Windows：继续走候选路径
        pass

    for cand in (
        r"D:\Program Files\OpenDSS\x64\OpenDSScmd.exe",
        r"C:\Program Files\OpenDSS\x64\OpenDSScmd.exe",
        r"C:\Program Files (x86)\OpenDSS\x64\OpenDSScmd.exe",
    ):
        if os.path.isfile(cand):
            return cand, "来自常见安装路径"
    return None, "未找到 OpenDSScmd.exe（可设 DPF_OPENDSS_CLI 指定）"


# --------------------------------------------------------------------------- 运行


@dataclass
class CliRunResult:
    """一次 CLI 运行的完整留痕。"""

    rc: int | None
    timed_out: bool
    duration_s: float
    stdout: str
    stderr: str
    workdir: str
    commands: tuple[str, ...] = ()
    #: 产物文件名 → 全文（文本）。键是**原文件名**，匹配时用 `artifact()`。
    artifacts: dict[str, str] = field(default_factory=dict)

    def artifact_all(self, keyword: str) -> dict[str, str]:
        """按关键词取**全部**匹配产物（大小写不敏感），键是原文件名。"""
        want = keyword.strip().upper()
        return {n: t for n, t in self.artifacts.items() if want in n.upper()}

    def artifact(self, keyword: str) -> str | None:
        """按关键词取产物（大小写不敏感）。OpenDSS 会用算例名作前缀，
        所以文件名不固定，只能按关键词找（如 `EXP_VOLTAGES` / `_Buses.Txt`）。

        ⚠️ **多匹配 = 明确失败**（而不是"取第一个"）。目录里出现两份同类产物
        只可能是**工作目录被复用**，此时返回哪一份由遍历顺序决定 →
        **静默读到上一个算例的结果**。实测过：连着跑两个算例，第二次的
        `_Buses` 取到了上一个算例的母线表，报出误导性的「未解析的母线引用」。
        宁可在这里明确失败，也不要让错误数据继续往下走。
        """
        hits = self.artifact_all(keyword)
        if len(hits) > 1:
            raise CliError(
                f"产物关键词 {keyword!r} 命中 {len(hits)} 份"
                f"（{sorted(hits)}）—— 工作目录被复用，会读到上一个算例的产物；"
                "请为每个任务/算例使用唯一的工作目录。"
            )
        return next(iter(hits.values())) if hits else None

    def artifact_lines(self, keyword: str) -> list[str]:
        text = self.artifact(keyword)
        return text.splitlines() if text else []

    def has(self, keyword: str) -> bool:
        """是否存在该类产物（**只看存在性**，多匹配不报错——那是 `artifact()` 的职责）。"""
        return bool(self.artifact_all(keyword))

    @property
    def exported(self) -> list[str]:
        """产出的 export 类别（从文件名反推），供"产物是否齐全"判定。"""
        out: list[str] = []
        for name in self.artifacts:
            up = name.upper()
            if "_EXP_" in up and up.endswith(".CSV"):
                out.append(up.split("_EXP_", 1)[1][:-4].lower())
        return sorted(set(out))

    def summary(self) -> dict[str, object]:
        return {
            "rc": self.rc,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 3),
            "exported": self.exported,
            "artifact_count": len(self.artifacts),
            "stdout_bytes": len(self.stdout),
        }


def _encode_commands(cmds: str) -> bytes:
    """把命令流编码成字节喂给 stdin。

    OpenDSS cmd 读的是**代码页字节**。纯 ASCII 用 ascii；含中文路径（常见：
    算例放在中文命名的目录下）时退化为系统 ANSI 代码页，否则直接抛异常。
    """
    try:
        return cmds.encode("ascii")
    except UnicodeEncodeError:
        return cmds.encode("mbcs", "replace")


def run_cli_commands(
    commands: list[str] | tuple[str, ...],
    workdir: str,
    *,
    exe: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    artifact_dirs: list[str] | tuple[str, ...] | None = None,
) -> CliRunResult:
    """在 `workdir` 里喂一段命令流给 CLI，回收产物。

    **硬超时 + 强制 kill**：超时不抛 `TimeoutExpired` 就返回，而是杀掉进程并
    返回 `timed_out=True` 的结果 —— 因为"能不能杀掉"正是这条通道的价值所在，
    调用方需要拿到明确的终止信号，而不是一个不确定的异常。

    ## 关于 `workdir` 与产物残留

    **必须给一个全新目录**（如 `<workspace>/cli_work` —— 任务工作目录天然唯一）。
    本函数**不做任何删除动作**：曾提供过 `clear_workdir` 开关来支持"复用目录 +
    逐个清理"，实测有两个问题 —— 一是清理不干净就会把上一次的结果当成本次结果
    （比不跑更危险），二是逐个删除会大量累积删除操作，触发文件安全策略的
    "批量删除"拦截。需要回收目录时由调用方在**任务收尾**统一做一次。

    `artifact_dirs` 是额外的产物收集目录。默认只收 `workdir`，配合
    `mirror_case()` 使用时产物本来就在 `workdir` 里；若调用方选择直接在算例目录跑
    （不推荐），必须把该目录传进来，否则收集不到产物。
    """
    path = exe or os.environ.get("DPF_OPENDSS_CLI") or discover_cli_path()[0]
    if not path or not os.path.isfile(path):
        raise CliError(
            "未找到 OpenDSScmd.exe；可设环境变量 DPF_OPENDSS_CLI 指定绝对路径"
        )
    os.makedirs(workdir, exist_ok=True)

    cmds = list(commands)
    if not cmds or cmds[-1].strip().lower() != "exit":
        cmds.append("exit")
    payload = _encode_commands("\n".join(cmds) + "\n")

    # ⚠️ 输出**落文件**而不是走 PIPE，这是刻意的（9/16 实测踩到）：
    #    用 PIPE + communicate() 时，kill 只杀掉直接进程，它派生的子进程仍持有
    #    管道写端，communicate() 会一直等到**所有**写端关闭才返回 ——
    #    实测超时设 3s、实际 18s 才拿回控制权。而"能立刻杀掉并回收"正是这条
    #    通道相对进程内 COM 的核心优势，等下去就等于把优势还回去了。
    #    改为文件后，kill 完直接读文件，不再依赖任何进程退出。
    out_path = os.path.join(workdir, "_cli_stdout.txt")
    err_path = os.path.join(workdir, "_cli_stderr.txt")
    t0 = time.time()
    try:
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            proc = subprocess.Popen(
                [path], stdin=subprocess.PIPE, stdout=fo, stderr=fe, cwd=workdir,
            )
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:  # noqa: BLE001 - 进程可能已自行退出
                pass
            timed_out = False
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
            rc = proc.returncode
    except OSError as exc:
        raise CliError(f"启动 OpenDSScmd 失败：{exc}") from exc
    duration = time.time() - t0

    def _read_file(p: str) -> str:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    out_text = _read_file(out_path)
    err_text = _read_file(err_path)

    artifacts: dict[str, str] = {}
    for root in [workdir] + list(artifact_dirs or ()):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            low = name.lower()
            if not low.endswith((".csv", ".txt")):
                continue
            if name.startswith("_cli_"):
                continue  # 驱动层自己的留痕（_cli_stdout.txt / _cli_stderr.txt）
            full = os.path.join(root, name)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    artifacts[name] = f.read()
            except OSError:
                continue

    return CliRunResult(
        rc=None if timed_out else rc,
        timed_out=timed_out,
        duration_s=duration,
        stdout=out_text,
        stderr=err_text,
        workdir=workdir,
        commands=tuple(cmds),
        artifacts=artifacts,
    )


#: 镜像算例时的体积上限。任务副本通常只有几十 KB~几 MB；
#: 超过这个量级说明目录选错了（比如指到了项目根），必须报错而不是硬拷。
MIRROR_MAX_BYTES = 200 * 1024 * 1024


def mirror_case(
    master_dss: str,
    workdir: str,
    *,
    subdir: str = "case",
    allow_existing: bool = False,
) -> str:
    """把算例**镜像**到专用工作目录，返回镜像后的 master 路径。

    为什么必须镜像：`export` 的产物落在**算例文件所在目录**（见模块文档第 2 条），
    直接在用户算例目录跑会往里写 `*_EXP_*.CSV` 等文件。镜像一份再跑，
    产物落在工作目录里，**源目录零写入**。

    拷贝整个源目录（而不是只拷 master）：`Redirect` 会引用同目录的其它文件，
    只拷 master 会让依赖丢失，表现为 `DATA_UNAVAILABLE`。

    ## 目录复用 = 明确失败（默认行为）

    镜像目录里若已有内容，说明**工作目录被复用了** —— 后果是：
    OpenDSS 的产物名以算例/电路名为前缀，但 `CliRunResult.artifact()` 是**按关键词**
    匹配的（`_Buses` / `EXP_Loads` …）。目录里有两份同类产物时，取到哪一份由遍历
    顺序决定 → **静默读到上一个算例的结果**。

    实测过这个坑：连着跑两个算例（第二个的电路名排序靠后），第二次的
    `artifact("_Buses")` 取到了**上一个算例的母线表**，于是报
    「发现未解析的母线引用：line.l1->sourcebus…」—— 错误信息把人引去查算例，
    真实原因却是目录复用。

    所以默认**拒绝**非空目录（fail-closed），而不是清空它们：调用方应改用
    **唯一的工作目录**（每个任务的工作区天然唯一），这样全程**零删除动作**。
    `allow_existing=True` 只给"明确知道自己在复用"的场景（如探针脚本）。

    ⚠️ 本模块**没有任何删除动作** —— 这是结构性保证，不是约定：
    `reset` 参数与 `cleanup_workdir()` 都已移除（前者会把"目录复用"从
    「明确失败」降级成「悄悄清空」，后者是无调用点的死代码却带着 `rmtree`）。
    工作目录的回收交由任务工作区自身的生命周期处理。
    """
    source_dir = os.path.dirname(os.path.abspath(master_dss))
    if not os.path.isdir(source_dir):
        raise CliError(f"算例目录不存在：{source_dir}")
    dest = os.path.join(workdir, subdir)
    dest_abs = os.path.abspath(dest)
    # ⚠️ **双向**包含检查，两个方向都必须拦（实测踩到，代价很大）：
    #   * `dest ⊂ source`：**dest 在源目录内部** → `os.walk` 遍历源目录时
    #     会把正在写入的 dest 也遍历进去，一边走一边复制自己 → **无限递归**。
    #     实测：一次就产生了 **4247 层嵌套目录 / 2124 个文件**，任务卡死 300s
    #     直到看门狗兜底重启进程。
    #   * `source ⊂ dest`：dest 在源目录外面包住源目录 → 镜像无意义且可能自包含。
    if (dest_abs == source_dir
            or dest_abs.startswith(source_dir + os.sep)
            or source_dir.startswith(dest_abs + os.sep)):
        raise CliError(
            "镜像目标与源目录存在包含关系，会造成递归拷贝："
            f"source={source_dir} dest={dest_abs}"
        )

    if os.path.isdir(dest) and not allow_existing and os.listdir(dest):
        raise CliError(
            "镜像目录已存在且非空，说明工作目录被复用（会静默读到上一个算例的产物）："
            f"{dest}。请为每个任务/算例使用**唯一**的工作目录。"
        )
    os.makedirs(dest, exist_ok=True)

    total = 0
    for dirpath, dirnames, filenames in os.walk(source_dir):
        dirnames.sort()
        rel = os.path.relpath(dirpath, source_dir)
        target_dir = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(target_dir, exist_ok=True)
        for filename in sorted(filenames):
            src = os.path.join(dirpath, filename)
            try:
                total += os.path.getsize(src)
            except OSError:
                continue
            if total > MIRROR_MAX_BYTES:
                raise CliError(
                    f"算例目录超过 {MIRROR_MAX_BYTES // (1024 * 1024)} MB 上限，"
                    f"疑似选错目录：{source_dir}"
                )
            try:
                shutil.copy2(src, os.path.join(target_dir, filename))
            except OSError:
                continue
    return os.path.join(dest, os.path.basename(master_dss))


# --------------------------------------------------------------------------- 命令构造


def build_export_commands(want: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """按白名单构造 `export` 命令序列。

    默认导出适配器需要的全套；传入 `want` 时只保留白名单内的项。
    遇到 `BANNED_EXPORTS` / `MISSING_EXPORTS` 里的名字会**明确跳过**
    （而不是让引擎崩一次再排查）。
    """
    names = list(want) if want else list(SAFE_EXPORTS)
    out: list[str] = []
    for name in names:
        key = str(name).strip().lower()
        if key in BANNED_EXPORTS or key in MISSING_EXPORTS:
            continue
        if key not in SAFE_EXPORTS:
            continue
        out.append(f"export {key}")
    return out


def parse_banner_version(stdout: str) -> str:
    """从 banner 里取引擎版本号（如 `11.0.0.1 (64-bit build)`）。

    banner 形如：
      `* OpenDSS console Version 11.0.0.1 (64-bit build) - Charlottesville *`
    取不到时返回空串（调用方据此决定是否降级）。
    """
    for raw in stdout.splitlines():
        line = raw.strip().strip("*").strip()
        low = line.lower()
        if "opendss" in low and "version" in low:
            idx = low.find("version")
            tail = line[idx + len("version"):].strip()
            # 截到 " - " 之前
            for sep in (" - ", " *", "*"):
                pos = tail.find(sep)
                if pos > 0:
                    tail = tail[:pos]
            return tail.strip()
    return ""
