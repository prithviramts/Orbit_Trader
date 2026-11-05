from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from orbit import auth
from orbit import forecasts as fc
from orbit.api import create_app
from orbit.config import Settings
from orbit.data.bars import Bar, bars_to_json
from orbit.data.live import LiveMarketData
from orbit.db import Database, Dataset, Forecast, init_db
from orbit.engine import Engine  # noqa: F401  (import check: engine must not depend on forecasts)
from orbit.market_calendar import xnys_calendar

ORIGIN = "http://127.0.0.1:8765"
PW = "correct horse battery staple"
H = {"Origin": ORIGIN}
UTC = timezone.utc


def test_target_is_next_unopened_session():
    cal = xnys_calendar()
    # Thu 2026-11-05 10:00 UTC (before the 14:30 UTC open): target is today, base is Wednesday
    t, b = fc.target_for(cal, datetime(2026, 11, 5, 10, 0, tzinfo=UTC))
    assert (t.day, b.day) == (date(2026, 11, 5), date(2026, 11, 4))
    # during the session: target moves to Friday
    t, b = fc.target_for(cal, datetime(2026, 11, 5, 15, 0, tzinfo=UTC))
    assert (t.day, b.day) == (date(2026, 11, 6), date(2026, 11, 5))
    # Saturday: target is Monday, compared with Friday's close
    t, b = fc.target_for(cal, datetime(2026, 11, 7, 12, 0, tzinfo=UTC))
    assert (t.day, b.day) == (date(2026, 11, 9), date(2026, 11, 6))


def _f(direction, base, target):
    return SimpleNamespace(direction=direction, base_session=base, target_session=target, outcome=None,
                           correct=None, base_close=None, outcome_close=None, scored_at=None)


def test_scoring_and_baseline():
    d1, d2, d3, d4 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)
    closes = {d1: Decimal("100"), d2: Decimal("101"), d3: Decimal("100.5"), d4: Decimal("100.5")}
    rows = [_f("UP", d1, d2), _f("UP", d2, d3), _f("DOWN", d3, d4), _f("UP", d4, date(2026, 11, 6))]
    assert fc.score_pending(rows, closes, datetime(2026, 11, 6, tzinfo=UTC)) == 3
    assert [r.outcome for r in rows] == ["UP", "DOWN", "FLAT", None]
    assert [r.correct for r in rows] == [True, False, None, None]
    sc = fc.scorecard(rows)
    assert sc["scored"] == 2 and sc["hits"] == 1 and sc["accuracy"] == 0.5
    assert sc["always_up_accuracy"] == 0.5 and sc["pending"] == 1 and sc["flat"] == 1
    assert "Too early" in sc["verdict"]


def test_coin_flip_p_value():
    assert fc._p_at_least(0, 10) == 1.0
    assert fc._p_at_least(10, 10) == pytest.approx(1 / 1024)


class FakeNews:
    def get_news(self, req):
        n = SimpleNamespace(id=1, headline="  Fed  holds rates ", summary="x" * 900, source="benzinga", author="A",
                            url="javascript:alert(1)", created_at=datetime(2026, 11, 5, 14, 0, tzinfo=UTC), symbols=["SPY"])
        return SimpleNamespace(data={"news": [n]})


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path))
    db = Database(tmp_path / "api.sqlite3")
    init_db(db.engine)
    with db.session() as s:
        auth.set_password(s, PW)
    app = create_app(settings, db)
    app.state.live = LiveMarketData(settings, news_client=FakeNews())
    c = TestClient(app, base_url=ORIGIN)
    r = c.post("/api/auth/login", json={"password": PW}, headers=H)
    csrf = r.json()["csrf_token"]
    return c, db, {**H, "X-CSRF-Token": csrf}, monkeypatch


def test_news_endpoint_sanitises(client):
    c, *_ = client
    r = c.get("/api/news").json()
    item = r["items"][0]
    assert item["headline"] == "Fed holds rates"
    assert item["url"] == ""  # non-https links are dropped
    assert len(item["summary"]) <= 600
    assert "never triggers orders" in r["note"]


def test_forecast_rules_end_to_end(client):
    c, db, hdr, mp = client
    before_open = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    mp.setattr("orbit.api.now_utc", lambda: before_open)
    assert c.post("/api/forecasts", json={"direction": "UP", "confidence": 70, "reason": "Fed calm"}).status_code == 403  # no CSRF
    r = c.post("/api/forecasts", json={"direction": "SIDEWAYS"}, headers=hdr)
    assert r.status_code == 422
    r = c.post("/api/forecasts", json={"direction": "UP", "confidence": 70, "reason": "Fed calm"}, headers=hdr)
    assert r.status_code == 200 and r.json()["target_session"] == "2026-11-05"
    r = c.post("/api/forecasts", json={"direction": "DOWN"}, headers=hdr)
    assert r.status_code == 409  # one per session, no changing your mind
    # after the open, the call goes to the next session instead
    mp.setattr("orbit.api.now_utc", lambda: datetime(2026, 11, 5, 15, 0, tzinfo=UTC))
    r = c.post("/api/forecasts", json={"direction": "DOWN"}, headers=hdr)
    assert r.status_code == 200 and r.json()["target_session"] == "2026-11-06"
    # scoring from stored Alpaca daily closes
    with db.session() as s:
        bars = [Bar(date(2026, 11, 4), Decimal(1), Decimal(1), Decimal(1), Decimal("766.73"), Decimal(1)),
                Bar(date(2026, 11, 5), Decimal(1), Decimal(1), Decimal(1), Decimal("770.10"), Decimal(1))]
        s.add(Dataset(symbol="SPY", mode="ALPACA", source="test", price_basis="split_adjusted", bars_json=bars_to_json(bars),
                      fingerprint="t", describe={}))
    g = c.get("/api/forecasts").json()
    first = [f for f in g["items"] if f["target_session"] == "2026-11-05"][0]
    assert first["outcome"] == "UP" and first["correct"] is True
    assert g["score"]["scored"] == 1 and g["next"]["target_session"] == "2026-11-06"
    with db.session() as s:
        assert len(s.scalars(select(Forecast)).all()) == 2


def test_migration_from_v1_adds_forecast_table_with_backup(tmp_path):
    from sqlalchemy import inspect, text

    path = tmp_path / "old.sqlite3"
    db = Database(path)
    init_db(db.engine)
    with db.engine.begin() as con:
        con.execute(text("DROP TABLE forecast"))
        con.execute(text("DELETE FROM schema_version"))
        con.execute(text("INSERT INTO schema_version (version, note, applied_at) VALUES (1, 'initial schema', '2026-11-05 00:00:00')"))
    init_db(db.engine, tmp_path / "backups", path)
    assert inspect(db.engine).has_table("forecast")
    assert list((tmp_path / "backups").glob("*pre-migrate-v1*"))
    with db.engine.connect() as con:
        assert con.execute(text("SELECT max(version) FROM schema_version")).scalar() == 2


def test_engine_never_reads_forecasts():
    import inspect as pyinspect

    import orbit.engine
    assert "forecast" not in pyinspect.getsource(orbit.engine).lower()
