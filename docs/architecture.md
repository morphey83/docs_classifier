# DocsClassifier — architecture & requirements

Status: **draft, requirements-gathering.** No code yet.
Last updated: 2026-09-01.

---

## 1. What it is

A self-hosted, multi-user **document store with manual tagging, a processing
queue, and faceted search**. "Classification" here means *tags* — there is no ML
classifier. Documents also carry static characteristics (type, name, dates,
size) that are searchable alongside tags.

Hosted on the same VDS as the Telegram reminder bot. Same conventions: Python
3.12, PostgreSQL 16, SQLAlchemy 2 (async), Docker Compose, `pydantic-settings`,
Russian-facing.

Two clients over one shared service layer:

1. **Web API + UI** (primary) — FastAPI.
2. **Telegram bot** (aiogram 3) — mirrors most of the UI: upload, incremental
   tagging, search, export.

---

## 2. Core concepts

### 2.1 Domain (workspace)

A **domain** is a shareable container that owns documents, its own tag
vocabulary, and its own inbox. Every document belongs to exactly one domain.

- On registration a user gets a personal domain (`"<name>'s space"`).
- A user can create additional domains and **invite** other users into them
  with a chosen access level.
- All document/tag/search/export operations are scoped to a domain and gated by
  the caller's rights in that domain.

### 2.2 Users & access levels

Underlying capabilities in a domain:

| capability | meaning |
|---|---|
| `view` | see documents & metadata, search, open/preview |
| `download` | download originals, run exports |
| `write` | upload, process the inbox, edit tags & metadata, request OCR/indexing |
| `manage` | manage members & invites, manage the tag vocabulary, domain settings |
| `delete` | soft-delete / restore / hard-delete documents |
| `own` | delete the domain, transfer ownership |

Bundled into roles (open question — see §12):

| role | capabilities |
|---|---|
| `owner` | all |
| `admin` | view, download, write, manage, delete |
| `editor` | view, download, write |
| `tagger` | view, download, write *(inbox + tags only; upload allowed, hard-delete not)* |
| `viewer` | view, download |
| `scanner` | view + request OCR/indexing only *(for outsourced digitisation)* |

A user may be a member of many domains with a different role in each. The bot
acts entirely as the linked user, with that user's per-domain rights.

### 2.3 Document

| field | notes |
|---|---|
| `id` | uuid |
| `domain_id` | owning domain |
| `sha256` | content hash; drives dedup & blob storage path |
| `storage_key` | path under `DATA_DIR/blobs/…` |
| `original_name` | as uploaded |
| `title` | editable display name, defaults to `original_name` |
| `mime`, `ext` | detected (python-magic + extension) |
| `size_bytes` | |
| `doc_date` | **document's own creation date** — extracted from file metadata (PDF info, Office core props, image EXIF) on ingest, then user-editable |
| `uploaded_at`, `uploaded_by` | |
| `source` | `upload` \| `archive` \| `bot` |
| `upload_batch_id` | nullable, links to the batch |
| `status` | `inbox` → `tagged` → `archived` |
| `extracted_text` | plain text for search; nullable |
| `text_source` | `none` \| `parsed` \| `ocr` |
| `ocr_status` | `none` \| `pending` \| `done` \| `failed` \| `unsupported` |
| `ocr_at` | timestamp when OCR completed — **the "digitised" label** |
| `index_status` | `none` \| `pending` \| `done` \| `failed` |
| `indexed_at` | timestamp when pushed to the advanced index — **the "indexed" label** |
| `notes` | free text |
| `deleted_at` | soft delete |

`ocr_at` / `indexed_at` are first-class, filterable fields (see §7), surfaced in
the UI as badges and (optionally) mirrored as reserved system tags.

### 2.4 Tag

Per-domain vocabulary.

`id, domain_id, name, slug, color, description, parent_id (optional hierarchy),
created_at, created_by, usage_count`.

`document_tag`: `document_id, tag_id, assigned_at, assigned_by`.

### 2.5 Upload batch

Tracks a single upload action (a file or an archive) so the user can see
"archive `2024-Q3.7z` → 42 documents, 40 still in inbox".

`id, domain_id, uploaded_by, source_filename, kind (single|archive), item_count,
status (processing|done|partial), error, uploaded_at`.

---

## 3. Workflows

### 3.1 Ingest

1. `POST /domains/{d}/uploads` — one or more files, or an archive.
2. For a plain file: hash → store blob (dedup) → create `document` with
   `status=inbox` → enqueue **metadata + text extraction**.
3. For an archive: create `upload_batch(processing)` → enqueue **archive
   extraction** which, per entry, does the plain-file path above; nested
   archives extracted to a configurable depth; zip-bomb guards (max entries,
   max total uncompressed bytes); path-traversal safe.
