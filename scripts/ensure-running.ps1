# Watchdog: starts Orbit if it is not running. Run by the "Orbit Trading Lab" scheduled task at logon and every 10 minutes.
# Respects a deliberate stop: after .\scripts\stop.ps1 it does nothing until .\scripts\start.ps1 is run again.
. "$PSScriptRoot\_common.ps1"
New-Item -ItemType Directory -Force $RunDir, $LogDir | Out-Null
$log = Join-Path $LogDir "watchdog.log"
function Note($msg) { Add-Content $log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" }

if (Test-Path (Join-Path $RunDir "manual-stop")) { exit 0 }

function Alive($name) {
  $pidFile = Join-Path $RunDir "$name.pid"
  if (-not (Test-Path $pidFile)) { return $false }
  $id = Get-Content $pidFile -ErrorAction SilentlyContinue
  return [bool]($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue))
}

if ((Alive "worker") -and (Alive "api")) { exit 0 }

Note "Orbit not fully running (worker=$(Alive 'worker'), api=$(Alive 'api')); starting it."
try {
  & (Join-Path $PSScriptRoot "start.ps1") *>&1 | Out-Null
  Note "start.ps1 finished (worker=$(Alive 'worker'), api=$(Alive 'api'))."
} catch {
  Note "start.ps1 failed: $($_.Exception.Message)"
  exit 1
}
