"""Historical simulation of the SMA rule, the risk overlay and two benchmarks.

Execution model (explicit and simplified - a SIMULATOR LIMITATION, not a broker match):
- Signal from completed day t; execution at day t+1 OPEN +/- adverse slippage, plus a fixed fee.
  The real paper worker submits shortly after the open, so actual fills will differ.
- Entry notional frozen at decision time; reduced at execution if cash (after reserve and
  fee) is short, e.g. after an adverse gap. Below the minimum order size -> skipped.
- Quantities are floored to 9 decimals, so rounding never spends more than available.
- Exits sell the full strategy holding; no shorting, no borrowing.
- If t+1 is not the next exchange session in the data (gap), the execution is recorded as
  MISSED_EXECUTION. Live operation requires a review; the simulator models that review as
  "re-assess on the next decision" and states so in the assumptions.
- Daily OHLC cannot reveal intraday paths, so nothing is filled intraday.
- Price-only unless reliable dividend data is supplied (it is not, in v1). Cash earns 0%.
Only the strategy applies the risk overlay. Benchmarks buy and hold.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from orbit.accounting import Ledger
from orbit.data.bars import BarSeries, split_adjust
from orbit.market_calendar import MarketCalendar, xnys_calendar
from orbit.money import D, floor_qty
from orbit.risk import DrawdownState, RiskPolicy, update_drawdown
from orbit.strategy import CASH, INSUFFICIENT, INVESTED, StrategyParams, entry_notional, evaluate_signal


@dataclass(frozen=True)
class CostModel:
    slippage_bps: Decimal = Decimal("5")
    fee_per_order_usd: Decimal = Decimal("0")

    def to_dict(self):
        return {"slippage_bps": str(self.slippage_bps), "fee_per_order_usd": str(self.fee_per_order_usd)}


@dataclass
class _Portfolio:
    name: str
    ledger: Ledger
    equity: list[tuple[date, Decimal]] = field(default_factory=list)
    exposure: list[Decimal] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    round_trips: int = 0


def _slip(price: Decimal, side: str, costs: CostModel) -> Decimal:
    f = costs.slippage_bps / Decimal(10000)
    return price * (1 + f) if side == "BUY" else price * (1 - f)


def _buy(pf: _Portfolio, day: date, open_px: Decimal, notional: Decimal, reserve: Decimal, costs: CostModel,
         min_notional: Decimal, reason: str, ref: str | None) -> dict:
    px = _slip(open_px, "BUY", costs)
    fee = costs.fee_per_order_usd
    available = pf.ledger.cash - reserve - fee
    used = min(notional, available)
    reduced = used < notional
    if used < min_notional:
        return {"status": "SKIPPED", "reason": f"Notional {used:.2f} below minimum after cash check (reserve+fee)."}
    qty = floor_qty(used / px)
    if qty <= 0:
        return {"status": "SKIPPED", "reason": "Quantity rounds to zero."}
    pf.ledger.buy(qty, px, fee, day)
    t = {
        "day": day.isoformat(), "side": "BUY", "qty": str(qty), "price": str(px), "open": str(open_px),
        "fee": str(fee), "notional": str(qty * px), "frozen_target": str(notional),
        "reduced_for_cash": reduced, "residual_cash": str(pf.ledger.cash), "reason": reason, "decision_ref": ref,
    }
    pf.trades.append(t)
    return {"status": "FILLED", **t}


def _sell(pf: _Portfolio, day: date, open_px: Decimal, costs: CostModel, reason: str, ref: str | None) -> dict:
    qty = pf.ledger.qty
    if qty <= 0:
        return {"status": "SKIPPED", "reason": "Nothing held."}
    px = _slip(open_px, "SELL", costs)
    realized = pf.ledger.sell(qty, px, costs.fee_per_order_usd)
    pf.round_trips += 1
    t = {
        "day": day.isoformat(), "side": "SELL", "qty": str(qty), "price": str(px), "open": str(open_px),
        "fee": str(costs.fee_per_order_usd), "notional": str(qty * px), "realized_pnl": str(realized),
        "residual_cash": str(pf.ledger.cash), "reason": reason, "decision_ref": ref,
    }
    pf.trades.append(t)
    return {"status": "FILLED", **t}


def compute_metrics(equity: list[tuple[date, Decimal]], pf: _Portfolio | None, start_cash: Decimal) -> dict:
    if not equity:
        return {"note": "no data"}
    end = equity[-1][1]
    n = len(equity)
    hwm = Decimal(0)
    mdd = Decimal(0)
    dd_series = []
    for d, e in equity:
        hwm = max(hwm, e)
        dd = (hwm - e) / hwm if hwm > 0 else Decimal(0)
        mdd = max(mdd, dd)
        dd_series.append((d.isoformat(), str(-dd)))
    net = end / start_cash - 1
    m = {
        "start_equity": str(start_cash),
        "ending_equity": str(end),
        "net_return": str(net),
        "max_drawdown": str(mdd),
        "sessions": n,
    }
    if n >= 252:
        years = n / 252
        m["annualized_return"] = str(Decimal(str(math.pow(float(end / start_cash), 1 / years) - 1)))
        m["annualized_note"] = f"Approximation over {years:.2f} years (252 sessions/year)."
    else:
        m["annualized_return"] = None
        m["annualized_note"] = "N/A: fewer than 252 sessions; annualising a short period would exaggerate."
    if pf is not None:
        avg_eq = sum((e for _, e in equity), Decimal(0)) / n
        m["time_in_market"] = str(Decimal(sum(1 for x in pf.exposure if x > 0)) / n)
        m["average_exposure"] = str(sum(pf.exposure, Decimal(0)) / n)
        m["turnover"] = str(pf.ledger.traded_notional / avg_eq) if avg_eq > 0 else None
        m["completed_round_trips"] = pf.round_trips
        m["fills"] = len(pf.trades)
        slip_cost = Decimal(0)
        for t in pf.trades:
            slip_cost += abs(D(t["price"]) - D(t["open"])) * D(t["qty"])
        m["estimated_costs"] = str(pf.ledger.fees_paid + slip_cost)
        m["fees"] = str(pf.ledger.fees_paid)
        m["estimated_slippage"] = str(slip_cost)
        m["realized_pnl"] = str(pf.ledger.realized_pnl)
    return {"metrics": m, "drawdown": dd_series}


def run_backtest(
    series: BarSeries,
    params: StrategyParams = StrategyParams(),
    risk: RiskPolicy = RiskPolicy(),
    costs: CostModel = CostModel(),
    starting_cash: Decimal = Decimal("1000"),
    start: date | None = None,
    end: date | None = None,
    calendar: MarketCalendar | None = None,
) -> dict:
    params.validate()
    risk.validate()
    cal = calendar or xnys_calendar()
    starting_cash = D(starting_cash)
    raw = series.bars
    if series.price_basis == "raw":
        sig_bars = split_adjust(raw, series.splits)
        splits_by_day = {s.ex_date: s.ratio for s in series.splits}
    else:
        sig_bars = raw
        splits_by_day = {}
    closes_all = [(b.day, b.close) for b in sig_bars]
    idx_of = {b.day: i for i, b in enumerate(raw)}

    # Evaluation starts at the first session with a full window that is >= start.
    first_eval = params.sma_window - 1
    if start:
        first_eval = max(first_eval, next((i for i, b in enumerate(raw) if b.day >= start), len(raw)))
    last = len(raw) - 1
    if end:
        last = max(i for i, b in enumerate(raw) if b.day <= end)
    if first_eval > last - 1:
        return {"ok": False, "error": f"Insufficient data: need {params.sma_window} sessions of warm-up plus at least 2 sessions to simulate.", "data": series.describe()}

    strat = _Portfolio("strategy", Ledger(starting_cash))
    matched = _Portfolio("matched_entry_benchmark", Ledger(starting_cash))
    full = _Portfolio("full_investment_benchmark", Ledger(starting_cash))
    dd = DrawdownState()
    decisions: list[dict] = []
    risk_events: list[dict] = []
    pending: dict | None = None
    target_state = CASH
    benchmarks_entered = False

    for i in range(first_eval, last + 1):
        bar = raw[i]
        day = bar.day
        # 1) corporate actions before the open
        if day in splits_by_day:
            for pf in (strat, matched, full):
                pf.ledger.apply_split(splits_by_day[day])
        # 2) executions at the open (never on the first evaluation day: decisions there act tomorrow)
        if i > first_eval:
            prev_day = raw[i - 1].day
            expected = cal.next_session(prev_day)
            gap = expected is not None and expected.day != day
            if not benchmarks_entered and not gap:
                frac = entry_notional(starting_cash, matched.ledger.cash, params, costs.fee_per_order_usd)
                _buy(matched, day, bar.open, frac["target"], params.cash_reserve_usd, costs,
                     params.min_order_notional_usd, "Matched-entry benchmark initial allocation", None)
                full_notional = full.ledger.cash - costs.fee_per_order_usd
                _buy(full, day, bar.open, full_notional, Decimal(0), costs, params.min_order_notional_usd,
                     "Full-investment benchmark initial allocation", None)
                benchmarks_entered = True
            if pending is not None:
                dref = pending["decision_ref"]
                if gap:
                    pending["decision"]["execution"] = {"status": "MISSED_EXECUTION", "reason": f"Data gap: expected session {expected.day} missing; no catch-up order."}
                elif pending["action"] == "BUY":
                    if dd.entries_paused:
                        pending["decision"]["execution"] = {"status": "CANCELLED", "reason": "RISK_PAUSED_ENTRIES latched before execution."}
                    else:
                        pending["decision"]["execution"] = _buy(strat, day, bar.open, pending["notional"], params.cash_reserve_usd, costs, params.min_order_notional_usd, "SMA entry", dref)
                else:
                    pending["decision"]["execution"] = _sell(strat, day, bar.open, costs, "SMA exit", dref)
                pending = None
        # 3) close: mark to market, drawdown overlay (strategy only)
        for pf in (strat, matched, full):
            e = pf.ledger.equity(bar.close)
            pf.equity.append((day, e))
            pf.exposure.append(pf.ledger.market_value(bar.close) / e if e > 0 else Decimal(0))
        for ev in update_drawdown(dd, strat.equity[-1][1], risk):
            risk_events.append({"day": day.isoformat(), "event": ev, "drawdown": str(dd.drawdown), "high_water_mark": str(dd.high_water_mark)})
        # 4) evaluate the rule with data up to and including today's close only
        sig = evaluate_signal(closes_all[: i + 1], day, target_state, params.sma_window)
        held = strat.ledger.qty
        ref = f"bt-{day.isoformat()}"
        dec = {"ref": ref, "session": day.isoformat(), "close": str(sig.close) if sig.close is not None else None,
               "sma": str(sig.sma) if sig.sma is not None else None, "comparison": sig.comparison,
               "previous_target": sig.previous_target, "target": sig.target, "held_qty": str(held),
               "equity": str(strat.equity[-1][1]), "cash": str(strat.ledger.cash),
               "drawdown": str(dd.drawdown), "entries_paused": dd.entries_paused}
        if sig.target == INSUFFICIENT:
            dec.update(action="SKIP", reason="Insufficient completed closes for the 200-session average.")
        else:
            target_state = sig.target
            if i == last:
                nxt = None
            else:
                nxt = raw[i + 1].day
            if sig.target == INVESTED and held == 0:
                if dd.entries_paused:
                    dec.update(action="SKIP", reason="Signal is INVESTED but RISK_PAUSED_ENTRIES is latched; no new buys.")
                else:
                    size = entry_notional(strat.equity[-1][1], strat.ledger.cash, params, costs.fee_per_order_usd)
                    dec["sizing"] = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in size.items()}
                    if size["too_small"]:
                        dec.update(action="SKIP", reason="Entry size below minimum order after limits and reserve.")
                    elif nxt is None:
                        dec.update(action="BUY", reason="Close above average; next session is beyond the data range (not executed).")
                    else:
                        dec.update(action="BUY", reason=f"Close above 200-session average while flat; buy at next session open (limited by {size['limited_by']}).")
                        pending = {"action": "BUY", "notional": size["target"], "decision_ref": ref, "decision": dec}
            elif sig.target == CASH and held > 0:
                if nxt is None:
                    dec.update(action="SELL", reason="Close below average; next session beyond data range (not executed).")
                else:
                    dec.update(action="SELL", reason="Close below 200-session average while invested; sell strategy holding at next open.")
                    pending = {"action": "SELL", "decision_ref": ref, "decision": dec}
            else:
                why = "equal to average: keep previous state" if sig.comparison == "EQUAL" else (
                    "already invested; no resizing" if held > 0 else "below average and already in cash")
                dec.update(action="HOLD", reason=f"Close {sig.comparison.lower()} average; {why}.")
        decisions.append(dec)

    out = {
        "ok": True,
        "data": series.describe(),
        "params": params.to_dict(),
        "risk_policy": risk.to_dict(),
        "costs": costs.to_dict(),
        "starting_cash": str(starting_cash),
        "range": {"first_session": raw[first_eval].day.isoformat(), "last_session": raw[last].day.isoformat()},
        "assumptions": [
            "Signal on completed close t; fills at t+1 open with adverse slippage: an approximation, not a broker fill.",
            "Price-only comparison: dividends excluded from strategy and both benchmarks. Cash earns 0%.",
            "Only the strategy applies the drawdown review overlay; in simulation a latched entry pause stays latched (no human review).",
            "Missing sessions are not filled; an execution falling on a gap is recorded as MISSED_EXECUTION and re-assessed at the next decision.",
            "Matched-entry benchmark matches the initial allocation only, not exposure over time.",
            f"Price basis: {series.price_basis}; splits applied to simulated quantities when basis is raw.",
        ],
        "decisions": decisions,
        "risk_events": risk_events,
        "portfolios": {},
    }
    for pf in (strat, matched, full):
        res = compute_metrics(pf.equity, pf, starting_cash)
        out["portfolios"][pf.name] = {
            **res,
            "equity": [(d.isoformat(), str(e)) for d, e in pf.equity],
            "trades": pf.trades,
        }
    return out
