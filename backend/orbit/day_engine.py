"""AI day trader (paper only): buys IVV at about 09:45 New York time on days the model expects a winning
trade, and is always flat again before the close.

Each US session:
1. 09:45-10:00 NY: once the first 15-minute bar is complete, the model (orbit.day_ai) scores today using
   the overnight gap, the first 15 minutes and earlier days. Buy if the chance of a winning trade is above
   the model's threshold, unless the news reader shows a very negative news day (news veto) or the trader
   has paused itself.
2. While holding: sell straight away if IVV falls STOP below the buy price (stop-loss, checked every cycle
   from the latest trade price).
3. 10 minutes before the close: sell whatever is held. A holding left from an earlier day (for example the
   computer was asleep) is sold as soon as the next session opens.

Learning: the model retrains every RETRAIN_EVERY_DAYS days outside market hours. Self-check: after every
completed trade, if the last SELF_CHECK_TRADES trades lost money in total, it pauses its own buys and alerts
the owner (`orbit.cli day resume` switches it back on). Exits are never blocked by the pause.

It reuses the shared order lifecycle (intent saved before broker contact, idempotent client ids,
reconciliation, review states) and every global control (OBSERVE mode, pauses, risk pause, per-order cap).
IVV is used so its shares never mix with the SPY 200-day rule's SPY shares in the same account.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit import day_ai, news_ai
from orbit.broker.base import BrokerError, make_client_order_id
from orbit.config import Settings, redact
from orbit.db import KV, Decision, Fill, OrderIntent
from orbit.engine import Engine, TickContext, active_version, audit, control, open_intents, position, transition_note, version_params
from orbit.market_calendar import NEW_YORK, UTC, ensure_utc
from orbit.notify import emit
from orbit.strategy import CASH, INSUFFICIENT, INVESTED

log = logging.getLogger("orbit.day")
SYMBOL = "IVV"
SIGNAL_SYMBOL = "SPY"  # the model reads SPY (same index, deepest history); orders go to IVV
SETTINGS_KEY = "day_settings"
MODEL_KEY = "day_model_status"
DEFAULT_BUDGET = Decimal("250")
DECIDE_AFTER_MIN = 15  # the first 15-minute bar must be complete
DECIDE_UNTIL_MIN = 30  # a late worker does not buy after 10:00 NY: the model was trained on 09:45 entries
EXIT_BEFORE_CLOSE_MIN = 10
RETRAIN_EVERY_DAYS = 7
SELF_CHECK_TRADES = 20
NEWS_VETO_MOOD = -0.30
TRAIN_START = date(2016, 1, 1)


# ---------------------------------------------------------------------- settings
def get_settings(s: Session) -> dict:
    row = s.get(KV, SETTINGS_KEY)
    v = dict(row.value) if row and row.value else {}
    return {"enabled": bool(v.get("enabled", False)), "budget_usd": str(v.get("budget_usd", DEFAULT_BUDGET)),
            "self_paused": bool(v.get("self_paused", False)), "self_pause_reason": v.get("self_pause_reason", ""),
            "count_trades_after": v.get("count_trades_after")}


def _put_settings(s: Session, value: dict) -> None:
    row = s.get(KV, SETTINGS_KEY)
    if row:
        row.value = value
    else:
        s.add(KV(key=SETTINGS_KEY, value=value))


def set_settings(s: Session, enabled: bool, budget_usd: Decimal, cap: Decimal, actor: str = "user") -> dict:
    budget = Decimal(budget_usd).quantize(Decimal("0.01"))
    if budget < Decimal("1") or budget > cap:
        raise ValueError(f"day-trader budget must be between 1 and {cap} USD (the per-order cap)")
    before = get_settings(s)
    value = {**before, "enabled": bool(enabled), "budget_usd": str(budget)}
    _put_settings(s, value)
    audit(s, actor, "day_settings_changed", {"before": before, "after": value})
    return value


def resume(s: Session, now: datetime, actor: str = "user") -> dict:
    """Clear a self-pause. Only trades after this moment count towards the next self-check."""
    before = get_settings(s)
    value = {**before, "self_paused": False, "self_pause_reason": "", "count_trades_after": ensure_utc(now).isoformat()}
    _put_settings(s, value)
    audit(s, actor, "day_trader_resumed", {"before": before})
    return value


# ---------------------------------------------------------------------- trade results
def completed_trades(s: Session, account_key: str, after: datetime | None = None) -> list[dict]:
    """Pair each day-trader buy with the sells that followed it (FIFO by time). Newest last."""
    fills = list(s.scalars(select(Fill).join(OrderIntent, Fill.intent_id == OrderIntent.id)
                           .where(OrderIntent.account_key == account_key, OrderIntent.symbol == SYMBOL).order_by(Fill.at, Fill.id)))
    trades, cur = [], None
    for f in fills:
        if f.side == "BUY":
            if cur is None:
                cur = {"bought_at": ensure_utc(f.at), "qty": Decimal(0), "cost": Decimal(0), "sold_qty": Decimal(0), "proceeds": Decimal(0)}
            cur["qty"] += f.qty
            cur["cost"] += f.qty * f.price
        elif cur is not None:
            cur["sold_qty"] += f.qty
            cur["proceeds"] += f.qty * f.price
            cur["sold_at"] = ensure_utc(f.at)
            if cur["sold_qty"] >= cur["qty"] - Decimal("0.000001"):
                cost_sold = cur["cost"]
                pnl = cur["proceeds"] - cost_sold
                trades.append({"day": cur["bought_at"].astimezone(NEW_YORK).date().isoformat(), "bought_at": cur["bought_at"].isoformat(),
                               "sold_at": cur["sold_at"].isoformat(), "buy_price": str((cur["cost"] / cur["qty"]).quantize(Decimal("0.0001"))),
                               "sell_price": str((cur["proceeds"] / cur["sold_qty"]).quantize(Decimal("0.0001"))),
                               "pnl": str(pnl.quantize(Decimal("0.01"))), "return_pct": float(pnl / cost_sold * 100) if cost_sold else 0.0})
                cur = None
    if after is not None:
        trades = [t for t in trades if datetime.fromisoformat(t["bought_at"]) >= after]
    return trades


def summary(s: Session, account_key: str | None) -> dict:
    gs = get_settings(s) if s else {}
    row = s.get(KV, MODEL_KEY)
    out = {"settings": gs, "model_status": dict(row.value) if row else {}, "symbol": SYMBOL, "trades": [], "stop_pct": day_ai.STOP * 100}
    if account_key:
        t = completed_trades(s, account_key)
        pnl = sum((Decimal(x["pnl"]) for x in t), Decimal(0))
        out.update(trades=t[-30:], count=len(t), wins=sum(1 for x in t if Decimal(x["pnl"]) > 0), pnl=str(pnl),
                   held_qty=str(position(s, account_key, SYMBOL).held_qty))
        last = s.scalar(select(Decision).where(Decision.account_key == account_key, Decision.symbol == SYMBOL).order_by(Decision.id.desc()).limit(1))
        out["latest"] = {"session": last.session.isoformat(), "action": last.action, "reason": last.reason,
                         "probability": (last.inputs or {}).get("probability")} if last else None
    return out


# ---------------------------------------------------------------------- data access (replaceable in tests)
class IntradayData:
    """Today's first 15-minute bar and latest trade price (Alpaca market data, read-only), plus training history."""

    def __init__(self, settings: Settings, live=None):
        from orbit.data.live import LiveMarketData
        self.settings = settings
        self.live = live or LiveMarketData(settings)

    def first_bar(self, symbol: str, sess, now: datetime) -> tuple[float, float] | None:
        """(open, close) of today's 09:30-09:45 bar, or None if it is not complete yet."""
        if now < sess.open_utc + timedelta(minutes=DECIDE_AFTER_MIN):
            return None
        data = self.live.candles(symbol, "15Min", now)
        for c in data["candles"]:
            if int(c["t"]) == int(sess.open_utc.timestamp()):
                return float(c["o"]), float(c["c"])
        return None

    def latest_price(self, symbol: str, now: datetime) -> float | None:
        data = self.live.candles(symbol, "1Min", now)
        lt = data.get("latest_trade")
        if lt and lt.get("price"):
            at = datetime.fromisoformat(lt["at"])
            if now - at < timedelta(minutes=5):
                return float(lt["price"])
        return None

    def history_15min(self, symbol: str, start: date, end: datetime) -> list:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        st = self.settings
        c = StockHistoricalDataClient(st.alpaca_api_key_id.get_secret_value(), st.alpaca_api_secret_key.get_secret_value())
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                               start=datetime.combine(start, datetime.min.time(), UTC), end=end, adjustment=Adjustment.SPLIT,
                               feed=DataFeed.SIP, limit=None)
        raw = c.get_stock_bars(req).data.get(symbol, [])
        return [(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close)) for b in raw]


