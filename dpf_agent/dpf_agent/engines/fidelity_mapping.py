"""Restore source-model identities from explicit conversion metadata.

Native electrical readings remain authoritative. Synthesized phase windings
and charging elements are grouped by their measured source-model ports;
    fixed bus admittance is reported separately from load and network loss.
"""
from __future__ import annotations

import json
import hashlib
import math
import re
from dataclasses import replace
from pathlib import Path

from ..schemas.enums import ErrorCode, ResultScope

MARKER = "! [Tianshu Fidelity] "
_MANIFEST = re.compile(r"^!\s*\[Tianshu Conversion\]\s*fidelity_records\s*=\s*(\d+)\s*;\s*fidelity_sha256\s*=\s*([0-9a-fA-F]{64})\s*$")


def _fail(message):
    from .opendss_adapter import AdapterError
    raise AdapterError(ErrorCode.RESULT_MAPPING_FAILED, "转换保真映射失败：" + message)


def _source_definitions(files, owned):
    """Resolve explicit New/Edit ports without evaluating DSS expressions.

    Normal generated models are in one file. Cross-file edits and advanced
    inheritance/batch edits are refused because sorted directory order does not
    prove their native execution order. Ordinary unmapped DSS is unaffected.
    """
    from .dss_source import _logical_lines, _split_tokens, _unquote, _norm_bus, parse_dss_text

    states = {}
    for file, text in files:
        for _, command in _logical_lines(text):
            tokens = _split_tokens(command)
            verb = tokens[0].lower()
            if verb == "batchedit":
                _fail("带保真映射的源体暂不支持 BatchEdit；请展开为显式 New/Edit")
            if verb not in {"new", "edit"}:
                continue
            elements = parse_dss_text(command, str(file))
            if len(elements) != 1 or elements[0].full_name.lower() not in owned:
                continue
            element = elements[0]
            name = element.full_name.lower()
            if verb == "new":
                if name in states:
                    _fail("映射原生元件重复 New；请使用显式 Edit：" + name)
                state = {"ports": {}, "wdg": 1, "windings": 2, "enabled": True, "file": file}
                states[name] = state
            else:
                if name not in states:
                    _fail("映射元件在 Edit 前没有可解析的 New：" + name)
                state = states[name]
                if state["file"] != file:
                    _fail("映射元件跨文件 Edit 顺序无法可靠确认；请展开到同一文件：" + name)
            properties = list(element.tokens)
            index = 0
            while index < len(properties):
                token = properties[index]
                if "=" in token:
                    key, _, value = token.partition("=")
                    if not value and index + 1 < len(properties):
                        index += 1
                        value = properties[index]
                elif index + 1 < len(properties) and properties[index + 1] == "=":
                    key = token
                    index += 2
                    value = properties[index] if index < len(properties) else ""
                else:
                    _fail("映射元件含未支持的非 key=value 属性：" + name)
                index += 1
                key, value = key.strip().lower(), _unquote(value.strip())
                if key in {"like", "makelike"}:
                    _fail("映射原生元件不支持 Like 继承；请展开显式属性：" + name)
                if key in {"wdg", "winding", "windings"}:
                    if not re.fullmatch(r"[1-9]\d*", value):
                        _fail("映射绕组编号/数量不是正整数字面量：" + name)
                    number = int(value)
                    if key == "windings":
                        if number != state["windings"]:
                            state["ports"] = {}
                        state["windings"] = number
                    else:
                        state["wdg"] = number
                elif key == "buses":
                    values = _split_tokens(value.strip("[]()").replace(",", " "))
                    if not values:
                        _fail("映射原生元件 buses 列表为空：" + name)
                    state["ports"] = {i: _norm_bus(_unquote(bus)) for i, bus in enumerate(values, 1)}
                elif key in {"bus", "bus1", "bus2"}:
                    terminal = state["wdg"] if key == "bus" and element.kind == "transformer" else (2 if key == "bus2" else 1)
                    state["ports"][terminal] = _norm_bus(value)
                elif key == "enabled":
                    if value.lower() not in {"yes", "no", "true", "false", "1", "0"}:
                        _fail("映射原生元件 Enabled 不是明确布尔字面量：" + name)
                    state["enabled"] = value.lower() in {"yes", "true", "1"}
    missing = sorted(set(owned) - set(states))
    if missing:
        _fail("映射原生元件在源体中缺失：" + ", ".join(missing[:5]))
    return states


