"""Strict literal MATPOWER-to-DSS balanced PQ snapshot adapter; no I/O/solver.

Copyright (c) yangshaopeng. Synthetic three-phase embedding is an explicitly
bounded positive-sequence equivalent, never recovered physical phase identity.
Finite source impedance must be supplied; ideal-slack equality is not claimed.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import re

from .contracts import ContractError
from .matpower_literals import MatpowerParseError, parse_literal_matpower


ADAPTER_VERSION = "matpower-balanced-pq-1.0.1"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_BUSES = 2000
MAX_BRANCHES = 5000
MAX_GENERATORS = 2000
_INPUT_FIELDS = {"format", "source_file", "content_utf8", "frequency_hz", "source_impedance_ohm"}
_BUS_COLUMNS = ("BUS_I", "BUS_TYPE", "PD", "QD", "GS", "BS", "BUS_AREA", "VM", "VA", "BASE_KV", "ZONE", "VMAX", "VMIN")
_GEN_COLUMNS = ("GEN_BUS", "PG", "QG", "QMAX", "QMIN", "VG", "MBASE", "GEN_STATUS", "PMAX", "PMIN", "PC1", "PC2", "QC1MIN", "QC1MAX", "QC2MIN", "QC2MAX", "RAMP_AGC", "RAMP_10", "RAMP_30", "RAMP_Q", "APF")
_BRANCH_COLUMNS = ("F_BUS", "T_BUS", "BR_R", "BR_X", "BR_B", "RATE_A", "RATE_B", "RATE_C", "TAP", "SHIFT", "BR_STATUS", "ANGMIN", "ANGMAX")


def _fail(field, message, *, unsupported=False):
    raise ContractError("model.matpower." + field, message,
                        "capability_unavailable" if unsupported else "invalid_contract")


def _finite(value, *, positive=False, nonnegative=False):
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
                and (not positive or value > 0) and (not nonnegative or value >= 0))
    except OverflowError:
        return False


def _integer(value, *, positive=False):
    return _finite(value) and float(value).is_integer() and abs(value) <= 2**53 - 1 and (not positive or value > 0)


def _number(value):
    if not _finite(value):
        _fail("conversion", "Derived native quantity is nonfinite or outside binary-double range.")
    return format(value, ".17g")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_source(payload):
    if not isinstance(payload, Mapping) or set(payload) != _INPUT_FIELDS:
        _fail("input", "Expected exactly format/source_file/content_utf8/frequency_hz/source_impedance_ohm.")
    if payload["format"] != "MATPOWER":
        _fail("format", "This adapter only accepts the explicit MATPOWER format.")
    name, text = payload["source_file"], payload["content_utf8"]
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,123}\.m", name)
            or name.endswith((". ", "..m"))
            or re.match(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])\.", name)):
        _fail("source_file", "Source must be one safe package-root .m filename.")
    if not isinstance(text, str) or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
        _fail("content_utf8", "Source must be bounded UTF-8 text without control characters.")
    try:
        data = text.encode("utf-8")
    except UnicodeError:
        _fail("content_utf8", "Source is not valid UTF-8 Unicode.")
    if not data or len(data) > MAX_SOURCE_BYTES:
        _fail("content_utf8", "Source requires 1 byte to 2 MiB.")
    frequency = payload["frequency_hz"]
    if not _finite(frequency, positive=True):
        _fail("frequency_hz", "Positive finite frequency is required; MATPOWER does not supply a frequency default.")
    impedance = payload["source_impedance_ohm"]
    if (not isinstance(impedance, Mapping) or set(impedance) != {"r", "x"}
            or any(not _finite(impedance[k], nonnegative=True) for k in ("r", "x"))
            or impedance["r"] == impedance["x"] == 0):
        _fail("source_impedance_ohm", "Explicit nonnegative finite R/X, not both zero, are required for the finite DSS source.")
    try:
        parser = parse_literal_matpower(text.removeprefix("\ufeff"), source=name)
    except (MatpowerParseError, ValueError, OverflowError, RecursionError) as exc:
        _fail("source", str(exc), unsupported=True)
    return parser, data


def _validate_case(parser):
    buses, generators, branches = parser.rows["bus"], parser.rows["gen"], parser.rows["branch"]
    if len(buses) > MAX_BUSES or len(branches) > MAX_BRANCHES or len(generators) > MAX_GENERATORS:
        _fail("size", "Input exceeds the 2000 bus / 5000 branch / 2000 generator inspection budget.", unsupported=True)
    by_id = {int(row[0]): row for row in buses}
    slack = []
    for index, row in enumerate(buses, 1):
        if row[1] not in {1, 3}:
            _fail(f"bus.{index}.BUS_TYPE", "Only PQ and one reference bus are supported; PV control and isolated buses are not mapped.", unsupported=True)
        if row[1] == 3:
            slack.append(int(row[0]))
        if row[2] < 0:
            _fail(f"bus.{index}.PD", "Negative demand is outside this first adapter; use an explicit fixed-PQ generator.", unsupported=True)
        if row[4] < 0:
            _fail(f"bus.{index}.GS", "Negative shunt conductance is outside the passive subset.", unsupported=True)
        for column in (6, 10):
            if not _integer(row[column], positive=True):
                _fail(f"bus.{index}", "Area and zone must be explicit positive integers.")
        if row[7] <= 0 or row[11] <= 0 or row[12] <= 0 or row[12] > row[11]:
            _fail(f"bus.{index}", "Initial voltage and declared voltage bounds must be positive and ordered.")
        if row[2] == 0 and row[3] != 0:
            _fail(f"bus.{index}.QD", "Pure constant-Q load cannot use native zero-kW normalization; this unsupported case is rejected.", unsupported=True)
    if len(slack) != 1:
        _fail("bus.BUS_TYPE", "Exactly one explicit reference bus is required.", unsupported=True)
    online_ref = []
    for index, row in enumerate(generators, 1):
        if row[3] < row[4] or row[8] < row[9] or row[5] <= 0 or row[6] < 0:
            _fail(f"gen.{index}", "Generator limits must be ordered, Vg positive and mBase nonnegative.")
        if any(row[column] != 0 for column in range(10, 21)):
            _fail(f"gen.{index}", "Nonzero P-Q capability curves, ramps and participation factors are not supported.", unsupported=True)
        if row[7] <= 0:
            continue
        if int(row[0]) == slack[0]:
            online_ref.append(row)
        elif row[1] <= 0:
            _fail(f"gen.{index}.PG", "Online non-reference fixed-PQ generators require positive Pg; no zero-kW or negative-capacity substitution.", unsupported=True)
    if len(online_ref) != 1:
        _fail("gen", "Exactly one online reference generator is supported; no implicit source or unit aggregation.", unsupported=True)
    adjacent = {key: set() for key in by_id}
    for index, row in enumerate(branches, 1):
        if row[0] == row[1]:
            _fail(f"branch.{index}", "Self-loop branches are not supported.", unsupported=True)
        if row[2] < 0 or row[3] < 0 or row[2] == row[3] == 0:
            _fail(f"branch.{index}", "Passive nonzero series impedance with nonnegative R/X is required.", unsupported=True)
        if any(row[column] < 0 for column in (5, 6, 7)) or row[11] > row[12]:
            _fail(f"branch.{index}", "Thermal ratings must be nonnegative and angle limits ordered.")
        if row[9] != 0:
            _fail(f"branch.{index}.SHIFT", "Nonzero phase-shifting transformers are not in this verified subset.", unsupported=True)
        transformer = row[8] > 0 or by_id[int(row[0])][9] != by_id[int(row[1])][9]
        if transformer and row[3] <= 0:
            _fail(f"branch.{index}.BR_X", "The supported two-winding transformer needs positive leakage reactance.", unsupported=True)
        if row[10] == 1:
            adjacent[int(row[0])].add(int(row[1]))
            adjacent[int(row[1])].add(int(row[0]))
    seen, pending = set(), [slack[0]]
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(adjacent[name] - seen)
    if seen != set(by_id):
        _fail("branch", "Every declared bus must be connected to the single source through in-service branches.", unsupported=True)
    return by_id, slack[0], online_ref[0]


def adapt_matpower_payload(payload):
    """Return deterministic native payload and full source/mapping provenance.

    This pure function performs no file I/O, MATLAB, solver or web imports.
    It proves only supported static translation; runtime readback and numerical
    reference comparison remain mandatory before this adapter is accepted.
    """
    parser, source_bytes = _check_source(payload)
    by_id, slack_id, slack_gen = _validate_case(parser)
    base = parser.scalars["baseMVA"]
    frequency = payload["frequency_hz"]
    source_z = dict(payload["source_impedance_ohm"])
    mappings = []
    lines = ["! MATPOWER balanced PQ snapshot; synthetic phases, no physical phase recovery.",
             "! Copyright (c) yangshaopeng.", "Clear", f"Set DefaultBaseFrequency={_number(frequency)}"]
    bus_name = lambda number: "mp_bus_" + str(int(number))
    source_bus = bus_name(slack_id)
    lines += [f"New Circuit.matpower phases=3 bus1={source_bus}.1.2.3 basekv={_number(by_id[slack_id][9])} pu={_number(slack_gen[5])} angle={_number(by_id[slack_id][8])} frequency={_number(frequency)} basefreq={_number(frequency)} r1={_number(source_z['r'])} x1={_number(source_z['x'])} r0={_number(source_z['r'])} x0={_number(source_z['x'])}",
              f"Edit Vsource.Source bus2={source_bus}.0.0.0", f"Set BaseFrequency={_number(frequency)}"]

    def mapped(matrix, index, elements, role, **facts):
        mappings.append({"source_matrix": matrix, "source_row": index,
                         "source_line": parser.row_tokens[matrix][index-1].line,
                         "target_elements": elements, "role": role, **facts})

    def power_load(name, bus, kv, p_kw, q_kvar, model):
        if p_kw == 0 or abs(p_kw) < 1e-8:
            _fail("conversion.power", "Native nonzero-kW normalization would change this declared power; input is rejected.", unsupported=True)
        lines.append(f"New Load.{name} phases=3 bus1={bus}.1.2.3.0 conn=wye kv={_number(kv)} kw={_number(p_kw)} kvar={_number(q_kvar)} model={model} vminpu=0 vmaxpu=1000000 vlowpu=0 status=fixed enabled=yes basefreq={_number(frequency)}")
        return "Load." + name

    def reactive_shunt(name, bus, kv, injected_mvar):
        if injected_mvar == 0:
            return None
        kind = "Capacitor" if injected_mvar > 0 else "Reactor"
        parameters = " numsteps=1 r=0 xl=0" if kind == "Capacitor" else " r=0"
        lines.append(f"New {kind}.{name} phases=3 bus1={bus}.1.2.3 bus2={bus}.0.0.0 conn=wye{parameters} kv={_number(kv)} kvar={_number(abs(injected_mvar)*1000)} enabled=yes basefreq={_number(frequency)}")
        return kind + "." + name

    for index, row in enumerate(parser.rows["bus"], 1):
        name, elements = bus_name(row[0]), []
        if row[2] != 0 or row[3] != 0:
            elements.append(power_load(f"mp_demand_{index}", name, row[9], row[2]*1000, row[3]*1000, 1))
        if row[4] > 0:
            elements.append(power_load(f"mp_conductance_{index}", name, row[9], row[4]*1000, 0, 2))
        reactive = reactive_shunt(f"mp_bus_shunt_{index}", name, row[9], row[5])
        if reactive:
            elements.append(reactive)
        mapped("bus", index, elements, "bus_and_demands", bus_id=int(row[0]), target_bus=name,
               input_type=int(row[1]), base_kv_ll=row[9], demand_mw_mvar=row[2:4], shunt_gs_bs=row[4:6],
               voltage_limits={"vmax": row[11], "vmin": row[12], "engineering_confirmed": False})

    for index, row in enumerate(parser.rows["gen"], 1):
        if row[7] <= 0:
            mapped("gen", index, [], "out_of_service_generator", enabled=False,
                   reason="Retained source row; out-of-service unit has no snapshot injection.")
        elif int(row[0]) == slack_id:
            mapped("gen", index, ["Vsource.Source"], "finite_source_equivalent", enabled=True,
                   voltage_setpoint_pu=row[5], angle_deg=by_id[slack_id][8],
                   input_pg_qg="initial/free_slack_dispatch_not_fixed_in_AC_power_flow")
        else:
            p_kw, q_kvar = row[1]*1000, row[2]*1000
            if p_kw < 1e-8:
                _fail(f"gen.{index}.PG", "Native zero/very-small-kW normalization would change the source injection.", unsupported=True)
            kva = math.hypot(p_kw, q_kvar)
            name = f"mp_generator_{index}"
            lines.append(f"New Generator.{name} phases=3 bus1={bus_name(row[0])}.1.2.3.0 conn=wye kv={_number(by_id[int(row[0])][9])} kw={_number(p_kw)} kvar={_number(q_kvar)} kva={_number(kva)} model=1 vminpu=0 vmaxpu=1000000 status=fixed enabled=yes basefreq={_number(frequency)}")
            mapped("gen", index, ["Generator." + name], "fixed_pq_generator", enabled=True,
                   pg_mw=row[1], qg_mvar=row[2], target_kva=kva,
                   kva_provenance="operating_point_apparent_power_only_not_nameplate_or_engineering_limit")

    for index, row in enumerate(parser.rows["branch"], 1):
        f_id, t_id = int(row[0]), int(row[1])
        kv_f, kv_t = by_id[f_id][9], by_id[t_id][9]
        tap = row[8] if row[8] > 0 else 1.0
        if not _finite(tap*tap, positive=True) or not _finite(1/(tap*tap), positive=True):
            _fail(f"branch.{index}.TAP", "Tap transformation is outside finite binary-double range.")
        if row[10] == 0:
            mapped("branch", index, [], "out_of_service_branch", enabled=False,
                   reason="All source parameters retained; open branch and its shunts have no snapshot admittance.")
            continue
        transformer = row[8] > 0 or kv_f != kv_t
        name = f"mp_branch_{index}"
        series, shunts = [], []
        if transformer:
            elements = f"New Transformer.{name} phases=3 windings=2 buses=[{bus_name(f_id)}.1.2.3.0 {bus_name(t_id)}.1.2.3.0] conns=[wye wye] kvs=[{_number(kv_f)} {_number(kv_t)}] kvas=[{_number(base*1000)} {_number(base*1000)}] %rs=[{_number(row[2]*50)} {_number(row[2]*50)}] xhl={_number(row[3]*100)} %noloadloss=0 %imag=0 ppm_antifloat=0 wdg=1 mintap={_number(min(1.0,tap))} maxtap={_number(max(1.0,tap))} tap={_number(tap)} wdg=2 tap=1 enabled=yes basefreq={_number(frequency)}"
            lines.append(elements)
            series = ["Transformer." + name]
        else:
            z_base = kv_f*kv_f/base
            r_ohm, x_ohm = row[2]*z_base, row[3]*z_base
            if (not _finite(z_base, positive=True)
                    or any(source != 0 and (not _finite(actual) or actual == 0)
                           for source,actual in ((row[2],r_ohm),(row[3],x_ohm)))):
                _fail(f"branch.{index}", "Impedance base conversion would overflow or erase a nonzero series parameter.")
            lines.append(f"New Line.{name} phases=3 bus1={bus_name(f_id)}.1.2.3 bus2={bus_name(t_id)}.1.2.3 length=1 units=none r1={_number(r_ohm)} x1={_number(x_ohm)} r0={_number(r_ohm)} x0={_number(x_ohm)} c1=0 c0=0 enabled=yes basefreq={_number(frequency)}")
            series = ["Line." + name]
        for side, bus, kv, factor in (("from", f_id, kv_f, 1/(tap*tap)), ("to", t_id, kv_t, 1.0)):
            q_mvar = row[4]*base*factor/2
            if row[4] != 0 and (not _finite(q_mvar) or q_mvar == 0):
                _fail(f"branch.{index}.BR_B", "Pi charging conversion would overflow or erase nonzero susceptance.")
            target = reactive_shunt(f"mp_charging_{index}_{side}", bus_name(bus), kv,
                                    q_mvar)
            if target:
                shunts.append({"element": target, "side": side,
                               "injected_mvar_at_1pu": q_mvar})
        mapped("branch", index, series + [s["element"] for s in shunts],
               "two_winding_transformer" if transformer else "positive_sequence_line", enabled=True,
               from_bus=f_id, to_bus=t_id, target_from_bus=bus_name(f_id), target_to_bus=bus_name(t_id),
               series_elements=series, shunts=shunts, tap_ratio=tap, shift_deg=0,
               resistance_pu=row[2], reactance_pu=row[3], total_charging_b_pu=row[4],
               impedance_base_mva=base, impedance_base_kv_ll_from=kv_f,
               rating_provenance="system_impedance_base_not_nameplate" if transformer else "no_native_thermal_rating_from_source",
               thermal_limits_mva={"rate_a":row[5],"rate_b":row[6],"rate_c":row[7],"engineering_confirmed":False})

    voltage_bases = sorted({row[9] for row in parser.rows["bus"]})
    lines += ["Set Loadmult=1", "Set Genmult=1",
              "Set VoltageBases=[" + " ".join(_number(v) for v in voltage_bases) + "]", "CalcVoltageBases",
              "Set mode=snapshot", "Set controlmode=off", f"Set frequency={_number(frequency)}", ""]
    master = "\n".join(lines)
    native_payload = {"master_file": "Master.dss", "files": [{"path":"Master.dss", "content_utf8":master}]}
    source_case = {"version":"2", "baseMVA":base,
                   **{key:[list(row) for row in rows] for key,rows in parser.rows.items()}}
    manifest = {"version":"1.0", "adapter_version":ADAPTER_VERSION, "source_format":"MATPOWER", "target_format":"OpenDSS",
        "source_file":payload["source_file"], "source_sha256":hashlib.sha256(source_bytes).hexdigest(),
        "source_bytes":len(source_bytes), "source_case":source_case,
        "source_columns":{"bus":list(_BUS_COLUMNS),"gen":list(_GEN_COLUMNS),"branch":list(_BRANCH_COLUMNS)},
        "source_assignment_lines":{key:value.line for key,value in parser.assignments.items()},
        "scope":"balanced_pq_ac_snapshot", "balanced_equivalent":True, "physical_phase_mapping":False,
        "phase_provenance":"synthetic_three_phase_positive_sequence_embedding",
        "parameters":{"frequency_hz":frequency,"source_impedance_ohm":source_z},
        "mappings":mappings,
        "assumptions":[
            "Caller supplied finite source R/X replaces the mathematical ideal reference source; exact ideal-slack equality is not claimed.",
            "Three equal wye phases and node labels 1/2/3/0 are a computational embedding, not physical phase or neutral identity.",
            "Equal line zero/positive-sequence impedances are synthetic; only balanced positive-sequence behavior is in scope.",
            "Transformer kVA uses system impedance base; wye-wye winding connection and zero magnetizing/anti-float terms are an equivalent embedding, not actual equipment construction.",
            "PQ devices remain constant power for 0 < voltage_pu < 1000000; lower-voltage switching is disabled, not a physical load-model assertion.",
            "Bus conductance and susceptance and branch pi charging are fixed admittances, not constant-Q injections or automatic controllers."],
        "not_applied":[
            {"fields":"bus.VM/VA except source reference angle; gen.VG on non-reference PQ buses", "reason":"Initial guesses and non-reference PQ generator VG are retained; they are not imposed as fixed PQ voltage constraints or PV regulation."},
            {"fields":"bus.VMAX/VMIN, branch.RATE_A/B/C/ANGMIN/ANGMAX, gen.QMAX/QMIN/PMAX/PMIN", "reason":"Retained original limits; unconstrained snapshot does not enforce OPF/Q-limit switching or approve engineering criteria."},
            {"fields":"gen.MBASE", "reason":"Machine per-unit base retained; never used as a physical nameplate kVA."},
            {"fields":"bus.BUS_AREA/ZONE, gencost, areas", "reason":"Retained source metadata; no economic dispatch or area model."}],
        "native_master_file":"Master.dss", "native_master_sha256":hashlib.sha256(master.encode()).hexdigest(),
        "native_payload_sha256":hashlib.sha256(_json(native_payload).encode()).hexdigest(),
        "runtime_verified":False, "engineering_accepted":False}
    # Fail before returning if any derived provenance quantity became nonfinite.
    _json(manifest)
    return {"native_payload":native_payload, "conversion_manifest":manifest}
