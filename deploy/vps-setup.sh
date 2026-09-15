#!/usr/bin/env bash
#
# Bring JARVIS up on a fresh VPS. Run it once, on the server, as a user with
# sudo. Everything after that is `deploy/update.sh`.
#
#   curl -fsSL https://raw.githubusercontent.com/waxyfx/jarvis/main/deploy/vps-setup.sh | bash
#
# ...is deliberately NOT how to run this. Clone the repository and read the file
# first: it generates the keys that every one of your devices will pin, and
# piping a script into a shell is the wrong habit to have about that.
#
#   git clone https://github.com/waxyfx/jarvis.git && cd jarvis
#   ./deploy/vps-setup.sh atlas.your-domain.com
#
# What it does, in order, stopping at the first thing that fails:
#
#   1. installs Docker if it is missing
#   2. generates every secret, into .env, with 0600 permissions
#   3. builds and starts postgres, the backend and Caddy
#   4. applies migrations — explicitly, never on container start
#   5. waits for the health check and prints the one thing you need next
#
# Nothing it writes goes into git: .env is ignored and chmod 0600. No secret is
# ever printed — the one thing it shows you is a pairing code, which is
# single-use, expires in minutes and is useless without a device key.

set -euo pipefail

DOMAIN="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
ok() { printf '    \033[0;32m%s\033[0m\n' "$*"; }
die() { printf '\n\033[0;31mstopped:\033[0m %s\n\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- arguments

if [[ -z "$DOMAIN" ]]; then
    cat >&2 <<'USAGE'
Usage: ./deploy/vps-setup.sh <domain>

  <domain>  the name an A record already points at this machine, e.g.
            jarvis.example.com. Caddy needs it to obtain a certificate, and
            it cannot be changed later without re-pairing every device.

Point the DNS record first and let it propagate. A certificate request against
a name that does not resolve here counts against Let's Encrypt's rate limit.
USAGE
    exit 2
fi

[[ "$DOMAIN" =~ ^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]] || die "'$DOMAIN' is not a domain name"

# --------------------------------------------------------------------- docker

say "docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "already installed"
else
    command -v apt-get >/dev/null 2>&1 || die "this installs Docker with apt; install it yourself and re-run"
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo usermod -aG docker "$USER" || true
    ok "installed — you may need to log out and back in for group membership"
fi

DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

# -------------------------------------------------------------------- secrets

say "secrets"
if [[ -f "$ENV_FILE" ]]; then
    # Never silently regenerated. Rotating the signing key invalidates the
    # public key every paired device pinned, and they would all stop working
    # with no error that points here.
    ok ".env already exists — leaving it alone"
else
    secret() { python3 -c "import secrets; print(secrets.token_urlsafe(48))"; }
    signing_key() {
        python3 -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('='))"
    }

    umask 077
    cat > "$ENV_FILE" <<EOF
# Written by deploy/vps-setup.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Not in git.
ATLAS_ENVIRONMENT=prod
ATLAS_LOG_LEVEL=INFO

ATLAS_DOMAIN=$DOMAIN
POSTGRES_USER=atlas
POSTGRES_PASSWORD=$(secret)
POSTGRES_DB=atlas

ATLAS_JWT_SECRET=$(secret)
# Every device pins the public half of this at pairing time. Changing it means
# re-pairing all of them.
ATLAS_SERVER_SIGNING_KEY=$(signing_key)
# Single-use, for the very first device. Comment it out once paired.
ATLAS_BOOTSTRAP_TOKEN=$(secret)

ATLAS_OWNER_DISPLAY_NAME=Owner
ATLAS_OWNER_LANGUAGE=ru
# Load-bearing: the briefing, the summary and the prayer times are all decided
# in local time.
ATLAS_OWNER_TIMEZONE=Asia/Almaty

