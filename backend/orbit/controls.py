"""Owner controls. Each is called only from authenticated, CSRF-checked POST endpoints and audited.

Descriptions shown in the UI before first use live in CONTROL_DESCRIPTIONS.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit import alerts
from orbit.broker.base import make_client_order_id
from orbit.db import Decision, EquityMark, OrderIntent, ReconciliationReport, StrategyVersion
from orbit.engine import (
    OPEN_INTENT_STATES, active_version, audit, control, create_version, next_eligible_session, open_intents, position,
    transition, transition_note, version_params,
)
from orbit.market_calendar import NEW_YORK, MarketCalendar
from orbit.money import D
from orbit.notify import emit
from orbit.risk import RiskPolicy
from orbit.strategy import INVESTED, StrategyParams

CONTROL_DESCRIPTIONS = {
    "observe": "OBSERVE: the worker keeps reading the paper account, evaluating the rule after each session and recording what it WOULD do. No order intents are created and nothing is submitted.",
    "activate": "Enable paper automation: from the next eligible session open, the worker may submit DAY market orders to your Alpaca PAPER account according to the rule. Requires passing setup checks and your password. No real money is involved, but orders are real paper orders.",
    "pause": "Pause new orders: no new automated orders (buys or sells) are submitted. Orders already at the broker and existing positions SURVIVE a pause; review them on the Orders page.",
    "resume": "Resume new orders: lifts your manual pause. Security, risk and review pauses are separate and stay in force.",
    "cancel_pending": "Cancel pending strategy orders: not-yet-submitted intents are skipped; working broker orders get a cancellation request. A request is not a confirmed cancellation; status updates after the broker confirms.",
    "close_position": "Close strategy position (paper): sells only the strategy's reconciled SPY quantity with a DAY market order at the next open window (or now if the market is open). Conflicting orders must be cancelled and resolved first. Never sells more than both the broker and the strategy show. It also pauses new automated orders so the rule does not buy back at the next open; resume when you are ready.",
}


class ControlError(ValueError):
    pass


def readiness(s: Session, now: datetime, broker_configured: bool) -> dict:
    """Setup checks required before activation. Each item is explained in plain English."""
    c = control(s)
    rep = s.scalar(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()))
    dec = s.scalar(select(Decision).where(Decision.account_key == c.account_fingerprint, Decision.symbol == "SPY").order_by(Decision.id.desc())) if c.account_fingerprint else None
    checks = [
        {"id": "credentials", "ok": broker_configured, "text": "Alpaca PAPER credentials found in local .env (values never shown)."},
        {"id": "account_read", "ok": bool(c.account_fingerprint), "text": "Worker has read the paper account at least once."},
        {"id": "reconciled", "ok": bool(rep and rep.ok and rep.at >= now - timedelta(minutes=10)),
         "text": "A clean reconciliation in the last 10 minutes (no unexpected holdings or orders)."},
        {"id": "no_security_pause", "ok": not c.security_paused, "text": "No security pause is active."},
        {"id": "no_review", "ok": not c.review_required, "text": "No intent is waiting for review."},
        {"id": "observed_decision", "ok": bool(dec and dec.data_mode == "ALPACA" and dec.action != "ERROR"),
         "text": "At least one decision computed from fetched Alpaca data has been observed."},
    ]
    return {"ready": all(x["ok"] for x in checks), "checks": checks}


def set_observe(s: Session, reason: str = "owner") -> None:
    c = control(s)
    prev = c.mode
    c.mode = "OBSERVE"
    audit(s, "user", "mode_observe", {"previous": prev, "reason": reason})
    pend = [i for i in open_intents(s, c.account_fingerprint) if i.state == "PENDING"] if c.account_fingerprint else []
    for i in pend:
        transition(s, i, "SKIPPED", "Returned to OBSERVE before submission.")
    emit(s, "automation_paused", "warning", "Paper automation disabled (OBSERVE)",
         f"Mode changed from {prev} to OBSERVE. {len(pend)} pending intent(s) skipped. Broker orders/positions are unchanged.",
         f"observe:{datetime.now().isoformat()}")


def activate(s: Session, now: datetime, cal: MarketCalendar, broker_configured: bool) -> dict:
    r = readiness(s, now, broker_configured)
    if not r["ready"]:
        raise ControlError("Setup checks not passed: " + ", ".join(x["id"] for x in r["checks"] if not x["ok"]))
    c = control(s)
    if c.mode == "PAPER_AUTO":
        return {"created_intent": None, "message": "Already active."}
    c.mode = "PAPER_AUTO"
    c.activated_at = now
    audit(s, "user", "mode_paper_auto", {"checks": r["checks"]}, "warning")
    emit(s, "automation_activated", "warning", "Paper automation ENABLED",
         "Automatic PAPER orders may now be submitted at the next eligible session open.", f"activate:{now.isoformat()}")
    # Onboarding rule: if flat and the latest completed close signals INVESTED, create the first entry intent.
    created = None
    pos = position(s, c.account_fingerprint)
    dec = s.scalar(select(Decision).where(Decision.account_key == c.account_fingerprint, Decision.symbol == "SPY", Decision.data_mode == "ALPACA")
                   .order_by(Decision.session.desc(), Decision.sequence.desc()))
    if dec and dec.target == INVESTED and pos.held_qty == 0 and not open_intents(s, c.account_fingerprint):
        created = _entry_from_decision(s, dec, now, cal, "Onboarding: flat while the latest completed close signals INVESTED.")
    return {"created_intent": created, "message": "Paper automation enabled."}


def _entry_from_decision(s: Session, dec: Decision, now: datetime, cal: MarketCalendar, why: str, parent_id: int | None = None) -> int | None:
    from orbit.strategy import entry_notional

    c = control(s)
    v = active_version(s)
    p, _ = version_params(v)
    sess = next_eligible_session(cal, dec.session, now, p.execution_window_minutes)
    if sess is None:
        return None
    latest = cal.latest_completed_session(now, p.completion_buffer_minutes)
    if latest is None or dec.session != latest.day:
        raise ControlError(f"The latest stored decision is for {dec.session}, but the latest completed session is "
                           f"{latest.day if latest else 'unknown'}. Wait for the worker to evaluate it, then retry.")
    snap_rep = s.scalar(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()))
    if not snap_rep or not snap_rep.ok or snap_rep.at < now - timedelta(minutes=10):
        raise ControlError("Need a clean broker reconciliation from the last 10 minutes for sizing.")
    equity, cash = D(snap_rep.broker["equity"]), D(snap_rep.broker["cash"])
    size = entry_notional(equity, cash, p)
    if size["too_small"]:
        raise ControlError(f"Entry size {size['target']:.2f} USD is below the minimum order.")
    attempt = (s.scalar(select(func.max(OrderIntent.attempt)).where(OrderIntent.account_key == c.account_fingerprint,
                                                                    OrderIntent.signal_session == dec.session, OrderIntent.kind == "ENTRY")) or 0) + 1
    it = OrderIntent(decision_id=dec.id, parent_intent_id=parent_id, account_key=c.account_fingerprint, symbol="SPY",
                     strategy_version_id=v.id, kind="ENTRY", side="BUY", signal_session=dec.session, execution_session=sess.day,
                     attempt=attempt, frozen_notional=size["target"],
                     sizing={k: (str(x) if isinstance(x, Decimal) else x) for k, x in size.items()},
                     client_order_id=make_client_order_id(c.account_fingerprint, v.id, "SPY", dec.session, "ENTRY", attempt),
                     created_at=now, reason=f"{why} Target {size['target']:.2f} USD, limited by {size['limited_by']}.")
    s.add(it)
    s.flush()
    transition_note(s, it, "Intent persisted before any broker contact.")
    title, body = alerts.planned_text(s, it)
    emit(s, "intent_created", "info", title, body, f"intent:{it.client_order_id}", ref=f"intent:{it.id}")
    return it.id


def pause_new_orders(s: Session, paused: bool) -> None:
    c = control(s)
    c.new_orders_paused = paused
    audit(s, "user", "pause_new_orders" if paused else "resume_new_orders", {})
    emit(s, "automation_paused" if paused else "automation_changed", "warning",
         "New orders paused by owner" if paused else "Owner resumed new orders",
         "Existing broker orders and positions are not affected by this switch.", f"pause:{paused}:{datetime.now().isoformat()}")


def cancel_pending(s: Session, engine, now: datetime) -> list[dict]:
    c = control(s)
    out = []
    for it in open_intents(s, c.account_fingerprint):
        res = engine._cancel_intent(s, it, "Cancelled by owner.", now)
        out.append({"intent_id": it.id, "result": res})
    audit(s, "user", "cancel_pending", {"results": out})
    return out


def close_position(s: Session, now: datetime, cal: MarketCalendar) -> int:
    c = control(s)
    rep = s.scalar(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()))
    if not rep or rep.at < now - timedelta(minutes=2):
        raise ControlError("Need a broker reconciliation from the last 2 minutes. Wait for the worker cycle and retry.")
    if open_intents(s, c.account_fingerprint):
        raise ControlError("Open strategy orders exist. Cancel them first and wait for confirmed terminal status.")
    pos = position(s, c.account_fingerprint)
    broker_qty = D(rep.broker.get("positions", {}).get("SPY", "0"))
    qty = min(pos.held_qty, broker_qty)
    if qty <= 0:
        raise ControlError("Strategy holds nothing to close (per reconciled strategy and broker records).")
    v = active_version(s)
    p, _ = version_params(v)
    today = now.astimezone(NEW_YORK).date()
    cur = cal.session(today)
    if cur and cur.open_utc <= now < cur.close_utc:
        sess = cur
    else:
        sess = next_eligible_session(cal, today - timedelta(days=1), now, p.execution_window_minutes)
    if sess is None:
        raise ControlError("No upcoming session in the calendar.")
    attempt = (s.scalar(select(func.max(OrderIntent.attempt)).where(OrderIntent.account_key == c.account_fingerprint,
                                                                    OrderIntent.signal_session == today, OrderIntent.kind == "CLOSE_MANUAL")) or 0) + 1
    it = OrderIntent(account_key=c.account_fingerprint, symbol="SPY", strategy_version_id=v.id, kind="CLOSE_MANUAL", side="SELL",
                     signal_session=today, execution_session=sess.day, attempt=attempt, target_qty=qty, created_at=now,
                     client_order_id=make_client_order_id(c.account_fingerprint, v.id, "SPY", today, "CLOSE_MANUAL", attempt),
                     reason=f"Owner requested closing the strategy position of {qty} SPY.")
    s.add(it)
    s.flush()
    transition_note(s, it, "Owner close request persisted before broker contact.")
    c.new_orders_paused = True  # otherwise the rule could re-enter at the next open while the signal is INVESTED
    audit(s, "user", "close_position", {"intent_id": it.id, "qty": str(qty), "paused_new_orders": True}, "warning")
    return it.id


def acknowledge_review(s: Session, intent_id: int, now: datetime, cal: MarketCalendar, create_new: bool,
                       confirm_absent_at_broker: bool, broker=None) -> dict:
    it = s.get(OrderIntent, intent_id)
    if it is None:
        raise ControlError("intent not found")
    if it.state == "AMBIGUOUS":
        if not confirm_absent_at_broker:
            raise ControlError("Ambiguous order: confirm you checked the Alpaca dashboard and no such order exists.")
        if broker is None:
            raise ControlError("Broker unavailable; cannot verify the ambiguous order.")
        if broker.get_order_by_client_id(it.client_order_id) is not None:
            raise ControlError("The broker now shows this order; it will be reconciled automatically. Not superseding.")
    elif it.state not in ("REJECTED", "MISSED_EXECUTION", "CANCELLED_ZERO_FILL", "REVIEW_REQUIRED"):
        raise ControlError(f"Intent state {it.state} does not need review.")
    transition(s, it, "SUPERSEDED", "Owner reviewed and acknowledged. History retained.", now=now)
    it.review_required = False
    c = control(s)
    if not s.scalar(select(OrderIntent.id).where(OrderIntent.review_required.is_(True))):
        c.review_required = False
        c.review_reason = ""
    audit(s, "user", "review_acknowledged", {"intent_id": it.id, "create_new": create_new})
    new_id = None
    if create_new and it.side == "BUY":
        dec = s.scalar(select(Decision).where(Decision.account_key == c.account_fingerprint, Decision.symbol == "SPY", Decision.data_mode == "ALPACA")
                       .order_by(Decision.session.desc(), Decision.sequence.desc()))
        if dec and dec.target == INVESTED and position(s, c.account_fingerprint).held_qty == 0 and c.mode == "PAPER_AUTO":
            new_id = _entry_from_decision(s, dec, now, cal, f"After review of intent {it.id}: latest completed signal still INVESTED.", parent_id=it.id)
    return {"superseded": it.id, "new_intent": new_id}


def resume_security(s: Session, now: datetime, note: str) -> None:
    c = control(s)
    rep = s.scalar(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()))
    if not rep or not rep.ok or rep.at < now - timedelta(minutes=5):
        raise ControlError("Resume requires a clean reconciliation from the last 5 minutes (no unexplained holdings/orders).")
    if len(note.strip()) < 10:
        raise ControlError("Describe what you reviewed (at least 10 characters).")
    c.security_paused = False
    audit(s, "user", "security_resumed", {"previous_reason": c.security_pause_reason, "note": note}, "warning")
    c.security_pause_reason = ""
    emit(s, "automation_changed", "warning", "Security pause lifted by owner", note[:300], f"secresume:{now.isoformat()}")


def resume_risk(s: Session, now: datetime, note: str, override: bool) -> None:
    c = control(s)
    if not c.risk_paused_entries:
        raise ControlError("Entries are not risk-paused.")
    v = active_version(s)
    p, risk = version_params(v)
    mark = s.scalar(select(EquityMark).where(EquityMark.account_key == c.account_fingerprint).order_by(EquityMark.session.desc()))
    dd = mark.drawdown if mark else Decimal(0)
    if len(note.strip()) < 10:
        raise ControlError("Record your review reason (at least 10 characters).")
    if not c.performance_valid:
        raise ControlError("Performance is invalid due to cash flows; reconcile cash activity and mark it reviewed first.")
    if dd >= risk.pause_fraction:
        if not override:
            raise ControlError(f"Drawdown {dd:.2%} is still at/above {risk.pause_fraction:.0%}; an explicit versioned override is required.")
        create_version(s, p, risk, f"Risk-policy override to resume entries at drawdown {dd:.2%}: {note}",
                       extra_risk={"override": {"reason": note, "at": now.isoformat(), "drawdown": str(dd)}})
    c.risk_paused_entries = False
    c.risk_override_reason = note if override else ""
    audit(s, "user", "risk_resumed", {"drawdown": str(dd), "override": override, "note": note}, "warning")
    c.risk_pause_reason = ""


def mark_cash_flows_reviewed(s: Session, note: str) -> None:
    c = control(s)
    if len(note.strip()) < 10:
        raise ControlError("Describe the cash activity you reconciled.")
    audit(s, "user", "cash_flows_reviewed", {"note": note, "previous": c.performance_invalid_reason}, "warning")
    c.performance_valid = True
    c.performance_invalid_reason = ""
    # High-water mark is NOT reset silently: next mark continues from the stored HWM; the note is in the audit log.


def update_settings(s: Session, values: dict, reason: str) -> dict:
    cur = active_version(s)
    p, risk = version_params(cur)
    merged = {**p.to_dict(), **{k: v for k, v in values.items() if k in p.to_dict() and k != "symbol"}}
    newp = StrategyParams.from_dict(merged)
    rp = RiskPolicy(D(values.get("warn_fraction", risk.warn_fraction)), D(values.get("pause_fraction", risk.pause_fraction)))
    if len(reason.strip()) < 5:
        raise ControlError("Give a reason for the change.")
    affected = [{"id": i.id, "kind": i.kind, "side": i.side, "state": i.state}
                for i in s.scalars(select(OrderIntent).where(OrderIntent.state == "PENDING"))]
    v = create_version(s, newp, rp, reason)
    emit(s, "settings_changed", "warning", "Strategy settings changed", f"New version {v.id}. {len(affected)} pending intent(s) moved to review.",
         f"settings:{v.id}")
    return {"version_id": v.id, "affected_pending_intents": affected}


def preview_settings(s: Session, values: dict) -> dict:
    cur = active_version(s)
    p, _ = version_params(cur)
    merged = {**p.to_dict(), **{k: v for k, v in values.items() if k in p.to_dict() and k != "symbol"}}
    newp = StrategyParams.from_dict(merged)
    pend = list(s.scalars(select(OrderIntent).where(OrderIntent.state == "PENDING")))
    return {
        "new_params": newp.to_dict(),
        "pending_intents": [{"id": i.id, "kind": i.kind, "side": i.side, "frozen_notional": str(i.frozen_notional) if i.frozen_notional else None,
                             "effect": "Will be paused for review (REVIEW_REQUIRED); it will not execute under the old version silently."} for i in pend],
    }
