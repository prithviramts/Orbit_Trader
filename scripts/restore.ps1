# Usage: .\scripts\restore.ps1 data\backups\orbit-YYYYMMDDTHHMMSSZ-manual.sqlite3
# Stop Orbit first. The current database is backed up before being replaced.
param([Parameter(Mandatory = $true)][string]$File)
. "$PSScriptRoot\_common.ps1"
if ((Test-Path (Join-Path $RunDir "worker.pid")) -or (Test-Path (Join-Path $RunDir "api.pid"))) { throw "Stop Orbit first: .\scripts\stop.ps1" }
Orbit-Py -m orbit.cli restore (Resolve-Path $File).Path
