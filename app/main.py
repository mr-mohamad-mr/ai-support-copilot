"""AI Support Copilot — FastAPI backend core (stage 1).

Endpoints
---------
- ``GET /health``
- ``POST /auth/register`` — creates a tenant + its owner user
- ``POST /auth/login`` — JWT bearer <redacted>
- ``GET /auth/me`` — current user + tenant
- ``GET /tenants/me`` — current tenant
- ``POST /documents`` — ingest a text/markdown document (chunked on write)
- ``GET /documents`` — paginated list, own tenant only
- ``GET /documents/{id}`` — document with its chunks
- ``DELETE /documents/{id}`` — delete document + chunks
- ``POST /chat`` — ask a question; TF-IDF retrieval over the tenant's
  chunks, extractive answer with citations, needs_human handoff flag
- ``GET /conversations`` — paginated list of stored conversations
- ``GET /conversations/{id}`` — conversation with its messages
- ``DELETE /conversations/{id}`` — delete a conversation + its messages
- ``GET /analytics/summary`` — per-tenant usage analytics (stage 4):
  question counts, handoff rate, daily series, top cited documents,
  recent questions that needed a human
- ``GET /`` — web app shell (stage 3): auth, chat UI, admin pages
- ``GET /widget`` — embeddable chat widget demo page (stage 3)
- ``GET /static/*`` — the frontend's CSS/JS assets (stage 3)

Tenant isolation: the tenant id always comes from the JWT, never from the
request. Every document/chunk query is filtered by it, and cross-tenant
access returns 404 (never revealing that the resource exists elsewhere).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from .analytics import DEFAULT_DAYS, MAX_DAYS, compute_summary
from .chunking import chunk_text
from .database import DEFAULT_DB_PATH, connect
from .retrieval import (
    DEFAULT_TOP_K,
    FALLBACK_ANSWER,
    TfIdfIndex,
    compose_answer,
)
from .schemas import (
    AnalyticsSummary,
    ChatIn,
    ChatOut,
    ChunkOut,
    CitationOut,
    ConversationDetail,
    ConversationOut,
    DocumentCreate,
    DocumentDetail,
    DocumentOut,
    LoginIn,
    MessageOut,
    PagedConversations,
    PagedDocuments,
    RegisterIn,
    TenantOut,
    TokenOut,
    UserOut,
)
from .security import (
    AuthError,
    create_token,
    decode_token,
    hash_password,
    slugify,
    verify_password,
)

app = FastAPI(title="AI Support Copilot", version="0.1.0")
bearer_scheme = HTTPBearer(auto_error=False)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
def frontend_index() -> FileResponse:
    """The web app shell (stage 3): auth + chat + admin, vanilla JS."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/widget", include_in_schema=False)
