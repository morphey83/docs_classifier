"""Test fixtures — SQLite-backed, no external services needed."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("JOB_MODE", "inline")
# Neutralise deployment-only settings that a local dev .env might carry, so
# tests see stock defaults regardless of the developer's .env.
os.environ.setdefault("PUBLIC_BASE_URL", "")
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "")
os.environ.setdefault("BOT_TOKEN", "")
os.environ.setdefault("DEFAULT_ALLOWED_TYPES", "")

from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db as db_module
from app.config import settings
from app.main import app
from app.models import Base


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path):
    old = settings.data_dir
    settings.data_dir = tmp_path / "data"
    yield
    settings.data_dir = old


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Point the whole app (request deps *and* background-task sessions) at it.
    prev_e, prev_s = db_module._engine, db_module._sessionmaker
    db_module._engine = eng
    db_module._sessionmaker = async_sessionmaker(eng, expire_on_commit=False, autoflush=False)
    yield eng
    db_module._engine, db_module._sessionmaker = prev_e, prev_s
    await eng.dispose()


@pytest_asyncio.fixture
async def client_factory(engine) -> AsyncGenerator[Callable[[], AsyncClient]]:
    clients: list[AsyncClient] = []

    def _make() -> AsyncClient:
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.aclose()


@pytest_asyncio.fixture
async def client(client_factory) -> AsyncClient:
    return client_factory()


async def register(c: AsyncClient, username: str, password: str = "correct horse!") -> dict:
    r = await c.post(
        "/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest_asyncio.fixture
async def alice(client_factory) -> AsyncClient:
    c = client_factory()
    await register(c, "alice")
    return c


@pytest_asyncio.fixture
async def bob(client_factory) -> AsyncClient:
    c = client_factory()
    await register(c, "bob")
    return c
