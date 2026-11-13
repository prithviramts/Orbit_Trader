"""Versioned loss-review overlay (experimental paper-research defaults).

Close-to-close account equity drawdown from the reconciled high-water mark,
including unrealised losses.
- drawdown >= warn_fraction  -> review warning (once per episode)
- drawdown >= pause_fraction -> latch RISK_PAUSED_ENTRIES (new buys blocked; sells continue)
Thresholds describe when checks trigger. They are not loss limits: prices can gap
far past a threshold before the next close is observed. The latch never resets
itself; resuming needs an authenticated review (and an explicit override if still
at/above the blocking threshold). The high-water mark is never silently reset.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from orbit.money import D

RISK_POLICY_NAME = "drawdown_review_v1"


@dataclass(frozen=True)
class RiskPolicy:
    warn_fraction: Decimal = Decimal("0.05")
    pause_fraction: Decimal = Decimal("0.10")
    version_label: str = RISK_POLICY_NAME

    def validate(self) -> None:
        if not (Decimal("0.01") <= self.warn_fraction < self.pause_fraction <= Decimal("0.5")):
            raise ValueError("need 1% <= warn < pause <= 50%")

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


@dataclass
class DrawdownState:
    high_water_mark: Decimal | None = None
    last_equity: Decimal | None = None
    drawdown: Decimal = Decimal(0)
    warned: bool = False
    entries_paused: bool = False

    def to_dict(self) -> dict:
        return {
            "high_water_mark": None if self.high_water_mark is None else str(self.high_water_mark),
            "last_equity": None if self.last_equity is None else str(self.last_equity),
            "drawdown": str(self.drawdown),
            "warned": self.warned,
            "entries_paused": self.entries_paused,
        }


def update_drawdown(state: DrawdownState, equity: Decimal, policy: RiskPolicy) -> list[str]:
    """Feed one close-to-close equity observation. Returns events: WARN, PAUSE."""
    eq = D(equity)
    if eq <= 0:
        raise ValueError("equity must be positive")
    events: list[str] = []
    if state.high_water_mark is None or eq > state.high_water_mark:
        state.high_water_mark = eq
    state.last_equity = eq
    state.drawdown = (state.high_water_mark - eq) / state.high_water_mark
    if state.drawdown < policy.warn_fraction / 2:
        # Hysteresis: a new warning episode needs a real recovery, so hovering near the
        # threshold does not spam alerts. The entry latch is NOT reset here.
        state.warned = False
    elif state.drawdown >= policy.warn_fraction and not state.warned:
        state.warned = True
        events.append("WARN")
    if state.drawdown >= policy.pause_fraction and not state.entries_paused:
        state.entries_paused = True
        events.append("PAUSE")
    return events
