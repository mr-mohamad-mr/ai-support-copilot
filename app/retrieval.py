"""Full-text retrieval (SQLite FTS5 + BM25) and extractive answers.

Retrieval is dependency-free: it uses the FTS5 module built into the
standard library's ``sqlite3``. Support queries reuse the documents' own
vocabulary (policy terms, product names, error codes), so sparse keyword
retrieval is the right first retriever. No embedding service, no network.

How a question is answered:

1. The question is tokenized (lowercase alphanumerics, stopwords removed).
2. FTS5 finds candidate chunks containing any query term, restricted to
   the caller's tenant, ordered by FTS5's ``bm25()`` (``CANDIDATE_POOL``).
   The index is maintained by triggers (see ``database.py``), so nothing
   is rebuilt per request and query cost follows the index, not the corpus.
3. The candidates are re-scored with BM25 using the *tenant's own*
   statistics (chunk count, average length, per-term document frequency),
   normalised to 0..1 by the best score the query could reach. That makes
   the score comparable across questions, so a fixed relevance floor
   (``MIN_SCORE``) decides between answering and handing off to a human.

Citation integrity: every citation points at a real chunk of the tenant's
database with the exact ``char_start``/``char_end`` offsets recorded at
ingestion time; answers are assembled only from the retrieved chunks' own
sentences, never invented (the optional LLM step in ``llm.py`` is checked
against the same citations).
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field

# Relevance floor on the normalised BM25 score (0..1). A chunk below it is
# "not an answer" and, if nothing clears it, the question is handed off to
# a human. Calibrated on the demo corpus and tests/eval: on-topic questions
# score roughly 0.25-0.6; a question sharing one incidental word with the
# knowledge base scores below 0.12.
MIN_SCORE = 0.12

# BM25 parameters (the usual defaults) and how many FTS5 candidates are
# re-scored per question.
# A hit must also score at least this fraction of the best hit, so a strong
# answer is not padded with weakly related chunks from other documents.
RELATIVE_FLOOR = 0.4

BM25_K1 = 1.2
BM25_B = 0.75
CANDIDATE_POOL = 50

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


def stem(token: str) -> str:
    """Light, conservative English suffix stripping.

    Folds the plural / past / -ing / trailing-e variants support questions
    use ("refunds", "refunded" -> "refund"; "arrive", "arriving" ->
    "arriv"; "batteries" -> "battery"). Short tokens and numbers are kept
    as they are. Applied identically to documents and questions.
    """
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        token = token[:-3] + "y"
    elif token.endswith("sses"):
        token = token[:-2]
    elif token.endswith(("ches", "shes", "xes", "zes")):
        token = token[:-2]
    elif token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    if token.endswith("ing") and len(token) > 5:
        token = token[:-3]
    elif token.endswith("ed") and len(token) > 4:
        token = token[:-2]
    if len(token) > 4 and token[-1] == token[-2] and token[-1] not in "lsz":
        token = token[:-1]  # "shipped" -> "shipp" -> "ship"
    if token.endswith("e") and len(token) > 4:
        token = token[:-1]
    return token


def index_text(text: str) -> str:
    """The normalised search terms of ``text``, space separated (stored in
    ``chunks.search_text`` and indexed by FTS5)."""
    return " ".join(stem(t) for t in tokenize(text))


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


def _fts_query(terms: list[str]) -> str:
    # Terms are [a-z0-9]+ (see tokenize), so quoting them is always safe.
    return " OR ".join(f'"{t}"' for t in terms)


def _idf(n_chunks: int, df: int) -> float:
    # BM25 idf with the +1 inside the log (Lucene style): always positive,
    # so tiny knowledge bases (two or three chunks) still rank sensibly.
    return math.log(1 + (n_chunks - df + 0.5) / (df + 0.5))


def search_chunks(
    db: sqlite3.Connection, tenant_id: int, query: str, top_k: int = DEFAULT_TOP_K
) -> list[ScoredChunk]:
    """Rank ``tenant_id``'s chunks for ``query``; returns at most ``top_k``.

    Only chunks with ``score >= MIN_SCORE`` (and at least one shared content
    term, which the FTS match guarantees) are returned, best first. An empty
    query, or one with no content terms, returns nothing.
    """
    # Surface query terms (reported as matched_terms) keyed by their stem.
    surface: dict[str, list[str]] = {}
    for token in tokenize(query):
        words = surface.setdefault(stem(token), [])
        if token not in words:
            words.append(token)
    terms = list(surface)
    if not terms:
        return []
    n_chunks, avg_len = db.execute(
        "SELECT COUNT(*), AVG(word_count) FROM chunks WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    if not n_chunks:
        return []
    avg_len = float(avg_len or 1.0)

    candidates = db.execute(
        "SELECT c.id, c.document_id, d.title AS document_title, c.idx, c.heading,"
        " c.text, c.char_start, c.char_end, c.word_count, c.search_text"
        " FROM chunks_fts CROSS JOIN chunks c ON c.id = chunks_fts.rowid"
        " CROSS JOIN documents d ON d.id = c.document_id"
        " WHERE chunks_fts MATCH ? AND c.tenant_id = ?"
        " ORDER BY chunks_fts.rank LIMIT ?",
        (_fts_query(terms), tenant_id, CANDIDATE_POOL),
    ).fetchall()
    if not candidates:
        return []

    idf: dict[str, float] = {}
    for term in terms:
        df = db.execute(
            # CROSS JOIN pins the plan: walk the term's postings in the
            # index, then filter by tenant (never probe FTS once per chunk).
            "SELECT COUNT(*) FROM chunks_fts CROSS JOIN chunks c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? AND c.tenant_id = ?",
            (f'"{term}"', tenant_id),
        ).fetchone()[0]
        idf[term] = _idf(n_chunks, df)
    # Best reachable score: every query term present with saturated tf.
    # Terms the knowledge base never uses stay in the denominator, so a
    # question that is mostly about something else scores low.
    max_score = sum(idf.values()) * (BM25_K1 + 1)

    scored: list[ScoredChunk] = []
    for row in candidates:
        doc_terms = row["search_text"].split()
        counts: dict[str, int] = {}
        for token in doc_terms:
            if token in idf:
                counts[token] = counts.get(token, 0) + 1
        if not counts:
            continue
        length_norm = 1 - BM25_B + BM25_B * (row["word_count"] or 1) / avg_len
        raw = sum(
            idf[t] * tf * (BM25_K1 + 1) / (tf + BM25_K1 * length_norm) for t, tf in counts.items()
        )
        score = raw / max_score
        if score < MIN_SCORE:
            continue
        scored.append(
            ScoredChunk(
                chunk_id=row["id"],
                document_id=row["document_id"],
                document_title=row["document_title"],
                index=row["idx"],
                heading=row["heading"],
                text=row["text"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                score=round(score, 4),
                matched_terms=sorted(w for t in counts for w in surface[t]),
            )
        )
    scored.sort(key=lambda s: (-s.score, s.document_id, s.index))
    if scored:
        floor = scored[0].score * RELATIVE_FLOOR
        scored = [s for s in scored if s.score >= floor]
    return scored[:top_k]


def insert_chunks(db: sqlite3.Connection, document_id: int, tenant_id: int, chunks) -> None:
    """Store a document's chunks with their search terms. The FTS5 index is
    updated by the ``chunks_fts_ai`` trigger in the same transaction."""
    db.executemany(
        "INSERT INTO chunks (document_id, tenant_id, idx, text, heading, char_start,"
        " char_end, word_count, search_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                document_id,
                tenant_id,
                c.index,
                c.text,
                c.heading,
                c.char_start,
                c.char_end,
                c.word_count,
                index_text(c.text),
            )
            for c in chunks
        ],
    )


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
        citations.append(citation_for(hit))
    answer = " ".join(parts)
    return answer, citations


def citation_for(hit: ScoredChunk) -> dict:
    """The API citation for a retrieved chunk."""
    return {
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


FALLBACK_ANSWER = (
    "I couldn't find an answer in your support documents, so I've flagged "
    "this for a human agent — they'll follow up with you."
)
