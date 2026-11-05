"""Decimal money helpers and the USD/AED reference conversion.

Invariants
- Money is always ``Decimal``; never float.
- USD is the calculation currency. AED is a *display* conversion:
  USD -> AED is multiplication, AED -> USD is division.
- Full precision is kept internally; rounding to 2 dp (ROUND_HALF_UP) only
  happens at display/serialisation time, and is applied to the converted
  full-precision value, never to a sum of already-rounded values.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation, getcontext

getcontext().prec = 34

CENT = Decimal("0.01")
QTY_STEP = Decimal("0.000000001")  # Alpaca fractional quantities: up to 9 decimals

DEFAULT_USD_TO_AED = Decimal("3.6725")
FX_SOURCE = "Configured reference rate (AED peg, see centralbank.ae). Not an executed bank conversion."


def D(value) -> Decimal:
    """Convert a value to Decimal safely. Floats are routed via ``repr`` to avoid binary noise."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        d = Decimal(repr(value))
    else:
        try:
            d = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    if not d.is_finite():
        raise ValueError(f"non-finite number: {value!r}")
    return d


def usd_to_aed(usd: Decimal, rate: Decimal = DEFAULT_USD_TO_AED) -> Decimal:
    return D(usd) * D(rate)


def aed_to_usd(aed: Decimal, rate: Decimal = DEFAULT_USD_TO_AED) -> Decimal:
    r = D(rate)
    if r <= 0:
        raise ValueError("FX rate must be positive")
    return D(aed) / r


def round_money(value: Decimal) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def floor_qty(qty: Decimal, step: Decimal = QTY_STEP) -> Decimal:
    """Round a quantity *down* so rounding can never buy more than cash allows or sell more than held."""
    q = D(qty)
    if q < 0:
        raise ValueError("quantity cannot be negative")
    return q.quantize(step, rounding=ROUND_DOWN)


def money_pair(usd: Decimal | None, rate: Decimal = DEFAULT_USD_TO_AED) -> dict | None:
    """Serialise a USD amount with its AED reference equivalent, both as strings (2 dp) plus full precision."""
    if usd is None:
        return None
    u = D(usd)
    return {
        "usd": f"{round_money(u):.2f}",
        "aed": f"{round_money(usd_to_aed(u, rate)):.2f}",
        "usd_exact": str(u),
    }


def fx_assumption(rate: Decimal = DEFAULT_USD_TO_AED) -> dict:
    return {"pair": "USD/AED", "rate": str(rate), "source": FX_SOURCE, "kind": "reference"}
