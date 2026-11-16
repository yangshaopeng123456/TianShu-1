"""真实消息路由、SQLite 与上传文件；只替换最终求解入口，不启动原生引擎或网络。"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from starlette.datastructures import UploadFile

from ..config import AgentConfig
from ..web import chat_api, chat_store


MODEL = b"Clear\nNew Circuit.Routing basekv=20\nNew Load.L1 bus1=b1 phases=3 kV=20 kW=100 kvar=0\n"


class CaseNameRoutingTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("DPF_TEST_ARTIFACT_ROOT")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="case-name-routing-", dir=base))
        self.cases = self.root / "cases"
        self.cases.mkdir()
        self.chat = self.root / "chat"
        self.chat.mkdir()
        self.uploads = self.chat / "uploads"
        self.uploads.mkdir()
        for name, value in {"DB_DIR": str(self.chat), "DB_PATH": str(self.chat / "chat.db"),
                            "UPLOADS_DIR": str(self.uploads)}.items():
            stub = patch.object(chat_store, name, value)
            stub.start()
            self.addCleanup(stub.stop)
        self.config = AgentConfig(cases_root=str(self.cases), workspace_root=str(self.root / "tasks"),
                                  outputs_root=str(self.root / "outputs"), llm_enabled=False,
                                  rag_enabled=False, intent_mode="off", conversion_cache_enabled=False)
        cfg = patch.object(chat_api, "AgentConfig", return_value=self.config)
        cfg.start()
        self.addCleanup(cfg.stop)
        for name in chat_api._BUILTIN_CASE_NAMES:
            self.builtin(name)
        chat_store.init_db()
        self.session = chat_store.create_session("名称路由离线行为")
        self.calls = []

        def run(message, session_id, content, case_ref, **kwargs):
            task_id = f"pf-routing-{len(self.calls) + 1}"
            self.calls.append({"case_ref": case_ref, "content": content,
                               "success_prefix": kwargs.get("success_prefix", "")})
            # 只记录路由目标；这不是数值结果或成功求解证明。
            reply = kwargs.get("success_prefix", "") + "离线求解入口已记录目标。"
            chat_store.update_message_task(message.id, task_id=task_id, task_case_ref=case_ref,
                                           task_status="success", content=reply)
            return task_id, "success", reply

        solver = patch.object(chat_api, "_run_case_into_message", side_effect=run)
        solver.start()
        self.addCleanup(solver.stop)
        self.addCleanup(lambda: (self.root / "route_calls.json").write_text(
            json.dumps(self.calls, ensure_ascii=False, indent=2), encoding="utf-8"))

    def builtin(self, name):
        directory = self.cases / name
        directory.mkdir(exist_ok=True)
        master = directory / "Master.dss"
        master.write_bytes(MODEL)
        return master

    def uploaded(self, name, *, other_session=None):
        directory = self.uploads / (other_session or self.session.id) / name
        directory.mkdir(parents=True, exist_ok=True)
        master = directory / "Master.dss"
        master.write_bytes(MODEL)
        return master

    def send(self, text, files=None):
        response = chat_api.send_message(self.session.id, content=text, files=files or [])
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["assistant_message"]["session_id"], self.session.id)
        return payload["assistant_message"]

    def rejected(self, text, name):
        before = len(self.calls)
        answer = self.send(text)
        self.assertEqual(len(self.calls), before, "未知身份不得沿用另一个模型启动求解")
        self.assertIn(f"`{name}`", answer["content"])
        self.assertIn("找不到", answer["content"])
        self.assertIsNone(answer["task_id"])

    def test_complete_unknown_names_never_become_builtin_prefixes(self):
        for name in ("case40", "case45", "case4_custom", "case_tx_alt", "ieee330"):
            with self.subTest(name=name):
                self.assertIsNone(chat_api._extract_case_name_from_text(f"计算{name}的潮流"))
                self.rejected(f"计算{name}的潮流", name)

    def test_missing_full_name_never_selects_uploaded_shorter_alias(self):
        self.uploaded("case4")
        self.uploaded("case_tx")
        for name in ("case40", "case45", "case4_custom", "case_tx_alt", "case40.json"):
            with self.subTest(name=name):
                self.rejected(f"计算 {name}", name)

    def test_exact_uploaded_custom_names_select_their_own_model(self):
        self.uploaded("case4")
        self.uploaded("case_tx")
        for name in ("case40", "case45", "case4_custom", "case_tx_alt", "case_11_x", "my_grid"):
            with self.subTest(name=name):
                master = self.uploaded(name)
                self.send(f"计算{name}的潮流")
                self.assertEqual(self.calls[-1]["case_ref"], str(master))

    def test_uppercase_and_adjacent_chinese_keep_full_identity(self):
        master = self.uploaded("CASE40_Custom")
        self.send("请计算CASE40_CUSTOM的潮流")
        self.assertEqual(self.calls[-1]["case_ref"], str(master))
        self.send("请计算CASE4的潮流")
        self.assertEqual(self.calls[-1]["case_ref"], "case4")

    def test_ieee_space_alias_and_case_space_alias_remain_supported(self):
        self.send("计算 IEEE 33 的潮流")
        self.assertEqual(self.calls[-1]["case_ref"], "ieee33")
        self.send("计算 CASE 4 的潮流")
        self.assertEqual(self.calls[-1]["case_ref"], "case4")

    def test_sentence_punctuation_does_not_hide_available_builtin(self):
        for text in ("计算case4.", "计算 case4。", "计算case_tx.", "计算case_tx。"):
            with self.subTest(text=text):
                self.send(text)
                self.assertEqual(self.calls[-1]["case_ref"], "case_tx" if "case_tx" in text else "case4")

    def test_uploaded_same_builtin_name_has_priority_over_builtin(self):
        master = self.uploaded("case4")
        self.send("计算case4")
        self.assertEqual(self.calls[-1]["case_ref"], str(master))

    def test_full_filename_selects_matching_format_and_bare_stem_is_ambiguous(self):
        json_master = self.uploaded("_converted/case40.json")
        csv_master = self.uploaded("_converted/case40.csv")
        answer = self.send("计算case40")
        self.assertEqual(self.calls, [])
        self.assertIn("多个已上传模型", answer["content"])
        self.assertIn("完整文件名和扩展名", answer["content"])
        self.send("计算CASE40.JSON")
        self.assertEqual(self.calls[-1]["case_ref"], str(json_master))
        self.send("计算case40.csv")
        self.assertEqual(self.calls[-1]["case_ref"], str(csv_master))

    def test_filename_with_builtin_stem_does_not_use_builtin(self):
        master = self.uploaded("_converted/case4.json")
        self.send("计算case4.json")
        self.assertEqual(self.calls[-1]["case_ref"], str(master))
        self.rejected("计算 case4.csv", "case4.csv")

    def test_attachment_wins_over_text_reference_to_missing_or_builtin_model(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Master.dss", MODEL)
        buffer.seek(0)
        file = UploadFile(filename="case40.zip", file=buffer)
        self.addCleanup(buffer.close)
        answer = self.send("计算附件；case4和case40_result.json仅供说明，不要使用", [file])
        self.assertEqual(len(self.calls), 1)
        attachment = chat_store.get_session(self.session.id).messages[0].attachments[0]
        self.assertEqual(Path(self.calls[0]["case_ref"]), Path(attachment["path"]) / "Master.dss")
        self.assertIsNotNone(answer["task_id"])

    def test_unknown_name_after_success_rejects_without_reusing_history(self):
        self.send("计算case4")
        for name in ("case40", "case_tx_alt", "missing_grid"):
            with self.subTest(name=name):
                self.rejected(f"重新计算 {name}", name)
        self.assertEqual([call["case_ref"] for call in self.calls], ["case4"])

    def test_rerun_uses_latest_named_task_instead_of_older_uploaded_model(self):
        old = self.uploaded("case4")
        current = self.uploaded("case40")
        chat_store.add_message(self.session.id, "user", "旧附件", attachments=[{
            "filename": "old.zip", "path": str(old.parent)}])
        self.send("计算case40")
        self.assertEqual(self.calls[-1]["case_ref"], str(current))
        self.send("重新计算")
        self.assertEqual(self.calls[-1]["case_ref"], str(current))

    def test_current_session_exact_name_beats_other_session(self):
        remote = self.uploaded("case40", other_session="another-session")
        local = self.uploaded("case40")
        self.send("计算case40")
        self.assertEqual(self.calls[-1]["case_ref"], str(local))
        self.assertNotEqual(self.calls[-1]["case_ref"], str(remote))

    def test_missing_master_removes_builtin_from_candidates_and_routing(self):
        (self.cases / "case_tx" / "Master.dss").unlink()
        self.assertFalse(chat_api._is_known_builtin_case("CASE_TX"))
        self.assertNotIn("case_tx", [item["id"] for item in chat_api._case_candidates(self.session.id)])
        self.rejected("计算case_tx", "case_tx")

    def test_public_synthetic_probes_are_recognized_as_complete_names(self):
        for name in ("bus_reference_probe", "pvsystem_probe", "storage_discharge_probe"):
            with self.subTest(name=name):
                self.send(f"计算{name}的潮流")
                self.assertEqual(self.calls[-1]["case_ref"], name)

    def test_default_with_ieee_present_uses_ieee_and_truthful_note(self):
        answer = self.send("计算潮流")
        self.assertEqual(self.calls[-1]["case_ref"], "ieee33")
        self.assertIn("默认使用内置 `ieee33`", answer["content"])

    def test_public_tree_without_ieee_defaults_to_case4_and_updates_hints(self):
        (self.cases / "ieee33" / "Master.dss").unlink()
        answer = self.send("计算潮流")
        self.assertEqual(self.calls[-1]["case_ref"], "case4")
        self.assertIn("默认使用内置 `case4`", answer["content"])
        for text in ("你好", "天气", "不理解的句子"):
            with self.subTest(text=text):
                hint = chat_api._casual_reply(text)
                self.assertIn("case4", hint)
                self.assertNotIn("ieee33", hint.lower())
        self.rejected("计算 IEEE 33", "ieee33")

    def test_no_available_builtin_never_starts_a_fake_default(self):
        for master in self.cases.glob("*/Master.dss"):
            master.unlink()
        answer = self.send("计算潮流")
        self.assertEqual(self.calls, [])
        self.assertIn("没有可用的内置算例", answer["content"])
        self.assertIsNone(chat_api._default_builtin_case())

    def test_unsupported_upload_hint_only_recommends_real_public_builtin(self):
        (self.cases / "ieee33" / "Master.dss").unlink()
        buffer = io.BytesIO(b"not a power-flow model")
        self.addCleanup(buffer.close)
        answer = self.send("计算附件", [UploadFile(filename="notes.txt", file=buffer)])
        self.assertEqual(self.calls, [])
        self.assertIn("计算 case4", answer["content"])
        self.assertNotIn("ieee33", answer["content"].lower())


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CaseNameRoutingTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
