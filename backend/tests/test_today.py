"""Today screen: plain-English summary of money, strategies, today's orders and problems. Read-only."""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from orbit import today
from orbit.db import ControlState, EventInbox, OrderIntent
from orbit.notify import emit
from tests.conftest import at
from tests.test_engine import D1, activate

RUNNING = {"state": "RUNNING", "text": ""}


def test_today_summarises_money_strategies_and_orders(env):
    activate(env)
    b = env["broker"]
    b.auto_fill_price = Decimal("500")
    b.now = at(D1, 14, 31)
    env["engine"].tick(b.now)  # SPY entry at the open
    b.now = at(D1, 14, 32)
    env["engine"].tick(b.now)
    with env["db"].session() as s:
        for e in s.scalars(select(EventInbox)):  # the owner has read the setup alerts
            e.read_at = e.at
        t = today.build(s, env["cal"], at(D1, 15, 0), RUNNING)
        assert t["level"] == "ok" and t["headline"].startswith("All good")
        assert t["money"]["equity"] and t["money"]["holdings"][0]["symbol"] == "SPY"
        assert [x["symbol"] for x in t["strategies"]] == ["SPY", "GLD", "IVV"]
        spy = t["strategies"][0]
        assert spy["status"] == "on" and float(spy["held_qty"]) > 0 and spy["latest"]["summary"][0].isupper()
        assert t["orders"]["day"] == D1.isoformat() and t["orders"]["items"][0]["side"] == "BUY"
        assert any("open until" in n for n in t["next"])
        assert s.scalars(select(OrderIntent)).all()  # nothing created or changed by building the screen
        n_before = len(s.scalars(select(OrderIntent)).all())
        today.build(s, env["cal"], at(D1, 15, 1), RUNNING)
        assert len(s.scalars(select(OrderIntent)).all()) == n_before


def test_today_flags_problems_in_plain_words(env):
    activate(env)
    with env["db"].session() as s:
        s.get(ControlState, 1).security_paused = True
        s.get(ControlState, 1).security_pause_reason = "Broker IVV qty 1 vs strategy-confirmed qty 0."
        emit(s, "worker_stopped", "warning", "Trading worker stopped", "x", "ws1", settings=env["settings"])
        emit(s, "reconciliation_failed", "warning", "Broker unreachable; reconciliation failed", "x", "rf1", settings=env["settings"])
        now = s.scalars(select(EventInbox)).all()[-1].at + timedelta(minutes=1)
        t = today.build(s, env["cal"], now, RUNNING)
        assert t["level"] == "bad" and "attention" in t["headline"]
        assert any("paused itself for safety" in a["text"] for a in t["attention"])
        titles = [a["title"] for a in t["alerts"]]
        assert "Broker unreachable; reconciliation failed" in titles and "Trading worker stopped" not in titles  # restart stop is hidden
        t2 = today.build(s, env["cal"], now, {"state": "STOPPED", "text": "Worker stopped."})
        assert any("not running" in a["text"] for a in t2["attention"])
