"""Offline model semantics derived from source data, with real files and atomic-write faults.

Fixtures use DPF_TEST_ARTIFACT_ROOT or a temporary directory. No OpenDSS/native
engine is imported or started. Physical tests inspect emitted DSS declarations;
they do not claim numerical equivalence or convergence.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

import pandas as pd

from ..web import case_converters as converters


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_RUN = time.strftime("%H%M%S") + "_" + str(os.getpid())


def _attribute(line: str, name: str) -> float:
    found = re.search(rf"(?i)\b{re.escape(name)}\s*=\s*({_NUMBER})", line)
    if not found:
        raise AssertionError(f"Missing {name}: {line}")
    return float(found.group(1))


def _elements(text: str, kind: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(rf"(?i)^\s*New\s+{kind}\.", line)]


def _bus_reference(line: str, name: str) -> str:
    found = re.search(rf"(?i)\b{re.escape(name)}\s*=\s*([^\s]+)", line)
    if not found:
        raise AssertionError(f"Missing {name}: {line}")
    # These fixtures use three-phase declarations without explicit node suffixes.
    return found.group(1)


def _network(slack="B1", downstream="B2") -> dict:
    return {"bus": [{"bus_id": slack, "type": "slack", "base_kv": 20, "p_kw": 50, "q_kvar": 20},
                    {"bus_id": downstream, "type": "pq", "base_kv": 20, "p_kw": 100, "q_kvar": 40}],
            "line": [{"line_id": "L1", "from_bus": slack, "to_bus": downstream, "r_ohm": .1, "x_ohm": .2}]}


_BUS = "1 3 .05 .02 0 0 1 1 0 20 1 1.1 .9;\n2 2 .1 .04 0 0 1 1 0 20 1 1.1 .9;"
_GEN = "1 .5 0 10 -10 1 100 1 10 0;\n2 .2 0 10 -10 1.03 100 1 10 0;"
_BRANCH = "1 2 .01 .02 0 100 100 100 0 0 1 -360 360;"


def _matpower(*, bus=_BUS, gen=_GEN, branch=_BRANCH) -> str:
    return ("function mpc = semantic_case\nmpc.version='2';\nmpc.baseMVA=100;\n"
            f"mpc.bus=[\n{bus}\n];\nmpc.gen=[\n{gen}\n];\nmpc.branch=[\n{branch}\n];\n")


class ConversionSemanticsTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("DPF_TEST_ARTIFACT_ROOT") or tempfile.mkdtemp(prefix="dpf-conversion-semantics-"))
        self.root = base / "unit_semantics" / _RUN / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=False)

    def json_convert(self, data, label="json") -> tuple[Path, str]:
        source = self.root / (label + ".json")
        source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        master = Path(converters.convert_json(str(source), str(self.root / (label + "_out"))))
        return master, master.read_text(encoding="utf-8")

    def matpower_convert(self, text, label="matpower") -> tuple[Path, str]:
        source = self.root / (label + ".m")
        source.write_text(text, encoding="utf-8")
        master = Path(converters.convert_matpower_m(str(source), str(self.root / (label + "_out"))))
        return master, master.read_text(encoding="utf-8")

    def xlsx_convert(self, data, label="xlsx") -> tuple[Path, str]:
        source = self.root / (label + ".xlsx")
        with pd.ExcelWriter(source, engine="openpyxl") as book:
            pd.DataFrame(data["bus"]).to_excel(book, sheet_name="Bus", index=False)
            pd.DataFrame(data["line"]).to_excel(book, sheet_name="Line", index=False)
            if "load" in data:
                pd.DataFrame(data["load"], columns=["load_id", "bus_id", "p_kw", "q_kvar", "phases"]).to_excel(book, sheet_name="Load", index=False)
        master = Path(converters.convert_xlsx(str(source), str(self.root / (label + "_out"))))
        return master, master.read_text(encoding="utf-8")

    def csv_convert(self, data, label="csv") -> tuple[Path, str]:
        source = self.root / label
        source.mkdir()
        pd.DataFrame(data["bus"]).to_csv(source / "bus.csv", index=False)
        pd.DataFrame(data["line"]).to_csv(source / "line.csv", index=False)
        if "load" in data:
            pd.DataFrame(data["load"], columns=["load_id", "bus_id", "p_kw", "q_kvar", "phases"]).to_csv(source / "load.csv", index=False)
        master = Path(converters.convert_csv(str(source), str(self.root / (label + "_out"))))
        return master, master.read_text(encoding="utf-8")

    def assert_loads_from_source(self, text, *, kw, kvar, source_load=True):
        loads = _elements(text, "Load")
        self.assertAlmostEqual(sum(_attribute(line, "kW") for line in loads), kw)
        self.assertAlmostEqual(sum(_attribute(line, "kvar") for line in loads), kvar)
        if source_load:
            self.assertTrue(any(re.search(r"(?i)\bBus1=sourcebus(?:\.|\s|$)", line) for line in loads), loads)

    def test_json_retains_slack_and_source_bus_local_demand(self):
        for name in ("B1", "SOURCE"):
            with self.subTest(name=name):
                _, text = self.json_convert(_network(name), name)
                self.assert_loads_from_source(text, kw=50 + 100, kvar=20 + 40)

    def test_xlsx_retains_slack_and_source_bus_local_demand(self):
        for name in ("B1", "SOURCE"):
            with self.subTest(name=name):
                _, text = self.xlsx_convert(_network(name), name)
                self.assert_loads_from_source(text, kw=50 + 100, kvar=20 + 40)

    def test_csv_retains_slack_and_source_bus_local_demand(self):
        for name in ("B1", "SOURCE"):
            with self.subTest(name=name):
                _, text = self.csv_convert(_network(name), name)
                self.assert_loads_from_source(text, kw=50 + 100, kvar=20 + 40)

    def test_matpower_retains_reference_bus_local_demand_in_mw_mvar(self):
        _, text = self.matpower_convert(_matpower())
        self.assert_loads_from_source(text, kw=(.05 + .1) * 1000, kvar=(.02 + .04) * 1000)

    def test_real_SOURCE_bus_distinct_from_slack_keeps_line_and_local_demand(self):
        for format_name, convert in (("json", self.json_convert), ("xlsx", self.xlsx_convert), ("csv", self.csv_convert)):
            with self.subTest(format_name=format_name):
                _, text = convert(_network(downstream="SOURCE"), format_name)
                line = _elements(text, "Line")[0]
                self.assertEqual(_bus_reference(line, "Bus1"), "sourcebus")
                self.assertEqual(_bus_reference(line, "Bus2"), "SOURCE")
                load = next(item for item in _elements(text, "Load") if "Load_SOURCE " in item)
                self.assertEqual(_bus_reference(load, "Bus1"), "SOURCE")
                self.assertEqual(_attribute(load, "kW"), 100)
                self.assertEqual(_attribute(load, "kvar"), 40)

    def test_real_sourcebus_bus_has_separate_internal_circuit_source(self):
        for format_name, convert in (("json", self.json_convert), ("xlsx", self.xlsx_convert), ("csv", self.csv_convert)):
            with self.subTest(format_name=format_name):
                _, text = convert(_network(downstream="SoUrCeBuS"), format_name)
                circuit_source = _bus_reference(_elements(text, "Circuit")[0], "bus1")
                self.assertNotEqual(circuit_source.casefold(), "sourcebus")
                line = _elements(text, "Line")[0]
                self.assertEqual(_bus_reference(line, "Bus1"), circuit_source)
                self.assertEqual(_bus_reference(line, "Bus2"), "SoUrCeBuS")
                loads = _elements(text, "Load")
                downstream = next(item for item in loads if "Load_SoUrCeBuS " in item)
                slack = next(item for item in loads if "Load_B1 " in item)
                self.assertEqual(_bus_reference(downstream, "Bus1"), "SoUrCeBuS")
                self.assertEqual(_bus_reference(slack, "Bus1"), circuit_source)
                self.assertEqual(_attribute(downstream, "kW"), 100)

    def test_generated_circuit_source_avoids_all_existing_bus_names(self):
        names = ("SOURCE", "sourcebus", "_TIANSHU_SOURCE1")
        data = _network(downstream=names[0])
        for index, name in enumerate(names[1:], 2):
            data["bus"].append({"bus_id": name, "type": "pq", "base_kv": 20, "p_kw": 10 * index})
            data["line"].append({"line_id": f"L{index}", "from_bus": names[index - 2], "to_bus": name,
                                 "r_ohm": .1, "x_ohm": .2})
        _, text = self.json_convert(data)
        circuit_source = _bus_reference(_elements(text, "Circuit")[0], "bus1")
        self.assertNotIn(circuit_source.casefold(), {name.casefold() for name in names})
        actual_edges = [(_bus_reference(item, "Bus1"), _bus_reference(item, "Bus2"))
                        for item in _elements(text, "Line")]
        self.assertEqual(actual_edges, [(circuit_source, names[0]), (names[0], names[1]), (names[1], names[2])])
        actual_load_buses = {_bus_reference(item, "Bus1") for item in _elements(text, "Load")}
        self.assertEqual(actual_load_buses, {circuit_source, *names})

    def test_consistent_complete_ohm_and_pu_impedances_are_accepted(self):
        # At 20 kV / 100 MVA, Zbase is 4 ohm. These two declarations
        # independently describe the same .1 + j.2 ohm physical branch.
        for kind in ("line", "transformer"):
            with self.subTest(kind=kind):
                data = _network()
                data["system"] = {"S_BASE_MVA": 100}
                data["line"][0].update(kind=kind, r_pu=.1 / 4, x_pu=.2 / 4)
                _, text = self.json_convert(data, kind)
                if kind == "line":
                    line = _elements(text, "Line")[0]
                    self.assertEqual(_attribute(line, "R1"), .1)
                    self.assertEqual(_attribute(line, "X1"), .2)
                else:
                    xfmr = _elements(text, "Transformer")[0]
                    self.assertEqual(_attribute(xfmr, "XHL"), (.2 / 4) * 100)
                    winding_resistances = re.search(r"%Rs=\[([^\]]+)\]", xfmr).group(1).split()
                    self.assertAlmostEqual(sum(map(float, winding_resistances)), (.1 / 4) * 100)

    def test_conflicting_complete_ohm_and_pu_impedances_preserve_old_publication(self):
        for kind in ("line", "transformer"):
            with self.subTest(kind=kind):
                data = _network()
                data["system"] = {"S_BASE_MVA": 100}
                data["line"][0]["kind"] = kind
                master, _ = self.json_convert(data, kind)
                meta = master.parent / ".conversion.json"
                before_master, before_meta = master.read_bytes(), meta.read_bytes()
                # 1 + j2 pu equals 4 + j8 ohm, not the simultaneous .1 + j.2.
                changed = copy.deepcopy(data)
                changed["line"][0].update(r_pu=1, x_pu=2)
                source = self.root / (kind + ".json")
                source.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "阻抗单位冲突"):
                    converters.convert_json(str(source), str(master.parent))
                self.assertEqual(master.read_bytes(), before_master)
                self.assertEqual(meta.read_bytes(), before_meta)

    def test_explicit_json_empty_load_does_not_fall_back_to_bus_demands(self):
        data = _network()
        data["load"] = []
        _, text = self.json_convert(data)
        self.assertEqual(_elements(text, "Load"), [])

    def test_explicit_empty_xlsx_load_sheet_and_csv_load_table_remain_zero(self):
        data = _network()
        data["load"] = []
        for format_name, convert in (("xlsx", self.xlsx_convert), ("csv", self.csv_convert)):
            with self.subTest(format_name=format_name):
                _, text = convert(data)
                self.assertEqual(_elements(text, "Load"), [])

    def assert_primary_only_tap(self, text, ratio):
        transformers = _elements(text, "Transformer")
        self.assertEqual(len(transformers), 1, text)
        self.assertEqual(_elements(text, "Line"), [], text)
        winding = None
        taps = {}
        for match in re.finditer(rf"(?i)\b(?:wdg\s*=\s*(\d+)|tap\s*=\s*({_NUMBER}))", transformers[0]):
            if match.group(1) is not None:
                winding = int(match.group(1))
            elif winding is not None:
                taps[winding] = float(match.group(2))
        self.assertEqual(taps, {1: ratio, 2: 1.0}, transformers[0])

    def test_matpower_same_voltage_nonunity_tap_is_primary_winding_transformer(self):
        _, text = self.matpower_convert(_matpower(branch=_BRANCH.replace("100 100 100 0 0", "100 100 100 1.1 0")))
        self.assert_primary_only_tap(text, 1.1)

    def test_mixed_csv_transformer_type_survives_category_split(self):
        data = _network()
        rows = [{**bus, "type": "bus", "bus_type": bus["type"]} for bus in data["bus"]]
        rows.append({**data["line"][0], "type": "transformer", "tap_ratio": 1.1})
        source = self.root / "mixed.csv"
        pd.DataFrame(rows).to_csv(source, index=False)
        master = Path(converters.convert_csv(str(source), str(self.root / "mixed_out")))
        self.assert_primary_only_tap(master.read_text(encoding="utf-8"), 1.1)

    def test_online_zero_p_q_pv_remains_voltage_control_with_usable_rating(self):
        _, text = self.matpower_convert(_matpower(gen=_GEN.replace("2 .2 0", "2 0 0")))
        generators = _elements(text, "Generator")
        self.assertEqual(len(generators), 1)
        gen = generators[0]
        self.assertEqual(_attribute(gen, "kW"), 0)
        self.assertEqual(_attribute(gen, "kvar"), 0)
        self.assertEqual(_attribute(gen, "Model"), 3)
        self.assertEqual(_attribute(gen, "Vpu"), 1.03)
        self.assertGreater(_attribute(gen, "kVA"), 0)
        self.assertLess(_attribute(gen, "minkvar"), 0)
        self.assertGreater(_attribute(gen, "maxkvar"), 0)
        self.assertIn("非设备真实容量", text)

    def test_all_offline_generators_do_not_create_pv_elements(self):
        gen = _GEN.replace("1 100 1", "1 100 0").replace("1.03 100 1", "1.03 100 -1")
        _, text = self.matpower_convert(_matpower(gen=gen))
        self.assertEqual(_elements(text, "Generator"), [])

    def test_semicolon_gen_rows_on_one_line_preserve_online_total_power(self):
        gen = _GEN + "\n2 .3 .1 10 -10 1.03 100 1 10 0;"
        _, text = self.matpower_convert(_matpower(gen=gen.replace("\n", " ")))
        generators = _elements(text, "Generator")
        self.assertEqual(len(generators), 1)
        self.assertAlmostEqual(_attribute(generators[0], "kW"), (.2 + .3) * 1000)
        self.assertAlmostEqual(_attribute(generators[0], "kvar"), .1 * 1000)

    def test_generator_continuations_preserve_power_and_voltage_target(self):
        gen = _GEN.replace("1 .5 0", "1 .5 ... % rest\n0").replace("2 .2 0", "2 .2 ...\n0")
        _, text = self.matpower_convert(_matpower(gen=gen))
        generators = _elements(text, "Generator")
        self.assertEqual(len(generators), 1)
        self.assertEqual(_attribute(generators[0], "kW"), .2 * 1000)
        self.assertEqual(_attribute(generators[0], "Vpu"), 1.03)

    def test_conflicting_online_generator_voltage_targets_are_rejected(self):
        gen = _GEN + "\n2 .3 0 10 -10 1.01 100 1 10 0;"
        with self.assertRaisesRegex(ValueError, "Vg.*不一致"):
            self.matpower_convert(_matpower(gen=gen))

    def test_same_bus_online_q_limits_are_summed_and_audited_in_mvar(self):
        gen = "1 .5 0 10 -10 1 100 1 10 0;\n2 0 0 4 -3 1.03 100 1 10 0;\n2 0 0 6 -5 1.03 100 1 10 0;"
        _, text = self.matpower_convert(_matpower(gen=gen))
        note = next(line for line in text.splitlines() if "聚合 Qmin=" in line)
        self.assertIn("合并 2 台在线发电机", note)
        self.assertEqual(_attribute(note, "Qmin"), -3 - 5)
        self.assertEqual(_attribute(note, "Qmax"), 4 + 6)
        self.assertIn("MVAr", note)

    def test_explicit_q_limit_policy_emits_exact_source_bounds(self):
        data = _network()
        data["bus"][1].update(type="pv", p_gen_kw=0, q_gen_kvar=0, v_set_pu=1.03,
                              qmin_mvar=-8, qmax_mvar=10)
        system = pd.DataFrame([{"parameter": "ENFORCE_Q_LIMITS", "value": 1}])
        master = Path(converters.build_dss_from_frames(system, pd.DataFrame(data["bus"]), pd.DataFrame(data["line"]), str(self.root / "limits_out")))
        gen = _elements(master.read_text(encoding="utf-8"), "Generator")[0]
        self.assertEqual(_attribute(gen, "minkvar"), -8 * 1000)
        self.assertEqual(_attribute(gen, "maxkvar"), 10 * 1000)

    def test_explicit_large_q_limits_are_not_removed_by_900_threshold(self):
        data = _network()
        data["bus"][1].update(type="pv", p_gen_kw=100, q_gen_kvar=0, v_set_pu=1.03,
                              qmin_mvar=-1000, qmax_mvar=1100)
        system = pd.DataFrame([{"parameter": "ENFORCE_Q_LIMITS", "value": 1}])
        master = Path(converters.build_dss_from_frames(system, pd.DataFrame(data["bus"]), pd.DataFrame(data["line"]), str(self.root / "limits_out")))
        gen = _elements(master.read_text(encoding="utf-8"), "Generator")[0]
        self.assertEqual(_attribute(gen, "minkvar"), -1000 * 1000)
        self.assertEqual(_attribute(gen, "maxkvar"), 1100 * 1000)

    def test_nonzero_matpower_conductance_or_susceptance_is_constant_admittance(self):
        for columns in (".1 0", "0 -.1"):
            with self.subTest(columns=columns):
                _, text = self.matpower_convert(_matpower(bus=_BUS.replace(".05 .02 0 0", ".05 .02 " + columns)), columns.replace(" ", "_"))
                records = [json.loads(line.removeprefix("! [Tianshu Fidelity] "))
                           for line in text.splitlines() if line.startswith("! [Tianshu Fidelity] ")]
                shunt = next(record for record in records if record["role"] == "bus_shunt")
                self.assertEqual((shunt["gs_mw"], shunt["bs_mvar"]), tuple(map(float, columns.split())))
                self.assertEqual(shunt["model"], "constant_admittance")
                self.assertIn("New Reactor.", text)

    def test_matpower_phase_shift_has_explicit_port_mapped_equivalent(self):
        branch = _BRANCH.replace("100 100 100 0 0", "100 100 100 1.1 30")
        _, text = self.matpower_convert(_matpower(branch=branch))
        records = [json.loads(line.removeprefix("! [Tianshu Fidelity] "))
                   for line in text.splitlines() if line.startswith("! [Tianshu Fidelity] ")]
        mapped = next(record for record in records if record["role"] == "branch")
        self.assertEqual(mapped["shift_deg"], 30)
        self.assertEqual(mapped["tap_ratio"], 1.1)
        self.assertEqual(mapped["model"], "balanced_positive_sequence_phase_bank")
        self.assertEqual(len(mapped["series_elements"]), 7)
        self.assertEqual(len(mapped["ports_from"]), 6)
        self.assertEqual(len(mapped["ports_to"]), 1)

    def test_json_integer_id_above_binary64_exact_range_keeps_literal_identity(self):
        large_id = 9007199254740993
        _, text = self.json_convert(_network(slack=1, downstream=large_id))
        loads = _elements(text, "Load")
        lines = _elements(text, "Line")
        self.assertTrue(any(re.search(rf"\bBus1={large_id}(?:\.|\s|$)", line) for line in loads), text)
        self.assertTrue(any(re.search(rf"\bBus2={large_id}(?:\.|\s|$)", line) for line in lines), text)
        self.assertNotIn("9007199254740992", text)

    def test_failed_atomic_master_publication_preserves_old_bytes_and_metadata(self):
        original = _network()
        master, _ = self.json_convert(original, "atomic")
        old_bytes = master.read_bytes()
        meta = master.parent / ".conversion.json"
        old_meta = meta.read_bytes()
        changed = copy.deepcopy(original)
        changed["bus"][1]["p_kw"] = 500
        source = self.root / "atomic.json"
        source.write_text(json.dumps(changed), encoding="utf-8")
        actual_replace = os.replace
        writes_observed = []

        def fail_master_replace(src, dst):
            if Path(dst).resolve() == master.resolve():
                temporary = Path(src)
                self.assertTrue(temporary.is_file())
                self.assertNotEqual(temporary.resolve(), master.resolve())
                writes_observed.append(temporary.read_bytes())
                raise OSError(28, "explicit test publication failure after real temporary write")
            return actual_replace(src, dst)

        with patch.object(os, "replace", side_effect=fail_master_replace):
            with self.assertRaises(OSError):
                converters.convert_json(str(source), str(master.parent))
        self.assertEqual(master.read_bytes(), old_bytes)
        self.assertEqual(meta.read_bytes(), old_meta)
        self.assertEqual(len(writes_observed), 1)
        self.assertRegex(writes_observed[0], rb"\bkW=500(?:\.0+)?(?=\s|$)")
        (self.root / "fault_observation.json").write_text(json.dumps({
            "real_temp_file_write_observed": True, "publication_rejected": True,
            "old_master_bytes_preserved": True, "old_metadata_bytes_preserved": True,
        }, indent=2), encoding="utf-8")

    def test_finite_input_charging_conversion_overflow_is_rejected_before_publication(self):
        data = _network()
        master, _ = self.json_convert(data, "overflow")
        before_master = master.read_bytes()
        meta = master.parent / ".conversion.json"
        before_meta = meta.read_bytes()
        # The source number itself is finite. Its charging-to-capacitance unit
        # conversion at 20 kV/100 MVA exceeds binary64, and must not reach DSS.
        changed = copy.deepcopy(data)
        changed["line"][0]["b_pu"] = 1e308
        source = self.root / "overflow.json"
        source.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ValueError):
            converters.convert_json(str(source), str(master.parent))
        self.assertEqual(master.read_bytes(), before_master)
        self.assertEqual(meta.read_bytes(), before_meta)


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ConversionSemanticsTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