4. Supported archives **from day one**: `zip`, `7z`, `rar`, plus `tar(.gz/.bz2/.xz)`.
   Unified via **libarchive** (`libarchive-tools` in the image) with `py7zr` /
   stdlib `zipfile` as fallbacks. `rar` needs `unar`/`unrar` in the image —
   licensing note in the deploy docs.

### 3.2 Inbox processing ("на обработку")

- `GET /domains/{d}/inbox/next` → the oldest `inbox` document not currently
  "deferred" by this user. Optional `?after=<id>` to page through.
- User assigns tags / edits `title`, `doc_date`, `notes`.
- `POST /documents/{id}/complete` → `status=tagged`, leaves the inbox.
- `POST /documents/{id}/defer` → pushed to the back of *this user's* queue view
  (per-user skip, not a global state) so multiple people can process in parallel.
- Bulk tagging: select N inbox docs → apply a tag set.

### 3.3 Search — see §7.

### 3.4 Export

- `POST /domains/{d}/exports` with either a filter (same params as search) or an
  explicit id list. Requires `download`.
- Small result → streamed zip response. Large → async job producing a
  time-limited download link.
- Zip contains the originals (name collisions de-duplicated) + `manifest.json`
  and `manifest.csv` with every document's metadata and tags.

### 3.5 OCR (optional, on demand)

- Manual: `POST /documents/{id}/ocr?lang=rus+eng`. Also a per-domain
  `auto_ocr` toggle that OCRs image files and image-only PDFs at ingest.
- Supported inputs: images (`png/jpg/jpeg/tiff/webp/bmp`), image-only PDFs.
  Text PDFs / Office docs are parsed directly and never OCR'd.
- Engine: **ocrmypdf** (→ tesseract) for PDFs — deskew, rotate, produces a
  searchable-PDF sidecar + text; **tesseract** directly for loose images.
  Image: `tesseract-ocr`, `tesseract-ocr-rus`, `tesseract-ocr-eng`,
  `ghostscript`.
- Runs on the **worker**, concurrency-limited. On success:
  `extracted_text` set, `text_source=ocr`, `ocr_status=done`, `ocr_at=now`;
  the PG FTS vector refreshes automatically; sidecar stored under
  `DATA_DIR/derived/<sha256>/ocr.pdf`.

### 3.6 Advanced full-text indexing (optional, on demand)

Two tiers:

- **Tier 1 — always on: PostgreSQL FTS.** A generated `tsvector`
  (`to_tsvector('russian', title || ' ' || extracted_text)`) with a GIN index,
  plus `pg_trgm` on `title` and `tag.name` for typo-tolerant prefix/substring.
  Gives Russian stemming/morphology and light fuzziness with zero extra infra.
  This is what search uses by default.
- **Tier 2 — opt-in per document: an external engine** for strong typo
  tolerance, phrase/proximity queries, ranked highlighting over large bodies of
  OCR text. `POST /documents/{id}/index`; per-domain `auto_index` toggle.
  `index_status`/`indexed_at` track it.
  Candidates (decide per VDS RAM — see §12):
  - **OpenSearch / Elasticsearch** — most capable (Russian analyzer, fuzzy,
    synonyms, highlight); ~1–2 GB RAM, JVM ops.
  - **Manticore Search** — light, has a Russian lemmatizer + fuzzy; SQL-ish.
  - **Meilisearch** — lightest, superb typo tolerance; Russian morphology is
    basic (stop-words, no full lemmatisation).
- The code talks to a `SearchBackend` interface (`index(doc)`, `search(query)`,
  `delete(doc)`), so Tier 2 is pluggable and the choice is deferrable.

---

## 4. Component / deployment view

```
                    ┌─────────── reverse proxy (Caddy / existing) ── TLS
                    │
        ┌───────────▼──────────┐        ┌──────────────────┐
        │  web  (FastAPI)      │        │  bot (aiogram 3) │
        │  REST + UI           │        │  linked-user     │
        └───────┬──────────────┘        └────────┬─────────┘
                │      shared  app/services/*     │
                └──────────────┬──────────────────┘
                               │
             ┌─────────────────┼───────────────────────┐
             │                 │                       │
     ┌───────▼──────┐   ┌──────▼───────┐        ┌──────▼──────────┐
     │ PostgreSQL 16│   │ worker (SAQ) │        │ DATA_DIR volume │
     │ metadata,    │   │ extract,     │        │ blobs/ derived/ │
     │ FTS, queue*  │   │ OCR, index,  │        └─────────────────┘
     │              │   │ export       │
     └──────────────┘   └──────┬───────┘
                               │ (Tier 2 only)
                        ┌──────▼───────────────────┐
                        │ search engine (optional) │
                        │ OpenSearch / Manticore / │
                        │ Meilisearch              │
                        └──────────────────────────┘
```

