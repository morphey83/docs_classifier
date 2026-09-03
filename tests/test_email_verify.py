"""Registration email confirmation (only active when SMTP is configured).

The address is confirmed with a short numeric code the user types back on the
site — not a link.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import User

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


async def _sessions():
    from app.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        yield s


async def _code_for(username: str) -> str:
    row = None
    async for db in _sessions():
        row = (
            await db.execute(
                select(User.email_verify_code).where(User.username == username)
            )
        ).scalar_one()
    return row


def _csrf(page: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


async def test_api_register_gives_no_session_and_login_is_blocked(client, smtp_on):
    r = await client.post("/api/auth/register", json=REG)
    assert r.status_code == 201
    assert not client.cookies.get("dcsid")  # dormant account
    assert (await client.get("/api/auth/me")).status_code == 401

    bad = await client.post(
        "/api/auth/login", json={"login": "verner", "password": REG["password"]}
    )
    assert bad.status_code == 403


async def test_web_verify_code_activates_the_account_and_lands_on_upload(client, smtp_on):
    csrf = _csrf((await client.get("/register")).text)
    r = await client.post(
        "/register",
        data={**REG, "password2": REG["password"], "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 200 and "Проверьте почту" in r.text

    # login still blocked — it re-sends a code rather than letting us in
    lcsrf = _csrf((await client.get("/login")).text)
    blocked = await client.post(
        "/login",
        data={"login": "verner", "password": REG["password"], "csrf_token": lcsrf},
        follow_redirects=False,
    )
    assert blocked.status_code == 403 and "не подтверждён" in blocked.text

    code = await _code_for("verner")
    assert re.fullmatch(r"\d{6}", code)

    v = await client.post(
        "/verify",
        data={"email": REG["email"], "code": code, "csrf_token": lcsrf},
        follow_redirects=False,
    )
    assert v.status_code == 303 and v.headers["location"] == "/upload"
    assert client.cookies.get("dcsid")  # auto-signed-in


async def test_wrong_code_is_rejected(client, smtp_on):
    csrf = _csrf((await client.get("/register")).text)
    await client.post(
        "/register",
        data={**REG, "password2": REG["password"], "csrf_token": csrf},
        follow_redirects=False,
    )
    v = await client.post(
        "/verify",
        data={"email": REG["email"], "code": "000000", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert v.status_code == 400 and "устарел" in v.text
