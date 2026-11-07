# AI Support Copilot — Stage Log

Multi-tenant AI support platform: RAG chat over each tenant's documents
with cited answers, human handoff, analytics. Built one stage per run;
nothing is published to GitHub until ALL stages are complete and verified.

## Stages

### Stage 1 — Architecture + FastAPI backend core ✅ DONE (2026-11-05)

Tenants, JWT auth, document ingestion + chunking.

**Built:**
- `app/database.py` — SQLite schema: `tenants`, `users` (role owner/admin/agent),
  `documents`, `chunks`; tenant-first composite indexes; WAL + foreign keys.
- `app/security.py` — PBKDF2-HMAC-SHA256 password hashing (stdlib) + HS256 JWT
  (stdlib hmac) carrying `tenant_id` + `role`, so every request is authenticated
  and tenant-scoped in one step; tenant slug generator.
- `app/chunking.py` — recursive, structure-aware chunker: paragraph → sentence →
  word boundaries, 1000 chars / 150 overlap (~15%), Markdown heading context
  tracked per chunk, exact `char_start`/`char_end` offsets for stage-2 citations,
  hard-split for over-long sentences. Safe overlap (never flushes a chunk twice).
- `app/main.py` + `app/schemas.py` — `GET /health`, `POST /auth/register`
  (creates tenant + owner), `POST /auth/login`, `GET /auth/me`,
  `GET /tenants/me`, `POST /documents` (ingest + chunk on write),
  `GET /documents` (paginated), `GET /documents/{id}` (with chunks),
  `DELETE /documents/{id}` (cascades to chunks).
- Tenant isolation: tenant id comes only from the JWT; every query filters by
  it; cross-tenant access returns 404 (existence never leaked).
- `demo-data/` (returns policy + shipping FAQ) and `scripts/seed_demo.py`
  (registers demo tenant, ingests both docs via the public API).
- `tests/` — 24 pytest tests (7 chunking unit + 17 API), per-test throwaway DB.

**Skills applied:** `fastapi_templates` (project layout, DI, TestClient patterns),
`multi_tenant_safety_checker` (tenant_id on every table, tenant-first indexes,
cross-tenant tests), `chunking_strategy` (recursive chunking, 256–1024 / 10–20%
overlap band, heading context, semantic-boundary rules), `pytest_patterns`.

**Verification (2026-11-05):**
- `pytest tests/ -q` → **24 passed**.
- Live smoke test (uvicorn on 127.0.0.1:8000): `/health` ok, register + login ok,
  `seed_demo.py` ingested both demo docs (1 chunk each), `GET /documents` → 2 docs.
- Environment note: this VM's `no_proxy` contains bracketed IPv6 entries that
  break httpx's proxy-pattern parser (`InvalidURL: Invalid port: ':1]'`) when
  constructing `httpx.Client`; `seed_demo.py` uses `trust_env=False` (localhost
  needs no proxy). Tests use `TestClient` and are unaffected.

### Stage 2 — Retrieval + chat API with citations and handoff flag ✅ DONE (2026-11-05)

TF-IDF retrieval over the tenant's chunks (stdlib, no external service),
`POST /chat` returning an answer assembled from the top chunks with per-chunk
citations (document title + char offsets), a `needs_human` handoff flag when
no chunk clears the relevance threshold, conversation storage per tenant.

**Built:**
- `app/retrieval.py` — stdlib TF-IDF index (smoothed IDF, cosine similarity,
  rebuilt per request from the tenant's current chunks so edits take effect
  immediately); `MIN_SCORE = 0.10` relevance floor plus a ≥1 shared content
  term requirement; stopword-filtered tokenizer (numbers kept, e.g. "30 days").
  Extractive answer composer: quotes the best 1–2 sentences per cited chunk
  (scored by query-term overlap, in document order) with `[n]` markers —
  nothing is generated beyond the documents' own words, so answers cannot
  invent facts. Citation integrity: every citation carries the exact
  `char_start`/`char_end` recorded at ingestion; tests verify the offsets
  slice the stored document to the cited chunk. Heading-marker cleanup for
  quoted sentences (the chunker flattens newlines, so `#` markers and a
  doubled heading word are stripped from quotes only, never from stored text).
- `app/database.py` — `conversations` (tenant, user, title) + `messages`
  (role user/assistant, content, `needs_human` flag); tenant-first indexes.
- `app/schemas.py` — `ChatIn` (question ≤2000 chars, optional
  `conversation_id`, `top_k` 1–10), `ChatOut`, `CitationOut`,
  `ConversationOut/Detail`, `PagedConversations`.
- `app/main.py` — `POST /chat` (resolves/creates the conversation, stores
  the user question even when retrieval finds nothing so handoffs are never
  lost, stores the assistant reply with its handoff flag), `GET
  /conversations` (paginated), `GET /conversations/{id}` (with messages),
  `DELETE /conversations/{id}`. Retrieval and conversations are tenant-scoped
  by the JWT: another tenant's documents can never be cited and its
  conversations return 404.
