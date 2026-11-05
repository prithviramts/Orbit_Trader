"""SQLAlchemy models and the SQLite engine.

Money and quantities are stored as TEXT via ``DecimalText`` so no float rounding
ever touches them. Timestamps are stored as naive UTC and re-attached to UTC on read.
Uniqueness constraints make duplicate decisions/intents/fills/jobs impossible even if
two processes race.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator

from orbit.market_calendar import UTC, now_utc

SCHEMA_VERSION = 1


class DecimalText(TypeDecorator):
    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("floats are not allowed for money/quantity columns")
        return str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=UTC)


class JSONText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value, default=str, sort_keys=True)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


class Base(DeclarativeBase):
    pass


def _now():
    return now_utc()


# ------------------------------------------------------------------ auth / audit
class User(Base):
    __tablename__ = "app_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    password_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class AuthSession(Base):
    __tablename__ = "auth_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_auth_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    client: Mapped[str] = mapped_column(String(64), default="")


class LoginAttempt(Base):
    __tablename__ = "login_attempt"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    client: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(32))  # user | worker | system
    action: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), default="info")
    details: Mapped[dict] = mapped_column(JSONText, default=dict)


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONText)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


# ------------------------------------------------------------------ control / strategy
class ControlState(Base):
    """Single row. Mode and pause flags are separate fields; none implies another."""

    __tablename__ = "control_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="OBSERVE")  # OBSERVE | PAPER_AUTO
    new_orders_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # manual pause
    risk_paused_entries: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_pause_reason: Mapped[str] = mapped_column(Text, default="")
    security_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    security_pause_reason: Mapped[str] = mapped_column(Text, default="")
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reason: Mapped[str] = mapped_column(Text, default="")
    performance_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    performance_invalid_reason: Mapped[str] = mapped_column(Text, default="")
    risk_override_reason: Mapped[str] = mapped_column(Text, default="")
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    account_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


class StrategyVersion(Base):
    __tablename__ = "strategy_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSONText)
    risk_policy: Mapped[dict] = mapped_column(JSONText)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    content_hash: Mapped[str] = mapped_column(String(64))


class StrategyPosition(Base):
    """What this strategy believes it owns, derived ONLY from reconciled confirmed fills."""

    __tablename__ = "strategy_position"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(16))
    signal_target: Mapped[str] = mapped_column(String(16), default="CASH")
    held_qty: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    cost_basis: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    exposure_note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("account_key", "symbol"),)


class Decision(Base):
    __tablename__ = "decision"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(16))
    session: Mapped[date] = mapped_column(Date)
    sequence: Mapped[int] = mapped_column(Integer, default=1)  # >1 only for a re-assessment after a conflicting order resolved
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_version.id"))
    data_mode: Mapped[str] = mapped_column(String(16))
    data_source: Mapped[str] = mapped_column(Text)
    data_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    data_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    close: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    sma: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    window_count: Mapped[int] = mapped_column(Integer, default=0)
    comparison: Mapped[str] = mapped_column(String(8), default="N/A")
    previous_target: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(8))  # BUY SELL HOLD SKIP ERROR
    mode_at_decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    inputs: Mapped[dict] = mapped_column(JSONText, default=dict)
    held_qty_at_decision: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    known_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    __table_args__ = (UniqueConstraint("account_key", "symbol", "session", "sequence", name="uq_decision_session"),)


INTENT_STATES = {
    "PENDING", "SUBMITTING", "SUBMITTED", "PARTIALLY_FILLED", "CANCEL_REQUESTED",
    "FILLED", "TERMINAL_PARTIAL", "AMBIGUOUS", "MISSED_EXECUTION", "REJECTED", "CANCELLED_ZERO_FILL",
    "SKIPPED", "SUPERSEDED", "REVIEW_REQUIRED",
}
OPEN_INTENT_STATES = {"PENDING", "SUBMITTING", "SUBMITTED", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "AMBIGUOUS"}


class OrderIntent(Base):
    __tablename__ = "order_intent"
    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision.id"), nullable=True)
    parent_intent_id: Mapped[int | None] = mapped_column(ForeignKey("order_intent.id"), nullable=True)
    account_key: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(16))
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_version.id"))
    kind: Mapped[str] = mapped_column(String(16))  # ENTRY EXIT RESIDUAL_EXIT CLOSE_MANUAL
    side: Mapped[str] = mapped_column(String(4))  # BUY SELL
    signal_session: Mapped[date] = mapped_column(Date)
    execution_session: Mapped[date] = mapped_column(Date)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    frozen_notional: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    sizing: Mapped[dict] = mapped_column(JSONText, default=dict)
    target_qty: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    submitted_notional: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="PENDING")
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    broker_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    filled_qty: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))
    filled_avg_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    applied_qty: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal(0))  # qty already applied to StrategyPosition
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)
    __table_args__ = (
        UniqueConstraint("account_key", "symbol", "signal_session", "kind", "attempt", name="uq_intent_identity"),
    )


class OrderEvent(Base):
    __tablename__ = "order_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("order_intent.id"), nullable=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # STATE, BROKER_STATUS, NOTE
    from_state: Mapped[str] = mapped_column(String(32), default="")
    to_state: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[dict] = mapped_column(JSONText, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Fill(Base):
    __tablename__ = "fill"
    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("order_intent.id"), nullable=True)
    broker_order_id: Mapped[str] = mapped_column(String(64))
    fill_key: Mapped[str] = mapped_column(String(128), unique=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[Decimal] = mapped_column(DecimalText)
    price: Mapped[Decimal] = mapped_column(DecimalText)
    at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(32), default="broker_order_snapshot")


class BrokerSnapshot(Base):
    __tablename__ = "broker_snapshot"
    id: Mapped[int] = mapped_column(primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    account: Mapped[dict] = mapped_column(JSONText, default=dict)
    positions: Mapped[list] = mapped_column(JSONText, default=list)
    open_orders: Mapped[list] = mapped_column(JSONText, default=list)
    clock: Mapped[dict] = mapped_column(JSONText, default=dict)


class ReconciliationReport(Base):
    __tablename__ = "reconciliation_report"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    discrepancies: Mapped[list] = mapped_column(JSONText, default=list)
    local: Mapped[dict] = mapped_column(JSONText, default=dict)
    broker: Mapped[dict] = mapped_column(JSONText, default=dict)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("broker_snapshot.id"), nullable=True)


class EquityMark(Base):
    __tablename__ = "equity_mark"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64))
    session: Mapped[date] = mapped_column(Date)
    equity: Mapped[Decimal] = mapped_column(DecimalText)
    cash: Mapped[Decimal] = mapped_column(DecimalText)
    high_water_mark: Mapped[Decimal] = mapped_column(DecimalText)
    drawdown: Mapped[Decimal] = mapped_column(DecimalText)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    __table_args__ = (UniqueConstraint("account_key", "session"),)


class Job(Base):
    __tablename__ = "job"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32))
    job_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")  # QUEUED RUNNING DONE FAILED
    payload: Mapped[dict] = mapped_column(JSONText, default=dict)
    result: Mapped[dict] = mapped_column(JSONText, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    __table_args__ = (UniqueConstraint("job_type", "job_key", name="uq_job_identity"),)


class WorkerLease(Base):
    __tablename__ = "worker_lease"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    pid: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(32), default="running")


class EventInbox(Base):
    __tablename__ = "event_inbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    type: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16))  # info routine warning critical
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    public_summary: Mapped[str] = mapped_column(String(300))
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    ref: Mapped[str] = mapped_column(String(64), default="")
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event_inbox.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), default="ntfy")
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED")  # QUEUED ACCEPTED_BY_PROVIDER FAILED SUPPRESSED HELD_QUIET
    priority: Mapped[int] = mapped_column(Integer, default=3)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(String(1000))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Dataset(Base):
    __tablename__ = "dataset"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(Text)
    price_basis: Mapped[str] = mapped_column(String(16))
    fingerprint: Mapped[str] = mapped_column(String(64))
    bars_json: Mapped[str] = mapped_column(Text)
    describe: Mapped[dict] = mapped_column(JSONText)
    quality: Mapped[dict] = mapped_column(JSONText, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class BacktestRun(Base):
    __tablename__ = "backtest_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id"))
    data_mode: Mapped[str] = mapped_column(String(16))
    period: Mapped[str] = mapped_column(String(16), default="full")
    config: Mapped[dict] = mapped_column(JSONText)
    summary: Mapped[dict] = mapped_column(JSONText, default=dict)
    result_json: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class JournalEntry(Base):
    __tablename__ = "journal_entry"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str] = mapped_column(String(500), default="")
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    instrument: Mapped[str] = mapped_column(String(16), default="SPY")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Expense(Base):
    __tablename__ = "expense"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(200), default="")
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(DecimalText)
    fx_rate: Mapped[Decimal] = mapped_column(DecimalText)
    amount_usd: Mapped[Decimal] = mapped_column(DecimalText)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    note: Mapped[str] = mapped_column(Text, default="")


# ------------------------------------------------------------------ engine
_engines: dict[str, object] = {}


def make_engine(db_path: Path | str):
    url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
    kw = {"connect_args": {"timeout": 10, "check_same_thread": False}}
    if str(db_path) == ":memory:":
        from sqlalchemy.pool import StaticPool
        kw["poolclass"] = StaticPool
    eng = create_engine(url, **kw)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        if str(db_path) != ":memory:":
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=FULL")
        cur.close()

    return eng


def init_db(engine, backup_dir: Path | None = None, db_path: Path | None = None) -> None:
    """Create/upgrade schema. Backs up an existing database before any schema change."""
    from sqlalchemy import inspect

    insp = inspect(engine)
    existing = insp.has_table("schema_version")
    if existing:
        with Session(engine) as s:
            cur = s.scalar(select(SchemaVersion.version).order_by(SchemaVersion.version.desc()))
        if cur == SCHEMA_VERSION:
            return
        if cur and cur > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {cur} is newer than this code ({SCHEMA_VERSION}); refusing to run")
        if db_path and backup_dir:
            backup_database(db_path, backup_dir, label=f"pre-migrate-v{cur}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        if not s.get(SchemaVersion, SCHEMA_VERSION):
            s.add(SchemaVersion(version=SCHEMA_VERSION, note="initial schema"))
        if not s.get(ControlState, 1):
            s.add(ControlState(id=1, mode="OBSERVE"))
        s.commit()


def backup_database(db_path: Path, backup_dir: Path, label: str = "manual") -> Path:
    """Consistent online backup using SQLite's backup API (safe while WAL writers run)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"orbit-{stamp}-{label}.sqlite3"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def verify_backup(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables if t.isidentifier()}
    finally:
        con.close()
    return {"integrity": ok, "tables": counts}


def restore_database(backup: Path, db_path: Path) -> None:
    """Restore requires both API and worker to be stopped (documented in scripts/restore.ps1)."""
    if not backup.exists():
        raise FileNotFoundError(backup)
    if verify_backup(backup)["integrity"] != "ok":
        raise RuntimeError("backup failed integrity check; not restoring")
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(backup, db_path)


class Database:
    def __init__(self, db_path: Path | str):
        self.path = db_path
        self.engine = make_engine(db_path)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
