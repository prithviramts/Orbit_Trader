# Builds a MOCK lifecycle demo (decision -> order -> partial fill -> exit) in data-mock-demo\ and serves it
# on http://127.0.0.1:8766. Uses the in-memory mock broker: no network, no credentials, no paper orders.
# Your real database in data\ is never touched.
param([switch]$Serve)
. "$PSScriptRoot\_common.ps1"
$demoDir = Join-Path $Root "data-mock-demo"
$env:ORBIT_DATA_DIR = $demoDir
$env:ORBIT_PORT = "8766"
if (-not $Serve) {
  if (Test-Path $demoDir) { Remove-Item -Recurse -Force $demoDir }
  Orbit-Py -m orbit.demo
  Write-Host "Set a password for the demo dashboard (separate from your real one):"
  Orbit-Py -m orbit.cli set-password
}
Write-Host "Serving MOCK demo on http://127.0.0.1:8766 (Ctrl+C to stop). Everything there is labelled MOCK." -ForegroundColor Yellow
Orbit-Py -m orbit.api
