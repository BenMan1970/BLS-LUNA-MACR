"""BLUESTAR Macro Briefing -- Streamlit application (production entrypoint).

Run with::

    streamlit run app.py

The app orchestrates the Data Integrity Layer (Forex Factory calendar + market
data), the macro engine, the validation engine and the HTML renderer. It runs
with **zero API keys**; any field without a keyless source degrades to
``[N/A]`` / ``[PROXY]`` and can be supplied via the sidebar "manual overrides"
JSON. The generated HTML embeds a ``window.print()`` button for browser PDF.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
import streamlit as st
import base64

from bluestar import __version__
from bluestar.calendar_layer import build_calendar, fetch_raw
from bluestar.config import (
    CALENDAR_CACHE_TTL, MARKET_CACHE_TTL, MODES, TZ_CET, TZ_UTC,
)
from bluestar.macro_engine import build_context, fr_date, fr_day_name, session_label
from bluestar.oanda_data import build_market_snapshot
from bluestar.renderer import render_html
from bluestar.validation import validate_context, validate_html

logging.basicConfig(level=logging.WARNING)

st.set_page_config(page_title="BLUESTAR · Macro Briefing Engine",
                   page_icon="🛰️", layout="wide",
                   initial_sidebar_state="expanded")

# --------------------------------------------------------------------------
# Cached layers (network) -- TTL configurable in config.py
# --------------------------------------------------------------------------
@st.cache_data(ttl=CALENDAR_CACHE_TTL, show_spinner=False)
def cached_calendar(_now_iso: str):
    raw = fetch_raw()
    now = datetime.now(TZ_UTC)
    return build_calendar(now_utc=now, raw_data=raw)


@st.cache_data(ttl=MARKET_CACHE_TTL, show_spinner=False)
def cached_market(_now_iso: str, overrides_key: str, allow_proxy: bool):
    overrides = json.loads(overrides_key) if overrides_key else None
    return build_market_snapshot(
        now_utc=datetime.now(TZ_UTC),
        overrides=(overrides or {}).get("market"),
        allow_proxy_levels=allow_proxy,
    )


def _slot() -> str:
    """Cache-busting slot rounded to the TTL so Refresh forces a refetch."""
    return datetime.now(TZ_UTC).strftime("%Y%m%d%H%M")


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
now_cet = datetime.now(TZ_CET)
label, is_live = session_label(now_cet)

with st.sidebar:
    st.caption("⬡ BLUESTAR SYSTEM")
    st.markdown(f"### MACRO BRIEFING ENGINE\n`v{__version__}`")
    st.divider()
    st.caption("HORLOGE")
    st.markdown(f"**{fr_day_name(now_cet)} {fr_date(now_cet)}**")
    st.markdown(f"🕐 {now_cet:%H:%M} CET — {label}")
    st.markdown("🟢 Session live" if is_live else "🔴 Hors session FX")
    st.divider()

    mode = st.selectbox("Mode opératoire", MODES, index=1)
    allow_proxy_levels = st.toggle("Autoriser les niveaux [PROXY] (ATR)", value=True)
    show_diagnostics = st.toggle("Afficher diagnostics data/source", value=True)
    show_raw_json = st.toggle("Afficher le JSON calendrier brut", value=False)
    st.divider()

    st.caption("OVERRIDES MANUELS (JSON)")
    st.caption(
        "Champs sans source sans clé : **taux CB** et **FedWatch** → stampés [PROXY]. "
        "**COT Non-Commercials** → stampé [OBSERVÉ — CFTC | vendredi de référence calculé] "
        "dès que les chiffres sont saisis (le vendredi CFTC en vigueur est calculé automatiquement). "
        "Prix marchés : récupérés via yfinance — saisir un bloc 'market' uniquement pour forcer une valeur."
    )
    sample_path = Path(__file__).parent / "sample_overrides.json"
    use_sample = st.toggle("Charger l'exemple fourni", value=False,
                           help="Charge sample_overrides.json (données de "
                                "démonstration : MOVE=66,8, textes FAIT/BIAIS "
                                "figés, COT d'exemple...). À laisser DÉSACTIVÉ "
                                "en production -- ne l'activer que pour tester "
                                "l'app hors-ligne sans overrides réels.")
    default_text = sample_path.read_text(encoding="utf-8") if (use_sample and sample_path.exists()) else "{}"
    overrides_text = st.text_area("Overrides", value=default_text, height=240,
                                  label_visibility="collapsed")
    st.divider()
    refresh = st.button("🔄 Refresh data", use_container_width=True)

# Parse overrides safely.
overrides: dict = {}
override_error = None
try:
    overrides = json.loads(overrides_text) if overrides_text.strip() else {}
except json.JSONDecodeError as e:
    override_error = str(e)

# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
st.markdown(
    f"<div style='border-top:3px solid #1B45B4;border:1px solid #dde3f5;"
    f"border-top:3px solid #1B45B4;padding:16px 22px;border-radius:8px;margin-bottom:14px;'>"
    f"<div style='font-size:10px;letter-spacing:4px;color:#1B45B4;font-weight:700;'>BLUESTAR SYSTEM · FX INSTITUTIONAL DESK</div>"
    f"<div style='font-size:22px;font-weight:700;color:#0d1f4e;'>INSTITUTIONAL MACRO BRIEFING ENGINE</div>"
    f"<div style='font-size:11px;color:#6B89D8;'>Data Integrity Layer · Macro Engine · Validation · Renderer — v{__version__}</div>"
    f"</div>", unsafe_allow_html=True)

if override_error:
    st.error(f"Overrides JSON invalide — ignorés : {override_error}")

if refresh:
    # Blanket clear (not cached_calendar.clear() / cached_market.clear()
    # named individually) so any @st.cache_data function added later is
    # covered automatically — no risk of a new cache silently surviving a
    # refresh because someone forgot to list it here.
    st.cache_data.clear()

# --------------------------------------------------------------------------
# Data Integrity Layer status
# --------------------------------------------------------------------------
slot = _slot()
calendar = cached_calendar(slot)
overrides_key = json.dumps(overrides, sort_keys=True, ensure_ascii=False) if overrides else ""
market = cached_market(slot, overrides_key, allow_proxy_levels)

meta = calendar.get("metadata", {})
c1, c2, c3, c4 = st.columns(4)
c1.metric("📅 Events high-impact", meta.get("total_high_impact", 0))
c2.metric("🟢 À venir", meta.get("upcoming_count", 0))
c3.metric("🔴 Critiques ≤6h", meta.get("critical_count", 0))
vix = market.gauge("VIX")
c4.metric("VIX", vix.display, vix.trend or None)

cal_ok = meta.get("reachable", False)
st.caption(("✅ Forex Factory atteignable · " if cal_ok else "⚠️ Forex Factory injoignable (fallback vide) · ")
           + ("✅ Marché yfinance OK" if vix.available else "⚠️ Marché yfinance indisponible — champs [N/A]"))

st.divider()

# --------------------------------------------------------------------------
# Generate
# --------------------------------------------------------------------------
left, right = st.columns([3, 1])
with right:
    generate = st.button("⚡ Generate Macro Briefing", type="primary",
                         use_container_width=True)

if "html" not in st.session_state:
    st.session_state.html = None

if generate or refresh:  # RC1 FIX: Refresh must regenerate the report, not keep the stale one
    now_utc = datetime.now(TZ_UTC)
    ctx = build_context(now_utc, market, calendar, overrides, mode, allow_proxy_levels)
    context_issues = validate_context(ctx)
    ctx.issues = context_issues  # visible to render_html's data-integrity footer
    html = render_html(ctx)
    issues = context_issues + validate_html(html)
    ctx.issues = issues
    st.session_state.html = html
    st.session_state.ctx_issues = issues
    st.session_state.ctx_summary = {
        "regime": ctx.regime,
        "priority": [(s.asset, s.color, s.action, s.conviction) for s in ctx.priority_assets],
        "avoid": ctx.avoid_assets,
        "no_setup": ctx.no_setup_reason,
    }

# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
if st.session_state.html:
    summary = st.session_state.get("ctx_summary", {})
    issues = st.session_state.get("ctx_issues", [])

    st.subheader("Briefing HTML")
    fname = f"Macro_Briefing_BLUESTAR_{now_cet:%d-%m-%Y}.html"
    # P0-3 FIX (Incident Review Board): the coverage/validation ERROR was
    # previously advisory-only -- the download button rendered unconditionally
    # even when publication_blocked was True. Gate it on any ERROR-severity
    # issue from this run.
    has_error = any(i.severity == "ERROR" for i in issues)
    if has_error:
        st.error("📥 Téléchargement bloqué — au moins une anomalie ERROR "
                 "(voir « Validation qualité » ci-dessous / le bloc « Intégrité "
                 "des données » dans le HTML).")
    else:
        st.download_button("📥 Télécharger HTML", data=st.session_state.html,
                           file_name=fname, mime="text/html", use_container_width=False)
    st.caption("Astuce PDF : ouvrir le HTML → bouton « 📥 Télécharger PDF » (window.print) "
               "→ Chrome → activer « Graphiques d'arrière-plan ».")

    # Rendu via data-URI base64 — remplace st.components.v1.html() retiré
    # en Streamlit 1.59.1 (deadline 2026-06-01 dépassée → segfault).
    # Avantages : iframe sandbox, scripts exécutés (window.print), no segfault.
    _b64 = base64.b64encode(
        st.session_state.html.encode("utf-8")
    ).decode("ascii")
    st.markdown(
        f'<iframe src="data:text/html;base64,{_b64}" '
        f'width="100%" height="900px" scrolling="yes" '
        f'style="border:1px solid #dde3f5;border-radius:8px;'
        f'box-shadow:0 2px 8px rgba(0,0,0,.06);"></iframe>',
        unsafe_allow_html=True,
    )

    if show_diagnostics:
        st.divider()
        d1, d2 = st.columns(2)
        with d1:
            st.markdown("#### 🔎 Diagnostics moteur")
            st.write({"Régime": summary.get("regime"),
                      "Actifs prioritaires": summary.get("priority"),
                      "À éviter": summary.get("avoid"),
                      "No-setup": summary.get("no_setup")})
            st.markdown("#### 📡 Sources marché")
            rows = []
            for k in ("VIX", "MOVE", "DXY", "US10Y", "XAU/USD", "Brent", "WTI"):
                g = market.gauge(k)
                rows.append({"Champ": k, "Valeur": g.display,
                             "Fiabilité": g.stamp.reliability.value,
                             "Source": g.stamp.render()})
            st.dataframe(rows, use_container_width=True, hide_index=True)
        with d2:
            st.markdown("#### ✅ Validation qualité")
            if not issues:
                st.success("Aucune anomalie détectée.")
            for i in issues:
                icon = "🔴" if i.severity == "ERROR" else "🟡" if i.severity == "WARN" else "🔵"
                st.markdown(f"{icon} **{i.rule}** — {i.message}")
else:
    st.info("Configurez les overrides (optionnel) puis cliquez sur "
            "**⚡ Generate Macro Briefing**. Sans données macro sourcées, le moteur "
            "produit honnêtement un bloc *no-setup* plutôt qu'un setup forcé.")

# --------------------------------------------------------------------------
# Calendar + raw JSON panels
# --------------------------------------------------------------------------
st.divider()
with st.expander("📆 Calendrier Forex Factory — events_engine (Data Integrity Layer)", expanded=False):
    for e in calendar.get("events_engine", [])[:40]:
        tag = e["priority"]
        st.markdown(f"`{e['time_display']}` **{e['currency']}** — {e['event_name']} "
                    f"· {tag} · prev {e['previous']} / cons {e['forecast']}"
                    + (f" / actual {e['actual']}" if e['actual'] != '—' else ""))

if show_raw_json:
    with st.expander("🔍 JSON calendrier brut", expanded=False):
        st.code(json.dumps(calendar, indent=2, ensure_ascii=False), language="json")

st.caption("BLUESTAR SYSTEM · MACRO BRIEFING ENGINE · "
           "Aucune donnée inventée — [N/A]/[PROXY] partout où la source manque.")
