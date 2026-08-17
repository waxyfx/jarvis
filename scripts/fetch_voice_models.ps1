<#
.SYNOPSIS
    Download the voice models the Windows Agent needs.

.DESCRIPTION
    Models are not committed: they are large, they are not ours, and their
    licences travel with the upstream projects. This script fetches them into
    .models/, which is gitignored.

    Everything here is local-inference only. No audio leaves the machine.

    | Model                | Size   | Licence    | Purpose                 |
    |----------------------|--------|------------|-------------------------|
    | silero_vad.onnx      | 2.2 MB | MIT        | Voice activity detection|
    | en_GB-alan-medium    |  60 MB | MIT        | English speech (Piper)  |
    | ru_RU-dmitri-medium  |  60 MB | MIT        | Russian speech (Piper)  |
    | melspectrogram.onnx  | 1.0 MB | Apache-2.0 | Wake-word features      |
    | embedding_model.onnx | 1.3 MB | Apache-2.0 | Wake-word features      |
    | hey_jarvis_v0.1.onnx | 1.2 MB | Apache-2.0 | Reference wake word     |

    The hey_jarvis model is not the wake word ATLAS uses. It is a classifier
    known to work, which is what proves the feature pipeline is correct
    independently of any model trained here.

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

Write-Step "openWakeWord feature stack"
$owwBase = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
foreach ($model in @("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx")) {
    Get-Model -Url "$owwBase/$model" -Destination (Join-Path $ModelDir "oww\$model")
}

Write-Step "sherpa-onnx keyword spotting"
# Apache-2.0, fully offline, no key and no vendor. Two model families: an
# English BPE model and a bilingual one that takes CMU phonemes. Both are kept
# because they disagree on which voices they hear, and the union is better than
# either.
$kwsBase = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models"
$kwsDir = Join-Path $ModelDir "kws"
foreach ($archive in @(
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01",
    "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
)) {
    if (Test-Path (Join-Path $kwsDir $archive)) {
        Write-Skip $archive
        continue
    }
    $tarball = Join-Path $kwsDir "$archive.tar.bz2"
    Get-Model -Url "$kwsBase/$archive.tar.bz2" -Destination $tarball
    Write-Host "    extracting $archive ..." -NoNewline
    python -c "import tarfile,sys; tarfile.open(sys.argv[1],'r:bz2').extractall(sys.argv[2], filter='data')" $tarball $kwsDir
    Remove-Item $tarball -Force
    Write-Host " ok" -ForegroundColor Green
}

Write-Host ""
Write-Host "Ready." -ForegroundColor Green
Write-Host "  Voice tests:  uv run pytest packages/atlas-voice"
Write-Host "  Without these models those tests skip rather than assert less."
