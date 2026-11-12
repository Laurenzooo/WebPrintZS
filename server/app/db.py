"""SQLite-Zugriff. Die Tabelle `jobs` ist zugleich Warteschlange und Druck-Log."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .timeutil import now_iso

_db_path: str = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    filename     TEXT    NOT NULL,
    size_bytes   INTEGER NOT NULL,
    pages        INTEGER NOT NULL,              -- Seiten im Dokument
    print_pages  INTEGER NOT NULL,              -- gedruckte Seiten (Bereich x Kopien)
    sheets       INTEGER NOT NULL,              -- Blatt Papier (Duplex berücksichtigt)
    format       TEXT    NOT NULL,
    color        INTEGER NOT NULL CHECK (color IN (0, 1)),
    duplex       TEXT    NOT NULL CHECK (duplex IN ('none', 'long', 'short')),
    copies       INTEGER NOT NULL CHECK (copies >= 1),
    page_range   TEXT,
    preset       TEXT,
    user_name    TEXT    NOT NULL,
    session_id   TEXT    NOT NULL,
    ip           TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN
                   ('queued', 'printing', 'done', 'error', 'cancelled', 'expired')),
    error        TEXT,
    created_at   TEXT    NOT NULL,
    queued_at    TEXT    NOT NULL,
    claimed_at   TEXT,
    finished_at  TEXT,
    has_file     INTEGER NOT NULL DEFAULT 1 CHECK (has_file IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_queued ON jobs (status, queued_at);
CREATE INDEX IF NOT EXISTS idx_jobs_session       ON jobs (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_user          ON jobs (user_name, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_ip            ON jobs (ip, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_created       ON jobs (created_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init(path: str) -> None:
    global _db_path
    _db_path = path
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- Aufträge

def insert_job(job: dict) -> None:
    cols = ", ".join(job)
    marks = ", ".join("?" for _ in job)
    with connect() as c:
        c.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(job.values()))


def get_job(job_id: str) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def jobs_for_session(session_id: str, limit: int = 20) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM jobs WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()


def active_jobs() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'printing') ORDER BY queued_at, rowid"
        ).fetchall()


def queue_position(job_id: str) -> int | None:
    with connect() as c:
        row = c.execute("SELECT queued_at, rowid FROM jobs WHERE id = ? AND status='queued'", (job_id,)).fetchone()
        if not row:
            return None
        return c.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','printing') AND (queued_at < ? OR (queued_at = ? AND rowid < ?))",
            (row["queued_at"], row["queued_at"], row["rowid"]),
        ).fetchone()[0] + 1


def log_entries(limit: int, offset: int, user: str = "") -> tuple[list[sqlite3.Row], int]:
    where, args = "", []
    if user:
        where, args = "WHERE user_name LIKE ?", [f"%{user}%"]
    with connect() as c:
        total = c.execute(f"SELECT COUNT(*) FROM jobs {where}", args).fetchone()[0]
        rows = c.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
    return rows, total


def all_log_entries() -> Iterator[sqlite3.Row]:
    with connect() as c:
        yield from c.execute("SELECT * FROM jobs ORDER BY created_at")


def recent_job_count(since: str, ip: str, user: str, session_id: str) -> int:
    with connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM jobs WHERE created_at >= ? AND (ip = ? OR user_name = ? OR session_id = ?)",
            (since, ip, user, session_id),
        ).fetchone()[0]


def recent_page_count(since: str, user: str, session_id: str) -> int:
    with connect() as c:
        return c.execute(
            "SELECT COALESCE(SUM(print_pages), 0) FROM jobs WHERE created_at >= ? "
            "AND (user_name = ? OR session_id = ?) AND status IN ('queued','printing','done')",
            (since, user, session_id),
        ).fetchone()[0]


def claim_next() -> sqlite3.Row | None:
    """Ältesten wartenden Auftrag atomar auf 'printing' setzen und zurückgeben."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND has_file = 1 ORDER BY queued_at, rowid LIMIT 1"
            ).fetchone()
            job = None
            if row:
                c.execute(
                    "UPDATE jobs SET status = 'printing', claimed_at = ? WHERE id = ? AND status = 'queued'",
                    (now_iso(), row["id"]),
                )
                job = c.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            c.execute("COMMIT")
            return job
        except Exception:
            c.execute("ROLLBACK")
            raise


def transition(job_id: str, from_states: tuple[str, ...], to_state: str, error: str | None = None) -> bool:
    """Statuswechsel nur aus erlaubten Zuständen. True, wenn etwas geändert wurde."""
    marks = ",".join("?" for _ in from_states)
    finished = now_iso() if to_state in ("done", "error", "cancelled", "expired") else None
    with connect() as c:
        cur = c.execute(
            f"UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ? AND status IN ({marks})",
            (to_state, error, finished, job_id, *from_states),
        )
        return cur.rowcount == 1


def requeue(job_id: str) -> bool:
    with connect() as c:
        cur = c.execute(
            "UPDATE jobs SET status='queued', error=NULL, claimed_at=NULL, finished_at=NULL, queued_at=? "
            "WHERE id = ? AND status IN ('error','cancelled','expired','done') AND has_file = 1",
            (now_iso(), job_id),
        )
        return cur.rowcount == 1


def mark_file_deleted(job_id: str) -> None:
    with connect() as c:
        c.execute("UPDATE jobs SET has_file = 0 WHERE id = ?", (job_id,))


def stats_since(since: str) -> dict:
    with connect() as c:
        by_format = c.execute(
            "SELECT format, color, COUNT(*) AS jobs, SUM(print_pages) AS pages, SUM(sheets) AS sheets "
            "FROM jobs WHERE status = 'done' AND created_at >= ? GROUP BY format, color ORDER BY format, color",
            (since,),
        ).fetchall()
        by_user = c.execute(
            "SELECT user_name, COUNT(*) AS jobs, SUM(print_pages) AS pages, "
            "SUM(CASE WHEN color = 1 THEN print_pages ELSE 0 END) AS color_pages "
            "FROM jobs WHERE status = 'done' AND created_at >= ? GROUP BY user_name ORDER BY pages DESC LIMIT 15",
            (since,),
        ).fetchall()
        errors = c.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'error' AND created_at >= ?", (since,)
        ).fetchone()[0]
    return {"by_format": by_format, "by_user": by_user, "errors": errors}


# ---------------------------------------------------------------- Key/Value (Agent-Status)

def kv_get(key: str) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def kv_set(key: str, value: dict) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def touch_agent(**fields) -> None:
    info = kv_get("agent") or {}
    info.update(fields)
    info["last_seen"] = now_iso()
    kv_set("agent", info)
