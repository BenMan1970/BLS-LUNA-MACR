"""Interpretation Engine — BLUESTAR analytical narrative layer.

This module implements the *interpretation layer* requested in Priority 3.
It does not fetch new data — it interprets the data already collected by the
pipeline and produces coherent analytical narratives.

The engine explains:
  * why the USD is strong or weak
  * why a currency is selected
  * which factors dominate currently
  * which indicators reinforce each other
  * which indicators contradict each other
  * which risks invalidate the scenario

It connects the analytical chain:
  Growth → Inflation → Central Banks → Rates → Liquidity → Volatility →
  Sentiment → Flows → Currencies → Assets
"""
from __future__ import annotations

from typing import Optional

import logging
from dataclasses import dataclass

from . import config as C
from .models import (
    AssetSetup, CentralBankSnapshot, CotPositioning, CurrencyStrength,
    Datum, MarketSnapshot,
)
from .regime_engine import RegimeAssessment

logger = logging.getLogger(__name__)


@dataclass
class FactorLink:
    """A causal link between two factors in the analytical chain."""
    upstream: str       # e.g. "Croissance"
    downstream: str     # e.g. "Inflation"
    mechanism: str      # how upstream drives downstream
    direction: str      # "positive", "negative", "neutral"
    confidence: str     # "high", "medium", "low"


@dataclass
class InterpretationLayer:
    """The full interpretation output for the briefing."""
    # USD analysis
    usd_assessment: str          # why USD is strong/weak
    usd_drivers: list[str]       # key drivers
    
    # Currency selection rationale
    currency_rationale: dict[str, str]   # currency -> why it's selected
    
    # Factor dominance
    dominant_factors: list[str]  # which factors are currently driving markets
    reinforcing_indicators: list[str]    # indicators that agree
    contradicting_indicators: list[str]  # indicators that disagree
    
    # Risk of invalidation
    invalidation_risks: list[str]
    
    # Full narrative chain
    narrative_chain: list[FactorLink]
    narrative_text: str          # the full story as a paragraph
    
    # Asset selection explanation
    asset_explanations: dict[str, str]   # asset -> why it's selected


def build_interpretation(
    market: MarketSnapshot,
    central_banks: list[CentralBankSnapshot],
    currency_strength: list[CurrencyStrength],
    ips: list[CotPositioning],
    regime: RegimeAssessment,
    priority_assets: list[AssetSetup],
    now_utc,
    pc_data: Optional[dict] = None,
) -> InterpretationLayer:
    """Build the full interpretation layer from pipeline data."""
    
    # --- USD Assessment ---
    usd_assessment, usd_drivers = _assess_usd(market, central_banks, currency_strength, regime)
    
    # --- Currency Rationale ---
    currency_rationale = _currency_rationale(currency_strength, central_banks, ips, regime)
    
    # --- Factor Dominance ---
    dominant, reinforcing, contradicting = _factor_analysis(market, central_banks, regime, pc_data)
    
    # --- Invalidation Risks ---
    invalidation = _invalidation_risks(market, ips, regime, priority_assets)
    
    # --- Narrative Chain ---
    chain, narrative_text = _build_narrative_chain(market, central_banks, currency_strength, regime, pc_data)
    
    # --- Asset Explanations ---
    asset_explanations = _asset_explanations(priority_assets, currency_strength, ips, regime)
    
    return InterpretationLayer(
        usd_assessment=usd_assessment,
        usd_drivers=usd_drivers,
        currency_rationale=currency_rationale,
        dominant_factors=dominant,
        reinforcing_indicators=reinforcing,
        contradicting_indicators=contradicting,
        invalidation_risks=invalidation,
        narrative_chain=chain,
        narrative_text=narrative_text,
        asset_explanations=asset_explanations,
    )


