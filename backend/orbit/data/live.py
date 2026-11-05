"""Read-only live market data for the dashboard's candlestick chart.

Uses only Alpaca's market-data client (never a trading client), so the dashboard cannot place orders
through this path. Display only: trading decisions keep using completed daily bars in the worker.
Responses are cached briefly so many open tabs cannot exhaust the account's API rate limit.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

# Imported once at module load: lazy imports from two request threads at the same moment can deadlock
# Python's import lock on first use (seen when the news and chart requests arrive together).
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from orbit.broker.alpaca_adapter import _with_timeout
from orbit.config import Settings, redact
from orbit.market_calendar import UTC, now_utc

# timeframe key -> (amount, unit, lookback in calendar days)
TIMEFRAMES: dict[str, tuple[int, str, int]] = {
    "1Min": (1, "Minute", 3),
    "5Min": (5, "Minute", 7),
    "15Min": (15, "Minute", 20),
    "1Hour": (1, "Hour", 60),
    "1Day": (1, "Day", 730),
}
CACHE_SECONDS = 10.0
NEWS_CACHE_SECONDS = 120.0


class LiveDataError(RuntimeError):
    pass


@dataclass
class _Entry:
    at: float
    value: dict


class LiveMarketData:
    def __init__(self, settings: Settings, client=None, news_client=None):
        self.settings = settings
        self._client = client
        self._news_client = news_client
        self._cache: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _data_client(self):
        if self._client is None:
            if not self.settings.broker_configured:
                raise LiveDataError("Alpaca paper credentials are not configured (.env), so live data is unavailable.")
            self._client = StockHistoricalDataClient(self.settings.alpaca_api_key_id.get_secret_value(),
                                                     self.settings.alpaca_api_secret_key.get_secret_value())
            _with_timeout(self._client._session)
        return self._client

    def _feed(self):
        return {"iex": DataFeed.IEX, "sip": DataFeed.SIP, "delayed_sip": DataFeed.DELAYED_SIP}[self.settings.alpaca_data_feed]

    def candles(self, symbol: str, tf: str, now: datetime | None = None) -> dict:
        if tf not in TIMEFRAMES:
            raise ValueError("unknown timeframe")
        key = f"{symbol}:{tf}"
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit.at < CACHE_SECONDS:
                return hit.value
        value = self._fetch(symbol, tf, now or now_utc())
        with self._lock:
            self._cache[key] = _Entry(time.monotonic(), value)
        return value

    def _fetch(self, symbol: str, tf: str, now: datetime) -> dict:
        amount, unit, days = TIMEFRAMES[tf]
        client = self._data_client()
        try:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame(amount, getattr(TimeFrameUnit, unit)),
                                   start=now - timedelta(days=days), end=now, adjustment=Adjustment.SPLIT, feed=self._feed(),
                                   limit=10000)
            resp = client.get_stock_bars(req)
            raw = resp.data.get(symbol, []) if hasattr(resp, "data") else []
            latest = None
            try:
                lt = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self._feed()))
                t = lt.get(symbol) if isinstance(lt, dict) else None
                if t is not None:
                    latest = {"price": float(t.price), "at": t.timestamp.astimezone(UTC).isoformat()}
            except Exception:  # noqa: BLE001 - the latest trade is optional
                latest = None
        except Exception as e:  # noqa: BLE001
            raise LiveDataError(f"Live data request failed: {type(e).__name__}: {redact(e)[:200]}") from None
        candles = [{"t": int(b.timestamp.timestamp()), "o": float(b.open), "h": float(b.high), "l": float(b.low),
                    "c": float(b.close), "v": float(b.volume)} for b in raw]
        return {"symbol": symbol, "timeframe": tf, "feed": self.settings.alpaca_data_feed, "fetched_at": now.astimezone(UTC).isoformat(),
                "candles": candles, "latest_trade": latest,
                "note": "Display only. IEX is one exchange's trades, so prices and volume can differ slightly from the consolidated tape. "
                        "Trading decisions use completed daily closes, not this feed."}

    # ------------------------------------------------------------ news
    def _news(self):
        if self._news_client is None:
            if not self.settings.broker_configured:
                raise LiveDataError("Alpaca paper credentials are not configured (.env), so the news feed is unavailable.")
            self._news_client = NewsClient(self.settings.alpaca_api_key_id.get_secret_value(),
                                           self.settings.alpaca_api_secret_key.get_secret_value())
            _with_timeout(self._news_client._session)
        return self._news_client

    def news(self, symbols: str = "SPY", limit: int = 30) -> dict:
        key = f"news:{symbols}:{limit}"
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit.at < NEWS_CACHE_SECONDS:
                return hit.value
        try:
            resp = self._news().get_news(NewsRequest(symbols=symbols, limit=limit, sort="desc", exclude_contentless=False))
            raw = resp.data.get("news", []) if hasattr(resp, "data") else []
        except LiveDataError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LiveDataError(f"News request failed: {type(e).__name__}: {redact(e)[:200]}") from None
        items = []
        for n in raw:
            url = str(n.url or "")
            items.append({
                "id": n.id, "headline": _clip(n.headline, 300), "summary": _clip(n.summary, 600),
                "source": _clip(n.source, 40), "author": _clip(n.author, 80),
                "url": url if url.startswith("https://") else "",  # only plain https links are shown
                "created_at": n.created_at.astimezone(UTC).isoformat() if n.created_at else None,
                "symbols": [str(x) for x in (n.symbols or [])][:12],
            })
        value = {"items": items, "fetched_at": now_utc().isoformat(),
                 "note": "Headlines from Alpaca's news feed (Benzinga). Display only: news never triggers orders. "
                         "By the time a headline reaches you, the market has usually already reacted."}
        with self._lock:
            self._cache[key] = _Entry(time.monotonic(), value)
        return value


def _clip(v, n: int) -> str:
    s = " ".join(str(v or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"
