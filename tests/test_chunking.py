"""Unit tests for the recursive chunker (app/chunking.py)."""

import pytest

from app.chunking import chunk_text


def test_empty_input_returns_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_text_is_one_chunk_with_offsets():
    text = "Our refund window is 30 days."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)
    assert chunks[0].word_count == len(text.split())


def test_long_text_splits_within_size_and_keeps_offsets():
    sentences = [f"Sentence number {i} explains the returns policy in detail." for i in range(40)]
    text = " ".join(sentences)
    chunks = chunk_text(text, chunk_size=200, chunk_overlap=40)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= 200, chunk.text
        # Every chunk must be a faithful slice of the source (modulo the
        # single-space joins between sentences).
        assert chunk.text[:20] in text
        assert 0 <= chunk.char_start < chunk.char_end <= len(text)
        assert text[chunk.char_start : chunk.char_end].strip()[:20] in chunk.text
    # Indexes are sequential and chunks cover the document in order.
    assert [c.index for c in chunks] == list(range(len(chunks)))
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts)


def test_overlap_repeats_tail_content():
    text = " ".join(f"Fact {i} about shipping and delivery times." for i in range(30))
    chunks = chunk_text(text, chunk_size=150, chunk_overlap=60)
    assert len(chunks) > 1
    # The start of chunk N+1 must appear inside chunk N (the overlap).
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.text[:30] in prev.text


def test_markdown_headings_are_tracked():
    text = (
        "# Returns\n\nYou can return items within 30 days of delivery.\n\n"
        "## Refunds\n\nRefunds go back to the original payment method within five days."
    )
    chunks = chunk_text(text, chunk_size=100, chunk_overlap=15)
    assert chunks
    headings = {c.heading for c in chunks}
    assert "Returns" in headings
    assert "Refunds" in headings
    # A chunk under "Refunds" must not be labelled "Returns".
    refund_chunks = [c for c in chunks if "Refunds go back" in c.text]
    assert refund_chunks and all(c.heading == "Refunds" for c in refund_chunks)


def test_overlong_sentence_is_hard_split_without_hanging():
    text = "word " * 500  # one 2500-char "sentence" with no punctuation
    chunks = chunk_text(text.strip(), chunk_size=200, chunk_overlap=40)
    assert len(chunks) > 1
    assert all(len(c.text) <= 200 for c in chunks)
    assert all(0 <= c.char_start < c.char_end <= len(text) for c in chunks)


def test_invalid_parameters_rejected():
    with pytest.raises(ValueError):
        chunk_text("hello", chunk_size=50)
    with pytest.raises(ValueError):
        chunk_text("hello", chunk_size=200, chunk_overlap=200)
    with pytest.raises(ValueError):
        chunk_text("hello", chunk_size=200, chunk_overlap=-1)


def test_paragraph_boundaries_preferred():
    para_a = "Alpha paragraph. " * 20
    para_b = "Beta paragraph. " * 20
    text = para_a.strip() + "\n\n" + para_b.strip()
    chunks = chunk_text(text, chunk_size=400, chunk_overlap=50)
    # Chunks never cut mid-sentence: every sentence inside a chunk is whole.
    for chunk in chunks:
        pieces = [p for p in chunk.text.split(". ") if p]
        assert pieces, chunk.text
        assert all(p in ("Alpha paragraph", "Beta paragraph", "Alpha paragraph.", "Beta paragraph.")
                   for p in pieces), chunk.text
        assert chunk.text.endswith("paragraph.")