# ---------------------------------------------------------------------------
# USD Assessment
# ---------------------------------------------------------------------------
def _usd_rate_driver(central_banks: list[CentralBankSnapshot]) -> str:
    """Factor 1: Fed rate carry driver."""
    fed = next((cb for cb in central_banks if cb.name == "FED"), None)
    if fed and fed.stamp.ok:
        try:
            fed_rate = float(fed.rate_display.replace("%", "").replace(",", ".").strip().split("–")[0])
            if fed_rate > 3.0:
                return f"Taux Fed élevés ({fed_rate:.2f}%) — soutien au USD via le carry."
            if fed_rate < 1.0:
                return f"Taux Fed bas ({fed_rate:.2f}%) — pression sur le USD."
        except (ValueError, IndexError):
            pass
    return ""


def _usd_vix_driver(vix: Datum) -> str:
    """Factor 2: Risk sentiment driver."""
    if not vix.available:
        return ""
    if vix.value >= C.VIX_RISK_OFF_MIN:
        return f"VIX élevé ({vix.value:.1f}) — flight-to-safety vers le USD (refuge)."
    if vix.value <= C.VIX_RISK_ON_MAX:
        return f"VIX bas ({vix.value:.1f}) — appétit de risque réduit la demande de USD refuge."
    return ""


def _usd_yield_driver(us10y: Datum) -> str:
    """Factor 3: Yield attraction driver."""
    if not us10y.available:
        return ""
    if us10y.value > 4.0:
        return f"US10Y élevé ({us10y.value:.2f}%) — attractivité des rendements USD."
    if us10y.value < 3.0:
        return f"US10Y bas ({us10y.value:.2f}%) — rendements USD moins attractifs."
    return ""


def _usd_dxy_driver(dxy: Datum) -> str:
    """Factor 4: DXY level driver."""
    if not dxy.available:
        return ""
    d = dxy.value
    if d > 105:
        return f"DXY à {d:.1f} — dollar structurellement fort (Dollar Smile potentiel)."
    if d < 100:
        return f"DXY à {d:.1f} — dollar faible, soutien aux actifs non-USD."
    return f"DXY à {d:.1f} — dollar dans sa fourchette neutre."


def _usd_strength_driver(usd_strength) -> str:
    """Factor 5: Currency strength ranking driver."""
    if not usd_strength:
        return ""
    if usd_strength.score >= 60:
        return f"USD classé fort (score {usd_strength.score}/100) dans le ranking multi-devises."
    if usd_strength.score <= 40:
        return f"USD classé faible (score {usd_strength.score}/100) dans le ranking multi-devises."
    return f"USD neutre (score {usd_strength.score}/100)."


def _assess_usd(
    market: MarketSnapshot,
    central_banks: list[CentralBankSnapshot],
    currency_strength: list[CurrencyStrength],
    regime: RegimeAssessment,
) -> tuple[str, list[str]]:
    """Explain why the USD is strong or weak."""
    usd_strength = next((r for r in currency_strength if r.currency == "USD"), None)

    drivers = [
        d for d in [
            _usd_rate_driver(central_banks),
            _usd_vix_driver(market.gauge("VIX")),
            _usd_yield_driver(market.gauge("US10Y")),
            _usd_dxy_driver(market.gauge("DXY")),
            _usd_strength_driver(usd_strength),
        ] if d
    ]

    if not drivers:
        return "Données insuffisantes pour évaluer le USD [N/A].", []

    strong_kw = ("fort", "élevé", "soutien", "attractiv")
    weak_kw = ("faible", "bas", "pression", "réduit")
    strong_signals = sum(1 for d in drivers if any(k in d for k in strong_kw))
    weak_signals = sum(1 for d in drivers if any(k in d for k in weak_kw))

    if strong_signals > weak_signals:
        direction = "Le USD est actuellement soutenu par"
    elif weak_signals > strong_signals:
        direction = "Le USD est actuellement sous pression par"
    else:
        direction = "Le USD est dans une position neutre, tiraillé entre"

    assessment = f"{direction} {len(drivers)} facteur(s) : " + " · ".join(drivers[:4])
    return assessment, drivers


