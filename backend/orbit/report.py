"""Monthly report card: once a month, a plain-English summary of last month, sent to the alerts bell
(and to Telegram when it is set up).

It covers the real paper results (from Orbit's own fills) and the shadow lab's ideas over the same month,
next to simply holding. Sent on the first worker cycle after 09:00 Dubai on or after the 1st. Months before
Orbit started paper trading are skipped.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from orbit import performance, shadow
from orbit.config import Settings
from orbit.db import KV, ControlState
from orbit.market_calendar import DUBAI
from orbit.money import round_money, usd_to_aed

KV_KEY = "monthly_report"
SEND_AFTER_HOUR_DUBAI = 9


def _prev_month(d: date) -> str:
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def _money(v: Decimal) -> str:
    q = round_money(v)
    sign = "+" if q > 0 else ""
    return f"{sign}{q:,.2f} USD ({sign}{round_money(usd_to_aed(q)):,.2f} AED)"


def _month_change(series: list[dict], month: str) -> tuple[float, float] | None:
    """(start value, end value) of a shadow line over ``month``; the start is the last value before it."""
    before = [p for p in series if p["day"][:7] < month]
    inside = [p for p in series if p["day"][:7] == month]
    if not inside:
        return None
    start = before[-1]["value"] if before else shadow.START_CAPITAL
    return start, inside[-1]["value"]


def build(s: Session, month: str) -> tuple[str, str]:
    c = s.get(ControlState, 1)
    akey = c.account_fingerprint if c else None
    lines = [f"📅 Orbit report card for {month} (paper money)", ""]
    lines.append("Real paper strategies (from actual fills):")
    for symbol, name in (("SPY", "SPY 200-day rule"), ("GLD", "Gold AI")):
        perf = performance.for_account(s, akey, symbol, performance.stored_closes(s, symbol))
        row = next((m for m in perf.get("months", []) if m["month"] == month), None) if perf.get("available") else None
        if row is None:
            lines.append(f"• {name}: no trades held this month.")
            continue
        st, bm = Decimal(row["strategy"]), Decimal(row["benchmark"])
        verdict = "ahead of" if st > bm else "behind" if st < bm else "level with"
        lines.append(f"• {name}: {_money(st)}; holding the same money: {_money(bm)}. {verdict.capitalize()} holding.")
    lab = shadow.get(s)
    if lab.get("available"):
        lines += ["", "Shadow lab (virtual USD 1,000 each, no orders):"]
        for idea in lab["ideas"]:
            live = idea.get("live") or {}
            ch = _month_change(live.get("series", []), month) if live.get("available") else None
            if ch:
                a, b = ch
                lines.append(f"• {idea['name']}: {(b / a - 1) * 100:+.2f}% this month, {live['return_pct']:+.2f}% since start.")
        lines += ["", "Shadow ideas are recorded only. Nothing is switched on unless you decide to."]
    lines += ["", "One month is far too short to judge a strategy; about 6 months of results are needed.",
              "Paper account: simulated money, no real funds."]
    return f"Orbit report card: {month}", "\n".join(lines)


def maybe_send(s: Session, settings: Settings, now: datetime) -> bool:
    from orbit.notify import emit
    local = now.astimezone(DUBAI)
    if local.hour < SEND_AFTER_HOUR_DUBAI:
        return False
    month = _prev_month(local.date())
    row = s.get(KV, KV_KEY)
    if row and row.value.get("month") >= month:
        return False
    c = s.get(ControlState, 1)
    started = c.activated_at.astimezone(DUBAI).strftime("%Y-%m") if c and c.activated_at else None
    if started is None or month < started:
        return False
    title, body = build(s, month)
    emit(s, "monthly_report", "info", title, body, f"monthly_report:{month}", settings=settings,
         public_summary=f"Orbit Trading Lab: report card for {month} is ready. Open the app to read it.")
    value = {"month": month, "sent_at": now.isoformat()}
    if row:
        row.value = value
    else:
        s.add(KV(key=KV_KEY, value=value))
    return True
