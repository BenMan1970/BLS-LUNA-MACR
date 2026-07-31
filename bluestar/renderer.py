"""HTML Renderer.

Turns a :class:`BriefingContext` into the final BLUESTAR v8.1 HTML document.
The ``<head>`` (CSS) and the static header are loaded verbatim from the scaffold
templates so the rendering stays pixel-identical to the reference. Every section
is built programmatically from data -- there are no ``{{PLACEHOLDER}}`` tokens in
the output (the validation engine enforces this).
"""
from __future__ import annotations

import html
from pathlib import Path

from .models import AssetSetup, BriefingContext, MacroEvent
from .macro_engine import fr_date, fr_day_name, session_label
from .staleness import build_coverage_report, stale_fields_summary

_TPL_DIR = Path(__file__).parent / "templates"


def _load(name: str) -> str:
    return (_TPL_DIR / name).read_text(encoding="utf-8")


def _e(text: object) -> str:
    """HTML-escape any dynamic text fragment."""
    return html.escape(str(text), quote=True)


_CB_LABEL_STYLE = ("font-size:9px;font-weight:700;color:var(--muted);"
                   "font-family:var(--mono);letter-spacing:.5px")


def _cb_biais_block(cb) -> str:
    """Build the cb-biais inner HTML for a central-bank card.

    CORRECTIF (23/07/2026, retour utilisateur) : la ligne "FAIT ·" ne doit
    plus jamais afficher de [N/A] en production. macro_engine.py renvoie
    désormais ``cb.fact == ""`` quand il n'y a rien de sourcé (au lieu du
    texte "[N/A] — ..." précédent) ; ici, on omet purement et simplement la
    ligne "FAIT ·" (span + texte + <br>) quand c'est le cas, plutôt que de
    rendre un champ vide ou un espace réservé. "BIAIS ·" reste toujours
    affiché : macro_engine.py garantit qu'il retombe au pire sur
    "[N/A] — interprétation à confirmer." (jamais une chaîne vide), donc
    pas de risque de ligne totalement vide ici.
    """
    fait = ""
    if cb.fact:
        fait = (f'<span style="{_CB_LABEL_STYLE}">FAIT ·</span> '
                f'{_e(cb.fact)}<br>')
    return (f'{fait}<span style="{_CB_LABEL_STYLE}">BIAIS ·</span> '
            f'{_e(cb.bias_interpretation)}')


def _stars(n: int) -> str:
    n = max(1, min(5, int(n)))
    return f'<span class="stars-{n}"></span>'


def _cs_source_tag(currency_strength: list) -> str:
    """Return source tag for Currency Strength Ranking footer.

    F-08 (audit 31/07/2026, zero-régression sur le calcul) : le classement
    CB-bias (macro_engine.build_currency_strength_ranking) est TOUJOURS
    calculé pour les 8 devises, puis écrasé devise par devise par
    _oanda_strength_scores() quand Oanda couvre cette devise précise --
    l'écrasement n'est PAS un tout-ou-rien (voir macro_engine.py). L'ancien
    tag binaire ("une seule ligne Oanda -> étiquette 'tout le ranking est
    Oanda' ") sur-affirmait la couverture réelle dès qu'au moins 1/8 devise
    venait d'Oanda -- un cas 1/8 s'affichait identiquement à un cas 8/8.
    Comportement affiché inchangé pour les deux cas purs déjà couverts par
    les golden files existants (0 Oanda -> "[PROXY]" ; 8/8 Oanda ->
    "[Oanda v20 · D1]", chaîne octet-identique à avant). Seul le cas mixte,
    auparavant silencieusement confondu avec le cas 8/8, affiche désormais
    la répartition réelle -- aucun score, ranking ou décision n'est modifié
    par ce patch, seul le libellé de provenance change dans ce cas précis.
    """
    if not currency_strength:
        return "[PROXY]"
    n_total = len(currency_strength)
    n_oanda = sum(1 for r in currency_strength if "Oanda" in (getattr(r, "driver", "") or ""))
    if n_oanda == 0:
        return "[PROXY]"
    if n_oanda == n_total:
        return "[Oanda v20 · D1]"
    return (f"[Oanda v20 · D1 : {n_oanda}/{n_total} devises · "
            f"CB-bias PROXY : {n_total - n_oanda}/{n_total} devises]")