# ---------------------------------------------------------------------------
# Currency Rationale
# ---------------------------------------------------------------------------
def _currency_rationale(
    currency_strength: list[CurrencyStrength],
    central_banks: list[CentralBankSnapshot],
    ips: list[CotPositioning],
    regime: RegimeAssessment,
) -> dict[str, str]:
    """Explain why each currency is ranked where it is."""
    rationale = {}
    cb_by_ccy = {}
    for cb in central_banks:
        for name, flag, ccy in [("FED", "🇺🇸", "USD"), ("BCE", "🇪🇺", "EUR"), 
                                   ("BoJ", "🇯🇵", "JPY"), ("BoE", "🇬🇧", "GBP")]:
            if cb.name == name:
                cb_by_ccy[ccy] = cb
    
    ips_by_ccy = {r.currency: r for r in ips}
    
    for r in currency_strength:
        reasons = []
        
        # CB stance
        cb = cb_by_ccy.get(r.currency)
        if cb and cb.stamp.ok:
            bias_text = cb.bias_interpretation.lower()
            if "hawkish" in bias_text:
                reasons.append("banque centrale hawkish")
            elif "dovish" in bias_text:
                reasons.append("banque centrale dovish")
            else:
                reasons.append("banque centrale neutre")
        
        # Regime effect
        if regime.category == "risk_off" and r.currency in C.SAFE_HAVENS:
            reasons.append("refuge en régime risk-off")
        elif regime.category == "risk_on" and r.currency in C.SAFE_HAVENS:
            reasons.append("refuge délaissé en régime risk-on")
        elif regime.category == "risk_on" and r.currency not in C.SAFE_HAVENS:
            reasons.append("devise pro-cyclique favorisée en régime risk-on")
        
        # Positioning
        ips_row = ips_by_ccy.get(r.currency)
        if ips_row and ips_row.is_extreme:
            if ips_row.ips_score <= 20:
                reasons.append(f"positionnement extrême short (IPS {ips_row.ips_score}) — risque de squeeze haussier")
            else:
                reasons.append(f"positionnement extrême long (IPS {ips_row.ips_score}) — risque de squeeze baissier")
        
        # Score interpretation
        if r.score >= 70:
            reasons.append(f"score de force élevé ({r.score}/100)")
        elif r.score <= 30:
            reasons.append(f"score de force faible ({r.score}/100)")
        
        rationale[r.currency] = " · ".join(reasons) if reasons else "positionnement neutre, pas de catalyseur directionnel clair."
    
    return rationale


# ---------------------------------------------------------------------------
# Factor Analysis
# ---------------------------------------------------------------------------
def _factor_analysis(
    market: MarketSnapshot,
    central_banks: list[CentralBankSnapshot],
    regime: RegimeAssessment,
    pc_data: Optional[dict] = None,
) -> tuple[list[str], list[str], list[str]]:
    """Identify dominant factors, reinforcing and contradicting indicators."""
    dominant = []
    reinforcing = []
    contradicting = []
    
    # Use the regime's supporting/contradicting indicators
    for ind in regime.supporting:
        reinforcing.append(f"{ind.name} = {ind.value} : {ind.note}")
        if ind.weight >= 0.15:
            dominant.append(f"{ind.name} ({ind.value}) — {ind.note}")
    
    for ind in regime.contradicting:
        contradicting.append(f"{ind.name} = {ind.value} : {ind.note}")
    
    # Add cross-factor reinforcement checks
    vix = market.gauge("VIX")
    move = market.gauge("MOVE")
    if vix.available and move.available:
        if vix.value < 18 and move.value < 100:
            reinforcing.append("VIX et MOVE concordent : volatilité comprimée sur actions et taux.")
        elif vix.value > 22 and move.value > 120:
            reinforcing.append("VIX et MOVE concordent : stress volatil sur actions et taux.")
        elif (vix.value < 18) != (move.value < 100):
            contradicting.append("VIX et MOVE divergent : tension sur un marché, calme sur l'autre.")
    
    # P/C sentiment — enrichit reinforcing_indicators (S7) ──────────────────
    if pc_data is not None:
        equity    = pc_data.get("equity") or {}
        index_pc  = pc_data.get("index")  or {}
        composite = pc_data.get("composite_signal", "")
        eq_ma     = equity.get("ma_5d")
        idx_ma    = index_pc.get("ma_5d")
        stale     = pc_data.get("stale", False)
        if composite and eq_ma is not None and idx_ma is not None:
            stale_note = " [données périmées]" if stale else ""
            reinforcing.append(
                f"P/C Ratio : Eq.MA5j {eq_ma} · Idx.MA5j {idx_ma}"
                f" — {composite}{stale_note}."
            )

    return dominant, reinforcing, contradicting


