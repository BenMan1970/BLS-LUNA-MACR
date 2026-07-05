"""Oanda v20 REST Data Layer — BLUESTAR engine.

Primary source for FX pairs and XAU/USD when an Oanda practice API key is
available via ``st.secrets["OANDA_API_KEY"]`` and
``st.secrets["OANDA_ACCOUNT_ID"]``.  Falls back to yfinance transparently for
any instrument that Oanda cannot serve (indices, Brent, WTI, VIX, MOVE, DXY,
US10Y) or when the key is absent / the request fails.

Design constraints (zero regression):
- Public signature of ``build_market_snapshot`` is unchanged.
- ``MarketSnapshot``, ``Datum``, ``SourceStamp`` contracts are unchanged.
- ATR calculation is the same SMA-14 of True Ranges used in ``market_data.py``
  (original Welles Wilder 1978 formula) — D1 candles, 30 bars, identical
  formula.  Note: this is a plain simple average of the last 14 TRs, NOT the
  EMA-recursive smoothing that MetaTrader / TradingView label "Wilder ATR";
  values will differ by ~5–15 % from those platforms in high-vol regimes.
- ``Reliability.PRIMARY`` stamped when Oanda responds; ``Reliability.FALLBACK``
  when yfinance is used instead.  The renderer and validation engine already
  handle both levels correctly.
- ``# type: ignore`` on the optional ``streamlit`` import: Streamlit is not
  available in test/offline contexts; we degrade gracefully.
"""
from __future__ import annotations

import concurrent.futures
import logging
import math
import os
import time
from datetime import datetime
from typing import Optional

import pytz
import requests

from .config import YF_TICKERS, MARKET_FETCH_MAX_WORKERS
from .models import Datum, Reliability, SourceStamp, MarketSnapshot, na_stamp
from .external_sources import fetch_gdp_nowcast

# Institutional Intelligence layer (best-effort; zero-regression if absent).
try:
    from . import institutional as _inst  # type: ignore
except Exception:  # pragma: no cover
    _inst = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional runtime dependencies
# ---------------------------------------------------------------------------
try:
    import yfinance as yf  # type: ignore
    _YF_OK = True
except Exception:  # pragma: no cover
    _YF_OK = False
    logger.warning("yfinance unavailable — Oanda-only mode, indices/commodities may be [N/A]")

try:
    import streamlit as st  # type: ignore
    _ST_OK = True
except Exception:  # pragma: no cover
    _ST_OK = False

# ---------------------------------------------------------------------------
# Currency strength bridge (requests-based, no oandapyV20)
# BLUESTAR-PATCH v10.0
# ---------------------------------------------------------------------------
try:
    from .oanda_strength_bridge import compute_scores as _bridge_compute_scores
    _STRENGTH_OK = True
except Exception as _imp_exc:  # pragma: no cover
    _STRENGTH_OK = False
    _bridge_compute_scores = None  # type: ignore
    logger.warning(
        "oanda_strength_bridge import failed — currency strength will be [PROXY]: %s",
        _imp_exc,
    )

# ---------------------------------------------------------------------------
# Oanda instrument mapping
# BLUESTAR key  ->  Oanda v20 instrument name
# Only FX pairs and XAU/USD are served by Oanda practice reliably.
# ---------------------------------------------------------------------------
_OANDA_INSTRUMENTS: dict[str, str] = {
    "EUR/USD": "EUR_USD",
    "GBP/USD": "GBP_USD",
    "USD/JPY": "USD_JPY",
    "USD/CHF": "USD_CHF",
    "AUD/USD": "AUD_USD",
    "NZD/USD": "NZD_USD",
    "USD/CAD": "USD_CAD",
    "EUR/GBP": "EUR_GBP",
    "GBP/JPY": "GBP_JPY",
    "XAU/USD": "XAU_USD",
}

# Instruments NOT served by Oanda — always routed to yfinance fallback.
_YF_ONLY: frozenset[str] = frozenset([
    "VIX", "MOVE", "DXY", "US10Y",
    "Brent", "WTI",
    "DAX", "US30", "NAS100", "SPX500",
])

# Oanda practice REST base URL.
_OANDA_BASE = "https://api-fxpractice.oanda.com/v3"

# Candle granularity and count for ATR 14j (need ≥ 15 closes for 14 TRs).
_GRANULARITY = "D"   # daily candles — identical time-frame to yfinance 1mo/1d
_CANDLE_COUNT = 30   # 30 bars gives a stable ATR-14 with room to spare

