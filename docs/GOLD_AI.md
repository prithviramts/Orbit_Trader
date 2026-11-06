# Gold AI (paper experiment)

Orbit has two strategies in the same Alpaca **paper** account:
- **SPY**: the 200-day rule (unchanged).
- **GLD (gold)**: a learning model, a live experiment that you asked for.

The page is **Gold AI** in the left menu.

## How the gold AI decides
1. **Every evening after the US close** (about 01:15 Dubai), it reads **26 signals**:
   - gold's returns over 5 to 250 days
   - distance from its 20/50/100/200-day averages
   - RSI and Bollinger position (overbought or oversold)
   - volatility and its change
   - fall from the 1-year high
   - share of up days
   - SPY's trend and volatility
   - gold vs SPY, and their correlation
   - five classic gold rules as yes/no inputs: above 100- and 200-day averages, two crossovers, and a buffered average
2. **A learning model** (logistic regression) turns them into one number: the **chance GLD is higher 20 trading
   days later**. It learned its weights from every trading day since 2016, and it **retrains every 28 days**.
3. **Rule for acting:** buy when the chance is **above 55 %**, sell when it is **below 45 %**, otherwise do nothing. The gap
   stops it flipping back and forth around 50 %.
4. Orders go in at the next US open (18:30 Dubai in winter), through the same safety checks as SPY:
   - paper-only endpoint
   - order saved before it is sent
   - duplicate protection
   - reconciliation
   - pauses and reviews
5. **Money per buy:** USD 100 by default. You can change it on the page, up to the USD 250 per-order cap.

## Honest expectations
On 2022 – Nov 2026, years it never trained on, the AI made about **+90 %**, while simply holding gold made **+123 %**.
Its up/down calls were right about **50 %** of the time, and gold rose in about 60 % of 20-day windows.
More signals do not automatically mean better predictions: gold prices are hard to forecast from their own history.
The **live scorecard** on the page counts every call once its 20 days have passed, so you can judge it on real results.

## Controls
- **On the page:** "Let the AI trade gold (paper)" and "Stop AI trading (keep predicting)". With trading stopped
  (shadow mode), it still predicts and is scored every day but places no orders. Stopping does not sell what it holds.
- **In PowerShell:** `cd backend` then `uv run python -m orbit.cli gold status`, `... gold on --budget 100` or `... gold off`.
- Orbit's global switches still apply: OBSERVE mode, Pause new orders, security pauses and risk pauses.

Code: `backend/orbit/gold_ai.py` (signals, model, rule) and `backend/orbit/gold_engine.py` (orders).
Tests: `backend/tests/test_gold_ai.py`. Research: [research/GOLD_STEP2.md](research/GOLD_STEP2.md).
