"""Macro Engine -- the institutional logic of BLUESTAR v8.1.

Rule-based and deterministic. It never invents a figure: when an input is
missing the corresponding output is ``[N/A]`` or a documented ``[PROXY]`` and
the conviction is reduced. The COT/IPS module only adjusts conviction, squeeze
risk and sizing -- it never generates a directional signal on its own.

Pipeline: regime -> catalysts -> central banks -> macro overlay -> currency
strength -> IPS -> asset selection -> levels/expected-move -> sizing -> risk
scenarios -> :class:`BriefingContext`.

=============================================================================
ROUND 1 (14/09/2026) -- correctifs [M1] à [M6], conservés tels quels
=============================================================================

[M1] PLAFOND ``medium[:6]`` -- LA DÉCISION BoJ DISPARAISSAIT DU RAPPORT
     ``build_catalysts`` tronquait la liste à 6 éléments après un tri
     purement chronologique, si bien que ``BOJ Policy Rate`` (17/09 22:30 ET)
     était silencieusement coupé alors que le setup n°1 du jour était
     USD/JPY SHORT.
     → ``_prioritise_events()`` réserve UN créneau par devise ayant une
       décision de banque centrale, puis remplit par proximité, puis
       réordonne chronologiquement. ``priority`` reste INCHANGÉ : aucune
       décision n'est promue en HIGH, donc ``catalyst_pen`` voit exactement
       les mêmes événements qu'avant. Zéro impact scoring.

[M2] FENÊTRE DES CATALYSEURS D'INVALIDATION BORNÉE À 72h
     → Troisième appel dédié (``ev_label``), borné à ``FF_WATCH_HORIZON_H``,
       utilisé UNIQUEMENT pour le libellé. ``ev`` (scoring) et
       ``blackout_ev`` (gating) inchangés à l'octet près.

[M3] ``ev_names`` TRIÉ ALPHABÉTIQUEMENT
     → Tri par (décision CB d'abord, puis proximité). Changement de sortie
       assumé sur les actifs ayant >1 catalyseur en fenêtre.

[M4] ``na_stamp("source sans clé API")`` TROMPEUR
     → Messages distincts selon une information réellement disponible ici.
       Étendu au round 2, voir [N4].

[M5] DIFFÉRENTIEL DOMINANT « USD (3,75%) vs USD (3,75%) »
     ``max()``/``min()`` renvoient la première clé en cas d'égalité.
     → Tri explicite sur (valeur, code devise).

[M6] « Prochaine réunion » NE RETOMBAIT JAMAIS SUR LE CALENDRIER
     → ``_next_meeting_from_feed()``, repli additif.
       Précédence : table officielle > override manuel > flux > [N/A].

=============================================================================
ROUND 2 (14/09/2026) -- [N1] à [N8]
Chaque point est vérifié sur le flux réel https://nfs.faireconomy.media/
ff_calendar_thisweek.json (HTTP 200, 13 793 octets, relu le 14/09/2026) et
sur la fiche FRED de la série BoJ.
=============================================================================

[N1] LA CARTE BoJ RESTAIT EN [N/A] MÊME APRÈS LES CORRECTIFS DE FRAÎCHEUR
     Deux faits vérifiés :
       a) La série FRED ``IRSTCI01JPM156N`` n'est PAS le taux directeur de la
          BoJ. Sa fiche FRED indique : source « Organization for Economic
          Co-operation and Development », « Frequency: Monthly », intitulé
          « Interest Rates: Immediate Rates (< 24 Hours): Call Money/
          Interbank Rate: Total for Japan ». C'est un taux INTERBANCAIRE
          MENSUEL OCDE. Relever son seuil de fraîcheur ne suffit donc pas :
          il ne faut pas la présenter comme le taux de politique monétaire.
       b) Le flux Forex Factory porte la donnée exacte :
          ``{"title":"BOJ Policy Rate","country":"JPY",
             "date":"2026-09-17T22:30:00-04:00","impact":"High",
             "forecast":"<1.25%","previous":"<1.00%"}``
     → ``_rate_event_for_bank()`` + ``_parse_feed_rate()`` résolvent le taux
       en vigueur depuis le champ ``previous`` de la prochaine décision.
       ``_CB_PREFER_CALENDAR = {"BoJ"}`` fait passer le flux DEVANT FRED pour
       cette banque, et uniquement pour elle, au motif (a) ci-dessus.

[N2] LES VALEURS BORNÉES « <1.00% » CASSAIENT TOUS LES PARSEURS
     ``_parse_rate_pct`` faisait ``s.strip().lstrip("~").rstrip("%")`` puis
     ``float(...)`` : sur « <1,00% » cela lève ``ValueError`` et la fonction
     retournait ``None``. La BoJ était donc exclue du différentiel de taux
     même une fois son taux résolu. Le même parseur inversait aussi le SIGNE
     des taux négatifs : ``re.split(r"[–-]", "-0,10")`` produit
     ``["", "0.10"]`` → +0,10 au lieu de −0,10 (bug latent, réactivé au
     premier taux négatif BoJ/SNB).
     → Parseur unique tolérant : qualificateurs (< > ≤ ≥ ~ ±) conservés et
       réaffichés, fourchettes « 3,50–3,75% » moyennées, signe négatif
       préservé (le tiret n'est traité comme séparateur de fourchette que
       s'il est ENTRE deux chiffres).

[N3] GARDE-FOU CONTRE LES FAUX TAUX (découverte de ce round)
     Le flux contient ``{"title":"MPC Official Bank Rate Votes",
     "country":"GBP","forecast":"3-0-6","previous":"3-0-6"}``. Ce libellé
     CONTIENT « official bank rate » : un match par mot-clé aurait retenu cet
     événement, et un ``re.search`` numérique aurait extrait « 3 » de
     « 3-0-6 », affichant un taux BoE de 3,00%.
     → Double garde-fou : la valeur doit contenir « % » (rejette « 3-0-6 » et
       « 480B »), et les libellés contenant « vote » sont exclus.
     Même famille de bug côté ``_CB_DECISION_HINTS`` : le mot-clé nu
     « press conference » matchait ``{"title":"NBS Press Conference",
     "country":"CNY"}`` -- le bureau statistique chinois, pas une banque
     centrale -- qui pouvait donc consommer un créneau réservé [M1]. Les
     conférences de presse sont désormais énumérées banque par banque.

[N4] ``na_stamp`` : TROISIÈME CAS MANQUANT, ET PROXY MENSONGER
     a) [M4] ne distinguait que « clé/réseau » et « série rejetée ». Il
        manquait le cas « la décision est au calendrier mais le flux ne
        publie pas de valeur » (cas BCE cette semaine : le flux ne contient
        AUCUNE décision BCE, seulement deux ``ECB President Lagarde Speaks``
        en Medium -- la carte EUR ne peut donc PAS être réparée par le
        calendrier, elle dépend du correctif de fraîcheur sur ``ECBDFR``).
     b) Bug distinct : ``elif o:`` posait ``proxy_stamp("manual override")``
        dès qu'un override EXISTAIT pour la banque, même s'il ne contenait
        pas de clé ``rate``. La carte affichait alors « [N/A] » sous un tag
        [PROXY · override] -- une source revendiquée pour une valeur absente.
     → Le stamp est désormais choisi sur l'origine RÉELLE de la valeur
       affichée (``kind`` ∈ live / feed / override / None).

[N5] BIAIS : LE CONSENSUS DE HAUSSE BoJ N'APPARAISSAIT NULLE PART
     ``_derive_bias_from_rate`` ne lit que le NIVEAU du taux contre une
     fourchette neutre. Sur la BoJ, cela peut produire « Dovish » (taux bas)
     le jour même où le consensus attend une HAUSSE (<1,25% contre <1,00%) --
     lecture exactement inverse du risque porté par le setup USD/JPY.
     → ``_derive_bias_from_consensus()``. Règle de précédence explicite :
       override > consensus SI le consensus implique un CHANGEMENT de taux >
       fourchette neutre > [N/A].
       CONSÉQUENCE VOULUE ET BORNÉE : quand ``forecast == previous`` (cas FED
       4,00%/3,75%… non, cas BoE 3,75%/3,75% cette semaine) la règle ne se
       déclenche PAS et le mot d'ouverture reste celui de la fourchette
       neutre -- donc ``_cb_bias_word`` renvoie la même valeur qu'avant et
       le score de force de devise est inchangé. Le seul cas qui bouge est
       une banque dont le consensus attend un mouvement.

[N6] ANCRE DES SCÉNARIOS ET DU THÈME : PREMIER ÉLÉMENT DE LISTE
     ``build_macro_overlay`` faisait ``events[0].event_name`` et
     ``build_risk_scenarios`` ``events[0]``, sur une liste ``upcoming`` NI
     TRIÉE NI FILTRÉE par importance. Le « prochain catalyseur » du thème
     pouvait donc être n'importe quel événement Low du flux (le 14/09, la
     première ligne à venir est ``New Home Prices m/m`` / CNY / Low), et le
     branchement « [N/A] — aucun événement CRITICAL/HIGH programmé » était
     structurellement inatteignable puisque la liste contenait tout.
     → ``_anchor_event()`` : plus imminent parmi CRITICAL/HIGH, sinon
       MEDIUM, sinon rien. Changement de sortie assumé sur le thème macro,
       ``risk_main`` et les déclencheurs bull/bear.

[N7] DÉPENDANCE À ``date_display``, NON VÉRIFIÉE
     ``_next_meeting_from_feed`` (round 1) lisait ``getattr(nxt,
     "date_display", "")`` en supposant un format ISO ``YYYY-MM-DD``, que je
     n'ai jamais pu vérifier faute d'accès à models.py.
     → ``_iso_date_of()`` essaie ``datetime_utc``, puis ``date_display``,
       puis ``date``, accepte ``datetime`` comme ``str``, et extrait la date
       par expression régulière. Retourne ``None`` si rien n'est lisible, le
       caller retombant alors sur override/[N/A] comme avant.
       NOTE : la date rendue est celle de ``datetime_utc``, donc en UTC. La
       réunion BoJ du 17/09 22:30 ET s'affiche au 18/09 (02:30 UTC), ce qui
       reste dans la fenêtre de réunion officielle 17-18/09.

[N8] ROBUSTESSE DU DIFFÉRENTIEL
     ``zip(_CB_DEFS, central_banks)`` supposait que les deux listes restent
     dans le même ordre. Remplacé par un appariement par ``cb.name``. Le
     filtre d'éligibilité n'exige plus ``stamp.ok`` seul (un stamp FALLBACK,
     nouveau depuis [N1], aurait pu être écarté selon la définition de ``ok``
     dans models.py, que je n'ai pas pu lire) : il retient toute valeur
     effectivement parsable portée par un stamp PRIMARY/FALLBACK/PROXY.

INCHANGÉ ET DÉLIBÉRÉMENT NON TOUCHÉ DANS CE MODULE
     ``_render_event_medium`` n'affiche ni ``previous`` ni ``forecast``
     (asymétrie avec ``_render_event_high``), ce qui fait perdre le
     « Federal Funds Rate · forecast 4,00% · previous 3,75% » pourtant
     transporté jusqu'ici -- ressort de renderer.py.
     ``build_coverage_report`` ne compte pas les taux directeurs -- ressort
     de staleness.py.
     Les bornes de fraîcheur PAR SÉRIE (``ECBDFR``, et le remplacement de
     ``IRSTCI01JPM156N``) -- ressort de external_sources.py.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import calendar_layer as cal
from . import config as C
from .config import TZ_UTC
from .models import (
    AssetSetup, BriefingContext, CentralBankSnapshot, CotPositioning,
    CurrencyStrength, Datum, MacroEvent, MarketSnapshot, Reliability, SourceStamp,
    RiskScenario, na_stamp, proxy_stamp,
)

# Institutional Intelligence layer (best-effort; zero-regression if absent).
try:
    from . import institutional as _inst  # type: ignore
except Exception:  # pragma: no cover
    _inst = None
from .oanda_data import fr_num
from .external_sources import (
    fetch_central_bank_rates,             # wrapper contrat historique dict[str,float]
    fetch_central_bank_rates_with_meta,   # P0-2 (15/09/2026) : provenance RÉELLE par run
    central_bank_rate_source,             # repli d'étiquetage (chemin principal attendu)
    fetch_fedwatch_probabilities,
    fetch_cot_data,
    fetch_liquidity_stress,
    fetch_pc_ratio,                      # C1: VIX × P/C composite signal
)

logger = logging.getLogger(__name__)

FR_DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
FR_MONTHS = ["", "janvier", "février", "mars", "avril", "mai", "juin",
             "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def fr_date(dt: datetime) -> str:
    return f"{dt.day:02d}/{dt.month:02d}/{dt.year}"


def fr_day_name(dt: datetime) -> str:
    return FR_DAYS[dt.weekday()]


# ---------------------------------------------------------------------------
# [M1]/[M3]/[M6] -- Reconnaissance des décisions de banque centrale
# ---------------------------------------------------------------------------
# Détection par mot-clé sur le LIBELLÉ de l'événement, volontairement
# indépendante de ``calendar_layer.classify_tier`` : ce dernier est une
# réplique EXACTE de v10.py (Desk Engine) dont l'en-tête interdit toute
# divergence, et son tier "S" couvre bien plus large que les seules décisions
# de politique monétaire (NFP, CPI...). On ne le détourne donc pas de son
# contrat ; cette table sert uniquement à l'ORDONNANCEMENT d'affichage et au
# repli « prochaine réunion », jamais au gating ni au scoring.
#
# NOTE sur "fomc" : le mot-clé nu est délibérément ABSENT. Le flux contient
# « FOMC Member Bowman Speaks » / « FOMC Member Schmid Speaks », des
# interventions de faible valeur qui monopoliseraient les créneaux réservés.
#
# [N3] NOTE sur "press conference" : le mot-clé NU a été RETIRÉ. Il matchait
# ``{"title":"NBS Press Conference","country":"CNY","impact":"Low"}`` --
# conférence du bureau statistique chinois, pas une banque centrale -- qui
# pouvait ainsi réserver un créneau [M1] au détriment d'une vraie décision.
# Les conférences sont désormais énumérées banque par banque.
_CB_DECISION_HINTS: tuple[str, ...] = (
    "policy rate",
    "official bank rate",
    "federal funds rate",
    "monetary policy statement",
    "monetary policy summary",
    "fomc statement",
    "fomc economic projections",
    "fomc press conference",
    "rate statement",
    "rate decision",
    "interest rate decision",
    "official cash rate",
    "overnight rate",
    "cash rate",
    "main refinancing rate",
    "deposit facility rate",
    "boj press conference",
    "ecb press conference",
    "snb press conference",
    "boc press conference",
    "rbnz press conference",
    "rba press conference",
)


def _is_cb_decision(event) -> bool:
    """True si l'événement est une décision (ou une communication liée) de
    banque centrale. Tolérant à la forme de l'objet : ``MacroEvent`` expose
    ``event_name``, un dict brut de flux exposerait ``title``. Retourne
    False silencieusement si aucun des deux n'est présent -- ce helper ne
    doit jamais lever dans le chemin de rendu."""
    name = (getattr(event, "event_name", None)
            or getattr(event, "title", None)
            or "")
    low = str(name).lower()
    return any(hint in low for hint in _CB_DECISION_HINTS)


# Plafonds d'affichage des catalyseurs. ``_HIGH_CAP`` est inchangé (6, valeur
# historique). ``_MEDIUM_CAP`` passe de 6 à 8 pour absorber la semaine
# FOMC+BoE+BoJ ; la réservation par devise ci-dessous rend de toute façon la
# décision CB de chaque devise imperdable, même si ce plafond est ramené à 6.
_HIGH_CAP: int = 6
_MEDIUM_CAP: int = 8


def _prioritise_events(events: list[MacroEvent], cap: int) -> list[MacroEvent]:
    """Sélectionne ``cap`` événements en garantissant la visibilité des
    décisions de banque centrale, puis les rend dans l'ordre CHRONOLOGIQUE.

    Algorithme, dans l'ordre :
      1. Réservation : pour chaque devise ayant au moins une décision de
         banque centrale, la plus PROCHE de ces décisions obtient un créneau
         garanti. C'est le cœur du correctif [M1] -- sans cette étape, un
         simple tri « CB d'abord » laissait encore tomber la BoJ, dernière
         chronologiquement parmi ~10 lignes CB pour 8 places.
      2. Remplissage : le reste des événements, triés (décisions CB d'abord,
         puis par proximité), jusqu'à atteindre ``cap``.
      3. Affichage : tri chronologique final.

    Zéro régression quand ``len(events) <= cap`` : la sortie est alors
    strictement identique à l'ancien ``sorted(...)[:cap]``.
    """
    if not events:
        return []
    if len(events) <= cap:
        return sorted(events, key=lambda e: e.hours_until)

    reserved: list[MacroEvent] = []
    reserved_ids: set[int] = set()
    seen_ccy: set[str] = set()
    for e in sorted((x for x in events if _is_cb_decision(x)),
                    key=lambda x: x.hours_until):
        ccy = getattr(e, "currency", "") or ""
        if ccy in seen_ccy:
            continue
        seen_ccy.add(ccy)
        reserved.append(e)
        reserved_ids.add(id(e))
        if len(reserved) >= cap:
            break

    remaining = [e for e in events if id(e) not in reserved_ids]
    remaining.sort(key=lambda e: (0 if _is_cb_decision(e) else 1, e.hours_until))

    selected = reserved + remaining[: max(0, cap - len(reserved))]
    return sorted(selected, key=lambda e: e.hours_until)


# ---------------------------------------------------------------------------
# [N6] Ancre calendaire -- événement de référence du thème et des scénarios
# ---------------------------------------------------------------------------
def _anchor_event(events: Optional[list[MacroEvent]]) -> Optional[MacroEvent]:
    """Événement de référence : le plus IMMINENT parmi CRITICAL/HIGH, sinon
    parmi MEDIUM, sinon ``None``.

    [N6] Remplace ``events[0]``, qui dépendait de l'ordre d'arrivée du flux et
    ne filtrait pas l'importance : le « prochain catalyseur » affiché dans le
    thème macro pouvait être un événement Low (le 14/09/2026, la première
    ligne à venir du flux est ``New Home Prices m/m`` / CNY / Low).

    Ne retourne JAMAIS un événement passé (``is_upcoming`` faux).
    """
    if not events:
        return None
    up = [e for e in events if getattr(e, "is_upcoming", False)]
    if not up:
        return None
    majors = [e for e in up if getattr(e, "priority", "") in ("CRITICAL", "HIGH")]
    pool = majors or [e for e in up if getattr(e, "priority", "") == "MEDIUM"]
    if not pool:
        return None
    return min(pool, key=lambda e: e.hours_until)


# ---------------------------------------------------------------------------
# Session / live-market helpers
# ---------------------------------------------------------------------------
def session_label(dt_cet: datetime) -> tuple[str, bool]:
    """Return (human session label, is_live_fx_session).

    Session boundaries are defined in **UTC** so they remain stable year-round
    regardless of DST transitions.  Using local CET/CEST hour comparisons
    introduced a systematic 1-hour drift during summer (CEST = UTC+2):
    "Session Londres" would fire at 08:00 CEST = 06:00 UTC, one hour before
    the actual FX open at 07:00 UTC.  All boundaries below are UTC-constant.

    Reference boundaries (UTC, approximate industry consensus):
      Asian      00:00–07:00  (Tokyo liquidity peak 00:00–03:00)
      London     07:00–17:00
      Overlap    13:00–17:00  (London + New York both active)
      New York   13:00–22:00
      FX closed  Fri 22:00 – Sun 22:00 (UTC)

    The ``dt_cet`` argument is kept for API compatibility (callers already hold
    a CET datetime); we reproject to UTC internally.
    """
    dt_utc = dt_cet.astimezone(TZ_UTC)
    wd = dt_utc.weekday()   # 0 = Monday … 6 = Sunday

    # FX cash market: opens Sun ~22:00 UTC, closes Fri ~22:00 UTC.
    if wd == 5:                          # Saturday
        return "MARCHÉ FX FERMÉ (week-end)", False
    if wd == 6 and dt_utc.hour < 22:     # Sunday before reopen
        return "MARCHÉ FX FERMÉ (week-end)", False
    if wd == 4 and dt_utc.hour >= 22:    # Friday after close
        return "MARCHÉ FX FERMÉ (week-end)", False

    h = dt_utc.hour
    london = 7 <= h < 17
    ny     = 13 <= h < 22
    if london and ny:
        return "Overlap Londres/New York", True
    if london:
        return "Session Londres", True
    if ny:
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
        return ("MIXTE — données vol insuffisantes [N/A]", "regime-mix",
                "[N/A]", 1)

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
# Step 4 -- Central banks
# ---------------------------------------------------------------------------
# P0 FIX (audit 23/07/2026): official ECB Governing Council monetary-policy
# meeting calendar (Day 1, Day 2 = press-conference day), verified directly
# against https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
# on 23/07/2026. Covers through Oct 2028; beyond the last entry the code falls
# back to the override / [N/A], same as before this fix.
_ECB_MEETING_DATES: list[tuple[str, str]] = [
    ("2026-07-22", "2026-07-23"), ("2026-09-09", "2026-09-10"),
    ("2026-10-28", "2026-10-29"), ("2026-12-16", "2026-12-17"),
    ("2027-02-03", "2027-02-04"), ("2027-03-17", "2027-03-18"),
    ("2027-04-28", "2027-04-29"), ("2027-06-09", "2027-06-10"),
    ("2027-07-21", "2027-07-22"), ("2027-09-08", "2027-09-09"),
    ("2027-10-27", "2027-10-28"), ("2027-12-15", "2027-12-16"),
    ("2028-02-02", "2028-02-03"), ("2028-03-22", "2028-03-23"),
    ("2028-05-03", "2028-05-04"), ("2028-06-07", "2028-06-08"),
    ("2028-07-19", "2028-07-20"), ("2028-09-06", "2028-09-07"),
    ("2028-10-11", "2028-10-12"),
]


def _next_ecb_meeting(now_utc: Optional[datetime]) -> Optional[str]:
    """Next ECB monetary-policy decision/press-conference date, or ``None``
    if ``now_utc`` is outside the verified table (caller then falls back to
    the override / [N/A] -- see _ECB_MEETING_DATES docstring above)."""
    if now_utc is None:
        return None
    today = now_utc.date().isoformat()
    for day1, day2 in _ECB_MEETING_DATES:
        if day2 >= today:
            d1 = datetime.strptime(day1, "%Y-%m-%d")
            d2 = datetime.strptime(day2, "%Y-%m-%d")
            if d1.month == d2.month:
                return f"{d1.day:02d}\u2013{d2.day:02d}/{d2.month:02d}/{d2.year} (BCE, calendrier officiel)"
            return f"{d1.day:02d}/{d1.month:02d}\u2013{d2.day:02d}/{d2.month:02d}/{d2.year} (BCE, calendrier officiel)"
    return None


# P0 FIX (audit 23/07/2026): official FOMC calendar, verified directly
# against https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# on 23/07/2026 (2027 dates listed there as "tentative until confirmed at
# the meeting immediately preceding it").
_FOMC_MEETING_DATES: list[tuple[str, str]] = [
    ("2026-07-28", "2026-07-29"), ("2026-09-15", "2026-09-16"),
    ("2026-10-27", "2026-10-28"), ("2026-12-08", "2026-12-09"),
    ("2027-01-26", "2027-01-27"), ("2027-03-16", "2027-03-17"),
    ("2027-04-27", "2027-04-28"), ("2027-06-08", "2027-06-09"),
    ("2027-07-27", "2027-07-28"), ("2027-09-14", "2027-09-15"),
    ("2027-10-26", "2027-10-27"), ("2027-12-07", "2027-12-08"),
]

# P0 FIX (audit 23/07/2026): official BoJ Monetary Policy Meeting calendar,
# verified directly against
# https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm on 23/07/2026.
_BOJ_MEETING_DATES: list[tuple[str, str]] = [
    ("2026-07-30", "2026-07-31"), ("2026-09-17", "2026-09-18"),
    ("2026-10-29", "2026-10-30"), ("2026-12-17", "2026-12-18"),
]


def _next_meeting_from_table(now_utc: Optional[datetime],
                             table: list[tuple[str, str]],
                             label: str) -> Optional[str]:
    """Shared lookup for the official-calendar tables above. Returns the
    next meeting as 'DD–DD/MM/YYYY (<label>, calendrier officiel)', or
    ``None`` if ``now_utc`` is past the table's last entry."""
    if now_utc is None:
        return None
    today = now_utc.date().isoformat()
    for day1, day2 in table:
        if day2 >= today:
            d1 = datetime.strptime(day1, "%Y-%m-%d")
            d2 = datetime.strptime(day2, "%Y-%m-%d")
            if d1.month == d2.month:
                return f"{d1.day:02d}\u2013{d2.day:02d}/{d2.month:02d}/{d2.year} ({label}, calendrier officiel)"
            return f"{d1.day:02d}/{d1.month:02d}\u2013{d2.day:02d}/{d2.month:02d}/{d2.year} ({label}, calendrier officiel)"
    return None


