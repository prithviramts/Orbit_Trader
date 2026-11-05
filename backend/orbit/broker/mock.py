"""Deterministic in-memory broker for tests and offline demos. Never contacts any network."""
from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta
from decimal import Decimal

from orbit.broker.base import (
    AccountInfo, AssetInfo, BrokerAdapter, BrokerAmbiguousError, BrokerNotFoundError, BrokerRejectedError,
    BrokerUnavailableError, CashActivity, ClockInfo, OrderInfo, OrderRequest, PositionInfo, fingerprint_account,
)
from orbit.market_calendar import UTC
from orbit.money import D


class MockBroker(BrokerAdapter):
    name = "mock"
    environment = "paper-mock"

    def __init__(self, cash: Decimal = Decimal("1000"), account_id: str = "mock-account-1"):
        self.account_id = account_id
        self.cash = D(cash)
        self.positions: dict[str, Decimal] = {}
        self.avg_price: dict[str, Decimal] = {}
        self.orders: dict[str, OrderInfo] = {}
        self.by_client: dict[str, str] = {}
        self._ids = itertools.count(1)
        self.now = datetime(2026, 1, 5, 14, 31, tzinfo=UTC)
        self.market_open = True
        self.submit_behaviour: list[str] = []  # accept | timeout_after_accept | timeout_before_accept | reject:<r> | unavailable
        self.lookup_behaviour: list[str] = []  # ok | timeout | not_found
        self.auto_fill_price: Decimal | None = None
        self.calls: list[str] = []
        self.cash_activities: list[CashActivity] = []
        self.status = "ACTIVE"
        self.fail_all_reads = False
        self.asset = AssetInfo("SPY", tradable=True, fractionable=True, status="active")

    # ---- helpers for tests
    def _order_id(self) -> str:
        return f"mock-order-{next(self._ids)}"

    def fill(self, order_id: str, qty: Decimal, price: Decimal) -> None:
        o = self.orders[order_id]
        qty, price = D(qty), D(price)
        prev = o.filled_qty
        new = prev + qty
        if o.qty is not None and new > o.qty:
            raise ValueError("overfill in mock")
        o.filled_avg_price = ((o.filled_avg_price or Decimal(0)) * prev + price * qty) / new
        o.filled_qty = new
        if o.side == "buy":
            self.cash -= qty * price
            held = self.positions.get(o.symbol, Decimal(0))
            self.avg_price[o.symbol] = ((self.avg_price.get(o.symbol, Decimal(0)) * held) + price * qty) / (held + qty)
            self.positions[o.symbol] = held + qty
        else:
            self.cash += qty * price
            self.positions[o.symbol] = self.positions.get(o.symbol, Decimal(0)) - qty
            if self.positions[o.symbol] == 0:
                del self.positions[o.symbol]
        full = (o.qty is not None and new == o.qty) or (o.notional is not None and new * o.filled_avg_price >= o.notional - Decimal("0.01"))
        o.status = "filled" if full else "partially_filled"
        o.updated_at = self.now
        if full:
            o.filled_at = self.now

    def set_status(self, order_id: str, status: str) -> None:
        self.orders[order_id].status = status
        self.orders[order_id].updated_at = self.now

    def add_manual_position(self, symbol: str, qty: Decimal, price: Decimal = Decimal("100")) -> None:
        self.positions[symbol] = self.positions.get(symbol, Decimal(0)) + D(qty)
        self.avg_price[symbol] = D(price)

    def add_foreign_order(self, symbol: str = "SPY") -> str:
        oid = self._order_id()
        self.orders[oid] = OrderInfo(oid, "manual-from-dashboard", symbol, "buy", "limit", "gtc", "new",
                                     Decimal(1), None, Decimal(0), None, self.now, self.now)
        return oid

    # ---- reads
    def _read(self, name: str):
        self.calls.append(name)
        if self.fail_all_reads:
            raise BrokerUnavailableError("mock broker unreachable")

    def get_account(self) -> AccountInfo:
        self._read("get_account")
        mv = sum((q * self.avg_price.get(s, Decimal(0)) for s, q in self.positions.items()), Decimal(0))
        return AccountInfo(
            account_fingerprint=fingerprint_account(self.account_id), account_number_masked="MOCK****",
            status=self.status, currency="USD", cash=self.cash, equity=self.cash + mv, last_equity=None,
            buying_power=self.cash * 2, non_marginable_buying_power=self.cash, multiplier="2",
            trading_blocked=False, account_blocked=False, transfers_blocked=False, shorting_enabled=False,
            pattern_day_trader=False,
        )

    def get_positions(self) -> list[PositionInfo]:
        self._read("get_positions")
        return [PositionInfo(s, q, self.avg_price.get(s, Decimal(0)), q * self.avg_price.get(s, Decimal(0)))
                for s, q in self.positions.items()]

    def get_open_orders(self) -> list[OrderInfo]:
        self._read("get_open_orders")
        return [o for o in self.orders.values() if not o.is_terminal]

    def get_recent_orders(self, after: datetime | None = None) -> list[OrderInfo]:
        self._read("get_recent_orders")
        return list(self.orders.values())

    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None:
        self._read("get_order_by_client_id")
        b = self.lookup_behaviour.pop(0) if self.lookup_behaviour else "ok"
        if b == "timeout":
            raise BrokerAmbiguousError("lookup timeout")
        if b == "not_found":
            return None
        oid = self.by_client.get(client_order_id)
        return self.orders.get(oid) if oid else None

    def get_clock(self) -> ClockInfo:
        self._read("get_clock")
        return ClockInfo(self.now, self.market_open, self.now + timedelta(days=1), self.now + timedelta(hours=6))

    def get_asset(self, symbol: str) -> AssetInfo:
        self._read("get_asset")
        return self.asset

    def get_cash_activities(self, after: date | None = None) -> list[CashActivity]:
        self._read("get_cash_activities")
        return [a for a in self.cash_activities if after is None or a.date >= after]

    # ---- writes
    def submit_order(self, req: OrderRequest) -> OrderInfo:
        self.calls.append(f"submit:{req.client_order_id}")
        b = self.submit_behaviour.pop(0) if self.submit_behaviour else "accept"
        if b == "unavailable":
            raise BrokerUnavailableError("connection refused before send")
        if b == "timeout_before_accept":
            raise BrokerAmbiguousError("read timeout")
        if b.startswith("reject"):
            raise BrokerRejectedError(b.split(":", 1)[1] if ":" in b else "rejected", 403)
        if req.client_order_id in self.by_client:
            raise BrokerRejectedError("client_order_id must be unique", 422)
        oid = self._order_id()
        o = OrderInfo(oid, req.client_order_id, req.symbol, req.side, req.type, req.time_in_force, "accepted",
                      req.qty, req.notional, Decimal(0), None, self.now, self.now)
        self.orders[oid] = o
        self.by_client[req.client_order_id] = oid
        if self.auto_fill_price is not None:
            qty = req.qty if req.qty is not None else (req.notional / self.auto_fill_price).quantize(Decimal("0.000000001"), rounding="ROUND_DOWN")
            self.fill(oid, qty, self.auto_fill_price)
        if b == "timeout_after_accept":
            raise BrokerAmbiguousError("read timeout after server accepted")
        return o

    def cancel_order(self, order_id: str) -> None:
        self.calls.append(f"cancel:{order_id}")
        o = self.orders.get(order_id)
        if not o:
            raise BrokerNotFoundError(order_id)
        if o.is_terminal:
            raise BrokerRejectedError("order already terminal", 422)
        o.status = "pending_cancel"
