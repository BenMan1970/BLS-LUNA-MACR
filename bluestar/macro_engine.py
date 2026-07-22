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

import concurrent.futures
import logging
import re
from datetime import datetime
from typing import Optional

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
    fetch_central_bank_rates,
    central_bank_rate_source,            # AUDIT-ENRICHMENT 15/07/2026: BoE via IADB
    fetch_fedwatch_probabilities,
    fetch_cot_data,
    fetch_liquidity_stress,
    fetch_pc_ratio,                      # C1: VIX Ã— P/C composite signal
)

logger = logging.getLogger(__name__)

FR_DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
FR_MONTHS = ["", "janvier", "fÃ©vrier", "mars", "avril", "mai", "juin",
             "juillet", "aoÃ»t", "septembre", "octobre", "novembre", "dÃ©cembre"]


def fr_date(dt: datetime) -> str:
    return f"{dt.day:02d}/{dt.month:02d}/{dt.year}"


def fr_day_name(dt: datetime) -> str:
    return FR_DAYS[dt.weekday()]


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
      Asian      00:00â€“07:00  (Tokyo liquidity peak 00:00â€“03:00)
      London     07:00â€“17:00
      Overlap    13:00â€“17:00  (London + New York both active)
      New York   13:00â€“22:00
      FX closed  Fri 22:00 â€“ Sun 22:00 (UTC)

    The ``dt_cet`` argument is kept for API compatibility (callers already hold
    a CET datetime); we reproject to UTC internally.
    """
    dt_utc = dt_cet.astimezone(TZ_UTC)
    wd = dt_utc.weekday()   # 0 = Monday â€¦ 6 = Sunday

    # FX cash market: opens Sun ~22:00 UTC, closes Fri ~22:00 UTC.
    if wd == 5:                          # Saturday
        return "MARCHÃ‰ FX FERMÃ‰ (week-end)", False
    if wd == 6 and dt_utc.hour < 22:     # Sunday before reopen
        return "MARCHÃ‰ FX FERMÃ‰ (week-end)", False
    if wd == 4 and dt_utc.hour >= 22:    # Friday after close
        return "MARCHÃ‰ FX FERMÃ‰ (week-end)", False

    h = dt_utc.hour
    london = 7 <= h < 17
    ny     = 13 <= h < 22
    if london and ny:
        return "Overlap Londres/New York", True
    if london:
        return "Session Londres", True
    if ny:
        return "Session New York", True
    return "Session Asie / hors liquiditÃ©", True


# ---------------------------------------------------------------------------
# Step 2 -- Market regime
# ---------------------------------------------------------------------------
def determine_market_regime(market: MarketSnapshot,
                            events: list[MacroEvent]) -> tuple[str, str, str, int]:
    """Return (regime_text, regime_class, regime_since, conviction_penalty)."""
    vix = market.gauge("VIX")
    penalty = 0
    if not vix.available:
        return ("MIXTE â€” donnÃ©es vol insuffisantes [N/A]", "regime-mix",
                "[N/A]", 1)

    v = vix.value
    imminent = any(e.priority == "CRITICAL" for e in events)
    if v >= C.VIX_RISK_OFF_MIN:
        text, cls = "RISK-OFF â€” aversion au risque", "regime-off"
    elif v <= C.VIX_RISK_ON_MAX:
        text, cls = "RISK-ON â€” appÃ©tit pour le risque", "regime-on"
    else:
        text, cls = "MIXTE â€” biais sÃ©lectif", "regime-mix"

    if imminent and cls != "regime-off":
        text += " Â· catalyseur binaire imminent"
        cls = "regime-mix"
        penalty = 1
    since = "Ã©vÃ©nements macro rÃ©cents"
    return text, cls, since, penalty


# ---------------------------------------------------------------------------
# Step 4 -- Central banks (no keyless source -> overrides or [N/A]/[PROXY])
# ---------------------------------------------------------------------------
_CB_DEFS = [
    ("FED", "ðŸ‡ºðŸ‡¸", "USD"),
    ("BCE", "ðŸ‡ªðŸ‡º", "EUR"),
    ("BoJ", "ðŸ‡¯ðŸ‡µ", "JPY"),
    ("BoE", "ðŸ‡¬ðŸ‡§", "GBP"),
]


def build_central_bank_context(overrides: Optional[dict],
                               now_utc: Optional[datetime] = None) -> list[CentralBankSnapshot]:
    """Build the four central-bank blocks.

    Sourcing precedence:
      1. FRED policy rate (``fetch_central_bank_rates``) for the *rate*
         field â€” preferred when live, so a working FRED feed is never
         permanently shadowed by an override typed once and left in place.
         AUDIT-FIX (15/07/2026): this used to be override-always-wins for
         the rate too ("[PROXY Â· taux saisis en overrides]" showing up
         indefinitely even when FRED was fine), per user request the two
         are now swapped for the *rate* specifically.
      2. User override (``overrides['central_banks'][name]['rate']``) â€”
         fallback when FRED has no value for that bank.
      3. ``fact`` / ``bias`` / ``next`` stay override-first (unchanged):
         FRED only supplies a numeric rate, not the qualitative FAIT/BIAIS
         write-up or the next-meeting date, so there is nothing live to
         prefer there.
      4. CME FedWatch (``fetch_fedwatch_probabilities``) â€” fills the Fed's
         pause/cut/hike when the override omits them (unchanged).
      5. Otherwise [N/A] â€” never invented.
    """
    cb_over = (overrides or {}).get("central_banks", {})

    # A6-fix: sequential calls â€” ThreadPoolExecutor nested inside
    # ThreadPoolExecutor caused SIGSEGV with curl_cffi/libcurl (non-thread-safe).
    fred_rates = fetch_central_bank_rates()     # {name: pct} or {}
    fedwatch = fetch_fedwatch_probabilities()   # {pause/cut/hike} or None

    out: list[CentralBankSnapshot] = []
    for name, flag, _ccy in _CB_DEFS:
        o = cb_over.get(name, {})

        # --- Rate: live source (FRED or BoE IADB) > override > [N/A] ---
        # AUDIT-ENRICHMENT (15/07/2026): renamed fred_val/rate_from_fred ->
        # live_val/rate_is_live â€” fetch_central_bank_rates() can now also
        # resolve "BoE" via the Bank of England's own IADB feed (see
        # external_sources.py), not just FRED, so "fred_val" was no longer
        # an accurate name for what this variable can hold.
        live_val = fred_rates.get(name)
        rate_is_live = False
        if live_val is not None:
            rate = f"{fr_num(live_val, 2)}%"
            rate_is_live = True
        else:
            rate = o.get("rate")
        if rate is None:
            rate = "[N/A]"

        fact = o.get("fact") or "[N/A] â€” taux/probabilitÃ© non sourcÃ©s sans clÃ© API."
        bias = o.get("bias") or "[N/A] â€” interprÃ©tation Ã  confirmer."
        nxt = o.get("next", "[N/A]")

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
            # N4 (17/07/2026, audit A1): date de prÃ©lÃ¨vement FedWatch quand
            # le payload BCM la fournit â€” affichÃ©e sous la barre de proba et
            # dans la source du risque principal (section 5).
            fw_as_of = fedwatch.get("as_of")

        # --- Stamp reflects the strongest source actually used ---
        # AUDIT-FIX (15/07/2026): FRED-live rate now takes stamp precedence
        # over a co-present override (e.g. override only supplying fact/
        # bias/next text) â€” previously `if o:` alone forced [PROXY] even
        # when the displayed rate itself came live from FRED.
        # AUDIT-ENRICHMENT (15/07/2026): src is now looked up via
        # central_bank_rate_source(name) instead of hardcoded "FRED" â€” that
        # hardcode would have mislabeled a live BoE (IADB) rate as "FRED".
        if rate_is_live or fw_used:
            src = central_bank_rate_source(name) if rate_is_live else ""
            if fw_used:
                src = (src + " + CME FedWatch").strip(" +")
            # v10.1 (P0 fraÃ®cheur): horodate le stamp PRIMARY pour la certification
            # (sans clÃ©, les CB sont PROXY/UNAVAILABLE -> aucun changement de rendu).
            stamp = SourceStamp(src or "external", Reliability.PRIMARY, timestamp=now_utc)
        elif o:
            stamp = proxy_stamp("manual override")
        else:
            stamp = na_stamp("source sans clÃ© API")

        out.append(CentralBankSnapshot(
            name=name, flag=flag, rate_display=str(rate),
            fact=str(fact), bias_interpretation=str(bias), next_meeting=str(nxt),
            stamp=stamp,
            pause_pct=pause, cut_pct=cut, hike_pct=hike,
            fedwatch_as_of=fw_as_of,
            # N4bis (17/07/2026, audit A1): barre de proba alimentÃ©e par les
            # overrides manuels â†’ le renderer l'Ã©tiquette honnÃªtement au lieu
            # de la laisser muer sous un stamp de taux live (cas du 16/07 :
            # 70/0/30 figÃ© ~1 semaine, stamp [FRED] seul sur la carte).
            proba_from_override=(
                name == "FED" and not fw_used
                and any(p is not None for p in (pause, cut, hike))
            ),
        ))
    return out


_HAWKISH_OPEN_RE = re.compile(r"^\s*(trÃ¨s\s+)?hawkish\b")
_DOVISH_OPEN_RE = re.compile(r"^\s*(trÃ¨s\s+)?dovish\b")
_HAWKISH_ANY_RE = re.compile(r"\bhawkish\b")
_DOVISH_ANY_RE = re.compile(r"\bdovish\b")


def _parse_rate_pct(rate_display: str) -> Optional[float]:
    """Parse a CB rate string into a float percentage.

    Handles a single value ("2,25%"), a range ("3,50â€“3,75%" -> midpoint) and
    a leading "~" ("~1,00%"). Returns ``None`` for anything not parseable
    (e.g. "[N/A]"), so the caller can tell "sourced" from "not sourced".
    """
    if not rate_display:
        return None
    s = rate_display.strip().lstrip("~").rstrip("%").strip()
    parts = re.split(r"[â€“-]", s)
    try:
        vals = [float(p.strip().replace(",", ".")) for p in parts if p.strip()]
    except ValueError:
        return None
    return sum(vals) / len(vals) if vals else None


def _build_rate_differential(central_banks: list[CentralBankSnapshot]) -> tuple[str, str]:
    """Dominant policy-rate differential among the tracked central banks.

    Previously this was hardcoded to "non calculable sans taux sourcÃ©s"
    unconditionally -- even on a run where every rate *was* sourced via
    manual overrides. It now actually reads ``central_banks`` and only
    degrades to [N/A] when fewer than 2 rates are genuinely available.
    """
    rates: dict[str, float] = {}
    stamps: dict[str, SourceStamp] = {}
    for (_name, _flag, ccy), cb in zip(_CB_DEFS, central_banks):
        if cb.stamp.ok:
            r = _parse_rate_pct(cb.rate_display)
            if r is not None:
                rates[ccy] = r
                stamps[ccy] = cb.stamp

    if len(rates) < 2:
        return ("[N/A] â€” taux directeurs non sourcÃ©s (saisir en overrides).",
                "DiffÃ©rentiel non calculable sans au moins 2 taux sourcÃ©s.")

    ccy_hi = max(rates, key=rates.get)
    ccy_lo = min(rates, key=rates.get)
    gap = rates[ccy_hi] - rates[ccy_lo]
    # AUDIT-FIX (15/07/2026): this tag used to hardcode "[PROXY Â· taux
    # saisis en overrides]" unconditionally, which was accurate by
    # construction back when the override always won the rate. Now that
    # build_central_bank_context() lets a live FRED rate win when
    # available, a hardcoded PROXY tag would misreport a genuinely live
    # differential as an approximation â€” the same class of bug as the
    # momentum-tag fix on the setup cards. The tag now reflects the actual
    # stamp of the two rates involved.
    # AUDIT-ENRICHMENT (15/07/2026): the live-case tag was itself hardcoded
    # to "[FRED Â· PRIMARY]" â€” wrong the moment one leg is BoE-sourced (see
    # external_sources.py / central_bank_rate_source). Built from the two
    # actual stamp.source_name values instead, so e.g. a GBP/JPY pair
    # correctly shows both "Bank of England Â· IADB" and "FRED" when both
    # legs are genuinely live.
    hi_stamp, lo_stamp = stamps[ccy_hi], stamps[ccy_lo]
    both_live = (hi_stamp.reliability is Reliability.PRIMARY and
                lo_stamp.reliability is Reliability.PRIMARY)
    both_proxy = (hi_stamp.reliability is Reliability.PROXY and
                 lo_stamp.reliability is Reliability.PROXY)
    if both_live:
        src_names = sorted({hi_stamp.source_name, lo_stamp.source_name})
        tag = f"[{' + '.join(src_names)} Â· PRIMARY]"
    elif both_proxy:
        tag = "[PROXY Â· taux saisis en overrides]"
    else:
        # AUDIT-FIX (15/07/2026, finding 5 â€” MAJEURE): this used to fall
        # through to the same unconditional "[PROXY Â· taux saisis en
        # overrides]" tag as the both-proxy case above, even when one leg
        # (e.g. USD via FRED) was genuinely live and only the other (e.g.
        # JPY via manual override) was a proxy â€” misreporting a real,
        # live rate as a manual approximation. Each leg's real provenance
        # is now stated explicitly.
        def _leg_tag(stamp: SourceStamp) -> str:
            if stamp.reliability is Reliability.PRIMARY:
                return stamp.source_name or "PRIMARY"
            if stamp.reliability is Reliability.PROXY:
                return "override"
            return stamp.source_name or stamp.reliability.value
        tag = f"[MIXTE Â· {ccy_hi} {_leg_tag(hi_stamp)} + {ccy_lo} {_leg_tag(lo_stamp)}]"
    dominant = (f"{ccy_hi} ({fr_num(rates[ccy_hi], 2)}%) vs {ccy_lo} "
               f"({fr_num(rates[ccy_lo], 2)}%) â†’ Ã©cart â‰ˆ {fr_num(gap, 2)} pt "
               f"{tag}")
    if gap == 0:
        implication = f"Taux directeurs identiques ({ccy_hi}/{ccy_lo}) â€” pas de portage net entre les deux."
    else:
        implication = (f"Portage structurellement favorable Ã  {ccy_hi} face Ã  {ccy_lo} "
                       "tant que cet Ã©cart de taux directeurs persiste.")
    return dominant, implication


def _cb_bias_word(cb: CentralBankSnapshot) -> int:
    """Map a CB bias string to a strength delta (+hawkish / -dovish).

    The bias field is free text (e.g. "Hawkish â€” biais de resserrement..."
    or "Neutre Ã  lÃ©gÃ¨rement hawkish."). A plain ``"hawkish" in text`` match
    treats an outright stance and a hedged "leaning hawkish" identically --
    that's exactly how BoE's "Neutre Ã  lÃ©gÃ¨rement hawkish" used to land on
    the same +12 as the Fed's unqualified "Hawkish", producing a false tie.
    The tone word must *open* the sentence for the full +/-12; appearing
    later or softened (lÃ©gÃ¨rement, neutre Ã , etc.) counts as a weaker +/-6.
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
    If the attribute is absent or empty, return ``cb_ranking`` unchanged
    (zero-regression fallback).
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
    """Qualitative 0-100 score per major currency. Always [PROXY]."""
    cb_by_ccy = {ccy: cb for (name, _f, ccy), cb in zip(_CB_DEFS, central_banks)}
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
    report remains the official reference (RÃ¨gle Absolue nÂ°3).

    Logic:
      1. Convert now_utc to ET to stay consistent with CFTC publication time.
      2. Find the most recent Friday â‰¤ today.
      3. If today IS a Friday but the 15:30 ET cut-off has not yet passed,
         step back one more week (the current report is not yet released).

    Returns a datetime set to 15:30 ET on the reference Friday (UTC-aware).
    """
    from datetime import timedelta
    now_et = now_utc.astimezone(C.TZ_ET)
    # days_since_friday: 0 if today is Friday, 1 if Saturday, ..., 6 if Thursday
    days_since_friday = (now_et.weekday() - 4) % 7
    candidate = now_et.replace(hour=15, minute=30, second=0, microsecond=0) \
                - timedelta(days=days_since_friday)
    # If it is Friday but before 15:30 ET, the current report is not yet out.
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


def _ips_from_institutional(ref_label: str) -> list[CotPositioning]:
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
        delta_str = f"Î”1s {stat.change_1w:+d}" if stat.change_1w is not None else "â‰ˆ stable"
        rows.append(CotPositioning(
            currency=ccy, net_contracts=stat.net, ips_score=score,
            ips_label=_ips_label_for(score), delta_week=delta_str,
            momentum="â†‘" if (stat.change_1w or 0) > 0 else "â†“" if (stat.change_1w or 0) < 0 else "â†’",
            stamp=SourceStamp(
                f"OBSERVÃ‰ â€” CFTC Non-Commercials | z={stat.zscore} | {int(stat.percentile)}e pct | {stat.report_date}",
                Reliability.PRIMARY,
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
            delta_week=o.get("delta", "â‰ˆ stable"), momentum=o.get("momentum", "â†’"),
            stamp=SourceStamp(f"{ref_label} [PROXY Â· scaling linÃ©aire]", Reliability.PROXY,
                              note="scaling linÃ©aire 150k contrats â€” PAS un vrai percentile"),
        ))
    return rows


def _ips_from_scrape(ref_date_str: str) -> list[CotPositioning]:
    """Path 3: live CFTC scrape with linear scaling [PROXY].

    ``ref_date_str`` is the date portion only (no "CFTC Non-Commercials |"
    prefix, no reliability tag) â€” see the audit-fix note in
    ``build_ips_scores`` for why this was split out of the old
    ``ref_label`` (avoids duplicating the "CFTC Non-Commercials |" prefix
    when ``external_date`` is unavailable and this fallback is used).
    """
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
        # MACRO-B3 FIX : retrait du mot "OBSERVÃ‰" trompeur. Ce n'est pas un percentile,
        # c'est un scaling linÃ©aire arbitraire (150k contrats).
        src_label = f"CFTC Non-Commercials | {external_date or ref_date_str} [PROXY Â· scaling linÃ©aire]"
        rows.append(CotPositioning(
            currency=ccy, net_contracts=net, ips_score=score,
            ips_label=_ips_label_for(score), delta_week="â‰ˆ stable", momentum="â†’",
            stamp=SourceStamp(src_label, Reliability.PROXY,
                              note="scaling linÃ©aire â€” PAS un vrai percentile"),
        ))
    return rows


def build_ips_scores(overrides: Optional[dict],
                     now_utc: datetime) -> tuple[list[CotPositioning], str]:
    """Build IPS rows from COT data â€” real z-scores/percentiles when available.

    Sourcing precedence (audit B4 fix):
    1. ``institutional.fetch_positioning_stats`` â€” real z-scores/percentiles.
    2. User override (``overrides["cot"]``) â€” linear scaling [PROXY].
    3. Live CFTC scrape (``fetch_cot_data``) â€” linear scaling [PROXY].
    4. [N/A] when no COT data is available.
    """
    ref = _reference_cftc_friday(now_utc)
    # AUDIT-FIX (15/07/2026, finding 3 â€” MAJEURE): "OBSERVÃ‰" used to be baked
    # into ref_label unconditionally, so the PROXY paths below (overrides /
    # scrape), which append "[PROXY Â· scaling linÃ©aire]" as a suffix, could
    # produce a self-contradictory label like
    # "OBSERVÃ‰ â€” CFTC ... [PROXY Â· scaling linÃ©aire]". ref_label is now
    # source-neutral (no reliability claim baked in); the "OBSERVÃ‰" prefix
    # is added by the caller (build_context) only once the actual
    # reliability of the resolved IPS rows is known. ref_date_str is kept
    # separate (date only, no "CFTC Non-Commercials |" prefix) so
    # _ips_from_scrape's own fallback composition doesn't end up
    # duplicating that prefix when the live scrape date is unavailable.
    ref_date_str = (
        f"{FR_DAYS[ref.weekday()]} "
        f"{ref.day} {FR_MONTHS[ref.month]} {ref.year}"
    )
    ref_label = f"CFTC Non-Commercials | {ref_date_str}"

    cot_over = (overrides or {}).get("cot", {})

    rows = _ips_from_institutional(ref_label)
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

# Audit fix: below this |r|, a real-but-noisy Pearson correlation is labelled
# "non significative" instead of printed as if it were signal. Add
# CORR_SIGNIFICANCE_MIN to config.py to override; 0.2 is the fallback.
_CORR_SIG_MIN = getattr(C, "CORR_SIGNIFICANCE_MIN", 0.2)


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
    """Asset-card '9. CorrÃ©lation clÃ©' field -- a real, computed [PROXY].

    Previously this was a hardcoded literal string ("[PROXY] corrÃ©lations
    30j") identical for every asset, every day, regardless of what the
    market actually did. The closes needed are already fetched for the ATR
    calculation, so this costs zero extra network calls / API keys.
    """
    res = _correlation_value(asset, market)
    if res is None:
        if asset not in _CORR_BENCHMARK:
            return "[N/A] â€” pas de rÃ©fÃ©rence dÃ©finie pour cet actif."
        return "[N/A] â€” corrÃ©lation indisponible (historique insuffisant)."
    r, bench, n = res
    sign = "+" if r >= 0 else "âˆ’"
    if abs(r) < _CORR_SIG_MIN:
        # Audit fix: a benchmark chosen by asset-class default (e.g. DXY for
        # every non-JPY pair, including EUR/GBP where DXY is largely
        # irrelevant) can produce a real but near-zero r. Printing "â‰ˆ +0,05
        # DXY" reads as a signal; it isn't one. Label it explicitly instead.
        return (f"â‰ˆ {sign}{fr_num(abs(r), 2)} {bench} â€” non significative "
                f"(|r|<{_CORR_SIG_MIN:.1f}) [Pearson Â· {n} sÃ©ances]")
    return f"â‰ˆ {sign}{fr_num(abs(r), 2)} {bench} [Pearson Â· {n} sÃ©ances]"


def _correlation_short(asset: str, market: MarketSnapshot) -> str:
    """Compact form for the Section 3 overlay row (no bracket -- the
    template already appends a single shared [PROXY] tag for that row)."""
    res = _correlation_value(asset, market)
    if res is None:
        return f"{asset} n/d"
    r, bench, _n = res
    sign = "+" if r >= 0 else "âˆ’"
    if abs(r) < _CORR_SIG_MIN:
        return f"{asset} n/s ({bench})"
    return f"{asset} â‰ˆ {sign}{fr_num(abs(r), 2)} {bench}"


# ---------------------------------------------------------------------------
# Step 6 -- Asset selection
# ---------------------------------------------------------------------------
def _events_for_ccys(events: list[MacroEvent], ccys: tuple[str, ...],
                     within_h: float = 72) -> list[MacroEvent]:
    return [e for e in events
            if e.currency in ccys and -6 <= e.hours_until <= within_h]


def _strength_map(cs: list[CurrencyStrength]) -> dict[str, int]:
    return {r.currency: r.score for r in cs}


def _strength_source_map(cs: list[CurrencyStrength]) -> dict[str, bool]:
    """True where a currency's score came from live Oanda D1 data.

    AUDIT-FIX (15/07/2026): companion to ``_strength_map``. Before this,
    ``_strength_map`` collapsed each ``CurrencyStrength`` row down to just
    its int score, discarding the ``driver`` field that distinguishes a
    live Oanda D1 row (``driver == "Oanda D1"``, set in
    ``_oanda_strength_scores``) from a CB-bias ``[PROXY]`` fallback row
    (``driver == ""``, set in ``build_currency_strength_ranking``). That
    meant ``_build_setup`` had no way to know the real source of the data
    behind an asset's directional edge, and hardcoded the "[PROXY]" tag on
    every setup card regardless â€” mislabeling genuinely live PRIMARY data
    as an approximation. This map restores that visibility without
    changing ``_strength_map`` itself (zero-regression: existing caller
    unaffected, this is purely additive).
    """
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

    Returns ``(direction, edge, source_is_live)``. ``source_is_live`` is
    True only when *both* legs of the pair are scored from live Oanda D1
    data (AUDIT-FIX 15/07/2026 â€” see ``_strength_source_map``); regime-tilt
    edges (commodities/indices, no ``ccys``) are never Oanda-sourced and
    stay False, matching their existing "[PROXY]" label unchanged.
    """
    if ccys:
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
        if overlap and len(priority) > 0 and len(overlap) >= 2:
            continue
        priority.append(setup)
        selected_ccys.update(ccys_set)
    # Relax guard if too aggressive
    if len(priority) < min(2, C.MAX_PRIORITY_ASSETS):
        for _score, setup in scored:
            if len(priority) >= C.MAX_PRIORITY_ASSETS:
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
    smap_src = _strength_source_map(currency_strength)  # AUDIT-FIX 15/07/2026
    ips_by_ccy = {r.currency: r for r in ips}
    min_score = C.MODE_SELECTION_MIN_SCORE.get(mode, 0.5)

    scored: list[tuple[float, AssetSetup]] = []
    avoid: list[tuple[str, str]] = []

    for asset in C.UNIVERSE:
        price = market.price(asset)
        if not price.available:
            continue

        ccys = C.INSTRUMENT_CCYS.get(asset)
        ev = _events_for_ccys(events, ccys) if ccys else []
        critical_now = [e for e in ev if e.priority == "CRITICAL"]

        if critical_now:
            ename = critical_now[0].event_name or "Ã©vÃ©nement majeur"
            avoid.append((asset, f"News binaire imminente ({ename}) â€” attendre la publication."))
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
                             ips_by_ccy, ccys, ev, cot_label, strength_is_live)
        scored.append((score, setup))

    scored.sort(key=lambda t: t[0], reverse=True)
    priority = _apply_correlation_guard(scored)

    no_setup = None
    if not priority:
        no_setup = ("Aucun actif ne rÃ©unit un biais macro directionnel suffisant "
                    "avec des niveaux exploitables aujourd'hui â€” donnÃ©es de prix/positionnement "
                    "insuffisantes ou rÃ©gime sans edge. ConformÃ©ment Ã  BLUESTAR v8.1, aucun "
                    "setup n'est forcÃ©.")
    return priority, avoid, no_setup