Compose services: `db`, `web`, `worker`, `bot`, `caddy` (optional),
`search` (optional), `redis` (only if the queue backend is switched).
Volumes: `pgdata`, `docdata`.

`* queue`: **SAQ on Postgres** is the default job queue — no Redis. Jobs:
archive extraction, metadata+text parsing, OCR, indexing, export builds.
If Tier 2 brings in more infra anyway, Redis + `arq` is an easy swap.

---

## 5. Data model (entities)

```
user(id, email, username, password_hash, tg_id?, is_active, created_at)
api_key(id, user_id, name, hash, created_at, last_used_at, revoked_at)
session(id, user_id, created_at, expires_at, user_agent, ip)          # server-side

domain(id, name, slug, owner_id, description, settings_jsonb, created_at)
  settings: auto_ocr, auto_index, default_ocr_lang, max_upload_mb,
            storage_quota_mb, retention_days?
domain_member(domain_id, user_id, role, added_by, added_at)
domain_invite(id, domain_id, email|username, role, token, created_by,
              expires_at, accepted_at)

document(… see §2.3 …)
tag(id, domain_id, name, slug, color, description, parent_id?, created_by, created_at)
document_tag(document_id, tag_id, assigned_by, assigned_at)

upload_batch(id, domain_id, uploaded_by, source_filename, kind, item_count,
             status, error, uploaded_at)

export_job(id, domain_id, requested_by, filter_jsonb, status, artifact_key?,
           item_count, expires_at, created_at)

audit_log(id, domain_id?, actor_id, action, target_type, target_id,
          detail_jsonb, at)
job(…)                                    # SAQ table(s)
```

Blob storage: `DATA_DIR/blobs/<h[0:2]>/<h[2:4]>/<h>` (content-addressed,
dedup). Derived files: `DATA_DIR/derived/<h>/…`.

---

## 6. API surface (sketch)

```
POST   /auth/register            POST /auth/login    POST /auth/logout
GET    /auth/me
POST   /auth/api-keys            DELETE /auth/api-keys/{id}
POST   /auth/tg-link             # returns one-time code for the bot

GET    /domains                  POST /domains
GET    /domains/{d}              PATCH /domains/{d}      DELETE /domains/{d}
GET    /domains/{d}/members      POST /domains/{d}/invites
PATCH  /domains/{d}/members/{u}  DELETE /domains/{d}/members/{u}
POST   /invites/{token}/accept

POST   /domains/{d}/uploads              # file(s) or archive  -> batch/docs
GET    /domains/{d}/uploads/{batch}
GET    /domains/{d}/documents            # faceted search (§7)
GET    /documents/{id}                   PATCH /documents/{id}
GET    /documents/{id}/content           # download original (+ range)
GET    /documents/{id}/preview           # thumbnail / first page
DELETE /documents/{id}                   POST /documents/{id}/restore
PATCH  /documents/{id}/tags              # set/add/remove
POST   /documents/{id}/complete          POST /documents/{id}/defer
POST   /documents/{id}/ocr               POST /documents/{id}/index

GET    /domains/{d}/inbox/next           GET /domains/{d}/inbox        # count/list
GET    /domains/{d}/tags                 POST /domains/{d}/tags
PATCH  /domains/{d}/tags/{id}            DELETE /domains/{d}/tags/{id}   # merge?

POST   /domains/{d}/exports              GET /exports/{id}   GET /exports/{id}/download
```

All list endpoints return facet aggregates (tag histogram, type histogram,
counts by status / has_ocr / has_index) to drive filter UIs.

---

## 7. Search & filtering

`GET /domains/{d}/documents?…`

| param | matches |
|---|---|
| `q` | full-text over `title` + `extracted_text`. Tier-1 (PG FTS) by default; `&mode=advanced` routes to Tier-2 for docs that have `index_status=done` |
| `tags_all` | has **every** listed tag (slugs, comma-sep) |
| `tags_any` | has **any** listed tag |
| `tags_none` | has **none** of the listed tags |
| `type` / `ext` / `mime` | document type |
| `size_min`, `size_max` | bytes |
| `doc_date_from`, `doc_date_to` | the document's own date |
| `uploaded_from`, `uploaded_to`, `uploaded_by` | ingest metadata |
| `status` | `inbox` \| `tagged` \| `archived` |
| `has_ocr`, `has_index` | boolean presence of `ocr_at` / `indexed_at` |
| `ocr_from`, `ocr_to`, `indexed_from`, `indexed_to` | processing-label dates |
| `text_source` | `parsed` \| `ocr` \| `none` |
| `sort` | `relevance` \| `doc_date` \| `uploaded_at` \| `size` \| `title` |
| `page`, `page_size` | |

