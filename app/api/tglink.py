"""Telegram account linking: create/confirm tokens + the minimal linking page (§8).

Two independent handshakes, one token table (`app/services/tglink.py`):

- Bot-initiated (`/start`, no payload) — the bot creates the token; the human
  opens ``GET /tg/link/{token}`` in a browser. If they aren't signed in we bounce
  them through the normal ``/login`` / ``/register`` pages (``?next=`` back here),
  then they confirm.
- Web-initiated (profile page) — ``POST /api/auth/tg-link`` (authed, in
  ``app/api/auth.py``) creates the token and returns a bot deep-link; the bot
  consumes it directly (in-process) when it receives ``/start <token>``.

These ``/tg/link/*`` routes are mounted at the site root (not under ``/api``).
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.schemas.tglink import TgLinkStatusOut
from app.security import get_current_user, get_current_user_optional
from app.services import tglink as svc
from app.util.time import as_aware, utcnow

router = APIRouter(tags=["telegram-link"])


@router.get("/tg/link/{token}/status", response_model=TgLinkStatusOut)
async def link_status(token: str, db: AsyncSession = Depends(get_session)) -> TgLinkStatusOut:
    tok = await svc.peek(db, token)
    if tok is None:
        return TgLinkStatusOut(valid=False, reason="ссылка не найдена")
    if tok.consumed_at is not None:
        return TgLinkStatusOut(valid=False, reason="эта ссылка уже использована")
    if as_aware(tok.expires_at) <= utcnow():
        return TgLinkStatusOut(valid=False, reason="срок действия ссылки истёк")
    kind = "bot" if tok.tg_id is not None else "web"
    return TgLinkStatusOut(valid=True, kind=kind, tg_username=tok.tg_username)


@router.post("/tg/link/{token}/confirm")
async def confirm_link(
    token: str,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    try:
        tok = await svc.confirm_bot_initiated(db, token, user)
    except svc.TgLinkError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    if tok.tg_id is not None:
        background.add_task(svc.notify_account_linked, tok.tg_id, user.username)
    return {"linked": True}


def _page_state(tok) -> tuple[str, str | None, str | None]:
    """(state, reason, tg_username) — state is 'ok' | 'bad' | 'wrong_kind'."""
    if tok is None:
        return "bad", "Ссылка не найдена.", None
    if tok.consumed_at is not None:
        return "bad", "Эта ссылка уже использована.", None
    if as_aware(tok.expires_at) <= utcnow():
        return "bad", "Срок действия ссылки истёк — запросите новую в боте.", None
    if tok.tg_id is None:
        return "wrong_kind", "Эту ссылку нужно открыть в Telegram — нажмите кнопку в чате с ботом.", None
    return "ok", None, tok.tg_username


@router.get("/tg/link/{token}", response_class=HTMLResponse, include_in_schema=False)
async def link_page(
    request: Request,
    token: str,
    user: User | None = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_session),
) -> Response:
    state, reason, tg_username = _page_state(await svc.peek(db, token))
    if state == "ok" and user is None:
        nxt = quote(f"/tg/link/{token}", safe="")
        return RedirectResponse(f"/login?next={nxt}", status_code=303)
    if state != "ok":
        return HTMLResponse(_shell(f'<p class="err">{reason}</p>'))
    uname = f" @{tg_username}" if tg_username else ""
    body = (
        f'<p class="msg">Вы вошли как <b>{user.username}</b>.</p>'
        f"<p>Привязать этот Telegram{uname} к аккаунту?</p>"
        '<button id="confirm">Привязать</button>'
        '<div class="err" id="err"></div>'
        "<script>"
        'document.getElementById("confirm").onclick=async function(){'
        'this.disabled=true;'
        f'var r=await fetch("/tg/link/{token}/confirm",{{method:"POST",credentials:"same-origin"}});'
        'if(r.ok){location.href="/search?tg_linked=1";return;}'
        'var b=null;try{b=await r.json();}catch(e){}'
        'document.getElementById("err").textContent=(b&&b.detail)||"Не удалось привязать.";'
        "this.disabled=false;};"
        "</script>"
    )
    return HTMLResponse(_shell(body))


def _shell(inner: str) -> str:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        "<title>Привязка Telegram — DocsClassifier</title><style>"
        ":root{color-scheme:light dark}"
        "body{font:15px/1.5 system-ui,sans-serif;max-width:420px;margin:10vh auto;padding:0 16px}"
        "h1{font-size:1.2rem}.card{border:1px solid #8884;border-radius:10px;padding:20px}"
        "button{padding:9px 16px;border-radius:6px;border:0;background:#2563eb;color:#fff;"
        "cursor:pointer;font-size:1em}button:disabled{opacity:.6;cursor:default}"
        ".err{color:#dc2626;margin:8px 0;min-height:1.2em}.msg{margin:8px 0}"
        "</style></head><body><h1>Привязка Telegram</h1>"
        f'<div class="card">{inner}</div></body></html>'
    )
