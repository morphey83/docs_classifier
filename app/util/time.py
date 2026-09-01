"""Datetime helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def as_aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; treat a naive value as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)
