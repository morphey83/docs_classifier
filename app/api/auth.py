"""Registration, login, logout, current user."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import User
from app.schemas.auth import LoginIn, RegisterIn, UserOut
from app.schemas.tglink import TgLinkCreateOut
from app.security import get_current_user
from app.services import tglink as tglink_svc
from app.services.email import send_verification_email
from app.services.users import (
    RegistrationError,
    authenticate,
    create_session,
    delete_session,
    email_unverified,
    issue_verify_code,
    register_user,
)
from app.util.urls import bot_deep_link

router = APIRouter()


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterIn,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
) -> User:
    try:
        user = await register_user(
            db, username=body.username, email=body.email, password=body.password
        )
    except RegistrationError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    if email_unverified(user):
        code = await issue_verify_code(db, user)
        email, username = user.email, user.username
        await db.commit()
        background.add_task(send_verification_email, email, username, code)
        return user  # no session — dormant until the code is confirmed
    session = await create_session(
        db, user, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )
    _set_session_cookie(response, session.id)
    return user


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> User:
    user = await authenticate(db, login=body.login, password=body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if email_unverified(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "email not verified")
    session = await create_session(
        db, user, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )
    _set_session_cookie(response, session.id)
    return user


@router.post("/logout")
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await delete_session(db, token)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"status": "logged out"}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/tg-link", response_model=TgLinkCreateOut, status_code=status.HTTP_201_CREATED)
async def create_tg_link(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
) -> TgLinkCreateOut:
    """Web-initiated Telegram linking: returns a token + bot deep-link (§8)."""
    if user.tg_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this account already has a linked Telegram")
    tok = await tglink_svc.create_web_initiated(db, user)
    return TgLinkCreateOut(token=tok.token, deep_link=bot_deep_link(tok.token))