Saved searches (per user, per domain) back the "выдать нужный набор" export and
the bot's `/find` shortcuts.

---

## 8. Telegram bot (aiogram 3)

Separate container, shares DB + `app/services/*`. Acts as the linked user.

- `/start` → link account with the one-time code from the web UI.
- `/domain` → pick the "current" domain (inline list of the user's memberships).
- **Upload**: send a document / photo / archive → ingested into the current
  domain's inbox. (Bot API download cap ≈ 20 MB — larger files via web, or run a
  self-hosted Bot API server; open question §12.)
- **Process inbox**: `/inbox` → bot shows the next document (preview + metadata)
  with an inline keyboard: frequent tags as buttons, "＋ new tag" (free text
  reply), "skip", "done". Loops until the inbox is empty.
- **Search**: `/find договор #контрагент type:pdf 2024` → paginated results,
  tap a result to receive the file.
- **Export**: pick a saved search or the last `/find` → bot sends the zip (or a
  link if large).

---

## 9. Background jobs (worker)

| job | trigger |
|---|---|
| `extract_archive` | archive upload |
| `ingest_file` | plain upload / each archive entry — hash, store, metadata, `doc_date`, quick text parse |
| `parse_text` | pdf (pymupdf), docx (python-docx), xlsx (openpyxl), pptx, txt/csv/md, html, eml/msg |
| `ocr_document` | manual request or `auto_ocr` |
| `index_document` | manual request or `auto_index` |
| `build_export` | large export request |
| `cleanup` | expired exports, purge past retention, orphan blobs (refcount = 0) |

Queue: **SAQ / Postgres** (default). Worker image carries the OCR + archive
native deps.

---

## 10. Security & ops

- Passwords: **argon2**. Server-side sessions (cookie) for the UI; API keys for
  scripts; bot via account linking.
- Every request authorises against `domain_member.role` → capability.
- Uploads: size cap, MIME sniffing, archive bomb guards, path-traversal safe
  extraction, per-domain storage quota.
- Blobs are opaque; original names only in DB. Optional at-rest encryption of
  blobs — open question §12.
- `audit_log` for uploads, deletes, member changes, exports.
- Backups: `pg_dump` + `tar` of `DATA_DIR`. Both on one volume set.
- Rate limiting on auth + upload endpoints.

---

## 11. Build phases (when we start coding)

| phase | scope |
|---|---|
| **0 skeleton** | repo layout, config, async DB, Alembic, Docker Compose (`db`+`web`+`worker`), auth (register/login/session), `/health` |
| **1 core** | domains, members, invites, capability checks; single-file upload; document CRUD + storage; metadata & `doc_date` extraction; inbox queue; tags CRUD + assignment |
| **2 ingest + search** | archive upload & extraction (zip/7z/rar/tar); upload batches; text parsing → PG FTS; faceted search + facet counts; export (zip + manifest) |
| **3 OCR** | OCR worker (ocrmypdf/tesseract); manual + `auto_ocr`; `ocr_*` fields & filters; sidecars |
| **4 advanced index** | `SearchBackend` interface; wire the chosen engine; per-doc opt-in; `index_*` fields & filters |
| **5 bot** | aiogram bot: linking, upload, inbox processing, search, export |
| **6 web UI** | *separate design discussion* — server-rendered (HTMX) vs SPA |

Cross-cutting throughout: tests, audit log, quotas.

---

## 12. Open questions

1. **Roles vs free permissions** — are the 6 preset roles (§2.2) enough, or do
   you want per-capability checkboxes assignable individually per member?
2. **Tag hierarchy** — flat tags, or parent/child (e.g. `Контрагенты/ООО Ромашка`)?
3. **Tag scope** — per-domain vocabulary (assumed) confirmed? Any need for
   cross-domain shared tag sets?
4. **Tier-2 search engine** — decide now, or default to PG FTS and revisit when
   it's not enough? How much free RAM on the VDS?
5. **Queue backend** — SAQ-on-Postgres (no Redis) acceptable?
6. **Reverse proxy** — is there already one on the VDS, or should compose ship
   Caddy? What hostname / subdomain?
7. **Quotas** — per-domain storage limit and max upload size — set defaults?
8. **Versioning** — re-uploading the same/updated file: keep versions, or just
   dedup and ignore?
9. **Retention / hard delete** — soft-delete + purge-after-N-days? Who may
   hard-delete?
10. **At-rest encryption** of blobs — required?
11. **Telegram large files** — accept the ~20 MB bot cap, or self-host a Bot API
    server for bigger uploads?
12. **UI stack** — lightweight server-rendered (HTMX + Jinja, fastest to build,
    pairs with FastAPI) vs a JS SPA (React/Svelte) — for the separate UI round.
