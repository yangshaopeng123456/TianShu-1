"""Complete native passive-PD terminal power, independent of Circuit.Losses.

Copyright (c) yangshaopeng. Pure projections only; no solver, defaults, or edits.
Circuit.Losses excludes shunt PD elements. Summing every native passive element
once avoids guessing IsShunt and counting mapped Gs/Bs admittances twice.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math

VERSION = "1.0"
SCOPE = "all_native_line_transformer_capacitor_reactor_signed_terminal_power"
KINDS = frozenset({"line", "transformer", "capacitor", "reactor"})
_SOURCES = {"bus_names": "AllBusNames", "bus_count": "NumBuses", "element_names": "AllElementNames",
            "line_count": "Lines.Count", "transformer_count": "Transformers.Count"}


def _number(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError("finite_native_passive_power_required")
    return float(value)


def _inventory(frames):
    previous = None
    for key in ("before", "after"):
        frame = frames.get(key)
        if (not isinstance(frame, Mapping) or frame.get("version") != "1.0"
                or frame.get("scope") != "native_bus_line_transformer_inventory"
                or frame.get("status") != "available" or frame.get("issues") != []):
            raise ValueError("complete_native_inventory_required")
        fields = frame.get("fields", {})
        if set(fields) != set(_SOURCES):
            raise ValueError("native_inventory_fields_required")
        values = {}
        for name, getter in _SOURCES.items():
            cell = fields[name]
            if (not isinstance(cell, Mapping) or cell.get("source") != "OpenDSS.ActiveCircuit." + getter
                    or cell.get("status") != "available" or cell.get("issues") != []):
                raise ValueError("native_inventory_source_unverified")
            values[name] = cell.get("value")
        for name in ("bus_names", "element_names"):
            names = values[name]
            if (not isinstance(names, list) or len(names) > 100000 or any(not isinstance(n, str) or not n for n in names)
                    or len(set(n.casefold() for n in names)) != len(names)):
                raise ValueError("native_inventory_identity_unverified")
            values[name] = sorted(n.casefold() for n in names)
        if (not values["element_names"] or any("." not in n for n in values["element_names"])
                or any(type(values[name]) is not int or values[name] < 0 for name in ("bus_count", "line_count", "transformer_count"))
                or values["bus_count"] != len(values["bus_names"])
                or values["line_count"] != sum(n.startswith("line.") for n in values["element_names"])
                or values["transformer_count"] != sum(n.startswith("transformer.") for n in values["element_names"])):
            raise ValueError("native_inventory_counts_unverified")
        if previous is not None and values != previous:
            raise ValueError("native_inventory_changed_during_collection")
        previous = values
    return previous


def _disabled_identity(data, name):
    """A verified disabled device is excluded, never a fabricated zero reading."""
    return (isinstance(data, Mapping) and data.get("schema_version") == "2.0"
        and data.get("status") == "disabled" and data.get("enabled") is False
        and data.get("identity_verified") is True and data.get("errors") == []
        and data.get("source") == "OpenDSS.ActiveCktElement"
        and isinstance(data.get("requested_element_name"), str) and data["requested_element_name"].casefold() == name
        and isinstance(data.get("active_element_name"), str) and data["active_element_name"].casefold() == name
        and type(data.get("num_terminals")) is int and 1 <= data["num_terminals"] <= 32
        and type(data.get("num_conductors")) is int and 1 <= data["num_conductors"] <= 64
        and type(data.get("num_phases")) is int and 1 <= data["num_phases"] <= data["num_conductors"]
        and isinstance(data.get("bus_names"), list) and len(data["bus_names"]) == data["num_terminals"]
        and all(isinstance(bus, str) and bus for bus in data["bus_names"]))


def validate_passive_power_boundary(boundary):
    """Recheck the complete inventory and re-sum every certified port in verifier."""
    if (not isinstance(boundary, Mapping) or boundary.get("version") != VERSION or boundary.get("scope") != SCOPE
            or boundary.get("status") != "verified" or boundary.get("issues") != []
            or boundary.get("includes_mapped_shunts") is not True
            or boundary.get("circuit_losses_modified") is not False):
        raise ValueError("passive_power_boundary_not_verified")
    # Rebuild from the frozen raw-v2 subset, not a caller-supplied "verified"
    # flag or aggregate. This repeats identity, bus mapping and S=VI* checks.
    proof = boundary.get("native_raw")
    if not isinstance(proof, Mapping):
        raise ValueError("passive_native_port_evidence_missing")
    rebuilt = build_passive_power_boundary(proof, _recheck=False)
    if rebuilt["status"] != "verified" or rebuilt["objects"] != boundary.get("objects"):
        raise ValueError("passive_native_port_evidence_mismatch")
    inventory = _inventory(boundary.get("native_inventory", {}))
    if inventory != _inventory(proof.get("solver", {}).get("domain_inventory", {})):
        raise ValueError("passive_native_inventory_evidence_mismatch")
    expected = {n for n in inventory["element_names"] if n.split(".", 1)[0] in KINDS}
    objects = boundary.get("objects")
    if not isinstance(objects, list):
        raise ValueError("passive_objects_missing")
    seen, powers, reactive = set(), [], []
    for obj in objects:
        name = obj.get("object_id")
        if (not isinstance(name, str) or name in seen or name not in expected or obj.get("class") != name.split(".", 1)[0]
                or type(obj.get("enabled")) is not bool or obj.get("metadata_verified") is not True):
            raise ValueError("passive_object_identity_unverified")
        seen.add(name)
        rows = obj.get("terminal_conductors")
        if not isinstance(rows, list):
            raise ValueError("passive_port_records_missing")
        if obj["enabled"] is False:
            if obj.get("status") != "disabled" or rows or not _disabled_identity(obj.get("disabled_native_identity"), name):
                raise ValueError("disabled_passive_identity_unverified")
            continue
        if obj.get("status") != "available":
            raise ValueError("active_passive_status_unverified")
        nt, nc = obj.get("num_terminals"), obj.get("num_conductors")
        if type(nt) is not int or not 1 <= nt <= 32 or type(nc) is not int or not 1 <= nc <= 64:
            raise ValueError("passive_port_dimensions_invalid")
        ports = set()
        for row in rows:
            port = row.get("terminal"), row.get("conductor")
            if (type(port[0]) is not int or type(port[1]) is not int or not 1 <= port[0] <= nt or not 1 <= port[1] <= nc
                    or port in ports or row.get("metadata_verified") is not True or row.get("numerically_valid") is not True
                    or row.get("power_sign") != "into_element" or not isinstance(row.get("evidence_ref"), str)
                    or not row["evidence_ref"].startswith("raw_opendss_result.json#/system/passive_power_boundary/native_raw/")):
                raise ValueError("passive_port_identity_unverified")
            ports.add(port)
            powers.append(_number(row.get("powers_kw"))); reactive.append(_number(row.get("powers_kvar")))
        if len(ports) != nt * nc:
            raise ValueError("passive_terminal_coverage_incomplete")
    if seen != expected:
        raise ValueError("passive_object_inventory_incomplete")
    p, q = _number(math.fsum(powers)), _number(math.fsum(reactive))
    if not math.isclose(p, _number(boundary.get("total_kw")), rel_tol=1e-12, abs_tol=1e-10) or not math.isclose(
            q, _number(boundary.get("total_kvar")), rel_tol=1e-12, abs_tol=1e-10):
        raise ValueError("passive_power_totals_mismatch")
    return p, q


def build_passive_power_boundary(raw, native_records=None, *, _recheck=True):
    """Validate raw-v2 metadata and S=VI*, preserving original raw and losses."""
    base = {"version": VERSION, "scope": SCOPE, "status": "not_verified", "issues": [],
            "includes_mapped_shunts": True, "circuit_losses_modified": False,
            "total_kw": None, "total_kvar": None, "objects": []}
    try:
        frames = raw.get("solver", {}).get("domain_inventory", {})
        inventory = _inventory(frames)
        if raw.get("solver", {}).get("converged") is not True:
            raise ValueError("converged_raw_required")
        aux = raw.get("auxiliary_elements")
        if not isinstance(aux, list):
            raise ValueError("auxiliary_inventory_missing")
        auxiliary_expected = {n for n in inventory["element_names"] if n.split(".", 1)[0] in {"vsource", "capacitor", "reactor"}}
        names = [row.get("name", "").casefold() for row in aux]
        if len(names) != len(set(names)) or set(names) != auxiliary_expected:
            raise ValueError("auxiliary_inventory_incomplete")
        records = native_records if native_records is not None else [*raw.get("lines", []), *raw.get("transformers", []),
            *(row for row in aux if row.get("class") in {"capacitor", "reactor"})]
        expected = {n for n in inventory["element_names"] if n.split(".", 1)[0] in KINDS}
        record_names = [row.get("name", "").casefold() for row in records]
        if len(record_names) != len(set(record_names)) or set(record_names) != expected:
            raise ValueError("complete_native_passive_records_required")
        # Supplemental SYSTEM-only reads and full display scopes traverse the
        # same inventory in different orders. Canonicalize by native identity.
        records = sorted(records, key=lambda row: row["name"].casefold())
        from ..professional.observations import _Builder
        projected = {"buses": raw.get("buses", []), "lines": [], "transformers": [], "auxiliary_elements": []}
        for record in records:
            kind = record["name"].split(".", 1)[0].casefold()
            projected["lines" if kind == "line" else "transformers" if kind == "transformer" else "auxiliary_elements"].append(record)
        builder = _Builder(projected, "passive-power-boundary", "raw_opendss_result.json", "converged_valid")
        builder.buses()
        for collection in ("lines", "transformers", "auxiliary_elements"):
            active = [row for row in projected[collection] if row.get("terminal_data", {}).get("enabled") is not False]
            projected[collection] = active
            builder.elements(collection)
        if any(issue.get("severity") == "error" for issue in builder.issues):
            raise ValueError("passive_terminal_or_bus_metadata_unverified")
        ports = {}
        for row in builder.terminals:
            ports.setdefault(row["object_id"].casefold(), []).append(row)
        objects = []
        for record in records:
            name = record["name"].casefold(); data = record.get("terminal_data", {})
            obj = {"object_id": name, "class": name.split(".", 1)[0], "enabled": data.get("enabled"),
                "num_terminals": data.get("num_terminals"), "num_conductors": data.get("num_conductors"),
                "metadata_verified": True, "status": data.get("status"), "terminal_conductors": []}
            if obj["enabled"] is False:
                if not _disabled_identity(data, name):
                    raise ValueError("disabled_passive_identity_unverified")
                obj["disabled_native_identity"] = deepcopy(dict(data))
            else:
                obj["terminal_conductors"] = [{key: row.get(key) for key in ("terminal", "conductor", "node", "powers_kw", "powers_kvar",
                    "metadata_verified", "numerically_valid", "power_sign", "evidence_ref")} for row in ports.get(name, [])]
            objects.append(obj)
        proof = {"solver":{"converged":True, "domain_inventory":deepcopy(frames)},
            "buses":deepcopy(raw.get("buses", [])), "auxiliary_elements":deepcopy(aux),
            "lines":[{"name":row["name"], "class":"line", "terminal_data":deepcopy(row.get("terminal_data"))}
                     for row in records if row["name"].split(".",1)[0].casefold() == "line"],
            "transformers":[{"name":row["name"], "class":"transformer", "terminal_data":deepcopy(row.get("terminal_data"))}
                            for row in records if row["name"].split(".",1)[0].casefold() == "transformer"]}
        # The projection used for numerical checks is not the display raw: a
        # SYSTEM-only request omits branches, and fidelity mapping may fold them.
        # Point into this immutable pre-mapping proof, with the native object's
        # terminal/conductor giving its exact P/Q vector index.
        for collection in ("lines", "transformers", "auxiliary_elements"):
            indices = {row["name"].casefold(): index for index, row in enumerate(proof[collection])}
            for obj in objects:
                if obj["object_id"] in indices:
                    for row in obj["terminal_conductors"]:
                        row["evidence_ref"] = ("raw_opendss_result.json#/system/passive_power_boundary/native_raw/"
                            + collection + "/" + str(indices[obj["object_id"]]) + "/terminal_data")
        base.update(status="verified", native_inventory=deepcopy(frames), objects=objects, native_raw=proof)
        base["total_kw"] = _number(math.fsum(row["powers_kw"] for obj in objects for row in obj["terminal_conductors"]))
        base["total_kvar"] = _number(math.fsum(row["powers_kvar"] for obj in objects for row in obj["terminal_conductors"]))
        if _recheck:
            validate_passive_power_boundary(base)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        base.update(status="not_verified", total_kw=None, total_kvar=None,
                    issues=[{"code": "complete_native_passive_power_not_verified"}])
    return base
