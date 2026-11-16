"""Pure, bounded replay of explicit native DSS nominal power configuration.

Copyright (c) yangshaopeng. This is neither a solver nor engineering approval.
Load recalculation boundaries and Generator immediate side effects intentionally
differ. PV request/mode declarations are never observations of actual output.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy


_FIELDS = {
    "load": ("kw_base", "kvar_base", "pf_nominal", "kva"),
    "generator": ("kw_base", "kvar_base", "pf_nominal", "kva"),
    "pvsystem": ("pmpp", "irradiance", "kva", "pf_nominal", "reactive_mode", "kvar_requested"),
}
_COMMON_SAFE = {"phases", "bus1", "kv", "conn", "model", "enabled", "basefreq", "class", "vminpu", "vmaxpu"}
_SAFE = {
    "load": _COMMON_SAFE | {"rneut", "xneut", "status", "vminnorm", "vminemerg", "cvrwatts", "cvrvars",
                             "%mean", "%stddev", "numcust", "%seriesrl", "relweight", "vlowpu", "puxharm", "xrharm"},
    "generator": _COMMON_SAFE | {"status", "vpu", "maxkvar", "minkvar", "pvfactor", "forceon", "%xd", "%xdp",
                                  "%xdpp", "xd", "xdp", "xdpp", "h", "d", "balanced", "xrdp"},
    "pvsystem": _COMMON_SAFE | {"%pmpp", "temperature", "%cutin", "%cutout", "effcurve", "p-tcurve", "%r", "%x",
                                 "%pminnovars", "%pminkvarmax", "kvarmax", "kvarmaxabs", "balanced", "limitcurrent",
                                 "wattpriority", "pfpriority", "varfollowinverter"},
}
_MAX_ITEMS = 100000
_MAX_CHARS = 20 * 1024 * 1024
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def _missing():
    return {"status": "not_declared", "value": None, "source_refs": [],
            "reason": "No explicit supported basis; native numerical defaults are not inserted."}


def _ordered(inspection):
    if not isinstance(inspection, Mapping):
        raise ValueError("Inspection must be a JSON object.")
    objects, commands = inspection.get("objects"), inspection.get("commands", [])
    if (not isinstance(objects, list) or not isinstance(commands, list)
            or len(objects) > _MAX_ITEMS or len(commands) > _MAX_ITEMS):
        raise ValueError("Objects and commands must be bounded ordered lists.")
    positions, ordered, chars = {}, True, 0
    for index, command in enumerate(commands):
        if (not isinstance(command, Mapping) or not isinstance(command.get("file"), str) or not command["file"]
                or not isinstance(command.get("command"), str) or isinstance(command.get("line"), bool)
                or not isinstance(command.get("line"), int) or not 1 <= command["line"] <= _MAX_ITEMS):
            raise ValueError("Malformed command/source metadata.")
        key = command["file"], command["line"]
        if key in positions:
            ordered = False
        positions[key] = index, command["command"].casefold()
        chars += len(command["file"]) + len(command["command"])
        if chars > _MAX_CHARS:
            raise ValueError("Source text budget exceeded.")
    identifiers, owners, selected, events, count = set(), {}, [], [], 0
    for obj in objects:
        if (not isinstance(obj, Mapping) or not isinstance(obj.get("name"), str) or not obj["name"]
                or len(obj["name"]) > 256 or not isinstance(obj.get("class"), str)
                or not isinstance(obj.get("properties"), list)):
            raise ValueError("Malformed object/property metadata.")
        identifier, kind = obj["name"].casefold(), obj["class"].casefold()
        if identifier in identifiers or "." not in identifier or identifier.split(".", 1)[0] != kind:
            raise ValueError("Duplicate or inconsistent object identity.")
        identifiers.add(identifier)
        if kind in _FIELDS:
            selected.append(obj)
        chars += len(obj["name"]) + len(obj["class"])
        prior = -1
        for property_index, prop in enumerate(obj["properties"]):
            count += 1
            if (count > _MAX_ITEMS or not isinstance(prop, Mapping)
                    or not all(isinstance(prop.get(key), str) and prop[key] for key in ("name", "value", "source_file"))
                    or isinstance(prop.get("line"), bool) or not isinstance(prop.get("line"), int)
                    or not 1 <= prop["line"] <= _MAX_ITEMS):
                raise ValueError("Malformed or excessive property/source metadata.")
            key = prop["source_file"], prop["line"]
            command = positions.get(key)
            if command is None or command[1] not in {"new", "edit", "~"} or command[0] < prior:
                ordered = False
                command = command or (len(commands), "unresolved")
            if key in owners and owners[key] != identifier:
                ordered = False
            owners[key], prior = identifier, command[0]
            if kind in _FIELDS:
                events.append((command[0], property_index, identifier, prop))
            chars += len(prop["name"]) + len(prop["value"]) + len(prop["source_file"])
            if chars > _MAX_CHARS:
                raise ValueError("Source text budget exceeded.")
    return selected, sorted(events, key=lambda item: (item[0], item[1])), ordered


class _Replay:
    def __init__(self, inspection):
        objects, self.events, self.ordered = _ordered(inspection)
        self.rows, self.states = {}, {}
        for obj in objects:
            identifier, kind = obj["name"].casefold(), obj["class"].casefold()
            self.rows[identifier] = {"object_id": obj["name"], "class": kind, "status": "partial",
                                     "fields": {field: _missing() for field in _FIELDS[kind]}, "issues": []}
            self.states[identifier] = {"load_mode": None, "automatic_generator_rating": True}

    def issue(self, row, code, reason, refs=(), *, invalid=False):
        item = {"code": code, "severity": "error" if invalid else "warning", "object_id": row["object_id"],
                "message": reason, "source_refs": deepcopy(list(refs))}
        if item not in row["issues"]:
            row["issues"].append(item)

    def refs(self, row, *names):
        result = []
        for name in names:
            for ref in row["fields"][name]["source_refs"]:
                if ref not in result:
                    result.append(ref)
        return result

    def put(self, row, key, value, refs, origin):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            self.uncertain(row, (key,), refs, "Derived value is nonfinite; the native quantity is not reconstructed.")
            return
        row["fields"][key] = {"status": "declared", "value": value, "source_refs": deepcopy(list(refs)), "origin": origin}

    def uncertain(self, row, names, refs, reason):
        for key in names:
            combined = self.refs(row, key)
            for ref in refs:
                if ref not in combined:
                    combined.append(ref)
            row["fields"][key] = {"status": "uncertain", "value": None, "source_refs": deepcopy(combined), "reason": reason}

    def known(self, row, *names):
        return all(row["fields"][name]["status"] == "declared" for name in names)

    def number(self, row, prop, ref, *, pf=False, nonnegative=False, positive=False, nonzero_parser=False):
        value = prop["value"].strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        try:
            if not _DECIMAL.fullmatch(value):
                raise ValueError
            value = float(value)
            if nonzero_parser and math.isfinite(value) and abs(value) < 1e-8:
                value = 1e-8
            if (not math.isfinite(value) or (pf and not 0 < abs(value) <= 1)
                    or (nonnegative and value < 0) or (positive and value <= 0)):
                raise ValueError
        except (ValueError, OverflowError):
            self.issue(row, "invalid_bounded_power_value", "Power setting is outside the bounded finite numerical domain.", [ref], invalid=True)
            return None
        return value

    def pq(self, p, pf):
        # Native P/PF sign convention, not a sign-normalized engineering rule.
        try:
            q = p * math.sqrt(1.0 / (pf * pf) - 1.0)
        except (ValueError, OverflowError, ZeroDivisionError):
            return None
        q = -q if pf < 0 else q
        return q if math.isfinite(q) else None

    def load_end(self, identifier):
        row, mode = self.rows[identifier], self.states[identifier]["load_mode"]
        fields = row["fields"]
        if mode == "pq":
            refs = self.refs(row, "kw_base", "kvar_base")
            if self.known(row, "kw_base", "kvar_base"):
                p, q = fields["kw_base"]["value"], fields["kvar_base"]["value"]
                s = math.hypot(p, q)
                self.put(row, "kva", s, refs, "load_command_end_pq_magnitude")
                if s > 0 and math.isfinite(s):
                    pf = p / s
                    if q != 0:
                        pf *= -1 if (p < 0) != (q < 0) else 1
                    self.put(row, "pf_nominal", pf, refs, "load_command_end_pq_power_factor")
                else:
                    self.uncertain(row, ("pf_nominal",), refs, "Zero/nonfinite apparent power does not establish a new native PF.")
            else:
                self.uncertain(row, ("pf_nominal", "kva"), refs, "The final kW/kvar mode lacks an explicit trustworthy pair.")
        elif mode in {"p_pf", "s_pf"}:
            root = "kw_base" if mode == "p_pf" else "kva"
            refs = self.refs(row, root, "pf_nominal")
            derived = ("kvar_base", "kva") if mode == "p_pf" else ("kw_base", "kvar_base")
            if self.known(row, root, "pf_nominal"):
                pf = fields["pf_nominal"]["value"]
                p = fields[root]["value"] if mode == "p_pf" else fields[root]["value"] * abs(pf)
                q = self.pq(p, pf)
                if q is not None and math.isfinite(p):
                    if mode == "s_pf":
                        self.put(row, "kw_base", p, refs, "load_command_end_kva_pf")
                    self.put(row, "kvar_base", q, refs, "load_command_end_power_factor")
                    if mode == "p_pf":
                        self.put(row, "kva", math.hypot(p, q), refs, "load_command_end_pq_magnitude")
                    return
            self.uncertain(row, derived, refs, "The final power/PF mode lacks a finite explicit trustworthy pair.")
        elif any(cell["status"] != "not_declared" for cell in fields.values()):
            self.uncertain(row, tuple(fields), self.refs(row, *fields), "The Load specification mode has no explicit supported basis.")

    def generator_sync(self, identifier, ref):
        row, state = self.rows[identifier], self.states[identifier]
        fields = row["fields"]
        refs = self.refs(row, "kw_base", "pf_nominal")
        if self.known(row, "kw_base", "pf_nominal"):
            q = self.pq(fields["kw_base"]["value"], fields["pf_nominal"]["value"])
            if q is None:
                self.uncertain(row, ("kvar_base",), refs, "Generator P/PF conversion is outside finite bounds.")
            else:
                self.put(row, "kvar_base", q, refs, "generator_immediate_kw_pf_sync")
        else:
            self.uncertain(row, ("kvar_base",), refs, "Generator immediate synchronization depends on an unspecified base kW/PF.")
        if state["automatic_generator_rating"] is True:
            refs = self.refs(row, "kw_base")
            if self.known(row, "kw_base") and fields["kw_base"]["value"] > 0:
                self.put(row, "kva", fields["kw_base"]["value"] * 1.2, refs, "generator_unset_rating_kw_times_1_2")
            else:
                self.uncertain(row, ("kva",), refs, "An unspecified or nonpositive kW cannot establish a usable automatic generator rating.")
        elif state["automatic_generator_rating"] is None:
            self.uncertain(row, ("kva",), [ref], "Generator rating mode has unsupported prior effects.")

    def event(self, identifier, prop, ref):
        row, state, name = self.rows[identifier], self.states[identifier], prop["name"].casefold()
        kind, fields = row["class"], row["fields"]
        supported = {"kw", "kvar", "pf", "kva"} if kind == "load" else {"kw", "kvar", "pf", "kva", "mva"} if kind == "generator" else {"pmpp", "irradiance", "kva", "pf", "kvar"}
        if name not in supported:
            if name not in _SAFE[kind]:
                self.uncertain(row, tuple(fields), [ref], "An unsupported property may change the nominal power configuration.")
                state["load_mode"], state["automatic_generator_rating"] = None, None
                self.issue(row, "unsupported_power_side_effect", "This property's power side effects are not replayed.", [ref])
            return
        value = self.number(row, prop, ref, pf=name == "pf", nonnegative=kind == "pvsystem" and name in {"pmpp", "irradiance"},
                            positive=name in {"kva", "mva"}, nonzero_parser=name == "kw" or name == "kva")
        if value is None:
            self.uncertain(row, tuple(fields), [ref], "An invalid power property blocks trustworthy replay.")
            state["load_mode"], state["automatic_generator_rating"] = None, None
            return
        key = {"kw": "kw_base", "kvar": "kvar_requested" if kind == "pvsystem" else "kvar_base", "pf": "pf_nominal", "mva": "kva"}.get(name, name)
        if name == "mva":
            value *= 1000
        origin = "explicit_native_property"
        if name == "kw" or name == "kva":
            try:
                literal = float(prop["value"].strip().strip("\"'"))
                if literal != value:
                    origin = "explicit_native_nonzero_parser_normalization"
            except (ValueError, OverflowError):
                pass
        self.put(row, key, value, [ref], origin)
        if kind == "load":
            if name in {"kw", "kvar", "kva"}:
                state["load_mode"] = {"kw": "p_pf", "kvar": "pq", "kva": "s_pf"}[name]
        elif kind == "generator":
            if name in {"kw", "pf"}:
                self.generator_sync(identifier, ref)
            elif name == "kvar":
                refs = self.refs(row, "kw_base", "kvar_base")
                if self.known(row, "kw_base"):
                    p, q = fields["kw_base"]["value"], value
                    s = math.hypot(p, q)
                    if math.isfinite(s):
                        pf = p / s if s != 0 else 1.0
                        if (p < 0) != (q < 0) and p != 0 and q != 0:
                            pf = -pf
                        self.put(row, "pf_nominal", pf, refs, "generator_immediate_kvar_power_factor")
                    else:
                        self.uncertain(row, ("pf_nominal",), refs, "The generator P/Q apparent power exceeds the bounded finite domain.")
                else:
                    self.uncertain(row, ("pf_nominal",), refs, "Generator kvar assignment depends on an unspecified base kW.")
            elif name in {"kva", "mva"}:
                state["automatic_generator_rating"] = False
        elif name in {"pf", "kvar"}:
            self.put(row, "reactive_mode", "source_selected_pf" if name == "pf" else "source_selected_kvar", [ref],
                     "last_explicit_source_selection_only_not_runtime_mode")

    def run(self):
        if not self.ordered:
            for row in self.rows.values():
                self.uncertain(row, tuple(row["fields"]), [], "Source command order is unavailable or ambiguous.")
                self.issue(row, "power_command_order_unresolved", "No source-ordered power replay is asserted.")
        else:
            previous = None
            for command_index, property_index, identifier, prop in self.events:
                batch = command_index, identifier
                if previous is not None and batch != previous and self.rows[previous[1]]["class"] == "load":
                    self.load_end(previous[1])
                ref = {"source_file": prop["source_file"], "line": prop["line"], "property": prop["name"],
                       "command_index": command_index, "property_index": property_index}
                self.event(identifier, prop, ref)
                previous = batch
            if previous is not None and self.rows[previous[1]]["class"] == "load":
                self.load_end(previous[1])
        result = []
        for identifier in sorted(self.rows):
            row = self.rows[identifier]
            if any(issue["severity"] == "error" for issue in row["issues"]):
                row["status"] = "invalid"
            elif all(cell["status"] == "declared" for cell in row["fields"].values()) and not row["issues"]:
                row["status"] = "complete"
            result.append(row)
        return result


def replay_power_operating_points(inspection: Mapping) -> dict:
    """Replay explicit nominal configuration without I/O, engine or mutation.

    ``reactive_mode`` and ``kvar_requested`` for PV describe source requests only;
    native runtime comparison must always mark them not_checked. Nominal fields
    do not establish terminal P/Q, operating limits or engineering validity.
    """
    result = {"version": "1.0", "scope": "bounded_native_power_setpoints", "status": "complete", "objects": [],
              "issues": [], "engineering_accepted": False,
              "limitations": ["No unspecified native numerical defaults are supplied.",
                              "Nominal base settings and device ratings are distinct from solved terminal power.",
                              "PV source-selected mode and requested kvar have no trustworthy native readback here and remain unverified.",
                              "PV inverter output, clipping, temperature/efficiency curves and control behavior are not reconstructed."]}
    try:
        replay = _Replay(inspection)
        result["objects"] = replay.run()
        result["issues"] = [deepcopy(issue) for row in result["objects"] for issue in row["issues"]]
        if any(row["status"] == "invalid" for row in result["objects"]):
            result["status"] = "invalid"
        elif any(row["status"] != "complete" for row in result["objects"]):
            result["status"] = "partial"
    except (ValueError, OverflowError, TypeError) as exc:
        result.update(status="invalid", objects=[], issues=[{"code": "malformed_power_inspection", "severity": "error", "message": str(exc)}])
    return result
