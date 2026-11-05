"""Plain-English trade alert texts: what happened, at what price, why, and what it means.

Only used to build notification bodies. Detailed bodies go to private channels (Telegram chat); public
channels (an ntfy topic) get the redacted summary instead. Never includes keys or account numbers.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.db import Decision, Fill, OrderIntent
from orbit.market_calendar import DUBAI
from orbit.money import round_money, usd_to_aed

PAPER_NOTE = "Paper account: simulated money, no real funds."


def _usd(v: Decimal) -> str:
    q = round_money(v)
    return f"USD {q:,.2f} (≈ AED {round_money(usd_to_aed(q)):,.2f})"


def _signed(v: Decimal) -> str:
    q = round_money(v)
    sign = "+" if q > 0 else ""
    return f"{sign}{q:,.2f} USD (≈ AED {sign}{round_money(usd_to_aed(q)):,.2f})"


def why(s: Session, it: OrderIntent) -> str:
    """The rule's reason for this order: the decision's explanation when there is one, else the intent's."""
    if it.decision_id:
        d = s.get(Decision, it.decision_id)
        if d and d.reason:
            return d.reason
    return it.reason or "No reason recorded."


def realized_for_fill(s: Session, it: OrderIntent, fill_id: int) -> Decimal | None:
    """FIFO profit/loss realised by one SELL fill (same convention as the strategy ledger)."""
    from orbit.accounting import Ledger

    led = Ledger(Decimal("1e15"))
    rows = s.scalars(select(Fill).join(OrderIntent, Fill.intent_id == OrderIntent.id)
                     .where(OrderIntent.account_key == it.account_key, OrderIntent.symbol == it.symbol).order_by(Fill.at, Fill.id))
    for f in rows:
        if f.side == "BUY":
            led.buy(f.qty, f.price, Decimal(0))
        else:
            r = led.sell(min(f.qty, led.qty), f.price, Decimal(0))
            if f.id == fill_id:
                return r
    return None


def fill_text(s: Session, it: OrderIntent, fill: Fill, held_after: Decimal, cost_after: Decimal) -> tuple[str, str]:
    verb = "BOUGHT" if fill.side == "BUY" else "SOLD"
    value = fill.qty * fill.price
    lines = [
        f"{'🟢' if fill.side == 'BUY' else '🔴'} {verb} {fill.qty.normalize():f} {it.symbol} at {fill.price.quantize(Decimal('0.01'))} USD",
        f"Value: {_usd(value)}",
        f"Time: {fill.at.astimezone(DUBAI):%a %d %b %H:%M:%S} Dubai",
        "",
        f"Why: {why(s, it)}",
    ]
    if fill.side == "SELL":
        pnl = realized_for_fill(s, it, fill.id)
        if pnl is not None:
            cost = value - pnl
            pct = (pnl / cost * 100) if cost else Decimal(0)
            lines += ["", f"Result of this sale: {_signed(pnl)} ({pct.quantize(Decimal('0.01')):+}%)"]
    lines += ["", f"Strategy now holds: {held_after.normalize():f} {it.symbol}" + (f" (cost {_usd(cost_after)})" if held_after > 0 else " (all cash)"),
              f"Order: {it.kind} {it.side}, session {it.execution_session}, ref {it.client_order_id}", PAPER_NOTE]
    return f"Orbit: {verb} {it.symbol}", "\n".join(lines)


def submitted_text(s: Session, it: OrderIntent, status: str) -> tuple[str, str]:
    amount = (f"{_usd(it.submitted_notional)} worth" if it.submitted_notional
              else f"{it.target_qty.normalize():f} shares" if it.target_qty else "")
    lines = [f"📤 Order sent to Alpaca: {it.side} {it.symbol} {amount}".rstrip(),
             f"Broker status: {status} (accepted is not filled; a fill alert follows).",
             "", f"Why: {why(s, it)}", "", PAPER_NOTE]
    return f"Orbit: {it.side} order sent", "\n".join(lines)


def planned_text(s: Session, it: OrderIntent) -> tuple[str, str]:
    amount = _usd(it.frozen_notional) if it.frozen_notional else (f"{it.target_qty.normalize():f} shares" if it.target_qty else "")
    lines = [f"🗓️ Planned: {it.side} {it.symbol} {amount} at the next US open ({it.execution_session}).",
             "Orbit sends it in the first 5 minutes after the open, 18:30 Dubai in winter / 17:30 in summer.",
             "", f"Why: {why(s, it)}", "", "Keep Orbit running then. You can cancel it on the Orders page.", PAPER_NOTE]
    return f"Orbit: {it.side} planned", "\n".join(lines)
