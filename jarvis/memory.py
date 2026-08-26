"""Shared memory — one brain across every surface.

The point of this file: whether you spoke to Jarvis at your desk or ran a
one-shot `--text` request, it's the same assistant with the same history. Every
surface reads and writes this one SQLite file.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL    NOT NULL,
    surface  TEXT    NOT NULL,   -- 'voice' | 'cli' | 'system'
    role     TEXT    NOT NULL,   -- 'user'  | 'assistant' | 'tool'
    text     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts DESC);

-- Durable facts Jarvis has been told to remember about you.
CREATE TABLE IF NOT EXISTS facts (
    key      TEXT PRIMARY KEY,
    value    TEXT NOT NULL,
    ts       REAL NOT NULL
);

-- Long-running jobs handed off to Claude Code.
CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    surface   TEXT    NOT NULL,
    task      TEXT    NOT NULL,
    cwd       TEXT,
    status    TEXT    NOT NULL,  -- 'running' | 'done' | 'failed'
    result    TEXT
);
"""


class Memory:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the voice loop, background Claude jobs and
        # the reflector run on different threads but share one brain.
        # A lock serialises writes.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # ---------------- conversation ----------------

    def add_turn(self, surface: str, role: str, text: str) -> None:
        if not text or not text.strip():
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO turns (ts, surface, role, text) VALUES (?, ?, ?, ?)",
                (time.time(), surface, role, text.strip()),
            )
            self._conn.commit()

    def recent_turns(self, limit: int = 20, include_tools: bool = False) -> list[dict[str, Any]]:
        """Most recent turns across ALL surfaces, oldest-first for prompt use.

        Tool turns are excluded by default. They're kept in the database for the
        audit trail, but injecting raw `run_shell({'command': 'python3 -c ...'})`
        blobs into the prompt burns a third of the context window on text the
        model cannot act on. What matters for continuity is what was SAID.
        """
        query = "SELECT surface, role, text FROM turns"
        if not include_tools:
            query += " WHERE role != 'tool'"
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def search_turns(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Full-text search over the ENTIRE conversation history.

        This is what makes memory feel unbounded: the prompt carries a small
        recent window, and anything older is reachable on demand instead of
        being silently forgotten.
        """
        needle = f"%{query.strip()}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, surface, role, text FROM turns "
                "WHERE role != 'tool' AND text LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (needle, limit),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            item.pop("ts", None)
            out.append(item)
        return out

    def session_transcript(self, since_ts: float) -> list[dict[str, Any]]:
        """Everything said (not done) since a timestamp — used for fact extraction."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, text FROM turns WHERE ts >= ? AND role != 'tool' ORDER BY id",
                (since_ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- facts ----------------

    def remember(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts (key, value, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (key.strip(), value.strip(), time.time()),
            )
            self._conn.commit()

    def recall(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM facts WHERE key = ?", (key.strip(),)
            ).fetchone()
        return row["value"] if row else None

    def all_facts(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM facts ORDER BY key").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def forget(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE key = ?", (key.strip(),))
            self._conn.commit()
        return cur.rowcount > 0

    # ---------------- delegated jobs ----------------

    def job_start(self, surface: str, task: str, cwd: str | None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (ts, surface, task, cwd, status) VALUES (?, ?, ?, ?, 'running')",
                (time.time(), surface, task, cwd),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def job_finish(self, job_id: int, ok: bool, result: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = ?, result = ? WHERE id = ?",
                ("done" if ok else "failed", result[:20000], job_id),
            )
            self._conn.commit()

    def recent_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, task, cwd, status, result FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- prompt context ----------------

    def context_block(self, turns: int = 20) -> str:
        """Render memory as a text block to prepend to a fresh session."""
        parts: list[str] = []

        facts = self.all_facts()
        if facts:
            parts.append(
                "ESLAB QOLGAN MA'LUMOTLAR (foydalanuvchi haqida):\n"
                + "\n".join(f"- {k}: {v}" for k, v in facts.items())
            )

        history = self.recent_turns(turns)
        if history:
            lines = [
                f"[{t['surface']}] {'SIZ' if t['role'] == 'user' else 'MEN'}: {t['text'][:400]}"
                for t in history
            ]
            parts.append(
                "OXIRGI SUHBAT:\n"
                + "\n".join(lines)
                + "\n\nBundan oldingisini eslash kerak bo'lsa `search_history` ishlat."
            )

        return "\n\n".join(parts)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
