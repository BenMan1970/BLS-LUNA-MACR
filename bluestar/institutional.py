"""Institutional Intelligence Layer — BLUESTAR engine.

Adds the *institutional research desk* enrichment layer requested in the
upgrade brief WITHOUT touching the existing price (Oanda) or macro (FRED)
sources. It only fills fields that today degrade to ``[N/A]`` / ``[PROXY]``,
using genuinely free / official sources that were verified reachable:

  * yfinance ...... VIX, VVIX, CBOE SKEW, MOVE, US10Y (realized vol), FX pairs
  * CFTC Socrata .. legacy Non-commercial COT *history* (z-score / percentile)
  * Forex Factory . actual-vs-forecast (homemade BLUESTAR Macro Surprise)
  * Atlanta Fed ... GDPNow value + prior estimate + delta + publication date
  * FRED .......... 2s10s curve & funding spread (uses the production key)

DESIGN CONTRACT (identical to external_sources.py — do not violate):
  * Every public function is best-effort: on ANY failure it returns ``None``
    or an empty container. It NEVER raises.
  * No function invents a vendor figure. Where a proprietary series (Citi CESI,
    CVIX, FX risk-reversals, OIS-implied probabilities, dealer gamma) has no
    free feed, we expose a clearly *self-branded, methodology-labelled* derived
    metric instead of faking a vendor number. That is the institutionally
    honest way to eliminate a blank field.
  * All diagnostics go through ``logging`` — never ``print``.
"""
from __future__ import annotations

import logging
import math
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0 (BLUESTAR institutional desk)"}

# Optional deps -------------------------------------------------------------
try:
    import yfinance as yf  # type: ignore
    _YF_OK = True
except Exception:  # pragma: no cover
    _YF_OK = False

try:
    import streamlit as st  # type: ignore
    _ST_OK = True
except Exception:  # pragma: no cover
    _ST_OK = False


def _get(url: str, **kw) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, **kw)
        r.raise_for_status()
        return r
    except Exception as exc:  # pragma: no cover - network
        logger.warning("institutional._get failed %s: %s", url, exc)
        return None


# ===========================================================================
# 0. Small formatting / freshness helpers
# ===========================================================================
def fr_num(x: Optional[float], dec: int = 1) -> str:
    if x is None:
        return "N/A"
    s = f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")
    return s


