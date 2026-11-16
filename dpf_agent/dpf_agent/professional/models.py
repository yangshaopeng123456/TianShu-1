"""Immutable, statically inspected native DSS packages; no solver is executed.

Copyright (c) yangshaopeng. Input support is not numerical or engineering acceptance.
"""
from __future__ import annotations

import hashlib
import csv
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Mapping

from .contracts import ContractError
from .device_semantics import inspect_device_semantics


MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 500
POLICY_VERSION = "native-snapshot-1.0"
_ID = re.compile(r"model-[0-9a-f]{64}\Z")
_OBJECT = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z0-9_][A-Za-z0-9_-]{0,127})\Z")
_RESERVED = re.compile(r"(?:con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?\Z", re.I)
_COMMON = {"basefreq", "enabled"}
_PROPERTIES = {
    "circuit": {"bus1", "bus2", "basekv", "pu", "angle", "frequency", "phases", "mvasc3", "mvasc1", "isc3", "isc1", "r1", "x1", "r0", "x0", "r1x1", "x0x1", "basefreq"},
    "vsource": _COMMON | {"bus1", "bus2", "basekv", "pu", "angle", "frequency", "phases", "mvasc3", "mvasc1", "isc3", "isc1", "r1", "x1", "r0", "x0", "r1x1", "x0x1", "sequence", "scantype", "model"},
    "line": _COMMON | {"bus1", "bus2", "phases", "linecode", "length", "units", "r1", "x1", "r0", "x0", "c1", "c0", "rmatrix", "xmatrix", "cmatrix", "switch", "normamps", "emergamps", "rg", "xg", "rho", "b1", "b0"},
    "linecode": {"nphases", "r1", "x1", "r0", "x0", "c1", "c0", "units", "rmatrix", "xmatrix", "cmatrix", "basefreq", "normamps", "emergamps", "neutral", "rg", "xg", "rho", "b1", "b0", "kron"},
    "transformer": _COMMON | {"phases", "windings", "wdg", "bus", "conn", "kv", "kva", "tap", "%r", "rneut", "xneut", "buses", "conns", "kvs", "kvas", "taps", "xhl", "xht", "xlt", "xscarray", "thermal", "n", "m", "flrise", "hsrise", "%loadloss", "%noloadloss", "normhkva", "emerghkva", "sub", "maxtap", "mintap", "numtaps", "%imag", "ppm_antifloat", "%rs", "leadlag", "x12", "x13", "x23"},
    "load": _COMMON | {"phases", "bus1", "kv", "kw", "pf", "model", "conn", "kvar", "rneut", "xneut", "status", "class", "vminpu", "vmaxpu", "vminnorm", "vminemerg", "kva", "allocationfactor", "cvrwatts", "cvrvars", "%mean", "%stddev", "numcust", "kwhr", "kwhrdays", "cfactor", "%seriesrl", "relweight", "vlowpu", "puxharm", "xrharm"},
    "generator": _COMMON | {"phases", "bus1", "kv", "kw", "pf", "model", "conn", "kvar", "vminpu", "vmaxpu", "status", "class", "vpu", "maxkvar", "minkvar", "pvfactor", "forceon", "kva", "mva", "%xd", "%xdp", "%xdpp", "xd", "xdp", "xdpp", "h", "d", "balanced", "xrdp"},
    "pvsystem": _COMMON | {"phases", "bus1", "kv", "irradiance", "pmpp", "%pmpp", "temperature", "pf", "conn", "kvar", "kva", "%cutin", "%cutout", "effcurve", "p-tcurve", "%r", "%x", "model", "vminpu", "vmaxpu", "class", "%pminnovars", "%pminkvarmax", "kvarmax", "kvarmaxabs", "balanced", "limitcurrent", "wattpriority", "pfpriority", "varfollowinverter"},
    "xycurve": {"npts", "points", "xarray", "yarray", "csvfile", "xshift", "yshift", "xscale", "yscale"},
    "capacitor": _COMMON | {"bus1", "bus2", "phases", "kv", "kvar", "conn", "numsteps", "states", "r", "xl"},
    "reactor": _COMMON | {"bus1", "bus2", "phases", "kv", "kvar", "conn", "r", "x"},
}
_SET = {"voltagebases", "basefrequency", "defaultbasefrequency", "frequency", "mode", "controlmode", "maxiterations", "tolerance", "algorithm", "loadmodel", "loadmult", "genmult"}
_TEXT_PROPERTIES = {"bus1", "bus2", "bus", "buses", "conn", "conns", "units", "status", "linecode", "effcurve", "p-tcurve", "enabled", "switch", "balanced", "forceon", "varfollowinverter", "wattpriority", "pfpriority", "limitcurrent", "sub", "leadlag", "sequence", "scantype", "csvfile", "kron"}
_ARRAY_PROPERTIES = {"rmatrix", "xmatrix", "cmatrix", "kvs", "kvas", "taps", "%rs", "xscarray", "points", "xarray", "yarray", "states"}