- `tests/test_retrieval.py` (10 unit tests) + `tests/test_chat.py` (10 API
  tests): ranking, threshold/off-topic handoff, empty knowledge base, citation
  offset integrity, conversation continuation + message counts, `top_k`,
  cross-tenant isolation (chat + read + delete + continue), validation/401s.

**Skills applied:** `rag_implementation` (sparse/TF-IDF retrieval as the
right first retriever for a support KB; reranking/fusion deferred),
`hybrid_search_implementation` (fusion design noted for a later dense leg;
scores logged on every citation for tuning), `citation_verification`
(principle adapted: every citation must point at a real stored chunk with
exact offsets — verified by tests), plus stage-1 skills
(`fastapi_templates`, `multi_tenant_safety_checker`, `pytest_patterns`).

**Verification (2026-11-05):**
- `pytest tests/ -q` → **44 passed** (24 stage-1 + 20 new).
- Live smoke test (uvicorn on 127.0.0.1:8123/8124, seeded demo data):
  "How can I return an item?" → cited Returns Policy, score 0.4416,
  answer quotes "30 days" + refund sentence, `needs_human: false`;
  "What is the weather in Paris?" → fallback answer, `needs_human: true`,
  no citations; `GET /conversations` lists the stored conversations.

### Stage 3 — Web frontend (chat UI + admin) ✅ DONE (2026-11-06)

Vanilla JS chat UI + admin pages (documents, conversations), served by
the same FastAPI app; plus an embeddable chat widget for tenants' own sites.

**Built:**
- `app/static/index.html` — single-page shell: auth view (login / create
  workspace), chat view (conversation list + message log + composer),
  admin view (document upload + knowledge-base table + conversation
  review). Hash routing (`#/login`, `#/chat`, `#/admin`), session restore
  from the stored token, one `role="status"` live region for feedback.
- `app/static/js/api.js` — infrastructure layer: the only file that calls
  `fetch()`. Token in localStorage (memory fallback), auth headers, error
  mapping (`ApiError` with status; 401 clears the token and drops the
  user back to login). Covers every endpoint from stages 1–2.
- `app/static/js/chat.js` — conversation list, message log with per-answer
  citations (document title, score, heading, excerpt) and the
  `needs_human` handoff banner (`role="alert"`); Enter-to-send, typing
  indicator, send disabled while a request is in flight.
- `app/static/js/admin.js` — document upload form (title/source/content,
  live character count), knowledge-base table (title, source, chars,
  chunks, added, delete with confirm), conversation list + detail view
  with a "Needs human" / "Answered" badge computed from stored messages.
- `app/static/js/widget.js` — embeddable widget: one `<script>` tag with
  `data-api` / `data-token` / `data-title` renders a floating chat button
  + panel on any site, talks to the same `/chat` endpoint, same citations
  and handoff banner. `widget-demo.html` (`GET /widget`) shows the embed
  snippet with a live demo button.
- `app/static/css/style.css` — one stylesheet, no external assets:
  responsive (chat/admin collapse to one column), visible focus styles,
  `prefers-reduced-motion` support.
- `app/main.py` — `GET /` serves the app shell, `GET /widget` the widget
  demo, `/static` mounted for the assets; API routes untouched (verified
  by tests: `/health` and `/documents` still answer normally).
- Rendering safety: every dynamic string goes through `textContent` /
  `createElement` — no `innerHTML` assignment anywhere in the JS
  (enforced by a test that strips comments and checks for assignments),
  so document text and answers can never inject markup.

**Skills applied:** `frontend_architecture` (layered structure: api.js
infrastructure / app.js application / chat.js + admin.js presentation,
separation of concerns), `frontend_a11y` (labels on every input,
`aria-live` chat log, `role="alert"` handoff, real buttons, visible
focus, Escape closes the widget), `frontend_testing_best_practices`
(behaviour-level tests through the real app, no mocks of the app itself),
`dashboard_builder` (admin shows what an operator needs: what's in the
KB and which chats need a human — not every metric available). The
`chatbot_builder` skill was checked and NOT used: it is a SaaS-platform
(Membrane) integration, not a guide to building a chat UI.

**Verification (2026-11-06):**
- `pytest tests/ -q` → **59 passed** (44 stage-1/2 + 15 new frontend tests).
- `node --check` clean on all 5 JS files (also asserted inside the suite).
- Static cross-check: every `getElementById` in the JS matches an id in
  `index.html`; every form field name exists in the shell.
- Live smoke test (uvicorn on 127.0.0.1:8125): `/` 200, `/widget` 200,
  all 6 static assets 200, path traversal `/static/../main.py` → 404;
  register → login → ingest Returns Policy → "How can I return an item?"
  → cited answer (score 0.378, `needs_human: false`); off-topic question
  → `needs_human: true`, no citations; 2 conversations stored;
  unauthenticated `/chat` → 401. Original `copilot.db` restored after
  the smoke test (Demo Store, 2 demo docs).

