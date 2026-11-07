"""Unit tests for the stage-2 TF-IDF retriever and answer composer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.retrieval import (  # noqa: E402
    FALLBACK_ANSWER,
    MIN_SCORE,
    TfIdfIndex,
    best_sentences,
    compose_answer,
    tokenize,
)

RETURNS = (
    "# Returns Policy\n\nYou can return any item within 30 days of delivery. "
    "Items must be unused and in their original packaging, with all tags attached.\n\n"
    "## Refunds\n\nRefunds are issued to the original payment method within five "
    "working days of the returned item arriving at our warehouse."
)

SHIPPING = (
    "# Shipping FAQ\n\nStandard delivery takes 3-5 working days in the UK and "
    "5-10 working days internationally. Express delivery arrives in 1-2 working "
    "days in the UK.\n\nEvery order includes tracking. You will receive a tracking "
    "link by email as soon as your order leaves the warehouse."
)


def _chunks():
    return [
        {
            "id": 1,
            "document_id": 1,
            "document_title": "Returns Policy",
            "idx": 0,
            "heading": "Returns Policy",
            "text": RETURNS,
            "char_start": 0,
            "char_end": len(RETURNS),
        },
        {
            "id": 2,
            "document_id": 2,
            "document_title": "Shipping FAQ",
            "idx": 0,
            "heading": "Shipping FAQ",
            "text": SHIPPING,
            "char_start": 0,
            "char_end": len(SHIPPING),
        },
    ]


def test_tokenize_lowercases_and_drops_stopwords():
    assert tokenize("How do I RETURN an item?") == ["return", "item"]
    assert tokenize("30 days of delivery") == ["30", "days", "delivery"]


def test_search_ranks_topical_chunk_first():
    hits = TfIdfIndex(_chunks()).search("how do I return an item")
    assert len(hits) == 1
    assert hits[0].document_title == "Returns Policy"
    assert hits[0].score >= MIN_SCORE
    assert "return" in hits[0].matched_terms


def test_search_shipping_query_hits_shipping_doc():
    hits = TfIdfIndex(_chunks()).search("how long does express delivery take")
    assert hits and hits[0].document_title == "Shipping FAQ"


def test_search_unrelated_query_returns_nothing():
    assert TfIdfIndex(_chunks()).search("what is the weather in Paris") == []
    assert TfIdfIndex(_chunks()).search("quantum computing qubits") == []


def test_search_stopwords_only_returns_nothing():
    assert TfIdfIndex(_chunks()).search("what is the") == []
    assert TfIdfIndex(_chunks()).search("") == []
    assert TfIdfIndex([]).search("return an item") == []


def test_search_top_k_limits_results():
    hits = TfIdfIndex(_chunks()).search("delivery days", top_k=1)
    assert len(hits) == 1


def test_best_sentences_picks_sentence_with_query_terms():
    picked = best_sentences(RETURNS, ["refund", "payment"], limit=1)
    assert len(picked) == 1
    assert "Refunds are issued" in picked[0]


def test_best_sentences_falls_back_to_first_sentence():
    picked = best_sentences(RETURNS, ["zebra"], limit=1)
    # Only the '#' marker is stripped from the quoted sentence; the heading
    # words stay (the chunker flattens newlines, so they can't be told
    # apart from body text) — see _clean_sentence.
    assert picked == ["Returns Policy You can return any item within 30 days of delivery."]


def test_best_sentences_dedupes_heading_word_repeated_in_sentence():
    # Flattened chunk text: "## Refunds" heading sits inline before a
    # sentence that itself starts with "Refunds".
    text = (
        "You can return any item within 30 days of delivery. "
        "## Refunds Refunds are issued to the original payment method."
    )
    picked = best_sentences(text, ["refunds"], limit=1)
    assert picked == ["Refunds are issued to the original payment method."]


def test_compose_answer_quotes_chunks_with_markers_and_offsets():
    hits = TfIdfIndex(_chunks()).search("how do I return an item")
    answer, citations = compose_answer("how do I return an item", hits)
    assert "30 days" in answer
    assert "[1]" in answer
    assert len(citations) == 1
    cit = citations[0]
    assert cit["document_title"] == "Returns Policy"
    # Citation integrity: the offsets must slice the source document
    # exactly to the cited chunk text (the citation_verification principle).
    assert RETURNS[cit["char_start"] : cit["char_end"]] == hits[0].text
    assert cit["excerpt"] == hits[0].text[:300]
    assert cit["score"] >= MIN_SCORE
    assert "return" in cit["matched_terms"]


def test_fallback_answer_is_honest_about_not_knowing():
    assert "couldn't find" in FALLBACK_ANSWER
    assert "human" in FALLBACK_ANSWER
