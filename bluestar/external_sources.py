"""External data sources for the BLUESTAR engine — keyed / scraped feeds.

This module centralises every *external* source that requires either an API
key (FRED) or web scraping (CFTC, CME FedWatch, Atlanta Fed GDPNow). It is the
single upgrade path away from the [PROXY]/[N/A] degradation that the keyless
core falls back to.

Contract / design rules (must not be violated by callers):
  * Every public function is **best-effort**: on any failure (missing key,
    network error, parse error, unexpected schema) it returns ``None`` or an
    empty container — it NEVER raises. The caller degrades to [N/A].
  * No function ever *invents* a value. A missing observation ("." in FRED,
    an empty scrape) yields ``None``, never a placeholder number.
  * User overrides always take precedence *upstream* (in macro_engine /
    oanda_data). This module has no knowledge of overrides by design.
  * All diagnostics go through ``logging.warning`` — never ``print``.

Note on FRED series IDs: the original spec referenced Quandl-style codes
(``ECB/ECB``, ``BOJ/BOJ``, ``BOE/BOE``) which do not exist on FRED and would
return ``None`` forever. We substitute the correct FRED series IDs (documented
in ``_CB_RATE_SERIES``) so the feature actually works. ``FEDFUNDS`` is kept
as specified.

Changelog — institutional audit patch (2026-07-11):
  C1  fetch_pc_ratio: vix_value optional param; _vix_pc_composite() added.
  C2  _cboe_parse: signal computed on MA window, not raw daily latest.
  C3  _CBOE_THRESHOLDS: equity thresholds recalibrated on empirical percentiles.
  C4  _CBOE_SEVERITY: severity map added; _pc_composite() preserves severity floor.
  M1  _pc_composite: 6-quadrant matrix (2 missing regimes added).
  M2  _cboe_parse: observation_date captured; fetch_pc_ratio: stale flag added.
  M3  _cboe_parse: ma_incomplete flag + ma_Nd_obs observation count added.
  m1  _cboe_signal: guard for unknown ratio_type → returns "N/A".
  m2  _cboe_parse: data_start is None vs == 0 produce distinct error messages.

Changelog — réseau vérifié empiriquement (2026-07-17, deux environnements
indépendants : crawler le 16/07 ~02:01 UTC + sandbox BLUESTAR le 17/07) :
  N1  §5 CBOE : les URLs .../datahouse/equitypc.csv & indexpc.csv sont MORTES
      (301/404 — infra retirée lors de la refonte du site CBOE, PAS un blocage
      WAF comme le supposait l'ancien commentaire). Elles coûtaient 1 requête
      HTTP inutile par jambe à chaque run. Supprimées de la chaîne active ;
      _cboe_parse conservé pour un éventuel miroir CSV futur (voir §5).
  N2  §5 CBOE : preuve que CBOE ne distribue plus les P/C ratios en public —
      cdn.cboe.com sert _VIX.json (1 153 407 octets) et _SKEW.json normalement
      depuis la même IP, mais _PCALL/_EQUITYPC/_TOTALPC/_INDEXPC/_VIXPC/PCALL
      .json renvoient tous 403 AccessDenied (objet absent). Donnée déplacée
      derrière DataShop / All Access API (payant). Jambe index : aucune source
      keyless fiable — dégradation vers None assumée (mode single-leg P1).
  N3  §5 CBOE : ^PCALL confirmé symbole RÉEL et vivant, MAIS Yahoo
      rate-limite (HTTP 429) les IPs de crawler indépendamment du ticker —
      fallback yfinance conservé en best-effort, étiqueté honnêtement.
  N4  §2 FedWatch : la probabilité rendue pouvait rester figée ~1 semaine sans
      indice de fraîcheur (anomalie audit A1 du 16/07). La clé additive
      "as_of" est désormais extraite du payload BCM quand elle y est présente
      (best-effort, schéma-agnostique — jamais inventée) pour que l'aval
      affiche la date de prélèvement.
"""
from __future__ import annotations

import csv
import concurrent.futures
import datetime
import io
import logging
import os
import re
import zipfile
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Optional Streamlit secrets access (mirrors oanda_data.py degradation pattern).
try:
    import streamlit as st  # type: ignore
    _ST_OK = True
except Exception:  # pragma: no cover
    _ST_OK = False

# Optional BeautifulSoup (used for CFTC index + GDPNow scraping).
try:
    from bs4 import BeautifulSoup  # type: ignore
    _BS_OK = True
except Exception:  # pragma: no cover
    _BS_OK = False
    logger.warning("beautifulsoup4 unavailable — CFTC/GDPNow scraping disabled")

# ---------------------------------------------------------------------------
# HTTP configuration
# ---------------------------------------------------------------------------
_TIMEOUT = 12          # seconds — within the 10–15 s spec band
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BLUESTAR/8.1; +macro-briefing) "
        "Python-requests"
    ),
    "Accept": "text/html,application/json,text/csv,*/*",
}


