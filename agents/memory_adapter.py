"""
MemoryAdapter - Phase 4A thin policy layer over the Memory Foundation.

Lives entirely outside agent.py / web_agent.py and outside the runtime path.
Exactly three allowed duties:

1. recall_for_prompt()  -> relevant memory as prompt-ready text ("", if none).
2. record_interaction()  -> persist one user turn + one agent turn.
3. summary_for_prompt()  -> compact summary text via the store's summarize().

The adapter NEVER decides, NEVER builds a real prompt, NEVER executes,
and knows nothing about the Core, Safety, Approval, or Tool Registry.
It owns no database file itself: the MemoryStore is injected (a
SQLiteMemoryStore by default, swappable by the same 3-method contract).

Fail-safe by design: every store call is guarded so a memory failure can
never crash a caller. Importing this module has no side effects.

Only stdlib + the existing memory package. No new dependency.
"""

from __future__ import annotations

from typing import Optional

from memory.store import MemoryStore
from memory.sqlite_store import SQLiteMemoryStore


# Policy caps: keep memory context bounded so prompts cannot explode.
DEFAULT_RECALL_MAX_CHARS = 1000
DEFAULT_SUMMARY_MAX_CHARS = 1000
DEFAULT_RETRIEVE_LIMIT = 6


class MemoryAdapter:
    """Thin policy wrapper around an injected MemoryStore."""

    def __init__(self, store: Optional[MemoryStore] = None) -> None:
        self._store: MemoryStore = store if store is not None else SQLiteMemoryStore()

    @property
    def store(self) -> MemoryStore:
        """The underlying store (for future wiring, never touched directly)."""
        return self._store

    def recall_for_prompt(
        self,
        query,
        project: str = "",
        max_chars: int = DEFAULT_RECALL_MAX_CHARS,
    ) -> str:
        """Return relevant turns formatted for a prompt, or "" when none.

        Fail-safe: any error returns "".
        """
        try:
            query_text = "" if query is None else str(query)
            project_text = "" if project is None else str(project)
            limit = int(max_chars) if max_chars is not None else DEFAULT_RECALL_MAX_CHARS
        except Exception:
            return ""
        if limit <= 0:
            return ""
        if not query_text.strip():
            return ""
        try:
            turns = self._store.retrieve_relevant(
                query_text, project=project_text or None, limit=DEFAULT_RETRIEVE_LIMIT
            )
        except Exception:
            return ""
        lines = []
        chars = 0
        for turn in turns or []:
            try:
                role = getattr(turn, "role", "") or ""
                text = getattr(turn, "text", "") or ""
                label = "user" if str(role) == "user" else "agent"
                chunk = "[%s] %s" % (label, str(text).strip())
            except Exception:
                continue
            if not chunk.strip("[] "):
                continue
            if chars + len(chunk) > limit:
                break
            lines.append(chunk)
            chars += len(chunk)
        return "\n".join(lines)

    def record_interaction(
        self,
        user_text,
        agent_text,
        project: str = "",
    ) -> None:
        """Persist one user turn + one agent turn. Never raises."""
        try:
            project_text = "" if project is None else str(project)
            user_msg = "" if user_text is None else str(user_text).strip()
            agent_msg = "" if agent_text is None else str(agent_text).strip()
        except Exception:
            return
        if not user_msg and not agent_msg:
            return
        try:
            if user_msg:
                self._store.store_turn("user", user_msg, project=project_text)
            if agent_msg:
                self._store.store_turn("agent", agent_msg, project=project_text)
        except Exception:
            # Fail-safe: memory must never crash the caller.
            return

    def summary_for_prompt(
        self,
        project: str = "",
        max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    ) -> str:
        """Thin wrapper over store.summarize(). Returns "" on error/empty."""
        try:
            project_text = "" if project is None else str(project)
            limit = int(max_chars) if max_chars is not None else DEFAULT_SUMMARY_MAX_CHARS
        except Exception:
            return ""
        if limit <= 0:
            return ""
        try:
            summary = self._store.summarize(
                project=project_text or None, max_chars=limit
            )
        except Exception:
            return ""
        return "" if summary is None else str(summary)[:limit]


def build_memory_adapter(
    db_path=None,
    store: Optional[MemoryStore] = None,
) -> MemoryAdapter:
    """Factory: injected store wins; else a SQLite store (temp or default)."""
    if store is not None:
        return MemoryAdapter(store=store)
    if db_path is not None:
        return MemoryAdapter(store=SQLiteMemoryStore(db_path=db_path))
    return MemoryAdapter()
