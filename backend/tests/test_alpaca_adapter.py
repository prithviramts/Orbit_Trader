"""Offline construction tests for the real Alpaca adapter (no network calls are made)."""
import pytest
from pydantic import SecretStr

from orbit.broker.alpaca_adapter import TIMEOUT, AlpacaPaperAdapter, safe_float_qty
from orbit.config import LiveTradingRejected, Settings


def _settings(url="https://paper-api.alpaca.markets/v2"):
    return Settings(_env_file=None, APCA_API_KEY_ID="PKOFFLINETESTKEY0000", APCA_API_SECRET_KEY="offline-test-secret-value",
                    APCA_API_BASE_URL=url)


def test_adapter_constructs_with_paper_endpoint_and_safe_clients():
    ad = AlpacaPaperAdapter(_settings())
    assert ad.read_client._base_url.value == "https://paper-api.alpaca.markets"
    assert ad.order_client._base_url.value == "https://paper-api.alpaca.markets"
    assert ad.order_client._retry_codes == []  # no silent re-POST of orders
    assert ad.read_client._session.request.__wrapped__  # timeout wrapper installed


def test_adapter_refuses_live_endpoint():
    with pytest.raises(LiveTradingRejected):
        AlpacaPaperAdapter(_settings("https://api.alpaca.markets"))


def test_float_qty_never_exceeds_decimal():
    from decimal import Decimal
    for q in ["0.123456789", "0.625", "1.000000001", "0.1"]:
        assert Decimal(repr(safe_float_qty(Decimal(q)))) <= Decimal(q)