# [M6] Devise de rattachement de chaque banque centrale, pour le repli sur le
# flux Forex Factory.
_CB_FEED_CCY: dict[str, str] = {
    "FED": "USD", "BCE": "EUR", "BoJ": "JPY", "BoE": "GBP",
}

_CB_DEFS = [
    ("FED", "🇺🇸", "USD"),
    ("BCE", "🇪🇺", "EUR"),
    ("BoJ", "🇯🇵", "JPY"),
    ("BoE", "🇬🇧", "GBP"),
]

_CCY_BY_CB: dict[str, str] = {name: ccy for name, _flag, ccy in _CB_DEFS}


# ---------------------------------------------------------------------------
# [N1]/[N2]/[N3] -- Résolution des taux directeurs depuis le flux calendrier
# ---------------------------------------------------------------------------
# Libellés du flux Forex Factory portant la VALEUR NUMÉRIQUE du taux
# directeur, banque par banque. Vérifiés sur le flux du 14/09/2026 :
#   Federal Funds Rate / USD / 2026-09-16T14:00:00-04:00 / High
#   Official Bank Rate / GBP / 2026-09-17T07:00:00-04:00 / High / 3.75%
#   BOJ Policy Rate    / JPY / 2026-09-17T22:30:00-04:00 / High / <1.00%
# Aucune décision BCE dans ce flux (uniquement « ECB President Lagarde
# Speaks », Medium) : la carte EUR ne peut donc pas être réparée par ce
# chemin, elle dépend des bornes de fraîcheur d'``ECBDFR``.
_RATE_EVENT_TITLES: dict[str, tuple[str, ...]] = {
    "FED": ("federal funds rate",),
    "BCE": ("main refinancing rate", "deposit facility rate", "minimum bid rate"),
    "BoJ": ("boj policy rate",),
    "BoE": ("official bank rate",),
}

