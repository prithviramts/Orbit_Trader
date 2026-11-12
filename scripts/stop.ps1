# Graceful stop: the worker finishes its cycle, records a shutdown event and releases its lease.
. "$PSScriptRoot\_common.ps1"
New-Item -ItemType Directory -Force $RunDir | Out-Null
Set-Content (Join-Path $RunDir "worker.stop") "stop"
Set-Content (Join-Path $RunDir "manual-stop") "Stopped on purpose; the auto-start watchdog waits until start.ps1 runs."

function Stop-Tree($id) {
  Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Tree $_.ProcessId }
  Stop-Process -Id $id -Force -Confirm:$false -ErrorAction SilentlyContinue
}

$wp = Join-Path $RunDir "worker.pid"
if (Test-Path $wp) {
  $id = [int](Get-Content $wp)
  $deadline = (Get-Date).AddSeconds(45)
  while ((Get-Date) -lt $deadline -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) { Start-Sleep -Milliseconds 500 }
  if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
    Write-Host "Worker did not exit within 45 s; terminating. Its lease expires after 90 s; state is durable." -ForegroundColor Yellow
    Stop-Tree $id
  } else { Write-Host "Worker stopped gracefully." }
  Remove-Item $wp -ErrorAction SilentlyContinue
}

$ap = Join-Path $RunDir "api.pid"
if (Test-Path $ap) {
  Stop-Tree ([int](Get-Content $ap))
  Remove-Item $ap -ErrorAction SilentlyContinue
  Write-Host "API stopped."
}
Write-Host "Orders already accepted by the broker and open positions are NOT affected by stopping Orbit."
