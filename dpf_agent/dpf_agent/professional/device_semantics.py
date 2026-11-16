"""Conservative, source-preserving inspection of explicit native DSS connections.

Copyright (c) yangshaopeng. This module performs no I/O or solver calls. Its
``complete`` status covers the fields below, not compilation or engineering
acceptance. Missing OpenDSS defaults are deliberately never manufactured.
"""
from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections.abc import Mapping


_VERSION = "1.3"
_MATRICES = ("rmatrix", "xmatrix", "cmatrix")
_WINDING_FIELDS = ("bus", "conn", "kv", "kva", "tap", "%r")
_ARRAY_FIELDS = {"buses": "bus", "conns": "conn", "kvs": "kv",
                 "kvas": "kva", "taps": "tap", "%rs": "%r"}
_MAX_ITEMS = 100000
_MAX_CHARS = 20 * 1024 * 1024


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return value


def _parts(value):
    """Split one explicit DSS array without evaluating expressions."""
    value = value.strip()
    if len(value) < 2 or value[0] not in "[(" or value[-1] not in ")]":
        raise ValueError("explicit array delimiters required")
    tokens, token, quote = [], [], None
    for char in value[1:-1]:
        if quote:
            token.append(char)
            if char == quote:
                quote = None
        elif char in "\"'":
            token.append(char)
            quote = char
        elif char.isspace() or char == ",":
            if token:
                tokens.append("".join(token))
                token = []
        else:
            token.append(char)
    if quote:
        raise ValueError("unclosed array quote")
    if token:
        tokens.append("".join(token))
    if not tokens or len(tokens) > 20000:
        raise ValueError("array dimension outside inspection budget")
    return tokens


def _ref(prop):
    return {"source_file": prop["source_file"], "line": prop["line"],
            "property": prop["name"]}


def _missing():
    return {"status": "not_declared", "value": None, "source_refs": []}


def _declared(value, prop, **metadata):
    return {"status": "declared", "value": value,
            "source_refs": [_ref(prop)], **metadata}


def _four_matrix(value):
    """Recognize an explicit finite order-four matrix without reducing it."""
    if not isinstance(value, str):
        return False
    try:
        raw = value.strip()
        if len(raw) < 2 or raw[0] not in "[(" or raw[-1] not in ")]":
            return False
        rows = [[float(x) for x in re.split(r"[\s,]+", row.strip()) if x]
                for row in raw[1:-1].split("|")]
        lengths = list(map(len, rows))
        return (lengths in ([4, 4, 4, 4], [1, 2, 3, 4], [16], [10])
                and all(math.isfinite(x) for row in rows for x in row))
    except (TypeError, ValueError, OverflowError):
        return False


def _four_conductor_objects(objects):
    """Opt in only explicit four-conductor matrix Lines/libraries.

    Node numbers retain their native conductor order; none denotes a physical
    neutral here. Ordered copy/resize uncertainty is still reported separately.
    """
    properties = {}
    for obj in objects:
        props = {p["name"].casefold(): (index, p)
                 for index, p in enumerate(obj["properties"])}
        properties[obj["name"].casefold()] = (obj["class"].casefold(), props)

    def matrices(props, phase_key):
        phase_position = props.get(phase_key, (-1, {}))[0]
        return all(name in props and props[name][0] > phase_position
                   and _four_matrix(props[name][1]["value"]) for name in _MATRICES)

    qualified = {name for name, (kind, props) in properties.items()
                 if kind == "linecode" and "nphases" in props
                 and _scalar(props["nphases"][1]["value"]) == "4"
                 and matrices(props, "nphases")}
    for name, (kind, props) in properties.items():
        if kind != "line":
            continue
        phase = _scalar(props["phases"][1]["value"]) if "phases" in props else None
        code = "linecode." + _scalar(props["linecode"][1]["value"]).casefold() if "linecode" in props else None
        if phase != "4" and not (phase is None and code in qualified):
            continue
        buses = [_scalar(props[key][1]["value"]).split(".") if key in props else []
                 for key in ("bus1", "bus2")]
        if not all(len(bus) == 5 and bus[0]
                   and all(re.fullmatch(r"[0-9]{1,10}", token) and int(token) <= 2147483647
                           for token in bus[1:]) for bus in buses):
            continue
        if (matrices(props, "phases") or (code in qualified
                and all(_four_matrix(props[key][1]["value"]) for key in _MATRICES if key in props))):
            qualified.add(name)
    return qualified


