"""Broker-neutral types and the adapter contract.

Execution limits are enforced in ``ExecutionGuard`` at the boundary, independent of
the UI, the strategy or any future model: SPY only, paper only, DAY market orders
only, regular hours only, no shorting, no borrowing, bounded buy notional.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from orbit.money import D

TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}
OPEN_STATUSES = {
    "new", "partially_filled", "accepted", "pending_new", "accepted_for_bidding", "pending_cancel",
    "pending_replace", "stopped", "calculated", "suspended", "done_for_day", "held",
}


class BrokerError(Exception):
    pass


class BrokerAmbiguousError(BrokerError):
    """Network timeout / connection reset after sending: the broker MAY have accepted the order."""


class BrokerUnavailableError(BrokerError):
    """Request definitely not processed (e.g. connection refused before send, DNS failure, 5xx before accept)."""


class BrokerRejectedError(BrokerError):
    def __init__(self, reason: str, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class BrokerNotFoundError(BrokerError):
    pass


class GuardViolation(BrokerError):
    pass


@dataclass
class AccountInfo:
    account_fingerprint: str  # sha256 of the account id; the raw id is never stored or shown
    account_number_masked: str
    status: str
    currency: str
    cash: Decimal
    equity: Decimal
    last_equity: Decimal | None
    buying_power: Decimal
    non_marginable_buying_power: Decimal | None
    multiplier: str
    trading_blocked: bool
    account_blocked: bool
    transfers_blocked: bool
    shorting_enabled: bool
    pattern_day_trader: bool
    created_at: str | None = None

    def to_public(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = str(v)
        return d


@dataclass
class PositionInfo:
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal | None
    side: str = "long"

    def to_public(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


@dataclass
class OrderInfo:
    id: str
    client_order_id: str
    symbol: str
    side: str  # buy | sell
    type: str
    time_in_force: str
    status: str
    qty: Decimal | None
    notional: Decimal | None
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    submitted_at: datetime | None
    updated_at: datetime | None
    filled_at: datetime | None = None
    extended_hours: bool = False
    raw_reason: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_public(self) -> dict:
        d = {}
        for k, v in asdict(self).items():
            if isinstance(v, Decimal):
                d[k] = str(v)
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
            else:
                d[k] = v
        return d


@dataclass
class ClockInfo:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass
class AssetInfo:
    symbol: str
    tradable: bool
    fractionable: bool
    status: str
    marginable: bool = False
    shortable: bool = False


@dataclass
class CashActivity:
    id: str
    activity_type: str
    date: date
    net_amount: Decimal


@dataclass
class OrderRequest:
    symbol: str
    side: str  # buy | sell
    client_order_id: str
    notional: Decimal | None = None
    qty: Decimal | None = None
    type: str = "market"
    time_in_force: str = "day"
    extended_hours: bool = False
    meta: dict = field(default_factory=dict)


class BrokerAdapter:
    """Contract implemented by AlpacaPaperAdapter and MockBroker."""

    name = "abstract"
    environment = "paper"

    def get_account(self) -> AccountInfo: raise NotImplementedError
    def get_positions(self) -> list[PositionInfo]: raise NotImplementedError
    def get_open_orders(self) -> list[OrderInfo]: raise NotImplementedError
    def get_recent_orders(self, after: datetime | None = None) -> list[OrderInfo]: raise NotImplementedError
    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None: raise NotImplementedError
    def submit_order(self, req: OrderRequest) -> OrderInfo: raise NotImplementedError
    def cancel_order(self, order_id: str) -> None: raise NotImplementedError
    def get_clock(self) -> ClockInfo: raise NotImplementedError
    def get_asset(self, symbol: str) -> AssetInfo: raise NotImplementedError
    def get_cash_activities(self, after: date | None = None) -> list[CashActivity]: raise NotImplementedError


def fingerprint_account(account_id: str) -> str:
    return hashlib.sha256(f"orbit-account|{account_id}".encode()).hexdigest()[:24]


def make_client_order_id(account_key: str, strategy_version_id: int, symbol: str, signal_session: date,
                         kind: str, attempt: int) -> str:
    """Stable, deterministic id: same intent identity -> same id, forever.

    Short (<= 40 chars) to stay well within broker limits (Alpaca's documented maximum
    was not confirmed in the docs reviewed; 128 is commonly cited).
    """
    h = hashlib.sha256(
        f"{account_key}|v{strategy_version_id}|{symbol}|{signal_session.isoformat()}|{kind}|{attempt}".encode()
    ).hexdigest()[:12]
    k = {"ENTRY": "EN", "EXIT": "EX", "RESIDUAL_EXIT": "RX", "CLOSE_MANUAL": "CM"}[kind]
    return f"orb-{symbol}-{signal_session.strftime('%Y%m%d')}-{k}{attempt}-{h}"


class ExecutionGuard:
    """Independent limits enforced right before any order leaves the process."""

    ALLOWED_SYMBOLS = {"SPY", "GLD"}  # SPY rule + gold AI experiment (paper only)

    def __init__(self, max_buy_notional: Decimal):
        self.max_buy_notional = D(max_buy_notional)

    def check(self, req: OrderRequest, account: AccountInfo, held_qty_at_broker: Decimal,
              open_sell_qty: Decimal = Decimal(0), pending_buy_notional: Decimal = Decimal(0)) -> None:
        if req.symbol not in self.ALLOWED_SYMBOLS:
            raise GuardViolation(f"symbol {req.symbol} not allowlisted")
        if req.type != "market" or req.time_in_force != "day":
            raise GuardViolation("only DAY market orders are allowed in v1")
        if req.extended_hours:
            raise GuardViolation("extended-hours orders are not allowed")
        if req.side not in ("buy", "sell"):
            raise GuardViolation("invalid side")
        if (req.notional is None) == (req.qty is None):
            raise GuardViolation("exactly one of notional or qty is required")
        if account.trading_blocked or account.account_blocked:
            raise GuardViolation("broker reports trading/account blocked")
        if account.status.upper() != "ACTIVE":
            raise GuardViolation(f"account status {account.status} is not ACTIVE")
        if req.side == "buy":
            if req.notional is None or req.notional <= 0:
                raise GuardViolation("buys must be positive notional orders")
            if req.notional > self.max_buy_notional:
                raise GuardViolation(f"buy notional {req.notional} exceeds hard cap {self.max_buy_notional}")
            # Unborrowed cash only: never rely on margin buying power.
            if req.notional + pending_buy_notional > account.cash:
                raise GuardViolation("buy exceeds unborrowed cash (margin is never used)")
        else:
            if req.qty is None or req.qty <= 0:
                raise GuardViolation("sells must be positive quantity orders")
            if req.qty + open_sell_qty > held_qty_at_broker:
                raise GuardViolation("sell would exceed the broker-confirmed holding (no shorting/overselling)")