# [N3] Exclusion indispensable : « MPC Official Bank Rate Votes » contient
# « official bank rate » et porte « 3-0-6 » en previous/forecast.
_RATE_EVENT_EXCLUDE: tuple[str, ...] = ("vote",)

# [N1] Banques pour lesquelles le flux PRIME sur FRED, et pourquoi.
# BoJ : la série FRED utilisée en amont (``IRSTCI01JPM156N``) est, d'après sa
# propre fiche FRED, un taux interbancaire MENSUEL publié par l'OCDE
# (« Immediate Rates (< 24 Hours): Call Money/Interbank Rate »), pas le taux
# de politique monétaire de la Banque du Japon. Le flux Forex Factory publie
# la bonne grandeur (« BOJ Policy Rate »), on la préfère donc explicitement.
_CB_PREFER_CALENDAR: frozenset[str] = frozenset({"BoJ"})

# Le flux, source live, passe-t-il devant un override manuel pour le TAUX ?
# True = cohérent avec la règle « live d'abord » adoptée le 15/07/2026 pour
# FRED. Passez à False pour rendre l'override prioritaire sur le flux.
# N'affecte PAS la « prochaine réunion », où l'override reste prioritaire sur
# le flux (dérivation heuristique par mots-clés, cf. [M6]).
_RATE_FEED_BEATS_OVERRIDE: bool = True

_RATE_QUAL_RE = re.compile(r"^\s*(<=|>=|≤|≥|<|>|~|±)")
_RATE_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_RATE_QUAL_NORM = {"<=": "≤", ">=": "≥", "≤": "≤", "≥": "≥",
                   "<": "<", ">": ">", "~": "~", "±": "±"}
_RATE_EMPTY = {"", "-", "--", "n/a", "N/A", "[N/A]", "none", "None"}


@dataclass(frozen=True)
class _ParsedRate:
    """Taux parsé depuis une chaîne de flux ou d'override.

    ``value`` est la valeur numérique en pourcentage ; pour une valeur BORNÉE
    (« <1.00% ») c'est la BORNE publiée, pas un point. ``display`` conserve le
    qualificateur, de sorte qu'aucun affichage ne transforme une borne en
    valeur exacte.
    """
    value: float
    qualifier: str
    display: str
    raw: str

    @property
    def is_bounded(self) -> bool:
        return self.qualifier in ("<", ">", "≤", "≥")


