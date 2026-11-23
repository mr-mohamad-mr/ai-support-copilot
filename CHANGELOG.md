# Changelog

## 0.2.0 (2026-11-23)

### Security
- **Widget key**: the embeddable widget now uses a per-tenant public widget
  key (`X-Widget-Key`) that can only call `POST /chat`. Owners/admins
  create, rotate and revoke it (`/widget-key`); only its SHA-256 hash is
  stored.
- **CORS**: `/chat` answers browser preflights only for origins on a
  tenant's allowlist (`PUT /widget-key/origins`), and checks the origin of
  widget requests against the key's own tenant. `Authorization` is never
  allowed cross-origin.
- **Rate limits** (in memory, configurable via `COPILOT_*_RATE`): failed
  logins per IP + username and per IP, registrations per IP, widget
  questions per key + IP and per key. Over the limit: `429` + `Retry-After`.
- **Startup check**: the app refuses to start with the built-in dev JWT
  secret unless `COPILOT_ENV=dev`.
- **Roles enforced**: owner/admin manage documents, users, the widget key,
  conversation deletion and analytics; agents chat and read. New `/users`
  endpoints to add and remove admins and agents.

### Added
- Optional grounded LLM step (`COPILOT_LLM_PROVIDER`: `openai`,
  `openai-compatible`, `ollama`), OpenAI-compatible API via stdlib. The
  model sees only retrieved chunks; replies without valid `[n]` citations
  fall back to the extractive answer. Off by default.
- `answer_mode` on chat responses, `channel` on conversations (additive).
- Retrieval eval set (`tests/eval/`) with a hit-rate test, plus
  `scripts/eval_retrieval.py` and `scripts/bench_chat.py`.
- Two more demo documents (account & billing, warranty & repairs).
- Admin UI card for the widget key and allowed origins;
  `seed_demo.py --widget-key`.
- GitHub Actions CI (ruff + pytest on Python 3.11 and 3.12), ruff config,
  exact dependency pins (`requirements.txt`, `requirements-dev.txt`).

### Changed
- Retrieval uses SQLite FTS5 with BM25 instead of rebuilding a TF-IDF index
  on every question (median `/chat` latency on 4,406 chunks: about
  350 ms → 17 ms). Light stemming matches word variants. Scores are
  normalised BM25 (0..1) with a relevance floor of 0.12, so `top_score`
  values differ from 0.1.x. Existing databases are migrated on startup.
- Tests use the `httpx2` test-client transport (removes the Starlette
  deprecation warning).

### Fixed
- Intermittent `500` errors under concurrent requests: the per-request
  SQLite connection could be used from a different threadpool thread than
  the one that opened it.

### Breaking changes
- `widget.js` no longer accepts `data-token`; use `data-widget-key` with a
  widget key. Embedding a login token in a public page is no longer
  supported.
- Agents (`role = agent`) get `403` on management endpoints (documents
  create/delete, conversation delete, analytics, users, widget key).
  Existing accounts are all owners, so they are unaffected.
- Without `COPILOT_SECRET`, the app only starts with `COPILOT_ENV=dev`.
- `POST /auth/login` and `POST /auth/register` can return `429`.

## 0.1.0

First release: multi-tenant FastAPI + SQLite backend with JWT auth,
document ingestion and chunking, TF-IDF retrieval with extractive cited
answers and human handoff, conversation storage, per-tenant analytics, a
vanilla JS web app and embeddable widget, and a Docker Compose setup.