def _get(url: str, extra_headers: dict | None = None,
         **kwargs) -> Optional[requests.Response]:
    """Single GET with unified timeout / headers / error handling.

    ``extra_headers`` overrides / extends ``_HEADERS`` for caller-specific
    needs (e.g. CBOE bot-bypass) without touching the module-level default.
    Returns the Response on HTTP 200, else ``None`` (logged). Never raises.
    """
    headers = {**_HEADERS, **(extra_headers or {})}
    try:
        r = requests.get(url, headers=headers, timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except requests.RequestException as exc:
        logger.warning("HTTP GET failed for %s: %s", url, exc)
        return None


# ===========================================================================
# 1. FRED API
# ===========================================================================
_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ---------------------------------------------------------------------------
# Audit A1 FIX : Séries vérifiées live 2026-07-12.
# - FEDFUNDS : mensuel, remplacé par DFEDTARU (quotidien, borne haute).
# - IRSTCB01JPM156N : coquille 'B', discontinuée. Remplacé par IRSTCI01JPM156N.
# - BOERUKM : gelée depuis jan. 2017. AUCUNE série BoE fiable sur FRED.
#   -> BoE retirée du mapping FRED. Voir plus bas (section 1bis) pour la
#      source BoE dédiée (API officielle Bank of England, hors FRED) ajoutée
#      le 15/07/2026 — enrichissement pur, ne touche à rien de ce qui suit.
# ---------------------------------------------------------------------------
_CB_RATE_SERIES: dict[str, str] = {
    "FED": "DFEDTARU",
    "BCE": "ECBDFR",
    "BoJ": "IRSTCI01JPM156N",
}

# Plausibilité : bornes larges pour détecter les sentinelles FRED (-999, .)
# et les valeurs aberrantes. Un taux hors bornes est écarté.
_CB_RATE_BOUNDS: dict[str, tuple[float, float]] = {
    "FED": (-0.5, 15.0),
    "BCE": (-0.5, 15.0),
    "BoJ": (-1.0, 10.0),
    "BoE": (-0.5, 15.0),
}

# Staleness max : au-delà, la série est considérée potentiellement gelée.
_CB_MAX_STALENESS_DAYS: int = 70

# BoE-specific staleness bound (audit-enrichment 15/07/2026): the FRED
# series above are DAILY (a value repeats every day even when the rate
# itself is unchanged), so a fresh observation *date* within 70 days is a
# reliable freshness signal even for a rate that hasn't moved. The BoE
# IADB Bank Rate series is event-based: a new row only appears the day the
# MPC actually changes the rate, so the latest observation can legitimately
# be many months old while still being the current valid rate (MPC meets
# ~8x/year and often holds). Reusing the 70-day FRED bound here would
# wrongly discard a perfectly valid, unchanged BoE rate as "stale". A much
# more generous window is used instead — long enough to tolerate a normal
# holding pattern, still short enough to catch a genuinely dead/discontinued
# feed.
_BOE_MAX_STALENESS_DAYS: int = 400

_SOFR_SERIES = "SOFR"
_EFFR_SERIES = "EFFR"


def _fred_api_key() -> Optional[str]:
    if _ST_OK:
        try:
            key = st.secrets.get("FRED_API_KEY") or st.secrets.get("fred_api_key")
            if key:
                return str(key)
        except Exception as exc:  # pragma: no cover
            logger.warning("Streamlit FRED key access failed: %s", exc)
    env = os.environ.get("FRED_API_KEY") or os.environ.get("fred_api_key")
    return str(env) if env else None


def _fred_series(series_id: str) -> Optional[float]:
    api_key = _fred_api_key()
    if not api_key:
        return None
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    r = _get(_FRED_BASE, params=params)
    if r is None:
        return None
    try:
        obs = r.json().get("observations", [])
        if not obs:
            return None
        raw = obs[0].get("value", ".")
        if raw in (".", "", None):
            return None
        return float(raw)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("FRED parse error for %s: %s", series_id, exc)
        return None


def _fred_series_dated(series_id: str) -> Optional[tuple[float, str]]:
    """Comme _fred_series mais renvoie (valeur, date_obs ISO) ou None."""
    api_key = _fred_api_key()
    if not api_key:
        return None
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    r = _get(_FRED_BASE, params=params)
    if r is None:
        return None
    try:
        obs = r.json().get("observations", [])
        if not obs:
            return None
        raw = obs[0].get("value", ".")
        if raw in (".", "", None):
            return None
        return float(raw), obs[0].get("date", "")
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("FRED dated parse error for %s: %s", series_id, exc)
        return None


# ===========================================================================
# 1bis. Bank of England — API officielle (hors FRED)
#
# AUDIT-ENRICHMENT (15/07/2026): FRED n'a aucune série BoE Bank Rate fiable
# (BOERUKM gelée depuis 2017, voir commentaire ci-dessus). Cette section
# interroge directement la base IADB de la Bank of England (endpoint public,
# sans clé) — même contrat "never raise, log and return None" que le reste
# du module. Purement additif : ne touche ni _fred_series, ni
# _fred_series_dated, ni aucune des séries FED/BCE/BoJ existantes.
# ===========================================================================
_BOE_IADB_URL = "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
_BOE_BANK_RATE_CODE = "IUDBEDR"  # Bank Rate officielle (code série IADB)

# Formats de date observés dans les exports IADB (varie selon les endpoints
# BoE) — essayés dans l'ordre jusqu'à ce qu'un marche.
_BOE_DATE_FORMATS = ("%d %b %Y", "%d/%m/%Y", "%d-%b-%y", "%Y-%m-%d")


def _boe_parse_date(raw: str) -> Optional[datetime.date]:
    raw = raw.strip()
    for fmt in _BOE_DATE_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _boe_bank_rate() -> Optional[tuple[float, str]]:
    """Fetch the latest BoE Bank Rate observation from the IADB CSV export.

    Returns ``(value, date_iso)`` or ``None`` on any failure — never raises,
    matching every other fetcher in this module. NOTE: this endpoint is
    outside the sandbox's allowed network domains at the time this was
    written, so this function could not be exercised against a live
    response here; the CSV parsing below is best-effort against the
    documented IADB export format (data rows after a short header/meta
    block) and should be verified against a real response in your
    environment before being relied on.
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=800)  # generous: MPC holds often
    params = {
        "csv.x": "yes",
        "Datefrom": start.strftime("%d/%b/%Y"),
        "Dateto": end.strftime("%d/%b/%Y"),
        "SeriesCodes": _BOE_BANK_RATE_CODE,
        "CSVF": "TN",
        "UsingCodes": "Y",
        "VPD": "Y",
        "ns": "1",
    }
    try:
        r = requests.get(_BOE_IADB_URL, params=params, timeout=15)
        r.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("BoE IADB fetch failed: %s", exc)
        return None

    try:
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        # IADB exports carry a short header/meta block before the data
        # table; scan for the first row that looks like "<date>,<number>"
        # rather than assuming a fixed skip count, since the exact preamble
        # length has varied across BoE endpoint versions.
        data_rows = []
        for row in csv.reader(lines):
            if len(row) < 2 or not row[0].strip():
                continue
            d = _boe_parse_date(row[0])
            if d is None:
                continue
            try:
                v = float(row[1].strip())
            except ValueError:
                continue
            data_rows.append((d, v))
        if not data_rows:
            logger.warning("BoE IADB: aucune ligne de données exploitable dans la réponse")
            return None
        data_rows.sort(key=lambda t: t[0])
        last_date, last_val = data_rows[-1]
        return last_val, last_date.isoformat()
    except (csv.Error, IndexError) as exc:
        logger.warning("BoE IADB parse error: %s", exc)
        return None


def central_bank_rate_source(name: str) -> str:
    """Human-readable source label for a rate resolved by
    ``fetch_central_bank_rates()`` — lets callers (macro_engine.py) render
    an accurate stamp instead of hardcoding "FRED" for every entry, which
    would now be wrong for "BoE" specifically (audit-enrichment 15/07/2026).
    """
    return "Bank of England · IADB" if name == "BoE" else "FRED"


def fetch_central_bank_rates() -> dict[str, float]:
    """Return ``{cb_name: rate_pct}`` for every rate a live source can serve.

    Audit A1 FIX: chaque série FRED résolue est contrôlée en fraîcheur (date
    d'observation) et en plausibilité (bornes _CB_RATE_BOUNDS). Une série qui
    échoue est écartée avec un WARNING, dégradant proprement vers [N/A] en aval.

    Audit-ENRICHMENT (15/07/2026): "BoE" est désormais résolu ici aussi, via
    la Bank of England elle-même plutôt que FRED (voir section 1bis) — le
    contrat public de cette fonction ({name: float}) est inchangé, BoE
    apparaît simplement comme une clé de plus quand la source répond.
    Utiliser ``central_bank_rate_source(name)`` en aval pour savoir quelle
    source a réellement servi une entrée donnée (au lieu de supposer "FRED").
    """
    out: dict[str, float] = {}

    def _resolve_fred(name: str, series_id: str) -> Optional[float]:
        res = _fred_series_dated(series_id)
        if res is None:
            logger.warning("CB rate: %s (%s) — aucune observation FRED", name, series_id)
            return None

        val, dt_iso = res

        # --- Contrôle 1 : fraîcheur ---
        try:
            obs_date = datetime.date.fromisoformat(dt_iso)
            age_days = (datetime.date.today() - obs_date).days
            if age_days > _CB_MAX_STALENESS_DAYS:
                logger.warning(
                    "CB rate: %s (%s) — observation datée du %s (%d j) "
                    "→ série potentiellement gelée, valeur écartée",
                    name, series_id, dt_iso, age_days,
                )
                return None
        except (ValueError, TypeError):
            # Date illisible → on NE GARDE PAS la valeur (élimine le risque de sentinelle)
            logger.warning(
                "CB rate: %s (%s) — date illisible '%s', valeur écartée",
                name, series_id, dt_iso,
            )
            return None

        # --- Contrôle 2 : plausibilité ---
        bounds = _CB_RATE_BOUNDS.get(name)
        if bounds is not None:
            lo, hi = bounds
            if not (lo <= val <= hi):
                logger.warning(
                    "CB rate: %s (%s) — valeur %.4f hors bornes [%.1f, %.1f], écartée",
                    name, series_id, val, lo, hi,
                )
                return None

        return val

    def _resolve_boe() -> Optional[float]:
        res = _boe_bank_rate()
        if res is None:
            logger.warning("CB rate: BoE (IADB %s) — aucune observation", _BOE_BANK_RATE_CODE)
            return None

        val, dt_iso = res

        # --- Contrôle 1 : fraîcheur (fenêtre BoE dédiée, cf. _BOE_MAX_STALENESS_DAYS) ---
        try:
            obs_date = datetime.date.fromisoformat(dt_iso)
            age_days = (datetime.date.today() - obs_date).days
            if age_days > _BOE_MAX_STALENESS_DAYS:
                logger.warning(
                    "CB rate: BoE (IADB) — observation datée du %s (%d j) "
                    "→ série potentiellement gelée, valeur écartée",
                    dt_iso, age_days,
                )
                return None
        except (ValueError, TypeError):
            logger.warning("CB rate: BoE (IADB) — date illisible '%s', valeur écartée", dt_iso)
            return None

        # --- Contrôle 2 : plausibilité ---
        bounds = _CB_RATE_BOUNDS.get("BoE")
        if bounds is not None:
            lo, hi = bounds
            if not (lo <= val <= hi):
                logger.warning(
                    "CB rate: BoE (IADB) — valeur %.4f hors bornes [%.1f, %.1f], écartée",
                    val, lo, hi,
                )
                return None

        return val

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_CB_RATE_SERIES) + 1) as ex:
        future_to_name = {ex.submit(_resolve_fred, name, series_id): name
                          for name, series_id in _CB_RATE_SERIES.items()}
        future_to_name[ex.submit(_resolve_boe)] = "BoE"
        for future in concurrent.futures.as_completed(future_to_name):
            name = future_to_name[future]
            try:
                val = future.result()
            except Exception:
                logger.exception("CB rate: exception imprévue pour %s", name)
                val = None
            if val is not None:
                out[name] = val
                
    _all_names = set(_CB_RATE_SERIES) | {"BoE"}
    if not out:
        logger.error("CB rate: AUCUN taux résolu — différentiels indisponibles")
    else:
        missing = _all_names - set(out)
        if missing:
            logger.warning("CB rate: taux manquants pour %s — dégradation [N/A] attendue", ", ".join(sorted(missing)))

    return out


def fetch_liquidity_stress() -> Optional[float]:
    """Return the latest SOFR − EFFR spread in basis points, or ``None``."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_sofr = ex.submit(_fred_series, _SOFR_SERIES)
        fut_effr = ex.submit(_fred_series, _EFFR_SERIES)
        sofr = fut_sofr.result()
        effr = fut_effr.result()
    if sofr is None or effr is None:
        return None
    return (sofr - effr) * 100.0


# ===========================================================================
# 2. CME FedWatch probabilities
# ===========================================================================
_FEDWATCH_URL = "https://www.cmegroup.com/CmeWS/md/BCM/BCM.json"


# Clés de fraîcheur candidates dans les payloads JSON de marché (schéma BCM
# non documenté publiquement — liste normalisée, comparée sans '_' ni espaces,
# en minuscules). La clé "date" seule est volontairement EXCLUE : dans ce
# payload elle porte la date de RÉUNION FOMC (cible), pas l'horodatage de
# prélèvement — l'accepter afficherait une fausse fraîcheur.
_FEDWATCH_TS_KEYS = frozenset({
    "lastupdated", "lastupdate", "updated", "updatetime", "updatetimeutc",
    "timestamp", "asof", "asofdate", "quotedate", "quotetime",
    "tradedate", "lasttradedate", "pricedate", "lastpriceupdate",
})


def _fedwatch_extract_timestamp(data) -> Optional[str]:
    """Best-effort : extrait l'horodatage de fraîcheur du payload BCM.

    Schéma-agnostique (le parser de probabilités ci-dessous l'est déjà) :
    retourne la première valeur textuelle non vide trouvée sous une clé de
    ``_FEDWATCH_TS_KEYS``, tronquée à 40 caractères — ou ``None`` si le
    payload n'en porte aucune (cas parfaitement admis : l'aval masque alors
    simplement la mention). N'invente jamais de valeur, ne lève jamais.
    """
    try:
        found: list[str] = []

        def _walk(node) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    norm = str(k).lower().replace("_", "").replace(" ", "")
                    if norm in _FEDWATCH_TS_KEYS and isinstance(v, str) and v.strip():
                        found.append(v.strip())
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(data)
        if not found:
            return None
        # Plusieurs candidats possibles (payload multi-nœuds) : le max
        # lexicographique = le plus récent pour les formats ISO-like ; pour
        # les formats non triables l'ordre est arbitraire mais reste réel.
        return max(found)[:40]
    except Exception:
        return None


def fetch_fedwatch_probabilities() -> Optional[dict]:
    """Return ``{'pause_pct', 'cut_pct', 'hike_pct'}`` for the next FOMC, or None.

    N4 (2026-07-17) : clé additive ``'as_of'`` (str) présente uniquement quand
    le payload BCM porte lui-même un horodatage exploitable — date de
    prélèvement affichée en aval (audit A1 : probabilités figées ~1 semaine
    sans mention de fraîcheur). Les trois clés historiques sont inchangées.
    """
    r = _get(_FEDWATCH_URL)
    if r is None:
        return None
    try:
        data = r.json()
    except ValueError as exc:
        logger.warning("FedWatch JSON decode failed: %s", exc)
        return None
    try:
        meetings = _fedwatch_locate_meetings(data)
        if not meetings:
            logger.warning("FedWatch: no meeting probabilities located in payload")
            return None
        nearest = meetings[0]
        buckets = _fedwatch_extract_buckets(nearest)
        if not buckets:
            return None
        cut = pause = hike = 0.0
        for delta_bp, prob in buckets:
            if delta_bp < 0:
                cut += prob
            elif delta_bp > 0:
                hike += prob
            else:
                pause += prob
        total = cut + pause + hike
        if total <= 0:
            return None
        scale = 100.0 / total
        result = {
            "cut_pct": int(round(cut * scale)),
            "pause_pct": int(round(pause * scale)),
            "hike_pct": int(round(hike * scale)),
        }
        drift = 100 - sum(result.values())
        if drift:
            biggest = max(result, key=result.get)
            result[biggest] += drift
        # N4 : horodatage de prélèvement quand le payload le fournit (additif).
        ts = _fedwatch_extract_timestamp(data)
        if ts:
            result["as_of"] = ts
        return result
    except Exception as exc:
        logger.warning("FedWatch parse failed (schema drift?): %s", exc)
        return None


def _fedwatch_locate_meetings(data) -> list:
    candidates: list[dict] = []

    def _looks_like_meeting(d: dict) -> bool:
        keys = {k.lower() for k in d.keys()}
        has_date = any("date" in k or "meeting" in k for k in keys)
        has_prob = any("prob" in k or "value" in k or "bp" in k for k in keys)
        return has_date and has_prob

    def _walk(node):
        if isinstance(node, dict):
            if _looks_like_meeting(node):
                candidates.append(node)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)

    def _mdate(d: dict) -> str:
        for k, v in d.items():
            if "date" in k.lower() and isinstance(v, str):
                return v
        return "9999-99-99"

    candidates.sort(key=_mdate)
    return candidates


def _fedwatch_extract_buckets(meeting: dict) -> list[tuple[int, float]]:
    buckets: list[tuple[int, float]] = []
    prob_list = None
    for k, v in meeting.items():
        if "prob" in k.lower() and isinstance(v, list):
            prob_list = v
            break
    rows = prob_list if prob_list is not None else [meeting]
    for row in rows:
        if not isinstance(row, dict):
            continue
        delta_bp = None
        prob = None
        for k, v in row.items():
            kl = k.lower()
            if delta_bp is None and ("bp" in kl or "change" in kl or "delta" in kl):
                try:
                    delta_bp = int(round(float(v)))
                except (TypeError, ValueError):
                    pass
            if prob is None and ("prob" in kl or kl in ("value", "pct")):
                try:
                    prob = float(v)
                except (TypeError, ValueError):
                    pass
        if delta_bp is not None and prob is not None:
            frac = prob / 100.0 if prob > 1.0 else prob
            buckets.append((delta_bp, frac))
    return buckets


# ===========================================================================
# 3. CFTC Commitments of Traders (Non-Commercials, legacy financial futures)
# ===========================================================================
_CFTC_INDEX = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"

_CFTC_CONTRACTS: dict[str, str] = {
    "EUR": "EURO FX",
    "JPY": "JAPANESE YEN",
    "GBP": "BRITISH POUND",
    "CHF": "SWISS FRANC",
    "AUD": "AUSTRALIAN DOLLAR",
    "CAD": "CANADIAN DOLLAR",
    "NZD": "NEW ZEALAND DOLLAR",
}

_COL_MARKET   = "Market and Exchange Names"
_COL_NC_LONG  = "Noncommercial Positions-Long (All)"
_COL_NC_SHORT = "Noncommercial Positions-Short (All)"
_COL_DATE     = "As of Date in Form YYYY-MM-DD"
_CME_TOKEN    = "CHICAGO MERCANTILE EXCHANGE"


def _cftc_find_report_url() -> Optional[str]:
    if not _BS_OK:
        return None
    r = _get(_CFTC_INDEX)
    if r is None:
        return None
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        logger.warning("CFTC index parse failed: %s", exc)
        return None

    def _abs(href: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return "https://www.cftc.gov" + href
        return "https://www.cftc.gov/" + href

    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    for href in hrefs:
        low = href.lower()
        if "other_disclaim" in low and low.endswith((".csv", ".zip")):
            return _abs(href)
    for href in hrefs:
        low = href.lower()
        if (("fin" in low or "fut" in low or "deacot" in low)
                and low.endswith((".csv", ".zip"))):
            return _abs(href)
    logger.warning("CFTC: no report CSV/zip link found on index page")
    return None


def _cftc_load_rows(url: str) -> Optional[list[dict]]:
    r = _get(url)
    if r is None:
        return None
    content = r.content
    text: Optional[str] = None
    try:
        if url.lower().endswith(".zip") or content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    logger.warning("CFTC zip has no CSV: %s", url)
                    return None
                text = zf.read(csv_names[0]).decode("utf-8", errors="replace")
        else:
            text = content.decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, UnicodeError) as exc:
        logger.warning("CFTC report decode failed for %s: %s", url, exc)
        return None
    try:
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)
    except csv.Error as exc:
        logger.warning("CFTC CSV parse failed: %s", exc)
        return None


