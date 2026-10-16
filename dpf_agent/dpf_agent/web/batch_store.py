"""Durable chat batches. Every public lookup/mutation is scoped to its session.

Only queued work can be claimed. An execution owner and attempt number fence late
results after recovery, cancellation, retry, or shutdown. SQLite transactions also
commit the corresponding chat message, so refresh cannot expose a half result.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
import json
import uuid

from . import chat_store as chat

ACTIVE = {"queued", "running", "cancelling"}
SUCCESS = {"success", "converged"}
RETRYABLE = {"partial", "not_assessed", "non_converged", "invalid_request",
             "data_unavailable", "validation_failed", "system_failed", "interrupted"}


class BatchConflict(ValueError):
    pass


def init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_batches (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_message_id TEXT NOT NULL,
            content TEXT NOT NULL, result_scopes TEXT NOT NULL, output_formats TEXT NOT NULL,
            solver_config TEXT NOT NULL DEFAULT 'null',
            paused INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chat_batches_session ON chat_batches(session_id, created_at);
        CREATE TABLE IF NOT EXISTS chat_batch_items (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, position INTEGER NOT NULL,
            case_ref TEXT NOT NULL, original_case_ref TEXT NOT NULL, display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', cancel_requested INTEGER NOT NULL DEFAULT 0,
            task_id TEXT, message_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
            owner TEXT, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chat_batch_items_queue ON chat_batch_items(status, batch_id, position);
        CREATE TABLE IF NOT EXISTS chat_batch_attempts (
            item_id TEXT NOT NULL, attempt INTEGER NOT NULL, task_id TEXT,
            message_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT, PRIMARY KEY(item_id, attempt)
        );
    """)
    if "solver_config" not in {row[1] for row in conn.execute("PRAGMA table_info(chat_batches)")}:
        conn.execute("ALTER TABLE chat_batches ADD COLUMN solver_config TEXT NOT NULL DEFAULT 'null'")


@contextmanager
def _connection():
    with closing(chat.get_conn()) as conn, conn:
        yield conn


@contextmanager
def _transaction():
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def _message(conn, sid, content):
    mid, now = "msg-" + uuid.uuid4().hex[:12], chat._now_iso()
    conn.execute("""INSERT INTO messages
        (id,session_id,role,content,attachments,artifacts,created_at)
        VALUES (?,?,'assistant',?,'[]','{}',?)""", (mid, sid, content, now))
    return mid


def _head(item):
    return f"### {item['position']} · `{item['display_name']}`\n\n"


