"""Gold step 2: choose the best rule on 2019-2021 only, then test it once on 2022-2026.

    uv run python -m orbit.research.run_gold2 fetch   # daily GLD + SPY since 2016 (SIP history, read-only)
    uv run python -m orbit.research.run_gold2 run     # writes data/research/gold_step2.json
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from datetime import date, datetime, timedelta

import numpy as np

from orbit.config import get_settings
from orbit.market_calendar import NEW_YORK, UTC
from orbit.research.gold import DayBar, combine, correlation, metrics
from orbit.research import gold_rules as gr

DEV = (date(2019, 1, 1), date(2021, 12, 31))
HOLD = (date(2022, 1, 1), date(2100, 1, 1))
SLIP = {"no costs": 0.0, "realistic (2 bp)": 0.0002, "pessimistic (5 bp)": 0.0005}


def _path(sym):
    return get_settings().data_dir / "research" / f"{sym.lower()}_daily_sip.csv.gz"


def fetch():
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    s = get_settings()
    c = StockHistoricalDataClient(s.alpaca_api_key_id.get_secret_value(), s.alpaca_api_secret_key.get_secret_value())
    for sym in ("GLD", "SPY"):
        raw = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=datetime(2016, 1, 1, tzinfo=UTC),
                                                end=datetime.now(UTC) - timedelta(days=1), adjustment=Adjustment.ALL,
                                                feed=DataFeed.SIP, limit=None)).data.get(sym, [])
        _path(sym).parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(_path(sym), "wt", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["day", "open", "close"])
            for b in raw:
                w.writerow([b.timestamp.astimezone(NEW_YORK).date().isoformat(), b.open, b.close])
        print(sym, len(raw), raw[0].timestamp.date(), raw[-1].timestamp.date())


def _load(sym):
    with gzip.open(_path(sym), "rt", newline="") as fh:
        return [DayBar(date.fromisoformat(r["day"]), float(r["open"]), float(r["close"])) for r in csv.DictReader(fh)]


def _hold_target(n):
    return [1] * n


def run():
    g_all, s_all = _load("GLD"), _load("SPY")
    days = sorted({b.day for b in g_all} & {b.day for b in s_all})
    keep = set(days)
    gld = [b for b in g_all if b.day in keep]
    spy = [b for b in s_all if b.day in keep]
    gc, sc = np.array([b.close for b in gld]), np.array([b.close for b in spy])

    candidates: dict[str, list[int]] = {}
    for n in (100, 150, 200):
        candidates[f"{n}-day average"] = gr.sma_band(gc, n, 0.0)
    for band in (0.01, 0.02, 0.03, 0.05):
        candidates[f"200-day average with {int(band * 100)}% buffer"] = gr.sma_band(gc, 200, band)
    for f, s in ((50, 200), (20, 100), (50, 150)):
        candidates[f"{f}/{s}-day crossover"] = gr.crossover(gc, f, s)
    candidates["200-day average, checked monthly"] = gr.monthly_sma(gld, gc, 200)
    models = {}
    for thr in (0.5, 0.55, 0.6):
        m = gr.walk_forward_model(gld, gc, sc, first_year=2019, threshold=thr)
        models[thr] = m
        candidates[f"Learning model (invest if chance > {int(thr * 100)}%)"] = m.target
    candidates["Just hold gold"] = _hold_target(len(gld))

    di, dj = gr.slice_index(gld, *DEV)
    hi, hj = gr.slice_index(gld, *HOLD)

    def seg(curve, i, j):
        return metrics(curve[i:j])

    report = {"generated_at": datetime.now(UTC).isoformat(), "data": "Alpaca SIP daily bars (all adjustments), GLD and SPY",
              "first_bar": days[0].isoformat(), "last_bar": days[-1].isoformat(),
              "development": [d.isoformat() for d in DEV], "holdout_from": HOLD[0].isoformat(),
              "correlation_spy_gld_daily": correlation(spy, gld), "features": gr.FEATURES, "costs": {}}
    for label, slip in SLIP.items():
        rows = {}
        for name, tgt in candidates.items():
            r = gr.simulate(gld, tgt, slip)
            dev, hold = seg(r["curve"], di, dj), seg(r["curve"], hi, hj)
            fills_hold = sum(1 for t in range(hi, hj) if t > 0 and tgt[t - 1] != (tgt[t - 2] if t > 1 else 0))
            rows[name] = {"development": dev, "holdout": hold, "holdout_switches": fills_hold,
                          "holdout_time_invested_pct": round(100 * sum(tgt[hi - 1:hj - 1]) / (hj - hi), 1)}
        rules = {k: v for k, v in rows.items() if k != "Just hold gold"}
        best = max(rules, key=lambda k: rules[k]["development"]["cagr_pct"])
        best_risk = max(rules, key=lambda k: rules[k]["development"]["return_per_risk"] or -9)
        report["costs"][label] = {"all": rows, "picked_on_development_by_return": best, "picked_on_development_by_return_per_risk": best_risk}

    # Mixes on the holdout (realistic costs): SPY held / SPY with Orbit's rule, plus a gold slice.
    slip = SLIP["realistic (2 bp)"]
    spy_hold = gr.simulate(spy, _hold_target(len(spy)), slip)["curve"]
    spy_rule = gr.simulate(spy, gr.sma_band(sc, 200, 0.0), slip)["curve"]
    gold_hold = gr.simulate(gld, _hold_target(len(gld)), slip)["curve"]
    pick = report["costs"]["realistic (2 bp)"]["picked_on_development_by_return_per_risk"]
    gold_pick = gr.simulate(gld, candidates[pick], slip)["curve"]

    def mix(a, b, wa):
        return [(d, wa * x / a[hi][1] + (1 - wa) * y / b[hi][1]) for (d, x), (_, y) in zip(a, b)]
    report["holdout_mixes_realistic"] = {
        "SPY: just hold": seg(spy_hold, hi, hj),
        "SPY: Orbit 200-day rule": seg(spy_rule, hi, hj),
        "70% SPY rule + 30% gold held": seg(mix(spy_rule, gold_hold, 0.7), hi, hj),
        f"70% SPY rule + 30% gold ({pick})": seg(mix(spy_rule, gold_pick, 0.7), hi, hj),
        "70% SPY held + 30% gold held": seg(mix(spy_hold, gold_hold, 0.7), hi, hj),
    }
    report["model_weights_by_year"] = {str(k): v for k, v in models[0.5].yearly_weights.items()}
    out = get_settings().data_dir / "research" / "gold_step2.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    r = report["costs"]["realistic (2 bp)"]
    print("picked by return:", r["picked_on_development_by_return"], "| by return/risk:", r["picked_on_development_by_return_per_risk"])
    for k, v in r["all"].items():
        d, h = v["development"], v["holdout"]
        print(f"{k:48s} dev cagr {d['cagr_pct']:6.2f} mdd {d['max_drawdown_pct']:5.1f} rpr {d['return_per_risk']} | "
              f"HOLD total {h['total_return_pct']:7.2f} cagr {h['cagr_pct']:6.2f} mdd {h['max_drawdown_pct']:5.1f} rpr {h['return_per_risk']} "
              f"sw {v['holdout_switches']} inv {v['holdout_time_invested_pct']}")
    for k, v in report["holdout_mixes_realistic"].items():
        print(f"MIX {k:45s} total {v['total_return_pct']:7.2f} cagr {v['cagr_pct']:6.2f} mdd {v['max_drawdown_pct']:5.1f} rpr {v['return_per_risk']} worst yr {v['worst_year_pct']}")


if __name__ == "__main__":
    {"fetch": fetch, "run": run}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
