"""Gold AI experiment: model maths, shadow mode, paper trading through the shared lifecycle, retraining."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from sqlalchemy import select

from orbit import gold_ai, gold_engine
from orbit.db import ControlState, Decision, OrderIntent, StrategyPosition
from orbit.engine import Engine, position
from orbit.gold_engine import GoldEngine
from tests.conftest import at
from tests.test_engine import D0, D1, D2, EVAL, activate

D3 = date(2026, 3, 10)


def _model(bias: float) -> gold_ai.GoldModel:
    n = len(gold_ai.KEYS)
    return gold_ai.GoldModel([bias] + [0.0] * n, [0.0] * n, [1.0] * n, "2026-02-27", 2000, trained_at="2026-03-01T00:00:00Z")


class FakeHistory:
    def __init__(self):
        self.calls = 0

    def daily(self, symbol, start, end):
        self.calls += 1
        rng = np.random.default_rng(7 if symbol == "GLD" else 8)
        out, d, px = [], date(2016, 1, 4), 100.0
        while d < end.date():
            if d.weekday() < 5:
                px *= float(np.exp(rng.normal(0.0003, 0.01)))
                out.append((d, px, px))
            d += timedelta(days=1)
        return out


def _gold(env, tmp_path, bias=2.0):
    env["engine"] = Engine(env["db"], env["broker"], env["cal"], env["data"], env["settings"], also_manage=("GLD",))
    g = GoldEngine(env["db"], env["broker"], env["cal"], env["data"], env["settings"], history=FakeHistory(), model_dir=tmp_path / "m")
    g.model = _model(bias)
    return g


def _cycle(env, gold, now):
    env["broker"].now = now
    ctx = env["engine"].tick(now)
    gold.tick_after(ctx)
    return ctx


def _gold_intents(env):
    with env["db"].session() as s:
        return list(s.scalars(select(OrderIntent).where(OrderIntent.symbol == "GLD").order_by(OrderIntent.id)))


def test_signals_use_only_past_data_and_policy_has_hysteresis():
    rng = np.random.default_rng(3)
    g = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    s = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    a = gold_ai.signals(g, s)
    g2 = g.copy()
    g2[300:] *= 1.5  # change the future
    b = gold_ai.signals(g2, s)
    assert np.allclose(a[:300], b[:300], equal_nan=True) and a.shape[1] == len(gold_ai.KEYS)
    assert not np.isnan(a[-1]).any()
    assert gold_ai.policy(0.56, False) == "BUY" and gold_ai.policy(0.50, False) == "HOLD"
    assert gold_ai.policy(0.50, True) == "HOLD" and gold_ai.policy(0.44, True) == "SELL"
    p, parts = _model(0.0).predict(a[-1])
    assert abs(p - 0.5) < 1e-9 and len(parts) == len(gold_ai.KEYS)


def test_shadow_mode_predicts_but_never_orders(env, tmp_path):
    gold = _gold(env, tmp_path)
    activate(env)
    _cycle(env, gold, EVAL + timedelta(minutes=1))
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.symbol == "GLD"))
        assert d.action == "SKIP" and d.inputs["would_action"] == "BUY" and "Shadow mode" in d.reason
        assert d.inputs["probability"] > 0.85 and len(d.inputs["signals"]) == len(gold_ai.KEYS)
    assert _gold_intents(env) == []


def test_enabled_ai_buys_with_budget_then_sells_and_reconciles_cleanly(env, tmp_path):
    gold = _gold(env, tmp_path)
    activate(env)
    _cycle(env, gold, EVAL + timedelta(minutes=1))  # D0 decision in shadow mode
    with env["db"].session() as s:
        gold_engine.set_settings(s, True, Decimal("100"), Decimal("250"))
    b = env["broker"]
    b.auto_fill_price = Decimal("400")
    _cycle(env, gold, at(D1, 14, 31))  # SPY onboarding entry executes
    _cycle(env, gold, at(D1, 21, 16))  # gold AI decides on D1 close: BUY
    it = _gold_intents(env)[0]
    assert it.side == "BUY" and it.frozen_notional == Decimal("100.00") and it.execution_session == D2
    b.auto_fill_price = Decimal("200")
    _cycle(env, gold, at(D2, 13, 31))  # Monday open (EDT): gold order submitted and filled
    _cycle(env, gold, at(D2, 13, 32))  # next cycle reconciles both holdings
    it = _gold_intents(env)[0]
    assert it.state == "FILLED"
    with env["db"].session() as s:
        assert position(s, it.account_key, "GLD").held_qty == Decimal("0.5")
        assert position(s, it.account_key, "SPY").held_qty > 0
        c = s.get(ControlState, 1)
        assert not c.security_paused and not c.review_required
    assert b.positions["GLD"] == Decimal("0.5")
    gold.model = _model(-2.0)
    _cycle(env, gold, at(D2, 20, 16))  # D2 close: SELL
    sell = _gold_intents(env)[-1]
    assert sell.side == "SELL" and sell.target_qty == Decimal("0.5") and sell.execution_session == D3
    _cycle(env, gold, at(D3, 13, 31))
    _cycle(env, gold, at(D3, 13, 32))
    assert _gold_intents(env)[-1].state == "FILLED" and "GLD" not in b.positions
    with env["db"].session() as s:
        assert not s.get(ControlState, 1).security_paused


def test_global_pause_blocks_gold_buys(env, tmp_path):
    gold = _gold(env, tmp_path)
    activate(env)
    with env["db"].session() as s:
        gold_engine.set_settings(s, True, Decimal("100"), Decimal("250"))
        s.get(ControlState, 1).new_orders_paused = True
    _cycle(env, gold, EVAL + timedelta(minutes=1))
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.symbol == "GLD"))
        assert d.action == "SKIP" and "blocked" in d.reason
    assert _gold_intents(env) == []


def test_budget_validation_and_retraining(env, tmp_path):
    with env["db"].session() as s:
        try:
            gold_engine.set_settings(s, True, Decimal("500"), Decimal("250"))
            raise AssertionError("budget above cap accepted")
        except ValueError:
            pass
    g = GoldEngine(env["db"], env["broker"], env["cal"], env["data"], env["settings"], history=FakeHistory(), model_dir=tmp_path / "m")
    assert g.model is None
    now = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)  # Saturday: far from any open
    assert g.maybe_retrain(now)
    assert g.model and g.model.train_rows >= gold_ai.MIN_TRAIN and "model" in g.model.evaluation
    assert gold_ai.load(tmp_path / "m").trained_through == g.model.trained_through
    assert not g.maybe_retrain(now + timedelta(days=1))  # fresh model: no retrain
    open_now = datetime(2026, 4, 6, 13, 40, tzinfo=timezone.utc)  # within 30 min after an open
    assert not g.maybe_retrain(open_now)


def test_gold_api_and_settings_switch(tmp_path):
    from tests.test_api_security import H, login, ORIGIN, PW  # noqa: F401
    from fastapi.testclient import TestClient
    from orbit import auth
    from orbit.api import create_app
    from orbit.config import Settings
    from orbit.db import Database, init_db
    settings = Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path))
    db = Database(tmp_path / "api.sqlite3")
    init_db(db.engine)
    with db.session() as s:
        auth.set_password(s, PW)
    c = TestClient(create_app(settings, db), base_url=ORIGIN)
    csrf = login(c)
    r = c.get("/api/gold").json()
    assert r["settings"] == {"enabled": False, "budget_usd": "100"} and r["model"] is None and r["latest"] is None
    assert c.post("/api/gold/settings", json={"enabled": True, "budget_usd": "120"}, headers={"Origin": ORIGIN}).status_code == 403
    ok = c.post("/api/gold/settings", json={"enabled": True, "budget_usd": "120"}, headers=H(csrf))
    assert ok.status_code == 200 and ok.json()["settings"]["enabled"] is True
    assert c.post("/api/gold/settings", json={"enabled": True, "budget_usd": "900"}, headers=H(csrf)).status_code == 422
    assert c.get("/api/performance?symbol=GLD").status_code == 200


def test_overview_and_chart_include_gold(tmp_path):
    from tests.test_api_security import login, ORIGIN, PW
    from fastapi.testclient import TestClient
    from orbit import auth
    from orbit.api import create_app
    from orbit.config import Settings
    from orbit.db import Database, init_db
    settings = Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path))
    db = Database(tmp_path / "api.sqlite3")
    init_db(db.engine)
    with db.session() as s:
        auth.set_password(s, PW)
    c = TestClient(create_app(settings, db), base_url=ORIGIN)
    login(c)
    ov = c.get("/api/overview").json()
    assert ov["gold"]["settings"]["enabled"] is False and ov["gold"]["held_qty"] == "0" and ov["gold"]["latest"] is None
    g = c.get("/api/market/chart?source=auto&symbol=GLD").json()
    assert g["available"] is False and "gold" in g["reason"].lower()
    assert c.get("/api/market/chart?symbol=BTC").status_code == 400
    assert c.get("/api/market/live?tf=1Day&symbol=BTC").status_code == 400


def test_disabling_gold_after_planning_blocks_submission(env, tmp_path):
    gold = _gold(env, tmp_path)
    activate(env)
    with env["db"].session() as s:
        gold_engine.set_settings(s, True, Decimal("100"), Decimal("250"))
    _cycle(env, gold, EVAL + timedelta(minutes=1))
    intent, = _gold_intents(env)
    assert intent.state == "PENDING"
    with env["db"].session() as s:
        gold_engine.set_settings(s, False, Decimal("100"), Decimal("250"))
    _cycle(env, gold, at(D1, 14, 31))
    intent, = _gold_intents(env)
    assert intent.state == "SKIPPED"
    assert "automation disabled" in intent.reason
    assert env["broker"].get_order_by_client_id(intent.client_order_id) is None
