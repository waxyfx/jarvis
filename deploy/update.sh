#!/usr/bin/env bash
#
# Deploy a new version onto a VPS that vps-setup.sh has already set up.
#
#   ./deploy/update.sh
#
# Pull, rebuild, migrate, restart, check. Stops at the first failure, and the
# order is the careful one: migrations run against the new image *before* the
# old containers are replaced, so a migration that fails leaves the previous
# version serving rather than a half-updated one.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok() { printf '    \033[0;32m%s\033[0m\n' "$*"; }
die() { printf '\n\033[0;31mstopped:\033[0m %s\n\n' "$*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "no .env — run deploy/vps-setup.sh first"

DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

say "source"
cd "$REPO_ROOT"
BEFORE="$(git rev-parse --short HEAD)"
git pull --ff-only
AFTER="$(git rev-parse --short HEAD)"
[[ "$BEFORE" == "$AFTER" ]] && ok "already at $AFTER" || ok "$BEFORE -> $AFTER"

say "build"
cd "$REPO_ROOT/infra"
$DOCKER compose --env-file "$ENV_FILE" build backend
ok "image built"

say "migrations"
# Against the new image, before the running containers are replaced: a
# migration that fails should leave the old version serving.
$DOCKER compose --env-file "$ENV_FILE" run --rm --no-deps backend \
    alembic -c /app/alembic.ini upgrade head
ok "schema at head"

say "restart"
$DOCKER compose --env-file "$ENV_FILE" up -d
ok "running"

say "health"
for attempt in $(seq 1 30); do
    if $DOCKER compose --env-file "$ENV_FILE" exec -T backend python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health/ready')" \
        >/dev/null 2>&1; then
        ok "answering"
        exit 0
    fi
    sleep 2
done

die "the backend did not come back — $DOCKER compose logs --tail 50 backend"
