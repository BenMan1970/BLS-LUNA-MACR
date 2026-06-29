"""Macro Engine -- the institutional logic of BLUESTAR v8.1.

Rule-based and deterministic. It never invents a figure: when an input is
missing the corresponding output is ``[N/A]`` or a documented ``[PROXY]`` and
the conviction is reduced. The COT/IPS module only adjusts conviction, squeeze
risk and sizing -- it never generates a directional signal on its own.

Pipeline: regime -> catalysts -> central banks -> macro overlay -> currency
strength -> IPS -> asset selection -> levels/expected-move -> sizing -> risk
scenarios -> :class:`BriefingContext`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from . import config as C
from .models import (
    AssetSetup, BriefingContext, CentralBankSnapshot, CotPositioning,
    CurrencyStrength, MacroEvent, MarketSnapshot,
    RiskScenario, na_stamp, proxy_stamp,
)
from .market_data import fr_num

logger = logging.getLogger(__name__)

FR_DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
FR_MONTHS = ["", "janvier", "février", "mars", "avril", "mai", "juin",
             "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def fr_date(dt: datetime) -> str:
    return f"{dt.day:02d}/{dt.month:02d}/{dt.year}"


def fr_day_name(dt: datetime) -> str:
    return FR_DAYS[dt.weekday()]


# ---------------------------------------------------------------------------
# Session / live-market helpers
# ---------------------------------------------------------------------------
def session_label(dt_cet: datetime) -> tuple[str, bool]:
    """Return (human session label, is_live_fx_session)."""
    wd = dt_cet.weekday()  # 0=Mon .. 6=Sun
    # FX cash market: opens ~Sunday 23:00 CET, closes Friday ~23:00 CET.
    if wd == 5:  # Saturday
        return "MARCHÉ FX FERMÉ (week-end)", False
    if wd == 6 and dt_cet.hour < 23:  # Sunday before reopen
        return "MARCHÉ FX FERMÉ (week-end)", False
    if wd == 4 and dt_cet.hour >= 23:  # Friday after close
        return "MARCHÉ FX FERMÉ (week-end)", False
    h = dt_cet.hour
    if 8 <= h < 14:
        return "Session Londres", True
    if 14 <= h < 18:
        return "Overlap Londres/New York", True
    if 18 <= h < 22:
        return "Session New York", True
    return "Session Asie / hors liquidité", True


# ---------------------------------------------------------------------------
# Step 2 -- Market regime
# ---------------------------------------------------------------------------
def determine_market_regime(market: MarketSnapshot,
                            events: list[MacroEvent]) -> tuple[str, str, str, int]:
    """Return (regime_text, regime_class, regime_since, conviction_penalty)."""
    vix = market.gauge("VIX")
    penalty = 0
    if not vix.available:
        return ("MIXTE — données vol insuffisantes [PROXY]", "regime-mix",
                "[PROXY]", 1)

    v = vix.value
    imminent = any(e.priority == "CRITICAL" for e in events)
    if v >= C.VIX_RISK_OFF_MIN:
        text, cls = "RISK-OFF — aversion au risque", "regime-off"
    elif v <= C.VIX_RISK_ON_MAX:
        text, cls = "RISK-ON — appétit pour le risque", "regime-on"
    else:
        text, cls = "MIXTE — biais sélectif", "regime-mix"

    if imminent and cls != "regime-off":
        text += " · catalyseur binaire imminent"
        cls = "regime-mix"
        penalty = 1
    since = "événements macro récents"
    return text, cls, since, penalty


# ---------------------------------------------------------------------------
# Step 4 -- Central banks (no keyless source -> overrides or [N/A]/[PROXY])
# ---------------------------------------------------------------------------
_CB_DEFS = [
    ("FED", "🇺🇸", "USD"),
    ("BCE", "🇪🇺", "EUR"),
    ("BoJ", "🇯🇵", "JPY"),
    ("BoE", "🇬🇧", "GBP"),
]


def build_central_bank_context(overrides: Optional[dict]) -> list[CentralBankSnapshot]:
    """Build the four central-bank blocks.

    ``overrides['central_banks'][name]`` may carry: rate, fact, bias, next,
    pause/cut/hike. Missing pieces render as [N/A] -- never invented.
    """
    cb_over = (overrides or {}).get("central_banks", {})
    out: list[CentralBankSnapshot] = []
    for name, flag, _ccy in _CB_DEFS:
        o = cb_over.get(name, {})
        rate = o.get("rate", "[N/A]")
        fact = o.get("fact") or "[N/A] — taux/probabilité non sourcés sans clé API."
        bias = o.get("bias") or "[N/A] — interprétation à confirmer."
        nxt = o.get("next", "[N/A]")
        stamp = (proxy_stamp("manual override") if o else na_stamp("source sans clé API"))
        out.append(CentralBankSnapshot(
            name=name, flag=flag, rate_display=str(rate),
            fact=str(fact), bias_interpretation=str(bias), next_meeting=str(nxt),
            stamp=stamp,
            pause_pct=o.get("pause"), cut_pct=o.get("cut"), hike_pct=o.get("hike"),
        ))
    return out


_HAWKISH_OPEN_RE = re.compile(r"^\s*(très\s+)?hawkish\b")
_DOVISH_OPEN_RE = re.compile(r"^\s*(très\s+)?dovish\b")
_HAWKISH_ANY_RE = re.compile(r"\bhawkish\b")
_DOVISH_ANY_RE = re.compile(r"\bdovish\b")


def _parse_rate_pct(rate_display: str) -> Optional[float]:
    """Parse a CB rate string into a float percentage.

    Handles a single value ("2,25%"), a range ("3,50–3,75%" -> midpoint) and
    a leading "~" ("~1,00%"). Returns ``None`` for anything not parseable
    (e.g. "[N/A]"), so the caller can tell "sourced" from "not sourced".
    """
    if not rate_display:
        return None
    s = rate_display.strip().lstrip("~").rstrip("%").strip()
    parts = re.split(r"[–-]", s)
    try:
        vals = [float(p.strip().replace(",", ".")) for p in parts if p.strip()]
    except ValueError:
        return None
    return sum(vals) / len(vals) if vals else None


def _build_rate_differential(central_banks: list[CentralBankSnapshot]) -> tuple[str, str]:
    """Dominant policy-rate differential among the tracked central banks.

    Previously this was hardcoded to "non calculable sans taux sourcés"
    unconditionally -- even on a run where every rate *was* sourced via
    manual overrides. It now actually reads ``central_banks`` and only
    degrades to [N/A] when fewer than 2 rates are genuinely available.
    """
    rates: dict[str, float] = {}
    for (_name, _flag, ccy), cb in zip(_CB_DEFS, central_banks):
        if cb.stamp.ok:
            r = _parse_rate_pct(cb.rate_display)
            if r is not None:
                rates[ccy] = r

    if len(rates) < 2:
        return ("[N/A] — taux directeurs non sourcés (saisir en overrides).",
                "Différentiel non calculable sans au moins 2 taux sourcés.")

    ccy_hi = max(rates, key=rates.get)
    ccy_lo = min(rates, key=rates.get)
    gap = rates[ccy_hi] - rates[ccy_lo]
    dominant = (f"{ccy_hi} ({fr_num(rates[ccy_hi], 2)}%) vs {ccy_lo} "
               f"({fr_num(rates[ccy_lo], 2)}%) → écart ≈ {fr_num(gap, 2)} pt "
               "[PROXY · taux saisis en overrides]")
    if gap == 0:
        implication = f"Taux directeurs identiques ({ccy_hi}/{ccy_lo}) — pas de portage net entre les deux."
    else:
        implication = (f"Portage structurellement favorable à {ccy_hi} face à {ccy_lo} "
                       "tant que cet écart de taux directeurs persiste.")
    return dominant, implication


def _cb_bias_word(cb: CentralBankSnapshot) -> int:
    """Map a CB bias string to a strength delta (+hawkish / -dovish).

    The bias field is free text (e.g. "Hawkish — biais de resserrement..."
    or "Neutre à légèrement hawkish."). A plain ``"hawkish" in text`` match
    treats an outright stance and a hedged "leaning hawkish" identically --
    that's exactly how BoE's "Neutre à légèrement hawkish" used to land on
    the same +12 as the Fed's unqualified "Hawkish", producing a false tie.
    The tone word must *open* the sentence for the full +/-12; appearing
    later or softened (légèrement, neutre à, etc.) counts as a weaker +/-6.
    """
    b = cb.bias_interpretation.strip().lower()
    if _HAWKISH_OPEN_RE.match(b):
        return 12
    if _DOVISH_OPEN_RE.match(b):
        return -12
    if _HAWKISH_ANY_RE.search(b):
        return 6
    if _DOVISH_ANY_RE.search(b):
        return -6
    return 0


# ---------------------------------------------------------------------------
# Step 5b -- Currency Strength Ranking (qualitative, always [PROXY])
# ---------------------------------------------------------------------------
def build_currency_strength_ranking(
    central_banks: list[CentralBankSnapshot],
    regime_class: str,
) -> list[CurrencyStrength]:
    """Qualitative 0-100 score per major currency. Always [PROXY]."""
    cb_by_ccy = {ccy: cb for (name, _f, ccy), cb in zip(_CB_DEFS, central_banks)}
    scores: dict[str, int] = {c: 50 for c in C.MAJOR_CURRENCIES}
    drivers: dict[str, str] = {c: "neutre [PROXY]" for c in C.MAJOR_CURRENCIES}

    for ccy in C.MAJOR_CURRENCIES:
        cb = cb_by_ccy.get(ccy)
        if cb is not None:
            delta = _cb_bias_word(cb)
            scores[ccy] += delta
            if delta >= 12:
                drivers[ccy] = "biais hawkish"
            elif delta > 0:
                drivers[ccy] = "biais hawkish modéré"
            elif delta <= -12:
                drivers[ccy] = "biais dovish"
            elif delta < 0:
                drivers[ccy] = "biais dovish modéré"
        if regime_class == "regime-off" and ccy in C.SAFE_HAVENS:
            scores[ccy] += 10
            drivers[ccy] = "flux refuge"
        if regime_class == "regime-on" and ccy in C.SAFE_HAVENS:
            scores[ccy] -= 6

    ranked = sorted(C.MAJOR_CURRENCIES, key=lambda c: scores[c], reverse=True)
    rows: list[CurrencyStrength] = []
    for ccy in ranked:
        s = max(0, min(100, scores[ccy]))
        # Tier by the score's own value, not by rank position. Previously a
        # tie (e.g. two currencies both at 50) could still land in different
        # thirds purely because of where they happened to fall in the sort,
        # so identical scores rendered with different colours/widths.
        cls = "strong" if s >= 60 else "weak" if s <= 40 else "neutral"
        rows.append(CurrencyStrength(ccy, s, drivers[ccy], cls))
    return rows


# ---------------------------------------------------------------------------
# Step 5c -- IPS (Non-Commercials only) -- always [PROXY]
# ---------------------------------------------------------------------------
def build_ips_scores(overrides: Optional[dict]) -> list[CotPositioning]:
    """Build IPS rows from COT overrides (Non-Commercials net contracts).

    The 0-100 score is a documented heuristic (bounded scaling) -- not a true
    CFTC percentile -- hence always [PROXY].
    """
    cot_over = (overrides or {}).get("cot", {})
    rows: list[CotPositioning] = []
    for ccy in C.MAJOR_CURRENCIES:
        o = cot_over.get(ccy)
        if o is None:
            continue
        net = o.get("net")
        if net is None:
            continue
        net = int(net)
        # Map net contracts to 0-100 around 50 (heuristic, bounded).
        frac = max(-1.0, min(1.0, net / C.IPS_FULL_SCALE_CONTRACTS))
        score = int(round(50 + frac * 50))
        label = ("Crowded long" if score >= C.IPS_CROWDED else
                 "Crowded short / Capitulation" if score <= C.IPS_CAPITULATION else
                 "Normal")
        rows.append(CotPositioning(
            currency=ccy, net_contracts=net, ips_score=score, ips_label=label,
            delta_week=o.get("delta", "≈ stable"),
            momentum=o.get("momentum", "→"),
            stamp=proxy_stamp("CFTC J-3 — Non-Commercials",
                              note=o.get("date", "")),
        ))
    return rows


# ---------------------------------------------------------------------------
# Step 8 -- Expected move + levels
# ---------------------------------------------------------------------------
def _decimals_for(asset: str) -> int:
    if asset in ("USD/JPY", "GBP/JPY"):
        return 2
    if asset in ("XAU/USD", "DAX", "US30", "NAS100", "SPX500", "Brent", "WTI"):
        return 0 if asset in ("DAX", "US30", "NAS100", "SPX500") else 2
    return 4


def compute_expected_move(asset: str, market: MarketSnapshot) -> tuple[str, str, float]:
    """Return (display, method, atr_used)."""
    price = market.price(asset)
    atr = market.atr.get(asset)
    if atr is not None and price.available:
        if asset in ("EUR/USD", "GBP/USD", "AUD/USD", "NZD/USD", "USD/CHF",
                     "USD/CAD", "EUR/GBP"):
            disp = f"{int(round(atr * 10000))} pips"
        elif asset in ("USD/JPY", "GBP/JPY"):
            disp = f"{int(round(atr * 100))} pips"
        else:
            disp = fr_num(atr, _decimals_for(asset), thousands=True)
        return disp, "ATR 14j", atr
    if price.available:
        atr_proxy = price.value * C.PROXY_ATR_PCT
        return (fr_num(atr_proxy, _decimals_for(asset), thousands=True),
                "PROXY ~0,6% prix", atr_proxy)
    return "[N/A]", "[N/A]", 0.0


def _level(value: float, asset: str) -> str:
    return fr_num(value, _decimals_for(asset), thousands=asset in
                  ("XAU/USD", "DAX", "US30", "NAS100", "SPX500"))


# ---------------------------------------------------------------------------
# Correlation overlay -- real short-window Pearson r, always [PROXY]
# ---------------------------------------------------------------------------
# Each instrument paired with the single macro benchmark it is most commonly
# read against. This is a heuristic pairing (not a full cross-asset matrix),
# which is exactly why the output stays tagged [PROXY] even when the number
# itself is genuinely computed.
_CORR_BENCHMARK: dict[str, str] = {
    "EUR/USD": "DXY", "GBP/USD": "DXY", "AUD/USD": "DXY", "NZD/USD": "DXY",
    "USD/CAD": "DXY", "USD/CHF": "DXY", "EUR/GBP": "DXY",
    "USD/JPY": "US10Y", "GBP/JPY": "US10Y",
    "XAU/USD": "US10Y", "Brent": "DXY", "WTI": "DXY",
    "DAX": "VIX", "US30": "VIX", "NAS100": "VIX", "SPX500": "VIX",
}


def _pct_returns(closes: list[float]) -> list[float]:
    """Day-over-day percentage returns (correlating returns, not raw levels,
    avoids the spurious correlation two trending price series would share)."""
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] != 0]


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation coefficient; None if too short or zero-variance."""
    n = min(len(xs), len(ys))
    if n < 10:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