def _fail(field, message, *, unsupported=False, integrity=False):
    code = "model_integrity_error" if integrity else "capability_unavailable" if unsupported else "invalid_contract"
    raise ContractError(field, message, code)


def _require_consistent_devices(report):
    if report["status"] != "invalid":
        return
    issue = next((item for item in report["issues"] if item.get("severity") == "error"), {})
    detail = ": ".join(str(issue[key]) for key in ("object_id", "field", "message") if issue.get(key))
    _fail("model.device_semantics", detail or "explicit device/connection declarations contradict the supported native semantics")


def _unicode(value, field):
    if not isinstance(value, str):
        _fail(field, "must be a string")
    try:
        data = value.encode("utf-8")
    except UnicodeError:
        _fail(field, "must be valid UTF-8 Unicode")
    if "\x00" in value:
        _fail(field, "NUL is not accepted")
    return data


def _object(value, keys, field):
    if not isinstance(value, Mapping):
        _fail(field, "must be an object")
    for key in value:
        _unicode(key, field)
    if set(value) != set(keys):
        _fail(field, "object fields do not match the package contract")
    return value


def _path(value, field="model.path"):
    _unicode(value, field)
    if not value or len(value) > 240 or value.startswith("/") or any(c in value for c in "\\:<>\"|?*%@$`") or any(ord(c) < 32 for c in value):
        _fail(field, "must be a bounded relative POSIX file path")
    parts = value.split("/")
    if any(not p or p in {".", ".."} or p.endswith((".", " ")) or _RESERVED.fullmatch(p) or _RESERVED.fullmatch(p.split(".", 1)[0].rstrip(" .")) or re.search(r"~[0-9]", p) for p in parts):
        _fail(field, "unsafe or aliased Windows path component")
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))


def _ancestors_unlinked(path):
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            if _link(item):
                _fail("model", "linked or reparse-point paths are not accepted")


def _uncomment(text, field):
    out, quote, block, i = [], None, False, 0
    while i < len(text):
        char, pair = text[i], text[i:i + 2]
        if block:
            if pair == "*/":
                block, i = False, i + 2
                continue
            out.append("\n" if char == "\n" else " ")
        elif quote:
            out.append(char)
            if char == quote:
                quote = None
            elif char in "\r\n":
                _fail(field, "multiline quoted DSS values are outside the input policy", unsupported=True)
        elif char in "\"'":
            quote = char
            out.append(char)
        elif pair == "/*":
            block, i = True, i + 2
            continue
        elif char == "!" or pair == "//":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        else:
            out.append(char)
        i += 1
    if block or quote:
        _fail(field, "unclosed DSS comment or quoted value")
    return "".join(out)


def _tokens(line, field):
    """Tokenize delimiters without treating text inside quotes as DSS commands."""
    result, i = [], 0
    pairs = {"[": "]", "(": ")", "{": "}"}
    while i < len(line):
        if line[i].isspace() or line[i] == ",":
            i += 1
            continue
        if line[i] == "=":
            result.append("=")
            i += 1
            continue
        start, stack, quote = i, [], None
        while i < len(line):
            char = line[i]
            if quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char in pairs:
                stack.append(pairs[char])
            elif char in "])}":
                if not stack or stack.pop() != char:
                    _fail(field, "unbalanced DSS value delimiters")
            elif not stack and (char.isspace() or char in ",="):
                break
            i += 1
        if quote or stack:
            _fail(field, "multiline or unclosed DSS value delimiters are unsupported", unsupported=True)
        token = line[start:i]
        if not token or ";" in token:
            _fail(field, "multiple-command or empty DSS token is unsupported", unsupported=True)
        result.append(token)
    return result


def _scalar(token):
    if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        return token[1:-1]
    return token


def _assignments(tokens, field):
    if len(tokens) % 3 or any(tokens[i + 1] != "=" for i in range(0, len(tokens), 3)):
        _fail(field, "only explicit named DSS properties are supported", unsupported=True)
    result = []
    for i in range(0, len(tokens), 3):
        key, value = tokens[i].casefold(), tokens[i + 2]
        if not re.fullmatch(r"[%A-Za-z][%A-Za-z0-9_-]*", key):
            _fail(field, "invalid DSS property name")
        if any(c in value for c in "@$`") or "%result%" in value.casefold():
            _fail(field, "dynamic variables or external expressions are unsupported", unsupported=True)
        result.append((key, value))
    return result


def _numbers(value, field):
    if len(value) < 2 or value[0] not in "[(" or value[-1] not in ")]":
        _fail(field, "numeric arrays require explicit bracket or parenthesis delimiters", unsupported=True)
    parts = re.split(r"[\s,|]+", value[1:-1].strip())
    if not parts or len(parts) > 20000:
        _fail(field, "numeric array is empty or exceeds the declared dimension budget")
    try:
        result = [float(x) for x in parts]
    except ValueError:
        _fail(field, "only explicit numeric array entries are supported", unsupported=True)
    if not all(math.isfinite(x) for x in result):
        _fail(field, "numeric arrays must be finite")
    return result


