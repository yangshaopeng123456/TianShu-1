"""Real file snapshots of DSS CSV/binary array dependencies; no native engine."""
from pathlib import Path
import struct
import tempfile
import unittest

from ..domain.case.case_loader import CaseLoadError, load_case_bundle, scan_dss_references


class DataDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-data-dependencies-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.master = self.source / "Master.dss"

    def load(self, name="snapshot"):
        return load_case_bundle(str(self.master), str(self.base / name), str(self.base / "cases"))

    def test_all_standard_array_file_formats_are_frozen_and_byte_exact(self):
        self.master.write_text("New Circuit.test\nNew LoadShape.l npts=2 mult=(file='profile ! values.csv', col=2, header=yes)\n"
                               "~ qmult=[sngfile=values.sng]\nNew LoadShape.m dblfile=values.dbl\n"
                               "New LoadShape.n csvfile=plain.csv\nNew LoadShape.q pqcsvfile=pq.csv\n", encoding="utf-8")
        fixtures = {"profile ! values.csv": b"t,value\n0,1\n1,2\n", "values.sng": struct.pack("ff", 1., 2.),
                    "values.dbl": struct.pack("dd", 3., 4.), "plain.csv": b"1\n2\n", "pq.csv": b"1,2\n3,4\n"}
        for name, content in fixtures.items():
            (self.source / name).write_bytes(content)
        case = self.load()
        self.assertEqual(case.dependency_manifest.file_count, 6)
        for name, content in fixtures.items():
            self.assertEqual((self.base / "snapshot/input" / name).read_bytes(), content)
            (self.source / name).write_bytes(b"changed after submission")
            self.assertEqual((self.base / "snapshot/input" / name).read_bytes(), content)

    def test_changed_data_changes_fingerprint_and_old_snapshot_stays_frozen(self):
        self.master.write_text("New Circuit.test\nNew LoadShape.l mult=(file=profile.csv)", encoding="utf-8")
        (self.source / "profile.csv").write_bytes(b"1\n")
        first = self.load("one")
        (self.source / "profile.csv").write_bytes(b"2\n")
        second = self.load("two")
        self.assertNotEqual(first.case_fingerprint, second.case_fingerprint)
        self.assertEqual((self.base / "one/input/profile.csv").read_bytes(), b"1\n")

    def test_missing_data_rejected_before_execution(self):
        self.master.write_text("New LoadShape.l mult=(file=missing.csv)", encoding="utf-8")
        with self.assertRaises(CaseLoadError):
            self.load()

    def test_absolute_parent_escape_and_dynamic_result_are_rejected(self):
        (self.base / "outside.csv").write_bytes(b"1\n")
        for ref in ("../outside.csv", str(self.base / "outside.csv"), "%result%"):
            with self.subTest(ref=ref):
                self.master.write_text(f'New LoadShape.l mult=(file="{ref}")', encoding="utf-8")
                with self.assertRaises(CaseLoadError):
                    self.load()

    def test_comments_do_not_create_false_dependencies(self):
        self.master.write_text("! mult=(file=not-there.csv)\n// Redirect missing.dss\n/*\nNew LoadShape.x csvfile=missing.csv\n*/\n"
                               "New LoadShape.l mult=(file='profile ! x.csv') ! file=comment.csv\n", encoding="utf-8")
        self.assertEqual(scan_dss_references(str(self.master)), ["profile ! x.csv"])

    def test_recursive_script_keeps_data_role_and_binary_is_not_parsed_as_script(self):
        self.master.write_text('Redirect ["child.dss"]\n', encoding="utf-8")
        (self.source / "child.dss").write_text("New LoadShape.l dblfile=values.dbl", encoding="utf-8")
        # Binary payload deliberately resembles a DSS path command in bytes.
        payload = b"Redirect should-not-be-parsed.dss\n\0\xff"
        (self.source / "values.dbl").write_bytes(payload)
        case = self.load()
        self.assertEqual(case.dependency_manifest.file_count, 3)
        data = next(e for e in case.dependency_manifest.entries if e.path == "values.dbl")
        self.assertEqual(data.role, "data")
        self.assertEqual((self.base / "snapshot/input/values.dbl").read_bytes(), payload)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DataDependencyTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
