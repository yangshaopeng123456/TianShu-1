"""Offline upload/publication regressions using real files and explicit I/O faults.

No service, GUI, COM or native solver is started. Fixtures, partial-write evidence
and products remain under DPF_TEST_ARTIFACT_ROOT or the system temporary directory.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

import pandas as pd
from starlette.datastructures import UploadFile

from ..domain.case.case_loader import load_case_bundle
from ..web import case_converters, chat_api, chat_store, conversion_cache


_RUN = time.strftime("%H%M%S") + "_" + str(os.getpid())


def _json_case(kw: float) -> bytes:
    return json.dumps({
        "bus": [{"bus_id": "B1", "type": "slack", "base_kv": 20},
                {"bus_id": "B2", "type": "pq", "base_kv": 20, "p_kw": kw}],
        "line": [{"line_id": "L1", "from_bus": "B1", "to_bus": "B2",
                  "r_ohm": .1, "x_ohm": .2}],
    }).encode("utf-8")


def _csv_bundle(*, load_kw: float | None = None, frequency: float | None = None) -> dict[str, bytes]:
    members = {
        "bus.csv": b"bus_id,type,base_kv,p_kw\nB1,slack,20,0\nB2,pq,20,100\n",
        "line.csv": b"line_id,from_bus,to_bus,r_ohm,x_ohm\nL1,B1,B2,.1,.2\n",
    }
    if frequency is not None:
        members["system.csv"] = f"parameter,value\nFREQUENCY_HZ,{frequency}\n".encode()
    if load_kw is not None:
        members["load.csv"] = f"load_id,bus_id,p_kw,q_kvar,phases\nLD,B2,{load_kw},0,3\n".encode()
    return members


def _mixed_csv(kw: float) -> bytes:
    stream = io.StringIO()
    pd.DataFrame([
        {"type": "bus", "bus_id": "B1", "bus_type": "slack", "base_kv": 20},
        {"type": "bus", "bus_id": "B2", "bus_type": "pq", "base_kv": 20, "p_kw": kw},
        {"type": "line", "line_id": "L1", "from_bus": "B1", "to_bus": "B2", "r_ohm": .1, "x_ohm": .2},
    ]).to_csv(stream, index=False)
    return stream.getvalue().encode()


def _xlsx(kw: float) -> bytes:
    data = json.loads(_json_case(kw))
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as book:
        pd.DataFrame(data["bus"]).to_excel(book, sheet_name="Bus", index=False)
        pd.DataFrame(data["line"]).to_excel(book, sheet_name="Line", index=False)
    return stream.getvalue()


def _matpower(kw: float) -> bytes:
    return ("function mpc=publication_case\nmpc.version='2';mpc.baseMVA=100;\n"
            f"mpc.bus=[1 3 0 0 0 0 1 1 0 20 1 1.1 .9;2 1 {kw / 1000:g} 0 0 0 1 1 0 20 1 1.1 .9];\n"
            "mpc.gen=[];mpc.branch=[1 2 .01 .02 0 100 0 0 0 0 1 -360 360];\n").encode()


def _loads_kw(text: str) -> float:
    return sum(float(re.search(r"\bkW=([-+0-9.eE]+)", line).group(1))
               for line in text.splitlines() if line.startswith("New Load."))


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


class _RealMasterFile:
    """Wrap a real temporary file to inject one explicit write fault/change."""
    def __init__(self, actual, on_write):
        self.actual = actual
        self.on_write = on_write

    def __getattr__(self, name):
        return getattr(self.actual, name)

    def __enter__(self):
        self.actual.__enter__()
        return self

    def __exit__(self, *args):
        return self.actual.__exit__(*args)

    def write(self, text):
        return self.on_write(self.actual, text)


class ConversionPublicationTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("DPF_TEST_ARTIFACT_ROOT") or
                    Path(tempfile.gettempdir()) / "tianshu-tests")
        self.root = base / "unit_publication" / _RUN / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=False)
        self.uploads = self.root / "uploads"
        self.upload_patch = patch.object(chat_store, "UPLOADS_DIR", str(self.uploads))
        self.upload_patch.start()
        self.addCleanup(self.upload_patch.stop)
        # Libraries that need an unspecified temporary file also stay in practice.
        self.temp_root = self.root / "temporary"
        self.temp_root.mkdir()
        self.temp_patch = patch.object(tempfile, "tempdir", str(self.temp_root))
        self.temp_patch.start()
        self.addCleanup(self.temp_patch.stop)

    def upload(self, members: dict[str, bytes], session="session"):
        return chat_api._process_uploaded_files(session, [
            UploadFile(filename=name, file=io.BytesIO(body)) for name, body in members.items()
        ])

    def bundle(self, *, load_kw=None, frequency=None):
        members = _csv_bundle(load_kw=load_kw, frequency=frequency)
        attachments = self.upload(members)
        master = Path(chat_api._extract_case_refs("session", attachments)[0])
        return self.uploads / "session", master, members

    def existing_json(self):
        source = self.root / "case.json"
        source.write_bytes(_json_case(100))
        output = Path(conversion_cache.conversion_output_dir(str(source)))
        master = Path(conversion_cache._reconvert_from_source(str(source), str(output), raise_errors=True))
        (output / "ancillary").mkdir()
        (output / "ancillary" / "keep.bin").write_bytes(b"original ancillary bytes\x00\xff")
        before = _snapshot(output)
        source.write_bytes(_json_case(900))
        self.assertEqual(conversion_cache.meta_status(str(output))[0], "stale")
        return source, output, master, before

    def rebuild(self, source, output):
        return conversion_cache._reconvert_from_source(str(source), str(output), raise_errors=True)

    def temporary_master_factory(self, on_write):
        actual_factory = tempfile.NamedTemporaryFile

        def factory(*args, **kwargs):
            actual = actual_factory(*args, **kwargs)
            if kwargs.get("prefix") == ".Master-":
                return _RealMasterFile(actual, on_write)
            return actual
        return factory

    def test_same_stem_four_formats_keep_independent_models_and_exact_names(self):
        members = {"case.csv": _mixed_csv(100), "case.json": _json_case(200),
                   "case.xlsx": _xlsx(300), "case.m": _matpower(400)}
        attachments = self.upload(members)
        refs = chat_api._extract_case_refs("session", attachments)
        self.assertEqual(len(refs), 4)
        self.assertEqual(len(set(refs)), 4)
        for attachment, ref, kw in zip(attachments, refs, (100, 200, 300, 400)):
            name = attachment["filename"]
            self.assertEqual(Path(ref).parent, self.uploads / "session" / "_converted" / name)
            self.assertEqual(_loads_kw(Path(ref).read_text(encoding="utf-8")), kw)
            self.assertEqual(chat_api._resolve_case_name_to_upload(name, "session"), ref)
            self.assertEqual((self.uploads / "session" / name).read_bytes(), members[name])
        with self.assertRaises(chat_api.CaseProductRejected) as rejection:
            chat_api._resolve_case_name_to_upload("case", "session")
        self.assertEqual(rejection.exception.status_code, 422)
        self.assertIn("完整文件名", rejection.exception.detail)

    def test_bus_first_four_csv_attachments_share_complete_60hz_900kw_model(self):
        members = _csv_bundle(load_kw=900, frequency=60)
        attachments = self.upload(members)
        self.assertEqual(len(attachments), 4)
        self.assertTrue(all(item.get("case_component") is True for item in attachments))
        self.assertEqual(len({item["path"] for item in attachments}), 1)
        refs = chat_api._extract_case_refs("session", attachments)
        self.assertEqual(len(refs), 1)
        master = Path(refs[0])
        self.assertEqual(master.parent.name, "bus.csv")
        self.assertEqual(_loads_kw(master.read_text(encoding="utf-8")), 900)
        self.assertIn("DefaultBaseFrequency=60", master.read_text(encoding="utf-8"))
        for name, body in members.items():
            self.assertEqual((self.uploads / "session" / name).read_bytes(), body)
            self.assertEqual(chat_api._resolve_case_name_to_upload(name, "session"), str(master))
        meta = conversion_cache.read_meta(str(master.parent))
        self.assertEqual(meta["source_kind"], "csv_bundle")
        self.assertEqual({Path(item["path"]).name for item in meta["source_dependencies"]}, set(members))

    def test_load_dependency_edit_stales_and_rebuilds_without_bus_change(self):
        source, master, _members = self.bundle(load_kw=100, frequency=60)
        before_bus = (source / "bus.csv").read_bytes()
        (source / "load.csv").write_bytes(_csv_bundle(load_kw=900)["load.csv"])
        self.assertEqual((source / "bus.csv").read_bytes(), before_bus)
        self.assertEqual(conversion_cache.meta_status(str(master.parent))[0], "stale")
        rebuilt, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual((rebuilt, status), (str(master), "rebuilt"))
        self.assertEqual(_loads_kw(master.read_text(encoding="utf-8")), 900)
        self.assertEqual(conversion_cache.meta_status(str(master.parent))[0], "fresh")

    def test_adding_system_dependency_stales_and_updates_frequency(self):
        source, master, _members = self.bundle()
        self.assertIn("DefaultBaseFrequency=50", master.read_text(encoding="utf-8"))
        (source / "system.csv").write_bytes(_csv_bundle(frequency=60)["system.csv"])
        self.assertEqual(conversion_cache.meta_status(str(master.parent))[0], "stale")
        self.assertEqual(conversion_cache.refresh_master_if_stale(str(master))[1], "rebuilt")
        self.assertIn("DefaultBaseFrequency=60", master.read_text(encoding="utf-8"))
        self.assertEqual(conversion_cache.meta_status(str(master.parent))[0], "fresh")

    def test_missing_load_dependency_stales_and_rebuilds_declared_bus_fallback(self):
        source, master, _members = self.bundle(load_kw=900)
        # Move a fixture dependency into evidence instead of deleting its bytes.
        (source / "load.csv").rename(self.root / "removed_load.csv")
        self.assertEqual(conversion_cache.meta_status(str(master.parent))[0], "stale")
        self.assertEqual(conversion_cache.refresh_master_if_stale(str(master))[1], "rebuilt")
        text = master.read_text(encoding="utf-8")
        self.assertEqual(_loads_kw(text), 100)
        self.assertIn("load_source=derived_bus", text)
        self.assertEqual(conversion_cache.meta_status(str(master.parent))[0], "fresh")

    def test_missing_line_dependency_retains_old_model_and_metadata(self):
        source, master, _members = self.bundle(load_kw=900)
        before = _snapshot(master.parent)
        (source / "line.csv").rename(self.root / "removed_line.csv")
        self.assertEqual(conversion_cache.meta_status(str(master.parent))[0], "stale")
        self.assertEqual(conversion_cache.refresh_master_if_stale(str(master)), (str(master), "stale-unresolved"))
        self.assertEqual(_snapshot(master.parent), before)

    def test_source_changes_during_real_master_write_are_not_published(self):
        source, output, _master, before = self.existing_json()
        observations = []

        def change_source(actual, text):
            written = actual.write(text)
            actual.flush()
            observations.append(Path(actual.name).read_bytes())
            source.write_bytes(_json_case(950))
            return written

        with patch.object(tempfile, "NamedTemporaryFile", side_effect=self.temporary_master_factory(change_source)):
            with self.assertRaisesRegex(OSError, "源文件.*变化"):
                self.rebuild(source, output)
        self.assertEqual(len(observations), 1)
        self.assertRegex(observations[0], rb"\bkW=900(?:\.0+)?(?=\s)")
        self.assertEqual(_snapshot(output), before)
        (self.root / "unpublished_master.dss").write_bytes(observations[0])

    def test_real_temporary_master_partial_write_enospc_preserves_old_directory(self):
        source, output, _master, before = self.existing_json()
        observations = []

        def fail_write(actual, text):
            actual.write(text[:256])
            actual.flush()
            os.fsync(actual.fileno())
            observations.append({"path": actual.name, "partial": Path(actual.name).read_bytes()})
            raise OSError(28, "explicit ENOSPC after real temporary prefix write and flush")

        with patch.object(tempfile, "NamedTemporaryFile", side_effect=self.temporary_master_factory(fail_write)):
            with self.assertRaises(OSError) as rejected:
                self.rebuild(source, output)
        self.assertEqual(rejected.exception.errno, 28)
        self.assertEqual(len(observations), 1)
        self.assertIn(".conversion-rebuild-", observations[0]["path"])
        self.assertTrue(observations[0]["partial"].startswith(b"Clear"))
        self.assertEqual(_snapshot(output), before)
        (self.root / "actual_partial_master.dss").write_bytes(observations[0]["partial"])

    def test_temporary_master_replace_failure_preserves_old_directory(self):
        source, output, _master, before = self.existing_json()
        actual_replace = os.replace
        observations = []

        def fail_master(src, dst):
            if Path(dst).name == "Master.dss" and Path(src).name.startswith(".Master-"):
                observations.append(Path(src).read_bytes())
                raise OSError(28, "explicit temporary Master replace failure")
            return actual_replace(src, dst)

        with patch.object(os, "replace", side_effect=fail_master):
            with self.assertRaises(OSError):
                self.rebuild(source, output)
        self.assertEqual(len(observations), 1)
        self.assertRegex(observations[0], rb"\bkW=900(?:\.0+)?(?=\s)")
        self.assertEqual(_snapshot(output), before)
        (self.root / "unpublished_master.dss").write_bytes(observations[0])

    def test_actual_metadata_replace_failure_prevents_directory_publication(self):
        source, output, _master, before = self.existing_json()
        actual_replace = os.replace
        observations = []

        def fail_meta(src, dst):
            if Path(dst).name == conversion_cache.META_NAME and ".conversion-rebuild-" in str(dst):
                observations.append(Path(src).read_bytes())
                raise OSError(28, "explicit metadata replace failure after real write")
            return actual_replace(src, dst)

        with patch.object(os, "replace", side_effect=fail_meta):
            with self.assertRaisesRegex(OSError, "元信息"):
                self.rebuild(source, output)
        self.assertTrue(observations)
        self.assertTrue(all(json.loads(body)["master_sha256"] for body in observations))
        self.assertEqual(_snapshot(output), before)
        (self.root / "unpublished_conversion.json").write_bytes(observations[-1])

    def test_directory_publish_failure_rolls_back_original_bytes_and_ancillary(self):
        source, output, _master, before = self.existing_json()
        actual_replace = os.replace
        observations = []

        def fail_publish(src, dst):
            if Path(src).name == "model" and Path(dst) == output:
                observations.append(_snapshot(Path(src)))
                raise OSError(28, "explicit directory publish failure")
            return actual_replace(src, dst)

        with patch.object(os, "replace", side_effect=fail_publish):
            with self.assertRaises(OSError):
                self.rebuild(source, output)
        self.assertEqual(len(observations), 1)
        self.assertRegex(observations[0]["Master.dss"], rb"\bkW=900(?:\.0+)?(?=\s)")
        self.assertIn("ancillary/keep.bin".replace("/", os.sep), observations[0])
        self.assertEqual(_snapshot(output), before)

    def test_failed_publish_and_rollback_preserve_real_original_backup(self):
        source, output, _master, before = self.existing_json()
        actual_replace = os.replace

        def fail_publish_and_restore(src, dst):
            if Path(src).name in {"model", "previous"} and Path(dst) == output:
                raise OSError(28, "explicit directory publish plus restore failure")
            return actual_replace(src, dst)

        with patch.object(os, "replace", side_effect=fail_publish_and_restore):
            self.assertIsNone(conversion_cache._reconvert_from_source(str(source), str(output)))
        backups = list(output.parent.glob(".conversion-rebuild-*/previous"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(_snapshot(backups[0]), before)
        (self.root / "rollback_evidence.json").write_text(json.dumps({
            "backup": str(backups[0]), "old_sha256": hashlib.sha256(before["Master.dss"]).hexdigest(),
            "actual_backup_retained": True,
        }, indent=2), encoding="utf-8")

    def test_loader_retains_specific_warnings_beyond_4k_and_explicit_load_counts(self):
        data = json.loads(_json_case(100))
        data["line"][0]["conn"] = "wye"
        loads = pd.DataFrame([{"load_id": "active", "bus_id": "B2", "p_kw": 100, "q_kvar": 0, "phase": "B"},
                              {"load_id": "zero", "bus_id": "B2", "p_kw": 0, "q_kvar": 0, "phase": "A"}])
        late_note = "late_header_sentinel_default_must_remain_visible"
        notes = [late_note] + [f"source_note_{i:03d}_" + "x" * 90 for i in range(60)]
        master = Path(case_converters.build_dss_from_frames(None, pd.DataFrame(data["bus"]),
            pd.DataFrame(data["line"]), str(self.root / "model"), load_df=loads, conversion_notes=notes))
        text = master.read_text(encoding="utf-8")
        self.assertGreater(text.index(late_note), 4096)
        self.assertGreater(text.index("New Circuit."), text.index(late_note))
        case = load_case_bundle(str(master), str(self.root / "task"), str(self.root / "builtin"))
        self.assertEqual(case.source_metadata["load_source"], "explicit_load")
        self.assertEqual(case.source_metadata["input_load_records"], "2")
        self.assertEqual(case.source_metadata["generated_load_records"], "1")
        self.assertEqual(case.source_metadata["zero_load_records"], "1")
        self.assertIn(late_note, case.fidelity_warnings)
        self.assertTrue(any("conn 列已忽略" in item for item in case.fidelity_warnings))
        self.assertTrue(all(note in case.fidelity_warnings for note in notes))

    def test_ignored_multiple_structured_system_rows_report_default_frequency(self):
        members = _csv_bundle()
        members["system.csv"] = b"frequency_hz,s_base_mva\n60,100\n60,100\n"
        attachments = self.upload(members)
        master = Path(chat_api._extract_case_refs("session", attachments)[0])
        text = master.read_text(encoding="utf-8")
        self.assertIn("DefaultBaseFrequency=50", text)
        self.assertIn("提供了 system 表但未识别出任何系统参数", text)
        case = load_case_bundle(str(master), str(self.root / "task"), str(self.root / "builtin"))
        self.assertEqual(case.source_metadata["frequency_hz"], 50)
        self.assertTrue(any("system 表" in item and "整体忽略" in item for item in case.fidelity_warnings))


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ConversionPublicationTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