# ---------------------------------------------------------------------------
# Invalidation Risks
# ---------------------------------------------------------------------------
def _invalidation_risks(
    market: MarketSnapshot,
    ips: list[CotPositioning],
    regime: RegimeAssessment,
    priority_assets: list[AssetSetup],
) -> list[str]:
    """Identify risks that could invalidate the current analysis."""
    risks = []
    
    # Regime transition triggers
    for trigger in regime.transition_triggers:
        risks.append(trigger)
    
    # Positioning squeeze
    extreme_ips = [r for r in ips if r.is_extreme]
    for r in extreme_ips:
        # Check if this currency is in any priority asset
        for asset in priority_assets:
            ccys = C.INSTRUMENT_CCYS.get(asset.asset, ())
            if r.currency in ccys:
                risks.append(
                    f"{r.currency} en positionnement extrême (IPS {r.ips_score}) "
                    f"et impliqué dans {asset.asset} — risque de squeeze inverse."
                )
                break
    
    # Stale data risk
    vix = market.gauge("VIX")
    if not vix.available:
        risks.append("VIX indisponible — régime de volatilité non évaluable, conviction réduite.")
    
    # Correlation risk: all setups same direction
    if priority_assets:
        all_long = all(a.action_class == "long" for a in priority_assets)
        all_short = all(a.action_class == "short" for a in priority_assets)
        if all_long and len(priority_assets) > 1:
            risks.append(
                f"Tous les setups sont LONG ({len(priority_assets)}) — "
                "concentration directionnelle, pas de diversification de biais."
            )
        elif all_short and len(priority_assets) > 1:
            risks.append(
                f"Tous les setups sont SHORT ({len(priority_assets)}) — "
                "concentration directionnelle, pas de diversification de biais."
            )
    
    return risks[:6]  # cap at 6


# ---------------------------------------------------------------------------
# Narrative Chain
# ---------------------------------------------------------------------------
def _growth_link(market: MarketSnapshot) -> FactorLink:
    """Growth → Inflation link."""
    gdp = market.gauge("GDP_NOWCAST")
    growth_val = f"{gdp.value:.1f}%" if gdp.available else "[N/A]"
    if gdp.available and gdp.value > 2.0:
        direction, mech = "positive", "croissance soutenue alimente les pressions inflationnistes"
    elif gdp.available and gdp.value < 1.0:
        direction, mech = "negative", "croissance faible réduit les pressions inflationnistes"
    else:
        direction, mech = "neutral", "croissance neutre, inflation stable"
    return FactorLink("Croissance", "Inflation", f"GDPNow {growth_val} — {mech}",
                      direction, "high" if gdp.available else "low")


def _inflation_link(market: MarketSnapshot) -> FactorLink:
    """Inflation → Central Banks link."""
    us10y = market.gauge("US10Y")
    infl_val = f"US10Y {us10y.value:.2f}%" if us10y.available else "[N/A]"
    if us10y.available and us10y.value > 4.0:
        direction, mech = "positive", "taux longs élevés poussent les BC au resserrement"
    elif us10y.available and us10y.value < 3.5:
        direction, mech = "negative", "taux longs bas donnent de la latitude aux BC"
    else:
        direction, mech = "neutral", "taux neutres, BC en attente"
    return FactorLink("Inflation", "Banques Centrales", f"{infl_val} — {mech}",
                      direction, "high" if us10y.available else "low")


