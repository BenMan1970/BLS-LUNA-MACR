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
"""
from __future__ import annotations

import csv
import io
import logging
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


def _get(url: str, **kwargs) -> Optional[requests.Response]:
    """Single GET with unified timeout / headers / error handling.

    Returns the Response on HTTP 200, else ``None`` (logged). Never raises.
    """
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except requests.RequestException as exc:
        logger.warning("HTTP GET failed for %s: %s", url, exc)
        return None


# ===========================================================================
# 1. FRED API
# ===========================================================================
_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# Correct FRED series IDs for the four tracked policy rates.
# FEDFUNDS kept per spec; the other three replace the invalid Quandl codes.
_CB_RATE_SERIES: dict[str, str] = {
    "FED": "FEDFUNDS",        # Effective Federal Funds Rate (monthly avg, %)
    "BCE": "ECBDFR",          # ECB Deposit Facility Rate (%)
    "BoJ": "IRSTCB01JPM156N",  # Japan immediate rates / policy rate proxy (%)
    "BoE": "BOERUKM",         # BoE Official Bank Rate (%)
}

_LIQUIDITY_SERIES = "TEDRATE"  # TED spread — funding stress proxy


def _fred_api_key() -> Optional[str]:
    """Resolve the FRED API key from st.secrets, else ``None``."""
    if not _ST_OK:
        return None
    try:
        key = st.secrets.get("FRED_API_KEY") or st.secrets.get("fred_api_key")
        return str(key) if key else None
    except Exception:  # pragma: no cover
        return None


def _fred_series(series_id: str) -> Optional[float]:
    """Return the latest numeric observation of a FRED series, or ``None``.

    Uses ``sort_order=desc&limit=1`` so exactly one row — the most recent —
    is fetched. FRED encodes missing observations as the string ".", which is
    treated as ``None`` (never coerced to a number).
    """
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


def fetch_central_bank_rates() -> dict[str, float]:
    """Return ``{cb_name: rate_pct}`` for every rate FRED can serve.

    Keys match the ``_CB_DEFS`` names in macro_engine ("FED", "BCE", "BoJ",
    "BoE"). Missing / unavailable rates are simply omitted from the dict so the
    caller can distinguish "sourced" from "not sourced" per central bank.
    Returns ``{}`` if the key is absent or every series fails.
    """
    out: dict[str, float] = {}
    for name, series_id in _CB_RATE_SERIES.items():
        val = _fred_series(series_id)
        if val is not None:
            out[name] = val
    if not out:
        logger.warning("fetch_central_bank_rates: no CB rate resolved (no key?)")
    return out


def fetch_liquidity_stress() -> Optional[float]:
    """Return the latest TED spread (%) as a funding-stress gauge, or ``None``."""
    return _fred_series(_LIQUIDITY_SERIES)


# ===========================================================================
# 2. CME FedWatch probabilities
# ===========================================================================
_FEDWATCH_URL = "https://www.cmegroup.com/CmeWS/md/BCM/BCM.json"


def fetch_fedwatch_probabilities() -> Optional[dict[str, int]]:
    """Return ``{'pause_pct', 'cut_pct', 'hike_pct'}`` for the next FOMC, or None.

    The CME BCM endpoint is undocumented and its schema changes; this parser is
    intentionally defensive. It walks the JSON looking for the nearest meeting's
    probability distribution across rate-move buckets, then aggregates buckets
    into cut / pause / hike relative to the current target. Any schema mismatch
    degrades to ``None`` (caller falls back to [N/A]).
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
        # The payload is a nested structure whose exact shape is unstable.
        # Strategy: locate the first list of meeting dicts that carry per-bucket
        # probabilities, take the nearest meeting, and classify buckets by the
        # sign of their implied rate change vs the "current"/"unchanged" bucket.
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
        # Normalise rounding drift onto the largest bucket.
        drift = 100 - sum(result.values())
        if drift:
            biggest = max(result, key=result.get)
            result[biggest] += drift
        return result
    except Exception as exc:  # defensive: unknown schema must not raise
        logger.warning("FedWatch parse failed (schema drift?): %s", exc)
        return None


def _fedwatch_locate_meetings(data) -> list:
    """Best-effort walk of the BCM payload to find a list of meeting dicts.

    Returns a list of meeting-like dicts sorted by date ascending, or ``[]``.
    Kept separate so the fragile schema-guessing is isolated and testable.
    """
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
    """Extract ``[(delta_bp, probability_fraction), ...]`` from a meeting dict.

    Returns an empty list if no interpretable buckets are found.
    """
    buckets: list[tuple[int, float]] = []
    # Probabilities may live under a nested list keyed by "probabilities" or
    # be flattened onto the meeting dict itself.
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
            # Normalise probability that may be expressed 0–100 or 0–1.
            frac = prob / 100.0 if prob > 1.0 else prob
            buckets.append((delta_bp, frac))
    return buckets


# ===========================================================================
# 3. CFTC Commitments of Traders (Non-Commercials, legacy financial futures)
# ===========================================================================
_CFTC_INDEX = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"

