<#
.SYNOPSIS
  Starts the local rehearsal stack for HireLog Exams: the Firebase Auth and Firestore emulators, the
  exam API (against the emulators) and the exams-only web app (production build, staging mode).
  Nothing here can reach a real Firebase project: the project id is a "demo-" id, the API gets a
  throwaway key, and the web app signs in against the Auth emulator.

.PARAMETER Python
  Python that has backend/requirements-exams.lock installed (default: backend\.venv).

.PARAMETER JavaHome
  A JDK/JRE for the Firestore emulator (default: $env:JAVA_HOME, then `java` on PATH).

.PARAMETER SkipBuild
  Reuse the existing frontend build instead of rebuilding it.

.PARAMETER CollectionPrefix
  Prefix for every exam collection (EXAM_COLLECTION_PREFIX), to rehearse a shared-project deployment.

Logs and state live in .e2e/ (git-ignored). Stop everything with e2e-local/stop-stack.ps1.
#>
param(
  [string]$Python = "",
  [string]$JavaHome = "",
  [switch]$SkipBuild,
  [string]$CollectionPrefix = ""
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$work = Join-Path $repo '.e2e'
New-Item -ItemType Directory -Force $work | Out-Null
$project = 'demo-hirelog-exams'

function Wait-Port([int]$Port, [int]$Seconds, [string]$Name) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { Write-Host "  $Name is up on :$Port"; return }
    Start-Sleep -Seconds 2
  }
  throw "$Name did not start on port $Port within $Seconds s (see the logs in $work)"
}

foreach ($port in 9099, 8080, 7862, 3100) {
  if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $port is already in use. Run e2e-local\stop-stack.ps1 first."
  }
}

if (-not $Python) { $Python = Join-Path $repo 'backend\.venv\Scripts\python.exe' }
if (-not (Test-Path $Python)) {
  throw "Python not found at $Python. Create a venv and run: pip install --require-hashes -r backend/requirements-exams.lock"
}

if ($JavaHome) { $env:JAVA_HOME = $JavaHome }
if ($env:JAVA_HOME) { $env:PATH = "$env:JAVA_HOME\bin;$env:PATH" }
if (-not (Get-Command java -ErrorAction SilentlyContinue)) {
  throw 'The Firestore emulator needs Java. Pass -JavaHome or set JAVA_HOME.'
}

Write-Host '1/4 Firebase emulators (Auth + Firestore, with the repository firestore.rules)'
$fb = Join-Path $work 'fb'
$fbBin = Join-Path $fb 'node_modules\.bin\firebase.cmd'
if (-not (Test-Path $fbBin)) {
  New-Item -ItemType Directory -Force $fb | Out-Null
  Push-Location $fb
  if (-not (Test-Path 'package.json')) { npm init -y | Out-Null }
  npm install firebase-tools@latest | Out-Null
  Pop-Location
}
Copy-Item (Join-Path $repo 'firestore.rules'), (Join-Path $repo 'firestore.indexes.json'), (Join-Path $PSScriptRoot 'firebase.json') $work -Force
Start-Process -FilePath $fbBin -WorkingDirectory $work -WindowStyle Hidden `
  -ArgumentList 'emulators:start', '--only', 'auth,firestore', '--project', $project, '--config', 'firebase.json' `
  -RedirectStandardOutput (Join-Path $work 'emulators.log') -RedirectStandardError (Join-Path $work 'emulators.err.log') | Out-Null
Wait-Port 9099 120 'Auth emulator'
Wait-Port 8080 120 'Firestore emulator'

Write-Host '2/4 Exam API (emulator-backed, development OTP sink on)'
$serviceAccount = Join-Path $work 'sa.json'
& $Python (Join-Path $PSScriptRoot 'make_service_account.py') $serviceAccount $project
$env:FIREBASE_AUTH_EMULATOR_HOST = '127.0.0.1:9099'
$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8080'
$env:FIREBASE_PROJECT_ID = $project
$env:FIREBASE_SERVICE_ACCOUNT_PATH = $serviceAccount
$env:EXAMS_ENABLED = 'true'
$env:EXAM_STORAGE_BOUNDARY = 'true'
if ($CollectionPrefix) { $env:EXAM_COLLECTION_PREFIX = $CollectionPrefix } else { Remove-Item Env:\EXAM_COLLECTION_PREFIX -ErrorAction SilentlyContinue }
$env:EXAM_OTP_ALLOW_DEV_SINK = 'true'
$env:EXAM_OTP_HMAC_SECRET = 'e2e-only-secret'
$env:EXAM_FACULTY_EMAILS = 'operator@example.edu'
$env:ALLOWED_ORIGINS = 'http://127.0.0.1:3100,http://localhost:3100'
$env:ENV = 'development'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONPATH = Join-Path $repo 'backend'
Start-Process -FilePath $Python -WorkingDirectory (Join-Path $repo 'backend') -WindowStyle Hidden `
  -ArgumentList '-m', 'uvicorn', 'app.exam_main:app', '--host', '127.0.0.1', '--port', '7862' `
  -RedirectStandardOutput (Join-Path $work 'backend.out.log') -RedirectStandardError (Join-Path $work 'backend.log') | Out-Null
Wait-Port 7862 60 'Exam API'

Write-Host '3/4 Web app (production build, exams-only mode)'
$env:NEXT_PUBLIC_EXAMS_ENABLED = 'true'
$env:NEXT_PUBLIC_EXAMS_STAGING = 'true'
$env:NEXT_PUBLIC_EXAMS_API_BASE_URL = 'http://127.0.0.1:7862'
$env:NEXT_PUBLIC_FIREBASE_API_KEY = 'fake-api-key'
$env:NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN = '127.0.0.1'
$env:NEXT_PUBLIC_FIREBASE_PROJECT_ID = $project
$env:NEXT_PUBLIC_FIREBASE_AUTH_EMULATOR_URL = 'http://127.0.0.1:9099'
Push-Location (Join-Path $repo 'frontend')
if (-not $SkipBuild -or -not (Test-Path '.next\BUILD_ID')) {
  cmd.exe /c "npm run build > `"$work\frontend-build.log`" 2>&1"
  if ($LASTEXITCODE -ne 0) { Pop-Location; throw "The frontend build failed; see $work\frontend-build.log" }
}
Start-Process -FilePath 'npm.cmd' -WorkingDirectory (Get-Location) -WindowStyle Hidden `
  -ArgumentList 'run', 'start', '--', '--hostname', '127.0.0.1', '--port', '3100' `
  -RedirectStandardOutput (Join-Path $work 'frontend.log') -RedirectStandardError (Join-Path $work 'frontend.err.log') | Out-Null
Pop-Location
Wait-Port 3100 60 'Web app'

Write-Host '4/4 Ready'
Write-Host "  web   http://127.0.0.1:3100   api http://127.0.0.1:7862   logs $work"
Write-Host '  run the browser journeys with: e2e-local\run-e2e.ps1'
