"""API tests for stage 4: GET /analytics/summary.

Covers headline totals, handoff rate, the daily series, top cited
documents, recent handoffs, and tenant isolation — all computed from
the stored messages and the citations saved on assistant replies.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from conftest import alice, bob, client, register, auth_headers  # noqa: F401

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo-data"


def _seed_demo(client, headers):
    for title, path in (
        ("Returns Policy", "returns-policy.md"),
        ("Shipping FAQ", "shipping-faq.md"),
    ):
        resp = client.post(
            "/documents",
            json={"title": title, "content": (DEMO_DIR / path).read_text()},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text


def _ask(client, headers, question):
    resp = client.post("/chat", json={"question": question}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _summary(client, headers, days=None):
    url = "/analytics/summary" + (f"?days={days}" if days else "")
    resp = client.get(url, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_analytics_empty_tenant(client, alice):
    body = _summary(client, alice)
    assert body["days"] == 14
    assert body["total_questions"] == 0
    assert body["total_conversations"] == 0
    assert body["answered"] == 0
    assert body["handed_off"] == 0
    assert body["handoff_rate"] == 0.0
    assert body["avg_questions_per_conversation"] == 0.0
    assert body["avg_citation_score"] is None
    assert len(body["daily"]) == 14
    assert all(d["questions"] == 0 for d in body["daily"])
    # Daily series ends today, oldest first, one row per day.
    assert body["daily"][-1]["date"] == date.today().isoformat()
    assert body["daily"][0]["date"] != body["daily"][-1]["date"]
    assert body["top_documents"] == []
    assert body["recent_handoffs"] == []


def test_analytics_counts_after_mixed_chats(client, alice):
    _seed_demo(client, alice)
    _ask(client, alice, "How can I return an item?")
    _ask(client, alice, "How long does express delivery take?")
    _ask(client, alice, "What is the weather in Paris today?")

    body = _summary(client, alice)
    assert body["total_questions"] == 3
    assert body["total_conversations"] == 3
    assert body["answered"] == 2
    assert body["handed_off"] == 1
    assert body["handoff_rate"] == 0.3333  # rounded to 4 decimals
    assert body["avg_questions_per_conversation"] == 1.0
    assert body["avg_citation_score"] is not None
    assert body["avg_citation_score"] >= 0.10
    # The daily series accounts for every question asked today.
    assert sum(d["questions"] for d in body["daily"]) == 3
    assert sum(d["answered"] for d in body["daily"]) == 2
    assert sum(d["handed_off"] for d in body["daily"]) == 1
    # The off-topic question is the one handoff, with its text.
    assert len(body["recent_handoffs"]) == 1
    assert body["recent_handoffs"][0]["question"] == "What is the weather in Paris today?"
    assert body["recent_handoffs"][0]["conversation_id"] > 0
    # Both documents were cited once each.
    cited = {d["document_title"]: d["citations"] for d in body["top_documents"]}
    assert cited == {"Returns Policy": 1, "Shipping FAQ": 1}


def test_analytics_top_documents_ranked_by_citations(client, alice):
    _seed_demo(client, alice)
    _ask(client, alice, "How can I return an item?")
    _ask(client, alice, "Can I return a damaged item?")
    _ask(client, alice, "How long does express delivery take?")

    body = _summary(client, alice)
    assert body["top_documents"][0]["document_title"] == "Returns Policy"
    assert body["top_documents"][0]["citations"] == 2
    assert body["top_documents"][1]["document_title"] == "Shipping FAQ"
    assert body["top_documents"][1]["citations"] == 1


def test_analytics_all_answered_means_zero_handoff_rate(client, alice):
    _seed_demo(client, alice)
    _ask(client, alice, "How can I return an item?")
    body = _summary(client, alice)
    assert body["answered"] == 1
    assert body["handed_off"] == 0
    assert body["handoff_rate"] == 0.0
    assert body["recent_handoffs"] == []


def test_analytics_days_window(client, alice):
    _seed_demo(client, alice)
    _ask(client, alice, "How can I return an item?")
    assert len(_summary(client, alice, days=1)["daily"]) == 1
    assert len(_summary(client, alice, days=7)["daily"]) == 7
    assert _summary(client, alice, days=30)["days"] == 30
    # The window only bounds the series; lifetime totals are untouched.
    assert _summary(client, alice, days=1)["total_questions"] == 1
    assert client.get("/analytics/summary?days=0", headers=alice).status_code == 422
    assert client.get("/analytics/summary?days=91", headers=alice).status_code == 422
    assert client.get("/analytics/summary?days=abc", headers=alice).status_code == 422


def test_analytics_isolated_between_tenants(client, alice, bob):
    _seed_demo(client, alice)
    _ask(client, alice, "How can I return an item?")
    _ask(client, alice, "What is the weather in Paris today?")

    # Bob's tenant has no activity at all — Alice's numbers never leak.
    body = _summary(client, bob)
    assert body["total_questions"] == 0
    assert body["total_conversations"] == 0
    assert body["handed_off"] == 0
    assert body["top_documents"] == []
    assert body["recent_handoffs"] == []
    assert sum(d["questions"] for d in body["daily"]) == 0


def test_analytics_requires_auth(client, alice):
    assert client.get("/analytics/summary").status_code == 401


def test_analytics_handoff_lists_the_question_not_the_fallback(client, alice):
    """The recent-handoffs entry is the customer's own question, so an
    operator can answer it — not the copilot's fallback reply."""
    _seed_demo(client, alice)
    _ask(client, alice, "Do you ship to Berlin?")
    body = _summary(client, alice)
    assert len(body["recent_handoffs"]) == 1
    assert body["recent_handoffs"][0]["question"] == "Do you ship to Berlin?"
    assert "human" not in body["recent_handoffs"][0]["question"].lower()
