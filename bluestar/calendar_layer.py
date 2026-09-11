"""Calendar Layer -- Forex Factory High-Impact feed (Data Integrity Layer).

Version 5 -- réparation + consolidation mono-fichier, câblée sur
``config.py`` / ``macro_engine.py`` / ``models.py`` / ``app.py``.

=============================================================================
CE QUE LA v5 CORRIGE PAR RAPPORT À LA v4 (chaque point vérifié sur le
briefing HTML du 08/09/2026)
=============================================================================

[F1] FLUX TRONQUÉ EN PERMANENCE (cause du bandeau rouge « tronqué à 93.4h »)
     ``FF_JSON_URL`` = ``ff_calendar_thisweek.json`` est un flux HEBDOMADAIRE
     (dimanche → samedi). Comparer sa fin à ``FF_WATCH_HORIZON_H = 168h``
     produisait ``feed_horizon_truncated = True`` TOUS LES JOURS sauf le
     dimanche (mardi 93h, mercredi 70h, jeudi 46h...). L'avertissement
     « un silence calendaire n'est PAS une absence de risque » était donc
     structurellement toujours affiché : bruit permanent, zéro information.
     → v5 : fusion ``thisweek`` + ``nextweek`` (deux GET séquentiels sur la
       même session, dédoublonnage par (country, title, date_utc)). Horizon
       roulant de 7 à 14 jours, donc >= 168h en permanence. Le drapeau ne se
       lève plus que si le second flux est réellement indisponible -- et il
       redevient alors un vrai signal.

[F2] INCOHÉRENCE DE FUSEAU DANS LE HTML (1 heure d'écart)
     ``DEFAULT_DISPLAY_TZ = "Africa/Casablanca"`` (UTC+1) alors que toute
     l'app macro affiche ``config.TZ_CET`` = Europe/Paris (UTC+2 en été).
     Le HTML mélangeait donc « 12:48 CEST » (sous-barre, macro_engine) et
     « 13:15 (UTC+1) » (section 2, calendar_layer) : la BCE était en réalité
     à 14:15 CEST. Un lecteur qui cale une session sur l'heure affichée se
     trompait d'une heure.
     → v5 : ``DISPLAY_TIMEZONE`` est LU depuis ``config.TZ_CET.key``
       (override par ``BLUESTAR_DISPLAY_TZ``). Plus aucun fuseau codé en
       dur. Les décisions (priority / is_blackout / hours_until) restent en
       UTC pur : inchangées.

[F3] ``actual`` À CHAÎNE VIDE AU LIEU DE "—"
     Le ``or ""`` de la v4 cassait DEUX consommateurs :
       * ``app.py`` : ``if e['actual'] != '—'`` → affichait « / actual »
         (vide) sur chaque ligne de l'expander calendrier ;
       * ``models.MacroEvent.from_enriched`` : ``d.get("actual","—")`` ne
         retombe sur le tiret QUE si la clé est absente, jamais si elle vaut
         "" -- le contrat "—" de models.py n'était donc jamais honoré.
     → v5 : placeholder unique ``ABSENT_DISPLAY = "—"`` pour
       forecast/previous/actual, aligné sur models.py. ``*_value`` reste
       ``None`` pour l'exploitation numérique.

[F4] FRAÎCHEUR DE SOURCE NON MESURABLE
     ``SourceInfo.fetched_at_utc = now_utc`` → ``source_age_seconds`` = 0 et
     ``is_stale`` = False par construction. Le seuil
     ``max_source_age_seconds`` ne pouvait rien détecter, et le score de
     qualité était structurellement plafonné à 1.0.
     → v5 : horodatage RÉEL du fetch, propagé via la méta.

[F5] ``content_hash`` INSTABLE (l'objectif même du fichier)
     Le hash v4 incluait ``scheduled_at_display``, ``date_display``,
     ``day_of_week``, ``display_timezone`` et ``source_index`` : changer le
     fuseau d'affichage OU l'ordre de fusion des flux changeait le hash pour
     un contenu économique identique. « Les deux apps voient le même
     calendrier » était donc invérifiable.
     → v5 : hash calculé sur une PROJECTION ÉCONOMIQUE explicite
       (occurrence_id, event_type_id, UTC, devise, nom, impact,
       forecast/previous/actual bruts, release_group_id).
       ``CONTENT_HASH_METHOD`` documente la méthode. Divergence assumée et
       tracée vs calendar_core.py -- à y répercuter (voir DETTE ci-dessous).

[F6] PARSEUR NUMÉRIQUE : SÉPARATEUR DE MILLIERS
     ``"1,234"`` (mille deux cent trente-quatre) était lu ``1.234``, et
     ``"1.234"`` symétriquement. Sur un NFP ou des ventes au détail publiés
     sans suffixe K/M, l'erreur est d'un facteur 1000.
     → v5 : ``_to_float`` distingue milliers et décimales (dernier
       séparateur = décimal si les deux sont présents ; groupes de 3 exacts
       = milliers). Nouveau statut ``APPROXIMATE`` pour ``<``/``>``/``~``.

[F7] ROBUSTESSE / DIVERS
     * ``_LEGACY_SESSION[e.session]`` en accès direct → KeyError si un
       membre est ajouté à ``Session``. Passé en ``.get(...)``.
     * ``day_of_week`` via ``strftime("%A")`` → dépend de la locale du
       conteneur (« JEUDI » vs « THURSDAY »). Table fixe désormais.
     * ``payload_sha256`` calculé sur ``body.decode(errors="replace")`` →
       hash d'un texte dégradé, pas de la charge réelle. Hash sur octets.
     * ``Retry(backoff_jitter=...)`` lève ``TypeError`` sur urllib3 < 2.0
       (donc fetch mort et calendrier vide). Repli automatique.
     * ``build_payload`` levait ``ValueError`` sur un flux hors-norme
       (racine non-liste, > max_events) → exception non rattrapée dans
       ``build_calendar`` → app Streamlit morte. Désormais rattrapé et
       dégradé proprement (contrat v3 : jamais d'exception).
     * ``FetchError`` : code mort, conservé en alias de compatibilité.
     * ``window_past_hours`` était dupliqué (72.0 codé en dur) : dérivé de
       ``config.RESIDUAL_RISK_WINDOW_H`` (source unique, cohérent avec le
       ``since_h=72`` de ``macro_engine._events_for_ccys``).
     * ``window_future_hours`` passé de 192h à ``FF_WATCH_HORIZON_H`` (168h)
       pour que la fenêtre annoncée et la fenêtre servie soient la MÊME.

=============================================================================
CE QUI RESTE VOLONTAIREMENT INCHANGÉ (zéro régression décisionnelle)
=============================================================================
Les seuils ``imminent_hours = 6`` / ``soon_hours = 48`` et donc la
projection ``priority`` (PAST / CRITICAL / HIGH / MEDIUM) sont IDENTIQUES à
la v4. Tentation écartée : élargir HIGH à 168h pour peupler
``catalysts_high`` aurait alimenté ``macro_engine._compute_asset_score``
(``catalyst_pen = 0.15 * len(HIGH)``, plafonné à 0.30) et pouvait faire
tomber les setups sous ``MODE_SELECTION_MIN_SCORE`` -- soit un rapport vide.
Le tag « 🟡 ÉLEVÉ · >48h » du renderer est la bonne réponse à ce point ; il
n'appartient pas à cette couche.

``TIER_WINDOWS`` / ``classify_tier`` / ``is_blackout`` restent une réplique
EXACTE de ``v10.py`` (Desk Engine), qui fait autorité. Sujet ORTHOGONAL à la
normalisation. Toute modification dans v10.py DOIT être répercutée ici.

=============================================================================
CE QUE LA v6 EMPRUNTE À calendar_core.py / calendar_ingestor.py (app TA) --
SYNTHÈSE LÉGÈRE, ZÉRO RUPTURE DE CONTRAT
=============================================================================
Objectif : que le module macro expose les MÊMES informations de diagnostic
que l'app TA sur ce que les deux apps ont déjà en commun (même
``occurrence_id``, même flux Fair Economy), sans importer l'architecture de
producteur autonome de ``calendar_ingestor.py`` (cron/systemd, écriture
atomique, verrou inter-processus, historique) : ``calendar_layer`` reste
SANS état persistant, appelé en synchrone par l'app macro -- ce choix n'est
pas remis en cause (cf. en-tête SECTION 3).

[S1] STATUT PAR FLUX (``ok`` / ``absent_404`` / ``error:CODE``)
     Avant la v6, un ``nextweek`` non encore publié par Fair Economy (cas
     NORMAL en milieu de semaine, cf. [F1]) était compté comme un flux en
     échec au même titre qu'une vraie panne réseau : ``feeds_ok < feeds_total``
     déclenchait ``PARTIAL_FEED_COVERAGE`` et une pénalité de score de 0.15,
     pour une situation qui ne signifie STRICTEMENT RIEN sur la qualité des
     données retenues. ``calendar_ingestor.py`` distingue déjà les deux cas
     (404 = normal, log info ; erreur = anormal, log warning ; aucun des
     deux n'affecte le flux primaire).
     → v6 : ``SourceInfo.feed_status`` (``{"thisweek": "ok", "nextweek":
       "absent_404"}``) porte ce diagnostic. Seul un échec du flux PRIMAIRE
       (``thisweek``) déclenche avertissement + pénalité ; un flux
       secondaire absent ou en erreur est journalisé (``SECONDARY_FEED_
       ERROR``) mais n'entame plus le score -- exactement le traitement que
       l'app TA applique déjà à son flux bonus. Repli explicite sur l'ancien
       calcul (``feeds_ok``/``feeds_total``) si ``feed_status`` est vide
       (compat totale avec tout appelant direct de ``build_payload`` qui
       construirait encore son propre ``SourceInfo``).

[S2] COUVERTURE PAR DEVISE : POLICY vs SOURCE RÉELLE (``CoverageInfo``)
     ``calendar_layer`` ne distinguait pas « AUD absent parce que la policy
     ne retient que HIGH et la source n'a que du MEDIUM pour l'AUD cette
     semaine » de « AUD absent parce que la source n'a RIEN sur l'AUD ». Le
     premier cas est un artefact de configuration ; le second est une
     information de marché neutre. Confondre les deux, c'est exactement le
     type de bruit que [F1] corrige déjà pour l'horizon global -- même
     défaut, à la maille devise.
     → v6 : ``CoverageInfo`` (``currencies_scope`` / ``currencies_covered``
       / ``currencies_excluded_by_policy`` / ``currencies_no_data_in_
       source``), calculée en comparant les lignes normalisées AVANT et
       APRÈS filtre de policy sur la fenêtre ``[lo, hi]``. Purement
       diagnostique : ne change NI la sélection, NI ``priority``, NI
       ``is_blackout``. Exposée dans ``metadata`` sous les mêmes noms de
       clé que ``calendar_core.to_legacy_payload`` pour rester lisible par
       quiconque connaît déjà l'app TA.

CE QUI N'EST PAS PORTÉ (et pourquoi)
     Circuit breaker, last-known-good sur disque avec plafond d'âge dur,
     ``health.json``, verrou inter-processus, rotation/historique : tout
     cela appartient au PRODUCTEUR autonome (``calendar_ingestor.py``), pas
     à la couche de normalisation appelée en direct par l'app macro. Le
     porter ici transformerait un module sans effet de bord en composant
     avec état disque et sémantique de concurrence -- un changement
     d'architecture, pas une synthèse légère, et un risque de régression
     (FS en lecture seule, exécutions concurrentes) pour un bénéfice qui ne
     s'exprime que si l'app macro tourne, elle aussi, en producteur détaché.
     Si ce besoin se confirme, il mérite sa propre revue, pas un ajout
     silencieux ici.

=============================================================================
DETTE DOCUMENTÉE
=============================================================================
[F2] et [F5] sont des divergences DÉLIBÉRÉES vs ``calendar_core.py`` (app
TA). Elles ne changent aucun instant UTC, aucun ``occurrence_id``, aucune
valeur économique -- seulement l'affichage et la méthode de hachage. Les
deux apps restent réconciliables événement par événement sur
``occurrence_id`` (fonction de ``event_type_id`` + UTC uniquement, donc
inchangé). À porter dans calendar_core.py lors de l'extraction du module
commun ``bluestar_shared.calendar_rules``.
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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
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

# Source unique de vérité pour les constantes partagées avec le reste de
# l'app (URL du flux, TTL, fenêtre de risque résiduel, fuseau d'affichage).
from . import config as _C

logger = logging.getLogger(__name__)

# =============================================================================
# SECTION 1 -- NORMALISATION
# =============================================================================

SCHEMA_VERSION = "2.2.0"  # 2.2.0 : additif SourceInfo.feed_status + CoverageInfo (synthèse v6, cf. en-tête)
PAIR_MAPPING_METHOD = "static_currency_membership_v1"
SESSION_POLICY_VERSION = "exchange_local_dst_aware_v1"
NUMERIC_PARSER_VERSION = "ff_numeric_v2"
CONTENT_HASH_METHOD = "economic_projection_v2"

UTC = timezone.utc

TZ_LONDON = ZoneInfo("Europe/London")
TZ_NEW_YORK = ZoneInfo("America/New_York")
TZ_TOKYO = ZoneInfo("Asia/Tokyo")

# [F2] Fuseau d'AFFICHAGE lu depuis config.TZ_CET (Europe/Paris) : le HTML
# macro ne peut plus mélanger deux fuseaux. Override possible pour les tests
# ou un desk situé ailleurs.
_FALLBACK_DISPLAY_TZ = "Europe/Paris"
DISPLAY_TIMEZONE = (
    os.getenv("BLUESTAR_DISPLAY_TZ")
    or getattr(_C.TZ_CET, "key", _FALLBACK_DISPLAY_TZ)
)
DEFAULT_DISPLAY_TZ = DISPLAY_TIMEZONE  # alias de compatibilité v4

# Placeholder d'affichage unique, aligné sur models.MacroEvent.from_enriched
# et sur le test ``e['actual'] != '—'`` de app.py. [F3]
ABSENT_DISPLAY = "—"

SESSION_HOURS = {
    "LONDON": (TZ_LONDON, 8 * 60, 16 * 60 + 30),
    "NEW_YORK": (TZ_NEW_YORK, 8 * 60, 17 * 60),
    "TOKYO": (TZ_TOKYO, 8 * 60, 17 * 60),
}

# [F7] Table fixe : ``strftime("%A")`` dépend de la locale du conteneur.
_DAY_NAMES = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
              "FRIDAY", "SATURDAY", "SUNDAY")

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
    """Appartenance mécanique de la devise à la paire. Aucune causalité."""
    ccy = (ccy or "").upper()
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
    "HOLIDAY": Impact.HOLIDAY, "NON-ECONOMIC": Impact.HOLIDAY,
    "GRAY": Impact.HOLIDAY, "GREY": Impact.HOLIDAY,
}

# Projection ``priority`` consommée comme variable de DÉCISION par
# macro_engine (determine_market_regime / _compute_asset_score /
# build_catalysts). Seuils identiques à ``time_proximity``. [inchangé v4]
PRIORITY_PAST = "PAST"
PRIORITY_CRITICAL = "CRITICAL"
PRIORITY_HIGH = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    """[F7] Hash des octets réels, pas d'un décodage ``errors='replace'``."""
    return hashlib.sha256(payload).hexdigest()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return _SLUG_RE.sub("-", norm.lower()).strip("-")


