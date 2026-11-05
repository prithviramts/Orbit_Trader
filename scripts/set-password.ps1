# Prompts (hidden input) for the local dashboard password. Only an Argon2id hash is stored.
. "$PSScriptRoot\_common.ps1"
Orbit-Py -m orbit.cli set-password
