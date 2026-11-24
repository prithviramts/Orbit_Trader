# November 2026 upgrades

## 1. Orbit keeps itself running

A Windows scheduled task named **Orbit Trading Lab** starts Orbit when you log in and checks every
10 minutes, restarting it if it stopped (for example after a Windows update restart).

- Install: `.\scripts\install-autostart.ps1` (already done on this computer; no admin needed)
- Remove: `.\scripts\uninstall-autostart.ps1`
- `.\scripts\stop.ps1` is respected: the watchdog does nothing until you run `.\scripts\start.ps1` again.
- What it did is logged in `data\logs\watchdog.log`.

The computer still has to be on and awake. For a warning on your phone when Orbit stops, finish
[HEARTBEAT.md](HEARTBEAT.md) and [ALERTS.md](ALERTS.md).

## 2. Shadow lab (ideas tracked with no orders)

After each US close, Orbit replays six lines with a virtual USD 1,000 each. Every line decides on the
close and trades at the next open, the same timing as the real strategies.

| Line | What it does |
|---|---|
| SPY 200-day rule | What the real SPY strategy does (reference) |
| Hold SPY | Buy once, never sell (the yardstick) |
| SPY rule + risk sizing | Same rule, holds less SPY when prices swing wildly (aims for about 15% yearly swings) |
| Hold gold | Buy GLD once, never sell |
| Gold 200-day rule | GLD above its 200-day average, cash below |
| 70% SPY rule + 30% gold | Rebalanced monthly |
| SPY 10-month rule | Checked only at month end: SPY above the average of its last 10 month-end closes, cash below (Faber) |
| SPY 200-day rule with 3% band | Sells only 3% below the 200-day average, buys back only 3% above; fewer false signals |
| SPY golden cross (50/200) | SPY while its 50-day average is above its 200-day average |
| Monthly buying (12 months) | One twelfth of the money into SPY at the start of each month for a year, then hold |

The last four were added on 14 November 2026. On 2022 to 13 November 2026 prices (context only):

| Line | Return | Worst fall | Trades |
|---|---|---|---|
| Hold SPY | +58% | 25% | 1 |
| SPY 200-day rule | +33% | 22% | 33 |
| SPY 10-month rule | +15% | 26% | 13 |
| SPY 200-day rule with 3% band | +37% | 21% | 9 |
| SPY golden cross (50/200) | +41% | 19% | 5 |
| Monthly buying (12 months) | +83% | 19% | 12 |

Monthly buying won here mostly because January 2022 was near a peak and the buying continued through the 2022
fall; it would have done worse than holding if prices had risen all year.

Two windows are shown: **since Orbit started** (the honest forward test) and **2022 to now** (context only).
Nothing here can place an order. If a shadow idea keeps doing better for about 6 months, it can be
switched on later; that needs a decision and a code change.

## 3. Performance page shows everything

The Performance page now has the real paper results for SPY and the Gold AI, the shadow lab chart and
table, and each real strategy in detail. Amounts are in USD with the AED reference value.

## 4. Monthly report card

On the 1st of each month (after 09:00 Dubai), Orbit posts a report card for the previous month to the
alerts bell, and to Telegram once it is set up. It covers the real results against holding, plus each
shadow idea. Preview it any time:

    cd backend
    uv run python -m orbit.cli report --month 2026-11

## 5. AI news reader (FinBERT)

FinBERT, a model trained on financial text, runs on this computer. It scores every SPY and gold headline
as positive, negative or neutral, and shows a 24-hour "news mood" on the News page.

- **It only warns.** News never places, changes or blocks an order.
- A **news warning** is sent (at most once per fund per day) when, in the last 24 hours, there are at
  least 6 headlines, of which at least 4 are strongly negative, and the average mood is -0.30 or lower.
- It pauses around the US open and just after the close, so it never slows down order handling.
- It can misread headlines (for example, "official does not comment" can read as negative).

Setup on a new computer (one-time, about 440 MB for the model plus the AI libraries):

    cd backend
    uv sync --extra news
    uv run --extra news python -m orbit.cli news-model download

Then restart Orbit. `start.ps1` includes the news libraries automatically once the model is present.

## 6. AI day trader (paper, added 24 November 2026)

A third paper strategy that trades during the day and never holds overnight.

- **When:** at about 18:45 Dubai (09:45 New York, after the first 15 minutes of trading), the model scores the day
  from the overnight gap, the first 15 minutes and recent days. It buys only above a 52% score.
- **What:** USD 250 of **IVV**, an S&P 500 fund like SPY. A different fund keeps its shares apart from the
  SPY 200-day rule's shares in the same account.
- **Safety exits:** sells straight away if IVV falls 0.75% below the buy price (stop-loss), and always sells
  10 minutes before the close. Anything left from an earlier day (for example the computer slept) is sold at the next open.
- **News veto:** no buy when the AI news reader shows a very negative day for SPY.
- **Learning:** retrains every week on SPY's 15-minute history since 2016. If its last 20 trades lose money in
  total, it pauses its own buys and alerts you.
- **Honest test (2019 to November 2026, unseen years, no costs):** AI about +33% over 776 trades (54% winners);
  the same trade every day about +67%; just holding SPY about +189%. It is an experiment, not an edge.
- **Needs the computer awake** from 18:30 to 01:00 Dubai on trading days, because the stop-loss is checked by Orbit.

Commands (from the `backend` folder): `uv run python -m orbit.cli day status|on|off|train|resume`.
The Performance page shows its latest decision, its trades and the model's test results.
