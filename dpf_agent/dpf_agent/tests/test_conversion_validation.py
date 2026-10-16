"""转换严格输入校验的行为边界，不调用求解器或模型。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from ..web.conversion_validation import validate_json_payload, validate_tables


def frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    bus = pd.DataFrame([
        {"bus_id": "SourceReal", "type": "slack", "base_kv": 20},
        {"bus_id": "Remote", "type": "pq", "base_kv": 20, "p_kw": 100},
    ])
    line = pd.DataFrame([{"line_id": "L1", "from_bus": "SourceReal", "to_bus": "Remote", "r_ohm": .1, "x_ohm": .2}])
    return bus, line


def payload() -> dict:
    bus, line = frames()
    # This fixture uses omitted optional fields, never explicit JSON NaN.
    return {"system": {"base_mva": 100, "frequency_hz": 50},
            "bus": [{key: value for key, value in row.items() if not pd.isna(value)} for row in bus.to_dict("records")],
            "line": line.to_dict("records")}


class ConversionValidationTests(unittest.TestCase):
    def _csv_entry_reads_all_companions(self, entry: str) -> None:
        from ..web.case_converters import convert_csv
        with tempfile.TemporaryDirectory(prefix="conversion-csv-entry-") as temporary:
            folder = Path(temporary)
            inputs = folder / "inputs"
            inputs.mkdir()
            (inputs / "bus.csv").write_text("bus_id,type,base_kv,p_kw\nB1,slack,20,0\nB2,pq,20,100\n", encoding="utf-8")
            (inputs / "line.csv").write_text("line_id,from_bus,to_bus,r_ohm,x_ohm\nL1,B1,B2,0.1,0.2\n", encoding="utf-8")
            (inputs / "system.csv").write_text("frequency_hz\n60\n", encoding="utf-8")
            (inputs / "load.csv").write_text("load_id,bus_id,p_kw\nLD1,B2,900\n", encoding="utf-8")
            source = inputs if entry == "directory" else inputs / entry
            text = Path(convert_csv(str(source), str(folder / "output"))).read_text(encoding="utf-8")
            self.assertIn("Set DefaultBaseFrequency=60", text)
            self.assertRegex(text, r"\bkW=900(?:\.0+)?(?=\s|$)")
            self.assertNotRegex(text, r"\bkW=100(?:\.0+)?(?=\s|$)")
            self.assertIn("load_source=explicit_load", text)

    def test_csv_directory_entry_reads_system_and_load_companions(self) -> None:
        self._csv_entry_reads_all_companions("directory")

    def test_csv_bus_file_keeps_electrical_type_and_reads_companions(self) -> None:
        self._csv_entry_reads_all_companions("bus.csv")

    def test_csv_line_file_reads_system_and_load_companions(self) -> None:
        self._csv_entry_reads_all_companions("line.csv")

    def test_valid_tables_and_json_do_not_modify_sources(self) -> None:
        bus, line = frames()
        bus_before, line_before = bus.copy(deep=True), line.copy(deep=True)
        system = {"S_BASE_MVA": "100", "FREQUENCY_HZ": 50}
        output = validate_tables(bus, line, system=system)
        self.assertEqual(output.system["S_BASE_MVA"], 100)
        self.assertEqual(system["S_BASE_MVA"], "100")
        assert_frame_equal(bus, bus_before)
        assert_frame_equal(line, line_before)
        data = payload()
        before = copy.deepcopy(data)
        validate_json_payload(data)
        self.assertEqual(data, before)

    def test_explicit_invalid_json_power_is_rejected_in_bus_and_load(self) -> None:
        for section in ("bus", "load"):
            for value in (float("nan"), float("inf"), -float("inf"), True, False, "not-a-number", "", "NaN", "1e309", [100], {"value": 100}):
                with self.subTest(section=section, value=value):
                    data = payload()
                    if section == "load":
                        data["load"] = [{"load_id": "LD1", "bus_id": "Remote", "p_kw": value}]
                    else:
                        data["bus"][1]["p_kw"] = value
                    with self.assertRaises(ValueError):
                        validate_json_payload(data)

    def test_json_system_finite_positive_and_boolean_rules(self) -> None:
        for field in ("base_mva", "baseMVA", "s_base_mva", "base_kv", "frequency_hz"):
            for value in (float("nan"), float("inf"), True, "bad", 0, -1):
                with self.subTest(field=field, value=value):
                    data = payload()
                    data["system"][field] = value
                    with self.assertRaises(ValueError):
                        validate_json_payload(data)
        for value in (True, "bad", "inf", 0):
            data = payload()
            data["system"] = [{"parameter": "FREQUENCY_HZ", "value": value}]
            with self.assertRaises(ValueError):
                validate_json_payload(data)

    def test_bus_voltage_and_line_impedance_never_use_invalid_defaults(self) -> None:
        for value in (True, np.bool_(True), float("inf"), "bad", "", 0, -20):
            bus, line = frames()
            bus["base_kv"] = [20, value]
            with self.subTest(field="base_kv", value=value), self.assertRaises(ValueError):
                validate_tables(bus, line)
        for value in (True, np.bool_(False), float("inf"), "bad", ""):
            bus, line = frames()
            line["r_ohm"] = [value]
            with self.subTest(field="r_ohm", value=value), self.assertRaises(ValueError):
                validate_tables(bus, line)
        bus, line = frames()
        line["r_ohm"] = pd.Series([np.array([100])], dtype=object)
        with self.assertRaisesRegex(ValueError, "单个数值"):
            validate_tables(bus, line)

    def test_dataframe_optional_missing_allowed_but_json_nan_rejected(self) -> None:
        bus, line = frames()
        bus["base_kv"] = [20, np.nan]
        bus["p_kw"] = [np.nan, np.nan]
        line["b_pu"] = [pd.NA]
        result = validate_tables(bus, line, system={"S_BASE_MVA": None})
        self.assertTrue(pd.isna(result.bus.loc[1, "base_kv"]))
        self.assertIsNone(result.system["S_BASE_MVA"])
        data = payload()
        data["bus"][1]["p_kw"] = np.nan
        with self.assertRaises(ValueError):
            validate_json_payload(data)
        data["bus"][1]["p_kw"] = None
        validate_json_payload(data)

    def test_power_units_and_aliases_must_be_consistent(self) -> None:
        bus, line = frames()
        bus["p_load_mw"] = [np.nan, .1]
        bus["q_kvar"] = [np.nan, 20]
        bus["q_load_mvar"] = [np.nan, .02]
        bus["p_gen_kw"] = [np.nan, 50]
        bus["p_gen_mw"] = [np.nan, .05]
        bus["q_gen_kvar"] = [np.nan, -10]
        bus["q_gen_mvar"] = [np.nan, -.01]
        validate_tables(bus, line)
        for column, value in (("p_load_mw", .2), ("q_load_mvar", .03), ("p_gen_mw", .06), ("q_gen_mvar", -.02), ("load_kw", 200)):
            bad = bus.copy(deep=True)
            bad[column] = [np.nan, value]
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, "冲突"):
                validate_tables(bad, line)
        data = payload()
        data["bus"][1].update(p_load_mw=.2)
        with self.assertRaisesRegex(ValueError, "冲突"):
            validate_json_payload(data)

    def test_floating_roundoff_not_treated_as_unit_conflict(self) -> None:
        bus, line = frames()
        bus["p_kw"] = [np.nan, .1 + .2]
        bus["p_load_mw"] = [np.nan, .0003]
        validate_tables(bus, line)

    def test_finite_input_cannot_overflow_after_unit_or_capacity_scaling(self) -> None:
        for name in ("p_load_mw", "q_load_mvar", "p_gen_mw", "q_gen_mvar", "qmin_mvar", "qmax_mvar", "rate_mva"):
            bus, line = frames()
            bus.loc[1, "p_kw"] = np.nan
            bus[name] = [np.nan, 1e307]
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, "换算后"):
                validate_tables(bus, line)
        bus, line = frames()
        with self.assertRaisesRegex(ValueError, "缩放"):
            validate_tables(bus, line, system={"S_BASE_MVA": 1e303})
        data = payload()
        data["bus"][1] = {"bus_id": "Remote", "p_load_mw": 1e307}
        with self.assertRaisesRegex(ValueError, "换算后"):
            validate_json_payload(data)
        # No arbitrary engineering ceiling: large representable scaled values remain valid.
        data["bus"][1]["p_load_mw"] = 1e100
        validate_json_payload(data)

    def test_converter_metadata_and_boolean_q_limit_flag_are_preserved(self) -> None:
        bus, line = frames()
        bus["_generator_online"] = [False, True]
        bus["_generator_count"] = [0, 2]
        result = validate_tables(bus, line, system={"ENFORCE_Q_LIMITS": True})
        self.assertEqual(result.bus["_generator_online"].tolist(), [False, True])
        self.assertEqual(result.bus["_generator_count"].tolist(), [0, 2])
        self.assertIs(result.system["ENFORCE_Q_LIMITS"], True)
        data = payload()
        data["bus"][1].update(_generator_online=True, _generator_count=1)
        data["system"]["ENFORCE_Q_LIMITS"] = True
        validate_json_payload(data)

    def test_q_limit_flag_rejects_typos_and_normalizes_numeric_switches(self) -> None:
        bus, line = frames()
        for value in ("flase", "", 2, float("inf"), [1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_tables(bus, line, system={"ENFORCE_Q_LIMITS": value})
            data = payload()
            data["system"]["ENFORCE_Q_LIMITS"] = value
            with self.assertRaises(ValueError):
                validate_json_payload(data)
        for value in (True, 1, 1.0, "1", "1.0", "true", "yes", "on"):
            self.assertIs(validate_tables(bus, line, system={"ENFORCE_Q_LIMITS": value}).system["ENFORCE_Q_LIMITS"], True)
        for value in (False, 0, 0.0, "0", "0.0", "false", "no", "off"):
            self.assertIs(validate_tables(bus, line, system={"ENFORCE_Q_LIMITS": value}).system["ENFORCE_Q_LIMITS"], False)

    def test_nonempty_load_table_requires_power_column_and_each_row_value(self) -> None:
        bus, line = frames()
        invalid = (
            pd.DataFrame([{"load_id": "LD1", "bus_id": "Remote"}]),
            pd.DataFrame([{"load_id": "LD1", "bus_id": "Remote", "p_kw": np.nan, "q_kvar": None}]),
            pd.DataFrame([{"load_id": "LD1", "bus_id": "Remote", "p_kw": 100}, {"load_id": "LD2", "bus_id": "Remote"}]),
        )
        for load in invalid:
            with self.subTest(load=load.to_dict("records")), self.assertRaisesRegex(ValueError, "功率"):
                validate_tables(bus, line, load)
        zero = pd.DataFrame([{"bus_id": "Remote", "p_kw": 0}])
        result = validate_tables(bus, line, zero)
        self.assertEqual(result.load["p_kw"].tolist(), [0])

    def test_load_power_unit_conflict_rejected(self) -> None:
        bus, line = frames()
        load = pd.DataFrame([{"bus_id": "Remote", "p_kw": 100, "p_load_mw": .2}])
        with self.assertRaisesRegex(ValueError, "冲突"):
            validate_tables(bus, line, load)

    def test_missing_load_ids_are_unique_and_do_not_collide_with_explicit_ids(self) -> None:
        bus, line = frames()
        load = pd.DataFrame([
            {"load_id": "ld1", "bus_id": "Remote", "p_kw": 100},
            {"load_id": np.nan, "bus_id": "Remote", "p_kw": 200},
            {"load_id": None, "bus_id": "Remote", "p_kw": 300},
            {"load_id": "LD3", "bus_id": "Remote", "p_kw": 400},
        ])
        before = load.copy(deep=True)
        result = validate_tables(bus, line, load)
        self.assertEqual(result.load["load_id"].tolist(), ["ld1", "LD2", "LD4", "LD3"])
        assert_frame_equal(load, before)
        self.assertEqual(result.load["p_kw"].sum(), 1000)
        no_id = pd.DataFrame([{"bus_id": "Remote", "p_kw": 1}, {"bus_id": "Remote", "p_kw": 2}])
        self.assertEqual(validate_tables(bus, line, no_id).load["load_id"].tolist(), ["LD1", "LD2"])

    def test_explicit_empty_or_duplicate_load_id_rejected(self) -> None:
        bus, line = frames()
        for ids in (("", "L2"), (" ", "L2"), ("l1", "L1"), (1, "1.0"), (False, "L2")):
            load = pd.DataFrame({"load_id": ids, "bus_id": ["Remote", "Remote"], "p_kw": [100, 200]})
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                validate_tables(bus, line, load)

    def test_bus_and_line_empty_and_duplicate_ids_rejected(self) -> None:
        for ids in (("", "Remote"), (np.nan, "Remote"), ("Node", "node"), (1, "1.0"), (True, "Remote")):
            bus, line = frames()
            bus["bus_id"] = list(ids)
            with self.subTest(bus_ids=ids), self.assertRaises(ValueError):
                validate_tables(bus, line)
        for value in ("", None, np.nan, True):
            bus, line = frames()
            line["line_id"] = [value]
            with self.subTest(line_id=value), self.assertRaises(ValueError):
                validate_tables(bus, line)
        bus, line = frames()
        line = pd.concat([line, line.assign(line_id="l1")], ignore_index=True)
        with self.assertRaisesRegex(ValueError, "碰撞"):
            validate_tables(bus, line)

    def test_references_canonicalized_and_source_virtual_alias_kept(self) -> None:
        bus, line = frames()
        line["from_bus"] = ["sourcereal"]
        line["to_bus"] = ["REMOTE"]
        load = pd.DataFrame([{"load_id": "LD1", "bus_id": "remote", "p_kw": 100}])
        result = validate_tables(bus, line, load)
        self.assertEqual(result.line["from_bus"].tolist(), ["SourceReal"])
        self.assertEqual(result.line["to_bus"].tolist(), ["Remote"])
        self.assertEqual(result.load["bus_id"].tolist(), ["Remote"])
        line["from_bus"] = ["source"]
        self.assertEqual(validate_tables(bus, line).line["from_bus"].tolist(), ["SOURCE"])
        bus.loc[0, "bus_id"] = "Source"
        self.assertEqual(validate_tables(bus, line).line["from_bus"].tolist(), ["Source"])

    def test_unknown_or_empty_load_and_line_bus_rejected(self) -> None:
        for value in ("MISSING", None, "", True):
            bus, line = frames()
            load = pd.DataFrame([{"bus_id": value, "p_kw": 100}])
            with self.subTest(load_bus=value), self.assertRaises(ValueError):
                validate_tables(bus, line, load)
            line["to_bus"] = [value]
            with self.subTest(line_bus=value), self.assertRaises(ValueError):
                validate_tables(bus, line)

    def test_numeric_ids_and_references_use_one_identity(self) -> None:
        bus = pd.DataFrame({"bus_id": [1.0, 2.0], "base_kv": [20, 20]})
        line = pd.DataFrame({"line_id": [1.0], "from_bus": ["1.0"], "to_bus": [2]})
        load = pd.DataFrame({"bus_id": ["2.0"], "p_kw": [100]})
        result = validate_tables(bus, line, load)
        self.assertEqual(result.bus["bus_id"].tolist(), ["1", "2"])
        self.assertEqual(result.line["from_bus"].tolist(), ["1"])
        self.assertEqual(result.load["bus_id"].tolist(), ["2"])

    def test_json_identifier_bool_not_lost_by_dataframe_type_coercion(self) -> None:
        for field in ("bus_id", "id"):
            data = payload()
            for row in data["bus"]:
                row.pop("bus_id")
            data["bus"][0][field] = True
            data["bus"][1][field] = 2
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "标识不能是布尔值"):
                validate_json_payload(data)
        bus, line = frames()
        line["to_bus"] = pd.Series([["Remote"]], dtype=object)
        with self.assertRaisesRegex(ValueError, "单个值"):
            validate_tables(bus, line)

    def test_large_integral_identifiers_never_round_through_float(self) -> None:
        first, second = 1000000000000000001, 1000000000000000002
        bus = pd.DataFrame({"bus_id": [first, second]})
        line = pd.DataFrame({"line_id": ["L1"], "from_bus": [str(first)], "to_bus": [str(second)]})
        result = validate_tables(bus, line)
        self.assertEqual(result.bus["bus_id"].tolist(), [str(first), str(second)])
        self.assertEqual(result.line["from_bus"].tolist(), [str(first)])

    def test_identifier_structure_delimiters_rejected_for_all_table_roles(self) -> None:
        unsafe = ("B2 Phases=1 !", "B2\nNew Load.Hidden", "B2\tA", "B2\x00", "B2;Solve", "B2=1", "B2//comment", "B2[1]", "B2(1)", "B2,3", 'B2"', "B2'", "B2?")
        for value in unsafe:
            bus, line = frames()
            with self.subTest(value=value, role="bus"), self.assertRaisesRegex(ValueError, "标识"):
                validate_tables(bus.assign(bus_id=["SourceReal", value]), line)
            with self.subTest(value=value, role="line"), self.assertRaisesRegex(ValueError, "标识"):
                validate_tables(bus, line.assign(line_id=[value]))
            load = pd.DataFrame([{"load_id": value, "bus_id": "Remote", "p_kw": 100}])
            with self.subTest(value=value, role="load"), self.assertRaisesRegex(ValueError, "标识"):
                validate_tables(bus, line, load)

    def test_bus_dot_rejected_object_dot_kept_and_integral_dot_normalized(self) -> None:
        bus, line = frames()
        with self.assertRaisesRegex(ValueError, "点号"):
            validate_tables(bus.assign(bus_id=["SourceReal", "Remote.1"]), line)
        with self.assertRaisesRegex(ValueError, "点号"):
            validate_tables(bus, line.assign(to_bus=["Remote.1"]))
        line["line_id"] = ["Line.1"]
        load = pd.DataFrame([{"load_id": "Load.1", "bus_id": "Remote", "p_kw": 100}])
        output = validate_tables(bus, line, load)
        self.assertEqual(output.line["line_id"].tolist(), ["Line.1"])
        self.assertEqual(output.load["load_id"].tolist(), ["Load.1"])
        numeric_bus = pd.DataFrame({"bus_id": ["1.0", "2.0"]})
        numeric_line = pd.DataFrame({"line_id": ["L1"], "from_bus": [1], "to_bus": ["2.0"]})
        self.assertEqual(validate_tables(numeric_bus, numeric_line).bus["bus_id"].tolist(), ["1", "2"])

    def test_public_json_unsafe_ids_never_publish_or_replace_old_master(self) -> None:
        from ..web.case_converters import convert_json
        with tempfile.TemporaryDirectory(prefix="conversion-unsafe-id-") as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            old_master = output / "Master.dss"
            original = b"old verified model\n"
            old_master.write_bytes(original)
            for name, value in (("bus_id", "Remote Phases=1 !"), ("bus_id", "Remote.1"), ("load_id", "L1\nSolve"), ("line_id", "L1;New Circuit.Evil")):
                data = payload()
                if name == "bus_id":
                    data["bus"][1][name] = value
                    data["line"][0]["to_bus"] = value
                elif name == "line_id":
                    data["line"][0][name] = value
                else:
                    data["load"] = [{"load_id": value, "bus_id": "Remote", "p_kw": 100}]
                source = root / "input.json"
                source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    convert_json(str(source), str(output))
                self.assertEqual(old_master.read_bytes(), original)
                self.assertEqual(sorted(path.name for path in output.iterdir()), ["Master.dss"])

    def test_explicit_empty_load_preserved_and_absence_distinct(self) -> None:
        bus, line = frames()
        self.assertIsNone(validate_tables(bus, line).load)
        result = validate_tables(bus, line, pd.DataFrame())
        self.assertIsNotNone(result.load)
        self.assertTrue(result.load.empty)
        data = payload()
        data["load"] = []
        validate_json_payload(data)
        data.pop("load")
        data["loads"] = []
        validate_json_payload(data)

    def test_json_bad_system_and_load_shapes_rejected(self) -> None:
        for name in ("load", "loads"):
            for value in ({"bus_id": "Remote", "p_kw": 700}, None, "bad", [1], [None]):
                data = payload()
                data[name] = value
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    validate_json_payload(data)
        for value in (None, "bad", 1, True, [1], [None]):
            data = payload()
            data["system"] = value
            with self.subTest(system=value), self.assertRaises(ValueError):
                validate_json_payload(data)
        data = payload()
        data.update(load=[], loads=[])
        with self.assertRaises(ValueError):
            validate_json_payload(data)

    def test_valid_system_object_and_key_value_rows(self) -> None:
        data = payload()
        data["system"] = [{"parameter": "BASE_MVA", "value": 100}, {"key": "FREQUENCY_HZ", "value": 60}]
        validate_json_payload(data)
        bus, line = frames()
        result = validate_tables(bus, line, system={"BASE_MVA": 100, "S_BASE_MVA": 100, "FREQUENCY_HZ": "60"})
        self.assertEqual(result.system["FREQUENCY_HZ"], 60)
        with self.assertRaisesRegex(ValueError, "冲突"):
            validate_tables(bus, line, system={"BASE_MVA": 50, "S_BASE_MVA": 100})

    def test_unknown_columns_preserved_and_not_renamed(self) -> None:
        bus, line = frames()
        bus["comment"] = ["alpha", "beta"]
        bus["load_kw"] = [np.nan, 100]
        result = validate_tables(bus, line)
        self.assertEqual(result.bus.columns.tolist(), bus.columns.tolist())
        self.assertEqual(result.bus["comment"].tolist(), ["alpha", "beta"])

    def test_duplicate_dataframe_index_not_used_to_overwrite_rows(self) -> None:
        bus, line = frames()
        bus.index = [5, 5]
        bus["p_kw"] = ["50", "100"]
        result = validate_tables(bus, line)
        self.assertEqual(result.bus["p_kw"].tolist(), [50, 100])
        duplicated = pd.concat([bus["p_kw"], bus["p_kw"]], axis=1)
        with self.assertRaisesRegex(ValueError, "重复列名"):
            validate_tables(duplicated, line)

    def test_status_boolean_valid_but_unknown_state_rejected(self) -> None:
        bus, line = frames()
        for value in (True, False, 0, 1, "off", "enabled"):
            line["status"] = [value]
            validate_tables(bus, line)
        for value in ("flase", "", 2, float("inf")):
            line["status"] = [value]
            with self.subTest(status=value), self.assertRaises(ValueError):
                validate_tables(bus, line)


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ConversionValidationTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
