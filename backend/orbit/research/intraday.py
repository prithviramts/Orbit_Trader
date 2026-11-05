"""Step 1 of the intraday experiment: honest backtests of simple intraday SPY rules on 1-minute bars.

Research only. This module never talks to a broker's trading API and is not used by the engine or worker.

Honesty rules built in:
- Signals use only information up to the close of the current minute; fills happen at the NEXT minute's
  open (no same-bar hindsight), with an adverse slippage charge on every fill.
- Stops are checked against each bar's low; a gap through the stop fills at the bar's open, not the stop.
- Long only, no leverage, flat before the close (last entry 15:30 ET, forced exit at the last full minute).
- Parameters are chosen on a development period only; the holdout period is reported once, untouched.
- Every parameter combination tried is reported, not just the best one.
- Floats are fine here: these are research statistics, not money records.
"""
from __future__ import annotations

import csv
import gzip
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Callable, Iterable

from orbit.market_calendar import NEW_YORK, MarketCalendar

UTC = timezone.utc
LAST_ENTRY_ET = time(15, 30)


@dataclass(frozen=True)
class MinuteBar:
    ts: datetime  # bar start, UTC
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float


@dataclass
class Day:
    day: date
    bars: list[MinuteBar]


@dataclass
class Trade:
    day: date
    entry_ts: datetime
    entry_px: float
    exit_ts: datetime
    exit_px: float
    reason: str

    @property
    def ret(self) -> float:
        return self.exit_px / self.entry_px - 1.0


@dataclass
class Result:
    name: str
    params: dict
    trades: list[Trade] = field(default_factory=list)
    daily: list[tuple[date, float]] = field(default_factory=list)  # (day, that day's return on the account)


# ------------------------------------------------------------------ data handling
def group_sessions(bars: Iterable[MinuteBar], cal: MarketCalendar) -> list[Day]:
    """Keep regular-session minutes only (calendar handles holidays/early closes); group by session day."""
    by_day: dict[date, list[MinuteBar]] = {}
    for b in bars:
        d = b.ts.astimezone(NEW_YORK).date()
        s = cal.session(d)
        if s is None or not (s.open_utc <= b.ts < s.close_utc):
            continue
        by_day.setdefault(d, []).append(b)
    return [Day(d, sorted(v, key=lambda x: x.ts)) for d, v in sorted(by_day.items()) if len(v) >= 30]


def save_bars(path: Path, bars: list[MinuteBar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "o", "h", "l", "c", "v"])
        for b in bars:
            w.writerow([int(b.ts.timestamp()), b.o, b.h, b.l, b.c, b.v])


def load_bars(path: Path) -> list[MinuteBar]:
    with gzip.open(path, "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        return [MinuteBar(datetime.fromtimestamp(int(t), UTC), float(o), float(h), float(lo), float(c), float(v))
                for t, o, h, lo, c, v in r]


# ------------------------------------------------------------------ simulator
Signal = Callable[[Day, int, dict], bool]


def _et(ts: datetime) -> time:
    return ts.astimezone(NEW_YORK).time()


def simulate(days: list[Day], name: str, params: dict, entry: Signal, exit_: Signal | None, stop_pct: float | None,
             slip: float, max_trades_per_day: int = 2) -> Result:
    """Generic long-only, flat-by-close intraday simulator.

    entry(day, i, state) -> True means "buy at the open of bar i+1".
    exit_(day, i, state) -> True means "sell at the open of bar i+1".
    state is a per-day dict the signal functions may use (e.g. running VWAP).
    """
    res = Result(name, params)
    for d in days:
        bars = d.bars
        state: dict = {}
        pos_px = pos_ts = stop = None
        n_trades = 0
        day_mult = 1.0
        for i in range(len(bars) - 1):
            b, nxt = bars[i], bars[i + 1]
            _update_state(state, b)
            if pos_px is not None:
                # stop check on this bar (we were already holding during bar i)
                if stop is not None and b.l <= stop:
                    px = min(b.o, stop) * (1 - slip)
                    res.trades.append(Trade(d.day, pos_ts, pos_px, b.ts, px, "stop"))
                    day_mult *= px / pos_px
                    pos_px = None
                    continue
                if exit_ is not None and exit_(d, i, state):
                    px = nxt.o * (1 - slip)
                    res.trades.append(Trade(d.day, pos_ts, pos_px, nxt.ts, px, "target"))
                    day_mult *= px / pos_px
                    pos_px = None
                    continue
            elif n_trades < max_trades_per_day and _et(b.ts) < LAST_ENTRY_ET and entry(d, i, state):
                pos_px, pos_ts = nxt.o * (1 + slip), nxt.ts
                stop = pos_px * (1 - stop_pct) if stop_pct else None
                n_trades += 1
        if pos_px is not None:  # forced exit at the last minute's close
            last = bars[-1]
            if stop is not None and last.l <= stop:
                px, why = min(last.o, stop) * (1 - slip), "stop"
            else:
                px, why = last.c * (1 - slip), "close"
            res.trades.append(Trade(d.day, pos_ts, pos_px, last.ts, px, why))
            day_mult *= px / pos_px
        res.daily.append((d.day, day_mult - 1.0))
    return res