### Stage 4 — Analytics, Docker Compose, full test pass, demo data, final README ✅ DONE (2026-11-07)

Per-tenant usage analytics (API + frontend view), Docker Compose
(api + named volume), full test pass, demo chat seeding, final README.
On completion: CV entry + GitHub publish (repo `ai-support-copilot`) +
profile README featured link.

**Built:**
- `app/analytics.py` — `compute_summary(db, tenant_id, days)`: headline
  totals (questions, conversations, answered, handed off, handoff rate,
  avg questions/conversation, avg citation score), a zero-filled daily
  series (questions/answered/handed off per day, last N days), top 5
  cited documents, and the last 10 questions that needed a human (the
  customer's own question text, so an operator can answer it). Computed
  from the stored messages + stored citations — no new tables, no
  background job. Headline totals cover the tenant's whole history; the
  `days` window (1–90, default 14) bounds only the daily series and the
  handoffs list. Every query is tenant-scoped by the JWT.
- `app/database.py` — additive migration: `messages.citations` JSON
  column (`ALTER TABLE … ADD COLUMN`, safe default `'[]'`) so databases
  built by stages 1–3 gain it without rewriting data (verified on a
  copy of the stage-3 `copilot.db`: column added, 2 docs / 10 messages
  intact). `COPILOT_DB_PATH` env override so Docker Compose can point
  the database at the `/data` volume.
- `app/main.py` — `GET /analytics/summary` (`days` validated 1–90);
  the chat endpoint now stores each assistant reply's citations
  (`[{document_id, document_title, score}]`) on its message row.
- `app/static/js/analytics.js` — Analytics view (`#/analytics`):
  6 headline cards, a CSS-bar daily chart (no chart library), top
  cited documents table, recent handoffs list, 7/14/30-day range
  switcher. `api.js` gained `getAnalytics(days)`; `app.js` routes
  `#/analytics`; `index.html` nav + section; `style.css` metric cards
  + bar chart styles. textContent-only rendering (same XSS guard as
  stage 3, enforced by the frontend tests).
- `Dockerfile` — python:3.12-slim, non-root user, stdlib-urllib
  healthcheck on `/health`, `/data` for the database.
  `docker-compose.yml` — one `api` service, port bound to localhost
  only, `COPILOT_SECRET` from `.env` (required), named volume
  `copilot-data`. `.env.example`, `.dockerignore`.
- `scripts/seed_demo.py` — `--demo-chats` asks three demo questions
  (two answered, one off-topic handoff) so the Analytics view has real
  numbers after seeding.
- `tests/test_analytics.py` — 9 API tests: empty tenant, mixed-chat
  counts + handoff rate + daily sums + top docs + handoffs, ranking,
  all-answered zero rate, days window + validation, cross-tenant
  isolation, 401, handoff entry carries the question (not the fallback).
  `tests/test_frontend.py` extended: analytics view ids, analytics.js
  asset + node check + textContent guard.

**Skills applied:** `docker_compose` (single service + named volume,
healthcheck, localhost binding, `.env` secrets), `docker_best_practices`
(pinned base image, non-root user, no secrets baked into the image),
`dashboard_builder` (headline numbers → trend → top sources → actionable
handoffs, reused from stage 3). `analytics_dashboard` and
`add_analytics_instrumentation` were checked and NOT used: the first is
a LinkedIn-export dashboard, the second an Amplitude pipeline — neither
fits a self-contained per-tenant usage summary computed from stored data.

**Verification (2026-11-07):**
- `pytest tests/ -q` → **68 passed** (59 stage-1/2/3 + 9 new).
- `docker-compose.yml` parses as valid YAML (PyYAML); Docker itself is
  NOT installed in this VM, so `docker compose up` could not be run
  here — recorded in the README; verify on a machine with Docker.
- Live smoke test (uvicorn on 127.0.0.1:8131, throwaway DB via
  `COPILOT_DB_PATH`, `seed_demo.py --demo-chats`): `/health` ok, 2 docs
  ingested, 3 demo chats (2 answered, 1 handoff);
  `GET /analytics/summary` → 3 questions, 2 answered, 1 handed off,
  handoff_rate 0.3333, avg_citation_score 0.3597, daily series with
  today's 3/2/1, top documents Returns Policy + Shipping FAQ (1 each),
  recent handoffs = the Paris question; unauthenticated → 401;
  `/`, `/widget`, `analytics.js`, `style.css` all 200.
- **Flagship complete — published to GitHub 2026-11-07:**
  https://github.com/mr-mohamad-mr/ai-support-copilot — snapshot commit
  `bd9842ad0963f6c05d75278739db611fea8791b4` on `main` (35 files); CV entry
  added, profile README featured projects updated.
