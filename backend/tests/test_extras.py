"""Shadow lab, monthly report card and AI news warnings: they record and warn, and never place orders."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from orbit import news_ai, report, shadow
from orbit.db import ControlState, EventInbox, OrderIntent

UTC = timezone.utc


def _bars(cal, start, end, price_fn):
    out = []
    for i, s in enumerate(cal.sessions_between(start, end)):
        p = price_fn(i)
        out.append((s.day, p, p))
    return out


def test_simulations_follow_their_rules(cal):
    n = len(cal.sessions_between(date(2021, 1, 4), date(2023, 6, 30)))
    up_then_down = lambda i: 100 + i * 0.2 if i < n - 60 else 100 + (n - 60) * 0.2 - (i - (n - 60)) * 2.0
    spy = _bars(cal, date(2021, 1, 4), date(2023, 6, 30), up_then_down)
    gld = _bars(cal, date(2021, 1, 4), date(2023, 6, 30), lambda i: 50 + i * 0.05)
    out = shadow.build(spy, gld, date(2023, 1, 3))
    by = {i["id"]: i for i in out["ideas"]}
    hold = by["spy_hold"]["history"]
    assert abs(hold["value"] - 1000 * spy[-1][2] / spy[[d for d, _, _ in spy].index(date(2022, 1, 3))][1]) < 0.01
    assert by["spy_rule"]["history"]["holding"]["SPY"] == 0.0  # the late fall pushed it below the 200-day average
    assert by["spy_rule"]["history"]["value"] > hold["value"]  # and selling saved money
    assert by["gold_hold"]["history"]["return_pct"] > 0 and by["gold_rule"]["history"]["holding"]["GLD"] == 1.0
    mix = by["mix_70_30"]["history"]
    assert mix["holding"] == {"SPY": 0.0, "GLD": 0.3} and mix["trades"] >= 2
    assert by["spy_risk"]["history"]["available"] and by["spy_rule"]["live"]["start"] == "2023-01-03"


def test_shadow_lab_updates_once_per_session_and_never_orders(env):
    cal = env["cal"]
    calls = []

    def fetch(sym, start, end):
        calls.append(sym)
        return _bars(cal, start, end.date(), lambda i: 100 + i * 0.1)

    now = datetime(2026, 3, 10, 21, 30, tzinfo=UTC)  # 90 minutes after the 10 March close (20:00 UTC in summer time)
    with env["db"].session() as s:
        assert shadow.maybe_update(s, cal, now, fetch)
        lab = shadow.get(s)
        assert lab["available"] and lab["as_of"] == "2026-03-10" and len(lab["ideas"]) == len(shadow.IDEAS)
        assert not shadow.maybe_update(s, cal, now + timedelta(minutes=5), fetch)  # same session: no refetch
        assert s.scalars(select(OrderIntent)).first() is None
    assert calls == ["SPY", "GLD"]


def test_shadow_lab_failure_is_recorded_and_retried_later(env):
    cal = env["cal"]

    def broken(sym, start, end):
        raise RuntimeError("feed down")

    now = datetime(2026, 3, 10, 21, 30, tzinfo=UTC)
    with env["db"].session() as s:
        assert not shadow.maybe_update(s, cal, now, broken)
        assert shadow.get(s)["error"] == "feed down"
        tried = []
        assert not shadow.maybe_update(s, cal, now + timedelta(minutes=5), lambda *a: tried.append(1) or [])
        assert tried == []  # backs off for 30 minutes


def test_monthly_report_sent_once_and_not_before_start(env):
    with env["db"].session() as s:
        c = s.get(ControlState, 1)
        c.activated_at = datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
        s.flush()
        assert not report.maybe_send(s, env["settings"], datetime(2026, 3, 20, 8, 0, tzinfo=UTC))  # February: before start
        assert not report.maybe_send(s, env["settings"], datetime(2026, 4, 1, 3, 0, tzinfo=UTC))  # 07:00 Dubai: too early
        assert report.maybe_send(s, env["settings"], datetime(2026, 4, 1, 6, 0, tzinfo=UTC))
        assert not report.maybe_send(s, env["settings"], datetime(2026, 4, 2, 6, 0, tzinfo=UTC))
        ev = s.scalars(select(EventInbox).where(EventInbox.type == "monthly_report")).all()
        assert len(ev) == 1 and "2026-03" in ev[0].title and "SPY 200-day rule" in ev[0].body


class FakeScorer:
    def __init__(self):
        self.seen = 0

    def score(self, texts):
        self.seen += len(texts)
        return [{"positive": 0.02, "negative": 0.95, "neutral": 0.03} if "crash" in t else
                {"positive": 0.1, "negative": 0.1, "neutral": 0.8} for t in texts]


def _news(now, n_bad, n_ok, sym="SPY"):
    return [{"id": i, "headline": ("Markets crash " if i < n_bad else "Fed speaks ") + str(i), "summary": "",
             "created_at": (now - timedelta(hours=1)).isoformat(), "symbols": [sym]} for i in range(n_bad + n_ok)]


def test_news_warning_once_per_day_and_scores_only_new_headlines(env):
    now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)  # Saturday: no open/close nearby
    sc = FakeScorer()
    with env["db"].session() as s:
        assert news_ai.maybe_update(s, env["settings"], env["cal"], now, lambda: _news(now, 5, 2), sc)
        m = news_ai.get_mood(s)["funds"]["SPY"]
        assert m["warn"] and m["strong_negative"] == 5 and m["headlines"] == 7
        assert not news_ai.maybe_update(s, env["settings"], env["cal"], now + timedelta(minutes=5), lambda: _news(now, 5, 2), sc)
        assert news_ai.maybe_update(s, env["settings"], env["cal"], now + timedelta(minutes=20), lambda: _news(now, 5, 2), sc)
        assert sc.seen == 7  # already-scored headlines are not scored again
        ev = s.scalars(select(EventInbox).where(EventInbox.type == "news_warning")).all()
        assert len(ev) == 1 and "SPY" in ev[0].title and "does not trade on news" in ev[0].body
        assert s.scalars(select(OrderIntent)).first() is None


def test_calm_news_does_not_warn_and_open_is_quiet(env):
    now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    with env["db"].session() as s:
        news_ai.maybe_update(s, env["settings"], env["cal"], now, lambda: _news(now, 2, 8), FakeScorer())
        assert not news_ai.get_mood(s)["funds"]["SPY"]["warn"]
        assert s.scalars(select(EventInbox).where(EventInbox.type == "news_warning")).first() is None
    open_now = datetime(2026, 3, 10, 13, 35, tzinfo=UTC)  # 5 minutes after the open (EDT)
    assert news_ai.quiet_time(env["cal"], open_now)
    assert not news_ai.quiet_time(env["cal"], datetime(2026, 3, 10, 16, 0, tzinfo=UTC))


def test_compare_and_news_api(tmp_path):
    from fastapi.testclient import TestClient
    from orbit import auth
    from orbit.api import create_app
    from orbit.config import Settings
    from orbit.db import Database, init_db
    from tests.test_api_security import ORIGIN, PW, login
    settings = Settings(_env_file=None, ORBIT_DATA_DIR=str(tmp_path))
    db = Database(tmp_path / "api.sqlite3")
    init_db(db.engine)
    with db.session() as s:
        auth.set_password(s, PW)
    c = TestClient(create_app(settings, db), base_url=ORIGIN)
    assert c.get("/api/compare").status_code == 401
    login(c)
    r = c.get("/api/compare").json()
    assert [x["name"] for x in r["real"]] == ["SPY 200-day rule", "Gold AI"] and r["lab"]["available"] is False
    n = c.get("/api/news").json()
    assert n["available"] is False and n["ai"]["installed"] is False
