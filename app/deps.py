"""Shared FastAPI dependencies for domain-scoped, capability-checked routes."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Document, Domain, DomainMember, User
from app.rbac import Cap, Role, role_has
from app.security import get_current_user
from app.services import domains as domains_svc


@dataclass
class DomainCtx:
    domain: Domain
    member: DomainMember
    user: User

    @property
    def role(self) -> Role:
        return Role(self.member.role)

    def has(self, cap: Cap) -> bool:
        return role_has(self.role, cap)


@dataclass
class DocCtx(DomainCtx):
    document: Document


def _check_caps(ctx: DomainCtx, caps: tuple[Cap, ...]) -> None:
    for cap in caps:
        if not ctx.has(cap):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires '{cap}' in this domain")


def require(*caps: Cap) -> Callable[..., Awaitable[DomainCtx]]:
    async def dep(
        domain_id: uuid.UUID = Path(...),
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_session),
    ) -> DomainCtx:
        row = await domains_svc.get_membership(db, domain_id, user.id)
        if row is None:
            # Non-members get 404, not 403 — don't reveal that the domain exists.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "domain not found")
        ctx = DomainCtx(domain=row[0], member=row[1], user=user)
        _check_caps(ctx, caps)
        return ctx

    return dep


def require_doc(*caps: Cap) -> Callable[..., Awaitable[DocCtx]]:
    async def dep(
        document_id: uuid.UUID = Path(...),
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_session),
    ) -> DocCtx:
        document = await db.get(Document, document_id)
        if document is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
        row = await domains_svc.get_membership(db, document.domain_id, user.id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
        ctx = DocCtx(domain=row[0], member=row[1], user=user, document=document)
        _check_caps(ctx, caps)
        return ctx

    return dep
