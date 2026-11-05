"""Durable event inbox + notification outbox + optional ntfy and Telegram adapters.

- The inbox works with no external credentials.
- ntfy topics on a public server can be read by anyone who knows the name, so ntfy messages default to a
  REDACTED summary: no size, balance or P&L. Details only if the owner opts in AND an ntfy token is configured.
- Telegram sends to one private chat (your own chat with your own bot), so it carries the full trade
  details and reasons. It never carries keys or account numbers.
- Outbox failures retry with backoff and never touch trading state.
- "Accepted by provider" only means ntfy accepted the HTTP request, not that a phone displayed it.
- Notification links may open the app; they never authorise anything (all controls need a session + CSRF).
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orbit.config import Settings, get_settings, redact
from orbit.market_calendar import UTC
from orbit.db import KV, EventInbox, NotificationOutbox
from orbit.market_calendar import DUBAI, now_utc

log = logging.getLogger("orbit.notify")

SEVERITY_PRIORITY = {"routine": 2, "info": 3, "warning": 4, "critical": 5}
PUSH_TYPES = {
    "automation_activated", "automation_paused", "automation_changed", "intent_created", "order_submitted",
    "order_filled", "order_rejected", "stale_data", "reconciliation_failed", "missed_execution",
    "worker_started", "worker_stopped", "security_paused", "risk_warning", "risk_paused", "ambiguous_order",
    "review_required", "settings_changed", "daily_summary", "test_notification", "cash_flow_detected",
}
MAX_ATTEMPTS = 8
NON_CRITICAL_PER_HOUR = 12

DEFAULT_NOTIFY_SETTINGS = {
    "enabled": False,
    "include_financial_details": False,
    "quiet_start": "23:00",
    "quiet_end": "07:00",
    "timezone": "Asia/Dubai",
}


def get_notify_settings(s: Session) -> dict:
    row = s.get(KV, "notify_settings")
    out = dict(DEFAULT_NOTIFY_SETTINGS)
    if row:
        out.update(row.value)
    return out


def set_notify_settings(s: Session, values: dict) -> dict:
    cur = get_notify_settings(s)
    for k in DEFAULT_NOTIFY_SETTINGS:
        if k in values:
            cur[k] = values[k]
    for k in ("quiet_start", "quiet_end"):
        time.fromisoformat(cur[k])  # validates HH:MM
    ZoneInfo(cur["timezone"])
    row = s.get(KV, "notify_settings")
    if row:
        row.value = cur
    else:
        s.add(KV(key="notify_settings", value=cur))
    return cur


def emit(s: Session, type_: str, severity: str, title: str, body: str, dedupe_key: str, ref: str = "",
         public_summary: str | None = None, settings: Settings | None = None) -> EventInbox | None:
    """Record an event once (deduplicated) and queue an external notification if routing allows."""
    if severity not in SEVERITY_PRIORITY:
        raise ValueError("bad severity")
    title, body = redact(title)[:200], redact(body)[:4000]
    public = public_summary or f"Orbit Trading Lab: {title}. Open the app to review."
    ev = EventInbox(type=type_, severity=severity, title=title, body=body, public_summary=redact(public)[:300],
                    dedupe_key=dedupe_key[:160], ref=ref)
    try:
        with s.begin_nested():
            s.add(ev)
            s.flush()
    except IntegrityError:
        return None  # duplicate event: already recorded
    if type_ in PUSH_TYPES and severity != "routine":
        _queue(s, ev, settings or get_settings())
    return ev


def _queue(s: Session, ev: EventInbox, settings: Settings | None) -> None:
    ns = get_notify_settings(s)
    enabled = bool(ns.get("enabled") and settings)
    if settings and settings.telegram_configured:
        # private chat: full details
        _add_outbox(s, ev, "telegram", "QUEUED" if enabled else "SUPPRESSED", ev.title, ev.body[:3500])
    if settings and settings.ntfy_topic:
        detailed = bool(ns.get("include_financial_details") and settings.ntfy_token)
        _add_outbox(s, ev, "ntfy", "QUEUED" if enabled else "SUPPRESSED",
                    ev.title if detailed else _public_title(ev), (ev.body if detailed else ev.public_summary)[:1000])
    if not (settings and (settings.telegram_configured or settings.ntfy_topic)):
        _add_outbox(s, ev, "ntfy", "SUPPRESSED", _public_title(ev), ev.public_summary[:1000])


def _add_outbox(s: Session, ev: EventInbox, channel: str, status: str, title: str, message: str) -> None:
    try:
        with s.begin_nested():
            s.add(NotificationOutbox(event_id=ev.id, channel=channel, dedupe_key=f"{channel}:{ev.dedupe_key}"[:160],
                                     status=status, priority=SEVERITY_PRIORITY[ev.severity], title=title, message=message,
                                     next_attempt_at=datetime(2000, 1, 1, tzinfo=UTC)))  # due immediately
            s.flush()
    except IntegrityError:
        pass


def _public_title(ev: EventInbox) -> str:
    labels = {"critical": "Action needed", "warning": "Warning", "info": "Update"}
    return f"Orbit: {labels.get(ev.severity, 'Update')} ({ev.type.replace('_', ' ')})"


def in_quiet_hours(now: datetime, ns: dict) -> bool:
    tz = ZoneInfo(ns.get("timezone", "Asia/Dubai"))
    local = now.astimezone(tz).time()
    start, end = time.fromisoformat(ns["quiet_start"]), time.fromisoformat(ns["quiet_end"])
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


class NtfySender:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)

    def send(self, title: str, message: str, priority: int) -> None:
        topic = self.settings.ntfy_topic.get_secret_value() if self.settings.ntfy_topic else None
        if not topic:
            raise RuntimeError("ntfy topic not configured")
        server = self.settings.ntfy_server.rstrip("/")
        if not server.startswith("https://"):
            raise RuntimeError("ntfy server must use https")
        headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": str(priority), "Tags": "orbit"}
        if self.settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_token.get_secret_value()}"
        r = self.client.post(f"{server}/{topic}", content=message.encode("utf-8"), headers=headers)
        if r.status_code >= 300:
            raise RuntimeError(f"ntfy HTTP {r.status_code}")


class TelegramSender:
    """Sends to one private chat via the Telegram Bot API. Errors never include the token."""

    API = "https://api.telegram.org"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.telegram_configured:
            raise RuntimeError("Telegram is not configured")
        self._token = settings.telegram_bot_token.get_secret_value()
        self._chat = settings.telegram_chat_id.get_secret_value()
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)

    def send(self, title: str, message: str, priority: int) -> None:
        text = message if message.startswith(("🟢", "🔴", "📤", "🗓")) else f"{title}\n\n{message}"
        if priority >= 5:
            text = "⛔ ACTION NEEDED\n" + text
        try:
            r = self.client.post(f"{self.API}/bot{self._token}/sendMessage",
                                 json={"chat_id": self._chat, "text": text[:4000], "disable_web_page_preview": True,
                                       "disable_notification": priority <= 2})
        except httpx.HTTPError as e:
            raise RuntimeError(f"Telegram request failed: {type(e).__name__}") from None  # message may contain the URL
        if r.status_code >= 300:
            desc = ""
            try:
                desc = str(r.json().get("description", ""))[:120]
            except ValueError:
                pass
            raise RuntimeError(f"Telegram HTTP {r.status_code} {redact(desc)}".strip())


def build_senders(settings: Settings) -> dict:
    senders = {}
    if settings.ntfy_topic:
        senders["ntfy"] = NtfySender(settings)
    if settings.telegram_configured:
        senders["telegram"] = TelegramSender(settings)
    return senders


def dispatch_outbox(s: Session, sender, now: datetime | None = None, limit: int = 20) -> dict:
    """Send due notifications. `sender` is one sender (used for ntfy) or a {channel: sender} dict.
    Never raises; never touches trading tables."""
    senders = sender if isinstance(sender, dict) else ({"ntfy": sender} if sender is not None else {})
    now = now or now_utc()
    ns = get_notify_settings(s)
    stats = {"sent": 0, "failed": 0, "held": 0}
    due = s.scalars(
        select(NotificationOutbox).where(NotificationOutbox.status.in_(["QUEUED", "FAILED"]),
                                         NotificationOutbox.next_attempt_at <= now,
                                         NotificationOutbox.attempts < MAX_ATTEMPTS)
        .order_by(NotificationOutbox.priority.desc(), NotificationOutbox.id).limit(limit)
    ).all()
    hour_ago = now - timedelta(hours=1)
    sent_recent = s.scalar(select(func.count()).select_from(NotificationOutbox).where(
        NotificationOutbox.accepted_at >= hour_ago, NotificationOutbox.priority < 5)) or 0
    for n in due:
        critical = n.priority >= 5
        if not critical and in_quiet_hours(now, ns):
            n.next_attempt_at = now + timedelta(minutes=30)
            stats["held"] += 1
            continue
        if not critical and sent_recent >= NON_CRITICAL_PER_HOUR:
            n.next_attempt_at = now + timedelta(minutes=15)
            n.last_error = "rate limited locally"
            stats["held"] += 1
            continue
        snd = senders.get(n.channel)
        if snd is None:
            n.next_attempt_at = now + timedelta(minutes=30)
            n.last_error = f"{n.channel} not configured"
            stats["held"] += 1
            continue
        n.attempts += 1
        try:
            snd.send(n.title, n.message, n.priority)
            n.status = "ACCEPTED_BY_PROVIDER"
            n.accepted_at = now
            n.last_error = ""
            stats["sent"] += 1
            if not critical:
                sent_recent += 1
        except Exception as exc:  # noqa: BLE001 - any failure is recorded, retried, never propagated
            n.status = "FAILED"
            n.last_error = redact(str(exc))[:300]
            n.next_attempt_at = now + timedelta(seconds=min(3600, 30 * (2 ** (n.attempts - 1))))
            stats["failed"] += 1
    return stats


def queue_daily_summary(s: Session, session_day, lines: list[str], settings: Settings | None) -> None:
    body = "Routine results: " + "; ".join(lines) if lines else "No routine results."
    emit(s, "daily_summary", "info", f"Daily summary {session_day}", body, f"daily_summary:{session_day}",
         public_summary="Orbit daily summary is ready. Open the app to review.", settings=settings)


def queue_test_notification(s: Session, settings: Settings) -> NotificationOutbox:
    stamp = now_utc().astimezone(DUBAI).strftime("%Y-%m-%d %H:%M:%S")
    ev = emit(s, "test_notification", "info", "Test notification",
              f"Test requested by the owner at {stamp} Dubai time.", f"test:{stamp}",
              public_summary=f"Orbit test notification ({stamp} Dubai). No action needed.", settings=settings)
    return s.scalar(select(NotificationOutbox).where(NotificationOutbox.event_id == ev.id)) if ev else None
