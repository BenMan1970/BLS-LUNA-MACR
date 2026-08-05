"""Test d'intégration empirique de bout en bout : calendar_layer v3 ->
macro_engine.build_context -> renderer.render_html.

Reproduit le scénario réel observé dans le Macro Briefing du 05/08/2026 :
- un événement NZD "Employment Change q/q" à ~2h (imminent, tier A)
- un événement USD "Non-Farm Employment Change" à ~67h (tier S, medium)

Vérifie :
1. Aucune exception sur tout le pipeline.
2. Section 2 n'est PAS vide (le bug de la v2 est corrigé).
3. Le badge rouge "ÉLEVÉ" apparaît pour l'événement NZD imminent.
4. Le badge jaune "ÉLEVÉ · >48h" (patch MACRO-A3) apparaît toujours pour
   l'événement USD à 67h -- preuve que ce correctif n'a pas été cassé.
5. L'avoid-list (section 1) contient bien NZD/USD en blackout tier A.
6. determine_market_regime() détecte bien le catalyseur binaire imminent.
"""
import sys
import datetime
import pytz

sys.path.insert(0, "/home/claude")

from testpkg2 import calendar_layer as cal
from testpkg2 import macro_engine as me
from testpkg2 import renderer
from testpkg2.models import MarketSnapshot, Datum, SourceStamp, Reliability, na_stamp

# --- now_utc choisi pour matcher les hours_until du scénario réel ---
now_utc = datetime.datetime(2026, 8, 4, 22, 43, 0, tzinfo=pytz.UTC)

raw = [
    {
        "title": "Employment Change q/q", "country": "NZD",
        "date": "2026-08-05T00:00:00Z",  # ~+1.28h, dans la fenêtre blackout tier A (avant=2h)
        "impact": "High", "forecast": "0.1%", "previous": "0.2%", "actual": "",
    },
    {
        "title": "Non-Farm Employment Change", "country": "USD",
        "date": "2026-08-07T12:30:00Z",  # ~+65.78h
        "impact": "High", "forecast": "0.1%", "previous": "0.2%", "actual": "",
    },
]

calendar_dict = cal.build_calendar(now_utc=now_utc, raw_data=raw)
print("--- metadata calendrier ---")
print(calendar_dict["metadata"])

# --- market minimal : VIX disponible (nécessaire pour que
#     determine_market_regime() aille jusqu'au test e.priority=="CRITICAL")
#     + prix pour NZD/USD et USD/CAD (univers de test) pour que
#     select_priority_assets() atteigne le check is_blackout().
market = MarketSnapshot(
    as_of_utc=now_utc,
    gauges={
        "VIX": Datum(18.0, SourceStamp("Test", Reliability.PRIMARY, timestamp=now_utc), "18,0", ""),
    },
    prices={
        "NZD/USD": Datum(0.5900, SourceStamp("Test", Reliability.PRIMARY, timestamp=now_utc), "0,5900"),
        "USD/CAD": Datum(1.3800, SourceStamp("Test", Reliability.PRIMARY, timestamp=now_utc), "1,3800"),
    },
    atr={"NZD/USD": 0.0050, "USD/CAD": 0.0060},
)

ctx = me.build_context(
    now_utc=now_utc,
    market=market,
    calendar=calendar_dict,
    overrides={},
    mode="Normal",
    allow_proxy_levels=True,
)

print("\n--- catalysts_high ---")
for e in ctx.catalysts_high:
    print(" ", e.currency, e.event_name, e.priority, e.hours_until)
print("--- catalysts_medium ---")
for e in ctx.catalysts_medium:
    print(" ", e.currency, e.event_name, e.priority, e.hours_until)
print("--- avoid_assets ---")
for a, r in ctx.avoid_assets:
    print(" ", a, "->", r)
print("--- regime ---")
print(" ", ctx.regime, "|", ctx.regime_class)

html_out = renderer.render_html(ctx)

# ---------------------------------------------------------------------
# Assertions empiriques
# ---------------------------------------------------------------------
errors = []

if not ctx.catalysts_high and not ctx.catalysts_medium:
    errors.append("RÉGRESSION: catalysts_high et catalysts_medium sont vides "
                   "(exactement le bug du 05/08/2026).")

if len(ctx.catalysts_high) != 1:
    errors.append(f"attendu 1 événement 'high' (NZD imminent), obtenu {len(ctx.catalysts_high)}")
elif ctx.catalysts_high[0].currency != "NZD":
    errors.append("l'événement 'high' attendu n'est pas le NZD imminent")

if len(ctx.catalysts_medium) != 1:
    errors.append(f"attendu 1 événement 'medium' (USD NFP ~67h), obtenu {len(ctx.catalysts_medium)}")
elif ctx.catalysts_medium[0].currency != "USD":
    errors.append("l'événement 'medium' attendu n'est pas le USD NFP")

if '<div class="event high">' not in html_out:
    errors.append("aucune carte d'événement 'high' rendue dans le HTML (section 2)")

if '<div class="event medium">' not in html_out:
    errors.append("aucune carte d'événement 'medium' rendue dans le HTML (section 2)")

if "🔴 ÉLEVÉ</span>" not in html_out:
    errors.append("badge rouge '🔴 ÉLEVÉ' absent (événement imminent NZD)")

if "🟡 ÉLEVÉ · &gt;48h" not in html_out:
    errors.append("badge jaune MACRO-A3 '🟡 ÉLEVÉ · >48h' absent ou cassé (régression du patch MACRO-A3)")

if "Flux Forex Factory <" in html_out and "silence calendaire n'est PAS une absence de risque" in html_out:
    # Cette bannière ne doit apparaître QUE dans la branche "aucun catalyseur".
    # Ici on A des catalyseurs, donc elle ne doit PAS apparaître seule.
    if '<div class="event high">' not in html_out:
        errors.append("la bannière 'section vide' est apparue alors que des événements existent")

if not any(a == "NZD/USD" for a, _ in ctx.avoid_assets):
    errors.append("NZD/USD attendu dans l'avoid-list (blackout tier A) — absent")
else:
    reason = next(r for a, r in ctx.avoid_assets if a == "NZD/USD")
    if "tier A" not in reason or "imminente" not in reason:
        errors.append(f"raison de blackout NZD/USD inattendue: {reason!r}")

if "catalyseur binaire imminent" not in ctx.regime:
    errors.append("determine_market_regime() n'a pas détecté le catalyseur binaire imminent "
                   f"(régime obtenu: {ctx.regime!r})")

if errors:
    print("\n=== ÉCHEC ===")
    for e in errors:
        print(" -", e)
    sys.exit(1)
else:
    print("\n=== TOUS LES TESTS EMPIRIQUES PASSENT ===")
    print(f"HTML généré : {len(html_out)} caractères, aucune exception, aucune régression détectée.")
