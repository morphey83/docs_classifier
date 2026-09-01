"""Web UI: login / register / logout (session cookie)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _client_ip, _set_session_cookie
from app.config import settings
from app.db import get_session
from app.schemas.auth import RegisterIn
from app.services.users import (
    RegistrationError,
    authenticate,
    create_session,
    delete_session,
    register_user,
)
from app.web import csrf
from app.web.templating import render

router = APIRouter()


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


@router.get("/login")
async def login_form(request: Request, next: str | None = None) -> Response:
    if request.cookies.get(settings.session_cookie_name):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next": next})


@router.post("/login")
async def login_submit(
    request: Request,
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
    session = await create_session(
        db, user, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
    )
    resp = RedirectResponse(_safe_next(next), status_code=303)
    _set_session_cookie(resp, session.id)
    return resp


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
