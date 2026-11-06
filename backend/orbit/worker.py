"""The single persistent trading worker.

Only this process evaluates the rule and submits orders. The web UI never owns the schedule:
reloading the page, opening tabs or restarting the API cannot create a second trader.

Duplicate prevention (layered):
1. OS file lock on data/worker.lock (same host).
2. Database lease row with expiry + heartbeat (survives crashes; another worker can take over
   only after the lease expires).
3. Unique constraints on decisions, intents, client order ids, fills and jobs.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from orbit import autobackup
from orbit.config import Settings, get_settings, redact, validate_startup
from orbit.db import Database, WorkerLease, init_db
from orbit.engine import Engine
from orbit.logging_setup import setup_logging
from orbit.market_calendar import DUBAI, MarketCalendar, now_utc, xnys_calendar
from orbit.heartbeat import Heartbeat
from orbit.notify import build_senders, dispatch_outbox, emit

log = logging.getLogger("orbit.worker")
LEASE_NAME = "trader"
LEASE_SECONDS = 90


class LeaseLost(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self) -> None:
        if self.fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.fh.close()
            self.fh = None


def acquire_lease(db: Database, owner: str, now: datetime) -> bool:
    with db.session() as s:
        res = s.execute(update(WorkerLease).where(WorkerLease.name == LEASE_NAME,
                                                  (WorkerLease.expires_at < now) | (WorkerLease.owner == owner))
                        .values(owner=owner, pid=os.getpid(), started_at=now, heartbeat_at=now,
                                expires_at=now + timedelta(seconds=LEASE_SECONDS), status="running"))
        if res.rowcount == 1:
            return True
        if s.get(WorkerLease, LEASE_NAME) is None:
            try:
                with s.begin_nested():
                    s.add(WorkerLease(name=LEASE_NAME, owner=owner, pid=os.getpid(), started_at=now, heartbeat_at=now,
                                      expires_at=now + timedelta(seconds=LEASE_SECONDS)))
                    s.flush()
                return True
            except IntegrityError:
                return False
        return False


def heartbeat(db: Database, owner: str, now: datetime, status: str = "running") -> None:
    with db.session() as s:
        res = s.execute(update(WorkerLease).where(WorkerLease.name == LEASE_NAME, WorkerLease.owner == owner)
                        .values(heartbeat_at=now, expires_at=now + timedelta(seconds=LEASE_SECONDS), status=status))
        if res.rowcount != 1:
            raise LeaseLost("worker lease taken by another process")


def release_lease(db: Database, owner: str) -> None:
    with db.session() as s:
        s.execute(update(WorkerLease).where(WorkerLease.name == LEASE_NAME, WorkerLease.owner == owner)
                  .values(expires_at=now_utc() - timedelta(seconds=1), status="stopped"))


def record_heartbeat(db: Database, hb: Heartbeat) -> None:
    from orbit.db import KV
    with db.session() as s:
        row = s.get(KV, "heartbeat_status")
        if row:
            row.value = hb.status()
        else:
            s.add(KV(key="heartbeat_status", value=hb.status()))


class AlpacaDataProvider:
    def __init__(self, adapter):
        self.adapter = adapter

    def fetch(self, symbol, start, end, completed_before, calendar, buffer_minutes):
        return self.adapter.fetch_daily_bars(symbol, start, end, completed_before, calendar, buffer_minutes)


def build_engine(settings: Settings, db: Database) -> Engine:
    broker = None
    data = None
    cal: MarketCalendar = xnys_calendar()
    if settings.broker_configured:
        from orbit.broker.alpaca_adapter import AlpacaPaperAdapter

        broker = AlpacaPaperAdapter(settings)
        data = AlpacaDataProvider(broker)
    return Engine(db, broker, cal, data, settings, also_manage=("GLD",))


def build_gold_engine(settings: Settings, db: Database, spy_engine: Engine):
    """Gold AI strategy sharing the SPY engine's broker, calendar and data provider (None without a broker)."""
    if spy_engine.broker is None:
        return None
    from orbit.gold_engine import GoldEngine
    return GoldEngine(db, spy_engine.broker, spy_engine.cal, spy_engine.data, settings)


