"""Domain, membership, and invite endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import DomainCtx, require
from app.models import User
from app.rbac import Cap, Role
from app.schemas.domains import (
    DomainCreate,
    DomainOut,
    DomainUpdate,
    InviteCreate,
    InviteOut,
    MemberAdd,
    MemberOut,
    MemberUpdate,
)
from app.security import get_current_user
from app.services import domains as svc

router = APIRouter(tags=["domains"])


def _domain_out(domain, role: Role | str | None = None) -> DomainOut:
    out = DomainOut.model_validate(domain)
    out.my_role = Role(role) if role is not None else None
    return out


def _member_out(user: User, role: Role | str, added_at) -> MemberOut:
    return MemberOut(
        user_id=user.id,
        username=user.username,
        email=user.email,
        role=Role(role),
        added_at=added_at,
    )


@router.get("/domains", response_model=list[DomainOut])
async def list_domains(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)
) -> list[DomainOut]:
    return [
        _domain_out(domain, member.role)
        for domain, member in await svc.list_memberships(db, user)
    ]


@router.post("/domains", response_model=DomainOut, status_code=status.HTTP_201_CREATED)
async def create_domain(
    body: DomainCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> DomainOut:
    domain = await svc.create_domain(
        db, user, name=body.name, description=body.description
    )
    return _domain_out(domain, Role.owner)


@router.get("/domains/{domain_id}", response_model=DomainOut)
async def get_domain(ctx: DomainCtx = Depends(require(Cap.view))) -> DomainOut:
    return _domain_out(ctx.domain, ctx.role)


@router.patch("/domains/{domain_id}", response_model=DomainOut)
async def update_domain(
    body: DomainUpdate,
    ctx: DomainCtx = Depends(require(Cap.manage)),
    db: AsyncSession = Depends(get_session),
) -> DomainOut:
    if body.name is not None:
        ctx.domain.name = body.name
    if body.description is not None:
        ctx.domain.description = body.description or None
    if body.settings is not None:
        ctx.domain.settings = {**(ctx.domain.settings or {}), **body.settings}
    await db.flush()
    return _domain_out(ctx.domain, ctx.role)


@router.delete("/domains/{domain_id}")
async def delete_domain(
    ctx: DomainCtx = Depends(require(Cap.own)), db: AsyncSession = Depends(get_session)
) -> Response:
    await db.delete(ctx.domain)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/domains/{domain_id}/members", response_model=list[MemberOut])
async def list_members(
    ctx: DomainCtx = Depends(require(Cap.view)), db: AsyncSession = Depends(get_session)
) -> list[MemberOut]:
    return [
        _member_out(u, m.role, m.added_at)
        for m, u in await svc.list_members(db, ctx.domain.id)
    ]


@router.post(
    "/domains/{domain_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    body: MemberAdd,
    ctx: DomainCtx = Depends(require(Cap.manage)),
    db: AsyncSession = Depends(get_session),
) -> MemberOut:
    target = await db.scalar(select(User).where(User.username == body.username.lower()))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    try:
        member = await svc.add_or_update_member(
            db, ctx.domain, target, body.role, actor=ctx.user
        )
    except svc.DomainError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return _member_out(target, member.role, member.added_at)


@router.patch("/domains/{domain_id}/members/{member_user_id}", response_model=MemberOut)
async def update_member(
    member_user_id: uuid.UUID,
    body: MemberUpdate,
    ctx: DomainCtx = Depends(require(Cap.manage)),
    db: AsyncSession = Depends(get_session),
) -> MemberOut:
    target = await db.get(User, member_user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
    try:
        member = await svc.add_or_update_member(
            db, ctx.domain, target, body.role, actor=ctx.user
        )
    except svc.DomainError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return _member_out(target, member.role, member.added_at)


@router.delete("/domains/{domain_id}/members/{member_user_id}")
async def remove_member(
    member_user_id: uuid.UUID,
    ctx: DomainCtx = Depends(require(Cap.manage)),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await svc.remove_member(db, ctx.domain, member_user_id)
    except svc.DomainError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/domains/{domain_id}/invites",
    response_model=InviteOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    body: InviteCreate,
    ctx: DomainCtx = Depends(require(Cap.manage)),
    db: AsyncSession = Depends(get_session),
) -> InviteOut:
    try:
        invite = await svc.create_invite(
            db,
            ctx.domain,
            role=body.role,
            email=str(body.email) if body.email else None,
            username=body.username,
            actor=ctx.user,
        )
    except svc.DomainError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return InviteOut.model_validate(invite)


@router.post("/invites/{token}/accept", response_model=DomainOut)
async def accept_invite(
    token: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> DomainOut:
    try:
        domain = await svc.accept_invite(db, token, user)
    except svc.DomainError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    row = await svc.get_membership(db, domain.id, user.id)
    return _domain_out(domain, row[1].role if row else None)
