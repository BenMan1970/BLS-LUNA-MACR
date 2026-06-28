"""Typed data models for the BLUESTAR engine.

Plain ``dataclasses`` are used (no Pydantic dependency) to keep the install
footprint minimal and Python 3.10+ friendly. Every figure that reaches the HTML
carries a :class:`SourceStamp` describing its provenance and reliability so the
renderer can always emit either ``[Source | HH:MM CET | JJ/MM]`` or
``[N/A]`` / ``[PROXY]`` -- never an unsourced number.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .config import TZ_CET


class Reliability(str, Enum):
    """Provenance quality of a single datapoint."""

    PRIMARY = "primary"
    FALLBACK = "fallback"
    PROXY = "proxy"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SourceStamp:
    """Provenance of a single value.

    ``render()`` produces the bracketed citation used throughout the HTML.
    """

    source_name: str
    reliability: Reliability = Reliability.PRIMARY
    timestamp: Optional[datetime] = None
    url: Optional[str] = None
    note: str = ""

    def render(self) -> str:
        """Return the bracket tag shown in the briefing."""
        if self.reliability is Reliability.UNAVAILABLE:
            return "[N/A]"
        if self.reliability is Reliability.PROXY:
            label = self.source_name or "PROXY"
            return f"[PROXY · {label}]" if self.source_name else "[PROXY]"
        ts = self.timestamp
        if ts is not None:
            cet = ts.astimezone(TZ_CET)
            return f"[{self.source_name} | {cet:%H:%M} CET | {cet:%d/%m}]"
        return f"[{self.source_name}]"

    @property
    def ok(self) -> bool:
        """True when the value is usable (not unavailable)."""
        return self.reliability is not Reliability.UNAVAILABLE


def na_stamp(note: str = "") -> SourceStamp:
    """Shorthand for an unavailable datapoint."""
    return SourceStamp("", Reliability.UNAVAILABLE, note=note)


def proxy_stamp(source_name: str = "", note: str = "") -> SourceStamp:
    """Shorthand for a documented approximation."""
    return SourceStamp(source_name, Reliability.PROXY, note=note)


@dataclass
class Datum:
    """A single market value with its provenance.

    ``value`` is ``None`` when unavailable. ``display`` is the formatted string
    used in the HTML (e.g. ``"18,9"`` or ``"N/A"``).
    """

    value: Optional[float]
    stamp: SourceStamp
    display: str = "N/A"
    trend: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None and self.stamp.ok

    @property
    def is_proxy(self) -> bool:
        return self.stamp.reliability is Reliability.PROXY


@dataclass
class MacroEvent:
    """A high-impact calendar event (enriched by the Calendar Layer)."""

    currency: str
    event_name: str
    datetime_utc: str
    date_display: str
    time_display: str
    day_of_week: str
    impact: str
    forecast: str
    previous: str
    actual: str
    hours_until: float
    priority: str            # CRITICAL / HIGH / MEDIUM / PAST
    session: str
    pairs_affected: list[str] = field(default_factory=list)
    is_upcoming: bool = True

    @classmethod
    def from_enriched(cls, d: dict) -> "MacroEvent":
        return cls(
            currency=d.get("currency", ""),
            event_name=d.get("event_name", ""),
            datetime_utc=d.get("datetime_utc", ""),
            date_display=d.get("date_display", ""),
            time_display=d.get("time_display", ""),
            day_of_week=d.get("day_of_week", ""),
            impact=d.get("impact", "high"),
            forecast=d.get("forecast", "—"),
            previous=d.get("previous", "—"),
            actual=d.get("actual", "—"),
            hours_until=float(d.get("hours_until", 0.0)),
            priority=d.get("priority", "MEDIUM"),
            session=d.get("session", "OFF"),
            pairs_affected=list(d.get("pairs_affected", [])),
            is_upcoming=bool(d.get("is_upcoming", True)),
        )


@dataclass
class MarketSnapshot:
    """Snapshot of market gauges and prices, each a :class:`Datum`."""

    as_of_utc: datetime
    gauges: dict[str, Datum] = field(default_factory=dict)   # VIX, MOVE, DXY, US10Y, GDP...
    prices: dict[str, Datum] = field(default_factory=dict)   # instrument -> Datum
    atr: dict[str, float] = field(default_factory=dict)      # instrument -> 14d ATR (absolute)

    def gauge(self, key: str) -> Datum:
        return self.gauges.get(key, Datum(None, na_stamp(), "N/A"))

    def price(self, key: str) -> Datum:
        return self.prices.get(key, Datum(None, na_stamp(), "N/A"))


@dataclass
class CentralBankSnapshot:
    """Central bank policy state, split FACT vs INTERPRETATION."""

    name: str
    flag: str
    rate_display: str            # e.g. "3,50–3,75%"
    fact: str                    # FAIT: rate + next-decision probability (sourced)
    bias_interpretation: str     # BIAIS: hawkish/dovish/neutre + 1-line argument
    next_meeting: str            # date or [PROXY]/[N/A]
    stamp: SourceStamp = field(default_factory=na_stamp)
    # Optional probability bar (Fed only in the scaffold)
    pause_pct: Optional[int] = None
    cut_pct: Optional[int] = None
    hike_pct: Optional[int] = None


@dataclass
class CotPositioning:
    """Non-Commercials positioning for one currency (CFTC, J-3)."""

    currency: str
    net_contracts: Optional[int]
    ips_score: Optional[int]     # 0-100 [PROXY]
    ips_label: str               # Crowded / Normal / Capitulation
    delta_week: str              # qualitative + / - / stable
    momentum: str                # arrows
    stamp: SourceStamp = field(default_factory=na_stamp)

    @property
    def is_extreme(self) -> bool:
        return self.ips_score is not None and (self.ips_score >= 80 or self.ips_score <= 20)


@dataclass
class CurrencyStrength:
    """One row of the qualitative Currency Strength Ranking ([PROXY])."""

    currency: str
    score: int                   # 0-100 qualitative
    driver: str                  # 3-4 words
    css_class: str = "neutral"   # strong / neutral / weak


@dataclass
class AssetSetup:
    """A full asset card (Section 4) or a row in Section 1 / recap."""

    asset: str
    color: str                   # green / yellow / red
    bias: str                    # short text
    bias_class: str              # long / short / wait
    reason_short: str
    reason_macro: str
    conviction: int              # 1-5 stars (after adjustments)
    action: str                  # CHERCHER LONG / CHERCHER SHORT / ATTENDRE
    action_class: str            # long / short / wait
    arrow: str                   # ↑ / ↓ / ⏸
    # Levels (each with an origin tag)
    zone_buy: str = "[N/A]"
    origin_buy: str = "[N/A]"
    zone_sell: str = "[N/A]"
    origin_sell: str = "[N/A]"
    stop: str = "[N/A]"
    origin_stop: str = "[N/A]"
    expected_move: str = "[N/A]"
    em_method: str = "[N/A]"
    session: str = "—"
    session_reason: str = ""
    invalidation_risk: str = ""
    invalidation_level: str = "[N/A]"
    positioning_link: str = ""
    correlation_key: str = "[PROXY]"
    ips_summary: str = "[N/A]"
    squeeze_risk: str = "Faible"
    squeeze_class: str = "green"
    sizing_factor: str = "[N/A]"
    price_display: str = "[N/A]"
    levels_are_proxy: bool = False


@dataclass
class RiskScenario:
    """Bull / bear scenario with an anchored trigger."""

    title: str
    proba: str                   # "%" only if anchored, else qualitative
    trigger: str
    trigger_source: str
    rows: list[str] = field(default_factory=list)


@dataclass
class ValidationIssue:
    """A single finding from the validation engine."""

    rule: str
    severity: str                # ERROR / WARN / INFO
    message: str


@dataclass
class BriefingContext:
    """Everything the renderer needs to produce the final HTML."""

    generated_utc: datetime
    generated_cet: datetime
    is_live_session: bool
    operational_note: Optional[str]
    regime: str
    regime_class: str            # regime-on / regime-off / regime-mix
    regime_since: str
    market: MarketSnapshot
    central_banks: list[CentralBankSnapshot]
    diff_dominant: str
    diff_implication: str
    macro_theme: str
    macro_theme_src: str
    cot_summary: str
    cot_date: str
    squeeze_currency: Optional[str]
    dxy_context: str
    dxy_src: str
    vol_regime: str
    vol_implication: str
    correlation_summary: str
    liquidity_flow: str
    currency_strength: list[CurrencyStrength]
    ips_scores: list[CotPositioning]
    positioning_alert: Optional[str]
    catalysts_high: list[MacroEvent]
    catalysts_medium: list[MacroEvent]
    catalyst_scenarios: dict[str, dict]   # event_key -> beat/miss/advice/cons/prev
    priority_assets: list[AssetSetup]
    avoid_assets: list[tuple[str, str]]   # (asset, reason)
    no_setup_reason: Optional[str]
    risk_main: dict                       # desc/asset/level/proba/source
    bull: RiskScenario
    bear: RiskScenario
    invalidation_principal: str
    issues: list[ValidationIssue] = field(default_factory=list)
