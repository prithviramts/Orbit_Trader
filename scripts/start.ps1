# Starts the dashboard/API and the single trading worker as two background processes.
. "$PSScriptRoot\_common.ps1"
New-Item -ItemType Directory -Force $RunDir, $LogDir | Out-Null
if (-not (Test-Path (Join-Path $Frontend "dist\index.html"))) { throw "Dashboard not built. Run .\scripts\setup.ps1 first." }

Push-Location $Backend
& uv run --frozen python -m orbit.cli check | Out-Host
$ok = $LASTEXITCODE
Pop-Location
if ($ok -ne 0) { throw "Configuration problems (see above). Nothing was started." }

function Start-Orbit($name, $module) {
  $pidFile = Join-Path $RunDir "$name.pid"
  if (Test-Path $pidFile) {
    $old = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) { Write-Host "$name already running (pid $old)."; return }
  }
  $out = Join-Path $LogDir "$name.console.log"
  $p = Start-Process -FilePath "uv" -ArgumentList @("run", "--frozen", "python", "-m", $module) -WorkingDirectory $Backend `
       -RedirectStandardOutput $out -RedirectStandardError "$out.err" -WindowStyle Hidden -PassThru
  Set-Content $pidFile $p.Id
  Write-Host "$name started (pid $($p.Id)). Console log: $out"
}

Start-Orbit "worker" "orbit.worker"
Start-Orbit "api" "orbit.api"
Start-Sleep -Seconds 4

$port = 8765
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^ORBIT_PORT=(\d+)' -ErrorAction SilentlyContinue
  if ($m) { $port = $m.Matches[0].Groups[1].Value }
}
Write-Host ""
Write-Host "Dashboard:  http://127.0.0.1:$port   (this computer only)" -ForegroundColor Green
Write-Host "Logs:       $LogDir\worker.log  and  $LogDir\api.log"
Write-Host "Stop with:  .\scripts\stop.ps1"
Write-Host "Keep this computer awake and online at the evening evaluation and the first 5 minutes after the US open when an order is pending."
