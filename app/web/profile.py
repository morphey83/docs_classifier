"""Web UI: the user's profile — Telegram linking, password."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security import hash_password, verify_password
from app.services import tglink as tglink_svc
from app.util.urls import absolute_url, bot_deep_link
from app.web.csrf import CsrfGuard
from app.web.deps import current_user
from app.web.templating import render

router = APIRouter()


@router.get("/profile")
async def profile_page(
    request: Request, user: User = Depends(current_user)
) -> Response:
    tok = request.query_params.get("tg_token")
    deep = bot_deep_link(tok) if tok else None
    return render(
        request,
        "profile.html",
        {
            "tg_token": tok,
            "tg_deep_link": deep,
            "tg_link_url": absolute_url(f"/tg/link/{tok}") if tok else None,
            "pw_error": request.query_params.get("pw") == "err",
            "pw_ok": request.query_params.get("pw") == "ok",
        },
    )


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
