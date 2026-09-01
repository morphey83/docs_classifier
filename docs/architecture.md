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
| UI | **HTMX + Jinja**, server-rendered, same FastAPI app. (Details in a later round.) |
| At-rest encryption | Not in scope. |
| Reverse proxy | **No proxy / no domain on the VDS yet** → compose ships its own **Caddy**. Interim: `tls internal` (self-signed) for IP access; once a (sub)domain points at the VDS, one Caddyfile line switches to real Let's Encrypt TLS. A free `*.duckdns.org` name + Caddy DNS-01 also works. |
| Document sets | Persistent hand-curated collections. The archive is a **cache of the set's current content** — built on first download, rebuilt automatically (overwritten in place) when the set changes, file purged after `domain.set_archive_ttl_days` (default 7). **Permanent / one-time links** bind to the set's stable artifact and always serve current contents (rights re-checked each access). See §15. |
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
            archive_on_conflict (skip|new), set_archive_ttl_days,
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

document_set(id, domain_id, name, description, visibility (private|domain),
             created_by, item_count, created_at, updated_at)
document_set_item(set_id, document_id, added_by, added_at, position)   # uniq(set,doc)

artifact(id, domain_id, kind (adhoc_export|set_archive), source_id, content_hash?,
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

Blob storage: `DATA_DIR/blobs/<h[0:2]>/<h[2:4]>/<h>` (content-addressed,
dedup). Derived files: `DATA_DIR/derived/<h>/…`.

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
| `SET_ARCHIVE_TTL_DAYS` | 7 | set-archive cache file lifetime (per-domain `set_archive_ttl_days`) |
| `DEFAULT_ALLOWED_TYPES` | *(unset = unrestricted)* | instance-wide default file-type allowlist, extensions (§3.1) |
| `PUBLIC_BASE_URL` | *(unset = relative)* | scheme+host used to build every absolute link the app hands out (share links, the bot's deep-links, the linking page, §8) — the one place that changes when a real (sub)domain replaces the bare VDS IP |

Per-domain overrides (`domain.settings`, editable by `owner`/`admin`, clamped to
the global caps): `storage_quota_mb`, `max_upload_mb`, `trash_retention_days`,
`auto_ocr`, `auto_index`, `default_ocr_lang`, `archive_on_conflict`,
`set_archive_ttl_days`, `allow_public_links`, `allowed_types`.

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
| **7a web UI core** ✅ | `app/web/` (Jinja2 + vendored pico.css/htmx, no build), served at `/`; **the JSON API moved to `/api`** (`/d/{token}`, `/tg/link/*`, `/health` stay at root); session-cookie auth + signed CSRF (`itsdangerous`), `AuthRequired` → 303 to `/login?next=`; dashboard / create-domain / domain overview / document page (inline tag/title/notes edits + OCR/index buttons, HTMX `#doc` swap) |
| **7d web IA rework** ✅ | **one root-level `GET /search`** with a domain-filter `<select>` (empty = all the caller's domains), card **and** table views (`view=cards\|table`), column sort with direction (`sort` + `dir`, `_SORT_COLS` + `_order_by` in the search service), sort control lifted out of the filter form; **`GET/POST /upload`** root-level with a target-domain picker; **`GET /inbox`** = "Очередь на сортировку", one queue across every domain the caller can tag (`next_inbox_across` / `inbox_count_across`); domain tabs drop search/inbox/upload; `register_user` now auto-creates a **«Мои документы»** domain (architecture §2.1) |
| **7b web UI** ✅ | `app/web/sets.py` (list/detail, add-from-document, remove item, delete, `GET .../archive` → file or `?building=1`, `POST .../links`, `POST /links/{id}/revoke`); `app/web/inbox.py` (redirect-per-action card flow, tag + complete, defer, undefer); `app/web/tags.py` (create/rename+recolour/delete/merge); `app/web/search.py` grew `GET /search` (all the caller's domains, `_results.html` shows `[Domain]`) |
| **7c web UI** ✅ | `app/web/members.py` (add / role `<select onchange=submit>` / remove / invite — owner row locked); `app/web/manage.py` (`GET/POST /domains/{slug}/settings` merging `domain.settings` with global-cap clamps, `POST .../delete` owner-only; `GET /domains/{slug}/trash` + restore + `POST .../trash/purge`); `app/web/profile.py` (Telegram link via `tglink` service → `?tg_token=`, unlink, password change). No API-keys UI — that feature isn't built server-side. |
| **7e inbox rework + previews** ✅ | `app/services/thumbs.py` — lazy per-blob WebP thumbnail (`can_thumb`, `ensure_thumb`, cached at `derived/<sha>/thumb.webp`, PIL in a threadpool); `GET /documents/{id}/thumb` (web) shown on the document page and in the inbox. `app/web/inbox.py` rebuilt: `GET /inbox` = domain-filterable **table** of every unlabelled doc the caller can tag; `GET /inbox/card` opens a `<dialog>` tagging card (htmx `#tagcard` swap, `htmx:afterSwap` opens the modal); `POST /inbox/{id}/done` / `/defer` return the next card + `HX-Trigger: inbox-refresh` so the table reloads underneath. Frequent-tag chips: click appends to the field and disables the chip (also disabled when the name is typed manually) — spent chips render light-grey / dashed / struck-through. Bot `/inbox` sends `answer_photo` with the thumbnail (or original ≤ 9 MB) as the tagging prompt. |
| **7f search polish + icons** ✅ | search filters: "Запрос" is a plain text box (was `type=search`), "Тип (расширение)" is a `<select>` built from `_distinct_exts` (extensions actually present in the caller's docs), the never-produced `archived` status option is dropped, the status column is humanised via a `statusfmt` Jinja filter, the "идx"/"индекс" badge is renamed "Indexed", and the "Фасеты" block is removed (the API still returns facet data). Vendored Lucide-style SVG sprite `app/web/static/icons.svg` + `_icons.html::icon()` macro replaces the emoji in the top nav, the Домены action column, the search view toggle, and the set archive/link buttons. |

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

### Concept

A **document set** (`набор`) is a persistent, hand-curated list of documents
that a user fills incrementally: run a search → tick some results →
**"добавить в набор"** → pick an existing set or create a new one. Distinct from
a search filter (dynamic) and from an ad-hoc export (one-shot).

The user never explicitly "builds" an archive. The archive is a **cache of the
set's current content**, produced on demand and rebuilt automatically whenever
the set has changed. Links are created against the set and keep working across
rebuilds — a link always serves the set's *current* contents (rights are
re-checked on every access).

### Model

- `document_set` — `domain_id`, `name`, `description`, `visibility`
  (`private` to creator, or `domain` = visible to all members),
  `created_by`, `item_count`.
- `document_set_item` — `(set_id, document_id)` unique (idempotent add),
  `added_by`, `added_at`, `position` (manual reorder). If a document is trashed
  the item stays but is skipped on build; on blob purge it is removed.
- `artifact` — **one per set** (`kind = set_archive`, `source_id = set_id`),
  created lazily and **overwritten in place** when the set changes. Fields:
  `status` (`building`/`ready`/`failed`), `storage_key` (fixed
  `set-<set_id>.zip`, cleared when the file is purged), `content_hash` (the set
  hash the current file was built from), `size_bytes`, `item_count`,
  `missing_count`, `snapshot` (doc ids at last build), `expires_at` (**file**
  expiry — see cleanup). Ad-hoc exports also use `artifact`
  (`kind = adhoc_export`) but those are point-in-time snapshots, never rebuilt.
- `download_link` — `artifact_id` (the set's stable artifact), opaque `token`
  (≈192-bit, URL-safe), `max_downloads` (`1` = one-time, `NULL` = unlimited),
  `expires_at` (nullable), `download_count`, `revoked_at`, `last_downloaded_at`,
  `created_by`.

### Set content hash

Deterministic, computed on every archive access and compared to
`artifact.content_hash`:

```
sha256(json([
  (str(doc.id), doc.sha256, doc.title,
   doc.doc_date.isoformat() or "", ",".join(sorted(tag_slugs)))
  for doc in non-deleted items, sorted by doc.id
]))
```

Covers everything that ends up in the zip or the manifest, so any change that
would change the archive triggers a rebuild.

### Ensure-current (the transparent build)

On `GET …/sets/{s}/archive[/download]` and on `GET /d/{token}`:

1. compute `current_hash` from the set.
2. load the set's `artifact` (create it, `status=building`, if absent).
3. if `artifact.content_hash != current_hash` **or** the file is missing **or**
   `expires_at` has passed → enqueue `build_set_archive`, set
   `status=building`, `expires_at = now + domain.set_archive_ttl_days`.
4. respond:
   - `ready` and current → **200**, stream the file.
   - building → **202** `{status:"building", retry_after: 2}` — the client
     polls / retries.

`build_set_archive(set_id)` writes `data/artifacts/set-<set_id>.zip` (files/ +
`manifest.json` + `manifest.csv`), sets `content_hash`, `size_bytes`,
`item_count`, `missing_count`, `status=ready`.

### Workflow

1. Search results carry checkboxes → **"добавить в набор"** → dialog lists the
   user's sets (+ editable `domain`-visible sets) or **"＋ создать набор"**.
2. Duplicates are ignored. Toast: *добавлено N в «набор X»*.
3. **Наборы** section: each set with its count; open a set to review, reorder,
   remove items, edit name/visibility.
4. **Скачать архив** — just a download. The first request builds it (202 while
   building), later requests stream it. Editing the set and downloading again
   transparently rebuilds.
5. **Создать ссылку** — *постоянная* (`max_downloads=NULL`, `expires_at=NULL`) /
   *одноразовая* (`max_downloads=1`, optional expiry) / *с истечением* (custom
   `expires_at`). The link binds to the set's stable `artifact_id`.
6. Links are managed on the set: list, copy, download count, **revoke**.

### Public download — `GET /d/{token}`

No auth cookie, but **rights are re-checked every time**:

- link not `revoked`, not past `expires_at`, `download_count < max_downloads`;
- `allow_public_links` still `true` for the domain;
- the link's `created_by` still has `download` in the domain (removed → link
  dies).

Then runs *ensure-current* (§ above); if `ready`, streams the file
(`Content-Disposition: attachment; filename="<set> <date>.zip"`), increments
`download_count`, sets `last_downloaded_at`. IP rate-limited. A one-time link is
dead after the first completed download.

### Permissions

| action | capability |
|---|---|
| create / edit own set, add/remove items | `view` |
| edit a `domain`-visible set someone else made | `manage` (or be the creator) |
| download the set archive | `download` |
| create a **one-time** link | `download` |
| create a **permanent** link | `write` *(a standing public URL is a bigger commitment)* |
| revoke a link | link creator, or `manage` |

Domain setting `allow_public_links` (default `true`). Every link create /
revoke / public download is in `audit_log`.

### Lifecycle & cleanup

- Set‑archive **files** expire after `domain.set_archive_ttl_days` (default
  **7**, per‑domain). The `cleanup` job deletes any expired archive file and
  clears `artifact.storage_key` / sets `status=building` — the row and its
  links survive; the next access rebuilds. Nothing is "pinned", so temp
  storage stays bounded.
- Ad‑hoc export files expire after `EXPORT_TTL_HOURS` and are removed row‑and‑
  all (they are snapshots, not rebuildable).
- Deleting a set deletes its artifact file, artifact row, and links.

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
