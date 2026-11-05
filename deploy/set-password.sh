#!/usr/bin/env bash
# Set the dashboard password on the server (hidden prompt; only an Argon2id hash is stored).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
docker compose -f /opt/orbit/deploy/compose.yaml run --rm -it --no-deps api python -m orbit.cli set-password
