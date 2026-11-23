"""Retrieval quality gate: a small labelled eval set (tests/eval/) run
end to end through POST /chat. Each answerable question must cite its
expected document first; off-topic questions must hand off to a human."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = json.loads((ROOT / "tests" / "eval" / "eval_set.json").read_text())

MIN_HIT_RATE = 0.85  # top-1 citation is the expected document
MIN_HIT_RATE_AT_3 = 0.95  # expected document among the citations (top_k=3)
MIN_HANDOFF_ACCURACY = 1.0  # every off-topic question is handed off


def _run(client, headers):
    for title, name in EVAL["documents"].items():
        resp = client.post(
            "/documents",
            headers=headers,
            json={"title": title, "content": (ROOT / "demo-data" / name).read_text()},
        )
        assert resp.status_code == 201
    results = []
    for case in EVAL["cases"]:
        resp = client.post("/chat", headers=headers, json={"question": case["question"]})
        assert resp.status_code == 200
        results.append((case, resp.json()))
    return results


def test_eval_set_is_well_formed():
    answerable = [c for c in EVAL["cases"] if c["expected_document"]]
    assert len(answerable) >= 15
    assert len(EVAL["cases"]) - len(answerable) >= 3
    for case in answerable:
        assert case["expected_document"] in EVAL["documents"]


def test_eval_hit_rate(client, alice):
    results = [(c, b) for c, b in _run(client, alice) if c["expected_document"]]
    top1 = [
        bool(b["citations"]) and b["citations"][0]["document_title"] == c["expected_document"]
        for c, b in results
    ]
    at3 = [
        c["expected_document"] in [cit["document_title"] for cit in b["citations"]]
        for c, b in results
    ]
    misses = [c["question"] for (c, _), ok in zip(results, top1, strict=True) if not ok]
    hit_rate = sum(top1) / len(top1)
    assert hit_rate >= MIN_HIT_RATE, f"hit@1 {hit_rate:.2f}, misses: {misses}"
    assert sum(at3) / len(at3) >= MIN_HIT_RATE_AT_3


def test_eval_off_topic_questions_hand_off(client, alice):
    results = [(c, b) for c, b in _run(client, alice) if not c["expected_document"]]
    handed_off = [b["needs_human"] and not b["citations"] for _, b in results]
    assert sum(handed_off) / len(handed_off) >= MIN_HANDOFF_ACCURACY
