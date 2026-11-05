from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from orbit.config import Settings, redact
from orbit.db import NotificationOutbox
from orbit.engine import strategy_ledger  # noqa: F401
from orbit.notify import TelegramSender, dispatch_outbox, emit, set_notify_settings

from tests.conftest import at

TOKEN = "123456789:" + "A" * 35
TG = dict(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="42")


def test_fill_alert_has_price_value_reason_and_pnl(env):
    """Mock-broker lifecycle: the BUY fill alert explains what, how much, why, and the position after."""
    from orbit import controls
    from orbit.db import EventInbox
    from tests.test_engine import D1, EVAL
    eng, db, b = env["engine"], env["db"], env["broker"]
    eng.tick(EVAL)
    with db.session() as s:
        controls.activate(s, EVAL, env["cal"], True)
    b.auto_fill_price = Decimal("400")
    for m in (31, 32, 33):
        eng.tick(at(D1, 14, m))
    with db.session() as s:
        evs = {e.type: e for e in s.scalars(select(EventInbox))}
    body = evs["order_filled"].body
    assert "BOUGHT 0.625 SPY at 400.00 USD" in body and "USD 250.00 (≈ AED 918.13)" in body
    assert "Why:" in body and "Strategy now holds: 0.625 SPY" in body and "Paper account" in body
    assert "Why:" in evs["order_submitted"].body and "Why:" in evs["intent_created"].body


def test_realized_pnl_for_sell_fill(env):
    from datetime import datetime, timezone

    from orbit import alerts
    from orbit.db import Fill, OrderIntent
    from orbit.engine import active_version
    with env["db"].session() as s:
        vid = active_version(s).id

        def mk(side, n):
            return OrderIntent(account_key="acct", symbol="SPY", strategy_version_id=vid, kind="ENTRY" if side == "BUY" else "EXIT",
                               side=side, signal_session=date(2026, 11, n), execution_session=date(2026, 11, n), attempt=1,
                               client_order_id=f"c{side}{n}", state="FILLED", reason="r")
        b, x = mk("BUY", 3), mk("SELL", 5)
        s.add_all([b, x])
        s.flush()
        s.add(Fill(intent_id=b.id, broker_order_id="o1", fill_key="k1", side="BUY", qty=Decimal("0.5"), price=Decimal("500"),
                   at=datetime(2026, 11, 3, 15, tzinfo=timezone.utc)))
        f2 = Fill(intent_id=x.id, broker_order_id="o2", fill_key="k2", side="SELL", qty=Decimal("0.5"), price=Decimal("520"),
                  at=datetime(2026, 11, 5, 15, tzinfo=timezone.utc))
        s.add(f2)
        s.flush()
        assert alerts.realized_for_fill(s, x, f2.id) == Decimal("10.0")
        title, body = alerts.fill_text(s, x, f2, Decimal(0), Decimal(0))
        assert "SOLD" in title and "+10.00 USD" in body and "(+4.00%)" in body and "(all cash)" in body


class _Transport(httpx.BaseTransport):
    def __init__(self, status=200, body=None, exc=None):
        self.status, self.body, self.exc, self.requests = status, body or {"ok": True}, exc, []

    def handle_request(self, request):
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, json=self.body)


def test_telegram_sender_posts_to_chat_and_hides_token():
    st = Settings(_env_file=None, **TG)
    t = _Transport()
    TelegramSender(st, httpx.Client(transport=t)).send("Orbit: BOUGHT SPY", "🟢 BOUGHT 0.3 SPY", 3)
    req = t.requests[0]
    assert req.url.path.endswith("/sendMessage") and b'"chat_id":"42"' in req.content.replace(b" ", b"")
    bad = _Transport(exc=httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage"))
    with pytest.raises(RuntimeError) as e:
        TelegramSender(st, httpx.Client(transport=bad)).send("t", "m", 3)
    assert TOKEN not in str(e.value)
    with pytest.raises(RuntimeError) as e:
        TelegramSender(st, httpx.Client(transport=_Transport(401, {"ok": False, "description": "Unauthorized"}))).send("t", "m", 3)
    assert "401" in str(e.value) and TOKEN not in str(e.value)


def test_token_pattern_is_redacted():
    assert TOKEN not in redact(f"error at /bot{TOKEN}/sendMessage")


def test_routing_telegram_detailed_ntfy_redacted(env):
    db = env["db"]
    st = Settings(_env_file=None, NTFY_TOPIC="orbit-test-topic-xyz", **TG)
    with db.session() as s:
        set_notify_settings(s, {"enabled": True})
        emit(s, "order_filled", "info", "Orbit: BOUGHT SPY", "🟢 BOUGHT 0.3 SPY at 765.82 USD\nWhy: above average", "fill:x", settings=st)
        rows = {r.channel: r for r in s.scalars(select(NotificationOutbox))}
    assert rows["telegram"].status == "QUEUED" and "765.82" in rows["telegram"].message
    assert rows["ntfy"].status == "QUEUED" and "765.82" not in rows["ntfy"].message  # public topic: redacted


def test_dispatch_uses_channel_sender(env):
    db = env["db"]
    st = Settings(_env_file=None, **TG)

    class Rec:
        def __init__(self):
            self.sent = []

        def send(self, title, message, priority):
            self.sent.append(message)
    rec = Rec()
    with db.session() as s:
        set_notify_settings(s, {"enabled": True, "quiet_start": "00:00", "quiet_end": "00:00"})
        emit(s, "order_filled", "info", "Orbit: BOUGHT SPY", "🟢 BOUGHT", "fill:y", settings=st)
        stats = dispatch_outbox(s, {"telegram": rec}, at(date(2026, 11, 4), 15, 0))
    assert stats["sent"] == 1 and rec.sent == ["🟢 BOUGHT"]
