<#
.SYNOPSIS
    Package the local JARVIS state so a VPS can carry on from it.

.DESCRIPTION
    Moving the backend to a server has two honest options, and this script is
    for the second one.

    **Start clean.** Run deploy/vps-setup.sh, pair the agent again, and let the
    laptop's history stay on the laptop. Two commands, nothing to carry, and the
    right choice unless there is history worth keeping.

    **Carry it over.** The audit chain is hash-linked and cannot be
    reconstructed; the device registry is what stops every device having to
    re-pair; the activity samples are the only record of how the days actually
    went. This script packages all three, plus the one secret that has to travel
    with them.

    That secret is the server signing key. Every paired device pinned its public
    half, so a server with a different key is a server none of them will accept
    - they do not fail with a message about keys, they simply refuse every
    command. It goes into the archive because the archive is the only way the
    move is lossless; guard it accordingly, and delete the archive afterwards.

.NOTES
    Produces one directory. Nothing is uploaded, nothing is deleted, and the
    running system is not touched - pg_dump takes a consistent snapshot of a
    live database.
#>

[CmdletBinding()]
param(
    [string]$Into = "$env:USERPROFILE\jarvis-export",
    [int]$PostgresPort = 55432
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pgDump = Join-Path $root ".tools\pgsql\bin\pg_dump.exe"
$envFile = Join-Path $root ".env"

function Write-Step { param($text) Write-Host "  $text" -ForegroundColor Cyan }
function Write-Ok { param($text) Write-Host "    $text" -ForegroundColor DarkGray }

if (-not (Test-Path $envFile)) { throw "no .env at $envFile" }
if (-not (Test-Path $pgDump)) { throw "no pg_dump at $pgDump" }

$settings = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*)$') { $settings[$Matches[1]] = $Matches[2].Trim() }
}

$databaseUrl = $settings["ATLAS_DATABASE_URL"]
if (-not $databaseUrl) { throw "ATLAS_DATABASE_URL is not set in .env" }

# postgresql+asyncpg://user:password@host:port/database
if ($databaseUrl -notmatch '://([^:]+):([^@]+)@[^/]+/(.+)$') {
    throw "could not read the database name and credentials out of ATLAS_DATABASE_URL"
}
$user = $Matches[1]
$password = $Matches[2]
$database = $Matches[3]

$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$target = Join-Path $Into $stamp
New-Item -ItemType Directory -Force $target | Out-Null

Write-Host ""
Write-Host "JARVIS export" -ForegroundColor White
Write-Host ""

Write-Step "database"
$env:PGPASSWORD = $password
try {
    & $pgDump -h 127.0.0.1 -p $PostgresPort -U $user -d $database `
        --format=custom --file (Join-Path $target "atlas.dump")
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
$size = (Get-Item (Join-Path $target "atlas.dump")).Length
# An empty dump is worse than none: it looks like a backup.
if ($size -lt 4096) { throw "the dump is only $size bytes; something is wrong" }
Write-Ok "atlas.dump, $([math]::Round($size / 1KB)) KB"

Write-Step "identity"
# Only the values that must be identical on the far side. The database
# password is not among them: the VPS has its own postgres with its own.
@(
    "# Carried from the laptop by deploy/export-local.ps1 on $stamp.",
    "#",
    "# These three must match on the server or the paired devices stop working.",
    "# Append them to the VPS .env, replacing what vps-setup.sh generated, then",
    "# restart with ./deploy/update.sh. Delete this file afterwards.",
    "ATLAS_SERVER_SIGNING_KEY=$($settings['ATLAS_SERVER_SIGNING_KEY'])",
    "ATLAS_JWT_SECRET=$($settings['ATLAS_JWT_SECRET'])",
    "ATLAS_OWNER_TIMEZONE=$($settings['ATLAS_OWNER_TIMEZONE'])"
) | Set-Content -Path (Join-Path $target "identity.env") -Encoding utf8
Write-Ok "identity.env  - contains the signing key; treat it as a password"

Write-Step "what to do with it"
@"
Move this directory to the server, then:

    # 1. the identity, so the already-paired agent keeps working
    grep -v '^#' identity.env >> ~/jarvis/.env
    # remove the duplicate lines vps-setup.sh generated, keeping these
    sudo -e ~/jarvis/.env

    # 2. the data
    cd ~/jarvis/infra
    docker compose --env-file ../.env cp ../atlas.dump postgres:/tmp/atlas.dump
    docker compose --env-file ../.env exec -T postgres \
        pg_restore -U atlas -d atlas --clean --if-exists /tmp/atlas.dump
    docker compose --env-file ../.env exec -T postgres rm /tmp/atlas.dump

    # 3. restart, and check the chain survived the move
    cd ~/jarvis && ./deploy/update.sh
    curl -fsS https://YOUR-DOMAIN/v1/health/ready

Then, on the laptop, point the agent at the server:

    setx ATLAS_AGENT_BACKEND_URL "https://YOUR-DOMAIN"

Open a new terminal afterwards - setx does not affect the window it was typed
in. The agent should connect without pairing again; if it is asked to pair, the
signing key did not make it across.

Delete this directory when the move is done. identity.env is a key.
"@ | Set-Content -Path (Join-Path $target "README.txt") -Encoding utf8
Write-Ok "README.txt"

Write-Host ""
Write-Host "  $target" -ForegroundColor White
Write-Host ""
Write-Host "  It contains the server signing key. Move it over ssh, not email," -ForegroundColor Yellow
Write-Host "  and delete it when the server is up." -ForegroundColor Yellow
Write-Host ""
