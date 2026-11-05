# Shared helpers for Orbit scripts. Paths are derived from this file's location (no hardcoded drive paths).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$DataDir = Join-Path $Root "data"
$RunDir = Join-Path $DataDir "run"
$LogDir = Join-Path $DataDir "logs"

function Need($cmd, $hint) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd not found. $hint" }
}

function Orbit-Py {
  Push-Location $Backend
  try {
    & uv run --frozen python @args
    if ($LASTEXITCODE -ne 0) { throw "python exited with code $LASTEXITCODE" }
  } finally { Pop-Location }
}
