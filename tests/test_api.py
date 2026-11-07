"""API tests: auth, tenants, document ingestion, tenant isolation."""

from __future__ import annotations

from conftest import auth_headers, register

SAMPLE_DOC = (
    "# Returns Policy\n\n"
    "You can return any item within 30 days of delivery. "
    "Items must be unused and in their original packaging.\n\n"
    "## Refunds\n\n"
    "Refunds are issued to the original payment method within five working days. "
    "Shipping costs are refunded only when the item arrived damaged or incorrect."
)


def _create_doc(client, headers, title="Returns Policy", content=SAMPLE_DOC):
    resp = client.post(
        "/documents", json={"title": title, "content": content}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_register_creates_tenant_and_owner(client):
    data = register(client)
    assert data["username"] == "alice"
    assert data["role"] == "owner"
    assert data["tenant"]["name"] == "Acme Support"
    assert data["tenant"]["slug"] == "acme-support"


def test_register_duplicate_username_conflicts(client, alice):
    resp = client.post(
        "/auth/register",
        json={"tenant_name": "Other", "username": "alice", "password": "correct-horse"},
    )
    assert resp.status_code == 409


def test_register_short_password_rejected(client):
    resp = client.post(
        "/auth/register",
        json={"tenant_name": "Tiny", "username": "tiny", "password": "short"},
    )
    assert resp.status_code == 422


def test_login_wrong_password_unauthorized(client, alice):
    resp = client.post("/auth/login", json={"username": "alice", "password": "nope-nope-nope"})
    assert resp.status_code == 401


def test_auth_me_returns_tenant(client, alice):
    resp = client.get("/auth/me", headers=alice)
    assert resp.status_code == 200
    assert resp.json()["tenant"]["slug"] == "acme-support"


def test_tenants_me(client, alice):
    resp = client.get("/tenants/me", headers=alice)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Acme Support"


def test_documents_require_auth(client):
    assert client.get("/documents").status_code == 401
    assert client.post("/documents", json={"title": "x", "content": "y"}).status_code == 401


def test_tampered_token_rejected(client, alice):
    token = alice["Authorization"].split(" ", 1)[1]
    bad = {"Authorization": f"Bearer {token[:-2]}xx"}
    assert client.get("/documents", headers=bad).status_code == 401


def test_create_document_chunks_and_counts(client, alice):
    doc = _create_doc(client, alice)
    assert doc["chunk_count"] >= 1
    assert doc["char_count"] == len(SAMPLE_DOC)
    assert len(doc["chunks"]) == doc["chunk_count"]
    first = doc["chunks"][0]
    assert first["index"] == 0
    assert first["heading"] == "Returns Policy"
    assert first["char_start"] == 0
    assert "return any item within 30 days" in first["text"]


def test_create_document_empty_content_rejected(client, alice):
    resp = client.post(
        "/documents", json={"title": "Empty", "content": "  "}, headers=alice
    )
    assert resp.status_code == 422


def test_list_documents_paginates(client, alice):
    for i in range(3):
        _create_doc(client, alice, title=f"Doc {i}", content=f"Content {i}. " * 5)
    page1 = client.get("/documents?page=1&page_size=2", headers=alice).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    page2 = client.get("/documents?page=2&page_size=2", headers=alice).json()
    assert len(page2["items"]) == 1


def test_get_document_returns_chunks(client, alice):
    created = _create_doc(client, alice)
    resp = client.get(f"/documents/{created['id']}", headers=alice)
    assert resp.status_code == 200
    assert len(resp.json()["chunks"]) == created["chunk_count"]


def test_delete_document_removes_it(client, alice):
    created = _create_doc(client, alice)
    assert client.delete(f"/documents/{created['id']}", headers=alice).status_code == 204
    assert client.get(f"/documents/{created['id']}", headers=alice).status_code == 404
    assert client.get("/documents", headers=alice).json()["total"] == 0


def test_cross_tenant_isolation(client, alice, bob):
    """Bob must not see, read or delete Alice's documents (404, not 403,
    so existence is never leaked across tenants)."""
    created = _create_doc(client, alice)
    doc_id = created["id"]
    assert client.get(f"/documents/{doc_id}", headers=bob).status_code == 404
    assert client.delete(f"/documents/{doc_id}", headers=bob).status_code == 404
    assert client.get("/documents", headers=bob).json()["total"] == 0
    # Alice's document is untouched.
    assert client.get(f"/documents/{doc_id}", headers=alice).status_code == 200


def test_tenant_slug_collision_gets_suffix(client):
    register(client, tenant="Acme Support", username="alice")
    other = register(client, tenant="Acme Support!", username="carol")
    assert other["tenant"]["slug"] == "acme-support-2"
