"""The Today screen: one plain-English answer to "how am I doing, and is anything wrong?".

Read-only. It combines the latest broker snapshot, each strategy's latest decision, today's orders and
anything that needs attention. Amounts are USD; the page adds the AED reference value.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.db import (OPEN_INTENT_STATES, BrokerSnapshot, ControlState, Decision, EventInbox, OrderIntent, ReconciliationReport,
                      StrategyPosition)
from orbit.market_calendar import DUBAI, NEW_YORK, ensure_utc

STRATEGIES = [
    # (symbol, name, what it does in one line)
    ("SPY", "SPY 200-day rule", "Holds SPY while it is above its 200-day average, cash below. Decides after each close, trades at the next open."),
    ("GLD", "Gold AI", "A learning model that buys gold above a 55% score and sells below 45%. Trades at the next open."),
    ("IVV", "AI day trader", "Buys IVV at about 18:45 Dubai on days its model expects a win, and always sells before the close."),
]
ATTENTION_TYPES_SEVERITY = ("warning", "critical")


def _d(v) -> Decimal | None:
    return Decimal(str(v)) if v not in (None, "") else None


def _dubai(dt: datetime | None) -> str | None:
    return ensure_utc(dt).astimezone(DUBAI).strftime("%a %d %b, %H:%M") if dt else None


def _summary(reason: str) -> str:
    """The first sentence (what the strategy saw) and the last one (what it did), capitalised."""
    text = reason.split("): ", 1)[-1].replace("\n", " ")
    parts = [p.strip().rstrip(".") for p in text.split(". ") if p.strip()]
    keep = parts[:1] + (parts[-1:] if len(parts) > 1 else [])
    out = ". ".join(keep) + "."
    return out[:1].upper() + out[1:]


def _enabled(s: Session, symbol: str, c: ControlState) -> tuple[str, str]:
    """(status code, plain words) for a strategy's switch."""
    if symbol == "SPY":
        return ("on", "On") if c.mode == "PAPER_AUTO" else ("off", "Watching only (OBSERVE mode)")
    if symbol == "GLD":
        from orbit import gold_engine
        return ("on", "On") if gold_engine.get_settings(s)["enabled"] else ("off", "Off (still scored)")
    from orbit import day_engine
    gs = day_engine.get_settings(s)
    if gs["self_paused"]:
        return "paused", "Paused itself after a losing streak"
    return ("on", "On") if gs["enabled"] else ("off", "Off (still scored)")


