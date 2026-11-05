# Future server deployment (documented, not provisioned, not tested here)

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

## Steps (when you decide to do this)
```bash
docker build -f deploy/Dockerfile -t orbit-trading-lab .
docker compose -f deploy/compose.yaml run --rm api uv run --frozen --no-dev python -m orbit.cli set-password
docker compose -f deploy/compose.yaml up -d
```
- Exactly one `worker` service. A second worker is refused by the DB lease and the file lock, but do
  not scale it.
- Containers run as uid 10001, read-only root filesystem, all capabilities dropped, resource limits,
  no Docker socket, only the `orbit-data` volume mounted.
- `SIGTERM` → the worker finishes its cycle, records shutdown and releases the lease (45 s grace).
  After any restart it reconciles with the broker before acting.
- Health: `GET /api/health` (no account data).
- Backups: `docker compose exec api uv run --frozen --no-dev python -m orbit.cli backup`; copy
  `/data/backups` off the host (encrypted). Run `restore-drill` before trusting unattended hosting.

## External watchdog (needed for unattended operation)
A host that is down cannot report itself. Plan: the worker pings an external heartbeat service
(e.g. a dead-man's-switch URL) every cycle; that service alerts your phone if pings stop for 10 min.
Not implemented in v1, because it needs an external account you choose.

## SQLite on a server
Keep SQLite on local disk (not NFS/SMB) with one worker and one API. Move to PostgreSQL before running
several hosts, multiple writers or HA.