def main() -> int:
    settings = get_settings()
    setup_logging(settings, "worker")
    problems = validate_startup(settings)
    if problems:
        for p in problems:
            log.error("startup refused: %s", p)
        return 2
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    flock = FileLock(settings.data_dir / "worker.lock")
    if not flock.acquire():
        log.error("Another Orbit worker holds %s on this machine. Exiting without trading.", flock.path)
        return 3
    db = Database(settings.db_path)
    init_db(db.engine, settings.data_dir / "backups", settings.db_path)
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    if not acquire_lease(db, owner, now_utc()):
        log.error("Database lease held by another live worker. Exiting without trading.")
        flock.release()
        return 3
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)

    stop_flag = settings.data_dir / "run" / "worker.stop"
    stop_flag.parent.mkdir(parents=True, exist_ok=True)
    if stop_flag.exists():
        stop_flag.unlink()
    engine = build_engine(settings, db)
    gold = build_gold_engine(settings, db, engine)
    sender = build_senders(settings)  # {channel: sender}; empty when no channel is configured
    hb = Heartbeat(settings)
    with db.session() as s:
        emit(s, "worker_started", "info", "Trading worker started",
             f"Worker {owner} started at {now_utc().astimezone(DUBAI):%Y-%m-%d %H:%M} Dubai. Broker: {'Alpaca PAPER' if engine.broker else 'not configured (no trading)'}.",
             f"worker_start:{owner}", settings=settings)
    log.info("Worker running (owner=%s). Broker=%s. Tick every %ss. DB=%s", owner,
             "alpaca-paper" if engine.broker else "none", settings.worker_tick_seconds, settings.db_path)
    exit_code = 0
    try:
        while not stop["flag"]:
            now = now_utc()
            heartbeat(db, owner, now)
            tick_ok = False
            try:
                ctx = engine.tick(now)
                tick_ok = True
                if gold is not None:
                    try:
                        gold.tick_after(ctx)
                    except Exception as gexc:  # noqa: BLE001 - the gold experiment must never break the SPY strategy
                        log.exception("gold AI cycle failed: %s", redact(gexc))
                        with db.session() as s:
                            emit(s, "reconciliation_failed", "warning", "Gold AI cycle error",
                                 f"{type(gexc).__name__}: {redact(gexc)[:400]}. The SPY strategy is not affected.",
                                 f"golderr:{now:%Y%m%d%H}", settings=settings)
            except Exception as exc:  # noqa: BLE001 - log, alert and continue; state is durable
                log.exception("tick failed: %s", redact(exc))
                with db.session() as s:
                    emit(s, "reconciliation_failed", "critical", "Worker cycle error",
                         f"{type(exc).__name__}: {redact(exc)[:500]}", f"tickerr:{now:%Y%m%d%H%M}", settings=settings)
            if sender:
                with db.session() as s:
                    dispatch_outbox(s, sender, now)
            # External dead-man's switch: only a completed cycle counts as "alive".
            if tick_ok and hb.maybe_ping(now) or hb.last_error:
                record_heartbeat(db, hb)
            try:
                with db.session() as s:
                    autobackup.maybe_run(s, settings, now)
            except Exception as exc:  # noqa: BLE001 - a backup problem must never stop trading
                log.warning("automatic backup step failed: %s", redact(exc))
            for _ in range(settings.worker_tick_seconds * 2):
                if stop_flag.exists():
                    stop["flag"] = True
                if stop["flag"]:
                    break
                time.sleep(0.5)
    except LeaseLost as exc:
        log.error("%s; stopping immediately.", exc)
        exit_code = 4
    finally:
        with db.session() as s:
            emit(s, "worker_stopped", "warning", "Trading worker stopped",
                 "Worker shut down. Broker-accepted orders and positions survive while the worker is stopped.",
                 f"worker_stop:{owner}", settings=settings)
        if sender:
            with db.session() as s:
                dispatch_outbox(s, sender, now_utc())
        release_lease(db, owner)
        flock.release()
        log.info("Worker stopped cleanly.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
