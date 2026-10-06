"""
memory/store.py - the MemoryStore interface.

A memory store is anything that can:
  * persist a conversation turn durably
  * recall turns relevant to a query
  * summarize a bounded window of turns

This is an interface contract, not an implementation. SQLiteMemoryStore
is the default implementation. A future in-memory or vector-backed
implementation can swap in by implementing the same three methods.
"""

import abc


class MemoryTurn(object):
    """A single persisted turn. Plain dict-like; no framework coupling."""

    __slots__ = (
        "id", "created_at", "project", "role", "text",
        "summary", "token_estimate", "keywords",
    )

    def __init__(
        self,
        id=None,
        created_at=None,
        project="",
        role="",
        text="",
        summary="",
        token_estimate=0,
        keywords=None,
    ):
        self.id = id
        self.created_at = created_at
        self.project = project or ""
        self.role = role or ""
        self.text = text or ""
        self.summary = summary or ""
        self.token_estimate = int(token_estimate or 0)
        self.keywords = list(keywords) if keywords else []

    def to_dict(self):
        return {
            "id": self.id,
            "created_at": self.created_at,
            "project": self.project,
            "role": self.role,
            "text": self.text,
            "summary": self.summary,
            "token_estimate": self.token_estimate,
            "keywords": list(self.keywords),
        }

    def __repr__(self):
        return (
            "MemoryTurn(id=%r, role=%r, project=%r, text=%r)"
            % (self.id, self.role, self.project, self.text[:40])
        )


class MemoryStore(abc.ABC):
    """Abstract memory store. Concrete stores implement three methods."""

    @abc.abstractmethod
    def store_turn(self, role, text, project="", summary=None,
                   keywords=None, token_estimate=None):
        """Persist one turn. Returns the stored MemoryTurn."""

    @abc.abstractmethod
    def retrieve_relevant(self, query, project=None, limit=10):
        """Return turns relevant to query, newest first."""

    @abc.abstractmethod
    def summarize(self, project=None, max_turns=50, max_chars=4000):
        """Return a compact text summary of recent turns."""

    # --- convenience helpers (default implementations) ---

    def store_user(self, text, project=""):
        return self.store_turn("user", text, project=project)

    def store_agent(self, text, project=""):
        return self.store_turn("agent", text, project=project)

    def store_system(self, text, project=""):
        return self.store_turn("system", text, project=project)

    def count(self, project=None):
        turns = self.retrieve_relevant("", project=project, limit=10**6)
        return len(turns)

    def recent(self, project=None, limit=10):
        return self.retrieve_relevant("", project=project, limit=limit)

    def clear(self, project=None):
        """Default: no-op. Concrete stores override."""
        return 0