#!/usr/bin/env bash
# Start or update the Orbit stack on the server:  sudo bash /opt/orbit/deploy/update.sh
# Backs up first, pulls the latest main, rebuilds, restarts gracefully, then checks health.
set -euo pipefail
APP_DIR=/opt/orbit
COMPOSE="docker compose -f $APP_DIR/deploy/compose.yaml"
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

grep -qE '^ORBIT_PUBLIC_ORIGIN=https://' /etc/orbit/orbit.env || { echo "Set ORBIT_PUBLIC_ORIGIN=https://... in /etc/orbit/orbit.env first."; exit 1; }

if $COMPOSE ps --status running --services 2>/dev/null | grep -q '^api$'; then
  echo "== Backup before update =="
  $COMPOSE exec -T api python -m orbit.cli backup >/dev/null && echo "backup ok"
fi

echo "== Pull and build =="
git -C "$APP_DIR" pull --ff-only
docker build -q -f "$APP_DIR/deploy/Dockerfile" -t orbit-trading-lab "$APP_DIR"

echo "== Restart (worker gets SIGTERM and up to 45 s to finish its cycle) =="
$COMPOSE up -d --remove-orphans

echo "== Health =="
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
    curl -fsS http://127.0.0.1:8765/api/health; echo
    echo "Running at $(grep -E '^ORBIT_PUBLIC_ORIGIN=' /etc/orbit/orbit.env | cut -d= -f2-)  (reachable only on your tailnet)"
    exit 0
  fi
  sleep 2
done
echo "API did not become healthy. Logs:"; $COMPOSE logs --tail 50
exit 1
