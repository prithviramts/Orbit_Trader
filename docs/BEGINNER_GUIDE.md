# Beginner guide

## The four milestones in plain English

**1. Historical replay and learning.** A "backtest" replays the rule on old prices as if you had followed
it day by day: decide after each close, act at the next morning's open. Orbit compares it with two
"buy and hold" benchmarks (just buy once and wait), one sized like the strategy's first purchase and one
nearly fully invested. Try it: Research → *Create SAMPLE dataset* → *Run backtest*. SAMPLE prices are
made up for practice and are labelled everywhere.

**2. Broker observation.** Orbit connects to your Alpaca *paper* account (pretend money at a real broker)
and only *reads*: balance, holdings, orders, the market clock, prices. Each evening it records what the
rule would do, without ordering anything (OBSERVE mode).

**3. Supervised paper execution.** When you enable it, the worker sends real *paper* orders. Every step
is saved before it talks to the broker, so a crash or network glitch never creates a duplicate order.
Anything unclear (missed time window, rejection, unknown result) stops and asks you.

**4. Notifications and operations.** An in-app inbox records every important event; optional phone pushes
via ntfy; backups; a security page; scripts to start/stop.

## Terms you will meet
| Term | Meaning |
|---|---|
| SPY | An ETF (a basket that trades like one stock) tracking ~500 large U.S. companies. |
| Session | One regular NYSE trading day, 9:30–16:00 New York time (some days close at 13:00). |
| 200-session average | Average of the last 200 closing prices, including today's. |
| Market order | "Buy/sell now at whatever the price is." Fast; price not guaranteed. |
| Limit order | "Only at my price or better." Price safe; may never fill. |
| Stop order | Turns into a market order at a trigger; can fill well past the trigger in a fast market. |
| Bid / ask / spread | Best buyer price / best seller price / the gap between them (a hidden cost). |
| Fill | The trade actually happening. "Accepted" is not a fill. |
| Partial fill | Only part of the order executed. Orbit keeps what filled and does not top up. |
| Position | What you own. Orbit counts only shares from its own confirmed fills. |
| Realized / unrealized P&L | Profit locked in by selling / paper gain on shares still held. |
| Exposure | Fraction of your money invested. 25% exposure: a 10% SPY drop ≈ 2.5% account drop. |
| Drawdown | How far you are below your best point so far. |
| Slippage | Getting a worse price than expected. Backtests assume 5 bps (0.05%). |
| Benchmark | A simple alternative to beat. If buy-and-hold does better, the rule adds nothing. |
| Data leakage | Using future information by accident in a test. Makes results look too good. |
| Overfitting | Tuning until the past looks great; usually fails later. The final holdout guards against it. |

## Why following the rule can still lose
Trend rules buy after rises and sell after falls. In choppy markets they repeatedly buy high and sell
low (you can see this in the SAMPLE run: many small losing round trips). Prices can also gap overnight,
so the next open can be far from the close that triggered the decision. Orbit labels these separately:
- **Software failure**: something broke (shown as an incident).
- **Rule loss**: the rule was followed and lost money (normal, expected sometimes).
- **Simulator limitation**: the backtest could not know something (e.g. exact fill price).

## Money and currency
Calculations are in USD. AED is shown beside every amount at the **reference** rate 3.6725 (USD→AED is
×3.6725). It is not what your bank would charge. Percentages are the same in both currencies.

## Your monitoring routine (Dubai time)
The Operations page shows the exact times for the next sessions from the exchange calendar. Typical,
not guaranteed:
- **Morning** (any time): open Overview, read last night's decision and its explanation.
- **US open** (≈17:30 Dubai in US summer, ≈18:30 in US winter) **only if an order is pending**: be online
  and keep the laptop awake for the first 5 minutes; check the order resolves on Orders & positions.
- **Evening decision** (≈00:15–01:15 Dubai): the laptop must be awake and online for the evaluation.
- **First automated sessions**: supervise every one until the order is filled or cancelled.
- **Weekly**: read the Weekly review on Learn and the reconciliation report on Operations.
- **Monthly**: review research runs. Do not keep re-tuning on the final holdout.

If the laptop sleeps through the open window, Orbit records MISSED_EXECUTION and waits for you. It
never sends a late "catch-up" order.
