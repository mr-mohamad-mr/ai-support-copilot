"""Seed a demo tenant and ingest the demo documents via the public API.

Usage (server running on localhost:8000):
    python scripts/seed_demo.py [--base-url http://127.0.0.1:8000] [--demo-chats] [--widget-key]

Creates tenant "Demo Store" / user ``demo`` (password ``demo-password``)
and uploads every ``.md`` file in ``demo-data/``. Safe to re-run: a second
run registers a fresh tenant (slug gets a numeric suffix).

With ``--demo-chats`` it also asks three demo questions through the
chat API — two answered from the documents, one off-topic (a handoff) —
so the Analytics view has real numbers to show. With ``--widget-key`` it
also creates the tenant's public widget key and prints an embed snippet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


DEMO_CHATS = [
    "How can I return an item?",
    "How long does express delivery take?",
    "What is the weather in Paris today?",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--demo-chats",
        action="store_true",
        help="ask three demo questions (two answered, one handoff) so"
        " the Analytics view has data to show",
    )
    parser.add_argument(
        "--widget-key",
        action="store_true",
        help="also create a public widget key and print an embed snippet",
    )
    args = parser.parse_args()

    # trust_env=False: the seed target is a local server, so proxy settings
    # from the environment are ignored.
    with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
        reg = client.post(
            "/auth/register",
            json={
                "tenant_name": "Demo Store",
                "username": "demo",
                "password": "demo-password",
            },
        )
        reg.raise_for_status()
        print(f"tenant: {reg.json()['tenant']['slug']}")
        login = client.post(
            "/auth/login", json={"username": "demo", "password": "demo-password"}
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        for path in sorted((ROOT / "demo-data").glob("*.md")):
            resp = client.post(
                "/documents",
                json={"title": path.stem.replace("-", " ").title(), "content": path.read_text()},
                headers=headers,
            )
            resp.raise_for_status()
            doc = resp.json()
            print(f"ingested {path.name}: {doc['chunk_count']} chunks")
        if args.demo_chats:
            for question in DEMO_CHATS:
                resp = client.post("/chat", json={"question": question}, headers=headers)
                resp.raise_for_status()
                body = resp.json()
                print(
                    f"chat: {question!r} -> needs_human={body['needs_human']},"
                    f" citations={len(body['citations'])}"
                )
        if args.widget_key:
            resp = client.post("/widget-key", json={}, headers=headers)
            resp.raise_for_status()
            key = resp.json()["key"]
            print(f"widget key: {key}")
            print(
                f'embed: <script src="{args.base_url}/static/js/widget.js"'
                f' data-api="{args.base_url}" data-widget-key="{key}"></script>'
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
