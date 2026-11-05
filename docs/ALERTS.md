# Trade alerts on your phone (Telegram)

Orbit sends a message to **your own private Telegram chat** for every planned order, every order sent and
every fill. Each message includes what was bought or sold, the price, the value in USD and AED, **why** the rule
decided it, the profit or loss on a sale, and what the strategy holds afterwards. Critical problems (missed
window, rejection, security pause) are marked "⛔ ACTION NEEDED". Routine HOLD results come as one daily summary.

Telegram is free. Your bot only talks to you. The bot token is a secret: it goes in `.env` only, never in chat,
screenshots or git.

## Setup (about 5 minutes)
1. Install Telegram on your phone and sign in.
2. In Telegram, search for **@BotFather** (blue tick), press **Start**, send `/newbot`.
   - Name: anything, e.g. `Orbit Alerts`.
   - Username: must end in `bot`, e.g. `orbit_prithvi_alerts_bot`.
   BotFather replies with a **token** that looks like `123456789:AA...`. Copy it.
3. Open `.env` in the project folder (`notepad .env`) and paste it after `TELEGRAM_BOT_TOKEN=`. Save.
4. In Telegram, open your new bot (tap the link BotFather sent) and press **Start**. Send it any message, e.g. `hi`.
5. In PowerShell, from the project folder:
   ```powershell
   cd backend
   uv run python -m orbit.cli telegram-setup   # finds your chat and saves TELEGRAM_CHAT_ID in .env
   uv run python -m orbit.cli telegram-test    # sends a test message straight away
   cd ..
   .\scripts\stop.ps1; .\scripts\start.ps1    # restart so the worker picks it up (safe outside the 18:30 window)
   ```
6. In the dashboard: **Operations → Phone notifications**, tick **Send push notifications**, then press
   **Send test notification**. A message should arrive within about 30 seconds.

## Good to know
- **Quiet hours** (default 23:00–07:00 Dubai) hold non-critical messages until morning, so the 01:15 decision
  summary arrives at 07:00. Fills happen around 18:30–18:35, outside quiet hours. Critical alerts always go through.
- "Accepted" on the Operations page means Telegram accepted the message. It cannot prove your phone showed it.
- If the laptop or server is off, nothing is sent. The external heartbeat (server setup) covers that case.
- Lost your phone or leaked the token? In @BotFather send `/revoke`, put the new token in `.env`, restart.
