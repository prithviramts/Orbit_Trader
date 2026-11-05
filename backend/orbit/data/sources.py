"""Data sources: labelled offline SAMPLE generator and documented CSV import.

The Alpaca fetcher lives in ``orbit.broker.alpaca_adapter`` because it shares credentials.
"""
from __future__ import annotations

import csv
import io
import math
import random
from datetime import date
from decimal import Decimal

from orbit.data.bars import Bar, BarSeries, DataQualityError, validate_bars
from orbit.market_calendar import xnys_calendar
from orbit.money import D

SAMPLE_SOURCE = "SAMPLE: synthetic random-walk prices generated offline by Orbit (seed {seed}). NOT real SPY prices."
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 20000
ALLOWED_SYMBOLS = {"SPY"}


def generate_sample_series(seed: int = 7, start: date = date(2017, 1, 3), end: date = date(2025, 12, 31)) -> BarSeries:
    """Deterministic synthetic series with trending and falling regimes, so the rule has something to do.

    These numbers are fabricated on purpose for offline learning. They are labelled SAMPLE
    everywhere and can never be used to place orders.
    """
    rng = random.Random(seed)
    cal = xnys_calendar()
    sessions = cal.sessions_between(start, end)
    price = 225.0
    bars: list[Bar] = []
    regime_days = 0
    drift = 0.0004
    for s in sessions:
        if regime_days <= 0:
            regime_days = rng.randint(60, 320)
            drift = rng.choice([0.0007, 0.0005, 0.0003, -0.0008, -0.0015, 0.0006])
        regime_days -= 1
        vol = 0.009 if drift > 0 else 0.017
        gap = rng.gauss(0, vol * 0.35)
        o = price * math.exp(gap)
        c = o * math.exp(drift + rng.gauss(0, vol))
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol * 0.4)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol * 0.4)))
        q = lambda x: Decimal(f"{x:.2f}")  # noqa: E731
        bo, bh, bl, bc = q(o), q(hi), q(lo), q(c)
        bh = max(bh, bo, bc)
        bl = min(bl, bo, bc)
        bars.append(Bar(s.day, bo, bh, bl, bc, Decimal(rng.randint(40, 120) * 1_000_000)))
        price = c
    return BarSeries(
        symbol="SPY",
        mode="SAMPLE",
        source=SAMPLE_SOURCE.format(seed=seed),
        price_basis="split_adjusted",
        bars=bars,
        dividends_included=False,
        metadata={"seed": seed, "synthetic": True},
    )


def parse_csv_import(raw: bytes, symbol: str, price_basis: str, source_description: str) -> BarSeries:
    """Parse a user-provided daily OHLC CSV with columns date,open,high,low,close[,volume].

    Rejects oversized files, unknown symbols, malformed rows and non-session dates.
    The description is stored as provenance text and always rendered escaped.
    """
    if len(raw) > MAX_CSV_BYTES:
        raise DataQualityError(f"CSV larger than {MAX_CSV_BYTES // 1024 // 1024} MB")
    symbol = symbol.upper().strip()
    if symbol not in ALLOWED_SYMBOLS:
        raise DataQualityError("only SPY is allowed in v1")
    if price_basis not in {"raw", "split_adjusted"}:
        raise DataQualityError("price_basis must be raw or split_adjusted")
    desc = (source_description or "").strip()
    if not (3 <= len(desc) <= 300):
        raise DataQualityError("describe the CSV source (3-300 characters)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataQualityError("CSV must be UTF-8 text") from exc
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.strip().lower() for c in (reader.fieldnames or [])}
    need = {"date", "open", "high", "low", "close"}
    if not need.issubset(cols):
        raise DataQualityError(f"CSV needs columns {sorted(need)}")
    bars: list[Bar] = []
    for i, row in enumerate(reader):
        if i >= MAX_CSV_ROWS:
            raise DataQualityError(f"more than {MAX_CSV_ROWS} rows")
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        try:
            bars.append(
                Bar(
                    date.fromisoformat(r["date"][:10]),
                    D(r["open"]),
                    D(r["high"]),
                    D(r["low"]),
                    D(r["close"]),
                    D(r.get("volume") or 0),
                )
            )
        except (ValueError, KeyError) as exc:
            raise DataQualityError(f"row {i + 2}: {exc}") from exc
    bars.sort(key=lambda b: b.day)
    validate_bars(bars, xnys_calendar())
    return BarSeries(
        symbol=symbol,
        mode="CSV_IMPORT",
        source=f"CSV import: {desc}",
        price_basis=price_basis,
        bars=bars,
        dividends_included=False,
        metadata={"rows": len(bars)},
    )
