"""Local single-user authentication.

- Password hashed with Argon2id (argon2-cffi). No default password; it is set locally via
  ``scripts/set-password.ps1`` (getpass prompt, never a command-line argument).
- Server-side sessions: random 256-bit token in an HttpOnly SameSite=Strict cookie; only its SHA-256
  is stored. Logout revokes the row. Sessions expire.
- CSRF: per-session token, required in the X-CSRF-Token header on every state-changing request.
- Login throttling per client and globally. Failed logins never pause trading.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.db import AuthSession, LoginAttempt, User

PH = PasswordHasher()  # argon2id defaults
COOKIE = "orbit_session"
PER_CLIENT_FAILS = 5
GLOBAL_FAILS = 30
WINDOW = timedelta(minutes=15)
MIN_PASSWORD = 12


class AuthError(Exception):
    def __init__(self, msg: str, status: int = 401):
        super().__init__(msg)
        self.status = status


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_is_set(s: Session) -> bool:
    return s.scalar(select(func.count()).select_from(User)) > 0


def set_password(s: Session, password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if password.lower() in {"password1234", "orbittrading", "123456789012"}:
        raise ValueError("password too common")
    u = s.scalar(select(User))
    hashed = PH.hash(password)
    if u:
        u.password_hash = hashed
        u.password_changed_at = datetime.now().astimezone()
        for sess in s.scalars(select(AuthSession).where(AuthSession.revoked_at.is_(None))):
            sess.revoked_at = datetime.now().astimezone()
    else:
        s.add(User(password_hash=hashed))


def verify_password(s: Session, password: str) -> bool:
    u = s.scalar(select(User))
    if not u:
        return False
    try:
        PH.verify(u.password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    if PH.check_needs_rehash(u.password_hash):
        u.password_hash = PH.hash(password)
    return True


def throttled(s: Session, client: str, now: datetime) -> bool:
    since = now - WINDOW
    per = s.scalar(select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.client == client, LoginAttempt.success.is_(False), LoginAttempt.at >= since))
    glob = s.scalar(select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.success.is_(False), LoginAttempt.at >= since))
    return per >= PER_CLIENT_FAILS or glob >= GLOBAL_FAILS


def login(s: Session, password: str, client: str, now: datetime, hours: int) -> tuple[str, str]:
    if not password_is_set(s):
        raise AuthError("No password set. Run scripts/set-password.ps1 locally first.", 409)
    if throttled(s, client, now):
        s.add(LoginAttempt(at=now, client=client, success=False))
        raise AuthError("Too many failed attempts. Wait 15 minutes.", 429)
    ok = verify_password(s, password[:256])
    s.add(LoginAttempt(at=now, client=client, success=ok))
    if not ok:
        raise AuthError("Incorrect password.", 401)
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    s.add(AuthSession(token_hash=_h(token), csrf_token=csrf, created_at=now, last_auth_at=now,
                      expires_at=now + timedelta(hours=hours), client=client[:64]))
    return token, csrf


def get_session(s: Session, token: str | None, now: datetime) -> AuthSession | None:
    if not token or len(token) > 100:
        return None
    row = s.get(AuthSession, _h(token))
    if not row or row.revoked_at is not None or row.expires_at <= now:
        return None
    return row


def check_csrf(row: AuthSession, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(row.csrf_token, header)


def logout(s: Session, token: str | None, now: datetime) -> None:
    if token:
        row = s.get(AuthSession, _h(token))
        if row and row.revoked_at is None:
            row.revoked_at = now