def _parse_feed_rate(raw) -> Optional[_ParsedRate]:
    """[N2]/[N3] Parse une valeur de taux issue du flux calendrier.

    Accepte « 3.75% », « <1.00% », « 3,75 % », « -0.10% ».
    Rejette (``None``) : vide, « - », et TOUTE chaîne sans « % » -- ce dernier
    garde-fou est ce qui empêche « 3-0-6 » (votes MPC) d'être lu comme 3,00%
    et « 480B » (New Loans) d'être lu comme un taux.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s in _RATE_EMPTY:
        return None
    if "%" not in s:
        return None
    m_q = _RATE_QUAL_RE.match(s)
    qual = _RATE_QUAL_NORM.get(m_q.group(1), "") if m_q else ""
    body = s[m_q.end():] if m_q else s
    m_n = _RATE_NUM_RE.search(body)
    if not m_n:
        return None
    try:
        val = float(m_n.group(0).replace(",", "."))
    except ValueError:
        return None
    return _ParsedRate(value=val, qualifier=qual,
                       display=f"{qual}{fr_num(val, 2)}%", raw=s)


def _iso_date_of(event) -> Optional[str]:
    """[N7] Date ISO ``YYYY-MM-DD`` d'un événement, quelle que soit la forme
    du champ porteur. Essaie ``datetime_utc``, ``date_display`` puis ``date``,
    accepte ``datetime`` comme ``str``. Retourne ``None`` si rien n'est
    lisible -- le caller retombe alors sur override/[N/A].

    La date rendue suit le fuseau du champ source (``datetime_utc`` est en
    UTC) : la réunion BoJ du 17/09 22:30 ET ressort donc au 18/09 UTC.
    """
    for attr in ("datetime_utc", "date_display", "date"):
        raw = getattr(event, attr, None)
        if isinstance(raw, datetime):
            return raw.date().isoformat()
        if raw:
            m = re.search(r"\d{4}-\d{2}-\d{2}", str(raw))
            if m:
                return m.group(0)
    return None


def _fr_date_from_iso(raw) -> Optional[str]:
    """'2026-09-17' -> '17/09/2026'. ``None`` si illisible (jamais
    d'exception)."""
    try:
        d = datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


def _fr_date_of_event(event) -> Optional[str]:
    iso = _iso_date_of(event)
    return _fr_date_from_iso(iso) if iso else None


def _rate_event_for_bank(events: Optional[list[MacroEvent]],
                         name: str) -> Optional[MacroEvent]:
    """[N1] Prochaine décision de ``name`` portant une valeur de taux dans le
    flux calendrier, ou ``None``.

    Seuls les événements À VENIR sont retenus : pour un événement à venir, le
    champ ``previous`` du flux est le taux EN VIGUEUR. Sur un événement déjà
    tombé, ``previous`` désigne le taux d'avant la décision, donc une valeur
    périmée d'un cran -- et le flux n'expose pas de champ ``actual`` ici.
    """
    titles = _RATE_EVENT_TITLES.get(name)
    ccy = _CB_FEED_CCY.get(name)
    if not events or not titles or not ccy:
        return None
    best: Optional[MacroEvent] = None
    best_h = float("inf")
    for e in events:
        if (getattr(e, "currency", "") or "") != ccy:
            continue
        low = str(getattr(e, "event_name", "") or "").lower()
        if not any(t in low for t in titles):
            continue
        if any(x in low for x in _RATE_EVENT_EXCLUDE):
            continue
        h = getattr(e, "hours_until", None)
        if h is None or h < 0:
            continue
        if h < best_h:
            best, best_h = e, float(h)
    return best


def _next_meeting_from_feed(events: Optional[list[MacroEvent]],
                            ccy: str, label: str) -> Optional[str]:
    """[M6] Repli : date de la prochaine décision de banque centrale telle
    qu'elle apparaît dans le flux calendrier DÉJÀ chargé.

    Corrige le « Prochaine : [N/A] » de la carte BoE, seule banque sans table
    officielle codée en dur, alors que ``Official Bank Rate`` figurait dans le
    flux au 17/09/2026. Sert aussi de filet pour FED/BCE/BoJ au-delà de
    l'horizon de leurs tables respectives.

    [N3] Les libellés de votes sont exclus. [N7] La date est extraite par
    ``_iso_date_of`` et non plus d'un ``date_display`` supposé ISO.
    """
    if not events:
        return None
    candidates = []
    for e in events:
        if (getattr(e, "currency", "") or "") != ccy:
            continue
        if not getattr(e, "is_upcoming", False):
            continue
        if not _is_cb_decision(e):
            continue
        low = str(getattr(e, "event_name", "") or "").lower()
        if any(x in low for x in _RATE_EVENT_EXCLUDE):
            continue
        candidates.append(e)
    if not candidates:
        return None
    nxt = min(candidates, key=lambda e: e.hours_until)
    disp = _fr_date_of_event(nxt)
    if disp is None:
        return None
    ev_name = (getattr(nxt, "event_name", "") or "").strip()
    suffix = f" — « {ev_name} »" if ev_name else ""
    return f"{disp} ({label}, calendrier Forex Factory{suffix})"


def _derive_bias_from_rate(name: str, live_rate_pct: Optional[float],
                           rate_label: Optional[str] = None) -> Optional[str]:
    """Fallback hawkish/dovish/neutral bias tag derived from the live policy
    rate, used when no textual override is supplied for that bank.

    AUDIT-FIX (23/07/2026, synergy gap #1): before this, ``bias`` had exactly
    one write path — the manual override — so ``_cb_link``,
    ``_currency_rationale`` and ``_cb_bias_word`` all fell back to "neutre"
    even on a day where the live FRED/BoE rate gave a usable reading.

    Returns ``None`` (never a fabricated string) when ``live_rate_pct`` is
    unavailable or the bank has no configured band, so the caller's
    "[N/A] — interprétation à confirmer." floor is preserved. The literal
    words "Hawkish"/"Dovish" are used deliberately so the three existing
    text-matching readers pick this up with zero changes on their side.

    [N2] ``rate_label`` (additif, défaut ``None`` -> ancien formatage) permet
    d'afficher la valeur AVEC son qualificateur (« <1,00% ») pour une valeur
    bornée, au lieu de la présenter comme un point.
    """
    band = C.CB_NEUTRAL_RATE_BAND.get(name)
    if live_rate_pct is None or band is None:
        return None
    low, high = band
    r = rate_label or f"{fr_num(live_rate_pct, 2)}%"
    if live_rate_pct > high:
        return (f"Hawkish — taux directeur ({r}) au-dessus de la fourchette "
                f"neutre estimée ({fr_num(low,2)}–{fr_num(high,2)}%) [dérivé taux, "
                f"pas un communiqué officiel].")
    if live_rate_pct < low:
        return (f"Dovish — taux directeur ({r}) en dessous de la fourchette "
                f"neutre estimée ({fr_num(low,2)}–{fr_num(high,2)}%) [dérivé taux, "
                f"pas un communiqué officiel].")
    return (f"Neutre — taux directeur ({r}) dans la fourchette neutre estimée "
            f"({fr_num(low,2)}–{fr_num(high,2)}%) [dérivé taux, pas un communiqué "
            f"officiel].")


def _derive_bias_from_consensus(prev: Optional[_ParsedRate],
                                fcst: Optional[_ParsedRate],
                                event) -> Optional[str]:
    """[N5] Biais dérivé du CONSENSUS de la prochaine décision.

    Retourne ``None`` -- donc ne prend jamais la main -- sauf si le flux
    publie à la fois ``previous`` et ``forecast`` ET qu'ils DIFFÈRENT, c'est-à-
    dire quand le marché attend un mouvement de taux. C'est ce cadrage qui
    rend le correctif non régressif : sur la semaine du 14/09/2026, BoE
    (3,75% vs 3,75%) reste sur la dérivation par fourchette neutre, donc le
    mot d'ouverture lu par ``_cb_bias_word`` ne change pas.

    Cas réel couvert : BoJ ``previous "<1.00%"`` / ``forecast "<1.25%"`` →
    hausse de 25 pb attendue, lecture INVERSE de celle qu'aurait produite la
    seule fourchette neutre sur un taux aussi bas.
    """
    if prev is None or fcst is None:
        return None
    if fcst.value == prev.value:
        return None
    when = _fr_date_of_event(event) or "la prochaine réunion"
    delta_bp = abs(fcst.value - prev.value) * 100.0
    bounded = (" (comparaison de bornes publiées, la banque ne communique "
               "pas un point)") if (prev.is_bounded or fcst.is_bounded) else ""
    if fcst.value > prev.value:
        word, move = "Hawkish", "hausse"
    else:
        word, move = "Dovish", "baisse"
    return (f"{word} — {move} de {fr_num(delta_bp, 0)} pb attendue le {when} : "
            f"consensus {fcst.display} contre {prev.display} en vigueur{bounded} "
            f"[consensus Forex Factory, pas un communiqué officiel].")


def _consensus_note(prev: Optional[_ParsedRate],
                    fcst: Optional[_ParsedRate],
                    event) -> str:
    """[N5] Complément TEXTUEL de statu quo, ajouté après un mot d'ouverture
    déjà décidé ailleurs. Ne contient volontairement ni « hawkish » ni
    « dovish » en tête, pour ne pas déplacer ``_cb_bias_word``."""
    if prev is None or fcst is None or fcst.value != prev.value:
        return ""
    when = _fr_date_of_event(event) or "la prochaine réunion"
    return (f" Consensus Forex Factory : statu quo attendu le {when} "
            f"({fcst.display}).")


def build_central_bank_context(overrides: Optional[dict],
                               now_utc: Optional[datetime] = None,
                               events: Optional[list[MacroEvent]] = None,
                               ) -> list[CentralBankSnapshot]:
    """Build the four central-bank blocks.

    ``events`` (additif, défaut ``None`` -- tout appelant existant à deux
    arguments continue de fonctionner) alimente le repli « prochaine réunion »
    [M6] ET, depuis [N1], la résolution du taux directeur depuis le flux.

    Précédence du TAUX (``kind``) :
      1. ``feed`` si la banque est dans ``_CB_PREFER_CALENDAR`` (BoJ : la
         série FRED amont n'est pas la bonne grandeur, cf. [N1]).
      2. ``live`` : FRED (FED/BCE/BoJ) et BoE via Bank-Rate.asp (repli IADB)
         -- ``fetch_central_bank_rates_with_meta`` [P0-2].
      3. ``feed`` si ``_RATE_FEED_BEATS_OVERRIDE`` (défaut True, cohérent avec
         la règle « live d'abord » du 15/07/2026).
      4. ``override`` : ``overrides['central_banks'][name]['rate']``.
      5. ``feed`` en dernier recours si l'override a été préféré.
      6. Sinon « [N/A] » -- jamais inventé.

    ``fact`` reste override-only (FRED ne fournit qu'un nombre, pas le FAIT
    qualitatif) ; chaîne vide quand aucun override ne l'alimente, ce que
    ``renderer._cb_biais_block`` sait déjà omettre (AUDIT-FIX 01/08/2026).

    ``bias`` : override > consensus impliquant un CHANGEMENT [N5] >
    fourchette neutre > [N/A].

    CME FedWatch (``fetch_fedwatch_probabilities``) remplit les probabilités
    pause/baisse/hausse de la Fed quand l'override les omet (inchangé).
    """
    cb_over = (overrides or {}).get("central_banks", {})

    # A6-fix: sequential calls — ThreadPoolExecutor nested inside
    # ThreadPoolExecutor caused SIGSEGV with curl_cffi/libcurl (non-thread-safe).
    # P0-2 (15/09/2026) : la version « _with_meta » est le chemin canonique —
    # elle rend (valeur, source RÉELLE) par banque et son contrat réseau est
    # identique (mêmes appels, internes au module external_sources, déjà
    # séquentiels du point de vue d'ici). ``fred_rates`` reste la vue
    # {name: float} pour toute la logique de résolution en aval (inchangée).
    rates_meta = fetch_central_bank_rates_with_meta()   # {name: (pct, source)} or {}
    fred_rates: dict[str, float] = {n: v for n, (v, _s) in rates_meta.items()}
    fedwatch = fetch_fedwatch_probabilities()   # {pause/cut/hike} or None

    out: list[CentralBankSnapshot] = []
    for name, flag, _ccy in _CB_DEFS:
        o = cb_over.get(name, {})

        # --- [N1] Données de taux disponibles dans le flux calendrier ---
        feed_ev = _rate_event_for_bank(events, name)
        feed_prev = _parse_feed_rate(getattr(feed_ev, "previous", None)) if feed_ev else None
        feed_fcst = _parse_feed_rate(getattr(feed_ev, "forecast", None)) if feed_ev else None

        live_val = fred_rates.get(name)
        over_rate = o.get("rate")
        prefer_feed = name in _CB_PREFER_CALENDAR

        # --- Résolution du taux affiché ---
        kind: Optional[str]
        if feed_prev is not None and prefer_feed:
            kind = "feed"
        elif live_val is not None:
            kind = "live"
        elif feed_prev is not None and _RATE_FEED_BEATS_OVERRIDE:
            kind = "feed"
        elif over_rate is not None:
            kind = "override"
        elif feed_prev is not None:
            kind = "feed"
        else:
            kind = None

        if kind == "feed":
            rate = feed_prev.display
            resolved_val: Optional[float] = feed_prev.value
            rate_label: Optional[str] = feed_prev.display
            if live_val is not None and prefer_feed:
                logger.info(
                    "%s: taux du flux calendrier (%s) préféré à la valeur "
                    "amont (%.2f) — série amont inadaptée, cf. [N1]",
                    name, feed_prev.display, live_val)
        elif kind == "live":
            rate = f"{fr_num(live_val, 2)}%"
            resolved_val = live_val
            rate_label = rate
        elif kind == "override":
            rate = str(over_rate)
            parsed_over = _parse_rate_pct(rate)
            resolved_val = parsed_over
            rate_label = rate
        else:
            rate = "[N/A]"
            resolved_val = None
            rate_label = None

        fact = o.get("fact", "")

        # --- [N5] Biais ---
        band_bias = _derive_bias_from_rate(name, resolved_val, rate_label=rate_label)
        change_bias = _derive_bias_from_consensus(feed_prev, feed_fcst, feed_ev)
        if o.get("bias"):
            bias = o["bias"]
        elif change_bias:
            # Le mouvement attendu prime sur le niveau ; la lecture de niveau
            # est conservée en contexte, jamais perdue.
            bias = change_bias + (f" Lecture de niveau : {band_bias}" if band_bias else "")
        elif band_bias:
            bias = band_bias + _consensus_note(feed_prev, feed_fcst, feed_ev)
        else:
            bias = "[N/A] — interprétation à confirmer."

        # P0 FIX (audit 23/07/2026): computed official calendar takes
        # precedence over a manual override, same rule as the live rate.
        computed_next = None
        if name == "FED":
            computed_next = _next_meeting_from_table(now_utc, _FOMC_MEETING_DATES, "FED")
        elif name == "BCE":
            computed_next = _next_ecb_meeting(now_utc)
        elif name == "BoJ":
            computed_next = _next_meeting_from_table(now_utc, _BOJ_MEETING_DATES, "BoJ")
        # [M6] Précédence : table officielle > override > flux > [N/A].
        # Le flux passe APRÈS l'override, contrairement à la table : une
        # saisie manuelle délibérée reste plus autoritaire qu'une dérivation
        # par mots-clés sur un libellé de flux. Seul le cas [N/A] change.
        feed_next = _next_meeting_from_feed(
            events, _CB_FEED_CCY.get(name, ""), name)
        nxt = computed_next or o.get("next") or feed_next or "[N/A]"

        # --- Fed probabilities: override > FedWatch > None ---
        pause = o.get("pause")
        cut = o.get("cut")
        hike = o.get("hike")
        fw_used = False
        fw_as_of = None
        if name == "FED" and pause is None and cut is None and hike is None and fedwatch:
            pause = fedwatch.get("pause_pct")
            cut = fedwatch.get("cut_pct")
            hike = fedwatch.get("hike_pct")
            fw_used = True
            fw_as_of = fedwatch.get("as_of")

        # --- [N4] Stamp : reflète l'origine RÉELLE de la valeur affichée ---
        if kind == "live":
            # P0-2 : provenance réellement servie ce run (BoE scrape vs IADB),
            # avec repli sur l'étiquette du chemin principal si la méta est
            # muette (ne peut arriver que pour un appelant fantôme de
            # fetch_central_bank_rates — jamais par ce chemin).
            src = rates_meta.get(name, (None, None))[1] or central_bank_rate_source(name)
            if fw_used:
                src = (src + " + CME FedWatch").strip(" +")
            stamp = SourceStamp(src or "external", Reliability.PRIMARY, timestamp=now_utc)
        elif kind == "feed":
            ev_name = (getattr(feed_ev, "event_name", "") or "décision de taux").strip()
            src = f"Forex Factory · {ev_name}"
            if fw_used:
                src += " + CME FedWatch"
            stamp = SourceStamp(
                src, Reliability.FALLBACK, timestamp=now_utc,
                note=("taux en vigueur = champ « previous » de la prochaine "
                      "décision au calendrier ; valeur bornée telle que publiée"
                      if feed_prev is not None and feed_prev.is_bounded
                      else "taux en vigueur = champ « previous » de la prochaine "
                           "décision au calendrier"),
            )
        elif kind == "override":
            stamp = proxy_stamp("manual override")
        elif fw_used:
            # Probabilités FedWatch obtenues alors que le taux reste [N/A] :
            # la source existe, mais pas pour le champ « taux ».
            stamp = SourceStamp("CME FedWatch", Reliability.PRIMARY, timestamp=now_utc,
                                note="probabilités sourcées, taux directeur non sourcé")
        elif feed_ev is not None:
            # [N4] Cas manquant jusqu'ici : la décision EST au calendrier mais
            # le flux ne publie pas de valeur exploitable pour cette banque.
            stamp = na_stamp("décision au calendrier, valeur de taux non publiée dans le flux")
        elif fred_rates:
            # [M4] Au moins un taux a été résolu en amont sur ce run : la clé
            # API et le réseau fonctionnent. L'absence de CELUI-CI vient donc
            # d'un rejet côté external_sources (fraîcheur / série inadaptée),
            # pas d'un défaut d'authentification. L'ancien libellé unique
            # « source sans clé API » affirmait le contraire.
            stamp = na_stamp("série rejetée en amont (fraîcheur / série inadaptée) — voir logs")
        else:
            # Aucun taux résolu, toutes banques confondues : cause globale.
            stamp = na_stamp("aucune source live disponible (clé API ou réseau)")

        out.append(CentralBankSnapshot(
            name=name, flag=flag, rate_display=str(rate),
            fact=str(fact), bias_interpretation=str(bias), next_meeting=str(nxt),
            stamp=stamp,
            pause_pct=pause, cut_pct=cut, hike_pct=hike,
            fedwatch_as_of=fw_as_of,
            proba_from_override=(
                name == "FED" and not fw_used
                and any(p is not None for p in (pause, cut, hike))
            ),
        ))
    return out


_HAWKISH_OPEN_RE = re.compile(r"^\s*(très\s+)?hawkish\b")
_DOVISH_OPEN_RE = re.compile(r"^\s*(très\s+)?dovish\b")
_HAWKISH_ANY_RE = re.compile(r"\bhawkish\b")
_DOVISH_ANY_RE = re.compile(r"\bdovish\b")


def _parse_rate_pct(rate_display) -> Optional[float]:
    """Parse a CB rate string into a float percentage.

    [N2] CORRECTIF. L'ancienne implémentation
    (``s.lstrip("~").rstrip("%")`` puis ``re.split(r"[–-]", s)``) avait deux
    défauts vérifiés :
      - « <1,00% » (forme réelle du BOJ Policy Rate dans le flux Forex
        Factory) levait ``ValueError`` → ``None`` → la BoJ était exclue du
        différentiel de taux même son taux résolu ;
      - « -0,10% » était découpé en ``["", "0.10"]`` → +0,10 au lieu de
        −0,10, inversion de signe sur tout taux négatif.

    Gère : valeur simple, fourchette (« 3,50–3,75% » → point milieu),
    qualificateur de tête (« ~ », « < », « ≤ », « ± »), signe négatif.
    Retourne ``None`` pour tout ce qui n'est pas exploitable (« [N/A] »).
    """
    if not rate_display:
        return None
    s = str(rate_display).strip()
    if s in _RATE_EMPTY:
        return None
    s = _RATE_QUAL_RE.sub("", s).strip()
    # Normalisation des séparateurs de fourchette. Le tiret n'est traité comme
    # séparateur que s'il est ENTRE deux chiffres, ce qui préserve le signe.
    s = s.replace("\u2013", "|").replace("\u2014", "|")
    s = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "|", s)
    vals: list[float] = []
    for seg in s.split("|"):
        m = _RATE_NUM_RE.search(seg)
        if not m:
            continue
        try:
            vals.append(float(m.group(0).replace(",", ".")))
        except ValueError:
            continue
    return sum(vals) / len(vals) if vals else None


_RATE_USABLE_RELIABILITY = (Reliability.PRIMARY, Reliability.FALLBACK, Reliability.PROXY)