def _cftc_col(row: dict, wanted: str) -> Optional[str]:
    if wanted in row:
        return row[wanted]
    want_norm = re.sub(r"\s+", " ", wanted).strip().lower()
    for k, v in row.items():
        if k and re.sub(r"\s+", " ", k).strip().lower() == want_norm:
            return v
    return None


def fetch_cot_data() -> tuple[dict[str, int], Optional[str]]:
    """Return ``({ccy: net_noncommercial}, as_of_date_str)`` from the CFTC."""
    url = _cftc_find_report_url()
    if not url:
        return {}, None
    rows = _cftc_load_rows(url)
    if not rows:
        return {}, None
    net_by_ccy: dict[str, int] = {}
    as_of: Optional[str] = None
    for row in rows:
        market = (_cftc_col(row, _COL_MARKET) or "").upper()
        if _CME_TOKEN not in market:
            continue
        rtype = _cftc_col(row, "Report Type") or _cftc_col(row, "FutOnly_or_Combined")
        if rtype and "FUT" not in rtype.upper() and "ONLY" not in rtype.upper():
            continue
        for ccy, token in _CFTC_CONTRACTS.items():
            if ccy in net_by_ccy:
                continue
            if token in market:
                long_s  = _cftc_col(row, _COL_NC_LONG)
                short_s = _cftc_col(row, _COL_NC_SHORT)
                if long_s is None or short_s is None:
                    continue
                try:
                    net = (int(float(long_s.replace(",", "")))
                           - int(float(short_s.replace(",", ""))))
                except (TypeError, ValueError):
                    continue
                net_by_ccy[ccy] = net
                if as_of is None:
                    d = _cftc_col(row, _COL_DATE)
                    if d:
                        as_of = d.strip()
                break
    if not net_by_ccy:
        logger.warning("CFTC: report loaded but no target contracts matched")
        return {}, None
    return net_by_ccy, as_of


