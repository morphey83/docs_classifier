# DocsClassifier

Self-hosted, multi-user document store: upload documents or archives, tag them
manually through a processing queue, and find them again with faceted search
(static characteristics + tags + optional full-text). Optional per-document OCR
and full-text indexing. Web API + UI, plus a Telegram bot that mirrors it.
Documents live in shareable **domains** with role-based access.

Full design: [`docs/architecture.md`](docs/architecture.md).

## Status

**Phases 0–1 done.**

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

Roadmap (§12 of the architecture doc): 4 sets & sharing ·
5 trash & lifecycle · 6 bot · 7 web UI.

## Stack

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 (async) · Alembic ·
SAQ (Postgres-backed jobs, no Redis) · aiogram 3 (bot) · Caddy · Docker Compose.

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
