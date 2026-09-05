"""Web UI: the user's profile — Telegram linking, password, API keys."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security import hash_password, verify_password
from app.services import api_keys as api_keys_svc
from app.services import tglink as tglink_svc
from app.util.urls import absolute_url, bot_deep_link
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()


async def _ctx(
    request: Request, user: User, db: AsyncSession, *, new_key_token: str | None = None
) -> dict:
    tok = request.query_params.get("tg_token")
    deep = bot_deep_link(tok) if tok else None
    return {
        "tg_token": tok,
        "tg_deep_link": deep,
        "tg_link_url": absolute_url(f"/tg/link/{tok}") if tok else None,
        "pw_error": request.query_params.get("pw") == "err",
        "pw_ok": request.query_params.get("pw") == "ok",
        "api_keys": await api_keys_svc.list_api_keys(db, user.id),
        "new_key_token": new_key_token,
    }


@router.get("/profile")
async def profile_page(
    request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
) -> Response:
    return render(request, "profile.html", await _ctx(request, user, db))


@router.post("/profile/tg-link")
async def profile_tg_link(
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    if user.tg_id is not None:
        return RedirectResponse("/profile", status_code=303)
    tok = await tglink_svc.create_web_initiated(db, user)
    return RedirectResponse(f"/profile?tg_token={tok.token}", status_code=303)


@router.post("/profile/tg-unlink")
async def profile_tg_unlink(
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    fresh = await db.get(User, user.id)
    fresh.tg_id = None
    await db.flush()
    return RedirectResponse("/profile", status_code=303)


@router.post("/profile/password")
async def profile_password(
    current: str = Form(...),
    new_password: str = Form(...),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    fresh = await db.get(User, user.id)
    if len(new_password) < 8 or not await verify_password(fresh.password_hash, current):
        return RedirectResponse("/profile?pw=err", status_code=303)
    fresh.password_hash = await hash_password(new_password)
    await db.flush()
    return RedirectResponse("/profile?pw=ok", status_code=303)


@router.post("/profile/api-keys")
async def profile_create_api_key(
    request: Request,
    name: str = Form(...),
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    _key, raw = await api_keys_svc.create_api_key(db, user, name=name)
    # the raw token is shown exactly once — rendered directly, never put in a
    # redirect URL or query string
    return render(request, "profile.html", await _ctx(request, user, db, new_key_token=raw))


@router.post("/profile/api-keys/{key_id}/revoke")
async def profile_revoke_api_key(
    key_id: uuid.UUID,
    _: None = CsrfGuard,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    key = await api_keys_svc.get_owned_key(db, key_id, user.id)
    if key is not None:
        await api_keys_svc.revoke_api_key(db, key)
    return RedirectResponse("/profile", status_code=303)
