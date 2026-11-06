"""FastAPI application: authenticated JSON API + static dashboard.

Security model (local single-user):
- Binds to 127.0.0.1 by default; remote binding refused unless explicitly configured (config.validate_startup).
- Host header must be an allowed host; state-changing requests need an allowed Origin, a valid session
  cookie AND the per-session CSRF header. GET endpoints never change trading state.
- Every endpoint except health/auth status/login requires a session.
- Sensitive actions (activation, settings, resuming pauses) also require re-entering the password.
- No WebSocket endpoints exist in v1; the dashboard polls authenticated endpoints.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select

from orbit import auth, autobackup, controls, performance
from orbit import forecasts as fc
from orbit.data.live import TIMEFRAMES as LIVE_TIMEFRAMES, LiveDataError, LiveMarketData
from orbit.backtest import CostModel, run_backtest
from orbit.config import ROOT, Settings, get_settings, mask, redact, validate_startup
from orbit.data.bars import BarSeries, DataQualityError, bars_from_json, bars_to_json, validate_bars
from orbit.data.sources import generate_sample_series, parse_csv_import
from orbit.db import (
    KV, AuditLog, AuthSession, BacktestRun, BrokerSnapshot, ControlState, Database, Dataset, Decision, EquityMark,
    EventInbox, Expense, Fill, Forecast, Job, JournalEntry, NotificationOutbox, OrderEvent, OrderIntent, ReconciliationReport,
    StrategyPosition, StrategyVersion, WorkerLease, backup_database, init_db, verify_backup,
)
from orbit.engine import OPEN_INTENT_STATES, active_version, audit, control, version_params
from orbit.learning import GLOSSARY, LESSONS, SOURCES, explain_decision
from orbit.market_calendar import DUBAI, NEW_YORK, UTC, now_utc, xnys_calendar
from orbit.money import D, fx_assumption, money_pair, usd_to_aed
from orbit.notify import get_notify_settings, queue_test_notification, set_notify_settings
from orbit.risk import RiskPolicy
from orbit.strategy import STRATEGY_LABEL, StrategyParams, sma_series

log = logging.getLogger("orbit.api")
MAX_BODY = 6 * 1024 * 1024
STATIC_DIR = ROOT / "frontend" / "dist"


# ------------------------------------------------------------------ app factory
def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    problems = validate_startup(settings)
    if problems:
        raise RuntimeError("Startup refused: " + " | ".join(problems))
    if db is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.db_path)
        init_db(db.engine, settings.data_dir / "backups", settings.db_path)
    app = FastAPI(title="Orbit Trading Lab", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.cal = xnys_calendar()
    app.state.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research")  # bounded: 1 research job at a time
    app.state.rate = defaultdict(deque)
    app.state.rate_lock = threading.Lock()
    app.state.live = LiveMarketData(settings)  # read-only market data for the dashboard chart

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in settings.allowed_hosts and not (request.url.path == "/api/health" and request.method == "GET"):
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin") or ""
            if not origin:
                ref = request.headers.get("referer") or ""
                p = urlparse(ref)
                origin = f"{p.scheme}://{p.netloc}" if p.scheme else ""
            if origin.rstrip("/") not in settings.allowed_origins:
                return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
        try:
            resp = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled: %s", redact(exc))
            resp = JSONResponse({"detail": "Internal error (details in local log, secrets redacted)."}, status_code=500)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        resp.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else resp.headers.get("Cache-Control", "no-cache")
        return resp

    _routes(app)
    _static(app)
    return app


# ------------------------------------------------------------------ deps
def S(request: Request):
    with request.app.state.db.session() as s:
        yield s


def client_id(request: Request) -> str:
    return (request.client.host if request.client else "unknown")[:64]


def require_session(request: Request):
    db = request.app.state.db
    with db.session() as s:
        row = auth.get_session(s, request.cookies.get(auth.COOKIE), now_utc())
        if row is None:
            raise HTTPException(401, "Login required")
        if request.method not in ("GET", "HEAD"):
            if not auth.check_csrf(row, request.headers.get("x-csrf-token")):
                raise HTTPException(403, "CSRF token missing or invalid")
            key = f"{row.token_hash[:12]}:{request.url.path}"
            now = time.monotonic()
            with request.app.state.rate_lock:
                dq = request.app.state.rate[key]
                while dq and now - dq[0] > 60:
                    dq.popleft()
                if len(dq) >= 30:
                    raise HTTPException(429, "Too many requests for this action; slow down.")
                dq.append(now)
        return {"token_hash": row.token_hash}


def require_password(s, password: str | None):
    if not password or not auth.verify_password(s, password):
        raise HTTPException(403, "Re-enter your password to confirm this sensitive action.")


def mp(v):
    return money_pair(v) if v is not None else None


def _iso(dt):
    return dt.isoformat() if dt else None


def _times(dt):
    if not dt:
        return None
    return {"utc": dt.isoformat(), "dubai": dt.astimezone(DUBAI).isoformat(), "new_york": dt.astimezone(NEW_YORK).isoformat()}


# ------------------------------------------------------------------ serializers
def ser_decision(d: Decision, full: bool = False) -> dict:
    out = {"id": d.id, "session": d.session.isoformat(), "sequence": d.sequence, "action": d.action, "target": d.target,
           "previous_target": d.previous_target, "comparison": d.comparison, "close": str(d.close) if d.close is not None else None,
           "sma": str(d.sma) if d.sma is not None else None, "window_count": d.window_count, "reason": d.reason,
           "mode_at_decision": d.mode_at_decision, "data_mode": d.data_mode, "data_source": d.data_source,
           "data_fingerprint": d.data_fingerprint, "known_at": _times(d.known_at), "strategy_version_id": d.strategy_version_id,
           "held_qty_at_decision": str(d.held_qty_at_decision), "close_money": mp(d.close), "sma_money": mp(d.sma)}
    if full:
        out["inputs"] = d.inputs
        out["data_fetched_at"] = _times(d.data_fetched_at)
    return out


def ser_intent(i: OrderIntent) -> dict:
    return {"id": i.id, "decision_id": i.decision_id, "parent_intent_id": i.parent_intent_id, "kind": i.kind, "side": i.side,
            "signal_session": i.signal_session.isoformat(), "execution_session": i.execution_session.isoformat(), "attempt": i.attempt,
            "client_order_id": i.client_order_id, "broker_order_id": i.broker_order_id, "broker_status": i.broker_status,
            "state": i.state, "frozen_notional": str(i.frozen_notional) if i.frozen_notional is not None else None,
            "frozen_notional_money": mp(i.frozen_notional), "submitted_notional": str(i.submitted_notional) if i.submitted_notional else None,
            "submitted_notional_money": mp(i.submitted_notional), "target_qty": str(i.target_qty) if i.target_qty is not None else None,
            "filled_qty": str(i.filled_qty), "filled_avg_price": str(i.filled_avg_price) if i.filled_avg_price else None,
            "review_required": i.review_required, "reason": i.reason, "sizing": i.sizing,
            "created_at": _times(i.created_at), "submitted_at": _times(i.submitted_at), "terminal_at": _times(i.terminal_at),
            "strategy_version_id": i.strategy_version_id}


def ser_fill(f: Fill) -> dict:
    return {"id": f.id, "intent_id": f.intent_id, "broker_order_id": f.broker_order_id, "side": f.side, "qty": str(f.qty),
            "price": str(f.price), "price_money": mp(f.price), "notional_money": mp(f.qty * f.price), "at": f.at.isoformat(), "source": f.source}


# ------------------------------------------------------------------ bodies
class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class NoteBody(PasswordBody):
    note: str = Field(default="", max_length=1000)
    override: bool = False


class SecPauseBody(BaseModel):
    note: str = Field(default="", max_length=1000)


class AckBody(BaseModel):
    intent_id: int
    create_new: bool = False
    confirm_absent_at_broker: bool = False


class SettingsBody(BaseModel):
    values: dict
    reason: str = Field(default="", max_length=500)
    password: str | None = Field(default=None, max_length=256)


class BacktestBody(BaseModel):
    dataset_id: int
    period: str = Field(default="development", pattern="^(development|validation|test_holdout|full)$")
    confirm_holdout: bool = False
    starting_cash: Decimal = Field(default=Decimal("1000"), ge=Decimal("50"), le=Decimal("1000000"))
    slippage_bps: Decimal = Field(default=Decimal("5"), ge=Decimal("0"), le=Decimal("200"))
    fee_per_order_usd: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("50"))
    params: dict = Field(default_factory=dict)
    label: str = Field(default="", max_length=120)


class JournalBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    source_url: str = Field(default="", max_length=500)
    published_at: datetime | None = None
    instrument: str = Field(default="SPY", pattern="^[A-Z]{1,6}$")
    notes: str = Field(default="", max_length=5000)


class ExpenseBody(BaseModel):
    day: date
    category: str = Field(pattern="^(data|hosting|ai|software|education|other)$")
    description: str = Field(default="", max_length=200)
    currency: str = Field(pattern="^(USD|AED)$")
    amount: Decimal = Field(gt=Decimal("0"), le=Decimal("1000000"))


class NotifyBody(BaseModel):
    enabled: bool | None = None
    include_financial_details: bool | None = None
    quiet_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    quiet_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class ForecastBody(BaseModel):
    direction: Literal["UP", "DOWN"]
    confidence: int = Field(60, ge=50, le=100)
    reason: str = Field("", max_length=2000)


class ProgressBody(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9_:\-]{1,64}$")
    value: dict


def period_bounds(bars, period: str):
    """Chronological split: development 60%, validation 20%, final holdout 20%. Warm-up comes from earlier data."""
    n = len(bars)
    a, b = int(n * 0.6), int(n * 0.8)
    if period == "development":
        return None, bars[a - 1].day
    if period == "validation":
        return bars[a].day, bars[b - 1].day
    if period == "test_holdout":
        return bars[b].day, None
    return None, None


def _series_from_dataset(ds: Dataset) -> BarSeries:
    from orbit.data.bars import SplitEvent
    desc = ds.describe or {}
    splits = [SplitEvent(date.fromisoformat(x["ex_date"]), D(x["ratio"])) for x in desc.get("splits", [])]
    return BarSeries(ds.symbol, ds.mode, ds.source, ds.price_basis, bars_from_json(ds.bars_json), splits,
                     bool(desc.get("dividends_included")), desc.get("metadata", {}))


def _run_backtest_job(db: Database, run_id: int):
    with db.session() as s:
        run = s.get(BacktestRun, run_id)
        run.status = "RUNNING"
        ds = s.get(Dataset, run.dataset_id)
        series = _series_from_dataset(ds)
        cfg = run.config
    try:
        params = StrategyParams.from_dict(cfg["params"])
        risk = RiskPolicy(D(cfg["risk"]["warn_fraction"]), D(cfg["risk"]["pause_fraction"]))
        start, end = period_bounds(series.bars, cfg["period"])
        res = run_backtest(series, params, risk, CostModel(D(cfg["slippage_bps"]), D(cfg["fee_per_order_usd"])),
                           D(cfg["starting_cash"]), start=start, end=end)
        res["fx"] = fx_assumption()
        res["period"] = cfg["period"]
        summary = {}
        if res.get("ok"):
            summary = {k: v["metrics"] for k, v in res["portfolios"].items()}
        with db.session() as s:
            run = s.get(BacktestRun, run_id)
            run.status = "DONE" if res.get("ok") else "FAILED"
            run.error = res.get("error", "")
            run.summary = summary
            run.result_json = json.dumps(res, default=str)
            run.finished_at = now_utc()
    except Exception as exc:  # noqa: BLE001
        with db.session() as s:
            run = s.get(BacktestRun, run_id)
            run.status, run.error, run.finished_at = "FAILED", redact(exc)[:500], now_utc()


def _worker_status(s) -> dict:
    lease = s.get(WorkerLease, "trader")
    now = now_utc()
    if not lease:
        return {"state": "NEVER_STARTED", "text": "Worker has not run yet."}
    age = (now - lease.heartbeat_at).total_seconds()
    state = "RUNNING" if lease.status == "running" and age < 120 else ("STOPPED" if lease.status == "stopped" else "STALE")
    return {"state": state, "heartbeat": _times(lease.heartbeat_at), "age_seconds": int(age), "owner": lease.owner.split(":")[0],
            "started": _times(lease.started_at),
            "text": {"RUNNING": "Worker heartbeat is fresh.", "STOPPED": "Worker was stopped cleanly.",
                     "STALE": "No heartbeat for over 2 minutes: the worker may have crashed or the laptop slept."}[state]}


def _market(cal, now):
    today = now.astimezone(NEW_YORK).date()
    cur = cal.session_containing(now)
    nxt = cal.next_session(today) if not (cal.session(today) and now < cal.session(today).open_utc) else cal.session(today)
    last = cal.latest_completed_session(now, 15)
    return {"is_open_by_calendar": cur is not None, "current": cur.to_dict() if cur else None,
            "next": nxt.to_dict() if nxt else None, "latest_completed": last.to_dict() if last else None,
            "calendar_source": cal.source, "now": _times(now),
            "next_evaluation": _times((cur or nxt).close_utc + timedelta(minutes=15)) if (cur or nxt) else None}


def _routes(app: FastAPI):
    st = app.state

    # -------------------------------------------------------------- public
    @app.get("/api/health")
    def health(s=Depends(S)):
        w = _worker_status(s)
        return {"ok": True, "api": "up", "worker": w["state"], "time": now_utc().isoformat()}

    @app.get("/api/auth/status")
    def auth_status(request: Request, s=Depends(S)):
        row = auth.get_session(s, request.cookies.get(auth.COOKIE), now_utc())
        return {"password_set": auth.password_is_set(s), "authenticated": row is not None,
                "csrf_token": row.csrf_token if row else None}

    @app.post("/api/auth/login")
    def do_login(body: LoginBody, request: Request, response: Response, s=Depends(S)):
        try:
            token, csrf = auth.login(s, body.password, client_id(request), now_utc(), st.settings.session_hours)
        except auth.AuthError as e:
            s.add(AuditLog(actor="anonymous", action="login_failed", severity="warning", details={"client": client_id(request), "reason": str(e)}))
            s.commit()
            raise HTTPException(e.status, str(e))
        audit(s, "user", "login", {"client": client_id(request)})
        response.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", secure=st.settings.deployment_mode == "remote",
                            max_age=st.settings.session_hours * 3600, path="/")
        return {"ok": True, "csrf_token": csrf}

    @app.post("/api/auth/logout")
    def do_logout(request: Request, response: Response, s=Depends(S), _=Depends(require_session)):
        auth.logout(s, request.cookies.get(auth.COOKIE), now_utc())
        audit(s, "user", "logout", {})
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    # -------------------------------------------------------------- overview
    @app.get("/api/overview")
    def overview(_=Depends(require_session), s=Depends(S)):
        now = now_utc()
        c = control(s)
        snap = s.scalar(select(BrokerSnapshot).where(BrokerSnapshot.ok.is_(True)).order_by(BrokerSnapshot.id.desc()))
        last_any = s.scalar(select(BrokerSnapshot).order_by(BrokerSnapshot.id.desc()))
        rep = s.scalar(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()))
        dec = s.scalar(select(Decision).order_by(Decision.session.desc(), Decision.sequence.desc()))
        pos = s.scalar(select(StrategyPosition).where(StrategyPosition.account_key == c.account_fingerprint)) if c.account_fingerprint else None
        oi = list(s.scalars(select(OrderIntent).where(OrderIntent.state.in_(OPEN_INTENT_STATES)).order_by(OrderIntent.id)))
        review = list(s.scalars(select(OrderIntent).where(OrderIntent.review_required.is_(True))))
        marks = list(s.scalars(select(EquityMark).where(EquityMark.account_key == c.account_fingerprint).order_by(EquityMark.session))) if c.account_fingerprint else []
        incidents = []
        if c.security_paused:
            incidents.append({"kind": "SECURITY_PAUSED", "severity": "critical", "text": c.security_pause_reason})
        if c.risk_paused_entries:
            incidents.append({"kind": "RISK_PAUSED_ENTRIES", "severity": "warning", "text": c.risk_pause_reason})
        for i in review:
            incidents.append({"kind": "REVIEW_REQUIRED", "severity": "critical", "text": f"Intent #{i.id} {i.kind} {i.side}: {i.state}. {i.reason}", "intent_id": i.id})
        if not c.performance_valid:
            incidents.append({"kind": "PERFORMANCE_INVALID", "severity": "warning", "text": c.performance_invalid_reason})
        w = _worker_status(s)
        if w["state"] == "STALE":
            incidents.append({"kind": "WORKER_STALE", "severity": "critical", "text": w["text"]})
        elif w["state"] in ("STOPPED", "NEVER_STARTED"):
            incidents.append({"kind": "WORKER_NOT_RUNNING", "severity": "critical" if c.mode == "PAPER_AUTO" else "warning",
                              "text": w["text"] + " No decisions are recorded and no orders are managed until it runs."})
        if last_any and not last_any.ok:
            incidents.append({"kind": "BROKER_UNREACHABLE", "severity": "warning", "text": last_any.error})
        acct = snap.account if snap else None
        account = None
        if acct:
            account = {"cash": mp(D(acct["cash"])), "equity": mp(D(acct["equity"])), "buying_power": mp(D(acct["buying_power"])),
                       "status": acct["status"], "masked": acct["account_number_masked"], "as_of": _times(snap.taken_at),
                       "multiplier": acct.get("multiplier"),
                       "buying_power_note": "Buying power can include margin. Orbit only ever spends unborrowed cash."}
        p_open = sum((i.frozen_notional or Decimal(0)) for i in oi if i.side == "BUY")
        v = active_version(s)
        params, risk = version_params(v)
        return {
            "mode": c.mode, "environment": "PAPER", "flags": {
                "new_orders_paused": c.new_orders_paused, "risk_paused_entries": c.risk_paused_entries,
                "security_paused": c.security_paused, "review_required": c.review_required, "performance_valid": c.performance_valid},
            "activated_at": _times(c.activated_at),
            "broker_configured": st.settings.broker_configured,
            "worker": w, "market": _market(st.cal, now),
            "account": account,
            "positions": snap.positions if snap else [],
            "strategy_position": {"held_qty": str(pos.held_qty), "cost_basis": mp(pos.cost_basis), "signal_target": pos.signal_target,
                                  "exposure_note": pos.exposure_note} if pos else None,
            "open_intents": [ser_intent(i) for i in oi], "pending_buy_notional": mp(p_open),
            "last_decision": ser_decision(dec) if dec else None,
            "reconciliation": {"ok": rep.ok, "at": _times(rep.at), "discrepancies": rep.discrepancies} if rep else None,
            "incidents": incidents,
            "drawdown": {"marks": [{"session": m.session.isoformat(), "equity": str(m.equity), "hwm": str(m.high_water_mark), "drawdown": str(m.drawdown)} for m in marks[-60:]],
                         "warn": str(risk.warn_fraction), "pause": str(risk.pause_fraction),
                         "note": "Thresholds trigger reviews; they are not loss limits. With ~25% exposure, a 10% account drawdown needs roughly a 40% SPY fall, so they may rarely trigger."},
            "fx": fx_assumption(),
            "health_vs_profit": "Healthy software (green status) and a profitable strategy are separate measures. Check both.",
        }

    # -------------------------------------------------------------- market & strategy
    @app.get("/api/market/chart")
    def chart(source: str = "auto", _=Depends(require_session), s=Depends(S)):
        if source == "auto":
            ds = s.scalar(select(Dataset).where(Dataset.mode == "ALPACA").order_by(Dataset.id.desc())) or \
                s.scalar(select(Dataset).order_by(Dataset.id.desc()))
        elif source.startswith("dataset:") and source[8:].isdigit():
            ds = s.get(Dataset, int(source[8:]))
        else:
            raise HTTPException(400, "bad source")
        if not ds:
            return {"available": False, "reason": "No price data stored yet. Create a SAMPLE dataset on the Research page, import a CSV, or connect Alpaca and let the worker evaluate."}
        series = _series_from_dataset(ds)
        closes = [(b.day, b.close) for b in series.bars]
        sma = dict(sma_series(closes, 200))
        bars = [{"day": b.day.isoformat(), "open": str(b.open), "high": str(b.high), "low": str(b.low), "close": str(b.close),
                 "sma200": str(sma[b.day]) if sma.get(b.day) is not None else None} for b in series.bars]
        decisions, fills = [], []
        if ds.mode == "ALPACA":
            decisions = [ser_decision(d) for d in s.scalars(select(Decision).where(Decision.data_mode == "ALPACA").order_by(Decision.session))]
            fills = [ser_fill(f) for f in s.scalars(select(Fill).order_by(Fill.at))]
        quality = validate_bars(series.bars, st.cal)
        return {"available": True, "dataset": {"id": ds.id, **(ds.describe or {})}, "bars": bars, "decisions": decisions, "fills": fills,
                "missing_sessions": quality["missing_sessions"][:200]}

    @app.get("/api/performance")
    def perf(_=Depends(require_session), s=Depends(S)):
        c = control(s)
        ds = s.scalar(select(Dataset).where(Dataset.mode == "ALPACA", Dataset.symbol == "SPY").order_by(Dataset.id.desc()))
        closes = [(b.day, b.close) for b in _series_from_dataset(ds).bars] if ds else []
        out = performance.for_account(s, c.account_fingerprint, "SPY", closes)
        out["fx"] = fx_assumption()
        out["usd_to_aed"] = str(st.settings.usd_to_aed)
        return out

    @app.get("/api/market/live")
    def market_live(tf: str = "5Min", _=Depends(require_session)):
        """Recent candles + latest trade for SPY. Display only; never used for decisions; no trading client."""
        if tf not in LIVE_TIMEFRAMES:
            raise HTTPException(400, "bad timeframe")
        now = now_utc()
        try:
            data = st.live.candles("SPY", tf, now)
        except LiveDataError as e:
            return {"available": False, "reason": str(e), "market": _market(st.cal, now)}
        return {"available": True, **data, "market": _market(st.cal, now)}

    @app.get("/api/news")
    def news(_=Depends(require_session)):
        """Latest SPY/market headlines. Display only; never used for decisions."""
        try:
            return {"available": True, **st.live.news("SPY", 30)}
        except LiveDataError as e:
            return {"available": False, "reason": str(e), "items": []}

    def _latest_closes(s) -> dict:
        ds = s.scalar(select(Dataset).where(Dataset.mode == "ALPACA").order_by(Dataset.id.desc()))
        return {b.day: b.close for b in _series_from_dataset(ds).bars} if ds else {}

    def ser_forecast(f: Forecast) -> dict:
        return {"id": f.id, "target_session": f.target_session.isoformat(), "base_session": f.base_session.isoformat(),
                "direction": f.direction, "confidence": f.confidence, "reason": f.reason, "created_at": _times(f.created_at),
                "base_close": str(f.base_close) if f.base_close is not None else None,
                "outcome_close": str(f.outcome_close) if f.outcome_close is not None else None,
                "outcome": f.outcome, "correct": f.correct}

    @app.get("/api/forecasts")
    def forecasts(_=Depends(require_session), s=Depends(S)):
        now = now_utc()
        rows = list(s.scalars(select(Forecast).order_by(Forecast.target_session.desc())))
        if any(f.outcome is None for f in rows):
            fc.score_pending(rows, _latest_closes(s), now)
        target, base = fc.target_for(st.cal, now)
        current = next((f for f in rows if f.target_session == target.day), None)
        return {"next": {"target_session": target.day.isoformat(), "base_session": base.day.isoformat(),
                         "deadline": _times(target.open_utc), "already_made": ser_forecast(current) if current else None},
                "items": [ser_forecast(f) for f in rows[:200]], "score": fc.scorecard(rows),
                "rules": ["Make your call before the target session opens (18:30 Dubai in winter, 17:30 in summer). Later calls are refused, so there is no hindsight.",
                          "One call per session. UP means the session will close higher than the previous session's close.",
                          "Scoring uses Orbit's stored Alpaca daily closes, after the worker records that session.",
                          "Your hit rate is compared with simply saying 'UP' every day, which is right more often than not in a rising market.",
                          "Learning only: forecasts never affect trading."]}

    @app.post("/api/forecasts")
    def forecast_add(body: ForecastBody, _=Depends(require_session), s=Depends(S)):
        now = now_utc()
        target, base = fc.target_for(st.cal, now)
        if now >= target.open_utc:
            raise HTTPException(409, "That session has already opened; forecasts must be made before the open.")
        if s.scalar(select(Forecast.id).where(Forecast.target_session == target.day)):
            raise HTTPException(409, f"You already made a call for {target.day.isoformat()}. One per session, and it cannot be changed.")
        f = Forecast(symbol="SPY", target_session=target.day, base_session=base.day, direction=body.direction,
                     confidence=body.confidence, reason=body.reason.strip(), created_at=now)
        s.add(f)
        s.flush()
        audit(s, "user", "forecast_recorded", {"id": f.id, "target_session": target.day.isoformat(), "direction": body.direction})
        return ser_forecast(f)

    @app.get("/api/strategy")
    def strategy(_=Depends(require_session), s=Depends(S)):
        v = active_version(s)
        p, r = version_params(v)
        return {
            "label": STRATEGY_LABEL,
            "rule_plain_english": [
                "Once per completed regular session, 15 minutes after the close, take SPY's close and the average of the last 200 session closes (including that close).",
                "Close strictly above the average -> target INVESTED. Strictly below -> target CASH. Exactly equal -> keep the previous target (start: CASH).",
                "Act at the next regular session: submit a DAY market order only after the broker confirms the market is open, within the first 5 minutes.",
                "Entry from flat: buy min(25% of equity, USD 250 / AED 918.13, cash after a USD 10 / AED 36.73 reserve). Hold without resizing while invested.",
                "Exit: sell only the strategy's own reconciled SPY shares. No margin, shorting, leverage, derivatives or extended hours.",
                "Missed windows, rejections and ambiguous orders stop and ask for your review. No catch-up orders."],
            "params": p.to_dict(),
            "params_money": {"max_entry_notional": mp(p.max_entry_notional_usd), "cash_reserve": mp(p.cash_reserve_usd), "min_order": mp(p.min_order_notional_usd)},
            "risk_policy": v.risk_policy,
            "active_version": {"id": v.id, "created_at": _times(v.created_at), "reason": v.reason, "hash": v.content_hash},
            "versions": [{"id": x.id, "created_at": _times(x.created_at), "reason": x.reason, "params": x.params, "risk_policy": x.risk_policy,
                          "active": x.active, "hash": x.content_hash} for x in s.scalars(select(StrategyVersion).order_by(StrategyVersion.id.desc()))],
            "control_descriptions": controls.CONTROL_DESCRIPTIONS,
        }

    @app.post("/api/strategy/preview")
    def strategy_preview(body: SettingsBody, _=Depends(require_session), s=Depends(S)):
        try:
            return controls.preview_settings(s, body.values)
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post("/api/strategy/update")
    def strategy_update(body: SettingsBody, _=Depends(require_session), s=Depends(S)):
        require_password(s, body.password)
        try:
            return controls.update_settings(s, body.values, body.reason)
        except (ValueError, TypeError, controls.ControlError) as e:
            raise HTTPException(422, str(e))

    # -------------------------------------------------------------- decisions & orders
    @app.get("/api/decisions")
    def decisions(q: str = "", action: str = "", page: int = 1, _=Depends(require_session), s=Depends(S)):
        stmt = select(Decision)
        if action:
            stmt = stmt.where(Decision.action == action.upper()[:8])
        if q:
            like = f"%{q[:50]}%"
            stmt = stmt.where(or_(Decision.reason.like(like), Decision.target.like(like)))
        page = max(1, min(page, 1000))
        rows = s.scalars(stmt.order_by(Decision.session.desc(), Decision.sequence.desc()).offset((page - 1) * 50).limit(50))
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        return {"items": [ser_decision(d) for d in rows], "total": total, "page": page}

    @app.get("/api/decisions/{did}")
    def decision_detail(did: int, _=Depends(require_session), s=Depends(S)):
        d = s.get(Decision, did)
        if not d:
            raise HTTPException(404, "not found")
        its = list(s.scalars(select(OrderIntent).where(OrderIntent.decision_id == d.id).order_by(OrderIntent.id)))
        ids = [i.id for i in its]
        children = list(s.scalars(select(OrderIntent).where(OrderIntent.parent_intent_id.in_(ids)))) if ids else []
        its_all = its + [c for c in children if c.id not in ids]
        ids_all = [i.id for i in its_all]
        fills = list(s.scalars(select(Fill).where(Fill.intent_id.in_(ids_all)).order_by(Fill.at))) if ids_all else []
        events = list(s.scalars(select(OrderEvent).where(OrderEvent.intent_id.in_(ids_all)).order_by(OrderEvent.id))) if ids_all else []
        dd = ser_decision(d, full=True)
        ii = [ser_intent(i) for i in its_all]
        ff = [ser_fill(f) for f in fills]
        timeline = [{"at": _times(d.known_at), "kind": "DECISION", "text": f"{d.action} decided (target {d.target})"}]
        for e in events:
            timeline.append({"at": _times(e.at), "kind": e.kind, "text": f"Intent #{e.intent_id}: {e.from_state or 'new'} -> {e.to_state}. {e.detail.get('reason') or e.detail.get('note') or ''}"})
        for f in fills:
            timeline.append({"at": _times(f.at), "kind": "FILL", "text": f"{f.side} {f.qty.normalize()} @ {f.price:.4f} USD (broker order {f.broker_order_id})"})
        timeline.sort(key=lambda x: x["at"]["utc"] if x["at"] else "")
        return {"decision": dd, "intents": ii, "fills": ff, "timeline": timeline, "explanation": explain_decision(dd, ii, ff)}

    @app.get("/api/orders")
    def orders(_=Depends(require_session), s=Depends(S)):
        its = list(s.scalars(select(OrderIntent).order_by(OrderIntent.id.desc()).limit(200)))
        return {"intents": [ser_intent(i) for i in its],
                "fills": [ser_fill(f) for f in s.scalars(select(Fill).order_by(Fill.at.desc()).limit(200))]}

    # -------------------------------------------------------------- controls
    def _done(s, msg, **kw):
        return {"ok": True, "message": msg, **kw}

    @app.post("/api/controls/observe")
    def c_observe(_=Depends(require_session), s=Depends(S)):
        controls.set_observe(s)
        return _done(s, "OBSERVE mode. No new intents will be created.")

    @app.post("/api/controls/activate")
    def c_activate(body: PasswordBody, _=Depends(require_session), s=Depends(S)):
        require_password(s, body.password)
        try:
            return {"ok": True, **controls.activate(s, now_utc(), st.cal, st.settings.broker_configured)}
        except controls.ControlError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/controls/readiness")
    def c_ready(_=Depends(require_session), s=Depends(S)):
        return controls.readiness(s, now_utc(), st.settings.broker_configured)

    @app.post("/api/controls/pause")
    def c_pause(_=Depends(require_session), s=Depends(S)):
        controls.pause_new_orders(s, True)
        return _done(s, "New orders paused. Existing orders/positions are unchanged; see Orders.")

    @app.post("/api/controls/resume")
    def c_resume(_=Depends(require_session), s=Depends(S)):
        controls.pause_new_orders(s, False)
        return _done(s, "Manual pause lifted. Other pauses still apply.")

    @app.post("/api/controls/cancel-pending")
    def c_cancel(_=Depends(require_session), s=Depends(S)):
        j = Job(job_type="owner_cancel", job_key=f"cancel:{now_utc().isoformat()}", status="QUEUED")
        s.add(j)
        audit(s, "user", "cancel_pending_requested", {})
        return _done(s, "Cancellation queued for the worker's next cycle. Watch Orders for confirmed terminal status.")

    @app.post("/api/controls/close-position")
    def c_close(body: PasswordBody, _=Depends(require_session), s=Depends(S)):
        require_password(s, body.password)
        try:
            iid = controls.close_position(s, now_utc(), st.cal)
        except controls.ControlError as e:
            raise HTTPException(409, str(e))
        return _done(s, "Close intent created; the worker submits it in the next eligible window.", intent_id=iid)

    @app.post("/api/controls/ack-review")
    def c_ack(body: AckBody, _=Depends(require_session), s=Depends(S)):
        broker = None
        if body.confirm_absent_at_broker and st.settings.broker_configured:
            from orbit.broker.alpaca_adapter import AlpacaPaperAdapter
            broker = AlpacaPaperAdapter(st.settings)
        try:
            return {"ok": True, **controls.acknowledge_review(s, body.intent_id, now_utc(), st.cal, body.create_new, body.confirm_absent_at_broker, broker)}
        except controls.ControlError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/controls/security-pause")
    def c_secpause(body: SecPauseBody, _=Depends(require_session), s=Depends(S)):
        c = control(s)
        c.security_paused = True
        c.security_pause_reason = f"Owner incident pause: {body.note[:300] or 'no note'}"
        audit(s, "user", "security_paused_by_owner", {"note": body.note}, "critical")
        return _done(s, "SECURITY_PAUSED. Automated submissions blocked. Follow the incident checklist on the Security page.")

    @app.post("/api/controls/resume-security")
    def c_secresume(body: NoteBody, _=Depends(require_session), s=Depends(S)):
        require_password(s, body.password)
        try:
            controls.resume_security(s, now_utc(), body.note)
        except controls.ControlError as e:
            raise HTTPException(409, str(e))
        return _done(s, "Security pause lifted.")

    @app.post("/api/controls/resume-risk")
    def c_riskresume(body: NoteBody, _=Depends(require_session), s=Depends(S)):
        require_password(s, body.password)
        try:
            controls.resume_risk(s, now_utc(), body.note, body.override)
        except controls.ControlError as e:
            raise HTTPException(409, str(e))
        return _done(s, "Entries resumed after review.")

    @app.post("/api/controls/cash-reviewed")
    def c_cash(body: NoteBody, _=Depends(require_session), s=Depends(S)):
        require_password(s, body.password)
        try:
            controls.mark_cash_flows_reviewed(s, body.note)
        except controls.ControlError as e:
            raise HTTPException(409, str(e))
        return _done(s, "Cash activity marked reviewed. The high-water mark was not reset.")

    # -------------------------------------------------------------- research
    @app.get("/api/datasets")
    def datasets(_=Depends(require_session), s=Depends(S)):
        rows = s.scalars(select(Dataset).order_by(Dataset.id.desc()).limit(50))
        return {"items": [{"id": d.id, "mode": d.mode, "source": d.source, "price_basis": d.price_basis, "created_at": _times(d.created_at),
                           "describe": d.describe, "quality": d.quality} for d in rows]}

    def _save_dataset(s, series: BarSeries) -> Dataset:
        q = validate_bars(series.bars, st.cal)
        fp = series.fingerprint()
        ex = s.scalar(select(Dataset).where(Dataset.fingerprint == fp))
        if ex:
            return ex
        ds = Dataset(mode=series.mode, symbol=series.symbol, source=series.source, price_basis=series.price_basis, fingerprint=fp,
                     bars_json=bars_to_json(series.bars), describe=series.describe(), quality={"missing_sessions": q["missing_sessions"][:500], "issues": q["issues"]})
        s.add(ds)
        s.flush()
        return ds

    @app.post("/api/datasets/sample")
    def ds_sample(_=Depends(require_session), s=Depends(S)):
        ds = _save_dataset(s, generate_sample_series())
        return {"ok": True, "dataset_id": ds.id, "mode": "SAMPLE"}

    @app.post("/api/datasets/import")
    async def ds_import(request: Request, file: UploadFile = File(...), price_basis: str = Form(...), source_description: str = Form(...),
                        _=Depends(require_session)):
        raw = await file.read(5 * 1024 * 1024 + 1)
        try:
            series = parse_csv_import(raw, "SPY", price_basis, source_description)
        except DataQualityError as e:
            raise HTTPException(422, str(e))
        with request.app.state.db.session() as s:
            ds = _save_dataset(s, series)
            audit(s, "user", "dataset_imported", {"dataset_id": ds.id, "rows": len(series.bars)})
            return {"ok": True, "dataset_id": ds.id, "rows": len(series.bars)}

    @app.post("/api/datasets/fetch-alpaca")
    def ds_fetch(_=Depends(require_session), s=Depends(S)):
        if not st.settings.broker_configured:
            raise HTTPException(409, "Alpaca credentials are not configured; history cannot be fetched. Use SAMPLE or CSV import.")
        from orbit.broker.alpaca_adapter import AlpacaPaperAdapter
        from orbit.broker.base import BrokerError
        ad = AlpacaPaperAdapter(st.settings)
        now = now_utc()
        try:
            series = ad.fetch_daily_bars("SPY", date(2016, 1, 1), now.date(), now, st.cal, 15)
        except BrokerError as e:
            raise HTTPException(502, f"Alpaca data request failed: {redact(e)}")
        if not series.bars:
            raise HTTPException(502, "Alpaca returned no bars (check data entitlement/feed).")
        ds = _save_dataset(s, series)
        return {"ok": True, "dataset_id": ds.id, "first": series.bars[0].day.isoformat(), "last": series.bars[-1].day.isoformat(), "bars": len(series.bars)}

    @app.post("/api/backtests")
    def bt_create(body: BacktestBody, _=Depends(require_session), s=Depends(S)):
        ds = s.get(Dataset, body.dataset_id)
        if not ds:
            raise HTTPException(404, "dataset not found")
        if body.period == "test_holdout" and not body.confirm_holdout:
            raise HTTPException(409, "The final holdout should be used rarely and only after settings are fixed. Confirm to run it; the attempt is recorded.")
        running = s.scalar(select(func.count()).select_from(BacktestRun).where(BacktestRun.status.in_(["QUEUED", "RUNNING"])))
        if running >= 3:
            raise HTTPException(429, "Research queue is full (max 3). Wait for runs to finish.")
        v = active_version(s)
        p, r = version_params(v)
        try:
            params = StrategyParams.from_dict({**p.to_dict(), **{k: x for k, x in body.params.items() if k != "symbol"}})
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))
        cfg = {"params": params.to_dict(), "risk": r.to_dict(), "period": body.period, "starting_cash": str(body.starting_cash),
               "slippage_bps": str(body.slippage_bps), "fee_per_order_usd": str(body.fee_per_order_usd), "label": body.label,
               "strategy_version_id": v.id, "fx": fx_assumption()}
        run = BacktestRun(dataset_id=ds.id, data_mode=ds.mode, period=body.period, config=cfg, status="QUEUED")
        s.add(run)
        s.flush()
        rid = run.id
        audit(s, "user", "backtest_requested", {"run_id": rid, "config": cfg})
        s.commit()
        st.pool.submit(_run_backtest_job, st.db, rid)
        return {"ok": True, "run_id": rid}

    @app.get("/api/backtests")
    def bt_list(_=Depends(require_session), s=Depends(S)):
        rows = s.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(100))
        return {"items": [{"id": r.id, "status": r.status, "data_mode": r.data_mode, "period": r.period, "created_at": _times(r.created_at),
                           "config": r.config, "summary": r.summary, "error": r.error} for r in rows]}

    @app.get("/api/backtests/{rid}")
    def bt_get(rid: int, _=Depends(require_session), s=Depends(S)):
        r = s.get(BacktestRun, rid)
        if not r:
            raise HTTPException(404, "not found")
        return {"id": r.id, "status": r.status, "data_mode": r.data_mode, "period": r.period, "config": r.config,
                "error": r.error, "result": json.loads(r.result_json) if r.result_json else None}

    @app.get("/api/backtests/{rid}/export")
    def bt_export(rid: int, fmt: str = "json", _=Depends(require_session), s=Depends(S)):
        r = s.get(BacktestRun, rid)
        if not r or not r.result_json:
            raise HTTPException(404, "not found")
        res = json.loads(r.result_json)
        name = f"orbit-backtest-{rid}-{r.data_mode}"
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow([f"# {r.data_mode} backtest {rid}. Assumptions: " + " | ".join(res.get("assumptions", []))])
            w.writerow([f"# FX reference: USD/AED {res.get('fx', {}).get('rate')} ({res.get('fx', {}).get('source')})"])
            w.writerow(["portfolio", "day", "side", "qty", "price_usd", "price_aed", "fee_usd", "notional_usd", "reason"])
            for name_, pf in res.get("portfolios", {}).items():
                for t in pf["trades"]:
                    w.writerow([name_, t["day"], t["side"], t["qty"], t["price"], f"{usd_to_aed(D(t['price'])):.2f}", t["fee"], t["notional"], t["reason"]])
            return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
        return JSONResponse(res, headers={"Content-Disposition": f'attachment; filename="{name}.json"'})

    # -------------------------------------------------------------- learning
    @app.get("/api/learn")
    def learn(_=Depends(require_session), s=Depends(S)):
        row = s.get(KV, "learning_progress")
        return {"lessons": LESSONS, "glossary": [{"id": a, "term": b, "text": c, "source": SOURCES.get(d) if d else None} for a, b, c, d in GLOSSARY],
                "sources": SOURCES, "progress": row.value if row else {}}

    @app.post("/api/learn/progress")
    def learn_progress(body: ProgressBody, _=Depends(require_session), s=Depends(S)):
        row = s.get(KV, "learning_progress")
        cur = dict(row.value) if row else {}
        cur[body.key] = {**body.value, "at": now_utc().isoformat()}
        if len(json.dumps(cur)) > 200_000:
            raise HTTPException(413, "progress too large")
        if row:
            row.value = cur
        else:
            s.add(KV(key="learning_progress", value=cur))
        return {"ok": True}

    @app.get("/api/learn/replay/{rid}")
    def learn_replay(rid: int, _=Depends(require_session), s=Depends(S)):
        r = s.get(BacktestRun, rid)
        if not r or not r.result_json:
            raise HTTPException(404, "Run a backtest first.")
        res = json.loads(r.result_json)
        decs = res.get("decisions", [])
        # pick interesting steps: every trade decision plus a few holds around them
        steps = []
        for i, d in enumerate(decs):
            if d.get("action") in ("BUY", "SELL") or (i % 40 == 0):
                prev = decs[max(0, i - 30): i + 1]
                steps.append({"index": i, "session": d["session"], "close": d.get("close"), "sma": d.get("sma"), "held_qty": d.get("held_qty"),
                              "previous_target": d.get("previous_target"), "context": [{"session": x["session"], "close": x.get("close"), "sma": x.get("sma")} for x in prev],
                              "answer": {"action": d.get("action"), "reason": d.get("reason"), "execution": d.get("execution")}})
        return {"run_id": rid, "data_mode": r.data_mode, "steps": steps[:120],
                "label": "SAMPLE replay: synthetic prices, not market history" if r.data_mode == "SAMPLE" else f"{r.data_mode} historical replay"}

    @app.get("/api/learn/weekly")
    def learn_weekly(_=Depends(require_session), s=Depends(S)):
        since = now_utc() - timedelta(days=7)
        d = s.scalar(select(Decision).where(Decision.known_at >= since, Decision.action.in_(["BUY", "SELL"])).order_by(Decision.id.desc())) or \
            s.scalar(select(Decision).order_by(Decision.id.desc()))
        if d:
            return {"kind": "actual_decision", "decision_id": d.id, "session": d.session.isoformat(),
                    "prompt": "Open this decision, read the inputs, and check: did the action follow the rule exactly?"}
        return {"kind": "replay", "prompt": "No live decisions yet. Do a replay round on the Learn page."}

    # -------------------------------------------------------------- journal & expenses
    @app.get("/api/journal")
    def journal(_=Depends(require_session), s=Depends(S)):
        return {"items": [{"id": j.id, "title": j.title, "source_url": j.source_url, "published_at": _times(j.published_at),
                           "first_seen_at": _times(j.first_seen_at), "instrument": j.instrument, "notes": j.notes}
                          for j in s.scalars(select(JournalEntry).order_by(JournalEntry.id.desc()).limit(200))],
                "note": "Manual journal only. URLs are stored as references and never fetched. Entries never affect trading."}

    @app.post("/api/journal")
    def journal_add(body: JournalBody, _=Depends(require_session), s=Depends(S)):
        url = body.source_url.strip()
        if url and urlparse(url).scheme not in ("http", "https"):
            raise HTTPException(422, "source_url must be http(s)")
        if body.published_at and body.published_at.tzinfo is None:
            raise HTTPException(422, "published_at must include a timezone")
        s.add(JournalEntry(title=body.title, source_url=url, published_at=body.published_at, instrument=body.instrument, notes=body.notes,
                           first_seen_at=now_utc()))
        return {"ok": True}

    @app.post("/api/journal/{jid}/delete")
    def journal_del(jid: int, _=Depends(require_session), s=Depends(S)):
        j = s.get(JournalEntry, jid)
        if j:
            s.delete(j)
        return {"ok": True}

    @app.get("/api/expenses")
    def expenses(_=Depends(require_session), s=Depends(S)):
        rows = list(s.scalars(select(Expense).order_by(Expense.day.desc())))
        total = sum((e.amount_usd for e in rows), Decimal(0))
        c = control(s)
        marks = list(s.scalars(select(EquityMark).where(EquityMark.account_key == c.account_fingerprint).order_by(EquityMark.session))) if c.account_fingerprint else []
        paper_pnl = (marks[-1].equity - marks[0].equity) if len(marks) >= 2 and c.performance_valid else None
        return {"items": [{"id": e.id, "day": e.day.isoformat(), "category": e.category, "description": e.description, "currency": e.currency,
                           "amount": str(e.amount), "fx_rate": str(e.fx_rate), "amount_money": mp(e.amount_usd)} for e in rows],
                "total": mp(total), "paper_pnl_since_activation": mp(paper_pnl),
                "hypothetical_net": mp(paper_pnl - total) if paper_pnl is not None else None,
                "note": "Hypothetical only: paper P&L is simulated and cannot pay real project costs.",
                "optional_costs": ["Market data upgrade (e.g. full-SIP real-time plan) — check current price on the provider's site; not needed for v1.",
                                   "Private server / VPS for hosting — not needed for local use.", "AI/LLM API usage for later news analysis — not used in v1."]}

    @app.post("/api/expenses")
    def expense_add(body: ExpenseBody, _=Depends(require_session), s=Depends(S)):
        rate = st.settings.usd_to_aed
        usd = body.amount if body.currency == "USD" else body.amount / rate
        s.add(Expense(day=body.day, category=body.category, description=body.description, currency=body.currency, amount=body.amount,
                      fx_rate=rate, amount_usd=usd))
        return {"ok": True}

    # -------------------------------------------------------------- operations & security
    @app.get("/api/ops")
    def ops(_=Depends(require_session), s=Depends(S)):
        now = now_utc()
        cfg = st.settings
        sessions = [x.to_dict() for x in st.cal.sessions_between(now.astimezone(NEW_YORK).date(), now.date() + timedelta(days=14))][:8]
        jobs = [{"type": j.job_type, "key": j.job_key, "status": j.status, "error": j.error, "finished": _times(j.finished_at)}
                for j in s.scalars(select(Job).order_by(Job.id.desc()).limit(20))]
        last_ok = s.scalar(select(Job).where(Job.job_type == "evaluate", Job.status == "DONE").order_by(Job.id.desc()))
        reps = list(s.scalars(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()).limit(10)))
        outbox = [{"id": n.id, "status": n.status, "title": n.title, "attempts": n.attempts, "last_error": n.last_error,
                   "created_at": _times(n.created_at), "accepted_at": _times(n.accepted_at)}
                  for n in s.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id.desc()).limit(30))]
        backups = sorted((cfg.data_dir / "backups").glob("*.sqlite3"), reverse=True)[:10] if (cfg.data_dir / "backups").exists() else []
        return {
            "worker": _worker_status(s), "market": _market(st.cal, now), "upcoming_sessions": sessions, "jobs": jobs,
            "last_successful_evaluation": {"key": last_ok.job_key, "at": _times(last_ok.finished_at)} if last_ok else None,
            "reconciliation_history": [{"id": r.id, "at": _times(r.at), "ok": r.ok, "discrepancies": r.discrepancies} for r in reps],
            "config": {"broker": "Alpaca PAPER (live route does not exist)", "base_url": cfg.alpaca_base_url,
                       "api_key_id": mask(cfg.alpaca_api_key_id), "api_secret": mask(cfg.alpaca_api_secret_key), "data_feed": cfg.alpaca_data_feed,
                       "bind": f"{cfg.bind_host}:{cfg.port}", "deployment_mode": cfg.deployment_mode, "usd_to_aed": str(cfg.usd_to_aed),
                       "ntfy_server": cfg.ntfy_server, "ntfy_topic": mask(cfg.ntfy_topic), "ntfy_token": mask(cfg.ntfy_token), "telegram": "configured (private chat)" if cfg.telegram_configured else "not set",
                       "heartbeat_url": mask(cfg.heartbeat_url),
                       "db_path": str(cfg.db_path.name)},
            "notifications": {"settings": get_notify_settings(s), "outbox": outbox,
                              "meaning": "ACCEPTED_BY_PROVIDER means ntfy accepted the message; it does not prove your phone displayed it. SUPPRESSED means push is not enabled/configured; the in-app inbox still has it."},
            "backups": [{"name": b.name, "size": b.stat().st_size, "modified": datetime.fromtimestamp(b.stat().st_mtime, UTC).isoformat()} for b in backups],
            "auto_backup": {**autobackup.status(s), "keep": max(3, cfg.backup_keep), "second_copy_configured": bool(cfg.backup_copy_dir),
                            "meaning": "The worker backs up the database once a day after 07:00 Dubai and keeps the newest copies. "
                                       "Set ORBIT_BACKUP_COPY_DIR in .env (for example a OneDrive folder) to keep a second copy off this disk."},
            "monitoring_routine": monitoring_routine(st.cal, now),
            "heartbeat": {"configured": bool(cfg.heartbeat_url), **((s.get(KV, "heartbeat_status").value if s.get(KV, "heartbeat_status") else {}) or {}),
                          "meaning": "An external monitor that alerts you when pings STOP. Configure ORBIT_HEARTBEAT_URL (https) on the server."},
        }

    @app.post("/api/ops/connection-test")
    def connection_test(_=Depends(require_session), s=Depends(S)):
        """Read-only checks. Never places an order."""
        if not st.settings.broker_configured:
            return {"ok": False, "results": [{"check": "credentials", "ok": False, "detail": "APCA_API_KEY_ID / APCA_API_SECRET_KEY not found in .env. See docs/SETUP.md."}]}
        from orbit.broker.alpaca_adapter import AlpacaPaperAdapter
        from orbit.broker.base import BrokerError
        res = []
        now = now_utc()
        try:
            ad = AlpacaPaperAdapter(st.settings)
            res.append({"check": "environment", "ok": True, "detail": "Paper endpoint verified (https://paper-api.alpaca.markets). Live endpoints are rejected."})
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "results": [{"check": "adapter", "ok": False, "detail": redact(e)}]}

        def step(name, fn):
            try:
                detail = fn()
                res.append({"check": name, "ok": True, "detail": detail})
            except BrokerError as e:
                res.append({"check": name, "ok": False, "detail": redact(e)[:300]})
            except Exception as e:  # noqa: BLE001
                res.append({"check": name, "ok": False, "detail": f"{type(e).__name__}: {redact(e)[:200]}"})

        def acct():
            a = ad.get_account()
            return {"status": a.status, "account": a.account_number_masked, "cash": mp(a.cash), "equity": mp(a.equity),
                    "buying_power": mp(a.buying_power), "multiplier": a.multiplier, "trading_blocked": a.trading_blocked,
                    "shorting_enabled": a.shorting_enabled, "note": "The paper balance is the broker's default/your choice; it is not a realistic budget. Orbit caps each entry at USD 250."}
        step("account", acct)
        step("positions", lambda: [p.to_public() for p in ad.get_positions()] or "No positions (clean account).")
        step("open_orders", lambda: [o.to_public() for o in ad.get_open_orders()] or "No open orders.")
        step("asset_SPY", lambda: ad.get_asset("SPY").__dict__)
        step("clock", lambda: (lambda c: {"is_open": c.is_open, "next_open": _times(c.next_open), "next_close": _times(c.next_close)})(ad.get_clock()))

        def data():
            ser = ad.fetch_daily_bars("SPY", (now - timedelta(days=420)).date(), now.date(), now, st.cal, 15)
            if not ser.bars:
                raise BrokerError("no bars returned for SPY (data entitlement?)")
            return {"feed": st.settings.alpaca_data_feed, "bars": len(ser.bars), "first": ser.bars[0].day.isoformat(), "last": ser.bars[-1].day.isoformat(),
                    "enough_for_200": len(ser.bars) >= 200, "note": ser.metadata.get("note")}
        step("market_data", data)
        audit(s, "user", "connection_test", {"results": [{"check": r["check"], "ok": r["ok"]} for r in res]})
        return {"ok": all(r["ok"] for r in res), "results": res, "orders_placed": 0}

    @app.post("/api/ops/notify-settings")
    def notify_settings(body: NotifyBody, _=Depends(require_session), s=Depends(S)):
        vals = {k: v for k, v in body.model_dump().items() if v is not None}
        if vals.get("include_financial_details") and not st.settings.ntfy_token:
            raise HTTPException(409, "Financial details in push messages need an authenticated ntfy setup (NTFY_TOKEN). A topic name alone is public.")
        try:
            cur = set_notify_settings(s, vals)
        except ValueError as e:
            raise HTTPException(422, str(e))
        audit(s, "user", "notify_settings", cur)
        return {"ok": True, "settings": cur}

    @app.post("/api/ops/notify-test")
    def notify_test(_=Depends(require_session), s=Depends(S)):
        if not st.settings.ntfy_topic:
            raise HTTPException(409, "Set NTFY_TOPIC in .env and restart first.")
        if not get_notify_settings(s).get("enabled"):
            raise HTTPException(409, "Enable push notifications first.")
        n = queue_test_notification(s, st.settings)
        return {"ok": True, "outbox_id": n.id if n else None, "message": "Queued. The worker sends it within ~20 s. 'Accepted by provider' does not prove your phone displayed it; confirm on the phone."}

    @app.post("/api/ops/backup")
    def do_backup(_=Depends(require_session), s=Depends(S)):
        p = backup_database(st.settings.db_path, st.settings.data_dir / "backups", "manual")
        v = verify_backup(p)
        s.add(KV(key=f"backup:{p.name}"[:64], value={"verified": v["integrity"], "at": now_utc().isoformat()}))
        audit(s, "user", "backup", {"file": p.name, "integrity": v["integrity"]})
        return {"ok": v["integrity"] == "ok", "file": p.name, "integrity": v["integrity"], "tables": v["tables"]}

    @app.get("/api/events")
    def events(_=Depends(require_session), s=Depends(S)):
        rows = s.scalars(select(EventInbox).where(EventInbox.severity != "routine").order_by(EventInbox.id.desc()).limit(100))
        return {"items": [{"id": e.id, "at": _times(e.at), "type": e.type, "severity": e.severity, "title": e.title, "body": e.body, "ref": e.ref,
                           "read": e.read_at is not None} for e in rows]}

    @app.post("/api/events/read")
    def events_read(_=Depends(require_session), s=Depends(S)):
        for e in s.scalars(select(EventInbox).where(EventInbox.read_at.is_(None))):
            e.read_at = now_utc()
        return {"ok": True}

    @app.get("/api/reconciliation")
    def reconciliation(_=Depends(require_session), s=Depends(S)):
        r = s.scalar(select(ReconciliationReport).order_by(ReconciliationReport.id.desc()))
        return {"report": {"id": r.id, "at": _times(r.at), "ok": r.ok, "discrepancies": r.discrepancies, "local": r.local, "broker": r.broker} if r else None,
                "field_sources": {
                    "from_broker": ["cash", "equity", "buying_power", "positions (symbol, qty, avg price)", "orders (status, filled qty, avg fill price)", "clock", "cash activities"],
                    "derived_by_orbit": ["strategy-confirmed held qty (sum of reconciled fills of Orbit orders only)", "cost basis", "intent states",
                                         "drawdown & high-water mark (close-to-close broker equity)", "AED equivalents (reference rate)"]}}

    @app.get("/api/security")
    def security(_=Depends(require_session), s=Depends(S)):
        cfg = st.settings
        c = control(s)
        ev_path = ROOT / "docs" / "TEST_EVIDENCE.md"
        recent = [{"at": _times(a.at), "action": a.action, "severity": a.severity} for a in
                  s.scalars(select(AuditLog).where(AuditLog.severity.in_(["warning", "critical"])).order_by(AuditLog.id.desc()).limit(20))]
        fails = s.scalar(select(func.count()).select_from(auth.LoginAttempt).where(auth.LoginAttempt.success.is_(False),
                                                                                   auth.LoginAttempt.at >= now_utc() - timedelta(hours=24)))
        backups = sorted((cfg.data_dir / "backups").glob("*.sqlite3"), reverse=True) if (cfg.data_dir / "backups").exists() else []
        return {
            "mode": c.mode, "security_paused": c.security_paused, "security_pause_reason": c.security_pause_reason,
            "access": {"bind": cfg.bind_host, "deployment_mode": cfg.deployment_mode, "auth": "Argon2id password + server-side session + CSRF header + Host/Origin checks",
                       "session_hours": cfg.session_hours, "failed_logins_24h": fails},
            "last_backup": backups[0].name if backups else None, "worker": _worker_status(s), "recent_security_events": recent,
            "verification": ev_path.read_text(encoding="utf-8")[:6000] if ev_path.exists() else "Run scripts/test.ps1 to generate docs/TEST_EVIDENCE.md.",
            "not_implemented": [
                "No remote deployment hardening is active (HTTPS, firewall, secure cookies only in remote mode; remote mode is untested).",
                "Local logs and the SQLite database are not tamper-proof if this computer is compromised.",
                ".env stores keys in plaintext; it does not protect against malware or an administrator on this host.",
                "No external heartbeat/watchdog yet: a sleeping or powered-off laptop cannot alert you.",
                "No multi-factor login for the local dashboard.",
                "No WebSocket endpoints exist (dashboard polls), so WebSocket auth is not applicable yet."],
            "incident_checklist": [
                "Press 'Security pause' (blocks all automated submissions).",
                "Log in to the Alpaca dashboard directly (not via links in messages) and inspect orders and positions.",
                "Revoke/regenerate the paper API keys in the Alpaca dashboard; update .env; restart Orbit.",
                "Export/backup the database and keep the logs (they are redacted).",
                "If the computer may be compromised, repair or rebuild it before reusing keys.",
                "Run a connection test, check the reconciliation report is clean, then resume with a written note.",
                "A local pause cannot stop someone who already holds working broker keys; revoking them can."],
            "disclaimer": "These controls reduce risk. Passing tests does not prove the app cannot be attacked, and this is not production-ready.",
        }

    @app.get("/api/export/decisions")
    def export_decisions(_=Depends(require_session), s=Depends(S)):
        rows = [ser_decision(d, full=True) for d in s.scalars(select(Decision).order_by(Decision.session))]
        its = [ser_intent(i) for i in s.scalars(select(OrderIntent).order_by(OrderIntent.id))]
        payload = json.loads(redact(json.dumps({"decisions": rows, "intents": its, "fx": fx_assumption(), "exported_at": now_utc().isoformat()}, default=str)))
        return JSONResponse(payload, headers={"Content-Disposition": 'attachment; filename="orbit-decisions.json"'})


def monitoring_routine(cal, now) -> list[dict]:
    """Supervision routine in Dubai time, computed from the exchange calendar (never hardcoded)."""
    out = []
    for sess in cal.sessions_between(now.astimezone(NEW_YORK).date(), now.date() + timedelta(days=7))[:3]:
        out.append({"session": sess.day.isoformat(),
                    "check_open_dubai": sess.open_utc.astimezone(DUBAI).strftime("%a %d %b %H:%M"),
                    "window_end_dubai": (sess.open_utc + timedelta(minutes=5)).astimezone(DUBAI).strftime("%H:%M"),
                    "evaluation_dubai": (sess.close_utc + timedelta(minutes=15)).astimezone(DUBAI).strftime("%a %d %b %H:%M"),
                    "early_close": sess.early_close})
    return out


def _static(app: FastAPI):
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "not found")
        target = (STATIC_DIR / path).resolve()
        if path and target.is_file() and STATIC_DIR.resolve() in target.parents:
            return FileResponse(target)
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return PlainTextResponse("Dashboard not built yet. Run scripts/setup.ps1 (builds frontend).", status_code=503)


def main():
    import uvicorn
    from orbit.logging_setup import setup_logging
    settings = get_settings()
    setup_logging(settings, "api")
    app = create_app(settings)
    log.info("Orbit dashboard: http://%s:%s  (paper-only; worker runs separately)", settings.bind_host, settings.port)
    uvicorn.run(app, host=settings.bind_host, port=settings.port, log_level="warning", proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