def _correlation_value(asset: str, market: MarketSnapshot) -> Optional[tuple[float, str, int]]:
    """Return (r, benchmark, n_sessions) computed from real closes, or None."""
    bench = _CORR_BENCHMARK.get(asset)
    if not bench:
        return None
    a_closes, b_closes = market.closes.get(asset), market.closes.get(bench)
    if not a_closes or not b_closes:
        return None
    n = min(len(a_closes), len(b_closes))
    r = _pearson(_pct_returns(a_closes[-n:]), _pct_returns(b_closes[-n:]))
    if r is None:
        return None
    return r, bench, n - 1  # n-1 usable return observations


def compute_correlation(asset: str, market: MarketSnapshot) -> str:
    """Asset-card '9. Corrélation clé' field -- a real, computed [PROXY].

    Previously this was a hardcoded literal string ("[PROXY] corrélations
    30j") identical for every asset, every day, regardless of what the
    market actually did. The closes needed are already fetched for the ATR
    calculation, so this costs zero extra network calls / API keys.
    """
    res = _correlation_value(asset, market)
    if res is None:
        if asset not in _CORR_BENCHMARK:
            return "[N/A] — pas de référence définie pour cet actif."
        return "[PROXY] corrélation indisponible (historique insuffisant)."
    r, bench, n = res
    sign = "+" if r >= 0 else "−"
    return f"≈ {sign}{fr_num(abs(r), 2)} {bench} [PROXY · {n} séances]"


