"""Recursive, structure-aware text chunking for the RAG knowledge base.

Design follows the ``chunking_strategy`` skill:
- Level 2/3 (recursive + structure-aware): split on structural boundaries
  first (blank lines → newlines → sentence ends → words), so chunks keep
  semantic units together instead of cutting mid-sentence.
- Defaults: 1000 characters per chunk with 150 characters (~15%) overlap,
  inside the skill's recommended 256–1024 / 10–20% band.
- Markdown heading context: the most recent ``#`` heading active at a
  chunk's start is stored with it, so a retrieved chunk stays interpretable
  on its own (contextual-retrieval style, without an LLM call).
- Every chunk records exact ``char_start``/``char_end`` offsets into the
  source document, so stage 2 can cite the precise passage an answer
  came from.

The implementation works on *segments* (sentences/paragraphs with known
offsets) and packs them greedily; a segment longer than ``chunk_size`` is
hard-split at word boundaries. Overlap is built by carrying whole trailing
segments of the previous chunk into the next one, so offsets stay exact
and no chunk ever starts mid-word.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 150

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])|\n+")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit of a document."""

    index: int
    text: str
    heading: str
    char_start: int
    char_end: int

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass(frozen=True)
class _Segment:
    """A sentence/paragraph slice with its offsets in the source text."""

    text: str
    start: int
    end: int
    heading: str


def _segments(text: str) -> list[_Segment]:
    """Split text into sentence/paragraph segments, tracking headings.

    Paragraphs are split on blank lines; inside a paragraph, sentences are
    split after ``.``/``!``/``?``. Heading lines (``# ...``) become their own
    segment and set the heading context for everything that follows.
    """
    segments: list[_Segment] = []
    heading = ""
    # Walk paragraph by paragraph so offsets stay exact.
    for para_match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text):
        para = para_match.group(0)
        base = para_match.start()
        stripped = para.strip()
        if not stripped:
            continue
        head_match = _HEADING.match(stripped)
        if head_match:
            heading = head_match.group(2)
            segments.append(
                _Segment(
                    text=stripped,
                    start=base + para.index(stripped),
                    end=base + para.index(stripped) + len(stripped),
                    heading=heading,
                )
            )
            continue
        # Sentence-split this paragraph, keeping offsets.
        cursor = 0
        for sent in _SENTENCE_END.split(para):
            start = para.index(sent, cursor)
            end = start + len(sent)
            cursor = end
            piece = sent.strip()
            if piece:
                segments.append(
                    _Segment(
                        text=piece,
                        start=base + start + (len(sent) - len(sent.lstrip())),
                        end=base + start + len(sent.rstrip()),
                        heading=heading,
                    )
                )
    return segments


def _hard_split(segment: _Segment, chunk_size: int) -> list[_Segment]:
    """Split one over-long segment at word boundaries, keeping offsets."""
    words = segment.text.split(" ")
    pieces: list[_Segment] = []
    current: list[str] = []
    current_len = 0
    # Offset of the first word inside the original segment.
    offset = segment.start + (len(segment.text) - len(segment.text.lstrip()))
    pos = offset
    for word in words:
        word_len = len(word) + (1 if current else 0)
        if current and current_len + word_len > chunk_size:
            text = " ".join(current)
            # pos points one past the trailing space of the last word, so
            # the piece's exclusive end is pos - 1 and its start is end - len.
            pieces.append(_Segment(text=text, start=pos - 1 - current_len, end=pos - 1, heading=segment.heading))
            current, current_len = [], 0
        current.append(word)
        current_len += word_len
        pos += len(word) + 1
    if current:
        text = " ".join(current)
        pieces.append(_Segment(text=text, start=pos - 1 - current_len, end=pos - 1, heading=segment.heading))
    return pieces


def _overlap_tail(segments: list[_Segment], chunk_overlap: int) -> list[_Segment]:
    """Trailing segments of a flushed chunk, totalling ~``chunk_overlap`` chars.

    Whole segments are carried when they fit; a single over-long trailing
    segment is sliced at a word boundary instead (offsets stay exact because
    a segment is one contiguous slice of the source). Returns ``[]`` when
    there is nothing sensible to carry.
    """
    carried: list[_Segment] = []
    total = 0
    for seg in reversed(segments):
        if total >= chunk_overlap:
            break
        room = chunk_overlap - total
        if len(seg.text) > room:
            if total > 0:
                break  # already have some overlap; don't slice mid-segment
            tail = seg.text[-chunk_overlap:]
            cut = tail.find(" ")
            if cut != -1:
                tail = tail[cut + 1 :]
            if not tail:
                break
            carried.append(
                _Segment(
                    text=tail,
                    start=seg.end - len(tail),
                    end=seg.end,
                    heading=seg.heading,
                )
            )
            total += len(tail)
            break
        carried.append(seg)
        total += len(seg.text) + 1
    return list(reversed(carried))


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    """Split ``text`` into overlapping, heading-annotated chunks.

    Raises ``ValueError`` for invalid parameters. Returns ``[]`` for empty
    or whitespace-only input.
    """
    if chunk_size < 100:
        raise ValueError("chunk_size must be at least 100")
    if not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")
    if not text or not text.strip():
        return []

    segments: list[_Segment] = []
    for seg in _segments(text):
        if len(seg.text) > chunk_size:
            segments.extend(_hard_split(seg, chunk_size))
        else:
            segments.append(seg)
    if not segments:
        return []

    chunks: list[Chunk] = []
    current: list[_Segment] = []
    current_len = 0

    def flush() -> None:
        if not current:
            return
        joined = " ".join(s.text for s in current)
        chunks.append(
            Chunk(
                index=len(chunks),
                text=joined,
                heading=current[0].heading,
                char_start=current[0].start,
                char_end=current[-1].end,
            )
        )

    i = 0
    while i < len(segments):
        seg = segments[i]
        added = len(seg.text) + (1 if current else 0)
        if current and current_len + added > chunk_size:
            flushed = list(current)
            flush()
            # Overlap: carry the tail of the flushed chunk — but only when
            # the pending segment still fits alongside it. Otherwise skip
            # the overlap for this boundary, so packing always progresses
            # and no chunk is ever flushed twice.
            carried = _overlap_tail(flushed, chunk_overlap)
            carried_len = sum(len(s.text) for s in carried) + max(0, len(carried) - 1)
            if carried and carried_len + 1 + len(seg.text) <= chunk_size:
                current = carried
                current_len = carried_len
            else:
                current = []
                current_len = 0
            continue  # re-try this segment against the (possibly empty) chunk
        current.append(seg)
        current_len += added
        i += 1
    flush()
    return chunks
