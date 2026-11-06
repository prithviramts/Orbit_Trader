"""Performance page maths and automatic daily backups."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from orbit import autobackup
from orbit.config import Settings
from orbit.db import KV, Database, EventInbox, Fill, init_db
from orbit.performance import compute

UTC = timezone.utc


def _fill(side, qty, price, day):
    return Fill(side=side, qty=Decimal(qty), price=Decimal(price), at=datetime(day.year, day.month, day.day, 14, 31, tzinfo=UTC),
                broker_order_id="x", fill_key=f"{side}{day}")


def test_no_fills_is_unavailable():
    assert compute([], [(date(2026, 1, 2), Decimal(1))])["available"] is False


def test_hand_checked_round_trip_against_buy_and_hold():
    d1, d2, d3, d4 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 4, 1), date(2026, 4, 2)
    fills = [_fill("BUY", "1", "100", d1), _fill("SELL", "1", "110", d3)]
    closes = [(date(2026, 2, 27), Decimal(99)), (d1, Decimal(101)), (d2, Decimal(105)), (d3, Decimal(111)), (d4, Decimal(90))]
    r = compute(fills, closes)
    assert r["available"] and r["sessions"] == 4  # starts on the first fill day
    sm = r["summary"]
    # Strategy: bought 100, sold 110 -> +10 realised, flat afterwards.
    assert sm["strategy_pnl"] == "10.00" and sm["realized"] == "10.00" and sm["unrealized"] == "0.00"
    # Benchmark held 1 share bought at 100; last close 90 -> -10.
    assert sm["benchmark_pnl"] == "-10.00" and sm["difference"] == "20.00"
    assert sm["strategy_pct"] == "10.00" and sm["benchmark_pct"] == "-10.00"
    assert sm["round_trips"] == 1 and sm["wins"] == 1 and r["trades"][0]["pct"] == "10.00"
    assert sm["invested_days"] == 2 and sm["cash_days"] == 2
    assert Decimal(sm["max_drawdown"]) >= 0
    # Monthly changes add up to the total.
    assert sum(Decimal(m["strategy"]) for m in r["months"]) == Decimal(sm["strategy_pnl"])
    assert sum(Decimal(m["benchmark"]) for m in r["months"]) == Decimal(sm["benchmark_pnl"])
    assert r["verdict"]["code"] == "TOO_EARLY"


def test_holding_only_equals_benchmark():
    d1 = date(2026, 11, 5)
    r = compute([_fill("BUY", "0.326432705", "765.824", d1)], [(d1, Decimal("770"))])
    assert r["summary"]["strategy_pnl"] == r["summary"]["benchmark_pnl"] and r["summary"]["difference"] == "0.00"
    assert r["open_trade"]["cost"] == "249.99"


def _settings(tmp_path, **kw):
    return Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path / "data"), **kw)


def _db(st):
    st.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(st.db_path)
    init_db(db.engine)
    return db


def test_daily_backup_once_per_day_after_7_dubai_with_second_copy_and_pruning(tmp_path):
    copy = tmp_path / "onedrive" / "orbit"
    st = _settings(tmp_path, ORBIT_BACKUP_COPY_DIR=str(copy), ORBIT_BACKUP_KEEP=3)
    db = _db(st)
    (st.data_dir / "backups").mkdir(parents=True, exist_ok=True)
    manual = st.data_dir / "backups" / "orbit-20200101T000000Z-manual.sqlite3"
    manual.write_bytes(b"keep me")
    early = datetime(2026, 11, 6, 2, 30, tzinfo=UTC)  # 06:30 Dubai: not yet
    with db.session() as s:
        assert autobackup.maybe_run(s, st, early) is None
    t = datetime(2026, 11, 6, 3, 5, tzinfo=UTC)  # 07:05 Dubai
    for k in range(5):
        now = t + timedelta(days=k)
        with db.session() as s:
            r = autobackup.maybe_run(s, st, now)
            assert r["ok"] and r["copy_ok"] is True, r
        with db.session() as s:  # same day again: nothing
            assert autobackup.maybe_run(s, st, now + timedelta(minutes=5)) is None
    autos = sorted((st.data_dir / "backups").glob("*-auto.sqlite3"))
    assert len(autos) == 3 and len(list(copy.glob("*-auto.sqlite3"))) == 3
    assert manual.exists()  # never pruned


def test_backup_failure_raises_warning_event_and_does_not_retry_same_day(tmp_path, monkeypatch):
    st = _settings(tmp_path)
    db = _db(st)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(autobackup, "backup_database", boom)
    now = datetime(2026, 11, 6, 4, 0, tzinfo=UTC)
    with db.session() as s:
        r = autobackup.maybe_run(s, st, now)
        assert r["ok"] is False and "disk full" in r["error"]
    with db.session() as s:
        assert autobackup.maybe_run(s, st, now + timedelta(minutes=1)) is None
        ev = s.query(EventInbox).filter_by(type="backup_failed").all()
        assert len(ev) == 1 and ev[0].severity == "warning"
        assert s.get(KV, "autobackup_status").value["ok"] is False
