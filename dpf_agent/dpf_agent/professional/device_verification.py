"""Compare bounded explicit DSS declarations with read-only native COM facts.

Copyright (c) yangshaopeng. No I/O, engine access, default reconstruction or
engineering limits are used here. A passing result covers only the individually
listed reliable declarations; it is never engineering acceptance.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy


_VERSION = "1.3"
_MAX_OBJECTS = 20000
_MAX_CHECKS = 250000
_MAX_NODES = 1000000
_MAX_CHARS = 32 * 1024 * 1024
_ELEMENT = "OpenDSS.ActiveCircuit.ActiveCktElement."
_TRANSFORMER = "OpenDSS.ActiveCircuit.Transformers"
_COMMON_SOURCES = {
    "enabled": _ELEMENT + "Enabled",
    "num_phases": _ELEMENT + "NumPhases",
    "num_conductors": _ELEMENT + "NumConductors",
    "num_terminals": _ELEMENT + "NumTerminals",
    "bus_names": _ELEMENT + "BusNames",
    "node_order": _ELEMENT + "NodeOrder",
    "num_windings": _TRANSFORMER + ".NumWindings",
}
_CONNECTIONS = {"wye": "wye", "ln": "wye", "delta": "delta", "ll": "delta"}


def _require_four_conductor_basis(objects):
    """Reject incomplete or forged four-conductor opt-ins before comparison."""
    from .device_semantics import _four_matrix

    def matrices(fields):
        for key in ("rmatrix", "xmatrix", "cmatrix"):
            item = fields.get(key, {})
            value = item.get("value") if isinstance(item, Mapping) else None
            if (not isinstance(item, Mapping) or item.get("status") != "declared" or item.get("phase_assignment_after_matrix")
                    or not isinstance(value, Mapping) or not _four_matrix(value.get("raw", ""))):
                return False
        return True

    libraries = {}
    count = 0
    for row in objects:
        if any(issue.get("code") == "phase_count_outside_scope" for issue in row.get("issues", [])):
            raise ValueError("Four-conductor extension cannot authorize an unsupported device phase count")
        fields, kind = row.get("fields", {}), row.get("class")
        phase = fields.get("nphases" if kind == "linecode" else "phases", {})
        if phase.get("value") != 4:
            continue
        if phase.get("status") != "declared" or kind not in {"line", "linecode"}:
            raise ValueError("Four conductors require an explicit supported Line/LineCode declaration")
        if kind == "linecode":
            if not matrices(fields):
                raise ValueError("Four-conductor library requires complete explicit matrices")
            libraries[row["object_id"].casefold()] = fields
            count += 1
    for row in objects:
        fields = row.get("fields", {})
        if row.get("class") != "line" or fields.get("phases", {}).get("value") != 4:
            continue
        for key in ("bus1", "bus2"):
            item = fields.get(key, {})
            value = item.get("value")
            if (item.get("status") != "declared" or not isinstance(value, Mapping)
                    or value.get("nodes_explicit") is not True
                    or len(value.get("node_tokens", [])) != 4
                    or len(value.get("nodes", [])) != 4
                    or any(type(node) is not int or not 0 <= node <= 2147483647 for node in value["nodes"])
                    or len([node for node in value["nodes"] if node])
                       != len({node for node in value["nodes"] if node})):
                raise ValueError("Four-conductor line requires four explicit unambiguous nodes at both ends")
        if not matrices(fields):
            code = fields.get("linecode", {})
            if (code.get("status") != "declared" or not isinstance(code.get("value"), str)
                    or "linecode." + code["value"].casefold() not in libraries):
                raise ValueError("Four-conductor line requires complete explicit or copied matrices")
            for key in ("rmatrix", "xmatrix", "cmatrix"):
                if key in fields:
                    item = fields[key]
                    if (item.get("status") != "declared" or item.get("phase_assignment_after_matrix")
                            or not isinstance(item.get("value"), Mapping)
                            or not _four_matrix(item["value"].get("raw", ""))):
                        raise ValueError("Four-conductor matrix override is incomplete or resized")
        count += 1
    if not count:
        raise ValueError("Four-conductor version requires at least one explicit complete matrix declaration")


def _ordered_objects(declarations):
    """Apply only explicitly enumerated replay paths, including uncertainty."""
    objects = declarations.get("objects")
    if not isinstance(objects, list):
        raise ValueError("source objects are missing")
    replay = declarations.get("ordered_replay")
    replay_version = ("1.2" if declarations.get("version") == "1.4" else
                      "1.1" if declarations.get("version") == "1.3" else "1.0")
    if (not isinstance(replay, Mapping) or replay.get("version") != replay_version
            or replay.get("scope") != "bounded_ordered_native_properties"
            or replay.get("status") not in {"complete", "partial"}
            or not isinstance(replay.get("issues"), list)
            or any(not isinstance(issue, Mapping) or issue.get("severity") == "error" for issue in replay["issues"])
            or not isinstance(replay.get("objects"), list)):
        raise ValueError("ordered replay is missing, invalid or untrusted")
    originals, replayed = {}, {}
    for row in objects:
        if not isinstance(row, Mapping) or not isinstance(row.get("object_id"), str):
            raise ValueError("source identity is malformed")
        name = row["object_id"].casefold()
        if name in originals:
            raise ValueError("source identity is duplicated")
        originals[name] = row
    for row in replay["objects"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("object_id"), str):
            raise ValueError("replay identity is malformed")
        name = row["object_id"].casefold()
        if (name in replayed or name not in originals or row.get("class") != originals[name].get("class")
                or row.get("status") not in {"complete", "partial"}
                or not isinstance(row.get("issues"), list)
                or any(not isinstance(issue, Mapping) or issue.get("severity") == "error" for issue in row["issues"])):
            raise ValueError("replay object identity/status is untrusted")
        replayed[name] = row
    if set(originals) != set(replayed):
        raise ValueError("ordered replay object coverage is incomplete")
    output, paths_by_name = [], {}
    for name, original in originals.items():
        row, merged = replayed[name], deepcopy(original)
        kind = merged["class"]
        if (not isinstance(merged.get("fields"), Mapping) or not isinstance(merged.get("windings"), list)
                or any(not isinstance(winding, Mapping) or not isinstance(winding.get("fields"), Mapping)
                       for winding in merged["windings"])):
            raise ValueError("source field/winding shape is malformed")
        paths = row.get("replayed_fields")
        if (not isinstance(paths, list) or any(not isinstance(path, str) for path in paths)
                or len(paths) != len(set(paths)) or not isinstance(row.get("fields"), Mapping)
                or not isinstance(row.get("windings"), list)):
            raise ValueError("replay overlay paths are malformed")
        field_scope = ({"phases", "linecode", "bus1", "bus2"} if kind == "line" else
                       {"nphases"} if kind == "linecode" else
                       {"phases", "bus1", "bus2", "basekv"} if kind in {"circuit", "vsource"} else
                       {"phases", "windings"} if kind == "transformer" else
                       {"enabled"} if declarations.get("version") in {"1.3", "1.4"}
                       and kind in {"load", "generator", "pvsystem"} else set())
        if set(row["fields"]) != field_scope:
            raise ValueError("ordered field coverage differs from its supported class scope")
        required_paths = {"fields." + key for key, cell in row["fields"].items()
                          if not isinstance(cell, Mapping) or cell.get("status") != "not_declared"}
        if kind != "circuit":
            required_paths.update("fields." + key for key, cell in merged["fields"].items()
                                  if key in field_scope and isinstance(cell, Mapping)
                                  and cell.get("status") in {"declared", "uncertain"})
        if original.get("created_by_circuit") and row.get("origin") != "implicit_source_ordered_replay":
            raise ValueError("implicit source replay origin is missing")
        source_windings = {}
        for winding in row["windings"]:
            if not isinstance(winding, Mapping) or type(winding.get("winding")) is not int:
                raise ValueError("replay winding index is malformed")
            index = winding["winding"]
            if index not in {1, 2, 3} or index in source_windings or not isinstance(winding.get("fields"), Mapping):
                raise ValueError("replay winding identity is invalid")
            source_windings[index] = winding["fields"]
            required_paths.update(f"windings.{index}.{key}" for key, cell in winding["fields"].items()
                                  if not isinstance(cell, Mapping) or cell.get("status") != "not_declared")
        for winding in merged["windings"]:
            required_paths.update(f"windings.{winding.get('winding')}.{key}" for key, cell in winding["fields"].items()
                                  if isinstance(cell, Mapping) and cell.get("status") in {"declared", "uncertain"})
        if not required_paths.issubset(paths):
            raise ValueError("ordered overlay omits a declared or uncertain supported field")
        for path in paths:
            tokens = path.split(".")
            if len(tokens) == 2 and tokens[0] == "fields" and tokens[1] in field_scope:
                cell = row["fields"].get(tokens[1])
                target = merged["fields"]
            elif (len(tokens) == 3 and tokens[0] == "windings" and kind == "transformer"
                    and tokens[1] in {"1", "2", "3"} and tokens[2] in {"bus", "conn", "kv", "kva", "tap", "%r"}):
                index = int(tokens[1])
                cell = source_windings.get(index, {}).get(tokens[2])
                matches = [item for item in merged["windings"] if item.get("winding") == index]
                if len(matches) > 1:
                    raise ValueError("source winding is duplicated")
                if not matches:
                    matches = [{"winding":index,"fields":{}}]
                    merged["windings"].append(matches[0])
                target = matches[0]["fields"]
            else:
                raise ValueError("replay attempted to overlay an unsupported field")
            if (not isinstance(cell, Mapping) or cell.get("status") not in {"declared", "uncertain"}
                    or not isinstance(cell.get("source_refs"), list)
                    or (cell["status"] == "declared" and not cell["source_refs"])):
                raise ValueError("replay cell is invalid or lacks its explicit provenance")
            if any(not isinstance(ref, Mapping) or not isinstance(ref.get("source_file"), str) or not ref["source_file"]
                   or not isinstance(ref.get("property"), str) or not ref["property"] or type(ref.get("line")) is not int
                   or not 1 <= ref["line"] <= 100000 for ref in cell["source_refs"]):
                raise ValueError("replay provenance is malformed")
            if tokens[-1] == "enabled" and cell["status"] == "declared":
                if (type(cell.get("value")) is not bool
                        or any(ref["property"].casefold() != "enabled" for ref in cell["source_refs"])):
                    raise ValueError("declared enabled replay requires a boolean and explicit enabled-property provenance")
            target[tokens[-1]] = deepcopy(cell)
        if row.get("origin") == "implicit_source_ordered_replay":
            if kind != "vsource" or name != "vsource.source" or not original.get("created_by_circuit"):
                raise ValueError("implicit source replay identity is invalid")
            merged.pop("created_by_circuit", None)
        paths_by_name[name] = set(paths)
        output.append(merged)
    return output, paths_by_name


def _bounded(value):
    stack, nodes, chars = [(value, 0)], 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_NODES or depth > 20:
            raise ValueError("input exceeds comparison budget")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("object keys must be strings")
                chars += len(key)
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            chars += len(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("input contains a nonfinite number")
        elif isinstance(item, int) and not isinstance(item, bool) and item.bit_length() > 64:
            raise ValueError("input integer exceeds comparison budget")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise ValueError("comparison inputs must contain JSON values")
        if chars > _MAX_CHARS:
            raise ValueError("input text exceeds comparison budget")


def _number(value, *, integer=False, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    if integer and (not isinstance(value, int) or value > 2147483647):
        return None
    if (positive and value <= 0) or (nonnegative and value < 0):
        return None
    return value


def _connection(value):
    return _CONNECTIONS.get(value.casefold()) if isinstance(value, str) else None


def _bus(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    parts = value.split(".")
    if not parts[0] or any(ord(char) < 32 for char in value):
        return None
    nodes = []
    for token in parts[1:]:
        if len(token) > 10 or not re.fullmatch(r"[0-9]+", token):
            return None
        number = int(token)
        if number > 2147483647:
            return None
        nodes.append(number)
    return parts[0].casefold(), nodes


def _declared_bus(value):
    if not isinstance(value, Mapping):
        return None
    raw = _bus(value.get("raw"))
    name, nodes = value.get("bus_name"), value.get("nodes")
    if (raw is None or not isinstance(name, str) or name.casefold() != raw[0]
            or not isinstance(nodes, list) or nodes != raw[1]
            or any(_number(node, integer=True, nonnegative=True) is None for node in nodes)
            or value.get("nodes_explicit") is not bool(raw[1])):
        return None
    return raw


class _Verifier:
    def __init__(self):
        self.checks = []
        self.issues = []
        self.object_count = 0
        self.native_count = 0
        self.replay_paths = {}
        self.extended_state_voltage = False
        self.four_conductor_lines = False

    def add(self, object_id, field, status, *, expected=None, actual=None,
            reason=None, item=None, derived=False, source=None):
        if len(self.checks) >= _MAX_CHECKS:
            raise ValueError("check count exceeds comparison budget")
        check = {"object_id": object_id, "field": field, "status": status,
                 "expected": expected, "actual": actual,
                 "origin": "derived_from_explicit_connection" if derived else "source_declaration",
                 "source_refs": list(item.get("source_refs", [])) if isinstance(item, Mapping) else []}
        if reason:
            check["reason"] = reason
        if source:
            check["native_source"] = source
        self.checks.append(check)
        if status in {"mismatch", "not_available"}:
            self.issues.append({"code": "device_connection_" + status,
                                "object_id": object_id, "field": field,
                                "message": reason or "Explicit declaration does not match trusted native readback."})
        return check

    def skip(self, object_id, field, item, reason):
        return self.add(object_id, field, "not_checked", reason=reason, item=item)

    def cell(self, row, key, source):
        fields = row.get("fields") if isinstance(row, Mapping) else None
        cell = fields.get(key) if isinstance(fields, Mapping) else None
        if (not isinstance(cell, Mapping) or cell.get("status") != "available"
                or cell.get("source") != source or cell.get("issues") != []):
            return None
        return cell

    def compare(self, object_id, field, item, row, key, source, kind="number", *, derived=False):
        if not isinstance(item, Mapping) or item.get("status") != "declared":
            self.skip(object_id, field, item, "Declaration is absent or uncertain; native defaults are not reconstructed.")
            return None
        expected = item.get("value")
        normalizer = ((lambda value: value if type(value) is bool else None) if kind == "boolean" else
                      _connection if kind == "connection" else
                      lambda value: _number(value, integer=kind == "integer", positive=kind != "nonnegative",
                                            nonnegative=kind == "nonnegative"))
        normalized = normalizer(expected)
        if normalized is None:
            if kind == "boolean":
                self.add(object_id, field, "not_available", expected=expected, item=item,
                         reason="An explicit declared state must be a genuine boolean, not a string or numeric substitute.")
            else:
                self.skip(object_id, field, item, "Declared value is outside this comparison subset.")
            return None
        cell = self.cell(row, key, source)
        actual = normalizer(cell.get("value")) if cell is not None else None
        if actual is None:
            self.add(object_id, field, "not_available", expected=normalized,
                     reason="Required native value is absent, invalid or lacks its exact trusted getter source.",
                     item=item, derived=derived)
            return None
        equal = (normalized == actual if kind in {"integer", "connection", "boolean"}
                 else math.isclose(normalized, actual, rel_tol=1e-9, abs_tol=1e-8))
        self.add(object_id, field, "matched" if equal else "mismatch", expected=normalized,
                 actual=actual, item=item, derived=derived, source=source)
        return actual

    def structure(self, object_id, row, expected_terminals, *, linecode_resize=False, disabled=False):
        values = {}
        for key in ("num_conductors", "num_terminals"):
            cell = self.cell(row, key, _COMMON_SOURCES[key])
            value = _number(cell.get("value"), integer=True, positive=True) if cell else None
            if value is None or value > 1000:
                self.add(object_id, "runtime." + key, "not_available", derived=True,
                         reason="Trusted positive bounded native terminal/conductor dimensions are required.")
            values[key] = value
        terminals, conductors = values["num_terminals"], values["num_conductors"]
        if expected_terminals is not None and terminals is not None:
            self.add(object_id, "num_terminals", "matched" if terminals == expected_terminals else "mismatch",
                     expected=expected_terminals, actual=terminals, derived=True,
                     source=_COMMON_SOURCES["num_terminals"])
        buses_cell = self.cell(row, "bus_names", _COMMON_SOURCES["bus_names"])
        nodes_cell = self.cell(row, "node_order", _COMMON_SOURCES["node_order"])
        buses = buses_cell.get("value") if buses_cell else None
        nodes = nodes_cell.get("value") if nodes_cell else None
        valid_buses = (terminals is not None and isinstance(buses, list) and len(buses) == terminals
                       and all(_bus(bus) is not None for bus in buses))
        valid_nodes = (terminals is not None and conductors is not None and isinstance(nodes, list)
                       and len(nodes) == terminals * conductors
                       and all(_number(node, integer=True, nonnegative=True) is not None for node in nodes))
        if not valid_buses:
            self.add(object_id, "runtime.bus_names", "not_available", derived=True,
                     reason="Trusted native bus names must exactly cover the observed terminals.")
        if disabled:
            self.skip(object_id, "runtime.node_order", None,
                      "Native PCE is disabled; electrical conductor-node mapping is not assessed.")
            valid_nodes = False
        elif not valid_nodes:
            self.add(object_id, "runtime.node_order", "not_available", derived=True,
                     reason="Trusted native NodeOrder must exactly cover terminal by conductor dimensions.")
        if valid_buses and valid_nodes:
            for index, bus in enumerate(buses):
                explicit = _bus(bus)[1]
                actual = nodes[index * conductors:(index + 1) * conductors]
                if linecode_resize and len(explicit) > conductors:
                    self.skip(object_id, f"runtime.bus_names.{index + 1}.unbound_nodes", None,
                              "LineCode resize effects are not replayed; native bus suffix nodes beyond observed conductors are not mapped.")
                    explicit = explicit[:conductors]
                if len(explicit) > conductors or explicit != actual[:len(explicit)]:
                    self.add(object_id, f"runtime.bus_names.{index + 1}", "mismatch", derived=True,
                             reason="Native BusNames node tokens contradict native NodeOrder.")
        return (buses if valid_buses else None, nodes if valid_nodes else None, conductors)

    def bus(self, object_id, field, item, terminal, facts, row=None, *, linecode_resize=False, disabled=False):
        if not isinstance(item, Mapping) or item.get("status") != "declared":
            self.skip(object_id, field, item, "Bus declaration is absent or uncertain; no native bus is inferred.")
            return
        expected = _declared_bus(item.get("value"))
        if expected is None:
            self.add(object_id, field, "not_available", item=item,
                     reason="Declared native bus metadata is malformed.")
            return
        buses, nodes, conductors = facts
        if disabled:
            if buses is None or conductors is None or not 1 <= terminal <= len(buses):
                self.add(object_id, field, "not_available", expected=item.get("value"), item=item,
                         reason="Trusted disabled native terminal dimensions and BusNames are unavailable.")
                return
            actual_name, explicit_nodes = _bus(buses[terminal - 1])
            equal = (expected[0] == actual_name and len(expected[1]) <= conductors
                     and len(explicit_nodes) <= conductors
                     and expected[1] == explicit_nodes[:len(expected[1])])
            self.add(object_id, field, "matched" if equal else "mismatch",
                     expected={"bus_name": expected[0], "explicit_nodes": expected[1]},
                     actual={"bus_name": actual_name, "explicit_nodes": explicit_nodes}, item=item,
                     source=_COMMON_SOURCES["bus_names"],
                     reason="Only read-only BusNames text is compared for this explicitly disabled device.")
            self.skip(object_id, field + ".electrical_node_mapping", item,
                      "Disabled electrical NodeOrder is unavailable; no live node binding or omitted node default is inferred.")
            return
        if (buses is None or nodes is None or conductors is None
                or not 1 <= terminal <= len(buses)):
            self.add(object_id, field, "not_available", expected=item.get("value"), item=item,
                     reason="Required trusted native bus/NodeOrder dimensions are unavailable.")
            return
        actual_name, _ = _bus(buses[terminal - 1])
        actual_nodes = nodes[(terminal - 1) * conductors:terminal * conductors]
        if linecode_resize and len(expected[1]) > conductors:
            self.add(object_id, field, "matched" if expected[0] == actual_name else "mismatch",
                     expected={"bus_name": expected[0]}, actual={"bus_name": actual_name}, item=item,
                     reason="Only the bus base name is compared because LineCode conductor resize precedence is not reconstructed.",
                     source=_COMMON_SOURCES["bus_names"])
            self.skip(object_id, field + ".nodes", item,
                      "Explicit nodes exceed the observed LineCode-sized conductors; the source nodes are preserved without guessing truncation.")
            return
        equal = expected[0] == actual_name and len(expected[1]) <= conductors and expected[1] == actual_nodes[:len(expected[1])]
        self.add(object_id, field, "matched" if equal else "mismatch",
                 expected={"bus_name": expected[0], "explicit_nodes": expected[1]},
                 actual={"bus_name": actual_name, "native_nodes": actual_nodes}, item=item,
                 source=_COMMON_SOURCES["bus_names"] + " + " + _COMMON_SOURCES["node_order"])
        if len(expected[1]) < conductors:
            self.skip(object_id, field + ".undeclared_nodes", item,
                      "Only the explicit node prefix was compared; omitted native conductor nodes are not source declarations.")
        if row is not None:
            source = _ELEMENT + f"BusNames[{terminal - 1}]"
            cell = self.cell(row, "bus", source)
            actual_bus = _bus(cell.get("value")) if cell else None
            if actual_bus != _bus(buses[terminal - 1]):
                self.add(object_id, field + ".winding_readback", "not_available", item=item,
                         reason="Selected winding bus readback is missing, untrusted or inconsistent with the element terminal.")

    def voltage_rating(self, object_id, kind, item, row):
        """Use class-fixed voltage sources; never downgrade a typed getter."""
        if not isinstance(item, Mapping) or item.get("status") != "declared":
            self.skip(object_id, "kv", item, "No reliable explicit nominal voltage is declared.")
            return
        expected = _number(item.get("value"), positive=True)
        if expected is None:
            self.add(object_id, "kv", "not_available", item=item,
                     reason="Explicit nominal voltage must be a finite positive number.")
            return
        interface = {"load": "Loads", "generator": "Generators", "pvsystem": "PVSystems"}[kind]
        prefix = "OpenDSS.ActiveCircuit." + interface
        block = row.get("voltage_rating")
        valid = (isinstance(block, Mapping) and block.get("version") == "1.0"
                 and block.get("scope") == "read_only_native_nominal_voltage"
                 and isinstance(block.get("object_id"), str)
                 and block["object_id"].casefold() == object_id.casefold()
                 and block.get("class") == kind and block.get("identity_verified") is True
                 and block.get("identity_source") == prefix + ".Name"
                 and block.get("status") == "available" and block.get("issues") == []
                 and block.get("electrical_parameters_modified") is False
                 and isinstance(block.get("fields"), Mapping))
        source = _ELEMENT + "Properties(kv).Val" if kind == "pvsystem" else prefix + ".kV"
        cell = block["fields"].get("kv") if valid else None
        actual = (_number(cell.get("value"), positive=True) if isinstance(cell, Mapping)
                  and cell.get("status") == "available" and cell.get("source") == source
                  and cell.get("issues") == [] else None)
        if actual is None:
            self.add(object_id, "kv", "not_available", expected=expected, item=item, source=source,
                     reason="Required class-specific nominal voltage readback is missing, invalid or untrusted.")
            return
        if kind == "pvsystem":
            from .power_point_verification import quantized_consistency
            comparison = (quantized_consistency(expected, cell.get("raw_text"), actual)
                          if cell.get("precision") == {"kind": "native_significant_digits", "digits": 6} else None)
            if comparison is None:
                self.add(object_id, "kv", "not_available", expected=expected, item=item, source=source,
                         reason="PV nominal voltage requires valid native 6g text and its explicit precision limit.")
                return
            matched = comparison.pop("consistent")
            check = self.add(object_id, "kv", "matched" if matched else "mismatch",
                             expected=expected, actual=actual, item=item, source=source,
                             reason="The official PV COM interface exposes nominal voltage only as six-significant-digit text; sub-bin differences are not observable.")
            check.update(comparison)
        elif cell.get("precision") != {"kind": "native_binary_double"}:
            self.add(object_id, "kv", "not_available", expected=expected, item=item, source=source,
                     reason="Load/Generator nominal voltage requires native binary-double precision; no text fallback is permitted.")
        else:
            matched = math.isclose(expected, actual, rel_tol=1e-10, abs_tol=1e-12)
            check = self.add(object_id, "kv", "matched" if matched else "mismatch",
                             expected=expected, actual=actual, item=item, source=source)
            check.update(comparison_kind="native_double_parameter_equality", precision_limited=False,
                         native_double_readback=True, relative_tolerance=1e-10, absolute_tolerance=1e-12)

    def object(self, declaration, row):
        object_id, kind = declaration["object_id"], declaration["class"].casefold()
        fields, windings = declaration.get("fields", {}), declaration.get("windings", [])
        if not isinstance(fields, Mapping) or not isinstance(windings, list):
            raise ValueError("declaration fields/windings are malformed")
        if kind in {"circuit", "linecode", "xycurve"} or declaration.get("created_by_circuit"):
            reason = ("Circuit/implicit-source edit precedence is not reconstructed." if kind == "circuit" or declaration.get("created_by_circuit")
                      else "Library/nonconnection objects are outside runtime connection comparison.")
            for field, item in fields.items():
                self.skip(object_id, field, item, reason)
            if not fields:
                self.skip(object_id, "object", None, reason)
            return
        if kind not in {"line", "load", "generator", "pvsystem", "transformer", "vsource"}:
            for field, item in fields.items():
                self.skip(object_id, field, item, "Device class is outside runtime connection comparison.")
            if not fields:
                self.skip(object_id, "object", None, "Device class is outside runtime connection comparison.")
            return
        self.object_count += 1
        if row is None:
            self.add(object_id, "identity", "not_available", derived=True,
                     reason="Declared native device is absent from readback.")
            row = {}
        elif (row.get("identity_verified") is not True or row.get("identity_source") != _ELEMENT + "Name"
              or not isinstance(row.get("name"), str)
              or row["name"].casefold() != object_id.casefold()
              or not isinstance(row.get("requested_name"), str)
              or row["requested_name"].casefold() != object_id.casefold()
              or not isinstance(row.get("class"), str) or row["class"].casefold() != kind
              or row.get("status") not in {"available", "partial"}):
            self.add(object_id, "identity", "not_available", derived=True,
                     reason="Native active-device identity/class or collection status is untrusted.")
            row = {}
        phase = fields.get("phases", {})
        linecode_unsafe = kind == "line" and "linecode" in fields
        replay_paths = self.replay_paths.get(object_id.casefold(), set())
        if linecode_unsafe and "fields.phases" not in replay_paths:
            self.skip(object_id, "phases", phase, "LineCode copy/resize precedence is not fully replayed by source inspection.")
            declared_phases = None
        else:
            self.compare(object_id, "phases", phase, row, "num_phases", _COMMON_SOURCES["num_phases"], "integer")
            declared_phases = phase.get("value") if isinstance(phase, Mapping) and phase.get("status") == "declared" else None
            allowed_phases = {1, 2, 3, 4} if self.four_conductor_lines and kind == "line" else {1, 2, 3}
            if declared_phases not in allowed_phases or isinstance(declared_phases, bool):
                declared_phases = None
        count_item = fields.get("windings", {})
        count = count_item.get("value") if isinstance(count_item, Mapping) and count_item.get("status") == "declared" else None
        if kind == "transformer":
            self.compare(object_id, "windings", count_item, row, "num_windings", _COMMON_SOURCES["num_windings"], "integer")
            if count not in {2, 3} or isinstance(count, bool):
                count = None
        expected_terminals = count if kind == "transformer" else 2 if kind in {"line", "vsource"} else 1
        pce_extended = self.extended_state_voltage and kind in {"load", "generator", "pvsystem"}
        disabled = False
        if pce_extended:
            enabled = fields.get("enabled", {})
            actual_enabled = self.compare(object_id, "enabled", enabled, row, "enabled",
                                          _COMMON_SOURCES["enabled"], "boolean")
            native_fields = row.get("fields", {})
            enabled_cell = self.cell(row, "enabled", _COMMON_SOURCES["enabled"])
            trusted_state = enabled_cell is not None and type(enabled_cell.get("value")) is bool
            if not trusted_state:
                self.add(object_id, "runtime.enabled", "not_available", derived=True,
                         reason="A trusted native boolean state is required to distinguish live electrical mapping from a disabled device.")
            node_marker = native_fields.get("node_order") if isinstance(native_fields, Mapping) else None
            disabled = trusted_state and enabled_cell["value"] is False
            marker_valid = (isinstance(node_marker, Mapping)
                        and node_marker.get("status") == "not_applicable"
                        and node_marker.get("value") is None
                        and node_marker.get("source") == _COMMON_SOURCES["node_order"]
                        and node_marker.get("issues") == []
                        and node_marker.get("reason") == "disabled_power_conversion_element_no_node_order")
            if disabled:
                explicit_false = (isinstance(enabled, Mapping) and enabled.get("status") == "declared"
                                  and enabled.get("value") is False and actual_enabled is False)
                if not explicit_false:
                    self.add(object_id, "runtime.disabled_source_state", "not_available", derived=True,
                             reason="Disabled electrical mapping can be excluded only when the ordered source explicitly declares false and trusted native state agrees.")
                if not marker_valid:
                    self.add(object_id, "runtime.node_order_marker", "not_available", derived=True,
                             reason="Disabled native device requires the canonical null not-applicable NodeOrder marker; available electrical node arrays cannot substitute.")
        facts = self.structure(object_id, row, expected_terminals, linecode_resize=linecode_unsafe,
                               disabled=disabled)
        handled = {"phases", "windings"} if kind == "transformer" else {"phases"}
        if pce_extended:
            handled.add("enabled")
        expected_conductors = None
        if declared_phases is not None:
            if kind in {"line", "vsource"}:
                expected_conductors = declared_phases
            elif kind == "transformer":
                expected_conductors = declared_phases + 1
            else:
                connection = fields.get("conn", {})
                connection = _connection(connection.get("value")) if isinstance(connection, Mapping) and connection.get("status") == "declared" else None
                if connection:
                    expected_conductors = declared_phases + 1 if connection == "wye" else 2 if declared_phases == 1 else 3
        if expected_conductors is not None:
            self.compare(object_id, "num_conductors", {"status": "declared", "value": expected_conductors},
                         row, "num_conductors", _COMMON_SOURCES["num_conductors"], "integer", derived=True)
        else:
            self.skip(object_id, "num_conductors", None, "Explicit trustworthy phase/connection basis is unavailable.")
        if kind == "transformer":
            self.windings(object_id, windings, count, facts, row, replay_paths)
        else:
            for index, key in enumerate(("bus1", "bus2") if kind in {"line", "vsource"} else ("bus1",), 1):
                handled.add(key)
                self.bus(object_id, key, fields.get(key), index, facts,
                         linecode_resize=linecode_unsafe, disabled=disabled)
            if kind in {"load", "generator", "pvsystem"}:
                for key, value_kind in (("conn", "connection"), ("kv", "number")):
                    handled.add(key)
                    if key == "kv" and pce_extended:
                        self.voltage_rating(object_id, kind, fields.get(key), row)
                    else:
                        self.compare(object_id, key, fields.get(key), row, key, _ELEMENT + f"Properties({key}).Val", value_kind)
            elif kind == "vsource":
                handled.add("basekv")
                self.compare(object_id, "basekv", fields.get("basekv"), row, "kv", _ELEMENT + "Properties(basekv).Val")
        for field, item in fields.items():
            if field not in handled:
                self.skip(object_id, field, item,
                          "This rating, matrix, library-copy or coupled field is outside reliable runtime connection comparison.")

    def windings(self, object_id, windings, count, facts, row, replay_paths):
        native_windings = row.get("windings", [])
        if not isinstance(native_windings, list) or len(native_windings) > 1000:
            native_windings = []
        by_index = {}
        for native in native_windings:
            if not isinstance(native, Mapping):
                self.add(object_id, "runtime.windings", "not_available", derived=True,
                         reason="Native winding records are malformed.")
                continue
            index = native.get("index")
            if _number(index, integer=True, positive=True) is None or index in by_index:
                self.add(object_id, "runtime.windings", "not_available", derived=True,
                         reason="Native winding indices are invalid or duplicated.")
                continue
            by_index[index] = native
            if native.get("issues") != []:
                self.add(object_id, f"runtime.windings.{index}", "not_available", derived=True,
                         reason="Native winding reports an identity, selection or restoration error.")
        if count is not None and set(by_index) != set(range(1, count + 1)):
            self.add(object_id, "runtime.windings", "not_available", derived=True,
                     reason="Native winding records do not cover exactly the explicitly declared winding count.")
        source_indices, kva_values = set(), []
        for winding in windings:
            if not isinstance(winding, Mapping) or not isinstance(winding.get("fields"), Mapping):
                raise ValueError("declared winding shape is malformed")
            index = winding.get("winding")
            if _number(index, integer=True, positive=True) is None or index in source_indices:
                raise ValueError("declared winding index is invalid or repeated")
            source_indices.add(index)
            kva = winding["fields"].get("kva", {})
            if kva.get("status") == "declared" and _number(kva.get("value"), positive=True) is not None:
                kva_values.append(kva["value"])
        two_winding_kva_unsafe = count == 2 and len(set(kva_values)) > 1
        for winding in windings:
            index, fields = winding["winding"], winding["fields"]
            native = by_index.get(index, {})
            # The collector can mark the whole winding not_available when one
            # optional field failed; each identity-protected cell is decisive.
            for key, item in fields.items():
                field = f"windings.{index}.{key}"
                if count is None or index > count:
                    self.skip(object_id, field, item, "Explicit trustworthy winding-count basis is unavailable.")
                elif key == "bus":
                    self.bus(object_id, field, item, index, facts, native)
                elif key in {"conn", "kv", "kva", "tap", "%r"}:
                    if key == "kva" and two_winding_kva_unsafe and field not in replay_paths:
                        self.skip(object_id, field, item,
                                  "Two-winding kVA assignments can propagate to the other winding; differing source values are not replayed.")
                        continue
                    native_key, getter = {"conn": ("conn", "IsDelta"), "kv": ("kv", "kV"),
                                          "kva": ("kva", "kVA"), "tap": ("tap", "Tap"),
                                          "%r": ("percent_r", "R")}[key]
                    source = _TRANSFORMER + f"[Wdg={index}].{getter}"
                    self.compare(object_id, field, item, native, native_key, source,
                                 "connection" if key == "conn" else "nonnegative" if key == "%r" else "number")
                else:
                    self.skip(object_id, field, item, "Winding field is outside runtime connection comparison.")

    def run(self, declarations, native):
        _bounded(declarations)
        _bounded(native)
        if (not isinstance(declarations, Mapping) or declarations.get("version") not in {"1.0", "1.1", "1.2", "1.3", "1.4"}
                or declarations.get("scope") != "explicit_native_device_semantics"
                or declarations.get("status") not in {"complete", "partial"}
                or not isinstance(declarations.get("issues"), list)
                or any(not isinstance(issue, Mapping) or issue.get("severity") == "error"
                       for issue in declarations["issues"])):
            raise ValueError("static declarations are invalid or untrusted")
        if (not isinstance(native, Mapping) or native.get("schema_version") != "1.0"
                or native.get("scope") != "read_only_native_device_connections"
                or native.get("source") != "OpenDSS.COM"
                or native.get("status") not in {"available", "partial"}):
            raise ValueError("native readback is unavailable or untrusted")
        objects, observed = declarations.get("objects"), native.get("objects")
        self.extended_state_voltage = declarations["version"] in {"1.3", "1.4"}
        self.four_conductor_lines = declarations["version"] == "1.4"
        if declarations["version"] in {"1.1", "1.2", "1.3", "1.4"}:
            objects, self.replay_paths = _ordered_objects(declarations)
        if self.four_conductor_lines:
            _require_four_conductor_basis(objects)
        if (not isinstance(objects, list) or not isinstance(observed, list)
                or len(objects) > _MAX_OBJECTS or len(observed) > _MAX_OBJECTS):
            raise ValueError("object list is malformed or exceeds comparison budget")
        by_name, source_names = {}, set()
        self.native_count = len(observed)
        if native.get("issues") != []:
            self.add(None, "runtime.collection", "not_available", derived=True,
                     reason="Native collection reports an engine, enumeration, identity or selector-restoration error.")
        for row in observed:
            if not isinstance(row, Mapping) or not isinstance(row.get("requested_name"), str):
                raise ValueError("native identity is malformed")
            name = row["requested_name"].casefold()
            if not name or len(name) > 256 or name in by_name:
                raise ValueError("native identity is invalid or duplicated")
            by_name[name] = row
            if row.get("issues") != []:
                self.add(row["requested_name"], "runtime.collection", "not_available", derived=True,
                         reason="Native device reports an identity, selection or restoration error.")
            if (row.get("identity_verified") is not True or row.get("identity_source") != _ELEMENT + "Name"
                    or not isinstance(row.get("name"), str)
                    or row["name"].casefold() != name or "." not in name
                    or row.get("class") != name.split(".", 1)[0]):
                self.add(row["requested_name"], "runtime.identity", "not_available", derived=True,
                         reason="Native requested, active and class identities must agree exactly apart from case.")
        coverage = native.get("coverage")
        inventory = coverage.get("element_names") if isinstance(coverage, Mapping) else None
        if (not isinstance(coverage, Mapping) or coverage.get("enumeration_status") != "available"
                or coverage.get("enumeration_source") != "OpenDSS.ActiveCircuit.AllElementNames"
                or not isinstance(inventory, list) or len(inventory) > _MAX_OBJECTS
                or any(not isinstance(name, str) or "." not in name or not name.split(".", 1)[1]
                       or len(name) > 256 for name in inventory)
                or len({name.casefold() for name in inventory}) != len(inventory)):
            raise ValueError("native full-element inventory is unavailable, duplicated or untrusted")
        inventory_names = {name.casefold() for name in inventory}
        if not set(by_name).issubset(inventory_names):
            self.add(None, "runtime.inventory_rows", "not_available", derived=True,
                     reason="Native collected objects must belong to the exact observed circuit inventory.")
        expected_inventory = set()
        for declaration in objects:
            if (not isinstance(declaration, Mapping) or not isinstance(declaration.get("object_id"), str)
                    or not isinstance(declaration.get("class"), str)
                    or declaration.get("status") not in {"complete", "partial"}):
                raise ValueError("declaration identity/status is malformed")
            name = declaration["object_id"].casefold()
            if (not name or len(name) > 256 or name in source_names or "." not in name
                    or not name.split(".", 1)[1] or name.split(".", 1)[0] != declaration["class"].casefold()):
                raise ValueError("declaration identity is invalid or duplicated")
            source_names.add(name)
            kind = declaration["class"].casefold()
            if kind == "circuit":
                implicit = declaration.get("implicit_source_object_id")
                if not isinstance(implicit, str) or implicit.casefold() != "vsource.source":
                    raise ValueError("implicit circuit source identity is unavailable")
                expected_inventory.add(implicit.casefold())
            elif kind not in {"linecode", "xycurve"}:
                expected_inventory.add(name)
            self.object(declaration, by_name.get(name))
        if declarations["version"] in {"1.2", "1.3", "1.4"}:
            from .power_point_verification import compare_power_operating_points
            for check in compare_power_operating_points(declarations, by_name):
                added = self.add(check["object_id"], check["field"], check["status"],
                    expected=check.get("expected"), actual=check.get("actual"), reason=check.get("reason"),
                    item={"source_refs":check.get("source_refs",[])}, source=check.get("native_source"))
                added.update({key:value for key,value in check.items() if key not in added})
        self.add(None, "runtime.element_inventory", "matched" if inventory_names == expected_inventory else "mismatch",
                 expected=sorted(expected_inventory), actual=sorted(inventory_names), derived=True,
                 source="OpenDSS.ActiveCircuit.AllElementNames",
                 reason=None if inventory_names == expected_inventory else
                 "Native electrical inventory differs from the explicit package, including its implicit circuit source.")
        for name in sorted(set(by_name) - source_names):
            self.skip(by_name[name]["requested_name"], "object", None,
                      "Native object has no explicit source declaration in this comparison scope.")

    def result(self):
        counts = {status: sum(check["status"] == status for check in self.checks)
                  for status in ("matched", "mismatch", "not_available", "not_checked")}
        declared = sum(check["origin"] == "source_declaration" and check["status"] != "not_checked" for check in self.checks)
        passed = bool(declared and counts["matched"] and not counts["mismatch"] and not counts["not_available"] and not self.issues)
        status = "matched" if passed else "mismatch" if counts["mismatch"] else "not_available"
        if not declared:
            self.issues.append({"code": "no_reliable_explicit_comparison", "object_id": None,
                                "field": "coverage", "message": "No reliable explicit declaration was compared with native readback."})
        return {"version": "1.4" if self.four_conductor_lines else _VERSION, "passed": passed, "status": status,
                "scope": "reliable_explicit_native_device_connections_only",
                "engineering_accepted": False,
                "checks": self.checks,
                "coverage": {"status": "none" if not declared else "partial" if counts["not_checked"] else "complete",
                             "declared_object_count": self.object_count, "native_object_count": self.native_count,
                             "reliable_declared_count": declared,
                             "compared_count": counts["matched"] + counts["mismatch"],
                             "matched_count": counts["matched"], "mismatch_count": counts["mismatch"],
                             "not_available_count": counts["not_available"], "not_checked_count": counts["not_checked"],
                             "full_model_semantics_checked": False, "engineering_limits_checked": False},
                "issues": self.issues,
                "comparison_tolerance": {"relative": 1e-9, "absolute": 1e-8,
                                         "purpose": "connection_numeric_readback_equality_not_engineering_limits"},
                "power_parameter_comparison": {"double_relative":1e-10, "double_absolute":1e-12,
                    "generator_base_pq":"native_6_significant_digit_consistency_only",
                    "pv_requested_kvar_and_reactive_mode":"not_checked",
                    "solved_terminal_power_is_not_parameter_readback":True},
                "extended_pce_comparison": {"enabled": self.extended_state_voltage,
                    "load_generator_nominal_kv": "native_double_parameter_equality" if self.extended_state_voltage else "legacy_generic_property_equality",
                    "pv_nominal_kv": "native_6_significant_digit_consistency" if self.extended_state_voltage else "legacy_generic_property_equality",
                    "disabled_node_mapping": "not_checked_only_when_explicit_false_and_trusted_native_false" if self.extended_state_voltage else "required_native_node_order"}}


def verify_device_connections(declarations, native_readback) -> dict:
    """Return deterministic JSON checks without mutating either JSON input.

    Missing/uncertain source declarations remain ``not_checked``. A reliable
    declaration with missing, invalid, ambiguous or untrusted native facts blocks
    publication. Numerical equality tolerances are not engineering thresholds.
    """
    verifier = _Verifier()
    try:
        verifier.run(declarations, native_readback)
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        verifier.add(None, "input", "not_available", derived=True,
                     reason="Declarations/readback are malformed, ambiguous, untrusted or outside the bounded comparison budget.")
    report = verifier.result()
    if isinstance(declarations, Mapping) and "snapshot_state" in declarations:
        from .source_state import verify_source_state
        state = verify_source_state(declarations["snapshot_state"],
            native_readback.get("snapshot_state") if isinstance(native_readback,Mapping) else None,
            native_readback.get("snapshot_state_baseline") if isinstance(native_readback,Mapping) else None)
        report["snapshot_state"] = state
        if not state["passed"]:
            report.update(passed=False,status="not_available")
            report["issues"].append({"code":"snapshot_state_not_verified", "message":"Explicit or frozen electrical operating state was not verified."})
    if isinstance(declarations,Mapping) and declarations.get("pv_fixed_points_required") is True:
        from .pv_points import verify_pv_points
        native=native_readback if isinstance(native_readback,Mapping) else {}
        pv=verify_pv_points(declarations,native.get("pv_controls"),native.get("pv_controls_baseline"))
        devices={r.get("name","").casefold():r for r in native.get("objects",[]) if isinstance(r,Mapping)}
        for row in native.get("pv_controls",{}).get("objects",[]) if isinstance(native.get("pv_controls"),Mapping) else []:
            actual=devices.get(row.get("object_id"),{}).get("fields",{}).get("enabled",{}).get("value")
            if actual is not row.get("enabled") or type(actual) is not bool:
                pv["passed"]=False
                pv["issues"].append({"code":"pv_control_and_native_enabled_disagree"})
        report["pv_fixed_points"]=pv
        if not pv["passed"]:
            report.update(passed=False,status="not_available")
            report["issues"].append({"code":"pv_fixed_point_not_verified", "message":"Native PV control mode or attainable specified point was not verified."})
    if isinstance(declarations,Mapping) and declarations.get("passive_stamp_required") is True:
        from .passive_stamp import verify_passive_stamp
        native=native_readback if isinstance(native_readback,Mapping) else {}
        stamp=verify_passive_stamp(native.get("passive_stamp"),native.get("passive_stamp_baseline"))
        report["passive_stamp"]=stamp
        if not stamp["passed"]:
            report.update(passed=False,status="not_available")
            report["issues"].append({"code":"passive_stamp_not_verified", "message":"The compiled passive electrical admittances are unavailable or have changed."})
    return report
