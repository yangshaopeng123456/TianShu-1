"""配电网算例格式统一转换器。

将 xlsx / csv / json / MATPOWER .m 等常见格式转换为 OpenDSS 可执行的
Master.dss。转换器只写入任务上传目录，不修改用户原始算例。

**相别（分相）建模**：输入可声明每个元件的相数（1/2/3）与相别身份
（接在哪一相），据此生成分相节点与 `Phases=k`，从而在非 `.dss` 输入上
也能得到真实的三相不平衡模型。列名归一化与相标解析见 `phase_model`。
没有相别信息时使用平衡三相等值，并在产物中声明转换假设。
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import tempfile
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from . import phase_model as pm
from .conversion_validation import validate_json_payload, validate_tables
from .matpower_parser import parse_matpower_case


_NUMBER_RE = r"(?:1e[-+]?\d+|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"

# 零阻抗判定阈值（单位 pu）。**只拦严格为零**，刻意不拦"非常小但非零" ——
# 后者是合法数据，接近奇异但可由求解器按容差处理；提前拒绝等于把
# 用户能用的算例挡在门外。单点定义：判据与对外解释共用这一个数。
ZERO_IMPEDANCE_TOL = 1e-12


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy().reset_index(drop=True)
    result.columns = [str(c).strip().lower().replace(" ", "_") for c in result.columns]
    duplicates = result.columns[result.columns.duplicated()].tolist()
    if duplicates:
        raise ValueError(f"归一化后出现重复字段：{', '.join(duplicates)}")
    return result


def _first_existing(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _rename_first(df: pd.DataFrame, target: str, names: tuple[str, ...]) -> None:
    if target in df.columns:
        return
    source = _first_existing(df, names)
    if source and source != target:
        df.rename(columns={source: target}, inplace=True)


def _normalize_bus_frame(df: pd.DataFrame) -> pd.DataFrame:
    result = _normalize_columns(df)
    _rename_first(result, "bus_id", ("id", "bus", "bus_name", "name", "bus_id"))
    # 混合 CSV 的 type 是记录类别（bus/line/system），真正的电气母线类型在
    # bus_type/type_code 中；不能把所有母线都误归为普通 bus。
    if "bus_type" in result.columns and (
        "type" not in result.columns
        or result["type"].astype(str).str.strip().str.lower().isin(
            {"bus", "node", "母线"}
        ).all()
    ):
        result["type"] = result["bus_type"]
    _rename_first(result, "type", ("bus_type", "type_code", "bustype", "type"))
    _rename_first(result, "base_kv", ("kv", "basekv", "base_kv"))
    _rename_first(result, "p_load_mw", ("pd_mw", "p_mw", "p_load_mw"))
    _rename_first(result, "q_load_mvar", ("qd_mvar", "q_mvar", "q_load_mvar"))
    _rename_first(result, "p_kw", ("load_kw", "p_kw"))
    _rename_first(result, "q_kvar", ("load_kvar", "q_kvar"))
    _rename_first(result, "p_gen_mw", ("pg_mw", "p_gen_mw"))
    _rename_first(result, "q_gen_mvar", ("qg_mvar", "q_gen_mvar"))
    # 电压设定列在各格式里叫法不同：表格式算例用 vm_set_pu，MATPOWER 走自己的解析分支。
    # 此前别名表里没有 vm_set_pu → 表格算例的源电压全部回落 1.0（实测 case09 表格里
    # 明明是 1.04，产物却是 pu=1），而参考值 vmax 就是 1.04。
    _rename_first(
        result, "v_set_pu",
        ("vm_set_pu", "vm_pu", "vset_pu", "voltage_pu", "voltage_setpoint_pu", "v_set_pu"),
    )
    _rename_first(result, "qmin_mvar", ("qmin", "qmin_mvar"))
    _rename_first(result, "qmax_mvar", ("qmax", "qmax_mvar"))
    _rename_first(result, "gs_mw", ("gs", "shunt_g_mw", "gs_mw"))
    _rename_first(result, "bs_mvar", ("bs", "shunt_b_mvar", "bs_mvar"))
    _rename_first(result, "gs_kw", ("shunt_g_kw", "gs_kw"))
    _rename_first(result, "bs_kvar", ("shunt_b_kvar", "bs_kvar"))
    _rename_first(result, "gen_rating_mva", ("generator_rating_mva", "gen_rating_mva"))
    _rename_first(result, "gen_rating_kva", ("generator_rating_kva", "gen_rating_kva"))
    # 相别：相数（1/2/3）与相别身份（接在哪一相）。两者都可缺省——
    # 都缺省时按旧的平衡三相等值建模，不产生任何假设。
    _rename_first(result, "phases", pm.PHASE_COUNT_ALIASES)
    _rename_first(result, "phase", pm.PHASE_ID_ALIASES)
    _rename_first(result, "conn", pm.CONN_ALIASES)
    if "bus_id" not in result.columns:
        raise ValueError("Bus 表缺少母线标识列，支持：bus_id、id、name")
    valid_types = {"slack", "3", "ref", "reference", "pv", "2", "pq", "1", "load", "swing"}
    if "type" in result.columns:
        for idx, value in result["type"].items():
            if _is_missing_scalar(value):
                result.at[idx, "type"] = "pq"
                continue
            normalized = str(value).strip().lower()
            if normalized.endswith(".0"):
                normalized = normalized[:-2]
            if normalized and normalized not in valid_types:
                raise ValueError(f"母线 {_id_text(result.at[idx, 'bus_id'])} 的 bus_type/type_code 不支持：{value!r}")
            result.at[idx, "type"] = ("slack" if normalized in {"slack", "3", "ref", "reference", "swing"}
                                      else "pv" if normalized in {"pv", "2"} else "pq")
    if "type_code" in result.columns:
        for idx, value in result["type_code"].items():
            if _is_missing_scalar(value):
                continue
            normalized = str(value).strip().lower()
            if normalized.endswith(".0"):
                normalized = normalized[:-2]
            if normalized not in {"1", "2", "3"}:
                raise ValueError(f"母线 {_id_text(result.at[idx, 'bus_id'])} 的 type_code 不支持：{value!r}")
    return result


def _normalize_line_frame(df: pd.DataFrame) -> pd.DataFrame:
    result = _normalize_columns(df)
    if result.empty and not len(result.columns):
        return pd.DataFrame(columns=["line_id", "from_bus", "to_bus", "r_ohm", "x_ohm"])
    _rename_first(result, "line_id", ("id", "line", "branch_id", "name", "line_id"))
    _rename_first(result, "from_bus", ("from", "fbus", "source", "from_bus"))
    _rename_first(result, "to_bus", ("to", "tbus", "target", "to_bus"))
    _rename_first(result, "r_pu", ("rpu", "r_pu"))
    _rename_first(result, "x_pu", ("xpu", "x_pu"))
    # 线路充电电纳（对地）：不建模会把线路容性无功支撑整体抹掉，电压偏低。
    _rename_first(result, "b_pu", ("b_pu", "b", "charging_b_pu", "b_susceptance_pu"))
    _rename_first(result, "r_ohm", ("r", "resistance_ohm", "r_ohm"))
    _rename_first(result, "x_ohm", ("x", "reactance_ohm", "x_ohm"))
    _rename_first(result, "status", ("in_service", "enabled", "status"))
    _rename_first(result, "kind", ("type", "element_type", "kind"))
    _rename_first(result, "rate_mva", ("ratea_mva", "rating_mva", "rate_mva"))
    _rename_first(result, "tap_ratio", ("ratio", "tap", "tap_ratio"))
    _rename_first(result, "shift_deg", ("angle", "shift", "shift_deg"))
    # 相别与零序参数：单相/两相支路必须给出相别身份，零序参数用于三相不平衡
    # 建模（OpenDSS 的 R0/X0/C0，Kron 等值）；缺省时不输出这些关键字。
    _rename_first(result, "phases", pm.PHASE_COUNT_ALIASES)
    _rename_first(result, "phase", pm.PHASE_ID_ALIASES)
    _rename_first(result, "conn", pm.CONN_ALIASES)
    _rename_first(result, "conn1", pm.CONN_PRIMARY_ALIASES)
    _rename_first(result, "conn2", pm.CONN_SECONDARY_ALIASES)
    for canonical, aliases in {**pm.ZERO_SEQ_PU_ALIASES, **pm.ZERO_SEQ_OHM_ALIASES}.items():
        _rename_first(result, canonical, aliases)
    if "line_id" not in result.columns:
        result["line_id"] = [f"L{i}" for i in range(1, len(result) + 1)]
    if "from_bus" not in result.columns or "to_bus" not in result.columns:
        raise ValueError("Line 表缺少起止母线列，支持：from_bus/to_bus、from/to")
    if not ("r_ohm" in result.columns and "x_ohm" in result.columns) and not (
        "r_pu" in result.columns and "x_pu" in result.columns
    ):
        raise ValueError("Line 表缺少阻抗列，支持：r_ohm/x_ohm 或 r_pu/x_pu")
    if "tap_ratio" in result.columns:
        for idx, value in result["tap_ratio"].items():
            if _is_missing_scalar(value):
                continue
            tap = _require_numeric(value, "tap_ratio", result.at[idx, "line_id"])
            if tap <= 0:
                raise ValueError(f"支路 {result.at[idx, 'line_id']} 的 tap_ratio 必须大于 0：{tap:.16g}")
    if "shift_deg" in result.columns:
        for idx, value in result["shift_deg"].items():
            if not _is_missing_scalar(value):
                _require_numeric(value, "shift_deg", result.at[idx, "line_id"])
    return result


def _normalize_load_frame(df: pd.DataFrame) -> pd.DataFrame:
    """独立负荷表归一化：每行一个负荷，可带相别（不平衡算例的常规表达方式）。"""
    result = _normalize_columns(df)
    _rename_first(result, "load_id", ("id", "load", "load_id", "name"))
    _rename_first(result, "bus_id", ("bus", "bus_id", "bus_name", "node", "node_bus"))
    _rename_first(result, "p_load_mw", ("pd_mw", "p_mw", "p_load_mw"))
    _rename_first(result, "q_load_mvar", ("qd_mvar", "q_mvar", "q_load_mvar"))
    _rename_first(result, "p_kw", ("load_kw", "p_kw"))
    _rename_first(result, "q_kvar", ("load_kvar", "q_kvar"))
    _rename_first(result, "base_kv", ("kv", "basekv", "base_kv"))
    _rename_first(result, "phases", pm.PHASE_COUNT_ALIASES)
    _rename_first(result, "phase", pm.PHASE_ID_ALIASES)
    _rename_first(result, "conn", pm.CONN_ALIASES)
    if "bus_id" not in result.columns:
        raise ValueError("负荷表缺少母线列，支持：bus_id、bus")
    return result


def _id_text(value: Any) -> str:
    """把 Excel/CSV 中的 1.0 这类数值 ID 规范成稳定的 1。"""
    if value is None:
        return ""
    try:
        number = Decimal(str(value).strip())
        if number.is_finite() and number.adjusted() <= 308 and number == number.to_integral_value():
            return str(int(number))
    except (TypeError, ValueError, InvalidOperation):
        pass
    return str(value).strip()


def _is_missing_scalar(value: Any) -> bool:
    if value is None:
        return True
    try:
        result = pd.isna(value)
        return bool(result) if not hasattr(result, "__len__") else False
    except (TypeError, ValueError):
        return False


def _to_float(value: Any, default: float = 0.0) -> float:
    if _is_missing_scalar(value):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"转换数值不是合法数字：{value!r}") from None
    if isinstance(value, bool) or not math.isfinite(parsed):
        raise ValueError(f"转换数值必须是有限数字：{value!r}")
    return parsed


def _require_numeric(value: Any, field: str, line_id: Any) -> float:
    """解析阻抗等关键字段；非数值必须显式报字段级错误，不能静默变成 0。"""
    if _is_missing_scalar(value):
        raise ValueError(f"支路 {line_id} 的字段 {field} 为空")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"支路 {line_id} 的字段 {field} 不是数值：{value!r}") from None
    if isinstance(value, bool) or not math.isfinite(parsed):
        raise ValueError(f"支路 {line_id} 的字段 {field} 必须是有限数字：{value!r}")
    return parsed


def _impedance_pair(row: Any, z_base_ohm: float | None = None) -> tuple[float, float, str]:
    """逐行选择完整的阻抗单位，避免混合表的空列遮盖实际数据。"""
    pairs = []
    for r_key, x_key, unit in (("r_ohm", "x_ohm", "ohm"), ("r_pu", "x_pu", "pu")):
        r_present = not _is_missing_scalar(row.get(r_key))
        x_present = not _is_missing_scalar(row.get(x_key))
        if r_present != x_present:
            raise ValueError(f"支路 {row.get('line_id')} 的 {r_key}/{x_key} 必须成对填写")
        if r_present:
            pairs.append((_require_numeric(row[r_key], r_key, row.get("line_id")),
                          _require_numeric(row[x_key], x_key, row.get("line_id")), unit))
    if not pairs:
        raise ValueError(f"支路 {row.get('line_id')} 缺少完整的 r_ohm/x_ohm 或 r_pu/x_pu")
    if len(pairs) == 2 and z_base_ohm is not None:
        ohm, pu = pairs
        if not all(math.isclose(actual, per_unit * z_base_ohm, rel_tol=1e-9, abs_tol=1e-9)
                   for actual, per_unit in zip(ohm[:2], pu[:2])):
            raise ValueError(f"支路 {row.get('line_id')} 的欧姆/标么阻抗单位冲突：按电压与容量基准换算后不一致")
    return pairs[0]


def _to_bool_status(value: Any) -> bool:
    if _is_missing_scalar(value):
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "open", "disabled", "no"}
    return bool(value)


def _extract_nodes(
    row: Any,
    phase_cols: dict[str, str],
    decl: pm.PhaseDeclaration,
    label: str,
    blank_policy: str = "assume_three",
) -> tuple[tuple[int, ...] | None, bool]:
    """从一行里解析相别，返回 `(节点元组 | None, 是否显式声明)`。

    规则（与 `phase_model` 的模块说明一致）：
    - 该表**没有相别列** → 返回 `(None, False)`，调用方保持三相默认（旧行为）；
    - 有"相别身份"列且给值 → 以它为准（`A`/`2`/`ABC`/`A0` …）；
    - 只有"相数"列且给值 → `3` 可解析，`1`/`2` **报错**（问不出接哪一相，不许猜）；
    - 相别列存在但该行为空 → 由 `blank_policy` 决定：
      * `"assume_three"`（母线表/支路表）：按三相处理，但**计入 `assumed_three_phase`**
        —— 中压母线绝大多数是三相，空值按三相是行业惯例，且已被计数声明；
      * `"error"`（独立负荷表）：**报错**。负荷表是"专门用来声明相别"的表，
        空白既可能是"三相"也可能是"漏填"，猜错会静默算错相，宁可让用户填清楚。
    """
    if not phase_cols or not ({"phase", "phases"} & set(phase_cols)):
        # 只有接线方式/零序参数而没有相别身份时，不构成"按相别建模"：
        # 元件仍是三相，只是接线或零序参数被显式给出。
        return None, False
    # 该行是否是"元件行"由调用方保证；这里只做计数
    decl.elements_total += 1
    identity_col = phase_cols.get("phase")
    count_col = phase_cols.get("phases")
    identity = row.get(identity_col) if identity_col else None
    count = row.get(count_col) if count_col else None

    if not _is_missing_scalar(identity) and str(identity).strip() != "":
        try:
            nodes = pm.parse_phase_token(identity)
        except pm.PhaseSpecError as exc:
            raise ValueError(f"{label} 的相别无法解析：{exc}") from exc
        decl.elements_with_phase += 1
        if not pm.is_three_phase(nodes):
            decl.elements_single_or_partial += 1
        if 0 in nodes:
            decl.neutral_declared += 1
        return nodes, True

    if not _is_missing_scalar(count) and str(count).strip() != "":
        try:
            nodes = pm.parse_phase_token(count, "count")
        except pm.PhaseSpecError as exc:
            raise ValueError(f"{label} 的相数无法解析：{exc}") from exc
        decl.elements_with_phase += 1
        decl.assumed_three_phase += 1
        return nodes, True

    if blank_policy == "error":
        raise ValueError(
            f"{label} 缺少相别：负荷表的 phase 列必须逐行填写"
            "（单相填 A/B/C，两相填 AB，三相填 abc 或 3）"
        )
    decl.assumed_three_phase += 1
    return None, False


def _zero_seq_text(
    row: Any,
    phase_cols: dict[str, str],
    decl: pm.PhaseDeclaration,
    z_base_ohm: float,
    base_freq_hz: float,
) -> str:
    """零序参数 → OpenDSS 关键字文本（` R0=… X0=… C0=…`）；无参数时返回空串。"""
    if not phase_cols:
        return ""
    r0_pu = row.get(phase_cols["r0_pu"]) if "r0_pu" in phase_cols else None
    x0_pu = row.get(phase_cols["x0_pu"]) if "x0_pu" in phase_cols else None
    b0_pu = row.get(phase_cols["b0_pu"]) if "b0_pu" in phase_cols else None
    r0_ohm = row.get(phase_cols["r0_ohm"]) if "r0_ohm" in phase_cols else None
    x0_ohm = row.get(phase_cols["x0_ohm"]) if "x0_ohm" in phase_cols else None

    def num(value: Any) -> float | None:
        if _is_missing_scalar(value) or str(value).strip() == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    parts = pm.sequence_conversion(
        r0_pu=num(r0_pu),
        x0_pu=num(x0_pu),
        b0_pu=num(b0_pu),
        r0_ohm=num(r0_ohm),
        x0_ohm=num(x0_ohm),
        z_base_ohm=z_base_ohm,
        base_freq_hz=base_freq_hz,
    )
    if not parts:
        return ""
    decl.zero_seq_declared += 1
    order = ("R0", "X0", "C0")
    return "".join(f" {k}={parts[k]:.16g}" for k in order if k in parts)


def _read_system_params(system_df: pd.DataFrame | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if system_df is None or system_df.empty:
        return params
    frame = _normalize_columns(system_df.copy())
    # 行式 key/value 表（`parameter|value` 或 `key|value`）：**先判形态再判行数**。
    # 否则"只有一个参数的 key/value 表"会被下面的单行分支当成结构化单行误读。
    is_kv = "value" in frame.columns and (
        "key" in frame.columns or "parameter" in frame.columns
    )
    if "parameter" in frame.columns and "value" in frame.columns:
        for _, row in frame.iterrows():
            key = str(row["parameter"]).strip().upper()
            if key and key != "NAN" and not _is_missing_scalar(row["value"]):
                params[key] = row["value"]
    # 结构化单行（JSON 的 system 对象或混合 CSV 的 system 行）：整行的**列名即参数名**。
    #
    # ⚠️ 这里原先要求"必须存在 base_mva 或 base_kv 列"才进入，后果是**整行被静默忽略**：
    #    只声明了 `frequency_hz`（或只给 slack/电压限值）的 system 表读出来是空字典，
    #    于是 `base_freq_hz` 回落到硬编码默认 **50 Hz** —— 60 Hz 算例的线路充电电纳
    #    （B=2πfC）随之整体算错，而产物里连一句提示都没有。
    #    判据改成"单行且不是 key/value 形态"，这才是"结构化单行"的真正语义：
    #    system_df 已由上游按表名/键识别，单行即系统参数表。
    elif len(frame) == 1 and not is_kv:
        for key, value in frame.iloc[0].to_dict().items():
            upper = str(key).upper()
            if not _is_missing_scalar(value) and not isinstance(value, (dict, list, tuple)):
                params[upper] = value
    # 行式 key/value（`key|value` 变体；`parameter|value` 已在上面的第一分支处理）
    elif set(frame.columns) <= {"key", "parameter", "value"} and len(frame.columns) >= 2:
        key_col = "key" if "key" in frame.columns else "parameter"
        value_col = "value" if "value" in frame.columns else next(c for c in frame.columns if c != key_col)
        for _, row in frame.iterrows():
            key = str(row[key_col]).strip().upper()
            if key and key != "NAN" and not _is_missing_scalar(row[value_col]):
                params[key] = row[value_col]
    aliases = {
        "BASE_MVA": "S_BASE_MVA",
        "BASE_KV": "V_BASE_KV",
        "VOLTAGE_BASE_KV": "V_BASE_KV",
        "FREQUENCY_HZ": "FREQUENCY_HZ",
    }
    for source, target in aliases.items():
        if source in params and target not in params:
            params[target] = params[source]
    return params


def _find_slack_buses(bus_df: pd.DataFrame, line_df: pd.DataFrame) -> set[str]:
    slack: set[str] = set()
    if "type" in bus_df.columns:
        types = bus_df["type"].astype(str).str.strip().str.lower()
        # DataFrame 从 MATPOWER/JSON 读入时可能把 3 变成 3.0，统一类型码。
        type_codes = types.map(lambda value: value[:-2] if value.endswith(".0") else value)
        slack = {_id_text(v) for v in bus_df[type_codes.isin({"slack", "3", "ref", "reference", "swing"})]["bus_id"].tolist()}
    if len(slack) > 1:
        raise ValueError(
            f"检测到多个平衡母线/Slack 母线：{', '.join(sorted(slack))}；"
            "请明确多岛运行策略后再转换"
        )
    source_links = line_df[
        (line_df["from_bus"].map(_id_text).str.upper() == "SOURCE")
        | (line_df["to_bus"].map(_id_text).str.upper() == "SOURCE")
    ]
    if not slack and not source_links.empty:
        for _, row in source_links.iterrows():
            for bus in (_id_text(row["from_bus"]), _id_text(row["to_bus"])):
                if bus.upper() != "SOURCE":
                    slack.add(bus)
    if not slack and not bus_df.empty:
        slack.add(str(bus_df.iloc[0]["bus_id"]).strip())
    return slack


def _source_alias(bus_set: set[str], slack_buses: set[str]) -> str:
    """虚拟源 `SOURCE` 在连通性分析里应折叠到哪个真实母线上。

    优先级：① 母线表里真有一个叫 SOURCE 的母线（那就用它本身）；
    ② 否则取第一个平衡母线；③ 都没有则退回 "SOURCE"（后续 missing 检查
    会把它报成"线路引用了不存在的母线"，这比静默通过好）。
    """
    for b in bus_set:
        if b.upper() == "SOURCE":
            return b
    for b in sorted(slack_buses):
        return b
    return "SOURCE"


def _validate_frames(bus_df: pd.DataFrame, line_df: pd.DataFrame, slack_buses: set[str]) -> None:
    bus_ids = [_id_text(v) for v in bus_df["bus_id"].tolist()]
    duplicates = sorted({v for v in bus_ids if bus_ids.count(v) > 1})
    if duplicates:
        raise ValueError(f"检测到重复母线 ID：{', '.join(duplicates)}")
    bus_set = set(bus_ids)
    missing: set[str] = set()
    active_edges: list[tuple[str, str]] = []
    # ⚠️ `SOURCE` 是**虚拟源节点**，不是数据里的母线名：算例常把平衡母线
    # 单独叫 SOURCE，再用支路 SOURCE→某母线 接出去。
    # 实测缺陷（aa 5 母线算例，拓扑完全连通却被判"4 个母线全孤岛"）：
    # 原实现把「任一端是 SOURCE」的支路**整条丢弃**，于是平衡母线自己成了孤点，
    # 它下游的所有母线自然全不可达 —— 一个正常的辐射网被判成全岛。
    # 正确做法：把 SOURCE 折叠到实际平衡母线上再算连通性，
    # 这样虚拟源与真实平衡母线是同一个电气节点。
    source_alias = _source_alias(bus_set, slack_buses)
    for _, row in line_df.iterrows():
        fb, tb = _id_text(row["from_bus"]), _id_text(row["to_bus"])
        # 虚拟源折叠：SOURCE 一律换成真实平衡母线名。
        if fb.upper() == "SOURCE":
            fb = source_alias
        if tb.upper() == "SOURCE":
            tb = source_alias
        if fb not in bus_set:
            missing.add(fb)
        if tb not in bus_set:
            missing.add(tb)
        if not _to_bool_status(row.get("status", 1)):
            continue
        r, x, _unit = _impedance_pair(row)
        # 零阻抗判据：只拦**严格为零**（|r|、|x| 双双 < 1e-12，单位 pu）。
        # ⚠️ 刻意**不**拦"非常小但非零" —— 那是合法数据，只是数值上接近奇异；
        # 放它进来由求解器按容差处理并如实报告迭代情况，
        # 提前拒绝等于把用户能用的算例挡在门外（真机题库里就有这种陷阱题：
        # 用户会问"如果是 1e-9 的阻抗算不算零"，判据必须能答出这个区别）。
        if abs(r) < ZERO_IMPEDANCE_TOL and abs(x) < ZERO_IMPEDANCE_TOL:
            raise ValueError(
                f"检测到零阻抗支路：{row.get('line_id', 'unknown')}（r=x=0）"
                f"。判据是 |r| 与 |x| **双双小于 {ZERO_IMPEDANCE_TOL:.16g} pu（严格为零）**；"
                f"若阻抗只是非常小但非零（如 1e-9 pu），本转换器**不会**拒绝 —— "
                f"那是合法数据，由求解器按容差处理"
            )
        if fb != tb:
            active_edges.append((fb, tb))
    if missing:
        raise ValueError(f"线路引用了不存在的母线：{', '.join(sorted(missing))}")
    reachable = set(str(v) for v in slack_buses)
    frontier = list(reachable)
    adjacency: dict[str, set[str]] = {}
    for a, b in active_edges:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    while frontier:
        current = frontier.pop()
        for nxt in adjacency.get(current, set()):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    islanded = sorted(bus_set - reachable)
    if islanded:
        raise ValueError(f"检测到供电孤岛，无法从平衡母线到达：{', '.join(islanded[:20])}")


def _fidelity_record(lines: list[str], **record: Any) -> None:
    """Machine-readable physical provenance, consumed independently of prose notes."""
    lines.append("! [Tianshu Fidelity] " + json.dumps(record, ensure_ascii=True, separators=(",", ":")))


def _admittance_shunt(lines: list[str], name: str, bus: str, kv: float,
                      gs_mw: float, bs_mvar: float, enabled: bool = True,
                      nodes: tuple[int, ...] | None = None) -> str | None:
    """Constant admittance: P=Gs*|V|², Q=-Bs*|V|² at nominal bus voltage.

    A PD Reactor keeps MATPOWER fixed shunts separate from scheduled Load power.
    R+jX=1/(G+jB); negative X is capacitive, negative R is fixed active injection.
    """
    if gs_mw == 0 and bs_mvar == 0:
        return None
    phases = 3 if nodes is None else len([n for n in nodes if n])
    coil_kv = pm.load_kv_for_nodes(kv, nodes)
    # 3-phase LL base or 1-phase LN base; the input is total element MW/Mvar.
    voltage_squared = coil_kv * coil_kv * (phases if phases != 3 else 1)
    admittance = complex(gs_mw, bs_mvar) / voltage_squared
    if not math.isfinite(admittance.real) or not math.isfinite(admittance.imag) or admittance == 0:
        raise ValueError("并联导纳换算不是有限非零数值，请检查功率与基压量级")
    impedance = 1 / admittance
    if not all(math.isfinite(v) for v in (impedance.real, impedance.imag)):
        raise ValueError("并联导纳等值阻抗不是有限数值")
    element = f"Reactor.{name}"
    lines.append(f"New {element} phases={phases} bus1={pm.node_ref(bus, nodes)} "
                 f"R={impedance.real:.16g} X={impedance.imag:.16g}" + ("" if enabled else " Enabled=no"))
    return element


def _require_balanced_phase_bank(tables: tuple[pd.DataFrame | None, ...]) -> None:
    """The complex-tap equivalent is defined for a balanced positive sequence case."""
    for frame in tables:
        if frame is None:
            continue
        columns = pm.detect_phase_columns(frame.columns)
        for _, row in frame.iterrows():
            nodes, _ = _extract_nodes(row, columns, pm.PhaseDeclaration(table="phase_bank"), "相移等值")
            if nodes is not None and not pm.is_three_phase(nodes):
                raise ValueError("非零 shift_deg 专用等值仅支持平衡正序三相算例，不能与单相/两相输入混用")
            for key in ("conn", "conn1", "conn2"):
                value = row.get(key)
                if not _is_missing_scalar(value) and pm.normalize_conn(value) != "wye":
                    raise ValueError("非零 shift_deg 专用等值不支持额外 delta 物理接线；请使用原始 DSS 绕组模型")
            if any(not _is_missing_scalar(row.get(key)) for key in
                   ("r0_pu", "x0_pu", "b0_pu", "r0_ohm", "x0_ohm")):
                raise ValueError("非零 shift_deg 专用等值仅定义正序，不能与显式零序参数混用")


def _phase_bank_coefficients(shift: float) -> list[tuple[int, float]]:
    """Choose a cyclic two-phase basis without vanishing secondary voltages.

    All three bases are algebraically equal on positive sequence. Choosing the
    largest smaller coefficient avoids the ill-conditioned tiny coil obtained
    by Va+epsilon*Vb near zero shift. Coefficients stay about .577..1.155.
    """
    angle = math.remainder(shift, 360.0)
    candidates = []
    for first in range(3):
        radians = math.radians(math.remainder(angle - first*120, 360.0))
        a = math.cos(radians) + math.sin(radians)/math.sqrt(3)
        b = 2*math.sin(radians)/math.sqrt(3)
        candidates.append([(first, a), ((first+1) % 3, b)])
    return max(candidates, key=lambda parts: min(abs(coefficient) for _, coefficient in parts))


def _phase_bank(lines: list[str], allocate: Any, fb: str, tb: str, kv1: float,
                kv2: float, sbase: float, r_pu: float, x_pu: float,
                tap: float, shift: float, enabled: bool) -> tuple[list[str], list[str]]:
    """Real winding synthesis of V'=V/t for positive sequence, t=tau*exp(jθ).

    For Vb=Va*exp(-j120°), a*Va+b*Vb=Va*exp(-jθ),
    a=cosθ+sinθ/sqrt(3), b=2sinθ/sqrt(3). The primary currents
    follow the real transpose, so the +sequence current ratio is conjugate.
    Each bank leakage is known and subtracted from the series Reactor, not
    hidden as an approximation. This is a mathematical positive-sequence
    equivalent, not an assertion about a physical transformer construction.
    """
    parts = _phase_bank_coefficients(shift)
    shifted = allocate("_ts_phase")
    coefficient_norm = sum(c*c for _, c in parts) / (tap*tap)
    leakage = min(1e-4, math.hypot(r_pu, x_pu) * 1e-3 / coefficient_norm)
    if leakage <= 0 or not math.isfinite(leakage):
        raise ValueError("相移等值漏抗基准不是有限正数")
    elements = []
    off = "" if enabled else " Enabled=no"
    for phase in (1, 2, 3):
        for position, (offset, coefficient) in enumerate(parts):
            primary = (phase - 1 + offset) % 3 + 1
            if len(parts) == 1:
                secondary_nodes = [phase, 0]
            elif position == 0:
                secondary_nodes = [phase + 3, 0]
            else:
                secondary_nodes = [phase, phase + 3]
            if coefficient < 0:
                secondary_nodes.reverse()
            secondary = shifted + "." + ".".join(map(str, secondary_nodes))
            element = "Transformer." + allocate("_ts_winding")
            elements.append(element)
            lines.append(f"New {element} phases=1 windings=2 buses=[{fb}.{primary}.0 {secondary}] "
                         f"conns=[wye wye] kvs=[{kv1/math.sqrt(3):.16g} {abs(coefficient)*kv2/math.sqrt(3)/tap:.16g}] "
                         f"kvas=[{sbase*1000/3:.16g} {sbase*1000/3:.16g}] %Rs=[0 0] "
                         f"XHL={leakage*100:.16g} ppm_antifloat=0 %imag=0 %noloadloss=0{off}")
    element = "Reactor." + allocate("_ts_series")
    elements.append(element)
    zbase = kv2 * kv2 / sbase
    lines.append(f"New {element} phases=3 bus1={shifted}.1.2.3 bus2={tb}.1.2.3 "
                 f"R={r_pu*zbase:.16g} X={(x_pu-leakage*coefficient_norm)*zbase:.16g}{off}")
    return elements, [shifted]


def _generator_q_policy(row: Any, bid: str, p_kw: float, q_kvar: float,
                        sbase: float, enforce: bool) -> tuple[float, float, float, dict[str, Any]]:
    qmin = row.get("qmin_mvar")
    qmax = row.get("qmax_mvar")
    if _is_missing_scalar(qmin) and not _is_missing_scalar(row.get("qmin_kvar")):
        qmin = float(row["qmin_kvar"])/1000
    if _is_missing_scalar(qmax) and not _is_missing_scalar(row.get("qmax_kvar")):
        qmax = float(row["qmax_kvar"])/1000
    complete = not _is_missing_scalar(qmin) and not _is_missing_scalar(qmax)
    if complete and float(qmin) > float(qmax):
        raise ValueError(f"母线 {bid} 的 qmin_mvar 不能大于 qmax_mvar")
    if enforce and (not complete or row.get("_q_limits_complete", True) is False):
        raise ValueError(f"母线 {bid} 启用 ENFORCE_Q_LIMITS 必须提供全部在线机组完整的 Qmin/Qmax 上下界")
    rating = row.get("gen_rating_mva")
    if _is_missing_scalar(rating) and not _is_missing_scalar(row.get("gen_rating_kva")):
        rating = float(row["gen_rating_kva"])/1000
    physical_rating = None if _is_missing_scalar(rating) else float(rating)*1000
    wide = max(sbase*1_000_000, abs(p_kw)*1000, abs(q_kvar)*1000)
    lower, upper = (float(qmin)*1000, float(qmax)*1000) if enforce else (-wide, wide)
    policy = "input_q_limits" if enforce else "reference_unconstrained_numeric_bounds"
    native_kva = max(sbase*1000, math.hypot(p_kw, q_kvar)*1.2)
    if physical_rating is not None:
        if abs(p_kw) > physical_rating:
            raise ValueError(f"母线 {bid} 的有功超出显式 gen_rating_mva/kva 额定容量")
        # Stable sqrt(S²-P²), without overflowing finite squared capacities.
        qcap = math.sqrt(max(0.0, 1-abs(p_kw)/physical_rating)) * math.sqrt(1+abs(p_kw)/physical_rating) * physical_rating
        lower, upper = max(lower, -qcap), min(upper, qcap)
        if lower > upper:
            raise ValueError(f"母线 {bid} 的 Qmin/Qmax 与显式额定容量没有可行交集")
        native_kva = physical_rating
        policy += "+explicit_rating_circle_at_scheduled_p"
    if not all(math.isfinite(value) for value in (lower, upper, native_kva)):
        raise ValueError(f"母线 {bid} 的发电机数值边界换算不是有限数值")
    return lower, upper, native_kva, {
        "role": "generator", "bus": bid, "element": f"Generator.Gen_{bid}",
        "q_policy": policy, "input_qmin_mvar": None if _is_missing_scalar(qmin) else float(qmin),
        "input_qmax_mvar": None if _is_missing_scalar(qmax) else float(qmax),
        "native_minkvar": lower, "native_maxkvar": upper,
        "native_kva": native_kva, "explicit_rating_kva": physical_rating,
        "kva_provenance": "explicit_nameplate" if physical_rating is not None else "numeric_base_not_equipment_rating",
    }


def build_dss_from_frames(
    system_df: pd.DataFrame | None,
    bus_df: pd.DataFrame,
    line_df: pd.DataFrame,
    output_dir: str,
    source_label: str = "case",
    load_df: pd.DataFrame | None = None,
    conversion_notes: list[str] | None = None,
) -> str:
    """从多种字段别名的 DataFrame 生成 OpenDSS Master.dss。

    `load_df` 可选：存在独立的负荷表时以它为准（每行一个负荷，可带相别），
    否则沿用旧行为（从母线表的 pd_mw/qd_mvar 派生三相负荷）。
    """
    os.makedirs(output_dir, exist_ok=True)
    bus_df = _normalize_bus_frame(bus_df)
    line_df = _normalize_line_frame(line_df)
    load_norm: pd.DataFrame | None = None
    if load_df is not None:
        load_norm = _normalize_load_frame(load_df) if len(load_df) > 0 else load_df.copy()
    sys_params = _read_system_params(system_df)
    checked = validate_tables(bus_df, line_df, load_norm, system=sys_params)
    bus_df, line_df, load_norm, sys_params = checked.bus, checked.line, checked.load, checked.system
    notes = list(conversion_notes or [])
    supported_system = {"BASE_MVA", "S_BASE_MVA", "BASE_KV", "V_BASE_KV", "VOLTAGE_BASE_KV",
                        "FREQUENCY_HZ", "ENFORCE_Q_LIMITS", "V_MIN_PU", "V_MAX_PU"}
    unsupported_system = sorted(set(sys_params) - supported_system)
    if unsupported_system:
        notes.append("System 中未支持的字段未用于建模：" + ", ".join(unsupported_system))
    if "FREQUENCY_HZ" not in sys_params:
        notes.append("输入未声明 FREQUENCY_HZ，使用默认频率 50 Hz")
    if "S_BASE_MVA" not in sys_params:
        notes.append("输入未声明容量基准 S_BASE_MVA，使用默认值 100 MVA")
    # 系统表**给了却一个参数都没读出来** ⇒ 整表被忽略，频率/基准会落到默认值。
    # 这属于"静默改变物理模型"，必须在产物里说出来（见 master_lines[2] 的拼接）。
    sys_unread_note = ""
    if system_df is not None and not system_df.empty and not sys_params:
        sys_unread_note = (
            "；⚠ 提供了 system 表但未识别出任何系统参数（该表已整体忽略），"
            "系统频率与基准将使用默认值（频率默认 50 Hz），请在报告中按此口径说明"
        )
    # 相别声明：整份转换只统计一份，写进产物头部供报告如实说明。
    decl = pm.PhaseDeclaration(table=source_label)

    bus_base: dict[str, float] = {}
    for _, row in bus_df.iterrows():
        bus_base[_id_text(row["bus_id"])] = _to_float(row.get("base_kv"), _to_float(sys_params.get("V_BASE_KV"), 20.0))
    slack_buses = _find_slack_buses(bus_df, line_df)
    if "type" not in bus_df.columns or not bus_df["type"].eq("slack").any():
        notes.append("输入未声明平衡母线，按虚拟 SOURCE 连接或首个母线选择电源节点：" + ", ".join(sorted(slack_buses)))
    missing_base_count = sum(_is_missing_scalar(row.get("base_kv")) for _, row in bus_df.iterrows())
    if missing_base_count:
        default_kv = _to_float(sys_params.get("V_BASE_KV"), 20.0)
        notes.append(f"{missing_base_count} 个母线未声明 base_kv，使用系统或默认基压 {default_kv:.16g} kV")
    active_line_count = sum(
        1 for _, row in line_df.iterrows() if _to_bool_status(row.get("status", 1))
    )
    inactive_line_count = len(line_df) - active_line_count
    # 记录源数据中明确的 PV/发电机电压设定值，供 OpenDSS Generator 映射使用。
    # 若源数据没有该列，仍保持原有固定功率兼容路径。
    gen_bus_rows = [
        row for _, row in bus_df.iterrows()
        if _id_text(row.get("bus_id")) not in slack_buses
        and str(row.get("type", "")).strip().lower() in {"pv", "2"}
    ]
    _validate_frames(bus_df, line_df, slack_buses)
    slack_remap = next(iter(slack_buses), None)
    # 内部源母线不能覆盖用户真实母线；SOURCE 只在没有该真实母线时
    # 才作为虚拟源别名。常规名称仍保持 sourcebus，发生碰撞时选独立名称。
    occupied = {name.casefold() for name in bus_base if name != slack_remap}
    internal_source = "sourcebus"
    counter = 0
    while internal_source.casefold() in occupied:
        counter += 1
        internal_source = f"_tianshu_source{counter}"
    if internal_source != "sourcebus":
        notes.append(f"内部电源母线使用 {internal_source}，保留输入母线 sourcebus 的独立身份")
    source_base_kv = bus_base.get(slack_remap or "", _to_float(sys_params.get("V_BASE_KV"), 20.0))
    s_base_mva = _to_float(sys_params.get("S_BASE_MVA"), 100.0)
    # 系统频率：影响线路充电电纳→电容的换算，不能写死（此前硬编码 50）。
    base_freq_hz = _to_float(sys_params.get("FREQUENCY_HZ"), 50.0)
    # PV 无功限值是否强制执行。默认**关闭**：参考解的求解口径是
    # "AC Newton-Raphson, polar, flat start（unconstrained PV-Q）"，即 PV 节点无功
    # 不受限；而表格算例给的 qmin/qmax 常常极小（case09 只有 ±5 MVAr，而全网负荷
    # 1117 MW），一旦强制执行，PV 节点立刻退化成 PQ，电压被系统性压低
    # （实测 vmin 0.79 而参考 0.895）。需要严格限值语义时用 ENFORCE_Q_LIMITS=1 打开。
    enforce_q_limits = str(sys_params.get("ENFORCE_Q_LIMITS", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    for _, row in bus_df.iterrows():
        if _id_text(row["bus_id"]) not in slack_buses:
            continue
        if any(not _is_missing_scalar(row.get(key)) for key in ("gen_rating_mva", "gen_rating_kva")):
            raise ValueError("平衡母线使用理想电源，不能映射显式发电机额定容量；请使用具有设备约束的原始求解器或 DSS 模型")
        if enforce_q_limits and any(not _is_missing_scalar(row.get(key)) for key in
                                    ("qmin_mvar", "qmax_mvar", "qmin_kvar", "qmax_kvar")):
            raise ValueError("ENFORCE_Q_LIMITS 不能对理想平衡电源实施 Qmin/Qmax；不能静默忽略源侧无功约束")
    v_min_pu = _to_float(sys_params.get("V_MIN_PU"), 0.95)
    v_max_pu = _to_float(sys_params.get("V_MAX_PU"), 1.05)
    if v_min_pu > v_max_pu:
        raise ValueError("V_MIN_PU 不能大于 V_MAX_PU")
    slack_row = next(
        (row for _, row in bus_df.iterrows() if _id_text(row.get("bus_id")) == slack_remap),
        None,
    )
    slack_pu = _to_float(
        slack_row.get("v_set_pu") if slack_row is not None else None,
        1.0,
    )
    # DSS 的 Circuit/source kV 与表格的 base_kv 均按线电压表达；线路母线
    # 的 kVBase 由 CalcVoltageBases 统一生成。显式保留 Slack 电压设定值，
    # 并提高等值电源短路容量，避免把源端内阻降压误当成网络潮降。

    def map_bus(name: Any) -> str:
        value = _id_text(name)
        if (value.upper() == "SOURCE" and value not in bus_base) or (slack_remap and value == slack_remap):
            return internal_source
        return value

    # 占位：转换完成后按相别声明的统计结果重写（见函数尾部的 master_lines[2] 赋值）。
    # 之所以要重写：只有"真的按输入相别建模"时才能不再声明"相别不平衡未保留"。
    q_policy_warning = (
        "非 Slack 发电机按输入完整 Q 上下界执行强制策略"
        if enforce_q_limits else
        "PV 采用参考非强制 Q 策略，有限数值边界与缺省 native kVA 不代表设备真实容量"
    ) + "；显式 gen_rating_mva/kva 约束给定有功下的容量圆"
    conversion_warning = (
        "原始表格/矩阵已转换为平衡三相 OpenDSS 等值模型；"
        f"{q_policy_warning}；控制器、相别不平衡与部分原始求解器语义需另做交叉核验。"
    )
    master_lines: list[str] = [
        "Clear",
        f"! [Tianshu Conversion] source_format={source_label}; approximation=true",
        f"! [Tianshu Conversion] warning={conversion_warning}",
        f"! [Tianshu Conversion] active_line_count={active_line_count}; inactive_line_count={inactive_line_count}",
        f"! [Tianshu Conversion] enforce_q_limits={int(enforce_q_limits)}",
        f"Set DefaultBaseFrequency={base_freq_hz:.16g}",
        f"New Circuit.{source_label} basekv={source_base_kv:.16g} pu={slack_pu:.16g} phases=3 MVAsc3={s_base_mva * 1000000:.16g} MVAsc1={s_base_mva * 1000000:.16g}"
        + (f" bus1={internal_source}" if internal_source != "sourcebus" else ""),
        "",
        "// 线路与变压器定义（p.u. 阻抗按母线电压等级换算）",
    ]
    # 三张表各自的相别列（空字典 = 该表无相别信息，保持三相默认）
    phase_cols_line = pm.detect_phase_columns(line_df.columns)
    phase_cols_bus = pm.detect_phase_columns(bus_df.columns)
    phase_cols_load = pm.detect_phase_columns(load_norm.columns) if load_norm is not None else {}
    # 只有"相数/相别身份"列才构成按相别建模；仅给接线方式或零序参数时仍属三相元件。
    for _cols in (phase_cols_line, phase_cols_bus, phase_cols_load):
        if {"phase", "phases"} & set(_cols):
            decl.has_phase_columns = True
    reserved = {name.casefold() for name in bus_base} | {internal_source.casefold()}
    reserved.update(_id_text(value).casefold() for value in line_df["line_id"])

    def allocate(prefix: str) -> str:
        number = 1
        while f"{prefix}{number}".casefold() in reserved:
            number += 1
        name = f"{prefix}{number}"
        reserved.add(name.casefold())
        return name

    if any(_to_float(row.get("shift_deg")) != 0 for _, row in line_df.iterrows()):
        _require_balanced_phase_bank((bus_df, line_df, load_norm))
    for idx, row in line_df.iterrows():
        lid = _id_text(row.get("line_id", f"L{idx + 1}")) or f"L{idx + 1}"
        fb, tb = map_bus(row["from_bus"]), map_bus(row["to_bus"])
        raw_kind = row.get("kind") if "kind" in row else None
        kind_missing = _is_missing_scalar(raw_kind) or str(raw_kind).strip().lower() in {"", "nan", "none"}
        kind = "" if kind_missing else str(raw_kind).strip().lower()
        status = _to_bool_status(row.get("status", 1))
        from_id = _id_text(row["from_bus"])
        to_id = _id_text(row["to_bus"])
        kv_from = bus_base.get(from_id, source_base_kv)
        kv_to = bus_base.get(to_id, source_base_kv)
        kv_differs = abs(kv_from - kv_to) > 1e-9
        tap = _to_float(row.get("tap_ratio"), 1.0)
        shift = _to_float(row.get("shift_deg"), 0.0)
        # 有明确 kind=line 时不能把跨电压等级连接静默当成普通线路；
        # 这通常意味着输入漏标了变压器语义，应在转换阶段拒绝。
        if kind in {"line", "branch"} and (kv_differs or abs(tap - 1.0) > 1e-12 or shift != 0):
            raise ValueError(
                f"支路 {lid} 标记为普通线路但两端电压等级不一致："
                f"{kv_from:.16g} kV -> {kv_to:.16g} kV，tap_ratio={tap:.16g}；"
                "请标记为 transformer"
            )
        # 判定以**物理事实（两端电压等级是否一致）为准**，kind 只作为补充。
        # 真实缺陷：kind 的取值不止 transformer/line，还有 tie / parallel_transformer 等；
        # 此前只认 transformer/xfmr/trafo，于是 case09~11 里 2 条跨电压等级的
        # parallel_transformer 被静默当成"110kV 直连 20kV 的普通线路"——阻抗折算基准
        # 完全不同，电压偏差随之而来（变压器支路数也少 2，与参考值对不上）。
        is_transformer = kv_differs or abs(tap - 1.0) > 1e-12 or shift != 0 or kind in {
            "transformer", "xfmr", "trafo", "parallel_transformer",
        }
        if is_transformer:
            kv1 = bus_base.get(from_id, source_base_kv)
            kv2 = bus_base.get(to_id, source_base_kv)
            rate_kva = _to_float(row.get("rate_mva"), s_base_mva) * 1000.0
            z_base = kv1 * kv1 / s_base_mva
            r_value, x_value, impedance_unit = _impedance_pair(row, z_base)
            r_pu = r_value / z_base if impedance_unit == "ohm" else r_value
            x_pu = x_value / z_base if impedance_unit == "ohm" else x_value
            enabled = "" if status else " Enabled=no"
            tap = _to_float(row.get("tap_ratio"), 1.0)
            shift_deg = _to_float(row.get("shift_deg"), 0.0)
            if tap <= 0:
                raise ValueError(f"支路 {lid} 的 tap_ratio 必须大于 0：{tap:.16g}")
            # 阻抗基准（关键，且此前是错的）：
            #   OpenDSS 的 %Rs / XHL 是**变压器自身 kVA 基准**下的百分值，
            #   而输入的 r_pu / x_pu 是**系统基准**（s_base_mva）下的标么值。
            #   要让两者等价，必须令变压器 kVA 基准 = 系统基准，
            #   于是 %R = r_pu × 100 恰好还原原算例的标么阻抗。
            #   此前用 rateA 当 kVA 基准 —— MATPOWER 的 rateA 是**热稳限额**，
            #   不是阻抗基准。实测 case12：以 rateA(55/18 MVA) 为基准会把变压器
            #   等值阻抗放大 1.8~5.6 倍，最低电压被压到 0.787（参考 0.900）；
            #   再按 base/rate 折算 %R 则放大到 3.3 倍，电压崩到 0.546。
            xfmr_kva = max(s_base_mva, 1e-9) * 1000.0
            # 双绕组：OpenDSS 的串联 R 是两绕组 %R 之和，故总 r_pct 需对半拆分；
            # XHL 已是**总**漏抗百分值，不再拆分（此前直接写两遍 = 电阻翻倍）。
            r_pct_half = r_pu * 100.0 / 2.0
            x_pct = x_pu * 100.0
            # MATPOWER 非标准变比位于 from 侧；DSS 必须显式选择绕组，
            # 否则 bare tap 会改最后活动的第二绕组，方向恰好相反。
            tap_text = (f" wdg=1 mintap={min(0.9, tap):.16g} maxtap={max(1.1, tap):.16g}"
                        f" tap={tap:.16g} wdg=2 tap=1")
            # 相别：三相变压器组可直接缺省；单相变压器（Phases=1）必须给相别。
            # 两相变压器不存在，直接拒绝而不是硬凑。
            xf_nodes, _ = _extract_nodes(row, phase_cols_line, decl, f"变压器 {lid}")
            if xf_nodes is not None and not pm.is_three_phase(xf_nodes):
                if len([n for n in xf_nodes if n != 0]) != 1:
                    raise ValueError(
                        f"变压器 {lid} 的相数不支持：仅支持三相（3）或单相（1）"
                    )
                xf_phases = 1
            else:
                xf_phases = 3

            def _winding_conn(key: str) -> str:
                raw = row.get(key) if key in row else None
                if _is_missing_scalar(raw) or str(raw).strip() == "":
                    # 未分侧给接线时退回通用 conn；都没有则 wye（与旧输出一致）
                    raw = row.get("conn") if "conn" in row else None
                try:
                    return pm.normalize_conn(raw, "wye")
                except pm.PhaseSpecError as exc:
                    raise ValueError(f"变压器 {lid} 的接线方式不合法：{exc}") from exc

            conns_text = f"[{_winding_conn('conn1')} {_winding_conn('conn2')}]"
            if "conn" in phase_cols_line or "conn1" in phase_cols_line or "conn2" in phase_cols_line:
                decl.conn_declared += 1
            internal_buses: list[str] = []
            if shift_deg != 0:
                elements, internal_buses = _phase_bank(master_lines, allocate, fb, tb,
                                                       kv1, kv2, s_base_mva, r_pu, x_pu,
                                                       tap, shift_deg, status)
                model = "balanced_positive_sequence_phase_bank"
                notes.append(f"支路 {lid} 的 shift_deg={shift_deg:.16g} 采用平衡正序实绕组串联等值；合成绕组与内部母线不是新增真实设备，不定义负序/零序保真")
            else:
                elements = [f"Transformer.{lid}"]
                model = "two_winding_transformer"
                master_lines.append(
                    f"New Transformer.{lid} phases={xf_phases} windings=2 "
                    f"buses=[{pm.node_ref(fb, xf_nodes)} {pm.node_ref(tb, xf_nodes)}] conns={conns_text} "
                    f"kvs=[{kv1:.16g} {kv2:.16g}] kvas=[{xfmr_kva:.16g} {xfmr_kva:.16g}] "
                    f"%Rs=[{r_pct_half:.16g} {r_pct_half:.16g}] XHL={x_pct:.16g}{tap_text} ppm_antifloat=0{enabled}"
                )
            shunts = []
            ports_from = [{"element": element, "terminal": 1} for element in elements[:-1]] if shift_deg != 0 else [{"element": elements[0], "terminal": 1}]
            ports_to = [{"element": elements[-1], "terminal": 2}]
            b_pu = _to_float(row.get("b_pu"))
            if b_pu != 0:
                for end_index, (end_bus, end_kv, end_b) in enumerate(((fb, kv1, b_pu / (2*tap*tap)),
                                               (tb, kv2, b_pu/2))):
                    element = _admittance_shunt(master_lines, allocate("_ts_charging"),
                                                end_bus, end_kv, 0, end_b*s_base_mva,
                                                status, xf_nodes)
                    if element:
                        shunts.append(element)
                        (ports_from if end_index == 0 else ports_to).append({"element": element, "terminal": 1})
                notes.append(f"变压器 {lid} 的 b_pu 映射为两端固定导纳 π 充电（from端按 tap_ratio² 折算），不是设备励磁电流或无载铁损")
            _fidelity_record(master_lines, role="branch", id=lid, from_bus=fb, to_bus=tb,
                             series_elements=elements, shunt_elements=shunts,
                             internal_buses=internal_buses, tap_ratio=tap,
                             shift_deg=shift_deg, model=model,
                             b_pu=b_pu, enabled=status, ports_from=ports_from, ports_to=ports_to,
                             original_kind="transformer", phases=xf_phases,
                             base_kv_from=kv1, base_kv_to=kv2,
                             input_rate_mva=(None if _is_missing_scalar(row.get("rate_mva")) or row.get("_rating_explicit", True) is False else float(row["rate_mva"])),
                             rating_source=("input_thermal_limit" if not _is_missing_scalar(row.get("rate_mva")) and row.get("_rating_explicit", True) is not False else "not_declared"),
                             kva_provenance="system_impedance_base_not_nameplate")
            continue
        base_kv = bus_base.get(_id_text(row["from_bus"]), source_base_kv)
        z_base = base_kv * base_kv / max(s_base_mva, 1e-9)
        r_value, x_value, impedance_unit = _impedance_pair(row, z_base)
        r_ohm = r_value * z_base if impedance_unit == "pu" else r_value
        x_ohm = x_value * z_base if impedance_unit == "pu" else x_value
        # --- 相别：单相/两相支路必须显式给相别，三相可缺省 ---
        nodes, _declared = _extract_nodes(row, phase_cols_line, decl, f"支路 {lid}")
        if nodes is not None and any(n == 0 for n in nodes):
            # 线路的中性点不由显式节点表达，而是通过 R0/X0/C0（Kron 等值）建模；
            # 若输入在相标里带了 0，剥掉并记录声明，避免生成语义不明确的节点。
            nodes = tuple(n for n in nodes if n != 0)
            decl.notes.append(f"支路 {lid} 的相标含中性点，已按 R0/X0/C0 口径处理（不显式建中性导体）")
        n_phases = 3 if nodes is None else len(nodes)
        if n_phases not in (1, 2, 3):
            raise ValueError(f"支路 {lid} 的相数不支持：{n_phases}（仅支持 1/2/3）")
        z0_text = _zero_seq_text(row, phase_cols_line, decl, z_base, base_freq_hz)
        # ⚠️ 绝不要给 `New Line` 写 `Conn=`：OpenDSS 的 Line **没有 Conn 属性**
        # （只有 Load / Generator 有）。实测把 `Conn=wye` 写在 Line 上会让
        # OpenDSS COM 引擎在 compile 阶段**死循环**（120s 硬超时才能脱身），
        # 而同一模型删掉这个词就 5 秒内正常收敛——最小差异对照已确认。
        # 输入表若带 conn 列（表格里常与变压器接线共用一列），对线路而言语义无意义，
        # 这里显式忽略并计入声明，而不是静默丢弃。
        if "conn" in row and not _is_missing_scalar(row.get("conn")):
            decl.conn_ignored_on_lines += 1
        # 线路充电电纳：源数据的 b_pu 是**系统基准**下的总对地充电电纳，
        # 此前完全没建模（既不报警也不提示），等于把线路的容性无功支撑抹掉，
        # 电压被系统性压低。换算：B(S) = b_pu / Z_base，C(nF) = B/(2πf) × 1e9，
        # 再交给 OpenDSS 的 C1（Units=none 时 C1 为整条线路的 nF 总量）。
        c_text = ""
        b_pu = _to_float(row.get("b_pu"), 0.0)
        if abs(b_pu) > 1e-12 and base_freq_hz > 0:
            c_nf = b_pu / z_base / (2.0 * 3.141592653589793 * base_freq_hz) * 1e9
            if abs(c_nf) > 1e-6:
                c_text = f" C1={c_nf:.16g}"
        enabled = "yes" if status else "no"
        master_lines.append(
            f"New Line.{lid} Bus1={pm.node_ref(fb, nodes)} Bus2={pm.node_ref(tb, nodes)} "
            f"R1={r_ohm:.16g} X1={x_ohm:.16g}{c_text}{z0_text} "
            f"Length=1 Units=none Phases={n_phases} Enabled={enabled}"
        )

    master_lines.extend(["", "// 固定并联导纳：额定电压下 Gs 消耗 MW，Bs 注入 MVAr，功率随 |V|² 变化"])
    for _, row in bus_df.iterrows():
        bid = _id_text(row["bus_id"])
        gs = _to_float(row.get("gs_mw"), _to_float(row.get("gs_kw"))/1000)
        bs = _to_float(row.get("bs_mvar"), _to_float(row.get("bs_kvar"))/1000)
        if gs == 0 and bs == 0:
            continue
        nodes, _ = _extract_nodes(row, phase_cols_bus, decl, f"母线 {bid} 的并联导纳")
        if nodes is not None and not (pm.is_three_phase(nodes) or len([n for n in nodes if n]) == 1):
            raise ValueError(f"母线 {bid} 的 Gs/Bs 仅支持三相或单相接地并联导纳")
        if not _is_missing_scalar(row.get("conn")) and pm.normalize_conn(row.get("conn")) != "wye":
            raise ValueError(f"母线 {bid} 的 Gs/Bs 以接地 wye 导纳定义，不接受 delta 别名")
        element = _admittance_shunt(master_lines, allocate("_ts_busshunt"), map_bus(bid),
                                    bus_base[bid], gs, bs, nodes=nodes)
        _fidelity_record(master_lines, role="bus_shunt", bus=map_bus(bid), input_bus=bid,
                         gs_mw=gs, bs_mvar=bs, elements=[element], model="constant_admittance")

    master_lines.extend(["", "// 负荷定义"])
    if load_norm is not None:
        # 独立负荷表：每行一个负荷，是表达不平衡算例（单相负荷接在哪一相）的常规方式。
        # 一旦提供，就完全取代"从母线表派生三相负荷"的旧路径，避免同一负荷被建两次。
        for idx, row in load_norm.iterrows():
            load_lid = _id_text(row.get("load_id")) or f"LD{idx + 1}"
            bid = _id_text(row.get("bus_id"))
            if not bid:
                raise ValueError(f"负荷表第 {idx + 1} 行缺少 bus_id")
            p_kw = _to_float(row.get("p_kw"), _to_float(row.get("p_load_mw")) * 1000.0)
            q_kvar = _to_float(row.get("q_kvar"), _to_float(row.get("q_load_mvar")) * 1000.0)
            if abs(p_kw) <= 1e-12 and abs(q_kvar) <= 1e-12:
                continue
            base_kv = _to_float(row.get("base_kv"), bus_base.get(bid, source_base_kv))
            # 独立负荷表：相别必须逐行给全（空白 = 无法判断是三相还是漏填，宁可报错）
            nodes, _ = _extract_nodes(
                row, phase_cols_load, decl, f"负荷 {load_lid}", blank_policy="error"
            )
            n_phases = 3 if nodes is None else len([n for n in nodes if n != 0])
            if n_phases not in (1, 2, 3):
                raise ValueError(f"负荷 {load_lid} 的相数不支持：{n_phases}（仅支持 1/2/3）")
            kv = pm.load_kv_for_nodes(base_kv, nodes)
            conn_text = ""
            if "conn" in row and not _is_missing_scalar(row.get("conn")):
                try:
                    conn_text = f" Conn={pm.normalize_conn(row.get('conn'))}"
                    decl.conn_declared += 1
                except pm.PhaseSpecError as exc:
                    raise ValueError(f"负荷 {load_lid} 的接线方式不合法：{exc}") from exc
            master_lines.append(
                f"New Load.Load_{load_lid} Bus1={pm.node_ref(map_bus(bid), nodes)} "
                f"Phases={n_phases} kV={kv:.16g} kW={p_kw:.16g} kvar={q_kvar:.16g}{conn_text}"
            )
    else:
        for _, row in bus_df.iterrows():
            bid = _id_text(row["bus_id"])
            base_kv = _to_float(row.get("base_kv"), source_base_kv)
            p_kw = _to_float(row.get("p_kw"), _to_float(row.get("p_load_mw")) * 1000.0)
            q_kvar = _to_float(row.get("q_kvar"), _to_float(row.get("q_load_mvar")) * 1000.0)
            if abs(p_kw) <= 1e-12 and abs(q_kvar) <= 1e-12:
                continue
            nodes, _ = _extract_nodes(row, phase_cols_bus, decl, f"母线 {bid} 的负荷")
            n_phases = 3 if nodes is None else len([n for n in nodes if n != 0])
            if n_phases not in (1, 2, 3):
                raise ValueError(f"母线 {bid} 的负荷相数不支持：{n_phases}（仅支持 1/2/3）")
            kv = pm.load_kv_for_nodes(base_kv, nodes)
            conn_text = ""
            if "conn" in row and not _is_missing_scalar(row.get("conn")):
                try:
                    conn_text = f" Conn={pm.normalize_conn(row.get('conn'))}"
                    decl.conn_declared += 1
                except pm.PhaseSpecError as exc:
                    raise ValueError(f"母线 {bid} 的接线方式不合法：{exc}") from exc
            master_lines.append(
                f"New Load.Load_{bid} Bus1={pm.node_ref(map_bus(bid), nodes)} "
                f"Phases={n_phases} kV={kv:.16g} kW={p_kw:.16g} kvar={q_kvar:.16g}{conn_text}"
            )

    master_lines.extend(["", "// 发电机定义（MATPOWER PV 机映射为定电压节点，PQ 机映射为固定功率注入）"])
    for _, row in bus_df.iterrows():
        bid = _id_text(row["bus_id"])
        if bid in slack_buses:
            continue
        p_kw = _to_float(row.get("p_gen_kw"), _to_float(row.get("p_gen_mw")) * 1000.0)
        q_kvar = _to_float(row.get("q_gen_kvar"), _to_float(row.get("q_gen_mvar")) * 1000.0)
        btype = str(row.get("type", "")).strip().lower()
        online = row.get("_generator_online", btype == "pv")
        if _is_missing_scalar(online):
            online = btype == "pv"
        is_zero_pv = btype == "pv" and bool(online) and abs(p_kw) <= 1e-12 and abs(q_kvar) <= 1e-12
        if abs(p_kw) > 1e-12 or abs(q_kvar) > 1e-12 or is_zero_pv:
            kv = _to_float(row.get("base_kv"), source_base_kv)
            vpu = _to_float(row.get("v_set_pu"), 1.0)
            # 发电机同样支持相别（例如只接在某一相的分布式电源）
            gen_nodes, _ = _extract_nodes(row, phase_cols_bus, decl, f"母线 {bid} 的发电机")
            gen_phases = 3 if gen_nodes is None else len([n for n in gen_nodes if n != 0])
            if gen_phases not in (1, 2, 3):
                raise ValueError(f"母线 {bid} 的发电机相数不支持：{gen_phases}（仅支持 1/2/3）")
            gen_kv = pm.load_kv_for_nodes(kv, gen_nodes)
            gen_bus = pm.node_ref(map_bus(bid), gen_nodes)
            btype = str(row.get("type", "")).strip().lower()
            is_pv = btype in {"pv", "2"} or (
                btype not in {"pq", "1", ""} and abs(vpu - 1.0) > 1e-9
            )
            if is_pv:
                # Model=3：恒定有功 + 维持 Vpu 电压（真正的 PV 节点），无功在上下限内自动调节。
                # 此前一律写成 Model=1（恒 P/Q），Vpu 被架空 → PV 节点不再支撑电压，
                # 实测 case03 最低电压因此被系统性带低 0.04 pu。
                lower, upper, native_kva, provenance = _generator_q_policy(
                    row, bid, p_kw, q_kvar, s_base_mva, enforce_q_limits)
                limits = f" minkvar={lower:.16g} maxkvar={upper:.16g}"
                rating = f" kVA={native_kva:.16g}"
                if not enforce_q_limits:
                    notes.append(f"PV 母线 {bid} 使用参考潮流非强制 Q 策略，有限数值边界为非设备真实容量；显式 gen_rating_mva/kva 始终约束运行点")
                master_lines.append(
                    f"New Generator.Gen_{bid} Bus1={gen_bus} Phases={gen_phases} kV={gen_kv:.16g} "
                    f"kW={p_kw:.16g} kvar={q_kvar:.16g}{rating} Model=3 Vpu={vpu:.16g}{limits}"
                )
                _fidelity_record(master_lines, **provenance)
            else:
                if enforce_q_limits or not _is_missing_scalar(row.get("gen_rating_mva")) or not _is_missing_scalar(row.get("gen_rating_kva")):
                    lower, upper, native_kva, provenance = _generator_q_policy(
                        row, bid, p_kw, q_kvar, s_base_mva, enforce_q_limits)
                    if not lower <= q_kvar <= upper:
                        raise ValueError(f"母线 {bid} 的固定 Q 注入超出显式无功/额定容量边界")
                    _fidelity_record(master_lines, **provenance)
                master_lines.append(
                    f"New Generator.Gen_{bid} Bus1={gen_bus} Phases={gen_phases} kV={gen_kv:.16g} "
                    f"kW={p_kw:.16g} kvar={q_kvar:.16g} Model=1 Vpu={vpu:.16g}"
                )

    levels = sorted({source_base_kv, *[v for v in bus_base.values() if v > 0]})
    master_lines.extend(["", "// 求解设置", f"Set VoltageBases=[{ ' '.join(f'{v:.16g}' for v in levels) }]", "Calcvoltagebases", "Solve", ""])
    master_lines.append(f"// 电压约束参考：Vmin={v_min_pu} pu, Vmax={v_max_pu} pu")

    # ---- 相别声明收尾：按实际统计结果重写 warning 行，并写入相别标记 ----
    # 关键区分：只有**真的按输入相别建模**时，才不必再声明"相别不平衡未保留"；
    # 否则（无相别列）必须保持旧的保守声明，不能因为改了代码就把话说满。
    if decl.conn_ignored_on_lines:
        decl.notes.append(
            f"{decl.conn_ignored_on_lines} 条线路的 conn 列已忽略："
            "OpenDSS 的 Line 没有 Conn 属性（接线方式对线路无意义，只对负荷/变压器有意义）"
        )
    if decl.has_phase_columns:
        conversion_warning = (
            "原始表格/矩阵已转换为 OpenDSS 等值模型；**相别信息已按输入建模**"
            f"（其中 {decl.elements_with_phase} 个元件显式声明相别、"
            f"{decl.assumed_three_phase} 个元件按默认三相处理）；"
            f"{q_policy_warning}；控制器和部分原始求解器语义需另做交叉核验。"
        )
    master_lines[2] = (
        f"! [Tianshu Conversion] warning={conversion_warning}{sys_unread_note}"
    )
    flags = decl.flags()
    flag_text = "; ".join(f"{k}={v}" for k, v in flags.items())
    master_lines.insert(5, f"! [Tianshu Conversion] phase_model={flag_text}")
    if decl.unbalanced:
        master_lines.insert(
            6,
            f"! [Tianshu Conversion] phase_note="
            f"{decl.elements_single_or_partial} 个单相/两相元件已按相别接入（分相建模）",
        )
    for note in decl.notes:
        master_lines.insert(7, f"! [Tianshu Conversion] phase_warning={note}")

    load_records = len(load_norm) if load_norm is not None else len(bus_df)
    generated_loads = sum(line.startswith("New Load.") for line in master_lines)
    master_lines.insert(5, f"! [Tianshu Conversion] load_source={'explicit_load' if load_norm is not None else 'derived_bus'}; input_load_records={load_records}; generated_load_records={generated_loads}; zero_load_records={load_records - generated_loads}")
    for note in dict.fromkeys(notes):
        safe_note = str(note).replace("\r", " ").replace("\n", " ")
        master_lines.insert(7, f"! [Tianshu Conversion] conversion_note={safe_note}")

    fidelity_prefix = "! [Tianshu Fidelity] "
    fidelity_records = [line[len(fidelity_prefix):] for line in master_lines if line.startswith(fidelity_prefix)]
    fidelity_digest = hashlib.sha256("\n".join(fidelity_records).encode("utf-8")).hexdigest()
    circuit_index = next(i for i, line in enumerate(master_lines) if line.startswith("New Circuit."))
    master_lines.insert(circuit_index, f"! [Tianshu Conversion] fidelity_records={len(fidelity_records)}; fidelity_sha256={fidelity_digest}")

    # 有限原始数值经阻抗/电纳/基准缩放仍可能溢出；拒绝发布含非有限
    # 参数的 DSS，而不是把 inf 留给原生引擎才报错。
    for command in master_lines:
        if command.startswith("New ") and re.search(r"(?i)(?<![\w.])[-+]?(?:nan|inf(?:inity)?)(?![\w.])", command):
            raise ValueError("单位或基准换算后的模型参数不是有限数值，请检查输入量级")

    master_path = os.path.join(output_dir, "Master.dss")
    # 原有模型在新文件完整写入前始终可读；附件发布还在上一层以目录事务
    # 同时切换 Master 与元数据。失败只清理本次私有临时文件。
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_dir,
                                         prefix=".Master-", suffix=".dss.tmp", delete=False) as f:
            temp_path = f.name
            f.write("\n".join(master_lines))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, master_path)
        temp_path = None
    finally:
        if temp_path is not None and os.path.isfile(temp_path):
            os.unlink(temp_path)
    return master_path


def convert_xlsx(xlsx_path: str, output_dir: str) -> str:
    xl = pd.ExcelFile(xlsx_path)
    sheets = {str(name).strip().lower(): name for name in xl.sheet_names}
    bus_name = sheets.get("bus")
    line_name = sheets.get("line")
    if not bus_name or not line_name:
        raise ValueError("xlsx 必须包含 Bus 和 Line 工作表")
    bus = xl.parse(bus_name, dtype=object, keep_default_na=False, na_values=[""])
    line = xl.parse(line_name, dtype=object, keep_default_na=False, na_values=[""])
    system = xl.parse(sheets["system"], dtype=object, keep_default_na=False, na_values=[""]) if "system" in sheets else None
    # 可选 Load 工作表：不平衡算例用它表达"哪个负荷接在哪一相"
    load_sheet = sheets.get("load") or sheets.get("loads")
    load = xl.parse(load_sheet, dtype=object, keep_default_na=False, na_values=[""]) if load_sheet else None
    return build_dss_from_frames(system, bus, line, output_dir, source_label="case_xlsx", load_df=load)


def _read_csv(path: str) -> pd.DataFrame:
    # Keep malformed literal NA/NaN visible to validators; preserve integer IDs.
    return pd.read_csv(path, dtype=object, keep_default_na=False, na_values=[""])


def convert_csv(csv_path: str, output_dir: str) -> str:
    """支持混合行 CSV、目录或 zip 内的 bus.csv/line.csv/system.csv。"""
    os.makedirs(output_dir, exist_ok=True)
    if os.path.isdir(csv_path):
        return _convert_csv_dir(csv_path, output_dir)
    if csv_path.lower().endswith(".zip"):
        extract_dir = os.path.join(output_dir, "csv_extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(csv_path, "r") as archive:
            archive.extractall(extract_dir)
        return _convert_csv_dir(extract_dir, output_dir)

    df = _normalize_columns(_read_csv(csv_path))
    if "type" in df.columns and df["type"].astype(str).str.lower().str.strip().isin(
        {"bus", "node", "line", "branch", "transformer", "system", "load", "loads"}
    ).any():
        type_values = df["type"].astype(str).str.lower().str.strip()
        bus = df[type_values == "bus"].copy()
        line = df[type_values.isin({"line", "branch", "transformer"})].copy()
        if bus.empty:
            raise ValueError("csv 的 type 列中未同时找到 bus 和 line/branch 记录")
        # type=line 是混合 CSV 的记录类别；实际电气 kind 可另列声明。
        # type=transformer 则携带电气含义，去掉类别列前必须保留。
        if "kind" not in line.columns:
            line["kind"] = None
        line.loc[type_values.loc[line.index] == "transformer", "kind"] = "transformer"
        system_rows = df[type_values == "system"].copy()
        # 可选 load 记录：不平衡算例在混合 CSV 里用 type=load 表达分相负荷
        load_rows = df[type_values.isin({"load", "loads"})].copy()
        for frame in (bus, line, system_rows, load_rows):
            frame.drop(columns=["type"], errors="ignore", inplace=True)
        system = system_rows if not system_rows.empty else None
        load = load_rows if not load_rows.empty else None
        return build_dss_from_frames(
            system, bus, line, output_dir, source_label="case_csv", load_df=load
        )

    normalized = _normalize_bus_frame(df) if _first_existing(df, ("bus_id", "id", "bus", "bus_name", "name")) else None
    if normalized is not None and (os.path.basename(csv_path).lower() == "bus.csv" or "p_load_mw" in normalized.columns or "p_kw" in normalized.columns):
        bus_path = os.path.join(os.path.dirname(csv_path), "bus.csv")
        line_path = os.path.join(os.path.dirname(csv_path), "line.csv")
        if os.path.isfile(line_path):
            return _build_csv_siblings(normalized, _read_csv(line_path), os.path.dirname(csv_path), output_dir)
    line = _normalize_line_frame(df)
    bus_path = os.path.join(os.path.dirname(csv_path), "bus.csv")
    if not os.path.isfile(bus_path):
        raise ValueError("仅上传 line.csv，需要同时提供 bus.csv")
    return _build_csv_siblings(_read_csv(bus_path), line, os.path.dirname(csv_path), output_dir)


def _build_csv_siblings(bus: pd.DataFrame, line: pd.DataFrame, directory: str, output_dir: str) -> str:
    system_path = os.path.join(directory, "system.csv")
    load_path = next((os.path.join(directory, name) for name in ("load.csv", "loads.csv")
                      if os.path.isfile(os.path.join(directory, name))), None)
    return build_dss_from_frames(_read_csv(system_path) if os.path.isfile(system_path) else None,
                                  bus, line, output_dir, source_label="case_csv",
                                  load_df=_read_csv(load_path) if load_path else None)


def _convert_csv_dir(csv_dir: str, output_dir: str) -> str:
    files: dict[str, str] = {}
    for root, _, names in os.walk(csv_dir):
        for name in names:
            if name.lower().endswith(".csv"):
                files.setdefault(name.lower(), os.path.join(root, name))
    bus_path = next((p for n, p in files.items() if os.path.basename(n) == "bus.csv"), None)
    line_path = next((p for n, p in files.items() if os.path.basename(n) in {"line.csv", "branch.csv"}), None)
    system_path = next((p for n, p in files.items() if os.path.basename(n) == "system.csv"), None)
    load_path = next((p for n, p in files.items() if os.path.basename(n) in {"load.csv", "loads.csv"}), None)
    if bus_path and line_path:
        system = _read_csv(system_path) if system_path else None
        load = _read_csv(load_path) if load_path else None
        return build_dss_from_frames(
            system, _read_csv(bus_path), _read_csv(line_path), output_dir,
            source_label="case_csv", load_df=load,
        )
    csv_files = list(files.values())
    if len(csv_files) == 1:
        return convert_csv(csv_files[0], output_dir)
    raise ValueError("目录或 zip 中必须包含 bus.csv 和 line.csv（system.csv 可选）")


def convert_json(json_path: str, output_dir: str) -> str:
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    validate_json_payload(data)
    system = data.get("system")
    if isinstance(system, dict):
        system_df = pd.DataFrame([system])
    elif isinstance(system, list):
        system_df = pd.DataFrame(system)
    else:
        system_df = None
    # 可选 load 数组：不平衡算例用它逐条声明相别（未提供时仍从母线表派生）
    raw_load = data.get("load") if "load" in data else data.get("loads")
    load_df = pd.DataFrame(raw_load) if isinstance(raw_load, list) else None
    return build_dss_from_frames(
        system_df,
        pd.DataFrame(data["bus"]),
        pd.DataFrame(data["line"]),
        output_dir,
        source_label="case_json",
        load_df=load_df,
    )


def convert_matpower_m(m_path: str, output_dir: str) -> str:
    """Convert validated literal MATPOWER v2 data without executing MATLAB."""
    case = parse_matpower_case(m_path)
    notes = list(case.warnings)
    buses: list[dict[str, Any]] = []
    bus_base: dict[int, float] = {}
    for row in case.bus:
        bus_i, bus_type = int(row[0]), int(row[1])
        bus_base[bus_i] = row[9]
        buses.append({
            "bus_id": str(bus_i), "type": "slack" if bus_type == 3 else str(bus_type),
            "base_kv": row[9], "p_load_mw": row[2], "q_load_mvar": row[3],
            "gs_mw": row[4], "bs_mvar": row[5],
            "p_gen_mw": 0.0, "q_gen_mvar": 0.0, "v_set_pu": row[7],
            "_generator_online": False, "_generator_count": 0,
        })
    bus_by_id = {int(bus["bus_id"]): bus for bus in buses}
    for row in case.gen:
        if len(row) > 7 and row[7] <= 0:
            continue
        bus_id = int(row[0])
        bus = bus_by_id[bus_id]
        if len(row) >= 16 and any(value != 0 for value in row[10:16]):
            raise ValueError(f"MATPOWER 母线 {bus_id} 的非零 P-Q 能力曲线暂不支持；不能仅用矩形 Qmin/Qmax 替代真实曲线")
        if len(row) > 5:
            vg = row[5]
            if vg <= 0:
                raise ValueError(f"MATPOWER 母线 {bus_id} 的在线发电机 Vg 必须大于 0")
            if bus["_generator_count"] and "_online_vg" in bus and not math.isclose(bus["_online_vg"], vg, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"MATPOWER 同母线 {bus_id} 的在线发电机 Vg 不一致，不能合并调压设定")
            bus["v_set_pu"] = vg
            bus["_online_vg"] = vg
        bus["_generator_online"] = True
        bus["_generator_count"] += 1
        bus["p_gen_mw"] += row[1]
        bus["q_gen_mvar"] += row[2]
        if len(row) > 4:
            if row[4] > row[3]:
                raise ValueError(f"MATPOWER 母线 {bus_id} 的发电机 Qmin 不能大于 Qmax")
            bus["qmax_mvar"] = bus.get("qmax_mvar", 0.0) + row[3]
            bus["qmin_mvar"] = bus.get("qmin_mvar", 0.0) + row[4]
        else:
            bus["_q_limits_complete"] = False
        if len(row) > 6 and row[6] != 0:
            notes.append("MATPOWER mBase 是机组标幺基准，不作为设备额定容量；Q 能力按所选限值策略解释")
    for bus in buses:
        if bus["_generator_count"] > 1:
            notes.append(f"MATPOWER 母线 {bus['bus_id']} 合并 {bus['_generator_count']} 台在线发电机；聚合 Qmin={bus.get('qmin_mvar', 0):.16g} MVAr，Qmax={bus.get('qmax_mvar', 0):.16g} MVAr")
    lines: list[dict[str, Any]] = []
    for idx, row in enumerate(case.branch, start=1):
        fbus, tbus = int(row[0]), int(row[1])
        ratio = row[8] if len(row) > 8 else 0.0
        if ratio < 0:
            raise ValueError(f"MATPOWER 支路 {idx} 的 ratio 不能为负数")
        shift = row[9] if len(row) > 9 else 0.0
        lines.append({
            "line_id": f"L{idx}", "from_bus": str(fbus), "to_bus": str(tbus),
            "r_pu": row[2], "x_pu": row[3], "b_pu": row[4] if len(row) > 4 else 0.0,
            "rate_mva": row[5] if len(row) > 5 and row[5] > 0 else case.base_mva,
            "_rating_explicit": len(row) > 5 and row[5] > 0,
            "status": int(row[10]) if len(row) > 10 else 1,
            "kind": "transformer" if ratio > 0 or shift != 0 or abs(bus_base[fbus] - bus_base[tbus]) > 1e-9 else "line",
            "tap_ratio": ratio if ratio > 0 else 1.0, "shift_deg": shift,
        })
    system_df = pd.DataFrame([{"parameter": "S_BASE_MVA", "value": case.base_mva}])
    return build_dss_from_frames(system_df, pd.DataFrame(buses), pd.DataFrame(lines),
                                 output_dir, source_label="case_matpower", conversion_notes=notes)


# ---------------------------------------------------------------------------
# 转换元数据打戳
#
# 为什么需要：产物（Master.dss）会被长期复用，"换算例"直接拿旧产物去算。
# 转换器改进后，老算例不会重新转换 —— 修复对它完全无效且毫无提示。
# 这里给四个公开入口统一打戳（转换器 id + 版本 + 源文件 + 源文件 sha256），
# 引用算例时由 conversion_cache.refresh_master_if_stale 校验并自动重建。
#
# ⚠️ 改了转换逻辑，必须同步升 conversion_cache.CONVERTER_VERSIONS 的版本号，
#    否则老算例不会被重建，等于修复白做。
# ---------------------------------------------------------------------------


def _stamp(fn):
    """包装转换入口：转换成功后写一份 .conversion.json。"""

    @functools.wraps(fn)
    def wrapper(source_path: str, output_dir: str, *args: Any, **kwargs: Any) -> str:
        master = fn(source_path, output_dir, *args, **kwargs)
        try:
            from .conversion_cache import write_meta

            write_meta(output_dir, "dss_from_tables", source_path)
        except Exception:  # noqa: BLE001 - 打戳失败绝不能影响转换结果
            pass
        return master

    return wrapper


convert_xlsx = _stamp(convert_xlsx)
convert_csv = _stamp(convert_csv)
convert_json = _stamp(convert_json)
convert_matpower_m = _stamp(convert_matpower_m)
