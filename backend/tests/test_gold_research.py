"""Gold research simulator: same no-peeking rule as live, next-open fills, costs charged."""
from datetime import date, timedelta

from orbit.research.gold import DayBar, combine, hold, metrics, sma_rule


def _bars(closes):
    d0 = date(2024, 1, 1)
    return [DayBar(d0 + timedelta(days=i), c, c) for i, c in enumerate(closes)]


def test_rule_waits_for_warmup_and_trades_next_open():
    bars = _bars([10.0, 10.0, 12.0, 13.0, 9.0, 8.0])
    r = sma_rule(bars, 0.0, start_cash=100.0, window=3)
    vals = [v for _, v in r["curve"]]
    assert vals[:3] == [100.0, 100.0, 100.0]  # day 3 close > avg -> buy at day 4 open, not day 3
    assert vals[3] == 100.0 * 13 / 13  # bought at 13 open (open == close in fixture)
    # day 4 close 13 > avg 11.67 stays invested; day 5 close 9 < avg -> sell at day 6 open (8)
    assert abs(vals[-1] - 100.0 * 8 / 13) < 1e-9 and r["trades"] == 2


def test_costs_reduce_result_and_hold_combine_metrics():
    bars = _bars([10.0 + i for i in range(10)])
    free = sma_rule(bars, 0.0, window=3)["curve"][-1][1]
    paid = sma_rule(bars, 0.001, window=3)["curve"][-1][1]
    assert paid < free
    h = hold(bars, 0.0, start_cash=100.0, skip=2)["curve"]
    assert h[0][1] == 100.0 and abs(h[-1][1] - 100.0 * 19 / 12) < 1e-9
    both = combine(h, h)
    assert both == h
    m = metrics(h, 2)
    assert m["total_return_pct"] == round((19 / 12 - 1) * 100, 2) and m["max_drawdown_pct"] == 0.0
