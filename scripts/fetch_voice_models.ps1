<#
.SYNOPSIS
    Download the voice models the Windows Agent needs.

.DESCRIPTION
    Models are not committed: they are large, they are not ours, and their
    licences travel with the upstream projects. This script fetches them into
    .models/, which is gitignored.

    Everything here is local-inference only. No audio leaves the machine.

    | Model                | Size   | Licence | Purpose                    |
    |----------------------|--------|---------|----------------------------|
    | silero_vad.onnx      | 2.2 MB | MIT     | Voice activity detection   |
    | en_GB-alan-medium    |  60 MB | MIT     | English speech (Piper)     |
    | ru_RU-dmitri-medium  |  60 MB | MIT     | Russian speech (Piper)     |

.PARAMETER Force
    Re-download files that are already present.
#>
[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ModelDir = Join-Path $RepoRoot ".models"

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Skip { param([string]$Message) Write-Host "    $Message (already present)" -ForegroundColor DarkGray }

function Get-Model {
    param([string]$Url, [string]$Destination)

    $name = Split-Path -Leaf $Destination
    if ((Test-Path $Destination) -and -not $Force) {
        Write-Skip $name
        return
    }

    $parent = Split-Path -Parent $Destination
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force $parent | Out-Null }

    Write-Host "    fetching $name ..." -NoNewline
    # A partial file left behind by an interrupted run looks exactly like a
    # complete one to every later check, so download beside the target and move
    # it into place only once the transfer finished.
    $temporary = "$Destination.partial"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $temporary -UseBasicParsing -TimeoutSec 900
        Move-Item -Path $temporary -Destination $Destination -Force
        $size = [math]::Round((Get-Item $Destination).Length / 1MB, 1)
        Write-Host " ok ($size MB)" -ForegroundColor Green
    } catch {
        Remove-Item $temporary -Force -ErrorAction SilentlyContinue
        Write-Host " FAILED" -ForegroundColor Red
        throw
    }
}

Write-Step "Silero VAD"
Get-Model `
    -Url "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx" `
    -Destination (Join-Path $ModelDir "silero_vad.onnx")

Write-Step "Piper voices"
$piperBase = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
$voices = @(
    @{ Name = "en_GB-alan-medium";   Path = "en/en_GB/alan/medium/en_GB-alan-medium" },
    @{ Name = "ru_RU-dmitri-medium"; Path = "ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium" }
)
foreach ($voice in $voices) {
    foreach ($extension in @(".onnx", ".onnx.json")) {
        Get-Model `
            -Url "$piperBase/$($voice.Path)$extension" `
            -Destination (Join-Path $ModelDir "piper\$($voice.Name)$extension")
    }
}

Write-Host ""
Write-Host "Ready." -ForegroundColor Green
Write-Host "  Voice tests:  uv run pytest packages/atlas-voice"
Write-Host "  Without these models those tests skip rather than assert less."
