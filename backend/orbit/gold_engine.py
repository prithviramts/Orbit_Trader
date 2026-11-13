"""Gold AI strategy (GLD, paper only): the learning model in orbit.gold_ai decides, this engine executes.

It reuses the SPY engine's tested order lifecycle (intent persisted before broker contact, client-order-id
idempotency, next-open window, reconciliation, review states). The SPY worker cycle reconciles the account
first and passes its context in, so there is one broker read per cycle and one owner of the schedule.

Settings live in KV "gold_settings" (owner-controlled from the Gold AI page, audited):
  enabled     False = shadow mode: the AI still predicts and is scored every day, but creates no orders.
  budget_usd  money per entry (default 100), never above the engine's per-order cap.
Global controls still apply: OBSERVE mode, pauses, review flags and risk-paused entries block gold buys too.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit import alerts, gold_ai
from orbit.broker.base import BrokerError, make_client_order_id
from orbit.config import Settings, redact
from orbit.data.bars import BarSeries, validate_bars
from orbit.db import KV, Decision, OrderIntent, StrategyVersion
from orbit.engine import Engine, TickContext, active_version, audit, control, next_eligible_session, open_intents, position, transition_note
from orbit.market_calendar import NEW_YORK, UTC
from orbit.notify import emit
from orbit.strategy import CASH, INSUFFICIENT, INVESTED, StrategyParams

log = logging.getLogger("orbit.gold")
SYMBOL = "GLD"
SETTINGS_KEY = "gold_settings"
MODEL_KEY = "gold_model_status"
DEFAULT_BUDGET = Decimal("100")
HISTORY_DAYS = 420  # calendar days fetched for live signals (>= 260 sessions)
RETRAIN_EVERY_DAYS = 28
TRAIN_START = date(2016, 1, 1)


def get_settings(s: Session) -> dict:
    row = s.get(KV, SETTINGS_KEY)
    v = dict(row.value) if row and row.value else {}
    return {"enabled": bool(v.get("enabled", False)), "budget_usd": str(v.get("budget_usd", DEFAULT_BUDGET))}


def set_settings(s: Session, enabled: bool, budget_usd: Decimal, cap: Decimal, actor: str = "user") -> dict:
    budget = Decimal(budget_usd).quantize(Decimal("0.01"))
    if budget < Decimal("1") or budget > cap:
        raise ValueError(f"gold budget must be between 1 and {cap} USD (the per-order cap)")
    value = {"enabled": bool(enabled), "budget_usd": str(budget)}
    row = s.get(KV, SETTINGS_KEY)
    before = dict(row.value) if row and row.value else {}
    if row:
        row.value = value
    else:
        s.add(KV(key=SETTINGS_KEY, value=value))
    audit(s, actor, "gold_settings_changed", {"before": before, "after": value})
    return value


class SipHistory:
    """Long daily history for training (Alpaca consolidated history; read-only market data)."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def daily(self, symbol: str, start: date, end: datetime) -> list[tuple[date, float, float]]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        st = self.settings
        c = StockHistoricalDataClient(st.alpaca_api_key_id.get_secret_value(), st.alpaca_api_secret_key.get_secret_value())
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=datetime.combine(start, datetime.min.time(), UTC),
                               end=end, adjustment=Adjustment.SPLIT, feed=DataFeed.SIP, limit=None)
        raw = c.get_stock_bars(req).data.get(symbol, [])
        return [(b.timestamp.astimezone(NEW_YORK).date(), float(b.open), float(b.close)) for b in raw]


