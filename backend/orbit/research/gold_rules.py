"""Gold rules, step 2: rules that try to avoid whipsaw, plus a small learning model.

Every rule decides a target (1 = hold GLD, 0 = cash) from data up to and including day t's close, and the
change is executed at day t+1's open with slippage. The learning model is trained walk-forward: to
predict any day in year Y it is trained only on examples whose outcome was fully known before Y began.
Research only: nothing here is imported by the engine or worker, and nothing here can place orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from orbit.research.gold import DayBar


def simulate(bars: list[DayBar], target: list[int], slip: float, start_cash: float = 10_000.0) -> dict:
    """target[t] is decided at bar t's close and applied at bar t+1's open."""
    cash, qty = start_cash, 0.0
    curve, fills, invested = [], 0, 0
    for t, b in enumerate(bars):
        want = target[t - 1] if t > 0 else 0
        if want and qty == 0:
            qty, cash = cash / (b.open * (1 + slip)), 0.0
            fills += 1
        elif not want and qty > 0:
            cash, qty = qty * b.open * (1 - slip), 0.0
            fills += 1
        invested += qty > 0
        curve.append((b.day, cash + qty * b.close))
    return {"curve": curve, "fills": fills, "invested_days": invested}


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def sma_band(closes: np.ndarray, n: int = 200, band: float = 0.0) -> list[int]:
    """Enter above SMA*(1+band), exit below SMA*(1-band), otherwise keep the previous target."""
    s = _sma(closes, n)
    tgt, cur = [], 0
    for c, a in zip(closes, s):
        if not np.isnan(a):
            if c > a * (1 + band):
                cur = 1
            elif c < a * (1 - band):
                cur = 0
        tgt.append(cur)
    return tgt


def crossover(closes: np.ndarray, fast: int, slow: int) -> list[int]:
    f, s = _sma(closes, fast), _sma(closes, slow)
    return [int(not np.isnan(b) and a > b) for a, b in zip(f, s)]


def monthly_sma(bars: list[DayBar], closes: np.ndarray, n: int = 200) -> list[int]:
    """Only allowed to change on the last session of each month (checked once a month)."""
    s = _sma(closes, n)
    tgt, cur = [], 0
    for t, b in enumerate(bars):
        last_of_month = t + 1 == len(bars) or bars[t + 1].day.month != b.day.month
        if last_of_month and not np.isnan(s[t]):
            cur = int(closes[t] > s[t])
        tgt.append(cur)
    return tgt


FEATURES = ["return 5 days", "return 20 days", "return 60 days", "return 120 days", "distance from 50-day average",
            "distance from 200-day average", "volatility 20 days", "SPY return 20 days", "SPY distance from 200-day average"]
HORIZON = 20  # the model predicts whether GLD is higher 20 sessions later


def features(gld: np.ndarray, spy: np.ndarray) -> np.ndarray:
    def ret(x, k):
        r = np.full(len(x), np.nan)
        r[k:] = x[k:] / x[:-k] - 1
        return r
    lr = np.diff(np.log(gld), prepend=np.nan)
    vol = np.full(len(gld), np.nan)
    for t in range(20, len(gld)):
        vol[t] = np.std(lr[t - 19:t + 1])
    return np.column_stack([ret(gld, 5), ret(gld, 20), ret(gld, 60), ret(gld, 120), gld / _sma(gld, 50) - 1,
                            gld / _sma(gld, 200) - 1, vol, ret(spy, 20), spy / _sma(spy, 200) - 1])


def _fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 400, lr: float = 0.5) -> np.ndarray:
    w = np.zeros(X.shape[1] + 1)
    Xb = np.column_stack([np.ones(len(X)), X])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w))
        g = Xb.T @ (p - y) / len(y)
        g[1:] += l2 * w[1:] / len(y)
        w -= lr * g
    return w


@dataclass
class ModelRun:
    target: list[int]
    prob: list[float | None]
    yearly_weights: dict[int, list[float]]


def walk_forward_model(bars: list[DayBar], gld: np.ndarray, spy: np.ndarray, first_year: int, threshold: float = 0.5,
                       l2: float = 1.0) -> ModelRun:
    """Logistic regression retrained at the start of each year on examples whose 20-day outcome is already known."""
    X = features(gld, spy)
    opens = np.array([b.open for b in bars])
    y = np.full(len(bars), np.nan)
    for t in range(len(bars) - HORIZON - 1):
        y[t] = float(gld[t + HORIZON] > opens[t + 1])  # bought next open, higher 20 sessions later?
    ok = ~np.isnan(X).any(axis=1)
    target, prob = [0] * len(bars), [None] * len(bars)
    weights = {}
    years = sorted({b.day.year for b in bars if b.day.year >= first_year})
    for yr in years:
        idx = [t for t, b in enumerate(bars) if b.day.year == yr]
        start = idx[0]
        train = [t for t in range(start) if ok[t] and not np.isnan(y[t]) and t + HORIZON + 1 < start]  # outcome known before the year
        if len(train) < 250:
            continue
        mu, sd = X[train].mean(axis=0), X[train].std(axis=0) + 1e-12
        w = _fit_logistic((X[train] - mu) / sd, y[train], l2=l2)
        weights[yr] = [round(float(v), 3) for v in w]
        for t in idx:
            if ok[t]:
                p = 1 / (1 + np.exp(-(w[0] + ((X[t] - mu) / sd) @ w[1:])))
                prob[t] = float(p)
                target[t] = int(p > threshold)
    return ModelRun(target, prob, weights)


def slice_index(bars: list[DayBar], start: date, end: date) -> tuple[int, int]:
    i = next(k for k, b in enumerate(bars) if b.day >= start)
    j = max(k for k, b in enumerate(bars) if b.day <= end)
    return i, j + 1
