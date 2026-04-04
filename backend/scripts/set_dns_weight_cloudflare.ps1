param(
    [Parameter(Mandatory = $true)]
    [int]$BlueWeight,

    [Parameter(Mandatory = $true)]
    [int]$GreenWeight,

    [Parameter(Mandatory = $false)]
    [string]$Stage = "manual",

    [Parameter(Mandatory = $true)]
    [string]$AccountId,

    [Parameter(Mandatory = $true)]
    [string]$PoolId,

    [Parameter(Mandatory = $true)]
    [string]$BlueOrigin,

    [Parameter(Mandatory = $true)]
    [string]$GreenOrigin,

    [Parameter(Mandatory = $false)]
    [string]$ApiToken = "",

    [Parameter(Mandatory = $false)]
    [switch]$DisableOtherOrigins,

    [Parameter(Mandatory = $false)]
    [int]$TimeoutSeconds = 20,

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($BlueWeight -lt 0 -or $BlueWeight -gt 100) {
    throw "BlueWeight must be between 0 and 100 for Cloudflare origin weighting."
}
if ($GreenWeight -lt 0 -or $GreenWeight -gt 100) {
    throw "GreenWeight must be between 0 and 100 for Cloudflare origin weighting."
}
if (($BlueWeight + $GreenWeight) -le 0) {
    throw "At least one of BlueWeight/GreenWeight must be greater than 0."
}

$statePath = Join-Path $PSScriptRoot "..\..\docs\reports\dns-weight-cloudflare-state.json"
$statePayload = @{
    timestamp_utc = [DateTime]::UtcNow.ToString("o")
    provider = "cloudflare"
    stage = $Stage
    account_id = $AccountId
    pool_id = $PoolId
    blue_origin = $BlueOrigin
    green_origin = $GreenOrigin
    blue_weight = $BlueWeight
    green_weight = $GreenWeight
    disable_other_origins = [bool]$DisableOtherOrigins
    dry_run = [bool]$DryRun
}

$directory = Split-Path -Parent $statePath
if (-not (Test-Path $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

if (-not $ApiToken) {
    $ApiToken = $env:CF_API_TOKEN
}
if (-not $ApiToken) {
    if ($DryRun) {
        $statePayload["note"] = "dry-run without API token; Cloudflare pool lookup and origin validation skipped"
        $statePayload | ConvertTo-Json -Depth 10 | Set-Content -Path $statePath -Encoding UTF8
        Write-Host "[dns-hook:cloudflare] dry-run(no-api) stage=$Stage blue=$BlueWeight green=$GreenWeight state=$statePath"
        return
    }
    throw "Cloudflare API token missing. Pass -ApiToken or set CF_API_TOKEN."
}

$baseUrl = "https://api.cloudflare.com/client/v4/accounts/$AccountId/load_balancers/pools/$PoolId"
$headers = @{
    Authorization = "Bearer $ApiToken"
    "Content-Type" = "application/json"
}

try {
    $poolResponse = Invoke-RestMethod -Method Get -Uri $baseUrl -Headers $headers -TimeoutSec $TimeoutSeconds
}
catch {
    throw "Cloudflare pool fetch failed: $($_.Exception.Message)"
}

if (-not $poolResponse.success) {
    throw "Cloudflare API returned an unsuccessful response while fetching pool $PoolId."
}

$pool = $poolResponse.result
if (-not $pool) {
    throw "Cloudflare API response did not include a pool object."
}

$origins = @($pool.origins)
if (-not $origins -or $origins.Count -eq 0) {
    throw "Pool '$PoolId' has no origins."
}

$foundBlue = $false
$foundGreen = $false
foreach ($origin in $origins) {
    if ($origin.name -eq $BlueOrigin) {
        $origin.weight = $BlueWeight
        $foundBlue = $true
        continue
    }
    if ($origin.name -eq $GreenOrigin) {
        $origin.weight = $GreenWeight
        $foundGreen = $true
        continue
    }
    if ($DisableOtherOrigins) {
        $origin.weight = 0
    }
}

if (-not $foundBlue) {
    throw "Blue origin '$BlueOrigin' not found in pool '$PoolId'."
}
if (-not $foundGreen) {
    throw "Green origin '$GreenOrigin' not found in pool '$PoolId'."
}

$body = @{
    name = $pool.name
    description = $pool.description
    enabled = if ($null -ne $pool.enabled) { [bool]$pool.enabled } else { $true }
    minimum_origins = if ($null -ne $pool.minimum_origins) { [int]$pool.minimum_origins } else { 1 }
    monitor = $pool.monitor
    notification_email = $pool.notification_email
    check_regions = $pool.check_regions
    latitude = $pool.latitude
    longitude = $pool.longitude
    load_shedding = $pool.load_shedding
    origin_steering = $pool.origin_steering
    origins = $origins
}

if ($DryRun) {
    $statePayload["request_body"] = $body
    $statePayload | ConvertTo-Json -Depth 12 | Set-Content -Path $statePath -Encoding UTF8
    Write-Host "[dns-hook:cloudflare] dry-run stage=$Stage blue=$BlueWeight green=$GreenWeight state=$statePath"
    return
}

try {
    $updateResponse = Invoke-RestMethod -Method Put -Uri $baseUrl -Headers $headers -Body ($body | ConvertTo-Json -Depth 12) -TimeoutSec $TimeoutSeconds
}
catch {
    throw "Cloudflare pool update failed: $($_.Exception.Message)"
}

if (-not $updateResponse.success) {
    $errorsJson = ($updateResponse.errors | ConvertTo-Json -Depth 8)
    throw "Cloudflare pool update returned failure: $errorsJson"
}

$statePayload["result"] = $updateResponse.result
$statePayload | ConvertTo-Json -Depth 12 | Set-Content -Path $statePath -Encoding UTF8
Write-Host "[dns-hook:cloudflare] applied stage=$Stage blue=$BlueWeight green=$GreenWeight state=$statePath"
