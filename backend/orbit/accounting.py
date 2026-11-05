"""FIFO lot ledger used by the backtester and for the hand-checked accounting fixture.

Convention (documented and tested):
- FIFO lot relief.
- Purchase fees are capitalised into the lot cost basis.
- Sale fees reduce sale proceeds (and therefore realised P&L).
- Cash may never go negative (no borrowing); quantity may never go negative (no shorting).
- Splits multiply lot quantities and divide per-unit cost; total basis unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from orbit.money import D


class AccountingError(ValueError):
    pass


@dataclass
class Lot:
    qty: Decimal
    cost: Decimal  # total cost including capitalised fee
    opened: date | None = None


@dataclass
class Ledger:
    cash: Decimal
    lots: list[Lot] = field(default_factory=list)
    realized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    traded_notional: Decimal = Decimal(0)

    def __post_init__(self):
        self.cash = D(self.cash)
        if self.cash < 0:
            raise AccountingError("starting cash cannot be negative")

    @property
    def qty(self) -> Decimal:
        return sum((l.qty for l in self.lots), Decimal(0))

    @property
    def cost_basis(self) -> Decimal:
        return sum((l.cost for l in self.lots), Decimal(0))

    def buy(self, qty: Decimal, price: Decimal, fee: Decimal = Decimal(0), day: date | None = None) -> None:
        qty, price, fee = D(qty), D(price), D(fee)
        if qty <= 0 or price <= 0 or fee < 0:
            raise AccountingError("buy needs positive qty/price and non-negative fee")
        total = qty * price + fee
        if total > self.cash:
            raise AccountingError(f"insufficient cash: need {total}, have {self.cash} (no borrowing)")
        self.cash -= total
        self.lots.append(Lot(qty, qty * price + fee, day))
        self.fees_paid += fee
        self.traded_notional += qty * price

    def sell(self, qty: Decimal, price: Decimal, fee: Decimal = Decimal(0)) -> Decimal:
        qty, price, fee = D(qty), D(price), D(fee)
        if qty <= 0 or price <= 0 or fee < 0:
            raise AccountingError("sell needs positive qty/price and non-negative fee")
        if qty > self.qty:
            raise AccountingError(f"oversell: selling {qty} but only {self.qty} held (no shorting)")
        proceeds = qty * price - fee
        if self.cash + proceeds < 0:
            raise AccountingError("sale fee would make cash negative")
        remaining = qty
        relieved = Decimal(0)
        while remaining > 0:
            lot = self.lots[0]
            if lot.qty <= remaining:
                relieved += lot.cost
                remaining -= lot.qty
                self.lots.pop(0)
            else:
                part = lot.cost * remaining / lot.qty
                relieved += part
                lot.cost -= part
                lot.qty -= remaining
                remaining = Decimal(0)
        realized = proceeds - relieved
        self.realized_pnl += realized
        self.cash += proceeds
        self.fees_paid += fee
        self.traded_notional += qty * price
        return realized

    def apply_split(self, ratio: Decimal) -> None:
        r = D(ratio)
        if r <= 0:
            raise AccountingError("split ratio must be positive")
        for lot in self.lots:
            lot.qty *= r

    def market_value(self, price: Decimal) -> Decimal:
        return self.qty * D(price)

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        return self.market_value(price) - self.cost_basis

    def equity(self, price: Decimal) -> Decimal:
        return self.cash + self.market_value(price)