def _correlation_short(asset: str, market: MarketSnapshot) -> str:
    """Compact form for the Section 3 overlay row (no bracket -- the
    template already appends a single shared [PROXY] tag for that row)."""
    res = _correlation_value(asset, market)
    if res is None:
        return f"{asset} n/d"
    r, bench, _n = res
    sign = "+" if r >= 0 else "−"
    return f"{asset} ≈ {sign}{fr_num(abs(r), 2)} {bench}"


# ---------------------------------------------------------------------------
# Sizing factor (NOT Kelly)
# ---------------------------------------------------------------------------
def compute_sizing_factor(conviction: int, market: MarketSnapshot) -> str:
    vix = market.gauge("VIX")
    if vix.available:
        sf = conviction * (1.0 / (1.0 + vix.value / C.SIZING_VIX_DENOM))
        return f"{fr_num(sf, 1)}×"
    sf = conviction * (1.0 / (1.0 + C.SIZING_PROXY_VIX / C.SIZING_VIX_DENOM))
    return f"{fr_num(sf, 1)}× [PROXY]"


# ---------------------------------------------------------------------------
# Step 6 -- Asset selection
# ---------------------------------------------------------------------------
def _events_for_ccys(events: list[MacroEvent], ccys: tuple[str, ...],
                     within_h: float = 72) -> list[MacroEvent]:
    return [e for e in events
            if e.currency in ccys and -6 <= e.hours_until <= within_h]


