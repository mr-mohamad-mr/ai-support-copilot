# AI Support Copilot

Multi-tenant AI support platform: each tenant uploads its own support
documents, and the platform answers customer questions from those documents
with citations — escalating to a human when it doesn't know.

> **Status: complete — all 4 stages done** (backend core + retrieval/chat
> API + web frontend + analytics & Docker). 68 pytest tests passing.
> GitHub publish (repo `ai-support-copilot`) follows in this run; see
> `STAGES.md` for the stage log.

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│ FastAPI app (app/main.py) — one process, SQLite database   │
│  /auth/*      register (tenant + owner), login, me         │
│  /tenants/me  current tenant                               │
│  /documents   ingest (chunk on write), list, get, delete   │
│  /chat        ask a question; TF-IDF retrieval + citations │
│  /conversations  stored chats: list, get, delete           │
│  /analytics/summary  per-tenant usage analytics (stage 4)  │
├────────────────────────────────────────────────────────────┤
│ app/security.py  PBKDF2 passwords + HS256 JWT (stdlib)    │
│                  token carries tenant_id + role            │
│ app/chunking.py  recursive chunker, 1000 chars / 15% overlap│
│                  heading context + exact char offsets      │
│ app/retrieval.py TF-IDF retrieval (stdlib), cosine ≥ 0.10  │
│                  extractive answers with [n] citations,    │
│                  needs_human handoff when nothing matches │
│ app/analytics.py per-tenant usage summary (stage 4):       │
│                  counts, handoff rate, daily series, top   │
│                  cited documents, recent handoffs          │
│ app/database.py  tenants / users / documents / chunks      │
│                  conversations / messages (stage 2)        │
│                  + citations JSON on messages (stage 4)    │
│ app/static/      vanilla JS frontend (stage 3 + 4):        │
│                  index.html shell, api/chat/admin/         │
│                  analytics.js, widget.js embeddable chat,  │
│                  style.css                                  │
└────────────────────────────────────────────────────────────┘
```

**Multi-tenancy:** every tenant-owned row carries `tenant_id`; the id is
taken from the JWT on every request, never from client input. Cross-tenant
access returns 404 (the resource's existence is never leaked). Tenant-first
composite indexes keep those filters fast.

**Chunking:** documents are split on structural boundaries (paragraph →
sentence → word) with ~15% overlap, so a chunk keeps its meaning on its
own. Each chunk stores its Markdown heading and exact character offsets,
which stage 2 uses for citations.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000/> — the web app (sign in or create a
workspace, chat, and manage documents/conversations from Admin).

Seed demo data (tenant `Demo Store`, user `demo` / `demo-password`,
two sample documents):

```bash
.venv/bin/python scripts/seed_demo.py
```

Add `--demo-chats` to also ask three demo questions (two answered from
the documents, one off-topic handoff) so the Analytics view has data:

```bash
.venv/bin/python scripts/seed_demo.py --demo-chats
```

## Analytics (stage 4)

`GET /analytics/summary?days=14` (Bearer token, own tenant only;
`days` 1–90, default 14) returns headline totals plus a daily series,
top cited documents and recent handoffs:

| Field | Meaning |
| ----- | ------- |
| `total_questions` / `total_conversations` | lifetime totals for this tenant |
| `answered` / `handed_off` / `handoff_rate` | assistant replies split by the `needs_human` flag |
| `avg_questions_per_conversation` | questions per conversation |
| `avg_citation_score` | mean top-citation score on answered questions (`null` when none) |
| `daily[]` | one row per day (`date`, `questions`, `answered`, `handed_off`), zero-filled |
| `top_documents[]` | the 5 most-cited documents with citation counts |
| `recent_handoffs[]` | the last 10 questions that needed a human, with conversation id |

The frontend's **Analytics** view (`#/analytics`, range 7 / 14 / 30 days)
shows the same data as headline cards, a daily bar chart, a top-documents
table and the recent handoffs list. Analytics is computed from the stored
messages and the citations saved on each assistant reply — no new tables,
no background job. Headline totals cover the tenant's whole history; the
`days` window bounds only the daily series and the handoffs list.

## Docker (stage 4)

```bash
cp .env.example .env   # set a real COPILOT_SECRET
docker compose up --build
```

Then open <http://127.0.0.1:8000/>. The API runs as a non-root user in a
single container; the SQLite database lives in the named volume
`copilot-data` (mounted at `/data`, selected via `COPILOT_DB_PATH`),
so data survives `docker compose down`. The compose file binds the port
to localhost only and health-checks `GET /health` with stdlib urllib.
Seed demo data inside the stack with:

```bash
docker compose run --rm api python scripts/seed_demo.py --demo-chats
```

> Note: Docker is not installed in the build VM, so the image and the
> compose stack could not be run there — the compose file was validated
> by YAML parsing and the Dockerfile follows the `docker_best_practices`
> guidance (pinned base image, non-root user, no secrets in the image).
> Run `docker compose up --build` on a machine with Docker to verify.

## Web frontend (stage 3)

Vanilla JS, no build step, no external assets — served by the same
FastAPI app:

- **`GET /`** — the app shell: sign in / create workspace, **Chat**
  (conversation list, message log with citations and the human-handoff
  banner), **Admin** (upload documents, knowledge-base table, review
  conversations with a “Needs human” badge).
- **`GET /widget`** — demo + embed snippet for the **embeddable chat
  widget** (`app/static/js/widget.js`): one `<script>` tag with
  `data-api` / `data-token` / `data-title` adds a floating support chat
  to any website, talking to the same `/chat` endpoint.
- **Layers** (`frontend_architecture`): `js/api.js` is the only file
  that calls `fetch()` (token storage, auth headers, error mapping);
  `js/app.js` owns hash routing and auth; `js/chat.js` / `js/admin.js`
  own their views. All dynamic content is rendered with `textContent` /
  `createElement` — no `innerHTML` anywhere, so document text and
  answers can never inject markup (enforced by a test).
- **Accessibility** (`frontend_a11y`): labels on every input, `aria-live`
  chat log, `role="alert"` handoff banner, visible focus styles, Escape
  closes the widget, `prefers-reduced-motion` respected.

## Chat (stage 2)

`POST /chat` asks a question against the tenant's own documents only. The
retriever is stdlib TF-IDF (cosine similarity, relevance floor 0.10, at
least one shared content term). The answer is **extractive**: it quotes the
best sentences from the top chunks with `[n]` markers and a citation per
chunk (document title, chunk index, heading, exact char offsets, score,
matched terms) — nothing is generated beyond the documents' own words. When
no chunk clears the threshold, the answer is an honest handoff message and
`needs_human` is `true`. Every exchange is stored as a conversation
(`conversation_id` continues an existing chat).

| Method | Endpoint | Auth | Description |
| ------ | -------- | ---- | ----------- |
| POST | `/chat` | Bearer | `{question, conversation_id?, top_k?}` → `{answer, citations, needs_human, top_score}` |
| GET | `/conversations?page=&page_size=` | Bearer | paginated, own tenant only |
| GET | `/conversations/{id}` | Bearer | conversation with its messages |
| DELETE | `/conversations/{id}` | Bearer | delete conversation + messages |

Example:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question": "How can I return an item?"}'
# → {"answer": "…within 30 days of delivery. … [1]", "citations": [{"document_title": "Returns Policy", "char_start": 0, "char_end": 515, "score": 0.4416, …}], "needs_human": false, "top_score": 0.4416}
```

## API (stage 1)

| Method | Endpoint | Auth | Description |
| ------ | -------- | ---- | ----------- |
| GET | `/health` | — | liveness |
| POST | `/auth/register` | — | `{tenant_name, username, password}` → tenant + owner user |
| POST | `/auth/login` | — | → `{access_token}` (JWT, 24 h) |
| GET | `/auth/me` | Bearer | current user + tenant |
| GET | `/tenants/me` | Bearer | current tenant |
| POST | `/documents` | Bearer | `{title, content, source?}` → document with chunks |
| GET | `/documents?page=&page_size=` | Bearer | paginated, own tenant only |
| GET | `/documents/{id}` | Bearer | document with its chunks |
| DELETE | `/documents/{id}` | Bearer | delete document + chunks |

Set `COPILOT_SECRET` in production — the default JWT secret is dev-only.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q   # 68 tests, all passing
```

Tests use a throwaway SQLite file per test (via the `get_db` dependency
override) and cover chunking invariants (sizes, offsets, overlap, headings,
hard-splits), auth, tenant isolation and pagination, plus retrieval ranking,
the handoff threshold, citation offset integrity, conversation storage and
cross-tenant chat isolation, analytics totals/rates/series/ranking and
isolation, and the frontend (served pages, static assets, no-`innerHTML`
XSS guard, `node --check` on every JS file, full register → chat journey).

## Skills used

`fastapi_templates`, `multi_tenant_safety_checker`, `chunking_strategy`,
`pytest_patterns`, `rag_implementation`, `hybrid_search_implementation`,
`citation_verification`, `frontend_architecture`, `frontend_a11y`,
`frontend_testing_best_practices`, `dashboard_builder`,
`docker_compose`, `docker_best_practices` — see `STAGES.md` for the
per-stage breakdown. (`analytics_dashboard` and
`add_analytics_instrumentation` were checked and NOT used: the first is a
LinkedIn-export dashboard, the second an Amplitude pipeline — neither
fits a self-contained per-tenant usage summary.)
