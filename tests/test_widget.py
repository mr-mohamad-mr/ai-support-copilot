"""Public widget key: scope (chat only), rotation, origin allowlist (CORS
preflight + actual requests) and the per-key/IP rate limit."""

from __future__ import annotations

from conftest import TEST_DB, auth_headers, register

from app.database import connect

DOC = {
    "title": "Returns Policy",
    "content": "You can return any item within 30 days of delivery for a full refund.",
}
QUESTION = {"question": "How can I return an item?"}
SHOP = "https://shop.example.com"


def _key(client, headers, origins=None):
    body = {} if origins is None else {"allowed_origins": origins}
    resp = client.post("/widget-key", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _wk(key, origin=None):
    h = {"X-Widget-Key": key}
    if origin:
        h["Origin"] = origin
    return h


def _preflight(client, origin, headers="content-type,x-widget-key", method="POST"):
    return client.options(
        "/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_create_widget_key_shows_key_once_and_stores_hash(client, alice):
    assert client.get("/widget-key", headers=alice).json()["configured"] is False
    created = _key(client, alice, [SHOP])
    assert created["key"].startswith("wk_") and len(created["key"]) > 30
    assert created["allowed_origins"] == [SHOP]
    info = client.get("/widget-key", headers=alice).json()
    assert info["configured"] is True
    assert "key" not in info
    assert created["key"].startswith(info["key_prefix"].rstrip("…"))
    db = connect(TEST_DB)
    stored = db.execute("SELECT key_hash FROM widget_keys").fetchone()[0]
    db.close()
    assert created["key"] not in stored


def test_widget_key_can_chat(client, alice):
    client.post("/documents", headers=alice, json=DOC)
    key = _key(client, alice)["key"]
    resp = client.post("/chat", headers=_wk(key), json=QUESTION)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["needs_human"] is False and "30 days" in body["answer"]
    conv = client.get(f"/conversations/{body['conversation_id']}", headers=alice).json()
    assert conv["channel"] == "widget"
    # The visitor can continue their own widget conversation.
    again = client.post(
        "/chat",
        headers=_wk(key),
        json={**QUESTION, "conversation_id": body["conversation_id"]},
    )
    assert again.status_code == 200


def test_widget_key_cannot_touch_anything_but_chat(client, alice):
    doc_id = client.post("/documents", headers=alice, json=DOC).json()["id"]
    key = _key(client, alice)["key"]
    for headers in (_wk(key), {"Authorization": f"Bearer {key}"}):
        assert client.delete(f"/documents/{doc_id}", headers=headers).status_code == 401
        assert client.get("/documents", headers=headers).status_code == 401
        assert client.post("/documents", headers=headers, json=DOC).status_code == 401
        assert client.get("/conversations", headers=headers).status_code == 401
        assert client.get("/analytics/summary", headers=headers).status_code == 401
        assert client.post("/widget-key", headers=headers, json={}).status_code == 401
        assert client.get("/users", headers=headers).status_code == 401
    assert client.get(f"/documents/{doc_id}", headers=alice).status_code == 200


def test_widget_cannot_continue_staff_conversation(client, alice):
    key = _key(client, alice)["key"]
    staff = client.post("/chat", headers=alice, json=QUESTION).json()
    resp = client.post(
        "/chat", headers=_wk(key), json={**QUESTION, "conversation_id": staff["conversation_id"]}
    )
    assert resp.status_code == 404


def test_invalid_and_missing_widget_key(client, alice):
    assert client.post("/chat", headers=_wk("wk_not-a-real-key"), json=QUESTION).status_code == 401
    assert client.post("/chat", json=QUESTION).status_code == 401


def test_rotate_invalidates_old_key_and_revoke(client, alice):
    old = _key(client, alice, [SHOP])["key"]
    new = _key(client, alice)  # rotate; allowlist kept when not given
    assert new["key"] != old
    assert new["allowed_origins"] == [SHOP]
    assert client.post("/chat", headers=_wk(old), json=QUESTION).status_code == 401
    assert client.post("/chat", headers=_wk(new["key"]), json=QUESTION).status_code == 200
    assert client.delete("/widget-key", headers=alice).status_code == 204
    assert client.post("/chat", headers=_wk(new["key"]), json=QUESTION).status_code == 401


def test_widget_key_scoped_to_its_tenant(client, alice, bob):
    client.post(
        "/documents",
        headers=bob,
        json={"title": "Beta secrets", "content": "The zebra discount code is ZEBRA50."},
    )
    key = _key(client, alice)["key"]
    body = client.post("/chat", headers=_wk(key), json={"question": "zebra discount code"}).json()
    assert body["needs_human"] is True and body["citations"] == []


def test_origin_allowlist_validation_and_normalisation(client, alice):
    _key(client, alice)
    ok = client.put(
        "/widget-key/origins",
        headers=alice,
        json={"allowed_origins": ["HTTPS://Shop.Example.com:443/", "http://localhost:3000"]},
    )
    assert ok.status_code == 200
    assert ok.json()["allowed_origins"] == ["http://localhost:3000", SHOP]
    for bad in ("*", "https://*.example.com", "ftp://x.com", "https://x.com/path", "shop.com"):
        resp = client.put("/widget-key/origins", headers=alice, json={"allowed_origins": [bad]})
        assert resp.status_code == 422, bad
    # A failed update leaves the allowlist untouched.
    info = client.get("/widget-key", headers=alice).json()
    assert info["allowed_origins"] == ["http://localhost:3000", SHOP]


def test_preflight_from_allowed_origin(client, alice):
    _key(client, alice, [SHOP])
    resp = _preflight(client, SHOP)
    assert resp.status_code == 204
    assert resp.headers["access-control-allow-origin"] == SHOP
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert "X-Widget-Key" in resp.headers["access-control-allow-headers"]
    assert resp.headers["vary"] == "Origin"


def test_preflight_from_disallowed_origin_rejected(client, alice):
    _key(client, alice, [SHOP])
    resp = _preflight(client, "https://evil.example.net")
    assert resp.status_code == 403
    assert "access-control-allow-origin" not in resp.headers
    # Allowed origin, but asking to send a JWT cross-origin: refused.
    assert _preflight(client, SHOP, headers="authorization,content-type").status_code == 403
    assert _preflight(client, SHOP, method="DELETE").status_code == 403


def test_actual_request_origin_checked_against_own_tenant(client, alice):
    client.post("/documents", headers=alice, json=DOC)
    key = _key(client, alice, [SHOP])["key"]
    ok = client.post("/chat", headers=_wk(key, SHOP), json=QUESTION)
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == SHOP
    bad = client.post("/chat", headers=_wk(key, "https://evil.example.net"), json=QUESTION)
    assert bad.status_code == 403
    assert "access-control-allow-origin" not in bad.headers
    # Another tenant allowlisting an origin does not open this tenant's key to it.
    register(client, tenant="Beta Corp", username="bob")
    bob = auth_headers(client, username="bob")
    _key(client, bob, ["https://beta.example.org"])
    assert _preflight(client, "https://beta.example.org").status_code == 204
    resp = client.post("/chat", headers=_wk(key, "https://beta.example.org"), json=QUESTION)
    assert resp.status_code == 403


def test_same_origin_pages_allowed_without_allowlist(client, alice):
    key = _key(client, alice)["key"]  # empty allowlist
    resp = client.post("/chat", headers=_wk(key, "http://testserver"), json=QUESTION)
    assert resp.status_code == 200


def test_widget_chat_rate_limited(client, alice, monkeypatch):
    monkeypatch.setenv("COPILOT_WIDGET_CHAT_RATE", "3/60")
    key = _key(client, alice, [SHOP])["key"]
    for _ in range(3):
        assert client.post("/chat", headers=_wk(key, SHOP), json=QUESTION).status_code == 200
    resp = client.post("/chat", headers=_wk(key, SHOP), json=QUESTION)
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) > 0
    # CORS headers are present so the widget can read the 429.
    assert resp.headers["access-control-allow-origin"] == SHOP
    # Signed-in users are not affected by the widget limit.
    assert client.post("/chat", headers=alice, json=QUESTION).status_code == 200


def test_widget_chat_rate_limited_per_key(client, alice, monkeypatch):
    monkeypatch.setenv("COPILOT_WIDGET_CHAT_KEY_RATE", "2/60")
    key = _key(client, alice)["key"]
    for _ in range(2):
        assert client.post("/chat", headers=_wk(key), json=QUESTION).status_code == 200
    assert client.post("/chat", headers=_wk(key), json=QUESTION).status_code == 429


def test_agent_cannot_manage_widget_key(client, alice):
    client.post(
        "/users",
        headers=alice,
        json={"username": "carol", "password": "agent-password", "role": "agent"},
    )
    agent = auth_headers(client, username="carol", password="agent-password")
    assert client.post("/widget-key", headers=agent, json={}).status_code == 403
    assert client.get("/widget-key", headers=agent).status_code == 403
    assert client.delete("/widget-key", headers=agent).status_code == 403
