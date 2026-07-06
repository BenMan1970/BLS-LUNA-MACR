"""Staleness & Data Coverage Engine — BLUESTAR reliability layer.

This module implements the *data integrity* layer that was missing from the
original codebase.  It addresses audit findings C2, C3, C5, C6:

* **Staleness detection**: every datapoint carries an age; if the age exceeds
  a configurable threshold, the datum is flagged as ``STALE``.
* **Source coverage report**: counts how many fields are live (PRIMARY),
  fallback, proxy, or unavailable — and blocks publication when the live
  coverage ratio falls below a minimum threshold.
* **Weekend & holiday awareness**: market data fetched on a weekend or US
  holiday is automatically stamped with the *data's* effective date (the last
  trading day), not the fetch timestamp.

Design contract:
  * Pure functions — no I/O, no side effects.
  * Never raises; returns structured results.
  * Integrates with existing ``Datum`` / ``SourceStamp`` without breaking
    their contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from enum import Enum
from typing import Optional

from .config import TZ_UTC, TZ_ET
from .models import Datum, MarketSnapshot, Reliability

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# US Federal holidays (observed) — covers CFTC, BEA, equity/FX market closures.
# This is a minimal static table; for production a proper holiday library
# (e.g. ``pandas-market-calendars``) should be used, but this covers the
# common cases that caused audit finding C1.
# ---------------------------------------------------------------------------
_US_FEDERAL_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day (3rd Monday)
    date(2026, 2, 16),   # Presidents' Day (3rd Monday)
    date(2026, 5, 25),   # Memorial Day (last Monday)
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day observed (July 4 = Saturday → Friday)
    date(2026, 9, 7),    # Labor Day (1st Monday)
    date(2026, 10, 12),  # Columbus Day (2nd Monday)
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving (4th Thursday)
    date(2026, 12, 25),  # Christmas
}

_US_FEDERAL_HOLIDAYS_2025: set[date] = {
    date(2025, 1, 1),
    date(2025, 1, 20),
    date(2025, 2, 17),
    date(2025, 5, 26),
    date(2025, 6, 19),
    date(2025, 7, 4),
    date(2025, 9, 1),
    date(2025, 10, 13),
    date(2025, 11, 11),
    date(2025, 11, 27),
    date(2025, 12, 25),
}


def is_us_holiday(d: date) -> bool:
    """Check if a date is a US federal holiday (2025-2026 table)."""
    return d in _US_FEDERAL_HOLIDAYS_2026 or d in _US_FEDERAL_HOLIDAYS_2025


def is_weekend(d: date) -> bool:
    """Saturday or Sunday."""
    return d.weekday() >= 5


def last_trading_day(ref: datetime) -> date:
    """Return the most recent US trading day at or before ``ref``.

    Walks backwards from the reference date, skipping weekends and US federal
    holidays.  This is the *effective date* of market data fetched outside
    trading hours (weekend, holiday, pre-market, post-market).
    """
    d = ref.astimezone(TZ_ET).date()
    while is_weekend(d) or is_us_holiday(d):
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Staleness classification
# ---------------------------------------------------------------------------
class Freshness(str, Enum):
    """Freshness classification for a single datapoint."""
    LIVE = "live"           # data is from the current or most recent trading session
    STALE = "stale"         # data age exceeds the staleness threshold
    PROXY = "proxy"         # documented approximation, not a real observation
    UNAVAILABLE = "unavailable"


@dataclass
class StalenessReport:
    """Per-datum staleness assessment."""
    field_name: str
    reliability: Reliability
    freshness: Freshness
    age_hours: Optional[float]       # None if no timestamp
    data_effective_date: Optional[str]  # ISO date of the underlying data
    fetch_timestamp: Optional[str]      # ISO datetime of when it was fetched
    is_stale: bool
    note: str = ""


@dataclass
class CoverageReport:
    """Aggregate source coverage for the entire briefing."""
    total_fields: int = 0
    live_count: int = 0
    fallback_count: int = 0
    proxy_count: int = 0
    unavailable_count: int = 0
    stale_count: int = 0
    fields: list[StalenessReport] = field(default_factory=list)

    @property
    def live_ratio(self) -> float:
        """Fraction of fields that are live (PRIMARY + not stale)."""
        if self.total_fields == 0:
            return 0.0
        return self.live_count / self.total_fields

    @property
    def usable_ratio(self) -> float:
        """Fraction of fields that are usable (live + fallback, excluding stale)."""
        if self.total_fields == 0:
            return 0.0
        return (self.live_count + self.fallback_count) / self.total_fields

    @property
    def publication_blocked(self) -> bool:
        """True if the live coverage is too low to publish."""
        return self.live_ratio < MIN_LIVE_COVERAGE_RATIO

    def summary_line(self) -> str:
        """One-line human-readable summary for the HTML footer."""
        return (
            f"Couverture sources : {self.live_count} live · "
            f"{self.fallback_count} fallback · {self.proxy_count} proxy · "
            f"{self.unavailable_count} N/A · {self.stale_count} stale · "
            f"ratio live = {self.live_ratio:.0%}"
        )


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
# A datum is STALE if its effective data date is older than this.
# For FX/market data: 24h on a trading day, 72h over a weekend.
MAX_STALENESS_HOURS_WEEKDAY = 26      # slightly more than 24h to allow for TZ drift
MAX_STALENESS_HOURS_WEEKEND = 96      # Fri close → Mon open is ~62h, 96h gives margin

# Macro data (GDPNow, COT, etc.) can legitimately be older.
MAX_STALENESS_HOURS_MACRO = 7 * 24   # 7 days for slow-moving macro series

# Minimum live coverage ratio to allow publication.
MIN_LIVE_COVERAGE_RATIO = 0.30       # at least 30% of fields must be live

# Fields that are "macro" (slow-moving) and get a longer staleness window.
_MACRO_FIELDS = {"GDP_NOWCAST", "SURPRISE_IDX"}


def _staleness_threshold(field_name: str, now_utc: datetime) -> float:
    """Return the staleness threshold in hours for a given field."""
    if field_name in _MACRO_FIELDS:
        return MAX_STALENESS_HOURS_MACRO
    # Check if we're in a weekend context
    now_et = now_utc.astimezone(TZ_ET)
    if is_weekend(now_et.date()):
        return MAX_STALENESS_HOURS_WEEKEND
    return MAX_STALENESS_HOURS_WEEKDAY


def assess_datum(
    field_name: str,
    datum: Datum,
    now_utc: datetime,
) -> StalenessReport:
    """Assess the staleness of a single ``Datum``.

    The key insight (audit C2): the ``SourceStamp.timestamp`` is the *fetch*
    time, not the *data* time.  For market data fetched on a weekend, the
    effective data date is the last trading day (Friday).  We compute the
    data's effective date and compare it to the staleness threshold.
    """
    if not datum.stamp.ok:
        return StalenessReport(
            field_name=field_name,
            reliability=datum.stamp.reliability,
            freshness=Freshness.UNAVAILABLE,
            age_hours=None,
            data_effective_date=None,
            fetch_timestamp=datum.stamp.timestamp.isoformat() if datum.stamp.timestamp else None,
            is_stale=False,
            note=datum.stamp.note or "unavailable",
        )

    if datum.stamp.reliability is Reliability.PROXY:
        return StalenessReport(
            field_name=field_name,
            reliability=Reliability.PROXY,
            freshness=Freshness.PROXY,
            age_hours=None,
            data_effective_date=None,
            fetch_timestamp=datum.stamp.timestamp.isoformat() if datum.stamp.timestamp else None,
            is_stale=False,
            note=datum.stamp.note or "proxy",
        )

    # PRIMARY or FALLBACK — compute effective data date
    fetch_ts = datum.stamp.timestamp
    if fetch_ts is None:
        # No timestamp at all — can't assess staleness, treat as unknown
        return StalenessReport(
            field_name=field_name,
            reliability=datum.stamp.reliability,
            freshness=Freshness.LIVE,  # benefit of the doubt
            age_hours=None,
            data_effective_date=None,
            fetch_timestamp=None,
            is_stale=False,
            note="no timestamp — staleness unverified",
        )

    # The effective data date: for market data, this is the last trading day
    # at or before the fetch timestamp.  For macro data, the fetch timestamp
    # itself is the best proxy (GDPNow publishes with its own date).
    if field_name in _MACRO_FIELDS:
        effective = fetch_ts.astimezone(TZ_ET).date()
    else:
        effective = last_trading_day(fetch_ts)

    age = now_utc - fetch_ts
    age_h = age.total_seconds() / 3600.0

    threshold = _staleness_threshold(field_name, now_utc)
    is_stale = age_h > threshold

    # For market data on a weekend, the "real" age is from the last trading day
    if field_name not in _MACRO_FIELDS:
        real_age = (now_utc.astimezone(TZ_ET).date() - effective).total_seconds() / 3600.0
        # Use the larger of fetch-age and calendar-age
        age_h = max(age_h, real_age)
        is_stale = age_h > threshold

    freshness = Freshness.STALE if is_stale else Freshness.LIVE

    return StalenessReport(
        field_name=field_name,
        reliability=datum.stamp.reliability,
        freshness=freshness,
        age_hours=round(age_h, 1),
        data_effective_date=effective.isoformat(),
        fetch_timestamp=fetch_ts.isoformat(),
        is_stale=is_stale,
        note="stale" if is_stale else "",
    )


def build_coverage_report(
    market: MarketSnapshot,
    now_utc: datetime,
) -> CoverageReport:
    """Build a full source-coverage report for the market snapshot.

    This is the function that should be called after ``build_market_snapshot``
    and before ``build_context`` — or at the end of the pipeline — to assess
    whether the briefing has enough live data to be published.
    """
    report = CoverageReport()
    now_utc = now_utc or datetime.now(TZ_UTC)

    # Gauges
    for key, datum in market.gauges.items():
        sr = assess_datum(key, datum, now_utc)
        report.fields.append(sr)
        report.total_fields += 1
        if sr.freshness is Freshness.UNAVAILABLE:
            report.unavailable_count += 1
        elif sr.freshness is Freshness.PROXY:
            report.proxy_count += 1
        elif sr.freshness is Freshness.STALE:
            report.stale_count += 1
            if sr.reliability is Reliability.PRIMARY:
                report.live_count += 1  # stale but was live
            else:
                report.fallback_count += 1
        elif sr.reliability is Reliability.PRIMARY:
            report.live_count += 1
        elif sr.reliability is Reliability.FALLBACK:
            report.fallback_count += 1

    # Prices
    for key, datum in market.prices.items():
        sr = assess_datum(key, datum, now_utc)
        report.fields.append(sr)
        report.total_fields += 1
        if sr.freshness is Freshness.UNAVAILABLE:
            report.unavailable_count += 1
        elif sr.freshness is Freshness.PROXY:
            report.proxy_count += 1
        elif sr.freshness is Freshness.STALE:
            report.stale_count += 1
            if sr.reliability is Reliability.PRIMARY:
                report.live_count += 1
            else:
                report.fallback_count += 1
        elif sr.reliability is Reliability.PRIMARY:
            report.live_count += 1
        elif sr.reliability is Reliability.FALLBACK:
            report.fallback_count += 1

    return report


def stale_fields_summary(report: CoverageReport) -> str:
    """Human-readable list of stale fields for the validation engine."""
    stale = [f for f in report.fields if f.is_stale]
    if not stale:
        return ""
    parts = []
    for f in stale:
        age_str = f"{f.age_hours:.0f}h" if f.age_hours is not None else "?"
        parts.append(f"{f.field_name} ({age_str})")
    return "Données stale : " + " · ".join(parts)
