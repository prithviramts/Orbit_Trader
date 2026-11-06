# Gold research, step 2: better rules and a learning model

**Short answer:** none of the 15 gold *trading* rules beat simply holding gold on unseen years, and the
learning model was the worst of all. What clearly helped was **holding a slice of gold next to Orbit's SPY
rule**: the worst fall halved and the return went up. Research on past data only; nothing placed an order.

Reproduce: `uv run python -m orbit.research.run_gold2 fetch` then `... run` → `data/research/gold_step2.json`.
Code: `backend/orbit/research/gold_rules.py`.

## How it was kept honest
- Data: Alpaca daily bars for GLD and SPY from **Jan 2016 to 5 Nov 2026** (2,727 sessions, consolidated SIP history, all adjustments).
- Every rule decides after the close and trades at the **next open**, with costs charged on each trade. The table uses 2 bp, a realistic level.
- The best rule was **picked on 2019–2021 only**, then run **once** on **2022 – Nov 2026**, years the choice never saw.
- **The learning model** (logistic regression, a simple AI model that estimates a probability) looks at 9 signals:
  - gold's returns over 5, 20, 60 and 120 days
  - gold's distance from its 50-day and 200-day averages
  - gold's volatility over the last 20 days
  - SPY's 20-day return
  - SPY's distance from its 200-day average

  From those it estimates the chance that gold is higher 20 trading days later. It is retrained each January
  only on outcomes that were already known by then, and a test checks this.

## Unseen years 2022 – Nov 2026 (2 bp costs)
| Approach | Total return | Worst fall from a high | Switches |
|---|---|---|---|
| **Just hold gold** | **+123 %** | −28 % | 0 |
| Rule picked on 2019–21 (50/200-day crossover) | +79 % | −26 % | 7 |
| Orbit's 200-day rule on gold | +107 % | −20 % | 35 |
| Learning model (invest if chance > 50 %) | +13 % | −27 % | 46 |
| Learning model (invest if chance > 60 %) | +23 % | −24 % | 61 |

A few rules *did* beat holding on these years. For example, the 20/100-day crossover made +137 %. But they looked
bad on 2019–21, so choosing them now would be picking the winner after seeing the race, the classic way trading
bots fool their owners.

## The idea that held up
| Unseen years 2022 – Nov 2026 | Total return | Worst fall | Worst year |
|---|---|---|---|
| SPY with Orbit's rule (today) | +43 % | −19 % | −13.6 % |
| SPY just held | +63 % | −25 % | −18.7 % |
| **70 % SPY with Orbit's rule + 30 % gold held** | **+67 %** | **−12 %** | **−9.3 %** |
| 70 % SPY held + 30 % gold held | +81 % | −20 % | −12.8 % |

Gold and SPY usually move independently (daily correlation 0.17), so a gold slice cushions SPY's falls. Part of
the return comes from gold's exceptional 2024–25 run, so don't expect that number to repeat. The halved
worst fall is the more reliable part.

## What it means for Orbit
- **Learning model:** price-only AI did not find an edge in gold. It should not trade money yet. It can run in
  "shadow mode": predicting every day and being scored against what really happened, like the SPY forecast scorecard,
  until it proves itself on live data.
- **Trading gold in and out:** not supported by the evidence. Holding beat every rule chosen fairly.
- **Holding a gold slice inside Orbit:** supported by the evidence. Orbit would buy GLD with its own budget, keep it,
  and record it on the Performance page next to SPY.
