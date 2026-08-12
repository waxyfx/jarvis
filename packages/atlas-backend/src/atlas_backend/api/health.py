"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from atlas_backend import __version__
from atlas_backend.api.schemas import HealthResponse, ReadinessResponse
from atlas_backend.auth.deps import DbSession
from atlas_shared.protocol.envelope import PROTOCOL_VERSION

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is up. Deliberately touches nothing else."""
    return HealthResponse(status="ok", version=__version__, protocol_version=PROTOCOL_VERSION)


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(session: DbSession) -> ReadinessResponse:
    """Readiness: dependencies this process cannot serve traffic without."""
    await session.execute(text("SELECT 1"))
    return ReadinessResponse(
        status="ok",
        version=__version__,
        protocol_version=PROTOCOL_VERSION,
        database="ok",
    )
