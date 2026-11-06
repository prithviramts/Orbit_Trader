"""Automatic daily database backup, run by the worker.

Once per Dubai day (first worker cycle after 07:00 Dubai, well away from the 01:15 decision and the
18:30 order window) the worker takes a consistent online SQLite backup, checks its integrity and keeps
the newest ORBIT_BACKUP_KEEP automatic copies. If ORBIT_BACKUP_COPY_DIR is set (for example a OneDrive
or USB folder), each verified backup is also copied there so one broken disk cannot lose your history.

Only files this module created (names ending in "-auto.sqlite3") are ever pruned. Manual,
pre-migration and pre-restore backups are never deleted. A failure raises a warning event and
never affects trading.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from orbit.config import Settings, redact
from orbit.db import KV, backup_database, verify_backup
from orbit.market_calendar import DUBAI, UTC

log = logging.getLogger("orbit.autobackup")
RUN_AFTER_HOUR_DUBAI = 7
SUFFIX = "-auto.sqlite3"
KV_KEY = "autobackup_status"


def _prune(folder: Path, keep: int) -> list[str]:
    files = sorted(folder.glob(f"orbit-*{SUFFIX}"), key=lambda p: p.name, reverse=True)
    removed = []
    for old in files[keep:]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError as exc:
            log.warning("could not prune %s: %s", old.name, type(exc).__name__)
    return removed


def status(s: Session) -> dict:
    row = s.get(KV, KV_KEY)
    return dict(row.value) if row and row.value else {}


def _save(s: Session, value: dict) -> None:
    row = s.get(KV, KV_KEY)
    if row:
        row.value = value
    else:
        s.add(KV(key=KV_KEY, value=value))


def maybe_run(s: Session, settings: Settings, now: datetime) -> dict | None:
    """Back up if one is due today. Returns the new status when a backup was attempted, else None. Never raises."""
    from orbit.notify import emit

    local = now.astimezone(DUBAI)
    today = local.date().isoformat()
    if local.hour < RUN_AFTER_HOUR_DUBAI:
        return None
    prev = status(s)
    if prev.get("attempt_day") == today:
        return None  # one attempt per day; a failure is reported, not retried every 20 seconds
    keep = max(3, settings.backup_keep)
    result: dict = {"attempt_day": today, "at": now.isoformat(), "ok": False, "file": None, "copied_to": None,
                    "copy_ok": None, "error": "", "kept": keep, "day": prev.get("day")}
    try:
        folder = settings.data_dir / "backups"
        made = backup_database(settings.db_path, folder, "auto-tmp")
        p = made.replace(folder / f"orbit-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}{SUFFIX}")  # named by the cycle time
        v = verify_backup(p)
        if v["integrity"] != "ok":
            raise RuntimeError(f"integrity check said {v['integrity']}")
        result.update(ok=True, file=p.name, size=p.stat().st_size, day=today)
        _prune(folder, keep)
        if settings.backup_copy_dir:
            dest = Path(settings.backup_copy_dir)
            result["copied_to"] = str(dest)
            try:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest / p.name)
                result["copy_ok"] = verify_backup(dest / p.name)["integrity"] == "ok"
                _prune(dest, keep)
            except Exception as exc:  # noqa: BLE001 - second copy is best effort, but reported
                result["copy_ok"] = False
                result["error"] = f"second copy failed: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 - backups must never break trading
        result["error"] = redact(f"{type(exc).__name__}: {exc}")[:300]
        log.warning("automatic backup failed: %s", result["error"])
    _save(s, result)
    if not result["ok"] or result["copy_ok"] is False:
        emit(s, "backup_failed", "warning", "Automatic backup problem",
             f"Today's automatic database backup did not fully work: {result['error'] or 'unknown error'}. "
             "Trading is not affected. Use Operations, Back up now, and check the disk or backup folder.",
             f"autobackup_fail:{today}", settings=settings)
    else:
        log.info("automatic backup %s ok%s", result["file"], " (+ second copy)" if result["copy_ok"] else "")
    return result
