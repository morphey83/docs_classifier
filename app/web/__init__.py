"""Server-rendered web UI (HTMX + Jinja), mounted at the site root.

Calls ``app/services/*`` directly, like the bot — the JSON API under ``/api``
is for scripts, not for this UI.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.web import (
    auth,
    dashboard,
    documents,
    domains,
    inbox,
    manage,
    members,
    profile,
    search,
    sets,
    tags,
)

web_router = APIRouter(include_in_schema=False)
_MODULES = (
    auth, dashboard, domains, search, documents, inbox,
    sets, tags, members, manage, profile,
)
for _mod in _MODULES:
    web_router.include_router(_mod.router)
