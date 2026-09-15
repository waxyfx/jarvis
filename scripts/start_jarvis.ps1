<#
.SYNOPSIS
    Brings JARVIS up: database, backend, agent, microphone.

.DESCRIPTION
    Everything that has to be running for JARVIS to be useful, in the order it
    has to start, with each step checked before the next one begins.

    Three processes, not one, and they are separate for reasons that are not
    negotiable: the database holds the audit chain and the day's activity, the
    backend holds the Gemini key and the tracker token and must never be on the
    laptop's network surface, and the agent is the only thing that may touch
    Windows. This script is the thing that knows the order.

    It is idempotent. A database already running is left alone; a backend
    already listening is used rather than replaced, because two backends on one
    database would both write the audit chain and both run the proactive
    scheduler, and the owner would get every reminder twice.

    Started here, everything stops here. Closing the window or pressing Ctrl+C
    takes down whatever this script started and leaves whatever it did not.

.NOTES
    ASCII only, deliberately. Windows PowerShell 5.1 reads a UTF-8 file with no
    byte-order mark as ANSI, and a single em dash in a comment then breaks
    string parsing several lines further down.
#>

[CmdletBinding()]
param(
    # Skip the microphone. Useful when the backend is wanted but the room is not
    # quiet - everything still works by typing through the API.
    [switch]$NoVoice,
    [int]$Port = 8000,
    [int]$PostgresPort = 55432
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
# Settings read .env relative to the working directory, so this is not cosmetic:
# from anywhere else the backend reports three missing fields instead of one
# missing file.
Set-Location $root

$venv = Join-Path $root ".venv\Scripts"
$python = Join-Path $venv "python.exe"
$pgCtl = Join-Path $root ".tools\pgsql\bin\pg_ctl.exe"
$pgData = Join-Path $root ".pgdata"
$logs = Join-Path $root ".logs"

# What this script started, and therefore what it is responsible for stopping.
$script:startedBackend = $null
$script:startedPostgres = $false

function Write-Step { param($text) Write-Host "  $text" -ForegroundColor Cyan }
function Write-Ok { param($text) Write-Host "  $text" -ForegroundColor DarkGray }
function Write-Bad { param($text) Write-Host "  $text" -ForegroundColor Red }

function Invoke-Native {
    <#
        Run a native executable and collect everything it said.

        Wrapped because of one Windows PowerShell 5.1 behaviour: with
        $ErrorActionPreference = "Stop", redirecting a native program's stderr
        turns each line into a terminating NativeCommandError - so a tool that
        merely logs its progress to stderr, as both alembic and pg_ctl do, kills
        the script while reporting success. The preference is function-scoped,
        so relaxing it here does not relax it anywhere else.

        The caller checks the exit code, which is the only signal that means
        what it says.
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [string[]]$Arguments = @(),
        [string]$LogFile
    )

    $ErrorActionPreference = "Continue"
    $output = & $Path @Arguments 2>&1
    $code = $LASTEXITCODE
    if ($LogFile) { $output | Out-File -Append -Encoding utf8 $LogFile }
    return [pscustomobject]@{ Output = ($output | Out-String); Code = $code }
}

function Test-Ready {
    try {
        $answer = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/v1/health/ready" `
            -TimeoutSec 2 -UseBasicParsing
        return $answer.StatusCode -eq 200
    } catch {
        return $false
    }
}

try {
    Write-Host ""
    Write-Host "JARVIS" -ForegroundColor White
    Write-Host ""

    if (-not (Test-Path $python)) {
        Write-Bad "No environment in .venv. Rebuild it with: uv sync"
        exit 1
    }
    if (-not (Test-Path (Join-Path $root ".env"))) {
        Write-Bad "No .env. See docs/runbook.md - the backend needs its keys."
        exit 1
    }
    New-Item -ItemType Directory -Force $logs | Out-Null

    # ---------------------------------------------------------------- database
    Write-Step "database"
    $status = Invoke-Native -Path $pgCtl -Arguments @("-D", $pgData, "status")
    if ($status.Output -match "server is running") {
        Write-Ok "already running on $PostgresPort"
    } else {
        $start = Invoke-Native -Path $pgCtl -Arguments @(
            "-D", $pgData,
            "-o", "-p $PostgresPort -c listen_addresses=127.0.0.1",
            "-l", (Join-Path $pgData "server.log"),
            "start"
        )
        Start-Sleep -Seconds 3
        $status = Invoke-Native -Path $pgCtl -Arguments @("-D", $pgData, "status")
        if ($status.Output -notmatch "server is running") {
            Write-Bad "the database did not start; see .pgdata\server.log"
            Write-Bad $start.Output.Trim()
            exit 1
        }
        $script:startedPostgres = $true
        Write-Ok "started on $PostgresPort"
    }

    # -------------------------------------------------------------- migrations
    # Idempotent and quick when there is nothing to do. Running it every time is
    # what stops "it worked yesterday" after a schema change.
    Write-Step "schema"
    $migrate = Invoke-Native -Path $python -Arguments @(
        "-m", "alembic",
        "-c", (Join-Path $root "packages\atlas-backend\alembic.ini"),
        "upgrade", "head"
    ) -LogFile (Join-Path $logs "migrate.log")
    if ($migrate.Code -ne 0) {
        Write-Bad "migrations failed; see .logs\migrate.log"
        exit 1
    }
    Write-Ok "up to date"

    # ----------------------------------------------------------------- backend
    Write-Step "backend"
    if (Test-Ready) {
        # Reused rather than replaced. Two backends on one database would both
        # run the proactive scheduler, and every reminder would arrive twice.
        Write-Ok "already listening on $Port"
    } else {
        $script:startedBackend = Start-Process `
            -FilePath (Join-Path $venv "atlas-backend.exe") `
            -ArgumentList "--port", $Port `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logs "backend.log") `
            -RedirectStandardError (Join-Path $logs "backend.err.log")

        $deadline = (Get-Date).AddSeconds(45)
        while (-not (Test-Ready)) {
            if ($script:startedBackend.HasExited) {
                Write-Bad "the backend stopped on startup; see .logs\backend.err.log"
                exit 1
            }
            if ((Get-Date) -gt $deadline) {
                Write-Bad "the backend did not become ready; see .logs\backend.log"
                exit 1
            }
            Start-Sleep -Milliseconds 500
        }
        Write-Ok "listening on $Port (pid $($script:startedBackend.Id))"
    }

    # ------------------------------------------------------------------- agent
    Write-Step "agent"
    $agentArgs = @("run")
    if (-not $NoVoice) { $agentArgs += "--voice" }
    Write-Host ""

    # In the foreground on purpose: this is the thing the owner watches, and
    # Ctrl+C here should end the session rather than orphan a microphone.
    & (Join-Path $venv "atlas-agent.exe") @agentArgs
} finally {
    if ($script:startedBackend -and -not $script:startedBackend.HasExited) {
        Write-Host ""
        Write-Step "stopping the backend"
        Stop-Process -Id $script:startedBackend.Id -Force -ErrorAction SilentlyContinue
    }
    if ($script:startedPostgres) {
        # Left alone when this script did not start it: something else is using
        # it, and stopping it would be rude.
        Write-Step "stopping the database"
        Invoke-Native -Path $pgCtl -Arguments @("-D", $pgData, "-m", "fast", "stop") | Out-Null
    }
    Write-Host ""
}
