"""Registration email confirmation (only active when SMTP is configured)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.email import make_verify_token

REG = {"username": "Verner", "email": "verner@example.com", "password": "correct horse!"}


@pytest.fixture
def smtp_on(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    # swallow the outbound send so no network / no aiosmtplib import
    import app.services.email as email_mod

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(email_mod, "send_email", _noop)
    yield


async def test_api_register_gives_no_session_and_login_is_blocked(client, smtp_on):
    r = await client.post("/api/auth/register", json=REG)
    assert r.status_code == 201
    assert not client.cookies.get("dcsid")  # dormant account
    assert (await client.get("/api/auth/me")).status_code == 401

    bad = await client.post(
        "/api/auth/login", json={"login": "verner", "password": REG["password"]}
    )
    assert bad.status_code == 403


async def test_web_verify_link_activates_the_account(client, smtp_on):
    page = (await client.get("/register")).text
    import re

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = await client.post(
        "/register",
        data={**REG, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 200 and "Проверьте почту" in r.text

    # login still blocked
    lp = (await client.get("/login")).text
    lcsrf = re.search(r'name="csrf_token" value="([^"]+)"', lp).group(1)
    blocked = await client.post(
        "/login",
        data={"login": "verner", "password": REG["password"], "csrf_token": lcsrf},
        follow_redirects=False,
    )
    assert blocked.status_code == 403 and "не подтверждён" in blocked.text

    # find the user, mint its token, hit the verify route
    from sqlalchemy import select

    from app.models import User

    async for db in _sessions():
        uid = (await db.execute(select(User.id).where(User.username == "verner"))).scalar_one()
    v = await client.get(f"/verify/{make_verify_token(uid)}", follow_redirects=False)
    assert v.status_code == 303 and v.headers["location"] == "/login?verified=1"

    ok = await client.post(
        "/login",
        data={"login": "verner", "password": REG["password"], "csrf_token": lcsrf},
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/"


async def _sessions():
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        yield s
