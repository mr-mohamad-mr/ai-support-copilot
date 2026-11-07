"""API tests for stage 2: POST /chat, conversation storage, isolation."""

from __future__ import annotations

from pathlib import Path

from conftest import alice, bob, client, register, auth_headers  # noqa: F401

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo-data"


def _ingest(client, headers, title, path):
    resp = client.post(
        "/documents",
        json={"title": title, "content": (DEMO_DIR / path).read_text()},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_demo(client, headers):
    _ingest(client, headers, "Returns Policy", "returns-policy.md")
    _ingest(client, headers, "Shipping FAQ", "shipping-faq.md")


def test_chat_answers_from_documents_with_citations(client, alice):
    _seed_demo(client, alice)
    resp = client.post(
        "/chat", json={"question": "How can I return an item?"}, headers=alice
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_human"] is False
    assert body["top_score"] >= 0.10
    assert "30 days" in body["answer"]
    assert "[1]" in body["answer"]
    assert body["citations"], "an answered question must cite its sources"
    cit = body["citations"][0]
    assert cit["document_title"] == "Returns Policy"
    assert cit["char_start"] < cit["char_end"]
    assert cit["score"] >= 0.10
    assert "return" in cit["matched_terms"]
    # The cited chunk's text must match the document's stored chunk, and
    # the citation offsets must slice the document exactly (via the chunk).
    doc = client.get(f"/documents/{cit['document_id']}", headers=alice).json()
    chunk = doc["chunks"][cit["chunk_index"]]
    assert chunk["char_start"] == cit["char_start"]
    assert chunk["char_end"] == cit["char_end"]
    assert chunk["text"][:300] == cit["excerpt"]


def test_chat_shipping_question_cites_shipping_doc(client, alice):
    _seed_demo(client, alice)
    resp = client.post(
        "/chat",
        json={"question": "How long does express delivery take?"},
        headers=alice,
    )
    body = resp.json()
    assert body["needs_human"] is False
    assert body["citations"][0]["document_title"] == "Shipping FAQ"
    assert "1–2 working days" in body["answer"]


def test_chat_off_topic_question_hands_off_to_human(client, alice):
    _seed_demo(client, alice)
    resp = client.post(
        "/chat", json={"question": "What is the weather in Paris today?"},
        headers=alice,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_human"] is True
    assert body["citations"] == []
    assert body["top_score"] == 0.0
    assert "human" in body["answer"].lower()


def test_chat_with_empty_knowledge_base_hands_off(client, alice):
    resp = client.post(
        "/chat", json={"question": "How can I return an item?"}, headers=alice
    )
    body = resp.json()
    assert body["needs_human"] is True
    assert body["citations"] == []


def test_chat_stores_conversation_and_continues_it(client, alice):
    _seed_demo(client, alice)
    first = client.post(
        "/chat", json={"question": "How can I return an item?"}, headers=alice
    ).json()
    conv_id = first["conversation_id"]

    detail = client.get(f"/conversations/{conv_id}", headers=alice).json()
    assert detail["title"].startswith("How can I return")
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "How can I return an item?"
    assert detail["messages"][1]["needs_human"] is False
    assert detail["message_count"] == 2

    # A follow-up in the same conversation appends; the handoff flag is
    # stored per message, so an off-topic follow-up is visible as such.
    second = client.post(
        "/chat",
        json={"question": "What is the weather?", "conversation_id": conv_id},
        headers=alice,
    ).json()
    assert second["conversation_id"] == conv_id
    assert second["needs_human"] is True
    detail = client.get(f"/conversations/{conv_id}", headers=alice).json()
    assert detail["message_count"] == 4
    assert detail["messages"][3]["needs_human"] is True

    listing = client.get("/conversations", headers=alice).json()
    assert listing["total"] == 1
    assert listing["items"][0]["message_count"] == 4


def test_chat_top_k_limits_citations(client, alice):
    _seed_demo(client, alice)
    body = client.post(
        "/chat",
        json={"question": "delivery days", "top_k": 1},
        headers=alice,
    ).json()
    assert len(body["citations"]) == 1


def test_chat_cannot_cite_another_tenants_documents(client, alice, bob):
    _seed_demo(client, alice)  # only Alice's tenant has documents
    body = client.post(
        "/chat", json={"question": "How can I return an item?"}, headers=bob
    ).json()
    assert body["needs_human"] is True
    assert body["citations"] == []


def test_conversation_isolation_between_tenants(client, alice, bob):
    _seed_demo(client, alice)
    conv_id = client.post(
        "/chat", json={"question": "How can I return an item?"}, headers=alice
    ).json()["conversation_id"]
    # Bob must not see Alice's conversation at all — 404, not a leak.
    assert client.get(f"/conversations/{conv_id}", headers=bob).status_code == 404
    assert client.delete(f"/conversations/{conv_id}", headers=bob).status_code == 404
    assert client.post(
        "/chat",
        json={"question": "follow-up?", "conversation_id": conv_id},
        headers=bob,
    ).status_code == 404
    # Alice can still read and delete it.
    assert client.get(f"/conversations/{conv_id}", headers=alice).status_code == 200
    assert client.delete(f"/conversations/{conv_id}", headers=alice).status_code == 204
    assert client.get(f"/conversations/{conv_id}", headers=alice).status_code == 404


def test_chat_validation_and_auth(client, alice):
    assert client.post("/chat", json={"question": "hi"}).status_code == 401
    assert client.post(
        "/chat", json={"question": "   "}, headers=alice
    ).status_code == 422
    assert client.post(
        "/chat", json={"question": "x" * 2001}, headers=alice
    ).status_code == 422
    assert client.post(
        "/chat", json={"question": "hi", "top_k": 0}, headers=alice
    ).status_code == 422
    assert client.post(
        "/chat", json={"question": "hi", "top_k": 11}, headers=alice
    ).status_code == 422
    assert client.post(
        "/chat",
        json={"question": "hi", "conversation_id": 9999},
        headers=alice,
    ).status_code == 404
    assert client.get("/conversations").status_code == 401
    assert client.get("/conversations/9999", headers=alice).status_code == 404
