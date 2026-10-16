"""SQLite 会话与消息存储。"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DB_DIR = os.environ.get("DPF_CHAT_DB_DIR", "")
if not DB_DIR:
    DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "chat_data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "chat.db")
UPLOADS_DIR = os.path.join(DB_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)


class SessionMissing(KeyError):
    """The session disappeared before a message could be committed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '新聊天',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                content TEXT NOT NULL DEFAULT '',
                attachments TEXT DEFAULT '[]',
                task_id TEXT,
                task_case_ref TEXT,
                task_status TEXT,
                report_url TEXT,
                artifacts TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
            """
        )
        # 兼容旧数据库：新增 task_case_ref 列
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN task_case_ref TEXT")
        except sqlite3.OperationalError:
            pass
        from . import batch_store
        batch_store.init_tables(conn)


@dataclass
class Message:
    id: str
    session_id: str
    role: str
    content: str
    attachments: list[dict[str, Any]]
    task_id: str | None
    task_case_ref: str | None
    task_status: str | None
    report_url: str | None
    artifacts: dict[str, str]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "attachments": self.attachments,
            "task_id": self.task_id,
            "task_case_ref": self.task_case_ref,
            "task_status": self.task_status,
            "report_url": self.report_url,
            "artifacts": self.artifacts,
            "created_at": self.created_at,
        }


@dataclass
class Session:
    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[Message] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages] if self.messages is not None else None,
        }


def create_session(title: str = "新聊天") -> Session:
    sid = f"sess-{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sid, title, now, now),
        )
    return Session(id=sid, title=title, created_at=now, updated_at=now, messages=[])


def list_sessions(limit: int = 100) -> list[Session]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [Session(**dict(r)) for r in rows]


def get_session(session_id: str, include_messages: bool = True) -> Session | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        session = Session(**dict(row))
        if include_messages:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            session.messages = [_row_to_message(r) for r in rows]
        return session


def get_message(message_id: str, session_id: str) -> Message | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=?", (message_id, session_id)).fetchone()
    return _row_to_message(row) if row else None


def update_session_time(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_now_iso(), session_id))


def update_session_title(session_id: str, title: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))


def delete_session(session_id: str) -> None:
    with get_conn() as conn:
        from . import batch_store
        batch_store.delete_session_rows(conn, session_id)
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def add_message(
    session_id: str,
    role: str,
    content: str,
    attachments: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
    task_case_ref: str | None = None,
    task_status: str | None = None,
    report_url: str | None = None,
    artifacts: dict[str, str] | None = None,
) -> Message:
    mid = f"msg-{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    attachments_json = json.dumps(attachments or [], ensure_ascii=False)
    artifacts_json = json.dumps(artifacts or {}, ensure_ascii=False)
    with get_conn() as conn:
        # Session deletion and message creation must serialize in one write
        # transaction; a check before opening the transaction still leaves orphans.
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise SessionMissing(session_id)
        conn.execute(
            """
            INSERT INTO messages
            (id, session_id, role, content, attachments, task_id, task_case_ref, task_status, report_url, artifacts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mid, session_id, role, content, attachments_json, task_id, task_case_ref, task_status, report_url, artifacts_json, now),
        )
    update_session_time(session_id)
    return Message(
        id=mid,
        session_id=session_id,
        role=role,
        content=content,
        attachments=attachments or [],
        task_id=task_id,
        task_case_ref=task_case_ref,
        task_status=task_status,
        report_url=report_url,
        artifacts=artifacts or {},
        created_at=now,
    )


def update_message_task(
    message_id: str,
    task_id: str | None = None,
    task_case_ref: str | None = None,
    task_status: str | None = None,
    report_url: str | None = None,
    artifacts: dict[str, str] | None = None,
    content: str | None = None,
) -> None:
    with get_conn() as conn:
        if task_id is not None:
            conn.execute("UPDATE messages SET task_id = ? WHERE id = ?", (task_id, message_id))
        if task_case_ref is not None:
            conn.execute("UPDATE messages SET task_case_ref = ? WHERE id = ?", (task_case_ref, message_id))
        if task_status is not None:
            conn.execute("UPDATE messages SET task_status = ? WHERE id = ?", (task_status, message_id))
        if report_url is not None:
            conn.execute("UPDATE messages SET report_url = ? WHERE id = ?", (report_url, message_id))
        if artifacts is not None:
            conn.execute(
                "UPDATE messages SET artifacts = ? WHERE id = ?",
                (json.dumps(artifacts, ensure_ascii=False), message_id),
            )
        if content is not None:
            conn.execute("UPDATE messages SET content = ? WHERE id = ?", (content, message_id))


def _row_to_message(row: sqlite3.Row) -> Message:
    d = dict(row)
    return Message(
        id=d["id"],
        session_id=d["session_id"],
        role=d["role"],
        content=d["content"],
        attachments=json.loads(d["attachments"]),
        task_id=d["task_id"],
        task_case_ref=d.get("task_case_ref"),
        task_status=d["task_status"],
        report_url=d["report_url"],
        artifacts=json.loads(d["artifacts"]),
        created_at=d["created_at"],
    )


def ensure_upload_dir(session_id: str) -> str:
    path = os.path.join(UPLOADS_DIR, session_id)
    os.makedirs(path, exist_ok=True)
    return path