# --- fill these in yourself -------------------------------------------------
# The model. Backend only; never reaches the laptop or the phone.
# ATLAS_GEMINI_API_KEY=
# The tracker.
# ATLAS_SUNNY_BASE_URL=
# ATLAS_SUNNY_TOKEN=
# Prayer times need both coordinates. Not guessed from the timezone.
# ATLAS_PRAYER_LATITUDE=
# ATLAS_PRAYER_LONGITUDE=
EOF
    chmod 600 "$ENV_FILE"
    ok "generated, 0600, four secrets"
fi

grep -q "^ATLAS_GEMINI_API_KEY=" "$ENV_FILE" \
    || printf '\n    note: no Gemini key yet — JARVIS will answer that no model is configured.\n'

# ----------------------------------------------------------------------- up

say "building and starting"
cd "$REPO_ROOT/infra"
$DOCKER compose --env-file "$ENV_FILE" up -d --build
ok "postgres, backend and caddy are up"

say "migrations"
# Explicitly, never on container start: an automatic migration on boot turns a
# rollback into a data-loss event.
$DOCKER compose --env-file "$ENV_FILE" exec -T backend alembic -c /app/alembic.ini upgrade head
ok "schema at head"

say "health"
for attempt in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:8000/v1/health/ready" >/dev/null 2>&1 \
        || $DOCKER compose --env-file "$ENV_FILE" exec -T backend \
            python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health/ready')" >/dev/null 2>&1; then
        ok "the backend is answering"
        break
    fi
    [[ $attempt -eq 30 ]] && die "the backend never became ready — $DOCKER compose logs backend"
    sleep 2
done

if curl -fsS --max-time 10 "https://$DOMAIN/v1/health/live" >/dev/null 2>&1; then
    ok "https://$DOMAIN is serving with a certificate"
else
    printf '    TLS is not answering yet. Caddy retries on its own; check DNS, then:\n'
    printf '      %s compose logs caddy\n' "$DOCKER"
fi

# -------------------------------------------------------------------- finish
#
# The pairing code is issued here rather than on the laptop, so the bootstrap
# token never has to leave this machine. The code is short, single-use and
# expires in minutes, which is the right thing to read out over a phone call to
# yourself.

say "pairing code for the first device"
BOOTSTRAP="$(grep '^ATLAS_BOOTSTRAP_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"

if [[ -z "$BOOTSTRAP" ]]; then
    ok "bootstrap pairing is closed — authorise new devices from a paired one"
else
    CODE="$($DOCKER compose --env-file "$ENV_FILE" exec -T backend python - "$BOOTSTRAP" <<'PYEOF'
import json, sys, urllib.error, urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8000/v1/pair/start",
    data=json.dumps({"kind": "windows_agent", "name": "windows"}).encode(),
    headers={"Content-Type": "application/json", "X-Atlas-Bootstrap-Token": sys.argv[1]},
)
try:
    with urllib.request.urlopen(request, timeout=15) as answer:
        print(json.load(answer)["code_display"])
except urllib.error.HTTPError as error:
    # 403 means a device already exists, which is not a failure worth stopping
    # the whole deployment over.
    print(f"(no code: HTTP {error.code})")
PYEOF
)"
    ok "code: $CODE"
fi

cat <<EOF

    JARVIS is running at https://$DOMAIN

    On the Windows machine, two commands:

        setx ATLAS_AGENT_BACKEND_URL "https://$DOMAIN"
        atlas-agent pair --code ${CODE:-XXXX-XXXX}

    Open a new terminal after the first one — setx does not affect the window
    it was typed in. Then start JARVIS as usual; it will connect here instead of
    to localhost, and the reminders keep running when the laptop is shut.

    When that works, close bootstrap pairing:

        sed -i 's/^ATLAS_BOOTSTRAP_TOKEN=/#ATLAS_BOOTSTRAP_TOKEN=/' .env
        ./deploy/update.sh

    Going back to a local backend is one variable:

        setx ATLAS_AGENT_BACKEND_URL "http://127.0.0.1:8000"

EOF
