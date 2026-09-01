# DocsClassifier

Self-hosted, multi-user document store: upload documents or archives, tag them
manually through a processing queue, and find them again with faceted search
(static characteristics + tags + optional full-text). Optional per-document OCR
and full-text indexing. Web API + UI, plus a Telegram bot that mirrors it.
Documents live in shareable **domains** with role-based access.

Full design: [`docs/architecture.md`](docs/architecture.md).

## Status

**Phases 0–6 done, 7a (web UI core) done.**

- **0 skeleton** — config, async DB, Alembic, auth (session cookies), `/health`,
  SAQ worker stub, Docker Compose, tests.
- **1 core** — domains + members (6 roles) + invites + capability-checked
  endpoints; content-addressed blob storage; single-file upload with
  dedup / replace / new + per-domain quota; document CRUD + `doc_date`
  extraction (PDF / Office); inbox queue with per-user defer;
  flat tags CRUD / merge / assignment.

- **2 ingest + search** — archive upload (zip/7z/rar/tar) → background
  extraction with bomb guards; upload batches with per-entry outcomes;
  opt-in body-text indexing (`POST /documents/{id}/index`); faceted search
  with facet counts; `POST /domains/{d}/exports` → zip + manifest artifact.
- **3 OCR** — SAQ worker (Postgres) with a `JOB_MODE=inline` fallback for
  dev; `POST /documents/{id}/ocr` + per-domain `auto_ocr` (ocrmypdf / tesseract);
  `ocr_status` / `ocr_at` fields + `has_ocr` search filter; searchable-PDF
  sidecars.
- **4 sets & sharing** — hand-curated document sets (`private` / `domain`
  visibility); the set archive is a rebuild-on-demand cache keyed by a
  deterministic set-content hash — `GET …/sets/{s}/archive/download` streams it
  when current, else `202 {status:"building"}` and a rebuild is queued;
  permanent / one-time share links bound to the set's stable artifact, served by
  the public `GET /d/{token}` with rights re-checked every hit
  (`allow_public_links`, per-IP rate limit); per-domain `set_archive_ttl_days`.
- **5 trash & lifecycle** — `DELETE /documents/{id}` soft-delete → `GET
  /domains/{d}/trash` (also `?include_trash=true`); `POST …/restore`; owner-only
  `POST /domains/{d}/trash/purge`; nightly `cleanup` SAQ cron — trash retention
  → hard purge with blob refcount GC, expired export / set-archive files,
  orphan-blob sweep. Dedup uniqueness is now partial (trashed content doesn't
  block a re-upload).
- **6a API groundwork** — `GET /documents` cross-domain search (`domain_id`
  optional filter, omitted = every domain the caller belongs to), tag filters
  now match by name (case-insensitive) so they compose across domains, `GET
  /tags` aggregates tag-name options the same way; per-domain `allowed_types`
  file-type policy (`415` on a disallowed direct upload, `skipped_type` batch
  outcome for a disallowed archive entry); `PUBLIC_BASE_URL` makes share links
  absolute; auto-reindex on a title edit; Telegram account linking
  (`tg_link_token`, bidirectional, always verified via a deep-link round
  trip) — `POST /auth/tg-link`, `GET /tg/link/{token}` (minimal standalone
  page), `GET /tg/link/{token}/status`, `POST /tg/link/{token}/confirm`.
- **6b Telegram bot** (`python -m app.bot`, aiogram 3, long-polling) —
  shares the DB and `app/services/*` directly. `/start` links the account
  (both directions); `/domain` picks the upload target; send a file / archive
  → the domain's inbox (with allowed-type feedback); `/inbox` walks the queue
  tagging as you go; `/find` is cross-domain with a mini-syntax
  (`#tag type:pdf 2024 ocr:yes`) and paged results, each with inline actions
  (send file, edit tags / title, request OCR / index, add to a set); `/sets`
  lists sets and hands back the archive (as a file ≤ 50 MB or a share link).
  Current domain + last search persist in `bot_user_state`.
- **7a web UI** (HTMX + Jinja, `app/web/`, served at `/`) — login / register /
  logout (session cookie + signed CSRF token); dashboard of your domains;
  domain overview; faceted **search** (filter form + live HTMX results +
  facets + pagination); document page with inline tag / title / notes edits
  and OCR / index buttons; upload (file or archive, with batch results and
  name-conflict resolution). Vendored `pico.css` + `htmx` (no CDN, no build).
  **The JSON API moved under `/api`** (`/d/{token}`, `/tg/link/*`, `/health`
  stay at the root).

Roadmap (§12): 7b sets / inbox / tag-vocabulary UI · 7c members / settings /
trash / profile.

## Stack

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 (async) · Alembic ·
SAQ (Postgres-backed jobs, no Redis) · aiogram 3 (bot, long-polling) ·
Caddy · Docker Compose.

The bot runs as an opt-in compose service:

```bash
# set BOT_TOKEN, TELEGRAM_BOT_USERNAME, PUBLIC_BASE_URL in .env
docker compose --profile bot up -d --build
```

## Run

### On a server (Docker)

```bash
cp .env.example .env      # set SECRET_KEY, POSTGRES_PASSWORD, DATABASE_URL
docker compose up --build -d
```

`web` runs `alembic upgrade head` on start. API docs at `http://127.0.0.1:8000/docs`.

### Locally, no Docker (SQLite)

Needs only the dev virtualenv. A local `.env` pointing at SQLite is enough:

```
DEBUG=true
SECRET_KEY=local-dev
DATABASE_URL=sqlite+aiosqlite:///./dev.db
COOKIE_SECURE=false
```

```bash
alembic upgrade head                       # creates dev.db
uvicorn app.main:app --reload              # http://127.0.0.1:8000/docs
```

## Develop

```bash
python -m venv .venv && . .venv/Scripts/activate      # or bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

Tests run on an in-memory SQLite database — no PostgreSQL needed locally.