def parse_source_datetime(raw: Any) -> datetime:
    """Accepte 'Z', '+00:00', '-04:00', naïf (traité UTC). Retourne aware UTC."""
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


# --- Parseur numérique -------------------------------------------------------
_NUM_RE = re.compile(
    r"^([<>~≈]?)\s*(-?[\d]+(?:[.,][\d]+)*)\s*([KMBT]?)\s*(%?)$", re.IGNORECASE
)
_SCALES = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_THOUSANDS_COMMA = re.compile(r"^-?\d{1,3}(?:,\d{3})+$")
_THOUSANDS_DOT = re.compile(r"^-?\d{1,3}(?:\.\d{3})+$")


def _to_float(text: str) -> Optional[float]:
    """[F6] Distingue séparateur de milliers et séparateur décimal.

    Règles, dans l'ordre :
      1. Les deux séparateurs présents → le DERNIER rencontré est le
         décimal, l'autre est un séparateur de milliers ("1.234,5" → 1234.5,
         "1,234.5" → 1234.5).
      2. Un seul séparateur, en groupes de 3 exacts ("1,234", "12.345.678")
         → séparateur de milliers.
      3. Sinon → séparateur décimal ("1,5" → 1.5, "0.25" → 0.25).
    Retourne ``None`` si non convertible (le caller émet UNPARSEABLE).
    """
    t = text.strip()
    has_comma, has_dot = "," in t, "." in t
    try:
        if has_comma and has_dot:
            if t.rfind(",") > t.rfind("."):
                return float(t.replace(".", "").replace(",", "."))
            return float(t.replace(",", ""))
        if has_comma:
            if _THOUSANDS_COMMA.match(t):
                return float(t.replace(",", ""))
            return float(t.replace(",", "."))
        if has_dot:
            if _THOUSANDS_DOT.match(t):
                return float(t.replace(".", ""))
            return float(t)
        return float(t)
    except ValueError:
        return None


