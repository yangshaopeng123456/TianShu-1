# -*- coding: utf-8 -*-
"""依赖声明守卫 —— 改 requirements.txt 前必跑。

起因（2026-10-04）：差点误删活依赖 dss-python。
静态扫 `import dss` 在 106 个 .py 里零命中，实为按需
`importlib.import_module("dss")`（engines/opendss_adapter.py:205），
且 web/api.py:495 用 `find_spec("dss")` 做引擎健康探测。

**判定依赖必须三面取证**：
  1. 静态 import（行首匹配，避免命中字符串里的 import）
  2. 动态加载（importlib / __import__ / find_spec / entry_point）
  3. 配置与探测引用

跑法（路径从 dpf_agent 起）：
    .venv/Scripts/python.exe dpf_agent/tests/dep_guard.py

FAIL 时的正确处理：**先怀疑判据，再怀疑被测对象**。
静态扫不到就断定「没用到」，正是这次差点出事的原因。

另外两个已知的「判据覆盖不到但确实需要」：
  - `openpyxl`   —— xlsx 由 `case_converters.convert_xlsx` 走
                   `pd.ExcelFile`（pandas）读取，代码里不直接 import openpyxl，
                   它是 pandas 的传递依赖。保留声明是对的。
  - `pythoncom`  —— 由已声明的 pywin32 提供（opendss_com_adapter.py:181）。
"""
import io
import json
import os
import re
import sys

# 文件位置：<树根>\dpf_agent\dpf_agent\tests\dep_guard.py
#   tests → dpf_agent(包) → dpf_agent(工程) → 树根  ⇒ 退 4 层
# 判据：推导出的树根下必须同时存在 dpf_agent\requirements.txt 与 tianshu_ui，
#     否则视为层级算错（不静默用错路径跑出假结果）。
AG = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
PKG = os.path.join(AG, "dpf_agent", "dpf_agent")
REQ = os.path.join(AG, "dpf_agent", "requirements.txt")
PKJ = os.path.join(AG, "tianshu_ui", "package.json")
OUT = os.path.join(AG, "dpf_agent", "_dep_guard.txt")

assert os.path.isfile(REQ), "树根推导错误：找不到 %s" % REQ
assert os.path.isdir(PKG), "树根推导错误：找不到 %s" % PKG

buf = []
PASS = FAIL = 0


def p(s=""):
    buf.append(str(s))


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        p("  [OK]   %s   %s" % (label, detail))
    else:
        FAIL += 1
        p("  [FAIL] %s   %s" % (label, detail))


def read_all_py(root):
    """返回 [(相对路径, 全文)]，排除 __pycache__。"""
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f.endswith(".py"):
                fp = os.path.join(dp, f)
                try:
                    out.append((os.path.relpath(fp, root),
                                io.open(fp, encoding="utf-8",
                                        errors="replace").read()))
                except OSError:
                    pass
    return out


pys = read_all_py(PKG)
p("=" * 78)
p("依赖声明守卫 —— 改 requirements.txt 前必跑")
p("=" * 78)
p()
p("扫了 %d 个 .py" % len(pys))
p()

# ---------- 1. requirements 解析 ----------
req = io.open(REQ, encoding="utf-8").read()
declared = []
for ln in req.splitlines():
    s = ln.strip()
    if not s or s.startswith("#"):
        continue
    if s.startswith("-"):        # -r / -e 等
        continue
    m = re.match(r"^([A-Za-z0-9_.\-]+)", s)
    if m:
        declared.append(m.group(1))
p("### 1. requirements.txt 声明的包")
for d in declared:
    p("   %s" % d)
p()

# ---------- 2. 三面证据扫描 ----------
STATIC = re.compile(r"^\s*(?:import\s+([A-Za-z0-9_.,\s]+)|from\s+([A-Za-z0-9_.]+)\s+import)", re.M)
DYN = re.compile(r"importlib\.import_module\(\s*[\"']([A-Za-z0-9_.]+)[\"']"
                 r"|__import__\(\s*[\"']([A-Za-z0-9_.]+)[\"']"
                 r"|find_spec\(\s*[\"']([A-Za-z0-9_.]+)[\"']"
                 r"|load_entry_point\(\s*[\"']([A-Za-z0-9_.]+)[\"']")


