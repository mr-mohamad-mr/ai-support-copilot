"""Isolated test setup: a throwaway SQLite file per session, wired into
the app via the get_db dependency override."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import connect  # noqa: E402
from app.main import app, get_db  # noqa: E402

TEST_DB = Path(__file__).resolve().parent / "test_copilot.db"


@pytest.fixture()
def client():
    # Fresh database per test so registrations never collide across tests.
    if TEST_DB.exists():
        TEST_DB.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(TEST_DB) + suffix)
        if p.exists():
            p.unlink()

    def _override_db():
        conn = connect(TEST_DB)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    if TEST_DB.exists():
        TEST_DB.unlink()


def register(client, tenant="Acme Support", username="alice", password="correct-horse"):
    resp = client.post(
        "/auth/register",
        json={"tenant_name": tenant, "username": username, "password": password},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth_headers(client, username="alice", password="correct-horse"):
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def alice(client):
    register(client)
    return auth_headers(client)


@pytest.fixture()
def bob(client):
    register(client, tenant="Beta Corp", username="bob")
    return auth_headers(client, username="bob")