class NumericValue(BaseModel):
    """Valeur économique : brut conservé + interprétation numérique explicite."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    scale: Optional[str] = None
    parse_status: str = "ABSENT"


ABSENT_NUMERIC = NumericValue()
_PLACEHOLDERS = {"", "-", "—", "–", "n/a", "n.a.", "na", "null", "none", "tentative"}


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
    if "|" in candidate:                      # ex. "0.3% | 1.2% y/y"
        candidate = candidate.split("|", 1)[0].strip()
        status = "COMPOSITE"

    m = _NUM_RE.match(candidate)
    if not m:
        return NumericValue(raw=text, parse_status="UNPARSEABLE")

    prefix, number, suffix, pct = m.groups()
    base = _to_float(number)
    if base is None:
        return NumericValue(raw=text, parse_status="UNPARSEABLE")

    if prefix and status == "PARSED":
        status = "APPROXIMATE"                # "<0.1%", "~2.5"

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

    has_ldn, has_ny, has_tky = ("LONDON" in active, "NEW_YORK" in active,
                                "TOKYO" in active)
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


# --- Politique de sélection --------------------------------------------------
# [F1] Horizon de veille. Doit rester synchronisé avec v10.WATCH_MAX_H (même
# dette de duplication assumée que TIER_WINDOWS).
FF_WATCH_HORIZON_H = 168.0

# [F7] Dérivé de config au lieu d'être dupliqué : cohérent avec le
# ``since_h=72`` de macro_engine._events_for_ccys (gating de blackout) et
# avec le split events / events_engine plus bas.
MACRO_RESIDUAL_RISK_WINDOW_H = float(_C.RESIDUAL_RISK_WINDOW_H)


class SelectionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = "1.1.0"
    impact_levels: Tuple[Impact, ...] = (Impact.HIGH,)
    currencies: Optional[Tuple[str, ...]] = None
    include_global_events: bool = True
    # Fenêtre passée = fenêtre de risque résiduel du moteur macro.
    window_past_hours: float = MACRO_RESIDUAL_RISK_WINDOW_H
    # [F7] Fenêtre servie == fenêtre annoncée (168h), plus 192h vs 168h.
    window_future_hours: float = FF_WATCH_HORIZON_H
    imminent_hours: float = 6.0          # INCHANGÉ (décisionnel)
    soon_hours: float = 48.0             # INCHANGÉ (décisionnel)
    display_timezone: str = DISPLAY_TIMEZONE
    max_source_age_seconds: int = int(_C.CALENDAR_CACHE_TTL) * 3
    max_events: int = 6000               # 2 semaines de flux, tous impacts
    include_holidays_in_metadata: bool = True

    @field_validator("currencies")
    @classmethod
    def _upper(cls, v):
        return None if v is None else tuple(sorted({c.upper() for c in v}))

    @field_validator("display_timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
            return v
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            logger.error("display_timezone '%s' inconnu — repli sur %s",
                         v, _FALLBACK_DISPLAY_TZ)
            return _FALLBACK_DISPLAY_TZ

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
    source_feed: str = ""            # v5 : quel flux a fourni la ligne
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
    urls: Tuple[str, ...] = ()            # v5 : fusion multi-flux [F1]
    feeds_ok: int = 0
    feeds_total: int = 0
    fetched_at_utc: datetime              # [F4] horodatage RÉEL du fetch
    fetch_duration_ms: int = 0
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    payload_bytes: int = 0
    payload_sha256: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    supports_actual: bool = False
    from_last_known_good: bool = False
    # [S1] Statut par flux ("ok" / "absent_404" / "error:CODE"), emprunté à
    # calendar_ingestor.SourceInfo.feed_status. Exclu du content_hash (root
    # "source" déjà exclu) : la disponibilité variable du flux bonus ne doit
    # jamais faire dériver le hash économique. Vide ({}) si l'appelant a
    # construit ce SourceInfo directement (compat totale, cf. build_payload).
    feed_status: Dict[str, str] = Field(default_factory=dict)

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


class CoverageInfo(BaseModel):
    """[S2] Emprunté à calendar_core.CoverageInfo. Sépare deux causes bien
    distinctes d'absence de devise dans l'artefact final :

      - currencies_excluded_by_policy : la source avait des événements pour
        cette devise sur la fenêtre, mais à un niveau d'impact (ou hors
        filtre devise) non retenu par la policy active. Artefact de
        configuration, PAS une information de marché.
      - currencies_no_data_in_source : la source n'avait AUCUN événement
        pour cette devise sur la fenêtre, quel que soit l'impact. Calendrier
        réellement creux -- statut neutre, sans dramatisation.

    Purement diagnostique : n'influence ni la sélection, ni ``priority``,
    ni ``is_blackout``, ni le content_hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start_utc: str
    window_end_utc: str
    currencies_scope: Tuple[str, ...]
    currencies_covered: Tuple[str, ...]
    currencies_excluded_by_policy: Tuple[str, ...]
    currencies_no_data_in_source: Tuple[str, ...]


class CalendarPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    generated_at_utc: datetime
    generator: str = "bluestar-calendar-macro-unified-v6"
    content_hash: Optional[str] = None
    content_hash_method: str = CONTENT_HASH_METHOD
    source: SourceInfo
    quality: QualityInfo
    selection_policy: SelectionPolicy
    # [S2] Optionnel (None) pour rester constructible par tout code qui
    # bâtirait encore un CalendarPayload sans ce champ -- build_payload() le
    # renseigne systématiquement.
    coverage: Optional[CoverageInfo] = None
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


