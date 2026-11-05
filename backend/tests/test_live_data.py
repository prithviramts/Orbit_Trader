from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from orbit import auth
from orbit.api import create_app
from orbit.config import Settings
from orbit.data.live import LiveMarketData
from orbit.db import Database, init_db

ORIGIN = "http://127.0.0.1:8765"
PW = "correct horse battery staple"
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


class FakeDataClient:
    def __init__(self):
        self.bar_calls = 0

    def get_stock_bars(self, req):
        self.bar_calls += 1
        b = SimpleNamespace(timestamp=datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc), open=760.1, high=761.0, low=759.5, close=760.8, volume=1200)
        return SimpleNamespace(data={"SPY": [b]})

    def get_stock_latest_trade(self, req):
        return {"SPY": SimpleNamespace(price=760.9, timestamp=datetime(2026, 11, 5, 14, 59, tzinfo=timezone.utc))}


def test_candles_shape_and_cache():
    fake = FakeDataClient()
    live = LiveMarketData(Settings(_env_file=None), client=fake)
    a = live.candles("SPY", "5Min", NOW)
    b = live.candles("SPY", "5Min", NOW)
    assert fake.bar_calls == 1  # second call served from the short cache
    assert a is b
    assert a["candles"][0] == {"t": int(datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc).timestamp()), "o": 760.1, "h": 761.0, "l": 759.5, "c": 760.8, "v": 1200.0}
    assert a["latest_trade"]["price"] == 760.9
    assert "Display only" in a["note"]


def test_live_data_has_no_trading_client():
    live = LiveMarketData(Settings(_env_file=None), client=FakeDataClient())
    assert not any("trading" in type(v).__name__.lower() for v in vars(live).values())


def _client(tmp_path, live_client=None):
    settings = Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path))
    db = Database(tmp_path / "api.sqlite3")
    init_db(db.engine)
    with db.session() as s:
        auth.set_password(s, PW)
    app = create_app(settings, db)
    if live_client is not None:
        app.state.live = LiveMarketData(settings, client=live_client)
    c = TestClient(app, base_url=ORIGIN)
    return c


def test_live_endpoint_requires_login_and_validates(tmp_path):
    c = _client(tmp_path, FakeDataClient())
    assert c.get("/api/market/live").status_code == 401
    c.post("/api/auth/login", json={"password": PW}, headers={"Origin": ORIGIN})
    assert c.get("/api/market/live?tf=7Min").status_code == 400
    r = c.get("/api/market/live?tf=1Min").json()
    assert r["available"] is True and r["timeframe"] == "1Min" and len(r["candles"]) == 1
    assert "market" in r


def test_live_endpoint_without_credentials_explains(tmp_path):
    c = _client(tmp_path)
    c.post("/api/auth/login", json={"password": PW}, headers={"Origin": ORIGIN})
    r = c.get("/api/market/live").json()
    assert r["available"] is False and "credentials" in r["reason"]
