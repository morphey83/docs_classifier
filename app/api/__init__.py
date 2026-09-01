"""API routers."""

from fastapi import APIRouter

from app.api import auth, docsets, documents, domains, exports, search, tags

# The JSON API — mounted under /api by app.main (§ "URL layout", architecture §6).
api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(domains.router)
api_router.include_router(tags.router)
api_router.include_router(documents.router)
api_router.include_router(exports.router)
api_router.include_router(docsets.router)
api_router.include_router(search.router)
