param(
    [Parameter(Mandatory = $true)]
    [int]$BlueWeight,

    [Parameter(Mandatory = $true)]
    [int]$GreenWeight,

    [Parameter(Mandatory = $false)]
    [string]$Stage = "manual"
)

$statePath = Join-Path $PSScriptRoot "..\..\docs\reports\dns-weight-state.json"
$payload = @{
    timestamp_utc = [DateTime]::UtcNow.ToString("o")
    stage = $Stage
    blue_weight = $BlueWeight
    green_weight = $GreenWeight
}

$directory = Split-Path -Parent $statePath
if (-not (Test-Path $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$payload | ConvertTo-Json -Depth 4 | Set-Content -Path $statePath -Encoding UTF8
Write-Host "[dns-hook] stage=$Stage blue=$BlueWeight green=$GreenWeight state=$statePath"