class GoldEngine(Engine):
    def __init__(self, db, broker, calendar, data_provider, settings: Settings, history=None, model_dir=None):
        super().__init__(db, broker, calendar, data_provider, settings, symbol=SYMBOL, also_manage=("SPY",))
        self.history = history or SipHistory(settings)
        self.model_dir = model_dir or (settings.data_dir / "models")
        self.model: gold_ai.GoldModel | None = gold_ai.load(self.model_dir)
        self._spy: BarSeries | None = None

    # ------------------------------------------------------------------ cycle
    def tick_after(self, ctx: TickContext) -> None:
        """Run after the SPY engine's tick (which already reconciled the account this cycle)."""
        if self.broker is None or ctx.account is None:
            return
        self.maybe_retrain(ctx.now)
        self.evaluate(ctx)
        self.reassess(ctx)
        self.execute(ctx)

    def automation_enabled(self, s: Session) -> bool:
        return get_settings(s)["enabled"]

    # ------------------------------------------------------------------ learning
    def _near_open(self, now: datetime) -> bool:
        day = now.astimezone(NEW_YORK).date()
        sess = self.cal.session(day)
        return bool(sess and sess.open_utc - timedelta(minutes=45) <= now <= sess.open_utc + timedelta(minutes=30))

    def maybe_retrain(self, now: datetime, force: bool = False) -> bool:
        if not force:
            if self._near_open(now):
                return False  # never compete with the order window
            if self.model and self.model.trained_at:
                age = now - datetime.fromisoformat(self.model.trained_at.replace("Z", "+00:00"))
                if age < timedelta(days=RETRAIN_EVERY_DAYS):
                    return False
            with self.db.session() as s:
                last = (s.get(KV, MODEL_KEY).value if s.get(KV, MODEL_KEY) else {}) or {}
            if last.get("last_attempt") and now - datetime.fromisoformat(last["last_attempt"]) < timedelta(hours=6):
                return False
        status = {"last_attempt": now.isoformat()}
        try:
            end = now - timedelta(minutes=20)
            g = self.history.daily(SYMBOL, TRAIN_START, end)
            sp = {d: c for d, _, c in self.history.daily("SPY", TRAIN_START, end)}
            g = [r for r in g if r[0] in sp]
            days = [r[0] for r in g]
            gc, go = np.array([r[2] for r in g]), np.array([r[1] for r in g])
            sc = np.array([sp[d] for d in days])
            model = gold_ai.train(days, gc, go, sc)
            first_test_year = days[-1].year - 4
            ev = gold_ai.walk_forward(days, gc, go, sc, first_test_year)
            model.evaluation = {k: v for k, v in ev.items()}
            model.trained_at = now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            gold_ai.save(model, self.model_dir)
            self.model = model
            status.update(ok=True, trained_through=model.trained_through, rows=model.train_rows, version=model.version)
            msg = (f"Gold AI retrained on {model.train_rows} days up to {model.trained_through}. Test on unseen years "
                   f"{ev['from'][:4]}-{ev['to'][:4]}: AI {ev['model']['total_return_pct']:+.1f}% vs just holding gold "
                   f"{ev['hold']['total_return_pct']:+.1f}%; direction calls right {ev['direction_hit_rate_pct']}% of the time.")
            with self.db.session() as s:
                emit(s, "gold_model_trained", "info", "Gold AI retrained", msg, f"goldtrain:{model.trained_through}", settings=self.settings)
        except Exception as exc:  # noqa: BLE001 - training problems never stop trading; the previous model stays
            status.update(ok=False, error=redact(f"{type(exc).__name__}: {exc}")[:300])
            log.warning("gold model training failed: %s", status["error"])
        with self.db.session() as s:
            row = s.get(KV, MODEL_KEY)
            if row:
                row.value = status
            else:
                s.add(KV(key=MODEL_KEY, value=status))
        return bool(status.get("ok"))

    # ------------------------------------------------------------------ data
    def _fetch(self, session_day: date, now: datetime, p: StrategyParams):
        start = session_day - timedelta(days=HISTORY_DAYS)
        out = {}
        for sym in (SYMBOL, "SPY"):
            try:
                series = self.data.fetch(sym, start, session_day, now, self.cal, p.completion_buffer_minutes)
            except BrokerError as e:
                return None, f"{sym} market data request failed: {redact(e)}"
            except Exception as e:  # noqa: BLE001
                return None, f"{sym} market data error: {redact(e)[:200]}"
            if not series.bars or series.bars[-1].day != session_day:
                return None, f"latest completed {sym} bar is not {session_day} yet (provider not updated)"
            try:
                validate_bars(series.bars, self.cal)
            except ValueError as e:
                return None, f"{sym} data quality: {e}"
            window = self.cal.sessions_between(series.bars[0].day, session_day)[-gold_ai.MIN_HISTORY:]
            have = {b.day for b in series.bars}
            if any(w.day not in have for w in window) or len(series.bars) < gold_ai.MIN_HISTORY:
                return None, f"{sym}: need {gold_ai.MIN_HISTORY} complete sessions without gaps"
            out[sym] = series
        self._spy = out["SPY"]  # same cycle's SPY bars, used for the stock-market signals
        return out[SYMBOL], None

    # ------------------------------------------------------------------ decision
    def _decide(self, s: Session, ctx: TickContext, day: date, series: BarSeries | None, data_err: str | None,
                v: StrategyVersion, p: StrategyParams, sequence: int = 1) -> Decision:
        c = control(s)
        akey = c.account_fingerprint
        pos = position(s, akey, SYMBOL)
        base = dict(account_key=akey, symbol=SYMBOL, session=day, sequence=sequence, strategy_version_id=v.id,
                    mode_at_decision=c.mode, previous_target=pos.signal_target, held_qty_at_decision=pos.held_qty, known_at=ctx.now)
        model = self.model
        if series is None or model is None:
            why = data_err or "the gold AI has no trained model yet (it trains automatically; see the Gold AI page)"
            d = Decision(**base, data_mode="ALPACA", data_source="unavailable" if series is None else series.source,
                         target=INSUFFICIENT, action="SKIP", reason=f"Gold AI: no decision possible: {why}.", inputs={"error": why})
            s.add(d)
            s.flush()
            return d
        spy = self._spy
        spy_close = {b.day: float(b.close) for b in spy.bars}
        bars = [b for b in series.bars if b.day in spy_close]
        gc = np.array([float(b.close) for b in bars])
        sc = np.array([spy_close[b.day] for b in bars])
        x = gold_ai.signals(gc, sc)[-1]
        if np.isnan(x).any():
            d = Decision(**base, data_mode=series.mode, data_source=series.source, target=INSUFFICIENT, action="SKIP",
                         reason="Gold AI: not enough history to compute every signal.", inputs={})
            s.add(d)
            s.flush()
            return d
        prob, parts = model.predict(x)
        invested = pos.held_qty > 0
        act = gold_ai.policy(prob, invested, model.enter, model.exit)
        target = (INVESTED if act == "BUY" else CASH if act == "SELL" else (INVESTED if invested else CASH))
        gs = get_settings(s)
        close = series.bars[-1].close
        sma200 = sum((b.close for b in series.bars[-200:]), Decimal(0)) / 200
        top = parts[:5]
        pushes = "; ".join(f"{t['label']} {'↑' if t['push'] > 0 else '↓'}" for t in top)
        core = (f"Gold AI ({model.version}, trained to {model.trained_through}): chance GLD is higher in 20 trading days = "
                f"{prob:.0%}. Rule: buy above {model.enter:.0%}, sell below {model.exit:.0%}. Strongest signals: {pushes}.")
        inputs = {"probability": round(prob, 4), "policy": {"enter": model.enter, "exit": model.exit}, "signals": parts,
                  "model": {"version": model.version, "trained_through": model.trained_through, "rows": model.train_rows},
                  "data": series.describe(), "gold_settings": gs, "spy_close": str(spy.bars[-1].close)}
        action, reason, plan = self._plan_gold(s, ctx, c, pos, act, core, p, inputs, gs, day)
        d = Decision(**base, data_mode=series.mode, data_source=series.source, data_fingerprint=series.fingerprint(),
                     data_fetched_at=ctx.now, close=close, sma=sma200, window_count=len(series.bars), comparison="AI",
                     target=target, action=action, reason=reason, inputs=inputs)
        s.add(d)
        s.flush()
        pos.signal_target = target
        if plan:
            it = OrderIntent(decision_id=d.id, account_key=akey, symbol=SYMBOL, strategy_version_id=v.id, signal_session=day,
                             created_at=ctx.now, **plan)
            s.add(it)
            s.flush()
            transition_note(s, it, "Gold AI intent persisted before any broker contact.")
            title, body = alerts.planned_text(s, it)
            emit(s, "intent_created", "info", title, body, f"intent:{it.client_order_id}", ref=f"intent:{it.id}", settings=self.settings)
        else:
            emit(s, "decision_routine", "routine", f"Gold AI {action} on {day}", reason, f"golddecision:{akey}:{day}:{sequence}",
                 ref=f"decision:{d.id}", settings=self.settings)
        return d

    def _plan_gold(self, s, ctx, c, pos, act, core, p: StrategyParams, inputs, gs, day):
        oi = open_intents(s, c.account_fingerprint, SYMBOL)
        if act == "HOLD":
            return "HOLD", f"{core} {'Invested: keep holding.' if pos.held_qty > 0 else 'In cash: stay out.'}", None
        conflicting = [i for i in oi if (i.side == "BUY" and act == "SELL") or (i.side == "SELL" and act == "BUY")]
        if conflicting:
            results = [self._cancel_intent(s, i, "Gold AI changed its mind; cancelling the opposite order first.", ctx.now) for i in conflicting]
            inputs["awaiting_conflict_resolution"] = [i.id for i in conflicting]
            return "SKIP", f"{core} An opposite gold order is still working ({', '.join(results)}); reassessing after it resolves.", None
        if not gs["enabled"]:
            inputs["would_action"] = act
            return "SKIP", f"{core} Shadow mode: the AI would {act}, but gold trading is switched off on the Gold AI page.", None
        if c.mode != "PAPER_AUTO":
            inputs["would_action"] = act
            return "SKIP", f"{core} OBSERVE mode: the AI would {act} but no order is created.", None
        blockers = []
        if c.security_paused:
            blockers.append("SECURITY_PAUSED")
        if c.new_orders_paused:
            blockers.append("new orders paused by owner")
        if c.review_required:
            blockers.append("review required")
        if act == "BUY" and c.risk_paused_entries:
            blockers.append("RISK_PAUSED_ENTRIES")
        if not ctx.reconciled_ok:
            blockers.append("latest reconciliation not clean")
        if oi:
            return "SKIP", f"{core} A gold order is already working; no duplicate.", None
        if blockers:
            return "SKIP", f"{core} Would {act}, blocked: {'; '.join(blockers)}.", None
        sess = next_eligible_session(self.cal, day, ctx.now, p.execution_window_minutes)
        if sess is None:
            return "SKIP", f"{core} No upcoming session in the calendar.", None
        akey = c.account_fingerprint
        v = active_version(s)
        if act == "BUY":
            committed = sum((i.frozen_notional or Decimal(0)) for sym in self.managed for i in open_intents(s, akey, sym) if i.side == "BUY")
            budget = min(Decimal(gs["budget_usd"]), p.max_entry_notional_usd)
            avail = ctx.account.cash - committed - p.cash_reserve_usd
            notional = min(budget, avail).quantize(Decimal("0.01"))
            inputs["sizing"] = {"budget_usd": str(budget), "cash_after_reserve_and_committed": str(avail), "target": str(notional)}
            if notional < p.min_order_notional_usd:
                return "SKIP", f"{core} Not enough unborrowed cash for a gold entry ({avail:.2f} USD available).", None
            return "BUY", f"{core} Buy {notional} USD of GLD at the next open ({sess.day}).", dict(
                kind="ENTRY", side="BUY", execution_session=sess.day, attempt=1, frozen_notional=notional, sizing=inputs["sizing"],
                client_order_id=make_client_order_id(akey, v.id, SYMBOL, day, "ENTRY", 1))
        return "SELL", f"{core} Sell the gold AI's {pos.held_qty} GLD at the next open ({sess.day}).", dict(
            kind="EXIT", side="SELL", execution_session=sess.day, attempt=1, target_qty=pos.held_qty,
            client_order_id=make_client_order_id(akey, v.id, SYMBOL, day, "EXIT", 1))