def _build_rate_differential(central_banks: list[CentralBankSnapshot]) -> tuple[str, str]:
    """Dominant policy-rate differential among the tracked central banks.

    [M5] CORRECTIF : ``max(rates, key=rates.get)`` et ``min(...)`` renvoient
    tous deux la PREMIÈRE clé rencontrée quand plusieurs valeurs sont égales.
    Avec Fed et BoE toutes deux à 3,75% (cas réel du 14/09/2026), ``ccy_hi``
    et ``ccy_lo`` valaient « USD » et la ligne affichait « USD (3,75%) vs USD
    (3,75%) → écart ≈ 0,00 pt ». Effet de bord :
    ``sorted({hi.source_name, lo.source_name})`` s'effondrait sur un seul
    élément et le tag devenait « [FRED · PRIMARY] » alors que la jambe GBP
    provient de la Bank of England IADB. Le tri explicite sur (valeur, code
    devise) garantit deux devises DISTINCTES dès qu'au moins deux taux sont
    disponibles.

    [N8] Appariement par ``cb.name`` au lieu de ``zip(_CB_DEFS,
    central_banks)`` (dépendance à l'ordre), et éligibilité fondée sur la
    valeur réellement parsable + une fiabilité connue, de sorte qu'un stamp
    FALLBACK (nouveau depuis [N1]) ne soit pas écarté silencieusement.

    [N2] Les valeurs sont affichées telles que publiées (``rate_display``),
    donc « <1,00% » reste borné à l'écran ; l'écart numérique est calculé sur
    les bornes et signalé comme tel.
    """
    rates: dict[str, float] = {}
    displays: dict[str, str] = {}
    stamps: dict[str, SourceStamp] = {}
    bounded: set[str] = set()
    for cb in central_banks:
        ccy = _CCY_BY_CB.get(cb.name)
        if not ccy:
            continue
        stamp = getattr(cb, "stamp", None)
        if stamp is None:
            continue
        if getattr(stamp, "reliability", None) not in _RATE_USABLE_RELIABILITY:
            continue
        r = _parse_rate_pct(cb.rate_display)
        if r is None:
            continue
        rates[ccy] = r
        displays[ccy] = str(cb.rate_display)
        stamps[ccy] = stamp
        if _RATE_QUAL_RE.match(str(cb.rate_display)):
            bounded.add(ccy)

    if len(rates) < 2:
        return ("[N/A] — taux directeurs non sourcés (saisir en overrides).",
                "Différentiel non calculable sans au moins 2 taux sourcés.")

    ordered = sorted(rates.items(), key=lambda kv: (kv[1], kv[0]))
    ccy_lo, rate_lo = ordered[0]
    ccy_hi, rate_hi = ordered[-1]
    gap = rate_hi - rate_lo

    hi_stamp, lo_stamp = stamps[ccy_hi], stamps[ccy_lo]
    both_live = (hi_stamp.reliability is Reliability.PRIMARY and
                lo_stamp.reliability is Reliability.PRIMARY)
    both_proxy = (hi_stamp.reliability is Reliability.PROXY and
                 lo_stamp.reliability is Reliability.PROXY)
    if both_live:
        src_names = sorted({hi_stamp.source_name, lo_stamp.source_name})
        tag = f"[{' + '.join(src_names)} · PRIMARY]"
    elif both_proxy:
        tag = "[PROXY · taux saisis en overrides]"
    else:
        def _leg_tag(stamp: SourceStamp) -> str:
            if stamp.reliability is Reliability.PRIMARY:
                return stamp.source_name or "PRIMARY"
            if stamp.reliability is Reliability.PROXY:
                return "override"
            if stamp.reliability is Reliability.FALLBACK:
                return stamp.source_name or "FALLBACK"
            return stamp.source_name or stamp.reliability.value
        tag = f"[MIXTE · {ccy_hi} {_leg_tag(hi_stamp)} + {ccy_lo} {_leg_tag(lo_stamp)}]"

    caveat = ""
    if bounded & {ccy_hi, ccy_lo}:
        caveat = (" — écart calculé sur les bornes publiées, pas sur des "
                  "points de taux")
    dominant = (f"{ccy_hi} ({displays[ccy_hi]}) vs {ccy_lo} "
               f"({displays[ccy_lo]}) → écart ≈ {fr_num(gap, 2)} pt "
               f"{tag}{caveat}")
    if gap == 0:
        implication = f"Taux directeurs identiques ({ccy_hi}/{ccy_lo}) — pas de portage net entre les deux."
    else:
        implication = (f"Portage structurellement favorable à {ccy_hi} face à {ccy_lo} "
                       "tant que cet écart de taux directeurs persiste.")
    return dominant, implication


