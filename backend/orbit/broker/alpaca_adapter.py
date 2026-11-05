"""Alpaca PAPER adapter (alpaca-py). There is deliberately no live code path.

- The base URL is validated with ``assert_paper_url`` and the SDK is constructed with paper=True.
- Every HTTP call gets a connect/read timeout (alpaca-py sets none by default).
- Order submission uses a client with 429-retry disabled so the SDK never silently re-POSTs.
- A read timeout or dropped connection during submit is AMBIGUOUS (the order may exist).
"""
from __future__ import annotations

import functools
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import requests

from orbit.broker.base import (
    AccountInfo, AssetInfo, BrokerAdapter, BrokerAmbiguousError, BrokerError, BrokerNotFoundError,
    BrokerRejectedError, BrokerUnavailableError, CashActivity, ClockInfo, OrderInfo, OrderRequest, PositionInfo,
    fingerprint_account,
)
from orbit.config import Settings, assert_paper_url, redact
from orbit.data.bars import Bar, BarSeries
from orbit.market_calendar import NEW_YORK, UTC, MarketCalendar, ensure_utc, now_utc
from orbit.money import D

log = logging.getLogger("orbit.broker")
TIMEOUT = (5, 20)
CASH_ACTIVITY_TYPES = "CSD,CSW,JNLC,JNLS,TRANS,DIV,INT,FEE,REORG,SPLIT"


def _with_timeout(session: requests.Session) -> None:
    orig = session.request

    @functools.wraps(orig)
    def request(method, url, **kw):
        kw.setdefault("timeout", TIMEOUT)
        return orig(method, url, **kw)

    session.request = request  # type: ignore[method-assign]


def safe_float_qty(qty: Decimal) -> float:
    """The SDK takes floats; make sure the float never exceeds the Decimal (no accidental oversell)."""
    import math

    f = float(qty)
    while Decimal(repr(f)) > qty:
        f = math.nextafter(f, 0.0)
    return f


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    return D(str(v))


def _enum(v) -> str:
    return str(getattr(v, "value", v) or "").lower()


def _order(o) -> OrderInfo:
    return OrderInfo(
        id=str(o.id), client_order_id=o.client_order_id, symbol=o.symbol, side=_enum(o.side),
        type=_enum(getattr(o, "order_type", None) or getattr(o, "type", None)), time_in_force=_enum(o.time_in_force),
        status=_enum(o.status), qty=_dec(o.qty), notional=_dec(o.notional), filled_qty=_dec(o.filled_qty) or Decimal(0),
        filled_avg_price=_dec(o.filled_avg_price),
        submitted_at=ensure_utc(o.submitted_at) if o.submitted_at else None,
        updated_at=ensure_utc(o.updated_at) if o.updated_at else None,
        filled_at=ensure_utc(o.filled_at) if o.filled_at else None,
        extended_hours=bool(o.extended_hours),
    )


