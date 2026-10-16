"""Real SQLite/files/ASGI with controlled runner events; no solver or server.

These tests exercise durable scheduling and HTTP contracts. Controlled runner
responses are explicitly not numerical/OpenDSS validation. Fixtures remain under
DPF_TEST_ARTIFACT_ROOT or the system temporary directory for inspection.
"""
from __future__ import annotations

import io
import asyncio
import json
import os
from pathlib import Path
import threading
import tempfile
import time
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import zipfile

from fastapi import FastAPI, HTTPException

from ..application.service import TaskBusyError
from ..schemas.enums import ResultScope, OutputFormat
from ..web import batch_scheduler, batch_store, chat_store, chat_api


class ASGIClient:
    """Invoke the real ASGI app without sockets or optional httpx test packages."""
    def __init__(self, app):
        self.app = app

    def close(self):
        pass

    def request(self, method, path, *, data=None, files=None, json=None):
        headers = [(b"host", b"offline-asgi")]
        body = b""
        if files:
            boundary = "batch-boundary-" + uuid.uuid4().hex
            parts = []
            for name, value in (data or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            for name, (filename, content, kind) in files:
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {kind}\r\n\r\n'.encode() + content + b"\r\n")
            body = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers.append((b"content-type", f"multipart/form-data; boundary={boundary}".encode()))
        elif json is not None:
            import json as json_module
            body = json_module.dumps(json).encode()
            headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode()))
        async def invoke():
            sent = False
            events = []
            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()
            async def send(event):
                events.append(event)
            await self.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                "query_string": b"", "headers": headers, "server": ("offline-asgi", 80), "client": ("local", 1)}, receive, send)
            status = next(event["status"] for event in events if event["type"] == "http.response.start")
            payload = b"".join(event.get("body", b"") for event in events if event["type"] == "http.response.body")
            return SimpleNamespace(status_code=status, text=payload.decode(), json=lambda: __import__("json").loads(payload))
        return asyncio.run(invoke())

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("Timed out waiting for controlled batch state")


def native(name):
    return f"Clear\nNew Circuit.{name} basekv=20\nNew Load.L1 bus1=b1 phases=3 kV=20 kW=100 kvar=0\n"


class BatchSchedulerTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("DPF_TEST_ARTIFACT_ROOT") or Path(tempfile.gettempdir()) / "tianshu-tests")
        self.root = base / "unit_batch" / (time.strftime("%H%M%S") + "_" + uuid.uuid4().hex[:8]) / self._testMethodName
        self.root.mkdir(parents=True)
        for name, value in {"DB_DIR": str(self.root / "chat"), "DB_PATH": str(self.root / "chat" / "chat.db"),
                            "UPLOADS_DIR": str(self.root / "chat" / "uploads")}.items():
            mocked = patch.object(chat_store, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        Path(chat_store.DB_DIR).mkdir()
        Path(chat_store.UPLOADS_DIR).mkdir()
        chat_store.init_db()
        self.session = chat_store.create_session("批次离线验证")
        self.other = chat_store.create_session("另一会话")
        self.runners = []
        self.gates = []
        self.cancelled = []
        self.calls = []
        app = FastAPI()
        app.include_router(chat_api.router)
        self.client = ASGIClient(app)
        self.addCleanup(self.client.close)
        self.singleton = patch.object(batch_scheduler, "_scheduler", None)
        self.singleton.start()
        self.addCleanup(self.singleton.stop)
        closing_patch = patch.object(batch_scheduler, "_closing", False)
        closing_patch.start()
        self.addCleanup(closing_patch.stop)
        self.addCleanup(self.cleanup_runners)

    def cleanup_runners(self):
        for gate in self.gates:
            gate.set()
        for scheduler in self.runners:
            scheduler.close(timeout=2)

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def model(self, name):
        root = self.root / name
        root.mkdir()
        source = root / "Master.dss"
        source.write_text(native(name), encoding="utf-8")
        return str(source)

    def create(self, names=("A", "B"), *, sid=None, solver_config=None):
        sid = sid or self.session.id
        refs = [self.model(name) for name in names]
        bid, items = batch_scheduler.prepare_inputs(refs, chat_api._case_display_name, str(self.root / "cases"))
        user = chat_store.add_message(sid, "user", "计算这几个算例")
        return batch_store.create(sid, user.id, user.content, items, ["bus", "system"], ["html"], batch_id=bid,
                                  solver_config=solver_config)

    def start(self, execute, *, recover=False):
        worker = batch_scheduler.BatchScheduler(execute, cancel_task=self.cancelled.append, idle_seconds=.01)
        worker.start(recover=recover)
        self.runners.append(worker)
        batch_scheduler._scheduler = worker
        return worker

    def result(self, item, callback, *, status="success", gate=None):
        self.calls.append(item["display_name"])
        tid = "controlled-" + uuid.uuid4().hex[:12]
        callback(tid)
        if gate:
            gate.wait(4)
        if tid in self.cancelled:
            status = "cancelled"
        return {"task_id": tid, "task_status": status, "report_url": f"/api/tasks/{tid}/report",
                "artifacts": {}, "content": item["display_name"] + " " + status}

    def get(self, batch):
        return batch_store.get(batch["session_id"], batch["id"])

    def done(self, batch):
        return wait_for(lambda: (value if value["status"] not in {"queued", "running", "cancelling", "paused"} else None)
                        if (value := self.get(batch)) else None)

    def url(self, batch, suffix="", sid=None):
        return f"/api/chat/sessions/{sid or batch['session_id']}/batches/{batch['id']}" + ("/" + suffix if suffix else "")

    def test_real_multipart_returns_202_before_runner_finishes_and_snapshots_inputs(self):
        gate = self.gate()
        self.start(lambda item, callback: self.result(item, callback, gate=gate))
        def archive(name):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as zipped:
                zipped.writestr("Master.dss", native(name))
            return stream.getvalue()
        response = self.client.post(f"/api/chat/sessions/{self.session.id}/messages", data={"content": "计算这两个算例"},
            files=[("files", ("first.zip", archive("A"), "application/zip")),
                   ("files", ("second.zip", archive("B"), "application/zip"))])
        self.assertEqual(response.status_code, 202, response.text)
        batch = response.json()["batch"]
        wait_for(lambda: self.calls)
        self.assertFalse(gate.is_set())
        snapshot = Path(batch["items"][0]["case_ref"])
        original = Path(batch["items"][0]["original_case_ref"])
        self.assertTrue(snapshot.is_relative_to(Path(chat_store.DB_DIR) / "batches"))
        original.write_text(native("REPLACED"), encoding="utf-8")
        self.assertIn("New Circuit.A", snapshot.read_text(encoding="utf-8"))
        session = self.client.get(f"/api/chat/sessions/{self.session.id}").json()
        self.assertEqual(session["batches"][0]["id"], batch["id"])
        self.assertTrue(any(item["task_id"] for item in self.get(batch)["items"]))
        gate.set()
        ended = self.done(batch)
        self.assertEqual(ended["counts"]["success"], 2)
        self.assertEqual(len({item["message_id"] for item in ended["items"]}), 2)
        for item in ended["items"]:
            self.assertEqual(item["message"]["session_id"], self.session.id)
            self.assertEqual(item["message"]["task_id"], item["task_id"])
            self.assertEqual(item["message"]["task_status"], "success")

    def test_single_uploaded_case_cannot_be_replaced_by_mentioned_builtin(self):
        calls = []
        def capture(message, session_id, content, case_ref, **kwargs):
            calls.append(case_ref)
            tid = "controlled-" + uuid.uuid4().hex[:12]
            chat_store.update_message_task(message.id, task_id=tid, task_case_ref=case_ref,
                                           task_status="success", content="captured input")
            return tid, "success", "captured input"
        with patch.object(chat_api, "_run_case_into_message", side_effect=capture):
            for name in ("IEEE33", "case4"):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w") as archive:
                    archive.writestr("Master.dss", native("uploaded_" + name))
                response = self.client.post(f"/api/chat/sessions/{self.session.id}/messages",
                    data={"content": f"请计算我上传的{name}算例，最大迭代次数100"},
                    files=[("files", ("uploaded_" + name + ".zip", stream.getvalue(), "application/zip"))])
                self.assertEqual(response.status_code, 200, response.text)
                value = response.json()
                reference = Path(calls[-1])
                self.assertTrue(reference.is_relative_to(Path(chat_store.UPLOADS_DIR) / self.session.id))
                self.assertIn("New Circuit.uploaded_" + name, reference.read_text(encoding="utf-8"))
                self.assertEqual(value["assistant_message"]["task_case_ref"], str(reference))

    def test_multiple_uploads_named_builtin_still_queue_every_uploaded_case(self):
        self.start(lambda item, callback: self.result(item, callback))
        files = []
        for name in ("A", "B"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("Master.dss", native("uploaded_" + name))
            files.append(("files", ("input_" + name + ".zip", stream.getvalue(), "application/zip")))
        response = self.client.post(f"/api/chat/sessions/{self.session.id}/messages",
            data={"content": "请计算上传的这两个IEEE33算例，最大迭代次数100"}, files=files)
        self.assertEqual(response.status_code, 202, response.text)
        batch = response.json()["batch"]
        ended = self.done(batch)
        self.assertEqual(ended["counts"]["success"], 2)
        self.assertEqual(self.calls, ["input_A.zip", "input_B.zip"])
        for item, suffix in zip(ended["items"], ("A", "B")):
            self.assertIn("New Circuit.uploaded_" + suffix, Path(item["case_ref"]).read_text(encoding="utf-8"))

    def test_no_new_attachment_still_allows_explicit_builtin_switch(self):
        old = self.model("old_uploaded")
        chat_store.add_message(self.session.id, "user", "上传了旧算例", attachments=[{
            "filename": "old_uploaded.zip", "path": str(Path(old).parent)}])
        calls = []
        def capture(message, session_id, content, case_ref, **kwargs):
            calls.append(case_ref)
            return "controlled-switch", "success", "captured input"
        with patch.dict(os.environ, {"DPF_INTENT_MODE": "off", "DPF_LLM_ENABLED": "0"}), \
                patch.object(chat_api, "_run_case_into_message", side_effect=capture):
            # Direct call avoids an empty multipart's optional-form encoding;
            # SQLite history and actual case routing are still exercised.
            response = chat_api.send_message(self.session.id, "改用内置case4计算", [])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["case4"])

    def test_fifo_failure_continues_and_retry_only_failed_keeps_old_attempt(self):
        batch = self.create(("A", "B", "C"))
        active = [0, 0]
        def execute(item, callback):
            active[0] += 1
            active[1] = max(active)
            try:
                status = "system_failed" if item["display_name"] == "B" and self.calls.count("B") == 0 else "success"
                return self.result(item, callback, status=status)
            finally:
                active[0] -= 1
        worker = self.start(execute)
        worker.notify()
        ended = self.done(batch)
        self.assertEqual(self.calls, ["A", "B", "C"])
        self.assertEqual(active[1], 1)
        previous = ended["items"][1]
        previous_message = chat_store.get_message(previous["message_id"], self.session.id).to_dict()
        retry = self.client.post(self.url(batch, "retry-failed"), json={})
        self.assertEqual(retry.status_code, 200, retry.text)
        ended = wait_for(lambda: value if (value := self.get(batch))["status"] == "success" else None)
        self.assertEqual(self.calls, ["A", "B", "C", "B"])
        self.assertEqual(len(ended["items"][1]["attempts"]), 2)
        self.assertEqual(chat_store.get_message(previous["message_id"], self.session.id).to_dict(), previous_message)
        self.assertNotEqual(ended["items"][1]["message_id"], previous["message_id"])
        self.assertEqual(ended["items"][0]["task_id"], ended["items"][0]["attempts"][0]["task_id"])

    def test_cancel_queued_item_never_calls_runner_and_other_item_finishes(self):
        batch = self.create(("A", "B", "C"))
        gate = self.gate()
        self.start(lambda item, callback: self.result(item, callback, gate=gate if item["position"] == 1 else None))
        wait_for(lambda: self.get(batch)["items"][0]["task_id"])
        item = batch["items"][1]
        response = self.client.post(self.url(batch, f"items/{item['id']}/cancel"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][1]["status"], "cancelled")
        self.assertEqual(self.cancelled, [])
        gate.set()
        ended = self.done(batch)
        self.assertEqual(self.calls, ["A", "C"])
        self.assertIsNone(ended["items"][1]["task_id"])

    def test_cancel_batch_is_ack_until_actual_terminal_and_cancels_all_pending(self):
        batch = self.create(("A", "B"))
        gate = self.gate()
        self.start(lambda item, callback: self.result(item, callback, gate=gate))
        tid = wait_for(lambda: self.get(batch)["items"][0]["task_id"])
        response = self.client.post(self.url(batch, "cancel"))
        self.assertEqual(response.status_code, 200)
        acknowledged = response.json()
        self.assertEqual(acknowledged["status"], "cancelling")
        self.assertEqual([i["status"] for i in acknowledged["items"]], ["cancelling", "cancelled"])
        self.assertEqual(self.cancelled, [tid])
        gate.set()
        self.assertEqual(self.done(batch)["status"], "cancelled")
        self.assertEqual(self.calls, ["A"])
        self.assertEqual(self.client.post(self.url(batch, "retry-failed")).status_code, 409)

    def test_cancel_between_claim_and_task_callback_binds_only_its_real_task(self):
        batch = self.create(("A",))
        entered, release = self.gate(), self.gate()
        def execute(item, callback):
            entered.set()
            release.wait(4)
            return self.result(item, callback)
        self.start(execute)
        self.assertTrue(entered.wait(3))
        response = self.client.post(self.url(batch, "cancel"))
        self.assertEqual(response.json()["status"], "cancelling")
        self.assertEqual(self.cancelled, [])
        release.set()
        ended = self.done(batch)
        self.assertEqual(ended["status"], "cancelled")
        self.assertEqual(self.cancelled, [ended["items"][0]["task_id"]])

    def test_cross_session_all_routes_and_foreign_item_reject_without_mutation(self):
        batch = self.create(("A",))
        other = self.create(("Other",), sid=self.other.id)
        worker = batch_scheduler.BatchScheduler(lambda item, callback: self.result(item, callback), cancel_task=self.cancelled.append)
        self.runners.append(worker)
        batch_scheduler._scheduler = worker
        before = self.get(batch)
        urls = [self.url(batch, sid=self.other.id)]
        response = self.client.get(urls[0])
        self.assertEqual(response.status_code, 404)
        for action in ("cancel", "resume", "retry-failed", f"items/{batch['items'][0]['id']}/cancel"):
            response = self.client.post(self.url(batch, action, sid=self.other.id))
            self.assertEqual(response.status_code, 404, (action, response.text))
        response = self.client.post(self.url(batch, f"items/{other['items'][0]['id']}/cancel"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.get(batch), before)
        self.assertEqual(self.cancelled, [])

    def test_restart_interrupts_running_pauses_queue_resume_runs_only_unstarted(self):
        batch = self.create(("A", "B", "C"))
        first = batch_store.claim("old-backend")
        batch_store.bind_task(first, "old-backend", "old-success")
        batch_store.complete(first, "old-backend", {"task_id": "old-success", "task_status": "success", "content": "A success"})
        active = batch_store.claim("old-backend")
        batch_store.bind_task(active, "old-backend", "old-running")
        self.start(lambda item, callback: self.result(item, callback), recover=True)
        recovered = self.get(batch)
        self.assertEqual([i["status"] for i in recovered["items"]], ["success", "interrupted", "queued"])
        self.assertEqual(recovered["status"], "paused")
        self.assertFalse(self.calls)
        self.assertEqual(self.client.post(self.url(batch, "retry-failed")).status_code, 409)
        self.assertEqual(self.client.post(self.url(batch, "resume")).status_code, 200)
        ended = self.done(batch)
        self.assertEqual(self.calls, ["C"])
        self.assertEqual(ended["items"][1]["task_id"], "old-running")
        self.assertEqual(self.client.post(self.url(batch, "retry-failed")).status_code, 200)
        ended = wait_for(lambda: value if (value := self.get(batch))["status"] == "success" else None)
        self.assertEqual(self.calls, ["C", "B"])
        self.assertEqual(len(ended["items"][1]["attempts"]), 2)
        self.assertFalse(batch_store.complete(active, "old-backend", {"task_id": "old-running", "task_status": "success", "content": "LATE"}))
        self.assertNotIn("LATE", self.get(batch)["items"][1]["message"]["content"])

    def test_shutdown_fences_late_cancel_response_as_interrupted_never_cancelled(self):
        batch = self.create(("A", "B"))
        gate = self.gate()
        worker = self.start(lambda item, callback: self.result(item, callback, gate=gate))
        wait_for(lambda: self.get(batch)["items"][0]["task_id"])
        worker.close(timeout=0)
        self.assertEqual(self.get(batch)["items"][0]["status"], "interrupted")
        gate.set()
        worker._thread.join(2)
        self.assertEqual(self.get(batch)["items"][0]["status"], "interrupted")
        self.assertEqual(self.get(batch)["items"][0]["message"]["task_status"], "interrupted")
        self.assertEqual(self.calls, ["A"])

    def test_busy_not_started_is_queued_again_without_fake_task_or_failed_attempt(self):
        batch = self.create(("A",))
        attempts = []
        def execute(item, callback):
            attempts.append(item["id"])
            if len(attempts) < 3:
                raise TaskBusyError("controlled unrelated single-case engine owner")
            return self.result(item, callback)
        self.start(execute)
        ended = self.done(batch)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.calls, ["A"])
        self.assertEqual(len(ended["items"][0]["attempts"]), 1)
        self.assertEqual(ended["items"][0]["attempts"][0]["status"], "success")

    def test_session_delete_cancels_owned_task_and_blocks_late_message_recreation(self):
        batch = self.create(("A", "B"))
        gate = self.gate()
        worker = self.start(lambda item, callback: self.result(item, callback, gate=gate))
        tid = wait_for(lambda: self.get(batch)["items"][0]["task_id"])
        response = self.client.delete(f"/api/chat/sessions/{self.session.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cancelled, [tid])
        gate.set()
        wait_for(lambda: worker._active_task is None)
        self.assertIsNone(self.get(batch))
        with chat_store.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (self.session.id,)).fetchone()[0], 0)

    def test_real_chat_runner_callback_and_atomic_result_keep_single_case_contract(self):
        batch = self.create(("A",))
        run_calls = []
        def run(request, cli_callback=None, *, on_task_started=None):
            run_calls.append((request, cli_callback))
            on_task_started("controlled-actual-task")
            return {"task_id": "controlled-actual-task", "status": "cancelled", "error": {"message": "owned cancel"}}
        with patch.object(chat_api, "_get_service", return_value=SimpleNamespace(run=run)):
            self.start(chat_api._execute_batch_item)
            ended = self.done(batch)
        self.assertEqual(len(run_calls), 1)
        self.assertEqual(ended["items"][0]["task_id"], "controlled-actual-task")
        self.assertIn("已取消", ended["items"][0]["message"]["content"])
        # Existing single-case callers still pass exactly the two original args.
        legacy = SimpleNamespace(run=lambda request, cli_callback: {"task_id": "single", "status": "cancelled"})
        with patch.object(chat_api, "_get_service", return_value=legacy), patch.object(chat_store, "update_message_task") as save:
            chat_api._run_case_into_message(SimpleNamespace(id="one"), self.session.id, "计算A", "A",
                result_scopes=[ResultScope.BUS], output_formats=[OutputFormat.HTML])
            self.assertEqual(save.call_args.kwargs["task_id"], "single")

    def test_global_shutdown_does_not_resurrect_worker_from_late_http_action(self):
        batch = self.create(("A",))
        worker = batch_scheduler.BatchScheduler(lambda item, callback: self.result(item, callback))
        batch_scheduler._scheduler = worker
        batch_scheduler.close()
        with self.assertRaisesRegex(RuntimeError, "正在关闭"):
            batch_scheduler.current(lambda *args: self.fail("late request started worker"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.get(batch)["items"][0]["status"], "queued")

    def test_independent_process_exit_leaves_durable_task_for_explicit_recovery(self):
        batch = self.create(("A", "B"))
        script = self.root / "controlled_crash.py"
        script.write_text("from dpf_agent.web import chat_store, batch_store\n"
            "import os\nchat_store.init_db()\nitem = batch_store.claim('independent-test-process')\n"
            "batch_store.bind_task(item, 'independent-test-process', 'controlled-crashed-task')\n"
            "print('controlled child committed running task then exited 23', flush=True)\nos._exit(23)\n", encoding="utf-8")
        env = {**os.environ, "DPF_CHAT_DB_DIR": chat_store.DB_DIR, "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(Path(chat_api.__file__).resolve().parents[2])}
        child = subprocess.run([sys.executable, "-B", str(script)], env=env, capture_output=True, timeout=10)
        (self.root / "controlled_crash.stdout").write_bytes(child.stdout)
        (self.root / "controlled_crash.stderr").write_bytes(child.stderr)
        self.assertEqual(child.returncode, 23)
        self.assertEqual(self.get(batch)["items"][0]["status"], "running")
        self.start(lambda item, callback: self.result(item, callback), recover=True)
        recovered = self.get(batch)
        self.assertEqual(recovered["items"][0]["status"], "interrupted")
        self.assertEqual(recovered["items"][0]["task_id"], "controlled-crashed-task")
        self.assertEqual(recovered["status"], "paused")
        self.assertEqual(self.calls, [])

    def test_explicit_solver_options_are_persisted_and_not_reparsed_in_worker(self):
        batch = self.create(("A",), solver_config={"max_iterations": 17, "tolerance": .00001, "solve_mode": "snapshot"})
        observed = []
        def run(request, cli_callback=None, *, on_task_started=None):
            observed.append(request.solver_config.model_dump())
            on_task_started("controlled-frozen-options")
            return {"task_id": "controlled-frozen-options", "status": "cancelled"}
        with patch.object(chat_api, "_get_service", return_value=SimpleNamespace(run=run)), \
             patch.object(chat_api, "_parse_solver_config_from_text", side_effect=AssertionError("frozen options must not be reparsed")):
            self.start(chat_api._execute_batch_item)
            self.done(batch)
        self.assertEqual(observed, [{"max_iterations": 17, "tolerance": .00001, "solve_mode": "snapshot"}])
        self.assertEqual(self.get(batch)["solver_config"]["max_iterations"], 17)

    def test_loadshape_data_dependency_is_frozen_before_original_changes(self):
        source = Path(self.model("daily"))
        source.write_text(source.read_text(encoding="utf-8") +
            "New LoadShape.daily npts=3 interval=1 mult=(file=profile.csv)\n", encoding="utf-8")
        profile = source.parent / "profile.csv"
        original = b"1.0\n0.8\n1.2\n"
        profile.write_bytes(original)
        _bid, items = batch_scheduler.prepare_inputs([str(source)], lambda ref: "daily", str(self.root / "cases"))
        copied_data = list(Path(items[0]["case_ref"]).parent.rglob("*.csv"))
        self.assertEqual(len(copied_data), 1)
        profile.write_bytes(b"9\n9\n9\n")
        self.assertEqual(copied_data[0].read_bytes(), original)

    def test_missing_data_dependency_rejects_submit_gate_without_partial_queue(self):
        source = Path(self.model("missing_daily"))
        source.write_text(source.read_text(encoding="utf-8") +
            "New LoadShape.daily npts=3 interval=1 mult=(file=missing-profile.csv)\n", encoding="utf-8")
        with self.assertRaises(Exception):
            batch_scheduler.prepare_inputs([self.model("good_daily"), str(source)], lambda ref: "case", str(self.root / "cases"))
        self.assertEqual(batch_store.list_for_session(self.session.id), [])
        snapshots = Path(chat_store.DB_DIR) / "batches"
        self.assertEqual(list(snapshots.iterdir()) if snapshots.exists() else [], [])

    def test_session_deleted_after_request_read_rejects_batch_without_orphan_message(self):
        user = chat_store.add_message(self.session.id, "user", "计算两个算例")
        refs = [self.model("A"), self.model("B")]
        worker = batch_scheduler.BatchScheduler(lambda *args: self.fail("deleted session must never run"))
        self.runners.append(worker)
        batch_scheduler._scheduler = worker
        chat_store.delete_session(self.session.id)
        with self.assertRaises(HTTPException) as rejected:
            chat_api._run_case_batch(session_id=self.session.id, content=user.content, user_msg=user,
                case_refs=refs, title_text="已删除", session=self.session,
                result_scopes=[ResultScope.BUS], output_formats=[OutputFormat.HTML])
        self.assertEqual(rejected.exception.status_code, 404)
        with chat_store.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (self.session.id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chat_batches").fetchone()[0], 0)
        self.assertEqual(list((Path(chat_store.DB_DIR) / "batches").iterdir()), [])

    def test_delete_between_snapshot_failure_and_error_message_is_transactionally_rejected(self):
        user = chat_store.add_message(self.session.id, "user", "计算失效输入")
        worker = batch_scheduler.BatchScheduler(lambda *args: self.fail("failed submission must never run"))
        self.runners.append(worker)
        batch_scheduler._scheduler = worker
        real_add_message = chat_store.add_message
        observed = []
        def delete_at_writer_boundary(*args, **kwargs):
            # Only the interleaving point is controlled: deletion and the real
            # guarded INSERT both operate on the actual SQLite database.
            chat_store.delete_session(self.session.id)
            observed.append("session deleted immediately before actual error INSERT")
            return real_add_message(*args, **kwargs)
        with patch.object(chat_store, "add_message", side_effect=delete_at_writer_boundary):
            with self.assertRaises(HTTPException) as rejected:
                chat_api._run_case_batch(session_id=self.session.id, content=user.content, user_msg=user,
                    case_refs=[str(self.root / "missing" / "Master.dss")], title_text="已删除", session=self.session,
                    result_scopes=[ResultScope.BUS], output_formats=[OutputFormat.HTML])
        self.assertEqual(rejected.exception.status_code, 404)
        self.assertEqual(len(observed), 1)
        self.assertIsNone(chat_store.get_session(self.session.id))
        with chat_store.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (self.session.id,)).fetchone()[0], 0)
        self.assertEqual(list((Path(chat_store.DB_DIR) / "batches").iterdir()), [])

    def test_real_http_delete_before_user_message_insert_returns_404_not_orphan_or_500(self):
        real_add_message = chat_store.add_message
        observed = []
        def delete_at_user_writer(*args, **kwargs):
            response = self.client.delete(f"/api/chat/sessions/{self.session.id}")
            observed.append(response.status_code)
            return real_add_message(*args, **kwargs)
        with patch.object(chat_store, "add_message", side_effect=delete_at_user_writer):
            response = self.client.post(f"/api/chat/sessions/{self.session.id}/messages", data={"content": "计算上传算例"},
                files=[("files", ("Master.dss", native("A").encode(), "text/plain"))])
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(observed, [200])
        with chat_store.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (self.session.id,)).fetchone()[0], 0)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BatchSchedulerTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
