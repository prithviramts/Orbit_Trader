# Orbit Trader audit and modernization plan

Baseline: GitHub Orbit_Gpt at 75906d9b2cac284ce61b7880bec7749a05faee5f. Review and tests use the supplied source archive in a separate Linux checkout. Windows state and live broker connectivity are outside the evidence.

## Existing architecture

React/TypeScript (Vite, React Router, Recharts and Lightweight Charts) talks to a FastAPI service. The API serves the built UI, manages the single-owner session and records control changes and jobs. The separate worker uses a file lock and database lease, reconciles with Alpaca paper, evaluates completed exchange sessions, persists order intents and submits eligible requests. SQLite via SQLAlchemy stores settings versions, decisions, orders, fills, account snapshots, risk marks, jobs, alerts and research. The worker polls for both operational and optional research work. Windows PowerShell runs separate worker and API processes. The repository also includes a Docker deployment path.

## Confirmed defects and implementation plan

1. **High**. GLD shadow toggle gates planning but misses queued intents at submission. Enforce the current strategy switch at the shared submission boundary for GLD automation. Preserve explicit owner exits.
2. **Medium**. The drawdown warning uses its reset boundary as its effective trigger while unarmed. Trigger only at the configured warning threshold and preserve hysteresis and the persistent entry pause.
3. **Medium**. Optional heartbeat eagerly initializes an HTTP client even when disabled. Client initialization can fail before the worker loop with a SOCKS proxy and no SOCKS dependency. Defer initialization into the already isolated ping error path.
4. Run regression tests after each change and run the complete suite, frontend build and dependency checks. Keep changes local pending review.

## Strategy and research assessment

| Path | Decision and sizing | Evidence and limitations |
|---|---|---|
| SPY SMA | Completed daily close above versioned SMA (default 200 sessions) targets invested, below targets cash, equality keeps prior target. Orders target the next regular open. Default entry is sized at 25% of equity, capped at $250, with a $10 cash reserve and available-cash checks. | Shared backtest signal, next-open execution, adverse slippage (default 5 bp), fees configurable, and matched-size and full-buy-and-hold benchmarks. Validate the execution approximation against fill data, corporate-action handling, parameter sensitivity and unseen regimes. |
| GLD model | L2-regularized logistic regression on trailing GLD/SPY features. Enters above its entry threshold, exits below its exit threshold and holds between. Default shadow mode. Default entry budget $100, subject to the shared cap and cash checks. | Walk-forward training uses known outcomes. A 20-session directional target is not a return estimate. Validate the actual deployed model and threshold pair separately from earlier research variants. |
| Shadow and historical research | SPY hold, GLD hold, both SMA rules, volatility-sized SPY, monthly 70/30 SPY rule and gold, gold bands/crossovers/monthly SMA and historical logistic variants. Intraday research includes VWAP dips, opening-range breakouts with range stops, and open-to-close and hold benchmarks. | These run in simulation. Keep their sizing, stops and fill conventions separate from operational policy. |

Operational SPY and GLD exits follow their signals. There are no per-position hard stops or take-profit targets in these paths. Per-order dollars measure exposure, with realized loss dependent on the fill and subsequent price path. Next-open market fills can be materially worse than simulated prices. Run locked walk-forward folds with purged overlapping labels for GLD, multiple fee/slippage scenarios, gap and missing-bar scenarios, and benchmark-relative results by regime. Keep a final untouched holdout. The checked-in Gold Step 2 report records substantial underperformance of the earlier logistic model versus holding GLD on its historical holdout. Those figures were not independently reproduced here.

## AI and explainability

There is one trade-capable ML model (GLD), a deterministic SPY trend rule, and a separate local FinBERT news classifier. The SPY forecasts and scorecard are separate from execution. There is no multi-agent debate or LLM-generated order authority in these paths. The GLD explanation ranks signed feature contributions, which are model attribution and do not establish market causation. The score represents directional likelihood for the training target and has no verified calibration here. Add walk-forward reliability curves, Brier score against base rate, sample counts, drift checks, finite input/artifact validation, model-age limits and explicit abstention for unreliable inputs. FinBERT sentiment should stay informational. Review source provenance, headline duplication, lag and sentiment applicability before interpreting aggregate mood as market evidence.

## Operational risk and security

