"""AI day trader model (paper only): should we hold the S&P 500 from 09:45 to 15:50 New York time today?

The model is a small logistic regression, the same kind as the Gold AI, trained on SPY's 15-minute
history since 2016. Each training day is replayed exactly as the live trader would trade it:

- decide at 09:45 New York time, once the first 15-minute bar is complete,
- buy at that price, sell if the price falls STOP below it (stop-loss), otherwise sell at 15:45-15:50.

The label is "did that trade make money". Signals only use what is known at 09:45: today's opening gap and
first 15 minutes, plus completed earlier days. The trader buys IVV (another S&P 500 fund) so its shares
stay separate from the SPY 200-day rule's shares in the same account; SPY and IVV track the same index.

Research before building (walk-forward, each year predicted by a model trained only on earlier years,
2019 to 2026-11-23, no costs): the AI made about +33% over 776 trades (54% right); trading every day the
same way made about +67%; simply holding SPY made about +189%. It is a paper experiment, not an edge.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

MODEL_VERSION = "day-logit-v1"
MODEL_FILE = "day_model.json"
STOP = 0.0075  # sell if the price falls 0.75% below the buy price
THRESHOLD = 0.52  # buy when the chance of a winning day trade is above this
L2 = 1.0
MIN_TRAIN = 500
MIN_HISTORY = 201  # completed days needed before today (200-day average + 1)

SIGNALS = [
    ("gap", "Overnight gap (open vs yesterday's close)"),
    ("first15", "First 15 minutes"),
    ("ret1", "Yesterday's move"),
    ("ret5", "Last 5 days"),
    ("ret20", "Last 20 days"),
    ("d20", "Distance from 20-day average"),
    ("d200", "Distance from 200-day average"),
    ("vol20", "Volatility, last 20 days"),
    ("range1", "Yesterday's high-low range"),
    ("mon", "Monday"),
    ("fri", "Friday"),
]
KEYS = [k for k, _ in SIGNALS]
LABELS = dict(SIGNALS)


@dataclass
class DayRow:
    day: date
    open: float
    entry: float  # price at 09:45 (close of the first 15-minute bar)
    high: float
    low: float
    close: float
    trade_ret: float = float("nan")  # result of the stop/close rule; nan for today (not known yet)


def rows_from_15min(bars: list[tuple[datetime, float, float, float, float]], tz, stop: float = STOP) -> list[DayRow]:
    """Group regular-hours 15-minute bars (timestamp, open, high, low, close) into days and replay the trade."""
    days: dict[date, list] = {}
    for t, o, h, lo, c in bars:
        lt = t.astimezone(tz)
        hm = (lt.hour, lt.minute)
        if (9, 30) <= hm < (16, 0):
            days.setdefault(lt.date(), []).append((hm, o, h, lo, c))
    out = []
    for d in sorted(days):
        b = sorted(days[d])
        if b[0][0] != (9, 30) or len(b) < 12:
            continue  # missing opening bar or a very short day in the data
        entry = b[0][4]
        out.append(DayRow(d, b[0][1], entry, max(x[2] for x in b), min(x[3] for x in b), b[-1][4], simulate_trade(entry, b[1:], stop)))
    return out


def simulate_trade(entry: float, later_bars: list, stop: float = STOP) -> float:
    """Return of buying at ``entry``: stop-loss first, else sell at the 15:45 bar's open (about 15:45-15:50)."""
    for hm, o, _h, lo, c in later_bars:
        if lo <= entry * (1 - stop):
            return min(o, entry * (1 - stop)) / entry - 1  # a gap through the stop fills at the worse price
        if hm >= (15, 45):
            return o / entry - 1
    return later_bars[-1][4] / entry - 1 if later_bars else 0.0


def signals(rows: list[DayRow]) -> np.ndarray:
    """One row of signals per day, using that day's open and 09:45 price plus completed earlier days only."""
    n = len(rows)
    c = np.array([r.close for r in rows], float)
    X = np.full((n, len(KEYS)), np.nan)
    for t in range(1, n):
        r, pc = rows[t], c[t - 1]
        x = {"gap": r.open / pc - 1, "first15": r.entry / r.open - 1,
             "range1": (rows[t - 1].high - rows[t - 1].low) / pc,
             "mon": float(r.day.weekday() == 0), "fri": float(r.day.weekday() == 4)}
        if t >= 2:
            x["ret1"] = pc / c[t - 2] - 1
        if t >= 6:
            x["ret5"] = pc / c[t - 6] - 1
        if t >= 21:
            x["ret20"] = pc / c[t - 21] - 1
            x["d20"] = pc / c[t - 20:t].mean() - 1
            lr = np.diff(np.log(c[t - 21:t]))
            x["vol20"] = float(np.std(lr) * math.sqrt(252))
        if t >= 200:
            x["d200"] = pc / c[t - 200:t].mean() - 1
        X[t] = [x.get(k, np.nan) for k in KEYS]
    return X


