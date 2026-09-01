"""Web-UI dependencies: the signed-in user (redirecting when absent) and domains."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Domain, DomainMember, User
from app.rbac import ROLE_CAPS, Cap, Role
from app.security import get_current_user_optional
from app.services import domains as domains_svc


class AuthRequired(Exception):
    """Caught by a handler in app.main → 303 redirect to /login?next=…"""

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


async def current_user(
    request: Request, user: User | None = Depends(get_current_user_optional)
) -> User:
    if user is None:
        raise AuthRequired(request.url.path)
    request.state.user = user
    return user


@dataclass
class DomainView:
    domain: Domain
    role: Role
    caps: frozenset[Cap]

    def has(self, cap: Cap) -> bool:
        return cap in self.caps


async def domain_by_slug(
    slug: str,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> DomainView:
    row = await db.execute(
        select(Domain, DomainMember)
        .join(DomainMember, DomainMember.domain_id == Domain.id)
        .where(Domain.slug == slug, DomainMember.user_id == user.id)
    )
    hit = row.first()
    if hit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "domain not found")
    domain, member = hit
    role = Role(member.role)
    return DomainView(domain=domain, role=role, caps=ROLE_CAPS[role])


async def memberships(user: User, db: AsyncSession):
    return await domains_svc.list_memberships(db, user)


def require_cap(view: DomainView, cap: Cap) -> None:
    if not view.has(cap):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires '{cap}' in this domain")


async def load_document(db: AsyncSession, user: User, document_id: uuid.UUID):
    from app.models import Document

    doc = await db.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    row = await domains_svc.get_membership(db, doc.domain_id, user.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    domain, member = row
    role = Role(member.role)
    return doc, DomainView(domain=domain, role=role, caps=ROLE_CAPS[role])
