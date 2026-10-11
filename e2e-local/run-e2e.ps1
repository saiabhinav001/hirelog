<#
.SYNOPSIS
  Runs the real-browser journeys (frontend/e2e/exams-staging.spec.ts) against the stack from start-stack.ps1.
  Extra arguments go to Playwright, e.g.  e2e-local\run-e2e.ps1 --headed -g "revoked"

.PARAMETER KeepData
  Do not wipe the emulators first (by default every run starts from empty emulators).
#>
param(
  [switch]$KeepData,
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$PlaywrightArgs
)

$repo = Split-Path $PSScriptRoot -Parent
$env:E2E_EXAMS_STAGING = '1'
if (-not $KeepData) { $env:E2E_RESET_EMULATORS = '1' }
$env:E2E_BACKEND_LOG = Join-Path $repo '.e2e\backend.log'

Push-Location (Join-Path $repo 'frontend')
npx playwright test -c playwright.exams-staging.config.ts @PlaywrightArgs
$code = $LASTEXITCODE
Pop-Location
exit $code
