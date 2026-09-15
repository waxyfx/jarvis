#!/usr/bin/env bash
#
# Nightly backup of everything that cannot be regenerated.
#
#   ./deploy/backup.sh [directory]
#
# Install it as a cron job:
#   (crontab -l 2>/dev/null; echo "17 3 * * * $PWD/deploy/backup.sh") | crontab -
#
# What is worth keeping, and why:
#
#   the database   the audit chain, which is hash-linked and cannot be
#                  reconstructed; the device registry, without which every
#                  device re-pairs; and the activity history, which is the only
#                  record of how the owner's days actually went.
#
#   .env           the server signing key. Every paired device pinned its
#                  public half, so losing it means re-pairing all of them. It
#                  is a secret, so the copy is 0600 and stays on this host
#                  unless the operator moves it deliberately.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
TARGET="${1:-$HOME/jarvis-backups}"
KEEP_DAYS=30

[[ -f "$ENV_FILE" ]] || { echo "no .env at $ENV_FILE" >&2; exit 1; }

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

umask 077
mkdir -p "$TARGET"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

cd "$REPO_ROOT/infra"
$DOCKER compose --env-file "$ENV_FILE" exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom \
    > "$TARGET/atlas-$STAMP.dump"

cp "$ENV_FILE" "$TARGET/env-$STAMP"
chmod 600 "$TARGET/atlas-$STAMP.dump" "$TARGET/env-$STAMP"

# A backup that is empty is worse than none: it looks like a backup.
SIZE="$(stat -c %s "$TARGET/atlas-$STAMP.dump")"
[[ "$SIZE" -gt 4096 ]] || { echo "dump is only $SIZE bytes; refusing to rotate" >&2; exit 1; }

find "$TARGET" -name 'atlas-*.dump' -mtime "+$KEEP_DAYS" -delete
find "$TARGET" -name 'env-*' -mtime "+$KEEP_DAYS" -delete

echo "$TARGET/atlas-$STAMP.dump ($SIZE bytes)"
