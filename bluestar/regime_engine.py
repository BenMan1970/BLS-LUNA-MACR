"""Market Regime Engine — BLUESTAR institutional regime identification.

Implements a multi-factor regime classification system that goes beyond the
original VIX-only threshold.  The engine combines:

  * **Volatility regime** (VIX / MOVE)
  * **Growth signal** (GDPNow, surprise index)
  * **Inflation / rates signal** (US10Y, rate differentials)
  * **Liquidity signal** (SOFR-EFFR spread, funding stress)
  * **Sentiment / flow signal** (DXY direction, risk appetite)
  * **Positioning signal** (COT extremes, squeeze risk)

Each regime is explained with:
  - why it was chosen (supporting indicators)
  - what weakens it (contradictory indicators)
  - what events could trigger a change

This addresses audit finding A4 (regime without guardrails) and implements
Priorities 3 & 4 of the maturation brief.
"""
from __future__ import annotations

from typing import Optional

import logging
from dataclasses import dataclass, field

from . import config as C
from .models import (
    CentralBankSnapshot, CotPositioning, CurrencyStrength,
    MarketSnapshot,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regime definitions
# ---------------------------------------------------------------------------
@dataclass
class RegimeIndicator:
    """A single indicator contributing to the regime assessment."""
    name: str
    value: str           # human-readable value
    signal: str          # "risk_on", "risk_off", "neutral", "inflation", "deflation", etc.
    weight: float        # contribution weight (0-1)
    supports: bool       # True if this indicator supports the chosen regime
    note: str = ""


@dataclass
class RegimeAssessment:
    """Full regime assessment with explanation."""
    name: str                    # e.g. "Risk-On", "Reflation", "Dollar Smile"
    category: str                # "risk_on", "risk_off", "transitional", "policy_divergence"
    confidence: float            # 0-1, how confident the engine is
    description: str             # 1-2 sentence explanation
    supporting: list[RegimeIndicator] = field(default_factory=list)
    contradicting: list[RegimeIndicator] = field(default_factory=list)
    transition_triggers: list[str] = field(default_factory=list)
    narrative: str = ""          # the story connecting indicators to the regime


# ---------------------------------------------------------------------------
# Regime catalogue
# ---------------------------------------------------------------------------
_REGIMES = {
    "Risk-On": {
        "category": "risk_on",
        "description": "Appétit pour le risque — les actifs pro-cycliques sont favorisés. "
                       "VIX comprimé, liquidité abondante, croissance soutenue.",
    },
    "Risk-Off": {
        "category": "risk_off",
        "description": "Aversion au risque — fuite vers les refuges (USD, JPY, CHF, Or). "
                       "VIX élevé, stress de liquidité, incertitude macro.",
    },
    "Goldilocks": {
        "category": "risk_on",
        "description": "Croissance modérée + inflation contenue + politiques accommodantes. "
                       "Environnement idéal pour les actifs risqués sans tension rates.",
    },
    "Reflation": {
        "category": "transitional",
        "description": "Reprise économique avec remontée de l'inflation. "
                       "Taux longs en hausse, banques centrales en mode normalisation. "
                       "Value vs growth, cycliques favorisées.",
    },
    "Disinflation": {
        "category": "transitional",
        "description": "Inflation en refroidissement, croissance ralentit. "
                       "Marché anticipe des baisses de taux. Duration longue favorisée.",
    },
    "Late Cycle": {
        "category": "risk_off",
        "description": "Fin de cycle — croissance ralentit, inflation persistante, "
                       "banques centrales restrictives. Volatilité croissante, "
                       "defensif privilégié.",
    },
    "Dollar Smile": {
        "category": "policy_divergence",
        "description": "USD fort indépendamment du risk appetite — soit par croissance "
                       "supérieure (droite du smile), soit par flight-to-safety (gauche). "
                       "Impact négatif sur EM et commodities.",
    },
    "Policy Divergence": {
        "category": "policy_divergence",
        "description": "Divergence marquée entre banques centrales (Fed hawkish vs BCE/BoJ "
                       "dovish). Crée des opportunités de carry trade directionnelles.",
    },
    "Liquidity Expansion": {
        "category": "risk_on",
        "description": "Injection de liquidité par les banques centrales (QE, repo, "
                       "interventions). Asset inflation, risk-on broad.",
    },
    "Liquidity Contraction": {
        "category": "risk_off",
        "description": "Resserrement de la liquidité (QT, drain repo, retraits de dépôts). "
                       "Pression sur les actifs risqués, volatilité en hausse.",
    },
    "Mixed / Selective": {
        "category": "transitional",
        "description": "Signaux contradictoires — ni risk-on clair ni risk-off franc. "
                       "Sélectivité requise, conviction réduite.",
    },
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def _vix_indicator(market: MarketSnapshot) -> RegimeIndicator | None:
    """VIX volatility regime indicator."""
    vix = market.gauge("VIX")
    if not vix.available:
        return None
    v = vix.value
    if v <= C.VIX_RISK_ON_MAX:
        return RegimeIndicator("VIX", f"{v:.1f}", "risk_on", 0.20, True,
                               "Volatilité actions comprimée — appétit de risque.")
    if v >= C.VIX_RISK_OFF_MIN:
        return RegimeIndicator("VIX", f"{v:.1f}", "risk_off", 0.20, True,
                               "Volatilité élevée — aversion au risque.")
    return RegimeIndicator("VIX", f"{v:.1f}", "neutral", 0.10, True,
                           "Volatilité modérée — régime transitionnel.")


def _move_indicator(market: MarketSnapshot) -> RegimeIndicator | None:
    """MOVE volatility regime indicator."""
    move = market.gauge("MOVE")
    if not move.available:
        return None
    m = move.value
    if m < 90:
        return RegimeIndicator("MOVE", f"{m:.1f}", "risk_on", 0.10, True,
                               "Volatilité taux comprimée — pas de stress rates.")
    if m > 120:
        return RegimeIndicator("MOVE", f"{m:.1f}", "risk_off", 0.10, True,
                               "Volatilité taux élevée — stress sur le marché obligataire.")
    return RegimeIndicator("MOVE", f"{m:.1f}", "neutral", 0.05, True,
                           "Volatilité taux modérée.")


def _us10y_indicator(market: MarketSnapshot) -> RegimeIndicator | None:
    """US10Y rates/inflation indicator."""
    us10y = market.gauge("US10Y")
    if not us10y.available:
        return None
    y = us10y.value
    if y > 4.5:
        return RegimeIndicator("US10Y", f"{y:.2f}%", "inflation", 0.10, True,
                               "Taux longs élevés — pression inflationniste ou croissance forte.")
    if y < 3.5:
        return RegimeIndicator("US10Y", f"{y:.2f}%", "deflation", 0.10, True,
                               "Taux longs bas — anticipation de ralentissement / disinflation.")
    return RegimeIndicator("US10Y", f"{y:.2f}%", "neutral", 0.05, True,
                           "Taux longs dans la fourchette neutre.")


def _dxy_indicator(market: MarketSnapshot) -> RegimeIndicator | None:
    """DXY dollar direction indicator."""
    dxy = market.gauge("DXY")
    if not dxy.available:
        return None
    d = dxy.value
    if d > 105:
        return RegimeIndicator("DXY", f"{d:.1f}", "dollar_strong", 0.10, True,
                               "Dollar fort — pression sur EM/commodities, potentiel Dollar Smile.")
    if d < 100:
        return RegimeIndicator("DXY", f"{d:.1f}", "dollar_weak", 0.10, True,
                               "Dollar faible — soutien aux actifs risqués et commodities.")
    return RegimeIndicator("DXY", f"{d:.1f}", "neutral", 0.05, True,
                           "Dollar dans sa fourchette neutre.")


def _rate_divergence_indicator(central_banks: list[CentralBankSnapshot]) -> RegimeIndicator | None:
    """Central bank policy divergence indicator."""
    cb_rates = {}
    for cb in central_banks:
        if cb.stamp.ok and cb.rate_display and cb.rate_display != "[N/A]":
            try:
                rate_str = cb.rate_display.replace("%", "").replace(",", ".").strip()
                parts = rate_str.split("–")
                if len(parts) == 2:
                    cb_rates[cb.name] = (float(parts[0]) + float(parts[1])) / 2
                else:
                    cb_rates[cb.name] = float(parts[0])
            except (ValueError, IndexError):
                pass
    if len(cb_rates) < 2:
        return None
    rates_list = sorted(cb_rates.values())
    spread = rates_list[-1] - rates_list[0]
    if spread > 2.0:
        return RegimeIndicator("Rate Differential", f"{spread:.1f}pt", "policy_divergence", 0.10, True,
                               "Écart de taux directeurs marqué — opportunités de carry trade.")
    if spread < 0.5:
        return RegimeIndicator("Rate Differential", f"{spread:.1f}pt", "neutral", 0.05, True,
                               "Taux directeurs convergents — peu de carry.")
    return None


def _positioning_indicator(ips: list[CotPositioning]) -> RegimeIndicator | None:
    """COT positioning squeeze risk indicator."""
    extreme_ips = [r for r in ips if r.is_extreme]
    if not extreme_ips:
        return None
    return RegimeIndicator("COT Positioning", f"{len(extreme_ips)} extreme(s)", "risk_off", 0.05, True,
                           "Positionnement extrême détecté — risque de squeeze, prudence.")


def _catalyst_indicator(events: list) -> RegimeIndicator | None:
    """Imminent binary catalyst indicator."""
    critical_events = [e for e in events if e.priority == "CRITICAL" and e.is_upcoming]
    if not critical_events:
        return None
    return RegimeIndicator("Calendar", f"{len(critical_events)} catalyseur(s) binaire(s)",
                           "risk_off", 0.10, True,
                           "Catalyseur binaire imminent — réduire la conviction avant publication.")


def _gdp_indicator(market: MarketSnapshot) -> RegimeIndicator | None:
    """GDP Nowcast growth signal indicator."""
    gdp = market.gauge("GDP_NOWCAST")
    if not gdp.available:
        return None
    g = gdp.value
    if g > 2.5:
        return RegimeIndicator("GDPNow", f"{g:.1f}%", "risk_on", 0.05, True,
                               "Croissance au-dessus du potentiel — environnement risk-on.")
    if g < 1.0:
        return RegimeIndicator("GDPNow", f"{g:.1f}%", "risk_off", 0.05, True,
                               "Croissance faible — risque de ralentissement.")
    return None



def _pc_indicator(pc_data: Optional[dict]) -> RegimeIndicator | None:
    """CBOE Put/Call ratio sentiment indicator.

    Translates the VIX × P/C composite signal into a RegimeIndicator.
    Weight 0.05 — informative but never regime-determining alone.
    Signal type mapping:
      COMPLACENCE* → risk_on   (options market confirms risk appetite)
      COUVERTURE*  → risk_off  (hedging flow confirms caution)
      DANGER ZONE / PEUR EXTREME / DIVERGENCE / NEUTRE → neutral
    Never raises — returns None on any missing or incomplete data.
    """
    if pc_data is None:
        return None
    equity    = pc_data.get("equity") or {}
    index     = pc_data.get("index")  or {}
    composite = pc_data.get("composite_signal", "")
    eq_ma     = equity.get("ma_5d")
    idx_ma    = index.get("ma_5d")
    stale     = pc_data.get("stale", False)

    if not composite or eq_ma is None or idx_ma is None:
        return None

    value_str  = f"Eq.P/C {eq_ma} · Idx.P/C {idx_ma}"
    stale_note = " [STALE]" if stale else ""
    note       = f"{composite}{stale_note}"

    # Signal type — minimal weight, additive only
    if "COMPLACENCE" in composite and "DANGER" not in composite:
        signal = "risk_on"
    elif any(x in composite for x in ("COUVERTURE GENERALISEE", "COUVERTURE ÉLEVÉE")):
        signal = "risk_off"
    else:
        signal = "neutral"   # NEUTRE / DANGER ZONE / PEUR EXTREME / DIVERGENCE

    return RegimeIndicator(
        "P/C Ratio", value_str, signal, 0.05, True, note
    )


def assess_regime(
    market: MarketSnapshot,
    central_banks: list[CentralBankSnapshot],
    currency_strength: list[CurrencyStrength],
    ips: list[CotPositioning],
    events: list,
    now_utc,
    pc_data: Optional[dict] = None,
) -> RegimeAssessment:
    """Assess the current market regime from all available indicators.

    This replaces the original ``determine_market_regime`` which used only VIX.
    The new engine weighs multiple factors and produces an explainable result.
    """
    indicators = [
        ind for ind in [
            _vix_indicator(market),
            _move_indicator(market),
            _us10y_indicator(market),
            _dxy_indicator(market),
            _rate_divergence_indicator(central_banks),
            _positioning_indicator(ips),
            _catalyst_indicator(events),
            _gdp_indicator(market),
            _pc_indicator(pc_data),          # C1: VIX × P/C sentiment
        ] if ind is not None
    ]

    regime_name, category, confidence, opposing_signal = _classify(indicators)

    # Audit fix ("Mixed sans contradiction"): every RegimeIndicator is built
    # with supports=True hardcoded at construction time (the individual
    # _xxx_indicator() helpers cannot know the final regime yet), so
    # `contradicting` was structurally always empty regardless of what the
    # data actually showed. _classify() now reports, alongside the regime,
    # which single signal had to be absent/sub-material for that regime to
    # be selected (derived directly from the existing has_X preconditions —
    # no new heuristic). Any indicator carrying exactly that signal is
    # flagged here as contradicting. Scope is deliberately narrow: only the
    # unambiguous risk_on/risk_off/inflation/deflation antonym pairs used by
    # the decision tree are covered; transitional/policy_divergence regimes
    # with no clean antonym are left untouched (supports stays True, i.e.
    # unchanged prior behaviour).
    if opposing_signal is not None:
        for ind in indicators:
            if ind.signal == opposing_signal:
                ind.supports = False

    supporting = [i for i in indicators if i.supports]
    contradicting = [i for i in indicators if not i.supports]
    triggers = _build_triggers(regime_name, events, indicators)
    narrative = _build_narrative(regime_name, indicators, central_banks, currency_strength)

    return RegimeAssessment(
        name=regime_name,
        category=category,
        confidence=confidence,
        description=_REGIMES.get(regime_name, {}).get("description", ""),
        supporting=supporting,
        contradicting=contradicting,
        transition_triggers=triggers,
        narrative=narrative,
    )


def _classify(indicators: list[RegimeIndicator]) -> tuple[str, str, float, Optional[str]]:
    """Classify the regime from the weighted indicator signals.

    Returns ``(regime_name, category, confidence, opposing_signal)``.

    ``opposing_signal`` is the ``RegimeIndicator.signal`` value that had to
    be absent/sub-material for this particular regime to be selected (e.g.
    "risk_off" for a "Risk-On" regime). It is derived directly from the
    ``has_X`` preconditions of the branch that fired below — it is not a new
    independent judgement — and lets the caller (``assess_regime``) flag any
    indicator carrying that signal as contradicting instead of the previous
    hardcoded "always supports" behaviour. ``None`` for regimes without a
    single clean antonym (Mixed / Selective, Dollar Smile, Policy
    Divergence): those are left exactly as before.

    Audit fix (BLUESTAR v9.x correction pass, July 2026) — two independent
    defects in the previous implementation:

    1. ``has_X = scores.get(X, 0) > 0`` tested mere *presence* of a signal,
       not its materiality. A single 0.05-weight confirmatory indicator
       (P/C Ratio, COT, GDPNow) could therefore single-handedly set
       has_risk_on/has_risk_off even when heavily outweighed by a "neutral"
       reading from a primary indicator, in violation of _pc_indicator's own
       documented contract. Fixed by requiring each bucket's cumulative
       weight to clear ``config.REGIME_MATERIAL_SIGNAL_WEIGHT`` before it
       can gate the tree.
    2. The confidence figure returned to the caller was
       ``scores[dominant] / total_weight`` — the market share of whichever
       bucket happened to be the largest, even when that bucket was NOT the
       one that determined the regime (e.g. "neutral" dominant by weight,
       "risk_on" determining the regime via defect #1). Fixed by reporting
       the normalised margin between the top and second signal instead,
       which collapses toward zero exactly when the vote is contested —
       the previous formula did not.
    """
    if not indicators:
        return "Mixed / Selective", "transitional", 0.0, None

    # Weighted vote
    scores: dict[str, float] = {}
    total_weight = 0.0
    for ind in indicators:
        scores[ind.signal] = scores.get(ind.signal, 0.0) + ind.weight
        total_weight += ind.weight

    if total_weight == 0:
        return "Mixed / Selective", "transitional", 0.0, None

    # Find dominant signal (used only for the internal 0.4 dominance
    # threshold below — kept as pure "market share" for that purpose).
    dominant = max(scores, key=scores.get)
    dominant_score = scores[dominant] / total_weight

    # Confidence actually returned/displayed: margin between the top signal
    # and its runner-up, normalised by total weight. Honest about contested
    # votes (two near-equal opposing buckets -> confidence near zero) in a
    # way the raw market-share figure was not.
    sorted_weights = sorted(scores.values(), reverse=True)
    top1 = sorted_weights[0]
    top2 = sorted_weights[1] if len(sorted_weights) > 1 else 0.0
    confidence = (top1 - top2) / total_weight

    # Map signal combinations to regime names. A bucket only "counts" once
    # its cumulative weight clears the materiality bar (see docstring).
    has_risk_on = scores.get("risk_on", 0) >= C.REGIME_MATERIAL_SIGNAL_WEIGHT
    has_risk_off = scores.get("risk_off", 0) >= C.REGIME_MATERIAL_SIGNAL_WEIGHT
    has_inflation = scores.get("inflation", 0) >= C.REGIME_MATERIAL_SIGNAL_WEIGHT
    has_deflation = scores.get("deflation", 0) >= C.REGIME_MATERIAL_SIGNAL_WEIGHT
    has_dollar_strong = scores.get("dollar_strong", 0) >= C.REGIME_MATERIAL_SIGNAL_WEIGHT
    has_policy_div = scores.get("policy_divergence", 0) >= C.REGIME_MATERIAL_SIGNAL_WEIGHT

    # Decision tree (unchanged branch structure/order; only the guards above
    # and the two return values documented in the docstring changed).
    if has_risk_off and has_risk_on:
        # Contradictory signals
        if dominant_score > 0.4:
            if dominant == "risk_off":
                return "Late Cycle", "risk_off", confidence, "risk_on"
            else:
                return "Mixed / Selective", "transitional", confidence, None
        return "Mixed / Selective", "transitional", confidence, None

    if has_risk_off and not has_risk_on:
        if has_inflation:
            return "Late Cycle", "risk_off", confidence, "risk_on"
        return "Risk-Off", "risk_off", confidence, "risk_on"

    if has_risk_on and not has_risk_off:
        if has_inflation:
            return "Reflation", "transitional", confidence, "risk_off"
        if has_deflation:
            return "Goldilocks", "risk_on", confidence, "risk_off"
        return "Risk-On", "risk_on", confidence, "risk_off"

    if has_policy_div and has_dollar_strong:
        return "Policy Divergence", "policy_divergence", confidence, None

    if has_dollar_strong and not has_risk_on:
        return "Dollar Smile", "policy_divergence", confidence, None

    if has_deflation and not has_risk_on:
        return "Disinflation", "transitional", confidence, "risk_on"

    if has_inflation and not has_risk_on and not has_risk_off:
        return "Reflation", "transitional", confidence, "risk_off"

    return "Mixed / Selective", "transitional", confidence, None


def _build_triggers(regime_name: str, events: list, indicators: list[RegimeIndicator]) -> list[str]:
    """Build a list of events that could trigger a regime change."""
    triggers = []

    # Calendar-based triggers
    critical = [e for e in events if e.priority in ("CRITICAL", "HIGH") and e.is_upcoming]
    for e in critical[:3]:
        triggers.append(
            f"{e.event_name} [{e.currency}] — un beat/miss pourrait décaler le régime "
            f"{'vers Risk-Off' if regime_name in ('Risk-On', 'Goldilocks') else 'vers Risk-On'}."
        )

    # Indicator-based triggers
    vix_ind = next((i for i in indicators if i.name == "VIX"), None)
    if vix_ind and "risk_on" in vix_ind.signal:
        triggers.append("VIX au-dessus de 22 → bascule vers Risk-Off / Late Cycle.")
    elif vix_ind and "risk_off" in vix_ind.signal:
        triggers.append("VIX sous 15 → compression de vol, potentiel retour vers Risk-On.")

    move_ind = next((i for i in indicators if i.name == "MOVE"), None)
    if move_ind and "risk_on" in move_ind.signal:
        triggers.append("MOVE au-dessus de 120 → stress rates, dégradation du régime.")

    return triggers[:5]  # cap at 5 for readability


def _build_narrative(
    regime_name: str,
    indicators: list[RegimeIndicator],
    central_banks: list[CentralBankSnapshot],
    currency_strength: list[CurrencyStrength],
) -> str:
    """Build a narrative explanation connecting indicators to the regime.

    This implements the 'story' chain from the maturation brief:
    Growth → Inflation → Central Banks → Rates → Liquidity → Volatility →
    Sentiment → Flows → Currencies → Assets
    """
    parts = []

    # Growth
    gdp_ind = next((i for i in indicators if i.name == "GDPNow"), None)
    if gdp_ind:
        parts.append(f"Croissance : {gdp_ind.value} ({gdp_ind.note})")
    else:
        parts.append("Croissance : signal indisponible [N/A].")

    # Inflation / Rates
    rates_ind = next((i for i in indicators if i.name == "US10Y"), None)
    if rates_ind:
        parts.append(f"Taux/Inflation : US10Y {rates_ind.value} — {rates_ind.note}")
    else:
        parts.append("Taux/Inflation : signal indisponible [N/A].")

    # Central banks
    cb_names = [cb.name for cb in central_banks if cb.stamp.ok]
    if cb_names:
        parts.append(f"Banques centrales : {', '.join(cb_names)} actives dans l'analyse.")
    else:
        parts.append("Banques centrales : taux non sourcés [N/A].")

    # Volatility
    vol_parts = []
    vix_ind = next((i for i in indicators if i.name == "VIX"), None)
    if vix_ind:
        vol_parts.append(f"VIX {vix_ind.value}")
    move_ind = next((i for i in indicators if i.name == "MOVE"), None)
    if move_ind:
        vol_parts.append(f"MOVE {move_ind.value}")
    pc_ind = next((i for i in indicators if i.name == "P/C Ratio"), None)
    if pc_ind:
        vol_parts.append(pc_ind.value)       # "Eq.P/C 0.69 · Idx.P/C 0.88"
    if vol_parts:
        parts.append(f"Volatilité : {' · '.join(vol_parts)} — régime {regime_name}.")
    else:
        parts.append("Volatilité : non mesurable [N/A].")

    # Currencies
    if currency_strength:
        top = currency_strength[0]
        bottom = currency_strength[-1]
        parts.append(
            f"Devises : {top.currency} le plus fort ({top.score}) · "
            f"{bottom.currency} le plus faible ({bottom.score})."
        )

    # Conclusion
    parts.append(
        f"Conclusion : le régime « {regime_name} » est soutenu par "
        f"{len([i for i in indicators if i.supports])} indicateur(s) "
        f"et fragilisé par {len([i for i in indicators if not i.supports])}."
    )

    return " → ".join(parts)
