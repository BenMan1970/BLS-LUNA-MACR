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

_TPL_DIR = Path(__file__).parent / "templates"


def _load(name: str) -> str:
    return (_TPL_DIR / name).read_text(encoding="utf-8")


def _e(text: object) -> str:
    """HTML-escape any dynamic text fragment."""
    return html.escape(str(text), quote=True)


def _stars(n: int) -> str:
    n = max(1, min(5, int(n)))
    filled = "★" * n
    empty = "☆" * (5 - n)
    return f'<span class="mono" style="color:var(--royal);letter-spacing:2px">{filled}{empty}</span>'


def _cs_source_tag(currency_strength: list) -> str:
    """Return source tag for Currency Strength Ranking footer.

    If ANY row carries the Oanda driver, the whole ranking is Oanda-sourced.
    Otherwise falls back to [PROXY] (CB-bias).
    """
    if not currency_strength:
        return "[PROXY]"
    if any("Oanda" in (getattr(r, "driver", "") or "") for r in currency_strength):
        return "[Oanda v20 · D1]"
    return "[PROXY]"


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


def _render_section1(ctx: BriefingContext) -> str:
    vix = ctx.market.gauge("VIX")
    move = ctx.market.gauge("MOVE")
    op_note = ""
    if ctx.operational_note:
        op_note = (f'<div class="abox wait" style="font-size:11px;margin-bottom:14px">'
                   f'<span>⚠️ <span class="bold">NOTE OPÉRATIONNELLE :</span> '
                   f'{_e(ctx.operational_note)}</span></div>')

    if ctx.priority_assets:
        cards = "".join(_render_top_card(s) for s in ctx.priority_assets)
        priority_block = f'<div class="top-grid">{cards}</div>'
    else:
        priority_block = (f'<div class="no-setup"><div class="no-setup-icon">🛑</div>'
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
      <span class="regime-val {ctx.regime_class}">{_e(ctx.regime)}</span>
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
# Section 2
# ---------------------------------------------------------------------------
def _render_event_high(e: MacroEvent, scn: dict) -> str:
    date_str = e.date_display if hasattr(e, 'date_display') and e.date_display else ""
    time_label = f"{_e(e.time_display)}" if not date_str else f"{_e(date_str)} · {_e(e.time_display)}"
    return f"""
    <div class="event high">
      <div class="event-hdr">
        <span class="ev-time">{time_label}</span>
        <span class="ev-name">{_e(e.event_name)} [{_e(e.currency)}]</span>
        <span class="ev-tag"><span class="badge badge-red">🔴 ÉLEVÉ</span></span>
      </div>
      <div class="ev-prev">Précédent : <span class="mono bold">{_e(scn.get('prev','—'))}</span> &nbsp;·&nbsp; Consensus : <span class="mono bold amber">{_e(scn.get('cons','—'))}</span></div>
      <div class="ev-scen">
        <div class="s-beat"><div class="s-lbl-b">✅ BEAT</div><div class="s-body">{_e(scn.get('beat_impact',''))}</div><div class="s-act">→ {_e(scn.get('beat_action',''))}</div></div>
        <div class="s-miss"><div class="s-lbl-m">❌ MISS</div><div class="s-body">{_e(scn.get('miss_impact',''))}</div><div class="s-act">→ {_e(scn.get('miss_action',''))}</div></div>
      </div>
      <div class="ev-conseil">💡 CONSEIL : {_e(scn.get('advice',''))}</div>
    </div>"""


def _render_event_medium(e: MacroEvent) -> str:
    pairs = " · ".join(e.pairs_affected[:4]) if e.pairs_affected else "—"
    date_str = e.date_display if hasattr(e, 'date_display') and e.date_display else ""
    time_label = f"{_e(e.time_display)}" if not date_str else f"{_e(date_str)} · {_e(e.time_display)}"
    return f"""
    <div class="event medium">
      <div class="event-hdr">
        <span class="ev-time">{time_label}</span>
        <span class="ev-name">{_e(e.event_name)} [{_e(e.currency)}]</span>
        <span class="ev-tag"><span class="badge badge-yellow">🟡 ÉLEVÉ · &gt;48h</span></span>
        <span style="margin-left:auto;font-size:11px;color:var(--muted)">{_e(pairs)}</span>
      </div>
    </div>"""


def _render_section2(ctx: BriefingContext) -> str:
    sec_title = "Catalyseurs du Jour"
    if ctx.is_live_session:
        sec_sub = "News qui peuvent invalider un setup"
    else:
        sec_sub = "Calendrier macro — fenêtre glissante 72h (marché fermé)"

    if not ctx.catalysts_high and not ctx.catalysts_medium:
        body = ('<div class="abox wait" style="font-size:12px"><span>Aucun catalyseur '
                'high-impact à venir dans la fenêtre du calendrier [Forex Factory].</span></div>')
    else:
        highs = "".join(_render_event_high(e, ctx.catalyst_scenarios.get(e.datetime_utc + e.event_name, {}))
                        for e in ctx.catalysts_high)
        meds = "".join(_render_event_medium(e) for e in ctx.catalysts_medium)
        body = highs + meds
    return f"""
<div class="section">
  <div class="sec-hdr"><div class="sec-num">2</div><div class="sec-ttl">{_e(sec_title)}</div><div class="sec-sub">{_e(sec_sub)}</div></div>
  <div class="sec-body">{body}
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section 3
# ---------------------------------------------------------------------------
def _kpi(label: str, value: str, sub: str, cls: str = "amber") -> str:
    return (f'<div class="kpi {cls}"><div class="kpi-lbl">{_e(label)}</div>'
            f'<div class="kpi-val {cls}">{_e(value)}</div>'
            f'<div class="kpi-sub">{_e(sub)}</div></div>')


def _render_fed(cb) -> str:
    pause = cb.pause_pct if cb.pause_pct is not None else 0
    cut = cb.cut_pct if cb.cut_pct is not None else 0
    hike = cb.hike_pct if cb.hike_pct is not None else 0
    proba = ""
    if cb.pause_pct is not None or cb.cut_pct is not None or cb.hike_pct is not None:
        proba = f"""
        <div class="proba-wrap">
          <div class="proba-bar"><div class="pb-pause" style="width:{pause}%"></div><div class="pb-cut" style="width:{cut}%"></div><div class="pb-hike" style="width:{hike}%"></div></div>
          <div class="proba-lbls"><span class="pl-p">Pause {pause}%</span><span class="pl-c">Baisse {cut}%</span><span class="pl-h">Hausse {hike}%</span></div>
        </div>"""
    return f"""
      <div class="cb">
        <div class="cb-flag">{cb.flag}</div><div class="cb-name">{_e(cb.name)}</div><div class="cb-rate">{_e(cb.rate_display)}</div>
        {proba}
        <div class="cb-next">Prochaine : {_e(cb.next_meeting)}</div>
        <div class="cb-biais"><span style="font-size:9px;font-weight:700;color:var(--muted);font-family:var(--mono);letter-spacing:.5px">FAIT ·</span> {_e(cb.fact)}<br><span style="font-size:9px;font-weight:700;color:var(--muted);font-family:var(--mono);letter-spacing:.5px">BIAIS ·</span> {_e(cb.bias_interpretation)}</div>
      </div>"""


def _render_cb_simple(cb) -> str:
    return f"""
      <div class="cb"><div class="cb-flag">{cb.flag}</div><div class="cb-name">{_e(cb.name)}</div><div class="cb-rate">{_e(cb.rate_display)}</div><div class="cb-biais"><span style="font-size:9px;font-weight:700;color:var(--muted);font-family:var(--mono);letter-spacing:.5px">FAIT ·</span> {_e(cb.fact)}<br><span style="font-size:9px;font-weight:700;color:var(--muted);font-family:var(--mono);letter-spacing:.5px">BIAIS ·</span> {_e(cb.bias_interpretation)}</div><div class="cb-next">Prochaine : {_e(cb.next_meeting)}</div></div>"""


def _render_section3(ctx: BriefingContext) -> str:
    m = ctx.market
    g = m.gauge
    kpis = "".join([
        _kpi("VIX", g("VIX").display, g("VIX").trend or "tendance n/d", "amber"),
        _kpi("MOVE Index", g("MOVE").display, g("MOVE").trend or "calme/ n/d", "blue"),
        _kpi("DXY", g("DXY").display, g("DXY").trend or "n/d", "green"),
        _kpi("US10Y", (g("US10Y").display + "%") if g("US10Y").available else "N/A",
             g("US10Y").trend or "n/d", "amber"),
        _kpi("Or XAU", g("XAU/USD").display, g("XAU/USD").trend or "n/d", "amber"),
        _kpi("GDP Nowcast", g("GDP_NOWCAST").display,
             g("GDP_NOWCAST").trend or g("GDP_NOWCAST").stamp.render(), "blue"),
        _kpi("Surprise Idx", g("SURPRISE_IDX").display,
             g("SURPRISE_IDX").trend or g("SURPRISE_IDX").stamp.render(), "amber"),
    ])
    fed = ctx.central_banks[0] if ctx.central_banks else None
    cb_blocks = (_render_fed(fed) if fed else "") + \
        "".join(_render_cb_simple(cb) for cb in ctx.central_banks[1:])
    return f"""
<div class="section">
  <div class="sec-hdr"><div class="sec-num">3</div><div class="sec-ttl">Contexte Macro & Banques Centrales</div><div class="sec-sub">Le vent de fond — différentiel de taux</div></div>
  <div class="sec-body">
    <div class="kpi-grid">{kpis}</div>
    <div class="sub-lbl">🏦 BANQUES CENTRALES</div>
    <div class="cb-grid">{cb_blocks}
    </div>
    <div class="abox wait" style="font-size:12px">
      <span><span class="bold">📊 DIFFÉRENTIEL DOMINANT :</span> {_e(ctx.diff_dominant)} — {_e(ctx.diff_implication)}</span>
    </div>
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


# ---------------------------------------------------------------------------
# Section 4
# ---------------------------------------------------------------------------
def _render_asset_card(s: AssetSetup) -> str:
    hdr_cls = "green" if s.color == "green" else "yellow"
    return f"""
    <div class="asset">
      <div class="asset-hdr {hdr_cls}">
        <span class="asset-name">{_e(s.asset)}</span>
        <span style="font-size:13px;color:var(--amber)">{_stars(s.conviction)}</span>
        <span class="asset-price">{_e(s.price_display)}</span>
      </div>
      <div class="asset-fields">
        <div><div class="field-lbl">1. Biais fondamental</div><div class="field-val {s.bias_class}">{_e(s.bias)} — {_e(s.reason_macro)}</div></div>
        <div><div class="field-lbl">2. Zone d'achat macro</div><div class="field-val green">{_e(s.zone_buy)} <span style="font-size:9px;color:var(--muted);font-weight:400">{_e(s.origin_buy)}</span></div></div>
        <div><div class="field-lbl">3. Zone de vente macro</div><div class="field-val red">{_e(s.zone_sell)} <span style="font-size:9px;color:var(--muted);font-weight:400">{_e(s.origin_sell)}</span></div></div>
        <div><div class="field-lbl">4. Stop macro</div><div class="field-val red">{_e(s.stop)} <span style="font-size:9px;color:var(--muted);font-weight:400">{_e(s.origin_stop)}</span></div></div>
        <div><div class="field-lbl">5. Expected Move</div><div class="field-val amber">±{_e(s.expected_move)} [{_e(s.em_method)}]</div></div>
        <div><div class="field-lbl">6. Session idéale</div><div class="field-val">{_e(s.session)} — {_e(s.session_reason)}</div></div>
        <div style="grid-column:1/-1"><div class="field-lbl">7. Risque d'invalidation</div><div class="field-val orange">{_e(s.invalidation_risk)} → <span style="font-weight:700">invalide si {_e(s.invalidation_level)}</span></div></div>
        <div><div class="field-lbl">8. Lien Positioning ↔ Setup</div><div class="field-val orange">{_e(s.positioning_link)}</div></div>
        <div><div class="field-lbl">9. Corrélation clé</div><div class="field-val orange">{_e(s.correlation_key)}</div></div>
      </div>
      <div class="asset-action {s.action_class}">{_e(s.arrow)} {_e(s.action)}</div>
    </div>"""


def _render_section4(ctx: BriefingContext) -> str:
    if ctx.priority_assets:
        body = "".join(_render_asset_card(s) for s in ctx.priority_assets)
    else:
        body = (f'<div class="no-setup"><div class="no-setup-icon">🛑</div>'
                f'<div class="no-setup-title">Aucune fiche actif aujourd\'hui</div>'
                f'<div class="no-setup-sub">{_e(ctx.no_setup_reason or "")}</div></div>')
    return f"""
<div class="section">
  <div class="sec-hdr"><div class="sec-num">4</div><div class="sec-ttl">Fiches Actifs — Plan pour TradingView</div><div class="sec-sub">Uniquement actifs 🟢 et 🟡</div></div>
  <div class="sec-body">{body}
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section 5
# ---------------------------------------------------------------------------
def _bear_style(r: str) -> str:
    """Return inline style attribute string for a bear-scenario row."""
    if "Refuges" in r:
        return ' style="color:var(--green);font-weight:700"'
    return ""


def _render_recap_row(s: AssetSetup) -> str:
    dot = "🟢" if s.color == "green" else "🟡"
    biais_col = "green" if s.bias_class == "long" else "red"
    return f"""
          <tr>
            <td class="mono bold">{_e(s.asset)}</td>
            <td>{dot}</td>
            <td><span class="badge badge-{biais_col}">{_e(s.bias)}</span></td>
            <td>{_stars(s.conviction)}</td>
            <td class="mono green sm">{_e(s.zone_buy)}</td>
            <td class="mono red sm">{_e(s.zone_sell)}</td>
            <td class="mono red sm">{_e(s.stop)}</td>
            <td class="mono amber sm">±{_e(s.expected_move)}</td>
            <td class="mono {s.squeeze_class} sm">{_e(s.squeeze_risk)}</td>
            <td class="mono blue sm">{_e(s.risk_reward)}</td>
            <td class="sm bold">{_e(s.action)}</td>
          </tr>"""


def _render_section5(ctx: BriefingContext) -> str:
    rm = ctx.risk_main
    bull_rows = "".join(f'<div class="risk-row">{_e(r)}</div>' for r in ctx.bull.rows)
    bear_rows = "".join(
        f'<div class="risk-row"{_bear_style(r)}>{_e(r)}</div>'
        for r in ctx.bear.rows)
    if ctx.priority_assets:
        recap = "".join(_render_recap_row(s) for s in ctx.priority_assets)
    else:
        recap = ""

    return f"""
<div class="section">
  <div class="sec-hdr"><div class="sec-num">5</div><div class="sec-ttl">Risques & Scénarios d'Invalidation</div><div class="sec-sub">Ce qui peut tout changer aujourd'hui</div></div>
  <div class="sec-body">
    <div class="risk-main">
      <strong>⚠️ RISQUE PRINCIPAL</strong>
      {_e(rm['desc'])} → Si réalisé : <span class="mono bold">{_e(rm['asset'])}</span> vers <span class="mono bold">{_e(rm['level'])}</span>
      <span style="font-size:10px;display:block;margin-top:4px">Probabilité estimée : {_e(rm['proba'])} {_e(rm['source'])}</span>
    </div>
    <div class="risk-grid">
      <div class="risk-bull">
        <div class="risk-ttl">📈 {_e(ctx.bull.title)} — {_e(ctx.bull.proba)}</div>
        <div class="risk-proba">Déclencheur ancré : {_e(ctx.bull.trigger)} {_e(ctx.bull.trigger_source)}</div>
        {bull_rows}
      </div>
      <div class="risk-bear">
        <div class="risk-ttl">📉 {_e(ctx.bear.title)} — {_e(ctx.bear.proba)}</div>
        <div class="risk-proba">Déclencheur ancré : {_e(ctx.bear.trigger)} {_e(ctx.bear.trigger_source)}</div>
        {bear_rows}
      </div>
    </div>
    <div class="abox wait" style="font-size:11px;margin-bottom:14px">
      <span>🔄 <span class="bold">INVALIDATION DU SCÉNARIO PRINCIPAL :</span> {_e(ctx.invalidation_principal)}</span>
    </div>
    <div class="sub-lbl">📊 RÉCAPITULATIF FINAL — VUE DESK</div>
    <div class="tw">
      <table>
        <thead><tr><th>Actif</th><th>Signal</th><th>Biais</th><th>Conviction</th><th>Achat</th><th>Vente</th><th>Stop</th><th>EM</th><th>Squeeze</th><th>R:R</th><th>Action</th></tr></thead>
        <tbody>{recap}
        </tbody>
      </table>
    </div>
    <div style="font-size:10px;color:var(--muted);margin-top:10px;font-family:var(--mono)">
      R:R = ratio reward/risk (reward = distance entrée→objectif, risk = distance entrée→stop). Squeeze Risk = Élevé si IPS&gt;80 ou &lt;20 sur une devise du setup, sinon Modéré/Faible. ATR = Wilder EMA-14 (réconciliable avec MT4/TradingView). Figures COT Non-Commercials : {_e(ctx.cot_date)}.
    </div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section 6 — Market Regime Engine (v9.0)
# ---------------------------------------------------------------------------
def _render_section6_regime(ctx: BriefingContext) -> str:
    """Render the multi-factor regime assessment section."""
    ra = getattr(ctx, 'regime_assessment', None)
    if ra is None:
        return """
<div class="section">
  <div class="sec-hdr"><div class="sec-num">6</div><div class="sec-ttl">Moteur de Régime</div><div class="sec-sub">Identification multi-facteur du régime de marché</div></div>
  <div class="sec-body">
    <div class="abox wait" style="font-size:12px"><span>[N/A] — évaluation de régime indisponible pour cette génération.</span></div>
  </div>
</div>"""

    supporting_rows = ""
    for ind in ra.supporting:
        supporting_rows += (
            f'<div class="rank-row"><span class="rank-lbl">✅ {_e(ind.name)}</span>'
            f'<span style="font-size:11px;color:var(--green)">{_e(ind.value)} — {_e(ind.note)}</span></div>'
        )

    contradicting_rows = ""
    for ind in ra.contradicting:
        contradicting_rows += (
            f'<div class="rank-row"><span class="rank-lbl">❌ {_e(ind.name)}</span>'
            f'<span style="font-size:11px;color:var(--red)">{_e(ind.value)} — {_e(ind.note)}</span></div>'
        )

    trigger_rows = ""
    for t in ra.transition_triggers:
        trigger_rows += f'<div class="risk-row">→ {_e(t)}</div>'

    confidence_pct = int(ra.confidence * 100)
    conf_color = "green" if ra.confidence >= 0.6 else "yellow" if ra.confidence >= 0.3 else "red"

    return f"""
<div class="section">
  <div class="sec-hdr"><div class="sec-num">6</div><div class="sec-ttl">Moteur de Régime</div><div class="sec-sub">Identification multi-facteur du régime de marché</div></div>
  <div class="sec-body">
    <div class="regime-bar">
      <span class="regime-lbl">Régime identifié</span>
      <span class="regime-val">{_e(ra.name)}</span>
      <span style="margin-left:auto;font-size:11px;color:var(--muted)">Confiance : <span class="mono bold {conf_color}">{confidence_pct}%</span></span>
    </div>
    <div class="abox wait" style="font-size:12px;margin-bottom:12px">
      <span>{_e(ra.description)}</span>
    </div>
    <div class="brief">
      <div class="brief-grid" style="grid-template-columns:130px 1fr;gap:6px 8px">
        <div class="brief-lbl">Narratif</div><div style="font-size:11px">{_e(ra.narrative)}</div>
      </div>
    </div>
    <div class="brief">
      <div class="sub-lbl" style="margin-top:0">✅ INDICATEURS DE SOUTIEN</div>
      <div style="font-family:var(--mono);font-size:11px">
        {supporting_rows or '<div class="rank-row"><span class="rank-lbl">—</span><span>Aucun indicateur de soutien.</span></div>'}
      </div>
    </div>
    <div class="brief">
      <div class="sub-lbl" style="margin-top:0">❌ INDICATEURS DE CONTRADICTION</div>
      <div style="font-family:var(--mono);font-size:11px">
        {contradicting_rows or '<div class="rank-row"><span class="rank-lbl">—</span><span>Aucun indicateur contradictoire.</span></div>'}
      </div>
    </div>
    <div class="brief" style="margin-bottom:0">
      <div class="sub-lbl" style="margin-top:0">🔄 DÉCLENCHEURS DE TRANSITION</div>
      <div style="font-family:var(--mono);font-size:11px">
        {trigger_rows or '<div class="risk-row">Aucun déclencheur identifié.</div>'}
      </div>
    </div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Section 7 — Interpretation Engine (v9.0)
# ---------------------------------------------------------------------------
def _render_section7_interpretation(ctx: BriefingContext) -> str:
    """Render the interpretation layer section."""
    interp = getattr(ctx, 'interpretation', None)
    if interp is None:
        return """
<div class="section">
  <div class="sec-hdr"><div class="sec-num">7</div><div class="sec-ttl">Moteur d'Interprétation</div><div class="sec-sub">Pourquoi ce régime, quels facteurs dominent, quels risques</div></div>
  <div class="sec-body">
    <div class="abox wait" style="font-size:12px"><span>[N/A] — couche d'interprétation indisponible pour cette génération.</span></div>
  </div>
</div>"""

    usd_block = (
        f'<div class="abox" style="font-size:12px;margin-bottom:12px">'
        f'<span class="bold">ANALYSE USD :</span> {_e(interp.usd_assessment)}</div>'
    )

    driver_rows = ""
    for d in interp.usd_drivers:
        driver_rows += f'<div class="risk-row">· {_e(d)}</div>'

    dominant_rows = ""
    for f in interp.dominant_factors:
        dominant_rows += f'<div class="risk-row">⭐ {_e(f)}</div>'

    reinforcing_rows = ""
    for r in interp.reinforcing_indicators:
        reinforcing_rows += f'<div class="risk-row">✅ {_e(r)}</div>'

    contradicting_rows = ""
    for c in interp.contradicting_indicators:
        contradicting_rows += f'<div class="risk-row">❌ {_e(c)}</div>'

    risk_rows = ""
    for r in interp.invalidation_risks:
        risk_rows += f'<div class="risk-row">⚠️ {_e(r)}</div>'

    chain_rows = ""
    for link in interp.narrative_chain:
        arrow = "→" if link.direction == "positive" else "←" if link.direction == "negative" else "↔"
        chain_rows += (
            f'<div class="rank-row">'
            f'<span class="rank-lbl">{_e(link.upstream)}</span>'
            f'<span style="font-size:11px">{arrow} {_e(link.downstream)}: {_e(link.mechanism)}</span></div>'
        )

    asset_rows = ""
    for asset, expl in interp.asset_explanations.items():
        asset_rows += (
            f'<div class="rank-row"><span class="rank-lbl">{_e(asset)}</span>'
            f'<span style="font-size:11px">{_e(expl)}</span></div>'
        )

    return f"""
<div class="section">
  <div class="sec-hdr"><div class="sec-num">7</div><div class="sec-ttl">Moteur d'Interprétation</div><div class="sec-sub">Pourquoi ce régime, quels facteurs dominent, quels risques</div></div>
  <div class="sec-body">
    {usd_block}
    <div class="brief">
      <div class="sub-lbl" style="margin-top:0">🔑 FACTEURS DOMINANTS</div>
      <div style="font-family:var(--mono);font-size:11px">
        {dominant_rows or '<div class="risk-row">Aucun facteur dominant identifié.</div>'}
      </div>
    </div>
    <div class="brief">
      <div class="sub-lbl" style="margin-top:0">✅ INDICATEURS QUI SE RENFORCENT</div>
      <div style="font-family:var(--mono);font-size:11px">
        {reinforcing_rows or '<div class="risk-row">Aucun renforcement détecté.</div>'}
      </div>
    </div>
    <div class="brief">
      <div class="sub-lbl" style="margin-top:0">❌ INDICATEURS QUI SE CONTREDISENT</div>
      <div style="font-family:var(--mono);font-size:11px">
        {contradicting_rows or '<div class="risk-row">Aucune contradiction détectée.</div>'}
      </div>
    </div>
    <div class="brief">
      <div class="sub-lbl" style="margin-top:0">🔗 CHAÎNE DE TRANSMISSION MACRO</div>
      <div style="font-family:var(--mono);font-size:11px">
        {chain_rows}
      </div>
    </div>
    <div class="brief">
      <div class="sub-lbl" style="margin-top:0">📋 POURQUOI CES ACTIFS SONT SÉLECTIONNÉS</div>
      <div style="font-family:var(--mono);font-size:11px">
        {asset_rows or '<div class="risk-row">Aucun actif sélectionné.</div>'}
      </div>
    </div>
    <div class="brief" style="margin-bottom:0">
      <div class="sub-lbl" style="margin-top:0">⚠️ RISQUES D'INVALIDATION</div>
      <div style="font-family:var(--mono);font-size:11px">
        {risk_rows or '<div class="risk-row">Aucun risque identifié.</div>'}
      </div>
    </div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Top-level render
# ---------------------------------------------------------------------------
def render_html(ctx: BriefingContext) -> str:
    """Render the complete BLUESTAR briefing HTML for ``ctx``."""
    head = _load("scaffold_head.html").replace("{{DATE}}", fr_date(ctx.generated_cet))
    header = _load("scaffold_header.html")
    label, _ = session_label(ctx.generated_cet)
    header = (header
              .replace("{{JOUR}}", fr_day_name(ctx.generated_cet))
              .replace("{{DATE}}", fr_date(ctx.generated_cet))
              .replace("{{HEURE}}", f"{ctx.generated_cet:%H:%M}")
              .replace("{{SESSION_LABEL}}", label))

    body = (
        '<div class="wrap">'
        + _render_section1(ctx)
        + _render_section2(ctx)
        + _render_section3(ctx)
        + _render_section3b(ctx)
        + _render_section4(ctx)
        + _render_section5(ctx)
        + _render_section6_regime(ctx)
        + _render_section7_interpretation(ctx)
        + '</div><!-- /wrap -->'
    )
    footer = (f'<div class="footer">CONFIDENTIEL — BLUESTAR SYSTEM · FX INSTITUTIONAL DESK · '
              f'Macro_Briefing_v8_{ctx.generated_cet:%d-%m-%Y}.pdf · '
              f'{fr_date(ctx.generated_cet)} {ctx.generated_cet:%H:%M} CET</div>')

    return head + "\n" + header + "\n" + body + "\n" + footer + "\n</div><!-- /page -->\n</body>\n</html>"
