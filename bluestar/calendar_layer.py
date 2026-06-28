"""Calendar Layer -- Forex Factory High-Impact feed (Data Integrity Layer).

This is a faithful refactor of the *validated* standalone calendar module. The
enrichment logic, field names, priority buckets, ``events_engine`` 72h residual
window and JSON contract are preserved **exactly** so the trusted module is not
broken -- only the Streamlit side effects were removed so the logic is
importable and unit-testable.

The engine reads ``events_engine`` in priority (future events + past events
inside the residual-risk window) and falls back to ``events`` if needed.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

import pytz
import requests

from .config import (
    FF_JSON_URL,
    HTTP_BACKOFF,
    HTTP_RETRIES,
    HTTP_TIMEOUT,
    RESIDUAL_RISK_WINDOW_H,
)

logger = logging.getLogger(__name__)

# Currency -> affected pairs (verbatim from the validated module).
PAIRS_MAP: Dict[str, List[str]] = {
    "USD": ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CAD", "AUD/USD", "NZD/USD", "USD/CHF"],
    "EUR": ["EUR/USD", "EUR/GBP", "EUR/JPY", "EUR/CHF", "EUR/CAD", "EUR/AUD", "EUR/NZD"],
    "GBP": ["GBP/USD", "EUR/GBP", "GBP/JPY", "GBP/CHF", "GBP/CAD", "GBP/AUD", "GBP/NZD"],
    "JPY": ["USD/JPY", "EUR/JPY", "GBP/JPY", "AUD/JPY", "NZD/JPY", "CAD/JPY", "CHF/JPY"],
    "CAD": ["USD/CAD", "EUR/CAD", "GBP/CAD", "AUD/CAD", "NZD/CAD", "CAD/JPY", "CAD/CHF"],
    "AUD": ["AUD/USD", "EUR/AUD", "GBP/AUD", "AUD/JPY", "AUD/CAD", "AUD/NZD", "AUD/CHF"],
    "NZD": ["NZD/USD", "EUR/NZD", "GBP/NZD", "NZD/JPY", "AUD/NZD", "NZD/CAD", "NZD/CHF"],
    "CHF": ["USD/CHF", "EUR/CHF", "GBP/CHF", "CHF/JPY", "AUD/CHF", "NZD/CHF", "CAD/CHF"],
    "CNY": ["USD/CNY", "EUR/CNY"],
}


def get_session(t: datetime) -> str:
    """Map a UTC datetime to its FX session label (verbatim logic)."""
    h = t.hour
    london, ny = 7 <= h < 16, 13 <= h < 22
    if london and ny:
        return "OVERLAP"
    if london:
        return "LONDON"
    if ny:
        return "NEW YORK"
    if 0 <= h < 9:
        return "ASIAN"
    return "OFF"


def fmt_until(h: float) -> str:
    """Human-readable countdown; ``h <= 0`` => ``PASSED`` (verbatim logic)."""
    if h <= 0:
        return "PASSED"
    total_min = int(h * 60)
    hh, mm = divmod(total_min, 60)
    if hh == 0:
        return f"{mm}m"
    if hh < 24:
        return f"{hh}h {mm}m"
    return f"{hh // 24}d {hh % 24}h"


def fetch_raw(url: str = FF_JSON_URL) -> List[Dict]:
    """Fetch the Forex Factory JSON with timeout + retry/backoff.

    Returns an empty list on any failure (the engine then degrades to a
    no-calendar state rather than crashing).
    """
    last_err: Optional[Exception] = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:  # noqa: PERF203
            last_err = e
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_BACKOFF ** (attempt + 1))
    logger.error("Calendar fetch failed after retries: %s", last_err)
    return []


def enrich(event: Dict, event_time_ref: datetime) -> Optional[Dict]:
    """Enrich one raw event into the canonical dict (verbatim field set)."""
    try:
        t = datetime.fromisoformat(event.get("date", "").replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = pytz.UTC.localize(t)
        h = (t - event_time_ref).total_seconds() / 3600
        ccy = event.get("country", "")
        prio = (
            "PAST" if h <= 0
            else "CRITICAL" if h <= 6
            else "HIGH" if h <= 48
            else "MEDIUM"
        )
        return {
            "currency": ccy,
            "event_name": event.get("title", "").strip(),
            "datetime_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date_display": t.strftime("%Y-%m-%d"),
            "time_display": t.strftime("%H:%M UTC"),
            "day_of_week": t.strftime("%A").upper(),
            "impact": (event.get("impact") or "High").lower(),
            "forecast": event.get("forecast", "") or "—",
            "previous": event.get("previous", "") or "—",
            "actual": event.get("actual", "") or "—",
            "hours_until": round(h, 2),
            "hours_until_display": fmt_until(h),
            "is_upcoming": h > 0,
            "priority": prio,
            "session": get_session(t),
            "pairs_affected": PAIRS_MAP.get(ccy, []),
        }
    except (ValueError, KeyError, AttributeError) as e:
        logger.warning("Skip event: %s", e)
        return None


def build_calendar(now_utc: Optional[datetime] = None,
                   raw_data: Optional[List[Dict]] = None) -> Dict:
    """Build the canonical calendar payload.

    Parameters
    ----------
    now_utc:
        Reference time (defaults to ``datetime.now(UTC)``).
    raw_data:
        Pre-fetched raw events (used by tests). If ``None`` the feed is fetched.

    Returns a dict with ``metadata``, ``events`` (all upcoming high-impact),
    ``events_engine`` (future + past within 72h) and ``summary_by_day`` -- the
    same contract the engine consumes.
    """
    now_utc = now_utc or datetime.now(pytz.UTC)
    if raw_data is None:
        raw_data = fetch_raw()

    all_events = [
        e for ev in raw_data
        if ev.get("impact") == "High"
        for e in [enrich(ev, now_utc)] if e
    ]
    all_events.sort(key=lambda x: (not x["is_upcoming"], x["datetime_utc"]))

    daily: Dict[str, List[str]] = defaultdict(list)
    for ev in all_events:
        daily[ev["datetime_utc"][:10]].append(f"{ev['currency']} – {ev['event_name']}")
    summary_by_day = dict(sorted(daily.items()))

    events_engine = [
        e for e in all_events
        if e["is_upcoming"] or e["hours_until"] >= -RESIDUAL_RISK_WINDOW_H
    ]
    upcoming = [e for e in all_events if e["is_upcoming"]]

    return {
        "metadata": {
            "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "Forex Factory Official JSON",
            "timezone": "UTC",
            "total_high_impact": len(all_events),
            "upcoming_count": len(upcoming),
            "critical_count": sum(1 for e in all_events if e["priority"] == "CRITICAL"),
            "engine_events_count": len(events_engine),
            "reachable": bool(raw_data),
        },
        "events": upcoming,
        "events_engine": events_engine,
        "summary_by_day": summary_by_day,
    }
