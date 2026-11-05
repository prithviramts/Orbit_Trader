# Verification notes (official documentation reviewed 2026-11-05)

| Topic | What the docs say | How Orbit uses it | Status |
|---|---|---|---|
| Alpaca paper environment | Base URL `https://paper-api.alpaca.markets`; paper keys differ from live; default balance USD 100k; reset = create a new paper account; fills only when marketable; ~10% random partial fills; no market impact, price improvement, regulatory fees or dividends simulated; paper accounts get IEX real-time data. | Adapter accepts only that URL and constructs the SDK with `paper=True`; never resets accounts; UI states the limitations. | Documented ✔ |
| alpaca-py | Latest 0.44.0 (2026-08-11), Python ≥3.10. `TradingClient(paper=True)`, `get_order_by_client_id`, `get_orders`, `get_clock`, `get_calendar`, `get_asset`, `submit_order`, `cancel_order_by_id`. **No request timeout by default**; 429s retried automatically. | Pinned 0.44.0. Timeouts injected (5 s connect / 20 s read). Order client has retries disabled so the SDK never silently re-POSTs. Account activities via the raw `/account/activities` endpoint. | Inspected in installed SDK ✔ |
| Order statuses | new, partially_filled, filled, done_for_day, canceled, expired, replaced, pending_cancel, pending_replace, accepted, pending_new, accepted_for_bidding, stopped, rejected, suspended, calculated. | Terminal: filled, canceled, expired, rejected, replaced. Everything else is "still working". | Documented ✔ |
| Fractional / notional | Fractional orders: DAY time-in-force only (market/limit/stop/stop-limit). Notional orders cannot be replaced (cancel + new). | Buys are DAY market **notional**; sells are DAY market **qty** (fractional, floored to 9 dp). No replaces. Asset `fractionable` checked before each order. | Documented ✔; minimum notional **not stated** in the docs reviewed (Orbit uses USD 1 and flags a rejection if the broker disagrees) |
| client_order_id | Client-supplied unique id; lookup by `/orders:by_client_order_id`. Maximum length **not stated** in the pages reviewed. | Ids are ≤ 40 chars and deterministic per intent identity. A not-found right after a timeout is never treated as proof of absence. | Partially verified ⚠ |
| Market data | Basic (free): IEX only, history since 2016, latest-15-minute limitation on historical requests, 200 calls/min. Adjustment enum: raw/split/dividend/all (SDK). | Default feed IEX, `adjustment=split`, price-only (dividends excluded). Bars filtered to completed sessions + 15 min. IEX closes may differ from the consolidated close (labelled). | **Externally verified 2026-11-05** on the owner's paper account: IEX daily SPY bars returned from 2018-11-01, with 433 missing sessions 2018-11 → 2020-07-24 and complete data from 2020-07-27. Docs said "since 2016"; the account received less. |
| Exchange calendar | NYSE hours/holidays; Alpaca `/v2/calendar` and `/v2/clock`. | Offline XNYS schedule from `exchange_calendars` for scheduling; broker clock must say `is_open` before any submission. Early close 2026-11-27 and DST shifts tested. | Tested offline ✔; broker clock **unverified live** |
| ntfy | POST body to `<server>/<topic>`; headers Title, Priority (1–5), Tags, Click; auth `Authorization: Bearer tk_…`; topics on ntfy.sh are effectively public; iOS needs the upstream relay for self-hosted servers (otherwise delayed); Android instant delivery option. | Adapter posts redacted summaries; Bearer token optional; https only; no redirects followed. | Documented ✔; phone receipt **unverified** (owner must confirm) |
| OWASP REST / LLM prompt injection | Auth on every endpoint, CSRF for cookie auth, input validation, security headers; untrusted text never gains privileges. | Implemented as described in OPERATIONS_SECURITY.md; hostile-news test. | Implemented, self-tested only |
| USD/AED | AED pegged at 3.6725 per USD (CBUAE reference). | `USD_TO_AED=3.6725`, labelled "reference, not a bank conversion"; stored in every backtest config. | ✔ |

## Owner paper account check (2026-11-05, read-only, 0 orders)
Account ACTIVE, cash/equity USD 100,000 (paper default), buying power USD 400,000 (4× margin, never used by Orbit),
shorting enabled at account level (never used), no positions, no open orders, no orders in 30 days, SPY tradable and
fractionable, clock reachable, cash-activity endpoint readable.

Two adapter bugs were found only against the real SDK and fixed with offline construction tests
(`tests/test_alpaca_adapter.py`): `TradingClient` does not accept retry settings, and its base URL is an enum.

## Material limitations recorded
- Usable gap-free Alpaca IEX history starts 2020-07-27, so the first valid 200-session decision is in May 2021.
  Backtests now skip any decision whose window contains a missing session (same rule as live).
- Paper fills ≠ live fills; backtest opens ≠ paper fills (both labelled).
- IEX daily bars ≠ consolidated official closes. Older-than-2016 history is not available on the free feed;
  use a documented CSV import if needed. Orbit never invents history.
- Dividends are excluded (price-only) for strategy and both benchmarks, so absolute returns understate
  a real buy-and-hold; the comparison stays like-for-like.
- Equity marks are read ~15 min after the close; after-hours price changes can make them differ slightly
  from a true close-to-close value.
