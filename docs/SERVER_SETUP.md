# Running Orbit 24/7 on a private server

**Status:** written and syntax-checked, **not yet run on a real server** (no server or Docker was available
when it was written). The first run is a supervised test: expect to fix small things together.

Goal: the worker runs around the clock; you open the dashboard from your phone or laptop over a
private network; you get a push only when something needs you; and an external monitor alerts you if
the server itself goes quiet. Nothing is exposed to the public internet.

```
 phone / laptop ──(Tailscale, HTTPS)──► server: tailscale serve :443 ─► 127.0.0.1:8765 API container
                                                                        worker container ─► Alpaca PAPER
 external heartbeat monitor ◄── ping every 5 min from the worker (alerts you if pings stop)
```

## What you need (you create these; nothing is bought automatically)
1. **A VPS** with Ubuntu 24.04 LTS, 1–2 vCPU, **2 GB RAM**, ~20 GB disk, from any provider you trust.
   Compare current prices yourself; the smallest tier that meets this is enough.
2. **Tailscale** account (has a free personal plan; check current terms). Install the app on your phone
   and laptop and sign in.
3. **A heartbeat/uptime monitor** that alerts when pings *stop* (a "dead man's switch", e.g.
   healthchecks.io). Create a check with period 10 min, grace 10 min, and copy its ping URL.
4. Optional: the **ntfy** app on your phone for event pushes (see SETUP.md).

## Step 1: SSH key from Windows (once)
```powershell
ssh-keygen -t ed25519 -C "orbit-server"
```
Press Enter for the default path and set a passphrase. When you create the VPS, paste the contents of
`C:\Users\<you>\.ssh\id_ed25519.pub` as the SSH key. Then connect: `ssh ubuntu@<server-ip>`.

## Step 2: Run the setup script on the server
```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/prithviramts/Orbit_Trader.git /tmp/orbit-src
sudo bash /tmp/orbit-src/deploy/server-setup.sh
```
It installs Docker, a firewall (SSH + Tailscale only), automatic security updates, key-only SSH (if your
key is present), Tailscale, the code in `/opt/orbit`, data in `/srv/orbit/data`, a protected config file
`/etc/orbit/orbit.env`, the Docker image, and a daily backup timer. It does not start trading.

## Step 3: Private HTTPS address
```bash
sudo tailscale up                 # open the printed link and approve the server
sudo tailscale serve --bg 8765    # HTTPS for your tailnet only
tailscale serve status            # shows https://<server-name>.<tailnet>.ts.net
```
In the Tailscale admin console, enable **MagicDNS** and **HTTPS certificates** if it asks.

## Step 4: Configure (on the server, never in chat)
```bash
sudo nano /etc/orbit/orbit.env
```
Set:
- `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`: your paper keys. Regenerating them for the server is a good
  idea; your laptop copy then stops working, which is what you want once the server is the trader.
- `ORBIT_PUBLIC_ORIGIN=https://<server-name>.<tailnet>.ts.net`, exactly as shown by `tailscale serve status`.
- `ORBIT_HEARTBEAT_URL=<ping URL from your monitor>`.
- Optional: `NTFY_TOPIC` / `NTFY_TOKEN`.

## Step 5: Password and start
```bash
sudo bash /opt/orbit/deploy/set-password.sh
sudo bash /opt/orbit/deploy/update.sh
```
On your phone (Tailscale connected), open `https://<server-name>.<tailnet>.ts.net/status` and bookmark it.

## Step 6: Stop the laptop copy
**Only one trader may use the account.** On the laptop run `.\scripts\stop.ps1`. The two copies have
separate databases, so their leases cannot see each other. If both ran in PAPER_AUTO they could both act,
and each would then pause on the other's "foreign" orders.

## Step 7: Verify, then observe for a week
- Operations → **Run connection test** (read-only), and check the **External heartbeat** line shows a recent ping.
- Test the dead man's switch: `sudo docker compose -f /opt/orbit/deploy/compose.yaml stop worker`, wait
  for the monitor to alert you, then `sudo bash /opt/orbit/deploy/update.sh`.
- Test a backup: `sudo systemctl start orbit-backup.service`, then pull it to the laptop:
  `.\scripts\pull-backups.ps1 -Server <server-name> -User ubuntu`.
- Leave it in **OBSERVE** for at least a week of evening decisions before enabling paper automation.

## Everyday operations
| Task | Command (on the server) |
|---|---|
| Update to the latest `main` (backs up first) | `sudo bash /opt/orbit/deploy/update.sh` |
| Status | `sudo docker compose -f /opt/orbit/deploy/compose.yaml ps` |
| Logs | `sudo docker compose -f /opt/orbit/deploy/compose.yaml logs --tail 100 worker` |
| Stop everything (positions/orders at the broker are unaffected) | `sudo docker compose -f /opt/orbit/deploy/compose.yaml down` |
| Manual backup | `sudo systemctl start orbit-backup.service` |
| Restore | stop the stack, copy a backup over `/srv/orbit/data/orbit.sqlite3` (keep the old file), `update.sh` |

Only update after the GitHub Actions CI check on that commit is green.

## What can still go wrong (honestly)
- The VPS provider, Tailscale, Alpaca or your phone's push delivery can each fail. The heartbeat
  covers "Orbit stopped". It does not cover "Alpaca is down" (Orbit alerts on that itself) or
  "the monitor is down".
- Security relies on Tailscale access control, the dashboard password, and a patched server. Anyone with
  root on the server can read the keys in `/etc/orbit/orbit.env`. Keep the server single-purpose.
- SQLite on the VPS's local disk is fine for this. Don't put `/srv/orbit/data` on network storage.
