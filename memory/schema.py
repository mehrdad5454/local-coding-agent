"""
memory/schema.py - DDL and version constant for agent-memory.db.

Single source of truth for the schema so migrations and stores cannot
drift. No external dependency; stdlib only.
"""

SCHEMA_VERSION = 1

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL    NOT NULL,
    project         TEXT    NOT NULL DEFAULT '',
    role            TEXT    NOT NULL,
    text            TEXT    NOT NULL,
    summary         TEXT    NOT NULL DEFAULT '',
    token_estimate  INTEGER NOT NULL DEFAULT 0,
    keywords        TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memory_turns_project
    ON memory_turns(project);

CREATE INDEX IF NOT EXISTS idx_memory_turns_created
    ON memory_turns(created_at);
"""

# Roles accepted by MemoryStore.store_turn. Kept as a frozenset so an
# unknown role is caught at the boundary instead of persisting garbage.
MEMORY_ROLES = frozenset({"user", "agent", "system", "tool"})

# Retrieval tuning. Deliberately simple: keyword overlap + recency.
DEFAULT_RETRIEVE_LIMIT = 10
DEFAULT_SUMMARY_MAX_TURNS = 50
DEFAULT_SUMMARY_MAX_CHARS = 4000

# Keyword extraction: lowercase, strip punctuation, drop noise.
MIN_KEYWORD_LEN = 2
STOP_WORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all",
    "can", "had", "her", "was", "one", "our", "out", "day",
    "get", "has", "him", "his", "how", "its", "may", "new",
    "now", "old", "see", "two", "way", "who", "boy", "did",
    "she", "use", "her", "oil", "sit", "set", "run", "eat",
    "far", "sea", "eye", "ago", "off", "too", "any", "say",
    "man", "try", "ask", "end", "why", "let", "put", "say",
    "she", "try", "way", "own", "say", "too", "old", "tell",
    "very", "when", "come", "here", "just", "like", "long",
    "make", "many", "over", "such", "take", "them", "well",
    "were", "will", "with", "have", "this", "that", "from",
    "they", "know", "want", "been", "good", "much", "some",
    "time", "very", "what", "would", "there", "their",
    "about", "which", "these", "those", "other", "where",
    "right", "left", "high", "low", "large", "small",
})