class AlpacaPaperAdapter(BrokerAdapter):
    name = "alpaca"
    environment = "paper"

    def __init__(self, settings: Settings):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        assert_paper_url(settings.alpaca_base_url)
        if not settings.broker_configured:
            raise BrokerError("Alpaca paper credentials are not configured (.env)")
        key = settings.alpaca_api_key_id.get_secret_value()
        secret = settings.alpaca_api_secret_key.get_secret_value()
        self.settings = settings
        self.read_client = TradingClient(key, secret, paper=True)
        self.order_client = TradingClient(key, secret, paper=True, retry_exception_codes=[599])
        self.data_client = StockHistoricalDataClient(key, secret)
        for c in (self.read_client, self.order_client, self.data_client):
            _with_timeout(c._session)
            if "paper-api.alpaca.markets" not in str(getattr(c, "_base_url", "paper-api.alpaca.markets")) and c is not self.data_client:
                raise BrokerError("SDK base URL is not the paper endpoint; refusing")

    # ------------------------------------------------------------ error mapping
    def _call(self, fn, *a, write: bool = False, **kw):
        from alpaca.common.exceptions import APIError

        try:
            return fn(*a, **kw)
        except APIError as e:
            code = e.status_code
            msg = redact(str(e))[:300]
            if code == 404:
                raise BrokerNotFoundError(msg) from None
            if code is not None and 400 <= code < 500 and code != 429:
                raise BrokerRejectedError(msg, code) from None
            if write:
                raise BrokerAmbiguousError(f"HTTP {code}: {msg}") from None
            raise BrokerUnavailableError(f"HTTP {code}: {msg}") from None
        except requests.exceptions.ConnectTimeout:
            raise BrokerUnavailableError("connect timeout (request not sent)") from None
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            if write:
                raise BrokerAmbiguousError(f"network failure after send: {type(e).__name__}") from None
            raise BrokerUnavailableError(f"network failure: {type(e).__name__}") from None

    # ------------------------------------------------------------ reads
    def get_account(self) -> AccountInfo:
        a = self._call(self.read_client.get_account)
        num = str(a.account_number or "")
        return AccountInfo(
            account_fingerprint=fingerprint_account(str(a.id)),
            account_number_masked=("****" + num[-4:]) if len(num) >= 4 else "****",
            status=_enum(a.status).upper(), currency=str(a.currency or "USD"),
            cash=_dec(a.cash) or Decimal(0), equity=_dec(a.equity) or Decimal(0), last_equity=_dec(a.last_equity),
            buying_power=_dec(a.buying_power) or Decimal(0), non_marginable_buying_power=_dec(a.non_marginable_buying_power),
            multiplier=str(a.multiplier), trading_blocked=bool(a.trading_blocked), account_blocked=bool(a.account_blocked),
            transfers_blocked=bool(a.transfers_blocked), shorting_enabled=bool(a.shorting_enabled),
            pattern_day_trader=bool(a.pattern_day_trader),
            created_at=a.created_at.isoformat() if getattr(a, "created_at", None) else None,
        )

    def get_positions(self) -> list[PositionInfo]:
        ps = self._call(self.read_client.get_all_positions)
        return [PositionInfo(p.symbol, D(str(p.qty)), D(str(p.avg_entry_price)), _dec(p.market_value), _enum(p.side) or "long") for p in ps]

    def get_open_orders(self) -> list[OrderInfo]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        os_ = self._call(self.read_client.get_orders, GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return [_order(o) for o in os_]

    def get_recent_orders(self, after: datetime | None = None) -> list[OrderInfo]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, after=after or (now_utc() - timedelta(days=30)))
        return [_order(o) for o in self._call(self.read_client.get_orders, req)]

    def get_order_by_client_id(self, client_order_id: str) -> OrderInfo | None:
        try:
            return _order(self._call(self.read_client.get_order_by_client_id, client_order_id))
        except BrokerNotFoundError:
            return None
        except BrokerUnavailableError as e:
            raise BrokerAmbiguousError(str(e)) from None

    def get_clock(self) -> ClockInfo:
        c = self._call(self.read_client.get_clock)
        return ClockInfo(ensure_utc(c.timestamp), bool(c.is_open), ensure_utc(c.next_open), ensure_utc(c.next_close))

    def get_asset(self, symbol: str) -> AssetInfo:
        a = self._call(self.read_client.get_asset, symbol)
        return AssetInfo(a.symbol, bool(a.tradable), bool(a.fractionable), _enum(a.status), bool(a.marginable), bool(a.shortable))

    def get_cash_activities(self, after: date | None = None) -> list[CashActivity]:
        params = {"activity_types": CASH_ACTIVITY_TYPES, "page_size": 100}
        if after:
            params["after"] = after.isoformat()
        rows = self._call(self.read_client.get, "/account/activities", params) or []
        out = []
        for r in rows:
            try:
                out.append(CashActivity(str(r.get("id")), str(r.get("activity_type")),
                                        date.fromisoformat(str(r.get("date"))[:10]), D(str(r.get("net_amount") or "0"))))
            except (ValueError, TypeError):
                out.append(CashActivity(str(r.get("id")), str(r.get("activity_type")), date.today(), Decimal(0)))
        return out

    def get_calendar(self, start: date, end: date) -> MarketCalendar:
        from alpaca.trading.requests import GetCalendarRequest

        from orbit.market_calendar import Session

        rows = self._call(self.read_client.get_calendar, GetCalendarRequest(start=start, end=end))
        sessions = []
        for r in rows:
            d = r.date if isinstance(r.date, date) else date.fromisoformat(str(r.date))
            o = r.open if isinstance(r.open, datetime) else datetime.combine(d, r.open)
            c = r.close if isinstance(r.close, datetime) else datetime.combine(d, r.close)
            o = o.replace(tzinfo=NEW_YORK) if o.tzinfo is None else o
            c = c.replace(tzinfo=NEW_YORK) if c.tzinfo is None else c
            sessions.append(Session(d, o.astimezone(UTC), c.astimezone(UTC)))
        return MarketCalendar(sessions, source="Alpaca /v2/calendar")

    # ------------------------------------------------------------ writes
    def submit_order(self, req: OrderRequest) -> OrderInfo:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if req.type != "market" or req.time_in_force != "day" or req.extended_hours:
            raise BrokerRejectedError("adapter only supports DAY market orders in v1")
        kw = dict(symbol=req.symbol, side=OrderSide.BUY if req.side == "buy" else OrderSide.SELL,
                  time_in_force=TimeInForce.DAY, client_order_id=req.client_order_id, extended_hours=False)
        if req.notional is not None:
            kw["notional"] = float(req.notional.quantize(Decimal("0.01"), rounding="ROUND_DOWN"))
        else:
            kw["qty"] = safe_float_qty(req.qty)
        return _order(self._call(self.order_client.submit_order, MarketOrderRequest(**kw), write=True))

    def cancel_order(self, order_id: str) -> None:
        self._call(self.order_client.cancel_order_by_id, order_id, write=True)

    # ------------------------------------------------------------ market data
    def fetch_daily_bars(self, symbol: str, start: date, end: date, completed_before: datetime,
                         calendar: MarketCalendar, buffer_minutes: int = 15) -> BarSeries:
        """Fetch split-adjusted daily bars; drop any bar whose session is not complete (+buffer)."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        feed = {"iex": DataFeed.IEX, "sip": DataFeed.SIP, "delayed_sip": DataFeed.DELAYED_SIP}[self.settings.alpaca_data_feed]
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                               start=datetime.combine(start, datetime.min.time(), tzinfo=UTC),
                               end=min(completed_before, datetime.combine(end, datetime.max.time(), tzinfo=UTC)),
                               adjustment=Adjustment.SPLIT, feed=feed)
        resp = self._call(self.data_client.get_stock_bars, req)
        raw = resp.data.get(symbol, []) if hasattr(resp, "data") else []
        bars: list[Bar] = []
        dropped = []
        for b in raw:
            d = ensure_utc(b.timestamp).astimezone(NEW_YORK).date()
            s = calendar.session(d)
            if s is None or s.close_utc + timedelta(minutes=buffer_minutes) > completed_before:
                dropped.append(d.isoformat())
                continue
            bars.append(Bar(d, D(str(b.open)), D(str(b.high)), D(str(b.low)), D(str(b.close)), D(str(b.volume))))
        return BarSeries(
            symbol=symbol, mode="ALPACA",
            source=f"Alpaca market data, feed={self.settings.alpaca_data_feed.upper()}, adjustment=split",
            price_basis="split_adjusted", bars=bars, dividends_included=False,
            metadata={"feed": self.settings.alpaca_data_feed, "fetched_at": now_utc().isoformat(),
                      "dropped_incomplete_or_non_session": dropped,
                      "note": "IEX-only daily bars reflect IEX trades, not the consolidated official close."},
        )