def _validate_source_ports(records, states):
    from .dss_source import _norm_bus

    for record in records:
        if record["role"] == "generator":
            continue
        expected_enabled = record.get("enabled", True)
        if type(expected_enabled) is not bool:
            _fail("源对象 enabled 必须是明确布尔值")
        elements = ((record.get("series_elements", []) + record.get("shunt_elements", []))
                    if record["role"] == "branch" else record["elements"])
        for name in elements:
            if states[name.lower()]["enabled"] != expected_enabled:
                _fail("源体 Enabled 与元数据不一致：" + name)
        sides = (("from_bus", record["ports_from"]), ("to_bus", record["ports_to"])) if record["role"] == "branch" else (
            ("bus", [{"element": name, "terminal": 1} for name in record["elements"]]),)
        for bus_key, ports in sides:
            expected = _norm_bus(str(record[bus_key]))
            for port in ports:
                actual = states[port["element"].lower()]["ports"].get(port["terminal"])
                if not actual or actual != expected:
                    _fail(f"端口母线与 {bus_key} 不一致：{port['element']} terminal={port['terminal']}，声明 {expected}，源体 {actual}")


def read_fidelity_mapping(case_workspace: str) -> list[dict]:
    if not case_workspace:
        return []
    from .dss_source import parse_dss_text
    records, identities, files, reserved = [], {}, [], set()
    for file in sorted(path for path in Path(case_workspace).rglob("*") if path.is_file() and path.suffix.lower() == ".dss"):
        text = file.read_text(encoding="utf-8-sig", errors="replace")
        files.append((file, text))
        declarations, payloads = [], []
        native = parse_dss_text(text, str(file))
        synthetic = {element.full_name.lower() for element in native
                     if element.kind in {"transformer", "reactor"} and element.short_name.lower().startswith("_ts_")}
        reserved.update(synthetic)
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("! [Tianshu Conversion]") and ("fidelity_records" in stripped or "fidelity_sha256" in stripped):
                match = _MANIFEST.fullmatch(stripped)
                if not match:
                    _fail(f"{file.name} 中的保真完整性声明非法")
                declarations.append((int(match[1]), match[2].lower()))
            if not stripped.startswith(MARKER):
                continue
            payloads.append(stripped[len(MARKER):])
        if len(declarations) > 1:
            _fail(f"{file.name} 存在重复保真完整性声明")
        if (payloads or synthetic) and not declarations:
            _fail(f"{file.name} 缺少 fidelity_records/fidelity_sha256 完整性声明")
        if declarations:
            actual = (len(payloads), hashlib.sha256("\n".join(payloads).encode("utf-8")).hexdigest())
            if declarations[0] != actual:
                _fail(f"{file.name} 的保真记录数量或哈希不一致，禁止按原生设备展开")
        for payload in payloads:
            try:
                record = json.loads(payload)
            except (ValueError, TypeError):
                _fail(f"{file.name} 中的元数据 JSON 非法")
            if not isinstance(record, dict) or record.get("role") not in {"branch", "bus_shunt", "generator"}:
                _fail("未知元数据角色")
            key = (record["role"], str(record.get("id", record.get("bus", record.get("name", "")))).lower())
            if key in identities:
                _fail("同一源对象存在重复或冲突定义")
            identities[key] = record
            if record["role"] == "branch":
                if not record.get("id") or not record.get("from_bus") or not record.get("to_bus"):
                    _fail("源支路身份或端点缺失")
                for side in ("ports_from", "ports_to"):
                    ports = record.get(side)
                    if not isinstance(ports, list) or not ports:
                        _fail("源支路的实测端口缺失")
                    seen = set()
                    for port in ports:
                        if (not isinstance(port, dict) or not isinstance(port.get("element"), str)
                                or type(port.get("terminal")) is not int or port["terminal"] < 1):
                            _fail("非法端口")
                        identity = (port["element"].lower(), port["terminal"])
                        if identity in seen:
                            _fail("同一支路端口重复，禁止重复求和")
                        seen.add(identity)
                for field in ("series_elements", "shunt_elements", "internal_buses"):
                    if not isinstance(record.get(field, []), list):
                        _fail("非法合成元件清单")
            elif record["role"] == "bus_shunt":
                if not record.get("bus") or not isinstance(record.get("elements"), list) or not record["elements"]:
                    _fail("固定并联导纳身份缺失")
            records.append(record)
    owned_elements = {}
    for record in records:
        if record["role"] == "generator":
            continue
        identity = (record["role"], record.get("id", record.get("bus")))
        elements = (record.get("series_elements", []) + record.get("shunt_elements", [])) if record["role"] == "branch" else record["elements"]
        own = set()
        for element in elements:
            if not isinstance(element, str) or "." not in element:
                _fail("非法原生元件身份")
            key = element.lower()
            if key in own or key in owned_elements:
                _fail("原生元件重复归属")
            own.add(key)
            owned_elements[key] = identity
        if record["role"] == "branch":
            ports = record["ports_from"] + record["ports_to"]
            identities = [(p["element"].lower(), p["terminal"]) for p in ports]
            if len(set(identities)) != len(identities) or any(p[0] not in own for p in identities):
                _fail("支路两端口重复或引用其他源对象元件")
    if reserved - set(owned_elements):
        _fail("保真保留元件缺少源对象归属：" + ", ".join(sorted(reserved - set(owned_elements))[:5]))
    if records:
        states = _source_definitions(files, owned_elements)
        _validate_source_ports(records, states)
    return records


