"""Forward operation: evaluation, intents, submission, reconciliation and recovery.

State is kept in separate fields (never inferred from one another):
  StrategyPosition.signal_target   what the rule wants (INVESTED/CASH)
  StrategyPosition.held_qty        what reconciled, confirmed fills say the strategy owns
  OrderIntent.state                lifecycle of each order intent (table below)
  ControlState.*                   mode (OBSERVE/PAPER_AUTO) and independent pause/review flags

Intent transitions (anything else is a bug and raises):
  PENDING           -> SUBMITTING | MISSED_EXECUTION | SKIPPED | REVIEW_REQUIRED | SUPERSEDED
  SUBMITTING        -> SUBMITTED | AMBIGUOUS | REJECTED | PENDING(definitely-not-sent only)
  AMBIGUOUS         -> SUBMITTED | PARTIALLY_FILLED | FILLED | TERMINAL_PARTIAL | CANCELLED_ZERO_FILL | REJECTED | SUPERSEDED(owner-confirmed absent)
  SUBMITTED         -> PARTIALLY_FILLED | FILLED | CANCEL_REQUESTED | TERMINAL_PARTIAL | CANCELLED_ZERO_FILL | REJECTED
  PARTIALLY_FILLED  -> FILLED | CANCEL_REQUESTED | TERMINAL_PARTIAL
  CANCEL_REQUESTED  -> FILLED | TERMINAL_PARTIAL | CANCELLED_ZERO_FILL | PARTIALLY_FILLED
  REJECTED / MISSED_EXECUTION / CANCELLED_ZERO_FILL(unexpected) / REVIEW_REQUIRED -> SUPERSEDED (owner review only)
Terminal: FILLED, TERMINAL_PARTIAL, SKIPPED, SUPERSEDED, and the review states until acknowledged.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orbit.broker.base import (
    AccountInfo, BrokerAdapter, BrokerAmbiguousError, BrokerError, BrokerRejectedError, BrokerUnavailableError,
    ExecutionGuard, GuardViolation, OrderInfo, OrderRequest, make_client_order_id,
)
from orbit.config import Settings, redact
from orbit.data.bars import BarSeries, validate_bars
from orbit.db import (
    OPEN_INTENT_STATES, AuditLog, BrokerSnapshot, ControlState, Decision, EquityMark, Fill, Job, OrderEvent,
    OrderIntent, ReconciliationReport, StrategyPosition, StrategyVersion,
)
from orbit.market_calendar import UTC, MarketCalendar
from orbit.money import D, floor_qty, money_pair
from orbit.notify import emit
from orbit.risk import DrawdownState, RiskPolicy, update_drawdown
from orbit.strategy import CASH, INSUFFICIENT, INVESTED, STRATEGY_NAME, StrategyParams, entry_notional, evaluate_signal

log = logging.getLogger("orbit.engine")

TRANSITIONS = {
    "PENDING": {"SUBMITTING", "MISSED_EXECUTION", "SKIPPED", "REVIEW_REQUIRED", "SUPERSEDED"},
    "SUBMITTING": {"SUBMITTED", "AMBIGUOUS", "REJECTED", "PENDING", "PARTIALLY_FILLED", "FILLED", "TERMINAL_PARTIAL", "CANCELLED_ZERO_FILL"},
    "AMBIGUOUS": {"SUBMITTED", "PARTIALLY_FILLED", "FILLED", "TERMINAL_PARTIAL", "CANCELLED_ZERO_FILL", "REJECTED", "SUPERSEDED", "CANCEL_REQUESTED"},
    "SUBMITTED": {"PARTIALLY_FILLED", "FILLED", "CANCEL_REQUESTED", "TERMINAL_PARTIAL", "CANCELLED_ZERO_FILL", "REJECTED"},
    "PARTIALLY_FILLED": {"FILLED", "CANCEL_REQUESTED", "TERMINAL_PARTIAL", "PARTIALLY_FILLED"},
    "CANCEL_REQUESTED": {"FILLED", "TERMINAL_PARTIAL", "CANCELLED_ZERO_FILL", "PARTIALLY_FILLED"},
    "REJECTED": {"SUPERSEDED"},
    "MISSED_EXECUTION": {"SUPERSEDED"},
    "CANCELLED_ZERO_FILL": {"SUPERSEDED"},
    "REVIEW_REQUIRED": {"SUPERSEDED", "PENDING"},
    "FILLED": set(), "TERMINAL_PARTIAL": set(), "SKIPPED": set(), "SUPERSEDED": set(),
}
REVIEW_STATES = {"REJECTED", "MISSED_EXECUTION", "REVIEW_REQUIRED", "AMBIGUOUS"}
DRIFT_EPS = Decimal("0.000000001")
RECON_FRESH_SECONDS = 120
INVALIDATING_CASH_TYPES = {"CSD", "CSW", "JNLC", "JNLS", "TRANS"}


class TransitionError(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers
def audit(s: Session, actor: str, action: str, details: dict, severity: str = "info") -> None:
    s.add(AuditLog(actor=actor, action=action, severity=severity, details=json.loads(redact(json.dumps(details, default=str)))))


def control(s: Session) -> ControlState:
    c = s.get(ControlState, 1)
    if c is None:
        c = ControlState(id=1, mode="OBSERVE")
        s.add(c)
        s.flush()
    return c


def active_version(s: Session) -> StrategyVersion:
    v = s.scalar(select(StrategyVersion).where(StrategyVersion.active.is_(True)).order_by(StrategyVersion.id.desc()))
    if v is None:
        v = create_version(s, StrategyParams(), RiskPolicy(), "initial default (experimental, unvalidated)", actor="system")
    return v


def version_params(v: StrategyVersion) -> tuple[StrategyParams, RiskPolicy]:
    rp = v.risk_policy
    return StrategyParams.from_dict(v.params), RiskPolicy(D(rp["warn_fraction"]), D(rp["pause_fraction"]), rp.get("version_label", "drawdown_review_v1"))


def create_version(s: Session, params: StrategyParams, risk: RiskPolicy, reason: str, actor: str = "user",
                   extra_risk: dict | None = None) -> StrategyVersion:
    params.validate()
    risk.validate()
    rp = {**risk.to_dict(), **(extra_risk or {})}
    content = json.dumps({"p": params.to_dict(), "r": rp}, sort_keys=True)
    for old in s.scalars(select(StrategyVersion).where(StrategyVersion.active.is_(True))):
        old.active = False
    v = StrategyVersion(name=STRATEGY_NAME, params=params.to_dict(), risk_policy=rp, reason=reason[:500], active=True,
                        content_hash=hashlib.sha256(content.encode()).hexdigest()[:16])
    s.add(v)
    s.flush()
    audit(s, actor, "strategy_version_created", {"version_id": v.id, "params": params.to_dict(), "risk": rp, "reason": reason})
    # Pending intents of older versions are paused for review instead of silently executing.
    for it in s.scalars(select(OrderIntent).where(OrderIntent.state == "PENDING", OrderIntent.strategy_version_id != v.id,
                                                  OrderIntent.kind != "CLOSE_MANUAL")):
        transition(s, it, "REVIEW_REQUIRED", f"Settings changed to version {v.id} after this intent was created; review before it can execute.")
        it.review_required = True
        c = control(s)
        c.review_required = True
        c.review_reason = "Pending intent from an older strategy version needs review."
    return v


def position(s: Session, account_key: str, symbol: str = "SPY") -> StrategyPosition:
    p = s.scalar(select(StrategyPosition).where(StrategyPosition.account_key == account_key, StrategyPosition.symbol == symbol))
    if p is None:
        p = StrategyPosition(account_key=account_key, symbol=symbol, signal_target=CASH, held_qty=Decimal(0), cost_basis=Decimal(0))
        s.add(p)
        s.flush()
    return p


def transition(s: Session, it: OrderIntent, to: str, reason: str = "", detail: dict | None = None, now: datetime | None = None) -> None:
    frm = it.state
    if to == frm and to != "PARTIALLY_FILLED":
        return
    if to not in TRANSITIONS.get(frm, set()):
        raise TransitionError(f"illegal intent transition {frm} -> {to} (intent {it.id})")
    it.state = to
    if reason:
        it.reason = redact(reason)[:1000]
    if to in {"FILLED", "TERMINAL_PARTIAL", "CANCELLED_ZERO_FILL", "REJECTED", "SKIPPED", "SUPERSEDED", "MISSED_EXECUTION"}:
        it.terminal_at = now or datetime.now(tz=UTC)
    s.flush()
    key = f"intent:{it.id}:{frm}->{to}:{it.filled_qty}"
    try:
        with s.begin_nested():
            s.add(OrderEvent(intent_id=it.id, event_key=key[:160], kind="STATE", from_state=frm, to_state=to,
                             detail={"reason": redact(reason)[:500], **(detail or {})}, at=now or datetime.now(tz=UTC)))
            s.flush()
    except IntegrityError:
        pass


def open_intents(s: Session, account_key: str, symbol: str = "SPY") -> list[OrderIntent]:
    return list(s.scalars(select(OrderIntent).where(OrderIntent.account_key == account_key, OrderIntent.symbol == symbol,
                                                    OrderIntent.state.in_(OPEN_INTENT_STATES)).order_by(OrderIntent.id)))


def next_eligible_session(cal: MarketCalendar, after_day: date, now: datetime, window_minutes: int):
    """First session strictly after ``after_day`` whose submission window has not already ended."""
    s = cal.next_session(after_day)
    while s is not None and s.open_utc + timedelta(minutes=window_minutes) <= now:
        s = cal.next_session(s.day)
    return s


@dataclass
class TickContext:
    now: datetime
    account: AccountInfo | None = None
    broker_positions: dict | None = None
    broker_open_orders: list | None = None
    reconciled_ok: bool = False
    reconciled_at: datetime | None = None
    clock_open: bool | None = None


class Engine:
    """All forward-operation logic. The worker calls ``tick``; tests call the same entry point."""

    def __init__(self, db, broker: BrokerAdapter | None, calendar: MarketCalendar, data_provider, settings: Settings,
                 symbol: str = "SPY"):
        self.db = db
        self.broker = broker
        self.cal = calendar
        self.data = data_provider
        self.settings = settings
        self.symbol = symbol

    # ============================================================ tick
    def tick(self, now: datetime) -> TickContext:
        ctx = TickContext(now=now)
        if self.broker is None:
            return ctx
        self.reconcile(ctx)
        self.owner_jobs(ctx)
        self.mark_equity(ctx)
        self.evaluate(ctx)
        self.reassess(ctx)
        self.execute(ctx)
        return ctx

    @property
    def account_key(self) -> str:
        with self.db.session() as s:
            return control(s).account_fingerprint or "unconnected"

    # ============================================================ reconciliation
    def reconcile(self, ctx: TickContext) -> ReconciliationReport | None:
        now = ctx.now
        try:
            # Orders first, then positions, then orders again: a fill landing in between is visible as a
            # change in the second order read and the drift check is deferred rather than mis-flagged.
            orders_a = self.broker.get_recent_orders(after=now - timedelta(days=30))
            acct = self.broker.get_account()
            positions = self.broker.get_positions()
            orders_b = self.broker.get_recent_orders(after=now - timedelta(days=30))
            clock = self.broker.get_clock()
            try:
                cash_acts = self.broker.get_cash_activities(after=(now - timedelta(days=30)).date())
                cash_err = None
            except BrokerError as e:
                cash_acts, cash_err = [], redact(e)[:200]
        except BrokerError as exc:
            with self.db.session() as s:
                s.add(BrokerSnapshot(taken_at=now, ok=False, error=redact(str(exc))[:500]))
                emit(s, "reconciliation_failed", "warning", "Broker unreachable; reconciliation failed",
                     f"Could not read broker state: {exc}. No new orders will be submitted until a reconciliation succeeds.",
                     f"recon_fail:{now:%Y%m%d%H}", settings=self.settings)
            return None
        ctx.account = acct
        ctx.clock_open = clock.is_open
        stable = [(o.id, o.status, o.filled_qty) for o in orders_a] == [(o.id, o.status, o.filled_qty) for o in orders_b]
        by_client = {o.client_order_id: o for o in orders_b}
        pos_map = {p.symbol: p for p in positions}
        ctx.broker_positions = pos_map
        ctx.broker_open_orders = [o for o in orders_b if not o.is_terminal]
        with self.db.session() as s:
            c = control(s)
            snap = BrokerSnapshot(taken_at=now, ok=True, account=acct.to_public(),
                                  positions=[p.to_public() for p in positions],
                                  open_orders=[o.to_public() for o in ctx.broker_open_orders],
                                  clock={"is_open": clock.is_open, "next_open": clock.next_open.isoformat(), "next_close": clock.next_close.isoformat(), "timestamp": clock.timestamp.isoformat()})
            s.add(snap)
            s.flush()
            discrepancies: list[dict] = []
            if not c.account_fingerprint:
                c.account_fingerprint = acct.account_fingerprint
                audit(s, "worker", "account_bound", {"account": acct.account_number_masked})
            elif c.account_fingerprint != acct.account_fingerprint:
                discrepancies.append({"kind": "ACCOUNT_CHANGED", "detail": "Broker account differs from the bound account (reset or different keys)."})
            akey = c.account_fingerprint
            # --- update intents from broker orders
            for it in s.scalars(select(OrderIntent).where(OrderIntent.account_key == akey,
                                                          OrderIntent.state.in_(OPEN_INTENT_STATES - {"PENDING"}))):
                bo = by_client.get(it.client_order_id)
                if bo is None and it.state in ("AMBIGUOUS", "SUBMITTING"):
                    try:
                        bo = self.broker.get_order_by_client_id(it.client_order_id)
                    except BrokerError:
                        bo = None
                if bo is None:
                    if it.state == "SUBMITTING":
                        transition(s, it, "AMBIGUOUS", "Process stopped during submission or broker result unknown; order may exist. Paused for reconciliation.", now=now)
                        self._flag_review(s, it, "Order submission outcome is unknown (ambiguous).")
                    continue
                self._apply_broker_order(s, it, bo, now)
            # --- foreign orders / holdings
            ours = {it.client_order_id for it in s.scalars(select(OrderIntent).where(OrderIntent.account_key == akey))}
            for o in ctx.broker_open_orders:
                if o.client_order_id not in ours:
                    discrepancies.append({"kind": "FOREIGN_ORDER", "detail": f"Open {o.side} {o.symbol} order not created by Orbit (status {o.status})."})
            pos = position(s, akey, self.symbol)
            for sym, p in pos_map.items():
                if sym != self.symbol:
                    discrepancies.append({"kind": "FOREIGN_HOLDING", "detail": f"Holding in {sym} is not managed by this strategy."})
            broker_qty = pos_map[self.symbol].qty if self.symbol in pos_map else Decimal(0)
            if abs(broker_qty - pos.held_qty) > DRIFT_EPS:
                discrepancies.append({"kind": "HOLDING_MISMATCH", "detail": f"Broker {self.symbol} qty {broker_qty} vs strategy-confirmed qty {pos.held_qty}."})
            # --- external cash flows invalidate performance & drawdown
            seen = set((self._kv_get(s, "seen_cash_activities") or {}).get("ids", []))
            new_flows = [a for a in cash_acts if a.activity_type in INVALIDATING_CASH_TYPES and a.id not in seen]
            if new_flows and c.activated_at is not None:
                c.performance_valid = False
                c.performance_invalid_reason = f"External cash activity detected ({', '.join(sorted({a.activity_type for a in new_flows}))}); drawdown and returns cannot be attributed to trading."
                if not c.risk_paused_entries:
                    c.risk_paused_entries = True
                    c.risk_pause_reason = "Entries paused: cash flow makes drawdown calculation invalid pending reconciliation."
                emit(s, "cash_flow_detected", "warning", "Deposit/withdrawal detected", c.performance_invalid_reason,
                     f"cashflow:{','.join(a.id for a in new_flows)}"[:150], settings=self.settings)
            self._kv_set(s, "seen_cash_activities", {"ids": sorted(seen | {a.id for a in cash_acts})[-500:]})
            if cash_err and c.activated_at is not None and c.performance_valid:
                c.performance_valid = False
                c.performance_invalid_reason = f"Cash activity could not be read ({cash_err}); deposits/withdrawals cannot be excluded from performance."
            # --- drift handling: require two consecutive observations unless the snapshot was stable with no open orders
            drift_kinds = {d["kind"] for d in discrepancies}
            ok = not discrepancies
            if discrepancies:
                prev = self._kv_get(s, "drift_suspect") or {}
                confirmed = (stable and not ctx.broker_open_orders) or prev.get("kinds") == sorted(drift_kinds) or "ACCOUNT_CHANGED" in drift_kinds
                self._kv_set(s, "drift_suspect", {"kinds": sorted(drift_kinds), "at": now.isoformat()})
                if confirmed and not c.security_paused:
                    c.security_paused = True
                    c.security_pause_reason = "Unexplained broker state: " + "; ".join(d["detail"] for d in discrepancies)
                    audit(s, "worker", "security_paused", {"discrepancies": discrepancies}, "critical")
                    emit(s, "security_paused", "critical", "Paused: broker state needs reconciliation", c.security_pause_reason,
                         f"secpause:{now:%Y%m%d%H%M}", settings=self.settings)
            else:
                self._kv_set(s, "drift_suspect", {})
            rep = ReconciliationReport(at=now, ok=ok, discrepancies=discrepancies, snapshot_id=snap.id,
                                       local={"strategy_held_qty": str(pos.held_qty), "signal_target": pos.signal_target,
                                              "open_intents": [{"id": i.id, "state": i.state, "client_order_id": i.client_order_id} for i in open_intents(s, akey)]},
                                       broker={"cash": str(acct.cash), "equity": str(acct.equity), "buying_power": str(acct.buying_power),
                                               "positions": {k: str(v.qty) for k, v in pos_map.items()},
                                               "open_orders": [{"client_order_id": o.client_order_id, "status": o.status} for o in ctx.broker_open_orders],
                                               "snapshot_stable": stable})
            s.add(rep)
            ctx.reconciled_ok = ok and stable
            ctx.reconciled_at = now
            # risk pause: cancel pending strategy buys when broker is trusted
            if c.risk_paused_entries and ok:
                for it in open_intents(s, akey):
                    if it.side == "BUY":
                        self._cancel_intent(s, it, "RISK_PAUSED_ENTRIES: pending strategy buy cancelled.", now)
            self._residual_exits(s, akey, now)
            return rep

    def _kv_get(self, s, key):
        from orbit.db import KV
        r = s.get(KV, key)
        return r.value if r else None

    def _kv_set(self, s, key, value):
        from orbit.db import KV
        r = s.get(KV, key)
        if r:
            r.value = value
        else:
            s.add(KV(key=key, value=value))

    def _flag_review(self, s: Session, it: OrderIntent, why: str) -> None:
        it.review_required = True
        c = control(s)
        c.review_required = True
        c.review_reason = why
        sev = "critical"
        emit(s, "review_required" if it.state != "AMBIGUOUS" else "ambiguous_order", sev, f"Review required: {it.kind} {it.side}",
             f"{why} Intent {it.id}, client id {it.client_order_id}, state {it.state}.", f"review:{it.id}:{it.state}",
             ref=f"intent:{it.id}", settings=self.settings)

    def _apply_broker_order(self, s: Session, it: OrderIntent, bo: OrderInfo, now: datetime) -> None:
        """Apply a broker order snapshot. Idempotent; stale/out-of-order snapshots never reduce fills."""
        it.broker_order_id = bo.id
        it.broker_status = bo.status
        cum = bo.filled_qty or Decimal(0)
        if cum > it.applied_qty:
            inc = cum - it.applied_qty
            prev_total = it.applied_qty * (it.filled_avg_price or Decimal(0))
            new_total = cum * (bo.filled_avg_price or Decimal(0))
            inc_price = ((new_total - prev_total) / inc if inc > 0 else (bo.filled_avg_price or Decimal(0))).quantize(Decimal("0.000000001"))
            key = f"{bo.id}:{cum}"
            try:
                with s.begin_nested():
                    s.add(Fill(intent_id=it.id, broker_order_id=bo.id, fill_key=key, side=it.side, qty=inc,
                               price=inc_price, at=bo.updated_at or now))
                    s.flush()
                applied = True
            except IntegrityError:
                applied = False  # duplicate fill event: already recorded
            if applied:
                pos = position(s, it.account_key, it.symbol)
                if it.side == "BUY":
                    pos.held_qty += inc
                else:
                    if inc > pos.held_qty + DRIFT_EPS:
                        raise TransitionError("broker reports selling more than the strategy holds")
                    pos.held_qty -= inc
                    if pos.held_qty <= DRIFT_EPS:
                        pos.held_qty = Decimal(0)
                s.flush()
                led = strategy_ledger(s, it.account_key, it.symbol)
                pos.cost_basis = led.cost_basis
                it.applied_qty = cum
                it.filled_qty = cum
                it.filled_avg_price = bo.filled_avg_price
                emit(s, "order_filled", "info", f"Fill confirmed: {it.side} {self.symbol}",
                     f"{it.side} {inc} {self.symbol} at {inc_price:.4f} USD (cumulative {cum}). Broker status {bo.status}.",
                     f"fill:{key}", ref=f"intent:{it.id}", settings=self.settings)
        st = bo.status
        if st == "filled":
            if it.state != "FILLED":
                transition(s, it, "FILLED", "Broker confirmed full fill.", {"broker_status": st}, now)
                if it.review_required:
                    it.review_required = False
                    self._refresh_review_flag(s)
        elif st in ("canceled", "expired", "rejected", "replaced"):
            if it.applied_qty > 0:
                if it.state != "TERMINAL_PARTIAL":
                    transition(s, it, "TERMINAL_PARTIAL", f"Order {st} after partial fill of {it.applied_qty}.", {"broker_status": st}, now)
                    pos = position(s, it.account_key, it.symbol)
                    if it.side == "BUY":
                        pos.exposure_note = (f"Entry partially filled: {it.applied_qty} shares instead of target notional "
                                             f"{it.frozen_notional} USD. Holding only the filled quantity; no automatic top-up.")
            elif st == "rejected":
                transition(s, it, "REJECTED", f"Broker rejected the order: {bo.raw_reason or 'see broker'}.", {"broker_status": st}, now)
                self._flag_review(s, it, "Order was rejected by the broker.")
            else:
                expected = it.state == "CANCEL_REQUESTED"
                transition(s, it, "CANCELLED_ZERO_FILL", f"Order {st} with no fill.", {"broker_status": st}, now)
                if not expected:
                    self._flag_review(s, it, f"Order {st} without any fill.")
        elif st == "partially_filled":
            if it.state in ("SUBMITTED", "AMBIGUOUS", "PARTIALLY_FILLED", "SUBMITTING"):
                if it.state == "SUBMITTING":
                    transition(s, it, "SUBMITTED", "Broker acknowledged.", now=now)
                transition(s, it, "PARTIALLY_FILLED", "Partial fill; order still working.", now=now)
        else:  # open statuses
            if it.state in ("SUBMITTING", "AMBIGUOUS"):
                transition(s, it, "SUBMITTED", f"Broker shows order {st} (matched by client order id).", now=now)
                if it.review_required:
                    it.review_required = False  # ambiguity resolved by broker evidence
                    self._refresh_review_flag(s)

    def _cancel_intent(self, s: Session, it: OrderIntent, reason: str, now: datetime) -> str:
        if it.state == "PENDING":
            transition(s, it, "SKIPPED", reason, now=now)
            return "skipped"
        if it.state in ("SUBMITTED", "PARTIALLY_FILLED") and it.broker_order_id:
            try:
                self.broker.cancel_order(it.broker_order_id)
                transition(s, it, "CANCEL_REQUESTED", reason + " Cancellation requested; not confirmed until broker reports terminal status.", now=now)
                return "cancel_requested"
            except BrokerRejectedError as e:
                return f"cancel rejected: {e.reason}"
            except BrokerError as e:
                return f"cancel uncertain: {e}"
        return f"no action for state {it.state}"

    def _residual_exits(self, s: Session, akey: str, now: datetime) -> None:
        pos = position(s, akey, self.symbol)
        if pos.held_qty <= 0 or pos.signal_target != CASH or open_intents(s, akey):
            return
        last_exit = s.scalar(select(OrderIntent).where(OrderIntent.account_key == akey, OrderIntent.side == "SELL")
                             .order_by(OrderIntent.id.desc()))
        if last_exit is None or last_exit.state != "TERMINAL_PARTIAL":
            return
        if s.scalar(select(OrderIntent.id).where(OrderIntent.parent_intent_id == last_exit.id)):
            return
        v = active_version(s)
        p, _ = version_params(v)
        sess = next_eligible_session(self.cal, self._yesterday_ny(now), now, p.execution_window_minutes)
        if sess is None:
            return
        attempt = last_exit.attempt + 1
        it = OrderIntent(decision_id=last_exit.decision_id, parent_intent_id=last_exit.id, account_key=akey, symbol=self.symbol,
                         strategy_version_id=v.id, kind="RESIDUAL_EXIT", side="SELL", signal_session=last_exit.signal_session,
                         execution_session=sess.day, attempt=attempt, target_qty=pos.held_qty,
                         client_order_id=make_client_order_id(akey, v.id, self.symbol, last_exit.signal_session, "RESIDUAL_EXIT", attempt),
                         created_at=now, reason=f"Residual exit of {pos.held_qty} after partially filled exit #{last_exit.id}; signal still CASH.")
        s.add(it)
        s.flush()
        transition_note(s, it, "Residual-exit intent created.")
        emit(s, "intent_created", "info", "Residual exit intent created", it.reason, f"intent:{it.client_order_id}", ref=f"intent:{it.id}", settings=self.settings)

    def _yesterday_ny(self, now: datetime) -> date:
        """Day before today in New York, so today's session counts if its window is still open."""
        from orbit.market_calendar import NEW_YORK
        return now.astimezone(NEW_YORK).date() - timedelta(days=1)

    def _refresh_review_flag(self, s: Session) -> None:
        c = control(s)
        still = s.scalar(select(OrderIntent.id).where(OrderIntent.review_required.is_(True)))
        if not still:
            c.review_required = False
            c.review_reason = ""

    def owner_jobs(self, ctx: TickContext) -> None:
        """Broker writes requested from the UI are executed here, by the single worker."""
        with self.db.session() as s:
            jobs = list(s.scalars(select(Job).where(Job.job_type == "owner_cancel", Job.status == "QUEUED")))
            for j in jobs:
                c = control(s)
                res = [{"intent_id": it.id, "result": self._cancel_intent(s, it, "Cancelled by owner.", ctx.now)}
                       for it in open_intents(s, c.account_fingerprint)] if (c.account_fingerprint and ctx.account) else []
                j.status = "DONE" if ctx.account else "FAILED"
                j.error = "" if ctx.account else "broker unreachable; cancellation not attempted"
                j.result, j.finished_at = {"results": res}, ctx.now
                audit(s, "worker", "owner_cancel_executed", {"results": res})

    # ============================================================ equity marks / drawdown
    def mark_equity(self, ctx: TickContext) -> None:
        if ctx.account is None:
            return
        with self.db.session() as s:
            v = active_version(s)
            p, risk = version_params(v)
            sess = self.cal.latest_completed_session(ctx.now, p.completion_buffer_minutes)
            c = control(s)
            if sess is None or not c.account_fingerprint or c.activated_at is None:
                return
            if s.scalar(select(EquityMark.id).where(EquityMark.account_key == c.account_fingerprint, EquityMark.session == sess.day)):
                return
            # Only mark if we are still before the next session open (close-to-close semantics).
            nxt = self.cal.next_session(sess.day)
            if nxt and ctx.now >= nxt.open_utc:
                return
            marks = list(s.scalars(select(EquityMark).where(EquityMark.account_key == c.account_fingerprint).order_by(EquityMark.session)))
            # Rebuild the overlay state by replaying stored marks (same function as the backtester).
            st = DrawdownState()
            for m in marks:
                update_drawdown(st, m.equity, risk)
            st.entries_paused = c.risk_paused_entries
            events = update_drawdown(st, ctx.account.equity, risk)
            s.add(EquityMark(account_key=c.account_fingerprint, session=sess.day, equity=ctx.account.equity, cash=ctx.account.cash,
                             high_water_mark=st.high_water_mark, drawdown=st.drawdown, at=ctx.now))
            if not c.performance_valid:
                return
            if "WARN" in events:
                emit(s, "risk_warning", "warning", "Drawdown review warning",
                     f"Account equity is {st.drawdown:.2%} below its high-water mark ({money_pair(st.high_water_mark)['usd']} USD). Review recommended.",
                     f"riskwarn:{sess.day}", settings=self.settings)
            if c.risk_override_reason and st.drawdown < risk.pause_fraction:
                c.risk_override_reason = ""  # recovered below the threshold: override no longer needed
            if st.drawdown >= risk.pause_fraction and c.risk_override_reason:
                pass  # owner resumed with a recorded, versioned override; do not re-latch every close
            elif "PAUSE" in events and not c.risk_paused_entries:
                c.risk_paused_entries = True
                c.risk_pause_reason = f"Drawdown {st.drawdown:.2%} reached the {risk.pause_fraction:.0%} review threshold on {sess.day}."
                audit(s, "worker", "risk_paused_entries", {"drawdown": str(st.drawdown), "hwm": str(st.high_water_mark)}, "warning")
                emit(s, "risk_paused", "critical", "New entries paused (drawdown review)", c.risk_pause_reason,
                     f"riskpause:{sess.day}", settings=self.settings)

    # ============================================================ evaluation
    def evaluate(self, ctx: TickContext) -> Decision | None:
        with self.db.session() as s:
            v = active_version(s)
            p, _ = version_params(v)
            akey = control(s).account_fingerprint
            if not akey:
                return None
            sess = self.cal.latest_completed_session(ctx.now, p.completion_buffer_minutes)
            if sess is None:
                return None
            nxt_open = self.cal.next_session(sess.day)
            if (control(s).mode == "PAPER_AUTO" and not ctx.reconciled_ok and nxt_open
                    and ctx.now < nxt_open.open_utc - timedelta(minutes=10)):
                return None  # broker state not trusted this cycle: retry next cycle instead of a final SKIP
            if s.scalar(select(Decision.id).where(Decision.account_key == akey, Decision.symbol == self.symbol, Decision.session == sess.day)):
                return None
            # durable job claim: unique (type,key) prevents a second worker/tick from evaluating the same session
            job = Job(job_type="evaluate", job_key=f"{akey}:{sess.day}:try{ctx.now:%Y%m%d%H}", status="RUNNING", started_at=ctx.now,
                      payload={"session": sess.day.isoformat()})
            try:
                with s.begin_nested():
                    s.add(job)
                    s.flush()
            except IntegrityError:
                return None
        series, data_err = self._fetch(sess.day, ctx.now, p)
        if series is not None:
            self._store_dataset(series)
        with self.db.session() as s:
            job = s.scalar(select(Job).where(Job.job_type == "evaluate", Job.job_key == f"{akey}:{sess.day}:try{ctx.now:%Y%m%d%H}"))
            nxt = self.cal.next_session(sess.day)
            if data_err and nxt and ctx.now < nxt.open_utc - timedelta(minutes=10):
                # Data not ready yet: wait (no decision yet), retry next hour's job slot.
                job.status, job.error, job.finished_at = "FAILED", data_err, ctx.now
                emit(s, "stale_data", "warning", "Waiting for completed market data", f"Session {sess.day}: {data_err}",
                     f"stale:{sess.day}", settings=self.settings)
                return None
            d = self._decide(s, ctx, sess.day, series, data_err, v, p)
            job.status, job.finished_at, job.result = "DONE", ctx.now, {"decision_id": d.id, "action": d.action}
            return d

    def _store_dataset(self, series: BarSeries) -> None:
        from orbit.data.bars import bars_to_json
        from orbit.db import Dataset
        with self.db.session() as s:
            fp = series.fingerprint()
            if not s.scalar(select(Dataset.id).where(Dataset.fingerprint == fp)):
                s.add(Dataset(mode=series.mode, symbol=series.symbol, source=series.source, price_basis=series.price_basis,
                              fingerprint=fp, bars_json=bars_to_json(series.bars), describe=series.describe(),
                              quality=series.metadata.get("quality", {})))

    def _fetch(self, session_day: date, now: datetime, p: StrategyParams) -> tuple[BarSeries | None, str | None]:
        start = session_day - timedelta(days=int(p.sma_window * 1.6) + 30)
        try:
            series = self.data.fetch(self.symbol, start, session_day, now, self.cal, p.completion_buffer_minutes)
        except BrokerError as e:
            return None, f"market data request failed: {redact(e)}"
        except Exception as e:  # noqa: BLE001
            return None, f"market data error: {redact(e)[:200]}"
        if not series.bars:
            return None, "no bars returned (check data entitlement/feed)"
        if series.bars[-1].day != session_day:
            return None, f"latest completed bar is {series.bars[-1].day}, need {session_day} (provider not updated yet)"
        try:
            rep = validate_bars(series.bars, self.cal)
        except ValueError as e:
            return None, f"data quality: {e}"
        window = self.cal.sessions_between(series.bars[0].day, session_day)[-p.sma_window:]
        have = {b.day for b in series.bars}
        missing = [w.day.isoformat() for w in window if w.day not in have]
        if missing:
            return None, f"{len(missing)} session(s) missing inside the 200-session window (e.g. {missing[0]}); refusing to compute on gapped data"
        if len(series.bars) < p.sma_window:
            return None, f"only {len(series.bars)} completed sessions available; need {p.sma_window}"
        series.metadata["quality"] = rep
        return series, None

    def _decide(self, s: Session, ctx: TickContext, day: date, series: BarSeries | None, data_err: str | None,
                v: StrategyVersion, p: StrategyParams, sequence: int = 1) -> Decision:
        c = control(s)
        akey = c.account_fingerprint
        pos = position(s, akey, self.symbol)
        base = dict(account_key=akey, symbol=self.symbol, session=day, sequence=sequence, strategy_version_id=v.id,
                    mode_at_decision=c.mode, previous_target=pos.signal_target, held_qty_at_decision=pos.held_qty, known_at=ctx.now)
        if series is None:
            d = Decision(**base, data_mode="ALPACA", data_source="unavailable", target=INSUFFICIENT, action="SKIP",
                         reason=f"No decision possible: {data_err}. Missing data is never substituted.", inputs={"error": data_err})
            s.add(d)
            s.flush()
            emit(s, "stale_data", "warning", "Decision skipped: data unavailable", d.reason, f"skipdata:{day}", ref=f"decision:{d.id}", settings=self.settings)
            return d
        closes = [(b.day, b.close) for b in series.bars]
        sig = evaluate_signal(closes, day, pos.signal_target, p.sma_window)
        inputs = {"signal": sig.to_dict(include_inputs=True), "data": series.describe(), "params": p.to_dict()}
        action, reason, intent_plan = self._plan(s, ctx, c, pos, sig, p, inputs)
        d = Decision(**base, data_mode=series.mode, data_source=series.source, data_fingerprint=series.fingerprint(),
                     data_fetched_at=ctx.now, close=sig.close, sma=sig.sma, window_count=sig.window_count, comparison=sig.comparison,
                     target=sig.target, action=action, reason=reason, inputs=inputs)
        s.add(d)
        s.flush()
        if sig.target in (INVESTED, CASH):
            pos.signal_target = sig.target
        if intent_plan:
            it = OrderIntent(decision_id=d.id, account_key=akey, symbol=self.symbol, strategy_version_id=v.id, signal_session=day,
                             created_at=ctx.now, **intent_plan)
            s.add(it)
            s.flush()
            transition_note(s, it, "Intent persisted before any broker contact.")
            emit(s, "intent_created", "info", f"New order intent: {it.side} {self.symbol}",
                 f"{reason} Execution session {it.execution_session}.", f"intent:{it.client_order_id}", ref=f"intent:{it.id}", settings=self.settings)
        elif action in ("HOLD", "SKIP"):
            emit(s, "decision_routine", "routine", f"{action} on {day}", reason, f"decision:{akey}:{day}:{sequence}", ref=f"decision:{d.id}", settings=self.settings)
            from orbit.notify import queue_daily_summary
            queue_daily_summary(s, day, [f"{action}: target {sig.target}, mode {c.mode}"], self.settings)
        return d

    def _plan(self, s, ctx, c, pos, sig, p: StrategyParams, inputs: dict):
        """Return (action, reason, intent_kwargs|None) using only stored/observed inputs."""
        if sig.target == INSUFFICIENT:
            return "SKIP", "Fewer than 200 completed closes; no signal.", None
        held = pos.held_qty
        want_buy = sig.target == INVESTED and held <= 0
        want_sell = sig.target == CASH and held > 0
        cmp_txt = {"ABOVE": "above", "BELOW": "below", "EQUAL": "exactly equal to"}[sig.comparison]
        core = f"Close {sig.close} USD is {cmp_txt} the 200-session average {sig.sma:.4f} USD; target {sig.target}."
        # A working order that opposes the new target must be cancelled and resolved before anything else.
        oi = open_intents(s, c.account_fingerprint, self.symbol) if c.account_fingerprint else []
        conflicting = [i for i in oi if i.kind != "CLOSE_MANUAL" and
                       ((i.side == "BUY" and sig.target == CASH) or (i.side == "SELL" and sig.target == INVESTED))]
        if conflicting:
            results = [self._cancel_intent(s, i, "Signal changed; cancelling conflicting order before reassessment.", ctx.now) for i in conflicting]
            inputs["awaiting_conflict_resolution"] = [i.id for i in conflicting]
            inputs["cancel_results"] = results
            return "SKIP", (f"{core} A conflicting order is unresolved ({', '.join(results)}). The signal will be reassessed only after "
                            "the broker confirms a terminal status and fills are reconciled."), None
        if not (want_buy or want_sell):
            why = "Already invested: hold without resizing." if held > 0 else "Already in cash."
            if sig.comparison == "EQUAL":
                why = "Equality keeps the previous state. " + why
            return "HOLD", f"{core} {why}", None
        would = "BUY" if want_buy else "SELL"
        mode_ok = inputs["data"]["mode"] == "ALPACA" or (inputs["data"]["mode"] == "MOCK" and getattr(self.broker, "name", "") == "mock")
        if not mode_ok:
            inputs["would_action"] = would
            return "SKIP", f"{core} Data mode {inputs['data']['mode']} can never create orders (only fetched broker data can).", None
        if c.mode != "PAPER_AUTO":
            inputs["would_action"] = would
            return "SKIP", f"{core} OBSERVE mode: the rule would {would} but no order intent is created.", None
        blockers = []
        if c.security_paused:
            blockers.append(f"SECURITY_PAUSED ({c.security_pause_reason})")
        if c.new_orders_paused:
            blockers.append("new orders paused by owner")
        if c.review_required:
            blockers.append(f"review required ({c.review_reason})")
        if want_buy and c.risk_paused_entries:
            blockers.append(f"RISK_PAUSED_ENTRIES ({c.risk_pause_reason})")
        if not ctx.reconciled_ok:
            blockers.append("latest reconciliation not clean")
        if oi:
            return "SKIP", f"{core} An order for this direction is already working or pending; no duplicate.", None
        if blockers:
            return "SKIP", f"{core} Would {would}, blocked: {'; '.join(blockers)}.", None
        sess = next_eligible_session(self.cal, sig.session, ctx.now, p.execution_window_minutes)
        if sess is None:
            return "SKIP", f"{core} No upcoming session in the calendar.", None
        akey = c.account_fingerprint
        v = active_version(s)
        if want_buy:
            committed = sum((i.frozen_notional or Decimal(0)) for i in oi if i.side == "BUY")
            size = entry_notional(ctx.account.equity, ctx.account.cash - committed, p)
            inputs["sizing"] = {k: (str(x) if isinstance(x, Decimal) else x) for k, x in size.items()}
            inputs["sizing_inputs"] = {"equity": str(ctx.account.equity), "cash": str(ctx.account.cash), "committed": str(committed),
                                       "buying_power_ignored": str(ctx.account.buying_power)}
            if size["too_small"]:
                return "SKIP", f"{core} Entry size {size['target']:.2f} USD is below the minimum order {p.min_order_notional_usd} USD after limits and reserve.", None
            return "BUY", (f"{core} Flat, so enter at the next session open ({sess.day}). Target {size['target']:.2f} USD = "
                           f"min(25% equity {size['by_fraction']:.2f}, cap {size['by_cap']:.2f}, cash after reserve {size['by_cash']:.2f}); limited by {size['limited_by']}."), dict(
                kind="ENTRY", side="BUY", execution_session=sess.day, attempt=1, frozen_notional=size["target"], sizing=inputs["sizing"],
                client_order_id=make_client_order_id(akey, v.id, self.symbol, sig.session, "ENTRY", 1))
        return "SELL", f"{core} Invested, so exit the strategy holding of {held} at the next session open ({sess.day}).", dict(
            kind="EXIT", side="SELL", execution_session=sess.day, attempt=1, target_qty=held,
            client_order_id=make_client_order_id(akey, v.id, self.symbol, sig.session, "EXIT", 1))

    def reassess(self, ctx: TickContext) -> None:
        """After a conflicting order resolves, re-run the plan for that session as a new sequence."""
        with self.db.session() as s:
            c = control(s)
            if not c.account_fingerprint:
                return
            d = s.scalar(select(Decision).where(Decision.account_key == c.account_fingerprint).order_by(Decision.session.desc(), Decision.sequence.desc()))
            if not d or not d.inputs.get("awaiting_conflict_resolution") or d.inputs.get("reassessed"):
                return
            ids = d.inputs["awaiting_conflict_resolution"]
            if any(s.get(OrderIntent, i).state in OPEN_INTENT_STATES for i in ids):
                return
            v = s.get(StrategyVersion, d.strategy_version_id)
            p, _ = version_params(active_version(s))
        series, err = self._fetch(d.session, ctx.now, p)
        with self.db.session() as s:
            d = s.get(Decision, d.id)
            d.inputs = {**d.inputs, "reassessed": True}
            self._decide(s, ctx, d.session, series, err, active_version(s), p, sequence=d.sequence + 1)

    # ============================================================ execution
    def execute(self, ctx: TickContext) -> None:
        with self.db.session() as s:
            c = control(s)
            pending = [i.id for i in open_intents(s, c.account_fingerprint) if i.state == "PENDING"] if c.account_fingerprint else []
        for iid in pending:
            self._execute_one(ctx, iid)

    def _execute_one(self, ctx: TickContext, intent_id: int) -> None:
        now = ctx.now
        with self.db.session() as s:
            it = s.get(OrderIntent, intent_id)
            c = control(s)
            v = active_version(s)
            p, _ = version_params(s.get(StrategyVersion, it.strategy_version_id))
            sess = self.cal.session(it.execution_session)
            if sess is None:
                transition(s, it, "REVIEW_REQUIRED", "Execution date is not an exchange session.", now=now)
                self._flag_review(s, it, "Execution date invalid.")
                return
            window_start = sess.open_utc
            if it.kind == "CLOSE_MANUAL":
                window_start = max(sess.open_utc, it.created_at)
            window_end = window_start + timedelta(minutes=p.execution_window_minutes)
            if now < sess.open_utc:
                return
            if now >= window_end:
                transition(s, it, "MISSED_EXECUTION", f"Submission window {sess.open_utc:%H:%M}-{window_end:%H:%M} UTC passed without submission (outage, sleep or block). No catch-up order.", now=now)
                self._flag_review(s, it, "Missed execution window.")
                emit(s, "missed_execution", "critical", "Missed execution window", it.reason, f"missed:{it.id}", ref=f"intent:{it.id}", settings=self.settings)
                return
            if it.strategy_version_id != v.id and it.kind != "CLOSE_MANUAL":
                transition(s, it, "REVIEW_REQUIRED", "Strategy settings changed; review this intent.", now=now)
                self._flag_review(s, it, "Settings changed after intent creation.")
                return
            blockers = []
            manual = it.kind == "CLOSE_MANUAL"  # explicit authenticated owner action
            if c.mode != "PAPER_AUTO" and not manual:
                blockers.append("mode is OBSERVE")
            if c.security_paused and not manual:
                blockers.append("SECURITY_PAUSED")
            if c.new_orders_paused and not manual:
                blockers.append("new orders paused")
            if it.side == "BUY" and c.risk_paused_entries:
                transition(s, it, "SKIPPED", "RISK_PAUSED_ENTRIES: entry cancelled before submission.", now=now)
                return
            if ctx.account is None or ctx.reconciled_at != now:
                blockers.append("no successful broker reconciliation this cycle")
            elif not ctx.reconciled_ok and not manual:
                blockers.append("latest reconciliation not clean")
            if ctx.clock_open is not True:
                blockers.append("broker clock does not confirm the market is open")
            if blockers:
                self._note(s, it, f"Waiting: {', '.join(blockers)}", now)
                return
            try:
                asset = self.broker.get_asset(self.symbol)
            except BrokerError as e:
                self._note(s, it, f"Waiting: asset check failed ({e})", now)
                return
            if not asset.tradable or not asset.fractionable or asset.status != "active":
                transition(s, it, "REVIEW_REQUIRED", f"Asset not eligible (tradable={asset.tradable}, fractionable={asset.fractionable}, status={asset.status}).", now=now)
                self._flag_review(s, it, "Asset eligibility check failed.")
                return
            acct = ctx.account
            pos = position(s, it.account_key, self.symbol)
            broker_qty = ctx.broker_positions[self.symbol].qty if self.symbol in ctx.broker_positions else Decimal(0)
            if it.side == "BUY":
                if pos.held_qty > 0 or broker_qty > 0:
                    transition(s, it, "SKIPPED", "Not flat at execution time; entries only from a reconciled flat position.", now=now)
                    return
                available = acct.cash - p.cash_reserve_usd
                notional = min(it.frozen_notional, available).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
                if notional < p.min_order_notional_usd:
                    transition(s, it, "SKIPPED", f"Cash after reserve ({available:.2f} USD) leaves {notional} USD, below minimum order.", now=now)
                    return
                req = OrderRequest(self.symbol, "buy", it.client_order_id, notional=notional)
                it.submitted_notional = notional
                if notional < it.frozen_notional:
                    note = f"Reduced from frozen {it.frozen_notional:.2f} to {notional} USD because available cash after reserve is {available:.2f} USD."
                    it.sizing = {**(it.sizing or {}), "reduced_at_execution": note}
                    self._note(s, it, note, now)
            else:
                qty = floor_qty(min(it.target_qty or Decimal(0), pos.held_qty, broker_qty))
                if qty <= 0:
                    transition(s, it, "SKIPPED", "Nothing to sell after reconciliation.", now=now)
                    return
                req = OrderRequest(self.symbol, "sell", it.client_order_id, qty=qty)
                it.target_qty = qty
            guard = ExecutionGuard(p.max_entry_notional_usd)
            try:
                guard.check(req, acct, broker_qty)
            except GuardViolation as e:
                transition(s, it, "REVIEW_REQUIRED", f"Execution guard blocked the order: {e}", now=now)
                self._flag_review(s, it, f"Execution guard: {e}")
                return
            transition(s, it, "SUBMITTING", "Persisted SUBMITTING before contacting the broker.", now=now)
            it.submitted_at = now
        # ---- broker contact happens outside the DB transaction (short SQLite writes)
        result, err = None, None
        try:
            result = self.broker.submit_order(req)
        except BrokerAmbiguousError as e:
            err = ("ambiguous", str(e))
        except BrokerRejectedError as e:
            err = ("rejected", e.reason)
        except BrokerUnavailableError as e:
            err = ("unavailable", str(e))
        except BrokerError as e:
            err = ("ambiguous", str(e))
        with self.db.session() as s:
            it = s.get(OrderIntent, intent_id)
            if result is not None:
                transition(s, it, "SUBMITTED", f"Broker accepted order {result.id} (accepted is not filled).", {"broker_status": result.status}, now)
                self._apply_broker_order(s, it, result, now)
                emit(s, "order_submitted", "info", f"Order submitted: {it.side} {self.symbol}",
                     f"Client id {it.client_order_id}; broker status {result.status}.", f"submitted:{it.client_order_id}", ref=f"intent:{it.id}", settings=self.settings)
                return
            kind, msg = err
            if kind == "rejected":
                transition(s, it, "REJECTED", f"Broker rejected: {redact(msg)}", now=now)
                self._flag_review(s, it, f"Order rejected: {redact(msg)[:200]}")
                emit(s, "order_rejected", "critical", "Order rejected", it.reason, f"rejected:{it.id}", ref=f"intent:{it.id}", settings=self.settings)
                return
            if kind == "unavailable":
                transition(s, it, "PENDING", f"Broker unreachable before the request was sent ({redact(msg)}); will retry within the window with the same client id.", now=now)
                return
            transition(s, it, "AMBIGUOUS", f"Submission outcome unknown ({redact(msg)}). Looking up by client order id; no new order will be created.", now=now)
        # immediate lookup by the same client id
        try:
            found = self.broker.get_order_by_client_id(req.client_order_id)
        except BrokerError:
            found = None
        with self.db.session() as s:
            it = s.get(OrderIntent, intent_id)
            if found is not None:
                self._apply_broker_order(s, it, found, now)
                emit(s, "order_submitted", "info", f"Order found after timeout: {it.side} {self.symbol}",
                     f"Matched client id {it.client_order_id}; status {found.status}.", f"submitted:{it.client_order_id}", ref=f"intent:{it.id}", settings=self.settings)
            else:
                self._flag_review(s, it, "Order outcome ambiguous: not found by client id right after a timeout, which does not prove it was not accepted.")

    def _note(self, s: Session, it: OrderIntent, msg: str, now: datetime) -> None:
        key = f"note:{it.id}:{hashlib.sha1(msg.encode()).hexdigest()[:10]}"
        try:
            with s.begin_nested():
                s.add(OrderEvent(intent_id=it.id, event_key=key, kind="NOTE", from_state=it.state, to_state=it.state, detail={"note": redact(msg)}, at=now))
                s.flush()
        except IntegrityError:
            pass


def strategy_ledger(s: Session, account_key: str, symbol: str = "SPY"):
    """Replay this strategy's confirmed fills through the FIFO Ledger (same convention as backtests).

    Alpaca paper charges no commission and does not simulate regulatory fees, so fees are 0 here.
    Cash is a large virtual amount: this ledger tracks lots and P&L, not the account's cash.
    """
    from orbit.accounting import Ledger
    led = Ledger(Decimal("1e15"))
    rows = s.scalars(select(Fill).join(OrderIntent, Fill.intent_id == OrderIntent.id)
                     .where(OrderIntent.account_key == account_key, OrderIntent.symbol == symbol).order_by(Fill.at, Fill.id))
    for f in rows:
        if f.side == "BUY":
            led.buy(f.qty, f.price, Decimal(0))
        else:
            led.sell(min(f.qty, led.qty), f.price, Decimal(0))
    return led


def transition_note(s: Session, it: OrderIntent, msg: str) -> None:
    try:
        with s.begin_nested():
            s.add(OrderEvent(intent_id=it.id, event_key=f"created:{it.id}", kind="STATE", from_state="", to_state=it.state, detail={"note": msg},
                             at=it.created_at or datetime.now(tz=UTC)))
            s.flush()
    except IntegrityError:
        pass
