"""Domain, membership, and invitation logic."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Domain, DomainInvite, DomainMember, User
from app.rbac import Role
from app.util.slug import unique_slug
from app.util.time import as_aware, utcnow

INVITE_TTL_DAYS = 14


class DomainError(ValueError):
    pass


async def create_domain(
    db: AsyncSession, owner: User, *, name: str, description: str | None = None
) -> Domain:
    existing = set(await db.scalars(select(Domain.slug)))
    slug = unique_slug(name, existing.__contains__)
    domain = Domain(name=name, slug=slug, owner_id=owner.id, description=description)
    db.add(domain)
    await db.flush()
    db.add(DomainMember(domain_id=domain.id, user_id=owner.id, role=Role.owner, added_by=owner.id))
    await db.flush()
    return domain


async def list_memberships(db: AsyncSession, user: User) -> list[tuple[Domain, DomainMember]]:
    rows = await db.execute(
        select(Domain, DomainMember)
        .join(DomainMember, DomainMember.domain_id == Domain.id)
        .where(DomainMember.user_id == user.id)
        .order_by(Domain.name)
    )
    return list(rows.all())


async def get_membership(
    db: AsyncSession, domain_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Domain, DomainMember] | None:
    row = await db.execute(
        select(Domain, DomainMember)
        .join(DomainMember, DomainMember.domain_id == Domain.id)
        .where(Domain.id == domain_id, DomainMember.user_id == user_id)
    )
    return row.first()  # type: ignore[return-value]


async def list_members(db: AsyncSession, domain_id: uuid.UUID) -> list[tuple[DomainMember, User]]:
    rows = await db.execute(
        select(DomainMember, User)
        .join(User, User.id == DomainMember.user_id)
        .where(DomainMember.domain_id == domain_id)
        .order_by(User.username)
    )
    return list(rows.all())


async def add_or_update_member(
    db: AsyncSession, domain: Domain, target: User, role: Role, *, actor: User
) -> DomainMember:
    if role == Role.owner:
        raise DomainError("assign ownership via transfer, not a role change")
    member = await db.get(DomainMember, (domain.id, target.id))
    if member is None:
        member = DomainMember(
            domain_id=domain.id, user_id=target.id, role=role, added_by=actor.id
        )
        db.add(member)
    else:
        if member.role == Role.owner:
            raise DomainError("the owner's role cannot be changed here")
        member.role = role
    await db.flush()
    return member


async def remove_member(db: AsyncSession, domain: Domain, user_id: uuid.UUID) -> None:
    if user_id == domain.owner_id:
        raise DomainError("the owner cannot be removed")
    member = await db.get(DomainMember, (domain.id, user_id))
    if member is not None:
        await db.delete(member)


async def create_invite(
    db: AsyncSession,
    domain: Domain,
    *,
    role: Role,
    email: str | None,
    username: str | None,
    actor: User,
) -> DomainInvite:
    if role == Role.owner:
        raise DomainError("cannot invite as owner")
    if not email and not username:
        raise DomainError("email or username required")
    invite = DomainInvite(
        domain_id=domain.id,
        email=email.lower() if email else None,
        username=username.lower() if username else None,
        role=role,
        token=secrets.token_urlsafe(24),
        created_by=actor.id,
        expires_at=utcnow() + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(invite)
    await db.flush()
    return invite


async def accept_invite(db: AsyncSession, token: str, user: User) -> Domain:
    invite = await db.scalar(select(DomainInvite).where(DomainInvite.token == token))
    if invite is None or invite.accepted_at is not None:
        raise DomainError("invite not found or already used")
    if as_aware(invite.expires_at) <= utcnow():
        raise DomainError("invite expired")
    if invite.email and invite.email != user.email:
        raise DomainError("invite was issued for a different email")
    if invite.username and invite.username != user.username:
        raise DomainError("invite was issued for a different account")

    domain = await db.get(Domain, invite.domain_id)
    if domain is None:
        raise DomainError("domain no longer exists")

    existing = await db.get(DomainMember, (domain.id, user.id))
    if existing is None:
        db.add(
            DomainMember(
                domain_id=domain.id, user_id=user.id, role=invite.role, added_by=invite.created_by
            )
        )
    invite.accepted_at = utcnow()
    invite.accepted_by = user.id
    await db.flush()
    return domain