def create(sid, user_message_id, content, items, scopes, formats, *, batch_id=None, solver_config=None):
    bid, now = batch_id or "batch-" + uuid.uuid4().hex[:16], chat._now_iso()
    with _transaction() as conn:
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            raise KeyError(sid)
        conn.execute("""INSERT INTO chat_batches
            (id,session_id,user_message_id,content,result_scopes,output_formats,solver_config,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (bid, sid, user_message_id, content,
                json.dumps(scopes), json.dumps(formats), json.dumps(solver_config), now, now))
        for index, item in enumerate(items, 1):
            row = {**item, "position": index}
            mid = _message(conn, sid, _head(row) + "等待计算。")
            conn.execute("""INSERT INTO chat_batch_items
                (id,batch_id,position,case_ref,original_case_ref,display_name,message_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (item["id"], bid, index, item["case_ref"],
                    item["original_case_ref"], item["display_name"], mid, now, now))
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
    return get(sid, bid)


def _state(items, paused, cancelled):
    statuses = [item["status"] for item in items]
    if any(value in {"running", "cancelling"} for value in statuses):
        return "cancelling" if cancelled else "running"
    if "queued" in statuses:
        return "paused" if paused else "queued"
    if statuses and all(value in SUCCESS for value in statuses):
        return "success"
    if statuses and all(value == "cancelled" for value in statuses):
        return "cancelled"
    return "completed_with_errors"


def get(sid, bid):
    with _connection() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM chat_batches WHERE id=? AND session_id=?", (bid, sid)).fetchone()
        if not row:
            return None
        batch = dict(row)
        items = [dict(row) for row in conn.execute(
            "SELECT * FROM chat_batch_items WHERE batch_id=? ORDER BY position", (bid,))]
        for item in items:
            attempts = conn.execute("SELECT * FROM chat_batch_attempts WHERE item_id=? ORDER BY attempt",
                                    (item["id"],)).fetchall()
            item["attempts"] = [dict(row) for row in attempts]
            msg = conn.execute("SELECT * FROM messages WHERE id=? AND session_id=?", (item["message_id"], sid)).fetchone()
            item["message"] = chat._row_to_message(msg).to_dict() if msg else None
            item.pop("owner", None)
        batch["items"] = items
        batch["status"] = _state(items, batch["paused"], batch["cancel_requested"])
        batch["counts"] = {"total": len(items), "success": sum(i["status"] in SUCCESS for i in items),
                           "finished": sum(i["status"] not in ACTIVE for i in items),
                           "retryable": sum(i["status"] in RETRYABLE for i in items)}
        batch["can_retry"] = bool(batch["counts"]["retryable"] and not any(i["status"] in ACTIVE for i in items))
        batch["result_scopes"] = json.loads(batch["result_scopes"])
        batch["output_formats"] = json.loads(batch["output_formats"])
        batch["solver_config"] = json.loads(batch["solver_config"])
        return batch


def list_for_session(sid):
    with _connection() as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM chat_batches WHERE session_id=? ORDER BY created_at DESC", (sid,))]
    return [batch for bid in ids if (batch := get(sid, bid))]


def recover():
    """A new backend never resumes old execution without an explicit action."""
    with _transaction() as conn:
        rows = conn.execute("""SELECT i.*,b.session_id FROM chat_batch_items i
            JOIN chat_batches b ON b.id=i.batch_id WHERE i.status IN ('running','cancelling')""").fetchall()
        now = chat._now_iso()
        for raw in rows:
            row = dict(raw)
            conn.execute("UPDATE chat_batch_items SET status='interrupted',owner=NULL,error=?,updated_at=? WHERE id=?",
                         ("后端中断，未形成最终结论。可明确重试；成功项不会重跑。", now, row["id"]))
            conn.execute("UPDATE chat_batch_attempts SET status='interrupted',finished_at=? WHERE item_id=? AND attempt=?",
                         (now, row["id"], row["attempt"]))
            conn.execute("UPDATE messages SET task_status='interrupted',content=? WHERE id=?",
                         (_head(row) + "执行已中断，未形成最终结论。可重试此失败或中断项。", row["message_id"]))
        conn.execute("""UPDATE chat_batches SET paused=1,updated_at=? WHERE id IN
            (SELECT batch_id FROM chat_batch_items WHERE status='queued')""", (now,))


def claim(owner):
    with _transaction() as conn:
        # A single worker owns execution; the DB fence also prevents accidental
        # duplicate workers from claiming a second item while one is active.
        if conn.execute("SELECT 1 FROM chat_batch_items WHERE status IN ('running','cancelling') LIMIT 1").fetchone():
            return None
        row = conn.execute("""SELECT i.*,b.session_id,b.content,b.result_scopes,b.output_formats,b.solver_config
            FROM chat_batch_items i JOIN chat_batches b ON b.id=i.batch_id
            WHERE i.status='queued' AND b.paused=0 AND b.cancel_requested=0
            ORDER BY b.created_at,i.position LIMIT 1""").fetchone()
        if not row:
            return None
        item, now = dict(row), chat._now_iso()
        item["attempt"] += 1
        conn.execute("UPDATE chat_batch_items SET status='running',owner=?,attempt=?,updated_at=? WHERE id=?",
                     (owner, item["attempt"], now, item["id"]))
        conn.execute("INSERT INTO chat_batch_attempts(item_id,attempt,message_id,status,started_at) VALUES (?,?,?,'running',?)",
                     (item["id"], item["attempt"], item["message_id"], now))
        conn.execute("UPDATE messages SET content=? WHERE id=?", (_head(item) + "正在计算…", item["message_id"]))
        return item


def bind_task(item, owner, task_id):
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM chat_batch_items WHERE id=? AND owner=? AND attempt=? AND status IN ('running','cancelling')",
                           (item["id"], owner, item["attempt"])).fetchone()
        if not row:
            raise BatchConflict("批次执行已被中断，拒绝启动旧项目")
        conn.execute("UPDATE chat_batch_items SET task_id=?,updated_at=? WHERE id=?", (task_id, chat._now_iso(), item["id"]))
        conn.execute("UPDATE chat_batch_attempts SET task_id=? WHERE item_id=? AND attempt=?", (task_id, item["id"], item["attempt"]))
        conn.execute("UPDATE messages SET task_id=?,task_case_ref=?,task_status='running' WHERE id=?",
                     (task_id, item["case_ref"], item["message_id"]))
        return bool(row["cancel_requested"])