# ===========================================================================
# 4. Atlanta Fed GDPNow
# ===========================================================================
_GDPNOW_URL = "https://www.atlantafed.org/cqer/research/gdpnow"

_GDPNOW_RE = re.compile(
    r"GDPNow (?:model )?estimate for (?:real GDP growth[^)]*\)[^0-9]*)?"
    r"Q?[1-4]?\s*\d{4}\s*is\s*(-?[\d.]+)\s*(?:percent|%)",
    re.IGNORECASE,
)
_GDPNOW_LATEST_RE = re.compile(
    r"[Ll]atest estimate:\s*(-?[\d.]+)\s*(?:percent|%)"
)


def fetch_gdp_nowcast() -> Optional[float]:
    """Return the current Atlanta Fed GDPNow estimate (%), or ``None``."""
    r = _get(_GDPNOW_URL)
    if r is None:
        return None
    html = r.text
    text = html
    if _BS_OK:
        try:
            text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        except Exception:
            text = html
    for rx in (_GDPNOW_LATEST_RE, _GDPNOW_RE):
        m = rx.search(text)
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                continue
    logger.warning("GDPNow: estimate not found in page text")
    return None


# ===========================================================================
# 5. CBOE Put/Call Ratios (Equity · Index)
#
# Audit patch 2026-07-11 — corrections C1/C2/C3/C4/M1/M2/M3/m1/m2 applied.
# Réseau vérifié 2026-07-16/17 (deux environnements indépendants) — N1/N2/N3 :
# les endpoints CSV publics sont retirés de la distribution CBOE ; la chaîne
# active est désormais yfinance best-effort → dégradation honnête vers None.
# ===========================================================================

