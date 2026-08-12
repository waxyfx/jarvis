# ATLAS backend image.
#
# NOT YET VERIFIED: Docker is not installed on the development machine, so this
# file has been written but never built. Treat the first `docker compose build`
# as part of M1 acceptance on the VPS, not as a step known to work.

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

# Dependency layer: changes only when the lockfile does.
COPY pyproject.toml uv.lock ./
COPY packages/atlas-shared/pyproject.toml packages/atlas-shared/
COPY packages/atlas-backend/pyproject.toml packages/atlas-backend/
COPY packages/atlas-agent-windows/pyproject.toml packages/atlas-agent-windows/
RUN uv sync --frozen --no-install-workspace --no-dev

# Source layer.
COPY packages/atlas-shared packages/atlas-shared
COPY packages/atlas-backend packages/atlas-backend
RUN uv sync --frozen --no-dev --no-editable \
        --package atlas-shared --package atlas-backend


FROM python:3.12-slim-bookworm

RUN groupadd --system atlas && useradd --system --gid atlas --create-home atlas

COPY --from=builder --chown=atlas:atlas /build/.venv /app/.venv
COPY --chown=atlas:atlas packages/atlas-backend/alembic.ini /app/alembic.ini
COPY --chown=atlas:atlas packages/atlas-backend/migrations /app/migrations

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
USER atlas

EXPOSE 8000

# Migrations run explicitly (see docs/runbook.md), not on container start:
# an automatic migration on boot turns a rollback into a data-loss event.
CMD ["uvicorn", "atlas_backend.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
