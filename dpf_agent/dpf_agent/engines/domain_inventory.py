"""Independent complete COM inventories for requested observation domains.

Copyright (c) yangshaopeng. Inventory is read-only and includes disabled devices.
"""
from __future__ import annotations

from .device_connections import _Reader, _integer, _array, _text


def collect_domain_inventory(engine):
    reader = _Reader(engine)
    result = {"version":"1.0", "scope":"native_bus_line_transformer_inventory",
              "status":"not_available", "fields":{}, "issues":[]}
    c = engine.ActiveCircuit
    for key, source, getter, convert in (
        ("bus_names", "AllBusNames", lambda:c.AllBusNames, lambda v:_array(v,_text)),
        ("bus_count", "NumBuses", lambda:c.NumBuses, _integer),
        ("element_names", "AllElementNames", lambda:c.AllElementNames, lambda v:_array(v,_text)),
        ("line_count", "Lines.Count", lambda:c.Lines.Count, _integer),
        ("transformer_count", "Transformers.Count", lambda:c.Transformers.Count, _integer),
    ):
        result["fields"][key] = reader.read("OpenDSS.ActiveCircuit."+source, getter, convert)
    if all(cell["status"]=="available" for cell in result["fields"].values()):
        result["status"]="available"
    return result
