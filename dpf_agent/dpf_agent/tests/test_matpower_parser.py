"""Offline literal MATPOWER parsing: preserve rows or fail explicitly, never run MATLAB."""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from ..web.matpower_parser import MatpowerParseError, _Parser, parse_matpower_case


BUS = "1 3 0 0 0 0 1 1 0 20 1 1.1 .9;\n2 2 1 .2 0 0 1 1 0 20 1 1.1 .9;"
GEN = "1 1 0 10 -10 1 100 1 10 0;\n2 .5 0 10 -10 1.03 100 1 10 0;"
BRANCH = "1 2 .01 .02 0 100 100 100 1.1 0 1 -360 360;"


def case(*, bus=BUS, gen=GEN, branch=BRANCH, base="100", prefix="", suffix=""):
    return (prefix + f"mpc.version = '2';\nmpc.baseMVA = {base};\nmpc.bus = [\n{bus}\n];\n"
            f"mpc.gen = [\n{gen}\n];\nmpc.branch = [\n{branch}\n];\n" + suffix)


class MatpowerParserTests(unittest.TestCase):
    def parse(self, text):
        return _Parser(text, "memory.m").parse()

    def test_standard_literal_rows_preserve_generator_voltage_tap_and_status(self):
        parsed = self.parse(case())
        self.assertEqual(parsed.base_mva, 100)
        self.assertEqual(len(parsed.bus), 2)
        self.assertEqual(parsed.gen[1][5], 1.03)
        self.assertEqual(parsed.branch[0][8], 1.1)
        self.assertEqual(parsed.gen[1][7], 1)

    def test_semicolon_rows_on_one_physical_line_equal_multiline(self):
        multiline = self.parse(case(gen=GEN + "\n2 .7 0 10 -10 1.03 100 1 10 0;"))
        inline = self.parse(case(bus=BUS.replace("\n", " "), gen=(GEN + "\n2 .7 0 10 -10 1.03 100 1 10 0;").replace("\n", " ")))
        self.assertEqual(inline.bus, multiline.bus)
        self.assertEqual(inline.gen, multiline.gen)
        self.assertEqual(sum(row[1] for row in inline.gen if row[0] == 2), 1.2)

    def test_newline_without_semicolon_delimits_rows(self):
        self.assertEqual(self.parse(case(bus=BUS.replace(";", ""))).bus, self.parse(case()).bus)

    def test_matlab_continuation_preserves_both_halves_and_comments(self):
        continued = GEN.replace("1 1 0", "1 1 ... % remaining columns\n0").replace("2 .5 0", "2 .5 ... arbitrary continuation tail\n0")
        self.assertEqual(self.parse(case(gen=continued)).gen, self.parse(case()).gen)

    def test_comma_columns_signed_decimals_and_scientific_notation(self):
        text = case(base="+1E2", branch="1,2,+1e-2,.02,0,1e2,100,100,1.1,-3e1,1,-360,+360;")
        parsed = self.parse(text)
        self.assertEqual(parsed.branch[0], (1, 2, .01, .02, 0, 100, 100, 100, 1.1, -30, 1, -360, 360))

    def test_commented_old_assignments_cannot_mask_real_matrix(self):
        old = "% mpc.gen = [\n% 2 9 0 10 -10 1.9 100 1 10 0;\n% ];\n"
        self.assertEqual(self.parse(old + case()).gen[1][1], .5)

    def test_inline_percent_brace_comment_is_not_block_comment(self):
        self.assertEqual(self.parse(case(prefix="%{ not a standalone block marker\n")).gen, self.parse(case()).gen)

    def test_standalone_nested_block_comments_ignore_only_comment_content(self):
        block = "%{\nmpc.gen = [garbage];\n%{\nnot code\n%}\n%}\n"
        self.assertEqual(self.parse(block + case()).gen, self.parse(case()).gen)

    def test_common_function_headers_and_optional_end(self):
        for header in ("function mpc = case_probe\n", "function [mpc] = case_probe()\n"):
            with self.subTest(header=header):
                self.assertEqual(self.parse(case(prefix=header, suffix="end\n")).bus, self.parse(case()).bus)

    def test_empty_function_reports_missing_input_not_index_error(self):
        with self.assertRaisesRegex(MatpowerParseError, "缺少 mpc.bus"):
            self.parse("function mpc = empty_case\nend\n")

    def test_bad_generator_token_is_rejected_with_source_location_never_skipped(self):
        with self.assertRaisesRegex(MatpowerParseError, r"memory.m:\d+:\d+:"):
            self.parse(case(gen=GEN.replace("2 .5", "2 bad")))

    def test_matlab_expressions_and_function_calls_are_rejected(self):
        for expression in ("100/2", "100*2", "abs(100)", "1+2", "1-2", "Inf", "NaN"):
            with self.subTest(expression=expression), self.assertRaises(MatpowerParseError):
                self.parse(case(gen=GEN.replace("2 .5", "2 " + expression)))

    def test_duplicate_and_dynamic_assignments_are_rejected(self):
        for extra in ("mpc.gen = [2 1 0];", "mpc.gen(2,2) = 9;", "mpc.bus(:,3) = 0;", "mpc = loadcase('case9');", "define_constants;", "if true\nend"):
            with self.subTest(extra=extra), self.assertRaises(MatpowerParseError):
                self.parse(case(suffix=extra))

    def test_repeated_scalar_cannot_override_first_value(self):
        with self.assertRaisesRegex(MatpowerParseError, "重复赋值"):
            self.parse(case(suffix="mpc.baseMVA = 10;"))

    def test_unknown_physical_field_is_rejected_not_dropped(self):
        with self.assertRaisesRegex(MatpowerParseError, "不支持 mpc.dcline"):
            self.parse(case(suffix="mpc.dcline = [1 2 0];"))

    def test_numeric_gencost_and_areas_are_declared_unused_metadata(self):
        parsed = self.parse(case(suffix="mpc.gencost = [2 0 0 3 .01 1 0];\nmpc.areas = [1 1];"))
        self.assertTrue(any("mpc.gencost" in warning for warning in parsed.warnings))
        self.assertTrue(any("mpc.areas" in warning for warning in parsed.warnings))

    def test_nonfinite_numbers_in_any_matrix_column_are_rejected(self):
        for field in ("bus", "gen", "branch"):
            data = {"bus": BUS, "gen": GEN, "branch": BRANCH}
            data[field] = data[field].replace("20" if field == "bus" else (".5" if field == "gen" else ".01"), "1e999", 1)
            with self.subTest(field=field), self.assertRaisesRegex(MatpowerParseError, "数值必须有限"):
                self.parse(case(**data))

    def test_base_mva_must_be_positive_finite(self):
        for value in ("0", "-100", "1e999", "NaN"):
            with self.subTest(value=value), self.assertRaises(MatpowerParseError):
                self.parse(case(base=value))

    def test_bus_id_type_gen_bus_and_branch_bus_cannot_be_fractional(self):
        for options in ({"bus": BUS.replace("1 3", "1.5 3")}, {"bus": BUS.replace("1 3", "1 3.1")},
                        {"gen": GEN.replace("2 .5", "2.5 .5")}, {"branch": BRANCH.replace("1 2", "1 2.5")}):
            with self.subTest(options=options), self.assertRaisesRegex(MatpowerParseError, "整数"):
                self.parse(case(**options))

    def test_bad_types_ids_and_duplicate_buses_are_rejected(self):
        for bus in (BUS.replace("1 3", "0 3"), BUS.replace("2 2", "1 2"), BUS.replace("2 2", "2 5"), BUS.replace("1 3", "9007199254740993 3")):
            with self.subTest(bus=bus), self.assertRaises(MatpowerParseError):
                self.parse(case(bus=bus))

    def test_ids_at_and_above_2_to_53_are_rejected_before_aliasing(self):
        # These distinct integer spellings collide as floats. Neither can become
        # a silently accepted bus ID, generator bus, or branch endpoint.
        self.assertEqual(float("9007199254740992"), float("9007199254740993"))
        for value in ("9007199254740992", "9007199254740993", "9007199254740993e0"):
            for options in ({"bus": BUS.replace("1 3", value + " 3")},
                            {"gen": GEN.replace("2 .5", value + " .5")},
                            {"branch": BRANCH.replace("1 2", "1 " + value)}):
                with self.subTest(value=value, options=options), self.assertRaisesRegex(MatpowerParseError, "精确表示"):
                    self.parse(case(**options))

    def test_unknown_generator_and_branch_references_are_rejected_even_if_offline(self):
        for options in ({"gen": GEN.replace("2 .5", "9 .5")}, {"gen": GEN.replace("2 .5", "9 .5").replace("1.03 100 1", "1.03 100 0")},
                        {"branch": BRANCH.replace("1 2", "1 9")}):
            with self.subTest(options=options), self.assertRaisesRegex(MatpowerParseError, "未知母线"):
                self.parse(case(**options))

    def test_status_is_not_truncated_from_fractional_value(self):
        for options in ({"gen": GEN.replace("1.03 100 1", "1.03 100 .5")}, {"branch": BRANCH.replace("1.1 0 1", "1.1 0 .5")},
                        {"branch": BRANCH.replace("1.1 0 1", "1.1 0 2")}):
            with self.subTest(options=options), self.assertRaises(MatpowerParseError):
                self.parse(case(**options))

    def test_negative_legacy_generator_status_preserved_for_downstream_offline_filter(self):
        parsed = self.parse(case(gen=GEN.replace("1.03 100 1", "1.03 100 -1")))
        self.assertEqual(parsed.gen[1][7], -1)

    def test_short_rows_have_explicit_default_warnings_and_preserve_numbers(self):
        parsed = self.parse(case(gen="1 1 0;\n2 .5 0;", branch="1 2 .01 .02;"))
        self.assertEqual(parsed.gen[1], (2, .5, 0))
        self.assertTrue(any("GEN_STATUS" in warning for warning in parsed.warnings))
        self.assertTrue(any("BR_STATUS" in warning for warning in parsed.warnings))

    def test_missing_base_version_and_gen_are_explicit_legacy_defaults(self):
        text = f"mpc.bus = [{BUS}];\nmpc.branch = [{BRANCH}];"
        parsed = self.parse(text)
        self.assertEqual(parsed.base_mva, 100)
        self.assertEqual(parsed.gen, ())
        self.assertEqual(len(parsed.warnings), 3)

    def test_minimum_columns_and_ragged_rows_are_rejected(self):
        for options in ({"bus": "1 3 0;"}, {"gen": "1 1;"}, {"branch": "1 2 .01;"},
                        {"gen": GEN.replace("2 .5 0", "2 .5")}):
            with self.subTest(options=options), self.assertRaises(MatpowerParseError):
                self.parse(case(**options))

    def test_invalid_voltage_base_and_negative_tap_rejected_without_defaults(self):
        for options in ({"bus": BUS.replace("0 20 1", "0 0 1")}, {"branch": BRANCH.replace("1.1 0 1", "-1.1 0 1")}):
            with self.subTest(options=options), self.assertRaises(MatpowerParseError):
                self.parse(case(**options))

    def test_incomplete_strings_comments_matrices_and_continuations_rejected(self):
        for text in ("%{\nunclosed", "mpc.version = '2;", "mpc.bus = [1 3", "mpc.baseMVA = 1 ..."):
            with self.subTest(text=text), self.assertRaises(MatpowerParseError):
                self.parse(text)

    def test_empty_optional_gen_and_no_branch_rows_are_not_fabricated(self):
        parsed = self.parse(case(gen="", branch=""))
        self.assertEqual(parsed.gen, ())
        self.assertEqual(parsed.branch, ())

    def test_no_code_execution_or_external_file_access_for_function_expression(self):
        with self.assertRaises(MatpowerParseError):
            self.parse(case(suffix="system('not_executed');"))

    def test_public_file_api_preserves_all_literal_rows(self):
        with patch.object(Path, "read_text", return_value=case(gen=GEN.replace("\n", " "))):
            self.assertEqual(parse_matpower_case("case.m").gen, self.parse(case()).gen)

    def test_invalid_encoding_is_rejected_not_silently_skipped(self):
        with patch.object(Path, "read_text", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            with self.assertRaisesRegex(MatpowerParseError, "不能忽略损坏字节"):
                parse_matpower_case("broken.m")


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(MatpowerParserTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