def mod_to_pkg(m):
    return m.replace("_", "-").lower()


evid = {}   # pkg -> [证据描述]
# ⚠️ 排除守卫自己：它的 docstring 里写着 importlib / find_spec 等字样，
#    会被自己的动态加载正则命中 ⇒ 报出「dep_guard.py 提供了 dss-python 证据」
#    这种假证据。判据不可信比没判据更危险。
SELF = os.path.basename(__file__)
for rel, text in pys:
    if os.path.basename(rel) == SELF:
        continue
    lines = text.splitlines()
    # (a) 静态 import —— 要落在行首，避免命中字符串里的 import
    for ln in lines:
        m = STATIC.match(ln)
        if m:
            mods = (m.group(1) or m.group(2) or "")
            for one in mods.split(","):
                one = one.split(" as ")[0].strip().split(".")[0]
                if one:
                    evid.setdefault(mod_to_pkg(one), []).append(
                        "%s:%d 静态 import" % (rel, lines.index(ln) + 1))
    # (b) 动态加载 —— 用字符串字面量，判据精确
    for m in DYN.finditer(text):
        name = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        if name:
            line_no = text[:m.start()].count("\n") + 1
            evid.setdefault(mod_to_pkg(name), []).append(
                "%s:%d 动态加载" % (rel, line_no))

# (c) 配置与探测引用
extra = []
cfg_text = ""
for f in ("config.py", "paths.py"):
    fp = os.path.join(PKG, f)
    if os.path.isfile(fp):
        cfg_text += io.open(fp, encoding="utf-8", errors="replace").read()
if "dss-python" in cfg_text:
    extra.append("config/paths 提到 dss-python")

p("### 2. 每个声明包的三面证据")
# ★ 导入名与 pip 包名不一致的映射。缺了它就会把「有证据的包」报成没证据 ——
#   判据看起来严格，实际是键名对不上造成的假红。
ALIAS = {
    # pip 名 -> 代码里的导入名
    "dss-python": "dss",
    "python-multipart": "multipart",
    "pywin32": "pythoncom",
    "uvicorn": "uvicorn",
    "openpyxl": "openpyxl",
}
for d in declared:
    key = d.lower().replace("_", "-")
    hits = list(evid.get(key, []))
    alt = ALIAS.get(key)
    if alt:
        hits += evid.get(alt, [])
    # pywin32 的证据是 win32com.client（COM 通道），不在包名直呼之列
    if key == "pywin32":
        for rel, text in pys:
            if os.path.basename(rel) == SELF:
                continue
            for i, ln in enumerate(text.splitlines(), 1):
                if "win32com" in ln and ln.strip().startswith(("import", "from")):
                    hits.append("%s:%d import win32com（COM 通道）" % (rel, i))
    # python-multipart 由 FastAPI **注册式**加载：代码里不 import 它，
    # 但 `from fastapi import File` + `files: list[UploadFile] = File(...)`
    # 会让 FastAPI 在运行时去找 multipart 解析器 ⇒ 缺它上传接口直接 422。
    # 判据必须扫这一组调用形式，否则又变成"扫不到=没用到"。
    # ⚠️ 两次踩过的坑：
    #   1) 只搜 `import multipart` —— 它压根不被 import
    #   2) 搜 upload 时 cwd 在 dpf_agent\，而 tianshu_ui 在**上一级** ⇒
    #      两次"零命中"都是路径错，不是代码不用。**先确认目录存在再断言。**
    if key == "python-multipart":
        for rel, text in pys:
            if os.path.basename(rel) == SELF:
                continue
            for i, ln in enumerate(text.splitlines(), 1):
                if ("from fastapi import" in ln
                        and re.search(r"\bFile\b", ln)) or \
                        re.search(r":\s*list\[\s*UploadFile\s*\]", ln) or \
                        re.search(r"\bUploadFile\b.*=\s*File\(", ln):
                    hits.append("%s:%d FastAPI 注册式上传（缺它上传报 422）"
                                % (rel, i))
    # openpyxl 是 pandas 的传递依赖：xlsx 由 case_converters.convert_xlsx
    # 走 pd.ExcelFile 读取，全仓 0 处 import openpyxl。声明保留是对的。
    if key == "openpyxl":
        for rel, text in pys:
            if os.path.basename(rel) == SELF:
                continue
            for i, ln in enumerate(text.splitlines(), 1):
                if "ExcelFile" in ln or "read_excel" in ln:
                    hits.append("%s:%d 经 pandas 读 xlsx（openpyxl 为其传递依赖）"
                                % (rel, i))
    check("%-18s 有使用证据" % d, bool(hits), "证据 %d 处" % len(hits))
    for h in hits[:3]:
        p("           - %s" % h)
