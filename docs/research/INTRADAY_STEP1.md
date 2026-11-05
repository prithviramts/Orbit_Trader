# Intraday experiment, step 1: can simple day-trading rules beat holding SPY?

**Short answer: no.** On 2021–2026 1-minute SPY data, no rule we tested beat simply buying and holding SPY,
even with trading costs set to zero. This is research on past data; nothing here placed an order.

Reproduce: `uv run python -m orbit.research.run_intraday fetch` then `... run`
(numbers land in `data/research/intraday_step1.json`). Code: `backend/orbit/research/intraday.py`.

## What was tested
| Rule | In plain English |
|---|---|
| **VWAP dip-buy** | After 10:00 New York time, buy when SPY falls a set amount (0.2 %, 0.3 %, 0.5 % or 0.75 %) below the day's average traded price (VWAP). Sell when it climbs back to that average, or at a stop-loss (0.3 %, 0.5 % or 1 % below entry). At most 2 trades a day. |
| **Opening-range breakout** | Watch the first 15, 30 or 60 minutes. If SPY then rises above that period's high, buy (momentum). Stop-loss at that period's low. |

Both rules always sell before the close (no overnight risk), never buy after 15:30, use no leverage and never short.
**15 parameter combinations were tried in total**, and all of them are reported in the JSON file, not just the best.

## How the test was kept honest
- **Data:** 578,678 one-minute SPY candles from Alpaca's free IEX feed, regular hours only, 1,466 sessions
  (Jan 2021 – Nov 2026; 1 session missing, 11 thin).
- **No hindsight:** a signal on one minute's close is filled at the **next** minute's open.
- **Stops are realistic:** if the price gaps past the stop, the fill is at the worse price, not at the stop.
- **Costs** (spread + slippage) were charged on every buy and every sell, at four levels: 0, 0.5, 2 and 5 basis points
  (1 bp = 0.01 %). Small SPY orders usually cost roughly 0.5–2 bp.
- **Development vs holdout:** the best settings were picked on **2021–2023 only**, then run **once** on
  **2024–Nov 2026**, data the choice never saw. That's how you catch rules that only fit the past.

## Results on the unseen holdout period (2024 – Nov 2026)
| Costs per fill | Best dip-buy (picked on 2021–23) | Best breakout (picked on 2021–23) | Buy at open, sell at close every day | **Buy and hold** |
|---|---|---|---|---|
| none (impossible in reality) | −10.0 % (727 trades) | +2.1 % (552 trades) | +10.1 % | **+62.4 %** |
| low, 0.5 bp | −15.4 % | −3.3 % | +2.5 % | **+62.4 %** |
| realistic, 2 bp | −3.5 % (86 trades) | −18.1 % | −17.2 % | **+62.4 %** |
| pessimistic, 5 bp | −11.0 % | −27.0 % | −46.0 % | **+62.4 %** |

The breakout rule looked great on 2021–2023 (+37 % before costs) and then nearly vanished on new data (+2 %).
That collapse is the classic sign of a rule that fitted past noise rather than a real pattern.

## What we learned (plain English)
1. **Most of SPY's gain happens overnight**, between one day's close and the next day's open. Every
   "flat by the close" rule misses it by design. Even buying every open and selling every close, with zero costs,
   made only +10 % while holding made +62 %.
2. **Costs eat intraday rules alive.** Hundreds of trades × a tiny cost each adds up to more than the edge.
3. **"Buy the dip" has a high win rate but loses money.** Many small wins and a few larger stop-outs: the classic trap.
   A 50–65 % win rate means nothing on its own.
4. **What looks best on old data usually fails on new data.** That's why the holdout matters.

## Limits of this test (also honest)
- IEX is one exchange's trades, so its VWAP and volume are approximations of the whole market.
- Only two rule families and 15 settings. Other rules (other stocks, short selling, leverage, options) were not tested;
  most of them add risk.
- News-based intraday rules (FinBERT on each headline) are **not tested yet**. That needs the ~700 MB FinBERT download
  and Alpaca's historical news, and is the natural step 1b if you still want it.

## Recommendation
Don't build a live intraday bot from these rules. Keep the long-term rule (and/or plain buy-and-hold) as the core.
If you want to continue the experiment, test the news-sentiment rule the same honest way (step 1b) before any paper trading.
