# DocsClassifier — architecture & requirements

Status: **design complete — all decisions locked.** No code yet.
Last updated: 2026-09-01 (rev 3 — sets & share links, Caddy).

---

## 0. Decisions locked (2026-09-01)

| # | decision |
|---|---|
| Roles | The 6 preset roles (§2.2) — **no** per-capability free assignment. |
| Tags | **Flat.** No hierarchy, no `parent_id`. Per-domain vocabulary. |
| VDS | **~2 GB RAM total.** This rules out Elasticsearch / OpenSearch. |
| Search | **Postgres-only, no external engine.** Tier 1 (`to_tsvector('russian')` + `pg_trgm`) is the whole search story. The `SearchBackend` interface stays so a small engine (Manticore) *could* be added if the VDS grows — not now. See §7. |
| "Indexing" action | Body-text search is **opt-in per document**: `extracted_text` is always stored, but a doc's `search_tsv` is only populated on an explicit *index* request (or `auto_index`). Keeps the GIN index small on a small box. `indexed_at` is the label. |
| Queue | **SAQ on Postgres.** No Redis (every MB counts at 2 GB). |
| Quotas | Global defaults in `.env` (hard caps) + per-domain overrides in `domain.settings`, owner/admin-editable, never above the global cap. See §10. |
| Dedup / replace | **Idempotent by content hash.** Same hash → ignored. Same name + different hash → 409, client chooses *replace* (new version kept) or *separate document*. See §13. |
| Trash | Soft-delete → "Корзина", kept **30 days** (`trash_retention_days`, per-domain). Owner can force-purge now. Re-uploading a file matching a trashed doc's **name + hash restores it with its tags**. See §14. |
| Telegram large files | Accept the ~20 MB Bot API cap. Bigger uploads via web only. No self-hosted Bot API server. |
| UI | **HTMX + Jinja**, server-rendered, same FastAPI app. (Details in a later round.) |
| At-rest encryption | Not in scope. |
| Reverse proxy | **No proxy / no domain on the VDS yet** → compose ships its own **Caddy**. Interim: `tls internal` (self-signed) for IP access; once a (sub)domain points at the VDS, one Caddyfile line switches to real Let's Encrypt TLS. A free `*.duckdns.org` name + Caddy DNS-01 also works. |
| Document sets | Persistent hand-curated collections → build an immutable archive **artifact** → download, or share via a **permanent / one-time link** (`GET /d/{token}`). See §15. |

All open questions resolved. Design phase complete once this rev is acked.

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

Bundled into **6 fixed roles** (locked — no free per-capability assignment):

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

Per-domain vocabulary. **Flat** — no hierarchy.

`id, domain_id, name, slug, color, description, created_at, created_by,
usage_count`.

`document_tag`: `document_id, tag_id, assigned_at, assigned_by`.

Tag admin (`manage`): create, rename, recolour, delete (detaches from docs),
**merge** two tags into one.

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

### 3.4 Export & sets

Two ways to get files out, both producing an **artifact** (a built zip on disk,
§15):

- **Ad-hoc export** — `POST /domains/{d}/exports` with a filter or an id list.
  One-shot; the artifact expires after `EXPORT_TTL_HOURS`.
- **Document set** — a persistent, hand-curated collection the user fills over
  many sessions ("добавить в набор"), then archives on demand. See **§15**.

Every artifact zip contains the originals (name collisions de-duplicated) +
`manifest.json` and `manifest.csv` (metadata + tags per document); trashed /
purged documents are skipped and listed in the manifest. Download directly
(authed) or via a **share link** — permanent or one-time (§15).
Requires `download`.

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

### 3.6 Full-text search & the "index" action

**One engine: PostgreSQL.** (2 GB VDS — an external search service is not
affordable; see §7 for the reasoning and the growth path.)

- Metadata / tag / characteristic search is always available for every document.
- **Body-text search is opt-in per document.** `extracted_text` is always stored
  after parsing/OCR, but the searchable vector `search_tsv`
  (`to_tsvector('russian', title || ' ' || extracted_text)`, GIN-indexed) is
  only built when the user calls `POST /documents/{id}/index` — or the domain has
  `auto_index=true`. This keeps the GIN index small on a small box and lets the
  user index only what matters.
- `index_status` (`none|pending|done|failed`) and `indexed_at` track it;
  `indexed_at` is the filterable "indexed" label. Re-runs after OCR adds text.
