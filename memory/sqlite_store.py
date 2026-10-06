"""
memory/sqlite_store.py - SQLite-backed implementation of MemoryStore.

Uses only stdlib: sqlite3, threading, time, os.
Independent from chat-history.db.
"""

import os
import sqlite3
import string
import threading
import time

from memory.store import MemoryStore, MemoryTurn
from memory.schema import TABLE_DDL, SCHEMA_VERSION, STOP_WORDS


class SQLiteMemoryStore(MemoryStore):
    def __init__(self, db_path=None):
        if db_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(base_dir, "agent-memory.db")
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.executescript(TABLE_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO memory_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.commit()

    def store_turn(self, role, text, project="", summary=None,
                   keywords=None, token_estimate=None):
        if role not in {"user", "agent", "system", "tool"}:
            raise ValueError(f"Invalid role: {role}")

        created_at = time.time()
        if keywords is None:
            keywords = _extract_keywords(text)
        if token_estimate is None:
            token_estimate = len(text.split())
        if summary is None:
            summary = text[:200] + ("..." if len(text) > 200 else "")

        with self._lock, sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO memory_turns "
                "(created_at, project, role, text, summary, token_estimate, keywords) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    created_at,
                    project or "",
                    role,
                    text,
                    summary,
                    token_estimate,
                    " ".join(keywords),
                ),
            )
            turn_id = cursor.lastrowid
            conn.commit()

        return MemoryTurn(
            id=turn_id,
            created_at=created_at,
            project=project,
            role=role,
            text=text,
            summary=summary,
            token_estimate=token_estimate,
            keywords=list(keywords),
        )

    def retrieve_relevant(self, query, project=None, limit=10):
        query_keywords = set(_extract_keywords(query))

        with self._lock, sqlite3.connect(self._db_path) as conn:
            sql = "SELECT id, created_at, project, role, text, summary, token_estimate, keywords FROM memory_turns WHERE 1=1"
            params = []
            if project:
                sql += " AND project = ?"
                params.append(project)

            rows = conn.execute(sql, params).fetchall()

        turns = [_row_to_turn(row) for row in rows]

        if not turns or not query_keywords:
            return turns[:limit]

        scored = []
        for turn in turns:
            overlap = len(query_keywords & set(turn.keywords))
            scored.append((overlap, turn.created_at, turn))

        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [t for _, _, t in scored[:limit]]

    def summarize(self, project=None, max_turns=50, max_chars=4000):
        turns = self.retrieve_relevant("", project=project, limit=max_turns)

        if not turns:
            return ""

        parts = []
        chars = 0
        for turn in turns:
            prefix = f"[{turn.role}] "
            chunk = prefix + turn.text + "\n"
            if chars + len(chunk) > max_chars:
                break
            parts.append(chunk)
            chars += len(chunk)

        return "\n".join(parts)

    def count(self, project=None):
        with self._lock, sqlite3.connect(self._db_path) as conn:
            if project:
                sql = "SELECT COUNT(*) FROM memory_turns WHERE project = ?"
                params = (project,)
            else:
                sql = "SELECT COUNT(*) FROM memory_turns"
                params = ()
            return conn.execute(sql, params).fetchone()[0]

    def clear(self, project=None):
        with self._lock, sqlite3.connect(self._db_path) as conn:
            if project:
                conn.execute("DELETE FROM memory_turns WHERE project = ?", (project,))
            else:
                conn.execute("DELETE FROM memory_turns")
            conn.commit()
        return self.count(project)


def _extract_keywords(text):
    if not text:
        return []
    words = text.lower().split()
    keywords = []
    for word in words:
        word = word.strip(string.punctuation)
        if len(word) >= 2 and word not in STOP_WORDS:
            keywords.append(word)
    return keywords


def _row_to_turn(row):
    (id_, created_at, project, role, text, summary, token_estimate, keywords) = row
    return MemoryTurn(
        id=id_,
        created_at=created_at,
        project=project,
        role=role,
        text=text,
        summary=summary,
        token_estimate=token_estimate,
        keywords=keywords.split() if keywords else [],
    )