def widget_demo() -> FileResponse:
    """Demo page for the embeddable chat widget (stage 3)."""
    return FileResponse(STATIC_DIR / "widget-demo.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_db():
    conn = connect(DEFAULT_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


DbDep = Annotated[sqlite3.Connection, Depends(get_db)]


def _tenant_out(row: sqlite3.Row) -> TenantOut:
    return TenantOut(id=row["id"], name=row["name"], slug=row["slug"])


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DbDep,
) -> dict:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer <redacted>")
    try:
        payload = decode_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    row = db.execute(
        "SELECT id, tenant_id, username, role FROM users WHERE id = ?", (payload["sub"],)
    ).fetchone()
    if row is None or row["tenant_id"] != payload.get("tenant_id"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return dict(row)


UserDep = Annotated[dict, Depends(current_user)]


def _document_or_404(db: sqlite3.Connection, doc_id: int, tenant_id: int) -> dict:
    row = db.execute(
        "SELECT * FROM documents WHERE id = ? AND tenant_id = ?", (doc_id, tenant_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return dict(row)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(data: RegisterIn, db: DbDep) -> UserOut:
    slug = slugify(data.tenant_name)
    base_slug, n = slug, 1
    while db.execute("SELECT 1 FROM tenants WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base_slug}-{n}"
    try:
        cur = db.execute(
            "INSERT INTO tenants (name, slug) VALUES (?, ?)", (data.tenant_name, slug)
        )
        tenant_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO users (tenant_id, username, password_hash, role)"
            " VALUES (?, ?, ?, 'owner')",
            (tenant_id, data.username, hash_password(data.password)),
        )
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "username already taken") from exc
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return UserOut(
        id=cur.lastrowid, username=data.username, role="owner", tenant=_tenant_out(tenant)
    )


@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn, db: DbDep) -> TokenOut:
    row = db.execute(
        "SELECT id, tenant_id, username, role, password_hash FROM users WHERE username = ?",
        (data.username,),
    ).fetchone()
    if row is None or not verify_password(data.password, row["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return TokenOut(
        access_token=create_token(row["id"], row["username"], row["tenant_id"], row["role"])
    )


@app.get("/auth/me", response_model=UserOut)
def me(user: UserDep, db: DbDep) -> UserOut:
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (user["tenant_id"],)).fetchone()
    return UserOut(
        id=user["id"], username=user["username"], role=user["role"], tenant=_tenant_out(tenant)
    )


@app.get("/tenants/me", response_model=TenantOut)
def my_tenant(user: UserDep, db: DbDep) -> TenantOut:
    tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (user["tenant_id"],)).fetchone()
    return _tenant_out(tenant)


@app.post("/documents", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED)
def create_document(data: DocumentCreate, user: UserDep, db: DbDep) -> DocumentDetail:
    chunks = chunk_text(data.content)
    cur = db.execute(
        "INSERT INTO documents (tenant_id, title, source, content, char_count, chunk_count)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user["tenant_id"], data.title, data.source, data.content, len(data.content), len(chunks)),
    )
    doc_id = cur.lastrowid
    db.executemany(
        "INSERT INTO chunks (document_id, tenant_id, idx, text, heading, char_start,"
        " char_end, word_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                doc_id,
                user["tenant_id"],
                c.index,
                c.text,
                c.heading,
                c.char_start,
                c.char_end,
                c.word_count,
            )
            for c in chunks
        ],
    )
    db.commit()
    return DocumentDetail(
        id=doc_id,
        title=data.title,
        source=data.source,
        char_count=len(data.content),
        chunk_count=len(chunks),
        created_at=db.execute(
            "SELECT created_at FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()["created_at"],
        chunks=[
            ChunkOut(
                index=c.index,
                text=c.text,
                heading=c.heading,
                char_start=c.char_start,
                char_end=c.char_end,
                word_count=c.word_count,
            )
            for c in chunks
        ],
    )


@app.get("/documents", response_model=PagedDocuments)
def list_documents(
    user: UserDep,
    db: DbDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PagedDocuments:
    total = db.execute(
        "SELECT COUNT(*) FROM documents WHERE tenant_id = ?", (user["tenant_id"],)
    ).fetchone()[0]
    rows = db.execute(
        "SELECT * FROM documents WHERE tenant_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user["tenant_id"], page_size, (page - 1) * page_size),
    ).fetchall()
    return PagedDocuments(
        items=[
            DocumentOut(
                id=r["id"],
                title=r["title"],
                source=r["source"],
                char_count=r["char_count"],
                chunk_count=r["chunk_count"],
                created_at=r["created_at"],
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@app.get("/documents/{doc_id}", response_model=DocumentDetail)
def get_document(doc_id: int, user: UserDep, db: DbDep) -> DocumentDetail:
    doc = _document_or_404(db, doc_id, user["tenant_id"])
    rows = db.execute(
        "SELECT * FROM chunks WHERE document_id = ? AND tenant_id = ? ORDER BY idx",
        (doc_id, user["tenant_id"]),
    ).fetchall()
    return DocumentDetail(
        **{k: doc[k] for k in ("id", "title", "source", "char_count", "chunk_count", "created_at")},
        chunks=[
            ChunkOut(
                index=r["idx"],
                text=r["text"],
                heading=r["heading"],
                char_start=r["char_start"],
                char_end=r["char_end"],
                word_count=r["word_count"],
            )
            for r in rows
        ],
    )


@app.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(doc_id: int, user: UserDep, db: DbDep) -> None:
    _document_or_404(db, doc_id, user["tenant_id"])
    db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Chat (stage 2): TF-IDF retrieval over the tenant's chunks, extractive
# answer with per-chunk citations, needs_human handoff flag, and
# conversation storage — all tenant-scoped by the JWT, like documents.
# ---------------------------------------------------------------------------


def _conversation_or_404(db: sqlite3.Connection, conv_id: int, tenant_id: int) -> dict:
    row = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND tenant_id = ?",
        (conv_id, tenant_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return dict(row)


def _conversation_title(question: str) -> str:
    title = " ".join(question.split())
    if len(title) <= 60:
        return title
    cut = title[:60].rsplit(" ", 1)[0]
    return (cut or title[:60]) + "…"


def _store_message(
    db: sqlite3.Connection,
    conversation_id: int,
    tenant_id: int,
    role: str,
    content: str,
    needs_human: bool = False,
    citations: list[dict] | None = None,
) -> None:
    """Store one message. Assistant replies may carry their citations
    (stage 4): a compact JSON list of ``{document_id, document_title,
    score}`` so analytics can rank the documents that answer questions.
    Handoff replies store an empty list."""
    db.execute(
        "INSERT INTO messages (conversation_id, tenant_id, role, content,"
        " needs_human, citations) VALUES (?, ?, ?, ?, ?, ?)",
        (
            conversation_id,
            tenant_id,
            role,
            content,
            1 if needs_human else 0,
            json.dumps(citations or []),
        ),
    )


@app.post("/chat", response_model=ChatOut)
def chat(data: ChatIn, user: UserDep, db: DbDep) -> ChatOut:
    tenant_id = user["tenant_id"]

    # Resolve (or create) the conversation first: the user's question is
    # stored even when retrieval finds nothing, so handoffs are never lost.
    if data.conversation_id is not None:
        conv = _conversation_or_404(db, data.conversation_id, tenant_id)
        conversation_id = conv["id"]
    else:
        cur = db.execute(
            "INSERT INTO conversations (tenant_id, user_id, title) VALUES (?, ?, ?)",
            (tenant_id, user["id"], _conversation_title(data.question)),
        )
        conversation_id = cur.lastrowid
    _store_message(db, conversation_id, tenant_id, "user", data.question)

    # Retrieval runs over this tenant's chunks only — the WHERE clause is
    # the isolation boundary (see database.py); another tenant's documents
    # can never be cited, whatever the question asks for.
    rows = db.execute(
        "SELECT c.id, c.document_id, d.title AS document_title, c.idx, c.heading,"
        " c.text, c.char_start, c.char_end"
        " FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE c.tenant_id = ? ORDER BY c.document_id, c.idx",
        (tenant_id,),
    ).fetchall()
    hits = TfIdfIndex([dict(r) for r in rows]).search(data.question, top_k=data.top_k)

    if hits:
        answer, citations = compose_answer(data.question, hits)
        needs_human = False
        top_score = hits[0].score
    else:
        answer, citations = FALLBACK_ANSWER, []
        needs_human = True
        top_score = 0.0
    _store_message(
        db,
        conversation_id,
        tenant_id,
        "assistant",
        answer,
        needs_human,
        citations=[
            {
                "document_id": c["document_id"],
                "document_title": c["document_title"],
                "score": c["score"],
            }
            for c in citations
        ],
    )
    db.commit()
    return ChatOut(
        conversation_id=conversation_id,
        answer=answer,
        citations=[CitationOut(**c) for c in citations],
        needs_human=needs_human,
        top_score=top_score,
    )


@app.get("/conversations", response_model=PagedConversations)
def list_conversations(
    user: UserDep,
    db: DbDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PagedConversations:
    total = db.execute(
        "SELECT COUNT(*) FROM conversations WHERE tenant_id = ?", (user["tenant_id"],)
    ).fetchone()[0]
    rows = db.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)"
        " AS message_count FROM conversations c WHERE c.tenant_id = ?"
        " ORDER BY c.id DESC LIMIT ? OFFSET ?",
        (user["tenant_id"], page_size, (page - 1) * page_size),
    ).fetchall()
    return PagedConversations(
        items=[
            ConversationOut(
                id=r["id"],
                title=r["title"],
                message_count=r["message_count"],
                created_at=r["created_at"],
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@app.get("/conversations/{conv_id}", response_model=ConversationDetail)
def get_conversation(conv_id: int, user: UserDep, db: DbDep) -> ConversationDetail:
    conv = _conversation_or_404(db, conv_id, user["tenant_id"])
    rows = db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND tenant_id = ? ORDER BY id",
        (conv_id, user["tenant_id"]),
    ).fetchall()
    return ConversationDetail(
        id=conv["id"],
        title=conv["title"],
        message_count=len(rows),
        created_at=conv["created_at"],
        messages=[
            MessageOut(
                id=r["id"],
                role=r["role"],
                content=r["content"],
                needs_human=bool(r["needs_human"]),
                created_at=r["created_at"],
            )
            for r in rows
        ],
    )


@app.delete("/conversations/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conv_id: int, user: UserDep, db: DbDep) -> None:
    _conversation_or_404(db, conv_id, user["tenant_id"])
    db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    db.commit()


# ---------------------------------------------------------------------------
# Analytics (stage 4): per-tenant usage summary, computed from the stored
# messages and their citations. Tenant-scoped by the JWT, like everything
# else; another tenant's numbers are never visible.
# ---------------------------------------------------------------------------


@app.get("/analytics/summary", response_model=AnalyticsSummary)
def analytics_summary(
    user: UserDep,
    db: DbDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = DEFAULT_DAYS,
) -> AnalyticsSummary:
    return AnalyticsSummary(**compute_summary(db, user["tenant_id"], days))
