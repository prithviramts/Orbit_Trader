"""Beginner learning content and deterministic decision explanations.

Explanations are built only from stored values with fixed templates. They never invent a narrative
for why the market moved and never claim the rule is validated.
"""
from __future__ import annotations

from decimal import Decimal

from orbit.money import money_pair

SOURCES = {
    "investor_orders": "https://www.investor.gov/introduction-investing/investing-basics/how-stock-markets-work/types-orders",
    "investor_etf": "https://www.investor.gov/introduction-investing/investing-basics/investment-products/mutual-funds-and-exchange-traded-1",
    "nyse_hours": "https://www.nyse.com/markets/hours-calendars",
    "alpaca_paper": "https://docs.alpaca.markets/us/docs/paper-trading",
    "alpaca_orders": "https://docs.alpaca.markets/us/docs/working-with-orders",
    "alpaca_data": "https://docs.alpaca.markets/us/docs/about-market-data-api",
    "cbuae_fx": "https://centralbank.ae/en/forex-eibor/exchange-rates/",
}

GLOSSARY = [
    ("ticker", "Ticker", "A short code for a traded security. SPY is the ticker of an ETF that tracks the S&P 500 index.", "investor_etf"),
    ("etf", "ETF", "Exchange-traded fund: a basket of securities that trades like one stock. Buying SPY gives exposure to ~500 large U.S. companies.", "investor_etf"),
    ("bid_ask", "Bid / ask", "Bid is the highest price buyers currently offer; ask is the lowest price sellers accept. You usually buy near the ask and sell near the bid.", "investor_orders"),
    ("spread", "Spread", "Ask minus bid. A cost you pay implicitly when trading with market orders.", "investor_orders"),
    ("market_order", "Market order", "Buy or sell now at the best available price. Fast, but the price is not guaranteed and can differ from the last price you saw.", "investor_orders"),
    ("limit_order", "Limit order", "Buy or sell only at your price or better. The price is protected, but the order may never fill.", "investor_orders"),
    ("stop_order", "Stop order", "Becomes a market order once a trigger price trades. It can fill well beyond the trigger in a fast or gapping market.", "investor_orders"),
    ("fill", "Fill", "An executed trade. An order that is accepted is not yet filled; it may fill fully, partly, or not at all.", "alpaca_orders"),
    ("position", "Position", "What you currently own. Here, the strategy only counts shares from confirmed fills of its own orders.", None),
    ("realized_pnl", "Realized P&L", "Profit or loss locked in by selling. Uses FIFO cost basis with purchase fees included.", None),
    ("unrealized_pnl", "Unrealized P&L", "Paper gain or loss on shares still held, using the current price. It changes until you sell.", None),
    ("exposure", "Exposure", "How much of your equity is invested. 25% exposure means a 10% SPY drop costs about 2.5% of equity.", None),
    ("drawdown", "Drawdown", "How far equity is below its previous peak (high-water mark). A 10% drawdown means 10% below the best close so far.", None),
    ("slippage", "Slippage", "Difference between the price you expected and the price you got. Backtests add an assumed adverse slippage.", None),
    ("benchmark", "Benchmark", "A simple alternative to compare against, e.g. buying and holding. A strategy is only interesting if it beats a fair benchmark after costs.", None),
    ("leakage", "Data leakage", "Accidentally using information from the future when testing. It makes results look better than anything achievable live.", None),
    ("overfitting", "Overfitting", "Tuning rules until they fit past noise. Overfit rules look great in backtests and disappoint later. Holdout data helps detect this.", None),
    ("sma", "Moving average (SMA)", "The average of the last N closing prices. Here N = 200 sessions, including the latest close.", None),
    ("session", "Trading session", "A regular NYSE trading day, normally 9:30-16:00 New York time. Holidays and early closes come from the exchange calendar.", "nyse_hours"),
    ("paper", "Paper trading", "Simulated trading with fake money at the broker. Fills are simulated and can differ from real markets (no market impact, random partial fills).", "alpaca_paper"),
    ("fx_reference", "Reference FX rate", "AED amounts use a fixed reference rate (3.6725 AED per USD). It is not what a bank would charge you.", "cbuae_fx"),
]

