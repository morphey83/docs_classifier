# DocsClassifier

Self-hosted, multi-user document store: upload documents or archives, tag them
manually through a processing queue, and find them again with faceted search
(static characteristics + tags + optional full-text). Optional per-document OCR
and full-text indexing. Web API + UI, plus a Telegram bot that mirrors it.
Documents live in shareable **domains** with role-based access.

Full design: [`docs/architecture.md`](docs/architecture.md).

## Status

**Phase 0 — skeleton.** Done: config, async DB, Alembic migrations, auth
(register / login / logout / me) with server-side session cookies, `/health`,
a SAQ worker stub, Docker Compose (`db` + `web` + `worker`), test suite.

Roadmap (§12 of the architecture doc): 1 core · 2 ingest + search · 3 OCR ·
4 sets & sharing · 5 trash & lifecycle · 6 bot · 7 web UI.

## Stack

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 (async) · Alembic ·
SAQ (Postgres-backed jobs, no Redis) · aiogram 3 (bot) · Caddy · Docker Compose.

## Run

```bash
cp .env.example .env      # set SECRET_KEY, POSTGRES_PASSWORD, DATABASE_URL
docker compose up --build -d
```

`web` runs `alembic upgrade head` on start. API docs at `http://127.0.0.1:8000/docs`.

## Develop

```bash
python -m venv .venv && . .venv/Scripts/activate      # or bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check .
```

Tests run on an in-memory SQLite database — no PostgreSQL needed locally.
