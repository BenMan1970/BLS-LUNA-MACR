"""External data sources for the BLUESTAR engine â€” keyed / scraped feeds.

This module centralises every *external* source that requires either an API
key (FRED) or web scraping (CFTC, CME FedWatch, Atlanta Fed GDPNow). It is the
single upgrade path away from the [PROXY]/[N/A] degradation that the keyless
core falls back to.

Contract / design rules (must not be violated by callers):
  * Every public function is **best-effort**: on any failure (missing key,
    network error, parse error, unexpected schema) it returns ``None`` or an
    empty container â€” it NEVER raises. The caller degrades to [N/A].
  * No function ever *invents* a value. A missing observation ("." in FRED,
    an empty scrape) yields ``None``, never a placeholder number.
  * User overrides always take precedence *upstream* (in macro_engine /
    oanda_data). This module has no knowledge of overrides by design.
  * All diagnostics go through ``logging.warning`` â€” never ``print``.

Note on FRED series IDs: the original spec referenced Quandl-style codes
(``ECB/ECB``, ``BOJ/BOJ``, ``BOE/BOE``) which do not exist on FRED and would
return ``None`` forever. We substitute the correct FRED series IDs (documented
in ``_CB_RATE_SERIES``) so the feature actually works. ``FEDFUNDS`` is kept
as specified.

Changelog â€” institutional audit patch (2026-07-11):
  C1  fetch_pc_ratio: vix_value optional param; _vix_pc_composite() added.
  C2  _cboe_parse: signal computed on MA window, not raw daily latest.
  C3  _CBOE_THRESHOLDS: equity thresholds recalibrated on empirical percentiles.
  C4  _CBOE_SEVERITY: severity map added; _pc_composite() preserves severity floor.
  M1  _pc_composite: 6-quadrant matrix (2 missing regimes added).
  M2  _cboe_parse: observation_date captured; fetch_pc_ratio: stale flag added.
  M3  _cboe_parse: ma_incomplete flag + ma_Nd_obs observation count added.
  m1  _cboe_signal: guard for unknown ratio_type â†’ returns "N/A".
  m2  _cboe_parse: data_start is None vs == 0 produce distinct error messages.

Changelog â€” rÃ©seau vÃ©rifiÃ© empiriquement (2026-07-17, deux environnements
indÃ©pendants : crawler le 16/07 ~02:01 UTC + sandbox BLUESTAR le 17/07) :
  N1  Â§5 CBOE : les URLs .../datahouse/equitypc.csv & indexpc.csv sont MORTES
      (301/404 â€” infra retirÃ©e lors de la refonte du site CBOE, PAS un blocage
      WAF comme le supposait l'ancien commentaire). Elles coÃ»taient 1 requÃªte
      HTTP inutile par jambe Ã  chaque run. SupprimÃ©es de la chaÃ®ne active ;
      _cboe_parse conservÃ© pour un Ã©ventuel miroir CSV futur (voir Â§5).
  N2  Â§5 CBOE : preuve que CBOE ne distribue plus les P/C ratios en public â€”
      cdn.cboe.com sert _VIX.json (1 153 407 octets) et _SKEW.json normalement
      depuis la mÃªme IP, mais _PCALL/_EQUITYPC/_TOTALPC/_INDEXPC/_VIXPC/PCALL
      .json renvoient tous 403 AccessDenied (objet absent). DonnÃ©e dÃ©placÃ©e
      derriÃ¨re DataShop / All Access API (payant). Jambe index : aucune source
      keyless fiable â€” dÃ©gradation vers None assumÃ©e (mode single-leg P1).
  N3  Â§5 CBOE : ^PCALL confirmÃ© symbole RÃ‰EL et vivant, MAIS Yahoo
      rate-limite (HTTP 429) les IPs de crawler indÃ©pendamment du ticker â€”
      fallback yfinance conservÃ© en best-effort, Ã©tiquetÃ© honnÃªtement.
  N4  Â§2 FedWatch : la probabilitÃ© rendue pouvait rester figÃ©e ~1 semaine sans
      indice de fraÃ®cheur (anomalie audit A1 du 16/07). La clÃ© additive
      "as_of" est dÃ©sormais extraite du payload BCM quand elle y est prÃ©sente
      (best-effort, schÃ©ma-agnostique â€” jamais inventÃ©e) pour que l'aval
      affiche la date de prÃ©lÃ¨vement.
  N5  Â§5 CBOE : ^PCALL live-testÃ© par l'utilisateur final (17/07/2026) â†’
      HTTP 404 "Quote not found for symbol: ^PCALL" / "possibly delisted".
      N3 ci-dessus est donc caduque : le symbole n'Ã©tait pas rate-limitÃ©,
      il est supprimÃ© chez Yahoo. Preuve directe, pas une infÃ©rence.
  N6  Â§5 CBOE : DÃ‰COMMISSIONNÃ‰. Sur la base de N2 (CBOE a retirÃ© la
      distribution publique keyless), N5 (^PCALL confirmÃ© mort, pas
      throttlÃ©) et de 4 audits croisÃ©s indÃ©pendants (17/07/2026) n'ayant
      trouvÃ© aucune source gratuite, documentÃ©e et conforme aux CGU,
      l'implÃ©mentation CBOE (~830 lignes) a Ã©tÃ© retirÃ©e. fetch_pc_ratio()
      est un stub permanent retournant None â€” voir le commentaire ADR en
      tÃªte de la section 5 (fin de fichier) pour le raisonnement complet.
      Le changelog C1â†’N5 ci-dessus est un historique factuel, pas une
      description du code actif.
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
    logger.warning("beautifulsoup4 unavailable â€” CFTC/GDPNow scraping disabled")

# ---------------------------------------------------------------------------
# HTTP configuration
# ---------------------------------------------------------------------------
_TIMEOUT = 12          # seconds â€” within the 10â€“15 s spec band
_HEADERS = {
    # RC5/RC6 FIX (Incident Review Board): User-Agent browser-like. L'UA
    # "compatible bot" dÃ©clenchait des 403 (Akamai) sur BoE IADB / CME FedWatch;
    # un UA standard rÃ©duit ce risque (les blocages Akamai persistants restent
    # dÃ©gradÃ©s en [N/A] avec diagnostic via la section Certification).
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,text/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
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
# Audit A1 FIX : SÃ©ries vÃ©rifiÃ©es live 2026-07-12.
# - FEDFUNDS : mensuel, remplacÃ© par DFEDTARU (quotidien, borne haute).
# - IRSTCB01JPM156N : coquille 'B', discontinuÃ©e. RemplacÃ© par IRSTCI01JPM156N.
# - BOERUKM : gelÃ©e depuis jan. 2017. AUCUNE sÃ©rie BoE fiable sur FRED.
#   -> BoE retirÃ©e du mapping FRED. Voir plus bas (section 1bis) pour la
#      source BoE dÃ©diÃ©e (API officielle Bank of England, hors FRED) ajoutÃ©e
#      le 15/07/2026 â€” enrichissement pur, ne touche Ã  rien de ce qui suit.
# ---------------------------------------------------------------------------
_CB_RATE_SERIES: dict[str, str] = {
    "FED": "DFEDTARU",
    "BCE": "ECBDFR",
    "BoJ": "IRSTCI01JPM156N",
}

# PlausibilitÃ© : bornes larges pour dÃ©tecter les sentinelles FRED (-999, .)
# et les valeurs aberrantes. Un taux hors bornes est Ã©cartÃ©.
_CB_RATE_BOUNDS: dict[str, tuple[float, float]] = {
    "FED": (-0.5, 15.0),
    "BCE": (-0.5, 15.0),
    "BoJ": (-1.0, 10.0),
    "BoE": (-0.5, 15.0),
}

# Staleness max : au-delÃ , la sÃ©rie est considÃ©rÃ©e potentiellement gelÃ©e.
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
# more generous window is used instead â€” long enough to tolerate a normal
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
# 1bis. Bank of England â€” API officielle (hors FRED)
#
# AUDIT-ENRICHMENT (15/07/2026): FRED n'a aucune sÃ©rie BoE Bank Rate fiable
# (BOERUKM gelÃ©e depuis 2017, voir commentaire ci-dessus). Cette section
# interroge directement la base IADB de la Bank of England (endpoint public,
# sans clÃ©) â€” mÃªme contrat "never raise, log and return None" que le reste
# du module. Purement additif : ne touche ni _fred_series, ni
# _fred_series_dated, ni aucune des sÃ©ries FED/BCE/BoJ existantes.
# ===========================================================================
_BOE_IADB_URL = "https://www.bankofengland.co.uk/boeapps/iadb/fromshowcolumns.asp"
_BOE_BANK_RATE_CODE = "IUDBEDR"  # Bank Rate officielle (code sÃ©rie IADB)

# Formats de date observÃ©s dans les exports IADB (varie selon les endpoints
# BoE) â€” essayÃ©s dans l'ordre jusqu'Ã  ce qu'un marche.
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

    Returns ``(value, date_iso)`` or ``None`` on any failure â€” never raises,
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
            logger.warning("BoE IADB: aucune ligne de donnÃ©es exploitable dans la rÃ©ponse")
            return None
        data_rows.sort(key=lambda t: t[0])
        last_date, last_val = data_rows[-1]
        return last_val, last_date.isoformat()
    except (csv.Error, IndexError) as exc:
        logger.warning("BoE IADB parse error: %s", exc)
        return None


def central_bank_rate_source(name: str) -> str:
    """Human-readable source label for a rate resolved by
    ``fetch_central_bank_rates()`` â€” lets callers (macro_engine.py) render
    an accurate stamp instead of hardcoding "FRED" for every entry, which
    would now be wrong for "BoE" specifically (audit-enrichment 15/07/2026).
    """
    return "Bank of England Â· IADB" if name == "BoE" else "FRED"


def fetch_central_bank_rates() -> dict[str, float]:
    """Return ``{cb_name: rate_pct}`` for every rate a live source can serve.

    Audit A1 FIX: chaque sÃ©rie FRED rÃ©solue est contrÃ´lÃ©e en fraÃ®cheur (date
    d'observation) et en plausibilitÃ© (bornes _CB_RATE_BOUNDS). Une sÃ©rie qui
    Ã©choue est Ã©cartÃ©e avec un WARNING, dÃ©gradant proprement vers [N/A] en aval.

    Audit-ENRICHMENT (15/07/2026): "BoE" est dÃ©sormais rÃ©solu ici aussi, via
    la Bank of England elle-mÃªme plutÃ´t que FRED (voir section 1bis) â€” le
    contrat public de cette fonction ({name: float}) est inchangÃ©, BoE
    apparaÃ®t simplement comme une clÃ© de plus quand la source rÃ©pond.
    Utiliser ``central_bank_rate_source(name)`` en aval pour savoir quelle
    source a rÃ©ellement servi une entrÃ©e donnÃ©e (au lieu de supposer "FRED").
    """
    out: dict[str, float] = {}

    def _resolve_fred(name: str, series_id: str) -> Optional[float]:
        res = _fred_series_dated(series_id)
        if res is None:
            logger.warning("CB rate: %s (%s) â€” aucune observation FRED", name, series_id)
            return None

        val, dt_iso = res

        # --- ContrÃ´le 1 : fraÃ®cheur ---
        try:
            obs_date = datetime.date.fromisoformat(dt_iso)
            age_days = (datetime.date.today() - obs_date).days
            if age_days > _CB_MAX_STALENESS_DAYS:
                logger.warning(
                    "CB rate: %s (%s) â€” observation datÃ©e du %s (%d j) "
                    "â†’ sÃ©rie potentiellement gelÃ©e, valeur Ã©cartÃ©e",
                    name, series_id, dt_iso, age_days,
                )
                return None
        except (ValueError, TypeError):
            # Date illisible â†’ on NE GARDE PAS la valeur (Ã©limine le risque de sentinelle)
            logger.warning(
                "CB rate: %s (%s) â€” date illisible '%s', valeur Ã©cartÃ©e",
                name, series_id, dt_iso,
            )
            return None

        # --- ContrÃ´le 2 : plausibilitÃ© ---
        bounds = _CB_RATE_BOUNDS.get(name)
        if bounds is not None:
            lo, hi = bounds
            if not (lo <= val <= hi):
                logger.warning(
                    "CB rate: %s (%s) â€” valeur %.4f hors bornes [%.1f, %.1f], Ã©cartÃ©e",
                    name, series_id, val, lo, hi,
                )
                return None

        return val

    def _resolve_boe() -> Optional[float]:
        res = _boe_bank_rate()
        if res is None:
            logger.warning("CB rate: BoE (IADB %s) â€” aucune observation", _BOE_BANK_RATE_CODE)
            return None

        val, dt_iso = res

        # --- ContrÃ´le 1 : fraÃ®cheur (fenÃªtre BoE dÃ©diÃ©e, cf. _BOE_MAX_STALENESS_DAYS) ---
        try:
            obs_date = datetime.date.fromisoformat(dt_iso)
            age_days = (datetime.date.today() - obs_date).days
            if age_days > _BOE_MAX_STALENESS_DAYS:
                logger.warning(
                    "CB rate: BoE (IADB) â€” observation datÃ©e du %s (%d j) "
                    "â†’ sÃ©rie potentiellement gelÃ©e, valeur Ã©cartÃ©e",
                    dt_iso, age_days,
                )
                return None
        except (ValueError, TypeError):
            logger.warning("CB rate: BoE (IADB) â€” date illisible '%s', valeur Ã©cartÃ©e", dt_iso)
            return None

        # --- ContrÃ´le 2 : plausibilitÃ© ---
        bounds = _CB_RATE_BOUNDS.get("BoE")
        if bounds is not None:
            lo, hi = bounds
            if not (lo <= val <= hi):
                logger.warning(
                    "CB rate: BoE (IADB) â€” valeur %.4f hors bornes [%.1f, %.1f], Ã©cartÃ©e",
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
                logger.exception("CB rate: exception imprÃ©vue pour %s", name)
                val = None
            if val is not None:
                out[name] = val
                
    _all_names = set(_CB_RATE_SERIES) | {"BoE"}
    if not out:
        logger.error("CB rate: AUCUN taux rÃ©solu â€” diffÃ©rentiels indisponibles")
    else:
        missing = _all_names - set(out)
        if missing:
            logger.warning("CB rate: taux manquants pour %s â€” dÃ©gradation [N/A] attendue", ", ".join(sorted(missing)))

    return out


def fetch_liquidity_stress() -> Optional[float]:
    """Return the latest SOFR âˆ’ EFFR spread in basis points, or ``None``."""
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


# ClÃ©s de fraÃ®cheur candidates dans les payloads JSON de marchÃ© (schÃ©ma BCM
# non documentÃ© publiquement â€” liste normalisÃ©e, comparÃ©e sans '_' ni espaces,
# en minuscules). La clÃ© "date" seule est volontairement EXCLUE : dans ce
# payload elle porte la date de RÃ‰UNION FOMC (cible), pas l'horodatage de
# prÃ©lÃ¨vement â€” l'accepter afficherait une fausse fraÃ®cheur.
_FEDWATCH_TS_KEYS = frozenset({
    "lastupdated", "lastupdate", "updated", "updatetime", "updatetimeutc",
    "timestamp", "asof", "asofdate", "quotedate", "quotetime",
    "tradedate", "lasttradedate", "pricedate", "lastpriceupdate",
})


def _fedwatch_extract_timestamp(data) -> Optional[str]:
    """Best-effort : extrait l'horodatage de fraÃ®cheur du payload BCM.

    SchÃ©ma-agnostique (le parser de probabilitÃ©s ci-dessous l'est dÃ©jÃ ) :
    retourne la premiÃ¨re valeur textuelle non vide trouvÃ©e sous une clÃ© de
    ``_FEDWATCH_TS_KEYS``, tronquÃ©e Ã  40 caractÃ¨res â€” ou ``None`` si le
    payload n'en porte aucune (cas parfaitement admis : l'aval masque alors
    simplement la mention). N'invente jamais de valeur, ne lÃ¨ve jamais.
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
        # Plusieurs candidats possibles (payload multi-nÅ“uds) : le max
        # lexicographique = le plus rÃ©cent pour les formats ISO-like ; pour
        # les formats non triables l'ordre est arbitraire mais reste rÃ©el.
        return max(found)[:40]
    except Exception:
        return None


def fetch_fedwatch_probabilities() -> Optional[dict]:
    """Return ``{'pause_pct', 'cut_pct', 'hike_pct'}`` for the next FOMC, or None.

    N4 (2026-07-17) : clÃ© additive ``'as_of'`` (str) prÃ©sente uniquement quand
    le payload BCM porte lui-mÃªme un horodatage exploitable â€” date de
    prÃ©lÃ¨vement affichÃ©e en aval (audit A1 : probabilitÃ©s figÃ©es ~1 semaine
    sans mention de fraÃ®cheur). Les trois clÃ©s historiques sont inchangÃ©es.
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
        # N4 : horodatage de prÃ©lÃ¨vement quand le payload le fournit (additif).
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
# GDPNow FIX (Incident Review Board, 2026): la page Atlanta Fed a changÃ© de
# format â€” l'estimation prÃ©cÃ¨de dÃ©sormais le libellÃ©, ex:
#   "1.7% Latest GDPNow Estimate for 2026:Q2"
# (le "%%" est un artefact d'Ã©chappement HTML). Regex ciblÃ©, validÃ© live.
_GDPNOW_LATEST2_RE = re.compile(
    r"(-?[\d.]+)\s*%+\s*Latest GDPNow Estimate",
    re.IGNORECASE,
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
    for rx in (_GDPNOW_LATEST2_RE, _GDPNOW_LATEST_RE, _GDPNOW_RE):
        m = rx.search(text)
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                continue
    logger.warning("GDPNow: estimate not found in page text")
    return None


# ===========================================================================
# 5. CBOE Put/Call Ratios â€” DECOMMISSIONED (17/07/2026)
#
# ADR: feature removed, not just degraded. Root cause proven (not inferred):
#   - ^PCALL is delisted on Yahoo: live-tested by the end user from their own
#     machine on 17/07/2026 -> HTTP 404 "Quote not found for symbol: ^PCALL"
#     / "possibly delisted". Not a rate-limit, not a network fault.
#   - CBOE's public CDN returns 403 AccessDenied on every P/C endpoint
#     (_PCALL/_EQUITYPC/_INDEXPC/_TOTALPC/_VIXPC) while _VIX/_SKEW on the
#     SAME CDN return 200 (byte-exact) -- a deliberate data removal by CBOE,
#     not a WAF or generic outage.
#   - Four independent cross-model audits (17/07/2026) found no free,
#     documented, ToS-compliant programmatic source. The only defensible
#     paid options (Barchart OnDemand $CPC/$CPCI, YCharts API) are
#     disproportionate for a signal weighted 0.05 and never regime-
#     determining alone (see config.REGIME_MATERIAL_SIGNAL_WEIGHT and
#     regime_engine._pc_indicator's own documented contract).
#
# The full CBOE fetch/parse/composite implementation (~830 lines: header
# spoofing, dead-endpoint diagnostics, yfinance fallback with retry/jitter,
# FR-labeled signal classification, VIX x P/C and P/C-only composites,
# single-leg degradation) was deleted rather than left dormant, per the
# user's explicit decommission decision. It is fully recoverable from git
# history / the pre-17/07/2026 version of this file if CBOE, Yahoo, or a
# new provider ever restores a free keyless source.
#
# fetch_pc_ratio() is kept as a single, permanent no-op choke point so
# macro_engine.py needs zero changes to its call site or its downstream
# handling (pc_data=None already degrades gracefully everywhere it is
# consumed -- verified against regime_engine._pc_indicator and
# macro_engine.build_macro_overlay, both of which already treated None as
# a normal, expected state before this decommission).
# ===========================================================================

def fetch_pc_ratio(
    ma_days: int = 5,
    vix_value: Optional[float] = None,
) -> Optional[dict]:
    """Decommissioned. Always returns None -- no network call, no exception.

    See the module-level ADR comment above for why. Signature unchanged
    from the pre-decommission version so call sites need no edits.
    """
    return None