| Status | Finding |
|---|---|
| Verified in source and fixture tests | Persisted intent and deterministic client ID before broker contact, ambiguous acceptance recovery, partial fills, position reconciliation, paused state persistence, missed-window behavior, paper endpoint guard, and stale/incomplete data handling. |
| Confirmed control gap | No explicit portfolio-wide exposure limit or daily/weekly loss budget is demonstrated. The account drawdown overlay uses close-to-close marks and latches new entries. It cannot cap intraday or gap losses. |
| High-priority potential weakness | Manual-close handling bypasses generic reconciliation cleanliness checks. Verify account-fingerprint mismatch rejection across manual close, cancellation and reconciliation before certifying execution. Gold also reuses the SPY cycle's account context after SPY execution. Test simultaneous submissions against a shared cash budget under partial fills and delayed broker visibility. |
| High-priority potential weakness | A pause or setting change can race a committed SUBMITTING intent. Document the cutoff and test API/worker interleavings. The new GLD gate does not recall an already-sent order. |
| Verified defenses | Argon2id password hashing, server-side sessions, cookie flags, Origin/Host and CSRF checks, action throttling, API import validation, local-first bind, and redacted rotating logs. The local owner is the authorization boundary. |
| Needs deployment validation | Verify Windows ACLs, container file permissions, backup encryption and restore, TLS termination, key rotation, account identity, and notification destinations. Treat SQLite data and backups as sensitive. Avoid running the Vite dev server on a shared network. Optional news packages and the model artifact were outside the installed backend dependency audit. |

## Maintainability and UX

Keep the process boundary and shared order lifecycle. The engine currently combines signal orchestration, reconciliation, state transitions, risk, sizing and broker I/O. FastAPI likewise concentrates unrelated routes. Extract pure execution eligibility and risk policy where tests establish a clear seam, then group API routes by responsibility. Add retention for frequent snapshots and audit/query indexes from measured workloads. SQLite and polling fit one worker and one operator. Multi-user or multi-worker operation would need a different concurrency and authorization design. Measure duration of reconciliation, decision, queue and submissions, and distinguish dashboard liveness from trusted broker state.

The responsive React navigation and chart labels, tabular decision detail and reduced-motion CSS are a useful base. Prioritize a persistent status row for mode, entry pause, data freshness and reconciliation timestamp. Show holdings, open intents and blocked actions separately. Put the latest decision and its evidence above research. On mobile, keep controls and errors reachable without relying on color, and verify focus order, contrast, chart alternatives and screen-reader announcements. The review was source-based plus a production build, with browser accessibility and layout checks still pending.

## Roadmap and readiness

1. **Before unattended paper automation:** resolve account-identity, cash-reservation and pause-race gaps. Add account-wide exposure and explicit loss/abstention policies, with override audit and tests for overnight gaps, stale data, failed recovery and partial fills.
2. **Before broadening use:** record a mock recovery drill, backup/restore, reconnect, watchdog and alert delivery. Validate Windows startup and security controls in an isolated instance. Keep CI running for every PR and consider enabling it for dedicated development pushes.
3. **Before promoting GLD:** record out-of-sample calibration and forward shadow evidence, compare against simple benchmarks, and independently validate data and costs.
4. **After safety gates:** simplify API/engine boundaries and redesign the dashboard status and decision hierarchy. Upgrade chart/tooling dependencies separately.

| Dimension | Score |
|---|---:|
| Architecture and maintainability | 13 / 20 |
| Execution and recovery | 13 / 20 |
| Risk and capital protection | 10 / 20 |
| Strategy evidence and AI | 7 / 20 |
| Security and operational evidence | 10 / 15 |
| UX | 3 / 5 |
| **Total** | **56 / 100** |

This is an engineering-readiness rubric for controlled paper testing. Credit reflects repository and offline fixture evidence. Deductions reflect the unresolved guardrails, deployment uncertainty and insufficient forward validation. No profitability or runtime certification is implied.

## Change verification

Baseline backend suite: 166 passed, one failed during disabled heartbeat client initialization. After the changes, 170 passed. Focused suites: 8 GLD tests and 47 risk/core/heartbeat tests passed. TypeScript and Vite production build passed on Node 24. The installed backend audit and frontend production audit reported no known vulnerabilities within their scan scopes. No active .env, Windows working tree, running Windows app or actual broker order was used. Changes are supplied as a local patch for review.

The full frontend dependency scan reported two findings (one high, one moderate) across Vite and esbuild. Keep the dev server local and upgrade the tooling in a separately tested change. CI currently runs on main pushes and pull requests. Its production-only frontend scan would not surface these development findings.
