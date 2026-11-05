# Restarts only the dashboard/API process (for example after an update). The trading worker keeps running,
# so pending orders and the market schedule are not interrupted.
. "$PSScriptRoot\_common.ps1"

function Stop-Tree($id) {
  Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Tree $_.ProcessId }
  Stop-Process -Id $id -Force -Confirm:$false -ErrorAction SilentlyContinue
}

$ap = Join-Path $RunDir "api.pid"
if (Test-Path $ap) {
  Stop-Tree ([int](Get-Content $ap))
  Remove-Item $ap -ErrorAction SilentlyContinue
  Write-Host "Dashboard stopped (worker untouched)."
}
Start-Sleep -Seconds 1
# start.ps1 skips anything already running, so only the API is started here.
& "$PSScriptRoot\start.ps1"
