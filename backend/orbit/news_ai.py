"""AI news reader: FinBERT scores each SPY / GLD headline as positive, negative or neutral.

It only *warns*: news never triggers or blocks an order. When an unusual number of strongly negative
headlines about SPY or gold appear within 24 hours, Orbit sends a "news warning" to the alerts bell (and
Telegram when it is set up), at most once per fund per Dubai day.

FinBERT (ProsusAI/finbert, a model trained on financial text) runs on this computer. It is optional:
install it with ``uv sync --extra news`` and ``uv run python -m orbit.cli news-model download``.
Without it, the News page simply shows headlines without scores.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy.orm import Session

from orbit.db import KV
from orbit.market_calendar import DUBAI, UTC

log = logging.getLogger(__name__)

MODEL_REPO = "ProsusAI/finbert"
MODEL_FILES = ["config.json", "vocab.txt", "tokenizer_config.json", "special_tokens_map.json", "pytorch_model.bin"]
SCORES_KEY = "news_scores"
MOOD_KEY = "news_mood"
EVERY_MINUTES = 15
KEEP_SCORES = 400
WINDOW_HOURS = 24
# Warning rule: in the last 24 hours, at least WARN_MIN_HEADLINES about the fund, of which at least
# WARN_MIN_STRONG are strongly negative (FinBERT negative >= STRONG), and the average mood <= WARN_MOOD.
STRONG = 0.80
WARN_MIN_HEADLINES = 6
WARN_MIN_STRONG = 4
WARN_MOOD = -0.30
FUNDS = {"SPY": "SPY (US stocks)", "GLD": "gold (GLD)"}


class Scorer(Protocol):
    def score(self, texts: list[str]) -> list[dict[str, float]]: ...


def model_dir(data_dir: Path) -> Path:
    return data_dir / "models" / "finbert"


def model_installed(data_dir: Path) -> bool:
    d = model_dir(data_dir)
    return all((d / f).exists() for f in MODEL_FILES)


def download(data_dir: Path) -> Path:
    """One-time download of FinBERT (about 440 MB) from Hugging Face into data/models/finbert."""
    from huggingface_hub import hf_hub_download
    d = model_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    for f in MODEL_FILES:
        hf_hub_download(MODEL_REPO, f, local_dir=d)
    return d


class FinBert:
    """Loads lazily on first use; the model stays in memory afterwards (about 0.5 GB)."""

    def __init__(self, data_dir: Path):
        self.path = model_dir(data_dir)
        self._pipe = None

    def score(self, texts: list[str]) -> list[dict[str, float]]:
        if self._pipe is None:
            from transformers import pipeline
            self._pipe = pipeline("text-classification", model=str(self.path), tokenizer=str(self.path), top_k=None, device=-1)
        out = []
        for res in self._pipe(texts, truncation=True, max_length=128, batch_size=16):
            d = {r["label"].lower(): float(r["score"]) for r in res}
            out.append({k: round(d.get(k, 0.0), 4) for k in ("positive", "negative", "neutral")})
        return out


def label(sc: dict) -> str:
    return max(("positive", "negative", "neutral"), key=lambda k: sc.get(k, 0.0))


def get_scores(s: Session) -> dict:
    row = s.get(KV, SCORES_KEY)
    return dict(row.value) if row else {}


def get_mood(s: Session) -> dict:
    row = s.get(KV, MOOD_KEY)
    return dict(row.value) if row else {"available": False}


def _put(s: Session, key: str, value: dict) -> None:
    row = s.get(KV, key)
    if row:
        row.value = value
    else:
        s.add(KV(key=key, value=value))


def mood(scores: dict, now: datetime) -> dict:
    """Per fund over the last 24 hours: headline count, average mood (-1 very negative to +1 very positive)."""
    since = now - timedelta(hours=WINDOW_HOURS)
    out = {}
    for fund in FUNDS:
        rows = [v for v in scores.values() if fund in v.get("symbols", []) and v.get("created_at")
                and datetime.fromisoformat(v["created_at"]) >= since]
        n = len(rows)
        avg = sum(r["positive"] - r["negative"] for r in rows) / n if n else 0.0
        strong = [r for r in rows if r["negative"] >= STRONG]
        out[fund] = {"headlines": n, "mood": round(avg, 3), "strong_negative": len(strong),
                     "positive": sum(1 for r in rows if label(r) == "positive"),
                     "negative": sum(1 for r in rows if label(r) == "negative"),
                     "neutral": sum(1 for r in rows if label(r) == "neutral"),
                     "warn": n >= WARN_MIN_HEADLINES and len(strong) >= WARN_MIN_STRONG and avg <= WARN_MOOD,
                     "worst": [r["headline"] for r in sorted(strong, key=lambda r: -r["negative"])[:3]]}
    return out


def quiet_time(calendar, now: datetime) -> bool:
    """Stay out of the way around the open (orders go in) and just after the close (decisions are made)."""
    for day in (now.astimezone(UTC).date(), (now - timedelta(days=1)).astimezone(UTC).date()):
        sess = calendar.session(day)
        if not sess:
            continue
        if sess.open_utc - timedelta(minutes=10) <= now <= sess.open_utc + timedelta(minutes=15):
            return True
        if sess.close_utc <= now <= sess.close_utc + timedelta(minutes=40):
            return True
    return False


def maybe_update(s: Session, settings, calendar, now: datetime, fetch_news: Callable[[], list[dict]], scorer: Scorer) -> bool:
    """Every 15 minutes: score new headlines, update the mood, and warn on an unusually negative day."""
    from orbit.notify import emit
    prev = get_mood(s)
    last = prev.get("updated_at")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=EVERY_MINUTES):
        return False
    if quiet_time(calendar, now):
        return False
    scores = get_scores(s)
    items = fetch_news()
    new = [n for n in items if str(n["id"]) not in scores and n.get("headline")]
    if new:
        texts = [n["headline"] if not n.get("summary") else f"{n['headline']}. {n['summary']}"[:600] for n in new]
        for n, sc in zip(new, scorer.score(texts)):
            scores[str(n["id"])] = {**sc, "label": label(sc), "headline": n["headline"], "created_at": n.get("created_at"),
                                    "symbols": [x for x in n.get("symbols", []) if x in FUNDS]}
        if len(scores) > KEEP_SCORES:
            keep = sorted(scores.items(), key=lambda kv: kv[1].get("created_at") or "")[-KEEP_SCORES:]
            scores = dict(keep)
        _put(s, SCORES_KEY, scores)
    m = mood(scores, now)
    _put(s, MOOD_KEY, {"available": True, "updated_at": now.astimezone(UTC).isoformat(), "funds": m,
                       "model": MODEL_REPO, "scored": len(scores)})
    today = now.astimezone(DUBAI).date().isoformat()
    for fund, v in m.items():
        if v["warn"]:
            body = "\n".join([
                f"⚠️ Unusually negative news about {FUNDS[fund]} in the last 24 hours:",
                f"{v['strong_negative']} of {v['headlines']} headlines read as strongly negative (average mood {v['mood']:+.2f} on a -1 to +1 scale).",
                "", "Most negative headlines:", *[f"• {h}" for h in v["worst"]], "",
                "Orbit does not trade on news. This is only a heads-up; the strategies keep following their rules.",
                "By the time a headline arrives, prices have usually already moved.",
            ])
            emit(s, "news_warning", "warning", f"News warning: {fund}", body, f"news_warning:{fund}:{today}", settings=settings,
                 public_summary=f"Orbit Trading Lab: unusually negative {fund} news today. Open the app to read it.")
    return True
