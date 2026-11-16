"""Durable single-owner numerical jobs, independent of the legacy chat DB."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


TERMINAL = frozenset({"finished", "failed", "cancelled", "interrupted"})


class RequestConflict(ValueError):
    pass


class ServiceOwned(RuntimeError):
    pass


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.sqlite3"
        self._owner = None
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    task_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    digest TEXT NOT NULL, request_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, status TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    artifact_json TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    task_id TEXT NOT NULL, seq INTEGER NOT NULL, at REAL NOT NULL,
                    kind TEXT NOT NULL, data_json TEXT NOT NULL,
                    PRIMARY KEY (task_id, seq), FOREIGN KEY(task_id) REFERENCES jobs(task_id)
                );
            """)
            if "artifact_json" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN artifact_json TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def acquire_owner(self):
        """OS releases this lock on process loss; never steal a live owner."""
        stream = open(self.root / "service.lock", "a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            stream.close()
            raise ServiceOwned("This professional state directory has a live service owner") from None
        self._owner = stream

    def release_owner(self):
        if self._owner is not None:
            stream, self._owner = self._owner, None
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()

    @staticmethod
    def _event(db, task_id, kind, data):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (task_id, seq, time.time(), kind,
                   json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def submit(self, request, freeze):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE request_id=?", (request.request_id,)).fetchone()
            if old:
                if old["digest"] != request.digest():
                    raise RequestConflict("request_id already belongs to a different request")
                return dict(old), False
            task_id = f"pf-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}"
            snapshot = freeze(task_id)
            now = time.time()
            db.execute("INSERT INTO jobs(task_id,request_id,digest,request_json,snapshot_json,status,attempt_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (task_id, request.request_id, request.digest(), request.canonical_json(),
                        json.dumps(snapshot, ensure_ascii=False, allow_nan=False), "queued", task_id + "-a1", now, now))
            self._event(db, task_id, "queued", {"status":"queued", "attempt_id":task_id + "-a1"})
            return dict(db.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone()), True

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            return dict(row)

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,task_id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running', updated_at=? WHERE task_id=?", (time.time(), row["task_id"]))
            self._event(db, row["task_id"], "running", {"status":"running", "attempt_id":row["attempt_id"]})
            return dict(db.execute("SELECT * FROM jobs WHERE task_id=?", (row["task_id"],)).fetchone())

    def event(self, task_id, kind, data):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._event(db, task_id, kind, data)

    def events(self, task_id, after=0, limit=250):
        self.get(task_id)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (task_id, after, limit)).fetchall()
            return [{"seq":r["seq"], "at":r["at"], "kind":r["kind"], "data":json.loads(r["data_json"])} for r in rows]

    def cancel(self, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            if row["status"] in TERMINAL:
                return dict(row)
            status = "cancelled" if row["status"] == "queued" else "cancelling"
            db.execute("UPDATE jobs SET cancel_requested=1,status=?,updated_at=? WHERE task_id=?", (status, time.time(), task_id))
            if not row["cancel_requested"]:
                self._event(db, task_id, "cancel_requested", {"status":status, "confirmed_stopped":status == "cancelled"})
            return dict(db.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone())

    def settle(self, task_id, status, result=None, error=None, writer=None):
        if status not in TERMINAL:
            raise ValueError("A settled job must have a terminal status")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            if row["status"] in TERMINAL:
                return
            # Cancellation wins only if committed before terminal settlement.
            if row["cancel_requested"]:
                status = "cancelled"
                if result is not None:
                    result["execution_status"] = "cancelled"
                    result["numerical_validity"] = "not_run"
                    result["publishable"] = False
            artifact_metadata = None
            if result is not None:
                result["execution_status"] = status
                if writer is not None:
                    # Fence cancellation against the same canonical report and
                    # terminal commit. No reader observes an unwritten artifact.
                    artifact_metadata = writer(result)
            db.execute("UPDATE jobs SET status=?,result_json=?,artifact_json=?,error=?,updated_at=? WHERE task_id=?", (status, json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None,
                       json.dumps(artifact_metadata, allow_nan=False) if artifact_metadata else None,
                       error, time.time(), task_id))
            self._event(db, task_id, "settled", {"status":status, "numerical_validity":result.get("numerical_validity") if result else "not_run"})

    def recover(self):
        """No automatic replay after process loss, including formerly queued jobs."""
        if self._owner is None:
            raise ServiceOwned("Recovery requires the exclusive service owner")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT task_id,status FROM jobs WHERE status NOT IN ('finished','failed','cancelled','interrupted')").fetchall()
            for row in rows:
                db.execute("UPDATE jobs SET status='interrupted',error=?,updated_at=? WHERE task_id=?", ("service_restart_requires_explicit_resubmission", time.time(), row["task_id"]))
                self._event(db, row["task_id"], "interrupted", {"previous_status":row["status"], "automatic_replay":False})
            return len(rows)
