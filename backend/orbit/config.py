"""Configuration, paper-only enforcement and secret redaction.

Secrets come only from environment variables or an ignored ``.env`` file.
They never reach the frontend, logs, HTTP responses or exports.
"""
from __future__ import annotations

import os
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
PAPER_TRADING_HOST = "paper-api.alpaca.markets"
LIVE_HOSTS = {"api.alpaca.markets", "broker-api.alpaca.markets"}


class LiveTradingRejected(RuntimeError):
    """Raised whenever configuration points at anything other than the Alpaca paper endpoint."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # Broker (Alpaca paper only)
    alpaca_api_key_id: SecretStr | None = Field(default=None, alias="APCA_API_KEY_ID")
    alpaca_api_secret_key: SecretStr | None = Field(default=None, alias="APCA_API_SECRET_KEY")
    alpaca_base_url: str = Field(default="https://paper-api.alpaca.markets", alias="APCA_API_BASE_URL")
    alpaca_data_feed: str = Field(default="iex", alias="ORBIT_DATA_FEED")

    # App
    data_dir: Path = Field(default=ROOT / "data", alias="ORBIT_DATA_DIR")
    bind_host: str = Field(default="127.0.0.1", alias="ORBIT_BIND_HOST")
    port: int = Field(default=8765, alias="ORBIT_PORT")
    deployment_mode: str = Field(default="local", alias="ORBIT_DEPLOYMENT_MODE")  # local | remote
    public_origin: str | None = Field(default=None, alias="ORBIT_PUBLIC_ORIGIN")
    session_hours: int = Field(default=12, alias="ORBIT_SESSION_HOURS")

    usd_to_aed: Decimal = Field(default=Decimal("3.6725"), alias="USD_TO_AED")

    # Notifications (optional)
    ntfy_server: str = Field(default="https://ntfy.sh", alias="NTFY_SERVER")
    ntfy_topic: SecretStr | None = Field(default=None, alias="NTFY_TOPIC")
    ntfy_token: SecretStr | None = Field(default=None, alias="NTFY_TOKEN")
    # Private Telegram chat for detailed alerts. Both are secrets: the token controls the bot.
    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: SecretStr | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    worker_tick_seconds: int = Field(default=20, alias="ORBIT_WORKER_TICK_SECONDS")

    # Optional external dead-man's-switch (e.g. a healthchecks-style ping URL). The URL is a secret.
    heartbeat_url: SecretStr | None = Field(default=None, alias="ORBIT_HEARTBEAT_URL")
    heartbeat_interval_seconds: int = Field(default=300, alias="ORBIT_HEARTBEAT_INTERVAL_SECONDS")

    @field_validator("usd_to_aed")
    @classmethod
    def _rate_positive(cls, v: Decimal) -> Decimal:
        if v <= 0 or v > 100:
            raise ValueError("USD_TO_AED must be a positive reference rate")
        return v

    @field_validator("alpaca_api_key_id", "alpaca_api_secret_key", "ntfy_topic", "ntfy_token", "heartbeat_url",
                     "telegram_bot_token", "telegram_chat_id", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
        raw = raw.strip()
        if not raw or raw.startswith("replace-with"):
            return None
        return raw

    @field_validator("alpaca_data_feed")
    @classmethod
    def _feed(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"iex", "sip", "delayed_sip"}:
            raise ValueError("ORBIT_DATA_FEED must be iex, sip or delayed_sip")
        return v

    # ---- derived ----
    @property
    def db_path(self) -> Path:
        return self.data_dir / "orbit.sqlite3"

    @property
    def broker_configured(self) -> bool:
        return bool(self.alpaca_api_key_id and self.alpaca_api_secret_key)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def allowed_origins(self) -> set[str]:
        if self.deployment_mode == "remote" and self.public_origin:
            return {self.public_origin.rstrip("/")}
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    @property
    def allowed_hosts(self) -> set[str]:
        loopback = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.deployment_mode == "remote" and self.public_origin:
            # The HTTPS proxy (e.g. `tailscale serve`) may forward either the public name or the loopback
            # upstream as Host. Loopback is only reachable from the server itself (port published on 127.0.0.1).
            return {urlparse(self.public_origin).netloc} | loopback
        return loopback


def assert_paper_url(url: str) -> str:
    """Accept only the Alpaca paper trading endpoint. Anything else (including live) is rejected."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host in LIVE_HOSTS:
        raise LiveTradingRejected(
            "Live Alpaca endpoint rejected. Orbit Trading Lab v1 is paper-only; there is no live route."
        )
    if parsed.scheme != "https" or host != PAPER_TRADING_HOST or parsed.port not in (None, 443):
        raise LiveTradingRejected(
            f"Broker base URL must be https://{PAPER_TRADING_HOST}; refusing {host or 'empty URL'}."
        )
    return f"https://{PAPER_TRADING_HOST}"


