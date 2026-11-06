"""Gold step 2 rules: next-open execution, hysteresis, and a learning model that never sees the future."""
from datetime import date, timedelta

import numpy as np

from orbit.research import gold_rules as gr
from orbit.research.gold import DayBar


def _bars(closes, start=date(2016, 1, 4)):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(DayBar(d, c, c))
        d += timedelta(days=1)
    return out


def test_simulate_applies_target_at_next_open():
    bars = _bars([10, 10, 20, 20])
    r = gr.simulate(bars, [0, 1, 1, 0], 0.0, start_cash=100.0)
    vals = [v for _, v in r["curve"]]
    assert vals == [100.0, 100.0, 100.0, 100.0]  # bought at bar 2's open (20), sold at bar 4's open (20)
    assert r["fills"] == 1  # the last target (0) would apply after the data ends


def test_buffer_band_keeps_previous_target_inside_band():
    c = np.array([100.0] * 3 + [106.0, 101.0, 101.0, 96.0])
    t = gr.sma_band(c, 3, 0.02)
    assert t[3] == 1 and t[4] == 1 and t[5] == 1  # 101 is below the average but inside the 2% band: still invested
    assert t[6] == 0


def test_model_trains_only_on_outcomes_known_before_each_year(monkeypatch):
    rng = np.random.default_rng(1)
    closes = list(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 1300))))
    bars = _bars(closes)
    seen = {}
    orig = gr._fit_logistic

    def spy_fit(X, y, **kw):
        seen.setdefault("n", []).append(len(y))
        return orig(X, y, **kw)
    monkeypatch.setattr(gr, "_fit_logistic", spy_fit)
    g = np.array(closes)
    run = gr.walk_forward_model(bars, g, g, first_year=2019)
    assert seen["n"] and all(n >= 250 for n in seen["n"])
    first_2019 = next(i for i, b in enumerate(bars) if b.day.year == 2019)
    assert all(p is None for p in run.prob[:first_2019])  # no predictions before the first trained year
    # Changing prices AFTER a year starts must not change that year's model weights.
    g2 = g.copy()
    g2[first_2019 + 5:] *= 2
    run2 = gr.walk_forward_model(bars, g2, g2, first_year=2019)
    assert run.yearly_weights[2019] == run2.yearly_weights[2019]
