"""MOCK lifecycle demo: drives the real Engine with the in-memory MockBroker and MOCK-labelled data.

Writes ONLY to the database in ORBIT_DATA_DIR (the script points it at data-mock-demo/, never your
real data/). No network, no broker credentials, no real or paper orders. Everything it creates is
labelled MOCK in the dashboard.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

from orbit.broker.mock import MockBroker
from orbit.config import get_settings
from orbit.data.bars import Bar, BarSeries
from orbit.db import Database, init_db
from orbit.engine import Engine, control
from orbit.market_calendar import UTC, xnys_calendar


class MockData:
    def __init__(self):
        self.down_after: date | None = None

    def fetch(self, symbol, start, end, completed_before, calendar, buffer_minutes):
        sess = [s for s in calendar.sessions_between(start, end) if s.close_utc + timedelta(minutes=buffer_minutes) <= completed_before]
        bars = []
        for i, s in enumerate(sess):
            px = Decimal(400) + Decimal(i) / 10
            if self.down_after and s.day >= self.down_after:
                px = Decimal(330)
            bars.append(Bar(s.day, px, px + 1, px - 1, px))
        return BarSeries(symbol, "MOCK", "MOCK demo data (deterministic fixture; not market prices)", "split_adjusted", bars)


def main() -> int:
    st = get_settings()
    if "mock" not in st.data_dir.name.lower():
        print("Refusing: point ORBIT_DATA_DIR at a separate mock directory (scripts/mock-demo.ps1 does this).")
        return 2
    st.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(st.db_path)
    init_db(db.engine)
    cal = xnys_calendar()
    broker = MockBroker(cash=Decimal("1000"), account_id="mock-demo-account")
    data = MockData()
    eng = Engine(db, broker, cal, data, st)
    d0 = date(2026, 3, 5)
    s1, s2, s3 = cal.next_session(d0), cal.next_session(cal.next_session(d0).day), None
    s3 = cal.next_session(s2.day)
    ev0 = cal.session(d0).close_utc + timedelta(minutes=16)
    eng.tick(ev0)  # OBSERVE decision
    with db.session() as s:
        from orbit import controls
        c = control(s)
        c.mode = "PAPER_AUTO"
        c.activated_at = ev0
        from orbit.db import Decision
        from sqlalchemy import select
        dec = s.scalar(select(Decision).order_by(Decision.id.desc()))
        controls._entry_from_decision(s, dec, ev0, cal, "MOCK demo onboarding: flat while signal INVESTED.")
    t = s1.open_utc + timedelta(minutes=1)
    broker.now = t
    eng.tick(t)  # submit entry
    oid = next(iter(broker.orders))
    broker.fill(oid, Decimal("0.30"), Decimal("400.10"))
    eng.tick(t + timedelta(minutes=1))  # partial fill
    broker.fill(oid, Decimal("0.3247"), Decimal("400.20"))
    broker.set_status(oid, "canceled")
    eng.tick(t + timedelta(minutes=2))  # terminal partial -> adopt filled qty
    eng.tick(s1.close_utc + timedelta(minutes=16))  # HOLD
    data.down_after = s2.day
    eng.tick(s2.close_utc + timedelta(minutes=16))  # SELL decision
    t3 = s3.open_utc + timedelta(minutes=1)
    broker.now = t3
    broker.auto_fill_price = Decimal("331.00")
    eng.tick(t3)  # exit submitted and filled
    eng.tick(t3 + timedelta(minutes=1))
    print(f"MOCK demo written to {st.db_path}. Start the dashboard on it with scripts/mock-demo.ps1 -Serve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