- `pg_trgm` indexes on `title` and `tag.name` give typo-tolerant matching for
  those (always on, cheap).
- A `SearchBackend` abstraction wraps query building so a small external engine
  (**Manticore**, if ever) can be slotted in without touching call sites — but
  that is explicitly **out of scope** unless the VDS is upgraded.

---

## 4. Component / deployment view

```
                    ┌─────────── Caddy (auto-TLS; self-signed until a domain)
                    │
        ┌───────────▼──────────┐        ┌──────────────────┐
        │  web  (FastAPI+HTMX) │        │  bot (aiogram 3) │
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
     │ FTS (tsvector│   │ OCR (conc.1),│        │ exports/        │
     │ + pg_trgm),  │   │ index,       │        └─────────────────┘
     │ SAQ queue    │   │ export,      │
     │              │   │ cleanup      │
     └──────────────┘   └──────────────┘
```

Compose services: **`db`, `web`, `worker`, `bot`, `caddy`**.
**No Redis, no separate search service.**
Volumes: `pgdata`, `docdata`, `caddydata` (certs).

---

## 5. Data model (entities)

```
user(id, email, username, password_hash, tg_id?, is_active, created_at)
api_key(id, user_id, name, hash, created_at, last_used_at, revoked_at)
session(id, user_id, created_at, expires_at, user_agent, ip)          # server-side

domain(id, name, slug, owner_id, description, settings_jsonb, created_at)
  settings: auto_ocr, auto_index, default_ocr_lang,
            max_upload_mb, storage_quota_mb, trash_retention_days,
            archive_on_conflict (skip|new)
domain_member(domain_id, user_id, role, added_by, added_at)
domain_invite(id, domain_id, email|username, role, token, created_by,
              expires_at, accepted_at)

document(… see §2.3 …; + version int, deleted_by)
document_version(id, document_id, version_no, sha256, size_bytes, doc_date,
                 replaced_by, replaced_at)          # one row per explicit replace
tag(id, domain_id, name, slug, color, description, created_by, created_at)
document_tag(document_id, tag_id, assigned_by, assigned_at)

upload_batch(id, domain_id, uploaded_by, source_filename, kind, item_count,
             conflict_count, status, error, uploaded_at)

document_set(id, domain_id, name, description, visibility (private|domain),
             created_by, item_count, created_at, updated_at)
document_set_item(set_id, document_id, added_by, added_at, position)   # uniq(set,doc)

artifact(id, domain_id, kind (adhoc_export|set_archive), source_id,
         format (zip), status (building|ready|failed), storage_key?, size_bytes,
         item_count, missing_count, snapshot_jsonb, requested_by, created_at,
         expires_at?)                       # replaces export_job
download_link(id, artifact_id, token, max_downloads?, download_count,
              expires_at?, revoked_at?, created_by, created_at, last_downloaded_at)

audit_log(id, domain_id?, actor_id, action, target_type, target_id,
          detail_jsonb, at)
job(…)                                    # SAQ table(s)
```

Uniqueness: `document (domain_id, sha256)` where `deleted_at IS NULL` — the
dedup guard (§13).

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
PATCH  /domains/{d}/tags/{id}            DELETE /domains/{d}/tags/{id}
POST   /domains/{d}/tags/{id}/merge      # into another tag

# --- document sets (§15) ---
GET    /domains/{d}/sets                 POST /domains/{d}/sets
GET    /domains/{d}/sets/{s}             PATCH /domains/{d}/sets/{s}   DELETE …
POST   /domains/{d}/sets/{s}/items       # body: {document_ids:[…]} — idempotent add
DELETE /domains/{d}/sets/{s}/items/{doc}
POST   /domains/{d}/sets/{s}/archives    # -> artifact (build job)

# --- artifacts & links (§15) ---
GET    /artifacts/{id}                   GET /artifacts/{id}/download   # authed
POST   /artifacts/{id}/links             # {kind: permanent|one_time, expires_at?}
DELETE /links/{id}                       # revoke
GET    /d/{token}                        # PUBLIC download, no auth