p()

# ---------- 3. 反向：代码用了但没声明 ----------
p("### 3. 反向检查（用了但没声明 → 会漏装）")
# 只查项目自研的模块（标准库与相对导入不算）
STDLIB = set("""os sys io re json time math shutil zipfile subprocess threading
hashlib sqlite3 asyncio pathlib datetime tempfile itertools collections logging
argparse csv unicodedata ctypes importlib inspect abc typing dataclasses enum
functools socket base64 glob random traceback uuid warnings contextlib copy
textwrap struct signal multiprocessing urllib http webbrowser xml""".split())
found_gap = []
for rel, text in pys:
    m = STATIC.match(text.split("\n")[0] or "")
    for ln in text.splitlines():
        mm = STATIC.match(ln)
        if not mm:
            continue
        mods = (mm.group(1) or mm.group(2) or "")
        for one in mods.split(","):
            one = one.split(" as ")[0].strip()
            if not one or one.startswith("."):
                continue
            if one in STDLIB or one in ("dpf_agent",):
                continue
            pkg = mod_to_pkg(one)
            if pkg not in [d.lower() for d in declared]:
                found_gap.append("%s -> %s" % (rel, one))
if found_gap:
    # ⚠️ 判据方向：**动态判定标准库与已声明包**，不能把名单写死。
    #   上一版把「标准库清单」硬编码在脚本里，于是 `cmath`/`gc`/`queue`
    #   这些漏了的名全被误报；写死的"需人工确认"提示也会在清单已修好后继续报，
    #   变成假红。改成：能用 `find_spec` 判的一律问 Python。
    import importlib.util as _u

    declared_lower = [d.lower() for d in declared]
    # 间接依赖：由别的包装着，不该直接声明
    INDIRECT = {"starlette", "uvicorn", "anyio", "h11", "httptools",
                "watchfiles", "websockets", "uvloop", "python-dotenv",
                "pyyaml", "typing-extensions", "pydantic-core"}
    # 已核实：不由本清单声明也对，但代码确实 import 了。逐个给证据，不是笼统排除。
    VERIFIED = {
        "pythoncom": "由已声明的 pywin32 提供（engines/opendss_com_adapter.py:181）",
        "win32com": "由已声明的 pywin32 提供（同上 :185）",
        # ★ 键名要与 `mod_to_pkg()` 的输出完全一致 —— 它把下划线换成连字符并转小写。
        #   写成 "power-rag-client" 而实际得到 "power-rag-client" 看似对，
        #   但 mod_to_pkg 作用于 "power_rag_client" 得 "power-rag-client"，
        #   这里曾经写成 "power-rag-client" 却没命中 —— 因为该模块是**本项目自有**，
        #   更稳妥的判据是「能在本包内找到同名 .py」而不是列白名单。
        "power-rag-client": "**本项目自有模块**（rag/power_rag_client.py），"
                            "那行 import 是同目录内的导入，不是第三方包",
    }
    # 本包内自研模块：根本不需要在清单里声明
    OWN_MODULES = set()
    try:
        for _f in os.listdir(os.path.join(PKG, "rag")):
            if _f.endswith(".py"):
                OWN_MODULES.add(mod_to_pkg(_f[:-3]))
        for _f in os.listdir(PKG):
            if _f.endswith(".py"):
                OWN_MODULES.add(mod_to_pkg(_f[:-3]))
    except OSError:
        pass
    top = set()
    stdlib_mis = []
    verified_hits = []
    for g in sorted(set(found_gap)):
        name = g.split("-> ")[-1].strip()
        top_name = name.split(".")[0]
        if top_name.startswith("__") or top_name in ("dpf_agent",):
            continue
        if mod_to_pkg(top_name) in OWN_MODULES:
            verified_hits.append("%s（本项目自有模块，非第三方包）" % name)
            continue
        if top_name.lower() in VERIFIED:
            verified_hits.append("%s（%s）" % (name, VERIFIED[top_name.lower()]))
            continue
        # 问 Python：是不是标准库（内建or 源码不在 site-packages）
        if top_name in sys.builtin_module_names:
            stdlib_mis.append(name)
            continue
        try:
            sp = _u.find_spec(top_name)
        except Exception:
            sp = None
        if sp is None:
            top.add(name)          # 真第三方且没装 → 值得报
            continue
        origin = sp.origin or ""
        if origin and "site-packages" not in origin:
            stdlib_mis.append(name)   # 标准库
            continue
        if top_name.lower() in declared_lower:
            continue                # 已声明
        if top_name.lower() in INDIRECT:
            continue                # 间接依赖
        if "." in name and name.split(".")[0].lower() in declared_lower:
            continue                # 已声明包的子模块
        top.add(name)

    if top:
        for n in sorted(top):
            p("  ★ %s" % n)
        p("")
        p("  ↑ 这些是代码里真的 import、但 requirements 没声明的第三方包。")
        p("    处理前先确认是不是「间接依赖」（靠别的包装着），")
        p("    是真直接依赖就补进清单。")
    else:
        p("  ✔ 顶层 import 的第三方包都已在 requirements 里声明")
        p("     （已排除标准库 %d 项、__future__、已声明包的子模块、间接依赖）"
          % len(stdlib_mis))
    if verified_hits:
        p("")
        p("  以下已逐个核实，不需声明（各附证据）：")
        for v in verified_hits:
            p("     - %s" % v)
    if stdlib_mis:
        p("")
        p("     已排除的标准库（%d 项）：%s"
          % (len(stdlib_mis), ", ".join(sorted(set(stdlib_mis)))))
