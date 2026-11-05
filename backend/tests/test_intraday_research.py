from datetime import date, datetime, timedelta, timezone

import pytest

from orbit.market_calendar import xnys_calendar
from orbit.research import intraday as ix

UTC = timezone.utc
D = date(2026, 11, 4)  # a regular session: 14:30-21:00 UTC
OPEN = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


def bars_from(closes, vol=100.0, lows=None, opens=None):
    out = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else (closes[i - 1] if i else c)
        lo = lows[i] if lows else min(o, c)
        out.append(ix.MinuteBar(OPEN + timedelta(minutes=i), o, max(o, c), lo, c, vol))
    return out


def test_group_sessions_drops_extended_hours_and_holidays():
    cal = xnys_calendar()
    pre = ix.MinuteBar(datetime(2026, 11, 4, 13, 0, tzinfo=UTC), 1, 1, 1, 1, 1)
    reg = bars_from([100.0] * 40)
    holiday = ix.MinuteBar(datetime(2026, 11, 26, 15, 0, tzinfo=UTC), 1, 1, 1, 1, 1)  # Thanksgiving
    days = ix.group_sessions([pre, holiday, *reg], cal)
    assert [d.day for d in days] == [D] and len(days[0].bars) == 40


def test_fills_are_next_bar_open_with_slippage():
    # 40 flat minutes then a dip at minute 40 (after 10:00 ET = 15:00 UTC) and recovery
    closes = [100.0] * 40 + [99.0, 99.0, 100.5] + [100.5] * 20
    day = ix.Day(D, bars_from(closes))
    r = ix.vwap_dip([day], k=0.005, stop_pct=0.02, slip=0.0001)
    t = r.trades[0]
    assert t.entry_ts == day.bars[41].ts  # signal on bar 40's close -> fill at bar 41's open
    assert t.entry_px == pytest.approx(day.bars[41].o * 1.0001)
    assert t.reason == "target"
    assert t.exit_px == pytest.approx(day.bars[43].o * 0.9999)  # exit signal on bar 42 -> bar 43 open


def test_stop_gap_fills_at_open_not_stop_price():
    closes = [100.0] * 40 + [99.0, 99.0] + [90.0] * 20
    opens = [None] * len(closes)
    opens = [closes[i - 1] if i else closes[0] for i in range(len(closes))]
    opens[42] = 91.0  # gaps far below a 1% stop
    lows = [min(opens[i], closes[i]) for i in range(len(closes))]
    day = ix.Day(D, bars_from(closes, opens=opens, lows=lows))
    r = ix.vwap_dip([day], k=0.005, stop_pct=0.01, slip=0.0)
    t = r.trades[0]
    assert t.reason == "stop" and t.exit_px == pytest.approx(91.0)  # worse than the 98.01 stop: honest


def test_forced_flat_by_close_and_no_late_entries():
    closes = [100.0] * 361 + [99.0] * 29  # dip only after 15:30 ET (minute 361 = 15:31)
    day = ix.Day(D, bars_from(closes))
    assert ix.vwap_dip([day], 0.005, 0.02, 0.0).trades == []
    closes = [100.0] * 40 + [99.0] * 350  # dip early and never recovers
    day = ix.Day(D, bars_from(closes))
    r = ix.vwap_dip([day], 0.005, 0.5, 0.0)
    assert r.trades[-1].reason == "close" and r.trades[-1].exit_ts == day.bars[-1].ts


def test_costs_make_benchmark_lose_on_flat_day():
    day = ix.Day(D, bars_from([100.0] * 60))
    m = ix.metrics(ix.open_to_close([day], slip=0.0002))
    assert m["total_return"] < 0 and m["trades"] == 1


def test_split_keeps_holdout_separate():
    a, b = ix.Day(date(2023, 12, 29), []), ix.Day(date(2024, 1, 2), [])
    dev, hold = ix.split([a, b], date(2024, 1, 1))
    assert dev == [a] and hold == [b]


def test_research_is_not_imported_by_trading_code():
    import inspect

    import orbit.engine
    import orbit.worker
    for mod in (orbit.engine, orbit.worker):
        assert "orbit.research" not in inspect.getsource(mod)
