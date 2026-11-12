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
