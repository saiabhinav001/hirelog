$ErrorActionPreference = "Stop"

# Recreate backend/.venv with Python 3.11 and install dependencies.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $scriptDir "..")

function Assert-LastExitCode($message) {
  if ($LASTEXITCODE -ne 0) {
    Write-Error $message
    exit 1
  }
}

& py -3.11 -c "import sys; print(sys.version)" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Error "Python 3.11 is required. Install it and ensure 'py -3.11' works."
  exit 1
}

if (Test-Path .venv) {
  Remove-Item -Recurse -Force .venv
}

& py -3.11 -m venv .venv
Assert-LastExitCode "Failed to create the virtual environment."

$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
& .\.venv\Scripts\python -m pip install --upgrade pip
Assert-LastExitCode "Failed to upgrade pip."

& .\.venv\Scripts\python -m pip install -r requirements.txt
Assert-LastExitCode "Failed to install Python dependencies."

# Install spaCy model with a deterministic URL to avoid CLI lookup failures.
$spacyModelVersion = @'
import json
import sys
import urllib.request

import spacy

model = "en_core_web_sm"
compat_url = "https://raw.githubusercontent.com/explosion/spacy-models/master/compatibility.json"

version = None
try:
    with urllib.request.urlopen(compat_url, timeout=15) as r:
        compat = json.load(r)
    version = (compat.get(spacy.__version__, {}).get(model) or [None])[0]
except Exception:
    version = None

# Fallback for spaCy 3.7.x if compatibility lookup fails.
if not version and spacy.__version__.startswith("3.7."):
    version = "3.7.1"

if not version:
    sys.exit(1)

print(version)
'@ | & .\.venv\Scripts\python -
Assert-LastExitCode "Could not resolve spaCy model version for download."

if ([string]::IsNullOrWhiteSpace($spacyModelVersion)) {
  Write-Error "Could not resolve spaCy model version for download."
  exit 1
}

$releaseTag = "en_core_web_sm-$spacyModelVersion"
$wheel = "$releaseTag-py3-none-any.whl"
$wheelUrl = "https://github.com/explosion/spacy-models/releases/download/$releaseTag/$wheel"

& .\.venv\Scripts\python -m pip install $wheelUrl
Assert-LastExitCode "Failed to install spaCy model $releaseTag."

Write-Host "Backend environment ready."
