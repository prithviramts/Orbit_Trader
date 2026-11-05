"""Local command-line tools. Run via scripts/*.ps1 or `uv run python -m orbit.cli <command>`."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from orbit.config import get_settings, validate_startup
from orbit.db import Database, backup_database, init_db, restore_database, verify_backup


def _db():
    st = get_settings()
    st.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(st.db_path)
    init_db(db.engine, st.data_dir / "backups", st.db_path)
    return st, db


def cmd_set_password(_):
    from orbit import auth
    st, db = _db()
    pw = getpass.getpass(f"New dashboard password (min {auth.MIN_PASSWORD} chars, not echoed): ")
    pw2 = getpass.getpass("Repeat: ")
    if pw != pw2:
        print("Passwords do not match."); return 1
    try:
        with db.session() as s:
            auth.set_password(s, pw)
    except ValueError as e:
        print(f"Rejected: {e}"); return 1
    print("Password set (Argon2id hash stored locally). Existing sessions were logged out.")
    return 0


def cmd_backup(_):
    st, _db_ = _db()
    p = backup_database(st.db_path, st.data_dir / "backups", "manual")
    v = verify_backup(p)
    print(json.dumps({"backup": str(p), "integrity": v["integrity"], "tables": v["tables"]}, indent=2))
    return 0 if v["integrity"] == "ok" else 1


def cmd_restore(a):
    st = get_settings()
    src = Path(a.file)
    print("Restoring requires the API and worker to be STOPPED (scripts/stop.ps1).")
    if input(f"Type RESTORE to replace {st.db_path.name} with {src.name}: ").strip() != "RESTORE":
        print("Cancelled."); return 1
    if st.db_path.exists():
        safety = backup_database(st.db_path, st.data_dir / "backups", "pre-restore")
        print(f"Current DB saved first as {safety.name}")
    restore_database(src, st.db_path)
    print("Restored. Start Orbit; the worker reconciles with the broker before any action.")
    return 0


def cmd_restore_drill(_):
    """Back up, restore into a scratch copy, and compare table counts. Does not touch the live DB."""
    import shutil, tempfile
    st, _db_ = _db()
    p = backup_database(st.db_path, st.data_dir / "backups", "drill")
    v1 = verify_backup(p)
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "restored.sqlite3"
        restore_database(p, target)
        v2 = verify_backup(target)
    ok = v1["integrity"] == v2["integrity"] == "ok" and v1["tables"] == v2["tables"]
    print(json.dumps({"backup": p.name, "integrity": v2["integrity"], "tables_match": v1["tables"] == v2["tables"], "tables": v2["tables"]}, indent=2))
    print("RESTORE DRILL PASSED" if ok else "RESTORE DRILL FAILED")
    return 0 if ok else 1


def cmd_check(_):
    st = get_settings()
    probs = validate_startup(st)
    print(json.dumps({"broker_configured": st.broker_configured, "base_url": st.alpaca_base_url, "feed": st.alpaca_data_feed,
                      "bind": f"{st.bind_host}:{st.port}", "ntfy_topic_set": bool(st.ntfy_topic), "problems": probs}, indent=2))
    return 1 if probs else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="orbit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-password").set_defaults(fn=cmd_set_password)
    sub.add_parser("backup").set_defaults(fn=cmd_backup)
    r = sub.add_parser("restore"); r.add_argument("file"); r.set_defaults(fn=cmd_restore)
    sub.add_parser("restore-drill").set_defaults(fn=cmd_restore_drill)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