else:
    p("  ✔ 顶层 import 的第三方包都已在 requirements 里声明")
p()

p("### 4. 特别关注：dss-python（差点误删的那个）")
dss_hits = [h for h in evid.get("dss-python", [])] + \
           [h for h in evid.get("dss", [])]
check("dss-python 有动态加载证据（不可删）", bool(dss_hits),
      "%d 处" % len(dss_hits))
for h in dss_hits:
    p("         - %s" % h)
p()
p("  判据说明：静态 `import dss` 抓不到它，它是 `importlib.import_module(\"dss\")`。")
p("  这条守卫的价值就是让「删依赖」必须有机械证据，不能凭印象。")
p()

p("### 5. package.json（前端）")
pj = json.load(io.open(PKJ, encoding="utf-8"))
deps = pj.get("dependencies", {})
devs = pj.get("devDependencies", {})
check("electron 已声明", "electron" in devs or "electron" in deps,
      "deps=%s dev=%s" % (list(deps), list(devs)))
ui_root = os.path.join(AG, "tianshu_ui")
ui_src = []
for dp, dn, fn in os.walk(ui_root):
    dn[:] = [d for d in dn if d not in ("node_modules", "__pycache__")]
    for f in fn:
        if f.endswith((".js", ".html")):
            ui_src.append(io.open(os.path.join(dp, f), encoding="utf-8",
                                  errors="replace").read())
has_electron = any("electron" in t.lower() for t in ui_src)
check("前端源码确实引用 electron", has_electron)
p()

p("=" * 78)
p("结果：通过 %d / 失败 %d" % (PASS, FAIL))
p("（**FAIL 时的正确处理**：先确认是不是按需动态导入造成的误判，")
p("  确认无误再动requirements.txt —— 直接删可能砍掉一条活的引擎通道）")
p("=" * 78)

io.open(OUT, "w", encoding="utf-8", newline="\n").write("\n".join(buf))
print("\n".join(buf))