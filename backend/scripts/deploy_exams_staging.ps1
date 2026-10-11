<#
.SYNOPSIS
  Deploys the exam API to the staging Hugging Face Space (backend/scripts/deploy_hf_exams_space.py) and
  asks for every secret at a hidden prompt, so none of them is echoed, stored on disk or kept in the
  shell history. The one-time-code signing secret is generated here.

.EXAMPLE
  .\deploy_exams_staging.ps1 -Owner my-hf-user -FirebaseProjectId my-project `
    -ServiceAccountPath C:\keys\exams-staging-api.json -AllowedOrigin https://hirelog-exams-staging.vercel.app `
    -FacultyEmails me@gmail.com -SmtpHost smtp.example.com -SmtpFrom exams@example.com -DryRun

  Drop -DryRun to deploy. Needs `pip install huggingface_hub` for the real run. -FacultyEmails takes several
  platform operators: -FacultyEmails a@x.com,b@y.com (or one quoted, comma-separated string).
#>
param(
  [Parameter(Mandatory)][string]$Owner,
  [Parameter(Mandatory)][string]$FirebaseProjectId,
  [Parameter(Mandatory)][string]$ServiceAccountPath,
  [Parameter(Mandatory)][string]$AllowedOrigin,
  [string[]]$FacultyEmails = @(),
  [string]$CollectionPrefix = "stg_",
  [string]$SmtpHost = "",
  [int]$SmtpPort = 587,
  [string]$SmtpFrom = "",
  [switch]$SmtpUseSsl,
  [string]$Python = "python",
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Read-Secret([string]$Prompt) {
  $secure = Read-Host -Prompt $Prompt -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

try {
  if (-not $DryRun) { $env:HF_TOKEN = Read-Secret 'Hugging Face token (write access)' }
  if ($SmtpHost) {
    $env:EXAM_OTP_SMTP_USERNAME = Read-Host -Prompt 'SMTP username (blank if none)'
    if ($env:EXAM_OTP_SMTP_USERNAME) { $env:EXAM_OTP_SMTP_PASSWORD = Read-Secret 'SMTP password' }
  }
  $bytes = New-Object byte[] 48
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $env:EXAM_OTP_HMAC_SECRET = [Convert]::ToBase64String($bytes)

  $arguments = @(
    (Join-Path $PSScriptRoot 'deploy_hf_exams_space.py'),
    '--owner', $Owner,
    '--visibility', 'public',
    '--firebase-project-id', $FirebaseProjectId,
    '--firebase-service-account-path', $ServiceAccountPath,
    '--allowed-origins', $AllowedOrigin,
    '--collection-prefix', $CollectionPrefix
  )
  if ($FacultyEmails) { $arguments += @('--faculty-emails', ($FacultyEmails -join ',')) }
  if ($SmtpHost) { $arguments += @('--smtp-host', $SmtpHost, '--smtp-port', "$SmtpPort") }
  if ($SmtpFrom) { $arguments += @('--smtp-from', $SmtpFrom) }
  if ($SmtpUseSsl) { $arguments += '--smtp-use-ssl' }
  if ($DryRun) { $arguments += '--dry-run' }

  & $Python @arguments
  exit $LASTEXITCODE
}
finally {
  foreach ($name in 'HF_TOKEN', 'EXAM_OTP_SMTP_USERNAME', 'EXAM_OTP_SMTP_PASSWORD', 'EXAM_OTP_HMAC_SECRET') {
    Remove-Item "Env:\$name" -ErrorAction SilentlyContinue
  }
}
