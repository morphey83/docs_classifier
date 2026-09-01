"""Ad-hoc exports and artifact download."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import get_session
from app.deps import DomainCtx, require
from app.jobs import dispatch
from app.models import Artifact, ArtifactStatus, User
from app.rbac import Cap
from app.schemas.exports import ArtifactOut, ExportCreate
from app.security import get_current_user
from app.services import domains as domains_svc
from app.services.export import build_artifact, create_export
from app.services.search import SearchFilters
from app.util.time import as_aware, utcnow

router = APIRouter(tags=["exports"])


@router.post(
    "/domains/{domain_id}/exports",
    response_model=ArtifactOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_export_route(
    body: ExportCreate,
    background: BackgroundTasks,
    ctx: DomainCtx = Depends(require(Cap.download)),
    db: AsyncSession = Depends(get_session),
) -> ArtifactOut:
    filters = None
    if body.document_ids is None:
        filters = SearchFilters(
            q=body.q,
            status=body.status,  # type: ignore[arg-type]
            tags_all=body.tags_all or [],
            tags_any=body.tags_any or [],
            tags_none=body.tags_none or [],
            ext=body.ext,
            mime=body.mime,
        )
    artifact = await create_export(
        db, ctx.domain, ctx.user, filters=filters, doc_ids=body.document_ids
    )
    out = ArtifactOut.model_validate(artifact)
    await db.commit()  # persist before the job (queue mode) picks it up
    await dispatch(background, "build_artifact", build_artifact, artifact_id=artifact.id)
    return out


async def _load_artifact(db: AsyncSession, artifact_id: uuid.UUID, user: User) -> Artifact:
    artifact = await db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    row = await domains_svc.get_membership(db, artifact.domain_id, user.id)
    if row is None or not row[1] or Cap.download not in _caps(row[1].role):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    return artifact


def _caps(role):
    from app.rbac import ROLE_CAPS, Role

    return ROLE_CAPS[Role(role)]


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
async def get_artifact(
    artifact_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ArtifactOut:
    return ArtifactOut.model_validate(await _load_artifact(db, artifact_id, user))


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> FileResponse:
    artifact = await _load_artifact(db, artifact_id, user)
    if artifact.status != ArtifactStatus.ready or not artifact.storage_key:
        raise HTTPException(status.HTTP_409_CONFLICT, f"artifact is {artifact.status}")
    if artifact.expires_at and as_aware(artifact.expires_at) <= utcnow():
        raise HTTPException(status.HTTP_410_GONE, "artifact has expired")
    path = storage.artifacts_dir() / artifact.storage_key
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "artifact file is gone")
    return FileResponse(path, media_type="application/zip", filename=f"export-{artifact.id}.zip")