def internal_buses(records):
    return {str(bus).lower() for r in records if r["role"] == "branch" for bus in r.get("internal_buses", [])}


def synthetic_elements(records):
    return {str(name).lower() for r in records if r["role"] == "branch"
            for field in ("series_elements", "shunt_elements") for name in r.get(field, [])}


def _port_power(reader, element, terminal):
    value = reader(str(element), int(terminal))
    if value is None or len(value) < 2 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in value[:2]):
        _fail(f"无法读取 {element} 端口 {terminal} 的有限实测功率")
    return value[0], value[1]


def apply_fidelity_mapping(raw, records, reader, scopes, actual_bus_names=None):
    """reader(element, terminal) returns measured total kW/kvar per terminal."""
    if not records:
        return raw
    hidden = internal_buses(records)
    present_hidden = hidden & {str(n).lower() for n in (actual_bus_names if actual_bus_names is not None else [b["name"] for b in raw.buses])}
    synthetic = synthetic_elements(records)
    raw.buses = [b for b in raw.buses if str(b["name"]).lower() not in hidden]
    raw.lines = [v for v in raw.lines if str(v["name"]).lower() not in synthetic]
    raw.transformers = [v for v in raw.transformers if str(v["name"]).lower() not in synthetic]
    shunt_p = shunt_q = charging_p = charging_q = 0.0
    measured_shunts = set()
    measured_charging = set()
    for record in records:
        if record["role"] == "bus_shunt":
            for element in record["elements"]:
                if str(element).lower() in measured_shunts:
                    _fail("固定并联元件重复归属")
                measured_shunts.add(str(element).lower())
                p, q = _port_power(reader, element, 1)
                shunt_p += p
                shunt_q += q
        elif record["role"] == "branch" and record.get("enabled", True):
            for element in record.get("shunt_elements", []):
                if str(element).lower() in measured_charging or str(element).lower() in measured_shunts:
                    _fail("支路充电元件重复归属")
                measured_charging.add(str(element).lower())
                measured_p, measured_q = _port_power(reader, element, 1)
                charging_p += measured_p
                charging_q += measured_q
            # Read both sides even when not displayed: a missing synthetic
            # terminal must fail rather than silently publish partial sums.
            from_pq = [_port_power(reader, p["element"], p["terminal"]) for p in record["ports_from"]]
            to_pq = [_port_power(reader, p["element"], p["terminal"]) for p in record["ports_to"]]
            p, q = sum(v[0] for v in from_pq), sum(v[1] for v in from_pq)
            p_to, q_to = sum(v[0] for v in to_pq), sum(v[1] for v in to_pq)
            kind = record.get("original_kind", "line")
            value = {"name": ("Transformer." if kind == "transformer" else "Line.") + str(record["id"]),
                     "bus1": str(record["from_bus"]), "bus2": str(record["to_bus"]),
                     "p_kw": p, "q_kvar": q, "p_to_kw": p_to, "q_to_kvar": q_to,
                     "source_model": record.get("model", ""), "loss_kw": p + p_to}
            if kind == "transformer":
                rate = record.get("input_rate_mva")
                value["rated_kva"] = float(rate) * 1000 if isinstance(rate, (float, int)) and rate > 0 else None
                if ResultScope.TRANSFORMER in scopes:
                    raw.transformers.append(value)
            else:
                # No numerical-base kVA is presented as a physical rating.
                value.update(current_a=None, rating_a=None)
                if ResultScope.BRANCH in scopes:
                    raw.lines.append(value)
    if measured_shunts:
        raw.system.update(shunt_kw=shunt_p, shunt_kvar=shunt_q, shunt_elements=len(measured_shunts),
                          native_pd_losses_kw=raw.system.get("losses_kw"),
                          native_pd_losses_kvar=raw.system.get("losses_kvar"),
                          shunt_loss_policy="native_circuit_losses_excludes_shunt")
        # OpenDSS Circuit.Losses excludes shunt PD elements already. Real COM
        # acceptance caught double-subtraction here; retain the measured native
        # network losses and include the separately measured shunt in balance.
    if measured_charging:
        raw.system.update(branch_shunt_kw=charging_p, branch_shunt_kvar=charging_q,
                          branch_shunt_elements=len(measured_charging))
    raw.element_count["bus"] = max(0, raw.element_count.get("bus", len(raw.buses)) - len(present_hidden))
    # Counts are source objects even when the caller did not request that scope.
    for kind in ("line", "transformer"):
        removed = sum(1 for name in synthetic if name.startswith(kind + "."))
        added = sum(1 for r in records if r["role"] == "branch" and r.get("original_kind", "line") == kind and r.get("enabled", True))
        raw.element_count[kind] = max(0, raw.element_count.get(kind, 0) - removed) + added
    return raw


