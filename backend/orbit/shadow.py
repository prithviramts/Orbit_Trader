"""Shadow lab: alternative strategies tracked side by side with the real ones. They never place orders.

Each idea starts with a virtual USD 1,000. It decides on a session's official close and trades at the next
session's open, the same timing as the real strategies, with no fees (Alpaca charges none for these ETFs).
Two windows are reported:

- "live": from the day Orbit started paper trading. This is the honest forward test, but it is short at first.
- "history": from 2022-01-03 to now, the same hold-out period the earlier research used, for context.

The real strategies appear as simulated references too ("SPY 200-day rule", "Hold SPY"), so every line
uses the same money and the same prices. The Gold AI's actual paper result comes from its real fills on
the Performance page; it is not re-simulated here because the model retrains and would look better in
hindsight than it really was.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from sqlalchemy.orm import Session

from orbit.db import KV, ControlState
from orbit.market_calendar import NEW_YORK, UTC

log = logging.getLogger(__name__)

KV_KEY = "shadow_lab"
START_CAPITAL = 1000.0
HISTORY_START = date(2022, 1, 3)
WARMUP_DAYS = 420  # calendar days of extra history so the 200-day averages exist at the start
RETRY_MINUTES = 30
TARGET_VOL = 0.15  # risk sizing: aim for about 15% yearly swings, roughly SPY's long-run average
REBALANCE_BAND = 0.10  # risk sizing only changes the holding when the target moves by 10 points or more

Bar = tuple[date, float, float]  # (day, open, close)


@dataclass(frozen=True)
class Idea:
    id: str
    name: str
    about: str
    kind: str  # "reference" (what Orbit already does or plain holding) or "candidate" (could be switched on later)


IDEAS = [
    Idea("spy_rule", "SPY 200-day rule", "What Orbit's real SPY strategy does: hold SPY above its 200-day average, cash below.", "reference"),
    Idea("spy_hold", "Hold SPY", "Buy SPY once and never sell. The yardstick to beat.", "reference"),
    Idea("spy_risk", "SPY rule + risk sizing",
         "Same 200-day rule, but hold less SPY when prices swing wildly (aims for about 15% yearly swings, never more than 100%).", "candidate"),
    Idea("gold_hold", "Hold gold", "Buy GLD once and never sell.", "candidate"),
    Idea("gold_rule", "Gold 200-day rule", "Hold GLD above its 200-day average, cash below.", "candidate"),
    Idea("mix_70_30", "70% SPY rule + 30% gold",
         "70% follows the SPY 200-day rule, 30% always holds gold. Rebalanced at the start of each month.", "candidate"),
]


def _sma(closes: list[float], i: int, n: int) -> float | None:
    if i + 1 < n:
        return None
    return sum(closes[i + 1 - n:i + 1]) / n


def _vol20(closes: list[float], i: int) -> float | None:
    if i < 21:
        return None
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(i - 19, i + 1)]
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)


def _targets(idea: str, i: int, spy: list[float], gld: list[float], cur: dict, days: list[date]) -> dict | None:
    """Target weights decided on close i. None means "keep what you hold"."""
    s200, g200 = _sma(spy, i, 200), _sma(gld, i, 200)
    spy_up = s200 is not None and spy[i] > s200
    if idea == "spy_hold":
        return {"SPY": 1.0, "GLD": 0.0}
    if idea == "gold_hold":
        return {"SPY": 0.0, "GLD": 1.0}
    if idea == "spy_rule":
        return {"SPY": 1.0 if spy_up else 0.0, "GLD": 0.0} if s200 is not None else None
    if idea == "gold_rule":
        return {"SPY": 0.0, "GLD": 1.0 if (g200 is not None and gld[i] > g200) else 0.0} if g200 is not None else None
    if idea == "spy_risk":
        if s200 is None:
            return None
        if not spy_up:
            return {"SPY": 0.0, "GLD": 0.0}
        v = _vol20(spy, i)
        w = 1.0 if not v else min(1.0, TARGET_VOL / v)
        if cur.get("SPY", 0.0) > 0 and abs(w - cur["SPY"]) < REBALANCE_BAND:
            return None
        return {"SPY": round(w, 3), "GLD": 0.0}
    if idea == "mix_70_30":
        if s200 is None:
            return None
        want = {"SPY": 0.7 if spy_up else 0.0, "GLD": 0.3}
        new_month = i + 1 < len(days) and days[i + 1].month != days[i].month
        flipped = (cur.get("SPY", 0.0) > 0) != spy_up
        return want if (new_month or flipped or not cur) else None
    raise ValueError(idea)


def simulate(idea: str, days: list[date], spy: list[Bar], gld: list[Bar], start: date) -> dict:
    """Run one idea from ``start``. Bars are aligned by index with ``days``."""
    sc = [b[2] for b in spy]
    gc = [b[2] for b in gld]
    so = [b[1] for b in spy]
    go = [b[1] for b in gld]
    first = next((k for k, d in enumerate(days) if d >= start), None)
    if first is None or first == 0:
        return {"available": False, "reason": "Not enough price history yet."}
    cash, qty = START_CAPITAL, {"SPY": 0.0, "GLD": 0.0}
    weights: dict = {}
    trades = 0
    series = []
    peak, max_dd = START_CAPITAL, 0.0
    pending = _targets(idea, first - 1, sc, gc, weights, days)  # decided on the close before the start day
    for k in range(first, len(days)):
        if pending is not None:  # trade at today's open
            value = cash + qty["SPY"] * so[k] + qty["GLD"] * go[k]
            new_qty = {"SPY": value * pending["SPY"] / so[k], "GLD": value * pending["GLD"] / go[k]}
            if any(abs(new_qty[x] - qty[x]) * (so[k] if x == "SPY" else go[k]) > 0.01 for x in qty):
                trades += sum(1 for x in qty if abs(new_qty[x] - qty[x]) * (so[k] if x == "SPY" else go[k]) > 0.01)
            qty = new_qty
            cash = value - qty["SPY"] * so[k] - qty["GLD"] * go[k]
            weights = dict(pending)
        value = cash + qty["SPY"] * sc[k] + qty["GLD"] * gc[k]
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak)
        series.append({"day": days[k].isoformat(), "value": round(value, 2)})
        pending = _targets(idea, k, sc, gc, weights, days)
    last = series[-1]["value"]
    nxt = pending if pending is not None else weights
    return {
        "available": True,
        "start": series[0]["day"], "as_of": series[-1]["day"], "sessions": len(series),
        "value": last, "return_pct": round((last / START_CAPITAL - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2), "trades": trades,
        "holding": {x: round(w, 3) for x, w in weights.items()} if weights else {"SPY": 0.0, "GLD": 0.0},
        "next": {x: round(w, 3) for x, w in nxt.items()} if nxt else {"SPY": 0.0, "GLD": 0.0},
        "changes_next_open": pending is not None and pending != weights,
        "series": series,
    }


def _weekly(series: list[dict]) -> list[dict]:
    """Keep the last point of each week so the long history chart stays light."""
    out: dict[str, dict] = {}
    for p in series:
        y, w, _ = date.fromisoformat(p["day"]).isocalendar()
        out[f"{y}-{w}"] = p
    return list(out.values())


def _align(spy: list[Bar], gld: list[Bar]) -> tuple[list[date], list[Bar], list[Bar]]:
    g = {b[0]: b for b in gld}
    days, s_out, g_out = [], [], []
    for b in spy:
        if b[0] in g:
            days.append(b[0])
            s_out.append(b)
            g_out.append(g[b[0]])
    return days, s_out, g_out


def live_start(s: Session) -> date | None:
    c = s.get(ControlState, 1)
    if c is None or c.activated_at is None:
        return None
    return c.activated_at.astimezone(NEW_YORK).date()


def build(spy: list[Bar], gld: list[Bar], start_live: date | None) -> dict:
    days, sp, gd = _align(spy, gld)
    if len(days) < 210:
        return {"available": False, "reason": "Not enough price history to compute the 200-day averages."}
    ideas = []
    for idea in IDEAS:
        hist = simulate(idea.id, days, sp, gd, HISTORY_START)
        if hist.get("available"):
            hist["series"] = _weekly(hist["series"])
        live = simulate(idea.id, days, sp, gd, start_live) if start_live else {"available": False, "reason": "Paper trading has not started."}
        ideas.append({"id": idea.id, "name": idea.name, "about": idea.about, "kind": idea.kind, "history": hist, "live": live})
    return {
        "available": True, "as_of": days[-1].isoformat(), "start_live": start_live.isoformat() if start_live else None,
        "history_start": HISTORY_START.isoformat(), "start_capital": START_CAPITAL,
        "spy_close": sp[-1][2], "gld_close": gd[-1][2],
        "ideas": ideas,
        "notes": [
            "Shadow ideas never place orders. They are recorded so you can compare before switching anything on.",
            "Each line starts with a virtual USD 1,000, decides on the close and trades at the next open. No fees.",
            "The 'history' window (2022 to now) is for context only: past results do not promise future ones.",
        ],
    }


def get(s: Session) -> dict:
    row = s.get(KV, KV_KEY)
    return dict(row.value) if row else {"available": False, "reason": "The shadow lab runs after the next market close."}


def _save(s: Session, value: dict) -> None:
    row = s.get(KV, KV_KEY)
    if row:
        row.value = value
    else:
        s.add(KV(key=KV_KEY, value=value))


def maybe_update(s: Session, calendar, now: datetime, fetch: Callable[[str, date, datetime], list[Bar]]) -> bool:
    """Recompute once per completed session. ``fetch(symbol, start, end)`` returns daily (day, open, close) bars."""
    sess = calendar.latest_completed_session(now, buffer_minutes=20)
    if sess is None:
        return False
    cur = get(s)
    if cur.get("as_of") == sess.day.isoformat():
        return False
    last_try = cur.get("last_attempt")
    if last_try and cur.get("attempt_for") == sess.day.isoformat() and \
            now - datetime.fromisoformat(last_try) < timedelta(minutes=RETRY_MINUTES):
        return False
    start = HISTORY_START - timedelta(days=WARMUP_DAYS)
    end = now - timedelta(minutes=16)  # free market data plan: consolidated data must be 15+ minutes old
    try:
        spy = [b for b in fetch("SPY", start, end) if b[0] <= sess.day]
        gld = [b for b in fetch("GLD", start, end) if b[0] <= sess.day]
        if not spy or spy[-1][0] != sess.day or not gld or gld[-1][0] != sess.day:
            raise RuntimeError(f"prices for {sess.day} are not available yet")
        out = build(spy, gld, live_start(s))
    except Exception as e:  # noqa: BLE001 - shadow research must never affect trading
        log.warning("shadow lab update failed: %s", e)
        keep = {k: v for k, v in cur.items() if k not in ("last_attempt", "attempt_for", "error")}
        _save(s, {**keep, "last_attempt": now.astimezone(UTC).isoformat(), "attempt_for": sess.day.isoformat(), "error": str(e)[:300]})
        return False
    out["computed_at"] = now.astimezone(UTC).isoformat()
    _save(s, out)
    log.info("shadow lab updated for %s", sess.day)
    return True
