"""Project-confirmed engineering checks derived from verified native facts.

Copyright (c) yangshaopeng. No engine, filesystem, defaults, standards, or model
calls are used here. Confirmation metadata is a caller declaration, not an
identity certification or recognition of actual engineering reference data.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import cmath
import math
import re

from .assessment import evaluate_assessment
from .contracts import Measurement
from .project_criteria import ProjectCriteria

ENGINEERING_VERSION = "1.0"
PASSIVE = frozenset({"line", "transformer", "capacitor", "reactor"})


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _finite(value):
    value = _number(value)
    if value is None:
        raise ValueError("derived_quantity_not_finite")
    return value


def _key(row):
    return (str(row.get("object_id", "")).casefold(), row.get("terminal"), row.get("conductor"))


def _row_valid(row, *, terminal=False, explicit_node=False):
    # Nonstandard node IDs retain an unassigned physical phase in the native
    # observation projection. A project rule selecting exact node identities
    # can still evaluate their verified numerical potential; this never infers
    # a phase or treats an arbitrary node as a neutral. Default/legacy metric
    # projections keep their original standard-node scope.
    identified_potential = (explicit_node and type(row.get("node")) is int
        and row["node"] > 3 and row.get("phase") is None
        and row.get("status") == "not_assessed"
        and _number(row.get("voltage_mag_v")) is not None
        and _number(row.get("voltage_angle_deg")) is not None)
    return (row.get("metadata_verified") is True
            and (row.get("numerically_valid") is True if terminal else row.get("status") == "available" or identified_potential))


def _trusted(result, package):
    source = package.get("provenance", {}).get("source_file", {})
    native = result.get("input", {}).get("snapshot_id") is not None
    device = result.get("device_connections_verification", {})
    return bool(result.get("publishable") is True
        and result.get("execution_status") == "finished"
        and result.get("numerical_validity") == package.get("numerical_validity") == "converged_valid"
        and result.get("task_id") == package.get("task_id")
        and isinstance(result.get("task_id"), str)
        and result.get("verification", {}).get("passed") is True
        and result.get("verification", {}).get("task_id") == result.get("task_id")
        and result.get("domain_observations_verification", {}).get("passed") is True
        and source.get("available") is True
        and source.get("artifact") == "raw_opendss_result.json"
        and isinstance(source.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
        and package.get("provenance", {}).get("input_fingerprint") == result.get("input", {}).get("fingerprint")
        and isinstance(result.get("input", {}).get("fingerprint"), str)
        and re.fullmatch(r"[a-f0-9]{64}", result["input"]["fingerprint"])
        and (not native or (device.get("passed") is True and device.get("publication_authorized") is True)))


def _metric(metric, value, unit, basis, rows, **where):
    return {"metric": metric, "value": _finite(value), "unit": unit, "measurement_basis": basis,
        "status": "available", "reason": "derived_from_verified_native_observations",
        "evidence_refs": sorted({r["evidence_ref"] for r in rows if isinstance(r.get("evidence_ref"), str)}), **where}


def analyze_engineering(result, criteria):
    """Return (analysis, assessment) without mutating result or running a solver.

    Only the terminal service writer calls this with facts rebuilt from its own
    raw bytes. A public request cannot supply these facts or an assessment.
    """
    package = result.get("professional_observations", {})
    trusted = _trusted(result, package)
    full = trusted and package.get("coverage", {}).get("all_network_coverage") is True
    balanced = package.get("provenance", {}).get("physical_phase_mapping") is False
    buses, terminals = package.get("bus_nodes", []), package.get("element_terminals", [])
    if not isinstance(buses, (list, tuple)) or not isinstance(terminals, (list, tuple)):
        buses, terminals, trusted, full = [], [], False, False
    buses = [r for r in buses if isinstance(r, dict)]
    terminals = [r for r in terminals if isinstance(r, dict)]
    bus_index, port_index, objects = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in buses:
        bus_index[(str(row.get("object_id", "")).casefold(), row.get("node"))].append(row)
    for row in terminals:
        port_index[_key(row)].append(row)
        objects[str(row.get("object_id", "")).casefold()].append(row)
    if full:
        coverage = package.get("coverage", {})
        domains = coverage.get("requested_domains", {}).get("domains", {})
        actual = {
            "buses":len({name for name, node in bus_index}),
            "lines":len({name for name, rows in objects.items() if rows[0].get("element_class") == "line"}),
            "transformers":len({name for name, rows in objects.items() if rows[0].get("element_class") == "transformer"}),
            "auxiliary_elements":len({name for name, rows in objects.items() if rows[0].get("element_class") in {"vsource", "capacitor", "reactor"}}),
        }
        full = all(type(domains.get(k, {}).get("expected_count")) is int
                   and domains[k]["expected_count"] == count for k, count in actual.items())
        full = full and (coverage.get("bus_node_records") == len(buses)
            and coverage.get("terminal_conductor_records") == sum(type(r.get("terminal")) is int and type(r.get("conductor")) is int for r in terminals)
            and coverage.get("power_conversion_elements", {}).get("expected_count") == len({
                name for name, rows in objects.items() if rows[0].get("element_class") in {"load", "generator", "pvsystem"}}))
    metrics, issues = [], []
    if not trusted:
        issues.append("validated_task_bound_observations_not_available")
    if not full:
        issues.append("complete_network_observation_coverage_not_available")
    if balanced:
        issues.append("balanced_equivalent_has_no_verified_physical_phase_mapping")

    # Inventory includes every retained bus node and every device conductor.
    # A nonexistent target cannot become N/A, nor can a sparse rule list imply
    # complete engineering coverage. Node zero is excluded only for bus voltage.
    expected = set()
    for (name, node), rows in bus_index.items():
        if isinstance(node, int) and node != 0:
            expected.add(("voltage", name, node))
    for (name, terminal, conductor), rows in port_index.items():
        kind = rows[0].get("element_class")
        if kind in {"line", "transformer"}:
            expected.add((kind + "_current", name, terminal, conductor))
    expected.add(("active_loss", "network"))
    measured = {}
    if trusted:
        for (name, node), rows in bus_index.items():
            row = rows[0]
            value = _number(row.get("voltage_mag_v"))
            if len(rows) == 1 and _row_valid(row) and value is not None and not balanced:
                metrics.append(_metric("voltage_magnitude_v", value, "V", "node_to_ground", rows,
                    object_id=row["object_id"], node=node, phase=row.get("phase")))
                pu = _number(row.get("voltage_pu"))
                base = _number(row.get("voltage_base_ln_kv"))
                if pu is not None and base is not None and base > 0 and math.isclose(value / (base * 1000), pu, rel_tol=1e-8, abs_tol=1e-8):
                    metrics.append(_metric("native_voltage_pu", pu, "pu", "node_to_ground_native_ln_base", rows,
                        object_id=row["object_id"], node=node, phase=row.get("phase"), base_voltage_v=base * 1000,
                        base_source="OpenDSS.ActiveBus.kVBase; numeric normalization, not confirmed engineering limit"))
        for (name, terminal, conductor), rows in port_index.items():
            row = rows[0]
            value = _number(row.get("currents_mag_a"))
            if len(rows) == 1 and _row_valid(row, terminal=True) and value is not None and not balanced:
                metrics.append(_metric("terminal_conductor_current_a", value, "A", "terminal_conductor_rms", rows,
                    object_id=row["object_id"], terminal=terminal, conductor=conductor, node=row.get("node")))
    # Sum signed complex power at every terminal, including neutral/ground
    # conductors. Disabled summary records do not supply invented zero readings.
    totals = defaultdict(complex)
    totals_rows = defaultdict(list)
    losses_valid = full and all(len(rows) == 1 for rows in port_index.values())
    for name, rows in objects.items():
        if all(r.get("enabled") is False for r in rows):
            continue
        valid_rows = all(_row_valid(r, terminal=True) and r.get("power_sign") == "into_element"
            and _number(r.get("powers_kw")) is not None and _number(r.get("powers_kvar")) is not None for r in rows)
        classes = {r.get("element_class") for r in rows}
        if len(classes) != 1 or not valid_rows:
            losses_valid = False
            continue
        kind = next(iter(classes))
        try:
            value = complex(math.fsum(r["powers_kw"] for r in rows), math.fsum(r["powers_kvar"] for r in rows))
            _finite(value.real); _finite(value.imag)
            totals[kind] += value
            totals_rows[kind].extend(rows)
            if kind in PASSIVE and full:
                metrics.append(_metric("element_active_loss_kw", value.real, "kW", "sum_signed_terminal_power_into_element", rows,
                    object_id=rows[0]["object_id"]))
                metrics.append(_metric("element_net_reactive_absorption_kvar", value.imag, "kvar", "sum_signed_terminal_power_into_element", rows,
                    object_id=rows[0]["object_id"]))
            if trusted and kind == "transformer":
                by_terminal = defaultdict(list)
                for r in rows:
                    by_terminal[r["terminal"]].append(r)
                for terminal, ports in by_terminal.items():
                    s = complex(math.fsum(r["powers_kw"] for r in ports), math.fsum(r["powers_kvar"] for r in ports))
                    metric = _metric("transformer_terminal_kva", abs(s), "kVA", "terminal_complex_power_magnitude", ports,
                        object_id=ports[0]["object_id"], terminal=terminal,
                        formula="abs(sum(P_kW) + j*sum(Q_kvar)); not sum(abs(S_conductor))",
                        inputs=[{"terminal":r["terminal"],"conductor":r["conductor"],"p_kw":r["powers_kw"],"q_kvar":r["powers_kvar"]} for r in ports])
                    metrics.append(metric)
                    measured[("transformer_kva", name, terminal)] = metric
        except (ValueError, OverflowError):
            losses_valid = False
    if losses_valid:
        try:
            passive = sum((totals[k] for k in PASSIVE), 0j)
            passive_rows = [r for k in PASSIVE for r in totals_rows[k]]
            loss_metric = _metric("network_active_loss_kw", passive.real, "kW", "passive_network_active_loss", passive_rows,
                object_id="network", included_element_classes=sorted(PASSIVE),
                excluded_element_classes=["vsource", "load", "generator", "pvsystem"],
                formula="sum signed P at every terminal and conductor of Line/Transformer/Capacitor/Reactor")
            metrics.append(loss_metric)
            measured[("active_loss", "network")] = loss_metric
            metrics.append(_metric("network_net_reactive_absorption_kvar", passive.imag, "kvar", "passive_network_net_reactive_exchange", passive_rows,
                object_id="network", formula="signed net Q absorption, including capacitor compensation; not a thermal loss"))
            for group, classes in (("line_transformer", ("line", "transformer")), ("fixed_compensation", ("capacitor", "reactor"))):
                value = sum((totals[k] for k in classes), 0j)
                rows = [r for k in classes for r in totals_rows[k]]
                metrics.append(_metric(group + "_active_loss_kw", value.real, "kW", "sum_signed_terminal_power_into_elements", rows, object_id="network"))
                metrics.append(_metric(group + "_net_reactive_absorption_kvar", value.imag, "kvar", "sum_signed_terminal_power_into_elements", rows, object_id="network"))
            for kind, value in sorted(totals.items()):
                for metric, number, unit in (("element_class_net_p_kw", value.real, "kW"), ("element_class_net_q_kvar", value.imag, "kvar")):
                    metrics.append(_metric(metric, number, unit, "signed_power_into_elements", totals_rows[kind], object_id=kind))
        except (ValueError, OverflowError):
            losses_valid = False
            metrics = [m for m in metrics if m.get("object_id") != "network"]
            measured.pop(("active_loss", "network"), None)
    if not losses_valid:
        issues.append("network_loss_boundary_or_terminal_power_incomplete")
    voltage_pu = [m for m in metrics if m["metric"] == "native_voltage_pu"]
    for label, choose in (("minimum", min), ("maximum", max)):
        if voltage_pu:
            extreme = choose(voltage_pu, key=lambda m:m["value"])
            metrics.append({**extreme, "metric":"native_" + label + "_voltage_pu",
                "scope":"available_verified_non_ground_standard_nodes_only",
                "not_an_engineering_compliance_conclusion":True})

    analysis = {"schema_version":ENGINEERING_VERSION, "scope":"verified_native_facts_and_explicit_project_criteria",
        "task_id":result.get("task_id"), "numerical_validity":result.get("numerical_validity"),
        "metrics":metrics, "coverage":{"trusted_task_bound_observations":trusted, "full_network":full,
            "network_loss_boundary_complete":losses_valid, "expected_check_points":len(expected)},
        "issues":issues, "engineering_accepted":False,
        "limitations":["项目确认信息为调用者声明，不认证身份或实际工程参考资格",
            "端口总kVA只是端口等值指标，不替代逐导体电流或完整热模型",
            "原生节点/导体编号不推定A/B/C或delta绕组相电流",
            "无默认工程限值、设备额定或损耗百分率分母；P/Q保留进入元件符号",
            "Capacitor/Reactor净无功交换包含补偿，不统称无功网损",
            "经认可工程参考仍待提供；已评估范围通过不代表工程整体合格"]}
    if not isinstance(criteria, ProjectCriteria):
        observations = ()
        if trusted:
            try:
                observations = tuple(Measurement.from_dict(x) for x in package.get("observations", []))
            except (ValueError, TypeError):
                analysis["issues"].append("legacy_measurement_projection_invalid")
        return analysis, evaluate_assessment(observations, criteria, result.get("numerical_validity", "not_run")).to_dict()

    covered, items = set(), []
    for rule in criteria.rules:
        kind, name = rule.kind, rule.object_id.casefold()
        selected, selected_keys, value, reason = [], set(), None, None
        if kind == "voltage":
            references = [rule.node] + ([rule.reference_node] if rule.reference_node else [])
            selected_keys = {("voltage", name, node) for node in references}
            matches = [bus_index.get((name, node), []) for node in references]
            if any(len(r) != 1 for r in matches):
                reason = "matching_node_missing_or_ambiguous"
            else:
                selected = [rows[0] for rows in matches]
        elif kind in {"line_current", "transformer_current"}:
            selected_keys = {(kind, name, rule.terminal, rule.conductor)}
            matches = port_index.get((name, rule.terminal, rule.conductor), [])
            if len(matches) != 1 or matches[0].get("element_class") != kind.split("_")[0]:
                reason = "matching_terminal_conductor_missing_or_ambiguous"
            else:
                selected = matches
        elif kind == "transformer_kva":
            selected = [r for r in objects.get(name, []) if r.get("terminal") == rule.terminal and r.get("element_class") == "transformer"]
            if not selected or len({_key(r) for r in selected}) != len(selected):
                reason = "matching_transformer_terminal_missing_or_ambiguous"
        else:
            selected_keys = {("active_loss", "network")}
        # Even N/A cannot suppress a missing or misspelled target.
        if not trusted:
            status, reason = "not_assessed", "numerical_result_or_task_bound_evidence_not_verified"
        elif reason is not None:
            status = "not_assessed"
        elif selected and not all(r.get("metadata_verified") is True for r in selected):
            status, reason = "not_assessed", "selected_target_metadata_not_verified"
        elif rule.applicability == "not_applicable":
            if kind == "active_loss" and not full:
                status, reason = "not_assessed", "network_inventory_not_available"
            else:
                status, reason = "not_applicable", "explicit_project_scope_exclusion: " + rule.exclusion_reason
        elif selected and all(r.get("enabled") is False for r in selected):
            status, reason = "not_applicable", "verified_device_disabled"
        elif balanced and kind != "active_loss":
            status, reason = "not_assessed", "physical_phase_mapping_not_verified_for_balanced_equivalent"
        else:
            status, reason = "not_assessed", None
            try:
                if kind == "voltage":
                    if not all(_row_valid(r, explicit_node=True) for r in selected):
                        raise ValueError("node_voltage_metadata_not_available")
                    phasors = [cmath.rect(_finite(r.get("voltage_mag_v")), math.radians(_finite(r.get("voltage_angle_deg")))) for r in selected]
                    value = _finite(abs(phasors[0] - phasors[1])) if rule.reference_node else _finite(abs(phasors[0]))
                    if rule.unit == "kV": value /= 1000.0
                    if rule.unit == "pu":
                        if rule.base_voltage_v is None: raise ValueError("confirmed_voltage_base_missing")
                        value /= rule.base_voltage_v
                elif kind in {"line_current", "transformer_current"}:
                    if not all(_row_valid(r, terminal=True) for r in selected):
                        raise ValueError("terminal_current_metadata_not_available")
                    value = _finite(selected[0].get("currents_mag_a"))
                else:
                    key = (kind, name, rule.terminal) if kind == "transformer_kva" else (kind, name)
                    metric = measured.get(key)
                    if metric is None: raise ValueError("complete_terminal_power_or_network_boundary_missing")
                    value = metric["value"]
                value = _finite(value)
                lower, upper, loading = rule.lower, rule.upper, None
                if kind in {"line_current", "transformer_current", "transformer_kva"}:
                    if rule.rated_value is None: raise ValueError("confirmed_equipment_rating_missing")
                    upper = _finite(rule.rated_value * rule.upper_loading_percent / 100.0)
                    loading = _finite(value / rule.rated_value * 100.0)
                status = "violation" if ((lower is not None and value < lower) or (upper is not None and value > upper)) else "pass"
                reason = "outside_confirmed_limits" if status == "violation" else "within_confirmed_limits"
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                # A partially derived voltage in V must never be labelled pu,
                # and failed/overflowed derivations must not leak nonfinite data.
                value, reason = None, str(exc)
        lower, upper, loading = rule.lower, rule.upper, None
        if kind in {"line_current", "transformer_current", "transformer_kva"} and value is not None and rule.rated_value is not None and rule.upper_loading_percent is not None:
            try:
                upper = _finite(rule.rated_value * rule.upper_loading_percent / 100.0)
                loading = _finite(value / rule.rated_value * 100.0)
            except (ValueError, OverflowError, ZeroDivisionError):
                value, upper, loading, status, reason = None, None, None, "not_assessed", "derived_quantity_not_finite"
        if status in {"pass", "violation", "not_applicable"}:
            covered.update(selected_keys)
        refs = sorted({r["evidence_ref"] for r in selected if isinstance(r.get("evidence_ref"), str)})
        if kind == "active_loss" and measured.get((kind, name)):
            refs = measured[(kind, name)]["evidence_refs"]
        items.append({"check_id":rule.rule_id, "kind":kind, "metric":kind, "object_id":rule.object_id,
            "node":rule.node, "reference_node":rule.reference_node, "terminal":rule.terminal, "conductor":rule.conductor,
            "unit":rule.unit, "measurement_basis":rule.measurement_basis,
            "measured":value if trusted and status not in {"not_applicable"} else None,
            "lower":lower, "upper":upper, "rating":rule.rated_value, "rating_unit":rule.unit,
            "loading_percent":loading, "status":status, "reason":reason, "rule_source":rule.source,
            "criteria_source":criteria.source, "evidence_refs":refs,
            "formula":("abs(sum(P)+j*sum(Q))" if kind == "transformer_kva" else "magnitude of explicitly selected node phasor difference" if kind == "voltage" else
                "sum signed passive element terminal P" if kind == "active_loss" else "terminal conductor RMS current / confirmed rating"),
            "inputs":[{key:r.get(key) for key in ("object_id","node","terminal","conductor","voltage_mag_v","voltage_angle_deg","currents_mag_a","powers_kw","powers_kvar","evidence_ref")} for r in selected]})
        if status in {"pass", "violation"}:
            if loading is not None:
                metrics.append({"metric":kind + "_loading_percent", "object_id":rule.object_id,
                    "terminal":rule.terminal, "conductor":rule.conductor, "value":loading, "unit":"percent",
                    "measurement_basis":rule.measurement_basis, "status":"available", "rating":rule.rated_value,
                    "rating_unit":rule.unit, "criteria_rule_id":rule.rule_id, "rule_source":rule.source,
                    "evidence_refs":refs, "formula":"measured / explicit_project_rating * 100"})
            if kind == "voltage" and rule.unit == "pu":
                metrics.append({"metric":"project_voltage_pu", "object_id":rule.object_id, "node":rule.node,
                    "reference_node":rule.reference_node, "value":value, "unit":"pu", "measurement_basis":rule.measurement_basis,
                    "base_voltage_v":rule.base_voltage_v, "status":"available", "criteria_rule_id":rule.rule_id,
                    "rule_source":rule.source, "evidence_refs":refs})
    for key in sorted(expected - covered, key=str):
        items.append({"check_id":"coverage:" + ":".join(map(str, key)), "metric":key[0], "kind":key[0],
            "object_id":key[1], "node":key[2] if key[0] == "voltage" else None,
            "terminal":key[2] if key[0].endswith("current") else None,
            "conductor":key[3] if key[0].endswith("current") else None,
            "status":"not_assessed", "reason":"confirmed_criteria_missing_or_check_not_evaluated"})
    for kind in ("voltage", "line_current", "transformer_current"):
        if full and not any(k[0] == kind for k in expected):
            items.append({"check_id":"inventory:" + kind, "metric":kind, "kind":kind,
                "status":"not_applicable", "reason":"verified_inventory_contains_no_applicable_objects"})
    if not full:
        items.append({"check_id":"coverage:network", "metric":"coverage", "status":"not_assessed",
            "reason":"complete_requested_network_domains_required_for_full_coverage"})
    counts = Counter(x["status"] for x in items)
    status = "violations_found" if counts["violation"] else "partially_assessed" if counts["not_assessed"] and counts["pass"] else "not_assessed" if counts["not_assessed"] else "evaluated_rules_passed"
    coverage = {"expected_points":len(expected), "covered_points":len(expected & covered),
        "missing_points":len(expected - covered), "complete":bool(full and not expected - covered and not counts["not_assessed"]),
        "scope":"full_native_inventory_with_explicit_project_connections_and_exclusions"}
    analysis["coverage"].update(coverage)
    for kind in ("line_current", "transformer_current", "transformer_kva"):
        values = [m for m in metrics if m["metric"] == kind + "_loading_percent"]
        if values:
            maximum = max(values, key=lambda m:m["value"])
            metrics.append({**maximum, "metric":"maximum_" + kind + "_loading_percent",
                "scope":"evaluated_project_ratings_only; missing/excluded points remain in assessment coverage"})
    report = {"schema_version":"2.0", "numerical_validity":result.get("numerical_validity"), "status":status,
        "criteria_id":criteria.criteria_id, "criteria_version":criteria.version, "criteria_digest":criteria.digest(),
        "criteria_source":criteria.source, "confirmed_by":criteria.confirmed_by, "confirmed_at":criteria.confirmed_at,
        "items":items, "counts":{s:counts[s] for s in ("pass","violation","not_assessed","not_applicable")},
        "coverage":coverage, "scope":"explicit_confirmed_project_rules_and_verified_inventory",
        "overall_compliance":"not_assessed", "declared_scope_checks_passed":bool(coverage["complete"] and not counts["violation"]),
        "engineering_accepted":False, "thermal_model_assessed":False}
    return analysis, report
