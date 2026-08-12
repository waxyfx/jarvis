<#
.SYNOPSIS
    Prepare a Windows development environment for ATLAS.

.DESCRIPTION
    Reproduces the setup used to build M1, without requiring administrator
    rights:

      * uv, installed for the current user
      * CPython 3.12 (the project pins it; the ML stack in later phases has no
        complete 3.14 support yet)
      * a portable PostgreSQL 17 cluster on port 55432, run as a plain process
        rather than a Windows service
      * three databases: atlas_dev, atlas_test, atlas_e2e
      * .env and .env.test with freshly generated secrets
      * dependencies synced and migrations applied

    Safe to re-run: every step is skipped if it is already done.

.PARAMETER Port
    Port for the local PostgreSQL cluster. Default 55432, chosen so it cannot
    collide with a system-wide PostgreSQL on 5432.
#>
[CmdletBinding()]
param(
    [int]$Port = 55432,
    [string]$PostgresVersion = "17.6-1"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $RepoRoot ".tools"
$DataDir = Join-Path $RepoRoot ".pgdata"
$PgBin = Join-Path $ToolsDir "pgsql\bin"

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Skip($message) { Write-Host "    (skip) $message" -ForegroundColor DarkGray }

function New-Secret([int]$length) {
    -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $length | ForEach-Object { [char]$_ })
}

# ---------------------------------------------------------------- uv
Write-Step "uv"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    $candidate = Join-Path $env:APPDATA "Python\Python314\Scripts\uv.exe"
    if (Test-Path $candidate) { $uv = $candidate }
}
if (-not $uv) {
    python -m pip install --user --quiet uv
    $uv = Get-ChildItem "$env:APPDATA\Python" -Recurse -Filter uv.exe |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $uv) { throw "uv installed but could not be located on disk" }
    Write-Host "    installed: $uv"
} else {
    Write-Skip "already present: $uv"
}
$env:PATH = "$(Split-Path -Parent $uv);$env:PATH"

# ---------------------------------------------------------------- python 3.12
Write-Step "CPython 3.12"
$hasPython312 = (& $uv python list --only-installed 2>&1 | Select-String "cpython-3\.12") -ne $null
if ($hasPython312) {
    Write-Skip "already installed"
} else {
    # uv may report a non-zero exit while still installing the interpreter; the
    # check below is what decides success.
    & $uv python install 3.12 2>&1 | Out-Null
    if (-not (& $uv python list --only-installed 2>&1 | Select-String "cpython-3\.12")) {
        throw "CPython 3.12 could not be installed"
    }
}

# ---------------------------------------------------------------- postgresql
Write-Step "portable PostgreSQL $PostgresVersion"
if (Test-Path (Join-Path $PgBin "postgres.exe")) {
    Write-Skip "binaries already extracted"
} else {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    New-Item -ItemType Directory -Force $ToolsDir | Out-Null
    $zip = Join-Path $ToolsDir "pg-binaries.zip"
    $url = "https://get.enterprisedb.com/postgresql/postgresql-$PostgresVersion-windows-x64-binaries.zip"
    Write-Host "    downloading (~315 MB) ..."
    (New-Object System.Net.WebClient).DownloadFile($url, $zip)
    if ((Get-Item $zip).Length -lt 50MB) { throw "download looks truncated; check the URL" }
    Expand-Archive -Path $zip -DestinationPath $ToolsDir -Force
    Remove-Item $zip -Force
}

Write-Step "database cluster on port $Port"
$envTestPath = Join-Path $RepoRoot ".env.test"
if (Test-Path (Join-Path $DataDir "PG_VERSION")) {
    Write-Skip "cluster already initialised"
    if (-not (Test-Path $envTestPath)) {
        throw ".pgdata exists but .env.test is missing; delete .pgdata to start over"
    }
    $password = ((Get-Content $envTestPath | Where-Object { $_ -like "ATLAS_TEST_DATABASE_URL=*" }) -split "://")[1].Split(":")[1].Split("@")[0]
} else {
    $password = New-Secret 32
    $pwFile = Join-Path $env:TEMP "atlas_pg_pw.txt"
    Set-Content -Path $pwFile -Value $password -NoNewline -Encoding ascii
    try {
        & "$PgBin\initdb.exe" -D $DataDir -U postgres --auth=scram-sha-256 --pwfile=$pwFile -E UTF8 --locale=C | Out-Null
    } finally {
        Remove-Item $pwFile -Force -ErrorAction SilentlyContinue
    }
}

$running = & "$PgBin\pg_ctl.exe" -D $DataDir status 2>&1 | Select-String "server is running"
if ($running) {
    Write-Skip "server already running"
} else {
    & "$PgBin\pg_ctl.exe" -D $DataDir -o "-p $Port -c listen_addresses=127.0.0.1" -l (Join-Path $DataDir "server.log") start | Out-Null
    Start-Sleep -Seconds 3
}

$env:PGPASSWORD = $password
foreach ($database in @("atlas_dev", "atlas_test", "atlas_e2e")) {
    $exists = & "$PgBin\psql.exe" -h 127.0.0.1 -p $Port -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$database'"
    if ($exists -eq "1") { Write-Skip "$database exists" }
    else { & "$PgBin\createdb.exe" -h 127.0.0.1 -p $Port -U postgres $database; Write-Host "    created $database" }
}

# ---------------------------------------------------------------- env files
Write-Step "environment files"
if (Test-Path $envTestPath) {
    Write-Skip ".env.test exists"
} else {
    @(
        "ATLAS_TEST_DATABASE_URL=postgresql+asyncpg://postgres:$password@127.0.0.1:$Port/atlas_test",
        "ATLAS_DEV_DATABASE_URL=postgresql+asyncpg://postgres:$password@127.0.0.1:$Port/atlas_dev",
        "ATLAS_E2E_DATABASE_URL=postgresql+asyncpg://postgres:$password@127.0.0.1:$Port/atlas_e2e"
    ) | Set-Content -Path $envTestPath -Encoding ascii
    Write-Host "    wrote .env.test"
}

$envPath = Join-Path $RepoRoot ".env"
if (Test-Path $envPath) {
    Write-Skip ".env exists"
} else {
    @(
        "ATLAS_ENVIRONMENT=dev",
        "ATLAS_LOG_LEVEL=INFO",
        "ATLAS_DATABASE_URL=postgresql+asyncpg://postgres:$password@127.0.0.1:$Port/atlas_dev",
        "ATLAS_JWT_SECRET=$(New-Secret 48)",
        "ATLAS_BOOTSTRAP_TOKEN=$(New-Secret 32)",
        "ATLAS_OWNER_DISPLAY_NAME=Owner",
        "ATLAS_OWNER_LANGUAGE=ru",
        "ATLAS_OWNER_TIMEZONE=Asia/Almaty"
    ) | Set-Content -Path $envPath -Encoding ascii
    Write-Host "    wrote .env"
}

# ---------------------------------------------------------------- project
Write-Step "dependencies"
Push-Location $RepoRoot
try {
    & $uv sync
    Write-Step "migrations"
    & $uv run alembic -c packages/atlas-backend/alembic.ini upgrade head
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Ready." -ForegroundColor Green
Write-Host "  Run the tests:   uv run pytest"
Write-Host "  Run the backend: uv run atlas-backend --reload"
Write-Host "  Stop the database: .tools\pgsql\bin\pg_ctl.exe -D .pgdata stop"
