"""Central configuration and universe constants for the BLUESTAR engine.

Everything that is a tunable knob or a fixed reference list lives here so the
rest of the codebase stays free of magic numbers. No secrets are stored here;
the engine runs with zero API keys (degrading to [N/A]/[PROXY] when an external
source is unavailable).
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Timezones
# ----------------------------------------------------------------------------
TZ_UTC = ZoneInfo("UTC")
TZ_CET = ZoneInfo("Europe/Paris")  # CET/CEST display timezone
TZ_ET  = ZoneInfo("America/New_York")  # CFTC publication timezone (Eastern)

# ----------------------------------------------------------------------------
# Calendar Layer (validated Forex Factory module)
# ----------------------------------------------------------------------------
FF_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_CACHE_TTL = 300           # seconds (matches the validated module)
RESIDUAL_RISK_WINDOW_H = 72        # past events kept in events_engine

# ----------------------------------------------------------------------------
# Market Data Layer
# ----------------------------------------------------------------------------
MARKET_CACHE_TTL = 300             # seconds
HTTP_TIMEOUT = 15                  # seconds
HTTP_RETRIES = 2
HTTP_BACKOFF = 1.5                 # seconds, exponential base
MARKET_FETCH_MAX_WORKERS = 8       # cap on concurrent instrument fetches
                                    # (Oanda v20 / yfinance) — bounds load on
                                    # the upstream APIs while still giving a
                                    # large wall-clock win over sequential.

# yfinance tickers used as a *no-key* primary/fallback market source.
# When yfinance (or the network) is unavailable the field becomes [N/A].
YF_TICKERS = {
    "VIX": "^VIX",
    "MOVE": "^MOVE",          # frequently unavailable -> [PROXY]/[N/A]
    "DXY": "DX-Y.NYB",
    "US10Y": "^TNX",          # quoted x10 (e.g. 43.8 => 4.38%)
    "XAU/USD": "GC=F",        # gold futures front month as spot proxy
    "Brent": "BZ=F",
    "WTI": "CL=F",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "USD/CHF": "CHF=X",
    "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
    "USD/CAD": "CAD=X",
    "EUR/GBP": "EURGBP=X",
    "GBP/JPY": "GBPJPY=X",
    "DAX": "^GDAXI",
    "US30": "^DJI",
    "NAS100": "^NDX",
    "SPX500": "^GSPC",
}

# ----------------------------------------------------------------------------
# Universe (BLUESTAR mandate) -- NO crypto.
# ----------------------------------------------------------------------------
FX_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "GBP/JPY",
]
INDICES = ["DAX", "US30", "NAS100", "SPX500"]
COMMODITIES = ["XAU/USD", "Brent", "WTI"]
UNIVERSE = FX_PAIRS + INDICES + COMMODITIES

MAJOR_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]

# Currencies that make up each instrument (used for positioning links).
INSTRUMENT_CCYS = {
    "EUR/USD": ("EUR", "USD"), "GBP/USD": ("GBP", "USD"),
    "USD/JPY": ("USD", "JPY"), "USD/CHF": ("USD", "CHF"),
    "AUD/USD": ("AUD", "USD"), "NZD/USD": ("NZD", "USD"),
    "USD/CAD": ("USD", "CAD"), "EUR/GBP": ("EUR", "GBP"),
    "GBP/JPY": ("GBP", "JPY"),
}

# Safe-haven currencies (used by the qualitative currency-strength overlay).
SAFE_HAVENS = ["USD", "JPY", "CHF"]

# ----------------------------------------------------------------------------
# Regime thresholds (VIX based, combined with other gauges in the engine)
# ----------------------------------------------------------------------------
VIX_RISK_ON_MAX = 15.0     # VIX below -> calm / risk-on tilt
VIX_RISK_OFF_MIN = 22.0    # VIX above -> stress / risk-off tilt

# MOVE (ICE BofA bond-vol index) thresholds used alongside VIX in the
# multi-factor regime engine (regime_engine.py). Values unchanged from the
# figures that were previously hardcoded inline there (90 / 120) — centralised
# here only so both the classification and the trigger-explainer text share a
# single source of truth instead of independently hardcoded numbers.
MOVE_RISK_ON_MAX = 90.0    # MOVE below -> calm bond-vol / risk-on tilt
MOVE_RISK_OFF_MIN = 120.0  # MOVE above -> stressed bond-vol / risk-off tilt

# ----------------------------------------------------------------------------
# Positioning / IPS heuristic (Non-Commercials only) -- always [PROXY]
# ----------------------------------------------------------------------------
IPS_CROWDED = 80           # >80  => crowded (squeeze risk inverse)
IPS_CAPITULATION = 20      # <20  => capitulation / crowded short
# Net contracts magnitude that maps to a "full" extreme reading (heuristic
# scaling because we do not have the complete CFTC history to compute a true
# percentile). Documented and surfaced as [PROXY] in the HTML.
IPS_FULL_SCALE_CONTRACTS = 150_000

# ----------------------------------------------------------------------------
# Sizing factor (NOT a real Kelly)
# ----------------------------------------------------------------------------
SIZING_VIX_DENOM = 30.0    # Sizing = conviction * 1/(1 + VIX/30)
SIZING_PROXY_VIX = 20.0    # assumed VIX when VIX is unavailable (+[PROXY])

# Expected-move ATR multipliers when only [PROXY] data is available.
PROXY_ATR_PCT = 0.006      # ~0.6% of price as a daily move proxy
LEVEL_ATR_MULT = 1.0       # buy/sell zone distance in ATR units
STOP_ATR_MULT = 1.8        # stop distance in ATR units

# ----------------------------------------------------------------------------
# Operating modes (affect selection thresholds / max conviction)
# ----------------------------------------------------------------------------
MODES = ("Conservative", "Normal", "Aggressive")
MODE_SELECTION_MIN_SCORE = {
    "Conservative": 0.62,
    "Normal": 0.50,
    "Aggressive": 0.40,
}
MAX_PRIORITY_ASSETS = 3

# ----------------------------------------------------------------------------
# Staleness & coverage thresholds (v9.0 — audit C2/C3/C5 fix)
# ----------------------------------------------------------------------------
MIN_LIVE_COVERAGE_RATIO = 0.30   # minimum fraction of live fields to publish