POST   /domains/{d}/exports              # ad-hoc: filter/id-list -> artifact
```

All list endpoints return facet aggregates (tag histogram, type histogram,
counts by status / has_ocr / has_index) to drive filter UIs.

---

## 7. Search & filtering

### Why Postgres-only

On ~2 GB RAM the budget is roughly: Postgres ~256–400 MB, `web` ~150 MB,
`worker` ~150 MB idle but **300–600 MB during an OCR page**, `bot` ~80 MB, OS
~150 MB. That is already near the ceiling. Elasticsearch/OpenSearch want
1–2 GB on their own → excluded.

Postgres covers the requirement well:

- `to_tsvector('russian', …)` applies **Snowball Russian stemming** —
  `договор / договора / договоров / договору` all match `договор`. That is the
  "морфология" ask, for free, in the DB we already run.
- `pg_trgm` (`similarity`, `word_similarity`, `<->`) gives **typo tolerance** on
  titles and tag names, and can be OR-combined with the `tsquery` for fuzzy body
  matches.
- GIN index on `search_tsv`; `ts_rank_cd` for relevance ordering; `ts_headline`
  for snippets.

Growth path (only if the VDS is upgraded): add **Manticore Search** behind the
`SearchBackend` interface — small footprint, real Russian lemmatiser, native
fuzzy. Not Elasticsearch. A hunspell Russian dictionary + the `RUM` index in
Postgres is an intermediate upgrade that needs no new container.

### Query parameters

`GET /domains/{d}/documents?…`

| param | matches |
|---|---|
| `q` | full-text over `title` + (for indexed docs) `extracted_text`, via `search_tsv` + `pg_trgm` fuzzy fallback |
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
  domain's inbox. Bot API download cap ≈ 20 MB — the bot tells the user to use
  the web UI for anything larger (accepted limitation, no self-hosted Bot API
  server).
- **Process inbox**: `/inbox` → bot shows the next document (preview + metadata)
  with an inline keyboard: frequent tags as buttons, "＋ new tag" (free text
  reply), "skip", "done". Loops until the inbox is empty.
- **Search**: `/find договор #контрагент type:pdf 2024` → paginated results,
  each with a "➕ в набор" button; tap a result to receive the file.
- **Sets**: `/sets` → list; open one → "📦 сформировать архив" → bot sends the
  zip, or a share link if it is large / the user asks for one.
- **Export**: the last `/find` → "📦 архив" → same as above.

---

## 9. Background jobs (worker)

| job | trigger |
|---|---|
| `extract_archive` | archive upload |
| `ingest_file` | plain upload / each archive entry — hash, store, metadata, `doc_date`, quick text parse |
| `parse_text` | pdf (pymupdf), docx (python-docx), xlsx (openpyxl), pptx, txt/csv/md, html, eml/msg |
| `ocr_document` | manual request or `auto_ocr` |
| `index_document` | manual request or `auto_index` |
| `build_artifact` | ad-hoc export **or** set-archive request — zips a snapshot + manifest |
| `cleanup` | expired artifacts (unless pinned by a live permanent link), trash past retention, orphan blobs (refcount = 0) |

Queue: **SAQ on Postgres** — no Redis. Worker image carries the OCR + archive
native deps.

### OCR on a 2 GB box

The one memory risk. Mitigations, all in the worker:

- OCR concurrency = **1** (a dedicated SAQ queue with `concurrency=1`).
- `OMP_THREAD_LIMIT=1`, `ocrmypdf --jobs 1` — caps tesseract RAM & CPU.
- Downscale images to ≤ 2500 px long edge before OCR.
- PDFs processed **page by page**, not whole-document.
- Recommend a **2 GB swap file** on the VDS as a safety net.
- If a job still OOM-kills: mark `ocr_status=failed`, the user retries later.

---

## 10. Quotas & limits

Global hard caps in `.env` (a per-domain setting can never exceed these):

| env | default | meaning |
|---|---|---|
| `MAX_UPLOAD_MB` | 200 | single file or archive |
| `MAX_ARCHIVE_ENTRIES` | 2000 | files per archive |
| `MAX_ARCHIVE_UNPACKED_MB` | 2000 | total uncompressed size (zip-bomb guard) |
| `MAX_ARCHIVE_DEPTH` | 2 | nested-archive recursion |
| `DEFAULT_DOMAIN_QUOTA_MB` | 5000 | storage per domain |
| `DEFAULT_TRASH_RETENTION_DAYS` | 30 | |
| `EXPORT_TTL_HOURS` | 48 | async export artifact lifetime |

Per-domain overrides (`domain.settings`, editable by `owner`/`admin`, clamped to
the global caps): `storage_quota_mb`, `max_upload_mb`, `trash_retention_days`,
`auto_ocr`, `auto_index`, `default_ocr_lang`, `archive_on_conflict`.