def _cb_link(central_banks: list[CentralBankSnapshot]) -> tuple[FactorLink, str]:
    """Central Banks → Rates link. Returns (link, cb_dir)."""
    fed = next((cb for cb in central_banks if cb.name == "FED"), None)
    cb_dir, cb_note = "neutral", "BC en attente"
    if fed and fed.stamp.ok:
        bias = fed.bias_interpretation.lower()
        if "hawkish" in bias:
            cb_dir, cb_note = "positive", "Fed hawkish — soutien aux taux courts et au USD"
        elif "dovish" in bias:
            cb_dir, cb_note = "negative", "Fed dovish — pression sur les taux courts et le USD"
    link = FactorLink("Banques Centrales", "Taux", cb_note, cb_dir,
                      "high" if fed and fed.stamp.ok else "low")
    return link, cb_dir


def _liquidity_link(cb_dir: str) -> FactorLink:
    """Rates → Liquidity link."""
    policy = {"positive": "restrictive", "negative": "accommodante", "neutral": "neutre"}[cb_dir]
    return FactorLink("Taux", "Liquidité",
                      f"Politique monétaire {policy} — impact sur la liquidité disponible",
                      cb_dir, "medium")


def _volatility_link(
    market: MarketSnapshot,
    pc_data: Optional[dict] = None,
) -> FactorLink:
    """Liquidity → Volatility link. Enriched with CBOE P/C flow (C1)."""
    vix = market.gauge("VIX")
    vol_val = f"VIX {vix.value:.1f}" if vix.available else "[N/A]"
    if vix.available and vix.value < 18:
        direction, mech = "negative", "liquidité abondante comprime la vol"
    elif vix.available and vix.value > 22:
        direction, mech = "positive", "liquidité restreinte augmente la vol"
    else:
        direction, mech = "neutral", "vol modérée"
    # Enrich with options flow when available (additive — never alters direction)
    if pc_data is not None:
        composite = pc_data.get("composite_signal", "")
        eq_ma  = (pc_data.get("equity") or {}).get("ma_5d")
        idx_ma = (pc_data.get("index")  or {}).get("ma_5d")
        if composite and eq_ma is not None and idx_ma is not None:
            mech += (f" · Options flow : Eq.P/C {eq_ma} / Idx.P/C {idx_ma}"
                     f" ({composite})")
    return FactorLink("Liquidité", "Volatilité", f"{vol_val} — {mech}",
                      direction, "high" if vix.available else "low")


def _sentiment_link(regime: RegimeAssessment) -> FactorLink:
    """Volatility → Sentiment link."""
    cat_map = {"risk_on": ("positive", "appétit de risque"),
               "risk_off": ("negative", "aversion au risque"),
               "transitional": ("neutral", "sentiment mixte"),
               "policy_divergence": ("neutral", "sentiment mixte")}
    direction, sentiment = cat_map.get(regime.category, ("neutral", "sentiment mixte"))
    return FactorLink("Volatilité", "Sentiment",
                      f"Régime « {regime.name} » — {sentiment}", direction, "medium")


def _flows_link(regime: RegimeAssessment) -> FactorLink:
    """Sentiment → Flows link."""
    flow_map = {"risk_on": ("positive", "Flux vers actifs risqués et devises pro-cycliques"),
                "risk_off": ("negative", "Flux vers refuges (USD, JPY, CHF, Or)"),
                "transitional": ("neutral", "Flux indécis, sélectifs"),
                "policy_divergence": ("neutral", "Flux indécis, sélectifs")}
    direction, flow = flow_map.get(regime.category, ("neutral", "Flux indécis, sélectifs"))
    return FactorLink("Sentiment", "Flux", flow, direction, "medium")


def _currencies_link(currency_strength: list[CurrencyStrength]) -> FactorLink:
    """Flows → Currencies link."""
    top_ccy = currency_strength[0] if currency_strength else None
    bottom_ccy = currency_strength[-1] if currency_strength else None
    ccy_note = "—"
    if top_ccy and bottom_ccy:
        ccy_note = f"{top_ccy.currency} favorisé ({top_ccy.score}) · {bottom_ccy.currency} délaissé ({bottom_ccy.score})"
    direction = ("positive" if top_ccy and top_ccy.score >= 60
                 else "negative" if top_ccy and top_ccy.score <= 40
                 else "neutral")
    return FactorLink("Flux", "Devises", ccy_note, direction, "medium")


