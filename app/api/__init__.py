"""API routers."""

from fastapi import APIRouter

from app.api import auth, documents, domains, exports, health, tags

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(domains.router)
api_router.include_router(tags.router)
api_router.include_router(documents.router)
api_router.include_router(exports.router)