Enforcement: upload rejected with `413` when
`used_bytes + incoming > storage_quota`; for archives the *projected* unpacked
size is checked first. `used_bytes` = sum of `size_bytes` of a domain's
non-deleted documents + its trash + its export artifacts.

---

## 11. Security & ops

- Passwords: **argon2**. Server-side sessions (cookie) for the UI; API keys for
  scripts; bot via account linking.
- Every request authorises against `domain_member.role` → capability.
- Uploads: size cap, MIME sniffing, archive-bomb guards, path-traversal-safe
  extraction, per-domain storage quota.
- Blobs are opaque; original names only in DB. **No at-rest encryption** (out of
  scope).
- `audit_log` for uploads, deletes, replaces, member/role changes, exports,
  set-archive builds, **share-link create / revoke**, **public downloads**,
  trash purges.
- Public share links (`GET /d/{token}`): 192-bit token, IP rate-limited,
  revocable, optional expiry; `allow_public_links` domain kill-switch.
- Backups: `pg_dump` + `tar` of `DATA_DIR`, one volume set.
- Rate limiting on auth, upload, and `/d/{token}` endpoints.
- **TLS**: Caddy. Self-signed (`tls internal`) until a domain is pointed at the
  VDS; automatic Let's Encrypt after.

---

## 12. Build phases (when we start coding)

| phase | scope |
|---|---|
| **0 skeleton** | repo layout, config, async DB, Alembic, Docker Compose (`db`+`web`+`worker`), auth (register/login/session), `/health` |
| **1 core** | domains, members, invites, 6 roles → capability checks; single-file upload with dedup/replace (§13); document CRUD + content-addressed storage; metadata & `doc_date` extraction; inbox queue; flat tags CRUD + assignment |
| **2 ingest + search** | archive upload & extraction (zip/7z/rar/tar via libarchive); upload batches + conflict report; text parsing; opt-in `index` → `search_tsv`; faceted search + facet counts + `pg_trgm` fuzzy; export (zip + manifest) |
| **3 OCR** | OCR worker (ocrmypdf/tesseract, concurrency 1); manual + `auto_ocr`; `ocr_*` fields & filters; searchable-PDF sidecars |
| **4 sets & sharing** | document sets (§15); `build_artifact` job; ad-hoc export as artifact; `download_link` + public `GET /d/{token}` (permanent / one-time); artifact TTL + link pinning |
| **5 trash & lifecycle** | soft-delete → Корзина; 30-day auto-purge (`cleanup` job); owner force-purge; restore-on-reupload (§14); `document_version` on replace; blob refcount GC |
| **6 bot** | aiogram bot: account linking, upload, inbox processing, `/find`, sets, export |
| **7 web UI** | HTMX + Jinja — *separate design round* |

Cross-cutting throughout: tests, audit log, quota enforcement.

---

## 13. Dedup, replace, idempotency

Upload of file `F` (hash `H`, name `N`) into domain `D`:

1. **Non-deleted document in `D` with hash `H`** → **ignore**, return that
   document, `200 {deduplicated: true}`. Fully idempotent.
2. **Trashed document in `D` with hash `H` and the same `original_name`** →
   **restore** it (clear `deleted_at`, keep all tag assignments & metadata),
   `200 {restored: true}`. (§14)
3. **Non-deleted document in `D` with the same `original_name` but a different
   hash** → **`409 Conflict`** `{conflict: "name", existing_id}`. The client
   re-submits with:
   - `?on_conflict=replace` → the existing document now points at blob `H`; the
     previous `(sha256, size, doc_date, uploaded_at)` is snapshotted into
     `document_version`, `document.version += 1`; **tags, title, notes,
     `ocr_*`/`index_*` are kept** (OCR/index re-run against the new content).
   - `?on_conflict=new` → a separate document, title auto-suffixed ` (2)`.
4. Otherwise → create a new document.

Archive entries can't prompt: the batch carries `archive_on_conflict`
(`skip` default, or `new`); every conflict is counted in
`upload_batch.conflict_count` and listed on the batch page for manual handling.

---

## 15. Document sets & shareable archives

### Concept

A **document set** (`набор`) is a persistent, hand-curated list of documents
that a user fills incrementally: run a search → tick some results →
**"добавить в набор"** → pick an existing set or create a new one. Distinct from
a search filter (dynamic) and from an ad-hoc export (one-shot).

