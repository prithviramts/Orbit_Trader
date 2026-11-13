"""External heartbeat: rate-limited, https-only, never breaks the trading loop, URL never logged."""
from datetime import datetime, timedelta, timezone

import httpx

from orbit.config import Settings, redact
from orbit.heartbeat import Heartbeat

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
URL = "https://hc-ping.example/abc-secret-uuid-123456"


def _hb(handler, url=URL):
    st = Settings(_env_file=None, ORBIT_HEARTBEAT_URL=url, ORBIT_HEARTBEAT_INTERVAL_SECONDS=300)
    return Heartbeat(st, httpx.Client(transport=httpx.MockTransport(handler)))


def test_pings_and_rate_limits():
    calls = []
    hb = _hb(lambda r: calls.append(str(r.url)) or httpx.Response(200))
    assert hb.maybe_ping(T0)
    assert not hb.maybe_ping(T0 + timedelta(seconds=60))  # within interval
    assert hb.maybe_ping(T0 + timedelta(seconds=301))
    assert len(calls) == 2 and hb.status()["last_ok"]


def test_failures_never_raise():
    def boom(r):
        raise httpx.ConnectError("down")
    hb = _hb(boom)
    assert hb.maybe_ping(T0) is False and hb.last_error == "ConnectError"
    hb2 = _hb(lambda r: httpx.Response(500))
    assert hb2.maybe_ping(T0) is False and hb2.last_error == "HTTP 500"


def test_http_url_refused_and_disabled_without_config():
    assert not _hb(lambda r: httpx.Response(200), "http://insecure.example/x").enabled
    assert not Heartbeat(Settings(_env_file=None)).enabled


def test_heartbeat_url_is_redacted(monkeypatch):
    monkeypatch.setenv("ORBIT_HEARTBEAT_URL", URL)
    assert "abc-secret-uuid" not in redact(f"GET {URL} failed")


def test_client_setup_failure_is_contained(monkeypatch):
    def fail(**kwargs):
        raise ImportError("unsupported proxy")

    monkeypatch.setattr(httpx, "Client", fail)
    hb = Heartbeat(Settings(_env_file=None, ORBIT_HEARTBEAT_URL="https://monitor.example/ping"))
    assert hb.enabled
    assert not hb.maybe_ping(datetime.now(timezone.utc))
    assert hb.last_error == "ImportError"