# Map BLUESTAR currency -> the CFTC contract market name substring.
# Matching is case-insensitive substring on the "Market and Exchange Names"
# column, restricted to CME (CHICAGO MERCANTILE EXCHANGE).
_CFTC_CONTRACTS: dict[str, str] = {
    "EUR": "EURO FX",
    "JPY": "JAPANESE YEN",
    "GBP": "BRITISH POUND",
    "CHF": "SWISS FRANC",
    "AUD": "AUSTRALIAN DOLLAR",
    "CAD": "CANADIAN DOLLAR",
    "NZD": "NEW ZEALAND DOLLAR",
    # USD is derived, not a CME FX contract — intentionally absent.
}

# Legacy report column headers (as published in the CSV / "socrata" export).
_COL_MARKET = "Market and Exchange Names"
_COL_NC_LONG = "Noncommercial Positions-Long (All)"
_COL_NC_SHORT = "Noncommercial Positions-Short (All)"
_COL_DATE = "As of Date in Form YYYY-MM-DD"
_CME_TOKEN = "CHICAGO MERCANTILE EXCHANGE"


def _cftc_find_report_url() -> Optional[str]:
    """Scrape the COT index page for the latest financial-futures report link.

    Looks for an ``other_disclaim_*`` archive (per spec) or, failing that, any
    financial-futures CSV/zip link. Returns an absolute URL or ``None``.
    """
    if not _BS_OK:
        return None
    r = _get(_CFTC_INDEX)
    if r is None:
        return None
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:  # pragma: no cover
        logger.warning("CFTC index parse failed: %s", exc)
        return None

    def _abs(href: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return "https://www.cftc.gov" + href
        return "https://www.cftc.gov/" + href

    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    # Priority 1: the specified other_disclaim CSV.
    for href in hrefs:
        low = href.lower()
        if "other_disclaim" in low and low.endswith((".csv", ".zip")):
            return _abs(href)
    # Priority 2: any legacy financial-futures CSV/zip.
    for href in hrefs:
        low = href.lower()
        if (("fin" in low or "fut" in low or "deacot" in low)
                and low.endswith((".csv", ".zip"))):
            return _abs(href)
    logger.warning("CFTC: no report CSV/zip link found on index page")
    return None


def _cftc_load_rows(url: str) -> Optional[list[dict]]:
    """Download the report (CSV or zipped CSV) and return DictReader rows."""
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
    """Fetch a column tolerantly (headers vary in whitespace/case slightly)."""
    if wanted in row:
        return row[wanted]
    want_norm = re.sub(r"\s+", " ", wanted).strip().lower()
    for k, v in row.items():
        if k and re.sub(r"\s+", " ", k).strip().lower() == want_norm:
            return v
    return None


def fetch_cot_data() -> tuple[dict[str, int], Optional[str]]:
    """Return ``({ccy: net_noncommercial}, as_of_date_str)`` from the CFTC.

    ``net = NonComm Long - NonComm Short``. Only CME FX contracts in the
    ``_CFTC_CONTRACTS`` mapping are extracted. On any failure returns
    ``({}, None)`` — the caller then degrades to [N/A].
    """
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
        # Optional "FUTURES ONLY" guard when the column exists.
        rtype = _cftc_col(row, "Report Type") or _cftc_col(row, "FutOnly_or_Combined")
        if rtype and "FUT" not in rtype.upper() and "ONLY" not in rtype.upper():
            continue

        for ccy, token in _CFTC_CONTRACTS.items():
            if ccy in net_by_ccy:
                continue  # first (most recent) match wins
            if token in market:
                long_s = _cftc_col(row, _COL_NC_LONG)
                short_s = _cftc_col(row, _COL_NC_SHORT)
                if long_s is None or short_s is None:
                    continue
                try:
                    net = int(float(long_s.replace(",", "")
                                    )) - int(float(short_s.replace(",", "")))
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

# Two accepted phrasings; the site has used both over time.
_GDPNOW_RE = re.compile(
    r"GDPNow (?:model )?estimate for (?:real GDP growth[^)]*\)[^0-9]*)?"
    r"Q?[1-4]?\s*\d{4}\s*is\s*(-?[\d.]+)\s*(?:percent|%)",
    re.IGNORECASE,
)
_GDPNOW_LATEST_RE = re.compile(
    r"[Ll]atest estimate:\s*(-?[\d.]+)\s*(?:percent|%)"
)


def fetch_gdp_nowcast() -> Optional[float]:
    """Return the current Atlanta Fed GDPNow estimate (%), or ``None``.

    Tries the "Latest estimate: X percent" banner first (most stable), then the
    fuller "GDPNow ... estimate for Q# YYYY is X percent" sentence.
    """
    r = _get(_GDPNOW_URL)
    if r is None:
        return None
    html = r.text

    # Prefer the plain-text extract if BeautifulSoup is available (avoids
    # matching inside scripts / attributes), else fall back to raw HTML.
    text = html
    if _BS_OK:
        try:
            text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        except Exception:  # pragma: no cover
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
