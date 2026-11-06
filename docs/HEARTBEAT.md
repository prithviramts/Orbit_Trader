# "Orbit is down" warning (heartbeat)

Orbit can tell you about problems only while it is running. If the laptop sleeps, crashes or loses
internet, it goes quiet, and silence looks the same as "all fine". A heartbeat fixes that. Every 5 minutes the
worker sends a tiny "I'm alive" ping to a free outside service. If the pings **stop**, that service
emails or messages you.

The ping URL acts like a password: anyone who has it can fake "alive". Put it in `.env` only, never in chat,
screenshots or git.

## Setup with Healthchecks.io (free, about 5 minutes)
1. Go to **healthchecks.io** and create a free account yourself (Orbit and Claude never create accounts for you).
2. It creates a first "check" for you. Open it and press **Edit Schedule** (or "Change schedule"):
   - **Period**: 5 minutes.
   - **Grace time**: 30 minutes. A short sleep or Wi-Fi drop then won't wake you, but a real outage will.
3. Copy its **ping URL**. It looks like `https://hc-ping.com/` followed by a long code.
4. Open `.env` (`notepad .env`) and paste it after `ORBIT_HEARTBEAT_URL=`. Save.
5. In PowerShell, from the project folder:
   ```powershell
   cd backend
   uv run python -m orbit.cli heartbeat-test   # sends one ping now
   cd ..
   .\scripts\stop.ps1; .\scripts\start.ps1     # restart so the worker pings every 5 minutes (safe outside 18:30-18:35 Dubai)
   ```
6. The check on healthchecks.io turns green ("up"). Under **Integrations** you can add Telegram as well as email.

## Good to know
- **On the laptop**, expect "down" alerts whenever the laptop sleeps or is shut down. That is the heartbeat working. Either keep
  the laptop awake (Settings → Power → Sleep: Never when plugged in), or pause the check in healthchecks.io
  when you switch the laptop off on purpose.
- **On a server**, a "down" alert is always worth reading: the server or Orbit really stopped.
- Only a **completed** worker cycle counts as alive. A worker that is stuck or failing every cycle stops pinging too.
- The Operations page shows the last successful ping and any ping error. A failed ping never affects trading.
