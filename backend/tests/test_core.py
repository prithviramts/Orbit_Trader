"""Money, strategy, accounting fixture, risk overlay and backtest invariants."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from orbit.accounting import AccountingError, Ledger
from orbit.backtest import CostModel, run_backtest
from orbit.data.bars import Bar, BarSeries, DataQualityError, SplitEvent, validate_bars
from orbit.data.sources import generate_sample_series, parse_csv_import
from orbit.market_calendar import NEW_YORK, UTC, xnys_calendar
from orbit.money import aed_to_usd, floor_qty, money_pair, round_money, usd_to_aed
from orbit.risk import DrawdownState, RiskPolicy, update_drawdown
from orbit.strategy import CASH, INSUFFICIENT, INVESTED, StrategyParams, entry_notional, evaluate_signal

R = Decimal("3.6725")


# ------------------------------------------------------------------ money
@pytest.mark.parametrize("usd,aed", [
    ("1000", "3672.50"), ("250", "918.13"), ("10", "36.73"), ("100", "367.25"), ("1", "3.67"),
    ("110", "403.98"), ("908", "3334.63"), ("100.50", "369.09"), ("8.50", "31.22"), ("9.50", "34.89"),
    ("1018", "3738.61"), ("18", "66.11"), ("-8.50", "-31.22"), ("-18", "-66.11"), ("0.005", "0.02"),
])
def test_usd_aed_reference_conversion(usd, aed):
    assert money_pair(Decimal(usd), R)["aed"] == aed


def test_exact_reference_values_and_direction():
    assert usd_to_aed(Decimal("250"), R) == Decimal("918.1250")
    assert usd_to_aed(Decimal("10"), R) == Decimal("36.7250")
    assert aed_to_usd(Decimal("3672.50"), R) == Decimal("1000")
    assert round_money(Decimal("918.125")) == Decimal("918.13")  # ROUND_HALF_UP, not banker's
    assert round_money(Decimal("-918.125")) == Decimal("-918.13")


def test_floor_qty_never_rounds_up():
    assert floor_qty(Decimal("0.1234567899")) == Decimal("0.123456789")
    with pytest.raises(ValueError):
        floor_qty(Decimal("-1"))


# ------------------------------------------------------------------ accounting fixture (hand-calculated)
def test_hand_calculated_fifo_fixture():
    """Synthetic example: bypasses the SPY signal; these prices did not occur in a market."""
    led = Ledger(Decimal("1000"))
    led.buy(Decimal(2), Decimal(100), Decimal(1))
    realized = led.sell(Decimal(1), Decimal(110), Decimal(1))
    px = Decimal(110)
    assert led.cash == Decimal("908")
    assert led.cost_basis == Decimal("100.50")
    assert realized == Decimal("8.50") == led.realized_pnl
    assert led.unrealized_pnl(px) == Decimal("9.50")
    assert led.equity(px) == Decimal("1018")
    assert led.equity(px) - Decimal(1000) == Decimal("18") == led.realized_pnl + led.unrealized_pnl(px)
    aed = {k: money_pair(v, R)["aed"] for k, v in {
        "cash": led.cash, "basis": led.cost_basis, "realized": realized,
        "unrealized": led.unrealized_pnl(px), "equity": led.equity(px), "total": Decimal(18)}.items()}
    assert aed == {"cash": "3334.63", "basis": "369.09", "realized": "31.22", "unrealized": "34.89",
                   "equity": "3738.61", "total": "66.11"}


def test_ledger_blocks_borrowing_and_shorting():
    led = Ledger(Decimal("100"))
    with pytest.raises(AccountingError):
        led.buy(Decimal(1), Decimal(100), Decimal("0.01"))  # needs 100.01
    led.buy(Decimal("0.5"), Decimal(100))
    with pytest.raises(AccountingError):
        led.sell(Decimal("0.500000001"), Decimal(100))
    assert led.cash >= 0 and led.qty >= 0


def test_split_keeps_basis():
    led = Ledger(Decimal(1000))
    led.buy(Decimal(2), Decimal(100), Decimal(0))
    led.apply_split(Decimal(4))
    assert led.qty == 8 and led.cost_basis == 200
    assert led.equity(Decimal(25)) == Decimal(1000)


# ------------------------------------------------------------------ strategy
def _closes(vals, start=date(2024, 1, 2)):
    cal = xnys_calendar()
    sess = cal.sessions_between(start, start + timedelta(days=800))[: len(vals)]
    return [(s.day, Decimal(str(v))) for s, v in zip(sess, vals)]


def test_warmup_requires_200_closes_including_current():
    c = _closes([100] * 199 + [101])
    r = evaluate_signal(c[:199], c[198][0], CASH)
    assert r.target == INSUFFICIENT and r.window_count == 199
    r = evaluate_signal(c, c[199][0], CASH)
    assert r.window_count == 200 and r.target == INVESTED
    assert r.sma == (Decimal(100) * 199 + 101) / 200  # current close included


def test_equality_keeps_previous_state_and_initial_is_cash():
    c = _closes([100] * 200)
    assert evaluate_signal(c, c[-1][0], CASH).target == CASH
    assert evaluate_signal(c, c[-1][0], INVESTED).target == INVESTED
    assert evaluate_signal(c, c[-1][0], "garbage").target == CASH


def test_strictly_below_goes_cash():
    c = _closes([100] * 199 + [99])
    assert evaluate_signal(c, c[-1][0], INVESTED).target == CASH


def test_no_future_prices_used():
    c = _closes([100] * 200 + [1] * 50)
    a = evaluate_signal(c[:200], c[199][0], CASH)
    b = evaluate_signal(c, c[199][0], CASH)  # later crash present in input but must be ignored
    assert a == b


def test_missing_current_bar_never_substituted():
    c = _closes([100] * 201)
    r = evaluate_signal(c[:200], c[200][0], CASH)  # session 201 has no bar yet
    assert r.target == INSUFFICIENT and r.close is None


def test_entry_sizing_limits():
    p = StrategyParams()
    s = entry_notional(Decimal(1000), Decimal(1000), p)
    assert s["target"] == Decimal(250) and s["limited_by"] == "fraction+cap"
    s = entry_notional(Decimal(100000), Decimal(100000), p)
    assert s["target"] == Decimal(250) and s["limited_by"] == "cap"
    s = entry_notional(Decimal(400), Decimal(400), p)
    assert s["target"] == Decimal(100) and s["limited_by"] == "fraction"
    s = entry_notional(Decimal(1000), Decimal(50), p)
    assert s["target"] == Decimal(40) and s["limited_by"] == "cash"
    s = entry_notional(Decimal(1000), Decimal(10.5), p)
    assert s["too_small"]


def test_params_validation_rejects_other_symbols():
    with pytest.raises(ValueError):
        StrategyParams(symbol="TSLA").validate()


# ------------------------------------------------------------------ risk overlay
def test_drawdown_warn_and_latch_never_self_resets():
    st, pol = DrawdownState(), RiskPolicy()
    assert update_drawdown(st, Decimal(1000), pol) == []
    assert update_drawdown(st, Decimal(950), pol) == ["WARN"]
    assert update_drawdown(st, Decimal(900), pol) == ["PAUSE"]
    assert update_drawdown(st, Decimal(1100), pol) == []
    assert st.entries_paused and st.high_water_mark == 1100


# ------------------------------------------------------------------ calendar / time
def test_calendar_holidays_early_close_and_dst():
    cal = xnys_calendar()
    assert not cal.is_session(date(2026, 11, 26))  # Thanksgiving
    s = cal.session(date(2026, 11, 27))
    assert s.early_close and s.close_utc.astimezone(NEW_YORK).hour == 13
    # 09:30 New York is 13:30 UTC in summer (EDT) and 14:30 UTC in winter (EST)
    assert cal.session(date(2026, 3, 6)).open_utc.hour == 14
    assert cal.session(date(2026, 3, 9)).open_utc.hour == 13
    assert cal.next_session(date(2026, 11, 25)).day == date(2026, 11, 27)
    assert cal.next_session(date(2026, 12, 31)).day == date(2027, 1, 4)  # weekend + New Year holiday


def test_latest_completed_session_respects_buffer():
    cal = xnys_calendar()
    s = cal.session(date(2026, 11, 27))
    assert cal.latest_completed_session(s.close_utc + timedelta(minutes=14)).day == date(2026, 11, 25)
    assert cal.latest_completed_session(s.close_utc + timedelta(minutes=15)).day == date(2026, 11, 27)


# ------------------------------------------------------------------ data validation
def test_duplicate_and_nonpositive_bars_rejected():
    b = Bar(date(2026, 1, 5), Decimal(1), Decimal(1), Decimal(1), Decimal(1))
    with pytest.raises(DataQualityError):
        validate_bars([b, b])
    with pytest.raises(DataQualityError):
        validate_bars([Bar(date(2026, 1, 5), Decimal(0), Decimal(1), Decimal(0), Decimal(1))])
    with pytest.raises(DataQualityError):
        validate_bars([Bar(date(2026, 1, 3), Decimal(1), Decimal(1), Decimal(1), Decimal(1))], xnys_calendar())


def test_missing_bars_reported_not_filled():
    cal = xnys_calendar()
    sess = cal.sessions_between(date(2026, 1, 5), date(2026, 1, 9))
    bars = [Bar(s.day, Decimal(1), Decimal(1), Decimal(1), Decimal(1)) for s in sess if s.day != date(2026, 1, 7)]
    rep = validate_bars(bars, cal)
    assert rep["missing_sessions"] == ["2026-01-07"]


def test_csv_import_limits():
    with pytest.raises(DataQualityError):
        parse_csv_import(b"x" * (5 * 1024 * 1024 + 1), "SPY", "raw", "big file")
    with pytest.raises(DataQualityError):
        parse_csv_import(b"date,open,high,low,close\n", "TSLA", "raw", "other")
    ok = parse_csv_import(b"date,open,high,low,close\n2026-01-05,1,2,1,1.5\n", "spy", "raw", "test <b>source</b>")
    assert ok.mode == "CSV_IMPORT" and ok.source.startswith("CSV import:")


# ------------------------------------------------------------------ backtest
def _series(prices, splits=(), basis="split_adjusted", opens=None):
    cal = xnys_calendar()
    sess = cal.sessions_between(date(2020, 1, 2), date(2028, 1, 1))[: len(prices)]
    bars = []
    for i, (s, p) in enumerate(zip(sess, prices)):
        o = Decimal(str(opens[i])) if opens else Decimal(str(p))
        c = Decimal(str(p))
        bars.append(Bar(s.day, o, max(o, c), min(o, c), c))
    return BarSeries("SPY", "SAMPLE", "test", basis, bars, list(splits))


def test_backtest_next_session_timing_and_sizing():
    prices = [100] * 199 + [101, 102, 103]
    res = run_backtest(_series(prices), costs=CostModel(Decimal(0), Decimal(0)))
    strat = res["portfolios"]["strategy"]
    assert strat["trades"][0]["day"] == res["decisions"][1]["session"]  # signal on day 200 close, fill next session
    assert res["decisions"][0]["action"] == "BUY"  # first full window (200th close = 101 > SMA)
    flat = run_backtest(_series([100] * 205))
    assert {d["action"] for d in flat["decisions"]} == {"HOLD"}  # equality from initial CASH -> no trade
    assert flat["portfolios"]["strategy"]["trades"] == []
    buy = strat["trades"][0]
    assert Decimal(buy["notional"]) <= 250 and Decimal(buy["frozen_target"]) == Decimal(250)
    assert Decimal(strat["metrics"]["ending_equity"]) > 0


def test_backtest_gap_reduces_entry_and_never_borrows():
    # Tiny account: 25% of 40 = 10; reserve 10 leaves 30 cash for buy; adverse gap is irrelevant to notional cap
    prices = [100] * 199 + [101, 300, 300]
    res = run_backtest(_series(prices), starting_cash=Decimal("15"), costs=CostModel(Decimal(0), Decimal("1")))
    # min(3.75, 250, 15-10-1=4) = 3.75 -> fill; never more than cash-reserve-fee
    strat = res["portfolios"]["strategy"]
    for e in strat["equity"]:
        assert Decimal(e[1]) > 0
    for t in strat["trades"]:
        assert Decimal(t["residual_cash"]) >= 0


def test_split_raw_matches_adjusted():
    """Raw prices with a 2:1 split event must produce the same equity as pre-adjusted prices."""
    adj = [100 + (i % 7) for i in range(199)] + [120, 121, 125, 90, 80, 130, 131]
    split_at = 202
    raw = [p * 2 if i < split_at else p for i, p in enumerate(adj)]
    s_adj = _series(adj)
    s_raw = _series(raw, basis="raw")
    s_raw.splits = [SplitEvent(s_raw.bars[split_at].day, Decimal(2))]
    a = run_backtest(s_adj, costs=CostModel(Decimal(0), Decimal(0)))
    r = run_backtest(s_raw, costs=CostModel(Decimal(0), Decimal(0)))
    ea = [Decimal(x[1]) for x in a["portfolios"]["strategy"]["equity"]]
    er = [Decimal(x[1]) for x in r["portfolios"]["strategy"]["equity"]]
    assert len(ea) == len(er)
    for x, y in zip(ea, er):
        assert abs(x - y) < Decimal("0.0001")


def test_benchmarks_share_dates_capital_and_costs():
    s = generate_sample_series()
    res = run_backtest(s)
    p = res["portfolios"]
    dates = [[d for d, _ in v["equity"]] for v in p.values()]
    assert dates[0] == dates[1] == dates[2]
    for v in p.values():
        assert v["metrics"]["start_equity"] == "1000"
    mb, fb = p["matched_entry_benchmark"]["trades"], p["full_investment_benchmark"]["trades"]
    assert len(mb) == 1 and len(fb) == 1 and mb[0]["day"] == fb[0]["day"]
    assert Decimal(mb[0]["notional"]) <= 250
    assert Decimal(fb[0]["residual_cash"]) < 1
    assert "Price-only" in " ".join(res["assumptions"])


def test_risk_pause_blocks_buys_in_backtest():
    prices = [100] * 199 + [101] + [101] * 3 + [60] * 3 + [50] + [200] * 5
    res = run_backtest(_series(prices), starting_cash=Decimal(1000), costs=CostModel(Decimal(0), Decimal(0)),
                       risk=RiskPolicy(Decimal("0.01"), Decimal("0.02")))
    assert any(e["event"] == "PAUSE" for e in res["risk_events"])
    buys = [t for t in res["portfolios"]["strategy"]["trades"] if t["side"] == "BUY"]
    assert len(buys) == 1  # re-entry after the rally blocked by the latch
    assert any("RISK_PAUSED" in d.get("reason", "") for d in res["decisions"])


def test_short_runs_do_not_annualize():
    prices = [100] * 199 + [101, 102, 103]
    m = run_backtest(_series(prices))["portfolios"]["strategy"]["metrics"]
    assert m["annualized_return"] is None and "N/A" in m["annualized_note"]


def test_sample_series_is_labelled_and_deterministic():
    a, b = generate_sample_series(), generate_sample_series()
    assert a.mode == "SAMPLE" and "NOT real" in a.source
    assert a.fingerprint() == b.fingerprint()


def test_backtest_skips_gapped_windows_like_live():
    """A missing session inside the window must block the signal in backtests too (consistency with live)."""
    s = _series([100] * 199 + [101] * 260)
    gap_day = s.bars[150].day
    del s.bars[150]  # one missing session inside the early windows
    res = run_backtest(s, costs=CostModel(Decimal(0), Decimal(0)))
    gapped = [d for d in res["decisions"] if "Missing session" in d.get("reason", "")]
    assert gapped and res["gap_skipped_decisions"] == len(gapped)
    first_ok = next(d for d in res["decisions"] if d["action"] != "SKIP")
    cal = xnys_calendar()
    sess = [x.day for x in cal.sessions]
    # the first usable window must start after the missing day
    window_start = sess[sess.index(date.fromisoformat(first_ok["session"])) - 199]
    assert window_start > gap_day
    assert all(date.fromisoformat(d["session"]) < date.fromisoformat(first_ok["session"]) for d in gapped)


def test_drawdown_warns_only_at_threshold_and_rearms_after_recovery():
    st, pol = DrawdownState(), RiskPolicy()
    assert update_drawdown(st, Decimal(1000), pol) == []
    assert update_drawdown(st, Decimal(970), pol) == []
    assert update_drawdown(st, Decimal(950), pol) == ["WARN"]
    assert update_drawdown(st, Decimal(970), pol) == []
    assert update_drawdown(st, Decimal(980), pol) == []
    assert update_drawdown(st, Decimal(950), pol) == ["WARN"]
