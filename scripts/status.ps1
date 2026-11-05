# Quick health check (no account data is returned by this endpoint).
. "$PSScriptRoot\_common.ps1"
try { Invoke-RestMethod "http://127.0.0.1:8765/api/health" | Format-List }
catch { Write-Host "API not reachable on 127.0.0.1:8765. Is Orbit started?" -ForegroundColor Yellow }
