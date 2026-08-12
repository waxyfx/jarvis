"""Application factory.

Everything the app needs lives on ``app.state`` and is created in the lifespan,
so a test can build an isolated instance with its own database and services
without patching module globals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from atlas_backend import __version__
from atlas_backend.api import api_router
from atlas_backend.auth.challenge import ChallengeService
from atlas_backend.auth.pairing import PairingService
from atlas_backend.auth.tokens import TokenService
from atlas_backend.config import Settings, get_settings
from atlas_backend.db.session import Database
from atlas_backend.errors import install_exception_handlers
from atlas_backend.logging import configure_logging, get_logger
from atlas_backend.ratelimit import SlidingWindowLimiter
from atlas_backend.ws import Hub, ws_router

__all__ = ["create_app"]

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json_output=resolved.is_production)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.database = Database(resolved)
        app.state.token_service = TokenService(resolved)
        app.state.challenge_service = ChallengeService(resolved)
        app.state.pairing_service = PairingService(resolved)
        app.state.hub = Hub()
        app.state.pairing_limiter = SlidingWindowLimiter(
            limit=resolved.pairing_rate_limit_per_minute, window_s=60.0
        )
        app.state.auth_limiter = SlidingWindowLimiter(
            limit=resolved.pairing_rate_limit_per_minute, window_s=60.0
        )

        log.info(
            "backend_started",
            version=__version__,
            environment=resolved.environment,
            bootstrap_open=resolved.bootstrap_token is not None,
        )
        try:
            yield
        finally:
            closed = await app.state.hub.close_all()
            await app.state.database.dispose()
            log.info("backend_stopped", connections_closed=closed)

    app = FastAPI(
        title="ATLAS Backend",
        version=__version__,
        lifespan=lifespan,
        # The schema is not secret, but a private service has no reason to serve
        # an interactive console in production.
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_production else "/openapi.json",
    )

    install_exception_handlers(app)
    app.include_router(api_router)
    app.include_router(ws_router)
    return app