# Headers navigateur conservés pour la voie de réactivation documentée dans
# _cboe_fetch_one (miroir CSV futur / accès DataShop authentifié).
_CBOE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json,text/csv,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.cboe.com/us/indices/dashboard/PCALL/",
    "Origin":          "https://www.cboe.com",
    "Connection":      "keep-alive",
}

# N1 — ANCIENS ENDPOINTS MORTS, NE PAS RÉINTRODUIRE TELS QUELS (vérifié
# 2026-07-16 crawler + 2026-07-17 sandbox, deux IPs indépendantes) :
#   https://www.cboe.com/publish/scheduledtask/mktdata/datahouse/equitypc.csv
#   https://www.cboe.com/publish/scheduledtask/mktdata/datahouse/indexpc.csv
# → 301/404 (page HTML « 404 Page Not Found | Cboe ») : infra RETIRÉE par CBOE
# lors de la refonte du site (SPA Next.js + CDN S3). Ce n'était PAS un blocage
# Cloudflare/WAF comme le supposait l'ancien commentaire : le fetch « échouait
# silencieusement » parce qu'il recevait du HTML 404 (statut 200) et que
# _cboe_parse renvoyait None sans bruit.
#
# N2 — Le CDN moderne ne publie PAS les P/C ratios (preuve par témoin) :
#   cdn.cboe.com/.../historical/_VIX.json  → 200, 1 153 407 octets (OK)
#   cdn.cboe.com/.../historical/_SKEW.json → 200, 1 184 801 octets (OK)
#   cdn.cboe.com/.../historical/_PCALL.json    → 403 AccessDenied (S3)
#   .../_EQUITYPC/_TOTALPC/_INDEXPC/_VIXPC/PCALL.json → tous 403
# _VIX et _SKEW (indices value-only, comme un P/C) passent depuis la même IP :
# les 403 ne sont donc PAS un blocage mais des objets absents. CBOE a déplacé
# la donnée derrière DataShop / Cboe All Access API (payant). Autres voies
# keyless testées et mortes : Nasdaq Data Link (403 Incapsula + clé requise),
# Stooq ^pcall (challenge JS proof-of-work inexploitable en requests).

# C3 FIX — Equity thresholds recalibrated on post-2015 empirical distribution.
# Zero-commission structural shift (2019-2020) lowered the mean permanently.
# Reference percentiles (CBOE Equity P/C MA5j, 2015–2024 approx):
#   P10 ≈ 0.52 | P25 ≈ 0.60 | P50 ≈ 0.66 | P75 ≈ 0.90 | P90 ≈ 1.10
# Previous thresholds (0.60/0.70/1.00) placed the median in COMPLACENCE,
# causing ~55 % of normal sessions to trigger the alert — no discriminant value.
# Index P/C: mean ~0.95–1.05; calm <0.80; stress >1.50. Seuils conservés.
_CBOE_THRESHOLDS: dict[str, dict[str, float]] = {
    "equity": {
        "extreme_greed": 0.52,   # < P10  → DANGER ZONE       (pré-crack historique)
        "complacency":   0.60,   # < P25  → COMPLACENCE        (euphorie structurelle)
        "fear":          0.90,   # > P75  → COUVERTURE         (protection active)
        "extreme_fear":  1.10,   # > P90  → PEUR EXTREME       (contrarian signal)
    },
    "index": {
        "complacency":   0.80,   # < ~P30 → COMPLACENCE INSTITUTIONNELLE
        "fear":          1.20,   # > ~P70 → COUVERTURE ELEVEE  (~P70, doc. empirique)
    },
}

# C4 FIX — Severity map prevents composite from producing a label softer than
# the most extreme individual signal (audit finding: DANGER ZONE → COMPLACENCE).
_CBOE_SEVERITY: dict[str, int] = {
    "DANGER ZONE":                   5,
    "PEUR EXTREME":                  5,
    "COMPLACENCE":                   4,
    "COMPLACENCE INSTITUTIONNELLE":  4,
    "COUVERTURE ELEVEE":             3,
    "COUVERTURE":                    2,
    "NEUTRE":                        1,
    "N/A":                           0,
}

# Staleness threshold: P/C is published post-close J-1; gap > 3 calendar days
# (covers weekends + public holiday) is considered stale.
_CBOE_STALE_DAYS = 3


def _cboe_signal(ratio_type: str, value: float) -> str:
    """Map a numeric P/C value to a BLUESTAR signal label.

    C2 contract: MUST be called with the MA value, never the raw daily.
    m1 FIX: unknown ratio_type returns "N/A" instead of silently falling
    through to index thresholds via the bare ``else`` branch.
    """
    t = _CBOE_THRESHOLDS.get(ratio_type)
    if t is None:
        # m1 FIX — guard against future ratio_type additions or typos.
        logger.warning(
            "CBOE _cboe_signal: unknown ratio_type '%s' — returning N/A",
            ratio_type,
        )
        return "N/A"

    if ratio_type == "equity":
        if value < t["extreme_greed"]:
            return "DANGER ZONE"
        if value < t["complacency"]:
            return "COMPLACENCE"
        if value > t["extreme_fear"]:
            return "PEUR EXTREME"       # C3: new level (> P90), contrarian signal
        if value > t["fear"]:
            return "COUVERTURE"
        return "NEUTRE"

    else:  # "index" — explicit branch; unknown type already handled above
        if value < t["complacency"]:
            return "COMPLACENCE INSTITUTIONNELLE"
        if value > t["fear"]:
            return "COUVERTURE ELEVEE"
        return "NEUTRE"


