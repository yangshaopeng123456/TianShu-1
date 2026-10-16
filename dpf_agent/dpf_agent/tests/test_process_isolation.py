"""Real subprocess lifecycle tests with an explicitly FAKE native model.

No COM engine, HTTP service, GUI or user session is accessed. The Windows parent
crash probe really uses os._exit and a Job-owned blocked worker; it does not mock
process death. Artifacts use DPF_TEST_ARTIFACT_ROOT or a temporary directory.
"""
from __future__ import annotations

import ctypes
from functools import partial
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import weakref

from ..engines import opendss_process_adapter as isolation
from ..engines.opendss_adapter import AdapterError, CircuitHandle
from ..runtime import cancellation
from ..schemas.enums import ErrorCode
from ._process_fixture import fake_factory


class _ProcessWitness:
    """Keep the original Windows process handle, avoiding PID-reuse guesses."""
    def __init__(self, pid):
        self.pid, self.handle = pid, None
        if os.name == "nt":
            from ctypes import wintypes as wt
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            self.kernel.OpenProcess.restype = wt.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
            self.kernel.WaitForSingleObject.restype = wt.DWORD
            self.kernel.CloseHandle.argtypes = [wt.HANDLE]
            self.kernel.CloseHandle.restype = wt.BOOL
            self.handle = self.kernel.OpenProcess(0x100000, False, pid)
            if not self.handle and ctypes.get_last_error() != 87:
                raise ctypes.WinError(ctypes.get_last_error())

    def exited(self, timeout=3):
        if os.name == "nt":
            return self.handle is None or self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000)) == 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(.01)
        return False

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _handle_count():
    if os.name != "nt":
        return len(list(Path("/proc/self/fd").iterdir())) if Path("/proc/self/fd").is_dir() else None
    from ctypes import wintypes as wt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel.GetProcessHandleCount.restype = wt.BOOL
    count = wt.DWORD()
    if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    return count.value


