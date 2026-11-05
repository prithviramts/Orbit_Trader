# Server deployment overview

**Step-by-step instructions are in [SERVER_SETUP.md](SERVER_SETUP.md).** This page covers the background.

Docker was not available on the development machine, so `deploy/Dockerfile` and `deploy/compose.yaml`
have **not** been built or run. Treat them as a reviewed starting point. Nothing has been purchased or
provisioned.

## Prerequisites before any remote use
- A private Linux host you control. Remote access **only** through a private VPN/tunnel (e.g. WireGuard
  or Tailscale) or an HTTPS reverse proxy with a valid certificate. Never expose the API port, the
  database, or the worker directly to the internet.
- Host firewall: deny inbound except SSH (key-only) and your VPN.
- `ORBIT_DEPLOYMENT_MODE=remote` and `ORBIT_PUBLIC_ORIGIN=https://...`. The app refuses to bind a
  non-loopback address without these; in remote mode cookies are `Secure`. The reverse proxy must pass the
  public Host header so Host/Origin checks match.
- Secrets in `/etc/orbit/orbit.env` (root-owned, `chmod 600`), never in the image or the repo.

## Steps
See SERVER_SETUP.md (`deploy/server-setup.sh`, `deploy/set-password.sh`, `deploy/update.sh`).
- Exactly one `worker` service. A second worker is refused by the DB lease and the file lock, but do
  not scale it.
- Containers run as uid 10001, read-only root filesystem, all capabilities dropped, resource limits,
  no Docker socket, only `/srv/orbit/data` mounted (bind mount, owned by uid 10001).
- `SIGTERM` → the worker finishes its cycle, records shutdown and releases the lease (45 s grace).
  After any restart it reconciles with the broker before acting.
- Health: `GET /api/health` (no account data).
- Backups: daily systemd timer into `/srv/orbit/data/backups` (30-day retention); copy off the host with
  `scripts\pull-backups.ps1`. Run a restore drill before trusting unattended hosting.

## External watchdog
A host that is down cannot report itself. Set `ORBIT_HEARTBEAT_URL` (https): after each completed cycle
the worker pings it at most every 5 minutes, and your monitor alerts you when pings stop. Ping failures
never affect trading. Status is shown on the Operations page.

## SQLite on a server
Keep SQLite on local disk (not NFS/SMB) with one worker and one API. Move to PostgreSQL before running
several hosts, multiple writers or HA.
