"""Exchange calendar (NYSE / XNYS).

Never hardcode market hours in Dubai time. Sessions, holidays, early closes and
DST all come from the exchange calendar. Live operation additionally confirms
the session is open with the broker clock before submitting an order.

The ``exchange_calendars`` parse helpers are incompatible with the installed
pandas datetime64[us] index, so this module reads the precomputed schedule table.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xc

UTC = timezone.utc
DUBAI = ZoneInfo("Asia/Dubai")
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Session:
    day: date
    open_utc: datetime
    close_utc: datetime

    @property
    def early_close(self) -> bool:
        return self.close_utc.astimezone(NEW_YORK).hour < 16

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "open_utc": self.open_utc.isoformat(),
            "close_utc": self.close_utc.isoformat(),
            "open_dubai": self.open_utc.astimezone(DUBAI).isoformat(),
            "close_dubai": self.close_utc.astimezone(DUBAI).isoformat(),
            "open_new_york": self.open_utc.astimezone(NEW_YORK).isoformat(),
            "close_new_york": self.close_utc.astimezone(NEW_YORK).isoformat(),
            "early_close": self.early_close,
        }


class MarketCalendar:
    def __init__(self, sessions: list[Session], source: str):
        self.sessions = sorted(sessions, key=lambda s: s.day)
        self._by_day = {s.day: s for s in self.sessions}
        self.source = source

    # --- queries
    def is_session(self, d: date) -> bool:
        return d in self._by_day

    def session(self, d: date) -> Session | None:
        return self._by_day.get(d)

    def next_session(self, d: date) -> Session | None:
        for s in self.sessions:
            if s.day > d:
                return s
        return None

    def previous_session(self, d: date) -> Session | None:
        prev = None
        for s in self.sessions:
            if s.day >= d:
                break
            prev = s
        return prev

    def latest_completed_session(self, now: datetime, buffer_minutes: int = 15) -> Session | None:
        """Most recent session whose close + buffer is at or before ``now``."""
        assert now.tzinfo is not None, "timestamps must be timezone-aware"
        best = None
        for s in self.sessions:
            if s.close_utc + timedelta(minutes=buffer_minutes) <= now:
                best = s
            else:
                break
        return best

    def session_containing(self, now: datetime) -> Session | None:
        for s in self.sessions:
            if s.open_utc <= now < s.close_utc:
                return s
        return None

    def sessions_between(self, start: date, end: date) -> list[Session]:
        return [s for s in self.sessions if start <= s.day <= end]


@lru_cache(maxsize=1)
def xnys_calendar(start: str = "2015-01-01", end: str = "2028-12-31") -> MarketCalendar:
    cal = xc.get_calendar("XNYS", start=start, end=end)
    sched = cal.schedule
    sessions = []
    for idx, row in sched.iterrows():
        d = idx.date()
        o = row["open"].to_pydatetime().astimezone(UTC)
        c = row["close"].to_pydatetime().astimezone(UTC)
        sessions.append(Session(d, o, c))
    return MarketCalendar(sessions, source=f"exchange_calendars {xc.__version__} XNYS (offline)")


def now_utc() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        # SQLite drops tzinfo; values are always stored as UTC.
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
