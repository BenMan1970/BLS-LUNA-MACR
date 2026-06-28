"""Market Data Layer.

Provides market gauges and prices with **no mandatory API key**. The primary
(and only built-in) source is yfinance, which works without credentials. Any
field that cannot be fetched degrades to ``[N/A]`` (or ``[PROXY]`` when the user
supplies a documented manual override). Every value carries a
:class:`SourceStamp`.

The architecture is deliberately extensible: ``_FETCHERS`` maps a logical key to
a callable, so additional licensed providers can be slotted in later without
touching the engine.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz

from .config import YF_TICKERS
from .models import Datum, Reliability, SourceStamp, MarketSnapshot, na_stamp

logger = logging.getLogger(__name__)

try:  # yfinance is optional at runtime; absence simply yields [N/A] fields.
    import yfinance as yf  # type: ignore
    _YF_OK = True
except Exception:  # pragma: no cover - environment dependent
    _YF_OK = False
    logger.warning("yfinance unavailable -- market fields will be [N/A] unless overridden")


# ---------------------------------------------------------------------------
# Formatting helpers (French number style: comma decimal, thin separators)
# ---------------------------------------------------------------------------
def fr_num(value: float, decimals: int = 2, thousands: bool = False) -> str:
    """Format a float the French way: ``1234.5`` -> ``1 234,50``."""
    s = f"{value:,.{decimals}f}"  # 1,234.50
    s = s.replace(",", "\u00a0").replace(".", ",")  # -> 1 234,50
    if not thousands:
        s = s.replace("\u00a0", "")
    return s


def _trend_str(last: float, prev: float, pct_decimals: int = 1) -> str:
    """Arrow + percentage move vs previous close."""
    if prev == 0:
        return ""
    chg = (last - prev) / prev * 100
    arrow = "↑" if chg > 0.05 else "↓" if chg < -0.05 else "→"
    return f"{arrow} {fr_num(abs(chg), pct_decimals)}%"


# ---------------------------------------------------------------------------
# yfinance fetch with ATR
# ---------------------------------------------------------------------------
def _atr(highs, lows, closes, period: int = 14) -> Optional[float]:
    """Classic ATR from OHLC arrays. Returns ``None`` if not enough data."""
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


def _yf_history(ticker: str):
    """Return a yfinance history DataFrame (period 1mo) or None."""
    if not _YF_OK:
        return None
    try:
        df = yf.Ticker(ticker).history(period="1mo", interval="1d", auto_adjust=False)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:  # pragma: no cover - network dependent
        logger.warning("yfinance history failed for %s: %s", ticker, e)
        return None


def _fetch_instrument(key: str, now_utc: datetime) -> tuple[Datum, Optional[float]]:
    """Fetch last price + trend + ATR for one logical instrument."""
    ticker = YF_TICKERS.get(key)
    if ticker is None:
        return Datum(None, na_stamp("no ticker mapping"), "N/A"), None
    df = _yf_history(ticker)
    if df is None or len(df) < 2:
        return Datum(None, na_stamp("yfinance unavailable"), "N/A"), None

    closes = [float(x) for x in df["Close"].tolist()]
    highs = [float(x) for x in df["High"].tolist()]
    lows = [float(x) for x in df["Low"].tolist()]
    last, prev = closes[-1], closes[-2]

    # ^TNX historically quoted x10 -- normalise to a percentage yield.
    if key == "US10Y" and last > 20:
        last, prev = last / 10.0, prev / 10.0
        closes = [c / 10.0 for c in closes]
        highs = [h / 10.0 for h in highs]
        lows = [lw / 10.0 for lw in lows]

    atr = _atr(highs, lows, closes)

    # Display formatting per instrument type.
    if key in ("VIX", "MOVE", "US10Y", "DXY", "Brent", "WTI", "USD/JPY", "GBP/JPY"):
        disp = fr_num(last, 2)
    elif key in ("XAU/USD", "DAX", "US30", "NAS100", "SPX500"):
        disp = fr_num(last, 0, thousands=True)
    else:
        disp = fr_num(last, 4)

    ts = now_utc.astimezone(pytz.UTC)
    stamp = SourceStamp("yfinance", Reliability.PRIMARY, timestamp=ts,
                        url=f"https://finance.yahoo.com/quote/{ticker}")
    return Datum(last, stamp, disp, _trend_str(last, prev)), atr


# Logical gauges and the instruments we price for setups.
_GAUGE_KEYS = ["VIX", "MOVE", "DXY", "US10Y", "XAU/USD", "Brent", "WTI"]


def build_market_snapshot(
    now_utc: Optional[datetime] = None,
    overrides: Optional[dict] = None,
    allow_proxy_levels: bool = True,
) -> MarketSnapshot:
    """Assemble a :class:`MarketSnapshot`.

    ``overrides`` is a flat ``{key: value}`` mapping (from the sidebar manual
    JSON). Any key present there is stamped ``[PROXY]`` (user-documented
    approximation) and takes precedence over the auto source.
    """
    now_utc = now_utc or datetime.now(pytz.UTC)
    overrides = overrides or {}
    snap = MarketSnapshot(as_of_utc=now_utc)

    from .config import UNIVERSE  # local import to avoid cycle at module load

    keys = set(_GAUGE_KEYS) | set(UNIVERSE)
    for key in keys:
        datum, atr = _fetch_instrument(key, now_utc)
        if key in overrides:
            val = overrides[key]
            try:
                fval = float(val)
                datum = Datum(fval, SourceStamp("manual override", Reliability.PROXY,
                                                note="saisie utilisateur"),
                              str(val).replace(".", ","), "")
            except (TypeError, ValueError):
                pass
        if key in _GAUGE_KEYS:
            snap.gauges[key] = datum
        if key in UNIVERSE:
            snap.prices[key] = datum
        if atr is not None:
            snap.atr[key] = atr

    # GDP Nowcast and FedWatch are not freely keyless; honest [N/A] unless overridden.
    for gkey in ("GDP_NOWCAST", "SURPRISE_IDX"):
        if gkey in overrides:
            snap.gauges[gkey] = Datum(
                None, SourceStamp("manual override", Reliability.PROXY),
                str(overrides[gkey]), "",
            )
        else:
            snap.gauges[gkey] = Datum(None, na_stamp("source sans cle API"), "N/A")

    return snap
