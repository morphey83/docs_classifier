"""Shared access helpers for bot handlers — mirror the API's capability checks."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, Domain, User
from app.rbac import ROLE_CAPS, Cap, Role
from app.services import domains as domains_svc


class BotAccessError(Exception):
    """Raised when the linked user lacks rights / the target is out of reach."""


async def domain_ctx(
    db: AsyncSession, user: User, domain_id: uuid.UUID
) -> tuple[Domain, Role, frozenset[Cap]]:
    row = await domains_svc.get_membership(db, domain_id, user.id)
    if row is None:
        raise BotAccessError("Домен недоступен.")
    domain, member = row
    role = Role(member.role)
    return domain, role, ROLE_CAPS[role]


async def doc_ctx(
    db: AsyncSession, user: User, document_id: uuid.UUID, *, need: Cap = Cap.view
) -> tuple[Document, Domain, frozenset[Cap]]:
    doc = await db.get(Document, document_id)
    if doc is None:
        raise BotAccessError("Документ не найден.")
    domain, _role, caps = await domain_ctx(db, user, doc.domain_id)
    if need not in caps:
        raise BotAccessError(f"Недостаточно прав ({need}) в домене «{domain.name}».")
    return doc, domain, caps


async def member_domain_ids(db: AsyncSession, user: User) -> list[uuid.UUID]:
    return [d.id for d, _ in await domains_svc.list_memberships(db, user)]


async def member_domain_names(db: AsyncSession, user: User) -> dict[uuid.UUID, str]:
    return {d.id: d.name for d, _ in await domains_svc.list_memberships(db, user)}
