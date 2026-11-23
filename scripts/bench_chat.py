"""Measure /chat latency on a large synthetic knowledge base.

Builds a throwaway SQLite database, registers a tenant, ingests a
deterministic synthetic corpus (default: 5 documents of ~0.9 MB, about
4-5k chunks) through the public API, then times ``POST /chat``.

Usage:
    python scripts/bench_chat.py [--docs 5] [--doc-chars 900000] [--runs 30]

No network, no external services: the app runs in-process via TestClient.
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOPIC_WORDS = (
    "return refund shipping delivery express tracking warranty invoice payment "
    "account password subscription cancel upgrade discount voucher exchange "
    "damaged parcel courier warehouse customs battery charger screen repair "
    "replacement order receipt billing address email support"
).split()

QUESTIONS = [
    "How do I get a refund for a damaged parcel?",
    "How long does express delivery take?",
    "Can I cancel my subscription and get a voucher?",
    "Where is my tracking link for the courier?",
    "How do I change the billing address on my invoice?",
    "Is screen repair covered by the warranty?",
]


def _corpus(n_docs: int, doc_chars: int, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    syllables = ["ka", "lo", "mi", "ten", "ra", "vo", "su", "pel", "dor", "ix", "an", "qu"]
    vocab = sorted(
        {"".join(rng.choice(syllables) for _ in range(rng.randint(2, 4))) for _ in range(6000)}
    )
    docs = []
    for d in range(n_docs):
        parts: list[str] = [f"# Synthetic help centre volume {d + 1}\n\n"]
        size = len(parts[0])
        section = 0
        while size < doc_chars:
            if rng.random() < 0.05:
                section += 1
                block = f"## Section {section} {rng.choice(TOPIC_WORDS)}\n\n"
            else:
                words = [
                    rng.choice(TOPIC_WORDS) if rng.random() < 0.15 else rng.choice(vocab)
                    for _ in range(rng.randint(8, 20))
                ]
                block = " ".join(words).capitalize() + ". "
                if rng.random() < 0.2:
                    block += "\n\n"
            if size + len(block) > doc_chars:
                break
            parts.append(block)
            size += len(block)
        docs.append("".join(parts))
    return docs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=5)
    parser.add_argument("--doc-chars", type=int, default=900_000)
    parser.add_argument("--runs", type=int, default=30)
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="copilot-bench-")
    os.environ["COPILOT_DB_PATH"] = str(Path(tmp) / "bench.db")
    os.environ.setdefault("COPILOT_ENV", "dev")

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        client.post(
            "/auth/register",
            json={"tenant_name": "Bench", "username": "bench", "password": "bench-password"},
        ).raise_for_status()
        token = client.post(
            "/auth/login", json={"username": "bench", "password": "bench-password"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        t0 = time.perf_counter()
        chunks = 0
        for i, text in enumerate(_corpus(args.docs, args.doc_chars)):
            resp = client.post(
                "/documents", headers=headers, json={"title": f"Volume {i + 1}", "content": text}
            )
            resp.raise_for_status()
            chunks += resp.json()["chunk_count"]
        ingest_s = time.perf_counter() - t0

        # Warm-up, then timed runs.
        client.post("/chat", headers=headers, json={"question": QUESTIONS[0]})
        timings = []
        for r in range(args.runs):
            q = QUESTIONS[r % len(QUESTIONS)]
            t = time.perf_counter()
            resp = client.post("/chat", headers=headers, json={"question": q})
            timings.append(time.perf_counter() - t)
            resp.raise_for_status()

    timings.sort()
    p95 = timings[max(0, int(len(timings) * 0.95) - 1)]
    print(f"documents={args.docs} chunks={chunks} ingest={ingest_s:.2f}s")
    print(
        f"/chat over {args.runs} runs: median={statistics.median(timings) * 1000:.1f} ms"
        f" mean={statistics.mean(timings) * 1000:.1f} ms p95={p95 * 1000:.1f} ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
