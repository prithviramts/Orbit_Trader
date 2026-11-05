"""Forecast scorecard: record a next-session UP/DOWN call before the open, score it after the close.

Learning only. Nothing here is read by the engine, so forecasts can never place or change orders.
A forecast must be saved before its target session opens (no hindsight), one per session.
Scoring compares the target session's close with the previous session's close from Orbit's stored
Alpaca daily bars, and compares your hit rate with the naive "always say UP" baseline.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from math import comb

from orbit.market_calendar import NEW_YORK, MarketCalendar, Session

MIN_MEANINGFUL = 60  # roughly three months of sessions before the hit rate says much


class ForecastError(ValueError):
    pass


def target_for(cal: MarketCalendar, now: datetime) -> tuple[Session, Session]:
    """The next session that has not opened yet, and the session before it (the comparison close)."""
    probe = now.astimezone(NEW_YORK).date()
    s = cal.session(probe)
    target = s if (s is not None and now < s.open_utc) else cal.next_session(probe)
    if target is None:
        raise ForecastError("no upcoming session in the exchange calendar")
    base = cal.previous_session(target.day)
    if base is None:
        raise ForecastError("no previous session in the exchange calendar")
    return target, base


def outcome_of(base_close: Decimal, target_close: Decimal) -> str:
    if target_close > base_close:
        return "UP"
    if target_close < base_close:
        return "DOWN"
    return "FLAT"


def score_pending(rows, closes: dict[date, Decimal], now: datetime) -> int:
    """Fill in outcomes for unscored forecasts whose two closes are available. Returns how many were scored."""
    n = 0
    for f in rows:
        if f.outcome is not None:
            continue
        b, t = closes.get(f.base_session), closes.get(f.target_session)
        if b is None or t is None:
            continue
        f.base_close, f.outcome_close = b, t
        f.outcome = outcome_of(b, t)
        f.correct = None if f.outcome == "FLAT" else (f.direction == f.outcome)
        f.scored_at = now
        n += 1
    return n


def _p_at_least(k: int, n: int) -> float:
    """Chance of k or more hits out of n by pure coin-flipping (one-sided binomial, p=0.5)."""
    return sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0


def scorecard(rows) -> dict:
    scored = [f for f in rows if f.correct is not None]
    n = len(scored)
    hits = sum(1 for f in scored if f.correct)
    ups = sum(1 for f in scored if f.outcome == "UP")
    acc = hits / n if n else None
    base = ups / n if n else None
    p = _p_at_least(hits, n) if n else None
    if n < 20:
        verdict = f"Too early to tell: {n} scored forecast(s). Luck dominates until there are dozens."
    elif n < MIN_MEANINGFUL:
        verdict = f"Early signal only ({n} scored). Keep going to at least {MIN_MEANINGFUL} before drawing conclusions."
    elif acc is not None and base is not None and acc > base and p is not None and p < 0.05:
        verdict = "Your calls beat both a coin flip and 'always UP' so far. Keep testing before letting it influence anything."
    else:
        verdict = "So far your calls do not clearly beat a coin flip or simply saying 'always UP'."
    return {"scored": n, "hits": hits, "accuracy": acc, "always_up_accuracy": base, "p_value_vs_coin": p,
            "pending": sum(1 for f in rows if f.outcome is None), "flat": sum(1 for f in rows if f.outcome == "FLAT"),
            "verdict": verdict, "min_meaningful": MIN_MEANINGFUL}