def _property_value(kind, key, value):
    scalar = _scalar(value).casefold()
    if kind in {"capacitor","reactor"} and key=="conn" and scalar not in {"wye","ln"}:
        _fail("model.properties", "passive shunt support is fixed wye only", unsupported=True)
    if key in {"phases", "nphases", "windings", "wdg", "npts", "numtaps", "model", "numsteps"}:
        if len(scalar) > 5 or not re.fullmatch(r"[0-9]+", scalar):
            _fail("model.properties", "dimension/model values must be explicit bounded integers")
        number = int(scalar)
        limits = {"phases": (1, 3), "nphases": (1, 3), "windings": (2, 3), "wdg": (1, 3), "npts": (1, 10000), "numtaps": (1, 10000), "model": (1, 8), "numsteps": (1,1)}
        lower, upper = limits[key]
        if kind in {"line", "linecode"} and key in {"phases", "nphases"}:
            upper = 4  # Full matrices and four explicit nodes are gated below.
        if not lower <= number <= upper:
            _fail("model.properties", "dimension/model exceeds the supported finite range", unsupported=True)
    elif key in _ARRAY_PROPERTIES:
        if re.search(r"(?i)(?:file|csvfile|pqcsvfile|sngfile|dblfile)\s*=", value):
            return  # Separately checked as a closed UTF-8 dependency expression.
        entries = _numbers(value, "model.properties")
        if key == "states" and (len(entries)!=1 or entries[0] not in (0,1)):
            _fail("model.properties", "only a single fixed capacitor step is supported", unsupported=True)
    elif key in _TEXT_PROPERTIES:
        choices = {
            "conn": {"wye", "delta", "ln", "ll"}, "units": {"none", "mi", "kft", "km", "m", "ft", "in", "cm", "mm"},
            "status": {"fixed", "variable"}, "leadlag": {"lead", "lag", "ansi", "euro"},
            "sequence": {"positive", "negative", "zero"}, "scantype": {"positive", "zero", "none"},
            "kron": {"no", "false"},  # Never silently eliminate a conductor.
        }
        if key in {"enabled", "switch", "balanced", "forceon", "varfollowinverter", "wattpriority", "pfpriority", "limitcurrent", "sub"}:
            choices[key] = {"yes", "no", "true", "false"}
        if key in choices and scalar not in choices[key]:
            _fail("model.properties", "unsupported explicit text property value", unsupported=True)
        if key in {"buses", "conns"}:
            if len(value) < 2 or value[0] not in "[(" or value[-1] not in ")]":
                _fail("model.properties", "winding arrays require explicit delimiters")
            parts = _tokens(value[1:-1], "model.properties")
            if not 2 <= len(parts) <= 3 or "=" in parts:
                _fail("model.properties", "only two/three-winding connection arrays are supported", unsupported=True)
            if key == "conns" and any(_scalar(x).casefold() not in {"wye", "delta", "ln", "ll"} for x in parts):
                _fail("model.properties", "unsupported winding connection")
    else:
        try:
            number = float(scalar)
        except ValueError:
            _fail("model.properties", "numeric property requires an explicit finite value", unsupported=True)
        if not math.isfinite(number):
            _fail("model.properties", "numeric property must be finite")
        if key in {"basefreq", "frequency", "kv", "basekv", "kva", "mva"} and number <= 0:
            _fail("model.properties", "rated base quantities must be positive")
        if kind=="pvsystem" and key in {"kvarmax","kvarmaxabs"} and number<0:
            _fail("model.properties", "PV reactive limit magnitudes must be nonnegative")


def _read_limited(path, maximum):
    if path.stat().st_size > maximum:
        _fail("model.files", "package exceeds the byte budget")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        _fail("model.files", "file changed beyond the byte budget")
    return data


