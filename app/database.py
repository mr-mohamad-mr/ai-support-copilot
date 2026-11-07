"""SQLite persistence layer.

Deliberately small and explicit, following the proven TaskBoard pattern:
one connection per request (via a FastAPI dependency), WAL mode, foreign
keys enforced, every query parameterised, schema created idempotently.

Multi-tenancy (see `multi_tenant_safety_checker` skill): every tenant-owned
table carries a NOT NULL ``tenant_id``; the application layer filters every
query by the tenant id taken from the JWT — never from client input — and
cross-tenant access is covered by tests. SQLite has no row-level security,
so the application filter IS the isolation boundary; composite indexes put
``tenant_id`` first so those filters stay fast.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Docker Compose (stage 4) points the data volume at /data via this
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
    word_count INTEGER NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_documents_tenant_created
    ON documents(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_tenant_document
    ON chunks(tenant_id, document_id, idx);
CREATE INDEX IF NOT EXISTS idx_conversations_tenant_created
    ON conversations(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_tenant
    ON messages(tenant_id, created_at DESC);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by earlier stages.

    Stage 4 stores each assistant reply's citations on its message row
    (``messages.citations``, JSON) so analytics can rank the documents
    that actually answer questions. Existing databases gain the column
    with a safe default; no data is rewritten.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "citations" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN citations TEXT NOT NULL DEFAULT '[]'"
        )
        conn.commit()
