"""Test fixtures — SQLite-backed, no external services needed."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DEBUG", "true")

from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import get_session
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
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def client_factory(engine) -> AsyncGenerator[Callable[[], AsyncClient]]:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _get_session():
        async with sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = _get_session
    clients: list[AsyncClient] = []

    def _make() -> AsyncClient:
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    yield _make

    for c in clients:
        await c.aclose()
    app.dependency_overrides.clear()


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
