"""Parse a documented, non-executable subset of MATPOWER v2 case files.

Only one literal assignment per field is accepted. Numeric matrices support
semicolon/newline rows, comma/space columns, scientific notation, MATLAB ``...``
continuations and percent comments. MATLAB expressions, indexing, functions and
dynamic modifications are rejected; this module never evaluates MATLAB code.

Historical short rows remain supported (bus >= 10, gen >= 3, branch >= 4), with
explicit warnings for missing optional input fields. Standard numeric gencost
and areas are accepted as declared, unused metadata for power-flow conversion.
Integer identities/statuses are restricted to the safe binary64 integer range
(absolute value <= 2**53-1); larger IDs are rejected rather than rounded.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re


class MatpowerParseError(ValueError):
    """The input cannot be interpreted reliably within the literal subset."""


@dataclass(frozen=True)
class MatpowerCase:
    base_mva: float
    bus: tuple[tuple[float, ...], ...]
    branch: tuple[tuple[float, ...], ...]
    gen: tuple[tuple[float, ...], ...]
    version: str = "2"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    line: int
    column: int
    start: int
    end: int


_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_MATRIX_FIELDS = {"bus", "branch", "gen", "gencost", "areas"}
_FIELDS = _MATRIX_FIELDS | {"version", "baseMVA"}
_MAX_EXACT_INTEGER = 2 ** 53 - 1


def _error(source: str, token: _Token, detail: str) -> MatpowerParseError:
    return MatpowerParseError(f"{source}:{token.line}:{token.column}: {detail}")


def _lex(text: str, source: str) -> list[_Token]:
    tokens: list[_Token] = []
    position, line, column = 0, 1, 1
    block_comment = 0

    def standalone_marker(marker: str) -> bool:
        if not text.startswith(marker, position):
            return False
        begin = text.rfind("\n", 0, position) + 1
        end = text.find("\n", position)
        return not text[begin:position].strip() and not text[position + 2:end if end >= 0 else len(text)].strip()

    while position < len(text):
        char = text[position]
        if block_comment:
            if standalone_marker("%}"):
                block_comment -= 1
                position += 2
                column += 2
            elif standalone_marker("%{"):
                block_comment += 1
                position += 2
                column += 2
            elif char == "\n":
                tokens.append(_Token("newline", char, line, column, position, position + 1))
                line, column, position = line + 1, 1, position + 1
            else:
                position += 1
                column += 1
            continue
        if char in " \t\r":
            position += 1
            column += 1
            continue
        if char == "\n":
            tokens.append(_Token("newline", char, line, column, position, position + 1))
            line, column, position = line + 1, 1, position + 1
            continue
        if standalone_marker("%{"):
            block_comment = 1
            position += 2
            column += 2
            continue
        if char == "%" or text.startswith("...", position):
            continuation = char != "%"
            end = text.find("\n", position)
            if end < 0:
                if continuation:
                    raise _error(source, _Token("...", "...", line, column, position, position + 3),
                                 "MATLAB ... 续行缺少下一行")
                column += len(text) - position
                position = len(text)
            elif continuation:
                position, line, column = end + 1, line + 1, 1
            else:
                column += end - position
                position = end
            continue
        start, start_line, start_column = position, line, column
        if char in "'\"":
            quote = char
            position += 1
            value: list[str] = []
            while position < len(text):
                if text[position] == "\n":
                    raise _error(source, _Token("string", "", start_line, start_column, start, position),
                                 "字符串不允许跨行")
                if text[position] == quote:
                    if position + 1 < len(text) and text[position + 1] == quote:
                        value.append(quote)
                        position += 2
                        continue
                    position += 1
                    break
                value.append(text[position])
                position += 1
            else:
                raise _error(source, _Token("string", "", start_line, start_column, start, position), "字符串未闭合")
            column += position - start
            tokens.append(_Token("string", "".join(value), start_line, start_column, start, position))
            continue
        match = _NUMBER.match(text, position)
        if match:
            position = match.end()
            column += position - start
            tokens.append(_Token("number", match.group(), start_line, start_column, start, position))
            continue
        match = _NAME.match(text, position)
        if match:
            position = match.end()
            column += position - start
            tokens.append(_Token("name", match.group(), start_line, start_column, start, position))
            continue
        if char in "=[](),;.":
            position += 1
            column += 1
            tokens.append(_Token(char, char, start_line, start_column, start, position))
            continue
        raise _error(source, _Token("invalid", char, line, column, position, position + 1),
                     f"不支持 MATLAB 字符/表达式 {char!r}；仅接受静态字面值")
    eof = _Token("eof", "", line, column, len(text), len(text))
    if block_comment:
        raise _error(source, eof, "MATLAB 块注释 %{ 未闭合")
    tokens.append(eof)
    return tokens


class _Parser:
    def __init__(self, text: str, source: str):
        self.source = source
        self.tokens = _lex(text, source)
        self.position = 0
        self.rows: dict[str, list[list[float]]] = {}
        self.row_tokens: dict[str, list[_Token]] = {}
        self.scalars: dict[str, float | str] = {}
        self.assignments: dict[str, _Token] = {}
        self.warnings: list[str] = []
        self.function_header = False

    @property
    def token(self) -> _Token:
        return self.tokens[self.position]

    def take(self, kind: str, value: str | None = None) -> _Token:
        token = self.token
        if token.kind != kind or (value is not None and token.value != value):
            raise _error(self.source, token, f"期望 {value or kind}，实际为 {token.value or token.kind}；仅支持静态字面值赋值")
        self.position += 1
        return token

    def number(self) -> tuple[float, _Token]:
        token = self.take("number")
        value = float(token.value)
        if not math.isfinite(value):
            raise _error(self.source, token, "数值必须有限，不能是 NaN/Inf 或溢出的科学记数值")
        return value, token

    def separators(self) -> None:
        while self.token.kind in {"newline", ";"}:
            self.position += 1

    def header(self) -> None:
        self.take("name", "function")
        if self.token.kind == "[":
            self.take("[")
            self.take("name", "mpc")
            self.take("]")
        else:
            self.take("name", "mpc")
        self.take("=")
        self.take("name")
        if self.token.kind == "(":
            self.take("(")
            self.take(")")
        if self.token.kind not in {"newline", ";", "eof"}:
            raise _error(self.source, self.token, "仅支持无参数的 function mpc = case_name 头")
        self.function_header = True
        self.separators()

    def matrix(self, field: str) -> None:
        self.take("[")
        rows: list[list[float]] = []
        origins: list[_Token] = []
        current: list[float] = []
        origin: _Token | None = None
        previous: _Token | None = None
        comma_pending = False
        while self.token.kind != "]":
            token = self.token
            if token.kind == "eof":
                raise _error(self.source, token, f"mpc.{field} 矩阵未闭合")
            if token.kind in {"newline", ";"}:
                if comma_pending:
                    raise _error(self.source, token, f"mpc.{field} 逗号后缺少数值；跨物理行请使用 ... 续行")
                if current:
                    rows.append(current)
                    origins.append(origin)
                current, origin, previous = [], None, None
                self.position += 1
                continue
            if token.kind == ",":
                if not current or comma_pending:
                    raise _error(self.source, token, f"mpc.{field} 存在空数值列")
                comma_pending = True
                self.position += 1
                continue
            if previous and not comma_pending and token.start == previous.end:
                raise _error(self.source, token, f"mpc.{field} 数值之间缺少分隔符，不执行 MATLAB 表达式")
            value, number_token = self.number()
            if origin is None:
                origin = number_token
            current.append(value)
            previous, comma_pending = number_token, False
        if comma_pending:
            raise _error(self.source, self.token, f"mpc.{field} 逗号后缺少数值")
        if current:
            rows.append(current)
            origins.append(origin)
        self.take("]")
        if rows and any(len(row) != len(rows[0]) for row in rows):
            raise _error(self.source, self.assignments[field], f"mpc.{field} 各行列数不一致，不能跳行或补列")
        self.rows[field], self.row_tokens[field] = rows, origins

    def parse(self) -> MatpowerCase:
        self.separators()
        if self.token.kind == "name" and self.token.value == "function":
            self.header()
        while self.token.kind != "eof":
            if self.token.kind == "name" and self.token.value == "end":
                if not self.function_header:
                    raise _error(self.source, self.token, "没有 function 头却出现 end")
                self.position += 1
                self.separators()
                if self.token.kind != "eof":
                    raise _error(self.source, self.token, "function end 之后存在不支持的语句")
                break
            self.take("name", "mpc")
            self.take(".")
            field_token = self.take("name")
            field = field_token.value
            if field not in _FIELDS:
                raise _error(self.source, field_token, f"不支持 mpc.{field}，不能可靠转换的字段必须先处理，不能静默忽略")
            if field in self.assignments:
                raise _error(self.source, field_token, f"mpc.{field} 重复赋值；不支持 MATLAB 后续修改/覆盖")
            self.assignments[field] = field_token
            self.take("=")
            if field in _MATRIX_FIELDS:
                self.matrix(field)
            elif field == "version" and self.token.kind == "string":
                self.scalars[field] = self.take("string").value
            else:
                self.scalars[field], _ = self.number()
            if self.token.kind not in {";", "newline", "eof"}:
                raise _error(self.source, self.token, f"mpc.{field} 赋值末尾存在表达式或动态代码")
            self.separators()
        return self.validate()

    def validate(self) -> MatpowerCase:
        for field in ("bus", "branch"):
            if field not in self.rows:
                raise _error(self.source, self.token, f"缺少 mpc.{field} 静态矩阵")
        if not self.rows["bus"]:
            raise _error(self.source, self.assignments["bus"], "mpc.bus 不能为空")
        version = str(self.scalars.get("version", "2"))
        if version not in {"2", "2.0"}:
            raise _error(self.source, self.assignments["version"], "仅支持 MATPOWER version=2")
        base_mva = float(self.scalars.get("baseMVA", 100.0))
        if base_mva <= 0:
            raise _error(self.source, self.assignments["baseMVA"], "mpc.baseMVA 必须是正有限数值")
        if "baseMVA" not in self.scalars:
            self.warnings.append("mpc.baseMVA 未提供，沿用历史缺省 100 MVA。")
        if "version" not in self.scalars:
            self.warnings.append("mpc.version 未提供，按 MATPOWER v2 字段顺序解释。")
        if "gen" not in self.rows:
            self.warnings.append("mpc.gen 未提供，没有显式机组记录。")
        minimums = {"bus": 10, "branch": 4, "gen": 3}
        for field, minimum in minimums.items():
            for index, row in enumerate(self.rows.get(field, [])):
                if len(row) < minimum:
                    raise _error(self.source, self.row_tokens[field][index], f"mpc.{field} 第 {index + 1} 行列数不足，至少 {minimum} 列")
        for field, standard in (("bus", 13), ("gen", 21), ("branch", 13)):
            rows = self.rows.get(field, [])
            if rows and len(rows[0]) < standard:
                self.warnings.append(f"mpc.{field} 使用历史短格式（{len(rows[0])} 列，标准输入 {standard} 列）；未给出的可选字段沿用转换器缺省。")
        bus_ids: set[int] = set()
        for index, row in enumerate(self.rows["bus"]):
            bus_id = self.integer(row[0], "bus", index, "BUS_I", positive=True)
            if bus_id in bus_ids:
                raise _error(self.source, self.row_tokens["bus"][index], f"重复母线 ID {bus_id}")
            bus_ids.add(bus_id)
            bus_type = self.integer(row[1], "bus", index, "BUS_TYPE")
            if bus_type not in {1, 2, 3, 4}:
                raise _error(self.source, self.row_tokens["bus"][index], f"BUS_TYPE 必须为 1/2/3/4，实际 {bus_type}")
            if row[9] <= 0:
                raise _error(self.source, self.row_tokens["bus"][index], "BASE_KV 必须大于 0，不能静默替换电压基准")
        for index, row in enumerate(self.rows.get("gen", [])):
            bus_id = self.integer(row[0], "gen", index, "GEN_BUS", positive=True)
            if bus_id not in bus_ids:
                raise _error(self.source, self.row_tokens["gen"][index], f"机组引用未知母线 {bus_id}")
            if len(row) >= 8:
                self.integer(row[7], "gen", index, "GEN_STATUS")
            else:
                self.warnings.append(f"mpc.gen 第 {index + 1} 行未提供 GEN_STATUS，按历史在线缺省处理。")
        for index, row in enumerate(self.rows["branch"]):
            for column, label in ((0, "F_BUS"), (1, "T_BUS")):
                bus_id = self.integer(row[column], "branch", index, label, positive=True)
                if bus_id not in bus_ids:
                    raise _error(self.source, self.row_tokens["branch"][index], f"支路引用未知母线 {bus_id}")
            if len(row) >= 11:
                status = self.integer(row[10], "branch", index, "BR_STATUS")
                if status not in {0, 1}:
                    raise _error(self.source, self.row_tokens["branch"][index], "BR_STATUS 必须为 0 或 1")
            else:
                self.warnings.append(f"mpc.branch 第 {index + 1} 行未提供 BR_STATUS，按历史在线缺省处理。")
            if len(row) > 8 and row[8] < 0:
                raise _error(self.source, self.row_tokens["branch"][index], "TAP 不能为负；0 表示无非标变比")
        for field in ("gencost", "areas"):
            if field in self.rows:
                self.warnings.append(f"mpc.{field} 静态数值矩阵已读取；该非潮流输入不映射至 OpenDSS。")
        return MatpowerCase(base_mva, tuple(map(tuple, self.rows["bus"])),
                            tuple(map(tuple, self.rows["branch"])),
                            tuple(map(tuple, self.rows.get("gen", []))), warnings=tuple(self.warnings))

    def integer(self, value: float, field: str, index: int, label: str, *, positive: bool = False) -> int:
        if not value.is_integer() or abs(value) > _MAX_EXACT_INTEGER or (positive and value <= 0):
            qualifier = "正" if positive else ""
            raise _error(self.source, self.row_tokens[field][index], f"{label} 必须为可精确表示的{qualifier}整数，实际 {value:g}")
        return int(value)


def parse_matpower_case(path: str | Path) -> MatpowerCase:
    """Read/validate literal MATPOWER v2 rows; raise ValueError on ambiguity.

    No MATLAB process, Python evaluation or application engine is invoked.
    Rows retain their original columns; downstream mapping decides which declared
    physical fields it supports and must propagate ``warnings`` to its product.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise MatpowerParseError(f"{source}: MATPOWER 输入必须为 UTF-8 文本，不能忽略损坏字节") from exc
    return _Parser(text, str(source)).parse()
