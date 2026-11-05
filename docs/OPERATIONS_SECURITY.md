# Operations and security guide

These controls reduce risk. They do not prove the app cannot be attacked, and the app is not
production-ready.

## What protects you, simply
| Control | What it means |
|---|---|
| Paper-only adapter | The code only accepts `https://paper-api.alpaca.markets`. A live URL makes startup fail. There is no live setting to flip. |
| Loopback binding | The dashboard listens on 127.0.0.1, so other computers cannot reach it. Remote binding refuses to start without explicit HTTPS configuration. |
| Password + session | Argon2id-hashed password set locally; no default. Sessions live in the DB, expire after 12 h, and are revoked at logout or password change. |
| CSRF + Origin + Host checks | Another website open in your browser cannot make your dashboard act: changes need a same-origin request with a secret header. Wrong Host headers (DNS rebinding) are rejected. |
| Re-enter password | Enabling automation, changing settings, closing the position and resuming pauses ask for your password again. All are audited. |
| Execution guard | Right before any order: SPY only, DAY market only, no extended hours, buy ≤ USD 250 cap and ≤ unborrowed cash, sell ≤ held. Independent of UI/strategy. |
| SECURITY_PAUSED | Set on unexplained holdings/orders, account change or by you. Blocks automated orders on both sides until you review a clean reconciliation. |
| Redaction | Keys and tokens are removed from logs, errors, API responses, exports and notifications. |
| Login throttling | 5 failures per client (or 30 overall) in 15 min → wait. Failed logins never pause trading. |
| Limits | 6 MB request cap, 5 MB CSV cap, 30 actions/min per session, 1 research job at a time (max 3 queued), upstream timeouts. |
| Untrusted text | Journal notes, imported descriptions and errors are rendered as text (React escaping, strict CSP). URLs are stored, never fetched. Journal text has no path to trading. |

## Incident response
1. Security page → **Security pause**.
2. Log in to the Alpaca dashboard directly (type the address; do not follow links in messages) and look
   at orders and positions.
3. Regenerate the paper API keys in Alpaca (revokes the old pair). Update `.env`, restart Orbit.
4. Back up the database (`.\scripts\backup.ps1`) and keep `data\logs` (already redacted).
5. If this PC may be compromised, repair/rebuild it before using new keys on it.
6. Run the connection test; check the reconciliation report is clean; resume with a written note.

A local pause cannot stop someone who already has working broker keys. Revoking the keys can.
Orders already at the broker and positions survive if Orbit stops; Orbit never auto-liquidates because
the dashboard is down or a news item looks bad.

## Backups
- `.\scripts\backup.ps1` (safe while running; SQLite online backup API) → `data\backups\` (user-only ACL).
- `.\scripts\restore-drill.ps1` restores the newest backup into a temp copy and compares tables.
- Real restore: `.\scripts\stop.ps1`, then `.\scripts\restore.ps1 data\backups\<file>` (asks you to type
  RESTORE and saves the current DB first). On start, the worker reconciles with the broker before acting.
- Schema upgrades back up automatically before migrating. If an update fails: stop, restore the
  `pre-migrate-*` backup, check out the previous commit, `.\scripts\setup.ps1`, start.

## Logs and disk
`data\logs\worker.log`, `api.log` rotate at 5 MB × 10 files (≈100 MB max). The audit log table keeps
security-relevant actions. Local logs are not tamper-proof if the host is compromised. Check free disk
space monthly; the Operations page lists backups and sizes.

## Updating dependencies
1. `.\scripts\test.ps1` (records tests, `pip-audit`, `npm audit`, secret scan in docs\TEST_EVIDENCE.md).
2. Update pins (`backend/pyproject.toml` + `uv lock`, `frontend/package.json` + `npm install`), choosing
   releases at least two weeks old.
3. Re-run `.\scripts\test.ps1`; commit only if green. Back up `data\` first.

## Monitoring routine
See BEGINNER_GUIDE.md. Exact times are on Operations (computed from the exchange calendar).
