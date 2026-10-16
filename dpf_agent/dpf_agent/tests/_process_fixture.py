"""Private fake-native model for real process lifecycle tests; never OpenDSS."""
from __future__ import annotations

from functools import partial
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import uuid

from ..engines.opendss_adapter import CircuitHandle, EngineHealth, OpenDSSRawResult, SolverExecutionResult


class FakeNativeAdapter:
    def __init__(self, settings, *, behavior="normal", directory=None):
        self.behavior = behavior
        self.directory = Path(directory) if directory else None
        self.count = 0
        self.solution = SimpleNamespace(Mode=0, ControlMode=-1, DblHour=0., MaxIterations=15, Tolerance=1e-4)

    def health(self):
        return EngineHealth(available=True, engine_version="FAKE-NATIVE-PROCESS-FIXTURE")

    def compile_case(self, workspace, master):
        self.count = 0
        return CircuitHandle(uuid.uuid4().hex, workspace, master, compiled=True)

    def apply_solver_profile(self, circuit, profile):
        self.solution.MaxIterations = int(profile["max_iterations"])
        self.solution.Tolerance = float(profile["tolerance"])

    def solve(self, circuit, **kwargs):
        if self.directory:
            (self.directory / "solve_entered").write_text(str(os.getpid()))
        if self.behavior == "hang":
            while True:
                time.sleep(.05)
        if self.behavior == "crash":
            os._exit(37)
        if self.behavior == "late":
            time.sleep(.35)
            if self.directory:
                (self.directory / "late_result_produced").write_text("fake result")
        self.count += 1
        return SolverExecutionResult(True, self.count, False)

    def extract_raw_result(self, circuit, scopes, **kwargs):
        return OpenDSSRawResult(solver={"fake_native": True, "pid": os.getpid(), "solve_count": self.count})

    def circuit_summary(self, circuit):
        return {"fake_native": True, "solve_count": self.count}, {"base": 20.}

    def run_with_engine(self, callback, **kwargs):
        return callback(SimpleNamespace(ActiveCircuit=SimpleNamespace(Solution=self.solution)))

    def collect_circuit_inventory(self):
        return {"fake_native": True, "pid": os.getpid()}

    def engine_diagnostics(self):
        return {"fake_native": True, "pid": os.getpid()}


def fake_factory(settings, *, behavior="normal", directory=None):
    if behavior == "factory_error":
        raise OSError("explicit fake-native construction failure")
    return FakeNativeAdapter(settings, behavior=behavior, directory=directory)


def parent_crash_probe(directory):
    from ..engines.opendss_process_adapter import OpenDSSProcessAdapter
    directory = Path(directory)
    adapter = OpenDSSProcessAdapter(com_quick_timeout_s=10, solve_timeout_s=30,
        _adapter_factory=partial(fake_factory, behavior="hang", directory=str(directory)))
    assert adapter.health().available
    circuit = adapter.compile_case(str(directory), str(directory / "fake_Master.dss"))
    thread = threading.Thread(target=lambda: adapter.solve(circuit), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (directory / "solve_entered").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("fake blocked call was not entered")
        time.sleep(.01)
    (directory / "orphan_probe.json").write_text(json.dumps({
        "parent_pid": os.getpid(), "worker_pid": adapter._last_pid,
        "fake_native": True, "worker_is_inside_blocked_fake_native_call": True,
    }))
    while not (directory / "release_parent").exists():
        if time.monotonic() >= deadline + 5:
            raise RuntimeError("supervisor did not acknowledge the child witness")
        time.sleep(.01)
    os._exit(23)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "parent-crash":
        parent_crash_probe(sys.argv[2])
    else:
        raise SystemExit("Private fixture, not a production/native command")
