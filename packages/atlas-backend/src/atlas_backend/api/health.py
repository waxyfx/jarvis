"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from atlas_backend import __version__
from atlas_backend.api.schemas import HealthResponse, ReadinessResponse, ServerIdentityResponse
from atlas_backend.auth.deps import DbSession
from atlas_shared.protocol.envelope import PROTOCOL_VERSION

router = APIRouter(tags=["health"])


@router.get("/server/identity", response_model=ServerIdentityResponse)
async def server_identity(request: Request) -> ServerIdentityResponse:
    """The public key devices use to verify commands.

    Public by design — it verifies signatures, it does not make them. Exposed so
    an already-paired device can confirm the key it pinned still matches, and so
    a mismatch surfaces as a clear error rather than silent refusals.
    """
    return ServerIdentityResponse(public_key=request.app.state.server_identity.public_key_b64)


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
