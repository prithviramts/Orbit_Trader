#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 24.04 LTS server. Run as root:  sudo bash deploy/server-setup.sh
# Safe to re-run. It does NOT start trading: the app starts in OBSERVE mode as always.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/prithviramts/Orbit_Trader.git}"
APP_DIR=/opt/orbit
DATA_DIR=/srv/orbit/data
ENV_FILE=/etc/orbit/orbit.env
ADMIN_USER="${SUDO_USER:-root}"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
. /etc/os-release
[ "${VERSION_ID:-}" = "24.04" ] || echo "WARNING: tested for Ubuntu 24.04; you have ${PRETTY_NAME:-unknown}."

echo "== 1/7 System updates and packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -y -q upgrade
apt-get -y -q install docker.io docker-compose-v2 git ufw unattended-upgrades sqlite3 curl ca-certificates
systemctl enable --now docker
# Automatic security updates
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

echo "== 2/7 Firewall: allow SSH and Tailscale only =="
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow in on tailscale0
ufw --force enable

echo "== 3/7 SSH: key-only login (only if you already have a key installed) =="
if [ -s "$(getent passwd "$ADMIN_USER" | cut -d: -f6)/.ssh/authorized_keys" ]; then
  cat > /etc/ssh/sshd_config.d/10-orbit-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  systemctl reload ssh || systemctl reload sshd || true
  echo "Password SSH login disabled (key-only)."
else
  echo "SKIPPED: no SSH key found for $ADMIN_USER. Add one, then re-run to disable password login."
fi

echo "== 4/7 Tailscale (private network + HTTPS) =="
if ! command -v tailscale >/dev/null; then
  # Official installer from tailscale.com (adds Tailscale's apt repository).
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo ">>> Run:  sudo tailscale up   and open the printed link to add this server to your tailnet."
fi

echo "== 5/7 Code and data directories =="
if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull --ff-only; else git clone "$REPO_URL" "$APP_DIR"; fi
mkdir -p "$DATA_DIR" /etc/orbit
chown -R 10001:10001 /srv/orbit
chmod 700 "$DATA_DIR"
if [ ! -f "$ENV_FILE" ]; then
  install -m 600 -o root -g root "$APP_DIR/.env.example" "$ENV_FILE"
  cat >> "$ENV_FILE" <<'EOF'

# --- server settings ---
# Set this to your Tailscale HTTPS name (see `tailscale serve status`), e.g. https://orbit.tail1234.ts.net
ORBIT_PUBLIC_ORIGIN=
# Optional dead-man's switch ping URL (https) from your uptime/heartbeat service:
ORBIT_HEARTBEAT_URL=
EOF
  echo ">>> Edit $ENV_FILE (sudo nano $ENV_FILE): paste Alpaca PAPER keys and set ORBIT_PUBLIC_ORIGIN."
fi

echo "== 6/7 Build the image =="
docker build -q -f "$APP_DIR/deploy/Dockerfile" -t orbit-trading-lab "$APP_DIR"

echo "== 7/7 Daily backups (kept 30 days in $DATA_DIR/backups) =="
cat > /etc/systemd/system/orbit-backup.service <<EOF
[Unit]
Description=Orbit Trading Lab database backup
[Service]
Type=oneshot
ExecStart=/usr/bin/docker compose -f $APP_DIR/deploy/compose.yaml exec -T api python -m orbit.cli backup
ExecStartPost=/usr/bin/find $DATA_DIR/backups -name 'orbit-*.sqlite3' -mtime +30 -delete
EOF
cat > /etc/systemd/system/orbit-backup.timer <<'EOF'
[Unit]
Description=Daily Orbit backup
[Timer]
OnCalendar=*-*-* 10:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now orbit-backup.timer

cat <<EOF

Setup finished. Remaining steps (see docs/SERVER_SETUP.md):
  1. sudo tailscale up                      (if not done)
  2. sudo tailscale serve --bg 8765         (HTTPS on your tailnet; note the https://...ts.net name)
  3. sudo nano $ENV_FILE                    (keys + ORBIT_PUBLIC_ORIGIN = that https name)
  4. sudo bash $APP_DIR/deploy/set-password.sh
  5. sudo bash $APP_DIR/deploy/update.sh    (starts the stack and checks health)
EOF