LESSONS = [
    {"id": "l1", "title": "What paper trading is (and is not)",
     "body": ["Paper trading sends orders to a simulated broker account. No real money moves.",
              "Simulated fills skip things real markets have: market impact, queue position, and some fees. Alpaca also fills partially at random about 10% of the time.",
              "A paper profit is not money you can spend, and a good paper result does not mean a strategy works live."],
     "check": {"q": "An order shows 'accepted'. Have you bought shares?", "options": ["Yes", "Not necessarily"], "answer": 1,
               "why": "Accepted means the broker received the order. Only a fill means shares changed hands."},
     "source": "alpaca_paper"},
    {"id": "l2", "title": "The rule in one minute",
     "body": ["After each completed session, compare SPY's close with the average of the last 200 closes (including today).",
              "Close above the average: want to be invested. Below: want cash. Exactly equal: keep the current state.",
              "Act at the NEXT session's open. Buy at most min(25% of equity, USD 250, cash after a USD 10 reserve).",
              "This rule is EXPERIMENTAL and UNVALIDATED. It is a research starting point, not advice."],
     "check": {"q": "The close is 500.00 and the 200-session average is 500.00. You are in cash. What happens?",
               "options": ["Buy", "Stay in cash", "Sell"], "answer": 1, "why": "Equality keeps the previous state, which is cash."},
     "source": None},
    {"id": "l3", "title": "Buying, filling, holding, selling",
     "body": ["Decision (evening, Dubai time) -> order intent saved -> market opens -> DAY market order submitted -> broker reports fills.",
              "If only part of the order fills, the strategy keeps just the filled shares and does not top up automatically.",
              "While invested, the strategy holds without buying more. When the close drops below the average, it sells only its own shares."],
     "check": {"q": "Your 250 USD buy filled only 100 USD before being cancelled. What does the strategy do next day if still above average?",
               "options": ["Buy another 150 USD", "Hold the 100 USD position"], "answer": 1,
               "why": "Partial entries are adopted as-is; no automatic top-up."},
     "source": "alpaca_orders"},
    {"id": "l4", "title": "Why following the rule can still lose money",
     "body": ["Trend rules are late by design: they buy after rises and sell after falls. In choppy markets they can buy high and sell low repeatedly.",
              "Prices can gap overnight: the next open may be far from the close that triggered the decision.",
              "A losing trade made exactly according to the rule is not a software bug. Orbit labels software failures, rule losses and simulator limits separately."],
     "check": {"q": "The bot bought, then SPY fell 3% and it sold at a loss. Is that a bug?",
               "options": ["Yes", "Not by itself"], "answer": 1, "why": "A rule-following loss is expected sometimes. Check the decision inputs to confirm the rule was followed."},
     "source": None},
    {"id": "l5", "title": "Probability, sample size and causation",
     "body": ["One good year proves very little: a slow trend rule may make only a few round trips per year.",
              "Probability is not certainty. A 70% forecast is wrong 3 times in 10 even when it is well calibrated.",
              "Correlation is not causation: a pattern in past data may be chance. That is why Orbit keeps a final holdout period and records every configuration tried."],
     "check": {"q": "A rule made money in 3 out of 3 trades. Is it proven?", "options": ["Yes", "No, the sample is tiny"], "answer": 1,
               "why": "Three trades are far too few to separate skill from luck."},
     "source": None},
]


def _m(v) -> str:
    if v is None:
        return "N/A"
    p = money_pair(Decimal(str(v)))
    return f"{p['usd']} USD (≈ {p['aed']} AED)"


def explain_decision(d: dict, intents: list[dict], fills: list[dict]) -> list[str]:
    """Deterministic plain-English explanation from stored fields only."""
    out = []
    data = f"Data: {d['data_mode']} from {d['data_source']}."
    if d["data_mode"] == "SAMPLE":
        data += " SAMPLE prices are synthetic and never real."
    out.append(data)
    if d.get("close") is None or d.get("sma") is None:
        out.append(f"No signal was computed: {d['reason']}")
    else:
        rel = {"ABOVE": "above", "BELOW": "below", "EQUAL": "exactly equal to"}.get(d["comparison"], "compared with")
        out.append(f"On {d['session']} SPY closed at {_m(d['close'])}, {rel} its 200-session average of {_m(d['sma'])} "
                   f"({d['window_count']} closes, including that day).")
        out.append(f"So the rule's target became {d['target']} (it was {d['previous_target']}).")
    out.append(f"The strategy held {d['held_qty_at_decision']} shares from confirmed fills when deciding. Mode: {d['mode_at_decision']}.")
    out.append(f"Action recorded: {d['action']}. {d['reason']}")
    if not intents:
        if d["action"] in ("HOLD", "SKIP"):
            out.append("No order intent was created, so no order followed.")
    for i in intents:
        line = f"Order intent #{i['id']} ({i['kind']} {i['side']}) for the {i['execution_session']} open is {i['state']}."
        if i.get("frozen_notional"):
            line += f" Frozen target {_m(i['frozen_notional'])}."
        if i.get("submitted_notional"):
            line += f" Submitted {_m(i['submitted_notional'])}."
        if i.get("decision_id") != d["id"]:
            line += " (Follow-up intent linked by lineage.)"
        if d["action"] not in ("BUY", "SELL") and i["kind"] in ("ENTRY", "EXIT"):
            line += " It was created after this decision (e.g. when paper automation was enabled or after a review), using this stored signal."
        line += f" Latest note: {i['reason']}"
        out.append(line)
    for f in fills:
        out.append(f"Fill: {f['side']} {f['qty']} at {_m(f['price'])} on {f['at']}.")
    out.append("This rule is experimental and unvalidated; a rule-following result is not evidence the rule works.")
    return out