def complete(item, owner, result):
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM chat_batch_items WHERE id=? AND owner=? AND attempt=? AND status IN ('running','cancelling')",
                           (item["id"], owner, item["attempt"])).fetchone()
        if not row:
            return False
        task_id = result.get("task_id") or row["task_id"]
        if row["task_id"] and result.get("task_id") not in (None, row["task_id"]):
            raise BatchConflict("执行结果与所属任务不匹配")
        status, now = result["task_status"], chat._now_iso()
        if status not in SUCCESS | RETRYABLE | {"cancelled"}:
            raise BatchConflict("任务没有返回可保存的最终状态")
        conn.execute("UPDATE chat_batch_items SET status=?,task_id=?,owner=NULL,error=?,updated_at=? WHERE id=?",
                     (status, task_id, result.get("error", ""), now, item["id"]))
        conn.execute("UPDATE chat_batch_attempts SET status=?,task_id=?,finished_at=? WHERE item_id=? AND attempt=?",
                     (status, task_id, now, item["id"], item["attempt"]))
        conn.execute("""UPDATE messages SET task_id=?,task_case_ref=?,task_status=?,report_url=?,artifacts=?,content=? WHERE id=?""",
                     (task_id, item["case_ref"], status, result.get("report_url"),
                      json.dumps(result.get("artifacts") or {}, ensure_ascii=False), result["content"], item["message_id"]))
        conn.execute("UPDATE chat_batches SET updated_at=? WHERE id=?", (now, item["batch_id"]))
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, item["session_id"]))
        return True


def requeue_busy(item, owner):
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM chat_batch_items WHERE id=? AND owner=? AND attempt=?", (item["id"], owner, item["attempt"])).fetchone()
        if not row:
            return
        status = "cancelled" if row["cancel_requested"] else "queued"
        conn.execute("UPDATE chat_batch_items SET status=?,owner=NULL,updated_at=? WHERE id=?", (status, chat._now_iso(), item["id"]))
        conn.execute("DELETE FROM chat_batch_attempts WHERE item_id=? AND attempt=?", (item["id"], item["attempt"]))
        conn.execute("UPDATE messages SET content=?,task_status=? WHERE id=?",
                     (_head(item) + ("已取消，尚未开始。" if status == "cancelled" else "等待引擎空闲。"), status, item["message_id"]))


