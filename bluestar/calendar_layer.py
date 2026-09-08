"""Calendar Layer -- Forex Factory High-Impact feed (Data Integrity Layer).

Version 4 -- consolidation mono-fichier pour compatibilité avec l'app
d'analyse technique (BLUESTAR), qui utilise ``calendar_core.py`` +
``calendar_ingestor.py`` + ``app.py`` (Streamlit) en environnement séparé
(pas de disque partagé, donc pas d'artefact JSON commun possible).

------------------------------------------------------------------
POURQUOI CE FICHIER EXISTE :
------------------------------------------------------------------
Les deux apps (macro et TA) ne partagent ni process ni disque. Pour qu'elles
"voient" le même calendrier de la même façon, il ne suffit pas de partager un
fichier de sortie : il faut que les DEUX embarquent EXACTEMENT la même
logique de normalisation. Ce fichier reprend donc intégralement la logique
de ``calendar_core.py`` (Pydantic, sessions FX DST-aware via zoneinfo,
scoring de qualité, parsing numérique explicite, regroupement des
publications liées) -- c'est la même version que consomme l'app TA via
``to_legacy_payload()``.

------------------------------------------------------------------
BUG ÉVITÉ (lu avant d'écrire une seule ligne de ce fichier) :
------------------------------------------------------------------
``to_legacy_payload()`` de calendar_core.py NE PRODUIT PAS le champ
``priority`` (CRITICAL/HIGH/MEDIUM/PAST) -- seulement ``time_proximity``
(IMMINENT/SOON/LATER/PAST). Or ``priority`` est le champ de DÉCISION dont
dépendent ``macro_engine.py`` :

    determine_market_regime() : imminent = any(e.priority == "CRITICAL" ...)
    _compute_asset_score()    : catalyst_pen = 0.15 * len([e for e in ev
                                 if e.priority == "HIGH"])
    build_catalysts()         : high   = [... e.priority in ("CRITICAL","HIGH") ...]
                                 medium = [... e.priority == "MEDIUM" ...]

Brancher calendar_core tel quel aurait reproduit -- par omission cette
fois, plus par renommage -- l'incident du 05/08/2026 ("Catalyseurs du Jour"
vide). ``build_calendar()`` ci-dessous restaure donc ``priority`` de façon
EXPLICITE et ADDITIVE dans la sortie legacy, sur les mêmes seuils que
``time_proximity`` (≤0h PAST, ≤6h CRITICAL, ≤48h HIGH, sinon MEDIUM) --
``macro_engine.py`` n'a besoin d'AUCUNE modification.

------------------------------------------------------------------
CE QUI RESTE INCHANGÉ (hors périmètre de calendar_core) :
------------------------------------------------------------------
La logique de tiers de blackout (``TIER_WINDOWS`` / ``classify_tier`` /
``is_blackout``) reste une réplique exacte de ``v10.py`` (Desk Engine), qui
fait autorité sur cette règle. C'est un sujet ORTHOGONAL à la normalisation
d'événements de calendar_core (l'app TA n'a pas cette notion) -- ne jamais
fusionner les deux logiques. Dette de duplication documentée et assumée en
attendant l'extraction en module commun (``bluestar_shared.calendar_rules``).

------------------------------------------------------------------
CE QUE CE FICHIER N'EST PAS :
------------------------------------------------------------------
Ce n'est PAS l'ingestor de production (pas de circuit breaker, pas de
last-known-good sur disque, pas de health.json) -- choix explicite pour
rester simple, puisque l'app macro n'a pas de disque partagé avec l'app TA
de toute façon. Le fetch reste résilient (retries + backoff urllib3,
identiques à calendar_ingestor.py) mais sans aucun état persistant entre
deux appels : en cas d'échec réseau, ``build_calendar()`` reçoit une liste
vide et se dégrade proprement (comme la v3), il ne lève pas d'exception.

Si un jour les deux apps partagent un disque, il vaudra mieux lire
directement ``calendar.legacy.json`` produit par le vrai
``calendar_ingestor.py`` plutôt que de dupliquer le fetch ici.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests

# COMPAT-FIX (audit de câblage, câblage v4 -> reste de l'app) : ce module
# n'importait plus rien de config.py (contrairement à la v3), ce qui a fait
# dériver silencieusement MACRO_RESIDUAL_RISK_WINDOW_H (48.0 codé en dur, voir
# commentaire "ASSOMPTION À VÉRIFIER" plus bas) de la vraie valeur de
# référence, config.RESIDUAL_RISK_WINDOW_H = 72. Réimport minimal pour
# retrouver la source unique de vérité -- ne change rien d'autre au fichier.
from . import config as _C
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# =============================================================================
# SECTION 1 -- NORMALISATION (identique à calendar_core.py, source unique de
# vérité partagée avec l'app TA). Ne PAS diverger de calendar_core.py sans
# répercuter le changement ici, sous peine de désynchroniser les deux apps.
# =============================================================================

SCHEMA_VERSION = "2.0.0"
PAIR_MAPPING_METHOD = "static_currency_membership_v1"
SESSION_POLICY_VERSION = "exchange_local_dst_aware_v1"
NUMERIC_PARSER_VERSION = "ff_numeric_v1"

UTC = timezone.utc

TZ_LONDON = ZoneInfo("Europe/London")
TZ_NEW_YORK = ZoneInfo("America/New_York")
TZ_TOKYO = ZoneInfo("Asia/Tokyo")
DEFAULT_DISPLAY_TZ = "Africa/Casablanca"

SESSION_HOURS = {
    "LONDON": (TZ_LONDON, 8 * 60, 16 * 60 + 30),
    "NEW_YORK": (TZ_NEW_YORK, 8 * 60, 17 * 60),
    "TOKYO": (TZ_TOKYO, 8 * 60, 17 * 60),
}

G10_PAIRS: Tuple[str, ...] = (
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD",
    "EUR/GBP", "EUR/JPY", "EUR/CHF", "EUR/CAD", "EUR/AUD", "EUR/NZD",
    "GBP/JPY", "GBP/CHF", "GBP/CAD", "GBP/AUD", "GBP/NZD",
    "AUD/JPY", "AUD/CHF", "AUD/CAD", "AUD/NZD",
    "NZD/JPY", "NZD/CHF", "NZD/CAD",
    "CAD/JPY", "CAD/CHF", "CHF/JPY",
)
EXTRA_PAIRS: Dict[str, Tuple[str, ...]] = {"CNY": ("USD/CNY", "EUR/CNY")}

KNOWN_CURRENCIES: Tuple[str, ...] = (
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF", "CNY",
)
GLOBAL_COUNTRY_TOKENS = {"ALL", "GLOBAL", "WORLD", ""}


def pairs_for_currency(ccy: str) -> List[str]:
    """Appartenance mécanique de la devise à la paire. Aucune notion de causalité."""
    ccy = ccy.upper()
    out = [p for p in G10_PAIRS if ccy in p.split("/")]
    out.extend(p for p in EXTRA_PAIRS.get(ccy, ()) if p not in out)
    return out


class Impact(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    HOLIDAY = "HOLIDAY"
    UNKNOWN = "UNKNOWN"


class Session(str, Enum):
    ASIAN = "ASIAN"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OVERLAP_ASIA_LONDON = "OVERLAP_ASIA_LONDON"
    OVERLAP_LONDON_NY = "OVERLAP_LONDON_NY"
    OFF = "OFF"


class TimeProximity(str, Enum):
    IMMINENT = "IMMINENT"
    SOON = "SOON"
    LATER = "LATER"
    PAST = "PAST"


class EventStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    DUE = "DUE"
    PAST_SCHEDULE = "PAST_SCHEDULE"
    HOLIDAY = "HOLIDAY"


class ActualStatus(str, Enum):
    UNSUPPORTED_BY_SOURCE = "UNSUPPORTED_BY_SOURCE"
    NOT_YET_RELEASED = "NOT_YET_RELEASED"
    RELEASED = "RELEASED"


class PairMappingStatus(str, Enum):
    MAPPED = "MAPPED"
    NO_MAPPING_GLOBAL_EVENT = "NO_MAPPING_GLOBAL_EVENT"
    NO_MAPPING_UNKNOWN_CURRENCY = "NO_MAPPING_UNKNOWN_CURRENCY"


class QualityStatus(str, Enum):
    VALID = "VALID"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


class ReleaseGroupType(str, Enum):
    CENTRAL_BANK_DECISION = "CENTRAL_BANK_DECISION"
    LABOR_MARKET_RELEASE = "LABOR_MARKET_RELEASE"
    SIMULTANEOUS_RELEASE = "SIMULTANEOUS_RELEASE"


IMPACT_ALIASES = {
    "HIGH": Impact.HIGH, "RED": Impact.HIGH,
    "MEDIUM": Impact.MEDIUM, "ORANGE": Impact.MEDIUM, "MED": Impact.MEDIUM,
    "LOW": Impact.LOW, "YELLOW": Impact.LOW,
    "HOLIDAY": Impact.HOLIDAY, "NON-ECONOMIC": Impact.HOLIDAY, "GRAY": Impact.HOLIDAY,
    "GREY": Impact.HOLIDAY,
}


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return _SLUG_RE.sub("-", norm.lower()).strip("-")


def parse_source_datetime(raw: Any) -> datetime:
    """Accepte 'Z', '+00:00', '-04:00', naïf (traité UTC). Retourne un aware UTC."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("empty datetime")
        txt = raw.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


