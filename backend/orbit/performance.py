"""Strategy performance from Orbit's own confirmed fills, marked at stored official daily closes.

Benchmark ("buy and hold the same money"): every time the strategy buys, the benchmark buys the same
USD amount at the same price, and it never sells. So the gap between the two lines shows exactly what
the rule's selling (stepping aside into cash) has gained or cost.

Only this strategy's fills count; other holdings in the paper account are ignored. Fees are zero on
Alpaca for these orders. Prices are not split-adjusted against fills (no SPY split is expected).
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.accounting import Ledger
from orbit.db import Fill, OrderIntent
from orbit.market_calendar import NEW_YORK

MIN_SESSIONS_TO_JUDGE = 120  # roughly six months of trading days


def _fills(s: Session, account_key: str, symbol: str) -> list[Fill]:
    return list(s.scalars(select(Fill).join(OrderIntent, Fill.intent_id == OrderIntent.id)
                          .where(OrderIntent.account_key == account_key, OrderIntent.symbol == symbol)
                          .order_by(Fill.at, Fill.id)))


def _q(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.01")))


def compute(fills: list[Fill], closes: list[tuple[date, Decimal]]) -> dict:
    if not fills:
        return {"available": False, "reason": "No trades yet. The chart starts on the day of the first fill."}
    first_day = fills[0].at.astimezone(NEW_YORK).date()
    days = [(d, c) for d, c in closes if d >= first_day]
    if not days:
        return {"available": False, "reason": "No official daily close is stored for the days since the first fill yet. It appears after the next evaluation."}

    led = Ledger(Decimal("1e15"))
    bench_qty = bench_cost = Decimal(0)
    peak_cost = Decimal(0)
    trips: list[dict] = []
    open_trip: dict | None = None
    i = 0
    series = []
    invested_days = 0
    hwm = None
    max_dd = Decimal(0)
    for d, close in days:
        while i < len(fills) and fills[i].at.astimezone(NEW_YORK).date() <= d:
            f = fills[i]
            if f.side == "BUY":
                led.buy(f.qty, f.price)
                bench_qty += f.qty
                bench_cost += f.qty * f.price
                if open_trip is None:
                    open_trip = {"opened": f.at.astimezone(NEW_YORK).date().isoformat(), "cost": Decimal(0), "pnl": Decimal(0)}
                open_trip["cost"] += f.qty * f.price
            else:
                r = led.sell(min(f.qty, led.qty), f.price)
                if open_trip is not None:
                    open_trip["pnl"] += r
                    if led.qty == 0:
                        open_trip["closed"] = f.at.astimezone(NEW_YORK).date().isoformat()
                        trips.append(open_trip)
                        open_trip = None
            peak_cost = max(peak_cost, led.cost_basis)
            i += 1
        pnl = led.realized_pnl + led.qty * close - led.cost_basis
        bench = bench_qty * close - bench_cost
        if led.qty > 0:
            invested_days += 1
        hwm = pnl if hwm is None else max(hwm, pnl)
        max_dd = max(max_dd, hwm - pnl)
        series.append({"day": d.isoformat(), "strategy": _q(pnl), "benchmark": _q(bench), "invested": led.qty > 0, "close": str(close)})

    last_close = days[-1][1]
    pnl = led.realized_pnl + led.qty * last_close - led.cost_basis
    bench = bench_qty * last_close - bench_cost
    base = peak_cost or Decimal(1)

    months: OrderedDict[str, dict] = OrderedDict()
    prev_s = prev_b = Decimal(0)
    for p in series:
        m = p["day"][:7]
        cur_s, cur_b = Decimal(p["strategy"]), Decimal(p["benchmark"])
        e = months.setdefault(m, {"month": m, "strategy": Decimal(0), "benchmark": Decimal(0)})
        e["strategy"] += cur_s - prev_s
        e["benchmark"] += cur_b - prev_b
        prev_s, prev_b = cur_s, cur_b

    closed = [{"opened": t["opened"], "closed": t["closed"], "cost": _q(t["cost"]), "pnl": _q(t["pnl"]),
               "pct": str((t["pnl"] / t["cost"] * 100).quantize(Decimal("0.01"))) if t["cost"] else "0.00"} for t in trips]
    wins = sum(1 for t in trips if t["pnl"] > 0)
    n = len(series)
    if n < MIN_SESSIONS_TO_JUDGE:
        verdict = ("TOO_EARLY", f"Only {n} trading day{'s' if n != 1 else ''} of results. About {MIN_SESSIONS_TO_JUDGE} are needed before the comparison means much.")
    elif pnl > bench:
        verdict = ("AHEAD", "The rule is ahead of simply holding the same money. Keep watching: one good stretch is not proof.")
    elif pnl < bench:
        verdict = ("BEHIND", "The rule is behind simply holding the same money. That is normal in steadily rising markets; it is meant to help in long falls.")
    else:
        verdict = ("LEVEL", "The rule and simply holding are level so far (it has not sold yet).")
    return {
        "available": True,
        "as_of": days[-1][0].isoformat(),
        "sessions": n,
        "summary": {
            "strategy_pnl": _q(pnl), "benchmark_pnl": _q(bench), "difference": _q(pnl - bench),
            "strategy_pct": str((pnl / base * 100).quantize(Decimal("0.01"))),
            "benchmark_pct": str((bench / (bench_cost or Decimal(1)) * 100).quantize(Decimal("0.01"))),
            "money_used": _q(peak_cost), "realized": _q(led.realized_pnl), "unrealized": _q(led.qty * last_close - led.cost_basis),
            "held_qty": str(led.qty), "max_drawdown": _q(max_dd),
            "invested_days": invested_days, "cash_days": n - invested_days,
            "round_trips": len(trips), "wins": wins, "losses": len(trips) - wins,
        },
        "verdict": {"code": verdict[0], "text": verdict[1]},
        "series": series,
        "months": [{"month": e["month"], "strategy": _q(e["strategy"]), "benchmark": _q(e["benchmark"])} for e in months.values()],
        "trades": closed,
        "open_trade": {"opened": open_trip["opened"], "cost": _q(open_trip["cost"])} if open_trip else None,
        "notes": [
            "Benchmark: every time the rule buys, imagine buying the same dollars of SPY and never selling.",
            "Values use official daily closes stored by Orbit, so today's live move is not included yet.",
            "Only this strategy's trades count. The rest of the paper account (the Alpaca starting balance) is left out.",
        ],
    }


def for_account(s: Session, account_key: str | None, symbol: str, closes: list[tuple[date, Decimal]]) -> dict:
    if not account_key:
        return {"available": False, "reason": "Connect the Alpaca paper account first."}
    return compute(_fills(s, account_key, symbol), closes)