# ---------------------------------------------------------------------------
# Section 1
# ---------------------------------------------------------------------------
def _render_top_card(s: AssetSetup) -> str:
    hdr_cls = "green" if s.color == "green" else "yellow"
    return f"""
      <div class="top-card">
        <div class="top-hdr {hdr_cls}">
          <span class="top-asset">{_e(s.asset)}</span>
          <span style="font-size:13px;color:var(--amber)">{_stars(s.conviction)}</span>
        </div>
        <div class="top-body">
          <div class="top-biais {s.bias_class}">{_e(s.arrow)} {_e(s.bias)} — {_e(s.reason_short)}</div>
          <div class="top-row"><span class="lbl">Achat macro</span><span class="vg">{_e(s.zone_buy)}</span></div>
          <div class="top-row"><span class="lbl">Vente macro</span><span class="vr">{_e(s.zone_sell)}</span></div>
          <div class="top-row"><span class="lbl">Stop macro</span><span class="vr">{_e(s.stop)}</span></div>
          <div class="top-row"><span class="lbl">Expected Move</span><span class="va">±{_e(s.expected_move)}</span></div>
          <div class="top-row"><span class="lbl">IPS / COT</span><span class="va">{_e(s.ips_summary)}</span></div>
          <div class="top-action {s.action_class}">{_e(s.action)}</div>
        </div>
      </div>"""


# Category (regime_engine.RegimeAssessment) -> existing CSS bucket used by
# ctx.regime_class ("regime-on" / "regime-off" / "regime-mix").
_CATEGORY_TO_CSS = {
    "risk_on": "regime-on",
    "risk_off": "regime-off",
    "transitional": "regime-mix",
    "policy_divergence": "regime-mix",
}


def _headline_regime(ctx: BriefingContext) -> tuple[str, str]:
    """Single source of truth for the Section 1 headline (audit fix, problem 1).

    Previously Section 1 always read the VIX-only ``ctx.regime`` while
    Section 6 independently displayed the multi-factor ``regime_assessment``
    — the two could (and did) disagree in the same document (e.g. "MIXTE"
    vs "Reflation"). Section 1 now prefers the multi-factor assessment
    (which itself applies the confidence floor — see regime_engine.py) and
    falls back to the legacy VIX-only regime only when no assessment could
    be computed for this run.
    """
    ra = getattr(ctx, "regime_assessment", None)
    if ra is not None and ra.name:
        return ra.name, _CATEGORY_TO_CSS.get(ra.category, "regime-mix")
    return ctx.regime, ctx.regime_class


def _render_section1(ctx: BriefingContext) -> str:
    vix = ctx.market.gauge("VIX")
    move = ctx.market.gauge("MOVE")
    headline_regime, headline_class = _headline_regime(ctx)
    op_note = ""
    if ctx.operational_note:
        op_note = (f'<div class="abox wait" style="font-size:11px;margin-bottom:14px">'
                   f'<span>⚠️ <span class="bold">NOTE OPÉRATIONNELLE :</span> '
                   f'{_e(ctx.operational_note)}</span></div>')

    if ctx.priority_assets:
        cards = "".join(_render_top_card(s) for s in ctx.priority_assets)
        priority_block = f'<div class="top-grid">{cards}</div>'
    else:
        # AUDIT-ENRICHMENT (15/07/2026): 🛑 (stop sign) read as an alarm in
        # an emoji font, which is the wrong register for a decision-support
        # tool that is explicitly choosing not to force a trade — it's a
        # calm, deliberate "nothing meets the bar today", not an error
        # state. Swapped for 🧭 (compass — "no clear directional read"),
        # same neutral dashed-border box (.no-setup CSS untouched), purely
        # cosmetic, no change to no_setup_reason logic or when this
        # branch fires.
        priority_block = (f'<div class="no-setup"><div class="no-setup-icon">🧭</div>'
                          f'<div class="no-setup-title">Aucun actif ne réunit les critères aujourd\'hui</div>'
                          f'<div class="no-setup-sub">{_e(ctx.no_setup_reason or "")}</div></div>')

    if ctx.avoid_assets:
        avoid_items = "".join(
            f'<div class="avoid-item"><span class="avoid-asset">{_e(a)}</span>'
            f'<span class="avoid-reason">{_e(r)}</span></div>'
            for a, r in ctx.avoid_assets)
    else:
        avoid_items = ('<div class="avoid-item"><span class="avoid-asset">—</span>'
                       '<span class="avoid-reason">Aucun actif explicitement à éviter aujourd\'hui.</span></div>')

    return f"""
<div class="section">
  <div class="sec-hdr"><div class="sec-num">1</div><div class="sec-ttl">Tableau de Bord Exécutif</div><div class="sec-sub">Où agir aujourd'hui — lecture en 30 sec</div></div>
  <div class="sec-body">
    <div class="regime-bar">
      <span class="regime-lbl">Régime du jour</span>
      <span class="regime-val {headline_class}">{_e(headline_regime)}</span>
      <span style="margin-left:auto;font-size:11px;color:var(--muted)">VIX : <span class="mono bold amber">{_e(vix.display)}</span> · MOVE : <span class="mono bold blue">{_e(move.display)}</span> · Depuis {_e(ctx.regime_since)}</span>
    </div>
    {op_note}
    <div class="sub-lbl">🎯 ACTIFS PRIORITAIRES DU JOUR</div>
    {priority_block}
    <div class="sub-lbl">🚫 ÉVITER AUJOURD'HUI</div>
    <div class="avoid-list">{avoid_items}</div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section 3b
# ---------------------------------------------------------------------------
def _render_section3b(ctx: BriefingContext) -> str:
    squeeze_badge = ""
    if ctx.squeeze_currency:
        squeeze_badge = (f'<span class="badge badge-red" style="margin-left:6px">'
                         f'⚠️ SQUEEZE RISK {_e(ctx.squeeze_currency)}</span>')
    cs_rows = "".join(
        f'<div class="rank-row"><span class="rank-lbl">{i+1}. {_e(r.currency)}</span>'
        f'<div class="rank-bar"><div class="rank-fill {r.css_class}" style="width:{r.score}%"></div></div>'
        f'<span class="rank-val {r.css_class}">{r.score}</span></div>'
        for i, r in enumerate(ctx.currency_strength))

    if ctx.ips_scores:
        ips_rows = "".join(
            f'<div class="rank-row"><span class="rank-lbl">{_e(r.currency)}</span>'
            f'<div class="rank-bar"><div class="rank-fill {"crowd" if r.is_extreme else "norm"}" style="width:{r.ips_score}%"></div></div>'
            f'<span class="rank-val {"weak" if r.is_extreme else "neutral"}">{r.ips_score} {_e(r.ips_label)} ({_e(r.delta_week)}, {_e(r.momentum)})</span></div>'
            for r in ctx.ips_scores)
    else:
        ips_rows = ('<div class="rank-row"><span class="rank-lbl">—</span>'
                    '<div class="rank-bar"><div class="rank-fill norm" style="width:0%"></div></div>'
                    '<span class="rank-val neutral">[N/A] — aucune donnée COT chargée (saisir en overrides)</span></div>')

    alert = ""
    if ctx.positioning_alert:
        alert = (f'<div class="abox wait" style="font-size:12px;margin-top:12px">'
                 f'<span>⚠️ <span class="bold">POSITIONING ALERT :</span> '
                 f'{_e(ctx.positioning_alert)}</span></div>')

    return f"""
