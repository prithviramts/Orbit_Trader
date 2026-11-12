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
                      "bind": f"{st.bind_host}:{st.port}", "ntfy_topic_set": bool(st.ntfy_topic),
                      "telegram_configured": st.telegram_configured, "problems": probs}, indent=2))
    return 1 if probs else 0


def cmd_telegram_setup(a):
    """Find the chat id of your private chat with your bot (after you send it any message) and save it to .env."""
    import re

    import httpx

    from orbit.config import ROOT
    st = get_settings()
    if not st.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set in .env. Create a bot with @BotFather first (see docs/ALERTS.md).")
        return 1
    tok = st.telegram_bot_token.get_secret_value()
    try:
        me = httpx.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
        upd = httpx.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=10).json()
    except httpx.HTTPError as e:
        print(f"Could not reach Telegram: {type(e).__name__}")
        return 1
    if not me.get("ok"):
        print("Telegram did not accept the bot token. Check TELEGRAM_BOT_TOKEN in .env (copy it again from @BotFather).")
        return 1
    print(f"Bot found: @{me['result'].get('username')}")
    chats = {}
    for u in upd.get("result", []):
        c = (u.get("message") or {}).get("chat") or {}
        if c.get("type") == "private":
            chats[str(c["id"])] = c.get("first_name", "")
    if not chats:
        print(f"No private messages yet. Open Telegram, search @{me['result'].get('username')}, press Start, then run this again.")
        return 1
    if len(chats) > 1:
        print("More than one person has messaged this bot:", ", ".join(f"{n} ({i})" for i, n in chats.items()))
        print("Only you should use it. Make a new bot, or put the right chat id in .env yourself.")
        return 1
    chat_id, name = next(iter(chats.items()))
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    line = f"TELEGRAM_CHAT_ID={chat_id}"
    if re.search(r"(?m)^TELEGRAM_CHAT_ID=", text):
        text = re.sub(r"(?m)^TELEGRAM_CHAT_ID=.*$", line, text)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    env.write_text(text, encoding="utf-8")  # same file, so its access permissions are kept
    print(f"Saved your chat ({name}) to .env. Restart Orbit, then use 'Send test notification' on the Operations page.")
    return 0


def cmd_telegram_test(_):
    from orbit.notify import TelegramSender
    st = get_settings()
    if not st.telegram_configured:
        print("Telegram is not configured (need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env).")
        return 1
    try:
        TelegramSender(st).send("Orbit test", "✅ Orbit can reach your Telegram. Trade alerts will arrive here.", 3)
    except RuntimeError as e:
        print(f"Failed: {e}")
        return 1
    print("Sent. Check Telegram.")
    return 0


def cmd_heartbeat_test(_):
    from orbit.heartbeat import Heartbeat
    from orbit.market_calendar import now_utc
    st = get_settings()
    hb = Heartbeat(st)
    if not hb.enabled:
        print("No heartbeat URL. Put your https ping URL after ORBIT_HEARTBEAT_URL= in .env (see docs/HEARTBEAT.md).")
        return 1
    if hb.maybe_ping(now_utc()):
        print("Ping accepted. Your monitor should now show Orbit as 'up'.")
        return 0
    print(f"Ping failed ({hb.last_error}). Check the URL in .env.")
    return 1


def cmd_report(a):
    """Print a monthly report card without sending it (preview)."""
    from orbit import report
    from orbit.market_calendar import DUBAI, now_utc
    _, db = _db()
    month = a.month or report._prev_month(now_utc().astimezone(DUBAI).date())
    with db.session() as s:
        title, body = report.build(s, month)
    print(title)
    print(body)
    return 0


def cmd_news_model(a):
    """Download FinBERT for the AI news reader, or show whether it is installed."""
    from orbit import news_ai
    st = get_settings()
    if a.action == "download":
        print(f"Downloading {news_ai.MODEL_REPO} (about 440 MB) into {news_ai.model_dir(st.data_dir)} ...")
        news_ai.download(st.data_dir)
    ok = news_ai.model_installed(st.data_dir)
    print("FinBERT installed." if ok else "FinBERT not installed. Run: uv run python -m orbit.cli news-model download")
    if ok and a.action == "download":
        print("Restart Orbit (run scripts/stop.ps1, then scripts/start.ps1) so the worker starts scoring headlines.")
    return 0 if ok else 1


def cmd_gold(a):
    from decimal import Decimal
    from orbit import gold_engine
    from orbit.engine import active_version, version_params
    st, db = _db()
    with db.session() as s:
        p, _ = version_params(active_version(s))
        cur = gold_engine.get_settings(s)
        if a.state == "status":
            print(json.dumps(cur))
            return 0
        budget = Decimal(a.budget) if a.budget else Decimal(cur["budget_usd"])
        out = gold_engine.set_settings(s, a.state == "on", budget, p.max_entry_notional_usd, actor="owner-cli")
    print(json.dumps(out), "(paper only; the worker reads this on its next cycle)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="orbit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-password").set_defaults(fn=cmd_set_password)
    sub.add_parser("backup").set_defaults(fn=cmd_backup)
    r = sub.add_parser("restore"); r.add_argument("file"); r.set_defaults(fn=cmd_restore)
    sub.add_parser("restore-drill").set_defaults(fn=cmd_restore_drill)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("telegram-setup").set_defaults(fn=cmd_telegram_setup)
    sub.add_parser("telegram-test").set_defaults(fn=cmd_telegram_test)
    sub.add_parser("heartbeat-test").set_defaults(fn=cmd_heartbeat_test)
    nm = sub.add_parser("news-model"); nm.add_argument("action", choices=["download", "status"]); nm.set_defaults(fn=cmd_news_model)
    rp = sub.add_parser("report"); rp.add_argument("--month", help="YYYY-MM, default last month"); rp.set_defaults(fn=cmd_report)
    gp = sub.add_parser("gold"); gp.add_argument("state", choices=["on", "off", "status"]); gp.add_argument("--budget")
    gp.set_defaults(fn=cmd_gold)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
