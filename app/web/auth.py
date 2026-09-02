"""Web UI: login / register / logout / email verification (session cookie)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _client_ip, _set_session_cookie
from app.config import settings
from app.db import get_session
from app.models import User
from app.schemas.auth import RegisterIn
from app.services.email import make_verify_token, read_verify_token, send_verification_email
from app.services.users import (
    RegistrationError,
    authenticate,
    create_session,
    delete_session,
    email_unverified,
    get_user_by_login,
    mark_email_verified,
    register_user,
)
from app.web import csrf
from app.web.templating import render

router = APIRouter()


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


def _send_verification(background: BackgroundTasks, user: User) -> None:
    background.add_task(
        send_verification_email, user.email, user.username, make_verify_token(user.id)
    )


@router.get("/login")
async def login_form(request: Request, next: str | None = None) -> Response:
    if request.cookies.get(settings.session_cookie_name):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(
        request,
        "login.html",
        {"next": next, "verified": request.query_params.get("verified")},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    background: BackgroundTasks,
    login: str = Form(...),
    password: str = Form(...),
    next: str | None = Form(default=None),
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    if not csrf.valid(request, csrf_token):
        return render(request, "login.html", {"error": "Сессия устарела, попробуйте ещё раз."})
    user = await authenticate(db, login=login, password=password)
    if user is None:
        return render(
            request,
            "login.html",
            {"error": "Неверный логин или пароль.", "login": login, "next": next},
            status_code=401,
        )
    if email_unverified(user):
        _send_verification(background, user)
        return render(
            request,
            "login.html",
            {
                "error": f"Адрес не подтверждён. Мы отправили письмо на {user.email}.",
                "resend_login": login,
                "login": login,
                "next": next,
            },
            status_code=403,
        )
    session = await create_session(
        db, user, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )
    resp = RedirectResponse(_safe_next(next), status_code=303)
    _set_session_cookie(resp, session.id)
    return resp


@router.get("/register")
async def register_form(request: Request, next: str | None = None) -> Response:
    return render(request, "register.html", {"next": next})


@router.post("/register")
async def register_submit(
    request: Request,
    background: BackgroundTasks,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    next: str | None = Form(default=None),
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    ctx = {"username": username, "email": email, "next": next}
    if not csrf.valid(request, csrf_token):
        return render(request, "register.html", {**ctx, "error": "Сессия устарела."})
    try:
        data = RegisterIn(username=username, email=email, password=password)
    except ValidationError:
        return render(
            request,
            "register.html",
            {**ctx, "error": "Проверьте логин (латиница/цифры, 3+), email и пароль (8+ символов)."},
            status_code=400,
        )
    try:
        user = await register_user(
            db, username=data.username, email=data.email, password=data.password
        )
    except RegistrationError as err:
        return render(request, "register.html", {**ctx, "error": str(err)}, status_code=409)

    if email_unverified(user):
        await db.commit()
        _send_verification(background, user)
        return render(request, "verify_sent.html", {"email": user.email})

    session = await create_session(
        db, user, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )
    resp = RedirectResponse(_safe_next(next), status_code=303)
    _set_session_cookie(resp, session.id)
    return resp


@router.get("/verify/{token}")
async def verify_email(
    request: Request, token: str, db: AsyncSession = Depends(get_session)
) -> Response:
    user_id = read_verify_token(token)
    user = await db.get(User, user_id) if user_id else None
    if user is None:
        return render(
            request,
            "login.html",
            {"error": "Ссылка недействительна или истекла. Войдите, чтобы получить новую."},
            status_code=400,
        )
    await mark_email_verified(db, user)
    return RedirectResponse("/login?verified=1", status_code=303)


@router.post("/verify/resend")
async def verify_resend(
    request: Request,
    background: BackgroundTasks,
    login: str = Form(...),
    csrf_token: str = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    if csrf.valid(request, csrf_token):
        user = await get_user_by_login(db, login)
        if user is not None and email_unverified(user):
            _send_verification(background, user)
    # Always the same response — don't leak whether the account exists.
    return render(request, "verify_sent.html", {"email": login})


@router.post("/logout")
async def logout(
    request: Request,
    csrf_token: str = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    token = request.cookies.get(settings.session_cookie_name)
    if token and csrf.valid(request, csrf_token):
        await delete_session(db, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(settings.session_cookie_name, path="/")
    return resp