When the set is ready, the user **generates an archive** from it and either
downloads it directly or creates a **share link** — *permanent* or *one-time*.

### Model

- `document_set` — `domain_id`, `name`, `description`, `visibility`
  (`private` to creator, or `domain` = visible to all members),
  `created_by`, `item_count`.
- `document_set_item` — `(set_id, document_id)` unique (idempotent add),
  `added_by`, `added_at`, `position` (manual reorder). If the document is later
  trashed the item stays but is flagged stale; on blob purge it is removed.
- `artifact` — a built zip on disk (`kind = set_archive`, `source_id = set_id`;
  or `kind = adhoc_export`). Holds a **snapshot** (`snapshot_jsonb`: the exact
  document ids + versions at build time) so the file is immutable even if the
  set changes afterwards. `status`, `size_bytes`, `item_count`, `missing_count`,
  `expires_at`.
- `download_link` — `artifact_id`, opaque `token` (≈192-bit, URL-safe),
  `max_downloads` (`1` = one-time, `NULL` = unlimited), `expires_at` (nullable),
  `download_count`, `revoked_at`, `last_downloaded_at`, `created_by`.

### Workflow

1. Search results carry checkboxes → **"добавить в набор"** → dialog lists the
   user's open sets (+ editable `domain`-visible sets) or **"＋ создать набор"**.
2. Duplicates are ignored. Toast: *добавлено N в «набор X»*.
3. **Наборы** section: each set with its count; open a set to review, reorder,
   remove items, edit name/visibility.
4. In a set → **"Сформировать архив"** → enqueues `build_artifact` →
   `artifact(building → ready)`. The set keeps a history of its artifacts (by
   date); re-generating makes a new one.
5. On a ready artifact: **"Скачать"** (authed) and **"Создать ссылку"** →
   choose *постоянная* (`max_downloads=NULL`, `expires_at=NULL`) /
   *одноразовая* (`max_downloads=1`, optional expiry) / *с истечением* (custom
   `expires_at`).
6. Links are managed on the set: list, copy, see download count, **revoke**.

### Public download — `GET /d/{token}`

No auth. Checks, in order: link not `revoked`, not past `expires_at`,
`download_count < max_downloads` (or unlimited); artifact `ready` and not
expired. Then streams the file (`Content-Disposition: attachment;
filename="<set> <date>.zip"`), increments `download_count`, sets
`last_downloaded_at`. IP rate-limited. A one-time link is dead after the first
completed download.

### Permissions

| action | capability |
|---|---|
| create / edit own set, add/remove items | `view` |
| edit a `domain`-visible set someone else made | `manage` (or be the creator) |
| generate an archive | `download` |
| create a **one-time** link | `download` |
| create a **permanent** link | `write` *(a standing public URL is a bigger commitment)* |
| revoke a link | link creator, or `manage` |

Domain setting `allow_public_links` (default `true`) — owner can switch off all
link creation. Every link create / revoke / public download is in `audit_log`.

### Lifecycle interplay

- An `artifact` normally expires after `EXPORT_TTL_HOURS` (ad-hoc) or a longer
  set-archive TTL, and the `cleanup` job deletes the zip.
- **A live, non-expiring `download_link` pins its artifact** — `cleanup` skips
  any artifact that still has an active permanent link. Revoke the link (or it
  expires) → the artifact becomes eligible for cleanup again.
- Deleting a set does **not** delete already-built artifacts or their links
  (they were snapshots); it just removes the curation list.

---

## 14. Trash & retention

- **Soft delete** (`delete` capability): `deleted_at` + `deleted_by` set. The
  document leaves search / inbox / exports and appears in the domain's
  **Корзина** view (`?include_trash=true` or `GET /domains/{d}/trash`).
- **Auto-purge**: the daily `cleanup` job hard-deletes documents where
  `deleted_at < now() - domain.trash_retention_days` (default 30, per-domain
  setting). Hard delete removes the `document` row and its `document_tag` /
  `document_version` rows, then decrements the blob refcount; a blob with
  refcount 0 and no `document_version` reference is removed from disk.
- **Force-purge**: `POST /domains/{d}/trash/purge` — **owner only** — hard-deletes
  the whole trash now.
- **Restore**: manual `POST /documents/{id}/restore`, or automatically when a
  re-uploaded file matches a trashed document by **name + hash** (§13 step 2) —
  tags come back with it.
- Storage quota (§10) counts trashed documents until they are purged.
