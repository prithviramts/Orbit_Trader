# Copies the server's database backups to this laptop over Tailscale (off-server copy).
# Usage: .\scripts\pull-backups.ps1 -Server orbit-server -User ubuntu
param([Parameter(Mandatory = $true)][string]$Server, [string]$User = "ubuntu")
. "$PSScriptRoot\_common.ps1"
Need "scp" "Windows' built-in OpenSSH client is required (Settings > Apps > Optional features > OpenSSH Client)."
$dest = Join-Path $Root "server-backups"
New-Item -ItemType Directory -Force $dest | Out-Null
& icacls $dest /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
# Backups are owned by the container user; copy them via sudo into a temp folder readable by your SSH user.
ssh "$User@$Server" "sudo rm -rf /tmp/orbit-backups && sudo cp -r /srv/orbit/data/backups /tmp/orbit-backups && sudo chown -R `$(whoami) /tmp/orbit-backups"
scp -q "$User@${Server}:/tmp/orbit-backups/*.sqlite3" $dest
ssh "$User@$Server" "rm -rf /tmp/orbit-backups"
Get-ChildItem $dest | Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name, Length, LastWriteTime
