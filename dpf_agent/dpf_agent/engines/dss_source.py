# -*- coding: utf-8 -*-
"""DSS 源文本解析：从算例文件提取元件定义（引擎无关）。

## 为什么需要它

两条通道都有「引擎对象模型看不到的东西」：

* **被禁用的元件** —— `Enabled=no` 的对象**不在** `ckt.Lines` 等集合里。
  `domain/network_profile._scan_defined_branch_status` 当初就是为它写的。
* **端点母线名** —— CLI 的 `export powers` CSV 只有 `Element` + `Terminal` +
  功率，**没有任何母线列**（9/16 实测全部 export 命令的列名），
  而拓扑画像（`cycle_rank` / `is_radial` / `parallel_line_pairs`）必须有
  `bus1` / `bus2`。

两件事都是"读源文件"，所以解析逻辑**单点定义**在这里，各通道按需取用。

## 扫描范围：目录，而不是跟随 redirect

跟随 `Redirect` 需要复刻 OpenDSS 的路径解析规则（相对路径基准、`Set DataPath`
的影响等），容易出错。任务副本目录（`<workspace>/input/`）按设计只含该算例的文件，
因此**扫目录**既简单又完备 —— 与既有 `_scan_defined_branch_status` 的做法一致。

## 支持的 DSS 语法

* 注释 `!` 与 `//`
* 逻辑行续接：行首 `~`
* `New <Class>.<name> props...` 与 `New <Class> <name> props...`
* `Edit <Class>.<name> props...`（覆盖同名元件的属性）
* 属性值可带引号
* **保留重复属性**（变压器每个绕组都有一个 `bus=`）—— 所以存的是**有序 token**，
  不是 dict

刻意**不支持** `Like=` 继承、`BatchEdit`、`Set` 变量插值等高级语法：它们罕见，
且静默解析错比明确不支持更危险。调用方拿到的 `elements` 是"文本里字面写了什么"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- 基础解析


def _strip_comment(raw: str) -> str:
    """去掉行内注释。`!` 与 `//` 都是 OpenDSS 的注释起始符。"""
    out = raw
    for marker in ("!", "//"):
        i = out.find(marker)
        if i >= 0:
            out = out[:i]
    return out.strip()


def _logical_lines(text: str):
    """把物理行合并成逻辑行（处理行首 `~` 续接）。

    产出 `(起始行号, 逻辑行文本)`。空行与纯注释行被跳过，且**不打断**续接 ——
    `New Transformer.T1 ...` 后面隔一行空行再写 `~ wdg=1 ...` 是合法且常见的写法。
    """
    buf = ""
    start = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        s = _strip_comment(raw)
        if not s:
            continue
        if s.startswith("~"):
            body = s[1:].strip()
            if not body:
                continue
            if buf:
                buf += " " + body
            else:
                buf, start = body, lineno
        else:
            if buf:
                yield start, buf
            buf, start = s, lineno
    if buf:
        yield start, buf


def _split_tokens(line: str) -> list[str]:
    """按空白切分并剥掉外层引号。

    不用 `shlex`：它会把反斜杠当转义符，而 DSS 里合法出现 Windows 路径。

    ⚠️ **方括号对必须当成引号对**（本轮修的真机缺陷）：
    DSS 的多值属性用 `[]` 包裹且**内部可以有空格**，例如
        `New Transformer.TA ... buses=[GRID SUB110A] conns=[wye wye]`
    只按空白切分会把它拆成 `buses=[GRID` 与 `SUB110A]` 两个 token ——
    前者去掉 `[` 后只剩 1 个母线，**端点就丢了一个**。
    真机 case31 实测：16 台变压器端点全空，而线路 264 条全正常，
    差别正在于此（线路用 `bus1=/bus2=`，变压器用 `buses=[...]`）。
    注意 `kvas=[150000 150000]`、`%rs=[...]` 同样受益 ——
    切分错误会一路影响到 `rated_kva` 的取值。
    """
    raw = line
    toks: list[str] = []
    buf: list[str] = []
    depth = 0          # 方括号嵌套深度
    quote: str | None = None   # 当前所处引号
    for ch in raw:
        if quote:
            # 收尾引号：写回但**标记它该被剥掉**。做法是把它放进 buf 之后
            # 由下面的统一剥离处理；这里只负责"引号内的空格不算分隔符"。
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            # 开引号：仅当当前缓冲区为空（token 起始）才视为引号，
            # 否则 `bus1=A"B` 这类畸形写法仍按普通字符处理。
            if not "".join(buf).strip():
                buf = []
            quote = ch
            buf.append(ch)
            continue
        if ch in "[(":
            depth += 1
            buf.append(ch)
            continue
        if ch in "])":
            if depth:
                depth -= 1
            buf.append(ch)
            continue
        if ch.isspace() and depth == 0:
            if buf:
                toks.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        toks.append("".join(buf))
    # 剥掉外层引号（成对的引号/方括号都不作为 token 的一部分保留）
    out: list[str] = []
    for piece in toks:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) >= 2 and (
            (piece[0] == '"' and piece[-1] == '"')
            or (piece[0] == "'" and piece[-1] == "'")
        ):
            piece = piece[1:-1]
        out.append(piece)
    return out


def _unquote(value: str) -> str:
    """剥掉**成对**的外层引号；不配对时原样返回（畸形写法不硬吞）。

    ⚠️ 为什么不能只在 token 两端剥：`bus1="A B"` 整个是一个 token，
    两端是 `b` 和 `"` —— 不成对。引号在 `=` **之后**，
    必须在值这一层剥（真机实测：`'"a b"'` 直接进了母线名）。
    """
    v = str(value).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1].strip()
    return v


def _norm_bus(value: str) -> str:
    """母线名归一化：去相别、去空白、转小写。

    `Bus4.1.2` → `bus4`。与 `circuit_validation.normalize_bus_name` 同语义 ——
    这里**不能 import 它**：本模块属于"源文本"层，不依赖任何引擎侧命名，
    以免将来 CLI 侧独立使用时要拖进整条链。

    （两者语义一致由测试锁死，见 `test_dss_source`。）
    """
    return str(value).split(".", 1)[0].strip().lower()


# --------------------------------------------------------------------------- 元件定义


@dataclass(frozen=True)
class DssElementDef:
    """一条 `New`/`Edit` 语句解析出的元件定义。

    `tokens` 是**有序的** `key=value` 序列（key 小写）。之所以不是 dict：
    变压器每个绕组都写一次 `bus=`，dict 会丢掉除第一个以外的全部。
    """

    kind: str  # 小写类名，如 "line" / "linecode" / "transformer"
    short_name: str  # 点号后的名字（无点号写法时为空串）
    tokens: tuple[str, ...]
    source_file: str = ""
    source_line: int = 0

    @property
    def full_name(self) -> str:
        """全名（小写）。有短名时是 `line.l1`，否则退化为类名本身。"""
        return f"{self.kind}.{self.short_name}" if self.short_name else self.kind

    def get(self, key: str, default: str | None = None) -> str | None:
        """取属性值（第一个匹配，大小写不敏感）。"""
        k = key.strip().lower()
        prefix = k + "="
        for tok in self.tokens:
            if tok.lower().startswith(prefix):
                return tok[len(prefix):]
        return default

    def get_all(self, key: str) -> tuple[str, ...]:
        """取属性值的全部出现（变压器多绕组场景）。"""
        k = key.strip().lower()
        prefix = k + "="
        return tuple(tok[len(prefix):] for tok in self.tokens if tok.lower().startswith(prefix))

    @property
    def enabled(self) -> bool:
        v = (self.get("enabled") or "").strip().lower()
        return v not in ("no", "false", "0")

    @property
    def buses(self) -> tuple[str, ...]:
        """该元件引用的母线（去相别、小写、按出现顺序）。

        覆盖 `bus1` / `bus2` / `bus=` / `buses=[...]` / `buses=(...)` 五种写法：
          * `New Line.L1 bus1=A bus2=B`        → (a, b)
          * `New Transformer.T1 wdg=1 bus=A …` → (a, b, …)  逐绕组
          * `New Transformer.TA buses=[GRID SUB110A]` → (grid, sub110a)  ⚠️ 复数+方括号
          * `New Transformer.TA buses=(GRID SUB110A)` → (grid, sub110a)  ⚠️ 复数+圆括号
            （OpenDSS 官方算例就用这种，见 IEEE 37 节点 `reg1a`）
          * `New Load.LD1 bus1=A`→ (a,)
          * `New Line.L1 bus1="A B" bus2=C`    → ('a b', c)  ⚠️ 值带引号

        ⚠️ `buses=[...]` 这一种**必须单列**：多绕组变压器（`windings=2/3`）在
        OpenDSS 里就用这个复数写法，一次给出全部绕组端子。只认单数会漏掉它 ——
        真机 case31 实测：16 台变压器端点全空，而线路 264 条全正常，
        差别就在线路用 `bus1=/bus2=`、变压器用 `buses=[...]`。
        """
        out: list[str] = []
        for tok in self.tokens:
            if "=" not in tok:
                continue
            key, _, val = tok.partition("=")
            k = key.strip().lower()
            if k in ("bus", "bus1", "bus2"):
                b = _norm_bus(_unquote(val))
                if b:
                    out.append(b)
            elif k == "buses":
                # 形如 `buses=[GRID SUB110A]`、`buses=(799.1.2 799r.1.2)` 或
                # `buses="A,B"`：去掉外层括号/引号后按空白或逗号切分，逐个归一。
                # ⚠️ 圆括号写法是 OpenDSS 官方算例在用的（IEEE 37 节点：
                #    `new transformer.reg1a … buses=(799.1.2 799r.1.2)`）。
                #    此前只剥 `[]`，于是 `(799.1.2` 被当成母线名 ⇒ 归一成 `(799`
                #    去查母线表必然查不到，报「发现未解析的母线引用」整例失败。
                inner = _unquote(val.strip().strip("[]()").strip())
                for part in inner.replace(",", " ").split():
                    b = _norm_bus(_unquote(part))
                    if b:
                        out.append(b)
        return tuple(out)


@dataclass(frozen=True)
class DssSourceFacts:
    """一个算例目录（任务副本）解析出的全部事实。"""

    elements: tuple[DssElementDef, ...] = ()
    files: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())

    # ---------------------------------------------------------------- 查询
    def by_kind(self, kind: str) -> tuple[DssElementDef, ...]:
        """按类名精确取元件（`kind="line"` **不会**匹配 `linecode`）。"""
        k = kind.strip().lower()
        return tuple(e for e in self.elements if e.kind == k)

    def kind_counts(self) -> dict[str, int]:
        """各元素名去重后的计数（同名多次定义算一个）。"""
        seen: dict[str, str] = {}
        for e in self.elements:
            seen[e.full_name] = e.kind
        out: dict[str, int] = {}
        for kind in seen.values():
            out[kind] = out.get(kind, 0) + 1
        return out

    def count(self, kind: str) -> int:
        return len(self.by_kind(kind))

    def branch_status(self) -> tuple[int, int, int, int]:
        """(线路总数, 停用线路数, 变压器总数, 停用变压器数)。

        同名多次定义时**后写覆盖前写**（与 OpenDSS 的 Edit 语义一致），
        这与既有 `_scan_defined_branch_status` 的取值方式逐位一致。
        """
        line_states: dict[str, bool] = {}
        tr_states: dict[str, bool] = {}
        for e in self.elements:
            if e.kind == "line":
                line_states[e.full_name] = e.enabled
            elif e.kind == "transformer":
                tr_states[e.full_name] = e.enabled
        return (
            len(line_states),
            sum(1 for v in line_states.values() if not v),
            len(tr_states),
            sum(1 for v in tr_states.values() if not v),
        )

    def line_endpoints(self) -> tuple[tuple[bool, str, str], ...]:
        """逐条线路：(是否启用, 端点1, 端点2)。供拓扑画像使用。

        只包含**两个端点都解析出来**的线路：拓扑推演缺一个端点会算错环网秩，
        宁可少算一条也不能算错。
        """
        out: list[tuple[bool, str, str]] = []
        for e in self.by_kind("line"):
            buses = e.buses
            if len(buses) >= 2:
                out.append((e.enabled, buses[0], buses[1]))
        return tuple(out)

    def transformer_endpoints(self) -> dict[str, tuple[str, str]]:
        """变压器名（小写，含 `transformer.` 前缀）→ `(端点1, 端点2)`。

        与 `line_endpoints` 同源同规则：**只收两个端点都解析出来的**，
        缺一个端点的拓扑推演会算错环网秩与连通分量，宁可少算也不能算错。

        ⚠️ 这条查询存在的理由（真机 case31 实测）：COM 侧
        `ActiveCktElement.BusNames` / `BusNodes` 对 `windings=2` 的变压器
        **全部返回空**（同批线路 264 条端点都正常），而配电网馈线首端
        普遍经变压器挂在上级电网节点上 —— 缺了它，拓扑判据会把每条馈线
        算成独立分量，与"已收敛"直接矛盾。
        """
        out: dict[str, tuple[str, str]] = {}
        for e in self.by_kind("transformer"):
            buses = e.buses
            if len(buses) >= 2:
                out[e.full_name.lower()] = (buses[0], buses[1])
        return out

    def vsource_buses(self) -> frozenset[str]:
        """拥有独立电压基准的母线（零基准校验的豁免集）。

        `New Circuit.X` 隐含创建源母线：显式 `bus1=` 优先，否则按 OpenDSS 约定
        就是 `sourcebus`。`New Vsource.X` 则取其 `bus1`。
        """
        out: set[str] = set()
        for e in self.elements:
            if e.kind == "circuit":
                out.add(_norm_bus(e.get("bus1") or "sourcebus"))
            elif e.kind == "vsource":
                out.update(e.buses)
        out.discard("")
        return frozenset(out)


# --------------------------------------------------------------------------- 入口


def parse_dss_text(text: str, source_file: str = "") -> tuple[DssElementDef, ...]:
    """解析一段 DSS 文本，产出元件定义（按出现顺序）。"""
    out: list[DssElementDef] = []
    for lineno, line in _logical_lines(text):
        toks = _split_tokens(line)
        if len(toks) < 2:
            continue
        verb = toks[0].strip().lower()
        if verb not in ("new", "edit"):
            continue
        name_tok = toks[1].strip()
        if not name_tok or "=" in name_tok:
            continue
        if "." in name_tok:
            kind, _, short = name_tok.partition(".")
        else:
            # `New Line L1 …` 这种不带点号的写法也合法，OpenDSS 会补类名。
            # 第二 token 是属性时说明没写名字（如 `New Line bus1=…`）—— 跳过。
            piece = toks[2] if len(toks) > 2 else ""
            if "=" in piece:
                kind, short = name_tok, ""
            else:
                kind, short = name_tok, piece
        kind = kind.strip().lower()
        if not kind:
            continue
        props = toks[2:]
        if short and "." not in name_tok:
            props = toks[3:]
        out.append(
            DssElementDef(
                kind=kind,
                short_name=short.strip().lower(),
                tokens=tuple(props),
                source_file=source_file,
                source_line=lineno,
            )
        )
    return tuple(out)


def parse_dss_directory(root: str) -> DssSourceFacts:
    """解析目录下所有 `.dss`（递归），合并为一个事实集。

    文件名排序保证结果确定性（同一目录两次调用必须完全一致）。
    """
    if not root or not os.path.isdir(root):
        return DssSourceFacts(notes=(f"目录不存在：{root}",))

    elements: list[DssElementDef] = []
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            if not filename.lower().endswith(".dss"):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            files.append(path)
            elements.extend(parse_dss_text(text, source_file=path))
    return DssSourceFacts(elements=tuple(elements), files=tuple(files))
