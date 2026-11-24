"""AI day trader: model maths, paper trades through the shared lifecycle, stop-loss, end-of-day exit,
news veto, off switch, leftovers, self-pause and retraining. Mock broker only."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from sqlalchemy import select

from orbit import day_ai, day_engine
from orbit.db import KV, ControlState, Decision, Fill, OrderIntent
from orbit.day_engine import DayEngine
from orbit.engine import Engine, position
from orbit.market_calendar import NEW_YORK
from tests.conftest import at
from tests.test_engine import D1, D2, activate

UTC = timezone.utc


def _model(bias: float) -> day_ai.DayModel:
    n = len(day_ai.KEYS)
    return day_ai.DayModel([bias] + [0.0] * n, [0.0] * n, [1.0] * n, "2026-02-27", 2000, trained_at="2026-03-01T00:00:00Z")


class FakeIntraday:
    def __init__(self):
        self.price = None
        self.first = (100.0, 100.4)
        self.history_calls = 0

    def first_bar(self, symbol, sess, now):
        return self.first if now >= sess.open_utc + timedelta(minutes=15) else None

    def latest_price(self, symbol, now):
        return self.price

    def history_15min(self, symbol, start, end):
        self.history_calls += 1
        rng = np.random.default_rng(11)
        out, d, px = [], date(2016, 1, 4), 200.0
        while d < end.date():
            if d.weekday() < 5:
                t0 = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NEW_YORK)
                for k in range(26):
                    o = px
                    px *= float(np.exp(rng.normal(0.00002, 0.0015)))
                    out.append((t0 + timedelta(minutes=15 * k), o, max(o, px) * 1.0005, min(o, px) * 0.9995, px))
            d += timedelta(days=1)
        return out


def _setup(env, tmp_path, bias=2.0, enabled=True):
    env["engine"] = Engine(env["db"], env["broker"], env["cal"], env["data"], env["settings"], also_manage=("GLD", "IVV"))
    day = DayEngine(env["db"], env["broker"], env["cal"], env["data"], env["settings"], intraday=FakeIntraday(), model_dir=tmp_path / "m")
    day.model = _model(bias)
    activate(env)
    with env["db"].session() as s:
        day_engine.set_settings(s, enabled, Decimal("250"), Decimal("250"))
    return day


def _cycle(env, day, now):
    env["broker"].now = now
    ctx = env["engine"].tick(now)
    day.tick_after(ctx)
    return ctx


def _ivv(env):
    with env["db"].session() as s:
        return list(s.scalars(select(OrderIntent).where(OrderIntent.symbol == "IVV").order_by(OrderIntent.id)))


def _clean(env):
    with env["db"].session() as s:
        c = s.get(ControlState, 1)
        return not c.security_paused and not c.review_required


# ---------------------------------------------------------------------- model maths
def test_signals_use_only_the_past_and_trades_replay_the_stop():
    rows = [day_ai.DayRow(date(2020, 1, 1) + timedelta(days=i), 100 + i, 100.2 + i, 101 + i, 99 + i, 100.5 + i, 0.001) for i in range(260)]
    a = day_ai.signals(rows)
    changed = [day_ai.DayRow(r.day, r.open, r.entry, r.high, r.low, r.close * (1.5 if i >= 240 else 1), r.trade_ret) for i, r in enumerate(rows)]
    b = day_ai.signals(changed)
    assert np.allclose(a[:241], b[:241], equal_nan=True)  # row t only uses closes before t
    assert not np.isnan(a[-1]).any() and a.shape[1] == len(day_ai.KEYS)
    later = [((9, 45), 100.0, 100.2, 99.8, 100.1), ((10, 0), 100.1, 100.2, 99.0, 99.2), ((15, 45), 99.5, 99.6, 99.4, 99.5)]
    assert abs(day_ai.simulate_trade(100.0, later) - (-0.0075)) < 1e-12  # stopped out at -0.75%
    calm = [((9, 45), 100.0, 100.3, 99.9, 100.2), ((15, 45), 100.6, 100.7, 100.5, 100.6)]
    assert abs(day_ai.simulate_trade(100.0, calm) - 0.006) < 1e-12  # sold at the 15:45 bar
    gap = [((10, 0), 98.0, 98.5, 97.5, 98.0)]
    assert abs(day_ai.simulate_trade(100.0, gap) - (-0.02)) < 1e-12  # a jump through the stop fills at the worse price


# ---------------------------------------------------------------------- trading
def test_buys_after_first_15_minutes_and_stop_loss_sells(env, tmp_path):
    day = _setup(env, tmp_path)
    b = env["broker"]
    b.auto_fill_price = Decimal("500")
    _cycle(env, day, at(D1, 14, 31))  # SPY rule's entry at the open; too early for the day trader
    assert _ivv(env) == []
    _cycle(env, day, at(D1, 14, 46))  # 09:46 New York: decide, buy, fill
    buy = _ivv(env)[0]
    assert buy.side == "BUY" and buy.frozen_notional == Decimal("250.00") and buy.execution_session == D1
    _cycle(env, day, at(D1, 14, 47))
    assert _ivv(env)[0].state == "FILLED" and b.positions["IVV"] == Decimal("0.5") and _clean(env)
    day.intraday.price = 497.0  # -0.6%: above the stop
    _cycle(env, day, at(D1, 15, 30))
    assert len(_ivv(env)) == 1
    day.intraday.price = 496.0  # -0.8%: stop-loss
    b.auto_fill_price = Decimal("496")
    _cycle(env, day, at(D1, 15, 31))
    sell = _ivv(env)[-1]
    assert sell.side == "SELL" and "stop-loss" in sell.sizing["exit_reason"] and sell.target_qty == Decimal("0.5")
    _cycle(env, day, at(D1, 15, 32))
    assert _ivv(env)[-1].state == "FILLED" and "IVV" not in b.positions and _clean(env)
    with env["db"].session() as s:
        t = day_engine.completed_trades(s, buy.account_key)
        assert len(t) == 1 and Decimal(t[0]["pnl"]) == Decimal("-2.00")
    _cycle(env, day, at(D1, 15, 40))  # no second buy the same day
    assert len(_ivv(env)) == 2


def test_sells_before_the_close_and_never_holds_overnight(env, tmp_path):
    day = _setup(env, tmp_path)
    env["broker"].auto_fill_price = Decimal("500")
    _cycle(env, day, at(D1, 14, 31))
    _cycle(env, day, at(D1, 14, 46))
    day.intraday.price = 502.0
    _cycle(env, day, at(D1, 20, 49))  # 15:49 New York: not yet
    assert len(_ivv(env)) == 1
    _cycle(env, day, at(D1, 20, 50))  # 10 minutes before the 16:00 close
    sell = _ivv(env)[-1]
    assert sell.side == "SELL" and "end of day" in sell.sizing["exit_reason"]
    _cycle(env, day, at(D1, 20, 51))
    assert "IVV" not in env["broker"].positions and _clean(env)


def test_holding_left_from_an_earlier_day_is_sold_at_the_next_open(env, tmp_path):
    day = _setup(env, tmp_path)
    env["broker"].auto_fill_price = Decimal("500")
    _cycle(env, day, at(D1, 14, 31))
    _cycle(env, day, at(D1, 14, 46))
    _cycle(env, day, at(D1, 14, 47))
    # the computer slept through the close; next session (Monday, EDT open 13:30 UTC)
    _cycle(env, day, at(D2, 13, 31))
    sell = _ivv(env)[-1]
    assert sell.side == "SELL" and "left over" in sell.sizing["exit_reason"] and sell.signal_session == D2


def test_switched_off_records_but_never_orders_and_exits_still_run(env, tmp_path):
    day = _setup(env, tmp_path, enabled=False)
    env["broker"].auto_fill_price = Decimal("500")
    _cycle(env, day, at(D1, 14, 31))
    _cycle(env, day, at(D1, 14, 46))
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.symbol == "IVV"))
        assert d.action == "SKIP" and d.inputs["would_action"] == "BUY" and "Switched off" in d.reason
    assert _ivv(env) == []
    # switched on: buy on Monday, then switched off while holding: the end-of-day exit still goes out
    with env["db"].session() as s:
        day_engine.set_settings(s, True, Decimal("250"), Decimal("250"))
    _cycle(env, day, at(D2, 13, 46))
    _cycle(env, day, at(D2, 13, 47))
    with env["db"].session() as s:
        day_engine.set_settings(s, False, Decimal("250"), Decimal("250"))
    _cycle(env, day, at(D2, 19, 50))  # 15:50 New York (EDT)
    _cycle(env, day, at(D2, 19, 51))
    assert _ivv(env)[-1].side == "SELL" and _ivv(env)[-1].state == "FILLED" and "IVV" not in env["broker"].positions


def test_unsure_model_and_news_veto_do_not_buy(env, tmp_path):
    day = _setup(env, tmp_path, bias=-2.0)
    env["broker"].auto_fill_price = Decimal("500")
    _cycle(env, day, at(D1, 14, 31))
    _cycle(env, day, at(D1, 14, 46))
    with env["db"].session() as s:
        assert s.scalar(select(Decision).where(Decision.symbol == "IVV")).action == "HOLD"
        s.add(KV(key="news_mood", value={"available": True, "funds": {"SPY": {"mood": -0.5, "warn": True, "headlines": 10}}}))
    day.model = _model(2.0)
    _cycle(env, day, at(D2, 13, 46))
    with env["db"].session() as s:
        d = s.scalars(select(Decision).where(Decision.symbol == "IVV").order_by(Decision.id)).all()[-1]
        assert d.action == "SKIP" and "News veto" in d.reason
    assert _ivv(env) == []


def test_late_worker_does_not_buy_after_ten(env, tmp_path):
    day = _setup(env, tmp_path)
    _cycle(env, day, at(D1, 15, 5))  # 10:05 New York
    with env["db"].session() as s:
        assert s.scalar(select(Decision).where(Decision.symbol == "IVV")) is None
    assert _ivv(env) == []


def test_self_check_pauses_after_losing_streak_and_resume_resets(env, tmp_path):
    day = _setup(env, tmp_path)
    with env["db"].session() as s:
        akey = s.get(ControlState, 1).account_fingerprint
        for i in range(20):
            t = datetime(2026, 1, 5, 15, 0, tzinfo=UTC) + timedelta(days=i)
            for side, price in (("BUY", Decimal("100")), ("SELL", Decimal("99"))):
                it = OrderIntent(account_key=akey, symbol="IVV", strategy_version_id=1, kind="ENTRY" if side == "BUY" else "EXIT", side=side,
                                 signal_session=t.date(), execution_session=t.date(), attempt=1, client_order_id=f"t{i}{side}", state="FILLED",
                                 created_at=t)
                s.add(it)
                s.flush()
                s.add(Fill(intent_id=it.id, broker_order_id=f"o{i}{side}", fill_key=f"f{i}{side}", side=side, qty=Decimal("1"), price=price,
                           at=t + timedelta(hours=1 if side == "SELL" else 0)))
    assert day.self_check(datetime(2026, 2, 1, tzinfo=UTC))
    with env["db"].session() as s:
        gs = day_engine.get_settings(s)
        assert gs["self_paused"] and "lost 20.00 USD" in gs["self_pause_reason"]
        day_engine.resume(s, datetime(2026, 2, 2, tzinfo=UTC))
    assert not day.self_check(datetime(2026, 2, 3, tzinfo=UTC))  # old trades no longer count
    env["broker"].auto_fill_price = Decimal("500")
    with env["db"].session() as s:
        row = s.get(KV, "day_settings")
        row.value = {**row.value, "self_paused": True, "self_pause_reason": "test"}
    _cycle(env, day, at(D1, 14, 46))
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.symbol == "IVV"))
        assert d.action == "SKIP" and "Self-paused" in d.reason


def test_retrains_outside_market_hours_and_reports_walk_forward(env, tmp_path):
    day = DayEngine(env["db"], env["broker"], env["cal"], env["data"], env["settings"], intraday=FakeIntraday(), model_dir=tmp_path / "m")
    assert day.model is None
    assert not day.maybe_retrain(at(D1, 15, 0))  # market open: never retrain
    now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)  # Saturday
    assert day.maybe_retrain(now)
    m = day.model
    assert m.train_rows >= day_ai.MIN_TRAIN and {"ai", "every_day", "hold_pct"} <= set(m.evaluation)
    assert day_ai.load(tmp_path / "m").trained_through == m.trained_through
    assert not day.maybe_retrain(now + timedelta(days=1))  # fresh model


def test_budget_must_respect_the_per_order_cap(env):
    with env["db"].session() as s:
        try:
            day_engine.set_settings(s, True, Decimal("500"), Decimal("250"))
            raise AssertionError("budget above cap accepted")
        except ValueError:
            pass
        assert position(s, "x", "IVV").held_qty == 0


def test_day_api(tmp_path):
    from fastapi.testclient import TestClient
    from orbit import auth
    from orbit.api import create_app
    from orbit.config import Settings
    from orbit.db import Database, init_db
    from tests.test_api_security import ORIGIN, PW, login
    settings = Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path))
    db = Database(tmp_path / "api.sqlite3")
    init_db(db.engine)
    with db.session() as s:
        auth.set_password(s, PW)
    c = TestClient(create_app(settings, db), base_url=ORIGIN)
    assert c.get("/api/day").status_code == 401
    login(c)
    r = c.get("/api/day").json()
    assert r["settings"]["enabled"] is False and r["model"] is None and r["symbol"] == "IVV"


def test_today_api_requires_login(tmp_path):
    from fastapi.testclient import TestClient
    from orbit import auth
    from orbit.api import create_app
    from orbit.config import Settings
    from orbit.db import Database, init_db
    from tests.test_api_security import ORIGIN, PW, login
    settings = Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path))
    db = Database(tmp_path / "api.sqlite3")
    init_db(db.engine)
    with db.session() as s:
        auth.set_password(s, PW)
    c = TestClient(create_app(settings, db), base_url=ORIGIN)
    assert c.get("/api/today").status_code == 401
    login(c)
    r = c.get("/api/today").json()
    assert r["money"] is None and len(r["strategies"]) == 3 and r["usd_to_aed"]