# ---------------------------------------------------------------------- engine
class DayEngine(Engine):
    def __init__(self, db, broker, calendar, data_provider, settings: Settings, intraday=None, model_dir=None):
        super().__init__(db, broker, calendar, data_provider, settings, symbol=SYMBOL, also_manage=("SPY", "GLD"))
        self.intraday = intraday or IntradayData(settings)
        self.model_dir = model_dir or (settings.data_dir / "models")
        self.model: day_ai.DayModel | None = day_ai.load(self.model_dir)

    def tick_after(self, ctx: TickContext) -> None:
        """Runs after the SPY and gold engines in the same cycle (the account was already reconciled)."""
        if self.broker is None or ctx.account is None:
            return
        self.maybe_retrain(ctx.now)
        sess = self.cal.session_containing(ctx.now)
        if sess is not None:
            self.manage_exits(ctx, sess)
            self.evaluate_day(ctx, sess)
        self.execute(ctx)
        self.self_check(ctx.now)

    def _window_start(self, it: OrderIntent, sess) -> datetime:
        # Day-trader orders are created during the session and may be sent as soon as they exist.
        return max(sess.open_utc, ensure_utc(it.created_at))

    # ------------------------------------------------------------------ learning
    def _market_hours(self, now: datetime) -> bool:
        sess = self.cal.session(now.astimezone(NEW_YORK).date())
        return bool(sess and sess.open_utc - timedelta(minutes=30) <= now <= sess.close_utc + timedelta(minutes=20))

    def maybe_retrain(self, now: datetime, force: bool = False) -> bool:
        if not force:
            if self._market_hours(now):
                return False
            if self.model and self.model.trained_at:
                age = now - datetime.fromisoformat(self.model.trained_at.replace("Z", "+00:00"))
                if age < timedelta(days=RETRAIN_EVERY_DAYS):
                    return False
            with self.db.session() as s:
                row = s.get(KV, MODEL_KEY)
                last = dict(row.value) if row else {}
            if last.get("last_attempt") and now - datetime.fromisoformat(last["last_attempt"]) < timedelta(hours=6):
                return False
        status = {"last_attempt": now.isoformat()}
        try:
            bars = self.intraday.history_15min(SIGNAL_SYMBOL, TRAIN_START, now - timedelta(minutes=20))
            rows = day_ai.rows_from_15min(bars, NEW_YORK)
            model = day_ai.train(rows)
            ev = day_ai.walk_forward(rows, rows[-1].day.year - 4, model.threshold)
            model.evaluation = ev
            model.trained_at = now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            day_ai.save(model, self.model_dir)
            self.model = model
            status.update(ok=True, trained_through=model.trained_through, rows=model.train_rows, version=model.version, evaluation=ev)
            msg = (f"AI day trader retrained on {model.train_rows} days up to {model.trained_through}. Test on unseen years "
                   f"{ev['from'][:4]}-{ev['to'][:4]}: AI {ev['ai']['total_return_pct']:+.1f}% over {ev['ai']['trades']} trades "
                   f"({ev['ai']['win_pct']}% winners); trading every day {ev['every_day']['total_return_pct']:+.1f}%; "
                   f"just holding {ev['hold_pct']:+.1f}%. No costs included.")
            with self.db.session() as s:
                emit(s, "day_model_trained", "info", "AI day trader retrained", msg, f"daytrain:{model.trained_through}", settings=self.settings)
        except Exception as exc:  # noqa: BLE001 - training problems never stop trading; the previous model stays
            status.update(ok=False, error=redact(f"{type(exc).__name__}: {exc}")[:300])
            log.warning("day model training failed: %s", status["error"])
        with self.db.session() as s:
            row = s.get(KV, MODEL_KEY)
            if row:
                row.value = status
            else:
                s.add(KV(key=MODEL_KEY, value=status))
        return bool(status.get("ok"))

    # ------------------------------------------------------------------ entries
    def _features(self, sess, now: datetime, p) -> tuple[np.ndarray | None, dict, str | None]:
        first = self.intraday.first_bar(SIGNAL_SYMBOL, sess, now)
        if first is None:
            return None, {}, "today's first 15-minute bar is not available yet"
        prev = self.cal.previous_session(sess.day)
        start = sess.day - timedelta(days=420)
        try:
            series = self.data.fetch(SIGNAL_SYMBOL, start, prev.day, now, self.cal, p.completion_buffer_minutes)
        except BrokerError as e:
            return None, {}, f"daily data request failed: {redact(e)}"
        except Exception as e:  # noqa: BLE001
            return None, {}, f"daily data error: {redact(e)[:200]}"
        bars = [b for b in series.bars if b.day <= prev.day]
        if len(bars) < day_ai.MIN_HISTORY or bars[-1].day != prev.day:
            return None, {}, f"need {day_ai.MIN_HISTORY} completed days ending {prev.day}"
        rows = [day_ai.DayRow(b.day, float(b.open), float("nan"), float(b.high), float(b.low), float(b.close)) for b in bars]
        rows.append(day_ai.DayRow(sess.day, first[0], first[1], float("nan"), float("nan"), float("nan")))
        x = day_ai.signals(rows)[-1]
        info = {"open": first[0], "price_0945": first[1], "prev_close": float(bars[-1].close), "data": series.describe()}
        if np.isnan(x).any():
            return None, info, "not enough history to compute every signal"
        return x, info, None

    def evaluate_day(self, ctx: TickContext, sess) -> Decision | None:
        now = ctx.now
        if not (sess.open_utc + timedelta(minutes=DECIDE_AFTER_MIN) <= now < sess.open_utc + timedelta(minutes=DECIDE_UNTIL_MIN)):
            return None
        if now >= sess.close_utc - timedelta(minutes=EXIT_BEFORE_CLOSE_MIN + 30):
            return None  # very short (half) day: no time for a trade
        with self.db.session() as s:
            c = control(s)
            akey = c.account_fingerprint
            if not akey or s.scalar(select(Decision.id).where(Decision.account_key == akey, Decision.symbol == SYMBOL,
                                                              Decision.session == sess.day)):
                return None
            v = active_version(s)
            p, _ = version_params(v)
        model = self.model
        x, info, err = (None, {}, "no trained model yet (run `orbit.cli day train`)") if model is None else self._features(sess, now, p)
        if err and now < sess.open_utc + timedelta(minutes=DECIDE_UNTIL_MIN - 2) and model is not None:
            return None  # data may arrive on the next cycle; decide (or skip) before the cut-off
        with self.db.session() as s:
            c = control(s)
            pos = position(s, akey, SYMBOL)
            base = dict(account_key=akey, symbol=SYMBOL, session=sess.day, sequence=1, strategy_version_id=v.id, mode_at_decision=c.mode,
                        previous_target=pos.signal_target, held_qty_at_decision=pos.held_qty, known_at=now, data_mode="ALPACA",
                        data_fetched_at=now)
            if err:
                d = Decision(**base, data_source="unavailable", target=INSUFFICIENT, action="SKIP",
                             reason=f"AI day trader: no decision today: {err}.", inputs={"error": err, **info})
                s.add(d)
                s.flush()
                emit(s, "decision_routine", "routine", f"Day trader SKIP on {sess.day}", d.reason, f"daydecision:{akey}:{sess.day}",
                     ref=f"decision:{d.id}", settings=self.settings)
                return d
            prob, parts = model.predict(x)
            gs = get_settings(s)
            pushes = "; ".join(f"{t['label']} {'↑' if t['push'] > 0 else '↓'}" for t in parts[:4])
            core = (f"AI day trader ({model.version}, trained to {model.trained_through}): chance today's 09:45-15:50 trade wins = "
                    f"{prob:.0%} (buys above {model.threshold:.0%}). Gap {info['open'] / info['prev_close'] - 1:+.2%}, "
                    f"first 15 minutes {info['price_0945'] / info['open'] - 1:+.2%}. Strongest signals: {pushes}.")
            inputs = {"probability": round(prob, 4), "threshold": model.threshold, "signals": parts, **info,
                      "model": {"version": model.version, "trained_through": model.trained_through, "rows": model.train_rows},
                      "day_settings": gs}
            action, reason, plan = self._plan(s, ctx, c, pos, prob > model.threshold, core, p, inputs, gs, sess)
            d = Decision(**base, data_source="alpaca 15-minute (IEX) + daily bars", target=INVESTED if action == "BUY" else CASH,
                         action=action, reason=reason, inputs=inputs, comparison="AI", window_count=day_ai.MIN_HISTORY)
            s.add(d)
            s.flush()
            if plan:
                it = OrderIntent(decision_id=d.id, account_key=akey, symbol=SYMBOL, strategy_version_id=v.id, signal_session=sess.day,
                                 created_at=now, reason=reason, **plan)
                s.add(it)
                s.flush()
                transition_note(s, it, "Day-trader intent persisted before any broker contact.")
                emit(s, "intent_created", "info", f"Orbit: day trade BUY {SYMBOL}",
                     f"{reason}\n\nIt sells by 10 minutes before the close, or earlier if IVV falls {model.stop:.2%} below the buy price.",
                     f"intent:{it.client_order_id}", ref=f"intent:{it.id}", settings=self.settings)
            else:
                emit(s, "decision_routine", "routine", f"Day trader {action} on {sess.day}", reason, f"daydecision:{akey}:{sess.day}",
                     ref=f"decision:{d.id}", settings=self.settings)
            return d

    def _plan(self, s, ctx, c, pos, wants_buy: bool, core: str, p, inputs: dict, gs: dict, sess):
        if not wants_buy:
            return "HOLD", f"{core} Not confident enough: no trade today.", None
        news = news_ai.get_mood(s)
        mood = news.get("funds", {}).get("SPY") if news.get("available") else None
        if mood and (mood.get("warn") or mood.get("mood", 0) <= NEWS_VETO_MOOD):
            inputs["news_veto"] = mood
            return "SKIP", f"{core} News veto: the last 24 hours of SPY news are very negative (mood {mood.get('mood')}), so no buy today.", None
        if not gs["enabled"]:
            inputs["would_action"] = "BUY"
            return "SKIP", f"{core} Switched off: the AI would buy, but the day trader is off (`orbit.cli day on`).", None
        if gs["self_paused"]:
            inputs["would_action"] = "BUY"
            return "SKIP", f"{core} Self-paused: {gs['self_pause_reason']} Resume with `orbit.cli day resume`.", None
        if c.mode != "PAPER_AUTO":
            return "SKIP", f"{core} OBSERVE mode: the AI would buy but no order is created.", None
        blockers = [b for b, on in (("SECURITY_PAUSED", c.security_paused), ("new orders paused by owner", c.new_orders_paused),
                                    ("review required", c.review_required), ("RISK_PAUSED_ENTRIES", c.risk_paused_entries),
                                    ("latest reconciliation not clean", not ctx.reconciled_ok)) if on]
        if open_intents(s, c.account_fingerprint, SYMBOL) or pos.held_qty > 0:
            return "SKIP", f"{core} Already holding or an IVV order is working; no second buy.", None
        if blockers:
            return "SKIP", f"{core} Would buy, blocked: {'; '.join(blockers)}.", None
        akey = c.account_fingerprint
        committed = sum((i.frozen_notional or Decimal(0)) for sym in self.managed for i in open_intents(s, akey, sym) if i.side == "BUY")
        budget = min(Decimal(gs["budget_usd"]), p.max_entry_notional_usd)
        avail = ctx.account.cash - committed - p.cash_reserve_usd
        notional = min(budget, avail).quantize(Decimal("0.01"))
        inputs["sizing"] = {"budget_usd": str(budget), "cash_after_reserve_and_committed": str(avail), "target": str(notional)}
        if notional < p.min_order_notional_usd:
            return "SKIP", f"{core} Not enough unborrowed cash ({avail:.2f} USD available).", None
        v = active_version(s)
        return "BUY", f"{core} Buy {notional} USD of {SYMBOL} now.", dict(
            kind="ENTRY", side="BUY", execution_session=sess.day, attempt=1, frozen_notional=notional, sizing=inputs["sizing"],
            client_order_id=make_client_order_id(akey, v.id, SYMBOL, sess.day, "ENTRY", 1))

    # ------------------------------------------------------------------ exits
    def manage_exits(self, ctx: TickContext, sess) -> None:
        now = ctx.now
        if ctx.clock_open is not True or not ctx.reconciled_ok:
            return
        with self.db.session() as s:
            c = control(s)
            akey = c.account_fingerprint
            if not akey:
                return
            pos = position(s, akey, SYMBOL)
            if pos.held_qty <= 0 or open_intents(s, akey, SYMBOL):
                return
            buy = s.scalar(select(OrderIntent).where(OrderIntent.account_key == akey, OrderIntent.symbol == SYMBOL,
                                                     OrderIntent.side == "BUY", OrderIntent.filled_avg_price.is_not(None))
                           .order_by(OrderIntent.id.desc()).limit(1))
            buy_price = float(buy.filled_avg_price) if buy is not None else None
            bought_today = buy is not None and buy.execution_session == sess.day
        why, price = None, None
        if not bought_today:
            why = "left over from an earlier session (Orbit was not running at the last close); selling at today's open"
        elif now >= sess.close_utc - timedelta(minutes=EXIT_BEFORE_CLOSE_MIN):
            why = "end of day: the day trader never holds overnight"
        else:
            try:
                price = self.intraday.latest_price(SYMBOL, now)
            except Exception as e:  # noqa: BLE001 - no price this cycle: try again in 20 seconds
                log.warning("day trader price check failed: %s", redact(e)[:200])
                price = None
            stop = (self.model.stop if self.model else day_ai.STOP)
            if price is not None and buy_price and price <= buy_price * (1 - stop):
                why = f"stop-loss: IVV {price:.2f} is {price / buy_price - 1:.2%} from the buy price {buy_price:.2f}"
        if why is None:
            return
        with self.db.session() as s:
            c = control(s)
            akey = c.account_fingerprint
            pos = position(s, akey, SYMBOL)
            if pos.held_qty <= 0 or open_intents(s, akey, SYMBOL):
                return
            v = active_version(s)
            attempt = 1 + len(list(s.scalars(select(OrderIntent.id).where(OrderIntent.account_key == akey, OrderIntent.symbol == SYMBOL,
                                                                           OrderIntent.side == "SELL", OrderIntent.signal_session == sess.day))))
            reason = f"AI day trader exit: {why}. Sell {pos.held_qty} {SYMBOL}."
            it = OrderIntent(decision_id=buy.decision_id if buy is not None else None, account_key=akey, symbol=SYMBOL,
                             strategy_version_id=v.id, kind="EXIT", side="SELL", signal_session=sess.day, execution_session=sess.day,
                             attempt=attempt, target_qty=pos.held_qty, created_at=now, reason=reason, sizing={"exit_reason": why},
                             client_order_id=make_client_order_id(akey, v.id, SYMBOL, sess.day, "EXIT", attempt))
            s.add(it)
            s.flush()
            transition_note(s, it, "Day-trader exit intent persisted before any broker contact.")
            emit(s, "intent_created", "info", f"Orbit: day trade SELL {SYMBOL}", reason, f"intent:{it.client_order_id}",
                 ref=f"intent:{it.id}", settings=self.settings)

    def _execute_one(self, ctx: TickContext, intent_id: int) -> None:
        # Exits must go out even if the owner switched the day trader off after buying.
        with self.db.session() as s:
            it = s.get(OrderIntent, intent_id)
            is_exit = it is not None and it.side == "SELL"
        self._exit_in_progress = is_exit
        try:
            super()._execute_one(ctx, intent_id)
        finally:
            self._exit_in_progress = False

    def automation_enabled(self, s: Session) -> bool:
        """Buys need the switch on; exits always go out (never leave a day trade open overnight)."""
        return getattr(self, "_exit_in_progress", False) or get_settings(s)["enabled"]

    # ------------------------------------------------------------------ self-check
    def self_check(self, now: datetime) -> bool:
        """Pause new buys if the last SELF_CHECK_TRADES completed trades lost money in total."""
        with self.db.session() as s:
            gs = get_settings(s)
            akey = control(s).account_fingerprint
            if gs["self_paused"] or not akey:
                return False
            after = datetime.fromisoformat(gs["count_trades_after"]) if gs.get("count_trades_after") else None
            trades = completed_trades(s, akey, after)[-SELF_CHECK_TRADES:]
            if len(trades) < SELF_CHECK_TRADES:
                return False
            total = sum((Decimal(t["pnl"]) for t in trades), Decimal(0))
            if total >= 0:
                return False
            wins = sum(1 for t in trades if Decimal(t["pnl"]) > 0)
            why = (f"its last {SELF_CHECK_TRADES} trades lost {abs(total):.2f} USD in total ({wins} winners), "
                   "which is worse than not trading.")
            _put_settings(s, {**gs, "self_paused": True, "self_pause_reason": why})
            audit(s, "worker", "day_trader_self_paused", {"trades": len(trades), "total": str(total)}, "warning")
            emit(s, "day_trader_paused", "warning", "AI day trader paused itself", f"The AI day trader stopped buying: {why} "
                 "Exits still run. Resume with `orbit.cli day resume` after reviewing its trades.", f"dayselfpause:{now:%Y%m%d}",
                 settings=self.settings)
            return True
