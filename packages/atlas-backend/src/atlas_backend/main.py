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
from atlas_backend.ai import AIProvider, Assistant, GeminiProvider
from atlas_backend.api import api_router
from atlas_backend.auth.challenge import ChallengeService
from atlas_backend.auth.pairing import PairingService
from atlas_backend.auth.tokens import TokenService
from atlas_backend.config import Settings, get_settings
from atlas_backend.db.session import Database
from atlas_backend.errors import install_exception_handlers
from atlas_backend.logging import configure_logging, get_logger
from atlas_backend.notify import Notifier, ProactiveScheduler
from atlas_backend.policy import ToolDispatcher
from atlas_backend.policy.service import prayer_settings_from
from atlas_backend.ratelimit import SlidingWindowLimiter
from atlas_backend.server_identity import ServerIdentity
from atlas_backend.tracker.provider import TrackerProvider, TrackerUnavailableError
from atlas_backend.tracker.sunny import SunnyTracker
from atlas_backend.web.tools import WebTools
from atlas_backend.ws import Hub, ws_router

__all__ = ["create_app"]

log = get_logger(__name__)


def _build_tracker(settings: Settings) -> TrackerProvider | None:
    """The tracker, if one is configured. Absent is the normal state.

    Both halves are required and neither is guessed: without a URL there is
    nowhere to ask, and without a token Sunny would refuse anyway. Returning
    ``None`` removes the tracker tools from what the model is offered, so
    nothing has to explain itself later.
    """
    token = settings.sunny_token.get_secret_value() if settings.sunny_token else ""
    if not settings.sunny_base_url or not token:
        return None
    try:
        return SunnyTracker(
            base_url=settings.sunny_base_url,
            token=token,
            timeout_s=settings.sunny_timeout_s,
        )
    except TrackerUnavailableError as exc:
        # A misconfigured tracker must not stop the backend starting: the rest
        # of the assistant works without it, and a refusal to boot would take
        # the machine down over an optional integration.
        log.warning("tracker_unavailable", reason=str(exc))
        return None


def create_app(
    settings: Settings | None = None,
    *,
    ai_provider: AIProvider | None = None,
    tracker: TrackerProvider | None = None,
    web: WebTools | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        ai_provider: Overrides the configured provider. Used by tests to drive
            the pipeline with scripted model responses — the only way to make
            adversarial cases deterministic.
        tracker: Overrides the configured tracker, for the same reason. Without
            it a test would need a running Next.js app to prove that a tracker
            call does not go to the agent.
        web: Overrides the web tools, so a test can assert what was searched and
            fetched without reaching the internet — and so no test run can put
            traffic on someone else's server.
    """
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json_output=resolved.is_production)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.database = Database(resolved)
        app.state.server_identity = ServerIdentity(resolved)
        app.state.token_service = TokenService(resolved)
        app.state.challenge_service = ChallengeService(resolved)
        app.state.pairing_service = PairingService(resolved)
        app.state.hub = Hub()
        resolved_tracker = tracker or _build_tracker(resolved)
        app.state.dispatcher = ToolDispatcher(
            hub=app.state.hub,
            server_identity=app.state.server_identity,
            settings=resolved,
            tracker=resolved_tracker,
            web=web,
        )
        app.state.pairing_limiter = SlidingWindowLimiter(
            limit=resolved.pairing_rate_limit_per_minute, window_s=60.0
        )
        app.state.auth_limiter = SlidingWindowLimiter(
            limit=resolved.pairing_rate_limit_per_minute, window_s=60.0
        )

        app.state.notifier = Notifier(
            hub=app.state.hub,
            server_identity=app.state.server_identity,
            database=app.state.database,
        )
        app.state.scheduler = (
            ProactiveScheduler(
                hub=app.state.hub,
                notifier=app.state.notifier,
                database=app.state.database,
                settings=resolved,
                tracker=resolved_tracker,
                prayer=prayer_settings_from(resolved),
            )
            if resolved.proactive_enabled
            else None
        )
        if app.state.scheduler is not None:
            app.state.scheduler.start()

        provider = ai_provider
        if provider is None and resolved.gemini_api_key is not None:
            provider = GeminiProvider(resolved)
        app.state.ai_provider = provider
        app.state.assistant = (
            Assistant(provider=provider, dispatcher=app.state.dispatcher, settings=resolved)
            if provider is not None
            else None
        )

        log.info(
            "backend_started",
            version=__version__,
            environment=resolved.environment,
            bootstrap_open=resolved.bootstrap_token is not None,
            # The key itself is never logged; only whether one is present.
            ai_provider=provider.name if provider else "none",
            ai_model=provider.model if provider else "",
        )
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                await app.state.scheduler.stop()
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
