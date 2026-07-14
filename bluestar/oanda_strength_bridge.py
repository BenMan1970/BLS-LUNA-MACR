"""bluestar/oanda_strength_bridge.py — Proven requests-based currency strength.

Thin adapter that reproduces the algorithm from the standalone ``strenghtmeter.py``
Streamlit app (verified working against Oanda practice) and emits the exact output
contract expected by ``macro_engine._oanda_strength_scores()``:

    Dict[str, float]  keys ∈ {USD,EUR,GBP,JPY,AUD,CAD,NZD,CHF}
                      values ∈ [0.0, 10.0], centered on 5.0 (neutral).

Design constraints (see integration prompt §3):
- No oandapyV20. Uses ``requests`` only (already a dependency).
- No caching (app.py owns TTL via @st.cache_data).
- No retry/backoff. Single 10s timeout per pair; fail fast, fail observable.
- Never invents a figure: a currency with zero usable data → 5.0 (neutral),
  NOT dropped and NOT invalidating the whole result. Fewer than MIN_PAIRS
  pairs overall → return None (documented degradation, caller falls back to
  CB-bias [PROXY]).
- Every failure branch logs at WARNING minimum.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — mirror strenghtmeter.py exactly.
# ---------------------------------------------------------------------------
_OANDA_BASE = "https://api-fxpractice.oanda.com/v3"
_TIMEOUT = 10.0            # seconds, per pair — no retry
_GRANULARITY = "D"        # D1 candles
_CANDLE_COUNT = 6         # need last 2 complete closes; 6 gives headroom
_MIN_PAIRS = 10           # below this, signal is meaningless → None

MAJORS: List[str] = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]

# Opponent weighting (proven algorithm): a move against a reserve/liquidity
# currency is more informative for relative-strength ranking.
_HEAVY_OPPONENTS = frozenset({"USD", "EUR", "JPY"})
_HEAVY_WEIGHT = 2.0
_LIGHT_WEIGHT = 1.0

# 28 major FX pairs (Oanda instrument naming).
_PAIRS: List[str] = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD",
    "EUR_GBP", "EUR_JPY", "EUR_CHF", "EUR_AUD", "EUR_CAD", "EUR_NZD",
    "GBP_JPY", "GBP_CHF", "GBP_AUD", "GBP_CAD", "GBP_NZD",
    "AUD_JPY", "AUD_CAD", "AUD_NZD", "AUD_CHF",
    "CAD_JPY", "CAD_CHF", "NZD_JPY", "NZD_CHF", "CHF_JPY",
]


# ---------------------------------------------------------------------------
# Single-pair fetch — % change of last two complete D1 closes.
# ---------------------------------------------------------------------------
def _fetch_pair_pct(
    instrument: str,
    access_token: str,
    *,
    session: Optional[requests.Session] = None,
    base_url: str = _OANDA_BASE,
) -> Optional[float]:
    """Return the 1-day % change for ``instrument``, or None on any failure.

    % change = (close_today - close_yesterday) / close_yesterday * 100
    Uses complete candles only. Logs WARNING with the pair name on every
    non-success outcome so failures are never silent.
    """
    url = f"{base_url}/instruments/{instrument}/candles"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"granularity": _GRANULARITY, "count": _CANDLE_COUNT, "price": "M"}
    getter = session.get if session is not None else requests.get
    try:
        r = getter(url, headers=headers, params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("strength_bridge: %s network error: %s", instrument, exc)
        return None

    if r.status_code != 200:
        logger.warning("strength_bridge: %s HTTP %s", instrument, r.status_code)
        return None

    try:
        candles = [c for c in r.json().get("candles", []) if c.get("complete")]
    except ValueError as exc:  # malformed JSON
        logger.warning("strength_bridge: %s bad JSON: %s", instrument, exc)
        return None

    if len(candles) < 2:
        logger.warning("strength_bridge: %s only %d complete candle(s)",
                       instrument, len(candles))
        return None

    try:
        prev_close = float(candles[-2]["mid"]["c"])
        last_close = float(candles[-1]["mid"]["c"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("strength_bridge: %s parse error: %s", instrument, exc)
        return None

    if prev_close == 0:
        logger.warning("strength_bridge: %s zero prev close", instrument)
        return None

    return (last_close - prev_close) / prev_close * 100.0


# ---------------------------------------------------------------------------
# Aggregation — weighted mean performance per currency, then 0–10 rescale.
# ---------------------------------------------------------------------------
def _aggregate(pct_by_pair: Dict[str, float]) -> Dict[str, float]:
    """Turn per-pair % changes into a 0–10 strength score per major.

    For each pair BASE_QUOTE with %chg p:
      BASE gains  +p  weighted by the *opponent* (QUOTE) weight.
      QUOTE gains -p  weighted by the *opponent* (BASE)  weight.
    Each currency's raw score = weighted mean of its contributions.

    AUDIT FIX (methodology change, not a cosmetic patch — flag for sign-off):
    scores were previously min-max rescaled to [0,10], which mathematically
    forces the day's weakest currency to exactly 0.00 and the strongest to
    exactly 10.00 regardless of how large the real dispersion is. On a day
    where the 8 majors' D1 returns are all within a fraction of a percent of
    each other, this manufactured a false "USD at rock bottom / NZD at the
    top" reading identical in shape to a genuine risk-divergence day. Now
    rescaled from a z-score (currency's raw value vs the cross-sectional
    mean/stdev of the 8 majors that day), centered on 5.0 (neutral) and
    clipped to [0,10]. A currency only approaches 0 or 10 when it is
    genuinely ~2 standard deviations from the day's average — an actual
    outlier — rather than every single day by construction.
    """
    totals: Dict[str, float] = {c: 0.0 for c in MAJORS}
    weights: Dict[str, float] = {c: 0.0 for c in MAJORS}

    for pair, pct in pct_by_pair.items():
        base, quote = pair.split("_")
        if base not in totals or quote not in totals:
            continue
        w_base = _HEAVY_WEIGHT if base in _HEAVY_OPPONENTS else _LIGHT_WEIGHT
        w_quote = _HEAVY_WEIGHT if quote in _HEAVY_OPPONENTS else _LIGHT_WEIGHT
        # BASE strengthens by +pct, opponent-weighted by QUOTE's weight.
        totals[base] += pct * w_quote
        weights[base] += w_quote
        # QUOTE strengthens by -pct, opponent-weighted by BASE's weight.
        totals[quote] += (-pct) * w_base
        weights[quote] += w_base

    raw: Dict[str, Optional[float]] = {}
    for c in MAJORS:
        raw[c] = (totals[c] / weights[c]) if weights[c] > 0 else None

    present = [v for v in raw.values() if v is not None]
    if not present:
        # Should not happen (caller guards on pair count), but stay safe.
        return {c: 5.0 for c in MAJORS}

    mean = sum(present) / len(present)
    variance = sum((v - mean) ** 2 for v in present) / len(present)
    stdev = variance ** 0.5

    # Z-units mapped to the [0,10] scale: +/-2 stdev (a genuinely wide day)
    # reaches the 0/10 bounds; smaller, more typical dispersion stays
    # clustered near 5.0 instead of being stretched to fill the full range.
    _Z_SPAN = 2.0

    scores: Dict[str, float] = {}
    for c in MAJORS:
        v = raw[c]
        if v is None:
            scores[c] = 5.0                       # no data → neutral, never dropped
        elif stdev <= 1e-9:
            scores[c] = 5.0                       # all equal → all neutral
        else:
            z = (v - mean) / stdev
            scaled = 5.0 + (z / _Z_SPAN) * 5.0
            scores[c] = round(min(10.0, max(0.0, scaled)), 2)
    return scores


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def compute_scores(
    access_token: str,
    *,
    session: Optional[requests.Session] = None,
    base_url: str = _OANDA_BASE,
) -> Optional[Dict[str, float]]:
    """Compute Oanda D1 relative-strength scores for the 8 majors.

    Returns a dict {currency: float in [0,10]} on success, or None when fewer
    than ``_MIN_PAIRS`` pairs could be fetched (documented degradation — the
    caller falls back to CB-bias [PROXY]). Never raises for per-pair errors;
    those are logged and skipped.
    """
    if not access_token:
        logger.warning("strength_bridge: no access token — skipping Oanda strength")
        return None

    owns_session = session is None
    sess = session or requests.Session()
    pct_by_pair: Dict[str, float] = {}
    try:
        for pair in _PAIRS:
            pct = _fetch_pair_pct(pair, access_token, session=sess, base_url=base_url)
            if pct is not None:
                pct_by_pair[pair] = pct
    finally:
        if owns_session:
            sess.close()

    n = len(pct_by_pair)
    if n < _MIN_PAIRS:
        logger.warning(
            "strength_bridge: only %d/%d pairs fetched (< %d) — falling back to CB-bias",
            n, len(_PAIRS), _MIN_PAIRS,
        )
        return None

    scores = _aggregate(pct_by_pair)
    if n < len(_PAIRS):
        logger.info("strength_bridge: partial success %d/%d pairs — scores computed",
                    n, len(_PAIRS))
    else:
        logger.info("strength_bridge: full success %d/%d pairs", n, len(_PAIRS))
    return scores