def _strength_map(cs: list[CurrencyStrength]) -> dict[str, int]:
    return {r.currency: r.score for r in cs}


def select_priority_assets(
    market: MarketSnapshot,
    regime_class: str,
    central_banks: list[CentralBankSnapshot],
    currency_strength: list[CurrencyStrength],
    ips: list[CotPositioning],
    events: list[MacroEvent],
    mode: str,
    allow_proxy_levels: bool,
) -> tuple[list[AssetSetup], list[tuple[str, str]], Optional[str]]:
    """Score the universe, return (priority[<=3], avoid, no_setup_reason)."""
    smap = _strength_map(currency_strength)
    ips_by_ccy = {r.currency: r for r in ips}
    min_score = C.MODE_SELECTION_MIN_SCORE.get(mode, 0.5)

    scored: list[tuple[float, AssetSetup]] = []
    avoid: list[tuple[str, str]] = []

    for asset in C.UNIVERSE:
        price = market.price(asset)
        if not price.available:
            continue  # cannot build a price-anchored setup -> silently skip

        ccys = C.INSTRUMENT_CCYS.get(asset)
        ev = _events_for_ccys(events, ccys) if ccys else []
        critical_now = [e for e in ev if e.priority == "CRITICAL"]

        # Binary-news guard: imminent critical event -> avoid (red).
        if critical_now:
            ename = critical_now[0].event_name or "événement majeur"
            avoid.append((asset, f"News binaire imminente ({ename}) — attendre la publication."))
            continue

        # Directional macro edge (FX only, from currency strength).
        direction = 0
        edge = 0.0
        if ccys:
            base, quote = ccys
            diff = smap.get(base, 50) - smap.get(quote, 50)
            edge = abs(diff) / 50.0
            direction = 1 if diff > 0 else -1 if diff < 0 else 0
        else:
            # Commodities / indices: only a weak regime tilt, no fabricated edge.
            if regime_class == "regime-off":
                if asset == "XAU/USD":
                    direction, edge = 1, 0.25
                elif asset in C.INDICES:
                    direction, edge = -1, 0.25
            elif regime_class == "regime-on" and asset in C.INDICES:
                direction, edge = 1, 0.2

        atr = market.atr.get(asset)
        price_avail = 1.0
        vol_ok = 1.0 if atr is not None else 0.6
        catalyst_pen = 0.15 * len([e for e in ev if e.priority == "HIGH"])

        score = 0.45 * edge + 0.25 * price_avail + 0.2 * vol_ok - min(0.3, catalyst_pen)
        score = max(0.0, min(1.0, score))

        if direction == 0 or score < min_score:
            continue

        setup = _build_setup(asset, direction, score, market, allow_proxy_levels,
                             ips_by_ccy, ccys, ev)
        scored.append((score, setup))

    scored.sort(key=lambda t: t[0], reverse=True)
    priority = [s for _, s in scored[:C.MAX_PRIORITY_ASSETS]]

    no_setup = None
    if not priority:
        no_setup = ("Aucun actif ne réunit un biais macro directionnel suffisant "
                    "avec des niveaux exploitables aujourd'hui — données de prix/positionnement "
                    "insuffisantes ou régime sans edge. Conformément à BLUESTAR v8.1, aucun "
                    "setup n'est forcé.")
    return priority, avoid, no_setup


