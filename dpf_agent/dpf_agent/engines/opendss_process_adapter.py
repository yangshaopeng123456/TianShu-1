"""Task-scoped OpenDSS COM isolation with a persistent private process.

All compile/solve/refinement/verification calls of one task share the same
native context. Only structured operations cross the private Pipe; callbacks,
COM objects and adapter handles never cross the process boundary. Windows Job
Objects prevent a worker surviving an abrupt parent exit. A failed worker is
never silently replaced beneath an existing circuit handle.
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
import json
import math
import multiprocessing as mp
from multiprocessing.reduction import ForkingPickler
import os
import threading
import time
import uuid
import weakref
from typing import Any

from .opendss_adapter import AdapterError, CircuitHandle, EngineHealth
from ..schemas.enums import ErrorCode


class _WindowsJob:
    """Own only this adapter's child; closing the job kills its process tree."""
    def __init__(self, process: Any):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes as wt

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wt.DWORD), ("minimum_ws", ctypes.c_size_t),
                        ("maximum_ws", ctypes.c_size_t), ("process_limit", wt.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        kernel.CreateJobObjectW.restype = wt.HANDLE
        kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        kernel.SetInformationJobObject.restype = wt.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        kernel.AssignProcessToJobObject.restype = wt.BOOL
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle.restype = wt.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        settings = Extended()
        settings.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(settings), ctypes.sizeof(settings)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(handle)
            raise error
        if not kernel.AssignProcessToJobObject(handle, int(process.sentinel)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(handle)
            raise error
        self.kernel, self.handle = kernel, handle

    def close(self) -> None:
        if self.handle is not None:
            if not self.kernel.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def _native_factory(settings: dict):
    from .opendss_com_adapter import OpenDSSComAdapter
    return OpenDSSComAdapter(**settings)


def _worker_main(connection, settings: dict, factory) -> None:
    # The channel is private to its parent; it is never a network/untrusted
    # pickle endpoint. Native construction waits until Job ownership is set.
    adapter = None
    circuits: dict[str, CircuitHandle] = {}
    try:
        while True:
            message = connection.recv()
            request_id, operation = message["id"], message["operation"]
            if operation == "shutdown":
                connection.send({"id": request_id, "ok": True, "value": None, "pid": os.getpid()})
                return
            try:
                if adapter is None:
                    adapter = factory(settings)
                args, kwargs = message.get("args", ()), message.get("kwargs", {})
                if operation == "compile_case":
                    # One native ActiveCircuit exists per task context. A second
                    # compile cannot leave an older logical handle usable.
                    circuits.clear()
                    circuit = adapter.compile_case(*args, **kwargs)
                    circuits[circuit.handle_id] = circuit
                    value = {"handle_id": circuit.handle_id, "case_workspace": circuit.case_workspace,
                             "master_dss": circuit.master_dss, "compiled": circuit.compiled}
                elif operation in {"apply_solver_profile", "solve", "extract_raw_result", "circuit_summary"}:
                    circuit_id, *remaining = args
                    if circuit_id not in circuits:
                        raise AdapterError(ErrorCode.ENGINE_ERROR, "原生算例上下文已失效，禁止在新引擎继续旧任务")
                    value = getattr(adapter, operation)(circuits[circuit_id], *remaining, **kwargs)
                elif operation == "context_signature":
                    include_frequency = kwargs.pop("include_frequency", False)
                    def read(engine):
                        sol = engine.ActiveCircuit.Solution
                        result = {"mode": int(sol.Mode), "control_mode": int(sol.ControlMode),
                                  "hour": float(sol.DblHour), "max_iterations": int(sol.MaxIterations),
                                  "tolerance": float(sol.Tolerance)}
                        if include_frequency:
                            result["frequency_hz"] = float(sol.Frequency)
                        return result
                    value = adapter.run_with_engine(read, **kwargs)
                elif operation in {"health", "collect_circuit_inventory", "collect_device_connections", "engine_diagnostics"}:
                    value = getattr(adapter, operation)(*args, **kwargs)
                else:
                    raise ValueError("不支持的原生引擎操作")
                response = {"id": request_id, "ok": True, "value": value, "pid": os.getpid()}
            except AdapterError as exc:
                response = {"id": request_id, "ok": False, "code": exc.code.value, "message": exc.message, "pid": os.getpid()}
            except Exception as exc:
                response = {"id": request_id, "ok": False, "code": ErrorCode.ENGINE_ERROR.value,
                            "message": f"{type(exc).__name__}: {exc}", "pid": os.getpid()}
            connection.send(response)
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


_LIVE = weakref.WeakSet()


def _shutdown_live() -> None:
    for adapter in list(_LIVE):
        try:
            adapter.shutdown()
        except Exception:
            # Continue reaping the other owned workers during interpreter exit.
            pass


atexit.register(_shutdown_live)


class OpenDSSProcessAdapter:
    """One private process per task, preserving native state until shutdown."""
    supports_snapshot_refinement = True
    process_isolated = True

    def __init__(self, solve_timeout_s: float = 60.0, com_timeout_s: float = 120.0,
                 com_quick_timeout_s: float = 45.0, *, task_id: str | None = None,
                 _adapter_factory=None):
        self.solve_timeout_s = solve_timeout_s
        self.com_timeout_s = com_timeout_s
        self.com_quick_timeout_s = com_quick_timeout_s
        self.task_id = task_id
        self._factory = _adapter_factory or _native_factory
        self._settings = {"solve_timeout_s": solve_timeout_s, "com_timeout_s": com_timeout_s,
                          "com_quick_timeout_s": com_quick_timeout_s}
        self._lock = threading.RLock()
        self._connection = self._process = self._job = None
        self._state = "new"
        self._last_error = ""
        self._last_health = None
        self._last_pid = None
        self._exitcode = None
        self._requests = 0
        self._reader = None
        self._circuits: dict[str, Any] = {}
        self._cleanup_errors: list[str] = []
        self._required_frequency_hz = None
        self.last_frequency_check = None
        self._required_device_semantics = None
        self._required_device_semantics_sha256 = None
        self.last_device_check = None
        self._device_guard_attempts = 0
        _LIVE.add(self)

    def _start(self) -> None:
        # Windows spawn creates an OS child before serializing Process args.
        # Reject a broken private test factory first, so a pickling exception
        # cannot leave an unowned bootstrap child without Process.pid.
        try:
            ForkingPickler.dumps((self._factory, self._settings))
        except Exception as exc:
            reason = f"原生引擎进程启动失败：{type(exc).__name__}: {exc}"
            self._terminate(reason)
            raise AdapterError(ErrorCode.ENGINE_ERROR, reason) from exc
        context = mp.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=_worker_main, args=(child, self._settings, self._factory),
                                  name="tianshu-com-task", daemon=True)
        try:
            process.start()
            child.close()
            self._process, self._connection = process, parent
            self._last_pid = process.pid
            self._job = _WindowsJob(process)
            self._state = "ready"
        except BaseException as exc:
            child.close()
            # Retain ownership even when Job assignment or spawn fails. The
            # common termination path closes the Process sentinel/handle too.
            self._process, self._connection = process, parent
            self._last_pid = process.pid
            reason = f"原生引擎进程启动失败：{type(exc).__name__}: {exc}"
            self._terminate(reason)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise AdapterError(ErrorCode.ENGINE_ERROR, reason) from exc

    def _invalidate_circuits(self) -> None:
        for reference in self._circuits.values():
            circuit = reference()
            if circuit is not None:
                circuit.compiled = False
        self._circuits.clear()

    def _terminate(self, reason: str) -> None:
        self._last_error = reason
        self._state = "failed"
        self._invalidate_circuits()
        errors = []
        process = self._process
        if self._job is not None:
            try:
                self._job.close()
                self._job = None
            except Exception as exc:
                errors.append(f"Job: {type(exc).__name__}: {exc}")
        if process is not None:
            try:
                if process.pid is not None:
                    self._last_pid = process.pid
                    if process.is_alive():
                        process.terminate()
                    process.join(2)
                    if process.is_alive():
                        process.kill()
                        process.join(2)
                    if process.is_alive():
                        raise RuntimeError("无法回收本任务的原生引擎进程")
                    self._exitcode = process.exitcode
                process.close()
                self._process = None
            except Exception as exc:
                # Keep a failed-to-reap process owned for a later shutdown retry.
                errors.append(f"Process: {type(exc).__name__}: {exc}")
        if self._connection is not None:
            try:
                self._connection.close()
                self._connection = None
            except Exception as exc:
                errors.append(f"Pipe: {type(exc).__name__}: {exc}")
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(2)
            if reader.is_alive():
                errors.append("RPC reader did not exit after native process/pipe termination")
            else:
                self._reader = None
        self._cleanup_errors = errors
        if errors:
            raise AdapterError(ErrorCode.ENGINE_ERROR, "任务引擎清理失败：" + "; ".join(errors))

    def _rpc(self, operation: str, *args, timeout_s: float | None = None, **kwargs):
        from ..runtime.cancellation import is_cancelled
        limit = float(self.com_quick_timeout_s if timeout_s is None else timeout_s)
        if not math.isfinite(limit) or limit <= 0:
            raise AdapterError(ErrorCode.TIMEOUT, "引擎调用没有剩余时间预算")
        deadline = time.monotonic() + limit
        with self._lock:
            if self._state in {"failed", "closed"}:
                raise AdapterError(ErrorCode.ENGINE_ERROR, "本任务的引擎上下文已终止；请启动新任务")
            if self.task_id and is_cancelled(self.task_id):
                self._terminate("用户取消，未执行新的原生操作")
                raise AdapterError(ErrorCode.CANCELLED, "任务已被用户取消")
            if time.monotonic() >= deadline:
                raise AdapterError(ErrorCode.TIMEOUT, "引擎调用等待已耗尽剩余时间预算")
            if self._process is None:
                self._start()
            self._requests += 1
            request_id = uuid.uuid4().hex
            message = {"id": request_id, "operation": operation, "args": args, "kwargs": kwargs}
            event, box = threading.Event(), {}
            connection = self._connection

            def transfer():
                try:
                    connection.send(message)
                    box["response"] = connection.recv()
                except BaseException as exc:
                    box["error"] = exc
                finally:
                    event.set()

            reader = threading.Thread(target=transfer, name="opendss-private-rpc", daemon=True)
            self._reader = reader
            try:
                reader.start()
            except BaseException as exc:
                self._reader = None
                self._terminate(f"原生响应读取线程启动失败：{type(exc).__name__}")
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise AdapterError(ErrorCode.ENGINE_ERROR, self._last_error) from exc
            while True:
                if self.task_id and is_cancelled(self.task_id):
                    self._terminate("用户取消，已回收任务引擎进程")
                    raise AdapterError(ErrorCode.CANCELLED, "任务已被用户取消，原生计算进程已停止")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(f"{operation} 超过 {limit:g} 秒，已回收任务引擎进程")
                    raise AdapterError(ErrorCode.TIMEOUT, self._last_error)
                if event.wait(min(0.025, remaining)):
                    break
            reader.join(2)
            if reader.is_alive():
                self._terminate("原生响应读取线程未退出")
                raise AdapterError(ErrorCode.ENGINE_ERROR, self._last_error)
            self._reader = None
            if self.task_id and is_cancelled(self.task_id):
                self._terminate("用户取消，丢弃已返回的旧结果")
                raise AdapterError(ErrorCode.CANCELLED, "任务已被用户取消")
            # Event.wait may observe a response just after its deadline. Such a
            # value must never become an authoritative late success.
            if time.monotonic() >= deadline:
                self._terminate(f"{operation} 返回时已超过 {limit:g} 秒，丢弃过期结果")
                raise AdapterError(ErrorCode.TIMEOUT, self._last_error)
            if "error" in box:
                self._terminate(f"原生计算进程通信中断：{type(box['error']).__name__}")
                raise AdapterError(ErrorCode.ENGINE_ERROR, self._last_error)
            response = box.get("response")
            if not isinstance(response, dict) or response.get("id") != request_id or response.get("pid") != self._last_pid:
                self._terminate("原生响应身份不匹配")
                raise AdapterError(ErrorCode.ENGINE_ERROR, self._last_error)
            if not response.get("ok"):
                try:
                    code = ErrorCode(response.get("code", ErrorCode.ENGINE_ERROR.value))
                except ValueError:
                    code = ErrorCode.ENGINE_ERROR
                # A timeout inside the child also invalidates native state.
                if code in {ErrorCode.TIMEOUT, ErrorCode.ENGINE_ERROR, ErrorCode.CANCELLED}:
                    self._terminate(str(response.get("message", "引擎错误")))
                raise AdapterError(code, str(response.get("message", "引擎错误")))
            return response["value"]

    def health(self) -> EngineHealth:
        with self._lock:
            if self._process is not None and not self._process.is_alive():
                self._terminate("原生引擎进程已退出")
            if self._last_health is not None and self._state == "ready":
                return self._last_health
        try:
            self._last_health = self._rpc("health", timeout_s=self.com_quick_timeout_s)
            if not self._last_health.available:
                self._terminate(self._last_health.detail or "原生引擎启动不可用")
            return self._last_health
        except AdapterError as exc:
            if exc.code == ErrorCode.CANCELLED:
                raise
            return EngineHealth(available=False, detail=exc.message)

    def probe_version(self, timeout_s: float | None = None) -> str:
        health = self._rpc("health", timeout_s=self.com_quick_timeout_s if timeout_s is None else timeout_s)
        if not health.available:
            self._terminate(health.detail or "原生引擎启动不可用")
            raise AdapterError(ErrorCode.ENGINE_ERROR, health.detail or "原生引擎不可用")
        return health.engine_version

    def compile_case(self, case_workspace: str, master_dss: str) -> CircuitHandle:
        self._invalidate_circuits()
        value = self._rpc("compile_case", case_workspace, master_dss)
        circuit = CircuitHandle(**value, dss_ctx=self)
        self._circuits[circuit.handle_id] = weakref.ref(circuit)
        return circuit

    def _check_circuit(self, circuit: CircuitHandle) -> None:
        reference = self._circuits.get(circuit.handle_id)
        if circuit.dss_ctx is not self or not circuit.compiled or reference is None or reference() is not circuit:
            raise AdapterError(ErrorCode.ENGINE_ERROR, "算例句柄不属于当前有效任务上下文")

    def apply_solver_profile(self, circuit: CircuitHandle, solver_profile) -> None:
        self._check_circuit(circuit)
        self._rpc("apply_solver_profile", circuit.handle_id, solver_profile)

    def circuit_summary(self, circuit: CircuitHandle):
        self._check_circuit(circuit)
        return self._rpc("circuit_summary", circuit.handle_id)

    def collect_circuit_inventory(self):
        return self._rpc("collect_circuit_inventory")

    def collect_device_connections(self, timeout_s=None):
        native = self._rpc("collect_device_connections", timeout_s=self.com_quick_timeout_s if timeout_s is None else timeout_s)
        baseline = getattr(self, "_snapshot_state_baseline", None)
        if baseline is not None:
            native["snapshot_state_baseline"] = json.loads(json.dumps(baseline, allow_nan=False))
        pv_baseline=getattr(self,"_pv_controls_baseline",None)
        if pv_baseline is not None:
            native["pv_controls_baseline"]=json.loads(json.dumps(pv_baseline,allow_nan=False))
        stamp_baseline=getattr(self,"_passive_stamp_baseline",None)
        if stamp_baseline is not None:
            native["passive_stamp_baseline"]=json.loads(json.dumps(stamp_baseline,allow_nan=False))
        return native

    def context_signature(self, timeout_s: float | None = None, *, include_frequency=False):
        return self._rpc("context_signature", include_frequency=include_frequency,
                         timeout_s=self.com_quick_timeout_s if timeout_s is None else timeout_s)

    def _check_required_frequency(self, timeout_s=None):
        """Check the actual native context; never set electrical conditions."""
        expected = self._required_frequency_hz
        if expected is None:
            return None
        actual = None
        try:
            actual = self.context_signature(timeout_s=timeout_s, include_frequency=True).get("frequency_hz")
            valid = (not isinstance(actual, bool) and isinstance(actual, (int, float))
                     and math.isfinite(actual) and actual > 0)
        except Exception as exc:
            if isinstance(exc, AdapterError) and exc.code in {ErrorCode.CANCELLED, ErrorCode.TIMEOUT}:
                raise
            self.last_frequency_check = {"expected_frequency_hz": expected, "actual_frequency_hz": None,
                                         "status": "not_available", "frequency_modified": False,
                                         "source": "OpenDSS.ActiveCircuit.Solution.Frequency"}
            raise AdapterError(ErrorCode.VALIDATION_FAILED,
                               "无法核对声明的运行频率，禁止继续主潮流求解") from exc
        matched = valid and math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)
        self.last_frequency_check = {"expected_frequency_hz": expected,
                                     "actual_frequency_hz": actual if valid else None,
                                     "status": "matched" if matched else "mismatch" if valid else "not_available",
                                     "frequency_modified": False,
                                     "source": "OpenDSS.ActiveCircuit.Solution.Frequency"}
        if not matched:
            raise AdapterError(ErrorCode.VALIDATION_FAILED,
                               f"实际运行频率{actual if valid else '未取得'}Hz与声明{expected}Hz不符，禁止暗改工况")
        return dict(self.last_frequency_check)

    def require_frequency(self, frequency_hz):
        try:
            valid = (not isinstance(frequency_hz, bool) and isinstance(frequency_hz, (int, float))
                     and math.isfinite(frequency_hz) and 0 < frequency_hz <= 1000)
        except (ValueError, OverflowError):
            valid = False
        if not valid:
            raise AdapterError(ErrorCode.REQ_SCHEMA_INVALID, "声明频率必须是0至1000Hz内的有限正数")
        with self._lock:
            if self._required_frequency_hz is not None and self._required_frequency_hz != float(frequency_hz):
                raise AdapterError(ErrorCode.VALIDATION_FAILED, "同一数值上下文不能替换已绑定运行频率")
            self._required_frequency_hz = float(frequency_hz)
            return self._check_required_frequency()

    def _check_required_devices(self, timeout_s=None):
        if self._required_device_semantics is None:
            return None
        self._device_guard_attempts += 1
        from ..professional.device_verification import verify_device_connections
        native = None
        read_error = None
        try:
            native = self.collect_device_connections(timeout_s=timeout_s)
        except AdapterError as exc:
            read_error = exc
        report = verify_device_connections(self._required_device_semantics, native)
        if (report["passed"] and "snapshot_state" in self._required_device_semantics
                and getattr(self,"_snapshot_state_baseline",None) is None):
            self._snapshot_state_baseline = json.loads(json.dumps(native["snapshot_state"], allow_nan=False))
            native["snapshot_state_baseline"] = json.loads(json.dumps(self._snapshot_state_baseline, allow_nan=False))
        if (report["passed"] and self._required_device_semantics.get("pv_fixed_points_required") is True
                and getattr(self,"_pv_controls_baseline",None) is None):
            self._pv_controls_baseline=json.loads(json.dumps(native["pv_controls"],allow_nan=False))
            native["pv_controls_baseline"]=json.loads(json.dumps(self._pv_controls_baseline,allow_nan=False))
        if (report["passed"] and self._required_device_semantics.get("passive_stamp_required") is True
                and getattr(self,"_passive_stamp_baseline",None) is None):
            self._passive_stamp_baseline=json.loads(json.dumps(native["passive_stamp"],allow_nan=False))
            native["passive_stamp_baseline"]=json.loads(json.dumps(self._passive_stamp_baseline,allow_nan=False))
        self.last_device_check = {"task_id": self.task_id,
                                  "declarations_sha256": self._required_device_semantics_sha256,
                                  "native_readback": native, "verification": report,
                                  "electrical_parameters_modified": False}
        if read_error is not None:
            self.last_device_check["read_error"] = {"code":read_error.code.value, "message":read_error.message}
            if read_error.code in {ErrorCode.CANCELLED, ErrorCode.TIMEOUT}:
                raise read_error
        if not report["passed"]:
            raise AdapterError(ErrorCode.VALIDATION_FAILED,
                               "设备或绕组声明未与真实OpenDSS读回值核对一致，禁止继续主潮流求解")
        return json.loads(json.dumps(self.last_device_check, allow_nan=False))

    def require_device_semantics(self, declarations):
        try:
            encoded = json.dumps(declarations, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
            if (not isinstance(declarations, dict) or declarations.get("version") not in {"1.0", "1.1", "1.2", "1.3", "1.4"}
                    or not isinstance(declarations.get("objects"), list)
                    or len(encoded) > 20 * 1024 * 1024):
                raise ValueError("invalid device declaration budget")
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise AdapterError(ErrorCode.REQ_SCHEMA_INVALID, "设备声明必须为预算内的有限JSON对象") from exc
        digest = hashlib.sha256(encoded).hexdigest()
        with self._lock:
            if self._required_device_semantics_sha256 not in {None, digest}:
                raise AdapterError(ErrorCode.VALIDATION_FAILED, "同一数值上下文不能替换已绑定设备声明")
            self._required_device_semantics = json.loads(encoded)
            self._required_device_semantics_sha256 = digest
            return self._check_required_devices()

    def solve(self, circuit: CircuitHandle, *, iteration_limit=None, timeout_s=None):
        with self._lock:
            self._check_circuit(circuit)
            limit = min(self.solve_timeout_s, timeout_s) if timeout_s is not None else self.solve_timeout_s
            deadline = time.monotonic() + limit
            def remaining():
                value = deadline - time.monotonic()
                if value <= 0:
                    raise AdapterError(ErrorCode.TIMEOUT, "设备/频率核对已用尽本次Solve预算")
                return value
            self._check_required_frequency(timeout_s=remaining())
            self._check_required_devices(timeout_s=remaining())
            return self._rpc("solve", circuit.handle_id, iteration_limit=iteration_limit,
                             timeout_s=deadline - time.monotonic())

    def extract_raw_result(self, circuit: CircuitHandle, result_scopes, *, timeout_s=None):
        with self._lock:
            self._check_circuit(circuit)
            limit = min(self.com_timeout_s, timeout_s) if timeout_s is not None else self.com_timeout_s
            deadline = time.monotonic() + limit
            raw = self._rpc("extract_raw_result", circuit.handle_id, result_scopes, timeout_s=limit)
            if self._required_device_semantics is not None:
                raw.solver["device_connections"] = self.collect_device_connections(timeout_s=deadline - time.monotonic())
            return raw

    def engine_diagnostics(self):
        with self._lock:
            process = self._process
            if process is not None and not process.is_alive():
                self._terminate("原生引擎进程已退出")
            report = {"process_isolated": True, "pid": self._last_pid, "state": self._state,
                    "alive": bool(self._process is not None and self._process.is_alive()), "exitcode": self._exitcode,
                    "requests": self._requests, "last_error": self._last_error,
                    "cleanup_errors": list(self._cleanup_errors),
                    "rpc_reader_alive": bool(self._reader and self._reader.is_alive()),
                    "parent_exit_guard": "windows_job" if os.name == "nt" else "pipe_eof_idle_only"}
            if self._required_device_semantics is not None:
                report.update(task_id=self.task_id, device_guard_attempts=self._device_guard_attempts,
                              last_device_check=json.loads(json.dumps(self.last_device_check, allow_nan=False)))
            return report

    def close(self, circuit: CircuitHandle) -> None:
        if circuit.dss_ctx is not self:
            raise AdapterError(ErrorCode.ENGINE_ERROR, "不能关闭其他任务的引擎句柄")
        if self._state == "ready":
            self._check_circuit(circuit)
        self.shutdown()
        circuit.compiled = False

    def shutdown(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
            self._terminate(self._last_error if self._state == "failed" else "任务引擎上下文已关闭")
            self._state = "closed"
            _LIVE.discard(self)

    def __del__(self):
        try:
            self.shutdown()
        except Exception:
            pass