class _Inspector:
    def __init__(self, inspection):
        self.issues = []
        self.outputs = []
        self.by_id = {}
        self.command_order = {}
        self._validate(inspection)
        self.inputs = inspection["objects"]
        self.four_conductor_objects = _four_conductor_objects(self.inputs)
        self.by_id = {obj["name"].casefold(): obj for obj in self.inputs}
        for index, command in enumerate(inspection.get("commands", [])):
            self.command_order[(command["file"], command["line"])] = index
        self.code_history = {}
        for obj in self.inputs:
            if obj["class"].casefold() != "linecode":
                continue
            positions, phase_events, ordered = [], [], True
            for prop in obj["properties"]:
                position = self.command_order.get((prop["source_file"], prop["line"]))
                if position is None:
                    ordered = False
                else:
                    positions.append(position)
                    if prop["name"].casefold() == "nphases":
                        phase_events.append((position, prop))
            phase_events.sort(key=lambda event: event[0])
            self.code_history[obj["name"].casefold()] = {
                "ordered": ordered, "last": max(positions, default=-1),
                "phase_positions": [event[0] for event in phase_events],
                "phase_properties": [event[1] for event in phase_events]}

    @staticmethod
    def _validate(inspection):
        if not isinstance(inspection, Mapping):
            raise ValueError("inspection must be an object")
        objects = inspection.get("objects")
        if not isinstance(objects, list) or len(objects) > _MAX_ITEMS:
            raise ValueError("objects must be a bounded list")
        total, chars, seen = 0, 0, set()
        for obj in objects:
            if not isinstance(obj, Mapping):
                raise ValueError("object entries must be objects")
            name, kind, props = obj.get("name"), obj.get("class"), obj.get("properties")
            if not isinstance(name, str) or not isinstance(kind, str) or not name or len(name) > 256:
                raise ValueError("object identifier/class must be bounded text")
            if name.casefold() in seen:
                raise ValueError("duplicate object identifiers")
            seen.add(name.casefold())
            if not isinstance(props, list):
                raise ValueError("properties must be an ordered list")
            total += len(props)
            if total > _MAX_ITEMS:
                raise ValueError("property count exceeds inspection budget")
            for prop in props:
                if not isinstance(prop, Mapping):
                    raise ValueError("property entries must be objects")
                if not all(isinstance(prop.get(key), str) for key in ("name", "value", "source_file")):
                    raise ValueError("property/source metadata must be text")
                line = prop.get("line")
                if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= _MAX_ITEMS:
                    raise ValueError("property line must be a bounded positive integer")
                chars += len(prop["name"]) + len(prop["value"]) + len(prop["source_file"])
                if chars > _MAX_CHARS:
                    raise ValueError("property text exceeds inspection budget")
        commands = inspection.get("commands", [])
        if not isinstance(commands, list) or len(commands) > _MAX_ITEMS:
            raise ValueError("commands must be a bounded list")
        for command in commands:
            if (not isinstance(command, Mapping) or not isinstance(command.get("file"), str)
                    or isinstance(command.get("line"), bool) or not isinstance(command.get("line"), int)):
                raise ValueError("command/source metadata is malformed")

    def issue(self, out, code, field, message, refs=(), *, invalid=False):
        item = {"code": code, "severity": "error" if invalid else "unresolved",
                "object_id": out["object_id"], "field": field,
                "message": message, "source_refs": list(refs)}
        out["issues"].append(item)
        self.issues.append(item)

    def number(self, out, prop, field, *, integer=False, positive=False, nonnegative=False):
        text = _scalar(prop["value"])
        try:
            if integer:
                if len(text) > 9 or not re.fullmatch(r"[0-9]+", text):
                    raise ValueError
                value = int(text)
            else:
                value = float(text)
            if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
                raise ValueError
        except (ValueError, OverflowError):
            self.issue(out, "invalid_explicit_number", field,
                       "Explicit numeric value is not finite or outside the field domain.", [_ref(prop)], invalid=True)
            return None
        return value

    def conn(self, out, prop, field):
        text = _scalar(prop["value"]).casefold()
        if text not in {"wye", "ln", "delta", "ll"}:
            self.issue(out, "unsupported_connection", field,
                       "Connection spelling is outside the explicit supported subset.", [_ref(prop)])
            return text
        return text

    def boolean(self, out, prop, field):
        text = _scalar(prop["value"]).casefold()
        if text in {"yes", "true", "1"}:
            return True
        if text in {"no", "false", "0"}:
            return False
        self.issue(out, "invalid_explicit_boolean", field,
                   "Explicit state must use a supported native boolean spelling.", [_ref(prop)], invalid=True)
        return None

    def bus(self, out, prop, field):
        raw = _scalar(prop["value"])
        tokens = raw.split(".")
        nodes, node_tokens = [], tokens[1:]
        value = {"raw": raw, "bus_name": tokens[0], "nodes": nodes,
                 "node_tokens": list(node_tokens), "nodes_explicit": bool(node_tokens)}
        if not tokens[0] or any(ord(c) < 32 for c in raw):
            self.issue(out, "invalid_bus_name", field,
                       "Bus name is empty or contains control characters.", [_ref(prop)], invalid=True)
        for token in node_tokens:
            if not re.fullmatch(r"[0-9]+", token):
                self.issue(out, "invalid_bus_node", field,
                           "Explicit bus nodes must be nonnegative native integer tokens.", [_ref(prop)], invalid=True)
                continue
            if len(token) > 10 or int(token) > 2147483647:
                self.issue(out, "bus_node_outside_inspected_range", field,
                           "Native node token is outside the bounded signed integer inspection range.", [_ref(prop)])
                continue
            nodes.append(int(token))
        nonzero = [n for n in nodes if n != 0]
        if len(nonzero) != len(set(nonzero)):
            self.issue(out, "repeated_nonzero_bus_node", field,
                       "Repeated nonzero nodes may intentionally short conductors; their intent is not assessed.", [_ref(prop)])
        return value

    def need(self, out, fields, name, prefix=""):
        item = fields.setdefault(name, _missing())
        if item["status"] == "not_declared":
            self.issue(out, "field_not_declared", prefix + name,
                       "No explicit native value is declared; no engine default is inserted.")
        return item

    def check_bus(self, out, item, field, conductors):
        if item["status"] != "declared" or not isinstance(item["value"], dict):
            return
        bus = item["value"]
        if not bus["nodes_explicit"]:
            self.issue(out, "bus_nodes_not_declared", field,
                       "Native nodes are omitted; engine node defaults are not reproduced.", item["source_refs"])
            return
        if conductors is None:
            self.issue(out, "conductor_count_unresolved", field,
                       "An explicit phase/connection basis for conductor count is unavailable.", item["source_refs"])
        elif len(bus["node_tokens"]) > conductors:
            self.issue(out, "too_many_bus_nodes", field,
                       "Explicit node count exceeds the known native conductor count.", item["source_refs"], invalid=True)
        elif len(bus["node_tokens"]) < conductors:
            self.issue(out, "bus_conductors_not_fully_declared", field,
                       "Some native conductor nodes are omitted; implicit neutral/node values are not filled.", item["source_refs"])

    def basic(self, obj, out):
        fields = out["fields"]
        positions = {}
        selected = {"phases", "nphases", "bus1", "bus2", "basekv", "kv", "conn", "linecode",
                    "normamps", "emergamps", "kva", "mva", "pmpp"} | set(_MATRICES)
        if obj["class"].casefold() in {"load", "generator", "pvsystem"}:
            selected.add("enabled")
        for index, prop in enumerate(obj["properties"]):
            name = prop["name"].casefold()
            if name not in selected:
                continue
            if name == "enabled":
                value = self.boolean(out, prop, name)
            elif name.startswith("bus"):
                value = self.bus(out, prop, name)
            elif name == "conn":
                value = self.conn(out, prop, name)
            elif name in {"phases", "nphases"}:
                value = self.number(out, prop, name, integer=True, positive=True)
                four_line = obj["name"].casefold() in self.four_conductor_objects
                if value is not None and value not in ({1, 2, 3, 4} if four_line else {1, 2, 3}):
                    incomplete_four = value == 4 and obj["class"].casefold() in {"line", "linecode"}
                    self.issue(out, "phase_count_outside_scope", name,
                               ("Four-conductor matrix declarations require complete explicit matrices and Line nodes."
                                if incomplete_four else "Only one-, two- and three-phase declarations are inspected."),
                               [_ref(prop)], invalid=incomplete_four)
                    value = None
            elif name in _MATRICES:
                value = self.matrix(out, prop, name)
            elif name == "linecode":
                value = _scalar(prop["value"]).casefold()
            else:
                value = self.number(out, prop, name, positive=name in {"basekv", "kv", "kva", "mva"},
                                    nonnegative=name in {"normamps", "emergamps", "pmpp"})
            fields[name] = _declared(value, prop)
            positions[name] = index
        phase_position = positions.get("nphases" if obj["class"].casefold() == "linecode" else "phases", -1)
        for name in _MATRICES:
            if name in fields and phase_position > positions[name]:
                fields[name]["phase_assignment_after_matrix"] = True
        return fields

    def matrix(self, out, prop, field):
        raw = prop["value"].strip()
        try:
            if len(raw) < 2 or raw[0] not in "[(" or raw[-1] not in ")]":
                raise ValueError
            rows = [[float(x) for x in re.split(r"[\s,]+", row.strip()) if x]
                    for row in raw[1:-1].split("|")]
            if not rows or sum(map(len, rows)) > 20000 or any(not row for row in rows):
                raise ValueError
            if not all(math.isfinite(x) for row in rows for x in row):
                raise ValueError
        except (ValueError, OverflowError):
            self.issue(out, "invalid_native_matrix", field,
                       "Native matrix must contain bounded explicit finite numeric entries.", [_ref(prop)], invalid=True)
            return None
        return {"raw": raw, "rows": rows, "entry_count": sum(map(len, rows))}

    def check_matrices(self, out, fields, phases):
        for name in _MATRICES:
            item = fields.get(name)
            if item is None or item["value"] is None:
                continue
            if item.get("phase_assignment_after_matrix"):
                self.issue(out, "matrix_phase_assignment_interaction", name,
                           "A later phase assignment may resize/reset this earlier matrix; final engine storage is not reconstructed.", item["source_refs"])
                continue
            if phases is None:
                self.issue(out, "matrix_phase_basis_not_declared", name,
                           "Matrix dimensions have no explicit phase-count basis.", item["source_refs"])
                continue
            count = item["value"]["entry_count"]
            if count not in {phases * phases, phases * (phases + 1) // 2}:
                self.issue(out, "matrix_dimension_mismatch", name,
                           "Explicit matrix entry count contradicts the explicit phase count.", item["source_refs"], invalid=True)
            rows = item["value"]["rows"]
            if len(rows) > 1 and [len(row) for row in rows] not in ([phases] * phases, list(range(1, phases + 1))):
                self.issue(out, "matrix_row_layout_unresolved", name,
                           "Native row delimiters do not describe a full or lower triangular matrix; parser truncation is not reproduced.", item["source_refs"])

    def line(self, obj, out):
        fields = self.basic(obj, out)
        phase_item = fields.get("phases", _missing())
        phases = phase_item["value"]
        code_item = fields.get("linecode")
        if code_item is not None:
            self.issue(out, "linecode_copy_effects_not_fully_replayed", "linecode",
                       "Referenced native LineCode copy/default/resize effects are not fully replayed by this bounded inspector.", code_item["source_refs"])
            code_name = "linecode." + code_item["value"]
            code = self.by_id.get(code_name)
            if code is None:
                self.issue(out, "linecode_missing", "linecode",
                           "Referenced LineCode is absent from this inspected package.", code_item["source_refs"], invalid=True)
            else:
                ref_prop = next(p for p in reversed(obj["properties"]) if p["name"].casefold() == "linecode")
                ref_order = self.command_order.get((ref_prop["source_file"], ref_prop["line"]))
                history = self.code_history[code_name]
                ordered = ref_order is not None and history["ordered"]
                latest_phase = bisect_right(history["phase_positions"], ref_order) - 1 if ordered else -1
                code_phases = history["phase_properties"][latest_phase] if latest_phase >= 0 else None
                later_edits = ref_order is not None and history["last"] > ref_order
                if not ordered or code_phases is None:
                    self.issue(out, "linecode_phase_at_reference_unresolved", "linecode",
                               "Explicit LineCode phase count at the reference event cannot be established.", code_item["source_refs"])
                else:
                    code_phase = self.number(out, code_phases, "linecode.nphases", integer=True, positive=True)
                    out["linecode_at_reference"] = {"object_id": code_name,
                                                    "nphases": _declared(code_phase, code_phases)}
                    if phases is None:
                        phases = code_phase
                        fields["phases"] = _declared(code_phase, code_phases, origin="explicit_linecode_at_reference")
                    elif phases != code_phase:
                        self.issue(out, "linecode_phase_assignment_interaction", "phases",
                                   "Line phases and referenced LineCode phases differ; copy/resize effects are not reproduced.",
                                   phase_item["source_refs"] + [_ref(code_phases)])
                        phases = None
                        fields["phases"]["status"] = "uncertain"
                if later_edits:
                    self.issue(out, "linecode_later_edit_not_propagated", "linecode",
                               "LineCode was edited after the reference; final library values are not assumed to be copied into this line.", code_item["source_refs"])
        if code_item is not None and phases == 4 and obj["name"].casefold() not in self.four_conductor_objects:
            self.issue(out, "four_conductor_line_basis_incomplete", "phases",
                       "A copied four-conductor Line requires complete explicit matrices and four nodes at both terminals.",
                       fields["phases"]["source_refs"], invalid=True)
        self.need(out, fields, "phases")
        for name in ("bus1", "bus2"):
            item = self.need(out, fields, name)
            # A LineCode reference can resize the native conductor storage.
            # The ordered replay reports the effective phase count separately;
            # this legacy view must not reject using a stale/default dimension.
            self.check_bus(out, item, name, None if code_item is not None else phases)
        self.check_matrices(out, fields, None if code_item is not None else phases)

    def power_conversion(self, obj, out):
        fields = self.basic(obj, out)
        fields.setdefault("enabled", _missing())
        for name in ("phases", "bus1", "conn", "kv"):
            self.need(out, fields, name)
        phases, conn = fields["phases"]["value"], fields["conn"]["value"]
        conductors = None
        if phases in {1, 2, 3} and conn in {"wye", "ln", "delta", "ll"}:
            conductors = phases + 1 if conn in {"wye", "ln"} else 2 if phases == 1 else 3
        self.check_bus(out, fields["bus1"], "bus1", conductors)
        out["voltage_basis"] = ("line_to_line" if phases in {2, 3} or conn in {"delta", "ll"}
                                else "across_single_winding_line_to_neutral" if phases == 1 and conn in {"wye", "ln"}
                                else "unresolved")
        out["native_conductor_count"] = conductors

    def transformer(self, obj, out):
        fields, windings, selector = out["fields"], {}, None
        count, phases = None, None
        any_winding_assignment = False
        def winding(index):
            return windings.setdefault(index, {name: _missing() for name in _WINDING_FIELDS})
        def uncertain(prop, affected):
            for record in windings.values():
                for name in affected:
                    if record[name]["status"] != "not_declared":
                        record[name] = {**record[name], "status": "uncertain",
                                        "source_refs": record[name]["source_refs"] + [_ref(prop)]}
        def assign(index, name, value_prop):
            nonlocal any_winding_assignment
            any_winding_assignment = True
            if name == "bus":
                value = self.bus(out, value_prop, f"windings.{index}.bus")
            elif name == "conn":
                value = self.conn(out, value_prop, f"windings.{index}.conn")
            else:
                value = self.number(out, value_prop, f"windings.{index}.{name}",
                                    positive=name in {"kv", "kva", "tap"}, nonnegative=name == "%r")
            if name == "kva" and index == 1:
                for other_index, record in windings.items():
                    if other_index != 1 and record["kva"]["status"] != "not_declared":
                        record["kva"] = {**record["kva"], "status": "uncertain",
                                         "source_refs": record["kva"]["source_refs"] + [_ref(value_prop)]}
                        self.issue(out, "first_winding_kva_side_effect", f"windings.{other_index}.kva",
                                   "A later first-winding kVA assignment can affect other winding ratings; no calculated rating is substituted.", [_ref(value_prop)])
            winding(index)[name] = _declared(value, value_prop)
        for prop in obj["properties"]:
            name = prop["name"].casefold()
            if name in {"phases", "windings"}:
                value = self.number(out, prop, name, integer=True, positive=True)
                allowed = {1, 2, 3} if name == "phases" else {2, 3}
                if value is not None and value not in allowed:
                    self.issue(out, "transformer_dimension_outside_scope", name,
                               "Transformer dimension is outside the supported inspection subset.", [_ref(prop)])
                    value = None
                old = phases if name == "phases" else count
                if any_winding_assignment and (old is None or old != value):
                    uncertain(prop, _WINDING_FIELDS)
                    self.issue(out, "transformer_dimension_reset_unresolved", name,
                               "A dimension assignment follows winding data; native reset/resize effects are not guessed.", [_ref(prop)])
                    selector = None
                if name == "phases":
                    phases = value
                else:
                    count = value
                fields[name] = _declared(value, prop)
            elif name == "wdg":
                selector = self.number(out, prop, name, integer=True, positive=True)
                if selector is not None and count is not None and selector > count:
                    self.issue(out, "winding_selector_out_of_range", name,
                               "Winding selector exceeds the explicitly active winding count.", [_ref(prop)], invalid=True)
                    selector = None
                elif count is None:
                    self.issue(out, "winding_count_at_selector_not_declared", name,
                               "Selector is explicit but its active winding-count basis is not declared.", [_ref(prop)])
            elif name in _WINDING_FIELDS:
                if selector is None or selector not in {1, 2, 3}:
                    self.issue(out, "winding_selector_not_declared", name,
                               "Single-winding assignment has no explicit, supported active selector.", [_ref(prop)])
                else:
                    assign(selector, name, prop)
            elif name in _ARRAY_FIELDS:
                try:
                    values = _parts(prop["value"])
                except ValueError:
                    self.issue(out, "invalid_winding_array", name,
                               "Explicit winding array is malformed or exceeds its inspection budget.", [_ref(prop)], invalid=True)
                    continue
                if len(values) > 3:
                    self.issue(out, "winding_array_outside_scope", name,
                               "Only arrays of at most three windings are inspected.", [_ref(prop)])
                    continue
                if count is not None and len(values) != count:
                    self.issue(out, "winding_array_dimension_mismatch", name,
                               "Array width contradicts the explicitly active winding count.", [_ref(prop)], invalid=True)
                elif count is None:
                    self.issue(out, "winding_count_at_array_not_declared", name,
                               "Array entries are preserved but no default winding count is inserted.", [_ref(prop)])
                for index, value in enumerate(values, 1):
                    assign(index, _ARRAY_FIELDS[name], {**prop, "value": value})
                # The documented array interface ignores wdg; no undocumented
                # post-array selector state is assumed for later scalar fields.
                selector = None
            elif name == "%loadloss":
                uncertain(prop, ("%r",))
                self.issue(out, "loadloss_resistance_side_effect", "%r",
                           "Percent load loss can overwrite first/second winding resistances; no inferred resistance is substituted.", [_ref(prop)])
        self.need(out, fields, "phases")
        self.need(out, fields, "windings")
        if count is not None:
            for index in range(1, count + 1):
                winding(index)
        for index in sorted(windings):
            record = windings[index]
            for name in _WINDING_FIELDS:
                self.need(out, record, name, f"windings.{index}.")
            self.check_bus(out, record["bus"], f"windings.{index}.bus", phases + 1 if phases in {1, 2, 3} else None)
            out["windings"].append({"winding": index, "fields": record})
        out["native_conductor_count"] = phases + 1 if phases in {1, 2, 3} else None
        out["voltage_basis"] = "line_to_line" if phases in {2, 3} else "actual_single_winding" if phases == 1 else "unresolved"

    def inspect(self):
        for obj in sorted(self.inputs, key=lambda item: item["name"].casefold()):
            kind = obj["class"].casefold()
            out = {"object_id": obj["name"], "class": kind, "status": "complete",
                   "fields": {}, "windings": [], "issues": []}
            if kind == "transformer":
                self.transformer(obj, out)
            elif kind == "line":
                self.line(obj, out)
            elif kind in {"load", "generator", "pvsystem"}:
                self.power_conversion(obj, out)
            elif kind == "linecode":
                fields = self.basic(obj, out)
                self.need(out, fields, "nphases")
                self.check_matrices(out, fields, fields["nphases"]["value"])
            elif kind in {"circuit", "vsource"}:
                fields = self.basic(obj, out)
                if kind == "circuit":
                    out["implicit_source_object_id"] = "vsource.source"
                    self.issue(out, "circuit_source_mapping_not_replayed", "implicit_source",
                               "Circuit declarations configure the implicit source; cross-object source edit precedence is not replayed.",
                               [ref for item in fields.values() for ref in item["source_refs"]])
                elif obj.get("implicit"):
                    out["created_by_circuit"] = True
                    self.issue(out, "implicit_source_values_not_synthesized", "implicit_source",
                               "Implicit source defaults/circuit-derived settings are not synthesized as explicit Vsource declarations.")
                for name in ("phases", "bus1", "basekv"):
                    self.need(out, fields, name)
                for name in ("bus1", "bus2"):
                    if name in fields:
                        # Source neutral-terminal construction is left to native compilation.
                        self.check_bus(out, fields[name], name, None)
            elif kind == "xycurve":
                out["scope"] = "not_an_electrical_connection_object"
            else:
                self.issue(out, "object_class_not_inspected", "class",
                           "Native device class is outside this semantic inspection subset.")
            if any(issue["severity"] == "error" for issue in out["issues"]):
                out["status"] = "invalid"
            elif out["issues"]:
                out["status"] = "partial"
            self.outputs.append(out)
        return self.outputs, self.issues


def inspect_device_semantics(inspection: Mapping) -> dict:
    """Return deterministic JSON facts/issues without mutating a native inspection.

    ``invalid`` means an explicit contradiction in this supported subset.
    ``partial`` includes absent defaults, ambiguous side effects and scope limits.
    Both statuses are distinct from native numerical or engineering validity.
    """
    result = {"version": _VERSION, "status": "complete",
              "scope": "explicit_native_device_semantics", "numerical_validity": "not_run",
              "engineering_accepted": False, "objects": [], "issues": [],
              "limitations": ["Native node numbers are preserved; no physical A/B/C phase mapping is inferred.",
                              "No OpenDSS default, missing engineering rating or project limit is inserted.",
                              "This is static inspection; no actual solver property readback is performed."]}
    try:
        inspector = _Inspector(inspection)
        result["objects"], result["issues"] = inspector.inspect()
        four_lines = bool(inspector.four_conductor_objects)
        if four_lines:
            result["version"] = "1.4"
        from .device_replay import replay_device_properties
        replay = replay_device_properties(inspection, include_device_states=True,
                                         include_four_conductor_lines=four_lines)
        result["ordered_replay"] = replay
        result["issues"].extend(replay["issues"])
        from .power_point_semantics import replay_power_operating_points
        power = replay_power_operating_points(inspection)
        result["power_operating_points"] = power
        result["issues"].extend(power["issues"])
        for obj in result["objects"]:
            added = [issue for issue in replay["issues"] + power["issues"] if issue.get("object_id") == obj["object_id"]]
            obj["issues"].extend(added)
            if any(issue["severity"] == "error" for issue in added):
                obj["status"] = "invalid"
            elif added and obj["status"] != "invalid":
                obj["status"] = "partial"
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        result["objects"] = []
        result["issues"] = [{"code": "inspection_shape_invalid", "severity": "error",
                             "object_id": None, "field": "inspection",
                             "message": "Input is not a bounded native inspection with ordered source properties.",
                             "source_refs": []}]
    if any(item["severity"] == "error" for item in result["issues"]):
        result["status"] = "invalid"
    elif (result["issues"] or result.get("ordered_replay", {}).get("status") == "partial"
          or result.get("power_operating_points", {}).get("status") == "partial"):
        result["status"] = "partial"
    return result