_NUM_RE = re.compile(r"^([<>~]?)\s*(-?\d+(?:[.,]\d+)?)\s*([KMBT]?)\s*(%?)$", re.IGNORECASE)
_SCALES = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


class NumericValue(BaseModel):
    """Valeur économique : brut conservé + interprétation numérique explicite."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    scale: Optional[str] = None
    parse_status: str = "ABSENT"


ABSENT_NUMERIC = NumericValue()
_PLACEHOLDERS = {"", "-", "—", "–", "n/a", "na", "null", "none"}


def normalize_numeric(raw: Any) -> NumericValue:
    """Ne devine jamais l'unité au-delà de ce que le suffixe garantit.
    '0' et 0 ne doivent JAMAIS devenir absents (piège du falsy Python)."""
    if raw is None:
        return ABSENT_NUMERIC
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return NumericValue(raw=str(raw), value=float(raw), unit="number",
                            scale=None, parse_status="PARSED")
    if not isinstance(raw, str):
        return NumericValue(raw=str(raw), parse_status="UNPARSEABLE")

    text = raw.strip()
    if text.lower() in _PLACEHOLDERS:
        return ABSENT_NUMERIC

    status = "PARSED"
    candidate = text
    if "|" in candidate:
        candidate = candidate.split("|", 1)[0].strip()
        status = "COMPOSITE"

    m = _NUM_RE.match(candidate)
    if not m:
        return NumericValue(raw=text, parse_status="UNPARSEABLE")

    _prefix, number, suffix, pct = m.groups()
    try:
        base = float(number.replace(",", "."))
    except ValueError:
        return NumericValue(raw=text, parse_status="UNPARSEABLE")

    suffix = suffix.upper()
    if pct:
        return NumericValue(raw=text, value=base, unit="percent",
                            scale=None, parse_status=status)
    return NumericValue(raw=text, value=base * _SCALES[suffix], unit="number",
                        scale=suffix or None, parse_status=status)


def classify_session(dt_utc: datetime) -> Tuple[Session, List[str]]:
    """Sessions calculées en heure LOCALE de chaque place, DST inclus."""
    active: List[str] = []
    for name, (tz, start_min, end_min) in SESSION_HOURS.items():
        local = dt_utc.astimezone(tz)
        if local.weekday() >= 5:
            continue
        minutes = local.hour * 60 + local.minute
        if start_min <= minutes < end_min:
            active.append(name)

    has_ldn, has_ny, has_tky = ("LONDON" in active, "NEW_YORK" in active, "TOKYO" in active)
    if has_ldn and has_ny:
        session = Session.OVERLAP_LONDON_NY
    elif has_ldn and has_tky:
        session = Session.OVERLAP_ASIA_LONDON
    elif has_ny:
        session = Session.NEW_YORK
    elif has_ldn:
        session = Session.LONDON
    elif has_tky:
        session = Session.ASIAN
    else:
        session = Session.OFF
    return session, sorted(active)


def fmt_until(hours: float) -> str:
    total_min = int(round(abs(hours) * 60))
    d, rem = divmod(total_min, 1440)
    h, m = divmod(rem, 60)
    if d:
        body = f"{d}d {h}h {m}m"
    elif h:
        body = f"{h}h {m}m"
    else:
        body = f"{m}m"
    return body if hours > 0 else f"{body} ago"


class SelectionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = "1.0.0"
    impact_levels: Tuple[Impact, ...] = (Impact.HIGH,)
    currencies: Optional[Tuple[str, ...]] = None
    include_global_events: bool = True
    window_past_hours: float = 72.0
    window_future_hours: float = 192.0
    imminent_hours: float = 6.0
    soon_hours: float = 48.0
    display_timezone: str = DEFAULT_DISPLAY_TZ
    max_source_age_seconds: int = 900
    max_events: int = 2000

    @field_validator("currencies")
    @classmethod
    def _upper(cls, v):
        return None if v is None else tuple(sorted({c.upper() for c in v}))

    @model_validator(mode="after")
    def _coherent(self):
        if self.window_past_hours < 0 or self.window_future_hours <= 0:
            raise ValueError("window bounds must be positive")
        if not 0 < self.imminent_hours < self.soon_hours:
            raise ValueError("imminent_hours must be < soon_hours")
        return self

    def display_tz(self) -> ZoneInfo:
        return ZoneInfo(self.display_timezone)


DEFAULT_POLICY = SelectionPolicy()


class TimeContext(BaseModel):
    """VOLATIL. Exclu du content_hash. Recalculable à tout instant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    computed_at_utc: datetime
    hours_until: float
    hours_until_display: str
    is_upcoming: bool
    time_proximity: TimeProximity
    status: EventStatus

    @field_serializer("computed_at_utc")
    def _ser(self, v: datetime, _info) -> str:
        return iso_z(v)


class CalendarEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    occurrence_id: str
    event_type_id: str
    release_group_id: Optional[str] = None
    release_group_type: Optional[ReleaseGroupType] = None

    currency: str
    is_global: bool = False
    name: str

    scheduled_at_utc: datetime
    scheduled_at_display: datetime
    display_timezone: str
    date_utc: str
    date_display: str
    day_of_week: str

    impact: Impact
    session: Session
    active_market_centers: Tuple[str, ...] = ()

    forecast: NumericValue = ABSENT_NUMERIC
    previous: NumericValue = ABSENT_NUMERIC
    actual: NumericValue = ABSENT_NUMERIC
    actual_status: ActualStatus = ActualStatus.UNSUPPORTED_BY_SOURCE

    pairs_with_currency_exposure: Tuple[str, ...] = ()
    pair_mapping_status: PairMappingStatus = PairMappingStatus.MAPPED
    pair_mapping_method: str = PAIR_MAPPING_METHOD

    source_index: int
    time_context: Optional[TimeContext] = None

    @field_validator("scheduled_at_utc")
    @classmethod
    def _aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("scheduled_at_utc must be timezone-aware")
        return v.astimezone(UTC)

    @field_validator("scheduled_at_display")
    @classmethod
    def _aware_display(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("scheduled_at_display must be timezone-aware")
        return v

    @field_serializer("scheduled_at_utc")
    def _ser_utc(self, v: datetime, _info) -> str:
        return iso_z(v)

    @field_serializer("scheduled_at_display")
    def _ser_display(self, v: datetime, _info) -> str:
        return v.isoformat()

    def with_time_context(self, ctx: TimeContext) -> "CalendarEvent":
        return self.model_copy(update={"time_context": ctx})


class SourceInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    url: str
    fetched_at_utc: datetime
    fetch_duration_ms: int = 0
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    payload_bytes: int = 0
    payload_sha256: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    supports_actual: bool = False
    from_last_known_good: bool = False

    @field_serializer("fetched_at_utc")
    def _ser(self, v: datetime, _info) -> str:
        return iso_z(v)


class QualityInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: QualityStatus
    is_stale: bool
    source_age_seconds: int
    raw_event_count: int
    accepted_event_count: int
    rejected_event_count: int
    duplicate_event_count: int
    coverage_start_utc: Optional[str] = None
    coverage_end_utc: Optional[str] = None
    data_quality_score: float = Field(ge=0.0, le=1.0)
    warnings: Tuple[str, ...] = ()
    rejections: Tuple[str, ...] = ()


class CalendarPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    generated_at_utc: datetime
    generator: str = "bluestar-calendar-macro-unified"
    content_hash: Optional[str] = None
    source: SourceInfo
    quality: QualityInfo
    selection_policy: SelectionPolicy
    session_policy_version: str = SESSION_POLICY_VERSION
    numeric_parser_version: str = NUMERIC_PARSER_VERSION
    events: Tuple[CalendarEvent, ...]

    @field_serializer("generated_at_utc")
    def _ser(self, v: datetime, _info) -> str:
        return iso_z(v)

    @model_validator(mode="after")
    def _invariants(self):
        ids = [e.occurrence_id for e in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate occurrence_id in payload")
        times = [e.scheduled_at_utc for e in self.events]
        if times != sorted(times):
            raise ValueError("events must be sorted by scheduled_at_utc")
        if self.quality.accepted_event_count != len(self.events):
            raise ValueError("accepted_event_count mismatch")
        return self


_RATE_KEYWORDS = (
    "official cash rate", "overnight rate", "rate statement", "cash rate",
    "monetary policy statement", "interest rate", "policy rate", "main refinancing",
    "federal funds", "bank rate", "fomc statement",
)
_PRESSER_KEYWORDS = ("press conference", "monetary policy press")
_LABOR_KEYWORDS = (
    "non-farm employment change", "unemployment rate", "average hourly earnings",
    "employment change", "claimant count",
)


def _match(title: str, keywords: Sequence[str]) -> bool:
    low = title.lower()
    return any(k in low for k in keywords)


def assign_release_groups(rows: List[Dict[str, Any]]) -> None:
    """Groupe (mutation in place de la clé 'release_group_*') :
      1. publications strictement simultanées d'un même pays,
      2. conférence de presse rattachée à la décision de taux du même pays
         survenue dans les 120 minutes précédentes."""
    buckets: Dict[Tuple[str, datetime], List[Dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault((row["currency"], row["scheduled_at_utc"]), []).append(row)

    ordered = sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    anchors: Dict[str, Tuple[datetime, str]] = {}

    for (ccy, when), members in ordered:
        titles = [m["name"] for m in members]
        is_cb = any(_match(t, _RATE_KEYWORDS) for t in titles)
        is_presser = all(_match(t, _PRESSER_KEYWORDS) for t in titles)
        is_labor = any(_match(t, _LABOR_KEYWORDS) for t in titles)

        gid: Optional[str] = None
        gtype: Optional[ReleaseGroupType] = None

        if is_presser and ccy in anchors:
            anchor_time, anchor_gid = anchors[ccy]
            if timedelta(0) <= (when - anchor_time) <= timedelta(minutes=120):
                gid, gtype = anchor_gid, ReleaseGroupType.CENTRAL_BANK_DECISION

        if gid is None and (is_cb or len(members) > 1):
            gid = "grp_" + sha256_hex(f"{ccy}|{iso_z(when)}")[:16]
            if is_cb:
                gtype = ReleaseGroupType.CENTRAL_BANK_DECISION
                anchors[ccy] = (when, gid)
            elif is_labor:
                gtype = ReleaseGroupType.LABOR_MARKET_RELEASE
            else:
                gtype = ReleaseGroupType.SIMULTANEOUS_RELEASE

        for m in members:
            m["release_group_id"] = gid
            m["release_group_type"] = gtype


def compute_time_context(
    event: CalendarEvent, now_utc: datetime, policy: SelectionPolicy = DEFAULT_POLICY
) -> TimeContext:
    hours = (event.scheduled_at_utc - now_utc).total_seconds() / 3600.0
    if event.impact is Impact.HOLIDAY:
        status = EventStatus.HOLIDAY
    elif hours > 0:
        status = EventStatus.SCHEDULED
    elif hours > -0.5:
        status = EventStatus.DUE
    else:
        status = EventStatus.PAST_SCHEDULE

    if hours <= 0:
        proximity = TimeProximity.PAST
    elif hours <= policy.imminent_hours:
        proximity = TimeProximity.IMMINENT
    elif hours <= policy.soon_hours:
        proximity = TimeProximity.SOON
    else:
        proximity = TimeProximity.LATER

    return TimeContext(
        computed_at_utc=now_utc,
        hours_until=round(hours, 4),
        hours_until_display=fmt_until(hours),
        is_upcoming=hours > 0,
        time_proximity=proximity,
        status=status,
    )


def _normalize_row(
    raw: Any, index: int, policy: SelectionPolicy, display_tz: ZoneInfo
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(raw, dict):
        return None, f"idx={index}: root element is {type(raw).__name__}, expected object"

    title = str(raw.get("title", "") or "").strip()
    if not title:
        return None, f"idx={index}: missing title"

    country = str(raw.get("country", "") or "").strip().upper()
    impact = IMPACT_ALIASES.get(str(raw.get("impact", "") or "").strip().upper(), Impact.UNKNOWN)

    try:
        when = parse_source_datetime(raw.get("date"))
    except (ValueError, TypeError) as exc:
        return None, f"idx={index} '{title}': invalid date ({exc})"

    is_global = country in GLOBAL_COUNTRY_TOKENS
    if is_global:
        currency, pairs, mapping = "ALL", (), PairMappingStatus.NO_MAPPING_GLOBAL_EVENT
    elif country in KNOWN_CURRENCIES:
        currency, mapping = country, PairMappingStatus.MAPPED
        pairs = tuple(pairs_for_currency(country))
    else:
        currency, pairs = country, ()
        mapping = PairMappingStatus.NO_MAPPING_UNKNOWN_CURRENCY

    has_actual_key = "actual" in raw
    actual = normalize_numeric(raw.get("actual")) if has_actual_key else ABSENT_NUMERIC
    if actual.parse_status != "ABSENT":
        actual_status = ActualStatus.RELEASED
    elif has_actual_key:
        actual_status = ActualStatus.NOT_YET_RELEASED
    else:
        actual_status = ActualStatus.UNSUPPORTED_BY_SOURCE

    local = when.astimezone(display_tz)
    type_id = f"ff:{currency.lower()}:{slugify(title)}"

    return {
        "occurrence_id": sha256_hex(f"{type_id}|{iso_z(when)}")[:32],
        "event_type_id": type_id,
        "release_group_id": None,
        "release_group_type": None,
        "currency": currency,
        "is_global": is_global,
        "name": title,
        "scheduled_at_utc": when,
        "scheduled_at_display": local,
        "display_timezone": policy.display_timezone,
        "date_utc": when.strftime("%Y-%m-%d"),
        "date_display": local.strftime("%Y-%m-%d"),
        "day_of_week": local.strftime("%A").upper(),
        "impact": impact,
        "session": None,
        "active_market_centers": None,
        "forecast": normalize_numeric(raw.get("forecast")),
        "previous": normalize_numeric(raw.get("previous")),
        "actual": actual,
        "actual_status": actual_status,
        "pairs_with_currency_exposure": pairs,
        "pair_mapping_status": mapping,
        "pair_mapping_method": PAIR_MAPPING_METHOD,
        "source_index": index,
    }, None


def build_payload(
    raw_list: Any,
    *,
    source: SourceInfo,
    now_utc: datetime,
    policy: SelectionPolicy = DEFAULT_POLICY,
) -> CalendarPayload:
    """Transforme le payload brut en artefact canonique validé."""
    if not isinstance(raw_list, list):
        raise ValueError(f"source root must be a JSON array, got {type(raw_list).__name__}")
    if len(raw_list) > policy.max_events:
        raise ValueError(f"payload too large: {len(raw_list)} > {policy.max_events}")

    display_tz = policy.display_tz()
    warnings: List[str] = []
    rejections: List[str] = []
    rows: List[Dict[str, Any]] = []

    for index, raw in enumerate(raw_list):
        row, err = _normalize_row(raw, index, policy, display_tz)
        if err:
            rejections.append(err)
            continue
        rows.append(row)

    unknown = {r["impact"] for r in rows if r["impact"] is Impact.UNKNOWN}
    if unknown:
        warnings.append("SOURCE_IMPACT_VOCABULARY_CHANGED")

    lo = now_utc - timedelta(hours=policy.window_past_hours)
    hi = now_utc + timedelta(hours=policy.window_future_hours)

    selected: List[Dict[str, Any]] = []
    for row in rows:
        if row["impact"] not in policy.impact_levels:
            continue
        if row["is_global"] and not policy.include_global_events:
            continue
        if (policy.currencies is not None and not row["is_global"]
                and row["currency"] not in policy.currencies):
            continue
        if not (lo <= row["scheduled_at_utc"] <= hi):
            continue
        selected.append(row)

    seen: Dict[str, Dict[str, Any]] = {}
    duplicates = 0
    for row in selected:
        if row["occurrence_id"] in seen:
            duplicates += 1
            continue
        seen[row["occurrence_id"]] = row
    selected = list(seen.values())
    if duplicates:
        warnings.append(f"DUPLICATE_OCCURRENCES_DROPPED:{duplicates}")

    selected.sort(key=lambda r: (r["scheduled_at_utc"], r["currency"], r["name"]))
    assign_release_groups(selected)

    events: List[CalendarEvent] = []
    for row in selected:
        session, centers = classify_session(row["scheduled_at_utc"])
        row["session"] = session
        row["active_market_centers"] = tuple(centers)
        event = CalendarEvent(**row)
        events.append(event.with_time_context(compute_time_context(event, now_utc, policy)))

    age = max(0, int((now_utc - source.fetched_at_utc).total_seconds()))
    is_stale = age > policy.max_source_age_seconds
    if is_stale:
        warnings.append(f"SOURCE_AGE_EXCEEDS_{policy.max_source_age_seconds}S")
    if source.from_last_known_good:
        warnings.append("SERVING_LAST_KNOWN_GOOD")
    if not source.supports_actual:
        warnings.append("SOURCE_DOES_NOT_PROVIDE_ACTUAL")

    all_times = [r["scheduled_at_utc"] for r in rows]
    coverage_start = min(all_times) if all_times else None
    coverage_end = max(all_times) if all_times else None
    if coverage_end is not None and coverage_end < now_utc:
        warnings.append("ALL_SOURCE_EVENTS_IN_THE_PAST_WEEK_ROLLOVER_PENDING")
    if coverage_start is not None and coverage_start > now_utc + timedelta(days=9):
        warnings.append("SOURCE_COVERAGE_STARTS_TOO_FAR_IN_FUTURE")
    if not rows:
        warnings.append("EMPTY_NORMALIZED_PAYLOAD")

    score = 1.0
    if is_stale:
        score -= 0.35
    if source.from_last_known_good:
        score -= 0.25
    if rejections:
        score -= min(0.25, 0.05 * len(rejections))
    if "ALL_SOURCE_EVENTS_IN_THE_PAST_WEEK_ROLLOVER_PENDING" in warnings:
        score -= 0.30
    if not rows:
        score = 0.0
    score = round(max(0.0, min(1.0, score)), 3)

    if not rows or score < 0.4:
        status = QualityStatus.INVALID
    elif warnings and (is_stale or source.from_last_known_good or score < 0.85):
        status = QualityStatus.DEGRADED
    else:
        status = QualityStatus.VALID

    quality = QualityInfo(
        status=status,
        is_stale=is_stale,
        source_age_seconds=age,
        raw_event_count=len(raw_list),
        accepted_event_count=len(events),
        rejected_event_count=len(rejections),
        duplicate_event_count=duplicates,
        coverage_start_utc=iso_z(coverage_start) if coverage_start else None,
        coverage_end_utc=iso_z(coverage_end) if coverage_end else None,
        data_quality_score=score,
        warnings=tuple(warnings),
        rejections=tuple(rejections[:50]),
    )

    payload = CalendarPayload(
        generated_at_utc=now_utc,
        source=source,
        quality=quality,
        selection_policy=policy,
        events=tuple(events),
    )
    return payload.model_copy(update={"content_hash": canonical_content_hash(payload)})


_VOLATILE_EVENT_FIELDS = ("time_context",)
_VOLATILE_ROOT_FIELDS = ("generated_at_utc", "content_hash", "source", "quality")


def canonical_content_hash(payload: CalendarPayload) -> str:
    """SHA-256 du contenu économique seul, insensible aux champs volatils."""
    dumped = payload.model_dump(mode="json")
    for field in _VOLATILE_ROOT_FIELDS:
        dumped.pop(field, None)
    for event in dumped.get("events", []):
        for f in _VOLATILE_EVENT_FIELDS:
            event.pop(f, None)
    canonical = json.dumps(dumped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + sha256_hex(canonical)


def refresh_time_contexts(
    payload: CalendarPayload, now_utc: datetime
) -> Tuple[CalendarEvent, ...]:
    return tuple(
        e.with_time_context(compute_time_context(e, now_utc, payload.selection_policy))
        for e in payload.events
    )


_LEGACY_SESSION = {
    Session.OVERLAP_LONDON_NY: "OVERLAP",
    Session.OVERLAP_ASIA_LONDON: "LONDON",
    Session.NEW_YORK: "NEW YORK",
    Session.LONDON: "LONDON",
    Session.ASIAN: "ASIAN",
    Session.OFF: "OFF",
}


def _utc_offset_label(dt: datetime) -> str:
    """Libellé "UTC+H" (ou "UTC-H") lisible, calculé à partir de l'offset RÉEL
    du datetime localisé -- jamais codé en dur.

    DEMANDE UTILISATEUR (câblage du 08/09/2026) : le renderer affichait le nom
    de zone IANA brut ("Africa/Casablanca") entre parenthèses à côté de chaque
    heure d'événement -- correct mais peu lisible pour un desk FX habitué à un
    offset. Un "+1" codé en dur aurait été FAUX pendant le Ramadan, période où
    le Maroc repasse à UTC+0 (pratique documentée depuis 2018) : is_blackout /
    priority ne sont pas affectés (ils travaillent en UTC pur via hours_until),
    mais l'AFFICHAGE doit rester exact toute l'année. D'où un calcul dynamique
    sur ``dt.utcoffset()`` plutôt qu'une chaîne fixe -- correct pour n'importe
    quel ``display_timezone`` de policy, pas seulement Casablanca.
    """
    offset = dt.utcoffset()
    if offset is None:
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hh}" + (f":{mm:02d}" if mm else "")


def to_legacy_payload(payload: CalendarPayload, now_utc: datetime) -> Dict[str, Any]:
    """Identique à celle de calendar_core.py -- utilisée ici uniquement comme
    étape intermédiaire ; build_calendar() y ajoute ensuite 'priority'."""
    events = refresh_time_contexts(payload, now_utc)
    rows: List[Dict[str, Any]] = []
    summary: Dict[str, List[str]] = {}

    for e in events:
        ctx = e.time_context
        rows.append({
            "occurrence_id": e.occurrence_id,
            "event_type_id": e.event_type_id,
            "release_group_id": e.release_group_id,
            "currency": e.currency,
            "event_name": e.name,
            "datetime_utc": iso_z(e.scheduled_at_utc),
            "date_display": e.date_display,
            # COMPAT-FIX (demande utilisateur 08/09/2026) : "UTC+1" au lieu du
            # nom de zone IANA brut "Africa/Casablanca" -- voir
            # _utc_offset_label ci-dessus pour pourquoi ce n'est pas une
            # chaîne "+1" codée en dur.
            "time_display": f"{e.scheduled_at_display.strftime('%H:%M')} ({_utc_offset_label(e.scheduled_at_display)})",
            "day_of_week": e.day_of_week,
            "impact": e.impact.value.lower(),
            # COMPAT-FIX : .raw est None (pas "") quand le flux ne fournit pas
            # la valeur -- MacroEvent.from_enriched() fait d.get("forecast","—")
            # qui ne retombe sur "—" QUE si la clé est absente, jamais si elle
            # vaut None. Sans ce "or ''", un consensus/previous non publié
            # devenait littéralement la chaîne "None" partout où ces champs
            # sont affichés (contrat v3 : toujours une chaîne, jamais None).
            "forecast": e.forecast.raw or "",
            "forecast_value": e.forecast.value,
            "previous": e.previous.raw or "",
            "previous_value": e.previous.value,
            "actual": e.actual.raw or "",
            "actual_status": e.actual_status.value,
            "hours_until": ctx.hours_until,
            "hours_until_display": ctx.hours_until_display,
            "is_upcoming": ctx.is_upcoming,
            "time_proximity": ctx.time_proximity.value,
            "status": ctx.status.value,
            "session": _LEGACY_SESSION[e.session],
            "session_v2": e.session.value,
            "pairs_affected": list(e.pairs_with_currency_exposure),
            "pair_mapping_status": e.pair_mapping_status.value,
        })
        summary.setdefault(e.date_display, []).append(f"{e.currency} – {e.name}")

    return {
        "metadata": {
            "schema_version": f"legacy-1.1.0+core-{payload.schema_version}",
            "generated_at_utc": iso_z(payload.generated_at_utc),
            "content_hash": payload.content_hash,
            "source": payload.source.provider,
            "source_url": payload.source.url,
            "fetched_at_utc": iso_z(payload.source.fetched_at_utc),
            "supports_actual": payload.source.supports_actual,
            "timezone": f"UTC (backend) / {payload.selection_policy.display_timezone} (display)",
            "quality_status": payload.quality.status.value,
            "data_quality_score": payload.quality.data_quality_score,
            "is_stale": payload.quality.is_stale,
            "source_age_seconds": payload.quality.source_age_seconds,
            "warnings": list(payload.quality.warnings),
            "total_high_impact": len(rows),
            "upcoming_count": sum(1 for r in rows if r["is_upcoming"]),
            "imminent_count": sum(1 for r in rows if r["time_proximity"] == "IMMINENT"),
            "engine_events_count": len(rows),
            "summary_by_day_basis": "display_timezone",
            "ui_filters_applied": None,
        },
        "events": rows,
        "events_engine": rows,
        "summary_by_day": {k: summary[k] for k in sorted(summary)},
    }


# =============================================================================
# SECTION 2 -- BLACKOUT (inchangé, réplique exacte de v10.py / Desk Engine).
# Sujet ORTHOGONAL à la normalisation ci-dessus : ne jamais fusionner cette
# logique avec calendar_core, elle n'a pas d'équivalent côté app TA.
# IMPORTANT -- dette technique assumée : toute modification de TIER_WINDOWS
# dans v10.py DOIT être répercutée ici à l'identique.
# =============================================================================

_TIER_S = ("non-farm", "nonfarm", "nfp", "fomc", "cpi", "cash rate",
           "bank rate", "rate statement", "interest rate", "monetary policy",
           "funds rate", "policy rate")
_TIER_A = ("gdp", "pmi", "adp", "pce", "employment change", "unemployment",
           "average hourly", "retail sales", "ppi")
_TIER_B = ("speaks", "speech", "press conference", "testifies", "testimony")

TIER_WINDOWS: Dict[str, tuple] = {
    "S": (4.0, 48.0),
    "A": (2.0, 24.0),
    "B": (1.0, 6.0),
}
DEFAULT_TIER_WINDOW = (2.0, 24.0)


def classify_tier(event_name: str) -> str:
    """Identique à v10.classify_tier -- même liste de mots-clés, même ordre
    de priorité (S avant A avant B)."""
    n = (event_name or "").lower()
    if any(k in n for k in _TIER_S):
        return "S"
    if any(k in n for k in _TIER_A):
        return "A"
    if any(k in n for k in _TIER_B):
        return "B"
    return "NONE"


def is_blackout(event_name: str, hours_until: float) -> tuple:
    """True si l'événement place sa devise en fenêtre de blackout, avant OU
    après l'annonce -- réplique de v10.CalendarData.bucket().

    ``hours_until`` suit la même convention que ``build_calendar()`` :
    positif = événement futur, négatif = événement déjà passé.
    Retourne ``(bloqué: bool, tier: str)``.
    """
    tier = classify_tier(event_name)
    before, after = TIER_WINDOWS.get(tier, DEFAULT_TIER_WINDOW)
    return (-after <= hours_until <= before), tier


# =============================================================================
# SECTION 3 -- FETCH (résilient mais SANS état persistant, à la demande de
# l'utilisateur : pas de circuit breaker, pas de last-known-good sur disque,
# pas de health.json -- juste retries/backoff urllib3 par appel, comme
# calendar_ingestor.py mais stateless).
# =============================================================================

# COMPAT-FIX : dupliquait l'URL en dur (par coïncidence identique à
# config.FF_JSON_URL) au lieu de la référencer -- source unique de vérité
# restaurée, override via variable d'env toujours possible pour les tests.
SOURCE_URL = os.getenv("BLUESTAR_SOURCE_URL", _C.FF_JSON_URL)
SOURCE_PROVIDER = "Forex Factory / Fair Economy weekly public feed"
USER_AGENT = os.getenv("BLUESTAR_USER_AGENT", "BluestarCalendarMacroUnified/4.0 (+ops@bluestar)")

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.5,
        backoff_jitter=0.4,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    return session


class FetchError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def fetch_raw(url: str = SOURCE_URL) -> Tuple[List[Dict], Dict[str, Any]]:
    """Fetch résilient (retries + backoff), SANS état persistant.

    Retourne ``([], meta_vide)`` sur tout échec -- l'app se dégrade
    proprement plutôt que de lever une exception (même contrat que la v3).
    """
    session = _build_session()
    started = time.monotonic()
    try:
        response = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True)
    except requests.RequestException as exc:
        logger.error("Calendar fetch failed: %s", exc)
        return [], {}

    try:
        with response:
            if response.status_code >= 400:
                logger.error("Calendar fetch failed: HTTP %s", response.status_code)
                return [], {}

            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_PAYLOAD_BYTES:
                    logger.error("Calendar fetch failed: payload too large (%d bytes)", size)
                    return [], {}
                chunks.append(chunk)
            body = b"".join(chunks)

            meta = {
                "http_status": response.status_code,
                "content_type": (response.headers.get("Content-Type") or "").split(";")[0].strip() or None,
                "payload_bytes": size,
                "payload_sha256": "sha256:" + sha256_hex(body.decode("utf-8", errors="replace")),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "fetch_duration_ms": int((time.monotonic() - started) * 1000),
            }
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, list):
            logger.error("Calendar fetch failed: root is %s, expected array", type(parsed).__name__)
            return [], {}
        return parsed, meta
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("Calendar fetch failed: invalid JSON (%s)", exc)
        return [], {}


# =============================================================================
# SECTION 4 -- POINT D'ENTRÉE PUBLIC, compatible macro_engine.py sans aucune
# modification côté consommateur.
# =============================================================================

# PATCH-FEEDHORIZON (identique à v3, round du 31/07/2026, audit F-15).
# FF_JSON_URL pointe sur un flux HEBDOMADAIRE. Un silence calendaire au-delà
# de la fin du flux n'est PAS une absence de risque. Doit rester synchronisé
# avec v10.WATCH_MAX_H (même dette de duplication assumée que TIER_WINDOWS).
FF_WATCH_HORIZON_H = 168.0

# COMPAT-FIX : config.py (fourni au round de câblage) donne
# RESIDUAL_RISK_WINDOW_H = 72, pas 48. L'assomption de la docstring
# ci-dessus était fausse -- confirmé en lisant le vrai config.py. Lue
# directement depuis la source unique de vérité, plus de duplication/dérive
# possible si RESIDUAL_RISK_WINDOW_H change côté config.py.
MACRO_RESIDUAL_RISK_WINDOW_H = _C.RESIDUAL_RISK_WINDOW_H


def build_calendar(
    now_utc: Optional[datetime] = None,
    raw_data: Optional[List[Dict]] = None,
    policy: SelectionPolicy = DEFAULT_POLICY,
) -> Dict:
    """Construit le payload canonique, avec le MÊME contrat de sortie que
    calendar_layer.py v3 (metadata / events / events_engine / summary_by_day),
    mais en s'appuyant en interne sur la normalisation de calendar_core --
    donc garanti identique, événement par événement, à ce que voit l'app TA.

    Trois corrections explicites par rapport à un branchement naïf de
    to_legacy_payload() (voir docstring de tête -- section "BUG ÉVITÉ") :
      1. ``priority`` (CRITICAL/HIGH/MEDIUM/PAST) restauré, absent de
         calendar_core mais consommé comme variable de décision par
         macro_engine.py.
      2. ``metadata.reachable`` / ``feed_horizon_h`` / ``feed_horizon_truncated``
         restaurés -- absents de calendar_core (patch F-15, spécifique à ce
         module macro) ; sans eux, ``calendar_reachable`` et
         ``calendar_feed_truncated`` de BriefingContext restent figés à des
         valeurs par défaut trompeuses (P0-1 FIX silencieusement perdu).
      3. ``events`` (à venir uniquement) redevient distinct de
         ``events_engine`` (à venir + passés dans
         MACRO_RESIDUAL_RISK_WINDOW_H), comme en v3 -- calendar_core renvoie
         par défaut la même liste pour les deux clés.
    """
    now_utc = now_utc or datetime.now(UTC)
    meta: Dict[str, Any] = {}
    if raw_data is None:
        raw_data, meta = fetch_raw()

    # --- Horizon réel du flux (TOUS impacts confondus), avant tout filtrage
    # par impact/fenêtre -- mesure la couverture du flux, pas celle des seuls
    # high-impact retenus plus bas. Aucune décision n'est modifiée ici : de
    # la visibilité, pas du gating (identique v3).
    feed_end: Optional[datetime] = None
    for _ev in raw_data:
        try:
            _t = parse_source_datetime(_ev.get("date")) if isinstance(_ev, dict) else None
        except (ValueError, TypeError):
            _t = None
        if _t is not None and (feed_end is None or _t > feed_end):
            feed_end = _t
    feed_horizon_h = ((feed_end - now_utc).total_seconds() / 3600.0) if feed_end else None
    feed_truncated = feed_horizon_h is not None and feed_horizon_h < FF_WATCH_HORIZON_H
    if feed_truncated:
        logger.warning(
            "FF feed horizon %.1fh < %.0fh — fenêtre WATCH non vérifiable au-delà "
            "du flux hebdomadaire ; un silence calendaire n'est PAS une absence de risque",
            feed_horizon_h, FF_WATCH_HORIZON_H)

    source = SourceInfo(
        provider=SOURCE_PROVIDER,
        url=SOURCE_URL,
        fetched_at_utc=now_utc,
        fetch_duration_ms=int(meta.get("fetch_duration_ms") or 0),
        http_status=meta.get("http_status"),
        content_type=meta.get("content_type"),
        payload_bytes=int(meta.get("payload_bytes") or 0),
        payload_sha256=str(meta.get("payload_sha256") or "sha256:unknown"),
        etag=meta.get("etag"),
        last_modified=meta.get("last_modified"),
        supports_actual=any(isinstance(r, dict) and "actual" in r for r in raw_data),
        from_last_known_good=False,
    )

    payload = build_payload(raw_data, source=source, now_utc=now_utc, policy=policy)
    legacy = to_legacy_payload(payload, now_utc)
    all_rows = legacy["events"]  # population complète (fenêtre policy), avant split v3

    # --- 1. Restauration explicite de 'priority' ---
    for row in all_rows:
        h = row["hours_until"]
        row["priority"] = (
            "PAST" if h <= 0
            else "CRITICAL" if h <= policy.imminent_hours
            else "HIGH" if h <= policy.soon_hours
            else "MEDIUM"
        )

    # --- 3. Split events (à venir) / events_engine (à venir + résiduel) ---
    upcoming_rows = [r for r in all_rows if r["is_upcoming"]]
    engine_rows = [r for r in all_rows
                   if r["is_upcoming"] or r["hours_until"] >= -MACRO_RESIDUAL_RISK_WINDOW_H]
    legacy["events"] = upcoming_rows
    legacy["events_engine"] = engine_rows

    critical_count = sum(1 for r in all_rows if r["priority"] == "CRITICAL")
    imminent_count = sum(1 for r in all_rows if r["time_proximity"] == "IMMINENT")

    # --- 2. Restauration des champs de couverture du flux + reachable ---
    legacy["metadata"].update({
        "total_high_impact": len(all_rows),
        "upcoming_count": len(upcoming_rows),
        "critical_count": critical_count,
        "imminent_count": imminent_count,
        "engine_events_count": len(engine_rows),
        "reachable": bool(raw_data),
        "feed_end_utc": iso_z(feed_end) if feed_end else None,
        "feed_horizon_h": round(feed_horizon_h, 1) if feed_horizon_h is not None else None,
        "feed_horizon_truncated": feed_truncated,
    })

    return legacy


PAIRS_MAP: Dict[str, List[str]] = {ccy: pairs_for_currency(ccy) for ccy in KNOWN_CURRENCIES}


def get_session(t: datetime) -> str:
    """Conservé pour compatibilité avec tout appelant direct existant.
    build_calendar() utilise en interne classify_session() (DST-aware),
    plus précis -- cette fonction simple UTC-only n'est plus dans le chemin
    de données réel, uniquement gardée en cas d'import direct ailleurs."""
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