# HTTP config (reuse values from config.py philosophy).
_TIMEOUT = 10        # seconds — tighter than generic HTTP_TIMEOUT
_RETRIES = 2
_BACKOFF = 1.5


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------
def _oanda_creds() -> tuple[Optional[str], Optional[str]]:
    """Return (api_key, account_id) from st.secrets, then os.environ, else (None, None)."""
    key = acc = None
    if _ST_OK:
        try:
            key = st.secrets.get("OANDA_API_KEY") or st.secrets.get("oanda_api_key")
            acc = st.secrets.get("OANDA_ACCOUNT_ID") or st.secrets.get("oanda_account_id")
        except Exception:  # pragma: no cover
            key = acc = None
    key = key or os.environ.get("OANDA_API_KEY") or os.environ.get("oanda_api_key")
    acc = acc or os.environ.get("OANDA_ACCOUNT_ID") or os.environ.get("oanda_account_id")
    return (str(key) if key else None, str(acc) if acc else None)


def _strength_access_token() -> Optional[str]:
    """Resolve the token for the strength bridge, trying all known secret names.

    _oanda_creds() only looks at OANDA_API_KEY / oanda_api_key. The standalone
    strength app uses OANDA_ACCESS_TOKEN, and that mismatch was the reason the
    strength block never fired. Try every documented spelling here.
    """
    if not _ST_OK:
        return None
    try:
        for name in ("OANDA_API_KEY", "OANDA_ACCESS_TOKEN",
                     "oanda_api_key", "oanda_access_token"):
            val = st.secrets.get(name)
            if val:
                return str(val)
    except Exception as exc:  # pragma: no cover
        logger.warning("strength token resolution failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Formatting helpers (identical to market_data.py — do not diverge)
# ---------------------------------------------------------------------------
def fr_num(value: float, decimals: int = 2, thousands: bool = False) -> str:
    """Format a float the French way: ``1234.5`` -> ``1 234,50``."""
    s = f"{value:,.{decimals}f}"
    s = s.replace(",", "\u00a0").replace(".", ",")
    if not thousands:
        s = s.replace("\u00a0", "")
    return s


def _trend_str(last: float, prev: float, pct_decimals: int = 1) -> str:
    if prev == 0:
        return ""
    chg = (last - prev) / prev * 100
    arrow = "↑" if chg > 0.05 else "↓" if chg < -0.05 else "→"
    return f"{arrow} {fr_num(abs(chg), pct_decimals)}%"


# ---------------------------------------------------------------------------
# ATR — SMA-14 of True Ranges (original Welles Wilder 1978 formula)
# Implementation: plain simple-average of the last 14 TR values.
# This is intentionally NOT the EMA-recursive version that most modern
# platforms (MetaTrader, TradingView) label "Wilder ATR".  The difference
# is documented here so risk / QA audits can reconcile values correctly.
# Single source of truth: this file.  market_data.py is a legacy stub.
# ---------------------------------------------------------------------------
def _atr(highs: list[float], lows: list[float],
         closes: list[float], period: int = 14) -> Optional[float]:
    """Classic ATR. Returns None if fewer than period+1 bars available."""
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    window = trs[-period:]
    return sum(window) / len(window) if window else None


# ---------------------------------------------------------------------------
# Display formatting — mirrors market_data._fetch_instrument logic exactly
# ---------------------------------------------------------------------------
def _display(key: str, value: float) -> str:
    if key in ("VIX", "MOVE", "US10Y", "DXY", "Brent", "WTI",
               "USD/JPY", "GBP/JPY"):
        return fr_num(value, 2)
    if key in ("XAU/USD", "DAX", "US30", "NAS100", "SPX500"):
        return fr_num(value, 0, thousands=True)
    return fr_num(value, 4)


# ---------------------------------------------------------------------------
# Oanda REST fetch
# ---------------------------------------------------------------------------
def _oanda_candles(
    instrument: str,
    api_key: str,
    granularity: str = _GRANULARITY,
    count: int = _CANDLE_COUNT,
) -> Optional[list[dict]]:
    """Fetch OHLC candles from Oanda v20.  Returns list of candle dicts or None."""
    url = f"{_OANDA_BASE}/instruments/{instrument}/candles"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept-Datetime-Format": "RFC3339",
        "Content-Type": "application/json",
    }
    params = {
        "granularity": granularity,
        "count": count,
        "price": "M",   # mid prices — appropriate for macro analysis
    }
    last_err: Optional[Exception] = None
    for attempt in range(_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, params=params,
                             timeout=_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            candles = [c for c in data.get("candles", []) if c.get("complete")]
            # Include the last incomplete candle as the current price bar.
            incomplete = [c for c in data.get("candles", [])
                          if not c.get("complete")]
            if incomplete:
                candles.append(incomplete[-1])
            return candles if len(candles) >= 2 else None
        except requests.RequestException as e:
            last_err = e
            if attempt < _RETRIES:
                time.sleep(_BACKOFF ** (attempt + 1))
    logger.warning("Oanda candles failed for %s: %s", instrument, last_err)
    return None


def _oanda_price(instrument: str, api_key: str) -> Optional[float]:
    """Fetch latest mid price from Oanda pricing endpoint (real-time, S5 bar).

    .. note::
        RESERVED — not yet wired into ``_fetch_oanda()``.

        Intended for intra-session price refresh: fetch only the current bar's
        mid price without pulling the full 30-bar D1 history (cost: one HTTP
        call vs three for a full candle fetch).  Candidate use-case: a
        ``is_live_session=True`` path in ``build_market_snapshot()`` that
        refreshes the spot price more frequently than the D1 ATR window.

        Do not remove without updating this docstring and the architecture
        note in the README.  Do not call from ``_fetch_oanda()`` without also
        deciding how to reconcile a real-time ``last`` price with the D1 OHLC
        series used for ATR (the two time-frames must not be mixed silently).
    """
    url = f"{_OANDA_BASE}/instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"granularity": "S5", "count": 1, "price": "M"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        candles = r.json().get("candles", [])
        if candles:
            mid = candles[-1].get("mid", {})
            return float(mid.get("c", 0)) or None
    except Exception as e:  # pragma: no cover
        logger.debug("Oanda real-time price failed for %s: %s", instrument, e)
    return None


# ---------------------------------------------------------------------------
# Instrument fetch — Oanda primary path
# ---------------------------------------------------------------------------
def _fetch_oanda(
    key: str,
    instrument: str,
    api_key: str,
    now_utc: datetime,
) -> tuple[Datum, Optional[float], list[float]]:
    """Fetch D1 OHLC from Oanda, compute ATR-14, return (Datum, atr, closes)."""
    candles = _oanda_candles(instrument, api_key)
    if not candles:
        return Datum(None, na_stamp("oanda unavailable"), "N/A"), None, []

    try:
        closes = [float(c["mid"]["c"]) for c in candles]
        highs  = [float(c["mid"]["h"]) for c in candles]
        lows   = [float(c["mid"]["l"]) for c in candles]
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Oanda candle parse error for %s: %s", instrument, e)
        return Datum(None, na_stamp("oanda parse error"), "N/A"), None, []

    last, prev = closes[-1], closes[-2]
    atr = _atr(highs, lows, closes)

    ts = now_utc.astimezone(pytz.UTC)
    stamp = SourceStamp(
        "Oanda v20", Reliability.PRIMARY, timestamp=ts,
        url=f"https://www.oanda.com/rw-en/trading/instrument-data/{instrument}/",
    )
    disp = _display(key, last)
    return Datum(last, stamp, disp, _trend_str(last, prev)), atr, closes


# ---------------------------------------------------------------------------
# Instrument fetch — yfinance fallback path
# Mirrors market_data._fetch_instrument exactly.
# ---------------------------------------------------------------------------
def _fetch_yf_fallback(
    key: str,
    now_utc: datetime,
) -> tuple[Datum, Optional[float], list[float]]:
    """yfinance fallback — stamped FALLBACK, not PRIMARY."""
    if not _YF_OK:
        return Datum(None, na_stamp("yfinance unavailable"), "N/A"), None, []

    ticker = YF_TICKERS.get(key)
    if ticker is None:
        return Datum(None, na_stamp("no ticker mapping"), "N/A"), None, []

    try:
        df = yf.Ticker(ticker).history(period="1mo", interval="1d",
                                       auto_adjust=False)
        if df is None or df.empty or len(df) < 2:
            return Datum(None, na_stamp("yfinance empty"), "N/A"), None, []
    except Exception as e:  # pragma: no cover
        logger.warning("yfinance fallback failed for %s: %s", key, e)
        return Datum(None, na_stamp("yfinance error"), "N/A"), None, []

    closes = [float(x) for x in df["Close"].tolist()]
    highs  = [float(x) for x in df["High"].tolist()]
    lows   = [float(x) for x in df["Low"].tolist()]

    # yfinance occasionally returns NaN OHLC for the most recent row (e.g. an
    # in-progress session, a stale/half-published bar around a holiday).
    # A NaN is not None, so it would otherwise sail past Datum.available and
    # print as a literal "nan" in the briefing -- silently breaking the
    # [N/A]/[PROXY]-or-real-value contract. Drop any row with a NaN in it.
    clean = [(c, h, lo) for c, h, lo in zip(closes, highs, lows)
             if not (math.isnan(c) or math.isnan(h) or math.isnan(lo))]
    if len(clean) < 2:
        return Datum(None, na_stamp("yfinance NaN OHLC"), "N/A"), None, []
    closes, highs, lows = (list(t) for t in zip(*clean))
    last, prev = closes[-1], closes[-2]

    # ^TNX x10 normalisation (unchanged from market_data.py).
    if key == "US10Y" and last > 20:
        last = last / 10.0
        prev = prev / 10.0
        closes = [c / 10.0 for c in closes]
        highs  = [h / 10.0 for h in highs]
        lows   = [lw / 10.0 for lw in lows]

    atr = _atr(highs, lows, closes)
    ts = now_utc.astimezone(pytz.UTC)
    stamp = SourceStamp(
        "yfinance", Reliability.FALLBACK, timestamp=ts,
        url=f"https://finance.yahoo.com/quote/{ticker}",
    )
    disp = _display(key, last)
    return Datum(last, stamp, disp, _trend_str(last, prev)), atr, closes


# ---------------------------------------------------------------------------
# Unified instrument fetch — routing logic
# ---------------------------------------------------------------------------
def _fetch_instrument(
    key: str,
    now_utc: datetime,
    api_key: Optional[str],
) -> tuple[Datum, Optional[float], list[float]]:
    """Route to Oanda or yfinance based on key and key availability."""
    oanda_instrument = _OANDA_INSTRUMENTS.get(key)

    # Instruments Oanda cannot serve: always yfinance.
    if key in _YF_ONLY or oanda_instrument is None:
        return _fetch_yf_fallback(key, now_utc)

    # Oanda capable instrument but no API key: yfinance fallback.
    if not api_key:
        return _fetch_yf_fallback(key, now_utc)

    # Primary: Oanda D1 candles.
    datum, atr, closes = _fetch_oanda(key, oanda_instrument, api_key, now_utc)
    if datum.available:
        return datum, atr, closes

    # Oanda failed: transparent fallback to yfinance, logged as WARN.
    logger.warning("Oanda fetch failed for %s — falling back to yfinance", key)
    return _fetch_yf_fallback(key, now_utc)


# ---------------------------------------------------------------------------
# Gauge keys (unchanged from market_data.py)
# ---------------------------------------------------------------------------
_GAUGE_KEYS = ["VIX", "MOVE", "DXY", "US10Y", "XAU/USD", "Brent", "WTI"]


# ---------------------------------------------------------------------------
# Public entry point — drop-in replacement for market_data.build_market_snapshot
# ---------------------------------------------------------------------------
def build_market_snapshot(
    now_utc: Optional[datetime] = None,
    overrides: Optional[dict] = None,
    allow_proxy_levels: bool = True,
) -> MarketSnapshot:
    """Assemble a :class:`MarketSnapshot` — Oanda primary, yfinance fallback.

    Signature is identical to ``market_data.build_market_snapshot`` so
    ``app.py`` and ``pipeline.py`` require zero changes beyond swapping the
    import.  ``overrides`` (manual sidebar JSON) always takes precedence and
    is stamped ``[PROXY]``.
    """
    now_utc = now_utc or datetime.now(pytz.UTC)
    overrides = overrides or {}
    snap = MarketSnapshot(as_of_utc=now_utc)

    api_key, _account_id = _oanda_creds()
    if api_key:
        logger.info("Oanda API key found — FX + XAU/USD sourced via Oanda v20 D1")
    else:
        logger.info("No Oanda API key — full yfinance mode")

    from .config import UNIVERSE  # local import to avoid cycle at module load

    keys = set(_GAUGE_KEYS) | set(UNIVERSE)

    # A6 (perf): each _fetch_instrument call is an independent blocking
    # network request (Oanda v20 REST or yfinance) with no shared state
    # between instruments (verified: no module-level cache/session) --
    # fetched concurrently instead of one-by-one. Capped at
    # MARKET_FETCH_MAX_WORKERS to bound load on the upstream APIs.
    results: dict[str, tuple] = {}
    max_workers = min(len(keys), MARKET_FETCH_MAX_WORKERS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_key = {ex.submit(_fetch_instrument, key, now_utc, api_key): key
                         for key in keys}
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            results[key] = future.result()

    # Everything below is unchanged from the sequential version: same
    # per-key branching, same override precedence, same assignment order
    # semantics (dict keys, so iteration order is immaterial to the result).
    for key in keys:
        datum, atr, closes = results[key]

        # Manual override always wins — stamped PROXY.
        if key in overrides:
            val = overrides[key]
            try:
                fval = float(val)
                datum = Datum(
                    fval,
                    SourceStamp("manual override", Reliability.PROXY,
                                note="saisie utilisateur"),
                    str(val).replace(".", ","), "",
                )
            except (TypeError, ValueError):
                pass

        if key in _GAUGE_KEYS:
            snap.gauges[key] = datum
        if key in UNIVERSE:
            snap.prices[key] = datum
        if atr is not None:
            snap.atr[key] = atr
        if closes:
            # Kept even when the current point is a manual override: the
            # historical series itself is real and still useful for the
            # [PROXY] short-window correlation overlay.
            snap.closes[key] = closes

    # GDP Nowcast precedence (updated): LIVE FRED GDPNOW (official, machine-
    # readable) > manual override > dead Atlanta scrape > [N/A].
    # Rationale: the manual override was only ever a crutch for the now-broken
    # Atlanta Fed page scrape. With FRED GDPNOW live, the official series is the
    # authoritative source; the override is demoted to a fallback so a stale
    # hand-typed value no longer masks the live nowcast. To restore the old
    # "override always wins" behaviour, swap the first two branches back.
    gdp = _inst.fetch_gdpnow_full() if _inst else None
    if gdp is not None and gdp.value is not None:
        sub = f"FRED · GDPNOW · publié {gdp.pub_date}"
        if gdp.quarter:
            sub = f"FRED · GDPNOW · {gdp.quarter} · publié {gdp.pub_date}"
        if gdp.delta is not None:
            sub += (f" · {'+' if gdp.delta >= 0 else '−'}"
                    f"{fr_num(abs(gdp.delta), 1)} pt vs préc.")
        snap.gauges["GDP_NOWCAST"] = Datum(
            gdp.value,
            SourceStamp("FRED · GDPNOW", Reliability.PRIMARY, timestamp=now_utc,
                        url="https://fred.stlouisfed.org/series/GDPNOW"),
            f"{fr_num(gdp.value, 1)} %", sub,
        )
    elif "GDP_NOWCAST" in overrides:
        snap.gauges["GDP_NOWCAST"] = Datum(
            None,
            SourceStamp("manual override", Reliability.PROXY),
            str(overrides["GDP_NOWCAST"]), "",
        )
    else:
        gdp_val = fetch_gdp_nowcast()
        if gdp_val is not None:
            snap.gauges["GDP_NOWCAST"] = Datum(
                gdp_val,
                SourceStamp("Atlanta Fed GDPNow", Reliability.PRIMARY,
                            timestamp=now_utc,
                            url="https://www.atlantafed.org/cqer/research/gdpnow"),
                f"{fr_num(gdp_val, 1)} %", "Atlanta Fed GDPNow",
            )
        else:
            snap.gauges["GDP_NOWCAST"] = Datum(
                None, na_stamp("source indisponible"), "N/A")

    # Surprise Index: no keyless source — [N/A] unless overridden (unchanged).
    if "SURPRISE_IDX" in overrides:
        snap.gauges["SURPRISE_IDX"] = Datum(
            None,
            SourceStamp("manual override", Reliability.PROXY),
            str(overrides["SURPRISE_IDX"]), "",
        )
    else:
        snap.gauges["SURPRISE_IDX"] = Datum(
            None, na_stamp("source sans cle API"), "N/A")

    # BLUESTAR-PATCH v10.0: attach Oanda price-derived currency strength via the
    # requests-based bridge. macro_engine reads this attribute via getattr;
    # absent/None → documented CB-bias [PROXY] fallback.
    # models.py is untouched (optional attribute set via assignment).
    # Token resolution is broadened here because _oanda_creds() (→ api_key)
    # only checks OANDA_API_KEY, but the strength source may be under
    # OANDA_ACCESS_TOKEN. Every outcome is logged.
    if not _STRENGTH_OK:
        logger.warning("Oanda strength bridge unavailable at import — CB-bias [PROXY]")
    else:
        _strength_token = _strength_access_token() or api_key
        if not _strength_token:
            logger.warning("No Oanda token for strength bridge — CB-bias [PROXY]")
        else:
            try:
                _scores = _bridge_compute_scores(_strength_token)
            except Exception as _exc:  # defensive: bridge should not raise
                logger.warning("Oanda strength bridge raised — CB-bias [PROXY]: %s", _exc)
                _scores = None
            if _scores:
                snap.currency_strength_oanda = _scores
                logger.info("Oanda strength attached (%d majors) — PRIMARY", len(_scores))
            else:
                logger.warning("Oanda strength returned None — CB-bias [PROXY]")

    return snap
