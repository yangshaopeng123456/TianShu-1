"""2026-10-15 codex: fidelity contracts, independent electrical equations, no engine.

Real COM port-matrix evidence is a separate reproducible probe, not mocked here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import unittest

import numpy as np
import pandas as pd

from ..web import case_converters as converter


RUN = time.strftime("%Y%m%d_%H%M%S") + "_" + str(os.getpid())
DEFAULT = Path(tempfile.gettempdir()) / "tianshu-tests"


def network():
    return {"system": {"base_mva": 100, "frequency_hz": 50},
            "bus": [{"bus_id": "1", "type": "slack", "base_kv": 20, "gs_mw": .2, "bs_mvar": -.3},
                    {"bus_id": "2", "type": "pq", "base_kv": 10, "p_load_mw": .1, "q_load_mvar": .02}],
            "line": [{"line_id": "L1", "from_bus": "1", "to_bus": "2", "kind": "transformer",
                      "r_pu": .01, "x_pu": .05, "b_pu": .02, "tap_ratio": 1.1, "shift_deg": 17.5}]}


def records(text, role=None):
    values = [json.loads(line.removeprefix("! [Tianshu Fidelity] ")) for line in text.splitlines()
              if line.startswith("! [Tianshu Fidelity] ")]
    return [row for row in values if role is None or row["role"] == role]


def attributes(line, name):
    return float(re.search(rf"(?i)\b{re.escape(name)}=([-+\deE.]+)", line).group(1))


class ConversionFidelityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ.get("DPF_TEST_ARTIFACT_ROOT", DEFAULT)) / "unit_fidelity" / RUN / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=False)

    def convert(self, data, fmt="json", label="input"):
        out = self.root / (label + "_out")
        if fmt == "json":
            source = self.root / (label + ".json")
            source.write_text(json.dumps(data), encoding="utf-8")
            master = converter.convert_json(str(source), str(out))
        elif fmt == "xlsx":
            source = self.root / (label + ".xlsx")
            with pd.ExcelWriter(source, engine="openpyxl") as writer:
                for name, value in data.items():
                    pd.DataFrame([value] if isinstance(value, dict) else value).to_excel(writer, sheet_name=name, index=False)
            master = converter.convert_xlsx(str(source), str(out))
        elif fmt == "csv":
            source = self.root / label
            source.mkdir()
            for name, value in data.items():
                pd.DataFrame([value] if isinstance(value, dict) else value).to_csv(source / (name + ".csv"), index=False)
            master = converter.convert_csv(str(source), str(out))
        else:
            raise AssertionError(fmt)
        return Path(master).read_text(encoding="utf-8")

    def matpower(self, *, gs=.2, bs=-.3, shift=17.5, tap=1.1, gen=None):
        source = self.root / "case.m"
        source.write_text("function mpc=fidelity\nmpc.version='2';mpc.baseMVA=100;\n"
                          f"mpc.bus=[1 3 0 0 {gs} {bs} 1 1 0 20 1 1.1 .9;2 1 .1 .02 0 0 1 1 0 10 1 1.1 .9];\n"
                          f"mpc.gen=[{gen or '1 1 0 10 -10 1 100 1 10 0'}];\n"
                          f"mpc.branch=[1 2 .01 .05 .02 55 55 55 {tap} {shift} 1 -360 360];\n", encoding="utf-8")
        return Path(converter.convert_matpower_m(str(source), str(self.root / "m_out"))).read_text(encoding="utf-8")

    def test_json_xlsx_csv_and_matpower_have_same_physical_features(self):
        all_records = [records(self.convert(network(), fmt, fmt)) for fmt in ("json", "xlsx", "csv")]
        all_records.append(records(self.matpower()))
        for values in all_records:
            shunt = next(row for row in values if row["role"] == "bus_shunt")
            branch = next(row for row in values if row["role"] == "branch")
            self.assertEqual((shunt["gs_mw"], shunt["bs_mvar"]), (.2, -.3))
            self.assertEqual((branch["b_pu"], branch["tap_ratio"], branch["shift_deg"]), (.02, 1.1, 17.5))
            self.assertEqual(branch["model"], "balanced_positive_sequence_phase_bank")

    def test_bus_shunt_is_impedance_not_a_scheduled_load(self):
        text = self.convert(network())
        shunt = records(text, "bus_shunt")[0]
        line = next(line for line in text.splitlines() if line.startswith("New " + shunt["elements"][0] + " "))
        z = complex(attributes(line, "R"), attributes(line, "X"))
        self.assertAlmostEqual((1/z).real, .2/20**2)
        self.assertAlmostEqual((1/z).imag, -.3/20**2)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("New Load.")]), 1)

    def test_shunt_scales_with_voltage_squared_and_signs(self):
        for gs, bs in ((1, 2), (-1, -2), (0, 2), (1, 0)):
            data = network(); data["bus"][0].update(gs_mw=gs, bs_mvar=bs)
            text = self.convert(data, label=f"s{gs}_{bs}")
            name = records(text, "bus_shunt")[0]["elements"][0]
            line = next(line for line in text.splitlines() if line.startswith("New " + name + " "))
            z = complex(attributes(line, "R"), attributes(line, "X"))
            for pu in (.9, 1, 1.1):
                actual = 20**2 * pu**2 * np.conj(1/z)
                self.assertAlmostEqual(actual.real, gs*pu**2)
                self.assertAlmostEqual(actual.imag, -bs*pu**2)

    def test_transformer_charging_has_from_tap_squared_scaling(self):
        data = network(); data["line"][0]["shift_deg"] = 0
        text = self.convert(data)
        mapped = records(text, "branch")[0]
        self.assertEqual(mapped["model"], "two_winding_transformer")
        self.assertEqual(mapped["series_elements"], ["Transformer.L1"])
        for i, (kv, expected) in enumerate(((20, .02/2/1.1**2), (10, .02/2))):
            name = mapped["shunt_elements"][i]
            line = next(line for line in text.splitlines() if line.startswith("New " + name + " "))
            actual = (1/complex(attributes(line, "R"), attributes(line, "X"))).imag*kv**2/100
            self.assertAlmostEqual(actual, expected)
        self.assertNotIn("未映射到励磁", text)

    def test_phase_bank_real_matrix_has_required_complex_eigenvalue_and_power_transpose(self):
        seq = np.exp(-2j*np.pi/3*np.arange(3))
        for theta in (-179, -120, -30, -17.5, -1e-6, 1e-6, 17.5, 90, 180, 360):
            radians = math.radians(math.remainder(theta, 360))
            a = math.cos(radians)+math.sin(radians)/math.sqrt(3)
            b = 2*math.sin(radians)/math.sqrt(3)
            matrix = a*np.eye(3)+b*np.roll(np.eye(3), 1, axis=1)
            for tap in (.9, 1.1):
                ratio = np.exp(-1j*radians)/tap
                np.testing.assert_allclose(matrix@seq/tap, ratio*seq, atol=1e-14)
                v = seq*(1+.1j); current = seq*(.3-.2j)
                vin_power = np.sum(v*np.conj(matrix.T@current/tap))
                vout_power = np.sum((matrix@v/tap)*np.conj(current))
                self.assertAlmostEqual(abs(vin_power-vout_power), 0, places=12)

    def test_phase_bank_ports_include_all_native_inputs_and_output(self):
        mapped = records(self.convert(network()), "branch")[0]
        self.assertEqual(len(mapped["series_elements"]), 7)
        self.assertEqual(len(mapped["ports_from"]), 7)  # Six coils plus from charging.
        self.assertEqual(len(mapped["ports_to"]), 2)
        self.assertTrue(all(p["terminal"] == 1 for p in mapped["ports_from"]))
        self.assertEqual(mapped["ports_to"][0]["terminal"], 2)
        self.assertEqual(len(mapped["internal_buses"]), 1)
        self.assertIsNone(mapped["input_rate_mva"])
        self.assertEqual(mapped["rating_source"], "not_declared")

    def test_native_coil_basis_avoids_tiny_secondaries_at_small_or_special_angles(self):
        seq = np.exp(-2j*np.pi/3*np.arange(3))
        for theta in (-1e-12, 1e-12, -1e-6, 1e-6, 0, 30, -30, 120, -120, 180, 360, 1e20):
            with self.subTest(theta=theta):
                parts = converter._phase_bank_coefficients(theta)
                self.assertGreaterEqual(min(abs(c) for _, c in parts), .57)
                self.assertLessEqual(max(abs(c) for _, c in parts), 1.16)
                actual = sum(c*seq[offset] for offset, c in parts)
                expected = np.exp(-1j*math.radians(math.remainder(theta, 360)))
                self.assertAlmostEqual(abs(actual-expected), 0, places=12)

    def test_explicit_rating_is_thermal_provenance_not_impedance_base(self):
        data = network(); data["line"][0]["rate_mva"] = 55
        mapped = records(self.convert(data), "branch")[0]
        self.assertEqual(mapped["input_rate_mva"], 55)
        self.assertEqual(mapped["kva_provenance"], "system_impedance_base_not_nameplate")

    def test_synthetic_names_do_not_overwrite_user_bus_or_branch(self):
        data = network(); data["bus"][1]["bus_id"] = "_ts_phase1"
        data["line"][0].update(to_bus="_ts_phase1", line_id="_ts_winding1")
        mapped = records(self.convert(data), "branch")[0]
        self.assertNotIn("_ts_phase1", mapped["internal_buses"])
        self.assertNotIn("Transformer._ts_winding1", mapped["series_elements"])

    def test_disabled_phase_branch_disables_every_component(self):
        data = network(); data["line"][0]["status"] = 0
        # Keep an independent active path to satisfy source connectivity.
        line = copy.deepcopy(data["line"][0]); line.update(line_id="L2", status=1, shift_deg=0)
        data["line"].append(line)
        text = self.convert(data); mapped = records(text, "branch")[0]
        self.assertFalse(mapped["enabled"])
        for element in mapped["series_elements"]+mapped["shunt_elements"]:
            self.assertIn("Enabled=no", next(line for line in text.splitlines() if line.startswith("New " + element + " ")))

    def test_phase_bank_rejects_unbalanced_input_in_all_tables(self):
        for section in ("bus", "line", "load"):
            data = network()
            if section == "load": data["load"] = [{"bus_id": "2", "p_kw": 1, "phase": "A"}]
            else: data[section][-1]["phase"] = "A"
            with self.subTest(section=section), self.assertRaisesRegex(ValueError, "平衡正序"):
                self.convert(data, label=section)

    def test_phase_bank_rejects_unproved_delta_and_zero_sequence(self):
        for field, value in (("conn1", "delta"), ("r0_pu", .01)):
            data = network(); data["line"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "相移|shift_deg"):
                self.convert(data, label=field)

    def test_pure_line_cannot_silently_gain_phase_transformer(self):
        data = network(); data["line"][0]["kind"] = "line"
        with self.assertRaisesRegex(ValueError, "transformer"):
            self.convert(data)

    def test_zero_shunts_and_no_shift_do_not_create_synthetic_inventory(self):
        data = network(); data["bus"][0].update(gs_mw=0, bs_mvar=0)
        data["line"][0].update(b_pu=0, shift_deg=0)
        mapped = records(self.convert(data), "branch")[0]
        self.assertEqual(mapped["series_elements"], ["Transformer.L1"])
        self.assertEqual(mapped["internal_buses"], [])
        self.assertEqual(mapped["shunt_elements"], [])
        self.assertIsNone(mapped["input_rate_mva"])

    def test_shunt_units_must_agree_across_formats(self):
        for fmt in ("json", "xlsx", "csv"):
            data = network(); data["bus"][0]["gs_kw"] = 999
            with self.subTest(fmt=fmt), self.assertRaisesRegex(ValueError, "冲突"):
                self.convert(data, fmt, fmt)

    def test_new_numeric_fields_reject_bool_bad_text_and_nonfinite(self):
        for field in ("gs_mw", "bs_mvar", "gen_rating_mva", "qmin_kvar"):
            for i, value in enumerate((True, "bad", float("inf"), "NaN")):
                data = network(); data["bus"][1][field] = value
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    self.convert(data, label=f"{field}{i}")

    def pv(self):
        data = network(); data["line"][0].update(shift_deg=0, b_pu=0)
        data["bus"][1].update(type="pv", p_gen_kw=100, q_gen_kvar=0, v_set_pu=1.001)
        return data

    def test_nonzero_pv_gets_explicit_numeric_q_bounds_without_native_defaults(self):
        text = self.convert(self.pv()); policy = records(text, "generator")[0]
        self.assertEqual(policy["native_minkvar"], -100e6)
        self.assertEqual(policy["native_maxkvar"], 100e6)
        self.assertIsNone(policy["explicit_rating_kva"])
        self.assertEqual(policy["kva_provenance"], "numeric_base_not_equipment_rating")

    def test_strict_q_requires_complete_bounds_in_all_table_formats(self):
        for fmt in ("json", "xlsx", "csv"):
            for fields in ({}, {"qmin_mvar": -1}, {"qmax_mvar": 1}):
                data = self.pv(); data["system"]["ENFORCE_Q_LIMITS"] = 1; data["bus"][1].update(fields)
                with self.subTest(fmt=fmt, fields=fields), self.assertRaisesRegex(ValueError, "完整.*Qmin/Qmax"):
                    self.convert(data, fmt, f"{fmt}{len(fields)}{'max' if 'qmax_mvar' in fields else 'min'}")

    def test_strict_q_preserves_zero_and_asymmetric_limits(self):
        data = self.pv(); data["system"]["ENFORCE_Q_LIMITS"] = True
        data["bus"][1].update(qmin_mvar=0, qmax_mvar=.003)
        policy = records(self.convert(data), "generator")[0]
        self.assertEqual((policy["native_minkvar"], policy["native_maxkvar"]), (0, 3))

    def test_q_kvar_aliases_conflicts_and_missing_are_not_defaults(self):
        data = self.pv(); data["system"]["ENFORCE_Q_LIMITS"] = True
        data["bus"][1].update(qmin_kvar=-1, qmax_kvar=2)
        policy = records(self.convert(data), "generator")[0]
        self.assertEqual((policy["native_minkvar"], policy["native_maxkvar"]), (-1, 2))
        data["bus"][1]["qmax_mvar"] = 3
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.convert(data, label="conflict")

    def test_real_nameplate_is_never_widened_by_reference_mode(self):
        for enforce in (False, True):
            data = self.pv(); data["system"]["ENFORCE_Q_LIMITS"] = enforce
            data["bus"][1].update(p_gen_kw=80, gen_rating_kva=100, qmin_mvar=-10, qmax_mvar=10)
            policy = records(self.convert(data, label=str(enforce)), "generator")[0]
            self.assertAlmostEqual(policy["native_minkvar"], -60)
            self.assertAlmostEqual(policy["native_maxkvar"], 60)
            self.assertEqual(policy["kva_provenance"], "explicit_nameplate")

    def test_p_above_nameplate_and_infeasible_q_are_explicit_rejections(self):
        data = self.pv(); data["bus"][1]["gen_rating_kva"] = 50
        with self.assertRaisesRegex(ValueError, "有功超出"):
            self.convert(data)
        data["bus"][1].update(gen_rating_kva=100, p_gen_kw=80, qmin_mvar=.1, qmax_mvar=.2)
        data["system"]["ENFORCE_Q_LIMITS"] = 1
        with self.assertRaisesRegex(ValueError, "可行交集"):
            self.convert(data, label="infeasible")

    def test_slack_device_constraints_are_rejected_instead_of_silently_ignored(self):
        for field, value, enforce in (("gen_rating_mva", 1, False), ("qmax_mvar", 1, True)):
            data = network(); data["bus"][0][field] = value
            data["system"]["ENFORCE_Q_LIMITS"] = enforce
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "平衡|平衡电源"):
                self.convert(data, label=field)

    def test_fixed_pq_must_satisfy_enforced_q_bounds(self):
        data = self.pv(); data["bus"][1].update(type="pq", q_gen_kvar=5, qmin_mvar=-.001, qmax_mvar=.003)
        data["system"]["ENFORCE_Q_LIMITS"] = True
        with self.assertRaisesRegex(ValueError, "固定 Q"):
            self.convert(data)

    def test_small_nonzero_q_bounds_keep_numeric_identity(self):
        data = self.pv(); data["bus"][1].update(qmin_kvar=-1e-9, qmax_kvar=2e-9)
        data["system"]["ENFORCE_Q_LIMITS"] = True
        text = self.convert(data)
        gen = next(line for line in text.splitlines() if line.startswith("New Generator."))
        self.assertAlmostEqual(attributes(gen, "minkvar"), -1e-9, places=20)
        self.assertAlmostEqual(attributes(gen, "maxkvar"), 2e-9, places=20)

    def test_matpower_mbase_is_not_nameplate(self):
        text = self.matpower(gen="1 1 0 10 -10 1 100 1 10 0;2 .1 0 10 -10 1.001 .05 1 10 0")
        # Bus 2 is PQ: a low mBase is never silently treated as a physical S limit.
        self.assertIn("不作为设备额定容量", text)

    def test_nonzero_pq_capability_curve_cannot_be_silently_rectangularized(self):
        with self.assertRaisesRegex(ValueError, "P-Q 能力曲线"):
            self.matpower(gen="1 1 0 10 -10 1 100 1 10 0 1 2 -1 1 -1 1 0 0 0 0 0")

    def test_invalid_feature_keeps_published_master_and_metadata(self):
        data = network(); self.convert(data)
        master = self.root / "input_out/Master.dss"; meta = master.parent / ".conversion.json"
        old_master, old_meta = master.read_bytes(), meta.read_bytes()
        data["bus"][0]["gs_mw"] = True
        with self.assertRaises(ValueError): self.convert(data)
        self.assertEqual(master.read_bytes(), old_master)
        self.assertEqual(meta.read_bytes(), old_meta)

    def test_fidelity_manifest_is_before_circuit_and_recomputable(self):
        text = self.convert(network())
        values = [line.removeprefix("! [Tianshu Fidelity] ") for line in text.splitlines()
                  if line.startswith("! [Tianshu Fidelity] ")]
        manifest = re.search(r"fidelity_records=(\d+); fidelity_sha256=([a-f0-9]{64})", text)
        self.assertLess(manifest.start(), text.index("New Circuit."))
        self.assertEqual(int(manifest[1]), len(values))
        self.assertEqual(manifest[2], hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest())
        for changed in (values[1:], [values[0].replace('"shift_deg":17.5', '"shift_deg":17.6')]+values[1:]):
            self.assertNotEqual(manifest[2], hashlib.sha256("\n".join(changed).encode("utf-8")).hexdigest())

    def test_empty_fidelity_manifest_has_sha256_of_empty_bytes(self):
        data = network(); data["bus"][0].update(gs_mw=0, bs_mvar=0)
        data["line"][0].update(shift_deg=0, b_pu=0)
        data["bus"][1]["base_kv"] = 20
        data["line"][0].update(kind="line", tap_ratio=1)
        text = self.convert(data)
        self.assertIn("fidelity_records=0; fidelity_sha256="+hashlib.sha256(b"").hexdigest(), text)

    def test_q_policy_warning_describes_actual_choice_and_rating_provenance(self):
        for enforce in (False, True):
            data = self.pv(); data["system"]["ENFORCE_Q_LIMITS"] = enforce
            data["bus"][1].update(qmin_mvar=-1, qmax_mvar=1)
            text = self.convert(data, label=str(enforce))
            warning = next(line for line in text.splitlines() if line.startswith("! [Tianshu Conversion] warning="))
            self.assertIn("强制策略" if enforce else "参考非强制", warning)
            self.assertIn("容量圆", warning)
            self.assertNotIn("PV/PQ 无功限值", warning)

    def test_high_precision_bases_tap_frequency_and_vpu_are_not_rounded(self):
        data = network()
        kv1, kv2, tap, frequency, vpu = 20.12345678912345, 10.98765432198765, 1.123456789, 50.12345678912345, 1.012345678912345
        data["bus"][0].update(base_kv=kv1, v_set_pu=vpu)
        data["bus"][1]["base_kv"] = kv2
        data["line"][0].update(shift_deg=0, tap_ratio=tap)
        data["system"]["frequency_hz"] = frequency
        text = self.convert(data)
        circuit = next(line for line in text.splitlines() if line.startswith("New Circuit."))
        xfmr = next(line for line in text.splitlines() if line.startswith("New Transformer."))
        self.assertEqual(attributes(circuit, "basekv"), kv1)
        self.assertEqual(attributes(circuit, "pu"), vpu)
        self.assertEqual(attributes(xfmr, "tap"), tap)
        self.assertEqual(attributes(xfmr, "maxtap"), tap)
        kvs = [float(value) for value in re.search(r"kvs=\[([^\]]+)\]", xfmr)[1].split()]
        self.assertEqual(kvs, [kv1, kv2])
        frequency_line = next(line for line in text.splitlines() if line.startswith("Set DefaultBaseFrequency="))
        self.assertEqual(attributes(frequency_line, "DefaultBaseFrequency"), frequency)

    def test_small_nonzero_power_and_impedance_are_not_serialized_as_zero(self):
        data = network(); data["bus"][0].update(gs_mw=0, bs_mvar=0)
        data["bus"][1].update(base_kv=20, p_load_mw=1e-12, q_load_mvar=2e-12)
        data["line"][0].update(kind="line", r_pu=1e-10, x_pu=2e-10, tap_ratio=1, shift_deg=0, b_pu=0)
        text = self.convert(data)
        line = next(line for line in text.splitlines() if line.startswith("New Line."))
        load = next(line for line in text.splitlines() if line.startswith("New Load."))
        self.assertEqual(attributes(line, "R1"), 4e-10)
        self.assertEqual(attributes(line, "X1"), 8e-10)
        self.assertEqual(attributes(load, "kW"), 1e-9)
        self.assertEqual(attributes(load, "kvar"), 2e-9)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ConversionFidelityTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
