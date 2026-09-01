# DocsClassifier

Self-hosted, multi-user document store: upload documents or archives, tag them
manually through a processing queue, and find them again with faceted search
(static characteristics + tags + optional full-text). Optional per-document OCR
and advanced indexing. Web API + UI, plus a Telegram bot that mirrors it.

**Status: design phase.** See [`docs/architecture.md`](docs/architecture.md).
No code yet.

## Planned stack

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 (async) · Alembic ·
SAQ (Postgres-backed jobs) · aiogram 3 (bot) · Docker Compose.
OCR via tesseract / ocrmypdf. Archives: zip / 7z / rar / tar.