def cancel(sid, bid, item_id=None):
    task_ids = []
    with _transaction() as conn:
        if not conn.execute("SELECT 1 FROM chat_batches WHERE id=? AND session_id=?", (bid, sid)).fetchone():
            raise KeyError(bid)
        rows = conn.execute("SELECT * FROM chat_batch_items WHERE batch_id=?" + (" AND id=?" if item_id else ""),
                            (bid, item_id) if item_id else (bid,)).fetchall()
        if item_id and not rows:
            raise KeyError(item_id)
        if not item_id and any(row["status"] in ACTIVE for row in rows):
            conn.execute("UPDATE chat_batches SET cancel_requested=1,paused=0,updated_at=? WHERE id=?", (chat._now_iso(), bid))
        for raw in rows:
            row = dict(raw)
            if row["status"] not in ACTIVE:
                continue
            state = "cancelled" if row["status"] == "queued" else "cancelling"
            conn.execute("UPDATE chat_batch_items SET status=?,cancel_requested=1,updated_at=? WHERE id=?",
                         (state, chat._now_iso(), row["id"]))
            if state == "cancelled":
                conn.execute("UPDATE messages SET task_status='cancelled',content=? WHERE id=?",
                             (_head(row) + "已取消，尚未开始。", row["message_id"]))
            elif row["task_id"]:
                task_ids.append(row["task_id"])
        return task_ids


def resume(sid, bid):
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM chat_batches WHERE id=? AND session_id=?", (bid, sid)).fetchone()
        if not row:
            raise KeyError(bid)
        if row["cancel_requested"]:
            raise BatchConflict("批次已取消；可重试失败或中断项")
        conn.execute("UPDATE chat_batches SET paused=0,updated_at=? WHERE id=?", (chat._now_iso(), bid))


def retry_failed(sid, bid):
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM chat_batches WHERE id=? AND session_id=?", (bid, sid)).fetchone()
        if not row:
            raise KeyError(bid)
        items = [dict(row) for row in conn.execute("SELECT * FROM chat_batch_items WHERE batch_id=? ORDER BY position", (bid,))]
        if any(item["status"] in ACTIVE for item in items):
            raise BatchConflict("请先继续或取消未开始项，等待执行结束后再重试失败项")
        retried = [item for item in items if item["status"] in RETRYABLE]
        if not retried:
            raise BatchConflict("没有可重试的失败或中断项；成功和主动取消项不会重跑")
        for item in retried:
            mid = _message(conn, sid, _head(item) + "已重新排队；上一次执行记录仍保留。")
            conn.execute("""UPDATE chat_batch_items SET status='queued',cancel_requested=0,task_id=NULL,
                message_id=?,owner=NULL,error='',updated_at=? WHERE id=?""", (mid, chat._now_iso(), item["id"]))
        conn.execute("UPDATE chat_batches SET paused=0,cancel_requested=0,updated_at=? WHERE id=?", (chat._now_iso(), bid))
        return len(retried)


def interrupt_owner(owner):
    with _transaction() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM chat_batch_items WHERE owner=?", (owner,))]
        for row in rows:
            now = chat._now_iso()
            conn.execute("UPDATE chat_batch_items SET status='interrupted',owner=NULL,error=?,updated_at=? WHERE id=?",
                         ("后端已关闭，未形成最终结论。", now, row["id"]))
            conn.execute("UPDATE chat_batch_attempts SET status='interrupted',finished_at=? WHERE item_id=? AND attempt=?",
                         (now, row["id"], row["attempt"]))
            conn.execute("UPDATE messages SET task_status='interrupted',content=? WHERE id=?",
                         (_head(row) + "执行已中断，未形成最终结论。", row["message_id"]))
            conn.execute("UPDATE chat_batches SET paused=1,updated_at=? WHERE id=?", (now, row["batch_id"]))


def delete_session_rows(conn, sid):
    conn.execute("DELETE FROM chat_batch_attempts WHERE item_id IN (SELECT i.id FROM chat_batch_items i JOIN chat_batches b ON b.id=i.batch_id WHERE b.session_id=?)", (sid,))
    conn.execute("DELETE FROM chat_batch_items WHERE batch_id IN (SELECT id FROM chat_batches WHERE session_id=?)", (sid,))
    conn.execute("DELETE FROM chat_batches WHERE session_id=?", (sid,))