def validate_startup(settings: Settings) -> list[str]:
    """Return a list of fatal configuration problems. Remote binding requires explicit secure config."""
    problems: list[str] = []
    try:
        assert_paper_url(settings.alpaca_base_url)
    except LiveTradingRejected as exc:
        problems.append(str(exc))
    loopback = settings.bind_host in {"127.0.0.1", "::1", "localhost"}
    if not loopback:
        if settings.deployment_mode != "remote":
            problems.append(
                "Non-loopback bind requested without ORBIT_DEPLOYMENT_MODE=remote. Refusing to expose the app."
            )
        elif not (settings.public_origin or "").startswith("https://"):
            problems.append(
                "Remote mode requires ORBIT_PUBLIC_ORIGIN=https://... behind HTTPS or a private tunnel/VPN."
            )
    return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------- redaction
_SECRET_PATTERNS = [
    re.compile(r"(?i)(apca[-_]api[-_](?:key[-_]id|secret[-_]key)\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)?\s*)(\S+)"),
    re.compile(r"(?i)(secret|token|password|api[_-]?key)(['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)"),
    re.compile(r"\b(PK|AK|CK)[A-Z0-9]{14,}\b"),  # Alpaca-style key ids
    re.compile(r"\btk_[A-Za-z0-9]{10,}\b"),  # ntfy tokens
    re.compile(r"(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,}"),  # Telegram bot tokens (also inside .../bot<token>/...)
]


def _known_secret_values() -> list[str]:
    vals = []
    for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "NTFY_TOKEN", "NTFY_TOPIC", "ORBIT_HEARTBEAT_URL", "TELEGRAM_BOT_TOKEN"):
        v = os.environ.get(name)
        if v and len(v) >= 6:
            vals.append(v)
    try:
        s = get_settings()
        for sv in (s.alpaca_api_key_id, s.alpaca_api_secret_key, s.ntfy_token, s.ntfy_topic, s.heartbeat_url, s.telegram_bot_token):
            if sv is not None:
                raw = sv.get_secret_value()
                if raw and len(raw) >= 6:
                    vals.append(raw)
    except Exception:  # settings may be invalid; redaction must still work
        pass
    return vals


def redact(text: object) -> str:
    """Remove secrets from any string destined for logs, responses, exports or notifications."""
    s = str(text)
    for v in _known_secret_values():
        s = s.replace(v, "[REDACTED]")
    s = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", s)
    s = _SECRET_PATTERNS[1].sub(r"\1[REDACTED]", s)
    s = _SECRET_PATTERNS[2].sub(r"\1\2[REDACTED]", s)
    s = _SECRET_PATTERNS[3].sub("[REDACTED]", s)
    s = _SECRET_PATTERNS[4].sub("[REDACTED]", s)
    s = _SECRET_PATTERNS[5].sub("[REDACTED]", s)
    return s


def mask(value: SecretStr | str | None) -> str:
    if value is None:
        return "not set"
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not raw:
        return "not set"
    return f"set (…{raw[-2:]})" if len(raw) > 8 else "set"
