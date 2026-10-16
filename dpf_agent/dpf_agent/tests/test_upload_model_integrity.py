"""上传模型保真回归：ZIP 隔离、在线机组、失败重建保留原模型（不启引擎）。"""
from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from starlette.datastructures import UploadFile

from ..web import case_converters, chat_api, chat_store, conversion_cache


def _case_json(kw: float) -> str:
    return json.dumps({
        "bus": [{"bus_id": "B1", "type": "slack", "base_kv": 20},
                {"bus_id": "B2", "type": "pq", "base_kv": 20, "p_kw": kw}],
        "line": [{"line_id": "L1", "from_bus": "B1", "to_bus": "B2",
                  "r_ohm": 0.1, "x_ohm": 0.2}],
    })


def _csv_members(kw: float) -> dict[str, str]:
    return {
        "bus.csv": f"bus_id,type,base_kv,p_kw\nB1,slack,20,0\nB2,pq,20,{kw}\n",
        "line.csv": "line_id,from_bus,to_bus,r_ohm,x_ohm\nL1,B1,B2,0.1,0.2\n",
    }


def _zip_bytes(members: dict[str, str]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return stream.getvalue()


class UploadModelIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="upload-integrity-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.upload_patch = patch.object(chat_store, "UPLOADS_DIR", str(self.uploads))
        self.upload_patch.start()
        self.addCleanup(self.upload_patch.stop)

    def _upload(self, name: str, members: dict[str, str]) -> UploadFile:
        return UploadFile(filename=name, file=io.BytesIO(_zip_bytes(members)))

    def _stale(self, output: Path, source: Path) -> bytes:
        meta = conversion_cache.write_meta(str(output), "dss_from_tables", str(source))
        meta["version"] = "1.6.3"
        path = output / conversion_cache.META_NAME
        path.write_text(json.dumps(meta), encoding="utf-8")
        return path.read_bytes()

    def _old_case(self, members: dict[str, str], *, legacy: bool = False) -> tuple[Path, Path, bytes]:
        folder = self.uploads / "session"
        folder.mkdir(parents=True)
        source = folder / "case.zip"
        source.write_bytes(_zip_bytes(members))
        output = folder / ("converted_csv" if legacy else "case")
        output.mkdir()
        master = output / "Master.dss"
        master.write_text("Clear\nNew Circuit.legacy basekv=20\n", encoding="utf-8")
        metadata = self._stale(output, source)
        return source, master, metadata

    def _assert_old_kept(self, master: Path, model: bytes, metadata: bytes) -> None:
        self.assertEqual(master.read_bytes(), model)
        self.assertEqual((master.parent / conversion_cache.META_NAME).read_bytes(), metadata)

    def test_csv_zip_attachments_keep_distinct_models_and_names(self) -> None:
        attachments = chat_api._process_uploaded_files("session", [
            self._upload("first.zip", _csv_members(100)),
            self._upload("second.zip", _csv_members(900)),
        ])
        refs = chat_api._extract_case_refs("session", attachments)
        self.assertEqual(len(refs), 2)
        self.assertNotEqual(attachments[0]["path"], attachments[1]["path"])
        for ref, name, kw in zip(refs, ("first", "second"), (100, 900)):
            value = float(re.search(r"\bkW=([-+\deE.]+)", Path(ref).read_text(encoding="utf-8"))[1])
            self.assertEqual(value, kw)
            self.assertEqual(Path(ref).parent.name, name)
            self.assertEqual(chat_api._resolve_case_name_to_upload(name, "session"), ref)
            meta = conversion_cache.read_meta(str(Path(ref).parent))
            self.assertEqual(Path(meta["source"]).name, name + ".zip")
            self.assertTrue(meta["source_sha256"])

    def test_json_zip_attachments_keep_distinct_models(self) -> None:
        attachments = chat_api._process_uploaded_files("session", [
            self._upload("first.zip", {"network.json": _case_json(120)}),
            self._upload("second.zip", {"network.json": _case_json(850)}),
        ])
        refs = chat_api._extract_case_refs("session", attachments)
        self.assertEqual(len(refs), 2)
        for ref, kw in zip(refs, (120, 850)):
            value = float(re.search(r"\bkW=([-+\deE.]+)", Path(ref).read_text(encoding="utf-8"))[1])
            self.assertEqual(value, kw)

    def test_matpower_mixed_status_aggregates_only_online_power_and_voltage(self) -> None:
        source = self.root / "mixed.m"
        source.write_text("""mpc.baseMVA = 100;
mpc.bus = [
1 3 0 0 0 0 1 1 0 20 1 1.1 .9;
2 2 1 0 0 0 1 1 0 20 1 1.1 .9;
3 2 1 0 0 0 1 1 0 20 1 1.1 .9;
];
mpc.branch = [
1 2 .01 .02 0 100 0 0 0 0 1;
2 3 .01 .02 0 100 0 0 0 0 1;
];
mpc.gen = [
1 8 0 10 -10 1.02 100 1 10 0;
1 99 9 99 -99 1.8 100 0 99 0;
2 2 1 10 -10 1.03 100 1 10 0;
2 3 2 10 -10 1.03 100 1 10 0;
2 50 40 90 -90 1.9 100 0 90 0;
2 90 70 90 -90 1.9 100 -1 90 0;
3 5 4 10 -10 1.7 100 0 10 0;
];
""", encoding="utf-8")
        master = case_converters.convert_matpower_m(str(source), str(self.root / "mixed"))
        lines = Path(master).read_text(encoding="utf-8").splitlines()
        generators = [line for line in lines if line.startswith("New Generator")]
        self.assertEqual(len(generators), 1)
        self.assertIn("Gen_2 ", generators[0])
        # Numeric identity is the contract; 16 significant digits retain tiny
        # valid inputs and need not print a fixed number of trailing zeroes.
        import re
        self.assertEqual(float(re.search(r"\bkW=([-+\d.eE]+)", generators[0]).group(1)), 5000.)
        self.assertEqual(float(re.search(r"\bkvar=([-+\d.eE]+)", generators[0]).group(1)), 3000.)
        self.assertIn("Model=3 Vpu=1.03", generators[0])
        circuit = next(line for line in lines if line.startswith("New Circuit"))
        self.assertIn("pu=1.02 ", circuit)

    def test_corrupt_zip_rebuild_preserves_model_and_metadata(self) -> None:
        source, master, metadata = self._old_case(_csv_members(100))
        before = master.read_bytes()
        source.write_bytes(b"not a zip")
        kept, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual((kept, status), (str(master), "stale-unresolved"))
        self._assert_old_kept(master, before, metadata)

    def test_conversion_failure_preserves_model_and_metadata(self) -> None:
        _source, master, metadata = self._old_case({"invalid.json": "{}"})
        before = master.read_bytes()
        kept, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual((kept, status), (str(master), "stale-unresolved"))
        self._assert_old_kept(master, before, metadata)

    def test_publish_failure_restores_model_and_metadata(self) -> None:
        _source, master, metadata = self._old_case(_csv_members(100))
        before = master.read_bytes()
        original_replace = os.replace

        def fail_new_model(src: str, dst: str) -> None:
            if Path(src).name == "model":
                raise OSError("injected publish failure")
            original_replace(src, dst)

        with patch.object(conversion_cache.os, "replace", side_effect=fail_new_model):
            kept, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual((kept, status), (str(master), "stale-unresolved"))
        self._assert_old_kept(master, before, metadata)

    def test_metadata_failure_preserves_model_and_metadata(self) -> None:
        _source, master, metadata = self._old_case(_csv_members(100))
        before = master.read_bytes()
        with patch.object(conversion_cache, "write_meta", side_effect=OSError("injected metadata failure")):
            kept, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual((kept, status), (str(master), "stale-unresolved"))
        self._assert_old_kept(master, before, metadata)

    def test_successful_zip_rebuild_has_durable_zip_source(self) -> None:
        source, master, _metadata = self._old_case(_csv_members(700))
        rebuilt, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual(rebuilt, str(master))
        self.assertEqual(status, "rebuilt")
        self.assertRegex(master.read_text(encoding="utf-8"), r"\bkW=700(?:\.0+)?(?=\s|$)")
        meta = conversion_cache.read_meta(str(master.parent))
        self.assertEqual(meta["source"], str(source))
        self.assertEqual(meta["source_sha256"], conversion_cache._sha256_file(str(source)))
        self.assertEqual(meta["version"], conversion_cache.CONVERTER_VERSIONS["dss_from_tables"])
        self.assertEqual(conversion_cache.refresh_master_if_stale(str(master)), (str(master), "fresh"))
        self.assertFalse(any(".zip-rebuild-" in name.name for name in source.parent.iterdir()))

    def test_json_zip_rebuild_has_durable_zip_source(self) -> None:
        source, master, _metadata = self._old_case({"model.json": _case_json(420)})
        rebuilt, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual((rebuilt, status), (str(master), "rebuilt"))
        self.assertRegex(master.read_text(encoding="utf-8"), r"\bkW=420(?:\.0+)?(?=\s|$)")
        self.assertEqual(conversion_cache.read_meta(str(master.parent))["source"], str(source))
        self.assertEqual(conversion_cache.refresh_master_if_stale(str(master)), (str(master), "fresh"))

    def test_failed_rollback_keeps_original_data_in_backup(self) -> None:
        source, master, metadata = self._old_case(_csv_members(100))
        before = master.read_bytes()
        original_replace = os.replace

        def fail_publish_and_rollback(src: str, dst: str) -> None:
            if Path(src).name in {"model", "previous"}:
                raise OSError("injected publish and rollback failure")
            original_replace(src, dst)

        with patch.object(conversion_cache.os, "replace", side_effect=fail_publish_and_rollback):
            kept, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual((kept, status), (str(master), "stale-unresolved"))
        backup = next(source.parent.glob(".zip-rebuild-*/previous"))
        self.assertEqual((backup / "Master.dss").read_bytes(), before)
        self.assertEqual((backup / conversion_cache.META_NAME).read_bytes(), metadata)
        original_replace(str(backup), str(master.parent))

    def test_legacy_shared_directory_path_and_named_reference_stay_usable(self) -> None:
        source, master, _metadata = self._old_case(_csv_members(350), legacy=True)
        old_extract = source.parent / "case"
        old_extract.mkdir()
        sentinel = old_extract / "keep.txt"
        sentinel.write_text("other historical files", encoding="utf-8")
        resolved = chat_api._resolve_case_name_to_upload("case", "session")
        self.assertEqual(resolved, str(master))
        self.assertRegex(master.read_text(encoding="utf-8"), r"\bkW=350(?:\.0+)?(?=\s|$)")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "other historical files")
        self.assertEqual(conversion_cache.read_meta(str(master.parent))["source"], str(source))

    def test_nested_dss_zip_rebuild_preserves_relative_dependencies(self) -> None:
        attachments = chat_api._process_uploaded_files("session", [self._upload("nested.zip", {
            "nested/Master.dss": "Clear\nRedirect ../shared/Lines.dss\n",
            "shared/Lines.dss": "New Circuit.original basekv=20\n",
        })])
        master = Path(chat_api._extract_case_refs("session", attachments)[0])
        source = self.uploads / "session" / "nested.zip"
        self._stale(master.parent, source)
        source.write_bytes(_zip_bytes({
            "nested/Master.dss": "Clear\nRedirect ../shared/Lines.dss\n",
            "shared/Lines.dss": "New Circuit.updated basekv=20\n",
        }))
        rebuilt, status = conversion_cache.refresh_master_if_stale(str(master))
        self.assertEqual(rebuilt, str(master))
        self.assertIn(status, ("rebuilt", "rebuilt-same"))
        dependency = master.parent.parent / "shared" / "Lines.dss"
        self.assertIn("updated", dependency.read_text(encoding="utf-8"))
        self.assertEqual(chat_api._resolve_case_name_to_upload("nested", "session"), str(master))
        self.assertEqual(conversion_cache.read_meta(str(master.parent))["source"], str(source))


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(UploadModelIntegrityTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
