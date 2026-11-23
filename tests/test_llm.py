"""Optional grounded LLM step, tested with fake providers (no network)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from app import llm
from app.retrieval import ScoredChunk, compose_answer

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo-data"


class FakeProvider:
    def __init__(self, reply="", error=None):
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture()
def fake():
    provider = FakeProvider()
    llm.set_provider(provider)
    yield provider
    llm.set_provider(None)


def _seed(client, headers):
    for title, name in (
        ("Returns Policy", "returns-policy.md"),
        ("Shipping FAQ", "shipping-faq.md"),
        ("Warranty and Repairs", "warranty-repairs.md"),
    ):
        resp = client.post(
            "/documents",
            headers=headers,
            json={"title": title, "content": (DEMO_DIR / name).read_text()},
        )
        assert resp.status_code == 201


def _ask(client, headers, question="How can I return an item?", top_k=3):
    resp = client.post("/chat", headers=headers, json={"question": question, "top_k": top_k})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _hit(n, title="Doc"):
    return ScoredChunk(
        chunk_id=n,
        document_id=n,
        document_title=f"{title} {n}",
        index=0,
        heading="",
        text=f"Source text number {n}.",
        char_start=0,
        char_end=22,
        score=0.5,
    )


# --- no provider: exactly today's behaviour -------------------------------


def test_no_provider_configured_by_default(monkeypatch):
    monkeypatch.delenv("COPILOT_LLM_PROVIDER", raising=False)
    assert llm.get_provider() is None
    monkeypatch.setenv("COPILOT_LLM_PROVIDER", "openai")
    monkeypatch.delenv("COPILOT_LLM_API_KEY", raising=False)
    assert llm.get_provider() is None  # no key: extractive only


def test_without_provider_answer_is_extractive(client, alice):
    _seed(client, alice)
    body = _ask(client, alice)
    assert body["answer_mode"] == "extractive"
    assert "30 days" in body["answer"] and body["answer"].endswith("[1]")


# --- accepted answers ------------------------------------------------------


def test_generated_answer_used_when_citations_check_out(client, alice, fake):
    _seed(client, alice)
    fake.reply = "You can return any unused item within 30 days of delivery [1]."
    body = _ask(client, alice)
    assert body["answer_mode"] == "generated"
    assert body["answer"] == fake.reply
    assert [c["document_title"] for c in body["citations"]] == ["Returns Policy"]
    assert body["needs_human"] is False


def test_prompt_contains_only_retrieved_chunks(client, alice, fake):
    _seed(client, alice)
    fake.reply = "Within 30 days [1]."
    _ask(client, alice)
    system, user = fake.calls[0]
    assert "ONLY the numbered sources" in system and "[n]" in user
    assert "How can I return an item?" in user
    assert "You can return any item within 30 days" in user
    # Documents that were not retrieved never reach the model.
    assert "Express delivery" not in user
    assert "two-year manufacturer warranty" not in user


def test_citations_renumbered_to_cited_sources():
    hits = [_hit(1), _hit(2), _hit(3)]
    provider = FakeProvider("First fact [3]. Second fact [1, 3].")
    answer, citations = llm.grounded_answer("q", hits, provider)
    assert answer == "First fact [1]. Second fact [2][1]."
    assert [c["document_title"] for c in citations] == ["Doc 3", "Doc 1"]


# --- rejected answers fall back to the extractive one -----------------------


@pytest.mark.parametrize(
    "reply",
    [
        "You can return items within 30 days.",  # no citation
        "You can return items within 30 days [7].",  # cites a source never given
        "Within 30 days [0].",
        "Within 30 days [1].\nAlso, we offer free gift wrapping.",  # uncited paragraph
        llm.NO_ANSWER,
        "",
        "x [1] " * 1000,  # over-long
    ],
)
def test_bad_answers_fall_back_to_extractive(client, alice, fake, reply):
    _seed(client, alice)
    fake.reply = reply
    body = _ask(client, alice)
    assert body["answer_mode"] == "extractive"
    assert "30 days" in body["answer"]
    assert body["citations"][0]["document_title"] == "Returns Policy"


def test_provider_error_falls_back_to_extractive(client, alice, fake):
    _seed(client, alice)
    fake.error = TimeoutError("provider timed out")
    body = _ask(client, alice)
    assert body["answer_mode"] == "extractive"
    assert body["citations"]


def test_fallback_matches_extractive_composer():
    hits = [_hit(1), _hit(2)]
    assert llm.grounded_answer("q", hits, FakeProvider("no citations here")) is None
    # The chat endpoint then uses compose_answer, unchanged:
    answer, citations = compose_answer("source text", hits)
    assert answer.endswith("[2]") and len(citations) == 2


def test_handoff_never_calls_provider(client, alice, fake):
    _seed(client, alice)
    body = _ask(client, alice, question="What is the weather in Paris today?")
    assert body["needs_human"] is True
    assert fake.calls == []


# --- provider configuration + HTTP client (urlopen faked) -------------------


def test_provider_configuration(monkeypatch):
    monkeypatch.setenv("COPILOT_LLM_PROVIDER", "ollama")
    provider = llm.get_provider()
    assert provider.url == "http://localhost:11434/v1/chat/completions"
    assert provider.api_key == ""
    monkeypatch.setenv("COPILOT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("COPILOT_LLM_API_KEY", "sk-test")
    assert llm.get_provider().url == "https://api.openai.com/v1/chat/completions"
    monkeypatch.setenv("COPILOT_LLM_PROVIDER", "openai-compatible")
    monkeypatch.delenv("COPILOT_LLM_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        llm.get_provider()
    monkeypatch.setenv("COPILOT_LLM_PROVIDER", "mystery")
    with pytest.raises(RuntimeError):
        llm.get_provider()


def test_openai_compatible_request_format(monkeypatch):
    captured = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        reply = {"choices": [{"message": {"content": "Within 30 days [1]."}}]}
        return _Resp(json.dumps(reply).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    provider = llm.OpenAICompatibleProvider(
        "http://127.0.0.1:1234/v1/", "local-model", api_key="k", timeout=5
    )
    assert provider.complete("sys", "usr") == "Within 30 days [1]."
    assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["body"]["model"] == "local-model"
    assert captured["body"]["temperature"] == 0
    assert [m["role"] for m in captured["body"]["messages"]] == ["system", "user"]
    assert captured["timeout"] == 5
