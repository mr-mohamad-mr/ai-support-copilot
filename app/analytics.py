"""Per-tenant usage analytics (stage 4).

All metrics are computed from data the platform already stores —
``messages`` (every question and answer, with the ``needs_human`` flag)
and the citations now saved on each assistant message — so analytics
needs no new tables and no background job.

Design notes (``dashboard_builder`` / ``metrics_dashboard``):
show an operator what they need to act on — how many questions arrive,
how many the copilot answers on its own, which documents actually get
cited, and which questions still need a human — not every metric that
could be computed. Every query is tenant-scoped by the JWT tenant id,
like the rest of the app; a tenant can never see another tenant's usage.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

DEFAULT_DAYS = 14
MAX_DAYS = 90
TOP_DOCUMENTS_LIMIT = 5
RECENT_HANDOFFS_LIMIT = 10


def _day_series(days: int) -> list[str]:
    """ISO dates for the last ``days`` days, oldest first (UTC)."""
    today = date.today()
    return [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]


def compute_summary(db: sqlite3.Connection, tenant_id: int, days: int) -> dict:
    """Aggregate the tenant's usage over the last ``days`` days.

    ``days`` bounds the *daily series* and the *recent handoffs* window;
    the headline totals cover the tenant's whole history, so a new
    tenant's lifetime numbers never silently reset when the window moves.
    """
    total_questions = db.execute(
        "SELECT COUNT(*) FROM messages WHERE tenant_id = ? AND role = 'user'",
        (tenant_id,),
    ).fetchone()[0]
    total_conversations = db.execute(
        "SELECT COUNT(*) FROM conversations WHERE tenant_id = ?", (tenant_id,)
    ).fetchone()[0]

    answered, handed_off = db.execute(
        "SELECT"
        "  SUM(CASE WHEN needs_human = 0 THEN 1 ELSE 0 END),"
        "  SUM(CASE WHEN needs_human = 1 THEN 1 ELSE 0 END)"
        " FROM messages WHERE tenant_id = ? AND role = 'assistant'",
        (tenant_id,),
    ).fetchone()
    answered = answered or 0
    handed_off = handed_off or 0
    total_replies = answered + handed_off
    handoff_rate = round(handed_off / total_replies, 4) if total_replies else 0.0
    avg_per_conversation = (
        round(total_questions / total_conversations, 2) if total_conversations else 0.0
    )

    # Daily series: one row per day, zero-filled so the chart never has
    # gaps. Questions and replies are counted in two separate queries
    # (message ids are global, so a reply is not reliably ``user id + 1``
    # when tenants interleave) and merged in Python. ``date()`` reads the
    # UTC ``created_at`` written by SQLite.
    per_day: dict[str, dict] = {}
    for row in db.execute(
        "SELECT date(created_at), COUNT(*) FROM messages"
        " WHERE tenant_id = ? AND role = 'user'"
        "   AND date(created_at) >= date('now', ?)"
        " GROUP BY date(created_at)",
        (tenant_id, f"-{days - 1} days"),
    ).fetchall():
        per_day.setdefault(row[0], {"questions": 0, "answered": 0, "handed_off": 0})
        per_day[row[0]]["questions"] = row[1]
    for row in db.execute(
        "SELECT date(created_at), needs_human, COUNT(*) FROM messages"
        " WHERE tenant_id = ? AND role = 'assistant'"
        "   AND date(created_at) >= date('now', ?)"
        " GROUP BY date(created_at), needs_human",
        (tenant_id, f"-{days - 1} days"),
    ).fetchall():
        per_day.setdefault(row[0], {"questions": 0, "answered": 0, "handed_off": 0})
        per_day[row[0]]["handed_off" if row[1] else "answered"] = row[2]
    daily = [
        {
            "date": day,
            "questions": per_day.get(day, {}).get("questions", 0),
            "answered": per_day.get(day, {}).get("answered", 0),
            "handed_off": per_day.get(day, {}).get("handed_off", 0),
        }
        for day in _day_series(days)
    ]

    # Top cited documents, from the citations stored on assistant
    # messages at chat time (stage 2 never persisted them; stage 4 does).
    # Also yields the mean top-citation score across answered questions.
    counts: dict[int, dict] = {}
    top_scores: list[float] = []
    for (citations_json,) in db.execute(
        "SELECT citations FROM messages"
        " WHERE tenant_id = ? AND role = 'assistant' AND needs_human = 0",
        (tenant_id,),
    ).fetchall():
        try:
            citations = json.loads(citations_json or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(citations, list) or not citations:
            continue
        top_scores.append(float(citations[0].get("score", 0.0)))
        for cit in citations:
            doc_id = cit.get("document_id")
            if doc_id is None:
                continue
            entry = counts.setdefault(
                doc_id,
                {"document_id": doc_id, "document_title": cit.get("document_title", ""), "citations": 0},
            )
            entry["citations"] += 1
    top_documents = sorted(
        counts.values(), key=lambda d: (-d["citations"], d["document_title"])
    )[:TOP_DOCUMENTS_LIMIT]
    avg_citation_score = (
        round(sum(top_scores) / len(top_scores), 4) if top_scores else None
    )

    # Recent questions that needed a human: the user message directly
    # before each handoff-flagged assistant reply, newest first.
    recent_handoffs = []
    for row in db.execute(
        "SELECT a.conversation_id, a.created_at,"
        "  (SELECT content FROM messages u WHERE u.conversation_id = a.conversation_id"
        "     AND u.role = 'user' AND u.id < a.id ORDER BY u.id DESC LIMIT 1) AS question"
        " FROM messages a WHERE a.tenant_id = ? AND a.role = 'assistant'"
        "   AND a.needs_human = 1 AND date(a.created_at) >= date('now', ?)"
        " ORDER BY a.id DESC LIMIT ?",
        (tenant_id, f"-{days - 1} days", RECENT_HANDOFFS_LIMIT),
    ).fetchall():
        if row["question"] is None:
            continue
        recent_handoffs.append(
            {
                "question": row["question"],
                "conversation_id": row["conversation_id"],
                "created_at": row["created_at"],
            }
        )

    return {
        "days": days,
        "total_questions": total_questions,
        "total_conversations": total_conversations,
        "answered": answered,
        "handed_off": handed_off,
        "handoff_rate": handoff_rate,
        "avg_questions_per_conversation": avg_per_conversation,
        "avg_citation_score": avg_citation_score,
        "daily": daily,
        "top_documents": top_documents,
        "recent_handoffs": recent_handoffs,
    }
