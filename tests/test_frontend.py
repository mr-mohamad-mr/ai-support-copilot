"""Stage 3 — frontend tests.

Behaviour-level: the served pages, the static assets, and a full
user journey (register → login → ingest → chat) exercised through the
same pages the browser loads. No mocking of the app itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
JS_FILES = sorted((STATIC / "js").glob("*.js"))


def test_index_serves_app_shell(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "AI Support Copilot" in resp.text
    # Shell contains every view and loads every JS layer.
    for marker in (
        'id="view-login"',
        'id="view-chat"',
        'id="view-admin"',
        'id="view-analytics"',
        'id="chat-log"',
        'id="doc-form"',
        'id="admin-conv-list"',
        'id="analytics-cards"',
        'id="analytics-chart"',
        'id="analytics-chart-empty"',
        'id="analytics-top-docs"',
        'id="analytics-top-docs-empty"',
        'id="analytics-handoffs"',
        'id="analytics-handoffs-empty"',
        'id="analytics-days-label"',
        'id="analytics-range"',
        "/static/js/api.js",
        "/static/js/chat.js",
        "/static/js/admin.js",
        "/static/js/analytics.js",
        "/static/js/app.js",
        "/static/css/style.css",
    ):
        assert marker in resp.text, marker
    # Accessibility hooks present in the shell.
    assert 'aria-live="polite"' in resp.text
    assert 'role="log"' in resp.text


def test_widget_demo_page_served(client):
    resp = client.get("/widget")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "widget.js" in resp.text
    assert "data-token" in resp.text


@pytest.mark.parametrize(
    "path, content_type",
    [
        ("/static/css/style.css", "text/css"),
        ("/static/js/api.js", "javascript"),
        ("/static/js/app.js", "javascript"),
        ("/static/js/chat.js", "javascript"),
        ("/static/js/admin.js", "javascript"),
        ("/static/js/analytics.js", "javascript"),
        ("/static/js/widget.js", "javascript"),
    ],
)
def test_static_assets_served(client, path, content_type):
    resp = client.get(path)
    assert resp.status_code == 200, path
    assert content_type in resp.headers["content-type"]
    assert len(resp.text) > 100


def test_static_path_traversal_blocked(client):
    resp = client.get("/static/../main.py")
    assert resp.status_code in (400, 404)


def test_health_still_works_after_frontend_mount(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_api_routes_not_shadowed_by_static(client, alice):
    """Frontend mount must not swallow API paths."""
    resp = client.get("/documents", headers=alice)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_no_innerhtml_with_dynamic_data():
    """XSS guard: views must render API data via textContent / DOM APIs,
    never by interpolating it into innerHTML."""
    import re

    for path in JS_FILES:
        src = path.read_text()
        # Strip comments so a mention of innerHTML in a comment can't
        # trip the check; only actual assignments count.
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"(^|\s)//.*$", "", code, flags=re.M)
        assert not re.search(r"\.innerHTML\s*\+?=", code), f"{path.name} assigns innerHTML"
        assert "document.write" not in code
    assert "textContent" in (STATIC / "js" / "chat.js").read_text()
    assert "textContent" in (STATIC / "js" / "admin.js").read_text()
    assert "textContent" in (STATIC / "js" / "analytics.js").read_text()
    assert "textContent" in (STATIC / "js" / "widget.js").read_text()


def test_js_files_parse():
    """Syntax-check every frontend file with node (no browser needed)."""
    import subprocess

    node = subprocess.run(["node", "--version"], capture_output=True)
    if node.returncode != 0:
        pytest.skip("node not available")
    for path in JS_FILES:
        result = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_full_journey_through_api_and_pages(client):
    """The flow the frontend drives: register → login → ingest a doc →
    chat against it → review the conversation in admin. All through the
    real app, with the pages served alongside."""
    assert client.get("/").status_code == 200

    reg = client.post(
        "/auth/register",
        json={"tenant_name": "Demo Store", "username": "demo", "password": "demo-password"},
    )
    assert reg.status_code == 201
    login = client.post("/auth/login", json={"username": "demo", "password": "demo-password"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    doc = client.post(
        "/documents",
        headers=headers,
        json={
            "title": "Returns Policy",
            "source": "help centre",
            "content": "You can return any item within 30 days of delivery for a full refund.",
        },
    )
    assert doc.status_code == 201
    assert doc.json()["chunk_count"] >= 1

    chat = client.post(
        "/chat", headers=headers, json={"question": "How can I return an item?"}
    )
    assert chat.status_code == 200
    body = chat.json()
    assert body["needs_human"] is False
    assert "30 days" in body["answer"]
    assert body["citations"], "answer must cite the returns document"
    assert body["citations"][0]["document_title"] == "Returns Policy"

    convs = client.get("/conversations", headers=headers)
    assert convs.status_code == 200
    assert convs.json()["total"] == 1
    detail = client.get(f"/conversations/{body['conversation_id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["message_count"] == 2

    docs = client.get("/documents", headers=headers)
    assert docs.json()["total"] == 1


def test_handoff_visible_to_admin(client, alice):
    """An off-topic question is stored with needs_human so the admin
    view can surface it for a human agent."""
    chat = client.post("/chat", headers=alice, json={"question": "What is the weather in Paris?"})
    assert chat.status_code == 200
    assert chat.json()["needs_human"] is True
    conv_id = chat.json()["conversation_id"]
    detail = client.get(f"/conversations/{conv_id}", headers=alice)
    assert any(m["needs_human"] for m in detail.json()["messages"])
