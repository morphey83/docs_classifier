# DocsClassifier — architecture & requirements

Status: **all phases (0–7) built.** The service is feature-complete.
Last updated: 2026-09-01 (rev 5 — API moved to /api, HTMX+Jinja web UI at /).

---

## 0. Decisions locked (2026-09-01)

| # | decision |
|---|---|
| Roles | The 6 preset roles (§2.2) — **no** per-capability free assignment. Caps: view / upload / write / download / process / manage / delete / own. |
| Tags | **Flat.** No hierarchy, no `parent_id`. Per-domain vocabulary. |
| VDS | **~2 GB RAM total.** This rules out Elasticsearch / OpenSearch. |
| Search | **Postgres-only, no external engine.** Tier 1 (`to_tsvector('russian')` + `pg_trgm`) is the whole search story. The `SearchBackend` interface stays so a small engine (Manticore) *could* be added if the VDS grows — not now. See §7. |
| "Indexing" action | Body-text search is **opt-in per document**: `extracted_text` is always stored, but a doc's `search_tsv` is only populated on an explicit *index* request (or `auto_index`). Keeps the GIN index small on a small box. `indexed_at` is the label. |
| Queue | **SAQ on Postgres.** No Redis (every MB counts at 2 GB). |
| Quotas | Global defaults in `.env` (hard caps) + per-domain overrides in `domain.settings`, owner/admin-editable, never above the global cap. See §10. |
| Dedup / replace | **Idempotent by content hash.** Same hash → ignored. Same name + different hash → 409, client chooses *replace* (new version kept) or *separate document*. See §13. |
| Trash | Soft-delete → "Корзина", kept **30 days** (`trash_retention_days`, per-domain). Owner can force-purge now. Re-uploading a file matching a trashed doc's **name + hash restores it with its tags**. See §14. |
| Telegram large files | Accept the ~20 MB Bot API cap. Bigger uploads via web only. No self-hosted Bot API server. |
| UI | **HTMX + Jinja on Tabler** (Bootstrap 5, vendored, no build), server-rendered, same FastAPI app. See phase 7h. |
| At-rest encryption | Not in scope. |
| Reverse proxy | **No proxy / no domain on the VDS yet** → compose ships its own **Caddy**. Interim: `tls internal` (self-signed) for IP access; once a (sub)domain points at the VDS, one Caddyfile line switches to real Let's Encrypt TLS. A free `*.duckdns.org` name + Caddy DNS-01 also works. |
| Document sets | **User-owned** collections defined as *N saved search filters + explicit include/exclude overrides*, resolved live against the owner's current access (§15 rev 4). The archive is a **cache of the set's current result** — rebuilt automatically when the result changes, file purged after `SET_ARCHIVE_TTL_DAYS` (global, default 7). **Permanent / one-time links** bind to the set's stable artifact and always serve current contents (rights re-checked each access). See §15. |
| Cross-domain search | The bot (and later a global web search) needs to search across every domain a user belongs to at once. `GET /documents` makes `domain_id` an optional filter rather than a path segment; `GET /domains/{d}/documents` is kept for domain-scoped browsing. Tag filters switch from slug- to **name**-matching (case-insensitive) so they compose across domains with different vocabularies. See §7. |
| Telegram account linking | **Bidirectional, always verified** — never a typed `@username`. Bot-initiated (`/start`) sends a link to a minimal linking page; web-initiated (profile) shows a bot deep-link (`t.me/<bot>?start=<token>`). One `tg_link_token`, single-use, 15 min TTL, either direction. See §8. |
| Allowed file types | Owner/admin can restrict a domain to a list of extensions (`domain.settings.allowed_types`, instance default `DEFAULT_ALLOWED_TYPES`). A disallowed direct upload is rejected (415); a disallowed archive entry is skipped and reported on the batch, not fatal. Applies to web and bot alike (one choke point, `ingest_upload`). See §3.1. |
| Public links & the bot | No domain yet, so every absolute link (share links, the bot's deep-links) is built from one setting, **`PUBLIC_BASE_URL`** — flipping bare-IP → real (sub)domain later touches config, not code. See §10. |

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

Underlying capabilities in a domain (`app/rbac.py`):

| capability | meaning |
|---|---|
| `view` | see documents & metadata, search, open/preview |
| `upload` | add new documents / archives |
| `write` | edit tags & metadata, process the inbox, **create tags** |
| `download` | download originals, run exports |
| `process` | request OCR / indexing |
| `manage` | members & invites, rename/delete/merge tags, domain settings |
| `delete` | soft-delete / restore / purge documents |
| `own` | delete the domain, transfer ownership |

Bundled into **6 fixed roles** (locked — no free per-capability assignment):

| role | capabilities |
|---|---|
| `owner` | all |
| `admin` | view, upload, write, download, process, manage, delete |
| `editor` | view, upload, write, download, process |
| `tagger` | view, write, download, process *(process the inbox & tag; no upload)* |
| `viewer` | view, download |
| `scanner` | view, process *(for outsourced digitisation)* |

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

#### Planned: linking a document into more than one domain

A document is uploaded into exactly one domain and that stays its **home
domain** (quota, ownership, trash, inbox all live there). A later phase adds
the ability to **link an existing document into additional domains** — a
`document_domain_link (document_id, domain_id, linked_at, linked_by)`
association alongside the unchanged `document.domain_id`.

Decided so far (2026-09-02):

- In a linked (non-home) domain a member gets **the same capabilities their
  role grants there** as if the document were local — view / download / edit
  tags & title / OCR / index. The only domain-specific action is *unlink*
  (removing the link, never the document).
- The document then appears in that domain's search, tag pickers, and sets.
- Open: whose tag vocabulary applies when tagging from a linked domain
  (home-only vs. union of all linked domains' vocabularies), quota accounting,
  and what a home-domain hard-delete does to outstanding links.

Not built yet — `document.domain_id` is still a single hard FK everywhere.

### 2.4 Tag

Per-domain vocabulary. **Flat** — no hierarchy.

`id, domain_id, name, slug, color, description, created_at, created_by,
usage_count`.

`document_tag`: `document_id, tag_id, assigned_at, assigned_by`.

Creating a tag needs `write` (the inbox workflow adds vocabulary on the fly).
Rename / recolour / delete (detaches from docs) / **merge** need `manage`.

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
5. **Allowed file types** — a domain's owner/admin can restrict which types are
   ever stored as documents, via `domain.settings.allowed_types` (a list of
   extensions, e.g. `["pdf", "docx", "png"]`; `null`/unset = unrestricted). New
   domains inherit the instance-wide `DEFAULT_ALLOWED_TYPES` (also `null` by
   default). The check applies uniformly wherever a document is created —
   direct upload *and* each entry unpacked from an archive — from one place:
   `ingest_upload()` raises `DisallowedType` after probing `mime`/`ext`.
   - Direct upload of a disallowed type → **415**, body names the type and the
     domain's allowed list.
   - An archive entry of a disallowed type is **skipped, not fatal**: recorded
     on its `upload_batch_item` as `outcome="skipped_type"` with a `note`
     explaining why, exactly like a name conflict is skipped today (§13). The
     batch's item list (`GET /domains/{d}/uploads/{batch}`) is how the caller
     — web or bot — learns which files were left out and why; the bot turns
     that into a short summary message after an archive upload.
   - This is content-addressed storage, so a rejected direct upload leaves an
     orphan blob; the existing `cleanup` orphan sweep (§9) reclaims it — no
     special-cased pre-check needed before the blob is written.

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
            archive_on_conflict (skip|new),
            allow_public_links, allowed_types (list[ext] | null)
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

document_set(id, owner_id, name, description, created_at, updated_at)   # user-owned, §15 rev 4
document_set_filter(id, set_id, position, filter_jsonb, description, created_at)
document_set_item(set_id, document_id, added_by, added_at, position)   # PK(set,doc) — explicit adds

artifact(id, domain_id?, kind (adhoc_export|set_archive), source_id, content_hash?,
         format (zip), status (building|ready|failed), storage_key?, size_bytes,
         item_count, missing_count, snapshot_jsonb, requested_by, created_at,
         expires_at?)                       # replaces export_job
download_link(id, artifact_id, token, max_downloads?, download_count,
              expires_at?, revoked_at?, created_by, created_at, last_downloaded_at)

audit_log(id, domain_id?, actor_id, action, target_type, target_id,
          detail_jsonb, at)
job(…)                                    # SAQ table(s)

tg_link_token(id, token, tg_id?, tg_username?, account_id?, created_at,
              expires_at, consumed_at)    # bridges a Telegram id <-> an account;
                                           # either tg_id or account_id is set at
                                           # creation, the other filled on consume
                                           # (§8). TTL 15 min, single-use.
```

Uniqueness: partial unique index `document (domain_id, sha256)` where
`deleted_at IS NULL` — the dedup guard (§13). Trashed rows are exempt, so
re-uploading content that is sitting in the trash under a different name still
ingests as a new document. (migration 0006)

Blob storage goes through a pluggable backend (`app/storage/`, `ObjectStore`
ABC — a flat `key -> bytes` store; content-addressing / dedup / the
`<h[0:2]>/<h[2:4]>/<h>` layout live in `blobs.py` above it):

| class | keys | backend | why |
|---|---|---|---|
| **blobs** (originals) | `ab/cd/<sha256>` | `LocalObjectStore` (`DATA_DIR/blobs`) or `S3ObjectStore` — `STORAGE_BLOBS` | durable; can live on a separate cheap-disk / S3 host |
| **derived** (thumbnails, OCR sidecars) | `ab/cd/<sha>/thumb.webp` | always local (`DATA_DIR/derived`) | regenerable cache; wants low latency |
| **artifacts** (export / set-archive zips) | `<id>.zip` | always local (`DATA_DIR/artifacts`) | regenerable cache |

`ObjectStore.open_local(key)` (via the async `storage.fetch_local` wrapper)
hands subprocess-bound consumers — OCR, thumbnails, text extraction, archive
ingest, the zip writer, the bot's file sender — a real filesystem path: the
stored file itself for the local backend, a temp copy (fetched in a worker
thread) for a remote one. Downloads use `app/downloads.py::blob_download`:
`FileResponse` locally, a presigned-URL redirect from S3 (`S3_PRESIGN`), or a
streamed proxy as the fallback.

**S3 backend.** `STORAGE_BLOBS=s3` + `S3_ENDPOINT` / `S3_BUCKET` /
`S3_ACCESS_KEY` / `S3_SECRET_KEY` points blobs at any S3-compatible service.
Local: `docker compose --profile s3 up -d` (MinIO + bucket setup). Remote
later: recommended **Garage** (or single-node MinIO) reachable over
**WireGuard** — only `S3_ENDPOINT` changes (set `S3_PUBLIC_ENDPOINT` too if
browsers can't reach the private address and you want presigned redirects).
Switch backends with `python -m app.storage.migrate --to s3 --commit` (copies
every blob across; idempotent; `--delete-source` after verification), then
flip `STORAGE_BLOBS`.

---

## 6. API surface (sketch)

The JSON API is served under **`/api`** (e.g. `POST /api/auth/login`); the
server-rendered web UI (§12 phase 7) owns the site root. Short public paths
— `/d/{token}` (share downloads), `/tg/link/*` (linking page), `/health` —
stay at the root. Paths below omit the `/api` prefix for brevity.

```
POST   /auth/register            POST /auth/login    POST /auth/logout
GET    /auth/me
POST   /auth/api-keys            DELETE /auth/api-keys/{id}
POST   /auth/tg-link             # web-initiated linking: {token, deep_link} (§8)
GET    /tg/link/{token}          # minimal standalone page: log in / register, confirm

GET    /domains                  POST /domains
GET    /domains/{d}              PATCH /domains/{d}      DELETE /domains/{d}
GET    /domains/{d}/members      POST /domains/{d}/invites
PATCH  /domains/{d}/members/{u}  DELETE /domains/{d}/members/{u}
POST   /invites/{token}/accept

POST   /domains/{d}/uploads              # file(s) or archive  -> batch/docs
GET    /domains/{d}/uploads/{batch}
GET    /domains/{d}/documents            # faceted search, one domain (§7)
GET    /documents                        # faceted search, cross-domain (§7)
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
GET    /tags                             # cross-domain tag-name options (§7)

# --- document sets (§15 rev 4 — user-owned, top-level) ---
GET    /sets                            POST /sets
GET    /sets/{s}                        PATCH /sets/{s}   DELETE /sets/{s}
POST   /sets/{s}/filters                # body: a serialized SearchFilters (+ domain_ids)
DELETE /sets/{s}/filters/{fid}
POST   /sets/{s}/items                  # body: {document_ids:[…]} — explicit adds
DELETE /sets/{s}/items/{doc}
GET    /sets/{s}/archive[/download]     # ensure-current -> 200 stream | 202 building
POST   /sets/{s}/export                 # «Полная выгрузка» -> adhoc_export artifact

# --- artifacts & links (§15) ---
GET    /artifacts/{id}                   GET /artifacts/{id}/download   # authed
POST   /sets/{s}/links                   # {kind: permanent|one_time, expires_at?} — owner only
DELETE /links/{id}                       # revoke — owner only
GET    /d/{token}                        # PUBLIC download, no auth

POST   /domains/{d}/exports              # ad-hoc: filter/id-list -> artifact
```

All list endpoints return facet aggregates (tag histogram, type histogram,
counts by status / has_ocr / has_index) to drive filter UIs.

---

## 7. Search & filtering

> **Rev 2 (2026-09-03).** Search is the one document surface. **Корзина** and
> **Очередь на сортировку** are search *presets*, not separate pages:
> `?preset=active` (default, `deleted_at IS NULL`), `?preset=inbox`
> (`status=inbox` + hide the docs this user deferred), `?preset=trash`
> (`deleted_at IS NOT NULL`). Results carry state-aware actions — a trashed row
> offers *restore* / *purge*, an inbox row opens the tagging modal. The
> card-by-card tagging flow (`/inbox/card`) launches from the inbox preset.
> `/inbox` and `/domains/{d}/trash` 307-redirect to the presets.
>
> **Tags are one global pool** — not owned by a domain. Created on use, matched
> by slug (`slugify`, case/space-insensitive → one tag per slug), shared across
> every domain. `GET /tags` (root page) renames / recolours / merges; anyone
> signed in. No manual delete: a tag lives while ≥1 document carries it and the
> nightly `cleanup` sweeps the rest. Frequent-tag suggestions =
> `suggest_tags(owner's domain ids)`.

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

### Cross-domain search

The search core is domain-**set**-based, not domain-single: `search_documents(db,
domain_ids, filters)`. Two routes share it:

- `GET /domains/{d}/documents` — the existing domain-scoped route; membership
  in `{d}` is required (`view`), `domain_ids = [d]`. Unchanged response shape,
  kept for a "documents in this domain" view.
- `GET /documents` — **the primary route for the bot (and eventually a
  cross-domain web search)**. No path-scoped domain. `domain_id` becomes an
  ordinary *optional* query filter: given → narrows to that one domain (the
  caller must be a member); omitted → searches every domain the caller belongs
  to. Each hit's `domain_id` (and, on this route, a resolved `domain_name`) is
  already part of `DocumentOut`, so results are self-describing without N+1
  lookups.

**Tags, either route:** `tags_all` / `tags_any` / `tags_none` now match by tag
**name**, case-insensitively — not by slug. A document only ever carries tags
from its own domain's vocabulary, so name-matching composes correctly across
domains without needing shared ids; it's also simply what a caller has on hand
(nobody types a slug). Slugs remain an internal, per-domain implementation
detail for `tag` CRUD and uniqueness.

**`GET /tags?domain_id=`** — the filter-picker's source of *available* tag
values: aggregates by (lowercased) name across the domain(s) searched
(one, if `domain_id` given; otherwise every domain the caller belongs to),
summing usage counts. Distinct from `GET /domains/{d}/tags` (unchanged),
which manages *that domain's* vocabulary (create/rename/merge — inherently
per-domain operations).

### Query parameters

| param | matches |
|---|---|
| `domain_id` | narrows to one domain; omitted on `GET /documents` = all of the caller's domains |
| `q` | full-text over `title` + (for indexed docs) `extracted_text`, via `search_tsv` + `pg_trgm` fuzzy fallback |
| `tags_all` | has **every** listed tag (names, comma-sep, case-insensitive) |
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

Facets (tag/type/status histograms) are computed over whatever domain set was
searched; the tag bucket groups by name the same way filtering does.

Saved searches (per user) back the "выдать нужный набор" export and the bot's
`/find` shortcuts (§8) — persisted so a bare `/find` repeats the last query.

---

## 8. Telegram bot (aiogram 3)

Separate container, **long-polling** (no public domain/cert yet — switch to a
webhook once Caddy has real TLS). Shares the DB + imports `app/services/*`
directly, in-process — the bot is not an HTTP client of its own API; only the
account-linking handshake touches a browser. A middleware resolves
`message.from_user.id` → `User` via `tg_id` on every update; no match →
"сначала привяжите аккаунт: /start".

### Account linking — bidirectional, always verified

One `tg_link_token` (§5), consumed once, TTL 15 min. Verification means: the
side that claims a Telegram identity must actually act *from* that Telegram
account (typing `/start <token>` proves it), and the side that claims a
service account must actually be an authenticated web session. A plain
"type your @username in your profile" is **not** accepted anywhere — usernames
aren't proof of ownership; Telegram identifies by numeric id.

- **Bot-initiated** (`/start`, no payload): bot creates a token holding this
  `tg_id` (+ `tg_username`), sends a link to `GET /tg/link/{token}` — a
  **minimal standalone page** (log in or register, then "Привязать этот
  Telegram?"; not the full app shell — ships with Phase 6a, ahead of the real
  Phase 7 UI). Confirming sets `user.tg_id`, consumes the token.
- **Web-initiated** (profile → "Подключить Telegram"): `POST /auth/tg-link`
  (authed) creates a token holding `account_id`, returns
  `{token, deep_link: "https://<host>/…"}` — actually a `t.me/<bot>?start=<token>`
  URL; the page shows it as a button/QR. Tapping it opens the bot, which
  receives `/start <token>`, matches the pending token, sets `user.tg_id`.
- Conflicts: token's `tg_id` already linked elsewhere → named error, no
  silent takeover. Caller already has a `tg_id` → must unlink first.
- Every absolute link the bot ever sends (share links, this deep-link, the
  linking page URL) is built from a configured **`PUBLIC_BASE_URL`** (§10) —
  not inferred from the request — so it survives the move from bare-IP to a
  real (sub)domain without code changes; sysadmin flips one setting.

### Everyday use

- `/domain` → pick or clear the "current" domain (affects only what upload
  defaults to and what a bare `/find` searches by default — search itself is
  cross-domain, see below).
- **Upload**: send a document / photo / archive → ingested into the current
  domain's inbox (asks which domain first if none is set and the user belongs
  to more than one). Bot API download cap ≈ 20 MB — larger files: use the web.
  A disallowed file type (§3.1 `allowed_types`) is rejected with the reason;
  for an archive, the bot summarizes what was skipped and why after
  processing (`GET /domains/{d}/uploads/{batch}` item outcomes).
- **Process inbox**: `/inbox` → next document (preview + metadata) with an
  inline keyboard: frequent tags as buttons, "＋ new tag" (free-text reply),
  "skip", "done". Loops until empty.
- **Search**: `/find договор #контрагент type:pdf 2024` hits `GET /documents`
  (cross-domain unless `/domain` narrows it) — mini-syntax in the message text
  (`#tag`, `type:`, `ocr:yes/no`, `index:yes/no`, a bare year/date) plus
  inline "refine" buttons for what doesn't fit in text: 📅 период (месяц/год/
  всё), 🗂 тип, 📁 домен, 🔖 теги (options pulled from `GET /tags`, cross-domain).
  Results show `[Домен] Название · тип · дата`; `◀ ▶` pagination. The last
  query persists per user so a bare `/find` repeats it.
- **Per-result actions** (inline keyboard under each hit): ✏️ название,
  🔖 теги (add/remove — reuses the domain's vocabulary, `write` cap), 🔍 OCR /
  📇 индексация (only offered when the type is supported / not already
  done — `process` cap), 🔖 в набор (§ below), 📄 файл (send the original if
  small enough, else a link).
  - *Does editing break a cache?* A set's archive cache is keyed by content
    (title + tags included, §15) — editing a tagged/titled document that
    belongs to a set makes that set's archive stale, and it **transparently
    rebuilds** on the next download/link hit. That's the intended behaviour,
    not a bug. Full-text search staleness is a separate, pre-existing gap:
    `search_tsv` isn't rebuilt on a title edit unless re-indexed — fixed
    globally (both web and bot) by re-running `index_document` on title change
    when the document was already indexed.
- **Sets**: `/sets` → list (own + domain-visible); open one to review items,
  or "➕ создать" from search results ("🔖 в набор", filtered to sets in that
  result's domain). "📦 скачать" runs the same ensure-current flow as the web
  (§15) — 202-while-building → bot says "готовлю архив, секунду" and retries.
  When ready: **≤ 50 MB → offers a choice** ("📄 файлом" / "🔗 ссылкой");
  above that, only a link. "🔗 ссылка на скачивание" → постоянная (`write`) /
  одноразовая (`download`), same rights model as the web (§15).

---

## 9. Background jobs (worker)

| job | trigger |
|---|---|
| `extract_archive` | archive upload |
| `ingest_file` | plain upload / each archive entry — hash, store, metadata, `doc_date`, quick text parse |
| `parse_text` | pdf (pymupdf), docx (python-docx), xlsx (openpyxl), pptx, txt/csv/md, html, eml/msg |
| `ocr_document` | manual request or `auto_ocr` |
| `index_document` | manual request or `auto_index` |
| `build_artifact` | ad-hoc export — zips a point-in-time snapshot + manifest |
| `build_set_archive` | (re)builds a set's archive cache; sets `content_hash` |
| `cleanup` | purge expired set-archive **files** (keep row + links → rebuild on next access); delete expired ad-hoc exports (row + file); trash past retention; orphan blobs (refcount 0) |

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
| `EXPORT_TTL_HOURS` | 48 | ad-hoc export artifact lifetime |
| `SET_ARCHIVE_TTL_DAYS` | 7 | set-archive cache file lifetime (global; sets are user-owned, §15 rev 4) |
| `SET_MAX_DOCS` | 5000 | hard cap on a set's resolved document count (build fails above it) |
| `SET_ARCHIVE_MAX_BYTES` | 5 GiB | hard cap on a set archive's total blob size |
| `DEFAULT_ALLOWED_TYPES` | *(unset = unrestricted)* | instance-wide default file-type allowlist, extensions (§3.1) |
| `PUBLIC_BASE_URL` | *(unset = relative)* | scheme+host used to build every absolute link the app hands out (share links, the bot's deep-links, the linking page, §8) — the one place that changes when a real (sub)domain replaces the bare VDS IP |

Per-domain overrides (`domain.settings`, editable by `owner`/`admin`, clamped to
the global caps): `storage_quota_mb`, `max_upload_mb`, `trash_retention_days`,
`auto_ocr`, `auto_index`, `default_ocr_lang`, `archive_on_conflict`,
`allow_public_links`, `allowed_types`. (`allow_public_links` is now enforced at
set-resolve time — see §15 — so a domain's kill-switch still keeps its documents
out of anyone's personal set archive.)

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
| **0 skeleton** ✅ | repo layout, config, async DB, Alembic, Docker Compose (`db`+`web`+`worker`), auth (register/login/session), `/health` |
| **1 core** ✅ | domains, members, invites, 6 roles → capability deps; single-file upload with dedup / replace / new (§13) + quota; content-addressed blob storage; document CRUD + `doc_date` extraction (pdf/office); inbox queue with per-user defer; flat tags CRUD + merge + assignment |
| **2 ingest + search** ✅ | archive upload → background extraction (zip/tar stdlib, 7z py7zr, rar rarfile+unar) with bomb/traversal guards + nested recursion; `upload_batch` + `upload_batch_item` (per-entry outcomes); opt-in `POST /documents/{id}/index` → parse text + PG `search_tsv` (SQLite → `ILIKE`); faceted search with tag/type/status facet counts; `POST /domains/{d}/exports` → `artifact` (zip + manifest.json/csv), authed `GET /artifacts/{id}[/download]` |
| **3 OCR** ✅ | SAQ worker on Postgres (`app/worker.py`) + `job_mode=inline` for dev/tests (`app/jobs.dispatch`); `POST /documents/{id}/ocr` + per-domain `auto_ocr` (image / image-only PDF only, else 422/unsupported); ocrmypdf for PDFs, pytesseract for images; `ocr_status`/`ocr_at`/`ocr_lang` + `has_ocr` filter; re-indexes with the OCR text; searchable-PDF sidecar under `DATA_DIR/derived/`. Archive extraction + artifact builds also moved onto `dispatch`. |
| **4 sets & sharing** ✅ | document sets (§15) with `private`/`domain` visibility + idempotent items; `set_content_hash` + `build_set_archive` job (lazy build, rebuild-on-change, overwrite in place, `set-<id>.zip`); transparent `GET …/sets/{s}/archive[/download]` (200 when current, else 202 while building); `Artifact.content_hash` col (migration 0005); `download_link` bound to the set's stable artifact + public `GET /d/{token}` (permanent needs `write` / one-time needs `download`, rights + `allow_public_links` + owner's `download` re-checked each hit, per-IP rate limit). Expired-file purge by `cleanup` is Phase 5 — until then a passed `expires_at` just triggers a rebuild on next access. |
| **5 trash & lifecycle** ✅ | `DELETE /documents/{id}` soft-delete (`delete` cap) → `GET /domains/{d}/trash` + `?include_trash=true`; `POST /documents/{id}/restore` (409 if content is active again); `POST /domains/{d}/trash/purge` owner-only; `cleanup` SAQ cron (`app/services/cleanup.py`, nightly) — per-domain trash retention → `hard_purge` (explicit child-row deletes + blob refcount GC), expired ad-hoc exports removed row+file, expired set-archive files cleared (row+links kept), orphan-blob disk sweep; dedup unique index made partial (`WHERE deleted_at IS NULL`, migration 0006) so trashed content doesn't block re-upload; `uploaded_at` gains a Python µs default for stable inbox FIFO |
| **6a API groundwork** ✅ | cross-domain search core (`search_documents(db, domain_ids, f)`) + `GET /documents` (`domain_id` optional filter) alongside unchanged `GET /domains/{d}/documents`; tag filters match by name, case-insensitively, folded in **Python** not SQL (`func.lower()` only folds ASCII on SQLite / a `C`-locale Postgres) — one `Tag` fetch per search, not per filter; `GET /tags` aggregates tag-name options the same way; `allowed_types` policy (§3.1): `DisallowedType` in `ingest_upload` → `415` on direct upload, `skipped_type` batch-item outcome for an archive entry; `PUBLIC_BASE_URL` (`app/util/urls.py::absolute_url`) makes `LinkOut.url` absolute; auto-reindex on a title edit when already indexed; `tg_link_token` (migration 0007) + bidirectional linking service (`app/services/tglink.py`) + `POST /auth/tg-link` + minimal standalone `GET /tg/link/{token}` page (inline HTML/JS, no build step) + `.../status` + `.../confirm` |
| **6b bot** ✅ | `app/bot/` — aiogram 3, long-polling, own process (`python -m app.bot`), shares the DB + `app/services/*` directly. `DbSessionMiddleware` + `LinkedUserMiddleware` (tg_id → `User`). `/start` both linking directions (`tglink` service); `/domain` (persisted in `bot_user_state`, migration 0008); file/photo/archive upload → inbox (`ingest_upload` / inline `process_archive`, `DisallowedType` + `skipped_type` surfaced); `/inbox` FSM tagging loop; `/find` cross-domain via `search_documents(db, member_domain_ids, …)` + `parse_query` mini-syntax + `PageCb` paging + persisted last query; per-result `DocCb` actions (send file from the shared blob volume, tags/title via FSM, OCR/index via `dispatch(None, …)`, add-to-set); `/sets` → `ensure_current_archive` + `create_share_link` (both extracted to `app/services/docsets.py`, shared with the API), ≤ 50 MB file-or-link choice. `dispatch()` grew a background=None branch for the bot. aiogram callback_data ≤ 64 B: doc-scoped verbs carry the uuid, two-object actions stash one side in FSM. |
| **7a web UI core** ✅ | `app/web/` (Jinja2 + htmx, no build; pico.css originally, now Tabler — see 7h), served at `/`; **the JSON API moved to `/api`** (`/d/{token}`, `/tg/link/*`, `/health` stay at root); session-cookie auth + signed CSRF (`itsdangerous`), `AuthRequired` → 303 to `/login?next=`; dashboard / create-domain / domain overview / document page (inline tag/title/notes edits + OCR/index buttons, HTMX `#doc` swap) |
| **7d web IA rework** ✅ | **one root-level `GET /search`** with a domain-filter `<select>` (empty = all the caller's domains), card **and** table views (`view=cards\|table`), column sort with direction (`sort` + `dir`, `_SORT_COLS` + `_order_by` in the search service), sort control lifted out of the filter form; **`GET/POST /upload`** root-level with a target-domain picker; **`GET /inbox`** = "Очередь на сортировку", one queue across every domain the caller can tag (`next_inbox_across` / `inbox_count_across`); domain tabs drop search/inbox/upload; `register_user` now auto-creates a **«Мои документы»** domain (architecture §2.1) |
| **7b web UI** ✅ | `app/web/sets.py` (list/detail, add-from-document, remove item, delete, `GET .../archive` → file or `?building=1`, `POST .../links`, `POST /links/{id}/revoke`); `app/web/inbox.py` (redirect-per-action card flow, tag + complete, defer, undefer); `app/web/tags.py` (create/rename+recolour/delete/merge); `app/web/search.py` grew `GET /search` (all the caller's domains, `_results.html` shows `[Domain]`) |
| **7c web UI** ✅ | `app/web/members.py` (add / role `<select onchange=submit>` / remove / invite — owner row locked); `app/web/manage.py` (`GET/POST /domains/{slug}/settings` merging `domain.settings` with global-cap clamps, `POST .../delete` owner-only; `GET /domains/{slug}/trash` + restore + `POST .../trash/purge`); `app/web/profile.py` (Telegram link via `tglink` service → `?tg_token=`, unlink, password change). No API-keys UI — that feature isn't built server-side. |
| **7e inbox rework + previews** ✅ | `app/services/thumbs.py` — lazy per-blob WebP thumbnail (`can_thumb`, `ensure_thumb`, cached at `derived/<sha>/thumb.webp`, PIL in a threadpool); `GET /documents/{id}/thumb` (web) shown on the document page and in the inbox. `app/web/inbox.py` rebuilt: `GET /inbox` = domain-filterable **table** of every unlabelled doc the caller can tag; `GET /inbox/card` opens a `<dialog>` tagging card (htmx `#tagcard` swap, `htmx:afterSwap` opens the modal); `POST /inbox/{id}/done` / `/defer` return the next card + `HX-Trigger: inbox-refresh` so the table reloads underneath. Frequent-tag chips: click appends to the field and disables the chip (also disabled when the name is typed manually) — spent chips render light-grey / dashed / struck-through. Bot `/inbox` sends `answer_photo` with the thumbnail (or original ≤ 9 MB) as the tagging prompt. |
| **7g inbox modal + bot previews** ✅ | tagging modal (`_inbox_card.html`): full-width centred image preview, an editable **Название** field above the tags field (`POST /inbox/{id}/done` gained an optional `title` → `update_document`), "частые" chips on one scrollable line under the tags field, bottom row is just `[Отложить] … [Готово, дальше →]` (close = the dialog's × only). Bot: `app/bot/handlers/_util.py::send_doc_card()` — sends a doc as photo-with-caption when it's a previewable image (thumb, or small original), else plain text; used by `/find` results and reused by the `/inbox` card. Web search results (`_res_cards.html` / `_res_table.html`) show a thumbnail for image docs; `.tag` chips recoloured to a light fill (were dark-on-dark). |
| **7f search polish + icons** ✅ | search filters: "Запрос" is a plain text box (was `type=search`), "Тип (расширение)" is a `<select>` built from `_distinct_exts` (extensions actually present in the caller's docs), the never-produced `archived` status option is dropped, the status column is humanised via a `statusfmt` Jinja filter, the "идx"/"индекс" badge is renamed "Indexed", and the "Фасеты" block is removed (the API still returns facet data). Vendored Lucide-style SVG sprite `app/web/static/icons.svg` + `_icons.html::icon()` macro replaces the emoji in the top nav, the Домены action column, the search view toggle, and the set archive/link buttons. |
| **8 email confirmation** ✅ (opt-in) | set `SMTP_HOST` and a new account must click a link before it can log in; unset → accounts work immediately (dev / single-tenant). Migration 0009 adds `user.email_verified_at` (existing rows backfilled). `app/services/email.py` — `aiosmtplib` (lazy import, no-op without SMTP), an `itsdangerous` signed token (no token table), link via `PUBLIC_BASE_URL`. Web + API `register` create a dormant account and send the mail; `login` is blocked (403 / login page with a resend button); `GET /verify/{token}` activates; `POST /verify/resend`; `verify_sent.html`. |
| **7n upload progress** ✅ | the upload form posts via htmx (`hx-encoding=multipart/form-data` → `#upload-result`), so `htmx:xhr:progress` drives a Tabler progress bar (percent while sending, then "Обработка на сервере…" while the archive unpacks); the submit button is disabled for the request. `_upload_result.html` is the HX partial (`POST /upload` sets `partial`); a JS-off POST still re-renders the whole page. |
| **7m 7z extractor** ✅ | `py7zr` bumped `0.22.0` → `1.1.3` (0.22's `pyppmd` has no py3.13 Windows wheel — dev boxes hit "7z support is not installed"). py7zr 1.x drops `readall()`; `archive.py::_sevenz` now reads the entry list first (bomb-checks declared sizes *before* touching the data), extracts the wanted files to a temp dir on disk (not the whole archive into RAM), then streams each into a size-capped temp file. New `tests/test_archive.py`. |
| **7l error pages + CSRF fix** ✅ | the upload and domain-settings forms had lost their hidden `csrf_token` in the Tabler pass (native POST → 403 "bad or missing CSRF token"); restored. `error.html` + `@app.exception_handler` for `StarletteHTTPException` / `RequestValidationError` / (non-debug) `Exception`: a web request gets a Tabler error page, `/api` keeps JSON, an htmx request gets `HX-Reswap: none` + `HX-Trigger` `dc-toast`. Multipart spool dir pinned to `DATA_DIR/tmp` (`tempfile.tempdir`) so big archive uploads don't fill a RAM-backed `/tmp`. |
| **7k bulk search actions** ✅ | checkboxes on every result (card + table) + a "select on page" box; a bulk bar over `#results` runs **Индексировать / OCR / В набор** (existing or a new set) on the selection. `POST /search/bulk` re-renders the results fragment and fires a transient toast (`HX-Trigger: dc-toast`). The selection is client-side in `localStorage` — survives pagination and sort/view changes, wiped when a filter changes (the server stamps a `filter_sig` on the results wrapper; the script compares and clears). Add-to-set needs a single-domain selection. Doc-page **"Наборы"** picker is now always shown (there's always "➕ Новый набор…"); `add-to-set` accepts `new_name`. |
| **7j document page** ✅ | breadcrumb dropped (it echoed the top nav). Cards: **Теги** (input + domain frequent-tag chips, inbox-style "chip inactive once present"), **Наборы** (all sets the doc is in — each removable — + a picker; a document is **many-to-many** with sets via `document_set_item`'s composite PK and always was), **Свойства** (file name + type + editable title / date / notes), **Метаданные** (domain, the single status marker, indexing, OCR, text-source, size, uploaded, version, sha256, process buttons — "Обработка" folded in here). "Скачать оригинал" is a pictogram over the image preview. `POST /documents/{id}/remove-from-set`; `add-to-set` returns the doc-body fragment. `Document.extracted_text` made **deferred** — can be MBs, only written on indexing / matched in SQL, never read for display. |
| **7i Tabler polish** ✅ | search card view is a real responsive grid (`col-sm-6 col-xl-4`), each card led by a media area — the thumbnail for images, else a colour-by-filetype tile with the extension; `status` dropped as a sort key; doc-date omitted when absent. New `_docmeta.html` — `card_media()` + `state_icons()` (status / index / OCR as pictograms with `data-bs-toggle="tooltip"`), reused in the search table, document page, and set contents. Forms use Tabler's **Form with Icons** (`.input-icon` + addon). **Dark theme**: pre-paint `<head>` script applies `data-bs-theme` from `localStorage` (fallback `prefers-color-scheme`); a navbar sun/moon button toggles + persists; tooltips init on load and `htmx:afterSwap`. Upload page gets a native drag-and-drop zone feeding the file input. |
| **7h Tabler reskin** ✅ | the whole web UI moved off pico.css onto **[Tabler](https://tabler.io) 1.4.0** (MIT, Bootstrap 5.3, no build) — `tabler.min.css` + `tabler.min.js` vendored in `static/` (the JS bundles Popper + Bootstrap and exposes `window.tabler`). `base.html` is the single Tabler shell: horizontal top navbar + optional page-header from a `page_title` block; `login.html` / `register.html` are standalone `.page-center` auth cards. Every template rebuilt with native components — `.card`, `.form-control` / `.form-select` / `.form-check`, `.btn`, `.table.card-table`, `.badge`, `.nav-tabs` (domain sub-nav), `.breadcrumb`, `.datagrid` (document metadata), `.btn-list`. The inbox tagging flow moved from `<dialog>`/`showModal()` to a real **Bootstrap modal** via `window.tabler.Modal` (lazy-init so it survives the deferred bundle); htmx `#tagcard` swap + `htmx:afterSwap` still drive it. Accent left at Tabler's default blue. |

Deliberately **out of scope for 6a/6b**: push notifications when an async job
(OCR/index/archive build) finishes — the bot re-shows current status when the
user next looks; revisit once there's a reason to add a job → bot message
channel.

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

> **Rev 4 (2026-09-03).** Sets left domains — a set now belongs to a **user**
> and spans every domain that user can reach. A set is no longer a static list:
> it is **N saved search filters + explicitly added documents**, resolved live.
> Only the owner edits or even sees a set. Two ways out: a **share link** (the
> *public segment* — only `is_public` documents) and the owner's own **«Полная
> выгрузка»** (everything the owner can reach).

### Concept

A **document set** (`набор`) belongs to one user. It has a *definition* the
owner controls and a *result* computed on demand:

```
result(set) =
    ( ⋃ over each saved filter: search(filter, scope from the filter) )
  ∪ explicitly-added documents
  ∩ documents the owner can currently `download`
```

The definition is static and owner-only. The result is **dynamic**: as other
people upload or tag documents that match a saved filter, those documents enter
the set — and its archive — on the next access. "Nobody but the owner edits the
set" and "the contents change on their own" are both true and intended: the
owner owns the *rules*, not the *matches*. There is no per-document exclusion —
to drop a match, narrow the filter.

### Model

- `document_set` — `id`, `owner_id` (FK `user`, **NOT NULL**, `ON DELETE
  CASCADE`), `name`, `description`, timestamps. No `domain_id` / `visibility` /
  `item_count` (the count is dynamic — computed live for the UI).
- `document_set_filter` — a saved search attached to a set. `id`, `set_id`
  (FK CASCADE), `position`, `filter` (JSON — a serialized `SearchFilters`,
  **including a `domain_ids` list**; empty = every domain the owner can reach
  at query time), `description` (cached human summary for the card, e.g.
  *«стройка · #договор · pdf · 2024»*), `created_at`. Any search on `/search`
  can be saved here.
- `document_set_item` — `(set_id, document_id)` PK, `added_by`, `added_at`,
  `position`. Explicitly-added documents, always in the result (subject to the
  owner still having `download`). Trashed → skipped on build; blob purge →
  row removed.
- `artifact` — **one per set**, `kind = set_archive`, `source_id = set_id`,
  **`domain_id` nullable** (no domain; the owner is `requested_by`). Overwritten
  in place. `status`, `storage_key` (`set-<set_id>.zip`), `content_hash`,
  `size_bytes`, `item_count`, `missing_count`, `snapshot`, `expires_at`. Ad-hoc
  exports still use `artifact` (`kind = adhoc_export`, domain-scoped or
  owner-scoped, never rebuilt).
- `download_link` — unchanged. `artifact_id`, opaque `token`, `max_downloads`
  (`1` = one-time, `NULL` = unlimited), `expires_at`, `download_count`,
  `revoked_at`, `last_downloaded_at`, `created_by` (= the set owner).

### Per-document visibility (`is_public`)

Sharing is a **public segment**: a share link only ever exposes documents
flagged public.

- `domain.settings.default_document_visibility` — `private` (default) |
  `public`. Set by the domain owner.
- `document.is_public` (bool) — set at ingest from the domain default,
  **whoever uploads** (the rule follows the domain, not the uploader).
- Changed per-document or in bulk from `/search`; the capability is `manage`
  (domain owner / admin), same as who configures the domain.
- Changing the domain default affects **new** uploads only; existing documents
  are re-flagged with the search bulk action.
- `is_public` only governs set-archive / share-link exposure. Inside a domain,
  who sees a document is still pure RBAC.

### Resolving a set (`resolve_set`)

Produces the ordered document list for the hash, the archive, the live count.
One scope rule, two visibility filters:

1. For each filter: `scope = filter.domain_ids` (empty → every domain in
   `list_memberships(owner)`), intersected with the owner's **current**
   membership; run `search_documents(db, scope, filter)` with
   `page_size = SET_MAX_DOCS + 1`. Union the ids.
2. Add `document_set_item` ids.
3. Keep ids where `deleted_at IS NULL` and the owner still has `download` in
   that document's domain.
4. Apply the visibility filter for the caller:
   - **share link / set archive** → keep only `is_public = true`.
   - **«Полная выгрузка»** (owner, authed) → no visibility filter.
5. Order: explicit items first by `position`, then filter matches by
   `uploaded_at DESC, id`.

### Set content hash & ensure-current

The cached `artifact` is the **public** archive (step 4 → share view). Its
`content_hash` is `sha256(json([...]))` over the resolved public list — same
shape as before (`doc.id`, `sha256`, `title`, `doc_date`, sorted tag slugs),
sorted by id. `is_public` and the owner's live access are baked into the
resolve, so flipping a document private or losing membership changes the hash
and rebuilds the archive without it.

On `GET /sets/{s}/archive[/download]` and `GET /d/{token}`:

1. `current_hash = hash(resolve_set(share view))`.
2. load / lazily create the set's `artifact`.
3. rebuild if `content_hash != current_hash` **or** the file is missing **or**
   `expires_at` passed → enqueue `build_set_archive`, `status=building`,
   `expires_at = now + SET_ARCHIVE_TTL_DAYS` (**global**). Concurrent hits with
   the same target hash do not re-enqueue.
4. `ready` and current → **200** stream; else **202**
   `{status:"building", retry_after: 2}`.

`build_set_archive(set_id)` resolves the share view, writes
`data/artifacts/set-<set_id>.zip` (`files/` + `manifest.json` +
`manifest.csv`), sets `content_hash` / `size_bytes` / `item_count` /
`missing_count` / `status=ready`.

**Size guard.** Resolved list over `SET_MAX_DOCS` (default 5000) or total blob
size over `SET_ARCHIVE_MAX_BYTES` (default 5 GiB) → build `status=failed` with a
clear error — **never a silent truncation** of a shared archive.

### «Полная выгрузка» — the owner's full export

A button on the set page, **owner only, authenticated**. Builds an
`adhoc_export` artifact from `resolve_set(full view)` — every document the
owner can `download`, ignoring `is_public`. One-shot: not cached long-term
(`EXPORT_TTL_HOURS`), never linkable. Each press rebuilds. Note under the
button: *«В архив войдут все документы ваших доменов и все документы из чужих
доменов, к которым у вас есть доступ. Архив личный — поделиться им по ссылке
нельзя.»*

### Workflow

1. **`/search`** → tick results → *«＋ в набор»* adds them as
   `document_set_item`s to a chosen / new set (cross-domain is fine).
   Separately, *«сохранить фильтр в набор»* stores the current query as a
   `document_set_filter`.
2. **Мои наборы** (top-level nav): the owner's sets with a live count.
3. **Set page** — name / description; the saved filters, each with its
   `description` and an **«открыть в поиске»** link (`/search?…` rebuilt from
   the stored filter); the explicitly-added documents; a live preview of the
   resolved result; the archive block; **«Полная выгрузка»**.
4. **Скачать архив** / **создать ссылку** — *постоянная* (`max_downloads=NULL`)
   / *одноразовая* (`max_downloads=1`, optional expiry). No capability gate:
   the set is the owner's and the archive is public-only anyway. The link binds
   to the set's stable `artifact_id`.
5. Links: list, copy, download count, **revoke** (owner only).

### Share links — `download_link.mode`

- **`archive`** (default) — `GET /d/{token}` streams the set's zip. Re-checked
  every hit: link not `revoked` / not past `expires_at` /
  `download_count < max_downloads`; set exists, owner active; *ensure-current*
  on the share view; `ready` → stream, bump `download_count`. Empty resolve →
  **410**. IP rate-limited. One-time links die after one completed download.
- **`gallery`** (`app/gallery.py`, `GET /g/{token}`) — a standing public
  browse page over the set's **public** documents (never single-use). Same
  per-hit re-check. `/g/{token}` = thumbnail grid + a form to pick sort
  (`uploaded|doc_date|title|size|random`+`seed`) and slide `interval`, then
  `/g/{token}/slideshow` — a standalone fullscreen player, **images only**
  (←/→ prev-next, space = next, `p` pause, esc = back). `/g/{token}.json` and
  `/g/{token}/feed` (Atom) expose the same list for external widgets — a
  dynamic set means new matches show up on their own. `/g/{token}/i/{doc_id}`
  serves one document only if its id is in the live public resolve (no
  enumeration). `/d` and `/g` reject each other's tokens. *(Video is a planned
  future document type — the slideshow will pick it up.)*

### Permissions

| action | who |
|---|---|
| everything about a set — see it, edit the definition, download, share, revoke | the **owner**, nobody else |
| open a share link | anyone with the token, subject to the re-check |
| set `document.is_public` (per-doc or bulk) | `manage` in that document's domain |

A future `document_set_member` table can add co-owners / read grants without
touching this model.

### Lifecycle & cleanup

- Set-archive **files** expire after `SET_ARCHIVE_TTL_DAYS` (default **7**,
  global). `cleanup` deletes the file, clears `storage_key`, sets
  `status=building` — row + links survive, next access rebuilds.
- Ad-hoc export files (incl. «Полная выгрузка») expire after `EXPORT_TTL_HOURS`,
  removed row-and-all.
- Deleting a set (or its owner) cascades: `document_set_filter`,
  `document_set_item`, the `artifact` row + file, `download_link`s.

### Routes (moved off `/domains/{…}`)

`/sets` · `/sets/{id}` · `/sets/{id}/filters` · `/sets/{id}/filters/{fid}` ·
`/sets/{id}/items` · `/sets/{id}/items/{doc}` · `/sets/{id}/archive[/download]` ·
`/sets/{id}/export` («Полная выгрузка») · `/sets/{id}/links` · `/links/{id}` ·
public `GET /d/{token}` unchanged.

### Migration

`0010` — `document_set`: drop `domain_id` / `visibility` / `item_count`, add
`owner_id NOT NULL`; add `document_set_filter`; `document`: add `is_public bool`
(server default `false`); `artifact.domain_id` → nullable; drop the
`SetVisibility` enum and the `allow_public_links` domain setting. Existing sets
are **dropped** (test-phase data). `document_set_item` keeps its shape.

---

## 14. Trash & retention

- **Soft delete** (`delete` capability): `deleted_at` + `deleted_by` set. The
  document leaves the default search / inbox / exports and shows up under the
  **Корзина** search preset (`/search?preset=trash`), where a `delete`-holder
  can bulk-restore or bulk-purge. `POST /domains/{d}/trash/purge` (owner) still
  empties a whole domain's trash.
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
