"""Auth hardening: login/register rate limits, the startup secret check,
and role enforcement on management endpoints."""

from __future__ import annotations

import pytest
from conftest import auth_headers, register
from fastapi.testclient import TestClient

from app import config
from app.main import app

# --- rate limits ----------------------------------------------------------


def test_login_rate_limited_per_ip_and_account(client, monkeypatch):
    monkeypatch.setenv("COPILOT_LOGIN_RATE", "3/300")
    register(client)
    bad = {"username": "alice", "password": "wrong-password"}
    for _ in range(3):
        assert client.post("/auth/login", json=bad).status_code == 401
    resp = client.post("/auth/login", json=bad)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0
    # Even the right password is refused while the account is locked out...
    good = {"username": "alice", "password": "correct-horse"}
    assert client.post("/auth/login", json=good).status_code == 429
    # ...but another account from the same IP is not affected.
    register(client, tenant="Beta Corp", username="bob")
    assert client.post("/auth/login", json={**good, "username": "bob"}).status_code == 200


def test_successful_login_clears_failures(client, monkeypatch):
    monkeypatch.setenv("COPILOT_LOGIN_RATE", "3/300")
    register(client)
    bad = {"username": "alice", "password": "wrong-password"}
    good = {"username": "alice", "password": "correct-horse"}
    for _ in range(2):
        assert client.post("/auth/login", json=bad).status_code == 401
    assert client.post("/auth/login", json=good).status_code == 200
    for _ in range(2):
        assert client.post("/auth/login", json=bad).status_code == 401
    assert client.post("/auth/login", json=good).status_code == 200


def test_login_rate_limited_per_ip_across_accounts(client, monkeypatch):
    monkeypatch.setenv("COPILOT_LOGIN_IP_RATE", "4/300")
    for i in range(4):
        resp = client.post("/auth/login", json={"username": f"user{i}", "password": "x" * 8})
        assert resp.status_code == 401
    resp = client.post("/auth/login", json={"username": "someone-else", "password": "x" * 8})
    assert resp.status_code == 429


def test_register_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setenv("COPILOT_REGISTER_RATE", "2/3600")
    register(client, tenant="T1", username="user1")
    register(client, tenant="T2", username="user2")
    resp = client.post(
        "/auth/register",
        json={"tenant_name": "T3", "username": "user3", "password": "correct-horse"},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# --- startup secret check ---------------------------------------------------


def test_refuses_to_start_with_default_secret(monkeypatch):
    monkeypatch.delenv("COPILOT_SECRET", raising=False)
    monkeypatch.delenv("COPILOT_ENV", raising=False)
    with pytest.raises(RuntimeError, match="COPILOT_SECRET"):
        config.validate_settings()
    with pytest.raises(RuntimeError, match="COPILOT_SECRET"):
        with TestClient(app):
            pass
    monkeypatch.setenv("COPILOT_SECRET", config.DEV_SECRET)
    with pytest.raises(RuntimeError):
        config.validate_settings()


def test_default_secret_allowed_in_dev(monkeypatch):
    monkeypatch.delenv("COPILOT_SECRET", raising=False)
    monkeypatch.setenv("COPILOT_ENV", "dev")
    config.validate_settings()  # no error


def test_real_secret_starts_in_production(monkeypatch):
    monkeypatch.setenv("COPILOT_SECRET", "a-long-random-production-secret-value")
    monkeypatch.setenv("COPILOT_ENV", "production")
    config.validate_settings()


# --- roles -----------------------------------------------------------------

DOC = {"title": "Returns Policy", "content": "You can return any item within 30 days."}


def _add_user(client, headers, username, role):
    resp = client.post(
        "/users",
        headers=headers,
        json={"username": username, "password": "agent-password", "role": role},
    )
    return resp


def _login(client, username):
    return auth_headers(client, username=username, password="agent-password")


def test_owner_adds_agent_and_admin(client, alice):
    assert _add_user(client, alice, "carol", "agent").status_code == 201
    resp = _add_user(client, alice, "dave", "admin")
    assert resp.status_code == 201
    assert resp.json()["role"] == "admin"
    users = client.get("/users", headers=alice).json()
    assert [(u["username"], u["role"]) for u in users] == [
        ("alice", "owner"),
        ("carol", "agent"),
        ("dave", "admin"),
    ]
    me = client.get("/auth/me", headers=_login(client, "carol")).json()
    assert me["role"] == "agent" and me["tenant"]["name"] == "Acme Support"


def test_cannot_create_owner_via_users_endpoint(client, alice):
    assert _add_user(client, alice, "mallory", "owner").status_code == 422


def test_agent_cannot_manage(client, alice):
    doc_id = client.post("/documents", headers=alice, json=DOC).json()["id"]
    _add_user(client, alice, "carol", "agent")
    agent = _login(client, "carol")
    # Management endpoints are refused...
    assert client.post("/documents", headers=agent, json=DOC).status_code == 403
    assert client.delete(f"/documents/{doc_id}", headers=agent).status_code == 403
    assert client.get("/analytics/summary", headers=agent).status_code == 403
    assert client.get("/users", headers=agent).status_code == 403
    assert _add_user(client, agent, "eve", "agent").status_code == 403
    # ...while the agent can still work: read documents, chat, read chats.
    assert client.get("/documents", headers=agent).json()["total"] == 1
    chat = client.post("/chat", headers=agent, json={"question": "How do I return an item?"})
    assert chat.status_code == 200 and not chat.json()["needs_human"]
    conv_id = chat.json()["conversation_id"]
    assert client.get(f"/conversations/{conv_id}", headers=agent).status_code == 200
    assert client.delete(f"/conversations/{conv_id}", headers=agent).status_code == 403
    assert client.get(f"/documents/{doc_id}", headers=agent).status_code == 200


def test_admin_manages_documents_but_not_admins(client, alice):
    _add_user(client, alice, "dave", "admin")
    admin = _login(client, "dave")
    resp = client.post("/documents", headers=admin, json=DOC)
    assert resp.status_code == 201
    assert client.delete(f"/documents/{resp.json()['id']}", headers=admin).status_code == 204
    assert _add_user(client, admin, "carol", "agent").status_code == 201
    assert _add_user(client, admin, "erin", "admin").status_code == 403


def test_delete_user_rules(client, alice, bob):
    carol = _add_user(client, alice, "carol", "agent").json()
    owner_id = client.get("/auth/me", headers=alice).json()["id"]
    assert client.delete(f"/users/{owner_id}", headers=alice).status_code == 403
    # Another tenant's manager cannot see or delete this tenant's users.
    assert client.delete(f"/users/{carol['id']}", headers=bob).status_code == 404
    assert client.delete(f"/users/{carol['id']}", headers=alice).status_code == 204
    resp = client.post(
        "/auth/login", json={"username": "carol", "password": "agent-password"}
    )
    assert resp.status_code == 401


def test_role_change_applies_to_existing_tokens(client, alice):
    """The role is read from the database on every request, not trusted
    from the token, so a demotion takes effect immediately."""
    _add_user(client, alice, "dave", "admin")
    admin = _login(client, "dave")
    assert client.get("/users", headers=admin).status_code == 200
    from conftest import TEST_DB

    from app.database import connect

    db = connect(TEST_DB)
    db.execute("UPDATE users SET role = 'agent' WHERE username = 'dave'")
    db.commit()
    db.close()
    assert client.get("/users", headers=admin).status_code == 403
