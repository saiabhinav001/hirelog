<#
.SYNOPSIS
  Stops the local rehearsal stack started by start-stack.ps1 (web app, exam API and the emulators).
#>
$ErrorActionPreference = 'SilentlyContinue'
foreach ($port in 3100, 7862, 9099, 8080, 4400, 4500, 9150) {
  Get-NetTCPConnection -LocalPort $port -State Listen | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force
    Write-Host "stopped the process listening on :$port (pid $($_.OwningProcess))"
  }
}
