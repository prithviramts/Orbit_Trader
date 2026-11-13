# Architecture, invariants and state

```
 browser ──HTTPS/HTTP loopback──► API (FastAPI, auth, CSRF)  ──► SQLite (WAL) ◄── Worker (single, lease)
   React dashboard (no secrets)    read-mostly; owner controls     durable state      reconcile → evaluate
                                   research job pool (1 thread)                       → execute → notify
                                                                                       │
                                                                     Alpaca PAPER REST ◄┘  ntfy (optional)
```

- **API process** never submits orders. Owner actions that need the broker are either queued as jobs
  for the worker (cancel) or created as PENDING intents the worker submits (close). Read-only broker
  checks (connection test, ambiguous-order lookup) run in the API.
- **Worker process** is the only trader. Duplicate prevention: OS file lock, DB lease with 90 s expiry
  and heartbeat, unique constraints (decision per account/symbol/session/sequence, intent identity,
  client_order_id, fill key, job key).
- **Strategy** (`strategy.py`) is pure and shared by backtest and live. **Execution models differ** and
  are explicit: backtest = next open ± slippage; live = broker fills.

## Tick (worker cycle, every 20 s)
1. Heartbeat lease. 2. Reconcile: orders → account → positions → orders again → clock → cash activity;
update intents from broker orders by client_order_id; apply fill increments; detect foreign orders,
foreign holdings, holding mismatch, account change, external cash flows. 3. Owner jobs (cancel).
4. Equity mark after a completed session (close-to-close drawdown overlay). 5. Evaluate the latest
completed session once (deferred if the broker state is not trusted). 6. Re-assess a decision whose
conflicting order has resolved. 7. Execute PENDING intents inside their window. 8. Dispatch notifications.

## Intent state table (enforced by `engine.TRANSITIONS`)
| From | Allowed to |
|---|---|
| PENDING | SUBMITTING, MISSED_EXECUTION, SKIPPED, REVIEW_REQUIRED, SUPERSEDED |
| SUBMITTING | SUBMITTED, AMBIGUOUS, REJECTED, PENDING (definitely not sent), PARTIALLY_FILLED, FILLED, TERMINAL_PARTIAL, CANCELLED_ZERO_FILL |
| AMBIGUOUS | SUBMITTED, PARTIALLY_FILLED, FILLED, TERMINAL_PARTIAL, CANCELLED_ZERO_FILL, REJECTED, CANCEL_REQUESTED, SUPERSEDED (owner confirmed absent) |
| SUBMITTED | PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED, TERMINAL_PARTIAL, CANCELLED_ZERO_FILL, REJECTED |
| PARTIALLY_FILLED | PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED, TERMINAL_PARTIAL |
| CANCEL_REQUESTED | FILLED, TERMINAL_PARTIAL, CANCELLED_ZERO_FILL, PARTIALLY_FILLED |
| REJECTED, MISSED_EXECUTION, CANCELLED_ZERO_FILL, REVIEW_REQUIRED | SUPERSEDED (owner review) |
| FILLED, TERMINAL_PARTIAL, SKIPPED, SUPERSEDED | — (terminal) |

Separate fields, never inferred from one another: `signal_target` (what the rule wants), `held_qty`
(sum of confirmed fills of Orbit orders), intent `state`, and control flags (`mode`, `new_orders_paused`,
`risk_paused_entries`, `security_paused`, `review_required`, `performance_valid`).

## Key invariants (each has a test)
- Money is `Decimal`, stored as text; AED = USD × rate, rounded HALF_UP to 2 dp only for display.
- Decisions use only completed bars dated ≤ the session; the current bar must exist; gaps → SKIP.
- Intent persisted before broker contact; SUBMITTING persisted before the POST.
- One stable client_order_id per intent identity; a timeout never produces a new id; "not found" after
  a timeout stays AMBIGUOUS.
- A buy never exceeds min(25% equity, hard cap, unborrowed cash − reserve); margin buying power is
  ignored. A sell never exceeds min(strategy holding, broker holding). Enforced again in `ExecutionGuard`.
- No catch-up orders: past the 5-minute window → MISSED_EXECUTION (review).
- SAMPLE / CSV data can never create orders.
- Partial entries are adopted without top-up; partial exits create a lineage-linked residual exit for
  the next window only after the previous exit is terminal.
- Live cost basis is FIFO replay of fills through the same `Ledger` as backtests.
- Settings edits create a new version; pending intents of old versions go to review.
- Drawdown latch never self-resets; resuming at/above threshold needs a versioned override.

## SQLite and when to migrate
WAL mode, `busy_timeout` 10 s, short transactions, broker calls outside transactions. Fine for one user,
one worker and one API process on one disk. Move to PostgreSQL if you need multiple hosts, several
strategies/accounts writing concurrently, network storage, or HA failover. Schema changes go through
`db.init_db`, which backs the DB up first (`SCHEMA_VERSION`, `schema_version` table).

## Future model seams (not active)
- Every backtest run stores dataset fingerprint, params, risk policy, costs, period and FX in
  `backtest_run.config`. Chronological development/validation/final-holdout splits exist now.
- Planned label: *SPY trades at least 5% below the observation close at any point in the next 20
  sessions*. It matures only after those 20 sessions complete. Training labels must end before the
  validation window starts (≥ 20-session embargo); overlapping observations within one sell-off are not
  independent and must be de-duplicated or block-bootstrapped when reported.
- Fit scalers/imputers/feature selection on training data only; log features, cutoff, data version,
  probabilities, labels and calibration; compare with the unconditional base rate and the fixed rule;
  shadow mode before any manual promotion; never load pickles or downloaded code.
- News/LLM components would get no broker credentials, no shell and no order permission; outputs are
  validated by schema and pass the same deterministic risk checks.

## Execution safeguards and optional monitoring

An automated GLD order must pass the current GLD enable switch again when
the worker processes its pending intent. A pending intent observed while
automation is disabled is skipped. An explicit owner close uses the separate
manual-close path. A request that has already entered submission may still
need broker reconciliation or cancellation.

The drawdown warning triggers at the configured threshold and re-arms only
after recovery below half that threshold. The entry pause remains separately
latched. These thresholds apply to observed close-to-close equity marks.

The optional external heartbeat initializes its HTTP client on the first due
ping. A client setup or ping error is recorded as a monitoring error.