def fit(X: np.ndarray, y: np.ndarray, l2: float = L2, iters: int = 600, lr: float = 0.5) -> np.ndarray:
    w = np.zeros(X.shape[1] + 1)
    Xb = np.column_stack([np.ones(len(X)), X])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        g = Xb.T @ (p - y) / len(y)
        g[1:] += l2 * w[1:] / len(y)
        w -= lr * g
    return w


@dataclass
class DayModel:
    weights: list[float]
    mean: list[float]
    std: list[float]
    trained_through: str
    train_rows: int
    version: str = MODEL_VERSION
    threshold: float = THRESHOLD
    stop: float = STOP
    trained_at: str = ""
    evaluation: dict = field(default_factory=dict)

    def predict(self, x: np.ndarray) -> tuple[float, list[dict]]:
        z = (np.asarray(x, float) - np.array(self.mean)) / np.array(self.std)
        w = np.array(self.weights)
        logit = float(w[0] + z @ w[1:])
        p = 1 / (1 + math.exp(-max(-30.0, min(30.0, logit))))
        parts = [{"key": k, "label": LABELS[k], "value": float(x[i]), "push": float(w[1 + i] * z[i])} for i, k in enumerate(KEYS)]
        parts.sort(key=lambda d: abs(d["push"]), reverse=True)
        return p, parts


def _usable(X: np.ndarray, rows: list[DayRow], upto: int) -> list[int]:
    return [t for t in range(upto) if not np.isnan(X[t]).any() and not math.isnan(rows[t].trade_ret)]


def train(rows: list[DayRow]) -> DayModel:
    X = signals(rows)
    use = _usable(X, rows, len(rows))
    if len(use) < MIN_TRAIN:
        raise ValueError(f"only {len(use)} usable training days; need {MIN_TRAIN}")
    y = np.array([rows[t].trade_ret > 0 for t in use], float)
    mu, sd = X[use].mean(axis=0), X[use].std(axis=0)
    sd = np.where(sd < 1e-12, 1.0, sd)
    w = fit((X[use] - mu) / sd, y)
    return DayModel([float(v) for v in w], [float(v) for v in mu], [float(v) for v in sd], rows[use[-1]].day.isoformat(), len(use),
                    trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))


def walk_forward(rows: list[DayRow], first_year: int, threshold: float = THRESHOLD) -> dict:
    """Each test year is predicted by a model trained only on earlier years. No costs."""
    X = signals(rows)
    years = np.array([r.day.year for r in rows])
    taken, every = [], []
    for yr in range(first_year, int(years.max()) + 1):
        tr = [t for t in _usable(X, rows, len(rows)) if years[t] < yr]
        te = [t for t in _usable(X, rows, len(rows)) if years[t] == yr]
        if len(tr) < MIN_TRAIN or not te:
            continue
        mu, sd = X[tr].mean(axis=0), X[tr].std(axis=0)
        sd = np.where(sd < 1e-12, 1.0, sd)
        w = fit((X[tr] - mu) / sd, np.array([rows[t].trade_ret > 0 for t in tr], float))
        p = 1 / (1 + np.exp(-(w[0] + ((X[te] - mu) / sd) @ w[1:])))
        taken += [rows[t].trade_ret for t, q in zip(te, p) if q > threshold]
        every += [rows[t].trade_ret for t in te]

    def stats(r):
        a = np.array(r)
        return {"trades": len(a), "win_pct": round(float((a > 0).mean() * 100), 1) if len(a) else None,
                "total_return_pct": round(float((np.prod(1 + a) - 1) * 100), 1) if len(a) else 0.0}

    first = next((i for i, r in enumerate(rows) if r.day.year >= first_year), 0)
    return {"from": rows[first].day.isoformat(), "to": rows[-1].day.isoformat(), "ai": stats(taken), "every_day": stats(every),
            "hold_pct": round((rows[-1].close / rows[first].open - 1) * 100, 1)}


def save(model: DayModel, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / MODEL_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(model.__dict__, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)
    return p


def load(folder: Path) -> DayModel | None:
    p = folder / MODEL_FILE
    if not p.exists():
        return None
    try:
        return DayModel(**json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - a broken file means "no model", never a crash
        return None
