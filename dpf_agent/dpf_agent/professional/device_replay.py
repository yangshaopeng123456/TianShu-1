"""Bounded replay of explicit native DSS property order; no engine or I/O.

Copyright (c) yangshaopeng. Only the listed property effects are reproduced.
Unknown defaults and electrical transformations are never manufactured. This
report supplements static inspection and is not numerical/engineering approval.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy


_VERSION = "1.0"
_MAX_ITEMS = 100000
_MAX_CHARS = 20 * 1024 * 1024
_WINDING = ("bus", "conn", "kv", "kva", "tap", "%r")
_ARRAYS = {"buses": "bus", "conns": "conn", "kvs": "kv", "kvas": "kva",
           "taps": "tap", "%rs": "%r"}
_SOURCE = {"phases", "bus1", "bus2", "basekv"}


def _scalar(value):
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0] else value


def _parts(value):
    value = value.strip()
    if len(value) < 2 or value[0] not in "[(" or value[-1] not in ")]":
        raise ValueError("explicit array delimiters required")
    result, token, quote = [], [], None
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
                result.append("".join(token))
                token = []
        else:
            token.append(char)
    if quote:
        raise ValueError("unclosed array quote")
    if token:
        result.append("".join(token))
    if not 1 <= len(result) <= 3:
        raise ValueError("winding array outside bounded subset")
    return result


def _missing():
    return {"status": "not_declared", "value": None, "source_refs": [],
            "replay_step": None, "reason": "No explicit supported assignment; native defaults are not inserted."}


def _validate(inspection):
    if not isinstance(inspection, Mapping):
        raise ValueError("inspection must be a JSON object")
    objects, commands = inspection.get("objects"), inspection.get("commands", [])
    if (not isinstance(objects, list) or not isinstance(commands, list)
            or len(objects) > _MAX_ITEMS or len(commands) > _MAX_ITEMS):
        raise ValueError("objects/commands must be bounded ordered lists")
    order, chars, identifiers, ordered, heads = {}, 0, set(), True, {}
    for index, command in enumerate(commands):
        if (not isinstance(command, Mapping) or not isinstance(command.get("file"), str)
                or not isinstance(command.get("command"), str)
                or isinstance(command.get("line"), bool) or not isinstance(command.get("line"), int)
                or not 1 <= command["line"] <= _MAX_ITEMS):
            raise ValueError("command/source metadata is malformed")
        key = command["file"], command["line"]
        if key in order:
            ordered = False
        kind = command["command"].casefold()
        if kind in {"new", "edit"}:
            heads[command["file"]] = kind
        elif kind != "~":
            heads[command["file"]] = None
        order[key] = index, kind, heads.get(command["file"])
        chars += len(command["file"]) + len(command["command"])
        if chars > _MAX_CHARS:
            raise ValueError("command text budget exceeded")
    events, owners, count = [], {}, 0
    for obj in objects:
        if (not isinstance(obj, Mapping) or not isinstance(obj.get("name"), str)
                or not isinstance(obj.get("class"), str) or not obj["name"]
                or len(obj["name"]) > 256 or not isinstance(obj.get("properties"), list)):
            raise ValueError("object/property metadata is malformed")
        identifier = obj["name"].casefold()
        if identifier in identifiers or "." not in identifier or identifier.split(".", 1)[0] != obj["class"].casefold():
            raise ValueError("object identifier/class is duplicate or inconsistent")
        identifiers.add(identifier)
        chars += len(obj["name"]) + len(obj["class"])
        if chars > _MAX_CHARS:
            raise ValueError("object text budget exceeded")
        prior = -1
        for position, prop in enumerate(obj["properties"]):
            count += 1
            if count > _MAX_ITEMS or not isinstance(prop, Mapping):
                raise ValueError("property event budget exceeded")
            if not all(isinstance(prop.get(key), str) for key in ("name", "value", "source_file")):
                raise ValueError("property/source metadata must be text")
            line = prop.get("line")
            if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= _MAX_ITEMS:
                raise ValueError("property source line is malformed")
            key = prop["source_file"], line
            command = order.get(key)
            if command is None or command[1] not in {"new", "edit", "~"} or command[0] < prior:
                ordered = False
                command = command or (len(commands), "unresolved", None)
            if key in owners and owners[key] != identifier:
                ordered = False
            owners[key], prior = identifier, command[0]
            events.append((command[0], position, identifier, command[1], prop, command[2]))
            chars += len(prop["name"]) + len(prop["value"]) + len(prop["source_file"])
            if chars > _MAX_CHARS:
                raise ValueError("property text budget exceeded")
    return objects, sorted(events, key=lambda item: (item[0], item[1])), ordered


class _Replay:
    def __init__(self, inspection, *, include_device_states=False, include_four_conductor_lines=False):
        objects, self.events, self.ordered = _validate(inspection)
        self.include_device_states = include_device_states
        self.include_four_conductor_lines = include_four_conductor_lines
        self.outputs, self.issues, self.steps, self.states = {}, [], [], {}
        for obj in objects:
            identifier, kind = obj["name"].casefold(), obj["class"].casefold()
            names = ({"phases", "linecode", "bus1", "bus2"} if kind == "line" else
                     {"nphases"} if kind == "linecode" else
                     _SOURCE if kind in {"circuit", "vsource"} else
                     {"phases", "windings"} if kind == "transformer" else
                     {"enabled"} if include_device_states and kind in {"load", "generator", "pvsystem"} else set())
            self.outputs[identifier] = {"object_id": obj["name"], "class": kind,
                                        "status": "complete", "fields": {key: _missing() for key in sorted(names)},
                                        "windings": [], "replayed_fields": [], "unhandled_properties": [], "issues": []}
            self.states[identifier] = {"selector": None, "selector_candidate": None,
                                       "count": None, "windings": {}, "paths": set()}
            if kind == "circuit":
                self.outputs[identifier]["implicit_source_object_id"] = "vsource.source"
            elif kind == "vsource" and obj.get("implicit"):
                self.outputs[identifier]["origin"] = "implicit_source_ordered_replay"

    def issue(self, out, code, field, message, refs=(), *, invalid=False):
        issue = {"code": code, "severity": "error" if invalid else "unresolved",
                 "object_id": out["object_id"], "field": field, "message": message,
                 "source_refs": deepcopy(list(refs))}
        out["issues"].append(issue)
        self.issues.append(issue)

    def mark(self, identifier, field, item):
        self.states[identifier]["paths"].add(field)
        self.steps[-1]["effects"].append({"object_id": identifier, "field": field, "status": item["status"]})

    def put(self, identifier, field, value, ref, *, refs=(), origin="explicit_property"):
        item = {"status": "declared", "value": value, "source_refs": deepcopy([*refs, ref]),
                "replay_step": ref["replay_step"], "origin": origin}
        self.outputs[identifier]["fields"][field] = item
        self.mark(identifier, "fields." + field, item)
        return item

    def uncertain(self, identifier, field, ref, reason, *, winding=None):
        if winding is None:
            fields, path = self.outputs[identifier]["fields"], "fields." + field
        else:
            fields, path = self.winding(identifier, winding), f"windings.{winding}.{field}"
        old = fields.get(field, _missing())
        # Only the prior determining event and the new invalidator are needed;
        # the ordered trace retains earlier events without quadratic histories.
        refs = old.get("source_refs", [])[-2:] + [ref]
        item = {"status": "uncertain", "value": deepcopy(old.get("value")),
                "source_refs": deepcopy(refs), "replay_step": ref["replay_step"], "reason": reason}
        fields[field] = item
        self.mark(identifier, path, item)
        return item

    def number(self, out, field, prop, ref, *, integer=False, nonnegative=False):
        try:
            text = _scalar(prop["value"])
            if integer and not re.fullmatch(r"[0-9]{1,9}", text):
                raise ValueError
            value = int(text) if integer else float(text)
            if not math.isfinite(value) or (value < 0 if nonnegative else value <= 0):
                raise ValueError
            allowed = ({1, 2, 3, 4} if self.include_four_conductor_lines
                       and out["class"] in {"line", "linecode"}
                       and field in {"phases", "nphases"} else {1, 2, 3})
            if integer and value not in allowed:
                raise ValueError
        except (ValueError, OverflowError):
            self.issue(out, "unsupported_explicit_value", field,
                       "Explicit value is outside the finite supported property subset.", [ref])
            return None
        return value

    def bus(self, out, field, prop, ref):
        raw = _scalar(prop["value"])
        parts = raw.split(".")
        if (not parts[0] or len(raw) > 2048 or any(ord(char) < 32 for char in raw)
                or any(not re.fullmatch(r"[0-9]{1,10}", token) or int(token) > 2147483647 for token in parts[1:])):
            self.issue(out, "unsupported_explicit_bus", field,
                       "Explicit bus metadata is outside the bounded native node subset.", [ref])
            return None
        return {"raw": raw, "bus_name": parts[0], "nodes": [int(token) for token in parts[1:]],
                "node_tokens": parts[1:], "nodes_explicit": bool(parts[1:])}

    def value(self, out, field, prop, ref):
        if field == "enabled":
            value = _scalar(prop["value"]).casefold()
            if value in {"yes", "true", "1"}:
                return True
            if value in {"no", "false", "0"}:
                return False
            self.issue(out, "invalid_explicit_boolean", field,
                       "Explicit state is outside the supported native boolean spellings.", [ref], invalid=True)
            return None
        if field in {"bus", "bus1", "bus2"}:
            return self.bus(out, field, prop, ref)
        if field == "conn":
            value = _scalar(prop["value"]).casefold()
            if value in {"wye", "ln", "delta", "ll"}:
                return value
            self.issue(out, "unsupported_explicit_connection", field,
                       "Connection spelling is outside the replay subset.", [ref])
            return None
        return self.number(out, field, prop, ref, integer=field in {"phases", "nphases", "windings", "wdg"},
                           nonnegative=field == "%r")

    def unsupported(self, identifier, prop, ref, reason="Property is outside the bounded ordered replay subset."):
        out = self.outputs[identifier]
        out["unhandled_properties"].append({"property": prop["name"], "raw_value": prop["value"],
                                             "status": "not_checked", "source_refs": [deepcopy(ref)], "reason": reason})
        self.issue(out, "property_not_replayed", prop["name"], reason, [ref])
        self.steps[-1]["status"] = "not_checked"

    def phase_change(self, identifier, value, ref):
        fields = self.outputs[identifier]["fields"]
        old = fields.get("phases", _missing())
        if old.get("status") != "declared" or old.get("value") != value:
            for bus in ("bus1", "bus2"):
                if fields.get(bus, _missing())["status"] != "not_declared":
                    self.uncertain(identifier, bus, ref, "A later phase-count resize may alter native conductor-node binding.")

    def line(self, identifier, prop, ref):
        name, out = prop["name"].casefold(), self.outputs[identifier]
        if name == "linecode":
            target = "linecode." + _scalar(prop["value"]).casefold()
            self.put(identifier, name, target.split(".", 1)[1], ref)
            code = self.outputs.get(target)
            phase = code["fields"].get("nphases", _missing()) if code else _missing()
            if phase["status"] != "declared":
                self.uncertain(identifier, "phases", ref, "Referenced LineCode has no reliable explicit phase count at this event.")
                self.issue(out, "linecode_phase_at_reference_unresolved", "phases",
                           "No later/final library value is substituted at this earlier reference.", [ref])
            else:
                self.phase_change(identifier, phase["value"], ref)
                self.put(identifier, "phases", phase["value"], ref, refs=phase["source_refs"], origin="linecode_snapshot_copy")
            # The remaining copied electrical data are deliberately not replayed.
            self.issue(out, "linecode_electrical_copy_outside_scope", "linecode",
                       "Only explicit phase-count copy is replayed; impedances, ampacities, matrices and units remain outside this subset.", [ref])
        elif name in {"phases", "bus1", "bus2"}:
            value = self.value(out, name, prop, ref)
            if value is None:
                self.uncertain(identifier, name, ref, "Explicit assignment is outside the replay subset.")
            else:
                if name == "phases":
                    self.phase_change(identifier, value, ref)
                self.put(identifier, name, value, ref)
        else:
            self.unsupported(identifier, prop, ref)

    def source(self, identifier, prop, ref, command):
        out, name = self.outputs[identifier], prop["name"].casefold()
        if out["class"] == "circuit" and self.states[identifier].get("circuit_edit_active"):
            self.unsupported(identifier, prop, ref,
                             "Native Edit Circuit is a no-op; this event does not modify the implicit Vsource.")
            self.steps[-1]["status"] = "ignored"
            out["unhandled_properties"][-1]["status"] = "ignored"
            return
        if name not in _SOURCE:
            self.unsupported(identifier, prop, ref)
            return
        value = self.value(out, name, prop, ref)
        if value is None:
            self.uncertain(identifier, name, ref, "Explicit source assignment is outside the replay subset.")
        else:
            if name == "phases":
                self.phase_change(identifier, value, ref)
            self.put(identifier, name, value, ref)
        if out["class"] != "circuit":
            return
        source = self.outputs.get("vsource.source")
        if source is None:
            self.issue(out, "implicit_source_missing", name,
                       "Circuit has no inspected implicit Vsource object.", [ref], invalid=True)
            return
        if value is None:
            self.uncertain("vsource.source", name, ref, "Circuit creation parameter is outside the replay subset.")
        else:
            if name == "phases":
                self.phase_change("vsource.source", value, ref)
            self.put("vsource.source", name, value, ref, origin="circuit_creation_source_mapping")

    def winding(self, identifier, index):
        return self.states[identifier]["windings"].setdefault(index, {key: _missing() for key in _WINDING})

    def winding_put(self, identifier, index, name, value, ref, *, origin="explicit_winding_property", refs=()):
        item = {"status": "declared", "value": value, "source_refs": deepcopy([*refs, ref]),
                "replay_step": ref["replay_step"], "origin": origin}
        self.winding(identifier, index)[name] = item
        self.mark(identifier, f"windings.{index}.{name}", item)

    def transformer(self, identifier, prop, ref):
        out, state, name = self.outputs[identifier], self.states[identifier], prop["name"].casefold()
        if name in {"phases", "windings"}:
            value = self.value(out, name, prop, ref)
            if name == "windings" and value not in {2, 3}:
                value = None
            # Dimension setters can reallocate native storage. Do not reconstruct
            # resets or defaults, including a same-value late dimension setter.
            if state["windings"]:
                for index, record in list(state["windings"].items()):
                    for field in _WINDING:
                        if record[field]["status"] != "not_declared":
                            self.uncertain(identifier, field, ref, "Later transformer dimension assignment has un-replayed reset effects.", winding=index)
                state["selector"] = None
                state["selector_candidate"] = None
                self.issue(out, "transformer_dimension_reset_not_replayed", name,
                           "Prior winding values are retained as uncertain; no native reset value is inserted.", [ref])
            if value is None:
                self.uncertain(identifier, name, ref, "Transformer dimension is outside the supported subset.")
            else:
                self.put(identifier, name, value, ref)
            if name == "windings":
                state["count"] = value
            return
        if name == "wdg":
            selector = self.value(out, name, prop, ref)
            state["selector_candidate"] = selector
            state["selector"] = selector if selector and state["count"] and selector <= state["count"] else None
            if state["selector"] is None:
                self.issue(out, "winding_selector_basis_unresolved", name,
                           "An explicit active selector and explicit supported winding count are required.", [ref])
            return
        if name in _ARRAYS:
            try:
                values = _parts(prop["value"])
            except ValueError:
                values = None
            field = _ARRAYS[name]
            if values is None or state["count"] not in {2, 3} or len(values) != state["count"]:
                candidates = set(state["windings"])
                if state["count"]:
                    candidates.update(range(1, state["count"] + 1))
                if values:
                    candidates.update(range(1, len(values) + 1))
                for index in sorted(candidates):
                    self.uncertain(identifier, field, ref, "Array has no exact explicit supported winding-count basis.", winding=index)
                self.issue(out, "winding_array_basis_unresolved", name,
                           "No default count, partial-array fill or excess-array truncation is reproduced.", [ref])
            else:
                for index, text in enumerate(values, 1):
                    value = self.value(out, field, {**prop, "value": text}, ref)
                    if value is None:
                        self.uncertain(identifier, field, ref, "Array entry is outside the supported subset.", winding=index)
                    else:
                        self.winding_put(identifier, index, field, value, ref, origin="explicit_winding_array")
            # No undocumented post-array selector is guessed.
            state["selector"] = None
            state["selector_candidate"] = None
            return
        if name in _WINDING:
            index = state["selector"]
            if name == "kva" and state["count"] == 2 and index is None:
                # Either active winding has the same two-winding setter effect.
                # This does not infer which selector the native array left active.
                value = self.value(out, name, prop, ref)
                for number in (1, 2):
                    if value is None:
                        self.uncertain(identifier, name, ref, "Explicit kVA is outside the supported subset.", winding=number)
                    else:
                        self.winding_put(identifier, number, name, value, ref,
                                         origin="two_winding_selector_independent_scalar_kva_linkage")
                return
            if index is None:
                self.issue(out, "winding_selector_unresolved", name,
                           "Scalar winding assignment requires an explicitly re-established selector.", [ref])
                candidates = set(state["windings"])
                if state["count"]:
                    candidates.update(range(1, state["count"] + 1))
                if state["selector_candidate"]:
                    candidates.add(state["selector_candidate"])
                for number in sorted(candidates):
                    self.uncertain(identifier, name, ref, "Scalar winding target is not reliably established.", winding=number)
                return
            value = self.value(out, name, prop, ref)
            if value is None:
                self.uncertain(identifier, name, ref, "Explicit winding assignment is outside the supported subset.", winding=index)
                return
            self.winding_put(identifier, index, name, value, ref)
            if name == "kva" and state["count"] == 2:
                other = 3 - index
                self.winding_put(identifier, other, name, value, ref, origin="two_winding_scalar_kva_linkage")
            elif name == "kva" and state["count"] == 3 and index == 1:
                for number in (2, 3):
                    self.uncertain(identifier, name, ref, "Three-winding first-winding kVA propagation is outside this bounded replay.", winding=number)
                self.issue(out, "three_winding_kva_side_effect_not_replayed", name,
                           "Two-winding same-rating linkage is not applied to three-winding ratios.", [ref])
            return
        if name == "%loadloss":
            for index in (1, 2):
                self.uncertain(identifier, "%r", ref, "Load-loss assignment can overwrite winding resistance; no inferred resistance is inserted.", winding=index)
        self.unsupported(identifier, prop, ref)

    def unresolved_order(self):
        """Keep source observations, but never infer chronology from object order."""
        refs = {}
        for command_index, position, identifier, command, prop, head in self.events:
            ref = {"source_file": prop["source_file"], "line": prop["line"], "property": prop["name"],
                   "command_index": None, "property_index": position, "replay_step": None}
            refs.setdefault(identifier, []).append(ref)
            self.outputs[identifier]["unhandled_properties"].append({
                "property": prop["name"], "raw_value": prop["value"], "status": "not_checked",
                "source_refs": [deepcopy(ref)], "reason": "Global command order is unavailable or ambiguous."})
            if self.outputs[identifier]["class"] == "transformer":
                name = prop["name"].casefold()
                if name in {"wdg", "windings"} and re.fullmatch(r"[123]", _scalar(prop["value"])):
                    for index in range(1, int(_scalar(prop["value"])) + 1):
                        self.winding(identifier, index)
                elif name in _ARRAYS:
                    try:
                        for index in range(1, len(_parts(prop["value"])) + 1):
                            self.winding(identifier, index)
                    except ValueError:
                        pass
        for identifier, out in self.outputs.items():
            source_refs = refs.get(identifier, [])
            if out.get("origin") == "implicit_source_ordered_replay":
                source_refs = source_refs + [ref for key, values in refs.items()
                                             if self.outputs[key]["class"] == "circuit" for ref in values]
            limited_refs = source_refs[:1] + source_refs[-1:] if len(source_refs) > 1 else source_refs
            for field in out["fields"]:
                out["fields"][field] = {"status": "uncertain", "value": None, "source_refs": deepcopy(limited_refs),
                                        "replay_step": None, "reason": "Global assignment order cannot be established."}
                self.states[identifier]["paths"].add("fields." + field)
            for index, record in self.states[identifier]["windings"].items():
                for field in record:
                    record[field] = {"status": "uncertain", "value": None, "source_refs": deepcopy(limited_refs),
                                     "replay_step": None, "reason": "Global assignment order cannot be established."}
                    self.states[identifier]["paths"].add(f"windings.{index}.{field}")
            self.issue(out, "global_property_order_unresolved", "order",
                       "No final value is guessed from the final object/library table or property-list grouping.", limited_refs)

    def run(self):
        if not self.ordered:
            self.unresolved_order()
        else:
            self.replay_events()
        outputs = []
        for identifier in sorted(self.outputs):
            out, state = self.outputs[identifier], self.states[identifier]
            if state["count"]:
                for index in range(1, state["count"] + 1):
                    self.winding(identifier, index)
            out["windings"] = [{"winding": index, "fields": record}
                               for index, record in sorted(state["windings"].items())]
            out["replayed_fields"] = sorted(state["paths"])
            all_items = list(out["fields"].values()) + [item for row in out["windings"] for item in row["fields"].values()]
            if any(issue["severity"] == "error" for issue in out["issues"]):
                out["status"] = "invalid"
            elif out["issues"] or any(item["status"] != "declared" for item in all_items):
                out["status"] = "partial"
            outputs.append(out)
        return outputs

    def replay_events(self):
        for command_index, position, identifier, command, prop, head in self.events:
            ref = {"source_file": prop["source_file"], "line": prop["line"], "property": prop["name"],
                   "command_index": command_index, "property_index": position, "replay_step": len(self.steps) + 1}
            self.steps.append({"step": ref["replay_step"], "object_id": identifier, "command": command,
                               "property": prop["name"], "source_ref": deepcopy(ref), "status": "replayed", "effects": []})
            out, name = self.outputs[identifier], prop["name"].casefold()
            if out["class"] == "circuit" and command in {"new", "edit"}:
                self.states[identifier]["circuit_edit_active"] = command == "edit"
            if out["class"] == "circuit" and command == "~" and head == "edit":
                self.issue(out, "circuit_edit_continuation_target_unsupported", prop["name"],
                           "Continuation after no-op Edit Circuit may target a different native active class; the input is outside this replay contract.", [ref], invalid=True)
                self.unsupported(identifier, prop, ref,
                                 "Native continuation target after Edit Circuit is not established by static object grouping.")
                self.steps[-1]["status"] = "unsupported"
                continue
            if out["class"] == "line":
                self.line(identifier, prop, ref)
            elif out["class"] == "linecode" and name == "nphases":
                value = self.value(out, name, prop, ref)
                if value is None:
                    self.uncertain(identifier, name, ref, "Explicit phase count is outside the supported subset.")
                else:
                    self.put(identifier, name, value, ref)
            elif out["class"] in {"circuit", "vsource"}:
                self.source(identifier, prop, ref, command)
            elif out["class"] == "transformer":
                self.transformer(identifier, prop, ref)
            elif (self.include_device_states and out["class"] in {"load", "generator", "pvsystem"}
                  and name == "enabled"):
                value = self.value(out, name, prop, ref)
                if value is None:
                    self.uncertain(identifier, name, ref, "Explicit enabled state is invalid or unsupported.")
                else:
                    self.put(identifier, name, value, ref)
            else:
                self.unsupported(identifier, prop, ref)


def replay_device_properties(inspection: Mapping, *, include_device_states=False,
                             include_four_conductor_lines=False) -> dict:
    """Replay a source-ordered inspection without modifying it or native state.

    ``replayed_fields`` is the exhaustive overlay list. Consumers must preserve
    uncertain entries from that list rather than restoring old static values.
    Other properties are retained as ``unhandled_properties`` with their source.
    """
    result = {"version": "1.2" if include_four_conductor_lines else "1.1" if include_device_states else _VERSION, "scope": "bounded_ordered_native_properties", "status": "complete",
              "engineering_accepted": False, "objects": [], "steps": [], "issues": [],
              "limitations": ["Only the explicitly listed property effects are replayed; this is not a native compiler.",
                              "No native defaults, electrical matrices, units conversion, source impedance or engineering limits are reconstructed.",
                              "Load/Generator/PV kW, kvar, power-factor and capacity side effects remain outside this subset.",
                              "Three-winding kVA side effects, dimension-reset values and post-array selector state are not guessed."]}
    try:
        replay = _Replay(inspection, include_device_states=include_device_states,
                         include_four_conductor_lines=include_four_conductor_lines)
        result["objects"] = replay.run()
        result["steps"], result["issues"] = replay.steps, replay.issues
        if any(obj["status"] == "invalid" for obj in result["objects"]):
            result["status"] = "invalid"
        elif any(obj["status"] == "partial" for obj in result["objects"]):
            result["status"] = "partial"
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        result["status"] = "invalid"
        result["objects"], result["steps"] = [], []
        result["issues"] = [{"code": "ordered_inspection_shape_invalid", "severity": "error", "object_id": None,
                             "field": "inspection", "message": "Input is not a bounded inspection with unambiguous ordered source properties.",
                             "source_refs": []}]
    return result
