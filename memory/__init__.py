"""
memory/ - Agent Memory Foundation (Phase 1)

Independent of agent.py. No external dependencies. Pure stdlib.

Deliberately NOT a replacement for chat-history.db:
  * chat-history.db  = UI / user-facing conversation log (web layer,
                       redacted on write, 20-row cap, S4)
  * agent-memory.db   = Agent memory: durable, project-scoped, with
                       retrieval and summarization, owned by the agent
                       layer

Design constraints honoured:
  * no RAG, no embeddings, no vector DB, no external dependency
  * retrieval is keyword-overlap scoring only
  * SQLite only, schema versioned in memory_meta
"""

from memory.store import MemoryStore, MemoryTurn
from memory.sqlite_store import SQLiteMemoryStore
from memory.schema import SCHEMA_VERSION, TABLE_DDL

__all__ = [
    "MemoryStore",
    "MemoryTurn",
    "SQLiteMemoryStore",
    "SCHEMA_VERSION",
    "TABLE_DDL",
]