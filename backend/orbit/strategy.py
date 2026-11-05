"""Experimental SPY 200-session moving-average rule (UNVALIDATED).

One implementation shared by the backtester and the live worker.

Rule, for a completed regular session t:
- Use the close of t and the trailing 200 valid session closes *including* t.
- close >  SMA200 -> target INVESTED
- close <  SMA200 -> target CASH
- close == SMA200 -> keep previous target (initial target is CASH)
- fewer than 200 closes -> INSUFFICIENT_DATA (no target change, no order)
The resulting target is acted on in the *next* regular session (t+1).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

from orbit.money import D

INVESTED = "INVESTED"
CASH = "CASH"
INSUFFICIENT = "INSUFFICIENT_DATA"

STRATEGY_NAME = "spy_sma200"
STRATEGY_LABEL = "EXPERIMENTAL / UNVALIDATED: SPY 200-session moving-average trend rule"


@dataclass(frozen=True)
class StrategyParams:
    """Versioned settings. Editing creates a new version effective for future decisions only."""

    symbol: str = "SPY"
    sma_window: int = 200
    completion_buffer_minutes: int = 15
    entry_fraction: Decimal = Decimal("0.25")
    max_entry_notional_usd: Decimal = Decimal("250")
    cash_reserve_usd: Decimal = Decimal("10")
    execution_window_minutes: int = 5
    min_order_notional_usd: Decimal = Decimal("1")

    def validate(self) -> None:
        if self.symbol != "SPY":
            raise ValueError("v1 allowlists SPY only")
        if not (20 <= self.sma_window <= 400):
            raise ValueError("sma_window must be 20-400")
        if not (0 <= self.completion_buffer_minutes <= 120):
            raise ValueError("completion buffer must be 0-120 minutes")
        if not (Decimal("0.01") <= self.entry_fraction <= Decimal("1")):
            raise ValueError("entry_fraction must be between 1% and 100%")
        if not (Decimal("1") <= self.max_entry_notional_usd <= Decimal("5000")):
            raise ValueError("max entry notional must be USD 1-5000")
        if not (Decimal("0") <= self.cash_reserve_usd <= Decimal("1000")):
            raise ValueError("cash reserve must be USD 0-1000")
        if not (1 <= self.execution_window_minutes <= 30):
            raise ValueError("execution window must be 1-30 minutes")
        if self.min_order_notional_usd < Decimal("1"):
            raise ValueError("minimum order notional must be at least USD 1")

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyParams":
        base = cls()
        kw = {}
        for k, v in d.items():
            if not hasattr(base, k):
                continue
            cur = getattr(base, k)
            kw[k] = D(v) if isinstance(cur, Decimal) else type(cur)(v)
        p = cls(**kw)
        p.validate()
        return p


@dataclass(frozen=True)
class SignalResult:
    session: date
    close: Decimal | None
    sma: Decimal | None
    window_count: int
    previous_target: str
    target: str  # INVESTED | CASH | INSUFFICIENT_DATA
    comparison: str  # ABOVE | BELOW | EQUAL | N/A
    window_first_session: date | None = None
    inputs: list[tuple[str, str]] = field(default_factory=list, repr=False)

    def to_dict(self, include_inputs: bool = False) -> dict:
        d = {
            "session": self.session.isoformat(),
            "close": None if self.close is None else str(self.close),
            "sma": None if self.sma is None else str(self.sma),
            "window_count": self.window_count,
            "window_first_session": self.window_first_session.isoformat() if self.window_first_session else None,
            "previous_target": self.previous_target,
            "target": self.target,
            "comparison": self.comparison,
        }
        if include_inputs:
            d["inputs"] = self.inputs
        return d


def evaluate_signal(
    closes: list[tuple[date, Decimal]], session: date, previous_target: str, window: int = 200
) -> SignalResult:
    """Evaluate the rule for ``session`` using only closes dated on or before it.

    ``closes`` may contain later data (e.g. in a backtest); anything after ``session``
    is ignored so future prices can never leak into the decision.
    """
    if previous_target not in (INVESTED, CASH):
        previous_target = CASH
    usable = [(d, D(c)) for d, c in closes if d <= session]
    if not usable or usable[-1][0] != session:
        return SignalResult(session, None, None, 0, previous_target, INSUFFICIENT, "N/A")
    tail = usable[-window:]
    close = tail[-1][1]
    if len(tail) < window:
        return SignalResult(session, close, None, len(tail), previous_target, INSUFFICIENT, "N/A")
    for _, c in tail:
        if c <= 0:
            raise ValueError("non-positive close in window")
    sma = sum((c for _, c in tail), Decimal(0)) / Decimal(window)
    if close > sma:
        target, cmp_ = INVESTED, "ABOVE"
    elif close < sma:
        target, cmp_ = CASH, "BELOW"
    else:
        target, cmp_ = previous_target, "EQUAL"
    return SignalResult(
        session, close, sma, len(tail), previous_target, target, cmp_, tail[0][0],
        [(d.isoformat(), str(c)) for d, c in tail],
    )


def sma_series(closes: list[tuple[date, Decimal]], window: int = 200) -> list[tuple[date, Decimal | None]]:
    out: list[tuple[date, Decimal | None]] = []
    running = Decimal(0)
    vals = [D(c) for _, c in closes]
    for i, (d, c) in enumerate(closes):
        running += vals[i]
        if i >= window:
            running -= vals[i - window]
        out.append((d, running / window if i >= window - 1 else None))
    return out


def entry_notional(equity: Decimal, available_cash: Decimal, p: StrategyParams, est_fee: Decimal = Decimal(0)) -> dict:
    """min(fraction x equity, hard cap, unborrowed cash - reserve - fee). Returns the limiting factor too."""
    by_fraction = D(equity) * p.entry_fraction
    by_cap = p.max_entry_notional_usd
    by_cash = D(available_cash) - p.cash_reserve_usd - D(est_fee)
    target = min(by_fraction, by_cap, by_cash)
    limiter = "+".join(n for n, v in (("fraction", by_fraction), ("cap", by_cap), ("cash", by_cash)) if v == target)
    if target < 0:
        target = Decimal(0)
    return {
        "target": target,
        "by_fraction": by_fraction,
        "by_cap": by_cap,
        "by_cash": by_cash,
        "limited_by": limiter,
        "too_small": target < p.min_order_notional_usd,
    }
