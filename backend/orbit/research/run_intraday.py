"""Run step 1 of the intraday experiment.

    uv run python -m orbit.research.run_intraday fetch     # download 1-minute SPY bars (read-only market data)
    uv run python -m orbit.research.run_intraday run       # backtest on development period, then holdout

Output: data/research/intraday_step1.json (all numbers). Nothing here can place orders.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta

from orbit.config import get_settings
from orbit.market_calendar import UTC, xnys_calendar
from orbit.research import intraday as ix

START = date(2021, 1, 4)
HOLDOUT_FROM = date(2024, 1, 1)
SLIPPAGE = {"no costs": 0.0, "low (0.5 bp per fill)": 0.00005, "realistic (2 bp per fill)": 0.0002, "pessimistic (5 bp per fill)": 0.0005}


def _paths():
    d = get_settings().data_dir / "research"
    return d / "spy_1min_iex.csv.gz", d / "intraday_step1.json"


def fetch(end: date | None = None) -> None:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    s = get_settings()
    if not s.broker_configured:
        sys.exit("Alpaca paper credentials are not configured in .env")
    client = StockHistoricalDataClient(s.alpaca_api_key_id.get_secret_value(), s.alpaca_api_secret_key.get_secret_value())
    feed = {"iex": DataFeed.IEX, "sip": DataFeed.SIP, "delayed_sip": DataFeed.DELAYED_SIP}[s.alpaca_data_feed]
    end = end or (datetime.now(UTC) - timedelta(days=1)).date()
    out: list[ix.MinuteBar] = []
    cur = START
    while cur <= end:
        nxt = min(date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1), end + timedelta(days=1))
        req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                               start=datetime.combine(cur, datetime.min.time(), UTC), end=datetime.combine(nxt, datetime.min.time(), UTC),
                               adjustment=Adjustment.SPLIT, feed=feed, limit=None)
        raw = client.get_stock_bars(req).data.get("SPY", [])
        out += [ix.MinuteBar(b.timestamp.astimezone(UTC), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)) for b in raw]
        print(f"{cur:%Y-%m}: {len(raw):>6} bars (total {len(out)})", flush=True)
        cur = nxt
    bars_path, _ = _paths()
    ix.save_bars(bars_path, out)
    print(f"saved {len(out)} bars to {bars_path}")


def run() -> None:
    bars_path, out_path = _paths()
    cal = xnys_calendar()
    days = ix.group_sessions(ix.load_bars(bars_path), cal)
    expected = [x.day for x in cal.sessions_between(days[0].day, days[-1].day)]
    have = {d.day for d in days}
    missing = [d for d in expected if d not in have]
    thin = [d.day for d in days if len(d.bars) < 300]
    dev, hold = ix.split(days, HOLDOUT_FROM)
    report: dict = {
        "generated_at": datetime.now(UTC).isoformat(), "feed": get_settings().alpaca_data_feed,
        "sessions": len(days), "first": days[0].day, "last": days[-1].day,
        "missing_sessions": len(missing), "thin_sessions_under_300_minutes": len(thin),
        "development": {"from": dev[0].day, "to": dev[-1].day, "sessions": len(dev)},
        "holdout": {"from": hold[0].day, "to": hold[-1].day, "sessions": len(hold)},
        "costs": {},
    }
    for label, slip in SLIPPAGE.items():
        rows_dev = ix.as_rows(ix.grid(dev, slip))
        bench_dev = [ix.metrics(ix.open_to_close(dev, slip)), ix.metrics(ix.buy_and_hold(dev))]
        report["costs"][label] = {"slippage_per_fill": slip, "development": {"all_tried": rows_dev, "benchmarks": bench_dev}}
        print(f"[{label}] development: {len(rows_dev)} combinations tried", flush=True)
    # For each cost level: choose the best combination per family on DEVELOPMENT, then run it once on the HOLDOUT.
    report["holdout"]["results"] = {}
    for label, slip in SLIPPAGE.items():
        rows = report["costs"][label]["development"]["all_tried"]
        out = []
        for fam in ("VWAP dip-buy", "Opening-range breakout"):
            best = max((r for r in rows if r["strategy"] == fam), key=lambda r: r["total_return"])
            p = best["params"]
            res = (ix.vwap_dip(hold, p["dip_below_vwap"], p["stop"], slip) if fam == "VWAP dip-buy"
                   else ix.orb_with_range_stop(hold, p["range_minutes"], slip))
            out.append({"chosen_on_development": best, "holdout": ix.metrics(res)})
        report["holdout"]["results"][label] = {"strategies": out, "benchmarks": [ix.metrics(ix.open_to_close(hold, slip)), ix.metrics(ix.buy_and_hold(hold))]}
    out_path.write_text(json.dumps(ix.to_json_safe(report), indent=1))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    {"fetch": fetch, "run": run}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
