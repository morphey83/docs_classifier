"""Web UI: domain members and invites (manage capability)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import DomainInvite, User
from app.rbac import ASSIGNABLE_ROLES, Cap, Role
from app.services import domains as svc
from app.web.csrf import CsrfGuard
from app.web.deps import DomainView, domain_by_slug, require_cap
from app.web.templating import render

router = APIRouter()


@router.get("/domains/{slug}/members")
async def members_page(
    request: Request,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    members = await svc.list_members(db, view.domain.id)
    invites = list(
        await db.scalars(
            select(DomainInvite)
            .where(DomainInvite.domain_id == view.domain.id, DomainInvite.accepted_at.is_(None))
            .order_by(DomainInvite.created_at.desc())
        )
    )
    return render(
        request,
        "members.html",
        {
            "view": view,
            "members": list(members),
            "invites": invites,
            "roles": [r.value for r in ASSIGNABLE_ROLES],
            "owner_id": view.domain.owner_id,
        },
    )


@router.post("/domains/{slug}/members")
async def member_add(
    request: Request,
    username: str = Form(...),
    role: str = Form(...),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    target = await db.scalar(select(User).where(User.username == username.strip().lower()))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "пользователь не найден")
    try:
        await svc.add_or_update_member(
            db, view.domain, target, Role(role), actor=request.state.user
        )
    except (svc.DomainError, ValueError) as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return RedirectResponse(f"/domains/{view.domain.slug}/members", status_code=303)


@router.post("/domains/{slug}/members/{user_id}")
async def member_role(
    request: Request,
    user_id: uuid.UUID,
    role: str = Form(...),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "пользователь не найден")
    try:
        await svc.add_or_update_member(
            db, view.domain, target, Role(role), actor=request.state.user
        )
    except (svc.DomainError, ValueError) as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return RedirectResponse(f"/domains/{view.domain.slug}/members", status_code=303)


@router.post("/domains/{slug}/members/{user_id}/remove")
async def member_remove(
    user_id: uuid.UUID,
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    try:
        await svc.remove_member(db, view.domain, user_id)
    except svc.DomainError as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return RedirectResponse(f"/domains/{view.domain.slug}/members", status_code=303)


@router.post("/domains/{slug}/invites")
async def invite_create(
    request: Request,
    username: str = Form(default=""),
    email: str = Form(default=""),
    role: str = Form(...),
    _: None = CsrfGuard,
    view: DomainView = Depends(domain_by_slug),
    db: AsyncSession = Depends(get_session),
) -> Response:
    require_cap(view, Cap.manage)
    try:
        await svc.create_invite(
            db, view.domain, role=Role(role),
            email=email.strip() or None, username=username.strip() or None,
            actor=request.state.user,
        )
    except (svc.DomainError, ValueError) as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
    return RedirectResponse(f"/domains/{view.domain.slug}/members", status_code=303)
