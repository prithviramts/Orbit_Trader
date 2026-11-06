"""Gold AI: a learning model that estimates the chance GLD is higher 20 trading days from now.

How it decides (all from data known at today's close):
  1. Signals: 26 statistics about GLD and SPY. They cover trend, momentum, mean-reversion (RSI, Bollinger),
     volatility, drawdown, the relationship between gold and stocks, and the outputs of every simple gold rule we tested
     (200/100-day averages, crossovers, buffered averages). The model learns how much weight each one deserves.
  2. Model: L2-regularised logistic regression on standardised signals (a probability, not a black box),
     trained only on past days whose 20-day outcome is already known. The worker retrains it monthly.
  3. Policy: buy when the chance is above ENTER (55 %), sell when it falls below EXIT (45 %), otherwise keep the
     current position. The gap between the two (hysteresis) stops it flip-flopping around 50 %.
  4. Honesty: every trained model stores its own walk-forward test on years it did not see, next to plain
     buy-and-hold, so the page can show whether the AI is earning its keep.

This module is pure computation (numpy). It has no broker access.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

MODEL_VERSION = "gold-logit-v1"
HORIZON = 20
ENTER = 0.55
EXIT = 0.45
L2 = 3.0
MIN_TRAIN = 500
MIN_HISTORY = 260  # sessions of history needed to compute every signal

SIGNALS: list[tuple[str, str]] = [
    ("ret5", "Gold return, last 5 days"),
    ("ret10", "Gold return, last 10 days"),
    ("ret20", "Gold return, last 20 days"),
    ("ret60", "Gold return, last 60 days"),
    ("ret120", "Gold return, last 120 days"),
    ("ret250", "Gold return, last 250 days"),
    ("d20", "Distance from 20-day average"),
    ("d50", "Distance from 50-day average"),
    ("d100", "Distance from 100-day average"),
    ("d200", "Distance from 200-day average"),
    ("rsi14", "RSI (14 days): overbought/oversold"),
    ("boll", "Bollinger position (20 days, in standard deviations)"),
    ("vol20", "Volatility, last 20 days"),
    ("vol_ratio", "Volatility now vs last 60 days"),
    ("dd252", "Fall from the 1-year high"),
    ("up_days", "Share of up days, last 20"),
    ("spy_ret20", "SPY return, last 20 days"),
    ("spy_d200", "SPY distance from its 200-day average"),
    ("spy_vol20", "SPY volatility, last 20 days"),
    ("rel60", "Gold vs SPY, last 60 days"),
    ("corr60", "Gold-SPY correlation, last 60 days"),
    ("rule_sma200", "Rule: above 200-day average"),
    ("rule_sma100", "Rule: above 100-day average"),
    ("rule_x50_200", "Rule: 50-day above 200-day average"),
    ("rule_x20_100", "Rule: 20-day above 100-day average"),
    ("rule_band2", "Rule: 200-day average with 2% buffer"),
]
KEYS = [k for k, _ in SIGNALS]
LABELS = dict(SIGNALS)


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _ret(x: np.ndarray, k: int) -> np.ndarray:
    r = np.full(len(x), np.nan)
    r[k:] = x[k:] / x[:-k] - 1
    return r


def _rolling(fn, x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for t in range(n - 1, len(x)):
        w = x[t - n + 1:t + 1]
        if not np.isnan(w).any():
            out[t] = fn(w)
    return out


def _band(closes: np.ndarray, sma: np.ndarray, band: float) -> np.ndarray:
    out, cur = np.full(len(closes), np.nan), 0.0
    for t, (c, a) in enumerate(zip(closes, sma)):
        if not np.isnan(a):
            cur = 1.0 if c > a * (1 + band) else (0.0 if c < a * (1 - band) else cur)
            out[t] = cur
    return out


def signals(gld: np.ndarray, spy: np.ndarray) -> np.ndarray:
    """Matrix [days x signals]; row t uses closes up to and including day t only."""
    gld, spy = np.asarray(gld, float), np.asarray(spy, float)
    lr = np.diff(np.log(gld), prepend=np.nan)
    slr = np.diff(np.log(spy), prepend=np.nan)
    s20, s50, s100, s200 = _sma(gld, 20), _sma(gld, 50), _sma(gld, 100), _sma(gld, 200)
    sd20 = _rolling(np.std, gld, 20)
    vol20, vol60 = _rolling(np.std, lr, 20), _rolling(np.std, lr, 60)
    gains = np.where(lr > 0, lr, 0.0)
    losses = np.where(lr < 0, -lr, 0.0)
    ag, al = _sma(np.nan_to_num(gains), 14), _sma(np.nan_to_num(losses), 14)
    rsi = 100 - 100 / (1 + ag / np.where(al == 0, 1e-12, al))
    hi252 = _rolling(np.max, gld, 252)
    corr = np.full(len(gld), np.nan)
    for t in range(60, len(gld)):
        a, b = lr[t - 59:t + 1], slr[t - 59:t + 1]
        if not (np.isnan(a).any() or np.isnan(b).any()) and a.std() and b.std():
            corr[t] = float(np.corrcoef(a, b)[0, 1])
    cols = {
        "ret5": _ret(gld, 5), "ret10": _ret(gld, 10), "ret20": _ret(gld, 20), "ret60": _ret(gld, 60),
        "ret120": _ret(gld, 120), "ret250": _ret(gld, 250),
        "d20": gld / s20 - 1, "d50": gld / s50 - 1, "d100": gld / s100 - 1, "d200": gld / s200 - 1,
        "rsi14": rsi, "boll": (gld - s20) / np.where(sd20 == 0, np.nan, sd20),
        "vol20": vol20, "vol_ratio": vol20 / vol60, "dd252": gld / hi252 - 1,
        "up_days": _sma((lr > 0).astype(float), 20),
        "spy_ret20": _ret(spy, 20), "spy_d200": spy / _sma(spy, 200) - 1, "spy_vol20": _rolling(np.std, slr, 20),
        "rel60": _ret(gld, 60) - _ret(spy, 60), "corr60": corr,
        "rule_sma200": np.where(np.isnan(s200), np.nan, (gld > s200).astype(float)),
        "rule_sma100": np.where(np.isnan(s100), np.nan, (gld > s100).astype(float)),
        "rule_x50_200": np.where(np.isnan(s200), np.nan, (s50 > s200).astype(float)),
        "rule_x20_100": np.where(np.isnan(s100), np.nan, (s20 > s100).astype(float)),
        "rule_band2": _band(gld, s200, 0.02),
    }
    return np.column_stack([cols[k] for k in KEYS])


def labels(gld: np.ndarray, opens: np.ndarray) -> np.ndarray:
    """1 if buying at the next open and holding HORIZON sessions would have been higher, else 0 (NaN if unknown)."""
    y = np.full(len(gld), np.nan)
    for t in range(len(gld) - HORIZON - 1):
        y[t] = float(gld[t + HORIZON] > opens[t + 1])
    return y


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
class GoldModel:
    weights: list[float]
    mean: list[float]
    std: list[float]
    trained_through: str
    train_rows: int
    version: str = MODEL_VERSION
    enter: float = ENTER
    exit: float = EXIT
    trained_at: str = ""
    evaluation: dict = field(default_factory=dict)

    def predict(self, x: np.ndarray) -> tuple[float, list[dict]]:
        """Probability plus each signal's push (positive = towards buying), largest first."""
        z = (np.asarray(x, float) - np.array(self.mean)) / np.array(self.std)
        w = np.array(self.weights)
        logit = float(w[0] + z @ w[1:])
        p = 1 / (1 + math.exp(-max(-30.0, min(30.0, logit))))
        parts = [{"key": k, "label": LABELS[k], "value": float(x[i]), "push": float(w[1 + i] * z[i])} for i, k in enumerate(KEYS)]
        parts.sort(key=lambda d: abs(d["push"]), reverse=True)
        return p, parts

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=1, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "GoldModel":
        return cls(**json.loads(raw))