def _build_setup(asset: str, direction: int, score: float, market: MarketSnapshot,
                 allow_proxy_levels: bool, ips_by_ccy: dict, ccys, ev) -> AssetSetup:
    price = market.price(asset)
    em_disp, em_method, atr = compute_expected_move(asset, market)
    # Levels are ATR-derived -> [PROXY] origin (we have no real S/R feed).
    p = price.value
    buy = p - C.LEVEL_ATR_MULT * atr if atr else None
    sell = p + C.LEVEL_ATR_MULT * atr if atr else None
    if direction > 0:  # long bias
        stop = (p - C.STOP_ATR_MULT * atr) if atr else None
        bias_class = action_class = "long"
        arrow, action = "↑", "CHERCHER LONG"
        bias = "🟢/🟡 LONG"
    else:  # short bias
        stop = (p + C.STOP_ATR_MULT * atr) if atr else None
        bias_class = action_class = "short"
        arrow, action = "↓", "CHERCHER SHORT"
        bias = "🟢/🟡 SHORT"

    levels_proxy = atr is None or em_method.startswith("PROXY")

    # Conviction: score -> stars, minus 1 if proxy levels.
    conviction = 2 + int(round(score * 2))  # 2..4
    if levels_proxy or not allow_proxy_levels:
        conviction = max(1, conviction - 1)

    # Positioning link (COT adjusts only).
    pos_link = "Pas de COT chargé pour cet actif [N/A] — positionnement non pris en compte."
    squeeze_risk, squeeze_cls = "Faible", "green"
    ips_summary = "[N/A]"
    if ccys:
        for ccy in ccys:
            r = ips_by_ccy.get(ccy)
            if r and r.ips_score is not None:
                ips_summary = f"{ccy} {r.ips_score} [PROXY] {r.ips_label}"
                if r.is_extreme:
                    squeeze_risk, squeeze_cls = f"Élevé ({ccy} IPS extrême)", "red"
                    pos_link = (f"{ccy} en zone extrême (IPS {r.ips_score} [PROXY]). "
                                "Le COT ne déclenche pas le trade ; il signale un risque de "
                                "squeeze inverse si le catalyseur déçoit → conviction ±, stop strict.")
                else:
                    pos_link = (f"{ccy} IPS {r.ips_score} [PROXY] (Normal) — positionnement "
                                "non extrême, n'amende pas la conviction.")
                break

    # Reduce conviction by 1 if positioning is contra and extreme (squeeze).
    color = "yellow"
    if score >= 0.7 and not levels_proxy:
        color = "green"

    ev_names = ", ".join(sorted({e.event_name for e in ev})[:2]) if ev else "aucun catalyseur proche"
    invalidation = (f"Retournement macro / catalyseur ({ev_names})")
    inval_level = (f"clôture {'au-dessus' if direction>0 else 'sous'} du stop "
                   f"{_level(stop, asset)}" if stop is not None else "[N/A]")

    return AssetSetup(
        asset=asset, color=color, bias=bias, bias_class=bias_class,
        reason_short=("force relative macro" if ccys else "tilt de régime"),
        reason_macro=("Différentiel de force de devises [PROXY] favorable"
                      if ccys else "Biais de régime (refuge / risque) [PROXY]"),
        conviction=conviction, action=action, action_class=action_class, arrow=arrow,
        zone_buy=_level(buy, asset) if buy is not None else "[N/A]",
        origin_buy="[PROXY · ATR 14j / support implicite]" if buy is not None else "[N/A]",
        zone_sell=_level(sell, asset) if sell is not None else "[N/A]",
        origin_sell="[PROXY · ATR 14j / résistance implicite]" if sell is not None else "[N/A]",
        stop=_level(stop, asset) if stop is not None else "[N/A]",
        origin_stop="[PROXY · ATR extension]" if stop is not None else "[N/A]",
        expected_move=em_disp, em_method=em_method,
        session="Londres / New York", session_reason="liquidité maximale",
        invalidation_risk=invalidation, invalidation_level=inval_level,
        positioning_link=pos_link, correlation_key=compute_correlation(asset, market),
        ips_summary=ips_summary, squeeze_risk=squeeze_risk, squeeze_class=squeeze_cls,
        sizing_factor=compute_sizing_factor(conviction, market),
        price_display=price.display, levels_are_proxy=levels_proxy,
    )


