"""REST surface, mounted under /v1."""

from fastapi import APIRouter

from atlas_backend.api import audit, auth, devices, health, pairing

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(pairing.router)
api_router.include_router(auth.router)
api_router.include_router(devices.router)
api_router.include_router(audit.router)

__all__ = ["api_router"]