# --- Regroupement des publications liées ------------------------------------
_RATE_KEYWORDS = (
    "official cash rate", "overnight rate", "rate statement", "cash rate",
    "monetary policy statement", "interest rate", "policy rate",
    "main refinancing", "federal funds", "bank rate", "fomc statement",
)
_PRESSER_KEYWORDS = ("press conference", "monetary policy press")
_LABOR_KEYWORDS = (
    "non-farm employment change", "unemployment rate", "average hourly earnings",
    "employment change", "claimant count",
)


def _match(title: str, keywords: Sequence[str]) -> bool:
    low = (title or "").lower()
    return any(k in low for k in keywords)


def assign_release_groups(rows: List[Dict[str, Any]]) -> None:
    """Groupe (mutation in place de 'release_group_*') :
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


def _coverage_diagnostics(
    rows: List[Dict[str, Any]],
    selected: List[Dict[str, Any]],
    policy: SelectionPolicy,
    lo: datetime,
    hi: datetime,
) -> CoverageInfo:
    """[S2] Emprunté à calendar_core._coverage_diagnostics. Compare les
    lignes normalisées AVANT filtre de policy (``rows``, restreintes à la
    fenêtre ``[lo, hi]``) et APRÈS (``selected``) pour séparer exclusion-
    policy et silence-source réels, devise par devise."""
    in_window = [r for r in rows if not r["is_global"] and lo <= r["scheduled_at_utc"] <= hi]

    scope: Tuple[str, ...] = policy.currencies if policy.currencies is not None else KNOWN_CURRENCIES
    raw_currencies_in_window = {r["currency"] for r in in_window}
    covered = {r["currency"] for r in selected if not r["is_global"]}

    excluded_by_policy: List[str] = []
    no_data_in_source: List[str] = []
    for ccy in scope:
        if ccy in covered:
            continue
        if ccy in raw_currencies_in_window:
            excluded_by_policy.append(ccy)
        else:
            no_data_in_source.append(ccy)

    return CoverageInfo(
        window_start_utc=iso_z(lo),
        window_end_utc=iso_z(hi),
        currencies_scope=tuple(sorted(scope)),
        currencies_covered=tuple(sorted(covered)),
        currencies_excluded_by_policy=tuple(sorted(excluded_by_policy)),
        currencies_no_data_in_source=tuple(sorted(no_data_in_source)),
    )


def render_coverage_note(coverage: Optional[CoverageInfo],
                         impact_levels: Sequence[Impact]) -> Optional[str]:
    """[S2] Formulation neutre pour un rendu desk. Jamais de vocabulaire de
    risque ("fail-closed", "non écarté") pour un simple constat de
    périmètre de données ; la distinction policy vs source réelle reste
    explicite mais factuelle. Retourne ``None`` si ``coverage`` est absent
    (compat)."""
    if coverage is None:
        return None
    levels = "+".join(lvl.value for lvl in impact_levels)

    if not coverage.currencies_excluded_by_policy and not coverage.currencies_no_data_in_source:
        return f"Couverture calendrier complète ({levels}) sur la fenêtre analysée."

    parts = [f"Couverture calendrier ({levels}) : {', '.join(coverage.currencies_covered) or '—'}."]
    if coverage.currencies_excluded_by_policy:
        parts.append(
            "Hors périmètre de sélection actif (données disponibles, non retenues) : "
            f"{', '.join(coverage.currencies_excluded_by_policy)}.")
    if coverage.currencies_no_data_in_source:
        parts.append(
            "Aucune publication programmée sur la fenêtre pour : "
            f"{', '.join(coverage.currencies_no_data_in_source)}.")
    return " ".join(parts)


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


def compute_priority(hours_until: float,
                     policy: SelectionPolicy = DEFAULT_POLICY) -> str:
    """Projection ``priority`` attendue par macro_engine.

    Champ de DÉCISION : determine_market_regime (CRITICAL),
    _compute_asset_score (HIGH → catalyst_pen), build_catalysts
    (CRITICAL/HIGH vs MEDIUM). Seuils IDENTIQUES à ``time_proximity``.
    """
    if hours_until <= 0:
        return PRIORITY_PAST
    if hours_until <= policy.imminent_hours:
        return PRIORITY_CRITICAL
    if hours_until <= policy.soon_hours:
        return PRIORITY_HIGH
    return PRIORITY_MEDIUM


def _normalize_row(
    raw: Any, index: int, policy: SelectionPolicy, display_tz: ZoneInfo,
    feed: str = "",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(raw, dict):
        return None, f"idx={index}: root element is {type(raw).__name__}, expected object"

    title = str(raw.get("title", "") or "").strip()
    if not title:
        return None, f"idx={index}: missing title"

    country = str(raw.get("country", "") or "").strip().upper()
    impact = IMPACT_ALIASES.get(
        str(raw.get("impact", "") or "").strip().upper(), Impact.UNKNOWN)

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
        # occurrence_id : fonction de (event_type_id, UTC) UNIQUEMENT — donc
        # identique entre les deux apps quel que soit le fuseau d'affichage.
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
        "day_of_week": _DAY_NAMES[local.weekday()],       # [F7] locale-safe
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
        "source_feed": feed,
    }, None


def build_payload(
    raw_list: Any,
    *,
    source: SourceInfo,
    now_utc: datetime,
    policy: SelectionPolicy = DEFAULT_POLICY,
    extra_warnings: Sequence[str] = (),
) -> CalendarPayload:
    """Transforme le payload brut en artefact canonique validé.

    Lève ``ValueError`` uniquement sur une erreur de PROGRAMMATION (racine
    non-liste, volume absurde) — ``build_calendar()`` la rattrape et dégrade.
    """
    if not isinstance(raw_list, list):
        raise ValueError(f"source root must be a JSON array, got {type(raw_list).__name__}")
    if len(raw_list) > policy.max_events:
        raise ValueError(f"payload too large: {len(raw_list)} > {policy.max_events}")

    display_tz = policy.display_tz()
    warnings: List[str] = list(extra_warnings)
    rejections: List[str] = []
    rows: List[Dict[str, Any]] = []

    for index, raw in enumerate(raw_list):
        feed = raw.get("_feed", "") if isinstance(raw, dict) else ""
        payload_row = {k: v for k, v in raw.items() if k != "_feed"} if isinstance(raw, dict) else raw
        row, err = _normalize_row(payload_row, index, policy, display_tz, feed)
        if err:
            rejections.append(err)
            continue
        rows.append(row)

    if any(r["impact"] is Impact.UNKNOWN for r in rows):
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

    # [S2] Diagnostic de couverture par devise -- AVANT dédoublonnage (qui ne
    # change aucune devise) et sur la même fenêtre [lo, hi] que la sélection.
    coverage = _coverage_diagnostics(rows, selected, policy, lo, hi)

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
        events.append(event.with_time_context(
            compute_time_context(event, now_utc, policy)))

    # [F4] Âge réel de la source (fetched_at_utc n'est plus == now_utc).
    age = max(0, int((now_utc - source.fetched_at_utc).total_seconds()))
    is_stale = age > policy.max_source_age_seconds
    if is_stale:
        warnings.append(f"SOURCE_AGE_EXCEEDS_{policy.max_source_age_seconds}S")
    if source.from_last_known_good:
        warnings.append("SERVING_LAST_KNOWN_GOOD")
    if not source.supports_actual:
        warnings.append("SOURCE_DOES_NOT_PROVIDE_ACTUAL")

    # [S1] Un flux SECONDAIRE (nextweek) absent ou en échec n'est PAS traité
    # comme le flux primaire : un 404 mi-semaine est la condition NORMALE de
    # la source (cf. en-tête [F1]/[S1]) et une erreur dessus reste un simple
    # bonus manqué. Repli explicite sur l'ancien calcul feeds_ok/feeds_total
    # si feed_status est vide (compat avec tout SourceInfo construit à la
    # main, hors fetch_raw).
    thisweek_status = source.feed_status.get("thisweek")
    nextweek_status = source.feed_status.get("nextweek")
    primary_failed = False
    if thisweek_status is not None:
        primary_failed = thisweek_status != "ok"
        if primary_failed:
            warnings.append(f"PRIMARY_FEED_FAILED:{thisweek_status}")
        if nextweek_status and nextweek_status not in ("ok", "absent_404"):
            warnings.append(f"SECONDARY_FEED_ERROR:{nextweek_status}")
    elif source.feeds_total and source.feeds_ok < source.feeds_total:
        # Ancien comportement (v5), conservé à l'identique quand la
        # granularité par flux n'est pas disponible.
        primary_failed = True
        warnings.append(f"PARTIAL_FEED_COVERAGE:{source.feeds_ok}/{source.feeds_total}")

    all_times = [r["scheduled_at_utc"] for r in rows]
    coverage_start = min(all_times) if all_times else None
    coverage_end = max(all_times) if all_times else None
    if coverage_end is not None and coverage_end < now_utc:
        warnings.append("ALL_SOURCE_EVENTS_IN_THE_PAST_WEEK_ROLLOVER_PENDING")
    if coverage_start is not None and coverage_start > now_utc + timedelta(days=9):
        warnings.append("SOURCE_COVERAGE_STARTS_TOO_FAR_IN_FUTURE")
    if not rows:
        warnings.append("EMPTY_NORMALIZED_PAYLOAD")
    if rows and not selected:
        # Cas piégeux : flux joignable, lignes normalisées, mais AUCUNE ne
        # passe le filtre d'impact. Sans ce warning, l'app affiche 0 event
        # avec reachable=True — silence indiscernable d'un jour calme.
        warnings.append("NO_EVENT_MATCHED_SELECTION_POLICY")

    score = 1.0
    if is_stale:
        score -= 0.35
    if source.from_last_known_good:
        score -= 0.25
    if rejections:
        score -= min(0.25, 0.05 * len(rejections))
    if primary_failed:
        score -= 0.15
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
        warnings=tuple(dict.fromkeys(warnings)),   # dédoublonne, ordre stable
        rejections=tuple(rejections[:50]),
    )

    payload = CalendarPayload(
        generated_at_utc=now_utc,
        source=source,
        quality=quality,
        selection_policy=policy,
        coverage=coverage,
        events=tuple(events),
    )
    return payload.model_copy(update={"content_hash": canonical_content_hash(payload)})


def canonical_content_hash(payload: CalendarPayload) -> str:
    """[F5] SHA-256 d'une PROJECTION ÉCONOMIQUE explicite.

    Insensible à : fuseau d'affichage, ordre/origine de fusion des flux,
    horodatages volatils, métadonnées de qualité. Deux instances de l'app
    (macro / TA) configurées avec des fuseaux d'affichage différents
    produisent donc le MÊME hash pour le même calendrier — ce que la v4
    ne pouvait structurellement pas garantir.
    """
    projection = [
        {
            "occurrence_id": e.occurrence_id,
            "event_type_id": e.event_type_id,
            "scheduled_at_utc": iso_z(e.scheduled_at_utc),
            "currency": e.currency,
            "name": e.name,
            "impact": e.impact.value,
            "forecast": e.forecast.raw,
            "previous": e.previous.raw,
            "actual": e.actual.raw,
            "release_group_id": e.release_group_id,
        }
        for e in payload.events
    ]
    canonical = json.dumps(
        {"method": CONTENT_HASH_METHOD, "events": projection},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
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
    """Libellé « UTC+H » calculé sur l'offset RÉEL du datetime localisé.

    Jamais codé en dur : un « +1 » fixe serait faux en Europe/Paris l'hiver
    comme en Africa/Casablanca pendant le Ramadan. is_blackout / priority ne
    sont pas concernés (UTC pur via hours_until) mais l'AFFICHAGE doit rester
    exact toute l'année et pour n'importe quel display_timezone.
    """
    offset = dt.utcoffset()
    if offset is None:
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hh}" + (f":{mm:02d}" if mm else "")


def _disp(nv: NumericValue) -> str:
    """[F3] Contrat d'affichage : toujours une chaîne, jamais None, jamais ""
    — ``ABSENT_DISPLAY`` ("—") quand le flux ne fournit pas la valeur.
    Aligné sur models.MacroEvent.from_enriched et sur le test
    ``e['actual'] != '—'`` de app.py."""
    return nv.raw if nv.raw else ABSENT_DISPLAY


def to_legacy_payload(payload: CalendarPayload, now_utc: datetime) -> Dict[str, Any]:
    """Vue « legacy » consommée par macro_engine / renderer / app.py.
    ``build_calendar()`` y ajoute ensuite priority / tier / blackout et les
    champs de couverture de flux."""
    events = refresh_time_contexts(payload, now_utc)
    policy = payload.selection_policy
    rows: List[Dict[str, Any]] = []
    summary: Dict[str, List[str]] = {}

    for e in events:
        ctx = e.time_context
        offset_lbl = _utc_offset_label(e.scheduled_at_display)
        rows.append({
            "occurrence_id": e.occurrence_id,
            "event_type_id": e.event_type_id,
            "release_group_id": e.release_group_id,
            "release_group_type": (e.release_group_type.value
                                   if e.release_group_type else None),
            "currency": e.currency,
            "event_name": e.name,
            "datetime_utc": iso_z(e.scheduled_at_utc),
            "date_display": e.date_display,
            "time_display": f"{e.scheduled_at_display.strftime('%H:%M')} ({offset_lbl})",
            "datetime_display": (f"{e.date_display} · "
                                 f"{e.scheduled_at_display.strftime('%H:%M')} ({offset_lbl})"),
            "display_timezone": e.display_timezone,
            "day_of_week": e.day_of_week,
            "impact": e.impact.value.lower(),
            "forecast": _disp(e.forecast),
            "forecast_value": e.forecast.value,
            "previous": _disp(e.previous),
            "previous_value": e.previous.value,
            "actual": _disp(e.actual),
            "actual_value": e.actual.value,
            "actual_status": e.actual_status.value,
            "hours_until": ctx.hours_until,
            "hours_until_display": ctx.hours_until_display,
            "is_upcoming": ctx.is_upcoming,
            "time_proximity": ctx.time_proximity.value,
            "status": ctx.status.value,
            "session": _LEGACY_SESSION.get(e.session, e.session.value),  # [F7]
            "session_v2": e.session.value,
            "pairs_affected": list(e.pairs_with_currency_exposure),
            "pair_mapping_status": e.pair_mapping_status.value,
            "source_feed": e.source_feed,
        })
        summary.setdefault(e.date_display, []).append(f"{e.currency} – {e.name}")

    return {
        "metadata": {
            "schema_version": f"legacy-1.2.0+core-{payload.schema_version}",
            "generated_at_utc": iso_z(payload.generated_at_utc),
            "content_hash": payload.content_hash,
            "content_hash_method": payload.content_hash_method,
            "source": payload.source.provider,
            "source_url": payload.source.url,
            "source_urls": list(payload.source.urls),
            "feeds_ok": payload.source.feeds_ok,
            "feeds_total": payload.source.feeds_total,
            # [S1] Statut par flux ("ok"/"absent_404"/"error:CODE"), même clé
            # ("feeds_status") que calendar_core.to_legacy_payload côté app TA.
            "feeds_status": dict(payload.source.feed_status),
            "fetched_at_utc": iso_z(payload.source.fetched_at_utc),
            "supports_actual": payload.source.supports_actual,
            "timezone": f"UTC (backend) / {policy.display_timezone} (display)",
            "display_timezone": policy.display_timezone,
            "window_past_hours": policy.window_past_hours,
            "window_future_hours": policy.window_future_hours,
            "imminent_hours": policy.imminent_hours,
            "soon_hours": policy.soon_hours,
            "quality_status": payload.quality.status.value,
            "data_quality_score": payload.quality.data_quality_score,
            "is_stale": payload.quality.is_stale,
            "source_age_seconds": payload.quality.source_age_seconds,
            "raw_event_count": payload.quality.raw_event_count,
            "rejected_event_count": payload.quality.rejected_event_count,
            "warnings": list(payload.quality.warnings),
            "rejections": list(payload.quality.rejections),
            "total_high_impact": len(rows),
            "upcoming_count": sum(1 for r in rows if r["is_upcoming"]),
            "imminent_count": sum(1 for r in rows if r["time_proximity"] == "IMMINENT"),
            "engine_events_count": len(rows),
            "summary_by_day_basis": "display_timezone",
            "ui_filters_applied": None,
            # [S2] Couverture par devise (policy vs source réelle), mêmes
            # noms de clé que calendar_core.to_legacy_payload.
            "coverage_window_start_utc": payload.coverage.window_start_utc if payload.coverage else None,
            "coverage_window_end_utc": payload.coverage.window_end_utc if payload.coverage else None,
            "currencies_scope": list(payload.coverage.currencies_scope) if payload.coverage else [],
            "currencies_covered": list(payload.coverage.currencies_covered) if payload.coverage else [],
            "currencies_excluded_by_policy": (
                list(payload.coverage.currencies_excluded_by_policy) if payload.coverage else []),
            "currencies_no_data_in_source": (
                list(payload.coverage.currencies_no_data_in_source) if payload.coverage else []),
            "coverage_note": render_coverage_note(payload.coverage, policy.impact_levels),
        },
        "events": rows,
        "events_engine": rows,
        "summary_by_day": {k: summary[k] for k in sorted(summary)},
    }


# =============================================================================
# SECTION 2 -- BLACKOUT (réplique EXACTE de v10.py / Desk Engine).
# Sujet ORTHOGONAL à la normalisation ci-dessus : ne JAMAIS fusionner cette
# logique avec calendar_core (l'app TA n'a pas cette notion).
# IMPORTANT : toute modification de TIER_WINDOWS dans v10.py DOIT être
# répercutée ici à l'identique. Dette de duplication assumée.
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
    """Identique à v10.classify_tier — mêmes mots-clés, même ordre (S>A>B)."""
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
    après l'annonce — réplique de v10.CalendarData.bucket().

    ``hours_until`` suit la convention de ``build_calendar()`` : positif =
    futur, négatif = déjà passé. Retourne ``(bloqué: bool, tier: str)``.

    NB : la fenêtre passée la plus large est 48h (tier S) — d'où le
    ``since_h=72`` de macro_engine._events_for_ccys, strictement suffisant,
    et d'où ``window_past_hours = RESIDUAL_RISK_WINDOW_H = 72``.
    """
    tier = classify_tier(event_name)
    before, after = TIER_WINDOWS.get(tier, DEFAULT_TIER_WINDOW)
    return (-after <= hours_until <= before), tier