def _update_state(state: dict, b: MinuteBar) -> None:
    typical = (b.h + b.l + b.c) / 3
    state["pv"] = state.get("pv", 0.0) + typical * b.v
    state["vol"] = state.get("vol", 0.0) + b.v
    state["vwap"] = state["pv"] / state["vol"] if state["vol"] > 0 else b.c
    state["n"] = state.get("n", 0) + 1
    if state["n"] <= state.get("or_minutes", 30):
        state["or_high"] = max(state.get("or_high", b.h), b.h)
        state["or_low"] = min(state.get("or_low", b.l), b.l)


# ------------------------------------------------------------------ strategy families
def vwap_dip(days, k: float, stop_pct: float, slip: float) -> Result:
    """Buy the dip: price closes k below the day's running VWAP (after 10:00 ET); sell when it gets back to VWAP."""
    def entry(d, i, st):
        return _et(d.bars[i].ts) >= time(10, 0) and d.bars[i].c <= st["vwap"] * (1 - k)

    def exit_(d, i, st):
        return d.bars[i].c >= st["vwap"]
    return simulate(days, "VWAP dip-buy", {"dip_below_vwap": k, "stop": stop_pct}, entry, exit_, stop_pct, slip)


def orb_with_range_stop(days, minutes: int, slip: float) -> Result:
    """Breakout with the stop placed at the opening range low (computed per day at entry)."""
    res = Result("Opening-range breakout", {"range_minutes": minutes, "stop": "range low"})
    for d in days:
        st: dict = {"or_minutes": minutes}
        sub = d
        hi = lo = None
        entered = False
        day_mult = 1.0
        pos = None
        for i in range(len(sub.bars) - 1):
            b, nxt = sub.bars[i], sub.bars[i + 1]
            _update_state(st, b)
            if st["n"] == minutes:
                hi, lo = st["or_high"], st["or_low"]
            if pos is not None:
                if b.l <= pos[2]:
                    px = min(b.o, pos[2]) * (1 - slip)
                    res.trades.append(Trade(d.day, pos[1], pos[0], b.ts, px, "stop"))
                    day_mult *= px / pos[0]
                    pos = None
                continue
            if not entered and hi is not None and st["n"] > minutes and _et(b.ts) < LAST_ENTRY_ET and b.c > hi:
                pos = (nxt.o * (1 + slip), nxt.ts, lo)
                entered = True
        if pos is not None:
            last = sub.bars[-1]
            px, why = ((min(last.o, pos[2]) * (1 - slip), "stop") if last.l <= pos[2] else (last.c * (1 - slip), "close"))
            res.trades.append(Trade(d.day, pos[1], pos[0], last.ts, px, why))
            day_mult *= px / pos[0]
        res.daily.append((d.day, day_mult - 1.0))
    return res


def open_to_close(days, slip: float) -> Result:
    """Benchmark for intraday rules: buy every open, sell every close (same costs, same flat-overnight rule)."""
    res = Result("Benchmark: buy every open, sell every close", {})
    for d in days:
        e, x = d.bars[0].o * (1 + slip), d.bars[-1].c * (1 - slip)
        res.trades.append(Trade(d.day, d.bars[0].ts, e, d.bars[-1].ts, x, "close"))
        res.daily.append((d.day, x / e - 1.0))
    return res


def buy_and_hold(days) -> Result:
    """Benchmark: buy once at the first open and hold (overnight moves included, no costs after the first fill)."""
    res = Result("Benchmark: buy and hold", {})
    prev = days[0].bars[0].o
    for d in days:
        c = d.bars[-1].c
        res.daily.append((d.day, c / prev - 1.0))
        prev = c
    return res


# ------------------------------------------------------------------ metrics
def metrics(r: Result) -> dict:
    eq, peak, mdd = 1.0, 1.0, 0.0
    for _, x in r.daily:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    n = len(r.daily)
    years = n / 252 if n else 0
    rets = [x for _, x in r.daily]
    mean = sum(rets) / n if n else 0
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (n - 1)) if n > 1 else 0
    wins = [t for t in r.trades if t.ret > 0]
    traded_days = len({t.day for t in r.trades})
    return {
        "strategy": r.name, "params": r.params, "sessions": n,
        "total_return": eq - 1, "cagr": (eq ** (1 / years) - 1) if years > 0 and eq > 0 else None,
        "max_drawdown": mdd, "sharpe": (mean / sd * math.sqrt(252)) if sd > 0 else None,
        "trades": len(r.trades), "win_rate": len(wins) / len(r.trades) if r.trades else None,
        "avg_trade": sum(t.ret for t in r.trades) / len(r.trades) if r.trades else None,
        "days_with_trade": traded_days,
    }


def split(days: list[Day], cut: date) -> tuple[list[Day], list[Day]]:
    return [d for d in days if d.day < cut], [d for d in days if d.day >= cut]


def grid(days: list[Day], slip: float) -> list[Result]:
    out = []
    for k in (0.002, 0.003, 0.005, 0.0075):
        for s in (0.003, 0.005, 0.01):
            out.append(vwap_dip(days, k, s, slip))
    for m in (15, 30, 60):
        out.append(orb_with_range_stop(days, m, slip))
    return out


def as_rows(results: list[Result]) -> list[dict]:
    return [metrics(r) for r in results]


def to_json_safe(x):
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: to_json_safe(v) for k, v in x.items()}
    if isinstance(x, list):
        return [to_json_safe(v) for v in x]
    return x