class ProcessIsolationTests(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("DPF_TEST_ARTIFACT_ROOT")
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="process-isolation-", dir=base))

    def adapter(self, behavior="normal", *, task_id=None, solve_timeout=.2):
        adapter = isolation.OpenDSSProcessAdapter(solve_timeout_s=solve_timeout, com_quick_timeout_s=10,
            task_id=task_id, _adapter_factory=partial(fake_factory, behavior=behavior, directory=str(self.root)))
        self.addCleanup(adapter.shutdown)
        return adapter

    def compile(self, adapter):
        self.assertTrue(adapter.health().available)
        return adapter.compile_case(str(self.root), str(self.root / "fake_Master.dss"))

    def witness(self, adapter):
        witness = _ProcessWitness(adapter.engine_diagnostics()["pid"])
        self.addCleanup(witness.close)
        return witness

    def assert_reaped(self, adapter, witness):
        self.assertTrue(witness.exited(), "the actual original worker process is still running")
        diagnostics = adapter.engine_diagnostics()
        self.assertFalse(diagnostics["alive"])
        self.assertFalse(diagnostics["rpc_reader_alive"])
        self.assertEqual(diagnostics["cleanup_errors"], [])
        self.assertIsNone(adapter._process)
        self.assertIsNone(adapter._connection)
        self.assertIsNone(adapter._job)
        (self.root / "reaped_process.json").write_text(json.dumps({"fake_native": True, **diagnostics}, indent=2))

    def test_same_task_reuses_one_real_process_and_native_state_for_all_solves(self):
        adapter = self.adapter(solve_timeout=5)
        circuit = self.compile(adapter)
        pid = adapter.engine_diagnostics()["pid"]
        adapter.apply_solver_profile(circuit, {"max_iterations": 25, "tolerance": 1e-6})
        self.assertEqual(adapter.context_signature()["max_iterations"], 25)
        for count in range(1, 4):
            self.assertEqual(adapter.solve(circuit).iterations, count)
            raw = adapter.extract_raw_result(circuit, [])
            self.assertTrue(raw.solver["fake_native"])
            self.assertEqual(raw.solver["pid"], pid)
            self.assertEqual(raw.solver["solve_count"], count)
        self.assertEqual(adapter.engine_diagnostics()["pid"], pid)
        self.assertFalse(any(t.name == "opendss-private-rpc" for t in threading.enumerate()))

    def test_hung_call_times_out_and_actual_worker_is_gone(self):
        adapter = self.adapter("hang")
        circuit = self.compile(adapter)
        witness = self.witness(adapter)
        started = time.monotonic()
        with self.assertRaises(AdapterError) as failure:
            adapter.solve(circuit)
        self.assertEqual(failure.exception.code, ErrorCode.TIMEOUT)
        self.assertTrue((self.root / "solve_entered").exists())
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(circuit.compiled)
        self.assert_reaped(adapter, witness)
        with self.assertRaises(AdapterError):
            adapter.solve(circuit)

    def test_cancel_hung_actual_call_reaps_worker_and_new_task_recovers(self):
        task_id = "unit-cancel-" + uuid.uuid4().hex
        self.addCleanup(cancellation.clear, task_id)
        adapter = self.adapter("hang", task_id=task_id, solve_timeout=10)
        circuit = self.compile(adapter)
        witness = self.witness(adapter)
        caught = []

        def call():
            try:
                adapter.solve(circuit)
            except Exception as exc:
                caught.append(exc)

        caller = threading.Thread(target=call, name="unit-process-cancel-caller")
        caller.start()
        deadline = time.monotonic() + 3
        while not (self.root / "solve_entered").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.root / "solve_entered").exists())
        cancellation.request_cancel(task_id)
        caller.join(4)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(caught), 1)
        self.assertIsInstance(caught[0], AdapterError)
        self.assertEqual(caught[0].code, ErrorCode.CANCELLED)
        self.assert_reaped(adapter, witness)
        healthy = self.adapter(solve_timeout=5)
        new_circuit = self.compile(healthy)
        self.assertTrue(healthy.solve(new_circuit).converged)
        self.assertNotEqual(healthy.engine_diagnostics()["pid"], witness.pid)

    def test_pre_cancelled_task_never_starts_a_worker(self):
        task_id = "unit-before-start-" + uuid.uuid4().hex
        self.addCleanup(cancellation.clear, task_id)
        cancellation.request_cancel(task_id)
        adapter = self.adapter(task_id=task_id)
        with self.assertRaises(AdapterError) as failure:
            adapter.compile_case(str(self.root), "fake")
        self.assertEqual(failure.exception.code, ErrorCode.CANCELLED)
        self.assertIsNone(adapter.engine_diagnostics()["pid"])

    def test_worker_crash_invalidates_handles_and_same_adapter_never_respawns(self):
        adapter = self.adapter("crash", solve_timeout=5)
        circuit = self.compile(adapter)
        witness = self.witness(adapter)
        with self.assertRaises(AdapterError) as failure:
            adapter.solve(circuit)
        self.assertEqual(failure.exception.code, ErrorCode.ENGINE_ERROR)
        self.assertEqual(adapter.engine_diagnostics()["exitcode"], 37)
        self.assert_reaped(adapter, witness)
        self.assertFalse(adapter.health().available)
        with self.assertRaises(AdapterError):
            adapter.compile_case(str(self.root), "new-name-in-same-failed-task")
        self.assertEqual(adapter.engine_diagnostics()["pid"], witness.pid)

    def test_late_fake_result_cannot_be_produced_or_accepted_after_timeout(self):
        adapter = self.adapter("late", solve_timeout=.1)
        circuit = self.compile(adapter)
        witness = self.witness(adapter)
        with self.assertRaises(AdapterError) as failure:
            adapter.solve(circuit)
        self.assertEqual(failure.exception.code, ErrorCode.TIMEOUT)
        self.assert_reaped(adapter, witness)
        time.sleep(.4)
        self.assertFalse((self.root / "late_result_produced").exists())

    def test_foreign_or_forged_handle_does_not_touch_valid_native_context(self):
        first, second = self.adapter(solve_timeout=5), self.adapter(solve_timeout=5)
        handle = self.compile(first)
        current = self.compile(second)
        for bad in (handle, CircuitHandle(current.handle_id, "fake", "fake", True, second)):
            with self.subTest(bad=bad.handle_id), self.assertRaises(AdapterError):
                second.solve(bad)
            with self.subTest(close_bad=bad.handle_id), self.assertRaises(AdapterError):
                second.close(bad)
        self.assertTrue(second.solve(current).converged)
        self.assertTrue(first.solve(handle).converged)

    def test_recompile_invalidates_previous_handle_in_same_worker(self):
        adapter = self.adapter(solve_timeout=5)
        old = self.compile(adapter)
        new = adapter.compile_case(str(self.root), "second-fake-circuit")
        self.assertFalse(old.compiled)
        with self.assertRaises(AdapterError):
            adapter.solve(old)
        self.assertTrue(adapter.solve(new).converged)

    def test_factory_error_is_structured_failclosed_and_reaped(self):
        adapter = self.adapter("factory_error")
        with self.assertRaises(AdapterError) as failure:
            adapter.compile_case(str(self.root), "fake")
        self.assertEqual(failure.exception.code, ErrorCode.ENGINE_ERROR)
        self.assertIn("fake-native construction failure", failure.exception.message)
        self.assertIsNone(adapter._process)
        self.assertFalse(adapter.health().available)

    def test_unpickleable_start_failure_maps_to_adapter_error_without_handles(self):
        adapter = isolation.OpenDSSProcessAdapter(_adapter_factory=lambda settings: None)
        self.addCleanup(adapter.shutdown)
        with self.assertRaises(AdapterError) as failure:
            adapter.compile_case(str(self.root), "fake")
        self.assertEqual(failure.exception.code, ErrorCode.ENGINE_ERROR)
        self.assertIn("进程启动失败", failure.exception.message)
        self.assertIsNone(adapter._process)
        self.assertIsNone(adapter._connection)

    def test_job_ownership_assignment_failure_kills_uninitialized_child(self):
        adapter = self.adapter()
        with patch.object(isolation, "_WindowsJob", side_effect=OSError("explicit ownership failure")):
            with self.assertRaises(AdapterError) as failure:
                adapter.compile_case(str(self.root), "fake")
        self.assertEqual(failure.exception.code, ErrorCode.ENGINE_ERROR)
        self.assertIsNone(adapter._process)
        self.assertIsNone(adapter._connection)
        self.assertEqual(adapter.engine_diagnostics()["state"], "failed")
        witness = _ProcessWitness(adapter._last_pid)
        self.addCleanup(witness.close)
        self.assertTrue(witness.exited())

    def test_cached_health_cannot_hide_external_worker_death(self):
        adapter = self.adapter()
        self.compile(adapter)
        process = adapter._process
        witness = self.witness(adapter)
        process.kill()
        process.join(3)
        self.assertFalse(adapter.health().available)
        self.assert_reaped(adapter, witness)

    def test_normal_shutdown_is_idempotent_and_closes_process_and_pipe_handles(self):
        adapter = self.adapter()
        circuit = self.compile(adapter)
        witness = self.witness(adapter)
        adapter.close(circuit)
        adapter.shutdown()
        self.assertFalse(circuit.compiled)
        self.assertEqual(adapter.engine_diagnostics()["state"], "closed")
        self.assert_reaped(adapter, witness)

    def test_dropping_last_adapter_reference_reaps_owned_process(self):
        adapter = isolation.OpenDSSProcessAdapter(com_quick_timeout_s=10, _adapter_factory=fake_factory)
        circuit = self.compile(adapter)
        witness = self.witness(adapter)
        reference = weakref.ref(adapter)
        del circuit, adapter
        gc.collect()
        if reference() is not None:
            reference().shutdown()
        self.assertIsNone(reference())
        self.assertTrue(witness.exited())

    def test_repeated_contexts_do_not_accumulate_handles_or_rpc_threads(self):
        warmup = self.adapter()
        self.compile(warmup)
        warmup.shutdown()
        before = _handle_count()
        for _ in range(8):
            adapter = self.adapter(solve_timeout=5)
            circuit = self.compile(adapter)
            adapter.solve(circuit)
            adapter.shutdown()
            self.assertIsNone(adapter._process)
        gc.collect()
        after = _handle_count()
        self.assertFalse(any(thread.name == "opendss-private-rpc" for thread in threading.enumerate()))
        if before is not None:
            self.assertLessEqual(after - before, 3)
        (self.root / "resource_counts.json").write_text(json.dumps({
            "fake_native": True, "contexts": 8, "handles_before": before, "handles_after": after,
            "rpc_reader_threads": 0,
        }, indent=2))

    @unittest.skipUnless(os.name == "nt", "Windows Job parent-exit guarantee is Windows-only")
    def test_windows_job_kills_blocked_worker_after_real_parent_os_exit(self):
        log = (self.root / "abrupt_parent.log").open("wb")
        parent = subprocess.Popen([sys.executable, "-B", "-m", "dpf_agent.tests._process_fixture", "parent-crash", str(self.root)],
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        witness = None
        try:
            info_file = self.root / "orphan_probe.json"
            deadline = time.monotonic() + 10
            while not info_file.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(info_file.is_file(), (self.root / "abrupt_parent.log").read_text(errors="replace"))
            info = json.loads(info_file.read_text())
            witness = _ProcessWitness(info["worker_pid"])
            self.assertFalse(witness.exited(.01))
            (self.root / "release_parent").write_text("actual process witness acquired")
            self.assertEqual(parent.wait(timeout=5), 23)
            self.assertTrue(witness.exited(4), "Job-owned blocked worker survived actual parent os._exit")
            (self.root / "parent_exit_result.json").write_text(json.dumps({
                **info, "actual_parent_exit_code": 23, "actual_worker_handle_signaled": True,
            }, indent=2))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if witness is not None:
                witness.close()
            log.close()


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProcessIsolationTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