# =============================================================================
# SECTION 3 -- FETCH (résilient, SANS état persistant : pas de circuit
# breaker, pas de last-known-good disque, pas de health.json).
# Deux GET SÉQUENTIELS sur la même session — surtout PAS de ThreadPoolExecutor
# ici : macro_engine documente un SIGSEGV (curl_cffi/libcurl non thread-safe)
# sur les exécuteurs imbriqués.
# =============================================================================

SOURCE_PROVIDER = "Forex Factory / Fair Economy weekly public feed"
USER_AGENT = os.getenv("BLUESTAR_USER_AGENT",
                       "BluestarCalendarMacroUnified/5.0 (+ops@bluestar)")

_THISWEEK_URL = os.getenv("BLUESTAR_SOURCE_URL", _C.FF_JSON_URL)


def _derive_nextweek_url(thisweek_url: str) -> Optional[str]:
    """[F1] Dérive l'URL du flux de la semaine suivante depuis celle de la
    semaine courante (``ff_calendar_thisweek.json`` →
    ``ff_calendar_nextweek.json``). Aucune URL codée en dur : si le nom du
    flux change côté config, la dérivation suit ou se désactive proprement."""
    override = os.getenv("BLUESTAR_SOURCE_URL_NEXT")
    if override:
        return override
    if "thisweek" in thisweek_url:
        return thisweek_url.replace("thisweek", "nextweek")
    return None