# ---------------------------------------------------------------------------
# Step 3 -- Catalysts
# ---------------------------------------------------------------------------
def build_catalysts(events: list[MacroEvent]) -> tuple[list[MacroEvent], list[MacroEvent], dict]:
    """Split events into 🔴 ÉLEVÉ and 🟡 MODÉRÉ, build beat/miss scenarios."""
    high = [e for e in events if e.priority in ("CRITICAL", "HIGH") and e.is_upcoming]
    medium = [e for e in events if e.priority == "MEDIUM" and e.is_upcoming]
    high.sort(key=lambda e: e.hours_until)
    medium.sort(key=lambda e: e.hours_until)

    scenarios: dict[str, dict] = {}
    for e in high:
        key = e.datetime_utc + e.event_name
        affected = " · ".join(e.pairs_affected[:4]) if e.pairs_affected else "[N/A]"
        scenarios[key] = {
            "prev": e.previous, "cons": e.forecast,
            "beat_impact": (f"{e.currency} plus fort → pression sur {affected}. "
                            "Ampleur en pips/% : [PROXY] (dépend de l'écart au consensus)."),
            "beat_action": f"Renforce les setups short {e.currency}-quote / long {e.currency}-base.",
            "miss_impact": (f"{e.currency} plus faible → soutien inverse sur {affected}. "
                            "Ampleur : [PROXY]."),
            "miss_action": f"Invalide les biais alignés sur un {e.currency} fort.",
            "advice": ("Ne pas ouvrir taille pleine avant la publication ; "
                       "attendre la confirmation post-chiffre."),
        }
    return high[:6], medium[:6], scenarios


