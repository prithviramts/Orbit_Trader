# Setup and first run (Windows)

## 0. Prerequisites
- **uv** (Python manager): `winget install astral-sh.uv`. Python 3.12 is installed by uv if missing.
- **Node.js 20+ LTS**: https://nodejs.org (only needed to build the dashboard).

## 1. Install
```powershell
cd C:\PROJECTS\Personal\PAPER_BOT_AI
.\scripts\setup.ps1
```
This installs exactly the versions in `backend/uv.lock` and `frontend/package-lock.json`, builds the
dashboard, creates `.env` from `.env.example`, and restricts `.env` and `data\` to your Windows user.

## 2. Put your Alpaca PAPER keys in `.env` (locally, never in chat)
1. Sign in at https://app.alpaca.markets, switch to your **Paper** account (top-left account menu).
   Ideally create a **dedicated** paper account with no other holdings or orders: Orbit pauses if it
   sees positions or orders it did not create.
2. Generate API keys for that paper account.
3. `notepad .env` and fill:
   ```
   APCA_API_KEY_ID=<your paper key id>
   APCA_API_SECRET_KEY=<your paper secret>
   APCA_API_BASE_URL=https://paper-api.alpaca.markets
   ```
   Do not change the base URL. Any live URL makes Orbit refuse to start.
4. Enable MFA on your Alpaca login if offered. `.env` is a plaintext file: it is protected from other
   Windows users by file permissions, but not from malware or an administrator on this PC.

**Rotating/revoking keys:** in the Alpaca dashboard, regenerate the paper keys (this invalidates the
old pair), paste the new ones into `.env`, then `.\scripts\stop.ps1` and `.\scripts\start.ps1`.

## 3. Set the dashboard password and start
```powershell
.\scripts\set-password.ps1   # min 12 characters; typed into a hidden prompt
.\scripts\start.ps1
```
Open http://127.0.0.1:8765 and sign in.

## 4. Validate the connection (read-only)
Setup page → **Run connection test**. It reads account status, cash, equity, buying power, positions,
open orders, SPY tradability/fractionability, the market clock and ~400 days of daily bars. It reports
`Orders placed: 0`. Check:
- *environment*: paper endpoint verified.
- *account*: status ACTIVE; note the balance is the paper default, not a budget.
- *positions/open_orders*: ideally empty.
- *asset_SPY*: tradable and fractionable true.
- *market_data*: feed IEX, ≥200 bars, last date = the latest completed session.

## 5. Watch one observed decision
The worker evaluates **15 minutes after each NYSE close**. In Dubai that is normally 01:15 (winter) or
00:15 (US summer time); early-close days are earlier. Exact times are on the Overview and Operations pages.
After it runs, open **Decisions** → the newest row → **Explain this decision**. In OBSERVE mode it says
what the rule *would* do ("OBSERVE mode: the rule would BUY but no order intent is created").
Check the close, the average, the 200 inputs, and that the reasoning matches the rule.

## 6. Only then, enable paper automation
Setup page → all checks green → **Enable paper automation…** → re-enter your password. If you are flat
and the latest completed close is above its average, Orbit immediately creates one entry intent for the
next open (max USD 250 / AED 918.13). Supervise the first sessions: be online at the US open
(Overview → *Next session open*) and confirm the order resolves on Orders & positions.

To go back: Orders & positions → **Return to OBSERVE** (pending intents are skipped; any order already
at the broker and any position stay; cancel or close them explicitly if you want).

## Optional: phone notifications (ntfy)
1. Install the ntfy app (Android: Play Store/F-Droid; iOS: App Store).
2. Choose a long random topic name, subscribe to it in the app, and put it in `.env` as `NTFY_TOPIC=...`.
   Restart Orbit.
3. Operations → tick **Send push notifications** → **Send test notification**. Confirm on the phone;
   Orbit can only see that ntfy *accepted* the message.
- A topic on ntfy.sh is public to anyone who guesses it, so pushes contain only a redacted summary.
  Sizes/prices are sent only if you also configure an access token (`NTFY_TOKEN`) on an ntfy server
  with access control **and** tick the opt-in box.
- Android: enable "instant delivery" in the app for prompt alerts. iOS: delivery relies on ntfy.sh's
  push relay; with a self-hosted server you must set `upstream-base-url` or alerts can be delayed.
- A laptop that is asleep or off cannot send anything, including "I'm down". See DEPLOYMENT.md
  for the external watchdog plan.
