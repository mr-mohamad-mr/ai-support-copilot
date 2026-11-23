"""Unit tests for the FTS5/BM25 retriever and the answer composer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3  # noqa: E402

from app.chunking import Chunk  # noqa: E402
from app.database import connect  # noqa: E402
from app.retrieval import (  # noqa: E402
    FALLBACK_ANSWER,
    MIN_SCORE,
    best_sentences,
    compose_answer,
    index_text,
    insert_chunks,
    search_chunks,
    stem,
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


def _db(docs=(("Returns Policy", RETURNS), ("Shipping FAQ", SHIPPING)), tenant_id=1):
    """In-memory database with one tenant and each text as a one-chunk doc."""
    db = connect(":memory:")
    db.execute("INSERT INTO tenants (id, name, slug) VALUES (1, 'Acme', 'acme')")
    db.execute("INSERT INTO tenants (id, name, slug) VALUES (2, 'Beta', 'beta')")
    for title, text in docs:
        _add_doc(db, tenant_id, title, text)
    db.commit()
    return db


def _add_doc(db, tenant_id, title, text):
    cur = db.execute(
        "INSERT INTO documents (tenant_id, title, content, char_count, chunk_count)"
        " VALUES (?, ?, ?, ?, 1)",
        (tenant_id, title, text, len(text)),
    )
    chunk = Chunk(index=0, text=text, heading=title, char_start=0, char_end=len(text))
    insert_chunks(db, cur.lastrowid, tenant_id, [chunk])
    return cur.lastrowid


def _search(query, top_k=3, db=None, tenant_id=1):
    return search_chunks(db or _db(), tenant_id, query, top_k=top_k)


def test_tokenize_lowercases_and_drops_stopwords():
    assert tokenize("How do I RETURN an item?") == ["return", "item"]
    assert tokenize("30 days of delivery") == ["30", "days", "delivery"]


def test_stem_folds_common_variants():
    assert stem("refunds") == stem("refunded") == stem("refund")
    assert stem("arrive") == stem("arriving") == stem("arrived")
    assert stem("batteries") == "battery"
    assert stem("shipped") == stem("ship")
    assert stem("30") == "30" and stem("is") == "is"
    assert index_text("The Refunds arrived!") == "refund arriv"


def test_search_ranks_topical_chunk_first():
    hits = _search("how do I return an item")
    assert len(hits) == 1
    assert hits[0].document_title == "Returns Policy"
    assert hits[0].score >= MIN_SCORE
    assert "return" in hits[0].matched_terms


def test_search_shipping_query_hits_shipping_doc():
    hits = _search("how long does express delivery take")
    assert hits and hits[0].document_title == "Shipping FAQ"


def test_search_unrelated_query_returns_nothing():
    assert _search("what is the weather in Paris") == []
    assert _search("quantum computing qubits") == []


def test_search_stopwords_only_returns_nothing():
    assert _search("what is the") == []
    assert _search("") == []
    assert _search("return an item", db=_db(docs=())) == []


def test_search_top_k_limits_results():
    hits = _search("delivery days", top_k=1)
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
    hits = _search("how do I return an item")
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


def test_search_matches_word_variants():
    # "refund"/"arrive" in the question, "Refunds"/"arriving" in the text.
    hits = _search("When will my refund arrive?")
    assert hits and hits[0].document_title == "Returns Policy"
    assert "refund" in hits[0].matched_terms


def test_search_is_tenant_isolated():
    db = _db()
    _add_doc(db, 2, "Beta secrets", "Our zebra discount code is ZEBRA50 for every order.")
    db.commit()
    assert _search("zebra discount code", db=db, tenant_id=1) == []
    hits = _search("zebra discount code", db=db, tenant_id=2)
    assert [h.document_title for h in hits] == ["Beta secrets"]


def test_index_follows_document_delete():
    db = _db()
    assert _search("express delivery", db=db)
    db.execute("DELETE FROM documents WHERE title = 'Shipping FAQ'")  # cascades to chunks
    db.commit()
    assert _search("express delivery", db=db) == []
    assert db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 1
    # The FTS index stays consistent with its content table.
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('integrity-check')")


def test_existing_database_is_migrated_and_backfilled(tmp_path):
    """A database created before FTS5 (no search_text, no chunks_fts) is
    upgraded on connect: search terms are backfilled and the index built."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE tenants (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            title TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual',
            content TEXT NOT NULL, char_count INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL, text TEXT NOT NULL, heading TEXT NOT NULL DEFAULT '',
            char_start INTEGER NOT NULL, char_end INTEGER NOT NULL,
            word_count INTEGER NOT NULL);
        INSERT INTO tenants (name, slug) VALUES ('Acme', 'acme');
        """
    )
    old.execute(
        "INSERT INTO documents (tenant_id, title, content, char_count, chunk_count)"
        " VALUES (1, 'Returns Policy', ?, ?, 1)",
        (RETURNS, len(RETURNS)),
    )
    old.execute(
        "INSERT INTO chunks (document_id, tenant_id, idx, text, heading, char_start,"
        " char_end, word_count) VALUES (1, 1, 0, ?, 'Returns Policy', 0, ?, 40)",
        (RETURNS, len(RETURNS)),
    )
    old.commit()
    old.close()

    db = connect(path)
    assert db.execute("SELECT search_text FROM chunks").fetchone()[0] == index_text(RETURNS)
    hits = search_chunks(db, 1, "how do I return an item")
    assert [h.document_title for h in hits] == ["Returns Policy"]
    db.close()
    connect(path).close()  # second connect: migration is idempotent
