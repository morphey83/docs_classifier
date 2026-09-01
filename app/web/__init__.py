"""Server-rendered web UI (HTMX + Jinja), mounted at the site root.

Calls ``app/services/*`` directly, like the bot — the JSON API under ``/api``
is for scripts, not for this UI.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.web import auth, dashboard, documents, domains, search

web_router = APIRouter(include_in_schema=False)
web_router.include_router(auth.router)
web_router.include_router(dashboard.router)
web_router.include_router(domains.router)
web_router.include_router(documents.router)
web_router.include_router(search.router)