# ---------------------------------------------------------------------------
# Step -- Macro overlay text blocks
# ---------------------------------------------------------------------------
def build_macro_overlay(market: MarketSnapshot, regime: str,
                        events: list[MacroEvent]) -> dict:
    vix = market.gauge("VIX")
    move = market.gauge("MOVE")
    dxy = market.gauge("DXY")

    nearest = events[0].event_name if events else "—"
    theme = (f"Semaine pilotée par le calendrier macro (prochain catalyseur : {nearest}). "
             f"Régime : {regime}.")

    if dxy.available:
        dxy_ctx = f"DXY {dxy.display} ({dxy.trend or 'tendance n/d'}) — impacte EUR/USD, USD/JPY, USD/CAD."
        dxy_src = dxy.stamp.render()
    else:
        dxy_ctx = "DXY [N/A] — contexte dollar non sourcé."
        dxy_src = "[N/A]"

    if vix.available:
        method = "niveau absolu + position vs seuils (15 / 22)"
        vol_regime = f"VIX {vix.display} · MOVE {move.display if move.available else 'N/A'}"
        vol_impl = (f"Méthode : {method}. "
                    + ("Vol comprimée → stops plus serrés viables." if vix.value < 18
                       else "Vol modérée à élevée → réduire la taille, élargir les stops."))
    else:
        vol_regime = "VIX [N/A]"
        vol_impl = "Méthode indisponible — régime vol non évaluable [PROXY]."

    return {
        "theme": theme, "theme_src": "[Forex Factory | calendrier]",
        "dxy_ctx": dxy_ctx, "dxy_src": dxy_src,
        "vol_regime": vol_regime, "vol_impl": vol_impl,
        "correlation": f"{_correlation_short('EUR/USD', market)} · {_correlation_short('USD/JPY', market)}",
        "liquidity": ("Pas de stress de financement USD signalé [PROXY]. "
                      "Surveiller les flux de fin de mois/trimestre si applicable."),
    }


