param(
    [Parameter(Mandatory = $true)]
    [int]$BlueWeight,

    [Parameter(Mandatory = $true)]
    [int]$GreenWeight,

    [Parameter(Mandatory = $false)]
    [string]$Stage = "manual",

    [Parameter(Mandatory = $true)]
    [string]$HostedZoneId,

    [Parameter(Mandatory = $true)]
    [string]$RecordName,

    [Parameter(Mandatory = $true)]
    [string]$BlueTarget,

    [Parameter(Mandatory = $true)]
    [string]$GreenTarget,

    [Parameter(Mandatory = $false)]
    [ValidateSet("CNAME", "A", "AAAA", "TXT")]
    [string]$RecordType = "CNAME",

    [Parameter(Mandatory = $false)]
    [ValidateRange(1, 172800)]
    [int]$TTL = 30,

    [Parameter(Mandatory = $false)]
    [string]$BlueSetIdentifier = "blue",

    [Parameter(Mandatory = $false)]
    [string]$GreenSetIdentifier = "green",

    [Parameter(Mandatory = $false)]
    [string]$AwsCliPath = "aws",

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Normalize-DnsName {
    param([string]$Name)
    $trimmed = $Name.Trim()
    if (-not $trimmed.EndsWith(".")) {
        return "$trimmed."
    }
    return $trimmed
}

if ($BlueWeight -lt 0 -or $BlueWeight -gt 255) {
    throw "BlueWeight must be between 0 and 255 for Route53 weighted records."
}
if ($GreenWeight -lt 0 -or $GreenWeight -gt 255) {
    throw "GreenWeight must be between 0 and 255 for Route53 weighted records."
}
if (($BlueWeight + $GreenWeight) -le 0) {
    throw "At least one of BlueWeight/GreenWeight must be greater than 0."
}

$recordNameNormalized = Normalize-DnsName -Name $RecordName
$blueTargetNormalized = Normalize-DnsName -Name $BlueTarget
$greenTargetNormalized = Normalize-DnsName -Name $GreenTarget

$changeBatch = @{
    Comment = "cutover-stage=$Stage blue=$BlueWeight green=$GreenWeight"
    Changes = @(
        @{
            Action = "UPSERT"
            ResourceRecordSet = @{
                Name = $recordNameNormalized
                Type = $RecordType
                SetIdentifier = $BlueSetIdentifier
                Weight = $BlueWeight
                TTL = $TTL
                ResourceRecords = @(@{ Value = $blueTargetNormalized })
            }
        },
        @{
            Action = "UPSERT"
            ResourceRecordSet = @{
                Name = $recordNameNormalized
                Type = $RecordType
                SetIdentifier = $GreenSetIdentifier
                Weight = $GreenWeight
                TTL = $TTL
                ResourceRecords = @(@{ Value = $greenTargetNormalized })
            }
        }
    )
}

$statePath = Join-Path $PSScriptRoot "..\..\docs\reports\dns-weight-route53-state.json"
$statePayload = @{
    timestamp_utc = [DateTime]::UtcNow.ToString("o")
    provider = "route53"
    stage = $Stage
    hosted_zone_id = $HostedZoneId
    record_name = $recordNameNormalized
    record_type = $RecordType
    blue = @{
        set_identifier = $BlueSetIdentifier
        target = $blueTargetNormalized
        weight = $BlueWeight
    }
    green = @{
        set_identifier = $GreenSetIdentifier
        target = $greenTargetNormalized
        weight = $GreenWeight
    }
    dry_run = [bool]$DryRun
}

$directory = Split-Path -Parent $statePath
if (-not (Test-Path $directory)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

if ($DryRun) {
    $statePayload["change_batch"] = $changeBatch
    $statePayload | ConvertTo-Json -Depth 8 | Set-Content -Path $statePath -Encoding UTF8
    Write-Host "[dns-hook:route53] dry-run stage=$Stage blue=$BlueWeight green=$GreenWeight state=$statePath"
    return
}

$awsCommand = Get-Command $AwsCliPath -ErrorAction SilentlyContinue
if (-not $awsCommand) {
    throw "AWS CLI not found at '$AwsCliPath'. Install AWS CLI or pass -AwsCliPath with a valid command/path."
}

$tempFile = [System.IO.Path]::GetTempFileName()
try {
    $changeBatch | ConvertTo-Json -Depth 10 | Set-Content -Path $tempFile -Encoding UTF8

    $result = & $AwsCliPath route53 change-resource-record-sets `
        --hosted-zone-id $HostedZoneId `
        --change-batch "file://$tempFile" `
        --output json

    if ($LASTEXITCODE -ne 0) {
        throw "Route53 change-resource-record-sets failed with exit code $LASTEXITCODE."
    }

    $statePayload["aws_result"] = if ($result) { $result | ConvertFrom-Json } else { $null }
    $statePayload | ConvertTo-Json -Depth 10 | Set-Content -Path $statePath -Encoding UTF8
    Write-Host "[dns-hook:route53] applied stage=$Stage blue=$BlueWeight green=$GreenWeight state=$statePath"
}
finally {
    if (Test-Path $tempFile) {
        Remove-Item -Path $tempFile -Force -ErrorAction SilentlyContinue
    }
}