def _vix_pc_composite(vix: float, eq_pc_ma: float, idx_pc_ma: float) -> str:
    """True VIX × P/C cross-signal for macro regime classification.

    C1 implementation. Called by fetch_pc_ratio() when vix_value is provided,
    or directly by macro_engine.py for inline regime scoring.

    Operates exclusively on MA values (C2 compliant — never on raw daily).
    C4 invariant: DANGER ZONE and PEUR EXTREME are never absorbed by a softer
    composite label, regardless of VIX level.

    VIX regime thresholds (institutional standard):
        < 15  : vol comprimée
        15-22 : neutre
        > 22  : vol élevée
        > 30  : stress systémique

    P/C thresholds: see _CBOE_THRESHOLDS (C3 recalibration).
    """
    vix_compressed = vix < 15.0
    vix_elevated   = vix > 22.0
    vix_stress     = vix > 30.0

    # Equity P/C boolean levels (on MA — C2)
    eq_extreme_greed = eq_pc_ma < 0.52   # DANGER ZONE
    eq_greed         = eq_pc_ma < 0.60   # COMPLACENCE
    eq_fear          = eq_pc_ma > 0.90   # COUVERTURE
    eq_extreme_fear  = eq_pc_ma > 1.10   # PEUR EXTREME

    # Index P/C boolean levels (institutional flow)
    idx_fear  = idx_pc_ma > 1.20
    idx_greed = idx_pc_ma < 0.80

    # ── C4: severity floor — DANGER ZONE / PEUR EXTREME cannot be downgraded ──
    if eq_extreme_greed and vix_compressed:
        return "DANGER ZONE — COMPLACENCE EXTRÊME SOUS VOL BASSE"
    if eq_extreme_greed:
        return "DANGER ZONE — PROTECTION RETAIL ABSENTE"
    if eq_extreme_fear and vix_stress:
        return "CAPITULATION — SIGNAL CONTRARIAN HAUSSIER FORT"
    if eq_extreme_fear:
        return "PEUR EXTREME — SIGNAL CONTRARIAN"

    # ── Main VIX × P/C regimes ────────────────────────────────────────────────
    if vix_compressed and eq_greed and not idx_fear:
        return "COMPLACENCE GENERALISEE — TAIL RISK ÉLEVÉ"
    if vix_compressed and eq_greed and idx_fear:
        return "COMPLACENCE RETAIL + HEDGE INSTITUTIONNEL"
    if vix_compressed and not eq_greed and not eq_fear:
        return "NEUTRE — VOL BASSE, POSITIONNEMENT ÉQUILIBRÉ"
    if vix_elevated and eq_fear and idx_fear:
        return "COUVERTURE GÉNÉRALISÉE — RISQUE SYSTÉMIQUE"
    if vix_elevated and not eq_fear and idx_greed:
        return "SQUEEZE POTENTIEL — CHOC SANS PROTECTION"
    if vix_elevated and eq_fear and idx_greed:
        return "DIVERGENCE — RETAIL FEARFUL / INSTIT COMPLACENT"   # M1 regime
    if not vix_compressed and not vix_elevated and eq_greed:
        return "COMPLACENCE PARTIELLE — SURVEILLER"

    return "NEUTRE"


def _pc_composite(eq_ma: float, idx_ma: float,
                  eq_sev: int, idx_sev: int) -> str:
    """P/C × P/C composite — backward-compat mode when VIX is unavailable.

    C4 fix: severity floor enforced — the composite cannot be softer than
    the most extreme individual signal.
    M1 fix: 6-quadrant matrix fills the two previously unclassified regimes:
      - Retail fearful + Institutions complacentes (contrarian bullish)
      - Prudence émergente retail + Index neutre
    """
    # C4: severity floor — highest individual signal sets composite minimum.
    max_sev = max(eq_sev, idx_sev)
    if max_sev >= 5:
        # DANGER ZONE or PEUR EXTREME present in at least one leg — cannot dilute.
        if eq_ma < 0.52:
            return "DANGER ZONE — PROTECTION RETAIL ABSENTE"
        return "PEUR EXTREME — SIGNAL CONTRARIAN"

    # 6-quadrant matrix — ordered from most to least extreme (M1).
    if eq_ma < 0.60 and idx_ma < 0.80:
        return "COMPLACENCE GENERALISEE"
    if eq_ma < 0.60 and idx_ma >= 0.80:
        return "COMPLACENCE RETAIL + HEDGE INSTITUTIONNEL"
    if eq_ma > 1.10 and idx_ma < 0.80:
        return "DIVERGENCE — RETAIL FEARFUL / INSTIT COMPLACENT"   # M1: was NEUTRE
    if eq_ma > 0.90 and idx_ma > 1.20:
        return "COUVERTURE GENERALISEE"
    if 0.60 <= eq_ma < 0.70 and 0.80 <= idx_ma < 1.00:
        return "PRUDENCE ÉMERGENTE"                                  # M1: was NEUTRE
    return "NEUTRE"


def _cboe_parse(text: str, ratio_type: str, ma_days: int) -> Optional[dict]:
    """Parse a raw CBOE P/C CSV string. Returns a result dict or None.

    CBOE CSV format (stable since 2015):
      line 0 : "Chicago Board Options Exchange"   ← banner, ignored
      line 1 : ""                                 ← blank, ignored
      line 2 : DATE,CALLS,PUTS,TOTAL,P/C RATIO   ← column header
      line 3+: MM/DD/YYYY,...                     ← observations

    Strategy: scan forward until a line whose first field matches the date
    pattern — index minus 1 is the header. Robust to extra banner lines.

    C2 FIX  : signal keyed on MA window, not raw daily.
    M3 FIX  : ma_incomplete flag + ma_Nd_obs observation count exposed.
    M2 PREP : last observation date captured for upstream staleness check.
    m2 FIX  : data_start is None vs == 0 produce distinct, accurate messages.
    """
    try:
        lines = text.splitlines()

        # Locate first data row (MM/DD/YYYY format).
        data_start: Optional[int] = None
        for i, line in enumerate(lines):
            first_field = line.split(",")[0].strip().strip('"')
            if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", first_field):
                data_start = i
                break

        # m2 FIX: two distinct error messages for two distinct failure modes.
        if data_start is None:
            logger.warning(
                "CBOE %s: no date-formatted rows found in CSV — schema changed?",
                ratio_type,
            )
            return None
        if data_start == 0:
            logger.warning(
                "CBOE %s: data rows start at line 0 — no preceding header row;"
                " CSV schema changed?",
                ratio_type,
            )
            return None

        header_line = lines[data_start - 1]
        body = "\n".join(lines[data_start:])
        reader = csv.DictReader(io.StringIO(header_line + "\n" + body))

        values: list[float] = []
        last_date: Optional[str] = None

        for row in reader:
            # M2 PREP: capture last observation date.
            date_key = next((k for k in row if k and "DATE" in k.upper()), None)
            if date_key and row.get(date_key, "").strip():
                last_date = row[date_key].strip()

            # Locate P/C ratio column (name varies slightly across CBOE files).
            pc_key = next((k for k in row if k and "P/C" in k.upper()), None)
            if pc_key is None:
                continue
            raw = (row[pc_key] or "").strip()
            if not raw or raw == ".":
                continue
            try:
                v = float(raw)
            except ValueError:
                continue
            if v > 0:
                values.append(v)

        if not values:
            logger.warning("CBOE %s: no valid P/C observations parsed", ratio_type)
            return None

        latest  = values[-1]
        window  = values[-ma_days:]
        ma      = sum(window) / len(window)   # len(window) >= 1 — guaranteed

        # C2 FIX: signal on MA; signal_raw on latest (additive — for reference only).
        # M3 FIX: expose window size so callers know if MA is based on fewer obs.
        return {
            "pc_ratio":           round(latest, 2),
            f"ma_{ma_days}d":     round(ma, 2),
            "signal":             _cboe_signal(ratio_type, ma),        # C2: MA
            "signal_raw":         _cboe_signal(ratio_type, latest),    # additive
            "ma_incomplete":      len(window) < ma_days,               # M3
            f"ma_{ma_days}d_obs": len(window),                         # M3
            "observation_date":   last_date,                           # M2 prep
            "source":             f"CBOE {ratio_type.upper()} P/C · clôture J-1",
        }

    except Exception as exc:   # defensive: unknown CSV schema must not raise
        logger.warning("CBOE %s parse error (schema drift?): %s", ratio_type, exc)
        return None




