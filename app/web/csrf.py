"""Signed, session-bound CSRF tokens (defense in depth over SameSite=Lax)."""

from __future__ import annotations

from fastapi import Depends, Form, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

_MAX_AGE = 60 * 60 * 24
_signer = URLSafeTimedSerializer(settings.secret_key, salt="dc-web-csrf")


def _session_id(request: Request) -> str:
    return request.cookies.get(settings.session_cookie_name, "anon")


def issue(request: Request) -> str:
    return _signer.dumps(_session_id(request))


def valid(request: Request, token: str | None) -> bool:
    if not token:
        return False
    try:
        return _signer.loads(token, max_age=_MAX_AGE) == _session_id(request)
    except BadSignature:
        return False


async def require_csrf(
    request: Request, csrf_token: str | None = Form(default=None)
) -> None:
    token = csrf_token or request.headers.get("X-CSRF-Token")
    if not valid(request, token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad or missing CSRF token")


CsrfGuard = Depends(require_csrf)