def measured_terminal_reader(ckt):
    """COM and dss-python share this structured, per-terminal reader."""
    cache = {}
    def read(element, terminal):
        key = (element.lower(), terminal)
        if key not in cache:
            selected = ckt.SetActiveElement(element)
            if selected is not None and int(selected) < 0:
                _fail("合成元件不存在：" + element)
            ce = ckt.ActiveCktElement
            if str(ce.Name).lower() != element.lower():
                _fail("合成元件身份不匹配：" + element)
            count, terminals = int(ce.NumConductors), int(ce.NumTerminals)
            values = list(ce.Powers or ())
            start, end = 2 * (terminal - 1) * count, 2 * terminal * count
            if count < 1 or not 1 <= terminal <= terminals or len(values) < end:
                _fail("合成元件端口数据缺失：" + element)
            cache[key] = (sum(float(x) for x in values[start:end:2]), sum(float(x) for x in values[start + 1:end:2]))
        return cache[key]
    return read


def restore_inventory(inventory, records):
    if not records:
        return inventory
    hidden, synthetic = internal_buses(records), synthetic_elements(records)
    branches = [r for r in records if r["role"] == "branch"]
    # Inventory endpoints have no object name. Remove only edges touching
    # explicitly internal buses; source-to-source native branches remain.
    endpoints = [v for v in inventory.line_endpoints if v[1].lower() not in hidden and v[2].lower() not in hidden]
    endpoints.extend((r.get("enabled", True), str(r["from_bus"]).lower(), str(r["to_bus"]).lower()) for r in branches)
    return replace(inventory,
        bus_count=max(0, inventory.bus_count - len(hidden & {str(n).lower() for n, kv in inventory.bus_kv_base})),
        bus_kv_base=tuple((n, kv) for n, kv in inventory.bus_kv_base if str(n).lower() not in hidden),
        line_count=max(0, inventory.line_count - sum(n.startswith("line.") for n in synthetic)) + sum(r.get("original_kind", "line") == "line" for r in branches),
        transformer_count=max(0, inventory.transformer_count - sum(n.startswith("transformer.") for n in synthetic)) + sum(r.get("original_kind") == "transformer" for r in branches),
        line_endpoints=tuple(endpoints))


def restore_summary(counts, base_kv, records):
    if not records:
        return counts, base_kv
    hidden, synthetic = internal_buses(records), synthetic_elements(records)
    counts = dict(counts)
    counts["bus"] = max(0, counts.get("bus", 0) - len(hidden & {k.lower() for k in base_kv}))
    for kind in ("line", "transformer"):
        counts[kind] = max(0, counts.get(kind, 0) - sum(n.startswith(kind + ".") for n in synthetic)) + sum(
            r["role"] == "branch" and r.get("original_kind", "line") == kind for r in records)
        mapped = [r for r in records if r["role"] == "branch" and r.get("original_kind", "line") == kind]
        if mapped:
            counts["active_" + kind] = counts[kind] - sum(not r.get("enabled", True) for r in mapped)
    return counts, {k: v for k, v in base_kv.items() if k.lower() not in hidden}