def _build_narrative_chain(
    market: MarketSnapshot,
    central_banks: list[CentralBankSnapshot],
    currency_strength: list[CurrencyStrength],
    regime: RegimeAssessment,
    pc_data: Optional[dict] = None,
) -> tuple[list[FactorLink], str]:
    """Build the causal chain: Growth → Inflation → CB → Rates → Liquidity →
    Vol → Sentiment → Flows → Currencies → Assets."""
    cb_link, cb_dir = _cb_link(central_banks)

    chain = [
        _growth_link(market),
        _inflation_link(market),
        cb_link,
        _liquidity_link(cb_dir),
        _volatility_link(market, pc_data),   # C1: P/C enrichi la chaîne
        _sentiment_link(regime),
        _flows_link(regime),
        _currencies_link(currency_strength),
        FactorLink("Devises", "Actifs",
                   "Les setups prioritaires exploitent les différentiels de force identifiés ci-dessus",
                   "neutral", "medium"),
    ]

    narrative_text = ". ".join(
        f"{link.upstream} → {link.downstream}: {link.mechanism}" for link in chain
    ) + "."
    return chain, narrative_text


# ---------------------------------------------------------------------------
# Asset Explanations
# ---------------------------------------------------------------------------
def _asset_explanations(
    priority_assets: list[AssetSetup],
    currency_strength: list[CurrencyStrength],
    ips: list[CotPositioning],
    regime: RegimeAssessment,
) -> dict[str, str]:
    """Explain why each priority asset is selected."""
    explanations = {}
    smap = {r.currency: r.score for r in currency_strength}
    ips_by_ccy = {r.currency: r for r in ips}
    
    for asset in priority_assets:
        ccys = C.INSTRUMENT_CCYS.get(asset.asset)
        parts = []
        
        if ccys:
            base, quote = ccys
            base_score = smap.get(base, 50)
            quote_score = smap.get(quote, 50)
            diff = base_score - quote_score
            
            if asset.action_class == "long":
                parts.append(
                    f"LONG {asset.asset} car {base} ({base_score}/100) est plus fort que "
                    f"{quote} ({quote_score}/100), différentiel de +{diff} points."
                )
            elif asset.action_class == "short":
                parts.append(
                    f"SHORT {asset.asset} car {base} ({base_score}/100) est plus faible que "
                    f"{quote} ({quote_score}/100), différentiel de {diff} points."
                )
            
            # Positioning context
            for ccy in ccys:
                ips_row = ips_by_ccy.get(ccy)
                if ips_row and ips_row.is_extreme:
                    if ips_row.ips_score <= 20:
                        parts.append(
                            f"⚠️ {ccy} en capitulation (IPS {ips_row.ips_score}) — "
                            "risque de squeeze inverse contre le biais."
                        )
                    else:
                        parts.append(
                            f"⚠️ {ccy} en crowded (IPS {ips_row.ips_score}) — "
                            "risque de déport inverse."
                        )
                    break
            
            # Regime alignment
            if regime.category == "risk_on" and asset.action_class == "long" and base not in C.SAFE_HAVENS:
                parts.append("Aligné avec le régime risk-on.")
            elif regime.category == "risk_off" and asset.action_class == "long" and base in C.SAFE_HAVENS:
                parts.append("Aligné avec le régime risk-off (refuge).")
            elif regime.category == "risk_off" and asset.action_class == "long" and base not in C.SAFE_HAVENS:
                parts.append("⚠️ Contre le régime risk-off — conviction réduite.")
        else:
            # Non-FX asset
            if regime.category == "risk_off" and asset.asset == "XAU/USD":
                parts.append("Or sélectionné comme refuge en régime risk-off.")
            elif regime.category == "risk_on" and asset.asset in C.INDICES:
                parts.append("Indice sélectionné pour le tilt risk-on.")
        
        explanations[asset.asset] = " ".join(parts) if parts else "Sélection basée sur le score composite."
    
    return explanations
