"""严格校验转换输入；别名归一化由转换器完成，本模块不重命名列。

JSON 在构造 DataFrame 前校验，区分显式 NaN 和真正缺省。DataFrame 的
NaN/NA 单元格视为可选字段缺省；独立负荷每行仍必须有至少一个功率值。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import numbers
from typing import Any, Mapping

import numpy as np
import pandas as pd


def _key(value: Any) -> str:
    return str(value).strip().lower().replace(" ", "").replace("_", "")


_POWER_GROUPS = (
    (("p_kw", "load_kw"), ("p_load_mw", "pd_mw", "p_mw")),
    (("q_kvar", "load_kvar"), ("q_load_mvar", "qd_mvar", "q_mvar")),
    (("p_gen_kw",), ("p_gen_mw", "pg_mw")),
    (("q_gen_kvar",), ("q_gen_mvar", "qg_mvar")),
    (("gs_kw", "shunt_g_kw"), ("gs_mw", "gs", "shunt_g_mw")),
    (("bs_kvar", "shunt_b_kvar"), ("bs_mvar", "bs", "shunt_b_mvar")),
    (("qmin_kvar",), ("qmin_mvar", "qmin")),
    (("qmax_kvar",), ("qmax_mvar", "qmax")),
    (("gen_rating_kva", "generator_rating_kva"), ("gen_rating_mva", "generator_rating_mva")),
)
_LOAD_POWER_KEYS = {_key(name) for group in _POWER_GROUPS[:2] for aliases in group for name in aliases}
_NUMERIC_KEYS = {_key(name) for group in _POWER_GROUPS for aliases in group for name in aliases}
_POSITIVE_GROUPS = (
    ("base_kv", "basekv", "kv", "v_base_kv", "voltage_base_kv"),
    ("base_mva", "basemva", "s_base_mva"),
    ("frequency_hz",),
    ("v_set_pu", "vm_set_pu", "vm_pu", "vset_pu", "voltage_pu", "voltage_setpoint_pu"),
    ("tap_ratio", "ratio", "tap"),
    ("v_min_pu",), ("v_max_pu",),
    ("gen_rating_mva", "generator_rating_mva"),
    ("gen_rating_kva", "generator_rating_kva"),
)
_POSITIVE_KEYS = {_key(name) for group in _POSITIVE_GROUPS for name in group}
_SBASE_KEYS = {_key(name) for name in ("base_mva", "basemva", "s_base_mva")}
_THOUSAND_SCALE_KEYS = {_key(name) for _, aliases in _POWER_GROUPS for name in aliases}
_THOUSAND_SCALE_KEYS |= {_key(name) for name in ("qmin", "qmax", "qmin_mvar", "qmax_mvar", "rate_mva", "ratea_mva", "rating_mva")}
_NUMERIC_KEYS |= _POSITIVE_KEYS | {_key(name) for name in (
    "qmin", "qmax", "qmin_mvar", "qmax_mvar", "r_pu", "rpu", "x_pu", "xpu",
    "r_ohm", "r", "resistance_ohm", "x_ohm", "x", "reactance_ohm",
    "b_pu", "b", "charging_b_pu", "b_susceptance_pu", "rate_mva", "ratea_mva", "rating_mva",
    "shift_deg", "angle", "shift", "r0_pu", "r0pu", "r0", "x0_pu", "x0pu", "x0",
    "b0_pu", "b0pu", "b0", "r0_ohm", "r0_ohms", "x0_ohm", "x0_ohms",
)}
_STATUS_KEYS = {_key(name) for name in ("status", "in_service", "enabled")}
_STATUS_TOKENS = {"0", "1", "true", "false", "on", "off", "open", "closed", "enabled", "disabled", "yes", "no"}
_FLAG_KEYS = {_key("ENFORCE_Q_LIMITS")}
_JSON_IDENTIFIERS = {
    "bus": (("bus_id", "id", "bus", "bus_name", "name"),),
    "line": (("line_id", "id", "line", "branch_id", "name"),
             ("from_bus", "from", "fbus", "source"), ("to_bus", "to", "tbus", "target")),
    "load": (("load_id", "id", "load", "name"), ("bus_id", "bus", "bus_name", "node", "node_bus")),
    "loads": (("load_id", "id", "load", "name"), ("bus_id", "bus", "bus_name", "node", "node_bus")),
}


@dataclass(frozen=True)
class ValidatedTables:
    bus: pd.DataFrame
    line: pd.DataFrame
    load: pd.DataFrame | None
    system: dict[str, Any]


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
        return bool(missing) if not hasattr(missing, "__len__") else False
    except (TypeError, ValueError):
        return False


def _numeric(value: Any, label: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} 必须是有限数值，不能使用布尔值")
    if not pd.api.types.is_scalar(value):
        raise ValueError(f"{label} 必须是单个数值，不能使用数组/对象")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{label} 数值不能为空字符串")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} 不是数值：{value!r}") from None
    if not math.isfinite(parsed):
        raise ValueError(f"{label} 必须是有限数值：{value!r}")
    return parsed


def _consistent(values: list[tuple[str, float]], label: str) -> None:
    if not values:
        return
    first_name, first = values[0]
    for name, value in values[1:]:
        if not math.isclose(value, first, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f"{label} 单位/别名字段冲突：{first_name} 与 {name} 表示的数值不一致")


def _boolean_flag(value: Any, label: str) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    token = str(value).strip().lower()
    if token in {"true", "yes", "on"}:
        return True
    if token in {"false", "no", "off"}:
        return False
    parsed = _numeric(value, label)
    if parsed not in (0, 1):
        raise ValueError(f"{label} 布尔开关仅支持 0/1、true/false、yes/no、on/off")
    return bool(parsed)


def _validate_numeric_row(row: Mapping, label: str) -> dict:
    output = dict(row)
    present: dict[str, float] = {}
    for name, value in row.items():
        key = _key(name)
        if key in _FLAG_KEYS and not _missing(value):
            output[name] = _boolean_flag(value, f"{label}.{name}")
        if key in _NUMERIC_KEYS and not _missing(value):
            parsed = _numeric(value, f"{label}.{name}")
            if key in _POSITIVE_KEYS and parsed <= 0:
                raise ValueError(f"{label}.{name} 必须大于 0")
            if key in _SBASE_KEYS and not math.isfinite(parsed * 1_000_000):
                raise ValueError(f"{label}.{name} 缩放为源容量后不是有限数值")
            if key in _THOUSAND_SCALE_KEYS and not math.isfinite(parsed * 1000):
                raise ValueError(f"{label}.{name} 功率单位换算后不是有限数值")
            present[name] = parsed
            output[name] = parsed
        if key in _STATUS_KEYS and not _missing(value):
            if isinstance(value, (bool, np.bool_)):
                continue
            if isinstance(value, numbers.Real):
                parsed = _numeric(value, f"{label}.{name}")
                if parsed not in (0, 1):
                    raise ValueError(f"{label}.{name} 状态仅支持 0/1 或启停标记")
            elif str(value).strip().lower() not in _STATUS_TOKENS:
                raise ValueError(f"{label}.{name} 不支持的启停状态：{value!r}")
    for kw_aliases, mw_aliases in _POWER_GROUPS:
        scales = {_key(name): 1.0 for name in kw_aliases}
        scales.update({_key(name): 1000.0 for name in mw_aliases})
        values = [(name, value * scales[_key(name)]) for name, value in present.items() if _key(name) in scales]
        if any(not math.isfinite(value) for _, value in values):
            raise ValueError(f"{label} 功率单位换算后不是有限数值")
        _consistent(values, label)
    for group in _POSITIVE_GROUPS:
        keys = {_key(name) for name in group}
        _consistent([(name, value) for name, value in present.items() if _key(name) in keys], label)
    return output


def _json_finite(value: Any, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} 必须是有限数值，JSON 不允许显式 NaN/Infinity")
    if isinstance(value, dict):
        for name, item in value.items():
            _json_finite(item, f"{label}.{name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _json_finite(item, f"{label}[{index}]")


def validate_json_payload(data: Any) -> None:
    """在 DataFrame 构造前拒绝非法 JSON 结构/显式数值；不修改输入。

    未给 load/loads 表示派生 Bus 负荷；显式 [] 表示独立空负荷表。
    system 可为结构化对象或对象数组（如 parameter/value 表）。
    """
    if not isinstance(data, dict):
        raise ValueError("json 顶层必须是对象")
    _json_finite(data, "json")
    for section in ("bus", "line"):
        if section not in data or not isinstance(data[section], list):
            raise ValueError(f"json 字段 {section} 必须是对象数组")
    if not data["bus"]:
        raise ValueError("json bus 数组不能为空")
    if "load" in data and "loads" in data:
        raise ValueError("json 不得同时提供 load 和 loads 两个负荷表")
    sections = [(name, data[name]) for name in ("bus", "line", "load", "loads") if name in data]
    for section, rows in sections:
        if not isinstance(rows, list):
            raise ValueError(f"json 字段 {section} 必须是对象数组；空负荷请明确填写 []")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"json {section}[{index}] 必须是对象")
            _validate_numeric_row(row, f"json {section}[{index}]")
            keys = {_key(name): name for name in row}
            for aliases in _JSON_IDENTIFIERS[section]:
                selected = next((keys[_key(alias)] for alias in aliases if _key(alias) in keys), None)
                if selected is not None and row[selected] is not None:
                    _identifier(row[selected], f"json {section}[{index}].{selected}",
                                bus=aliases[0] in {"bus_id", "from_bus", "to_bus"})
    if "system" in data:
        system = data["system"]
        if isinstance(system, dict):
            rows = [system]
        elif isinstance(system, list) and all(isinstance(row, dict) for row in system):
            rows = system
        else:
            raise ValueError("json system 必须是对象或对象数组")
        for index, row in enumerate(rows):
            label = f"json system[{index}]"
            _validate_numeric_row(row, label)
            key = row.get("parameter", row.get("key"))
            if key is not None and "value" in row:
                _validate_numeric_row({str(key): row["value"]}, label)


def _identifier(value: Any, label: str, *, bus: bool = False) -> str:
    if not pd.api.types.is_scalar(value):
        raise ValueError(f"{label} 标识必须是单个值")
    if _missing(value) or not str(value).strip():
        raise ValueError(f"{label} 标识不能为空")
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} 标识不能是布尔值")
    text = str(value).strip()
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        numeric = None
    if numeric is not None:
        if not numeric.is_finite():
            raise ValueError(f"{label} 标识不能是 NaN/Infinity")
        # 数值型整数 ID 不能经 float 舍入，尤其是超过 2**53 的设备编号。
        # 超长数值名称保留文本，避免把简短指数名称展开成巨大整数。
        if numeric.adjusted() <= 308 and numeric == numeric.to_integral_value():
            text = str(int(numeric))
    if (any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text)
            or any(character in text for character in "!;=,[]()\"'?") or "//" in text):
        raise ValueError(f"{label} 标识含 DSS 结构分隔符/注释或内部空白，请使用简单标识")
    if bus and "." in text:
        raise ValueError(f"{label} 母线标识不能含点号节点后缀；请通过 phase 字段声明相别")
    return text


def _frame(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"{label} 必须是 DataFrame")
    if frame.columns.duplicated().any():
        raise ValueError(f"{label} 含重复列名")
    output = frame.copy(deep=True)
    numeric_columns = {name: [] for name in output.columns if _key(name) in _NUMERIC_KEYS}
    for position in range(len(output)):
        row = output.iloc[position].to_dict()
        validated = _validate_numeric_row(row, f"{label} 第 {position + 1} 行")
        for name in numeric_columns:
            numeric_columns[name].append(validated[name])
    # 整列赋值兼容 pandas 字符串扩展 dtype；按位置构造避免重复索引误写。
    for name, values in numeric_columns.items():
        output[name] = values
    return output


def _ids(frame: pd.DataFrame, column: str, label: str, auto: bool = False) -> pd.DataFrame:
    output = frame.copy(deep=True)
    if column not in output.columns and not auto:
        raise ValueError(f"{label} 缺少标识列 {column}")
    values = output[column].tolist() if column in output.columns else [None] * len(output)
    names: list[str | None] = []
    used: set[str] = set()
    for position, value in enumerate(values):
        if auto and _missing(value):
            names.append(None)
            continue
        name = _identifier(value, f"{label} 第 {position + 1} 行 {column}", bus=column == "bus_id")
        if name.casefold() in used:
            raise ValueError(f"{label} 含重复/大小写碰撞标识：{name}")
        used.add(name.casefold())
        names.append(name)
    counter = 1
    for position, name in enumerate(names):
        if name is not None:
            continue
        while f"LD{counter}".casefold() in used:
            counter += 1
        names[position] = f"LD{counter}"
        used.add(names[position].casefold())
        counter += 1
    output[column] = names
    return output


def validate_tables(
    bus: pd.DataFrame, line: pd.DataFrame, load: pd.DataFrame | None = None,
    *, system: Mapping[str, Any] | None = None,
) -> ValidatedTables:
    """校验转换器归一化后的表，返回副本及规范母线引用。

    bus_id/line_id 必须非空且唯一；缺省负荷 ID 安全自动编号。显式空
    load 表保留为空，None 才表示未提供。SOURCE 是允许的虚拟源引用。
    """
    bus_out = _ids(_frame(bus, "Bus 表"), "bus_id", "Bus 表")
    if bus_out.empty:
        raise ValueError("Bus 表不能为空")
    line_out = _ids(_frame(line, "Line 表"), "line_id", "Line 表")
    bus_names = {name.casefold(): name for name in bus_out["bus_id"]}

    def reference(value: Any, label: str) -> str:
        name = _identifier(value, label, bus=True)
        if name.casefold() in bus_names:
            return bus_names[name.casefold()]
        if name.upper() == "SOURCE":
            return "SOURCE"
        raise ValueError(f"{label} 引用了不存在的母线：{name}")

    for column in ("from_bus", "to_bus"):
        if column not in line_out.columns:
            raise ValueError(f"Line 表缺少母线引用列 {column}")
        line_out[column] = [reference(value, f"Line 表第 {position + 1} 行 {column}")
                            for position, value in enumerate(line_out[column])]
    load_out = None
    if load is not None:
        load_out = _frame(load, "Load 表")
        if not load_out.empty:
            if "bus_id" not in load_out.columns:
                raise ValueError("Load 表缺少母线引用列 bus_id")
            columns = [column for column in load_out.columns if _key(column) in _LOAD_POWER_KEYS]
            if not columns:
                raise ValueError("非空 Load 表至少需要 p_kw/q_kvar 或 p_load_mw/q_load_mvar 功率列")
            for position in range(len(load_out)):
                if all(_missing(load_out.iloc[position][column]) for column in columns):
                    raise ValueError(f"Load 表第 {position + 1} 行功率全部缺省；显式零负荷请填写 0")
            load_out = _ids(load_out, "load_id", "Load 表", auto=True)
            load_out["bus_id"] = [reference(value, f"Load 表第 {position + 1} 行 bus_id")
                                   for position, value in enumerate(load_out["bus_id"])]
    if system is not None and not isinstance(system, Mapping):
        raise ValueError("system 参数必须是映射")
    system_out = _validate_numeric_row(dict(system or {}), "System 参数")
    return ValidatedTables(bus_out, line_out, load_out, system_out)