# N3 — ^PCALL est un symbole RÉEL et vivant (coté ~0,68 par les screeners de
# marché au 2026-07-16), MAIS non garanti via yfinance : Yahoo rate-limite
# (HTTP 429) les IPs de crawler indépendamment du ticker, et renvoie souvent
# un historique vide pour ces indices value-only. Best-effort uniquement.
# Index P/C : AUCUNE source keyless fiable (N2) — dégradation vers None assumée.
# N6 (audit 17/07/2026, non-regression prudente) : deux runs de production
# consecutifs (2 patches retry differents) ont echoue a obtenir une seule
# donnee via ^PCALL — DataFrame vide, HTTP 200, jamais d'exception. Un audit
# independant rapporte que le lookup Yahoo repond "No results for '^PCALL'"
# HORS chemin rate-limite (429) — indice fort que le symbole n'est plus
# servi, pas seulement throttle. PRUDENCE : ce temoin est unique, non
# redonde (l'API search JSON etait 429 au moment du test). Mis a None par
# precaution operationnelle — arreter d'interroger une adresse qui echoue
# a 100% sur 3 runs observes, sans emettre de requete vouee a l'echec.
# Aucune perte fonctionnelle : la jambe equity etait deja [N/A] a chaque
# run. A reactiver UNIQUEMENT si un test manuel confirme un symbole vivant
# (ex.: recherche Yahoo hors periode de rate-limit, ou provider alternatif).
_CBOE_YF_TICKERS: dict[str, str | None] = {
    "equity": None,   # ^PCALL suspecte mort chez Yahoo (temoin unique, cf. commentaire ci-dessus)
    "index":  None,   # aucune source keyless fiable (N2)
}


def _cboe_fetch_yf(ratio_type: str, ma_days: int) -> Optional[dict]:
    """Fallback yfinance — seule voie keyless active depuis le retrait CBOE (N1/N2).

    Best-effort assumé (N3) : ^PCALL est réel mais Yahoo rate-limite les IPs
    de crawler ; un échec renvoie None, jamais d'exception, jamais de valeur
    inventée. yfinance est déjà dans requirements.txt — zéro dépendance.
    """
    ticker_sym = _CBOE_YF_TICKERS.get(ratio_type)
    if not ticker_sym:
        return None   # index P/C: aucune source keyless fiable (N2)
    try:
        import yfinance as yf   # lazy import — only on fallback path
        import random, time as _time
        # AUDIT-FIX (17/07/2026, synergie avec oanda_data._yf_history):
        # meme cause racine que le DXY manquant du 16/07 — un seul appel
        # yfinance, zero retry, alors que Yahoo rate-limite (429) l'IP
        # entiere independamment du ticker (prouve empiriquement sur
        # ^PCALL par les deux audits reseau). Jitter + 2 tentatives avec
        # backoff exponentiel, ciblees sur les erreurs 429/RateLimit
        # uniquement — meme forme que le fix deja applique dans
        # oanda_data.py, pour ne pas introduire un second pattern de
        # retry dans la meme codebase. Additif : signature, type de
        # retour et degradation finale vers None strictement inchanges.
        _time.sleep(random.uniform(0, 0.4))
        hist = None
        # AUDIT-FIX #2 (17/07/2026): the first pass only retried on a raised
        # exception (429/RateLimit). But the diagnostic notes already on
        # record for ^PCALL (macro-cl4_8 report, section on yfinance) state
        # this ticker frequently returns an HTTP 200 with an EMPTY DataFrame
        # instead of raising — a silent failure mode with no exception to
        # catch, so the retry above never fired for it. Retrying is now keyed
        # on the *outcome* (empty/short history) as well as on exceptions,
        # for up to 3 attempts total. Still additive: same signature, same
        # graceful None on final failure, no invented data — an empty result
        # after all retries is still logged and returned as None exactly as
        # before.
        for _attempt in range(3):
            try:
                hist = yf.Ticker(ticker_sym).history(period=f"{max(ma_days * 3, 15)}d")
            except Exception as _e:
                hist = None
                _is_rl = "429" in str(_e) or "RateLimit" in type(_e).__name__
                if _attempt < 2 and _is_rl:
                    _time.sleep(1.5 ** (_attempt + 1) + random.uniform(0, 0.4))
                    continue
                raise
            if hist is not None and not hist.empty and "Close" in hist.columns:
                break   # got real data — stop retrying
            if _attempt < 2:
                _time.sleep(1.5 ** (_attempt + 1) + random.uniform(0, 0.4))
        if hist is None or hist.empty or "Close" not in hist.columns:
            logger.warning("CBOE yfinance %s: empty history for %s after retries",
                           ratio_type, ticker_sym)
            return None
        values = [float(v) for v in hist["Close"].dropna() if v > 0]
        if not values:
            return None
        latest = values[-1]
        window = values[-ma_days:]
        ma     = sum(window) / len(window)
        return {
            "pc_ratio":           round(latest, 2),
            f"ma_{ma_days}d":     round(ma, 2),
            "signal":             _cboe_signal(ratio_type, ma),
            "signal_raw":         _cboe_signal(ratio_type, latest),
            "ma_incomplete":      len(window) < ma_days,
            f"ma_{ma_days}d_obs": len(window),
            "observation_date":   hist.index[-1].strftime("%m/%d/%Y"),
            # Étiquette honnête (N1/N2) : c'est le TOTAL P/C servi comme proxy
            # de l'equity, la distribution publique CBOE ayant été retirée —
            # ce n'est PAS un blocage contournable.
            "source":             (
                f"CBOE TOTAL P/C · yfinance {ticker_sym} "
                "[fallback/PROXY · distribution publique CBOE retirée]"
            ),
        }
    except Exception as exc:
        logger.warning("CBOE %s yfinance fallback failed: %s", ratio_type, exc)
        return None