def _inspect(master, files):
    by_fold = {name.casefold(): name for name in files}
    seen_scripts, dependencies, objects, commands, references = set(), [], {}, [], []
    circuit_count, physical_lines = 0, 0
    def reference(value, source, line, kind):
        target = _path(_scalar(value), "model.dependency")
        exact = by_fold.get(target.casefold())
        if exact is None:
            _fail("model.dependencies", "a referenced package file is missing")
        dependencies.append({"source": source, "line": line, "target": exact, "kind": kind})
        return exact
    def process(name, root=False):
        nonlocal circuit_count, physical_lines
        if name in seen_scripts:
            _fail("model.dependencies", "repeated or cyclic DSS script inclusion is not accepted")
        seen_scripts.add(name)
        text = _uncomment(files[name].decode("utf-8-sig"), "model.files")
        physical_lines += text.count("\n") + 1
        if physical_lines > 100000:
            _fail("model.files", "DSS input exceeds the declared line budget", unsupported=True)
        active = None
        for line_number, line in enumerate(text.splitlines(), 1):
            tokens = _tokens(line, "model.files")
            if not tokens:
                continue
            command = tokens[0].casefold()
            commands.append({"file": name, "line": line_number, "command": command})
            if command == "compile":
                _fail("model.commands", "internal Compile changes directory state and is not supported", unsupported=True)
            if command == "redirect":
                if not root:
                    _fail("model.dependencies", "leaf DSS scripts cannot include other files", unsupported=True)
                if len(tokens) != 2:
                    _fail("model.dependencies", "Redirect requires exactly one quoted or bare file path")
                target = reference(tokens[1], name, line_number, "script")
                process(target)
                active = None
                continue
            if command == "buscoords":
                if not root or len(tokens) != 2:
                    _fail("model.dependencies", "Buscoords is supported only in the root entry with one path", unsupported=True)
                reference(tokens[1], name, line_number, "bus_coordinates")
                active = None
                continue
            if command in {"clear", "calcvoltagebases"}:
                if len(tokens) != 1:
                    _fail("model.commands", "unexpected command arguments")
                if command == "clear" and (not root or objects or any(x["command"] == "clear" for x in commands[:-1])):
                    _fail("model.commands", "Clear is supported once before circuit/object creation", unsupported=True)
                if command == "calcvoltagebases" and not circuit_count:
                    _fail("model.commands", "circuit must exist before voltage-base calculation")
                active = None
                continue
            if command == "set":
                values = _assignments(tokens[1:], "model.set")
                if not values:
                    _fail("model.set", "Set requires named supported options")
                for key, value in values:
                    scalar = _scalar(value).casefold()
                    if key not in _SET:
                        _fail("model.set", "DSS setting is outside the snapshot policy", unsupported=True)
                    choices = {"mode": {"snapshot"}, "controlmode": {"off"}, "algorithm": {"normal", "newton"}, "loadmodel": {"powerflow", "admittance"}}
                    if key in choices and scalar not in choices[key]:
                        _fail("model.set", "unsupported temporal, control or solution semantics", unsupported=True)
                    if key == "voltagebases" and any(x <= 0 for x in _numbers(value, "model.set")):
                        _fail("model.set", "voltage bases must be positive")
                    if key not in choices and key != "voltagebases":
                        try:
                            number = float(scalar)
                        except ValueError:
                            _fail("model.set", "numeric snapshot setting must be explicit")
                        if not math.isfinite(number) or number < 0 or (key not in {"loadmult", "genmult"} and number == 0):
                            _fail("model.set", "invalid numeric snapshot setting")
                active = None
                continue
            if command in {"open", "close"}:
                if len(tokens)<2 or not _OBJECT.fullmatch(_scalar(tokens[1])):
                    _fail("model.commands", "Open/Close requires one existing Line object", unsupported=True)
                full = _scalar(tokens[1]).casefold()
                pairs = _assignments(tokens[2:], "model.commands")
                values = dict(pairs)
                if full not in objects or not full.startswith("line.") or len(pairs)!=2 or set(values)!={"term","cond"}:
                    _fail("model.commands", "only explicit existing Line term/cond snapshot states are supported", unsupported=True)
                if any(not re.fullmatch(r"[0-9]{1,4}",v) for v in values.values()) or not 1<=int(values["term"])<=2 or not 0<=int(values["cond"])<=3:
                    _fail("model.commands", "invalid explicit switch terminal/conductor")
                active=None
                continue
            if command in {"new", "edit"}:
                if len(tokens) < 2 or not _OBJECT.fullmatch(_scalar(tokens[1])):
                    _fail("model.objects", "use an explicit supported Class.Name identifier", unsupported=True)
                full = _scalar(tokens[1]).casefold()
                kind, _ = full.split(".", 1)
                if kind not in _PROPERTIES:
                    _fail("model.objects", "DSS device/library class is outside the snapshot policy", unsupported=True)
                if command == "new":
                    if full in objects:
                        _fail("model.objects", "duplicate device or library object")
                    if kind == "circuit":
                        circuit_count += 1
                        if circuit_count != 1:
                            _fail("model.objects", "exactly one circuit is supported")
                        objects["vsource.source"] = {"name": "vsource.source", "class": "vsource", "implicit": True, "properties": []}
                    elif not circuit_count:
                        _fail("model.objects", "create the circuit before other objects", unsupported=True)
                    objects[full] = {"name": full, "class": kind, "implicit": False, "properties": []}
                elif full not in objects:
                    _fail("model.objects", "Edit target has not been explicitly created")
                active = full
                values = _assignments(tokens[2:], "model.properties")
            elif command == "~":
                if active is None:
                    _fail("model.commands", "continuation needs a preceding New/Edit in the same script", unsupported=True)
                kind = objects[active]["class"]
                values = _assignments(tokens[1:], "model.properties")
            else:
                _fail("model.commands", "DSS command or abbreviation is outside the snapshot policy", unsupported=True)
            for key, value in values:
                if key not in _PROPERTIES[kind]:
                    _fail("model.properties", "DSS property or abbreviation is outside the class policy", unsupported=True)
                _property_value(kind, key, value)
                if key == "model" and kind in {"generator", "pvsystem"} and _scalar(value) not in {"1", "2"}:
                    _fail("model.properties", "only explicit constant-power/admittance generation models are supported", unsupported=True)
                if key == "csvfile":
                    if not root:
                        _fail("model.dependencies", "leaf DSS scripts cannot reference data files", unsupported=True)
                    reference(value, name, line_number, "xycurve_csv")
                elif re.search(r"(?i)(?:file|csvfile|pqcsvfile|sngfile|dblfile)\s*=", value):
                    if not root or kind != "xycurve" or key not in {"xarray", "yarray"}:
                        _fail("model.dependencies", "external value files are unsupported in this property/context", unsupported=True)
                    if value[0:1] not in "([" or value[-1:] not in ")]":
                        _fail("model.dependencies", "external numeric arrays require a single file expression")
                    nested = _assignments(_tokens(value[1:-1], "model.dependencies"), "model.dependencies")
                    if len(nested) != 1 or nested[0][0] != "file":
                        _fail("model.dependencies", "only one UTF-8 numeric File expression is supported", unsupported=True)
                    reference(nested[0][1], name, line_number, "numeric_array")
                if key == "linecode":
                    references.append(("linecode." + _scalar(value).casefold(), name))
                if key in {"effcurve", "p-tcurve"}:
                    references.append(("xycurve." + _scalar(value).casefold(), name))
                objects[active]["properties"].append({"name": key, "value": value, "source_file": name, "line": line_number})
    process(master, root=True)
    if circuit_count != 1:
        _fail("model.objects", "exactly one native Circuit is required")
    if any(target not in objects for target, _ in references):
        _fail("model.objects", "a referenced library object is missing")
    for item in dependencies:
        if item["kind"] == "script":
            continue
        lines = files[item["target"]].decode("utf-8-sig").splitlines()
        if len(lines) > 10000 or any(len(line) > 65536 for line in lines):
            _fail("model.dependencies", "data dependency exceeds the row/field budget")
        try:
            rows = [row for row in csv.reader(lines, strict=True) if row and any(cell.strip() for cell in row)]
        except csv.Error:
            _fail("model.dependencies", "malformed or oversized CSV dependency")
        expected_columns = {"bus_coordinates": 3, "xycurve_csv": 2, "numeric_array": 1}[item["kind"]]
        if not 1 <= len(rows) <= 10000 or any(len(row) != expected_columns for row in rows):
            _fail("model.dependencies", "data file has unsupported row/column dimensions")
        try:
            numbers = [float(cell) for row in rows for cell in (row[1:] if item["kind"] == "bus_coordinates" else row)]
        except ValueError:
            _fail("model.dependencies", "data dependency requires explicit numeric columns")
        if not all(math.isfinite(x) for x in numbers):
            _fail("model.dependencies", "data dependency contains non-finite values")
        item["rows"] = len(rows)
    for obj in objects.values():
        properties = {x["name"]: x["value"] for x in obj["properties"]}
        if obj["class"] in {"line", "linecode"}:
            phases = int(_scalar(properties.get("nphases" if obj["class"] == "linecode" else "phases", "3")))
            for key in {"rmatrix", "xmatrix", "cmatrix"} & set(properties):
                count = len(_numbers(properties[key], "model.properties"))
                if count not in {phases * phases, phases * (phases + 1) // 2}:
                    _fail("model.properties", "matrix dimensions do not match the declared native phase count")
        if obj["class"] == "xycurve":
            if "npts" not in properties:
                _fail("model.properties", "XYCurve requires explicit bounded npts", unsupported=True)
            count = int(_scalar(properties["npts"]))
            for key in {"points", "xarray", "yarray", "csvfile"} & set(properties):
                value = properties[key]
                if key == "csvfile":
                    length = next(x["rows"] for x in dependencies if x["target"].casefold() == _scalar(value).casefold() and x["kind"] == "xycurve_csv")
                elif re.search(r"(?i)file\s*=", value):
                    ref = _assignments(_tokens(value[1:-1], "model.dependencies"), "model.dependencies")[0][1]
                    length = next(x["rows"] for x in dependencies if x["target"].casefold() == _scalar(ref).casefold() and x["kind"] == "numeric_array")
                else:
                    length = len(_numbers(value, "model.properties"))
                if length != count * (2 if key == "points" else 1):
                    _fail("model.properties", "XYCurve point count does not match explicit npts")
            if not ({"points", "csvfile"} & set(properties) or {"xarray", "yarray"} <= set(properties)):
                _fail("model.properties", "XYCurve requires a complete explicit point definition")
        if obj["class"] == "transformer":
            windings = int(_scalar(properties.get("windings", "2")))
            if any(x["name"] == "wdg" and int(_scalar(x["value"])) > windings for x in obj["properties"]):
                _fail("model.properties", "winding selector exceeds winding count")
            for key in {"buses", "conns", "kvs", "kvas", "taps", "%rs"} & set(properties):
                value = properties[key]
                count = len(_tokens(value[1:-1], "model.properties")) if key in {"buses", "conns"} else len(_numbers(value, "model.properties"))
                if count != windings:
                    _fail("model.properties", "winding array dimensions do not match winding count")
            if "xscarray" in properties and len(_numbers(properties["xscarray"], "model.properties")) != windings * (windings - 1) // 2:
                _fail("model.properties", "reactance array dimension does not match winding count")
    active_files = {master} | {x["target"] for x in dependencies}
    return {"policy_version": POLICY_VERSION, "status": "supported_snapshot_input", "format": "OpenDSS",
            "numerical_validity": "not_run", "engineering_accepted": False,
            "semantics": {"units": "native_OpenDSS_preserved", "phases": "native_nodes_preserved_not_inferred", "connections": "native_OpenDSS_preserved", "temporal_scope": "specified_snapshot_only", "controls": "automatic_controls_not_supported"},
            "dependencies": dependencies, "objects": [objects[k] for k in sorted(objects)],
            "commands": commands, "unused_files": sorted(set(files) - active_files),
            "limitations": ["Static supported-input inspection only; native compilation and numerical validity remain untested.", "Root entry Redirects leaf scripts only; internal Compile and leaf file references are not supported.", "No normalization, conversion, phase assignment or engineering limits are inferred."]}


def _payload(payload):
    value = _object(payload, {"master_file", "files"}, "model")
    master = _path(value["master_file"], "model.master_file")
    if "/" in master or PurePosixPath(master).suffix.casefold() != ".dss":
        _fail("model.master_file", "entry must be one package-root DSS filename", unsupported=True)
    if not isinstance(value["files"], list) or not 1 <= len(value["files"]) <= MAX_FILES:
        _fail("model.files", "package requires 1 to 500 files")
    files, folded, uppered, aliases, total = {}, set(), set(), {}, 0
    for item in value["files"]:
        item = _object(item, {"path", "content_utf8"}, "model.files")
        path = _path(item["path"])
        parts = path.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            for alias in ("fold:" + prefix.casefold(), "upper:" + prefix.upper()):
                if alias in aliases and aliases[alias] != prefix:
                    _fail("model.files", "case-insensitive path component aliases are not accepted")
                aliases[alias] = prefix
        if path.casefold() in folded or path.upper() in uppered:
            _fail("model.files", "duplicate or case-insensitive colliding file paths")
        data = _unicode(item["content_utf8"], "model.content_utf8")
        if any(ord(c) < 32 and c not in "\r\n\t" for c in item["content_utf8"]):
            _fail("model.content_utf8", "unsupported control character")
        total += len(data)
        if total > MAX_BYTES:
            _fail("model.files", "package exceeds 20 MiB")
        folded.add(path.casefold())
        uppered.add(path.upper())
        files[path] = data
    if master not in files:
        _fail("model.master_file", "exact entry filename is missing")
    for name in files:
        if any(parent.as_posix().casefold() in folded or parent.as_posix().upper() in uppered for parent in PurePosixPath(name).parents if parent.as_posix() != "."):
            _fail("model.files", "a package file also names a directory")
    return master, files


def _conversion_metadata(master, files):
    """Rebuild conversion provenance; a caller's JSON is never trusted alone."""
    reserved = {name for name in files if name.casefold().startswith("__tianshu_input__/")}
    if not reserved:
        return None
    try:
        request_path="__tianshu_input__/request.json"
        conversion_path="__tianshu_input__/conversion.json"
        request=json.loads(files[request_path].decode("utf-8"))
        if files[request_path] != _canonical(request):raise ValueError("noncanonical_request")
        if isinstance(request, Mapping) and request.get("format") in {"JSON", "CSV"}:
            from .tabular_input import verify_packaged_tables
            return verify_packaged_tables(master, files, request)
        from .matpower_input import adapt_matpower_payload, ADAPTER_VERSION
        historical=json.loads(files[conversion_path].decode("utf-8"))
        if (not isinstance(request, Mapping) or request.get("format") != "MATPOWER"
                or not isinstance(request.get("source_file"), str)
                or not isinstance(request.get("content_utf8"), str)):
            raise ValueError("source_request_header_invalid")
        source_name = _path(request["source_file"], "model.conversion.source_file")
        if "/" in source_name or PurePosixPath(source_name).suffix.casefold() != ".m":
            raise ValueError("source_request_filename_invalid")
        original = "__tianshu_input__/original/" + source_name
        if reserved != {request_path, conversion_path, original}:
            raise ValueError("reserved_conversion_file_set_invalid")
        if (not isinstance(historical, Mapping) or files[conversion_path] != _canonical(historical)
                or historical.get("version") != "1.0"
                or historical.get("source_format") != "MATPOWER"
                or historical.get("target_format") != "OpenDSS"
                or historical.get("source_file") != source_name
                or not isinstance(historical.get("adapter_version"), str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", historical["adapter_version"]) is None):
            raise ValueError("conversion_container_header_invalid")
        original_bytes = request["content_utf8"].encode("utf-8")
        if (files[original] != original_bytes
                or type(historical.get("source_bytes")) is not int
                or historical["source_bytes"] != len(original_bytes)
                or historical.get("source_sha256") != hashlib.sha256(original_bytes).hexdigest()):
            raise ValueError("original_source_bytes_or_hash_invalid")
        if master in reserved:
            raise ValueError("native_master_is_reserved_input")
        native = {"master_file": master, "files": [
            {"path": name, "content_utf8": files[name].decode("utf-8")}
            for name in sorted(set(files) - reserved)]}
        if (historical.get("native_master_file") != master
                or historical.get("native_master_sha256") != hashlib.sha256(files[master]).hexdigest()
                or historical.get("native_payload_sha256") != hashlib.sha256(_canonical(native)).hexdigest()):
            raise ValueError("native_bytes_or_hash_invalid")
        # The outer byte/container contract is checked independently of its
        # versioned electrical interpreter. Unavailable code must not rewrite
        # old packages or misreport sound source bytes as file corruption.
        if historical["adapter_version"] != ADAPTER_VERSION:
            _fail("model.conversion", "stored conversion adapter version is not available; original files are retained and are not automatically rewritten", unsupported=True)
        adapted=adapt_matpower_payload(request)
        expected={item["path"]:item["content_utf8"].encode("utf-8") for item in adapted["native_payload"]["files"]}
        expected.update({request_path:_canonical(request), conversion_path:_canonical(adapted["conversion_manifest"]),
                         original:request["content_utf8"].encode("utf-8")})
        if master!=adapted["native_payload"]["master_file"] or files!=expected:
            raise ValueError("conversion_or_original_mismatch")
        return adapted["conversion_manifest"]
    except ContractError as exc:
        if exc.code=="capability_unavailable":raise
        _fail("model.conversion", "reserved conversion files do not match their source contract", integrity=True)
    except (KeyError,TypeError,ValueError,UnicodeError,RecursionError):
        _fail("model.conversion", "reserved conversion files must exactly reproduce the original input and declared adapter", integrity=True)


class ModelStore:
    """Write-once packages, with full file/hash/policy verification on resolution."""

    def __init__(self, root):
        path = Path(root).absolute()
        _ancestors_unlinked(path)
        path.mkdir(parents=True, exist_ok=True)
        self.root = path.resolve()

    @staticmethod
    def pack_directory(directory, master_file):
        source = Path(directory).absolute()
        _ancestors_unlinked(source)
        if not source.is_dir():
            _fail("model.directory", "explicit source directory is missing")
        entries, total = [], 0
        directory_count = 0
        for directory_path, directories, names in os.walk(source, followlinks=False):
            directory_count += 1
            if directory_count > 501:
                _fail("model.directory", "package exceeds the directory budget")
            folder = Path(directory_path)
            for name in directories + names:
                path = folder / name
                _path(path.relative_to(source).as_posix())
                if _link(path):
                    _fail("model.directory", "linked or reparse-point package entries are not accepted")
            for name in names:
                path = folder / name
                if not stat.S_ISREG(path.lstat().st_mode):
                    _fail("model.directory", "only regular package files are accepted")
                if len(entries) >= MAX_FILES or total + path.stat().st_size > MAX_BYTES:
                    _fail("model.directory", "directory exceeds the 500 file / 20 MiB budget")
                remaining = MAX_BYTES - total
                data = _read_limited(path, remaining)
                total += len(data)
                if total > MAX_BYTES:
                    _fail("model.directory", "source changed beyond the byte budget")
                try:
                    text = data.decode("utf-8")
                except UnicodeError:
                    _fail("model.directory", "native package files must be UTF-8")
                if data != _read_limited(path, remaining):
                    _fail("model.directory", "source changed while being packaged")
                entries.append({"path": path.relative_to(source).as_posix(), "content_utf8": text})
        payload = {"master_file": master_file, "files": sorted(entries, key=lambda x: x["path"])}
        _payload(payload)
        return payload

    def _manifest(self, master, files):
        entries = [{"path": name, "sha256": hashlib.sha256(files[name]).hexdigest(), "bytes": len(files[name])} for name in sorted(files)]
        fingerprint = hashlib.sha256(_canonical({"schema_version": "1.0", "master_file": master, "entries": entries})).hexdigest()
        snapshot_id = "model-" + fingerprint
        return {"schema_version": "1.0", "snapshot_id": snapshot_id, "format": "OpenDSS", "master_file": master,
                "entries": entries, "fingerprint": fingerprint, "inspection": _inspect(master, files)}

    def import_payload(self, payload):
        if isinstance(payload, Mapping) and payload.get("format") in {"JSON", "CSV"}:
            from .tabular_input import packaged_tables
            payload = packaged_tables(payload)
        if isinstance(payload, Mapping) and payload.get("format") == "MATPOWER":
            from .matpower_input import adapt_matpower_payload
            adapted = adapt_matpower_payload(payload)
            payload = {"master_file":adapted["native_payload"]["master_file"],
                "files":adapted["native_payload"]["files"] + [
                    {"path":"__tianshu_input__/request.json", "content_utf8":_canonical(payload).decode("utf-8")},
                    {"path":"__tianshu_input__/conversion.json", "content_utf8":_canonical(adapted["conversion_manifest"]).decode("utf-8")},
                    {"path":"__tianshu_input__/original/"+payload["source_file"], "content_utf8":payload["content_utf8"]}]}
        master, files = _payload(payload)
        _conversion_metadata(master, files)
        manifest = self._manifest(master, files)
        device_semantics = inspect_device_semantics(manifest["inspection"])
        _require_consistent_devices(device_semantics)
        manifest_bytes = _canonical(manifest)
        if len(manifest_bytes) > MAX_BYTES:
            _fail("model", "static package manifest exceeds the supported complexity budget", unsupported=True)
        target = self.root / manifest["snapshot_id"]
        if target.exists() or target.is_symlink():
            return self.inspect(manifest["snapshot_id"])
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.root))
        try:
            model = pending / "model"
            model.mkdir()
            for name, data in files.items():
                destination = model / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            with (pending / "manifest.json").open("xb") as stream:
                stream.write(manifest_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.rename(pending, target)
            except FileExistsError:
                pass
        finally:
            if pending.exists():
                if pending.parent.resolve() != self.root or not pending.name.startswith(".pending-") or _link(pending):
                    raise RuntimeError("unsafe temporary package cleanup target")
                shutil.rmtree(pending)
        return self.inspect(manifest["snapshot_id"])

    def inspect(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not _ID.fullmatch(snapshot_id):
            _fail("model.snapshot_id", "must be a content-addressed model identifier")
        target = self.root / snapshot_id
        if not target.exists() and not target.is_symlink():
            raise KeyError(snapshot_id)
        try:
            _ancestors_unlinked(target)
            if not target.is_dir() or {p.name for p in target.iterdir()} != {"manifest.json", "model"}:
                _fail("model.snapshot_id", "stored package layout changed", integrity=True)
            manifest_path = target / "manifest.json"
            if _link(manifest_path) or manifest_path.stat().st_size > MAX_BYTES:
                _fail("model.snapshot_id", "stored manifest is invalid", integrity=True)
            manifest_bytes = _read_limited(manifest_path, MAX_BYTES)
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            model = target / "model"
            payload = self.pack_directory(model, manifest["master_file"])
            master, files = _payload(payload)
            expected = self._manifest(master, files)
            if expected != manifest or expected["snapshot_id"] != snapshot_id or manifest_bytes != _canonical(expected):
                _fail("model.snapshot_id", "stored package integrity verification failed", integrity=True)
        except KeyError:
            _fail("model.snapshot_id", "stored manifest is incomplete", integrity=True)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, ContractError) and exc.code == "model_integrity_error":
                raise
            _fail("model.snapshot_id", "stored package integrity or policy verification failed", integrity=True)
        # Versioned interpretation is additive and derived. Never rewrite a
        # stored manifest or change the content ID when semantic coverage grows.
        converted = _conversion_metadata(master, files)
        from .topology import inspect_topology
        return {"snapshot_id": snapshot_id, "fingerprint": manifest["fingerprint"], "manifest": manifest,
                "inspection": manifest["inspection"],
                "conversion":converted, "source_format":converted.get("source_format", "MATPOWER") if converted is not None else "OpenDSS",
                "balanced_equivalent":converted is not None and converted.get("source_format", "MATPOWER") == "MATPOWER",
                "device_semantics": inspect_device_semantics(manifest["inspection"]),
                "topology":inspect_topology(manifest["inspection"], files)}

    def resolve(self, snapshot_id):
        result = self.inspect(snapshot_id)
        _require_consistent_devices(result["device_semantics"])
        return self.root / snapshot_id / "model", result["manifest"]

    def list_models(self):
        result = []
        for path in sorted(self.root.iterdir(), key=lambda p: p.name):
            if not _ID.fullmatch(path.name):
                continue
            try:
                inspected = self.inspect(path.name)
                manifest = inspected["manifest"]
                result.append({"snapshot_id": path.name, "format": "OpenDSS", "master_file": manifest["master_file"],
                               "source_format":inspected["source_format"], "balanced_equivalent":inspected["balanced_equivalent"],
                               "physical_phase_mapping":False if inspected["balanced_equivalent"] else None,
                               "fingerprint": manifest["fingerprint"], "status": "verified_input", "inspection": inspected["inspection"],
                               "device_semantics_status": inspected["device_semantics"]["status"]})
            except (ContractError, KeyError) as exc:
                unavailable=isinstance(exc,ContractError) and exc.code=="capability_unavailable"
                result.append({"snapshot_id": path.name, "status": "interpretation_unavailable" if unavailable else "integrity_error", "issues": [{"code": "capability_unavailable" if unavailable else "model_integrity_error", "message": "stored input interpretation is unavailable; bytes retained" if unavailable else "stored package failed integrity verification"}]})
        return result


def pack_directory(directory, master_file):
    """Package an explicitly selected local directory; never called by HTTP."""
    return ModelStore.pack_directory(directory, master_file)