def build(s: Session, cal, now: datetime, worker: dict) -> dict:
    c = s.get(ControlState, 1)
    akey = c.account_fingerprint if c else None
    snap = s.scalar(select(BrokerSnapshot).where(BrokerSnapshot.ok.is_(True)).order_by(BrokerSnapshot.id.desc()).limit(1))
    last_any = s.scalar(select(BrokerSnapshot).order_by(BrokerSnapshot.id.desc()).limit(1))

    # ---------------- money
    money = None
    if snap and snap.account:
        a = snap.account
        equity, last_eq = _d(a.get("equity")), _d(a.get("last_equity"))
        start_snap = None
        if c and c.activated_at:
            start_snap = s.scalar(select(BrokerSnapshot).where(BrokerSnapshot.ok.is_(True), BrokerSnapshot.taken_at >= c.activated_at)
                                  .order_by(BrokerSnapshot.id).limit(1))
        start_eq = _d(start_snap.account.get("equity")) if start_snap and start_snap.account else None
        holdings = []
        for p in snap.positions or []:
            qty, avg, mv = _d(p.get("qty")), _d(p.get("avg_entry_price")), _d(p.get("market_value"))
            cost = qty * avg if qty is not None and avg is not None else None
            holdings.append({"symbol": p["symbol"], "qty": str(qty), "value": str(mv) if mv is not None else None,
                             "cost": str(cost.quantize(Decimal("0.01"))) if cost is not None else None,
                             "gain": str((mv - cost).quantize(Decimal("0.01"))) if mv is not None and cost is not None else None})
        money = {"equity": str(equity), "cash": a.get("cash"),
                 "today": str((equity - last_eq).quantize(Decimal("0.01"))) if equity is not None and last_eq is not None else None,
                 "since_start": str((equity - start_eq).quantize(Decimal("0.01"))) if equity is not None and start_eq is not None else None,
                 "start_equity": str(start_eq) if start_eq is not None else None,
                 "invested": str(sum((_d(h["value"]) or Decimal(0) for h in holdings), Decimal(0))),
                 "holdings": holdings, "as_of": _dubai(snap.taken_at)}

    # ---------------- strategies
    strategies = []
    for symbol, name, about in STRATEGIES:
        code, words = _enabled(s, symbol, c) if c else ("off", "Not set up")
        d = s.scalar(select(Decision).where(Decision.symbol == symbol).order_by(Decision.session.desc(), Decision.sequence.desc(), Decision.id.desc()).limit(1))
        pos = s.scalar(select(StrategyPosition).where(StrategyPosition.account_key == akey, StrategyPosition.symbol == symbol)) if akey else None
        pending = s.scalar(select(OrderIntent).where(OrderIntent.symbol == symbol, OrderIntent.state.in_(OPEN_INTENT_STATES))
                           .order_by(OrderIntent.id).limit(1))
        prob = (d.inputs or {}).get("probability") if d else None
        strategies.append({
            "symbol": symbol, "name": name, "about": about, "status": code, "status_text": words,
            "held_qty": str(pos.held_qty) if pos else "0",
            "latest": None if not d else {"session": d.session.isoformat(), "action": d.action, "score": prob,
                                          "summary": _summary(d.reason), "reason": d.reason},
            "pending": None if not pending else {"side": pending.side, "state": pending.state,
                                                 "when": pending.execution_session.isoformat(),
                                                 "amount": str(pending.frozen_notional) if pending.frozen_notional else None,
                                                 "qty": str(pending.target_qty) if pending.target_qty else None},
        })

    # ---------------- today's orders (the current or most recent New York session)
    sess = cal.session_containing(now) or cal.latest_completed_session(now, 0)
    day = sess.day if sess else now.astimezone(NEW_YORK).date()
    orders = []
    for it in s.scalars(select(OrderIntent).where(OrderIntent.execution_session == day).order_by(OrderIntent.id)):
        orders.append({"symbol": it.symbol, "side": it.side, "state": it.state, "kind": it.kind,
                       "amount": str(it.submitted_notional or it.frozen_notional) if (it.submitted_notional or it.frozen_notional) else None,
                       "filled_qty": str(it.filled_qty), "price": str(it.filled_avg_price) if it.filled_avg_price else None,
                       "why": (it.sizing or {}).get("exit_reason") or "", "at": _dubai(it.submitted_at or it.created_at)})

    # ---------------- needs attention
    attention = []
    if c:
        if c.security_paused:
            attention.append({"level": "bad", "text": f"Orbit paused itself for safety: {c.security_pause_reason}"})
        if c.review_required:
            attention.append({"level": "bad", "text": "An order needs your review on the Trades page."})
        if c.risk_paused_entries:
            attention.append({"level": "warn", "text": f"New buys are paused: {c.risk_pause_reason}"})
        if c.new_orders_paused:
            attention.append({"level": "warn", "text": "You paused new orders."})
    if worker.get("state") != "RUNNING":
        attention.append({"level": "bad", "text": f"The trading worker is not running. {worker.get('text', '')}".strip()})
    if last_any and not last_any.ok:
        attention.append({"level": "warn", "text": "Orbit cannot reach Alpaca right now (internet or Alpaca problem). It retries every 20 seconds."})
    recent = list(s.scalars(select(EventInbox).where(EventInbox.severity.in_(ATTENTION_TYPES_SEVERITY), EventInbox.read_at.is_(None),
                                                     EventInbox.at >= now - timedelta(hours=24)).order_by(EventInbox.id.desc()).limit(5)))
    if worker.get("state") == "RUNNING":  # a stop that was followed by a restart is history, not a problem now
        recent = [e for e in recent if e.type != "worker_stopped"]
    alerts = [{"title": e.title, "at": _dubai(e.at), "severity": e.severity} for e in recent]
    rep = s.scalar(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()).limit(1))

    # ---------------- what happens next
    nxt = []
    cur = cal.session_containing(now)
    if cur:
        nxt.append(f"US market is open until {_dubai(cur.close_utc)} Dubai.")
    else:
        up = next((x for x in cal.sessions if x.open_utc > now), None)
        if up:
            nxt.append(f"US market opens {_dubai(up.open_utc)} Dubai.")
    for st in strategies:
        if st["pending"]:
            p = st["pending"]
            what = f"USD {p['amount']}" if p["amount"] else f"{p['qty']} shares"
            nxt.append(f"{st['name']}: {p['side'].lower()} {what} of {st['symbol']} planned for {p['when']} ({p['state'].lower()}).")

    bad = any(a["level"] == "bad" for a in attention)
    headline = ("Something needs your attention." if bad else
                "Running, with warnings to look at." if attention or alerts else
                "All good. Orbit is running and the account matches.")
    return {"headline": headline, "level": "bad" if bad else "warn" if (attention or alerts) else "ok",
            "money": money, "strategies": strategies, "orders": {"day": day.isoformat(), "items": orders},
            "attention": attention, "alerts": alerts, "next": nxt,
            "checked": {"ok": rep.ok, "at": _dubai(rep.at)} if rep else None, "worker": worker}
