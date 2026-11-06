"""Daily 200-day-average rule on gold (GLD), compared with SPY and with simply holding.

Same rule as the live SPY strategy: after each close, if the close is above its 200-session average the
target is INVESTED, if below it is CASH, if exactly equal keep the previous target. The change is
executed at the NEXT session's open (no peeking), with slippage charged on every fill. Research only:
nothing here is imported by the engine or worker, and nothing here can place orders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DayBar:
    day: date
    open: float
    close: float


def sma_rule(bars: list[DayBar], slip: float, start_cash: float = 10_000.0, window: int = 200) -> dict:
    """Equity curve (at closes) for the SMA rule. The first `window - 1` sessions are warm-up (held in cash)."""
    cash, qty = start_cash, 0.0
    target = "CASH"
    pending: str | None = None
    closes: list[float] = []
    run = 0.0
    curve: list[tuple[date, float]] = []
    trades = invested = 0
    for b in bars:
        if pending and pending != ("INVESTED" if qty > 0 else "CASH"):
            if pending == "INVESTED":
                px = b.open * (1 + slip)
                qty, cash = cash / px, 0.0
            else:
                cash, qty = qty * b.open * (1 - slip), 0.0
            trades += 1
        pending = None
        closes.append(b.close)
        run += b.close
        if len(closes) > window:
            run -= closes[-window - 1]
        if len(closes) >= window:
            avg = run / window
            if b.close > avg:
                target = "INVESTED"
            elif b.close < avg:
                target = "CASH"
            pending = target
        if qty > 0:
            invested += 1
        curve.append((b.day, cash + qty * b.close))
    return {"curve": curve, "trades": trades, "invested_days": invested}


def hold(bars: list[DayBar], slip: float, start_cash: float = 10_000.0, skip: int = 0) -> dict:
    """Buy at the open of session `skip` (to start on the same day as the rule) and never sell."""
    curve, qty = [], 0.0
    for i, b in enumerate(bars):
        if i == skip:
            qty = start_cash / (b.open * (1 + slip))
        curve.append((b.day, start_cash if i < skip else qty * b.close))
    return {"curve": curve, "trades": 1, "invested_days": len(bars) - skip}


def combine(a: list[tuple[date, float]], b: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Half the money in each (no rebalancing). Both curves must cover the same days."""
    return [(d, (x + y) / 2) for (d, x), (_, y) in zip(a, b, strict=True)]


def metrics(curve: list[tuple[date, float]], start_index: int = 0) -> dict:
    pts = curve[start_index:]
    if len(pts) < 2:
        return {}
    v0, v1 = pts[0][1], pts[-1][1]
    years = max((pts[-1][0] - pts[0][0]).days / 365.25, 1e-9)
    peak, mdd = v0, 0.0
    rets = []
    for (_, a), (_, b) in zip(pts, pts[1:]):
        rets.append(b / a - 1 if a else 0.0)
    for _, v in pts:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak if peak else 0)
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1))
    by_year: dict[int, list[float]] = {}
    for d, v in pts:
        by_year.setdefault(d.year, []).append(v)
    yearly, prev = {}, v0
    for y in sorted(by_year):
        end = by_year[y][-1]
        yearly[y] = round((end / prev - 1) * 100, 2)
        prev = end
    return {
        "from": pts[0][0].isoformat(), "to": pts[-1][0].isoformat(),
        "total_return_pct": round((v1 / v0 - 1) * 100, 2),
        "cagr_pct": round(((v1 / v0) ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "volatility_pct": round(sd * math.sqrt(252) * 100, 2),
        "return_per_risk": round(((v1 / v0) ** (1 / years) - 1) / (sd * math.sqrt(252)), 2) if sd else None,
        "worst_year_pct": min(yearly.values()) if yearly else None,
        "yearly_pct": yearly,
    }


def correlation(a: list[DayBar], b: list[DayBar]) -> float:
    ra = [y.close / x.close - 1 for x, y in zip(a, a[1:])]
    rb = [y.close / x.close - 1 for x, y in zip(b, b[1:])]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return round(cov / (va * vb), 3) if va and vb else 0.0