<div class="section">
  <div class="sec-hdr"><div class="sec-num">M</div><div class="sec-ttl">Macro Overlay</div><div class="sec-sub">Contexte institutionnel — colore le jugement, ne filtre pas les setups</div></div>
  <div class="sec-body">
    <div class="brief">
      <div class="brief-grid">
        <span class="brief-lbl">Macro Theme</span>
        <span>{_e(ctx.macro_theme)} <span style="font-size:10px;color:var(--muted)">{_e(ctx.macro_theme_src)}</span></span>
        <span class="brief-lbl">COT &amp; Positioning</span>
        <span>{_e(ctx.cot_summary)} {squeeze_badge}<span style="font-size:10px;color:var(--muted)"> [{_e(ctx.cot_date)}]</span></span>
        <span class="brief-lbl">DXY Context</span>
        <span>{_e(ctx.dxy_context)} <span style="font-size:10px;color:var(--muted)">{_e(ctx.dxy_src)}</span></span>
        <span class="brief-lbl">Volatility</span>
        <span>{_e(ctx.vol_regime)} → <span style="font-style:italic">{_e(ctx.vol_implication)}</span></span>
        <span class="brief-lbl">Correlation</span>
        <span class="mono" style="font-size:11px">{_e(ctx.correlation_summary)} <span style="font-size:10px;color:var(--muted)">[PROXY · échantillon court]</span></span>
        <span class="brief-lbl">Liquidity &amp; Flow</span>
        <span>{_e(ctx.liquidity_flow)}</span>
      </div>
    </div>
    <div class="sub-lbl">💪 CURRENCY STRENGTH RANKING — 8 devises majeures</div>
    <div style="font-family:var(--mono);font-size:11px">
      {cs_rows}
      <div style="font-size:10px;color:var(--muted);margin-top:4px">Score relatif · 0–100 <span class="amber">{_e(_cs_source_tag(ctx.currency_strength))}</span></div>
    </div>
    <div class="sub-lbl">📊 INSTITUTIONAL POSITIONING SCORE (IPS 0–100) — Non-Commercials CFTC</div>
    <div style="font-family:var(--mono);font-size:11px">
      {ips_rows}
      <div style="font-size:10px;color:var(--muted);margin-top:4px">Lecture : &gt;80 = Crowded · 20–80 = Normal · &lt;20 = Capitulation. <span class="amber">[{_e(ctx.cot_date)}]</span></div>
    </div>
    {alert}
  </div>
</div>"""