# ---------------------------------------------------------------------------
# Step -- Risk scenarios
# ---------------------------------------------------------------------------
def build_risk_scenarios(events: list[MacroEvent], regime_class: str,
                         priority: list[AssetSetup]) -> tuple[dict, RiskScenario, RiskScenario, str]:
    anchor = events[0] if events else None
    anchor_name = anchor.event_name if anchor else "[N/A]"
    anchor_src = "[Forex Factory | calendrier]" if anchor else "[N/A]"

    risk_main = {
        "desc": ("Surprise macro sur le principal catalyseur de la semaine "
                 f"({anchor_name}) déclenchant un repricing brutal."),
        "asset": priority[0].asset if priority else "—",
        "level": "niveaux [PROXY]",
        "proba": "qualitative — équilibré",
        "source": anchor_src,
    }
    bull = RiskScenario(
        title="Scénario BULL (risk-on)", proba="favori si données molles",
        trigger=f"{anchor_name} sous le consensus → détente des taux/vol",
        trigger_source=anchor_src,
        rows=[f"{s.asset} → mouvement aligné risk-on" for s in priority[:3]] or
             ["Aucun actif prioritaire — voir bloc no-setup"],
    )
    bear = RiskScenario(
        title="Scénario BEAR (risk-off)", proba="favori si données chaudes / choc",
        trigger=f"{anchor_name} au-dessus du consensus → repricing hawkish / fuite vers la qualité",
        trigger_source=anchor_src,
        rows=["Refuges : Or · JPY · CHF"] +
             [f"{s.asset} → mouvement aligné risk-off" for s in priority[:3]],
    )
    inval = (f"Désamorçage du catalyseur principal ({anchor_name}) ou retour de la "
             "volatilité dans sa fourchette → révision du scénario dominant.")
    return risk_main, bull, bear, inval


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def build_context(
    now_utc: datetime,
    market: MarketSnapshot,
    calendar: dict,
    overrides: Optional[dict],
    mode: str = "Normal",
    allow_proxy_levels: bool = True,
) -> BriefingContext:
    """Assemble the full :class:`BriefingContext` from all layers."""
    overrides = overrides or {}
    now_cet = now_utc.astimezone(C.TZ_CET)
    _, is_live = session_label(now_cet)

    raw_engine = calendar.get("events_engine") or calendar.get("events") or []
    events = [MacroEvent.from_enriched(e) for e in raw_engine]
    upcoming = [e for e in events if e.is_upcoming]

    regime, regime_cls, regime_since, _pen = determine_market_regime(market, events)
    central_banks = build_central_bank_context(overrides)
    cs = build_currency_strength_ranking(central_banks, regime_cls)
    ips = build_ips_scores(overrides)
    high, medium, scenarios = build_catalysts(events)
    overlay = build_macro_overlay(market, regime, upcoming)

    priority, avoid, no_setup = select_priority_assets(
        market, regime_cls, central_banks, cs, ips, events, mode, allow_proxy_levels)

    risk_main, bull, bear, inval = build_risk_scenarios(upcoming, regime_cls, priority)

    # Squeeze / positioning alert
    squeeze_ccy = None
    pos_alert = None
    setup_ccys = set()
    for s in priority:
        c = C.INSTRUMENT_CCYS.get(s.asset)
        if c:
            setup_ccys.update(c)
    for r in ips:
        if r.is_extreme and r.currency in setup_ccys:
            squeeze_ccy = r.currency
            pos_alert = (f"{r.currency} IPS {r.ips_score} [PROXY] en zone extrême et impliqué "
                         "dans un setup → risque de squeeze inverse. Ne rejette pas le setup, "
                         "mais impose un stop discipliné.")
            break

    # Diff dominant -- computed from sourced CB rates (>= 2 needed to mean anything).
    diff_dominant, diff_impl = _build_rate_differential(central_banks)

    cot_date = ""
    for r in ips:
        if r.stamp.note:
            cot_date = r.stamp.note
            break
    cot_summary = ("Aucune donnée COT chargée [N/A] — saisir les Non-Commercials en overrides."
                   if not ips else
                   "Non-Commercials : " + " · ".join(
                       f"{r.currency} {r.ips_label} (IPS {r.ips_score})" for r in ips[:4]))

    op_note = None
    if not is_live:
        op_note = ("Briefing généré hors session FX live — niveaux & données basés sur la "
                   "dernière clôture. À revalider à l'ouverture de la prochaine session.")

    return BriefingContext(
        generated_utc=now_utc, generated_cet=now_cet,
        is_live_session=is_live, operational_note=op_note,
        regime=regime, regime_class=regime_cls, regime_since=regime_since,
        market=market, central_banks=central_banks,
        diff_dominant=diff_dominant, diff_implication=diff_impl,
        macro_theme=overlay["theme"], macro_theme_src=overlay["theme_src"],
        cot_summary=cot_summary, cot_date=cot_date or "[N/A]",
        squeeze_currency=squeeze_ccy,
        dxy_context=overlay["dxy_ctx"], dxy_src=overlay["dxy_src"],
        vol_regime=overlay["vol_regime"], vol_implication=overlay["vol_impl"],
        correlation_summary=overlay["correlation"], liquidity_flow=overlay["liquidity"],
        currency_strength=cs, ips_scores=ips, positioning_alert=pos_alert,
        catalysts_high=high, catalysts_medium=medium, catalyst_scenarios=scenarios,
        priority_assets=priority, avoid_assets=avoid, no_setup_reason=no_setup,
        risk_main=risk_main, bull=bull, bear=bear, invalidation_principal=inval,
        issues=[],
    )
