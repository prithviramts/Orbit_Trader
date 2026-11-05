"""Execution lifecycle and recovery, exercised through the worker's normal entry point Engine.tick()."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from orbit import controls
from orbit.broker.base import AccountInfo, ExecutionGuard, GuardViolation, OrderRequest
from orbit.config import LiveTradingRejected, assert_paper_url, redact
from orbit.db import ControlState, Decision, EventInbox, Fill, NotificationOutbox, OrderIntent, StrategyPosition
from orbit.engine import Engine, control, position
from orbit.notify import dispatch_outbox, emit, set_notify_settings
from orbit.strategy import StrategyParams
from orbit.worker import acquire_lease, heartbeat, LeaseLost
from tests.conftest import at

D0 = date(2026, 3, 5)  # Thursday; next session 2026-03-06 (open 14:30 UTC, EST)
D1 = date(2026, 3, 6)
D2 = date(2026, 3, 9)  # Monday after DST change: open 13:30 UTC (EDT)
EVAL = at(D0, 21, 16)


def submits(broker):
    return [c for c in broker.calls if c.startswith("submit:")]


def intents(env):
    with env["db"].session() as s:
        return list(s.scalars(select(OrderIntent).order_by(OrderIntent.id)))


def ctl(env) -> ControlState:
    with env["db"].session() as s:
        return s.get(ControlState, 1)


def activate(env, now=EVAL):
    eng = env["engine"]
    eng.tick(now)  # reconcile + observe decision
    with env["db"].session() as s:
        return controls.activate(s, now, env["cal"], True)


# ------------------------------------------------------------------ observe / activation
def test_first_launch_is_observe_and_never_submits(env):
    eng, b = env["engine"], env["broker"]
    eng.tick(EVAL)
    eng.tick(EVAL + timedelta(seconds=20))  # duplicate tick / UI refresh equivalent
    assert ctl(env).mode == "OBSERVE"
    with env["db"].session() as s:
        decs = list(s.scalars(select(Decision)))
    assert len(decs) == 1 and decs[0].action == "SKIP" and "OBSERVE" in decs[0].reason
    assert decs[0].inputs["would_action"] == "BUY"
    assert intents(env) == [] and submits(b) == []
    eng.tick(at(D1, 14, 31))
    assert submits(b) == []


def test_activation_refused_before_checks(env):
    with env["db"].session() as s:
        with pytest.raises(controls.ControlError):
            controls.activate(s, EVAL, env["cal"], True)
    assert ctl(env).mode == "OBSERVE"


def test_activation_refused_without_credentials(env):
    env["engine"].tick(EVAL)
    with env["db"].session() as s:
        with pytest.raises(controls.ControlError):
            controls.activate(s, EVAL, env["cal"], broker_configured=False)


def test_onboarding_entry_then_single_submission_and_fill(env):
    res = activate(env)
    assert res["created_intent"]
    it = intents(env)[0]
    assert it.state == "PENDING" and it.execution_session == D1 and it.frozen_notional == Decimal(250)
    b, eng = env["broker"], env["engine"]
    eng.tick(at(D1, 14, 29))  # before open: nothing
    assert submits(b) == []
    b.auto_fill_price = Decimal("400")
    eng.tick(at(D1, 14, 31))
    eng.tick(at(D1, 14, 32))
    eng.tick(at(D1, 14, 33))
    assert len(submits(b)) == 1
    it = intents(env)[0]
    assert it.state == "FILLED" and it.submitted_notional == Decimal("250.00")
    with env["db"].session() as s:
        pos = position(s, it.account_key)
        assert pos.held_qty == Decimal("0.625") and pos.signal_target == "INVESTED"
        assert s.scalar(select(func.count()).select_from(Fill)) == 1


def test_invested_hold_does_not_rebuy(env):
    activate(env)
    env["broker"].auto_fill_price = Decimal("400")
    env["engine"].tick(at(D1, 14, 31))
    env["engine"].tick(at(D1, 21, 16))  # evaluation for D1: still invested
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.session == D1))
    assert d.action == "HOLD" and "without resizing" in d.reason
    assert len(intents(env)) == 1


# ------------------------------------------------------------------ ambiguity / restart
def test_timeout_after_accept_is_reconciled_without_duplicate(env):
    activate(env)
    b = env["broker"]
    b.submit_behaviour = ["timeout_after_accept"]
    env["engine"].tick(at(D1, 14, 31))
    it = intents(env)[0]
    assert it.state == "SUBMITTED" and it.broker_order_id
    env["engine"].tick(at(D1, 14, 32))
    assert len(submits(b)) == 1


def test_timeout_and_not_found_stays_ambiguous_and_never_resubmits(env):
    activate(env)
    b = env["broker"]
    b.submit_behaviour = ["timeout_before_accept"]
    env["engine"].tick(at(D1, 14, 31))
    it = intents(env)[0]
    assert it.state == "AMBIGUOUS" and it.review_required
    for m in range(32, 40):
        env["engine"].tick(at(D1, 14, m))
    assert len(submits(b)) == 1
    assert ctl(env).review_required
    # next evaluation does not create a second entry while ambiguity is unresolved
    env["engine"].tick(at(D1, 21, 16))
    assert len(intents(env)) == 1
    # owner acknowledgement requires an explicit absent-at-broker confirmation
    with env["db"].session() as s:
        with pytest.raises(controls.ControlError):
            controls.acknowledge_review(s, it.id, at(D1, 22, 0), env["cal"], True, False, b)


def test_restart_after_submission_recovers_from_broker(env):
    activate(env)
    b = env["broker"]
    # Simulate: process persisted SUBMITTING, sent the order, then crashed before saving the result.
    it = intents(env)[0]
    with env["db"].session() as s:
        row = s.get(OrderIntent, it.id)
        row.state = "SUBMITTING"
    b.submit_order(OrderRequest("SPY", "buy", it.client_order_id, notional=Decimal(250)))
    b.fill(b.by_client[it.client_order_id], Decimal("0.625"), Decimal(400))
    new_engine = Engine(env["db"], b, env["cal"], env["data"], env["settings"])  # fresh process
    new_engine.tick(at(D1, 14, 32))
    it = intents(env)[0]
    assert it.state == "FILLED"
    assert len(submits(b)) == 1  # only the pre-crash submission


def test_restart_with_submitting_and_no_broker_order_goes_ambiguous(env):
    activate(env)
    it = intents(env)[0]
    with env["db"].session() as s:
        s.get(OrderIntent, it.id).state = "SUBMITTING"
    env["broker"].lookup_behaviour = ["not_found"]
    env["engine"].tick(at(D1, 14, 32))
    assert intents(env)[0].state == "AMBIGUOUS"
    assert submits(env["broker"]) == []


# ------------------------------------------------------------------ fills
def test_duplicate_and_out_of_order_fill_snapshots(env):
    activate(env)
    b, eng = env["broker"], env["engine"]
    eng.tick(at(D1, 14, 31))
    oid = intents(env)[0].broker_order_id
    b.fill(oid, Decimal("0.3"), Decimal(400))
    eng.tick(at(D1, 14, 33))
    eng.tick(at(D1, 14, 34))  # duplicate snapshot
    # out-of-order: broker briefly reports an older cumulative quantity
    b.orders[oid].filled_qty = Decimal("0.1")
    eng.tick(at(D1, 14, 35))
    b.orders[oid].filled_qty = Decimal("0.3")
    with env["db"].session() as s:
        assert s.scalar(select(func.count()).select_from(Fill)) == 1
        assert position(s, intents(env)[0].account_key).held_qty == Decimal("0.3")


def test_partial_entry_then_cancel_adopts_filled_qty_without_topup(env):
    activate(env)
    b, eng = env["broker"], env["engine"]
    eng.tick(at(D1, 14, 31))
    oid = intents(env)[0].broker_order_id
    b.fill(oid, Decimal("0.2"), Decimal(400))
    b.set_status(oid, "canceled")
    eng.tick(at(D1, 15, 0))
    it = intents(env)[0]
    assert it.state == "TERMINAL_PARTIAL"
    with env["db"].session() as s:
        pos = position(s, it.account_key)
        assert pos.held_qty == Decimal("0.2") and "no automatic top-up" in pos.exposure_note
    eng.tick(at(D1, 21, 16))
    with env["db"].session() as s:
        assert s.scalar(select(Decision).where(Decision.session == D1)).action == "HOLD"
    assert len(intents(env)) == 1


def test_partial_exit_creates_residual_exit_with_lineage(env):
    activate(env)
    b, eng, data = env["broker"], env["engine"], env["data"]
    b.auto_fill_price = Decimal(400)
    eng.tick(at(D1, 14, 31))
    b.auto_fill_price = None
    data.trend = "down"
    eng.tick(at(D1, 21, 16))  # signal CASH -> exit intent for D2
    ex = intents(env)[1]
    assert ex.kind == "EXIT" and ex.execution_session == D2 and ex.target_qty == Decimal("0.625")
    eng.tick(at(D2, 13, 31))
    b.fill(ex.broker_order_id if intents(env)[1].broker_order_id else None, Decimal("0.5"), Decimal(390)) if False else None
    oid = intents(env)[1].broker_order_id
    b.fill(oid, Decimal("0.5"), Decimal(390))
    b.set_status(oid, "expired")
    eng.tick(at(D2, 20, 30))
    its = intents(env)
    assert its[1].state == "TERMINAL_PARTIAL"
    rx = its[2]
    assert rx.kind == "RESIDUAL_EXIT" and rx.parent_intent_id == its[1].id and rx.target_qty == Decimal("0.125")
    # never submitted while the previous exit could still fill; it targets the next window
    assert rx.state == "PENDING" and rx.execution_session > D2


# ------------------------------------------------------------------ failures needing review
def test_missed_window_requires_review_and_no_catchup(env):
    activate(env)
    env["engine"].tick(at(D1, 15, 40))  # laptop asleep through the window
    it = intents(env)[0]
    assert it.state == "MISSED_EXECUTION" and it.review_required
    assert submits(env["broker"]) == []
    env["engine"].tick(at(D1, 21, 16))
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.session == D1))
    assert d.action == "SKIP" and "review required" in d.reason
    # owner acknowledges -> superseded; a new intent for the next window with lineage
    env["engine"].tick(at(D1, 21, 58))  # fresh reconciliation is required for sizing
    with env["db"].session() as s:
        r = controls.acknowledge_review(s, it.id, at(D1, 22, 0), env["cal"], True, False, env["broker"])
    its = intents(env)
    assert its[0].state == "SUPERSEDED" and its[1].parent_intent_id == its[0].id
    assert its[1].signal_session == D1 and its[1].execution_session == D2  # latest completed signal, next window
    assert its[1].client_order_id != its[0].client_order_id


def test_rejection_is_visible_failure(env):
    activate(env)
    env["broker"].submit_behaviour = ["reject:insufficient buying power"]
    env["engine"].tick(at(D1, 14, 31))
    it = intents(env)[0]
    assert it.state == "REJECTED" and "insufficient" in it.reason
    with env["db"].session() as s:
        assert s.scalar(select(EventInbox).where(EventInbox.type == "order_rejected"))


def test_manual_account_drift_pauses_and_blocks_orders(env):
    env["broker"].add_manual_position("AAPL", Decimal(1))
    env["engine"].tick(EVAL)
    assert ctl(env).security_paused and "AAPL" in ctl(env).security_pause_reason
    with env["db"].session() as s:
        with pytest.raises(controls.ControlError):
            controls.activate(s, EVAL, env["cal"], True)


def test_foreign_order_pauses_after_confirmation(env):
    activate(env)
    env["broker"].add_foreign_order()
    env["engine"].tick(at(D1, 14, 29))
    env["engine"].tick(at(D1, 14, 30))
    assert ctl(env).security_paused
    env["engine"].tick(at(D1, 14, 31))
    assert submits(env["broker"]) == []


def test_insufficient_cash_after_gap_reduces_notional(env):
    activate(env)
    env["broker"].cash = Decimal("100")
    env["engine"].tick(at(D1, 14, 31))
    it = intents(env)[0]
    assert it.submitted_notional == Decimal("90.00")  # 100 cash - 10 reserve, below frozen 250
    assert "Reduced" in it.sizing["reduced_at_execution"]


def test_risk_pause_blocks_entries_but_not_exits(env):
    activate(env)
    with env["db"].session() as s:
        c = control(s)
        c.risk_paused_entries = True
        c.risk_pause_reason = "test"
    env["engine"].tick(at(D1, 14, 31))
    assert intents(env)[0].state == "SKIPPED"
    assert submits(env["broker"]) == []


def test_settings_change_moves_pending_intent_to_review(env):
    activate(env)
    with env["db"].session() as s:
        prev = controls.preview_settings(s, {"max_entry_notional_usd": "200"})
        assert prev["pending_intents"] and "REVIEW_REQUIRED" in prev["pending_intents"][0]["effect"]
        controls.update_settings(s, {"max_entry_notional_usd": "200"}, "lower cap for test")
    assert intents(env)[0].state == "REVIEW_REQUIRED"
    env["engine"].tick(at(D1, 14, 31))
    assert submits(env["broker"]) == []


def test_conflicting_working_order_is_cancelled_before_reassessment(env):
    activate(env)
    b, eng, data = env["broker"], env["engine"], env["data"]
    eng.tick(at(D1, 14, 31))  # submitted, not filled
    data.trend = "down"
    eng.tick(at(D1, 21, 16))
    it = intents(env)[0]
    assert it.state == "CANCEL_REQUESTED"
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.session == D1))
        assert "conflicting" in d.reason and d.inputs["awaiting_conflict_resolution"] == [it.id]
    b.set_status(it.broker_order_id, "canceled")
    eng.tick(at(D1, 21, 30))
    with env["db"].session() as s:
        seqs = list(s.scalars(select(Decision).where(Decision.session == D1).order_by(Decision.sequence)))
    assert [d.sequence for d in seqs] == [1, 2]
    assert intents(env)[0].state == "CANCELLED_ZERO_FILL" and not intents(env)[0].review_required


def test_close_position_never_oversells(env):
    activate(env)
    b, eng = env["broker"], env["engine"]
    b.auto_fill_price = Decimal(400)
    eng.tick(at(D1, 14, 31))
    eng.tick(at(D1, 16, 0))
    with env["db"].session() as s:
        iid = controls.close_position(s, at(D1, 16, 0), env["cal"])
    eng.tick(at(D1, 16, 1))
    it = [i for i in intents(env) if i.id == iid][0]
    assert it.kind == "CLOSE_MANUAL" and it.target_qty == Decimal("0.625") and it.state == "FILLED"
    assert "SPY" not in b.positions


# ------------------------------------------------------------------ guard / config
def _acct(cash="1000"):
    return AccountInfo("x", "****", "ACTIVE", "USD", Decimal(cash), Decimal(cash), None, Decimal(cash) * 4, Decimal(cash),
                       "4", False, False, False, False, False)


def test_execution_guard_blocks_bypass_attempts():
    g = ExecutionGuard(Decimal(250))
    with pytest.raises(GuardViolation):
        g.check(OrderRequest("TSLA", "buy", "c", notional=Decimal(10)), _acct(), Decimal(0))
    with pytest.raises(GuardViolation):
        g.check(OrderRequest("SPY", "buy", "c", notional=Decimal(251)), _acct(), Decimal(0))
    with pytest.raises(GuardViolation):  # margin buying power is never used
        g.check(OrderRequest("SPY", "buy", "c", notional=Decimal(200)), _acct("150"), Decimal(0))
    with pytest.raises(GuardViolation):
        g.check(OrderRequest("SPY", "sell", "c", qty=Decimal("1.000000001")), _acct(), Decimal(1))
    with pytest.raises(GuardViolation):
        g.check(OrderRequest("SPY", "buy", "c", notional=Decimal(10), type="limit"), _acct(), Decimal(0))
    with pytest.raises(GuardViolation):
        g.check(OrderRequest("SPY", "buy", "c", notional=Decimal(10), extended_hours=True), _acct(), Decimal(0))
    g.check(OrderRequest("SPY", "sell", "c", qty=Decimal(1)), _acct(), Decimal(1))


@pytest.mark.parametrize("url", ["https://api.alpaca.markets", "https://api.alpaca.markets/v2", "http://paper-api.alpaca.markets",
                                 "https://paper-api.alpaca.markets.evil.com", "https://broker-api.alpaca.markets", ""])
def test_live_or_unknown_broker_urls_rejected(url):
    with pytest.raises(LiveTradingRejected):
        assert_paper_url(url)


def test_paper_url_accepted():
    assert assert_paper_url("https://paper-api.alpaca.markets/") == "https://paper-api.alpaca.markets"


def test_secret_redaction(monkeypatch):
    monkeypatch.setenv("APCA_API_SECRET_KEY", "superSecretValue123456")
    msg = "error APCA-API-SECRET-KEY: superSecretValue123456 Authorization: Bearer tk_abcdefghijklmnop key PKABCDEFGHIJKLMNOPQR"
    out = redact(msg)
    assert "superSecretValue123456" not in out and "tk_abcdefghijklmnop" not in out and "PKABCDEFGHIJKLMNOPQR" not in out


# ------------------------------------------------------------------ worker overlap
def test_overlapping_workers_cannot_both_hold_the_lease(env):
    db = env["db"]
    t = at(D1, 14, 0)
    assert acquire_lease(db, "A", t)
    assert not acquire_lease(db, "B", t + timedelta(seconds=10))
    assert acquire_lease(db, "B", t + timedelta(seconds=200))  # A's lease expired (crash)
    with pytest.raises(LeaseLost):
        heartbeat(db, "A", t + timedelta(seconds=201))


def test_duplicate_evaluation_attempts_produce_one_decision(env):
    eng2 = Engine(env["db"], env["broker"], env["cal"], env["data"], env["settings"])
    env["engine"].tick(EVAL)
    eng2.tick(EVAL)
    with env["db"].session() as s:
        assert s.scalar(select(func.count()).select_from(Decision)) == 1


# ------------------------------------------------------------------ data / calendar in the live path
def test_missing_current_bar_waits_then_skips(env):
    env["data"].drop_last = True
    env["engine"].tick(EVAL)
    with env["db"].session() as s:
        assert s.scalar(select(func.count()).select_from(Decision)) == 0  # waiting, not substituting
    env["engine"].tick(at(D1, 14, 25))  # just before next open: give up with an explicit SKIP
    with env["db"].session() as s:
        d = s.scalar(select(Decision))
    assert d.action == "SKIP" and "never substituted" in d.reason


def test_holiday_and_early_close_scheduling(env, cal):
    # Wednesday before Thanksgiving 2026: next session is Friday (early close); Thursday is skipped
    from orbit.engine import next_eligible_session
    s = next_eligible_session(cal, date(2026, 11, 25), at(date(2026, 11, 25), 21, 20), 5)
    assert s.day == date(2026, 11, 27) and s.early_close
    # evaluation of the early-close session waits for its own 13:00 ET close + 15 min
    assert cal.latest_completed_session(at(date(2026, 11, 27), 18, 14)).day == date(2026, 11, 25)
    assert cal.latest_completed_session(at(date(2026, 11, 27), 18, 15)).day == date(2026, 11, 27)


def test_dst_window_uses_calendar_open(env):
    activate(env, at(D1, 21, 16))  # signal from D1 -> execute Monday D2 at 13:30 UTC (EDT)
    it = intents(env)[0]
    assert it.execution_session == D2
    env["engine"].tick(at(D2, 13, 29))
    assert submits(env["broker"]) == []
    env["engine"].tick(at(D2, 13, 31))
    assert len(submits(env["broker"])) == 1


# ------------------------------------------------------------------ notifications are independent of trading
class FailingSender:
    def __init__(self):
        self.n = 0

    def send(self, *a):
        self.n += 1
        raise RuntimeError("ntfy down")


def test_notification_failure_never_retries_trades(env):
    env["settings"].ntfy_topic = __import__("pydantic").SecretStr("test-topic-x")
    with env["db"].session() as s:
        set_notify_settings(s, {"enabled": True, "quiet_start": "00:00", "quiet_end": "00:00"})
    activate(env)
    env["broker"].auto_fill_price = Decimal(400)
    env["engine"].tick(at(D1, 14, 31))
    sender = FailingSender()
    with env["db"].session() as s:
        st = dispatch_outbox(s, sender, at(D1, 14, 32))
    assert st["failed"] >= 1
    with env["db"].session() as s:
        rows = list(s.scalars(select(NotificationOutbox)))
        assert all(r.status in ("FAILED", "QUEUED", "SUPPRESSED") for r in rows)
        assert any(r.next_attempt_at > at(D1, 14, 32) for r in rows if r.status == "FAILED")
    assert len(submits(env["broker"])) == 1


def test_public_push_is_redacted_and_deduplicated(env):
    env["settings"].ntfy_topic = __import__("pydantic").SecretStr("test-topic-x")
    with env["db"].session() as s:
        set_notify_settings(s, {"enabled": True})
        emit(s, "order_filled", "info", "Fill confirmed", "BUY 0.625 SPY at 400; equity 1000 USD", "k1", settings=env["settings"])
        emit(s, "order_filled", "info", "Fill confirmed", "BUY 0.625 SPY at 400; equity 1000 USD", "k1", settings=env["settings"])
        rows = list(s.scalars(select(NotificationOutbox)))
    assert len(rows) == 1
    assert "0.625" not in rows[0].message and "1000" not in rows[0].message and "USD" not in rows[0].message


# ------------------------------------------------------------------ review regressions (adversarial pass)
def test_drawdown_warning_has_hysteresis():
    from orbit.risk import DrawdownState, RiskPolicy, update_drawdown
    st, pol = DrawdownState(), RiskPolicy()
    evs = [e for eq in [1000, 949, 960, 949, 960, 949] for e in update_drawdown(st, Decimal(eq), pol)]
    assert evs == ["WARN"]  # hovering near 5% warns once
    update_drawdown(st, Decimal(990), pol)  # recovers below 2.5%
    assert update_drawdown(st, Decimal(949), pol) == ["WARN"]


def test_live_drawdown_latches_entries_and_override_is_respected(env):
    from orbit.db import EquityMark
    activate(env)
    b, eng = env["broker"], env["engine"]
    # Simulate equity falling 12% at the D1 close (no positions needed for the overlay test)
    b.cash = Decimal("1000")
    eng.tick(at(D1, 14, 29))
    with env["db"].session() as s:
        assert s.scalar(select(EquityMark).where(EquityMark.session == D0)).equity == Decimal(1000)  # marked after activation
    b.cash = Decimal("880")
    eng.tick(at(D1, 21, 16))
    assert ctl(env).risk_paused_entries
    with env["db"].session() as s:
        with pytest.raises(controls.ControlError):
            controls.resume_risk(s, at(D1, 22, 0), "reviewed the drawdown", override=False)
        controls.resume_risk(s, at(D1, 22, 0), "reviewed the drawdown carefully", override=True)
    assert not ctl(env).risk_paused_entries and ctl(env).risk_override_reason
    eng.tick(at(D2, 20, 20))  # next close still at -12%: override respected, no re-latch
    assert not ctl(env).risk_paused_entries


def test_sample_or_csv_data_never_creates_orders(env):
    activate(env)
    env["data"].mode = "SAMPLE"
    env["broker"].auto_fill_price = Decimal(400)
    env["engine"].tick(at(D1, 14, 31))  # onboarding intent (from fetched data) fills
    env["data"].trend = "down"
    env["engine"].tick(at(D1, 21, 16))
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.session == D1))
    assert d.action == "SKIP" and "can never create orders" in d.reason
    assert len(intents(env)) == 1


def test_review_onboarding_refuses_stale_decision(env):
    env["engine"].tick(EVAL)  # decision for D0 exists
    # Worker was off during D1; owner tries to activate the next evening
    with env["db"].session() as s:
        from orbit.db import ReconciliationReport
        r = s.scalar(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()))
        r.at = at(D1, 21, 20)
        with pytest.raises(controls.ControlError, match="latest completed session"):
            controls._entry_from_decision(s, s.scalar(select(Decision)), at(D1, 21, 20), env["cal"], "test")


def test_review_broker_hiccup_defers_evaluation_instead_of_final_skip(env):
    activate(env)
    b, eng = env["broker"], env["engine"]
    b.auto_fill_price = Decimal(400)
    eng.tick(at(D1, 14, 31))
    b.fail_all_reads = True
    eng.tick(at(D1, 21, 16))
    with env["db"].session() as s:
        assert s.scalar(select(Decision).where(Decision.session == D1)) is None  # deferred, not SKIP
    b.fail_all_reads = False
    eng.tick(at(D1, 21, 40))
    with env["db"].session() as s:
        assert s.scalar(select(Decision).where(Decision.session == D1)).action == "HOLD"


def test_review_cash_activity_failure_is_not_fatal(env):
    activate(env)
    b = env["broker"]
    b.get_cash_activities = lambda after=None: (_ for _ in ()).throw(__import__("orbit.broker.base", fromlist=["x"]).BrokerUnavailableError("403"))
    b.auto_fill_price = Decimal(400)
    env["engine"].tick(at(D1, 14, 31))
    assert intents(env)[0].state == "FILLED"  # reconciliation still worked
    assert not ctl(env).performance_valid and "Cash activity could not be read" in ctl(env).performance_invalid_reason


def test_review_live_cost_basis_is_fifo_like_backtest(env):
    activate(env)
    b, eng = env["broker"], env["engine"]
    eng.tick(at(D1, 14, 31))
    oid = intents(env)[0].broker_order_id
    b.fill(oid, Decimal("0.3"), Decimal("400"))
    eng.tick(at(D1, 14, 32))
    b.fill(oid, Decimal("0.3"), Decimal("410"))
    b.set_status(oid, "canceled")
    eng.tick(at(D1, 14, 33))
    with env["db"].session() as s:
        from orbit.engine import strategy_ledger
        pos = position(s, intents(env)[0].account_key)
        assert pos.held_qty == Decimal("0.6") and pos.cost_basis == Decimal("243.0")
        led = strategy_ledger(s, pos.account_key)
        led.sell(Decimal("0.3"), Decimal("420"))
        assert led.cost_basis == Decimal("123.0")  # FIFO relieved the 400 lot first


def test_review_manual_close_pauses_rebuy(env):
    activate(env)
    b, eng = env["broker"], env["engine"]
    b.auto_fill_price = Decimal(400)
    eng.tick(at(D1, 14, 31))
    eng.tick(at(D1, 16, 0))
    with env["db"].session() as s:
        controls.close_position(s, at(D1, 16, 0), env["cal"])
    eng.tick(at(D1, 16, 1))
    assert ctl(env).new_orders_paused
    eng.tick(at(D1, 21, 16))  # signal still INVESTED, flat -> must not create a re-entry
    with env["db"].session() as s:
        d = s.scalar(select(Decision).where(Decision.session == D1))
    assert d.action == "SKIP" and "paused by owner" in d.reason


def test_review_settings_change_does_not_block_manual_close(env):
    activate(env)
    b, eng = env["broker"], env["engine"]
    b.auto_fill_price = Decimal(400)
    eng.tick(at(D1, 14, 31))
    eng.tick(at(D1, 16, 0))
    b.auto_fill_price = None
    with env["db"].session() as s:
        iid = controls.close_position(s, at(D1, 16, 0), env["cal"])
        controls.update_settings(s, {"cash_reserve_usd": "20"}, "test change")
    eng.tick(at(D1, 16, 1))
    it = [i for i in intents(env) if i.id == iid][0]
    assert it.state == "SUBMITTED"


def test_risk_pause_still_allows_scheduled_exit(env):
    activate(env)
    b, eng, data = env["broker"], env["engine"], env["data"]
    b.auto_fill_price = Decimal(400)
    eng.tick(at(D1, 14, 31))
    with env["db"].session() as s:
        c = control(s)
        c.risk_paused_entries, c.risk_pause_reason = True, "test latch"
    data.trend = "down"
    eng.tick(at(D1, 21, 16))
    ex = intents(env)[1]
    assert ex.kind == "EXIT" and ex.state == "PENDING"
    eng.tick(at(D2, 13, 31))
    assert intents(env)[1].state == "FILLED" and "SPY" not in b.positions


def test_deposit_invalidates_performance_and_pauses_entries(env):
    from orbit.broker.base import CashActivity
    activate(env)
    env["broker"].cash_activities.append(CashActivity("act-1", "CSD", D1, Decimal("500")))
    env["broker"].cash += Decimal(500)
    env["engine"].tick(at(D1, 14, 29))
    c = ctl(env)
    assert not c.performance_valid and "CSD" in c.performance_invalid_reason
    assert c.risk_paused_entries  # drawdown math is invalid until the owner reconciles the cash flow
    env["engine"].tick(at(D1, 14, 31))
    assert intents(env)[0].state == "SKIPPED"  # the pending entry was not submitted
    with env["db"].session() as s:
        with pytest.raises(controls.ControlError):
            controls.resume_risk(s, at(D1, 15, 0), "deposit reviewed fine", override=False)
