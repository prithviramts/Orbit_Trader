# Proves a backup restores correctly (into a temporary copy) without touching the live database.
. "$PSScriptRoot\_common.ps1"
Orbit-Py -m orbit.cli restore-drill
