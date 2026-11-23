"""Print retrieval quality on the labelled eval set (tests/eval/eval_set.json).

Runs the real app in-process (TestClient, throwaway database): ingests the
demo documents, asks every question through POST /chat, and reports hit@1,
hit@3 and handoff accuracy, plus every miss.

Usage:
    python scripts/eval_retrieval.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    os.environ["COPILOT_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "eval.db")
    os.environ.setdefault("COPILOT_ENV", "dev")
    from fastapi.testclient import TestClient

    from app.main import app

    data = json.loads((ROOT / "tests" / "eval" / "eval_set.json").read_text())
    with TestClient(app) as client:
        client.post(
            "/auth/register",
            json={"tenant_name": "Eval", "username": "eval", "password": "eval-password"},
        ).raise_for_status()
        token = client.post(
            "/auth/login", json={"username": "eval", "password": "eval-password"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        for title, name in data["documents"].items():
            content = (ROOT / "demo-data" / name).read_text()
            client.post(
                "/documents", headers=headers, json={"title": title, "content": content}
            ).raise_for_status()
        hit1 = hit3 = answerable = handoff_ok = off_topic = 0
        for case in data["cases"]:
            body = client.post(
                "/chat", headers=headers, json={"question": case["question"]}
            ).json()
            titles = [c["document_title"] for c in body["citations"]]
            expected = case["expected_document"]
            if expected is None:
                off_topic += 1
                ok = body["needs_human"]
                handoff_ok += ok
            else:
                answerable += 1
                ok = bool(titles) and titles[0] == expected
                hit1 += ok
                hit3 += expected in titles
            mark = "ok " if ok else "MISS"
            print(f"{mark} {body['top_score']:.3f} {case['question']} -> {titles or 'handoff'}")
    print(
        f"\nhit@1 {hit1}/{answerable} ({hit1 / answerable:.0%})  "
        f"hit@3 {hit3}/{answerable} ({hit3 / answerable:.0%})  "
        f"handoffs {handoff_ok}/{off_topic} ({handoff_ok / off_topic:.0%})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
