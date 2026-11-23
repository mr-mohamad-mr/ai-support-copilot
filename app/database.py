"""SQLite persistence layer.

Deliberately small and explicit: one connection per request (via a
FastAPI dependency), WAL mode, foreign keys enforced, every query
parameterised, schema created idempotently.

Multi-tenancy: every tenant-owned table carries a NOT NULL ``tenant_id``;
the application layer filters every query by the tenant id taken from the
JWT (or the widget key) — never from client input — and
cross-tenant access is covered by tests. SQLite has no row-level security,
so the application filter IS the isolation boundary; composite indexes put
``tenant_id`` first so those filters stay fast.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Docker Compose points the data volume at /data via this
# variable; locally it defaults to copilot.db next to the app package.
DEFAULT_DB_PATH = Path(
    os.environ.get("COPILOT_DB_PATH", "")
    or (Path(__file__).resolve().parent.parent / "copilot.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner'
        CHECK (role IN ('owner', 'admin', 'agent')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    word_count INTEGER NOT NULL,
    search_text TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    needs_human INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Public widget key (one per tenant). Only a SHA-256 hash is stored; the
-- plaintext key is returned once, when it is created or rotated.
CREATE TABLE IF NOT EXISTS widget_keys (
    tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Browser origins allowed to call /chat with a tenant's widget key (CORS).
CREATE TABLE IF NOT EXISTS widget_origins (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    origin TEXT NOT NULL,
    PRIMARY KEY (tenant_id, origin)
);

CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_widget_origins_origin ON widget_origins(origin);
CREATE INDEX IF NOT EXISTS idx_documents_tenant_created
    ON documents(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_tenant_document
    ON chunks(tenant_id, document_id, idx);
-- Covering index for the per-tenant BM25 statistics (count, avg length).
CREATE INDEX IF NOT EXISTS idx_chunks_tenant_len ON chunks(tenant_id, word_count);
CREATE INDEX IF NOT EXISTS idx_conversations_tenant_created
    ON conversations(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_tenant
    ON messages(tenant_id, created_at DESC);
"""


# Full-text index over chunk text (SQLite FTS5, BM25 ranking). Each chunk
# stores ``search_text``: its normalised search terms (see
# ``retrieval.index_text``: lowercased, stopwords removed, light stemming),
# so the index and the Python scorer agree on every term. The FTS table is
# external-content (it reads ``chunks.search_text``) and triggers
# keep the index in sync on insert/update/delete — including deletes that
# cascade from a document or tenant. Tenant isolation is unchanged: every
# search joins back to ``chunks`` and filters on ``chunks.tenant_id``.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    search_text, content='chunks', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE OF search_text ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
    INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    # One connection per request, but FastAPI may run a request's sync
    # dependencies and its endpoint on different threadpool threads; the
    # connection is never shared between requests, so this is safe.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent migrations for databases created by older versions.

    - ``messages.citations`` (JSON): each assistant reply's citations, so
      analytics can rank the documents that actually answer questions.
    - ``conversations.channel``: ``app`` (signed-in user) or ``widget``
      (public widget key), so a widget visitor can only continue widget
      conversations.
    - ``chunks.search_text`` + ``chunks_fts``: the FTS5 index. On an
      existing database the search terms are backfilled for every chunk
      and the index is built in one rebuild.
    """
    from .retrieval import index_text  # local import: retrieval has no DB import

    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "citations" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN citations TEXT NOT NULL DEFAULT '[]'")
        conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "channel" not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN channel TEXT NOT NULL DEFAULT 'app'")
        conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    if "search_text" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
        rows = conn.execute("SELECT id, text FROM chunks").fetchall()
        conn.executemany(
            "UPDATE chunks SET search_text = ? WHERE id = ?",
            [(index_text(r["text"]), r["id"]) for r in rows],
        )
        conn.commit()
    has_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'"
    ).fetchone()
    if not has_fts:
        conn.executescript(FTS_SCHEMA)
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        conn.commit()
