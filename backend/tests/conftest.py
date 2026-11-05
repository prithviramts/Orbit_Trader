import os
import warnings
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

# Tests must never see real credentials or touch real services.
for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "NTFY_TOPIC", "NTFY_TOKEN"):
    os.environ.pop(k, None)
os.environ["ORBIT_TESTING"] = "1"
warnings.filterwarnings("ignore")

from orbit.broker.mock import MockBroker  # noqa: E402
from orbit.config import Settings  # noqa: E402
from orbit.data.bars import Bar, BarSeries  # noqa: E402
from orbit.db import Database, init_db  # noqa: E402
from orbit.engine import Engine  # noqa: E402
from orbit.market_calendar import UTC, xnys_calendar  # noqa: E402


class FakeData:
    """Deterministic provider: closes rise (signal INVESTED) unless a trend override is set."""

    def __init__(self):
        self.trend = "up"
        self.fail = None
        self.calls = 0
        self.drop_last = False
        self.mode = "ALPACA"  # stands in for the fetched-data provider in tests

    def fetch(self, symbol, start, end, completed_before, calendar, buffer_minutes):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.fail)
        sess = [s for s in calendar.sessions_between(start, end) if s.close_utc + timedelta(minutes=buffer_minutes) <= completed_before]
        if self.drop_last:
            sess = sess[:-1]
        bars = []
        for i, s in enumerate(sess):
            base = Decimal(400) + Decimal(i) / 10
            if self.trend == "down" and i == len(sess) - 1:
                base = Decimal(300)
            if self.trend == "flat":
                base = Decimal(400)
            bars.append(Bar(s.day, base, base + 1, base - 1, base))
        return BarSeries(symbol, self.mode, "FakeData (test fixture)", "split_adjusted", bars)


@pytest.fixture
def cal():
    return xnys_calendar()


@pytest.fixture
def env(tmp_path, cal):
    db = Database(tmp_path / "t.sqlite3")
    init_db(db.engine)
    broker = MockBroker(cash=Decimal("1000"))
    data = FakeData()
    settings = Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path))
    eng = Engine(db, broker, cal, data, settings)
    return {"db": db, "broker": broker, "data": data, "engine": eng, "cal": cal, "settings": settings}


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC)
