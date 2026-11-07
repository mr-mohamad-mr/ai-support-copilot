"""Pydantic schemas for the AI Support Copilot API (stage 1)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

MAX_DOCUMENT_CHARS = 1_000_000


class TenantOut(BaseModel):
    id: int
    name: str
    slug: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    tenant: TenantOut


class RegisterIn(BaseModel):
    tenant_name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)
    source: str = Field(default="manual", max_length=100)

    @field_validator("title", "content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ChunkOut(BaseModel):
    index: int
    text: str
    heading: str
    char_start: int
    char_end: int
    word_count: int


class DocumentOut(BaseModel):
    id: int
    title: str
    source: str
    char_count: int
    chunk_count: int
    created_at: str


class DocumentDetail(DocumentOut):
    chunks: list[ChunkOut]


class PagedDocuments(BaseModel):
    items: list[DocumentOut]
    total: int
    page: int
    page_size: int


MAX_QUESTION_CHARS = 2000


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    conversation_id: int | None = None
    top_k: int = Field(default=3, ge=1, le=10)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class CitationOut(BaseModel):
    document_id: int
    document_title: str
    chunk_index: int
    heading: str
    char_start: int
    char_end: int
    excerpt: str
    score: float
    matched_terms: list[str]


class ChatOut(BaseModel):
    conversation_id: int
    answer: str
    citations: list[CitationOut]
    needs_human: bool
    top_score: float


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    needs_human: bool
    created_at: str


class ConversationOut(BaseModel):
    id: int
    title: str
    message_count: int
    created_at: str


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class PagedConversations(BaseModel):
    items: list[ConversationOut]
    total: int
    page: int
    page_size: int


# --- Analytics (stage 4) -------------------------------------------------


class DailyPoint(BaseModel):
    date: str
    questions: int
    answered: int
    handed_off: int


class TopDocument(BaseModel):
    document_id: int
    document_title: str
    citations: int


class HandoffItem(BaseModel):
    question: str
    conversation_id: int
    created_at: str


class AnalyticsSummary(BaseModel):
    days: int
    total_questions: int
    total_conversations: int
    answered: int
    handed_off: int
    handoff_rate: float
    avg_questions_per_conversation: float
    avg_citation_score: float | None
    daily: list[DailyPoint]
    top_documents: list[TopDocument]
    recent_handoffs: list[HandoffItem]
