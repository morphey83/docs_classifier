"""Async database engine, session factory, and the FastAPI session dependency."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def install_sqlite_unicode(engine: AsyncEngine) -> None:
    """SQLite's built-in ``lower``/``upper`` (and therefore ``ILIKE``) only fold
    ASCII, so a search for «реестр» never matches «Реестра». Swap in Python's
    Unicode-aware case folding. PostgreSQL (production) is already locale-aware."""
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _register(dbapi_connection, _record):
        for name, fn in (("lower", str.lower), ("upper", str.upper)):
            dbapi_connection.create_function(
                name, 1, lambda s, _fn=fn: _fn(s) if isinstance(s, str) else s,
                deterministic=True,
            )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,
        )
        install_sqlite_unicode(_engine)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _sessionmaker


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency: one transaction-scoped session per request."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