def _build_setup_levels(p: float, atr, direction: int, asset: str) -> tuple:
    """Compute buy/sell/stop levels from ATR. Returns (buy, sell, stop, levels_proxy)."""
    buy = p - C.LEVEL_ATR_MULT * atr if atr else None
    sell = p + C.LEVEL_ATR_MULT * atr if atr else None
    if direction > 0:
        stop = (p - C.STOP_ATR_MULT * atr) if atr else None
    else:
        stop = (p + C.STOP_ATR_MULT * atr) if atr else None
    return buy, sell, stop


# Currencies with no standalone CFTC Non-Commercials contract in
# institutional.CFTC_MARKETS (USD is the implicit counter-currency in all
# 7 legacy majors, not a line item itself; a separate ICE USD Index
# contract exists but is not currently integrated). Audit fix: previously
# these currencies were silently dropped from `candidates`, so a USD pair
# only ever showed squeeze risk on its non-USD leg with no indication that
# the USD leg was simply never measured.
_NO_STANDALONE_CFTC_CONTRACT = {"USD"}


def _build_setup_positioning(ccys, ips_by_ccy: dict, cot_label: str) -> tuple:
    """Compute positioning link, squeeze risk, IPS summary, both-legs-extreme flag."""
    pos_link = "Pas de COT chargÃ© pour cet actif [N/A] â€” positionnement non pris en compte."
    squeeze_risk, squeeze_cls = "Faible", "green"
    ips_summary = "[N/A]"

    def _append_untracked_note(summary: str, link: str) -> tuple[str, str]:
        untracked = [c for c in (ccys or []) if c in _NO_STANDALONE_CFTC_CONTRACT]
        if not untracked:
            return summary, link
        note = (f" Â· {', '.join(untracked)} : IPS [N/A] â€” pas de contrat CFTC "
                f"autonome (contrepartie implicite des 7 majors ; positionnement "
                f"non mesurÃ© ici, cf. USD Index ICE non intÃ©grÃ©).")
        return summary + note, link + note

    if not ccys:
        return pos_link, squeeze_risk, squeeze_cls, ips_summary, False
    candidates = [(ccy, ips_by_ccy.get(ccy)) for ccy in ccys]
    candidates = [(ccy, r) for ccy, r in candidates if r and r.ips_score is not None]
    extreme_candidates = [c for c in candidates if c[1].is_extreme]
    # Audit fix (both-legs-extreme, e.g. EUR/GBP both crowded-short): the old
    # logic picked the *first* extreme currency in `ccys` order and never
    # checked whether the other leg was also extreme, so a pair where both
    # currencies are in capitulation looked like a normal single-leg squeeze.
    both_extreme = len(extreme_candidates) >= 2
    chosen = next((c for c in candidates if c[1].is_extreme),
                  candidates[0] if candidates else None)
    if not chosen:
        pos_link, ips_summary = _append_untracked_note(ips_summary, pos_link)
        return pos_link, squeeze_risk, squeeze_cls, ips_summary, False
    ccy, r = chosen
    ips_summary = f"{ccy} {r.ips_score} â€” {r.ips_label}"
    if both_extreme:
        other_ccy, other_r = next(c for c in extreme_candidates if c[0] != ccy)
        ips_summary = (f"{ccy} {r.ips_score} ({r.ips_label}) Â· "
                       f"{other_ccy} {other_r.ips_score} ({other_r.ips_label}) â€” DEUX jambes extrÃªmes")
        squeeze_risk, squeeze_cls = f"Ã‰levÃ© (2 jambes extrÃªmes : {ccy}/{other_ccy})", "red"
        pos_link = (f"{ccy} ET {other_ccy} en zone extrÃªme simultanÃ©ment "
                    f"(IPS {r.ips_score} / {other_r.ips_score}). [{cot_label}]. "
                    "Les deux devises de la paire sont crowded â€” le squeeze peut se "
                    "dÃ©clencher dans les deux sens ; conviction plafonnÃ©e, stop strict.")
    elif r.is_extreme:
        squeeze_risk, squeeze_cls = f"Ã‰levÃ© ({ccy} IPS extrÃªme)", "red"
        pos_link = (f"{ccy} en zone extrÃªme (IPS {r.ips_score} Â· {r.ips_label}). "
                    f"[{cot_label}]. "
                    "Le COT ne dÃ©clenche pas le trade ; il signale un risque de "
                    "squeeze inverse si le catalyseur dÃ©Ã§oit â†’ conviction Â±, stop strict.")
    else:
        pos_link = (f"{ccy} IPS {r.ips_score} â€” {r.ips_label}. "
                    f"[{cot_label}]. "
                    "Positionnement non extrÃªme, n'amende pas la conviction.")
    ips_summary, pos_link = _append_untracked_note(ips_summary, pos_link)
    return pos_link, squeeze_risk, squeeze_cls, ips_summary, both_extreme


