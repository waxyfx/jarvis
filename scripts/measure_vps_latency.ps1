<#
.SYNOPSIS
    Measure round-trip latency to candidate VPS locations.

.DESCRIPTION
    The voice loop crosses the network to the backend and back, so the region
    you pick shows up directly in how quickly ATLAS answers. Aim for an RTT
    under 60 ms; anything above ~120 ms is noticeable in conversation.

    Run this before buying a VPS. Each provider below publishes a public test
    endpoint in the given region.

.EXAMPLE
    .\scripts\measure_vps_latency.ps1
    .\scripts\measure_vps_latency.ps1 -Targets @{ "my-vps" = "203.0.113.10" }
#>
[CmdletBinding()]
param(
    [hashtable]$Targets = @{
        "Hetzner — Falkenstein, DE" = "fsn1-speed.hetzner.com"
        "Hetzner — Helsinki, FI"    = "hel1-speed.hetzner.com"
        "Hetzner — Ashburn, US"     = "ash-speed.hetzner.com"
        "DigitalOcean — Frankfurt"  = "speedtest-fra1.digitalocean.com"
        "DigitalOcean — London"     = "speedtest-lon1.digitalocean.com"
        "Vultr — Frankfurt"         = "fra-de-ping.vultr.com"
        "Vultr — Stockholm"         = "sto-se-ping.vultr.com"
    },
    [int]$Count = 8
)

$ErrorActionPreference = "Continue"

Write-Host "Measuring round-trip time ($Count pings each). Lower is better." -ForegroundColor Cyan
Write-Host ""

$results = foreach ($entry in $Targets.GetEnumerator()) {
    $replies = Test-Connection -ComputerName $entry.Value -Count $Count -ErrorAction SilentlyContinue
    if (-not $replies) {
        [pscustomobject]@{ Location = $entry.Key; Host = $entry.Value; AvgMs = $null; MinMs = $null; Verdict = "unreachable" }
        continue
    }

    $times = $replies | ForEach-Object { $_.ResponseTime }
    $average = [math]::Round(($times | Measure-Object -Average).Average, 1)
    $minimum = ($times | Measure-Object -Minimum).Minimum

    $verdict = if ($average -lt 60) { "good" }
               elseif ($average -lt 120) { "acceptable" }
               else { "noticeable lag" }

    [pscustomobject]@{
        Location = $entry.Key
        Host     = $entry.Value
        AvgMs    = $average
        MinMs    = $minimum
        Verdict  = $verdict
    }
}

$results | Sort-Object { if ($null -eq $_.AvgMs) { [double]::MaxValue } else { $_.AvgMs } } |
    Format-Table -AutoSize

Write-Host "ICMP is a lower bound: a TLS WebSocket adds a handshake on top." -ForegroundColor DarkGray
Write-Host "Budget context: the Gemini call in the voice loop costs 400-900 ms," -ForegroundColor DarkGray
Write-Host "so ~50 ms of network is about 10% of the total, not the bottleneck." -ForegroundColor DarkGray