SOURCE_URL = _THISWEEK_URL                      # compat v3/v4
_NEXTWEEK_URL = _derive_nextweek_url(_THISWEEK_URL)
SOURCE_URLS: Tuple[str, ...] = tuple(u for u in (_THISWEEK_URL, _NEXTWEEK_URL) if u)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


def _build_session() -> requests.Session:
    session = requests.Session()
    retry_kwargs: Dict[str, Any] = dict(
        total=4, connect=3, read=3, status=3,
        backoff_factor=1.5,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    try:
        # [F7] backoff_jitter n'existe qu'à partir d'urllib3 2.0 : sur 1.x le
        # constructeur levait TypeError → fetch mort → calendrier vide.
        retry = Retry(backoff_jitter=0.4, **retry_kwargs)
    except TypeError:
        logger.debug("urllib3 < 2.0 : backoff_jitter indisponible")
        retry = Retry(**retry_kwargs)
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
    """Conservé pour compatibilité d'import. Le fetch ne lève jamais :
    ``fetch_raw`` retourne toujours ``(liste, méta)``."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _fetch_one(session: requests.Session, url: str) -> Tuple[List[Dict], Dict[str, Any]]:
    """Un seul flux. Ne lève JAMAIS : retourne ``([], {"ok": False, ...})``."""
    started = time.monotonic()
    label = "nextweek" if "nextweek" in url else "thisweek"
    fail = {"ok": False, "url": url, "feed": label, "status": "error:UNKNOWN"}
    try:
        response = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True)
    except requests.Timeout as exc:
        logger.error("Calendar fetch failed (%s): timeout (%s)", label, exc)
        return [], {**fail, "status": "error:NETWORK_TIMEOUT"}
    except requests.RequestException as exc:
        logger.error("Calendar fetch failed (%s): %s", label, exc)
        return [], {**fail, "status": "error:NETWORK_ERROR"}

    try:
        with response:
            if response.status_code >= 400:
                # [S1] Un 404 sur le flux secondaire est la condition NORMALE
                # de la source en milieu de semaine (cf. [F1]/[S1]) : c'est un
                # STATUT, pas une erreur à crier au même niveau qu'une vraie
                # panne. Le flux primaire n'a, lui, jamais de 404 "normal".
                status = ("absent_404" if response.status_code == 404
                          else f"error:HTTP_{response.status_code}")
                log = logger.info if status == "absent_404" else logger.error
                log("Calendar fetch (%s): HTTP %s (%s)", label, response.status_code, status)
                return [], {**fail, "http_status": response.status_code, "status": status}

            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_PAYLOAD_BYTES:
                    logger.error("Calendar fetch failed (%s): payload too large (%d B)",
                                 label, size)
                    return [], {**fail, "status": "error:PAYLOAD_TOO_LARGE"}
                chunks.append(chunk)
            body = b"".join(chunks)

            meta = {
                "ok": True,
                "url": url,
                "feed": label,
                "status": "ok",
                "http_status": response.status_code,
                "content_type": (response.headers.get("Content-Type") or "")
                                .split(";")[0].strip() or None,
                "payload_bytes": size,
                # [F7] hash des octets réels
                "payload_sha256": "sha256:" + sha256_bytes(body),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "fetch_duration_ms": int((time.monotonic() - started) * 1000),
            }
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, list):
            logger.error("Calendar fetch failed (%s): root is %s, expected array",
                         label, type(parsed).__name__)
            return [], {**fail, "status": "error:SCHEMA_ROOT_NOT_ARRAY"}
        for row in parsed:
            if isinstance(row, dict):
                row["_feed"] = label
        return parsed, meta
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("Calendar fetch failed (%s): invalid JSON (%s)", label, exc)
        return [], {**fail, "status": "error:INVALID_JSON"}
    except Exception as exc:                      # ceinture + bretelles
        logger.error("Calendar fetch failed (%s): %s", label, exc)
        return [], {**fail, "status": f"error:{type(exc).__name__}"}


def _raw_dedupe_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    """Clé de dédoublonnage inter-flux, robuste aux formats de date."""
    try:
        stamp = iso_z(parse_source_datetime(row.get("date")))
    except (ValueError, TypeError):
        stamp = str(row.get("date"))
    return (str(row.get("country", "") or "").upper(),
            str(row.get("title", "") or "").strip().lower(),
            stamp)


def fetch_raw(urls: Optional[Iterable[str]] = None) -> Tuple[List[Dict], Dict[str, Any]]:
    """[F1] Fetch résilient MULTI-FLUX (thisweek + nextweek), sans état.

    Séquentiel et volontairement non parallélisé (voir en-tête de section).
    Retourne ``([], meta)`` sur échec total — ``build_calendar()`` dégrade
    proprement plutôt que de lever (même contrat que la v3).
    """
    url_list = list(urls) if urls is not None else list(SOURCE_URLS)
    session = _build_session()
    fetched_at = datetime.now(UTC)

    merged: List[Dict] = []
    metas: List[Dict[str, Any]] = []
    for url in url_list:
        rows, meta = _fetch_one(session, url)
        metas.append(meta)
        merged.extend(rows)

    seen: set = set()
    deduped: List[Dict] = []
    raw_dupes = 0
    for row in merged:
        if not isinstance(row, dict):
            deduped.append(row)
            continue
        key = _raw_dedupe_key(row)
        if key in seen:
            raw_dupes += 1
            continue
        seen.add(key)
        deduped.append(row)

    ok_metas = [m for m in metas if m.get("ok")]
    # [S1] Statut lisible par flux ("thisweek"/"nextweek" -> "ok"/
    # "absent_404"/"error:CODE"), emprunté à calendar_ingestor.feed_status.
    feed_status: Dict[str, str] = {}
    for m in metas:
        feed_status[m.get("feed", "?")] = m.get("status", "error:UNKNOWN")
    agg: Dict[str, Any] = {
        "fetched_at_utc": fetched_at,
        "urls": tuple(url_list),
        "feeds_total": len(url_list),
        "feeds_ok": len(ok_metas),
        "feeds": metas,
        "feed_status": feed_status,
        "raw_duplicates_dropped": raw_dupes,
        "fetch_duration_ms": sum(int(m.get("fetch_duration_ms") or 0) for m in metas),
        "payload_bytes": sum(int(m.get("payload_bytes") or 0) for m in metas),
    }
    if ok_metas:
        first = ok_metas[0]
        agg.update({
            "http_status": first.get("http_status"),
            "content_type": first.get("content_type"),
            "etag": first.get("etag"),
            "last_modified": first.get("last_modified"),
            "payload_sha256": "sha256:" + sha256_hex(
                "|".join(str(m.get("payload_sha256")) for m in ok_metas)),
        })
    if not ok_metas:
        logger.error("Calendar fetch: aucun flux disponible sur %d tentés",
                     len(url_list))
    elif len(ok_metas) < len(url_list):
        logger.warning("Calendar fetch: couverture partielle (%d/%d flux) — "
                       "l'horizon de veille peut être tronqué",
                       len(ok_metas), len(url_list))
    return deduped, agg


# =============================================================================
# SECTION 4 -- POINT D'ENTRÉE PUBLIC, compatible macro_engine.py / app.py /
# renderer.py sans AUCUNE modification côté consommateur.
# =============================================================================

def _empty_source(now_utc: datetime, meta: Dict[str, Any]) -> SourceInfo:
    fetched = meta.get("fetched_at_utc") or now_utc
    urls = tuple(meta.get("urls") or SOURCE_URLS)
    return SourceInfo(
        provider=SOURCE_PROVIDER,
        url=urls[0] if urls else SOURCE_URL,
        urls=urls,
        feeds_ok=int(meta.get("feeds_ok") or 0),
        feeds_total=int(meta.get("feeds_total") or len(urls)),
        fetched_at_utc=fetched,
        payload_sha256=str(meta.get("payload_sha256") or "sha256:unknown"),
        supports_actual=False,
        from_last_known_good=False,
        feed_status=dict(meta.get("feed_status") or {}),
    )


def _feed_bounds(raw_data: Sequence[Any]) -> Tuple[Optional[datetime], Optional[datetime],
                                                   Dict[str, int], List[Dict[str, str]]]:
    """Bornes de couverture du flux, TOUS impacts confondus (mesure la
    couverture réelle, pas celle des seuls high-impact retenus), + comptage
    par impact + jours fériés à venir. Aucune décision n'est prise ici : de
    la VISIBILITÉ, pas du gating."""
    start = end = None
    counts: Dict[str, int] = {}
    holidays: List[Dict[str, str]] = []
    for ev in raw_data:
        if not isinstance(ev, dict):
            continue
        try:
            t = parse_source_datetime(ev.get("date"))
        except (ValueError, TypeError):
            continue
        if start is None or t < start:
            start = t
        if end is None or t > end:
            end = t
        imp = IMPACT_ALIASES.get(
            str(ev.get("impact", "") or "").strip().upper(), Impact.UNKNOWN)
        counts[imp.value] = counts.get(imp.value, 0) + 1
        if imp is Impact.HOLIDAY:
            holidays.append({
                "date_utc": t.strftime("%Y-%m-%d"),
                "currency": str(ev.get("country", "") or "").upper(),
                "name": str(ev.get("title", "") or "").strip(),
            })
    return start, end, counts, holidays


def build_calendar(
    now_utc: Optional[datetime] = None,
    raw_data: Optional[List[Dict]] = None,
    policy: SelectionPolicy = DEFAULT_POLICY,
    urls: Optional[Iterable[str]] = None,
) -> Dict:
    """Construit le payload canonique avec le MÊME contrat de sortie que la
    v3/v4 (``metadata`` / ``events`` / ``events_engine`` / ``summary_by_day``),
    en s'appuyant en interne sur la normalisation canonique — donc garanti
    identique, événement par événement (au sens ``occurrence_id``), à ce que
    voit l'app TA.

    Quatre restaurations/corrections explicites par rapport à un branchement
    naïf de ``to_legacy_payload()`` :

      1. ``priority`` (CRITICAL/HIGH/MEDIUM/PAST) — absent du cœur canonique
         mais consommé comme variable de DÉCISION par macro_engine
         (determine_market_regime / _compute_asset_score / build_catalysts).
         Sans lui : incident du 05/08/2026, « Catalyseurs du Jour » vide.
      2. ``metadata.reachable`` / ``feed_horizon_h`` /
         ``feed_horizon_truncated`` — alimentent ``calendar_reachable``,
         ``calendar_feed_truncated`` et ``calendar_feed_horizon_h`` de
         BriefingContext (patch F-15 / P0-1 / V4-04), plus la caption et le
         KPI de app.py.
      3. ``events`` (à venir uniquement) distinct de ``events_engine``
         (à venir + passés dans MACRO_RESIDUAL_RISK_WINDOW_H).
      4. v5 — ``tier`` / ``is_blackout`` par ligne : le Desk et la Macro
         énoncent désormais la MÊME vérité de blackout sur la même donnée,
         et la diagnostic view de app.py peut l'afficher sans recalculer.

    Ne lève jamais : tout échec (réseau, JSON, flux hors-norme) dégrade vers
    un calendrier vide avec ``reachable = False``.
    """
    now_utc = now_utc or datetime.now(UTC)
    meta: Dict[str, Any] = {}
    fetched = False
    if raw_data is None:
        raw_data, meta = fetch_raw(urls)
        fetched = True
    raw_data = list(raw_data or [])

    # --- Couverture réelle du flux, avant tout filtrage --------------------
    feed_start, feed_end, impact_counts, holidays = _feed_bounds(raw_data)
    feed_horizon_h = ((feed_end - now_utc).total_seconds() / 3600.0) if feed_end else None
    feed_truncated = feed_horizon_h is not None and feed_horizon_h < FF_WATCH_HORIZON_H
    if not raw_data:
        # Flux injoignable : l'horizon n'est pas « tronqué », il est INEXISTANT.
        # Ne pas confondre les deux dans le HTML — reachable=False le dit déjà.
        feed_truncated = False
    elif feed_truncated:
        logger.warning(
            "FF feed horizon %.1fh < %.0fh — fenêtre WATCH non vérifiable au-delà "
            "du flux (couverture %d/%d) ; un silence calendaire n'est PAS une "
            "absence de risque",
            feed_horizon_h, FF_WATCH_HORIZON_H,
            int(meta.get("feeds_ok") or 0), int(meta.get("feeds_total") or 0))

    # --- Source + payload canonique ---------------------------------------
    urls_used = tuple(meta.get("urls") or (SOURCE_URLS if fetched else ()))
    source = SourceInfo(
        provider=SOURCE_PROVIDER,
        url=(urls_used[0] if urls_used else SOURCE_URL),
        urls=urls_used,
        feeds_ok=int(meta.get("feeds_ok") or (0 if fetched else 0)),
        feeds_total=int(meta.get("feeds_total") or len(urls_used)),
        # [F4] horodatage RÉEL du fetch (== now_utc seulement si raw_data
        # est injecté par un appelant, cas des tests).
        fetched_at_utc=(meta.get("fetched_at_utc") or now_utc),
        fetch_duration_ms=int(meta.get("fetch_duration_ms") or 0),
        http_status=meta.get("http_status"),
        content_type=meta.get("content_type"),
        payload_bytes=int(meta.get("payload_bytes") or 0),
        payload_sha256=str(meta.get("payload_sha256") or "sha256:unknown"),
        etag=meta.get("etag"),
        last_modified=meta.get("last_modified"),
        supports_actual=any(isinstance(r, dict) and "actual" in r for r in raw_data),
        from_last_known_good=False,
        feed_status=dict(meta.get("feed_status") or {}),
    )

    extra_warnings: List[str] = []
    if int(meta.get("raw_duplicates_dropped") or 0):
        extra_warnings.append(
            f"RAW_CROSS_FEED_DUPLICATES_DROPPED:{meta['raw_duplicates_dropped']}")

    # [F7] Volume hors-norme : tronquer + avertir, plutôt que lever et tuer
    # l'app Streamlit.
    if len(raw_data) > policy.max_events:
        logger.error("Calendar payload trop volumineux (%d > %d) — troncature",
                     len(raw_data), policy.max_events)
        extra_warnings.append(f"RAW_PAYLOAD_TRUNCATED_TO_{policy.max_events}")
        raw_data = raw_data[:policy.max_events]

    try:
        payload = build_payload(raw_data, source=source, now_utc=now_utc,
                                policy=policy, extra_warnings=extra_warnings)
    except Exception as exc:
        logger.error("build_payload a échoué (%s) — dégradation vers calendrier vide", exc)
        payload = build_payload([], source=_empty_source(now_utc, meta),
                                now_utc=now_utc, policy=policy,
                                extra_warnings=[*extra_warnings,
                                                f"BUILD_PAYLOAD_FAILED:{type(exc).__name__}"])
        raw_data = []

    legacy = to_legacy_payload(payload, now_utc)
    all_rows = legacy["events"]   # population complète (fenêtre policy)

    # --- 1. priority + 4. tier / blackout (par ligne) ----------------------
    for row in all_rows:
        h = row["hours_until"]
        row["priority"] = compute_priority(h, policy)
        blocked, tier = is_blackout(row["event_name"], h)
        row["tier"] = tier
        row["is_blackout"] = blocked

    # --- 3. Split events (à venir) / events_engine (à venir + résiduel) ----
    upcoming_rows = [r for r in all_rows if r["is_upcoming"]]
    engine_rows = [r for r in all_rows
                   if r["is_upcoming"] or r["hours_until"] >= -MACRO_RESIDUAL_RISK_WINDOW_H]
    legacy["events"] = upcoming_rows
    legacy["events_engine"] = engine_rows

    critical_count = sum(1 for r in all_rows if r["priority"] == PRIORITY_CRITICAL)
    high_count = sum(1 for r in all_rows if r["priority"] == PRIORITY_HIGH)
    medium_count = sum(1 for r in all_rows if r["priority"] == PRIORITY_MEDIUM)
    imminent_count = sum(1 for r in all_rows if r["time_proximity"] == "IMMINENT")
    blackout_rows = [r for r in engine_rows if r["is_blackout"]]

    next_event = None
    if upcoming_rows:
        nxt = upcoming_rows[0]
        next_event = {
            "currency": nxt["currency"],
            "event_name": nxt["event_name"],
            "hours_until": nxt["hours_until"],
            "priority": nxt["priority"],
            "datetime_display": nxt["datetime_display"],
        }

    # --- 2. Couverture du flux + reachable + diagnostics -------------------
    legacy["metadata"].update({
        "total_high_impact": len(all_rows),
        "upcoming_count": len(upcoming_rows),
        "critical_count": critical_count,
        "high_count": high_count,
        "medium_count": medium_count,
        "imminent_count": imminent_count,
        "engine_events_count": len(engine_rows),
        "blackout_count": len(blackout_rows),
        "blackout_currencies": sorted({r["currency"] for r in blackout_rows}),
        "next_event": next_event,
        # reachable : au moins un flux réellement servi (et non « la liste
        # n'est pas vide », qui confondait jour calme et panne réseau).
        "reachable": (int(meta.get("feeds_ok") or 0) > 0) if fetched else bool(raw_data),
        "feed_start_utc": iso_z(feed_start) if feed_start else None,
        "feed_end_utc": iso_z(feed_end) if feed_end else None,
        "feed_horizon_h": round(feed_horizon_h, 1) if feed_horizon_h is not None else None,
        "feed_horizon_truncated": feed_truncated,
        "feed_watch_horizon_h": FF_WATCH_HORIZON_H,
        "feed_impact_counts": impact_counts,
        "raw_duplicates_dropped": int(meta.get("raw_duplicates_dropped") or 0),
        "holidays_upcoming": ([h for h in holidays
                               if h["date_utc"] >= now_utc.strftime("%Y-%m-%d")][:10]
                              if policy.include_holidays_in_metadata else []),
        "priority_thresholds": {"critical_max_h": policy.imminent_hours,
                                "high_max_h": policy.soon_hours},
        "residual_risk_window_h": MACRO_RESIDUAL_RISK_WINDOW_H,
    })

    return legacy


# --- Compatibilité d'import (appelants directs existants) --------------------
PAIRS_MAP: Dict[str, List[str]] = {ccy: pairs_for_currency(ccy)
                                   for ccy in KNOWN_CURRENCIES}


def get_session(t: datetime) -> str:
    """Conservé pour compatibilité avec tout appelant direct existant.
    ``build_calendar()`` utilise en interne ``classify_session()`` (DST-aware),
    plus précis — cette fonction UTC-only n'est PAS dans le chemin de données
    réel."""
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
