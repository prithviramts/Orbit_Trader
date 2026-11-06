# Open Orbit on your phone (private, free)

Orbit is not on the public internet, on purpose. To reach it from your phone, both devices join **your
own private network** using Tailscale (free for personal use). Nobody else can even see the page. The same
steps work later on a server.

What you get: an "Orbit" icon on your phone's home screen. It opens full-screen like an app, with the same pages,
live chart and alerts bell as the laptop.

**Limit while Orbit runs on the laptop:** the phone can reach it only while the laptop is on and awake. A server removes that limit.

## Setup (about 15 minutes; you do the sign-ups yourself)
1. **Laptop:** install Tailscale from **tailscale.com/download** (Windows) and sign in.
2. **Phone:** install the **Tailscale** app (App Store / Play Store) and sign in with the **same** account. Switch it on.
3. In the **Tailscale admin console** (login.tailscale.com → DNS), turn on **MagicDNS** and **HTTPS Certificates**.
4. **Laptop, PowerShell** (normal window, any folder):
   ```powershell
   tailscale serve --bg 8765
   tailscale serve status
   ```
   The second command shows an address like `https://your-laptop.tail1234.ts.net`. Copy it.
5. Open `.env` (`notepad .env`) in the project folder and set:
   ```
   ORBIT_DEPLOYMENT_MODE=remote
   ORBIT_PUBLIC_ORIGIN=https://your-laptop.tail1234.ts.net
   ```
   Leave `ORBIT_BIND_HOST=127.0.0.1` as it is: Tailscale passes requests to Orbit inside the laptop.
6. Restart Orbit (outside 18:30–18:35 Dubai): `.\scripts\stop.ps1; .\scripts\start.ps1`
7. **Use the new address everywhere**, on both the laptop and the phone. In this mode Orbit only accepts sign-in from that
   exact address, and the old `http://127.0.0.1:8765` stops accepting actions. That is a safety rule, not a bug.
8. **Phone:** open the address in Safari (iPhone) or Chrome (Android), sign in, then:
   - iPhone: **Share → Add to Home Screen**.
   - Android: **⋮ → Add to Home screen** (or **Install app**).

## Undo
Set `ORBIT_DEPLOYMENT_MODE=local`, remove the `ORBIT_PUBLIC_ORIGIN` line, run `tailscale serve reset`, and restart Orbit.

## Safety notes
- `tailscale serve` shares the page **only inside your tailnet**. Never use `tailscale funnel`, which puts it on the public internet.
- Keep the dashboard password strong. Anyone who joins your Tailscale account could reach the sign-in page.
- If you lose the phone, remove it in the Tailscale admin console (Machines → … → Remove) and change the dashboard password.