def _cboe_fetch_one(ratio_type: str, ma_days: int) -> Optional[dict]:
    """Récupère une série P/C — chaîne ordonnée par fiabilité réelle vérifiée.

      1. (désactivée — N1/N2) Fetch CSV CBOE direct : endpoints publics MORTS
         au 2026-07-16 (301/404, distribution retirée — PAS un WAF). Ne pas
         taper une URL 404 à chaque appel. Pour réactiver cette voie si un
         miroir CSV au schéma legacy (ou un accès DataShop / All Access
         authentifié) devient disponible, brancher ici ::
             r = _get(url, extra_headers=_CBOE_HEADERS)
             if r is not None:
                 res = _cboe_parse(r.text, ratio_type, ma_days)
                 if res is not None:
                     return res
      2. Fallback yfinance (best-effort — N3).
      3. None → dégradation propre (mode single-leg P1 côté fetch_pc_ratio).

    Signature et schéma de retour identiques dans tous les cas. Ne lève jamais.
    """
    if ratio_type not in _CBOE_YF_TICKERS:
        logger.warning("CBOE: unknown ratio_type '%s'", ratio_type)
        return None
    return _cboe_fetch_yf(ratio_type, ma_days)


def fetch_pc_ratio(
    ma_days: int = 5,
    vix_value: Optional[float] = None,
) -> Optional[dict]:
    """Return CBOE Equity and Index P/C ratios with MA and signal qualifiers.

    Both series are fetched concurrently (ThreadPoolExecutor × 2).

    C1 FIX: ``vix_value`` optional parameter (default None — fully backward-
        compatible). When provided, ``composite_signal`` becomes a true
        VIX × P/C cross-signal via ``_vix_pc_composite()``. When None,
        ``composite_signal`` is computed from P/C × P/C only via
        ``_pc_composite()`` (C4+M1 corrected).

    Return schema — all existing keys preserved (zero regression).
    New additive keys marked [NEW]::

        {
          "equity": {
              "pc_ratio":     0.57,              # raw daily — display only
              "ma_5d":        0.61,              # MA (signal basis — C2)
              "signal":       "COMPLACENCE",     # on MA — C2 fix
              "signal_raw":   "DANGER ZONE",     # [NEW] raw daily ref
              "ma_incomplete": False,            # [NEW] M3
              "ma_5d_obs":    5,                 # [NEW] M3
              "observation_date": "07/10/2026",  # [NEW] M2 prep
              "source":       "CBOE EQUITY P/C · clôture J-1",
          },
          "index":        { ... },               # same structure
          "delta_eq_idx": -0.24,
          "composite_signal": "COMPLACENCE RETAIL + HEDGE INSTITUTIONNEL",
          "stale":        False,                 # [NEW] M2
        }

    Partial failure (one series unavailable) → dict returned without
        ``delta_eq_idx`` / ``composite_signal`` / ``stale``.
    Both unavailable → ``None``. Never raises.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_eq  = ex.submit(_cboe_fetch_one, "equity", ma_days)
        fut_idx = ex.submit(_cboe_fetch_one, "index",  ma_days)
        equity  = fut_eq.result()
        index   = fut_idx.result()

    if equity is None and index is None:
        logger.warning("fetch_pc_ratio: both CBOE series unavailable")
        return None

    out: dict = {"equity": equity, "index": index}

    # AUDIT-FIX (validation audit, finding P1 — 15/07/2026): this used to be
    # an unconditional early return with no composite_signal/stale at all
    # whenever EITHER leg was missing — including "index", which has no
    # yfinance fallback by design (_CBOE_YF_TICKERS["index"] is None) and is
    # therefore the leg most likely to fail whenever the direct CBOE fetch
    # is blocked (the module's own docstrings document this as an expected
    # scenario, e.g. cloud-host IP blocking). That silently discarded a
    # real, available equity P/C reading every time. Degrade gracefully
    # instead: when exactly one leg succeeded, expose a single-leg
    # composite (clearly labelled as degraded) so the sentiment layer isn't
    # fully blind just because the other leg has no fallback path. The
    # true both-missing case (both equity and index unavailable) still
    # returns ``None`` above — nothing to salvage there.
    if equity is None or index is None:
        available, leg_name, other_leg = (
            (equity, "equity", "index") if equity is not None else (index, "index", "equity")
        )
        if available is not None:
            today = datetime.date.today()
            stale = False
            obs_str = available.get("observation_date")
            if obs_str:
                try:
                    obs_date = datetime.datetime.strptime(obs_str, "%m/%d/%Y").date()
                    stale = (today - obs_date).days > _CBOE_STALE_DAYS
                except ValueError:
                    pass   # unparseable date — not flagged (avoid false positives)
            out["stale"] = stale
            out["degraded_single_leg"] = leg_name
            out["composite_signal"] = (
                f"{available['signal']} — {leg_name} seul "
                f"({other_leg} P/C indisponible)"
            )
        return out

    # ── M2 FIX: staleness flag ────────────────────────────────────────────────
    # P/C is published post-close J-1. Gap > _CBOE_STALE_DAYS calendar days
    # (covers weekends + public holidays) flags the data as stale.
    # VIX-vs-P/C timestamp comparison requires VIX date → handled in
    # macro_engine.py which owns both values.
    today = datetime.date.today()
    stale = False
    for series in (equity, index):
        obs_str = series.get("observation_date")
        if obs_str:
            try:
                obs_date = datetime.datetime.strptime(obs_str, "%m/%d/%Y").date()
                if (today - obs_date).days > _CBOE_STALE_DAYS:
                    stale = True
                    break
            except ValueError:
                pass   # unparseable date — not flagged (avoid false positives)
    out["stale"] = stale

    # ── Composite signal ──────────────────────────────────────────────────────
    eq_ma   = equity[f"ma_{ma_days}d"]
    idx_ma  = index[f"ma_{ma_days}d"]
    eq_sev  = _CBOE_SEVERITY.get(equity["signal"], 0)
    idx_sev = _CBOE_SEVERITY.get(index["signal"], 0)

    out["delta_eq_idx"] = round(equity["pc_ratio"] - index["pc_ratio"], 2)

    # C1 FIX: true VIX × P/C when VIX available; P/C-only otherwise.
    if vix_value is not None:
        out["composite_signal"] = _vix_pc_composite(vix_value, eq_ma, idx_ma)
    else:
        out["composite_signal"] = _pc_composite(eq_ma, idx_ma, eq_sev, idx_sev)

    return out