def freshness(ts: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Human 'freshness' string, e.g. 'il y a 12 min' / 'il y a 3 h'."""
    if ts is None:
        return ""
    now = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = (now - ts).total_seconds()
    if delta < 0:
        return "à l'instant"
    mins = int(delta // 60)
    if mins < 1:
        return "à l'instant"
    if mins < 60:
        return f"il y a {mins} min"
    hours = mins // 60
    if hours < 48:
        return f"il y a {hours} h"
    days = hours // 24
    return f"il y a {days} j"


# ===========================================================================
# 1. Volatility complex (yfinance) — VVIX, SKEW, MOVE, realized vols
# ===========================================================================
@dataclass
class VolGauge:
    key: str
    label: str
    value: Optional[float]
    source: str
    asof: Optional[datetime] = None
    interpretation: str = ""


def _yf_last(symbol: str) -> tuple[Optional[float], Optional[datetime]]:
    if not _YF_OK:
        return None, None
    try:
        h = yf.Ticker(symbol).history(period="5d")
        s = h["Close"].dropna()
        if s.empty:
            return None, None
        val = float(s.iloc[-1])
        idx = s.index[-1]
        ts = idx.to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return val, ts.astimezone(timezone.utc)
    except Exception as exc:  # pragma: no cover - network
        logger.warning("yf_last %s failed: %s", symbol, exc)
        return None, None


def _realized_vol(symbol: str, window: int = 20) -> Optional[float]:
    """Annualized realized vol (%) from daily log returns of a yfinance series."""
    if not _YF_OK:
        return None
    try:
        h = yf.Ticker(symbol).history(period=f"{window + 15}d")
        s = h["Close"].dropna()
        if len(s) < window + 1:
            return None
        rets = [math.log(s.iloc[i] / s.iloc[i - 1]) for i in range(len(s) - window, len(s))]
        sd = statistics.pstdev(rets)
        return round(sd * math.sqrt(252) * 100, 1)
    except Exception as exc:  # pragma: no cover
        logger.warning("realized_vol %s failed: %s", symbol, exc)
        return None


def _interp_vix(v: float) -> str:
    if v < 13:
        return "Complacency — le marché actions price un très faible risque court terme."
    if v < 18:
        return "Volatilité comprimée — appétit pour le risque, stops plus serrés viables."
    if v < 26:
        return "Volatilité modérée — régime neutre, dimensionnement standard."
    return "Stress actions — réduire la taille, élargir les stops, privilégier les refuges."


def _interp_move(v: float) -> str:
    if v < 90:
        return "Vol obligataire faible — marché de taux calme, portage favorisé."
    if v < 120:
        return "Vol obligataire modérée — sensibilité aux surprises d'inflation/Fed."
    return ("Le marché obligataire price une hausse importante de volatilité "
            "— risque de contagion FX/actions.")


def _interp_skew(v: float) -> str:
    if v < 120:
        return "Demande de protection tail faible — peu de couverture contre un krach."
    if v < 140:
        return "Couverture tail normale — coût des puts OTM dans sa fourchette."
    return "Forte demande de protection tail — les mains fortes couvrent le risque extrême."


def _interp_vvix(v: float) -> str:
    if v < 90:
        return "Vol-of-vol basse — la volatilité elle-même est jugée stable."
    if v < 110:
        return "Vol-of-vol modérée — nervosité latente sur les options VIX."
    return "Vol-of-vol élevée — le marché price un choc de volatilité possible."


def fetch_vol_complex() -> list[VolGauge]:
    """Full volatility complex from free sources. Missing items are dropped
    (never faked)."""
    out: list[VolGauge] = []
    specs = [
        ("VIX", "VIX (vol actions)", "^VIX", _interp_vix),
        ("VVIX", "VVIX (vol-of-vol)", "^VVIX", _interp_vvix),
        ("SKEW", "CBOE SKEW (tail risk)", "^SKEW", _interp_skew),
        ("MOVE", "MOVE (vol obligataire)", "^MOVE", _interp_move),
    ]
    for key, label, sym, interp in specs:
        val, ts = _yf_last(sym)
        if val is not None:
            out.append(VolGauge(key, label, round(val, 2),
                             "yfinance/CBOE" if key != "MOVE" else "yfinance/ICE",
                                ts, interp(val)))
    # Realized vols (derived — labelled)
    rv10 = _realized_vol("^TNX", 20)
    if rv10 is not None:
        out.append(VolGauge("US10Y_RV", "US10Y Realized Vol 20j", rv10, "dérivé ^TNX",
                            datetime.now(timezone.utc),
                            "Volatilité réalisée des taux longs — "
                             "comparer au MOVE (vol implicite)."))
    # FX vol composite (self-branded derived metric — NOT Deutsche Bank CVIX)
    fx_rvs = [rv for rv in (_realized_vol(s, 20) for s in ("EURUSD=X", "USDJPY=X", "GBPUSD=X")) if rv is not None]
    if fx_rvs:
        comp = round(sum(fx_rvs) / len(fx_rvs), 1)
        out.append(VolGauge("FX_VOL", "FX Vol Composite (BLUESTAR)", comp, "dérivé G3 réalisé 20j",
                            datetime.now(timezone.utc),
                            "Proxy libre du régime de vol FX (moyenne EURUSD·USDJPY·GBPUSD). "
                            "Substitut méthodologique du CVIX propriétaire."))
    return out


# ===========================================================================
# 2. CFTC positioning statistics (Socrata legacy Non-commercial history)
# ===========================================================================
_CFTC_SOCRATA = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# Currency -> CFTC legacy market name (Non-commercial, CME)
CFTC_MARKETS = {
    "EUR": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBP": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
    "JPY": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
    "AUD": "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "CAD": "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "CHF": "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE",
    "NZD": "NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE",
}


@dataclass
class CotStat:
    currency: str
    net: int                       # latest net non-commercial (long-short)
    change_1w: Optional[int]
    change_4w: Optional[int]
    zscore: Optional[float]        # vs trailing history
    percentile: Optional[float]    # 0..100 within trailing history
    report_date: str
    extreme: bool = False
    interpretation: str = ""


def _cot_history(market: str, weeks: int = 156) -> list[tuple[str, int]]:
    """Return [(date, net_noncomm)] oldest->newest, best-effort."""
    r = _get(_CFTC_SOCRATA, params={
        "market_and_exchange_names": market,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(weeks),
        "$select": "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all",
    })
    if r is None:
        return []
    try:
        rows = r.json()
    except Exception:
        return []
    out = []
    for row in rows:
        try:
            lng = int(float(row["noncomm_positions_long_all"]))
            sht = int(float(row["noncomm_positions_short_all"]))
            date = row["report_date_as_yyyy_mm_dd"][:10]
            out.append((date, lng - sht))
        except (KeyError, TypeError, ValueError):
            continue
    out.reverse()  # oldest -> newest
    return out


def fetch_positioning_stats(currencies: Optional[list[str]] = None) -> dict[str, CotStat]:
    """Per-currency positioning stats (Δ1w, Δ4w, z-score, percentile, extreme)."""
    currencies = currencies or list(CFTC_MARKETS.keys())
    out: dict[str, CotStat] = {}
    for ccy in currencies:
        market = CFTC_MARKETS.get(ccy)
        if not market:
            continue
        hist = _cot_history(market)
        if not hist:
            continue
        dates = [d for d, _ in hist]
        nets = [n for _, n in hist]
        latest = nets[-1]
        ch1 = latest - nets[-2] if len(nets) >= 2 else None
        ch4 = latest - nets[-5] if len(nets) >= 5 else None
        z = pct = None
        if len(nets) >= 20:
            mu = statistics.mean(nets)
            sd = statistics.pstdev(nets)
            z = round((latest - mu) / sd, 2) if sd else None
            pct = round(100.0 * sum(1 for n in nets if n <= latest) / len(nets), 0)
        extreme = bool(z is not None and abs(z) >= 2.0) or bool(pct is not None and (pct >= 90 or pct <= 10))
        bias = "net long" if latest > 0 else "net short"
        if extreme:
            interp = (f"Positionnement {bias} en extrême historique "
                      f"(z={z}, {int(pct)}e pct) — risque de squeeze inverse élevé.")
        elif z is not None and abs(z) >= 1.0:
            interp = f"Positionnement {bias} tendu ({int(pct)}e pct) — surveiller un dégonflement."
        else:
            interp = f"Positionnement {bias} dans sa fourchette normale ({int(pct) if pct is not None else '·'}e pct)."
        out[ccy] = CotStat(ccy, latest, ch1, ch4, z, pct, dates[-1], extreme, interp)
    return out


# ===========================================================================
# 3. Homemade Macro Surprise Index (Forex Factory actual-vs-forecast)
# ===========================================================================
_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def _num(x) -> Optional[float]:
    if x is None:
        return None
    s = str(x).strip().replace("%", "").replace(",", "")
    mult = 1.0
    if s and s[-1] in "KkMmBb":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}[s[-1].lower()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


@dataclass
class SurpriseIndex:
    value: float                # normalized surprise score (z of standardized beats)
    n: int                      # number of released events used
    trend: str                  # ↑ / ↓ / →
    detail: list[tuple]         # (event, ccy, actual, forecast, surprise%)
    interpretation: str
    source: str = "BLUESTAR (Forex Factory actual-vs-forecast)"


def _ev_get(e, *names):
    """Read a field from either a dict or a MacroEvent-like object."""
    for n in names:
        if isinstance(e, dict):
            if n in e:
                return e[n]
        elif hasattr(e, n):
            return getattr(e, n)
    return None


def fetch_macro_surprise(currency: str = "USD", raw_events: Optional[list] = None) -> Optional[SurpriseIndex]:
    """Self-branded macro momentum index. Free substitute for the proprietary
    Citi Economic Surprise Index — clearly labelled, never a faked vendor number.

    Two honest modes (auto-selected, whichever data is present):
      * REALIZED surprise: mean of (actual-forecast)/|forecast| over released
        events — requires ``actual`` (present in the production calendar layer).
      * EXPECTATIONS drift: mean of (forecast-previous)/|previous| over upcoming
        events — used when no actuals are out yet, so the field is never blank.

    Pass ``raw_events`` = the engine's already-enriched events (dicts or
    ``MacroEvent``). Falls back to the bare weekly feed (forecast/previous only).
    """
    events = raw_events
    if events is None:
        r = _get(_FF_URL)
        if r is None:
            return None
        try:
            events = r.json()
        except Exception:
            return None

    realized, drift, detail_r, detail_d = [], [], [], []
    for e in events or []:
        ccy = _ev_get(e, "country", "currency")
        if ccy != currency:
            continue
        impact = str(_ev_get(e, "impact") or "").lower()
        # NOTE: "medium" is currently unreachable via the production call path --
        # calendar_layer.build_calendar() only keeps impact == "High" upstream,
        # so `events`/`events_engine` never contain medium-impact events. This
        # filter stays permissive on purpose (e.g. for callers that pass their
        # own raw_events with medium included), but do not assume it widens
        # coverage today without also relaxing the upstream calendar filter.
        if impact not in ("high", "medium"):
            continue
        title = _ev_get(e, "title", "event_name") or "?"
        act = _num(_ev_get(e, "actual"))
        fc = _num(_ev_get(e, "forecast"))
        prev = _num(_ev_get(e, "previous"))
        if act is not None and fc is not None and fc != 0:
            s = max(-1.0, min(1.0, (act - fc) / abs(fc)))
            realized.append(s)
            detail_r.append((title, ccy, _ev_get(e, "actual"), _ev_get(e, "forecast"), round(s * 100, 1)))
        elif fc is not None and prev is not None and prev != 0:
            s = max(-1.0, min(1.0, (fc - prev) / abs(prev)))
            drift.append(s)
            detail_d.append((title, ccy, _ev_get(e, "forecast"), _ev_get(e, "previous"), round(s * 100, 1)))

    if realized:
        mode, vals, detail = "realized", realized, detail_r
        src = "BLUESTAR US Macro Surprise (actual-vs-forecast, Forex Factory)"
    elif drift:
        mode, vals, detail = "drift", drift, detail_d
        src = "BLUESTAR Macro Momentum (consensus-vs-previous, Forex Factory)"
    else:
        return None

    score = round((sum(vals) / len(vals)) * 100, 1)
    trend = "↑" if score > 5 else ("↓" if score < -5 else "→")
    if mode == "realized":
        if score > 15:
            interp = f"Les publications {currency} dépassent nettement les attentes — impulsion pro-{currency}."
        elif score > 5:
            interp = f"Données {currency} au-dessus du consensus — biais {currency} constructif."
        elif score < -15:
            interp = f"Les publications {currency} déçoivent nettement — pression baissière {currency}."
        elif score < -5:
            interp = f"Données {currency} sous le consensus — biais {currency} prudent."
        else:
            interp = f"Données {currency} en ligne — pas de surprise directionnelle."
    else:
        if score > 8:
            interp = f"Consensus {currency} révisé au-dessus des dernières publications — attentes en amélioration."
        elif score < -8:
            interp = f"Consensus {currency} révisé sous les dernières publications — attentes en détérioration."
        else:
            interp = f"Attentes {currency} stables vs dernières publications."
    return SurpriseIndex(score, len(vals), trend, detail, interp, source=src)


# ===========================================================================
# 4. Multi-horizon correlations (from closes already in the snapshot)
# ===========================================================================
def _pct(closes: list[float]) -> list[float]:
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 8:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


@dataclass
class MultiCorr:
    asset: str
    benchmark: str
    r20: Optional[float]
    r60: Optional[float]
    r120: Optional[float]
    stability: str
    interpretation: str


def multi_horizon_corr(asset: str, benchmark: str,
                       a_closes: list[float], b_closes: list[float]) -> Optional[MultiCorr]:
    if not a_closes or not b_closes:
        return None
    ra, rb = _pct(a_closes), _pct(b_closes)

    def corr(win: int) -> Optional[float]:
        if len(ra) < win or len(rb) < win:
            return None
        r = _pearson(ra[-win:], rb[-win:])
        return round(r, 2) if r is not None else None

    r20, r60, r120 = corr(20), corr(60), corr(120)
    vals = [v for v in (r20, r60, r120) if v is not None]
    if not vals:
        return None
    spread = max(vals) - min(vals)
    stability = "stable" if spread <= 0.15 else ("modérément instable" if spread <= 0.30 else "instable")
    ref = r60 if r60 is not None else vals[0]
    strength = "forte" if abs(ref) >= 0.7 else ("modérée" if abs(ref) >= 0.4 else "faible")
    sign = "négative" if ref < 0 else "positive"
    interp = (f"Corrélation {sign} {strength} vs {benchmark}, régime {stability} "
              f"sur 20/60/120 séances.")
    return MultiCorr(asset, benchmark, r20, r60, r120, stability, interp)


# ===========================================================================
# 5. GDPNow (value + prior + delta + date) — Atlanta Fed, best-effort
# ===========================================================================
@dataclass
class GdpNow:
    value: Optional[float]
    previous: Optional[float]
    delta: Optional[float]
    quarter: str
    pub_date: str
    url: str = "https://www.atlantafed.org/cqer/research/gdpnow"
    source: str = "Atlanta Fed"
    interpretation: str = ""


def fetch_gdpnow_full() -> Optional[GdpNow]:
    """Latest GDPNow estimate + prior + delta from FRED series ``GDPNOW``.

    FRED mirrors the Atlanta Fed GDPNow nowcast as a machine-readable series and
    keeps the full revision history, so value / previous / delta / publication
    date all come from one official call using the existing FRED production key.
    The JS-rendered Atlanta Fed landing page is no longer scrapeable, so this is
    the robust institutional source. Degrades to ``None`` without a key.
    """
    key = _fred_key()
    if not key:
        return None
    r = _get("https://api.stlouisfed.org/fred/series/observations",
             params={"series_id": "GDPNOW", "api_key": key, "file_type": "json",
                     "sort_order": "desc", "limit": "2"})
    if r is None:
        return None
    try:
        obs = [o for o in r.json().get("observations", []) if o.get("value") not in (".", "", None)]
        if not obs:
            return None
        cur = round(float(obs[0]["value"]), 1)
        pub_date = obs[0]["date"]
        prev = delta = None
        if len(obs) > 1:
            prev = round(float(obs[1]["value"]), 1)
            delta = round(cur - prev, 1)
        # Fiscal quarter label from the observation date
        try:
            m = int(pub_date[5:7])
            q = (m - 1) // 3 + 1
            quarter = f"T{q} {pub_date[:4]}"
        except Exception:
            quarter = ""
        if delta is not None:
            interp = (f"GDPNow {fr_num(cur,1)}% — croissance US "
                      + ("révisée en hausse" if delta > 0 else "révisée en baisse" if delta < 0 else "stable")
                      + f" ({'+' if delta >= 0 else ''}{fr_num(delta,1)} pt vs estimation précédente).")
        else:
            interp = f"GDPNow {fr_num(cur,1)}% — nowcast de croissance US en temps réel."
        try:
            dd = datetime.strptime(pub_date, "%Y-%m-%d")
            pub_disp = dd.strftime("%d/%m")
        except Exception:
            pub_disp = pub_date
        return GdpNow(cur, prev, delta, quarter, pub_disp, interpretation=interp)
    except Exception as exc:  # pragma: no cover
        logger.warning("gdpnow FRED parse failed: %s", exc)
        return None


# ===========================================================================
# 6. Market Regime Dashboard building blocks (FRED via production key)
# ===========================================================================
def _fred_key() -> Optional[str]:
    if _ST_OK:
        try:
            k = st.secrets.get("FRED_API_KEY")  # type: ignore
            if k:
                return k
        except Exception as exc:
            logger.warning("Streamlit FRED key access failed: %s", exc)
    return os.environ.get("FRED_API_KEY")


def _fred_latest(series: str) -> Optional[tuple[float, str]]:
    key = _fred_key()
    if not key:
        return None
    r = _get("https://api.stlouisfed.org/fred/series/observations",
             params={"series_id": series, "api_key": key, "file_type": "json",
                     "sort_order": "desc", "limit": "1"})
    if r is None:
        return None
    try:
        obs = r.json()["observations"][0]
        if obs["value"] in (".", "", None):
            return None
        return float(obs["value"]), obs["date"]
    except Exception:
        return None


@dataclass
class RegimeMetric:
    label: str
    value: str
    source: str
    interpretation: str


def build_regime_dashboard() -> list[RegimeMetric]:
    """Free-source Market Regime blocks. Each item present only when sourced."""
    out: list[RegimeMetric] = []

    # Yield curve 2s10s (FRED)
    t10 = _fred_latest("DGS10")
    t2 = _fred_latest("DGS2")
    if t10 and t2:
        spread = round(t10[0] - t2[0], 2)
        interp = ("Courbe inversée — signal de fin de cycle / risque récession pricé."
                  if spread < 0 else
                  "Courbe pentue — anticipation de croissance/normalisation." if spread > 0.5 else
                  "Courbe plate — incertitude sur la trajectoire de croissance.")
        out.append(RegimeMetric("Courbe 2s10s", f"{'+' if spread>=0 else ''}{fr_num(spread,2)} pt",
                                f"FRED · {t10[1]}", interp))

    # Funding stress: SOFR - EFFR (positive = funding pressure)
    sofr = _fred_latest("SOFR")
    effr = _fred_latest("EFFR")
    if sofr and effr:
        basis = round((sofr[0] - effr[0]) * 100, 1)  # bp
        interp = ("Tension de financement USD — SOFR au-dessus de l'EFFR."
                  if basis > 5 else "Financement USD normal — pas de stress repo.")
        out.append(RegimeMetric("Stress financement (SOFR-EFFR)", f"{'+' if basis>=0 else ''}{fr_num(basis,1)} bp",
                                f"FRED · {sofr[1]}", interp))

    # Vol regime headline from the vol complex
    vc = {g.key: g for g in fetch_vol_complex()}
    if "VIX" in vc and "MOVE" in vc:
        vix, move = vc["VIX"].value, vc["MOVE"].value
        if vix < 18 and move < 100:
            reg = "RISK-ON (vol comprimée actions + taux)"
        elif vix > 24 or move > 120:
            reg = "RISK-OFF (stress vol)"
        else:
            reg = "NEUTRE / transition"
        out.append(RegimeMetric("Régime de volatilité", reg, "yfinance",
                                f"VIX {fr_num(vix,1)} · MOVE {fr_num(move,1)}"))
    return out
