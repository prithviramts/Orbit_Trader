"""Gold research: does the 200-day-average rule work on GLD, and does adding gold to SPY help?

    uv run python -m orbit.research.run_gold fetch   # download daily GLD and SPY bars (read-only market data)
    uv run python -m orbit.research.run_gold run     # write data/research/gold_step1.json

Nothing here can place orders.
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from datetime import date, datetime, timedelta

from orbit.config import get_settings
from orbit.market_calendar import NEW_YORK, UTC
from orbit.research.gold import DayBar, combine, correlation, hold, metrics, sma_rule

SYMBOLS = ("GLD", "SPY")
START = date(2016, 1, 1)
SLIPPAGE = {"no costs": 0.0, "realistic (2 bp per fill)": 0.0002, "pessimistic (5 bp per fill)": 0.0005}
WINDOW = 200


def _dir():
    return get_settings().data_dir / "research"


def fetch() -> None:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    s = get_settings()
    if not s.broker_configured:
        sys.exit("Alpaca paper credentials are not configured in .env")
    client = StockHistoricalDataClient(s.alpaca_api_key_id.get_secret_value(), s.alpaca_api_secret_key.get_secret_value())
    feed = {"iex": DataFeed.IEX, "sip": DataFeed.SIP, "delayed_sip": DataFeed.DELAYED_SIP}[s.alpaca_data_feed]
    end = datetime.now(UTC) - timedelta(days=1)
    _dir().mkdir(parents=True, exist_ok=True)
    for sym in SYMBOLS:
        req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=datetime.combine(START, datetime.min.time(), UTC),
                               end=end, adjustment=Adjustment.ALL, feed=feed, limit=None)
        raw = client.get_stock_bars(req).data.get(sym, [])
        path = _dir() / f"{sym.lower()}_daily_{s.alpaca_data_feed}.csv.gz"
        with gzip.open(path, "wt", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["day", "open", "close"])
            for b in raw:
                w.writerow([b.timestamp.astimezone(NEW_YORK).date().isoformat(), b.open, b.close])
        print(f"{sym}: {len(raw)} daily bars, {raw[0].timestamp:%Y-%m-%d} to {raw[-1].timestamp:%Y-%m-%d}" if raw else f"{sym}: no data")


def _load(sym: str) -> list[DayBar]:
    path = _dir() / f"{sym.lower()}_daily_{get_settings().alpaca_data_feed}.csv.gz"
    with gzip.open(path, "rt", newline="") as fh:
        return [DayBar(date.fromisoformat(r["day"]), float(r["open"]), float(r["close"])) for r in csv.DictReader(fh)]


def run() -> None:
    data = {s: _load(s) for s in SYMBOLS}
    common = sorted(set.intersection(*({b.day for b in v} for v in data.values())))
    data = {s: [b for b in v if b.day in set(common)] for s, v in data.items()}
    gld, spy = data["GLD"], data["SPY"]
    start = WINDOW  # first session the rule can act on (after warm-up); everything is measured from here
    report: dict = {"generated_at": datetime.now(UTC).isoformat(), "feed": get_settings().alpaca_data_feed,
                    "sessions": len(common), "first_bar": common[0].isoformat(), "measured_from": common[start].isoformat(),
                    "last_bar": common[-1].isoformat(), "daily_return_correlation_spy_gld": correlation(spy, gld), "costs": {}}
    for label, slip in SLIPPAGE.items():
        g_rule, s_rule = sma_rule(gld, slip, window=WINDOW), sma_rule(spy, slip, window=WINDOW)
        g_hold, s_hold = hold(gld, slip, skip=start), hold(spy, slip, skip=start)
        rows = {
            "GLD: 200-day rule": (g_rule["curve"], g_rule),
            "GLD: just hold": (g_hold["curve"], g_hold),
            "SPY: 200-day rule (Orbit today)": (s_rule["curve"], s_rule),
            "SPY: just hold": (s_hold["curve"], s_hold),
            "Half SPY rule + half GLD rule": (combine(s_rule["curve"], g_rule["curve"]), None),
            "Half SPY + half GLD, just hold": (combine(s_hold["curve"], g_hold["curve"]), None),
        }
        out = {}
        for name, (curve, info) in rows.items():
            m = metrics(curve, start)
            if info:
                m["trades"] = info["trades"]
                m["time_invested_pct"] = round(info["invested_days"] / (len(curve) - start) * 100, 1)
            out[name] = m
        report["costs"][label] = out
    path = _dir() / "gold_step1.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: {n: {x: v[x] for x in ("total_return_pct", "cagr_pct", "max_drawdown_pct", "return_per_risk", "worst_year_pct")}
                          for n, v in rows.items()} for k, rows in report["costs"].items()}, indent=1))
    print("correlation", report["daily_return_correlation_spy_gld"], "measured from", report["measured_from"], "to", report["last_bar"])


if __name__ == "__main__":
    {"fetch": fetch, "run": run}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
