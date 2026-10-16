"""Offline source-object restoration; actual native equivalence is tested separately."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from ..config import SimDefaults
from ..domain.network_profile import build_network_profile
from ..domain.result_normalizer import normalize_powerflow_result
from ..engines.circuit_inventory import CircuitInventory
from ..engines.fidelity_mapping import (MARKER, apply_fidelity_mapping, measured_terminal_reader,
                                       read_fidelity_mapping, restore_summary)
from ..engines.opendss_adapter import AdapterError, OpenDSSRawResult, SolverExecutionResult
from ..schemas.enums import ResultScope
from ..verification.verifier import Verifier
from ..web.case_converters import convert_json


BODY = """New Circuit.test bus1=sourcebus
New Transformer.w1 windings=2 buses=[sourcebus.1.0 _ts_internal.1.0]
New Transformer.w2 windings=2 buses=[sourcebus.2.0 _ts_internal.2.0]
New Reactor.series bus1=_ts_internal.1.2.3 bus2=b2.1.2.3
New Reactor.charging bus1=b2.1.2.3
New Reactor.shunt bus1=b2.1.2.3
"""


def branch():
    return dict(role="branch", id="L1", original_kind="transformer", from_bus="sourcebus", to_bus="b2",
                series_elements=["Transformer.w1", "Transformer.w2", "Reactor.series"],
                shunt_elements=["Reactor.charging"], internal_buses=["_ts_internal"], enabled=True,
                ports_from=[dict(element="Transformer.w1", terminal=1), dict(element="Transformer.w2", terminal=1)],
                ports_to=[dict(element="Reactor.series", terminal=2), dict(element="Reactor.charging", terminal=1)],
                model="balanced_positive_sequence_phase_bank", input_rate_mva=2)


def raw():
    return OpenDSSRawResult(buses=[dict(name=n, voltage_pu=[1.0], angle_deg=[0.0], base_kv=20) for n in
                                  ("sourcebus", "b2", "_ts_internal")],
        transformers=[dict(name="Transformer.w1", p_kw=0., q_kvar=0.), dict(name="Transformer.w2", p_kw=0., q_kvar=0.)],
        system=dict(total_power_kw=100.0, total_power_kvar=0., losses_kw=5., losses_kvar=2.,
                    load_kw=85., load_kvar=0., gen_kw=0., gen_kvar=0.),
        element_count=dict(bus=3, line=0, transformer=2, load=1))


class FidelityMappingTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("DPF_TEST_ARTIFACT_ROOT")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="fidelity-mapping-", dir=base))

    def write(self, records, text="", *, body=BODY):
        payloads = [json.dumps(r) for r in records]
        digest = hashlib.sha256("\n".join(payloads).encode()).hexdigest()
        manifest = f"! [Tianshu Conversion] fidelity_records={len(payloads)}; fidelity_sha256={digest}"
        (self.root / "Master.dss").write_text(manifest + "\n" + body + text + "\n" + "\n".join(MARKER + payload for payload in payloads), encoding="utf-8")
        return read_fidelity_mapping(str(self.root))

    def test_source_identity_endpoints_and_real_rating_replace_synthetic_devices(self):
        records = self.write([branch()])
        values = {("Transformer.w1", 1): (60., 2.), ("Transformer.w2", 1): (40., 3.),
                  ("Reactor.series", 2): (-95., -7.), ("Reactor.charging", 1): (0., -1.)}
        result = apply_fidelity_mapping(raw(), records, lambda e, t: values[(e, t)], list(ResultScope))
        self.assertEqual([b["name"] for b in result.buses], ["sourcebus", "b2"])
        self.assertEqual(result.element_count, dict(bus=2, line=0, transformer=1, load=1))
        self.assertEqual(len(result.transformers), 1)
        tr = result.transformers[0]
        self.assertEqual((tr["name"], tr["bus1"], tr["bus2"], tr["p_kw"], tr["p_to_kw"]),
                         ("Transformer.L1", "sourcebus", "b2", 100., -95.))
        self.assertEqual(tr["rated_kva"], 2000.)
        self.assertEqual(result.system["branch_shunt_kvar"], -1.)

    def test_numerical_system_base_is_never_reported_as_nameplate(self):
        record = branch()
        record["input_rate_mva"] = None
        result = apply_fidelity_mapping(raw(), self.write([record]), lambda e, t: (0., 0.), list(ResultScope))
        self.assertIsNone(result.transformers[0]["rated_kva"])

    def test_positive_and_negative_shunt_remain_separate_from_load_and_native_loss(self):
        for power in (10., -10.):
            with self.subTest(power=power):
                value = raw()
                value.system["total_power_kw"] = 90. + power
                records = self.write([dict(role="bus_shunt", bus="b2", elements=["Reactor.shunt"])])
                apply_fidelity_mapping(value, records, lambda e, t: (power, -2.), list(ResultScope))
                result = normalize_powerflow_result(value, SolverExecutionResult(True, 2, False), "t", "fp", "sp", "eng", "adapter", list(ResultScope))
                self.assertEqual(result.system_summary.total_load_kw, 85.)
                self.assertEqual(result.system_summary.total_shunt_kw, power)
                self.assertEqual(result.loss_summary.total_loss_kw, 5.)
                case = SimpleNamespace(case_fingerprint="fp", dependency_manifest=SimpleNamespace(file_count=1),
                                       object_summary={}, fidelity_status="warning", source_format="json")
                verification = Verifier(SimDefaults()).verify(case, result, None, [])
                check = next(c for c in verification.checks if c.check_id == "power_balance")
                self.assertTrue(check.passed)
                result.system_summary.total_shunt_kw = 0.
                verification = Verifier(SimDefaults()).verify(case, result, None, [])
                self.assertFalse(next(c for c in verification.checks if c.check_id == "power_balance").passed)

    def test_missing_or_nonfinite_port_fails_instead_of_publishing_partial_sums(self):
        for values in (None, (float("nan"), 0.), (0., float("inf"))):
            with self.subTest(values=values), self.assertRaises(AdapterError):
                apply_fidelity_mapping(raw(), self.write([branch()]), lambda e, t: values, list(ResultScope))

    def test_conflicting_metadata_and_duplicate_ports_are_rejected(self):
        duplicate = branch()
        duplicate["ports_from"].append(copy.deepcopy(duplicate["ports_from"][0]))
        with self.assertRaises(AdapterError):
            self.write([duplicate])
        conflict = branch()
        conflict["to_bus"] = "other"
        with self.assertRaises(AdapterError):
            self.write([branch(), conflict])

    def test_malformed_metadata_is_not_silently_ignored(self):
        for suffix in ("{broken", "[]", '{"role":"unknown"}'):
            (self.root / "Master.dss").write_text(MARKER + suffix, encoding="utf-8")
            with self.assertRaises(AdapterError):
                read_fidelity_mapping(str(self.root))

    def test_per_terminal_power_includes_both_non_ground_conductors(self):
        element = SimpleNamespace(Name="Transformer.w1", NumConductors=2, NumTerminals=2,
                                  Powers=[10., 2., 5., 3., -14., -4., -0.5, -0.5])
        ckt = SimpleNamespace(SetActiveElement=lambda name: 1, ActiveCktElement=element)
        reader = measured_terminal_reader(ckt)
        self.assertEqual(reader("Transformer.w1", 1), (15., 5.))
        self.assertEqual(reader("Transformer.w1", 2), (-14.5, -4.5))
        with self.assertRaises(AdapterError):
            reader("Transformer.w1", 3)

    def test_wrong_active_element_identity_is_rejected(self):
        ckt = SimpleNamespace(SetActiveElement=lambda name: -1, ActiveCktElement=SimpleNamespace(Name="old"))
        with self.assertRaises(AdapterError):
            measured_terminal_reader(ckt)("Transformer.w1", 1)

    def test_summary_and_network_profile_restore_original_bus_branch_counts(self):
        records = self.write([branch()])
        counts, bases = restore_summary(dict(bus=3, line=0, transformer=2), dict(sourcebus=10., b2=10., _ts_internal=10.), records)
        self.assertEqual(counts, dict(bus=2, line=0, transformer=1, active_transformer=1))
        self.assertNotIn("_ts_internal", bases)
        inventory = CircuitInventory(3, 0, 2, 0, 0, 0, 0, 0, 0,
                    (("sourcebus", 10.), ("b2", 10.), ("_ts_internal", 10.)), (), ())
        profile = build_network_profile("test", inventory, str(self.root))
        self.assertEqual((profile.bus_count, profile.transformer_count, profile.active_branch_count), (2, 1, 1))
        self.assertTrue(profile.is_radial)

    def test_source_port_and_shunt_changes_participate_in_independent_recompute(self):
        value = raw()
        apply_fidelity_mapping(value, self.write([branch()]), lambda e, t: (0., 0.), list(ResultScope))
        first = normalize_powerflow_result(value, SolverExecutionResult(True, 2, False), "t", "fp", "sp", "eng", "adapter", list(ResultScope))
        second = first.model_copy(deep=True)
        second.transformer_results[0].p_to_kw = 10.
        self.assertFalse(Verifier(SimDefaults()).check_reproducibility(first, second).passed)

    def test_deleted_one_or_all_records_fail_against_declared_count_and_hash(self):
        records = [branch(), dict(role="bus_shunt", bus="b2", elements=["Reactor.shunt"])]
        self.write(records)
        path = self.root / "Master.dss"
        original = path.read_text(encoding="utf-8")
        for delete_all in (False, True):
            with self.subTest(delete_all=delete_all):
                lines, removed = [], False
                for line in original.splitlines():
                    if line.startswith(MARKER) and (delete_all or not removed):
                        removed = True
                        continue
                    lines.append(line)
                path.write_text("\n".join(lines), encoding="utf-8")
                with self.assertRaisesRegex(AdapterError, "数量或哈希"):
                    read_fidelity_mapping(str(self.root))

    def test_changed_record_json_fails_hash_even_when_number_is_unchanged(self):
        self.write([branch()])
        path = self.root / "Master.dss"
        path.write_text(path.read_text().replace('"L1"', '"renamed_source"'))
        with self.assertRaisesRegex(AdapterError, "数量或哈希"):
            read_fidelity_mapping(str(self.root))

    def test_metadata_record_without_manifest_and_synthetic_without_all_markers_fail_closed(self):
        self.write([branch()])
        path = self.root / "Master.dss"
        original = path.read_text()
        path.write_text("\n".join(line for line in original.splitlines() if "fidelity_records=" not in line))
        with self.assertRaisesRegex(AdapterError, "完整性声明"):
            read_fidelity_mapping(str(self.root))
        path.write_text("New Transformer._ts_winding1 buses=[sourcebus b2]\n")
        with self.assertRaisesRegex(AdapterError, "完整性声明"):
            read_fidelity_mapping(str(self.root))

    def test_each_dss_file_must_have_its_own_declared_record_manifest(self):
        self.write([branch()])
        (self.root / "extra.dss").write_text(MARKER + json.dumps(dict(role="bus_shunt", bus="b2", elements=["Reactor.shunt"])))
        with self.assertRaisesRegex(AdapterError, "extra.dss.*完整性声明"):
            read_fidelity_mapping(str(self.root))

    def test_empty_manifest_and_ordinary_native_without_mapping_remain_valid(self):
        self.assertEqual(self.write([]), [])
        (self.root / "Master.dss").write_text("New Circuit.native\nNew Transformer.ordinary like=Existing\n")
        self.assertEqual(read_fidelity_mapping(str(self.root)), [])

    def test_changed_endpoint_or_swapped_terminal_rejected_even_with_valid_hash(self):
        wrong_bus = branch()
        wrong_bus["from_bus"] = "wrong_source"
        with self.assertRaisesRegex(AdapterError, "端口母线"):
            self.write([wrong_bus])
        swapped = branch()
        swapped["ports_from"][0]["terminal"] = 2
        with self.assertRaisesRegex(AdapterError, "端口母线"):
            self.write([swapped])

    def test_bus_shunt_endpoint_is_checked_against_native_port_one(self):
        with self.assertRaisesRegex(AdapterError, "端口母线"):
            self.write([dict(role="bus_shunt", bus="sourcebus", elements=["Reactor.shunt"])])

    def test_missing_native_element_is_rejected_before_any_measurement(self):
        body = "\n".join(line for line in BODY.splitlines() if "New Reactor.series " not in line)
        with self.assertRaisesRegex(AdapterError, "源体中缺失"):
            self.write([branch()], body=body)

    def test_shared_cross_side_port_and_cross_object_element_are_rejected(self):
        shared = branch()
        shared["ports_to"] = copy.deepcopy(shared["ports_from"])
        with self.assertRaisesRegex(AdapterError, "两端口重复"):
            self.write([shared])
        duplicate_owner = branch()
        duplicate_owner["id"] = "different_source"
        with self.assertRaisesRegex(AdapterError, "重复归属"):
            self.write([branch(), duplicate_owner])

    def test_duplicate_identical_source_records_are_not_hidden_by_deduplication(self):
        with self.assertRaisesRegex(AdapterError, "重复或冲突"):
            self.write([branch(), branch()])

    def test_same_file_new_edit_and_winding_order_preserve_final_port_identity(self):
        records = self.write([branch()], "Edit Transformer.w1 wdg=1 bus=wrong\nEdit Transformer.w1 wdg=1 bus=SOURCEBUS.3.0\n")
        self.assertEqual(records[0]["from_bus"], "sourcebus")
        with self.assertRaisesRegex(AdapterError, "端口母线"):
            self.write([branch()], "Edit Transformer.w1 wdg=1 bus=wrong\n")

    def test_unsupported_inheritance_and_batch_edit_are_explicitly_rejected(self):
        for command in ("Edit Transformer.w1 Like=base\n", "BatchEdit Transformer..* Enabled=no\n"):
            with self.subTest(command=command), self.assertRaisesRegex(AdapterError, "Like|BatchEdit"):
                self.write([branch()], command)

    def test_cross_file_edit_order_is_rejected_instead_of_guessed(self):
        self.write([branch()])
        (self.root / "z_override.dss").write_text("Edit Transformer.w1 wdg=1 bus=sourcebus\n")
        with self.assertRaisesRegex(AdapterError, "跨文件 Edit"):
            read_fidelity_mapping(str(self.root))

    def test_source_enabled_state_cannot_disagree_with_metadata(self):
        with self.assertRaisesRegex(AdapterError, "Enabled.*不一致"):
            self.write([branch()], "Edit Transformer.w1 Enabled=no\n")

    def test_disabled_internal_bus_absent_in_native_observation_is_not_subtracted(self):
        record = branch()
        record["enabled"] = False
        edits = "\n".join("Edit " + name + " Enabled=no" for name in record["series_elements"] + record["shunt_elements"]) + "\n"
        records = self.write([record], edits)
        value = raw()
        value.buses = value.buses[:2]
        value.element_count.update(bus=2, transformer=0)
        apply_fidelity_mapping(value, records, lambda *_: self.fail("inactive port should not be measured"), [])
        self.assertEqual(value.element_count["bus"], 2)
        counts, bases = restore_summary(dict(bus=2, line=0, transformer=0), dict(sourcebus=10., b2=10.), records)
        self.assertEqual(counts["bus"], 2)
        self.assertEqual(counts["transformer"], 1)
        self.assertEqual(counts["active_transformer"], 0)

    def test_negative_loss_roundoff_has_absolute_boundary_and_does_not_hide_real_negative_loss(self):
        tolerance = SimDefaults().loss_roundoff_kw
        self.assertEqual(tolerance, 1e-3)
        for loss, accepted in ((-tolerance, True), (-tolerance + 1e-9, True),
                               (-tolerance - 1e-9, False), (tolerance, True), (-1., False)):
            with self.subTest(loss=loss):
                value = raw()
                value.system.update(losses_kw=loss, total_power_kw=85. + loss)
                result = normalize_powerflow_result(value, SolverExecutionResult(True, 2, False), "t", "fp", "sp", "eng", "adapter", [])
                self.assertEqual(result.loss_summary.total_loss_kw, loss)
                case = SimpleNamespace(case_fingerprint="fp", dependency_manifest=SimpleNamespace(file_count=1),
                                       object_summary={}, fidelity_status="native", source_format="dss")
                checks = Verifier(SimDefaults()).verify(case, result, None, []).checks
                self.assertEqual(next(check for check in checks if check.check_id == "physical_sanity").passed, accepted)

    def test_real_converter_transformer_bodies_and_manifests_pass_strict_reader(self):
        for label, shift, charging in (("ordinary", 0, 0), ("charging", 0, .02), ("phase_bank", 17.5, .02)):
            with self.subTest(label=label):
                directory = self.root / label
                directory.mkdir()
                data = {"bus": [{"bus_id": "B1", "type": "slack", "base_kv": 20, "gs_mw": .2, "bs_mvar": -.3},
                                {"bus_id": "B2", "type": "pq", "base_kv": 10, "p_kw": 100}],
                        "line": [{"line_id": "T1", "from_bus": "B1", "to_bus": "B2", "kind": "transformer",
                                  "r_pu": .01, "x_pu": .05, "tap_ratio": 1.1, "shift_deg": shift, "b_pu": charging}]}
                source = directory / "source.json"
                source.write_text(json.dumps(data), encoding="utf-8")
                master = Path(convert_json(str(source), str(directory / "model")))
                parsed = read_fidelity_mapping(str(master.parent))
                self.assertEqual(len([r for r in parsed if r["role"] == "branch"]), 1)
                self.assertEqual(len([r for r in parsed if r["role"] == "bus_shunt"]), 1)
                self.assertEqual(next(r for r in parsed if r["role"] == "branch")["id"], "T1")

    def test_real_converter_plain_line_zero_record_manifest_remains_valid(self):
        data = {"bus": [{"bus_id": "B1", "type": "slack", "base_kv": 20},
                        {"bus_id": "B2", "type": "pq", "base_kv": 20, "p_kw": 100}],
                "line": [{"line_id": "L1", "from_bus": "B1", "to_bus": "B2", "r_ohm": .1, "x_ohm": .2}]}
        source = self.root / "source.json"
        source.write_text(json.dumps(data), encoding="utf-8")
        master = Path(convert_json(str(source), str(self.root / "line-model")))
        self.assertIn("fidelity_records=0", master.read_text(encoding="utf-8"))
        self.assertEqual(read_fidelity_mapping(str(master.parent)), [])


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FidelityMappingTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