def _cb_bias_word(cb: CentralBankSnapshot) -> int:
    """Map a CB bias string to a strength delta (+hawkish / -dovish).

    The bias field is free text. A plain ``"hawkish" in text`` match treats an
    outright stance and a hedged "leaning hawkish" identically -- that's how
    BoE's "Neutre à légèrement hawkish" used to land on the same +12 as the
    Fed's unqualified "Hawkish", producing a false tie. The tone word must
    *open* the sentence for the full +/-12; appearing later or softened counts
    as a weaker +/-6.

    [N5] Compatible avec le biais dérivé du consensus : celui-ci commence
    délibérément par « Hawkish » ou « Dovish », et le complément de niveau
    ajouté après ne peut pas renverser le score puisque la correspondance
    d'OUVERTURE est testée en premier.
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
# Step 5b -- Currency Strength Ranking (Oanda primary, CB-bias fallback)
# ---------------------------------------------------------------------------
def _oanda_strength_scores(
    market: MarketSnapshot,
    cb_ranking: list[CurrencyStrength],
) -> list[CurrencyStrength]:
    """Merge Oanda D1 relative-strength scores into the CB-bias ranking.

    If ``market.currency_strength_oanda`` is present (dict with 8 major
    currencies on a 0-10 scale, 5.0 neutral), convert to 0-100 and replace
    the CB-bias scores.  The sort order is re-established by score.
    If the attribute is absent or empty, return ``cb_ranking`` unchanged.
    """
    oanda = getattr(market, "currency_strength_oanda", None)
    if not oanda:
        return cb_ranking

    cb_by_ccy = {r.currency: r for r in cb_ranking}
    rows: list[CurrencyStrength] = []

    for ccy in C.MAJOR_CURRENCIES:
        v10 = oanda.get(ccy)
        if v10 is None:
            fallback = cb_by_ccy.get(ccy)
            if fallback:
                rows.append(fallback)
            else:
                rows.append(CurrencyStrength(ccy, 50, "neutre [PROXY]", "neutral"))
            continue

        score_100 = int(round(50.0 + (v10 - 5.0) / 5.0 * 50.0))
        score_100 = max(0, min(100, score_100))
        cls = "strong" if score_100 >= 60 else "weak" if score_100 <= 40 else "neutral"
        rows.append(CurrencyStrength(
            currency=ccy,
            score=score_100,
            driver="Oanda D1",
            css_class=cls,
        ))

    rows.sort(key=lambda r: r.score, reverse=True)
    return rows


def build_currency_strength_ranking(
    central_banks: list[CentralBankSnapshot],
    regime_class: str,
) -> list[CurrencyStrength]:
    """Qualitative 0-100 score per major currency. Always [PROXY].

    [N8] Appariement banque → devise par ``cb.name``, sans dépendance à
    l'ordre des deux listes.
    """
    cb_by_ccy = {_CCY_BY_CB[cb.name]: cb for cb in central_banks
                 if cb.name in _CCY_BY_CB}
    scores: dict[str, int] = {c: 50 for c in C.MAJOR_CURRENCIES}

    for ccy in C.MAJOR_CURRENCIES:
        cb = cb_by_ccy.get(ccy)
        if cb is not None:
            scores[ccy] += _cb_bias_word(cb)
        if regime_class == "regime-off" and ccy in C.SAFE_HAVENS:
            scores[ccy] += 10
        if regime_class == "regime-on" and ccy in C.SAFE_HAVENS:
            scores[ccy] -= 6

    ranked = sorted(C.MAJOR_CURRENCIES, key=lambda c: scores[c], reverse=True)
    rows: list[CurrencyStrength] = []
    for ccy in ranked:
        s = max(0, min(100, scores[ccy]))
        cls = "strong" if s >= 60 else "weak" if s <= 40 else "neutral"
        rows.append(CurrencyStrength(ccy, s, "", cls))
    return rows


# ---------------------------------------------------------------------------
# Step 5c -- IPS (Non-Commercials only)
# ---------------------------------------------------------------------------
def _reference_cftc_friday(now_utc: datetime) -> datetime:
    """Return the CFTC Friday whose report is the current authoritative reference.

    The CFTC publishes Non-Commercials data every Friday at approximately
    15:30 ET.  Until the next Friday's publication, the previous Friday's
    report remains the official reference (Règle Absolue n°3).

    Returns a datetime set to 15:30 ET on the reference Friday (UTC-aware).
    """
    from datetime import timedelta
    now_et = now_utc.astimezone(C.TZ_ET)
    days_since_friday = (now_et.weekday() - 4) % 7
    candidate = now_et.replace(hour=15, minute=30, second=0, microsecond=0) \
                - timedelta(days=days_since_friday)
    if now_et < candidate:
        candidate -= timedelta(days=7)
    return candidate


def _ips_label_for(score: int) -> str:
    """Map an IPS score to its label."""
    if score >= C.IPS_CROWDED:
        return "Crowded long"
    if score <= C.IPS_CAPITULATION:
        return "Crowded short / Capitul."
    return "Normal"


def _ips_from_institutional(ref_label: str,
                            now_utc: Optional[datetime] = None) -> list[CotPositioning]:
    """Path 1: real z-scores/percentiles from institutional layer."""
    if _inst is None:
        return []
    try:
        stats = _inst.fetch_positioning_stats()
    except Exception as exc:
        logger.warning("institutional.fetch_positioning_stats failed: %s", exc)
        return []
    if not stats:
        return []
    rows = []
    for ccy in C.MAJOR_CURRENCIES:
        stat = stats.get(ccy)
        if stat is None:
            continue
        score = max(0, min(100, int(stat.percentile) if stat.percentile is not None else 50))
        delta_str = f"Δ1s {stat.change_1w:+d}" if stat.change_1w is not None else "≈ stable"
        rows.append(CotPositioning(
            currency=ccy, net_contracts=stat.net, ips_score=score,
            ips_label=_ips_label_for(score), delta_week=delta_str,
            momentum="↑" if (stat.change_1w or 0) > 0 else "↓" if (stat.change_1w or 0) < 0 else "→",
            stamp=SourceStamp(
                f"OBSERVÉ — CFTC Non-Commercials | z={stat.zscore} | {int(stat.percentile)}e pct | {stat.report_date}",
                Reliability.PRIMARY,
                timestamp=now_utc,
                note=f"z-score {stat.zscore}, percentile {int(stat.percentile) if stat.percentile is not None else '?'}",
            ),
        ))
    return rows


def _ips_from_overrides(cot_over: dict, ref_label: str) -> list[CotPositioning]:
    """Path 2: user overrides with linear scaling [PROXY]."""
    rows = []
    for ccy in C.MAJOR_CURRENCIES:
        o = cot_over.get(ccy)
        if o is None or o.get("net") is None:
            continue
        net = int(o["net"])
        frac = max(-1.0, min(1.0, net / C.IPS_FULL_SCALE_CONTRACTS))
        score = int(round(50 + frac * 50))
        rows.append(CotPositioning(
            currency=ccy, net_contracts=net, ips_score=score,
            ips_label=_ips_label_for(score),
            delta_week=o.get("delta", "≈ stable"), momentum=o.get("momentum", "→"),
            stamp=SourceStamp(f"{ref_label} [PROXY · scaling linéaire]", Reliability.PROXY,
                              note="scaling linéaire 150k contrats — PAS un vrai percentile"),
        ))
    return rows


def _ips_from_scrape(ref_date_str: str) -> list[CotPositioning]:
    """Path 3: live CFTC scrape with linear scaling [PROXY]."""
    ext_net, external_date = fetch_cot_data()
    if not ext_net:
        return []
    rows = []
    for ccy in C.MAJOR_CURRENCIES:
        net = ext_net.get(ccy)
        if net is None:
            continue
        frac = max(-1.0, min(1.0, net / C.IPS_FULL_SCALE_CONTRACTS))
        score = int(round(50 + frac * 50))
        # MACRO-B3 FIX : retrait du mot "OBSERVÉ" trompeur. Ce n'est pas un
        # percentile, c'est un scaling linéaire arbitraire (150k contrats).
        src_label = f"CFTC Non-Commercials | {external_date or ref_date_str} [PROXY · scaling linéaire]"
        rows.append(CotPositioning(
            currency=ccy, net_contracts=net, ips_score=score,
            ips_label=_ips_label_for(score), delta_week="≈ stable", momentum="→",
            stamp=SourceStamp(src_label, Reliability.PROXY,
                              note="scaling linéaire — PAS un vrai percentile"),
        ))
    return rows


def build_ips_scores(overrides: Optional[dict],
                     now_utc: datetime) -> tuple[list[CotPositioning], str]:
    """Build IPS rows from COT data — real z-scores/percentiles when available.

    Sourcing precedence (audit B4 fix):
    1. ``institutional.fetch_positioning_stats`` — real z-scores/percentiles.
    2. User override (``overrides["cot"]``) — linear scaling [PROXY].
    3. Live CFTC scrape (``fetch_cot_data``) — linear scaling [PROXY].
    4. [N/A] when no COT data is available.
    """
    ref = _reference_cftc_friday(now_utc)
    ref_date_str = (
        f"{FR_DAYS[ref.weekday()]} "
        f"{ref.day} {FR_MONTHS[ref.month]} {ref.year}"
    )
    ref_label = f"CFTC Non-Commercials | {ref_date_str}"

    cot_over = (overrides or {}).get("cot", {})

    rows = _ips_from_institutional(ref_label, now_utc)
    if not rows and cot_over:
        rows = _ips_from_overrides(cot_over, ref_label)
    if not rows:
        rows = _ips_from_scrape(ref_date_str)

    return rows, ref_label


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
_CORR_BENCHMARK: dict[str, str] = {
    "EUR/USD": "DXY", "GBP/USD": "DXY", "AUD/USD": "DXY", "NZD/USD": "DXY",
    "USD/CAD": "DXY", "USD/CHF": "DXY", "EUR/GBP": "DXY",
    "USD/JPY": "US10Y", "GBP/JPY": "US10Y",
    "XAU/USD": "US10Y", "Brent": "DXY", "WTI": "DXY",
    "DAX": "VIX", "US30": "VIX", "NAS100": "VIX", "SPX500": "VIX",
}


def _resolve_corr_sig_min() -> float:
    """Seuil de significativité de |r|.

    L'ancienne écriture ``getattr(C, "A", None) or getattr(C, "B", 0.2)``
    ignorait silencieusement une valeur configurée à 0.0 (falsy). Test
    explicite contre ``None``, et repli documenté sur 0.2.
    """
    for attr in ("CORRELATION_SIGNIFICANCE_MIN", "CORR_SIGNIFICANCE_MIN"):
        val = getattr(C, attr, None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                logger.warning("config.%s illisible (%r) — repli 0.2", attr, val)
                break
    return 0.2


_CORR_SIG_MIN = _resolve_corr_sig_min()


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
    """Asset-card '9. Corrélation clé' field -- a real, computed [PROXY]."""
    res = _correlation_value(asset, market)
    if res is None:
        if asset not in _CORR_BENCHMARK:
            return "[N/A] — pas de référence définie pour cet actif."
        return "[N/A] — corrélation indisponible (historique insuffisant)."
    r, bench, n = res
    sign = "+" if r >= 0 else "−"
    if abs(r) < _CORR_SIG_MIN:
        return (f"≈ {sign}{fr_num(abs(r), 2)} {bench} — non significative "
                f"(|r|<{_CORR_SIG_MIN:.1f}) [Pearson · {n} séances]")
    return f"≈ {sign}{fr_num(abs(r), 2)} {bench} [Pearson · {n} séances]"


def _correlation_short(asset: str, market: MarketSnapshot) -> str:
    """Compact form for the Section 3 overlay row."""
    res = _correlation_value(asset, market)
    if res is None:
        return f"{asset} n/d"
    r, bench, _n = res
    sign = "+" if r >= 0 else "−"
    if abs(r) < _CORR_SIG_MIN:
        return f"{asset} n/s ({bench})"
    return f"{asset} ≈ {sign}{fr_num(abs(r), 2)} {bench}"


# ---------------------------------------------------------------------------
# Step 6 -- Asset selection
# ---------------------------------------------------------------------------
# [M2] Horizon utilisé UNIQUEMENT pour nommer les catalyseurs dans le texte
# « Risque d'invalidation ». Aligné sur l'horizon de veille annoncé par la
# couche calendrier : le rapport déclarait surveiller 168h mais n'en servait
# que 72 pour ce libellé, d'où une réunion BoJ à ~85h invisible sur la fiche
# USD/JPY. Ne touche NI le scoring (``ev``), NI le gating (``blackout_ev``).
_CATALYST_LABEL_HORIZON_H: float = float(getattr(cal, "FF_WATCH_HORIZON_H", 168.0))


def _events_for_ccys(events: list[MacroEvent], ccys: tuple[str, ...],
                     within_h: float = 72, since_h: float = 6) -> list[MacroEvent]:
    """``since_h`` (défaut 6, comportement historique inchangé) borne la
    fenêtre rétrospective utilisée pour le scoring/l'affichage (catalyst_pen,
    ev_names) -- INCHANGÉ, zero-régression sur ces deux usages.

    PATCH-CALGATE-F2 (31/07/2026) : le SEUL appelant qui a besoin d'une
    fenêtre plus large est le gating de blackout (``is_blackout`` couvre
    jusqu'à -48h pour un event Tier S). Avant ce correctif,
    ``select_priority_assets`` appelait cette fonction UNE SEULE fois avec
    ``since_h`` à 6 et réutilisait le résultat tronqué à la fois pour le
    scoring ET pour le blackout.

    [M2] (14/09/2026) : un TROISIÈME appel, à fenêtre PROSPECTIVE large
    (``within_h=_CATALYST_LABEL_HORIZON_H``), alimente désormais le seul
    libellé d'invalidation -- voir ``select_priority_assets``."""
    return [e for e in events
            if e.currency in ccys and -since_h <= e.hours_until <= within_h]


def _strength_map(cs: list[CurrencyStrength]) -> dict[str, int]:
    return {r.currency: r.score for r in cs}


def _strength_source_map(cs: list[CurrencyStrength]) -> dict[str, bool]:
    """True where a currency's score came from live Oanda D1 data."""
    return {r.currency: (r.driver == "Oanda D1") for r in cs}


_PRICE_RELIABILITY_WEIGHT = {
    Reliability.PRIMARY:  1.00,
    Reliability.FALLBACK: 0.85,
    Reliability.PROXY:    0.70,
}


def _compute_direction_edge(
    asset: str, ccys, smap: dict, regime_class: str,
    smap_src: Optional[dict] = None,
) -> tuple[int, float, bool]:
    """Compute directional edge for an asset from currency strength or regime tilt.

    Returns ``(direction, edge, source_is_live)``. ``source_is_live`` is True
    only when *both* legs of the pair are scored from live Oanda D1 data;
    regime-tilt edges (commodities/indices, no ``ccys``) stay False.

    CORRECTIF (02/08/2026) : seul un tuple à EXACTEMENT 2 éléments (une vraie
    paire FX) emprunte la branche de différentiel de force ; tout le reste
    retombe sur la branche "regime tilt". Sans ce garde-fou,
    ``base, quote = ccys`` levait ``ValueError``."""
    if ccys and len(ccys) == 2:
        base, quote = ccys
        diff = smap.get(base, 50) - smap.get(quote, 50)
        edge = abs(diff) / 50.0
        direction = 1 if diff > 0 else -1 if diff < 0 else 0
        source_is_live = bool(smap_src) and smap_src.get(base, False) and smap_src.get(quote, False)
        return direction, edge, source_is_live
    # Commodities / indices: weak regime tilt only
    if regime_class == "regime-off":
        if asset == "XAU/USD":
            return 1, 0.25, False
        if asset in C.INDICES:
            return -1, 0.25, False
    elif regime_class == "regime-on" and asset in C.INDICES:
        return 1, 0.2, False
    return 0, 0.0, False


def _compute_asset_score(edge: float, price, atr, ev: list) -> float:
    """Compute the composite selection score for an asset."""
    price_avail = _PRICE_RELIABILITY_WEIGHT.get(price.stamp.reliability, 0.85)
    vol_ok = 1.0 if atr is not None else 0.6
    catalyst_pen = 0.15 * len([e for e in ev if e.priority == "HIGH"])
    score = 0.45 * edge + 0.25 * price_avail + 0.2 * vol_ok - min(0.3, catalyst_pen)
    return max(0.0, min(1.0, score))


def _apply_correlation_guard(
    scored: list[tuple[float, AssetSetup]],
) -> list[AssetSetup]:
    """Select top assets with currency overlap guard (audit A4 fix)."""
    priority: list[AssetSetup] = []
    selected_ccys: set[str] = set()
    for _score, setup in scored:
        if len(priority) >= C.MAX_PRIORITY_ASSETS:
            break
        ccys_set = set(C.INSTRUMENT_CCYS.get(setup.asset, ()))
        overlap = ccys_set & selected_ccys
        # PATCH-CORRGUARD-F3 (31/07/2026) : seuil >=1 devise commune, seul
        # seuil qui peut matériellement se déclencher pour des paires à 2
        # jambes (l'ancien >=2 était structurellement inatteignable).
        if overlap and len(priority) > 0:
            continue
        priority.append(setup)
        selected_ccys.update(ccys_set)
    # PATCH-CORRGUARD-F3 (suite) : la boucle de repli s'arrête au même
    # plancher que la condition qui la déclenche -- au maximum 1 actif
    # corrélé réintroduit, jamais 2.
    floor = min(2, C.MAX_PRIORITY_ASSETS)
    if len(priority) < floor:
        for _score, setup in scored:
            if len(priority) >= floor:
                break
            if setup not in priority:
                priority.append(setup)
    return priority


def select_priority_assets(
    market: MarketSnapshot,
    regime_class: str,
    central_banks: list[CentralBankSnapshot],
    currency_strength: list[CurrencyStrength],
    ips: list[CotPositioning],
    events: list[MacroEvent],
    mode: str,
    allow_proxy_levels: bool,
    cot_label: str = "[PROXY]",
) -> tuple[list[AssetSetup], list[tuple[str, str]], Optional[str]]:
    """Score the universe, return (priority[<=3], avoid, no_setup_reason)."""
    smap = _strength_map(currency_strength)
    smap_src = _strength_source_map(currency_strength)
    ips_by_ccy = {r.currency: r for r in ips}
    min_score = C.MODE_SELECTION_MIN_SCORE.get(mode, 0.5)

    scored: list[tuple[float, AssetSetup]] = []
    avoid: list[tuple[str, str]] = []

    for asset in C.UNIVERSE:
        price = market.price(asset)
        if not price.available:
            continue

        ccys = C.INSTRUMENT_CCYS.get(asset)
        # (a) SCORING -- fenêtre historique inchangée (72h / -6h).
        ev = _events_for_ccys(events, ccys) if ccys else []
        # (b) GATING BLACKOUT -- fenêtre rétrospective large (PATCH-CALGATE-F2).
        blackout_ev = _events_for_ccys(events, ccys, since_h=72) if ccys else []
        # (c) [M2] LIBELLÉ D'INVALIDATION -- fenêtre prospective large (168h),
        #     strictement descriptive : n'entre dans AUCUN calcul de score ni
        #     dans aucune décision de gating.
        ev_label = (_events_for_ccys(events, ccys,
                                     within_h=_CATALYST_LABEL_HORIZON_H,
                                     since_h=0)
                    if ccys else [])

        blackout_hits = [
            (e, *cal.is_blackout(e.event_name, e.hours_until)) for e in blackout_ev
        ]
        blackout_hits = [(e, tier) for e, blocked, tier in blackout_hits if blocked]

        if blackout_hits:
            # PATCH-C10 (31/07/2026) : afficher TOUS les hits, pas seulement
            # le premier -- un double blackout (deux jambes) était masqué.
            parts = []
            for e, tier in blackout_hits:
                ename = e.event_name or "événement majeur"
                if e.hours_until >= 0:
                    parts.append(f"News binaire imminente ({ename}, tier {tier}) — attendre la publication.")
                else:
                    parts.append(f"Blackout post-événement en cours ({ename}, tier {tier}, "
                                 f"tombé il y a {abs(e.hours_until):.1f}h) — risque résiduel non purgé.")
            reason = " + ".join(parts)
            avoid.append((asset, reason))
            continue

        direction, edge, strength_is_live = _compute_direction_edge(
            asset, ccys, smap, regime_class, smap_src)
        if direction == 0:
            continue

        atr = market.atr.get(asset)
        score = _compute_asset_score(edge, price, atr, ev)
        if score < min_score:
            continue

        setup = _build_setup(asset, direction, score, market, allow_proxy_levels,
                             ips_by_ccy, ccys, ev, cot_label, strength_is_live,
                             ev_label=ev_label)
        scored.append((score, setup))

    scored.sort(key=lambda t: t[0], reverse=True)
    priority = _apply_correlation_guard(scored)

    no_setup = None
    if not priority:
        no_setup = ("Aucun actif ne réunit un biais macro directionnel suffisant "
                    "avec des niveaux exploitables aujourd'hui — données de prix/positionnement "
                    "insuffisantes ou régime sans edge. Conformément à BLUESTAR v8.1, aucun "
                    "setup n'est forcé.")
    return priority, avoid, no_setup


def _build_setup_levels(p: float, atr, direction: int, asset: str) -> tuple:
    """Compute buy/sell/stop levels from ATR. Returns (buy, sell, stop)."""
    buy = p - C.LEVEL_ATR_MULT * atr if atr else None
    sell = p + C.LEVEL_ATR_MULT * atr if atr else None
    if direction > 0:
        stop = (p - C.STOP_ATR_MULT * atr) if atr else None
    else:
        stop = (p + C.STOP_ATR_MULT * atr) if atr else None
    return buy, sell, stop


# Currencies with no standalone CFTC Non-Commercials contract in
# institutional.CFTC_MARKETS. Kept as a mechanism (not deleted) in case a
# currency's CFTC coverage is ever lost again.
_NO_STANDALONE_CFTC_CONTRACT: set[str] = set()


def _build_setup_positioning(ccys, ips_by_ccy: dict, cot_label: str) -> tuple:
    """Compute positioning link, squeeze risk, IPS summary, both-legs-extreme flag."""
    pos_link = "Pas de COT chargé pour cet actif [N/A] — positionnement non pris en compte."
    squeeze_risk, squeeze_cls = "Faible", "green"
    ips_summary = "[N/A]"

    def _append_untracked_note(summary: str, link: str) -> tuple[str, str]:
        untracked = [c for c in (ccys or []) if c in _NO_STANDALONE_CFTC_CONTRACT]
        if not untracked:
            return summary, link
        note = (f" · {', '.join(untracked)} : IPS [N/A] — pas de contrat CFTC "
                f"autonome (contrepartie implicite des 7 majors ; positionnement "
                f"non mesuré ici, cf. USD Index ICE non intégré).")
        return summary + note, link + note

    if not ccys:
        return pos_link, squeeze_risk, squeeze_cls, ips_summary, False
    candidates = [(ccy, ips_by_ccy.get(ccy)) for ccy in ccys]
    candidates = [(ccy, r) for ccy, r in candidates if r and r.ips_score is not None]
    extreme_candidates = [c for c in candidates if c[1].is_extreme]
    both_extreme = len(extreme_candidates) >= 2
    chosen = next((c for c in candidates if c[1].is_extreme),
                  candidates[0] if candidates else None)
    if not chosen:
        pos_link, ips_summary = _append_untracked_note(ips_summary, pos_link)
        return pos_link, squeeze_risk, squeeze_cls, ips_summary, False
    ccy, r = chosen
    ips_summary = f"{ccy} {r.ips_score} — {r.ips_label}"
    if both_extreme:
        other_ccy, other_r = next(c for c in extreme_candidates if c[0] != ccy)
        ips_summary = (f"{ccy} {r.ips_score} ({r.ips_label}) · "
                       f"{other_ccy} {other_r.ips_score} ({other_r.ips_label}) — DEUX jambes extrêmes")
        squeeze_risk, squeeze_cls = f"Élevé (2 jambes extrêmes : {ccy}/{other_ccy})", "red"
        pos_link = (f"{ccy} ET {other_ccy} en zone extrême simultanément "
                    f"(IPS {r.ips_score} / {other_r.ips_score}). [{cot_label}]. "
                    "Les deux devises de la paire sont crowded — le squeeze peut se "
                    "déclencher dans les deux sens ; conviction plafonnée, stop strict.")
    elif r.is_extreme:
        squeeze_risk, squeeze_cls = f"Élevé ({ccy} IPS extrême)", "red"
        pos_link = (f"{ccy} en zone extrême (IPS {r.ips_score} · {r.ips_label}). "
                    f"[{cot_label}]. "
                    "Le COT ne déclenche pas le trade ; il signale un risque de "
                    "squeeze inverse si le catalyseur déçoit → conviction ±, stop strict.")
    else:
        pos_link = (f"{ccy} IPS {r.ips_score} — {r.ips_label}. "
                    f"[{cot_label}]. "
                    "Positionnement non extrême, n'amende pas la conviction.")
    ips_summary, pos_link = _append_untracked_note(ips_summary, pos_link)
    return pos_link, squeeze_risk, squeeze_cls, ips_summary, both_extreme


def _compute_rr_ratio(p: float, stop, sell, direction: int, atr) -> str:
    """Compute risk/reward ratio display string (audit B2 fix).

    C1 (certification, cause racine R-1): directional R:R measured from the
    macro ENTRY zone to the OPPOSITE (objective) zone, exactly as the recap
    footnote defines it. For a long the entry is the buy zone and the
    objective is the sell zone; for a short the entry is the sell zone and
    the objective is the buy zone.
    """
    if atr is None:
        return "[N/A]"
    buy = p - C.LEVEL_ATR_MULT * atr
    if direction > 0:
        risk, reward = buy - stop, sell - buy
    else:
        risk, reward = stop - sell, sell - buy
    if risk <= 0:
        return "[N/A]"
    # PATCH-RRFLAG-F4 (31/07/2026) : format strict (1:X,X ou [N/A]) exigé par
    # le validateur institutionnel.
    return f"1:{fr_num(reward / risk, 1)}"


def _catalyst_names_for_label(ev_for_label: list[MacroEvent], limit: int = 2) -> str:
    """[M3] Nomme les catalyseurs les plus pertinents pour le texte
    « Risque d'invalidation ».

    L'ancienne implémentation faisait ``sorted({e.event_name for e in ev})[:2]``
    -- un tri ALPHABÉTIQUE sur un set, qui retenait donc deux noms arbitraires
    plutôt que les plus importants ou les plus proches. Tri désormais par
    (décision de banque centrale d'abord, puis proximité temporelle),
    dédoublonné en préservant cet ordre.
    """
    if not ev_for_label:
        return "aucun catalyseur proche"
    ordered = sorted(ev_for_label,
                     key=lambda e: (0 if _is_cb_decision(e) else 1, e.hours_until))
    names: list[str] = []
    seen: set[str] = set()
    for e in ordered:
        nm = (getattr(e, "event_name", "") or "").strip()
        if not nm or nm in seen:
            continue
        seen.add(nm)
        names.append(nm)
        if len(names) >= limit:
            break
    return ", ".join(names) if names else "aucun catalyseur proche"


def _build_setup(asset: str, direction: int, score: float, market: MarketSnapshot,
                 allow_proxy_levels: bool, ips_by_ccy: dict, ccys, ev,
                 cot_label: str = "[PROXY]", strength_is_live: bool = False,
                 ev_label: Optional[list] = None) -> AssetSetup:
    """``ev_label`` (additif, défaut ``None`` -> retombe sur ``ev``, donc
    comportement historique pour tout appelant à l'ancienne signature) porte
    la liste élargie à 168h utilisée UNIQUEMENT pour nommer les catalyseurs
    d'invalidation. Voir [M2]."""
    price = market.price(asset)
    em_disp, em_method, atr = compute_expected_move(asset, market)
    p = price.value

    buy, sell, stop = _build_setup_levels(p, atr, direction, asset)
    levels_proxy = atr is None or em_method.startswith("PROXY")

    if direction > 0:
        bias_class = action_class = "long"
        arrow, action, bias = "↑", "CHERCHER LONG", "🟢/🟡 LONG"
    else:
        bias_class = action_class = "short"
        arrow, action, bias = "↓", "CHERCHER SHORT", "🟢/🟡 SHORT"

    conviction = 2 + int(round(score * 2))
    if levels_proxy or not allow_proxy_levels:
        conviction = max(1, conviction - 1)

    pos_link, squeeze_risk, squeeze_cls, ips_summary, both_extreme = _build_setup_positioning(
        ccys, ips_by_ccy, cot_label)
    if both_extreme:
        conviction = min(conviction, 2)

    color = "green" if score >= 0.7 and not levels_proxy else "yellow"

    ev_for_label = ev_label if ev_label is not None else ev
    ev_names = _catalyst_names_for_label(ev_for_label)
    invalidation = f"Retournement macro / catalyseur ({ev_names})"
    inval_level = (f"clôture {'sous le' if direction > 0 else 'au-dessus du'} stop "
                   f"{_level(stop, asset)}" if stop is not None else "[N/A]")

    momentum_tag = "[Oanda D1]" if (ccys and strength_is_live) else "[PROXY]"
    is_fx_pair = bool(ccys) and len(ccys) == 2

    return AssetSetup(
        asset=asset, color=color, bias=bias, bias_class=bias_class,
        reason_short=("momentum prix D1" if is_fx_pair else "tilt de régime"),
        reason_macro=(f"Différentiel de momentum prix D1 {momentum_tag} favorable"
                      if is_fx_pair else "Biais de régime (refuge / risque) [PROXY]"),
        conviction=conviction, action=action, action_class=action_class, arrow=arrow,
        zone_buy=_level(buy, asset) if buy is not None else "[N/A]",
        origin_buy="[ATR 14j · support implicite]" if buy is not None else "[N/A]",
        zone_sell=_level(sell, asset) if sell is not None else "[N/A]",
        origin_sell="[ATR 14j · résistance implicite]" if sell is not None else "[N/A]",
        stop=_level(stop, asset) if stop is not None else "[N/A]",
        origin_stop="[ATR 14j · stop dynamique]" if stop is not None else "[N/A]",
        expected_move=em_disp, em_method=em_method,
        session="Londres / New York", session_reason="liquidité maximale",
        invalidation_risk=invalidation, invalidation_level=inval_level,
        positioning_link=pos_link, correlation_key=compute_correlation(asset, market),
        ips_summary=ips_summary, squeeze_risk=squeeze_risk, squeeze_class=squeeze_cls,
        risk_reward=_compute_rr_ratio(p, stop, sell, direction, atr),
        price_display=price.display, levels_are_proxy=levels_proxy,
    )


# ---------------------------------------------------------------------------
# Step 3 -- Catalysts
# ---------------------------------------------------------------------------
def build_catalysts(events: list[MacroEvent]) -> tuple[list[MacroEvent], list[MacroEvent], dict]:
    """Split events into 🔴 ÉLEVÉ and 🟡 MODÉRÉ, build beat/miss scenarios.

    [M1] La troncature est déléguée à ``_prioritise_events()``, qui garantit un
    créneau à la décision de banque centrale la plus proche de CHAQUE devise
    avant de remplir par proximité. Sans cette garantie, la réunion BoJ du
    17-18/09/2026 -- catalyseur binaire de la jambe JPY du setup n°1 du jour --
    était purement et simplement absente du rapport.

    ``scenarios`` reste construit sur la liste ``high`` COMPLÈTE avant
    troncature : un scénario préparé pour un événement non affiché ne coûte
    rien, alors que l'inverse laisserait une carte vide.
    """
    high = [e for e in events if e.priority in ("CRITICAL", "HIGH") and e.is_upcoming]
    medium = [e for e in events if e.priority == "MEDIUM" and e.is_upcoming]
    high.sort(key=lambda e: e.hours_until)
    medium.sort(key=lambda e: e.hours_until)

    scenarios: dict[str, dict] = {}
    for e in high:
        key = e.datetime_utc + e.event_name
        # Never emit "[N/A]" for affected pairs: derive them from the event
        # currency against the traded universe when the feed omits them.
        pairs = e.pairs_affected[:4] if e.pairs_affected else _pairs_for_ccy(e.currency)
        affected = " · ".join(pairs) if pairs else f"les paires {e.currency}"
        scenarios[key] = {
            "prev": e.previous, "cons": e.forecast,
            "beat_impact": (f"{e.currency} plus fort → pression sur {affected}. "
                            "Ampleur proportionnelle à l'écart au consensus "
                            "(voir move attendu par actif)."),
            "beat_action": f"Renforce les setups short {e.currency}-quote / long {e.currency}-base.",
            "miss_impact": (f"{e.currency} plus faible → soutien inverse sur {affected}. "
                            "Ampleur proportionnelle à l'écart au consensus."),
            "miss_action": f"Invalide les biais alignés sur un {e.currency} fort.",
            "advice": ("Ne pas ouvrir taille pleine avant la publication ; "
                       "attendre la confirmation post-chiffre."),
        }
    return (_prioritise_events(high, _HIGH_CAP),
            _prioritise_events(medium, _MEDIUM_CAP),
            scenarios)


def _pairs_for_ccy(ccy: str) -> list[str]:
    """Traded pairs that contain a given currency (deterministic, no [N/A])."""
    return [p for p, ccys in C.INSTRUMENT_CCYS.items() if ccy in ccys][:4]


# ---------------------------------------------------------------------------
# Step -- Macro overlay text blocks
# ---------------------------------------------------------------------------
def _build_cot_summary(ips: list) -> str:
    """Synthesize the qualitative COT/positioning summary."""
    if not ips:
        return "[N/A] — aucune donnée de positionnement disponible."
    crowded_long = [r.currency for r in ips
                    if r.ips_score is not None and r.ips_score >= C.IPS_CROWDED]
    crowded_short = [r.currency for r in ips
                     if r.ips_score is not None and r.ips_score <= C.IPS_CAPITULATION]
    extremes = crowded_long + crowded_short
    if not extremes:
        return "Aucune devise majeure en positionnement extrême — scores IPS en zone neutre."
    parts = []
    if crowded_long:
        parts.append(f"{'/'.join(crowded_long)} crowded long")
    if crowded_short:
        parts.append(f"{'/'.join(crowded_short)} crowded short/capitulation")
    plural = "s" if len(extremes) > 1 else ""
    return (f"{len(extremes)} devise{plural} en positionnement extrême "
            f"({', '.join(parts)}).")


def build_macro_overlay(market: MarketSnapshot, regime: str,
                        events: list[MacroEvent],
                        liquidity_msg: str,
                        pc_data: Optional[dict] = None,
                        ips: Optional[list] = None) -> dict:
    """[N6] Le « prochain catalyseur » du thème est désormais l'ancre
    ``_anchor_event`` (plus imminent parmi CRITICAL/HIGH, sinon MEDIUM) et non
    plus ``events[0]``, qui dépendait de l'ordre d'arrivée du flux et pouvait
    nommer un événement Low. Le branchement « aucun événement CRITICAL/HIGH
    programmé », jusqu'ici inatteignable, devient effectif."""
    vix = market.gauge("VIX")
    move = market.gauge("MOVE")
    dxy = market.gauge("DXY")

    anchor = _anchor_event(events)
    if anchor is not None:
        theme = (f"Semaine pilotée par le calendrier macro (prochain catalyseur : "
                 f"{anchor.event_name}). Régime : {regime}.")
        theme_src = "[Forex Factory | calendrier]"
    else:
        theme = (f"Semaine sans catalyseur macro daté majeur — lecture pilotée par le régime "
                 f"de volatilité et le positionnement plutôt que par le calendrier. Régime : {regime}.")
        theme_src = "[N/A] — aucun événement CRITICAL/HIGH/MEDIUM programmé"

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
        # DECOMMISSIONED (17/07/2026, ADR): CBOE Put/Call ratio removed from
        # the briefing. `pc_data` stays wired through unchanged since both
        # consumers already degrade gracefully on None.
    else:
        vol_regime = "VIX [N/A]"
        vol_impl = "Méthode indisponible — régime vol non évaluable [N/A]."

    return {
        "theme": theme, "theme_src": theme_src,
        "dxy_ctx": dxy_ctx, "dxy_src": dxy_src,
        "vol_regime": vol_regime, "vol_impl": vol_impl,
        "correlation": f"{_correlation_short('EUR/USD', market)} · {_correlation_short('USD/JPY', market)}",
        "liquidity": liquidity_msg,
        "cot_summary": _build_cot_summary(ips or []),
    }


# ---------------------------------------------------------------------------
# Step -- Risk scenarios
# ---------------------------------------------------------------------------
def build_risk_scenarios(events: list[MacroEvent], regime_class: str,
                         priority: list[AssetSetup],
                         central_banks: Optional[list[CentralBankSnapshot]] = None
                         ) -> tuple[dict, RiskScenario, RiskScenario, str]:
    """[N6] Ancre = ``_anchor_event(events)`` et non ``events[0]``. Le test
    ``anchor.currency == "USD"`` qui conditionne l'affichage des probabilités
    FedWatch porte donc maintenant sur un événement réellement majeur."""
    anchor = _anchor_event(events)
    if anchor is not None:
        anchor_name = anchor.event_name
        anchor_src = "[Forex Factory | calendrier]"
        bull_trig = f"{anchor_name} sous le consensus → détente des taux/vol"
        bear_trig = f"{anchor_name} au-dessus du consensus → repricing hawkish / fuite vers la qualité"
        inval_txt = (f"Désamorçage du catalyseur principal ({anchor_name}) ou retour de la "
                     "volatilité dans sa fourchette → révision du scénario dominant.")
    else:
        anchor_name = "régime de volatilité (pas de catalyseur daté dans la fenêtre)"
        anchor_src = "[BLUESTAR · régime de marché]"
        bull_trig = "Compression de la volatilité / détente des taux → rotation risk-on"
        bear_trig = "Choc de volatilité / repricing hawkish → fuite vers la qualité"
        inval_txt = ("Rupture du régime de volatilité actuel (VIX/MOVE hors fourchette) "
                     "→ révision du scénario dominant.")

    proba = "qualitative — équilibré"
    proba_src = anchor_src
    if anchor is not None and anchor.currency == "USD" and central_banks:
        fed = next((cb for cb in central_banks if cb.name == "FED"), None)
        if fed and fed.pause_pct is not None and fed.cut_pct is not None and fed.hike_pct is not None:
            proba = (f"Pause {fed.pause_pct}% · Baisse {fed.cut_pct}% "
                     f"· Hausse {fed.hike_pct}%")
            # MACRO-B2 FIX : l'attribution de source doit refléter le stamp réel.
            fed_reliability = getattr(getattr(fed, "stamp", None), "reliability", None)
            if fed_reliability is Reliability.PRIMARY:
                proba_src = "[CME FedWatch]"
                if getattr(fed, "fedwatch_as_of", None):
                    proba_src = f"[CME FedWatch · prélèvement {fed.fedwatch_as_of}]"
            elif fed_reliability is Reliability.PROXY:
                proba_src = "[PROXY · override manuel]"
            else:
                proba_src = anchor_src

    risk_main = {
        "desc": ("Surprise macro sur le principal catalyseur de la semaine "
                 f"({anchor_name}) déclenchant un repricing brutal." if anchor is not None else
                 f"Repricing brutal piloté par le {anchor_name}."),
        "asset": priority[0].asset if priority else "—",
        "level": (priority[0].invalidation_level
                  if priority and priority[0].invalidation_level not in ("[N/A]", "", None)
                  else "seuil de bascule = sortie du régime de volatilité courant (VIX/MOVE)"),
        "proba": proba,
        "source": proba_src,
    }
    bull = RiskScenario(
        title="Scénario BULL (risk-on)", proba="favori si données molles",
        trigger=bull_trig,
        trigger_source=anchor_src,
        rows=[f"{s.asset} → mouvement aligné risk-on" for s in priority[:3]] or
             ["USD faible → EUR/USD ↑ · Or ↑ · US10Y ↓"],
    )
    bear = RiskScenario(
        title="Scénario BEAR (risk-off)", proba="favori si données chaudes / choc",
        trigger=bear_trig,
        trigger_source=anchor_src,
        rows=["Refuges : Or · JPY · CHF"] +
             ([f"{s.asset} → mouvement aligné risk-off" for s in priority[:3]]
              or ["USD fort → US10Y ↑ · JPY faible → Or ↓"]),
    )
    return risk_main, bull, bear, inval_txt


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
    # PATCH-C08BIS (31/07/2026, F-11/C-08) : ZoneInfo("Europe/Paris") plutôt
    # que C.TZ_CET, pour que tzname() renvoie dynamiquement CEST/CET.
    now_cet = now_utc.astimezone(ZoneInfo("Europe/Paris"))
    _, is_live = session_label(now_cet)

    raw_engine = calendar.get("events_engine") or calendar.get("events") or []
    events = [MacroEvent.from_enriched(e) for e in raw_engine]
    upcoming = [e for e in events if e.is_upcoming]

    # Surprise / Momentum gauge — BLUESTAR free substitute for the proprietary
    # Citi CESI.
    if _inst is not None and "SURPRISE_IDX" not in (overrides.get("market") or {}):
        try:
            si = _inst.fetch_macro_surprise("USD", raw_events=events)
            if si is not None:
                disp = f"{'+' if si.value >= 0 else ''}{si.value:.0f}"
                gauge_mode = "US Macro Surprise" if "Surprise" in si.source else "US Macro Momentum"
                market.gauges["SURPRISE_IDX"] = Datum(
                    si.value,
                    SourceStamp("BLUESTAR · Forex Factory", Reliability.PRIMARY, timestamp=now_utc),
                    disp, f"{si.trend} {gauge_mode} · n={si.n}",
                )
            else:
                market.gauges["SURPRISE_IDX"] = Datum(
                    None,
                    na_stamp("aucun évènement USD high-impact dans la fenêtre calendrier courante"),
                    "N/A",
                )
        except Exception as exc:  # pragma: no cover - never break the pipeline
            logger.warning("surprise gauge injection failed: %s", exc)
            market.gauges["SURPRISE_IDX"] = Datum(
                None, na_stamp("erreur interne pendant le calcul du surprise index (voir logs)"), "N/A",
            )

    regime, regime_cls, regime_since, _pen = determine_market_regime(market, events)

    try:
        from .regime_engine import assess_regime as _assess_regime
        _regime_pending = True
    except Exception:
        _regime_pending = False

    # A6-fix: sequential execution to avoid SIGSEGV from nested
    # ThreadPoolExecutor + curl_cffi/libcurl thread-unsafety.
    # [M6]/[N1] ``events`` alimente le repli « prochaine réunion » ET la
    # résolution du taux directeur depuis le flux (carte BoJ).
    central_banks = build_central_bank_context(overrides, now_utc, events=events)
    ips, cot_ref_label = build_ips_scores(overrides, now_utc)
    sofr_effr_bp = fetch_liquidity_stress()
    # DECOMMISSIONED (17/07/2026, ADR) : fetch_pc_ratio() est un stub no-op.
    try:
        _vix_gauge = market.gauge("VIX")
        pc_data = fetch_pc_ratio(
            vix_value=_vix_gauge.value if _vix_gauge.available else None
        )
    except Exception as exc:   # pragma: no cover — jamais casser le pipeline
        logger.warning("fetch_pc_ratio failed: %s", exc)
        pc_data = None

    cs = build_currency_strength_ranking(central_banks, regime_cls)
    cs = _oanda_strength_scores(market, cs)   # BLUESTAR-PATCH v10.0
    high, medium, scenarios = build_catalysts(events)

    regime_assessment = None
    if _regime_pending:
        try:
            regime_assessment = _assess_regime(market, central_banks, cs, ips, events, now_utc, pc_data)
        except Exception as exc:
            logger.warning("Regime engine failed: %s", exc)
    headline_regime_name = regime_assessment.name if regime_assessment is not None else regime

    # Liquidity / funding stress — SOFR−EFFR spread (bp) via FRED, else [N/A].
    # Seuils (8 bp / 15 bp) HEURISTIQUES, non backtestés.
    if sofr_effr_bp is not None:
        if sofr_effr_bp >= 15.0:
            tone = "tension de financement USD notable"
        elif sofr_effr_bp >= 8.0:
            tone = "légère tension de financement USD"
        else:
            tone = "pas de stress de financement USD"
        liquidity_msg = (f"Spread SOFR−EFFR {fr_num(sofr_effr_bp, 1)} bp → {tone} "
                         "[FRED · SOFR/EFFR]. Surveiller les flux de fin de "
                         "journée et les rebalancings de fonds monétaires.")
    else:
        liquidity_msg = "[N/A] — spread SOFR−EFFR non sourcé."

    overlay = build_macro_overlay(market, headline_regime_name, upcoming, liquidity_msg, pc_data, ips=ips)

    if not ips:
        cot_date = "[N/A]"
    else:
        reliabs = {r.stamp.reliability for r in ips if r.stamp is not None}
        if Reliability.PRIMARY in reliabs and Reliability.PROXY not in reliabs:
            cot_date = f"OBSERVÉ — {cot_ref_label}"
        else:
            cot_date = cot_ref_label + " [PROXY · scaling linéaire]"

    priority, avoid, no_setup = select_priority_assets(
        market, regime_cls, central_banks, cs, ips, events, mode,
        allow_proxy_levels, cot_label=cot_date,
    )

    # AUDIT-FIX (17/07/2026, anomalie A3) : `upcoming` et non `events`, pour
    # que l'ancre du scénario soit toujours un catalyseur à venir.
    risk_main, bull, bear, inval_txt = build_risk_scenarios(
        upcoming, regime_cls, priority, central_banks
    )

    # Diff rate
    diff_dominant, diff_implication = _build_rate_differential(central_banks)

    # Positioning alert (checks if any prioritized asset has an extreme IPS)
    positioning_alert = ""
    for s in priority:
        ccys_s = C.INSTRUMENT_CCYS.get(s.asset)
        if not ccys_s:
            continue
        for ccy in ccys_s:
            ips_r = next((r for r in ips if r.currency == ccy), None)
            if ips_r and ips_r.is_extreme:
                positioning_alert = (f"{ccy} en zone extrême (IPS {ips_r.ips_score}) — "
                                     f"risque de squeeze si catalyseur déçoit.")
                break
        if positioning_alert:
            break

    interpretation = None
    if regime_assessment is not None:
        try:
            from .interpretation import build_interpretation
            interpretation = build_interpretation(market, central_banks, cs, ips, regime_assessment, priority, now_utc, pc_data)
        except Exception as exc:
            logger.warning("Interpretation engine failed: %s", exc)

    # V4-04 FIX: compute calendar metadata BEFORE return
    cal_meta = (calendar or {}).get("metadata", {})
    return BriefingContext(
        generated_utc=now_utc,
        generated_cet=now_cet,
        is_live_session=is_live,
        market=market,
        regime=regime,
        regime_class=regime_cls,
        regime_since=regime_since,
        regime_assessment=regime_assessment,
        interpretation=interpretation,
        operational_note=overrides.get("operational_note"),
        priority_assets=priority,
        avoid_assets=avoid,
        no_setup_reason=no_setup,
        catalysts_high=high,
        catalysts_medium=medium,
        catalyst_scenarios=scenarios,
        central_banks=central_banks,
        currency_strength=cs,
        ips_scores=ips,
        diff_dominant=diff_dominant,
        diff_implication=diff_implication,
        macro_theme=overlay["theme"],
        macro_theme_src=overlay["theme_src"],
        cot_summary=overlay.get("cot_summary", ""),
        cot_date=cot_date,
        dxy_context=overlay["dxy_ctx"],
        dxy_src=overlay["dxy_src"],
        vol_regime=overlay["vol_regime"],
        vol_implication=overlay["vol_impl"],
        correlation_summary=overlay["correlation"],
        liquidity_flow=overlay["liquidity"],
        squeeze_currency=positioning_alert.split(" ")[0] if positioning_alert else None,
        positioning_alert=positioning_alert,
        risk_main=risk_main,
        bull=bull,
        bear=bear,
        invalidation_principal=inval_txt,
        calendar_reachable=bool(cal_meta.get("reachable", True)),
        calendar_feed_truncated=bool(cal_meta.get("feed_horizon_truncated", False)),
        calendar_feed_horizon_h=(f"{cal_meta.get('feed_horizon_h')}h"
                                  if cal_meta.get("feed_horizon_h") is not None else None),
    )