def train(days: list[date], gld: np.ndarray, gld_open: np.ndarray, spy: np.ndarray, through_index: int | None = None) -> GoldModel:
    """Train on rows whose outcome is known by `through_index` (default: the last day)."""
    end = len(days) - 1 if through_index is None else through_index
    X, y = signals(gld, spy), labels(gld, gld_open)
    rows = [t for t in range(end + 1) if not np.isnan(X[t]).any() and not np.isnan(y[t]) and t + HORIZON + 1 <= end]
    if len(rows) < MIN_TRAIN:
        raise ValueError(f"only {len(rows)} usable training days; need {MIN_TRAIN}")
    mu, sd = X[rows].mean(axis=0), X[rows].std(axis=0)
    sd = np.where(sd < 1e-12, 1.0, sd)
    w = fit((X[rows] - mu) / sd, y[rows])
    return GoldModel([float(v) for v in w], [float(v) for v in mu], [float(v) for v in sd], days[end].isoformat(), len(rows),
                     trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))


def policy(prob: float, invested: bool, enter: float = ENTER, exit_: float = EXIT) -> str:
    if not invested and prob > enter:
        return "BUY"
    if invested and prob < exit_:
        return "SELL"
    return "HOLD"


def walk_forward(days: list[date], gld: np.ndarray, gld_open: np.ndarray, spy: np.ndarray, first_year: int,
                 slip: float = 0.0002, enter: float = ENTER, exit_: float = EXIT) -> dict:
    """Retrain every January on known outcomes, trade next open with hysteresis; compare with holding. No peeking."""
    X = signals(gld, spy)
    probs: list[float | None] = [None] * len(days)
    model = None
    for t, d in enumerate(days):
        if d.year >= first_year and (model is None or days[t - 1].year != d.year):
            try:
                model = train(days, gld, gld_open, spy, through_index=t - 1)
            except ValueError:
                model = None
        if model is not None and d.year >= first_year and not np.isnan(X[t]).any():
            probs[t] = model.predict(X[t])[0]
    start = next(t for t, d in enumerate(days) if d.year >= first_year)
    cash, qty, invested_days, fills = 10_000.0, 0.0, 0, 0
    pending = None
    curve, hold_curve = [], []
    hold_qty = 10_000.0 / (gld_open[start] * (1 + slip))
    hits = n_pred = 0
    for t in range(start, len(days)):
        if pending == "BUY" and qty == 0:
            qty, cash, fills = cash / (gld_open[t] * (1 + slip)), 0.0, fills + 1
        elif pending == "SELL" and qty > 0:
            cash, qty, fills = qty * gld_open[t] * (1 - slip), 0.0, fills + 1
        pending = None
        if probs[t] is not None:
            pending = policy(probs[t], qty > 0, enter, exit_)
            if t + HORIZON < len(days):
                n_pred += 1
                hits += int((probs[t] > 0.5) == (gld[t + HORIZON] > gld_open[t + 1] if t + 1 < len(days) else False))
        invested_days += qty > 0
        curve.append((days[t], cash + qty * gld[t]))
        hold_curve.append((days[t], hold_qty * gld[t]))

    def stats(c):
        v0, v1 = c[0][1], c[-1][1]
        peak, mdd = v0, 0.0
        for _, v in c:
            peak = max(peak, v)
            mdd = max(mdd, 1 - v / peak)
        yrs = max((c[-1][0] - c[0][0]).days / 365.25, 1e-9)
        return {"total_return_pct": round((v1 / v0 - 1) * 100, 2), "cagr_pct": round(((v1 / v0) ** (1 / yrs) - 1) * 100, 2),
                "max_drawdown_pct": round(mdd * 100, 2)}
    return {"from": days[start].isoformat(), "to": days[-1].isoformat(), "model": stats(curve), "hold": stats(hold_curve),
            "fills": fills, "time_invested_pct": round(100 * invested_days / (len(days) - start), 1),
            "direction_hit_rate_pct": round(100 * hits / n_pred, 1) if n_pred else None, "predictions": n_pred,
            "always_up_rate_pct": round(100 * float(np.mean([gld[t + HORIZON] > gld_open[t + 1] for t in range(start, len(days) - HORIZON - 1)])), 1),
            "curve": [(d.isoformat(), round(v, 2)) for d, v in curve[::5]], "hold_curve": [(d.isoformat(), round(v, 2)) for d, v in hold_curve[::5]]}


def save(model: GoldModel, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "gold_model.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(model.to_json(), encoding="utf-8")
    tmp.replace(path)
    return path


def load(folder: Path) -> GoldModel | None:
    path = folder / "gold_model.json"
    if not path.exists():
        return None
    try:
        return GoldModel.from_json(path.read_text(encoding="utf-8"))
    except (ValueError, TypeError, KeyError):
        return None
