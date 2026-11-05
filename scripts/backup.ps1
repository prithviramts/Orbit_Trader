# Consistent online backup of the SQLite database into data\backups (safe while Orbit runs).
. "$PSScriptRoot\_common.ps1"
Orbit-Py -m orbit.cli backup
