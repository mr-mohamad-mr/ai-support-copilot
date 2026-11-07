"""TF-IDF retrieval + extractive answer composition (stage 2).

Deliberately dependency-free (stdlib only) and deterministic, following the
``rag_implementation`` skill's sparse-retrieval pattern: keyword/TF-IDF
retrieval is the right first retriever for a support knowledge base, where
queries reuse the documents' own vocabulary (policy terms, product names,
error codes). No external embedding service, no paid API, no network.

The ``hybrid_search_implementation`` skill's fusion idea is kept in mind
for later stages (a dense retriever can be added as a second leg and fused
by reciprocal-rank fusion), but stage 2 ships the sparse leg only, with
scores logged on every citation so the fusion weights can be tuned later.

Citation integrity (``citation_verification`` principle, adapted from
academic citations to RAG): every citation must point at a real chunk that
exists in the tenant's database, with the exact ``char_start``/``char_end``
offsets recorded at ingestion time — answers are assembled only from the
retrieved chunks' own sentences, never invented. A citation whose offsets
do not slice the stored document text is a bug, and the tests check that.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# Relevance floor: a chunk whose cosine similarity to the question is below
# this is treated as "not an answer". Calibrated against the demo corpus in
# tests/test_retrieval.py — on-topic questions score 0.3–0.6, unrelated ones
# score 0.0–0.05. A chunk also needs at least one shared content term (see
# ``search``), so a tiny cosine from pure stopword overlap can never pass.
MIN_SCORE = 0.10

# How many chunks a chat answer may cite at most (and the API default).
DEFAULT_TOP_K = 3
MAX_TOP_K = 10

# Sentences quoted per cited chunk in the assembled answer.
MAX_SENTENCES_PER_CHUNK = 2

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Small English stopword set: words that carry no topical meaning for
# support questions. Deliberately conservative — domain words (return,
# refund, shipping, order) are never stopped.
STOPWORDS = frozenset(
    """
    a an the and or but if then else when while of at by for with about
    into through after over between out against during without before
    under around among is am are was were be been being have has had
    having do does did doing would should could ought i you he she it
    we they me him her us them my your his its our their this that
    these those there here what which who whom how why can could shall
    will just not no yes so very really also only own same too as
    from on in to of
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords removed.

    Numbers are kept (order numbers, "30 days") and hyphenated words split
    ("pre-paid" -> "pre", "paid"), which matches how users phrase queries.
    """
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


@dataclass
class ScoredChunk:
    """A chunk with its retrieval score and the query terms that hit it."""

    chunk_id: int
    document_id: int
    document_title: str
    index: int
    heading: str
    text: str
    char_start: int
    char_end: int
    score: float
    matched_terms: list[str] = field(default_factory=list)


class TfIdfIndex:
    """In-memory TF-IDF index over one tenant's chunks.

    Built fresh per chat request from the tenant's current chunks, so
    document edits are reflected immediately with no stale-index problem.
    Corpora here are small (a support KB is hundreds of chunks, not
    millions), so a rebuild per request is cheap and keeps the code simple.
    """

    def __init__(self, chunks: list[dict]):
        self._chunks = chunks
        self._doc_count = len(chunks)
        self._doc_freq: dict[str, int] = {}
        self._vectors: list[dict[str, float]] = []
        self._norms: list[float] = []
        for chunk in chunks:
            tokens = tokenize(chunk["text"])
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for term in counts:
                self._doc_freq[term] = self._doc_freq.get(term, 0) + 1
            self._vectors.append(counts)
        for counts in self._vectors:
            weights: dict[str, float] = {}
            total = sum(counts.values()) or 1
            for term, count in counts.items():
                tf = count / total
                idf = math.log((1 + self._doc_count) / (1 + self._doc_freq[term])) + 1.0
                weights[term] = tf * idf
            norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
            self._vectors[len(self._norms)] = weights
            self._norms.append(norm)

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> list[ScoredChunk]:
        """Rank the tenant's chunks for ``query``; returns at most ``top_k``.

        Only chunks with ``score >= MIN_SCORE`` and at least one shared
        content term are returned, ordered by score descending. An empty
        query (or one with no content terms) returns nothing.
        """
        query_terms = tokenize(query)
        if not query_terms or not self._chunks:
            return []
        q_counts: dict[str, int] = {}
        for term in query_terms:
            q_counts[term] = q_counts.get(term, 0) + 1
        q_weights: dict[str, float] = {}
        for term, count in q_counts.items():
            df = self._doc_freq.get(term, 0)
            if df == 0:
                continue  # term appears in no chunk: contributes nothing
            idf = math.log((1 + self._doc_count) / (1 + df)) + 1.0
            q_weights[term] = (count / len(query_terms)) * idf
        q_norm = math.sqrt(sum(w * w for w in q_weights.values()))
        if q_norm == 0:
            return []

        scored: list[ScoredChunk] = []
        for i, chunk in enumerate(self._chunks):
            weights = self._vectors[i]
            matched = [t for t in q_weights if t in weights]
            if not matched:
                continue
            dot = sum(q_weights[t] * weights[t] for t in matched)
            score = dot / (q_norm * self._norms[i])
            if score < MIN_SCORE:
                continue
            scored.append(
                ScoredChunk(
                    chunk_id=chunk["id"],
                    document_id=chunk["document_id"],
                    document_title=chunk["document_title"],
                    index=chunk["idx"],
                    heading=chunk["heading"],
                    text=chunk["text"],
                    char_start=chunk["char_start"],
                    char_end=chunk["char_end"],
                    score=round(score, 4),
                    matched_terms=sorted(matched),
                )
            )
        scored.sort(key=lambda s: (-s.score, s.document_id, s.index))
        return scored[:top_k]


_HEADING_MARKER_RE = re.compile(r"(^|(?<=[.!?]\s))#{1,6}\s+")
_REPEATED_LEAD_RE = re.compile(r"^(\S+)\s+\1(\s|$)", re.IGNORECASE)


def _clean_sentence(sentence: str) -> str:
    """Make a quoted sentence read cleanly without changing its meaning.

    The chunker flattens newlines, so Markdown headings sit inline in the
    chunk text (``# Returns Policy You can return…``, ``…warehouse. ##
    Refunds Refunds are issued…``). Two small cleanups, both quote-safe
    (they touch only heading markup, never body words):

    1. Strip ``#`` markers at the start of the text or right after a
       sentence boundary.
    2. Drop a doubled leading word (``Refunds Refunds are…``) — the heading
       word followed by a sentence that starts with the same word.
    """
    text = _HEADING_MARKER_RE.sub(r"\1", sentence.strip())
    text = " ".join(text.split())
    m = _REPEATED_LEAD_RE.match(text)
    if m:
        text = text[m.end(1) + 1 :]
    return text


def _split_sentences(text: str) -> list[str]:
    return [_clean_sentence(s) for s in _SENTENCE_RE.split(text.strip()) if s.strip()]


def best_sentences(chunk_text: str, query_terms: list[str], limit: int) -> list[str]:
    """The sentences of a chunk that best answer the question.

    Sentences are scored by how many distinct query terms they contain
    (ties broken by term count, then by original position), so the answer
    quotes the sentence that actually addresses the question, not just the
    chunk's first line. Falls back to the chunk's first sentence when no
    sentence contains a query term.
    """
    sentences = _split_sentences(chunk_text)
    if not sentences:
        return []
    term_set = set(query_terms)
    scored: list[tuple[int, int, int, str]] = []
    for pos, sentence in enumerate(sentences):
        tokens = set(tokenize(sentence))
        distinct = len(tokens & term_set)
        total = sum(1 for t in tokenize(sentence) if t in term_set)
        scored.append((distinct, total, -pos, sentence))
    scored.sort(reverse=True)
    if scored[0][0] == 0:
        return [sentences[0]]
    picked = sorted(scored[:limit], key=lambda s: -s[2])  # back to doc order
    return [s[3] for s in picked]


def compose_answer(question: str, hits: list[ScoredChunk]) -> tuple[str, list[dict]]:
    """Assemble the chat answer and its citations from retrieved chunks.

    The answer is extractive: quoted sentences from the cited chunks, each
    followed by a ``[n]`` marker pointing at the citation list. Nothing is
    generated beyond the documents' own words, so the answer cannot invent
    facts the knowledge base does not contain. Returns ``(answer, citations)``
    where each citation carries the document title, chunk index, heading,
    exact char offsets and the retrieval score.
    """
    query_terms = tokenize(question)
    parts: list[str] = []
    citations: list[dict] = []
    for n, hit in enumerate(hits, start=1):
        sentences = best_sentences(hit.text, query_terms, MAX_SENTENCES_PER_CHUNK)
        quote = " ".join(sentences)
        parts.append(f"{quote} [{n}]")
        citations.append(
            {
                "document_id": hit.document_id,
                "document_title": hit.document_title,
                "chunk_index": hit.index,
                "heading": hit.heading,
                "char_start": hit.char_start,
                "char_end": hit.char_end,
                "excerpt": hit.text[:300],
                "score": hit.score,
                "matched_terms": hit.matched_terms,
            }
        )
    answer = " ".join(parts)
    return answer, citations


FALLBACK_ANSWER = (
    "I couldn't find an answer in your support documents, so I've flagged "
    "this for a human agent — they'll follow up with you."
)
