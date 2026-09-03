"""Web UI: login / register / logout / email confirmation (session cookie)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _client_ip, _set_session_cookie
from app.config import settings
from app.db import get_session
from app.schemas.auth import RegisterIn
from app.services.email import send_verification_email
from app.services.users import (
    RegistrationError,
    authenticate,
    confirm_email_code,
    create_session,
    delete_session,
    email_unverified,
    get_user_by_login,
    issue_verify_code,
    register_user,
)
from app.web import csrf
from app.web.templating import render

router = APIRouter()

# per-field hints shown under an invalid registration input
_FIELD_HINT = {
    "username": "Только латинские буквы, цифры, точка, дефис и подчёркивание; от 3 до 64 символов.",
    "email": "Введите корректный адрес, например you@example.com.",
    "password": "От 8 до 256 символов.",
}


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


def _post_auth_dest(nxt: str | None) -> str:
    """Where to land a freshly signed-in user: an explicit ``next`` if given
    (e.g. the Telegram-linking page), otherwise straight to the upload page."""
    dest = _safe_next(nxt)
    return "/upload" if dest == "/" else dest


async def _send_code(background: BackgroundTasks, db: AsyncSession, user) -> None:
    """Mint a confirmation code, commit it, and queue the email."""
    code = await issue_verify_code(db, user)
    email, username = user.email, user.username
    await db.commit()
    background.add_task(send_verification_email, email, username, code)


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
        await _send_code(background, db, user)
        return render(
            request,
            "verify_sent.html",
            {
                "email": user.email,
                "next": next,
                "notice": "Адрес ещё не подтверждён — мы отправили новый код.",
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
    password2: str = Form(default=""),
    next: str | None = Form(default=None),
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    ctx = {"username": username, "email": email, "next": next}
    if not csrf.valid(request, csrf_token):
        return render(request, "register.html", {**ctx, "error": "Сессия устарела."})

    field_errors: dict[str, str] = {}
    data: RegisterIn | None = None
    try:
        data = RegisterIn(username=username, email=email, password=password)
    except ValidationError as exc:
        for err in exc.errors():
            loc = str(err["loc"][-1]) if err["loc"] else ""
            field_errors.setdefault(loc, _FIELD_HINT.get(loc, "Проверьте значение."))
    if password != password2:
        field_errors["password2"] = "Пароли не совпадают."
    if field_errors or data is None:
        return render(
            request, "register.html", {**ctx, "field_errors": field_errors}, status_code=400
        )

    try:
        user = await register_user(
            db, username=data.username, email=data.email, password=data.password
        )
    except RegistrationError:
        fe: dict[str, str] = {}
        if await get_user_by_login(db, data.username):
            fe["username"] = "Этот логин уже занят."
        if await get_user_by_login(db, data.email):
            fe["email"] = "Этот email уже зарегистрирован."
        if not fe:
            fe["username"] = "Логин или email уже заняты."
        return render(request, "register.html", {**ctx, "field_errors": fe}, status_code=409)

    if email_unverified(user):
        await _send_code(background, db, user)
        return render(request, "verify_sent.html", {"email": user.email, "next": next})

    session = await create_session(
        db, user, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )
    resp = RedirectResponse(_post_auth_dest(next), status_code=303)
    _set_session_cookie(resp, session.id)
    return resp


@router.post("/verify")
async def verify_code(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(...),
    code: str = Form(...),
    next: str | None = Form(default=None),
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    base = {"email": email, "next": next}
    if not csrf.valid(request, csrf_token):
        return render(request, "verify_sent.html", {**base, "error": "Сессия устарела."})
    user = await get_user_by_login(db, email)
    if user is None or not await confirm_email_code(db, user, code):
        return render(
            request,
            "verify_sent.html",
            {**base, "error": "Код неверен или устарел. Запросите новый."},
            status_code=400,
        )
    session = await create_session(
        db, user, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )
    resp = RedirectResponse(_post_auth_dest(next), status_code=303)
    _set_session_cookie(resp, session.id)
    return resp


@router.post("/verify/resend")
async def verify_resend(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(...),
    next: str | None = Form(default=None),
    csrf_token: str = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    if csrf.valid(request, csrf_token):
        user = await get_user_by_login(db, email)
        if user is not None and email_unverified(user):
            await _send_code(background, db, user)
    # Always the same response — don't leak whether the account exists.
    return render(
        request,
        "verify_sent.html",
        {"email": email, "next": next, "notice": "Отправили код ещё раз."},
    )


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
