# Release checklist — v0.1.0 (2026-11-05)

Statuses: Implemented · Test passed · Inspected (UI, manual) · Externally verified · Blocked.
Nothing here is "Externally verified" yet. This release is **not** production-ready and is not verified
against a real Alpaca paper account.

| Item | Status | Evidence / note |
|---|---|---|
| M1 Local login, shell, SAMPLE mode, USD/AED display | Test passed + Inspected | api tests; browser on 127.0.0.1:8765 |
| M1 Strategy, recovery state tests, accounting fixture | Test passed | 110 pytest tests pass (`scripts\test.ps1`) |
| M1 Backtests, both benchmarks, charts, guided explanations | Test passed + Inspected | SAMPLE run #1 rendered; replay predict/reveal works |
| M1 Real historical data | **Blocked** | Needs your Alpaca keys (Research → Fetch SPY history) or a documented CSV |
| M2 Broker observation (read-only) | Implemented, mocked | **Blocked** for real account until you add keys and run the connection test |
| M3 Scheduling, lifecycle, limits, drawdown, security pauses, recovery | Test passed | timeout/restart/partial/residual/missed/conflict scenarios via `Engine.tick` |
| M3 Decision→order→fill UI | Inspected (MOCK) | `scripts\mock-demo.ps1` lifecycle viewed in browser |
| M3 Supervised paper order at Alpaca | **Blocked** | Requires your explicit activation |
| M4 Event inbox, outbox, ntfy adapter, failure isolation | Test passed | phone receipt **Blocked** until you configure and confirm |
| M4 Backup / restore drill | Verified manually | `restore-drill` on the demo DB: RESTORE DRILL PASSED |
| M4 Graceful stop + lease release | Verified manually | worker log "Worker stopped cleanly", lease `stopped` |
| M4 Launch scripts | Implemented | `start.ps1`/`stop.ps1` written; run by you on first setup (I launched via the same modules) |
| M4 Container deployment | **Blocked** | Docker not installed here; files unbuilt |
| Dependency audit | Test passed | pip-audit: no known vulnerabilities; npm audit: 0 (after upgrading Starlette/FastAPI, python-multipart, pytest, React Router) |
| Secret scan | Test passed | simple pattern scan, no hits; not a full scanner |
| Adversarial review | Done (self-review) | 8 issues found and fixed with regression tests (see below); not an independent review |
| Automated browser (E2E) tests | **Not implemented** | UI checked by hand only |

## Adversarial review findings (fixed, each with a regression test)
1. A working BUY was not cancelled when the signal flipped to CASH before it filled → now cancelled and re-assessed after terminal status.
2. Live engine would have accepted SAMPLE/CSV data for orders → now refused.
3. Activation could create an entry from a stale decision → must be the latest completed session.
4. A brief broker outage at evaluation time produced a final SKIP for the day → evaluation now defers and retries.
5. An unreadable cash-activity endpoint blocked all reconciliation → now marks performance invalid and continues.
6. Live cost basis used average cost while backtests used FIFO → live now replays fills through the same FIFO ledger.
7. Manual close could be followed by an automatic re-buy → close also pauses new orders.
8. Drawdown warnings repeated while hovering near 5% → hysteresis; owner overrides are respected instead of re-latching.
Also from the audit: vulnerable Starlette/python-multipart/pytest/React Router versions replaced.

## Performance acceptance (defined before any tuning)
- Compare the strategy against both benchmarks on the **validation** period with fixed v1 settings,
  report net return, max drawdown, exposure and round trips with costs, and state that differences
  with few round trips are weak evidence.
- Run the final holdout **once** after settings are frozen; record it; do not re-tune afterwards.
- Paper automation can be operationally correct while the strategy underperforms; both are reported.

## Known limitations / next steps
- Verify with your paper account: connection test, one observed decision, then supervised activation.
- Automated browser tests (e.g. Playwright) for onboarding/decision/replay/pause.
- External heartbeat watchdog; server deployment after a separate review.
- Dividends excluded (price-only) everywhere; IEX closes vs consolidated closes.
- Single local user; no MFA on the dashboard; logs not tamper-proof.
- Future live trading requires a separate project review (eligibility, tax/withholding, FX costs, etc.).
