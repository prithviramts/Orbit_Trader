# Gold research, step 1: should Orbit also trade gold (GLD)?

**Short answer: not with the 200-day rule.** On gold, the rule earned less than simply holding gold, and it
traded far more (26 buy-and-sell round trips in 5½ years). The more interesting finding is that **holding
half SPY and half gold** was smoother than holding SPY alone. That is a portfolio choice, not a trading rule.
This is research on past data; nothing here placed an order.

Reproduce: `uv run python -m orbit.research.run_gold fetch` then `... run`
(numbers land in `data/research/gold_step1.json`). Code: `backend/orbit/research/gold.py`.

## What was tested
- **GLD** is a fund that tracks the price of gold. It trades on the US exchange at the same times as SPY, and Alpaca supports it.
- **The rule** is exactly Orbit's SPY rule: after each close, above the 200-day average means be invested and
  below means be in cash. The order is placed at the **next** day's open (no hindsight).
- **Costs** of 0, 2 and 5 basis points per fill were tested. The tables use 2 bp, a realistic level.
- **Data** comes from Alpaca's free IEX feed, with dividends included. Gold history there only starts in July 2020, so after the 200-day
  warm-up the results cover **12 May 2021 to 5 Nov 2026** (about 5½ years). That is short: it contains one
  bear market (2022) and an unusual gold boom (2025).

## Results (2 bp per fill, USD 10,000 start)
| Approach | Total return | Per year | Worst fall from a high | Worst calendar year | Time invested |
|---|---|---|---|---|---|
| GLD: 200-day rule | +84 % | 11.8 % | −20 % | −9.0 % (2022) | 72 % |
| **GLD: just hold** | **+120 %** | **15.5 %** | −28 % | −5.3 % (2026 so far) | 100 % |
| SPY: 200-day rule (Orbit today) | +70 % | 10.1 % | −19 % | −13.1 % (2022) | 80 % |
| SPY: just hold | +95 % | 12.9 % | −25 % | −18.2 % (2022) | 100 % |
| Half SPY rule + half GLD rule | +77 % | 11.0 % | **−13 %** | −11.3 % | — |
| **Half SPY + half GLD, just hold** | **+108 %** | 14.2 % | −18 % | **−10.1 %** | 100 % |

Gold and SPY moved almost independently: the correlation of their daily moves was **0.17**, where 1 would
mean they always move together.

## What it means
1. **The rule did not help on gold.** Gold often swings back and forth around its 200-day average, so the rule kept
   getting shaken out: it sold low and bought back higher. It cut the worst fall (−20 % vs −28 %), but it
   gave up a third of the gain and had a *worse* worst year than just holding.
2. **The same is true of SPY in this period.** The rule earned less than holding. Its job is protection in a long
   crash (2022 was −13 % instead of −18 %), not beating the market in rising years. That trade-off is the
   design, and it is why the Performance page compares against "just hold".
3. **Mixing helped more than timing.** Holding half SPY and half gold had a smaller worst fall (−18 %) and
   a smaller worst year (−10 %) than either one alone, *without any trading*, because they rarely fell together.
   Its higher return here comes mostly from gold's exceptional 2024–25. Do not expect that to repeat.

## Recommendation
- **Do not add a gold 200-day-rule bot.** The evidence says it would trade more and earn less than holding.
- If you want gold, the evidence-backed idea is a **fixed slice held** (for example, 20–50 % of the money),
  topped up now and then. That is an investing decision, not a trading bot, and it is yours to make. Orbit is
  not a financial adviser.
- If you still want Orbit to test a gold rule *live in paper*, the next step is engineering work. The engine
  is built around one symbol, and reconciliation flags any other holding. It needs a second strategy slot
  with its own budget, its own position and its own Performance page line. That is several days of careful
  work and tests. It is not worth doing for a rule the data says loses to holding.
