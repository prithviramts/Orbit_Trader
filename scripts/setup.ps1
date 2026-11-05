# One-time setup: installs pinned dependencies, builds the dashboard, prepares .env.
. "$PSScriptRoot\_common.ps1"
Need "uv" "Install it: winget install astral-sh.uv   (https://docs.astral.sh/uv/)"
Need "node" "Install Node.js 20+ LTS from https://nodejs.org/"
Need "npm" "Install Node.js 20+ LTS from https://nodejs.org/"

Write-Host "== Backend (Python 3.12, versions locked in backend/uv.lock) ==" -ForegroundColor Cyan
Push-Location $Backend
uv sync --frozen
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "uv sync failed" }
Pop-Location

Write-Host "== Dashboard (versions locked in frontend/package-lock.json) ==" -ForegroundColor Cyan
Push-Location $Frontend
npm ci --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "npm ci failed" }
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "dashboard build failed" }
Pop-Location

New-Item -ItemType Directory -Force $DataDir, $RunDir, $LogDir | Out-Null
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
  Copy-Item (Join-Path $Root ".env.example") $envFile
  Write-Host "Created .env from .env.example. Open it in Notepad and paste your Alpaca PAPER keys." -ForegroundColor Yellow
}

# Restrict .env and data\ to the current user. .env is still plaintext: this does not stop malware or an administrator.
& icacls $envFile /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
& icacls $DataDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null

Write-Host "== Configuration check ==" -ForegroundColor Cyan
Push-Location $Backend
& uv run --frozen python -m orbit.cli check
Pop-Location

Write-Host ""
Write-Host "Next: set the dashboard password:  .\scripts\set-password.ps1" -ForegroundColor Green
Write-Host "Then start everything:            .\scripts\start.ps1" -ForegroundColor Green
