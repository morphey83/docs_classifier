# DocsClassifier

Self-hosted, multi-user document store: upload documents or archives, tag them
manually through a processing queue, and find them again with faceted search
(static characteristics + tags + optional full-text). Optional per-document OCR
and full-text indexing. Web API + UI, plus a Telegram bot that mirrors it.
Documents live in shareable **domains** with role-based access.

Full design: [`docs/architecture.md`](docs/architecture.md).

## Status

**Phases 0–7 done — the app is feature-complete.**

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
- **7 web UI** (HTMX + Jinja on **Tabler** — Bootstrap 5, vendored, no CDN,
  no build; session cookie + signed CSRF) —
  **The JSON API moved under `/api`** (`/d/{token}`, `/tg/link/*`, `/health`
  stay at the root).
  - **7a** login / register / logout; dashboard; domain overview; faceted
    search (filter form + live HTMX results + facets + pager); document page
    (inline tag / title / notes edits, OCR / index); upload (file or archive,
    batch results, name-conflict resolution).
  - **7b** document sets (list / detail / items / archive download / share
    links); inbox card-by-card processing; per-domain tag-vocabulary
    management (create / rename / recolour / merge / delete); `/search`
    across every domain you belong to.
  - **7c** members + role changes + invites; domain settings
    (`allowed_types`, auto-OCR / auto-index, quotas, retention, …);
    trash (restore / owner purge); profile (Telegram linking, password).
  - **7e** "Очередь на сортировку" is a domain-filterable table of every
    unlabelled document; a modal card tags them one by one ("Готово,
    дальше" loads the next and refreshes the table). Image documents get
    a cached WebP thumbnail (`app/services/thumbs.py`,
    `GET /documents/{id}/thumb`), shown on the document page, the inbox
    card, and the bot's `/inbox` prompt.
  - **7f** search-filter polish (extension picker from real data,
    humanised status, no facet block); vendored Lucide-style SVG icon
    set (`app/web/static/icons.svg`) replacing the emoji.
  - **7g** tagging modal redesign (full-width preview, editable name
    field, one-line chips, Отложить/Готово row); the bot now sends image
    documents as photo cards, including in `/find` results; web search
    results show image thumbnails; readable tag chips.
  - **7h** full reskin onto **Tabler** (`@tabler/core` 1.4.0, MIT,
    Bootstrap 5.3) — vendored CSS/JS, no build; top navbar, native cards,
    forms, tables, badges, tabs, and a real Bootstrap modal for inbox
    tagging. pico.css dropped.
  - **7i** search results as a card grid (image or colour-by-type media);
    status / index / OCR shown as tooltipped pictograms; "Form with
    Icons" inputs; **dark theme** with a navbar toggle; drag-and-drop
    upload zone.
  - **7j** document page: breadcrumb removed; a dedicated "Наборы" card
    (add / remove sets in place, any file type, always a "new set"
    option); tag input with the domain's frequent-tag chips.
  - **7k** bulk actions on selected search results — index / OCR / add to
    a set; the selection persists in `localStorage` across pages and
    resets on a filter change. Action feedback is a bottom-right toast.
  - **7l** HTML error pages (`error.html`) for the web UI instead of raw
    JSON; `/api` keeps JSON, htmx errors become a toast.
  - **7n** upload progress bar (htmx `xhr:progress`) + disabled submit
    while a file is uploading; `_upload_result.html` HX partial.
  - **7o** pluggable blob storage (`app/storage/`, `ObjectStore` ABC):
    `STORAGE_BLOBS=local` (default) or `s3` — any S3-compatible
    endpoint (local MinIO via `docker compose --profile s3 up -d`, a
    remote Garage/MinIO host later — only `S3_ENDPOINT` changes).
    Derived/artifact caches stay local. `python -m app.storage.migrate`
    moves existing blobs between backends.
  - **7p** document sets are now **user-owned** (§15 rev 4): a set is
    *N saved search filters + explicit adds*, resolved live. Share
    links expose only `is_public` documents (`default_document_visibility`
    per domain, overridable per-doc / in bulk); the owner gets a
    private «Полная выгрузка» of everything they can reach. Routes
    moved to `/sets/*`; migration 0010.
- **8 email confirmation** (opt-in) — set `SMTP_HOST` to require a
  verified address before a new account can log in (migration 0009,
  `app/services/email.py`, `GET /verify/{token}`); unset keeps sign-up
  instant.

## Stack

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 (async) · Alembic ·
SAQ (Postgres-backed jobs, no Redis) · aiogram 3 (bot, long-polling) ·
Pillow (thumbnails) · Tabler / Bootstrap 5 (web UI) · Caddy · Docker Compose.

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
