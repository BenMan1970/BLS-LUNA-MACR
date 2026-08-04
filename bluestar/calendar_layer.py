"""Calendar Layer -- Forex Factory High-Impact feed (Data Integrity Layer).

Version 2 -- harmonisation avec le module Desk (audit du 04/08/2026).

Ce module reste un refactor 100% Streamlit-free, importable et testable :
aucune dépendance à `streamlit`, aucun effet de bord d'affichage. Le contrat
JSON (`metadata`, `events`, `events_engine`, `summary_by_day`) est préservé
pour ne rien casser côté modules consommateurs de l'app macro (report
generator, Committee, etc.).

CHANGEMENT DE FOND vs la v1 (round du 04/08/2026, audit calendrier
Desk/Macro) :
------------------------------------------------------------------
Le Desk (module `04_calendar.py`, dashboard Streamlit) a renommé son champ
`priority` en `time_proximity` et banni les valeurs "HIGH"/"MEDIUM" de ce
champ, précisément parce qu'elles entraient en collision avec le champ
`impact` (qui, dans ce flux filtré, vaut TOUJOURS "high"). Cette v1 du
module macro ne reprenait pas ce fix : elle exposait encore
`priority: "MEDIUM"` à côté de `impact: "high"` -- exactement la collision
supprimée côté Desk.

Conséquence observée : le générateur de rapport Macro Briefing devait
compenser ça a posteriori avec un patch cosmétique en aval :
    <!-- MACRO-A3 FIX : Le flux FF n'a pas de "Medium". Ces events sont
         "High" à >48h. Remplacement de "MODÉRÉ" par "ÉLEVÉ · >48h" -->
Ce patch soignait le symptôme, pas la cause. Cette v2 corrige la cause à la
source, à l'identique du Desk (mêmes seuils, mêmes libellés : IMMINENT ≤6h,
SOON ≤48h, LATER au-delà, PAST si passé).

Compatibilité descendante : le champ `priority` est conservé dans le JSON
(pour ne provoquer aucun KeyError chez un consommateur existant qui le lit
encore) mais n'est plus qu'un ALIAS de `time_proximity` -- il ne contient
donc plus jamais "HIGH"/"MEDIUM"/"CRITICAL", uniquement les valeurs
harmonisées Desk. C'est un champ DEPRECATED : à supprimer une fois que le
générateur de rapport (et le patch MACRO-A3, devenu obsolète) auront été
migrés vers `time_proximity`. Voir TODO-REMOVE-PRIORITY-ALIAS plus bas.

Le calcul de couverture réelle du flux (`feed_end_utc` / `feed_horizon_h` /
`feed_horizon_truncated`, patch F-15 du 31/07/2026) est propre à ce module
macro et n'existe pas côté Desk -- il est conservé tel quel : c'est une
information utile (mesurée sur TOUS les impacts, pas seulement les
high-impact) qui alimente l'alerte de couverture calendaire lue par le
Committee. Rien n'indique qu'elle doive être supprimée ou alignée sur le
Desk ; seule la logique de proximité temporelle (`priority`/`time_proximity`)
avait un vrai défaut de cohérence.

La logique de tiers de blackout (`TIER_WINDOWS` / `classify_tier` /
`is_blackout`) reste une réplique exacte de `v10.py` (Desk Engine), qui fait
autorité sur cette règle -- inchangée dans cette v2, cf. dette de duplication
déjà documentée ci-dessous (module partagé à extraire : voir
`bluestar_shared.calendar_rules`).

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

# PATCH-FEEDHORIZON (round du 31/07/2026, audit F-15).
# FF_JSON_URL pointe sur ff_calendar_thisweek.json : le flux est HEBDOMADAIRE.
# Un vendredi, l'horizon prospectif résiduel tombe sous les ~48h alors que la
# fenêtre WATCH du Desk Engine (v10.WATCH_MAX_H) est de 168h — un silence
# calendaire au-delà de la fin du flux n'est donc PAS une absence de risque,
# et le flag C7 (cohérence d'horizon) est neutralisé sans que rien ne le dise.
# L'endpoint multi-semaines recommandé par le harnais n'existe pas chez cet
# hébergeur (ff_calendar_nextweek.json -> HTTP 404, vérifié le 31/07/2026) :
# le seul correctif zero-régression disponible est la VISIBILITÉ de la
# troncature, pas un changement de flux. Doit rester synchronisé avec
# v10.WATCH_MAX_H (même dette de duplication assumée que TIER_WINDOWS
# ci-dessous, en attendant l'extraction en module commun).
FF_WATCH_HORIZON_H = 168.0

# P0-1 FIX (Incident Review Board, RC3): fetch_raw sent no User-Agent at all.
# This did not cause the observed 429s (rate-limiting, not UA-blocking -- see
# audit section3), but a bare python-requests UA is best avoided on a public
# JSON feed. Additive only: does not change retry/backoff/timeout behaviour.
_FF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Currency -> affected pairs (identique au module Desk -- même matrice,
# même 8 devises Forex Factory + CNY hérité ; aucun indice/métal ajouté
# faute de donnée source qui documente cette corrélation).
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
    """Map a UTC datetime to its FX session label (identique Desk & Macro)."""
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
    """Human-readable countdown; ``h <= 0`` => ``PASSED`` (identique Desk & Macro)."""
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
            r = requests.get(url, timeout=HTTP_TIMEOUT, headers=_FF_HEADERS)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:  # noqa: PERF203
            last_err = e
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_BACKOFF ** (attempt + 1))
    logger.error("Calendar fetch failed after retries: %s", last_err)
    return []


def enrich(event: Dict, event_time_ref: datetime) -> Optional[Dict]:
    """Enrich one raw event into the canonical dict.

    FIX-TIMEPROX (round du 04/08/2026, harmonisation Desk/Macro) : le champ
    de proximité temporelle est désormais nommé ``time_proximity`` et prend
    les valeurs ``IMMINENT / SOON / LATER / PAST`` -- strictement identiques
    au module Desk (mêmes seuils : ≤0h PAST, ≤6h IMMINENT, ≤48h SOON, au-delà
    LATER). Ces valeurs ne se confondent plus jamais avec ``impact`` (qui
    vaut toujours "high" dans ce flux déjà filtré).

    ``priority`` est conservé en DEPRECATED ALIAS de ``time_proximity`` pour
    ne pas casser un consommateur existant qui lirait encore cette clé --
    mais il ne contient plus "CRITICAL"/"HIGH"/"MEDIUM" : uniquement les
    valeurs harmonisées ci-dessus. TODO-REMOVE-PRIORITY-ALIAS : supprimer ce
    doublon une fois le report generator (patch MACRO-A3, devenu obsolète
    avec ce fix) et tout autre consommateur migrés vers ``time_proximity``.
    """
    try:
        t = datetime.fromisoformat(event.get("date", "").replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=pytz.UTC)

        # MACRO-A2 FIX : strftime formate les composantes locales, il ne convertit pas.
        # Projection explicite en UTC AVANT tout formatage pour garantir l'exactitude
        # de l'affichage (ex: 04:45 ET devient bien 08:45 UTC et non 04:45 UTC).
        t_utc = t.astimezone(pytz.UTC)

        h = (t - event_time_ref).total_seconds() / 3600
        ccy = event.get("country", "")

        # FIX-TIMEPROX : mêmes seuils qu'avant, nouveau nommage/vocabulaire
        # (aligné Desk), pour ne plus collisionner avec "impact".
        time_proximity = (
            "PAST" if h <= 0
            else "IMMINENT" if h <= 6
            else "SOON" if h <= 48
            else "LATER"
        )

        return {
            "currency": ccy,
            "event_name": event.get("title", "").strip(),
            "datetime_utc": t_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date_display": t_utc.strftime("%Y-%m-%d"),
            "time_display": t_utc.strftime("%H:%M UTC"),
            "day_of_week": t_utc.strftime("%A").upper(),
            "impact": (event.get("impact") or "High").lower(),
            "forecast": event.get("forecast", "") or "",
            "previous": event.get("previous", "") or "",
            "actual": event.get("actual", "") or "",
            "hours_until": round(h, 2),
            "hours_until_display": fmt_until(h),
            "is_upcoming": h > 0,
            "time_proximity": time_proximity,
            # DEPRECATED ALIAS -- voir TODO-REMOVE-PRIORITY-ALIAS ci-dessus.
            "priority": time_proximity,
            "session": get_session(t_utc),
            "pairs_affected": PAIRS_MAP.get(ccy, []),
        }
    except (ValueError, KeyError, AttributeError) as e:
        logger.warning("Skip event: %s", e)
        return None


# ---------------------------------------------------------------------------
# PATCH-CALGATE (round du 31/07/2026) : classification tier + fenêtres
# blackout avant/après. RÉPLIQUE EXACTE de ENGINE v10 (v10.py, SECTION 3 :
# _TIER_S / _TIER_A / _TIER_B / TIER_WINDOWS / classify_tier). C'est le Desk
# Engine qui fait autorité sur cette règle (c'est lui que le comité audite
# in fine) ; le module macro ne fait ici que la consommer à l'identique.
#
# Cause racine du bug corrigé : le module macro ne connaissait qu'un seuil
# unique "CRITICAL si h<=6h", appliqué seulement aux événements FUTURS. Un
# événement Tier S (ex: décision FOMC) qui vient de tomber restait donc
# invisible pour le gating macro dès qu'il passait en h<=0 ("PAST"), alors
# que le Desk Engine bloque la devise concernée jusqu'à 48h APRÈS l'annonce
# (TIER_WINDOWS[S] = (4.0, 48.0)). Résultat observé le 29-30/07/2026 :
# EUR/USD, GBP/USD, USD/CHF recommandés "CHERCHER LONG/SHORT" côté macro
# quelques heures après la FOMC, alors que le comité les bloquait déjà en
# CAL_BLACKOUT sur USD.
#
# IMPORTANT — dette technique assumée : ces constantes sont dupliquées
# (macro + Desk) faute d'un package partagé entre les deux applications.
# Toute modification de TIER_WINDOWS dans v10.py DOIT être répercutée ici à
# l'identique, sous peine de recréer exactement le bug qu'on corrige. Ce
# commentaire fait office de garde-fou en attendant l'extraction en module
# commun (ex: bluestar_shared.calendar_rules) -- extraction qui devrait
# aussi embarquer désormais `time_proximity`, `PAIRS_MAP`, `get_session` et
# `fmt_until`, redevenus strictement identiques entre Desk et Macro avec ce
# fix.
# ---------------------------------------------------------------------------
_TIER_S = ("non-farm", "nonfarm", "nfp", "fomc", "cpi", "cash rate",
           "bank rate", "rate statement", "interest rate", "monetary policy",
           "funds rate", "policy rate")
_TIER_A = ("gdp", "pmi", "adp", "pce", "employment change", "unemployment",
           "average hourly", "retail sales", "ppi")
_TIER_B = ("speaks", "speech", "press conference", "testifies", "testimony")

# (heures_avant, heures_après) -- identique à v10.py TIER_WINDOWS.
TIER_WINDOWS: Dict[str, tuple] = {
    "S": (4.0, 48.0),
    "A": (2.0, 24.0),
    "B": (1.0, 6.0),
}
DEFAULT_TIER_WINDOW = (2.0, 24.0)


def classify_tier(event_name: str) -> str:
    """Identique à v10.classify_tier -- même liste de mots-clés, même ordre
    de priorité (S avant A avant B), pour ne jamais diverger sur un événement
    ambigu (ex: un titre contenant à la fois 'GDP' et 'Press Conference')."""
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

    ``hours_until`` suit la même convention que ``enrich()`` : positif =
    événement futur, négatif = événement déjà passé (ex: -5.0 = tombé il y a
    5h). Retourne ``(bloqué: bool, tier: str)``.
    """
    tier = classify_tier(event_name)
    before, after = TIER_WINDOWS.get(tier, DEFAULT_TIER_WINDOW)
    return (-after <= hours_until <= before), tier


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
    ``events_engine`` (future + past within the residual-risk window) and
    ``summary_by_day`` -- the same contract the engine consumes.
    """
    now_utc = now_utc or datetime.now(pytz.UTC)
    if raw_data is None:
        raw_data = fetch_raw()

    # PATCH-FEEDHORIZON (round du 31/07/2026, audit F-15) : horizon réel
    # du flux (TOUS impacts confondus — on mesure la couverture du flux, pas
    # celle des seuls high-impact) publié en métadonnée + warning. Aucune
    # décision n'est modifiée : c'est de la visibilité, pas du gating.
    # Propre à ce module macro (n'existe pas côté Desk) -- conservé tel
    # quel, cf. docstring de tête pour la justification.
    feed_end: Optional[datetime] = None
    for _ev in raw_data:
        try:
            _t = datetime.fromisoformat(str(_ev.get("date", "")).replace("Z", "+00:00"))
            if _t.tzinfo is None:
                _t = _t.replace(tzinfo=pytz.UTC)
            if feed_end is None or _t > feed_end:
                feed_end = _t
        except (ValueError, AttributeError):
            continue
    feed_horizon_h = ((feed_end - now_utc).total_seconds() / 3600.0) if feed_end else None
    feed_truncated = feed_horizon_h is not None and feed_horizon_h < FF_WATCH_HORIZON_H
    if feed_truncated:
        logger.warning(
            "FF feed horizon %.1fh < %.0fh — fenêtre WATCH non vérifiable au-delà "
            "du flux hebdomadaire ; un silence calendaire n'est PAS une absence de risque",
            feed_horizon_h, FF_WATCH_HORIZON_H)

    # MACRO-A3 FIX (partie conservée) : .lower() rend le filtre robuste à un
    # changement de casse du flux Forex Factory (ex: "High" vs "high"). Ne
    # peut pas causer de régression car il élargit le périmètre de capture
    # au lieu de le rétrécir. -- La partie "relabeling MODÉRÉ -> ÉLEVÉ" de
    # MACRO-A3, elle, devient obsolète avec FIX-TIMEPROX ci-dessus : le
    # champ ne peut plus produire "MODÉRÉ" du tout, à supprimer côté report
    # generator (cf. TODO-REMOVE-PRIORITY-ALIAS).
    all_events = [
        e for ev in raw_data
        if (ev.get("impact") or "").strip().lower() == "high"
        for e in [enrich(ev, now_utc)] if e
    ]
    all_events.sort(key=lambda x: (not x["is_upcoming"], x["datetime_utc"]))

    daily: Dict[str, List[str]] = defaultdict(list)
    for ev in all_events:
        daily[ev["datetime_utc"][:10]].append(f"{ev['currency']}  {ev['event_name']}")
    summary_by_day = dict(sorted(daily.items()))

    events_engine = [
        e for e in all_events
        if e["is_upcoming"] or e["hours_until"] >= -RESIDUAL_RISK_WINDOW_H
    ]
    upcoming = [e for e in all_events if e["is_upcoming"]]

    # FIX-TIMEPROX : imminent_count devient le nom canonique (aligné Desk).
    # critical_count reste exposé en DEPRECATED ALIAS, même valeur -- pour
    # ne pas casser un consommateur qui lirait encore cette clé de metadata.
    # TODO-REMOVE-PRIORITY-ALIAS : supprimer critical_count à terme.
    imminent_count = sum(1 for e in all_events if e["time_proximity"] == "IMMINENT")

    return {
        "metadata": {
            "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "Forex Factory Official JSON",
            "timezone": "UTC",
            "total_high_impact": len(all_events),
            "upcoming_count": len(upcoming),
            "imminent_count": imminent_count,
            # DEPRECATED ALIAS -- voir TODO-REMOVE-PRIORITY-ALIAS.
            "critical_count": imminent_count,
            "engine_events_count": len(events_engine),
            "reachable": bool(raw_data),
            # PATCH-FEEDHORIZON (audit F-15) : horizon réel du flux hebdo.
            # Le Desk lit déjà "metadata" ; champs additifs, zéro régression
            # (v10.CalendarData a extra="ignore" et ses propres champs
            # feed_horizon_* alimentés par son patch §2.3-G).
            "feed_end_utc": feed_end.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if feed_end else None,
            "feed_horizon_h": round(feed_horizon_h, 1) if feed_horizon_h is not None else None,
            "feed_horizon_truncated": feed_truncated,
        },
        "events": upcoming,
        "events_engine": events_engine,
        "summary_by_day": summary_by_day,
    }