def _compute_rr_ratio(p: float, stop, sell, direction: int, atr) -> str:
    """Compute risk/reward ratio display string (audit B2 fix).

    C1 (certification, cause racine R-1): directional R:R measured from the
    macro ENTRY zone to the OPPOSITE (objective) zone, exactly as the recap
    footnote defines it (reward = entrÃ©e->objectif, risk = entrÃ©e->stop). For a
    long the entry is the buy zone and the objective is the sell zone; for a
    short the entry is the sell zone and the objective is the buy zone. The buy
    zone is reconstructed from the same construction as ``_build_setup_levels``
    (buy = p - LEVEL_ATR_MULT*atr), so the signature and the call site stay
    unchanged.
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
    return f"1:{fr_num(reward / risk, 1)}"


def _build_setup(asset: str, direction: int, score: float, market: MarketSnapshot,
                 allow_proxy_levels: bool, ips_by_ccy: dict, ccys, ev,
                 cot_label: str = "[PROXY]", strength_is_live: bool = False) -> AssetSetup:
    price = market.price(asset)
    em_disp, em_method, atr = compute_expected_move(asset, market)
    p = price.value

    buy, sell, stop = _build_setup_levels(p, atr, direction, asset)
    levels_proxy = atr is None or em_method.startswith("PROXY")

    if direction > 0:
        bias_class = action_class = "long"
        arrow, action, bias = "â†‘", "CHERCHER LONG", "ðŸŸ¢/ðŸŸ¡ LONG"
    else:
        bias_class = action_class = "short"
        arrow, action, bias = "â†“", "CHERCHER SHORT", "ðŸŸ¢/ðŸŸ¡ SHORT"

    conviction = 2 + int(round(score * 2))
    if levels_proxy or not allow_proxy_levels:
        conviction = max(1, conviction - 1)

    pos_link, squeeze_risk, squeeze_cls, ips_summary, both_extreme = _build_setup_positioning(
        ccys, ips_by_ccy, cot_label)
    if both_extreme:
        # Audit fix: longing/shorting a pair where BOTH currencies are
        # crowded-short (e.g. EUR/GBP) is not a clean squeeze trade â€” cap
        # conviction instead of letting the raw score-derived value through.
        conviction = min(conviction, 2)

    color = "green" if score >= 0.7 and not levels_proxy else "yellow"

    ev_names = ", ".join(sorted({e.event_name for e in ev})[:2]) if ev else "aucun catalyseur proche"
    invalidation = f"Retournement macro / catalyseur ({ev_names})"
    inval_level = (f"clÃ´ture {'sous le' if direction > 0 else 'au-dessus du'} stop "
                   f"{_level(stop, asset)}" if stop is not None else "[N/A]")

    # AUDIT-FIX (15/07/2026): this tag used to hardcode "[PROXY]" regardless
    # of the actual data source behind the directional edge, so a setup
    # built from live Oanda D1 currency strength (PRIMARY) was permanently
    # mislabeled as an approximation. It now reflects the real source
    # reported by _compute_direction_edge / _strength_source_map: "[Oanda
    # D1]" when both currency legs are live-scored, "[PROXY]" when either
    # leg fell back to CB-bias, and unchanged "[PROXY]" for the regime-tilt
    # case (commodities/indices have no currency-strength source at all).
    momentum_tag = "[Oanda D1]" if (ccys and strength_is_live) else "[PROXY]"

    return AssetSetup(
        asset=asset, color=color, bias=bias, bias_class=bias_class,
        reason_short=("momentum prix D1" if ccys else "tilt de rÃ©gime"),
        reason_macro=(f"DiffÃ©rentiel de momentum prix D1 {momentum_tag} favorable"
                      if ccys else "Biais de rÃ©gime (refuge / risque) [PROXY]"),
        conviction=conviction, action=action, action_class=action_class, arrow=arrow,
        zone_buy=_level(buy, asset) if buy is not None else "[N/A]",
        origin_buy="[ATR 14j Â· support implicite]" if buy is not None else "[N/A]",
        zone_sell=_level(sell, asset) if sell is not None else "[N/A]",
        origin_sell="[ATR 14j Â· rÃ©sistance implicite]" if sell is not None else "[N/A]",
        stop=_level(stop, asset) if stop is not None else "[N/A]",
        origin_stop="[ATR 14j Â· stop dynamique]" if stop is not None else "[N/A]",
        expected_move=em_disp, em_method=em_method,
        session="Londres / New York", session_reason="liquiditÃ© maximale",
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
    """Split events into ðŸ”´ Ã‰LEVÃ‰ and ðŸŸ¡ MODÃ‰RÃ‰, build beat/miss scenarios."""
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
        affected = " Â· ".join(pairs) if pairs else f"les paires {e.currency}"
        scenarios[key] = {
            "prev": e.previous, "cons": e.forecast,
            "beat_impact": (f"{e.currency} plus fort â†’ pression sur {affected}. "
                            "Ampleur proportionnelle Ã  l'Ã©cart au consensus "
                            "(voir move attendu par actif)."),
            "beat_action": f"Renforce les setups short {e.currency}-quote / long {e.currency}-base.",
            "miss_impact": (f"{e.currency} plus faible â†’ soutien inverse sur {affected}. "
                            "Ampleur proportionnelle Ã  l'Ã©cart au consensus."),
            "miss_action": f"Invalide les biais alignÃ©s sur un {e.currency} fort.",
            "advice": ("Ne pas ouvrir taille pleine avant la publication ; "
                       "attendre la confirmation post-chiffre."),
        }
    return high[:6], medium[:6], scenarios


def _pairs_for_ccy(ccy: str) -> list[str]:
    """Traded pairs that contain a given currency (deterministic, no [N/A])."""
    return [p for p, ccys in C.INSTRUMENT_CCYS.items() if ccy in ccys][:4]


# ---------------------------------------------------------------------------
# Step -- Macro overlay text blocks
# ---------------------------------------------------------------------------
def _build_cot_summary(ips: list) -> str:
    """Synthesize the qualitative COT/positioning summary for the 'COT &
    Positioning' card.

    AUDIT-FIX (15/07/2026, finding 1 â€” CRITIQUE): ``build_macro_overlay()``'s
    returned dict never had a ``"cot_summary"`` key, so ``build_context()``'s
    ``overlay.get("cot_summary", "")`` always resolved to an empty string â€”
    the card rendered as a bare date tag with no content, even though the
    IPS scores it should summarize are computed and displayed further down
    the same page. This reuses the already-computed ``ips_score`` thresholds
    (no new thresholds introduced) to describe which currencies, if any,
    sit in an extreme positioning zone.
    """
    if not ips:
        return "[N/A] â€” aucune donnÃ©e de positionnement disponible."
    crowded_long = [r.currency for r in ips
                    if r.ips_score is not None and r.ips_score >= C.IPS_CROWDED]
    crowded_short = [r.currency for r in ips
                     if r.ips_score is not None and r.ips_score <= C.IPS_CAPITULATION]
    extremes = crowded_long + crowded_short
    if not extremes:
        return "Aucune devise majeure en positionnement extrÃªme â€” scores IPS en zone neutre."
    parts = []
    if crowded_long:
        parts.append(f"{'/'.join(crowded_long)} crowded long")
    if crowded_short:
        parts.append(f"{'/'.join(crowded_short)} crowded short/capitulation")
    plural = "s" if len(extremes) > 1 else ""
    return (f"{len(extremes)} devise{plural} en positionnement extrÃªme "
            f"({', '.join(parts)}).")


def build_macro_overlay(market: MarketSnapshot, regime: str,
                        events: list[MacroEvent],
                        liquidity_msg: str,
                        pc_data: Optional[dict] = None,
                        ips: Optional[list] = None) -> dict:
    vix = market.gauge("VIX")
    move = market.gauge("MOVE")
    dxy = market.gauge("DXY")

    # Audit fix (empty-calendar narrative): claiming "pilotÃ©e par le calendrier
    # macro" while showing "prochain catalyseur : â€”" contradicts itself. When
    # there is no event to point to, switch the framing to what's actually
    # driving the read (volatility / positioning) instead of asserting
    # calendar primacy with a dash.
    if events:
        nearest = events[0].event_name
        theme = (f"Semaine pilotÃ©e par le calendrier macro (prochain catalyseur : {nearest}). "
                 f"RÃ©gime : {regime}.")
        theme_src = "[Forex Factory | calendrier]"
    else:
        theme = (f"Semaine sans catalyseur macro datÃ© majeur â€” lecture pilotÃ©e par le rÃ©gime "
                 f"de volatilitÃ© et le positionnement plutÃ´t que par le calendrier. RÃ©gime : {regime}.")
        theme_src = "[N/A] â€” aucun Ã©vÃ©nement CRITICAL/HIGH programmÃ©"

    if dxy.available:
        dxy_ctx = f"DXY {dxy.display} ({dxy.trend or 'tendance n/d'}) â€” impacte EUR/USD, USD/JPY, USD/CAD."
        dxy_src = dxy.stamp.render()
    else:
        dxy_ctx = "DXY [N/A] â€” contexte dollar non sourcÃ©."
        dxy_src = "[N/A]"

    if vix.available:
        method = "niveau absolu + position vs seuils (15 / 22)"
        vol_regime = f"VIX {vix.display} Â· MOVE {move.display if move.available else 'N/A'}"
        vol_impl = (f"MÃ©thode : {method}. "
                    + ("Vol comprimÃ©e â†’ stops plus serrÃ©s viables." if vix.value < 18
                       else "Vol modÃ©rÃ©e Ã  Ã©levÃ©e â†’ rÃ©duire la taille, Ã©largir les stops."))
        # DECOMMISSIONED (17/07/2026, ADR): CBOE Put/Call ratio removed from
        # the briefing. Root cause proven, not inferred: ^PCALL returns a
        # confirmed 404 "Quote not found" from Yahoo (delisted, verified live
        # by the user from their own machine), CBOE stopped public keyless
        # distribution of the aggregate ratio (403 AccessDenied on all
        # _PCALL/_EQUITYPC/_INDEXPC/_TOTALPC endpoints while _VIX/_SKEW on
        # the same CDN return 200 â€” a deliberate removal, not a network
        # fault), and four independent cross-model audits (17/07/2026) found
        # no free, documented, ToS-compliant programmatic source. The only
        # defensible paid options (Barchart OnDemand $CPC/$CPCI, YCharts API)
        # are disproportionate for a signal weighted 0.05 and never
        # regime-determining alone (see config.REGIME_MATERIAL_SIGNAL_WEIGHT
        # and _pc_indicator's own contract in regime_engine.py). Rather than
        # a permanent "[N/A] â€” CBOE indisponible" placeholder implying a
        # transient/fixable outage, the mention is removed outright â€” an
        # accurate reflection of a feature that has been retired, not one
        # that is temporarily broken. `pc_data` stays wired through
        # unchanged (still passed to _assess_regime/build_interpretation as
        # None) since both already degrade gracefully on None â€” no other
        # file needs to change.
    else:
        vol_regime = "VIX [N/A]"
        vol_impl = "MÃ©thode indisponible â€” rÃ©gime vol non Ã©valuable [N/A]."

    return {
        "theme": theme, "theme_src": theme_src,
        "dxy_ctx": dxy_ctx, "dxy_src": dxy_src,
        "vol_regime": vol_regime, "vol_impl": vol_impl,
        "correlation": f"{_correlation_short('EUR/USD', market)} Â· {_correlation_short('USD/JPY', market)}",
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
    anchor = events[0] if events else None
    if anchor is not None:
        anchor_name = anchor.event_name
        anchor_src = "[Forex Factory | calendrier]"
        bull_trig = f"{anchor_name} sous le consensus â†’ dÃ©tente des taux/vol"
        bear_trig = f"{anchor_name} au-dessus du consensus â†’ repricing hawkish / fuite vers la qualitÃ©"
        inval_txt = (f"DÃ©samorÃ§age du catalyseur principal ({anchor_name}) ou retour de la "
                     "volatilitÃ© dans sa fourchette â†’ rÃ©vision du scÃ©nario dominant.")
    else:
        # No dated catalyst in the residual window: anchor the scenario on the
        # prevailing volatility/risk regime instead of emitting "[N/A]".
        anchor_name = "rÃ©gime de volatilitÃ© (pas de catalyseur datÃ© dans la fenÃªtre)"
        anchor_src = "[BLUESTAR Â· rÃ©gime de marchÃ©]"
        bull_trig = "Compression de la volatilitÃ© / dÃ©tente des taux â†’ rotation risk-on"
        bear_trig = "Choc de volatilitÃ© / repricing hawkish â†’ fuite vers la qualitÃ©"
        inval_txt = ("Rupture du rÃ©gime de volatilitÃ© actuel (VIX/MOVE hors fourchette) "
                     "â†’ rÃ©vision du scÃ©nario dominant.")

    # A8 fix: reuse the FedWatch pause/cut/hike odds already sourced onto the
    # Fed's CentralBankSnapshot (see build_central_bank_context) instead of a
    # fixed qualitative placeholder -- but only when the anchor catalyst is
    # actually USD-denominated (NFP/CPI/FOMC etc.); a Fed rate-path
    # distribution isn't the relevant probability for e.g. a EUR or GBP
    # catalyst. Falls back to the original qualitative text otherwise.
    proba = "qualitative â€” Ã©quilibrÃ©"
    proba_src = anchor_src
    if anchor is not None and anchor.currency == "USD" and central_banks:
        fed = next((cb for cb in central_banks if cb.name == "FED"), None)
        if fed and fed.pause_pct is not None and fed.cut_pct is not None and fed.hike_pct is not None:
            proba = (f"Pause {fed.pause_pct}% Â· Baisse {fed.cut_pct}% "
                     f"Â· Hausse {fed.hike_pct}%")
            # MACRO-B2 FIX : L'attribution de source doit reflÃ©ter le stamp rÃ©el de la donnÃ©e.
            # Un "[CME FedWatch]" codÃ© en dur ment si la donnÃ©e provient d'un override manuel.
            fed_reliability = getattr(getattr(fed, "stamp", None), "reliability", None)
            if fed_reliability is Reliability.PRIMARY:
                proba_src = "[CME FedWatch]"
                # N4 (17/07/2026, audit A1): date de prÃ©lÃ¨vement quand le
                # payload la fournit â€” rend visible une proba figÃ©e.
                if getattr(fed, "fedwatch_as_of", None):
                    proba_src = f"[CME FedWatch Â· prÃ©lÃ¨vement {fed.fedwatch_as_of}]"
            elif fed_reliability is Reliability.PROXY:
                proba_src = "[PROXY Â· override manuel]"
            else:
                proba_src = anchor_src

    risk_main = {
        "desc": ("Surprise macro sur le principal catalyseur de la semaine "
                 f"({anchor_name}) dÃ©clenchant un repricing brutal." if events else
                 f"Repricing brutal pilotÃ© par le {anchor_name}."),
        "asset": priority[0].asset if priority else "â€”",
        # Use the primary asset's computed invalidation level when available.
        # [PROXY] is wrong here: we either have a real level or we don't.
        # [N/A] is the honest tag when the level cannot be determined.
        "level": (priority[0].invalidation_level
                  if priority and priority[0].invalidation_level not in ("[N/A]", "", None)
                  else "seuil de bascule = sortie du rÃ©gime de volatilitÃ© courant (VIX/MOVE)"),
        "proba": proba,
        "source": proba_src,
    }
    bull = RiskScenario(
        title="ScÃ©nario BULL (risk-on)", proba="favori si donnÃ©es molles",
        trigger=bull_trig,
        trigger_source=anchor_src,
        rows=[f"{s.asset} â†’ mouvement alignÃ© risk-on" for s in priority[:3]] or
             ["USD faible â†’ EUR/USD â†‘ Â· Or â†‘ Â· US10Y â†“"],
    )
    bear = RiskScenario(
        title="ScÃ©nario BEAR (risk-off)", proba="favori si donnÃ©es chaudes / choc",
        trigger=bear_trig,
        trigger_source=anchor_src,
        rows=["Refuges : Or Â· JPY Â· CHF"] +
             ([f"{s.asset} â†’ mouvement alignÃ© risk-off" for s in priority[:3]]
              or ["USD fort â†’ US10Y â†‘ Â· JPY faible â†’ Or â†“"]),
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
    now_cet = now_utc.astimezone(C.TZ_CET)
    _, is_live = session_label(now_cet)

    # v10.1 (Incident Review Board â€” P0): rapport de couverture/fraÃ®cheur des
    # sources marchÃ© pour la certification (additif, best-effort; n'affecte aucune
    # valeur mÃ©tier ni la sÃ©lection d'actifs).
    try:
        from .staleness import build_coverage_report as _build_coverage_report
        _coverage = _build_coverage_report(market, now_utc)
    except Exception:
        _coverage = None

    raw_engine = calendar.get("events_engine") or calendar.get("events") or []
    events = [MacroEvent.from_enriched(e) for e in raw_engine]
    upcoming = [e for e in events if e.is_upcoming]

    # Surprise / Momentum gauge â€” BLUESTAR free substitute for the proprietary
    # Citi CESI. Uses released actuals when present, else consensus-vs-previous
    # momentum, so the KPI is never blank. Populated here because this is where
    # the enriched calendar events (with actuals) are available.
    if _inst is not None and "SURPRISE_IDX" not in (overrides.get("market") or {}):
        try:
            si = _inst.fetch_macro_surprise("USD", raw_events=events)
            if si is not None:
                disp = f"{'+' if si.value >= 0 else ''}{si.value:.0f}"
                gauge_mode = "US Macro Surprise" if "Surprise" in si.source else "US Macro Momentum"
                market.gauges["SURPRISE_IDX"] = Datum(
                    si.value,
                    SourceStamp("BLUESTAR Â· Forex Factory", Reliability.PRIMARY, timestamp=now_utc),
                    disp, f"{si.trend} {gauge_mode} Â· n={si.n}",
                )
            else:
                # No USD high-impact event in the current calendar window -- honest
                # N/A with an accurate reason (replaces the generic default set
                # upstream in build_market_snapshot, which wrongly implies a
                # missing API key).
                market.gauges["SURPRISE_IDX"] = Datum(
                    None,
                    na_stamp("aucun Ã©vÃ¨nement USD high-impact dans la fenÃªtre calendrier courante"),
                    "N/A",
                )
        except Exception as exc:  # pragma: no cover - never break the pipeline
            logger.warning("surprise gauge injection failed: %s", exc)
            market.gauges["SURPRISE_IDX"] = Datum(
                None, na_stamp("erreur interne pendant le calcul du surprise index (voir logs)"), "N/A",
            )

    regime, regime_cls, regime_since, _pen = determine_market_regime(market, events)

    # v9.0: Enhanced regime assessment using the multi-factor regime engine.
    # The original VIX-only regime is kept as the "headline" for backward
    # compatibility, but the full RegimeAssessment is computed and attached
    # for the interpretation layer and the new HTML sections.
    try:
        from .regime_engine import assess_regime as _assess_regime
        # Need central_banks and cs first â€” but they're computed below.
        # We'll compute the regime assessment after cs is ready.
        _regime_pending = True
    except Exception:
        _regime_pending = False

    # A6-fix: sequential execution to avoid SIGSEGV from nested
    # ThreadPoolExecutor + curl_cffi/libcurl thread-unsafety.
    central_banks = build_central_bank_context(overrides, now_utc)
    ips, cot_ref_label = build_ips_scores(overrides, now_utc)
    sofr_effr_bp = fetch_liquidity_stress()
    # DECOMMISSIONED (17/07/2026, ADR â€” see build_macro_overlay comment for
    # full rationale): fetch_pc_ratio() is now a documented no-op stub in
    # external_sources.py that returns None instantly, no network call.
    # Left wired (not deleted) so pc_data continues to flow through
    # unchanged to _assess_regime/build_interpretation exactly as it already
    # did during the weeks CBOE was unreachable â€” zero behavioural change
    # downstream, just no more wasted HTTP attempts.
    try:
        _vix_gauge = market.gauge("VIX")
        pc_data = fetch_pc_ratio(
            vix_value=_vix_gauge.value if _vix_gauge.available else None
        )
    except Exception as exc:   # pragma: no cover â€” jamais casser le pipeline
        logger.warning("fetch_pc_ratio failed: %s", exc)
        pc_data = None

    cs = build_currency_strength_ranking(central_banks, regime_cls)
    cs = _oanda_strength_scores(market, cs)   # BLUESTAR-PATCH v10.0
    high, medium, scenarios = build_catalysts(events)

    # Audit fix (residual regime mismatch): regime_assessment only needs
    # market/central_banks/cs/ips/events/pc_data, all of which are ready
    # here â€” so it's computed now, BEFORE build_macro_overlay, instead of
    # after priority/setups. This lets Section 3's "ThÃ¨me macro" cite the
    # same reconciled regime name as Section 1 and Section 6, rather than
    # the stale VIX-only `regime` string (previously the last place in the
    # document still showing e.g. "MIXTE" while everywhere else said
    # "Mixed / Selective").
    regime_assessment = None
    if _regime_pending:
        try:
            regime_assessment = _assess_regime(market, central_banks, cs, ips, events, now_utc, pc_data)
        except Exception as exc:
            logger.warning("Regime engine failed: %s", exc)
    headline_regime_name = regime_assessment.name if regime_assessment is not None else regime

    # Liquidity / funding stress â€” SOFRâˆ’EFFR spread (bp) via FRED, else [N/A].
    # NOTE: TEDRATE was discontinued (2022-01-31); the gauge is now the
    # SOFRâˆ’EFFR spread expressed in basis points. Thresholds below (8 bp /
    # 15 bp) are HEURISTIC and must be backtested against SOFR/EFFR history
    # before production use. (sofr_effr_bp fetched concurrently above.)
    if sofr_effr_bp is not None:
        if sofr_effr_bp >= 15.0:
            tone = "tension de financement USD notable"
        elif sofr_effr_bp >= 8.0:
            tone = "lÃ©gÃ¨re tension de financement USD"
        else:
            tone = "pas de stress de financement USD"
        liquidity_msg = (f"Spread SOFRâˆ’EFFR {fr_num(sofr_effr_bp, 1)} bp â†’ {tone} "
                         "[FRED Â· SOFR/EFFR]. Surveiller les flux de fin de "
                         "journÃ©e et les rebalancings de fonds monÃ©taires.")
    else:
        liquidity_msg = "[N/A] â€” spread SOFRâˆ’EFFR non sourcÃ©."

    overlay = build_macro_overlay(market, headline_regime_name, upcoming, liquidity_msg, pc_data, ips=ips)
    
    # MACRO-B3 FIX: le suffixe [PROXY Â· scaling linÃ©aire] apparaÃ®t dÃ¨s qu'un chemin
    # heuristique (overrides/scrape) alimente l'IPS. "OBSERVÃ‰" nu est rÃ©servÃ©
    # au chemin z-score/percentile rÃ©el (institutional/Socrata).
    #
    # AUDIT-FIX (15/07/2026, finding 3 â€” MAJEURE): "OBSERVÃ‰" used to be
    # baked into cot_ref_label itself, so this branch's PROXY suffix could
    # end up appended to an already-"OBSERVÃ‰"-labelled string, producing a
    # self-contradictory "OBSERVÃ‰ ... [PROXY Â· scaling linÃ©aire]" tag
    # whenever the resolved IPS rows were not all PRIMARY. cot_ref_label is
    # now reliability-neutral (see build_ips_scores) and the "OBSERVÃ‰ â€”"
    # prefix is only prepended here, in the one branch where it's actually
    # true. Output is byte-identical to before for the PRIMARY-only case;
    # the PROXY/mixed case no longer carries the contradictory prefix.
    if not ips:
        cot_date = "[N/A]"
    else:
        reliabs = {r.stamp.reliability for r in ips if r.stamp is not None}
        if Reliability.PRIMARY in reliabs and Reliability.PROXY not in reliabs:
            cot_date = f"OBSERVÃ‰ â€” {cot_ref_label}"
        else:
            cot_date = cot_ref_label + " [PROXY Â· scaling linÃ©aire]"

    priority, avoid, no_setup = select_priority_assets(
        market, regime_cls, central_banks, cs, ips, events, mode,
        allow_proxy_levels, cot_label=cot_date,
    )
    
    # AUDIT-FIX (17/07/2026, anomalie A3 de l'audit externe du 16/07):
    # build_risk_scenarios recevait `events` (tous les Ã©vÃ©nements enrichis,
    # passÃ©s inclus), donc anchor = events[0] pouvait Ãªtre un catalyseur DÃ‰JÃ€
    # PUBLIÃ‰ (ex. briefing du 16/07 citant en Â« risque principal Â» le Core CPI
    # sorti le 14/07), en contradiction avec la section 2 qui, elle, filtrait
    # dÃ©jÃ  sur is_upcoming. On passe dÃ©sormais `upcoming` â€” le mÃªme sous-ensemble
    # futur que build_macro_overlay reÃ§oit juste au-dessus â€” donc l'ancre du
    # scÃ©nario est toujours un catalyseur Ã  venir, ou le fallback honnÃªte
    # Â« rÃ©gime de volatilitÃ© (pas de catalyseur datÃ© dans la fenÃªtre) Â» quand
    # la fenÃªtre est vide. ZÃ©ro changement de signature.
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
                positioning_alert = (f"{ccy} en zone extrÃªme (IPS {ips_r.ips_score}) â€” "
                                     f"risque de squeeze si catalyseur dÃ©Ã§oit.")
                break
        if positioning_alert:
            break

    # v9.0 interpretation layer (needs `priority`, computed just above; the
    # regime assessment itself was already computed earlier â€” see note near
    # build_macro_overlay).
    interpretation = None
    if regime_assessment is not None:
        try:
            from .interpretation import build_interpretation
            interpretation = build_interpretation(market, central_banks, cs, ips, regime_assessment, priority, now_utc, pc_data)
        except Exception as exc:
            logger.warning("Interpretation engine failed: %s", exc)

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
        # v10.1 (Incident Review Board â€” P0): certification fraÃ®cheur/couverture/diagnostic
        calendar_reachable=bool(calendar.get("metadata", {}).get("reachable", True)),
        calendar_source=str(calendar.get("metadata", {}).get("source", "Forex Factory")),
        coverage=_coverage,
        coverage_summary=_coverage.summary_line() if _coverage is not None else "",
        stale_fields=([f.field_name for f in _coverage.fields if f.is_stale]
                      if _coverage is not None else []),
    )
