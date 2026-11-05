"""Outbound heartbeat to an external monitor (dead-man's switch).

The worker pings ORBIT_HEARTBEAT_URL after a cycle completes, at most once per interval. The external
service alerts you when pings STOP (worker crashed, server down, network lost) — the one failure a
dead host cannot report itself. Ping failures are logged and never affect trading.
Only https URLs are accepted; redirects are not followed; the URL itself is treated as a secret.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

from orbit.config import Settings, redact

log = logging.getLogger("orbit.heartbeat")


class Heartbeat:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.url = settings.heartbeat_url.get_secret_value() if settings.heartbeat_url else None
        if self.url and urlparse(self.url).scheme != "https":
            log.error("ORBIT_HEARTBEAT_URL must be https; heartbeat disabled.")
            self.url = None
        self.interval = timedelta(seconds=max(60, settings.heartbeat_interval_seconds))
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)
        self.last_ok: datetime | None = None
        self.last_attempt: datetime | None = None
        self.last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def maybe_ping(self, now: datetime) -> bool:
        """Ping if due. Returns True when a ping was accepted (HTTP 2xx). Never raises."""
        if not self.url:
            return False
        if self.last_attempt and now - self.last_attempt < self.interval:
            return False
        self.last_attempt = now
        try:
            r = self.client.get(self.url)
            if 200 <= r.status_code < 300:
                self.last_ok, self.last_error = now, ""
                return True
            self.last_error = f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001 - monitoring must never break the trading loop
            self.last_error = type(exc).__name__
        log.warning("heartbeat ping failed: %s", redact(self.last_error))
        return False

    def status(self) -> dict:
        return {"enabled": self.enabled, "last_ok": self.last_ok.isoformat() if self.last_ok else None,
                "last_error": self.last_error, "interval_seconds": int(self.interval.total_seconds())}
