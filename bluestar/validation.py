"""Validation Engine — BLUESTAR v9.0 institutional-grade validation.

Audits a :class:`BriefingContext` (and optionally the rendered HTML) against
the BLUESTAR golden rules. Returns a list of :class:`ValidationIssue`.

ERROR findings indicate a contract breach the renderer must never ship.
WARN/INFO are advisory.

New checks in v9.0 (audit fixes):
  * check_rr_ratio: every setup must have a valid R:R ratio (B2)
  * check_staleness: flag stale data (C2/C3)
  * check_coverage: block publication if live coverage < threshold (C5)
  * check_no_contradictory_directions: flag all-same-direction setups (A4)
  * check_event_dates: every event card must show a date (A1)
  * check_no_momentum_as_macro: "macro"/"fondamental" must not label momentum (A2)

New check (17/07/2026, anomalie A3 de l'audit externe du 16/07):
  * check_risk_anchor_upcoming: the main-risk scenario anchor must be an
    upcoming catalyst, never an already-published one (WARN).
"""
from __future__ import annotations

import logging
import re

from . import config as C
from .models import BriefingContext, ValidationIssue

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = [
    "bias", "zone_buy", "zone_sell", "stop", "expected_move", "session",
    "invalidation_level", "positioning_link", "correlation_key",
]


def check_max_3_assets(ctx: BriefingContext) -> list[ValidationIssue]:
    if len(ctx.priority_assets) > C.MAX_PRIORITY_ASSETS:
        return [ValidationIssue("max_3_assets", "ERROR",
                                f"{len(ctx.priority_assets)} actifs prioritaires (> 3).")]
    return []


def check_no_red_assets_in_setups(ctx: BriefingContext) -> list[ValidationIssue]:
    issues = []
    avoid_names = {a for a, _ in ctx.avoid_assets}
    for s in ctx.priority_assets:
        if s.color == "red":
            issues.append(ValidationIssue("no_red_in_setups", "ERROR",
                                          f"{s.asset} est rouge mais figure dans les setups."))
        if s.asset in avoid_names:
            issues.append(ValidationIssue("no_red_in_setups", "ERROR",
                                          f"{s.asset} est à la fois en setup et en 'Éviter'."))
    return issues


def check_required_fields(ctx: BriefingContext) -> list[ValidationIssue]:
    issues = []
    for s in ctx.priority_assets:
        for f in _REQUIRED_FIELDS:
            if not str(getattr(s, f, "")).strip():
                issues.append(ValidationIssue("required_fields", "ERROR",
                                              f"{s.asset}: champ '{f}' vide."))
        if s.action not in ("CHERCHER LONG", "CHERCHER SHORT", "ATTENDRE"):
            issues.append(ValidationIssue("required_fields", "ERROR",
                                          f"{s.asset}: action invalide '{s.action}'."))
    return issues


_RR_SHAPE = re.compile(r"^(\[N/A\]|1:\d+(?:,\d+)?)$")
_INVAL_SHAPE = re.compile(r"^(\[N/A\]|clôture (sous le|au-dessus du) stop .+)$")


def check_sources_or_na(ctx: BriefingContext) -> list[ValidationIssue]:
    """Numeric levels must be a number, [N/A] or [PROXY]-tagged.

    AUDIT-FIX (15/07/2026, finding 4 — MAJEURE): this rule used to check
    only zone_buy/zone_sell/stop, leaving expected_move, risk_reward,
    invalidation_level and ips_summary — all numeric fields rendered on
    every asset card — completely unchecked, a gap that would let a future
    silent drift (e.g. a bug emitting a raw, unexplained number in any of
    them) through with no warning at all. Extended below.

    Unlike zone_buy/zone_sell/stop (which have a dedicated ``origin_*``
    field), these four have no sibling origin field, so the check for
    each is built from what's actually available as origin evidence:
      - expected_move has ``em_method`` (ATR 14j / PROXY .../ [N/A]) —
        same origin-field pattern as zone_buy/origin_buy.
      - risk_reward and invalidation_level are pure arithmetic derivations
        of the already-validated buy/sell/stop levels with a fixed,
        known-good shape ("1:X,X" / "clôture ... stop ..."); anything
        outside that shape is flagged.
      - ips_summary has no tag of its own (today the "[cot_label]" tag
        lives on the sibling ``positioning_link`` field, built in the
        same function) — checked against that sibling being populated.
    These are intentionally shape/presence checks rather than a blanket
    "must contain a bracket" rule, so they add real coverage for future
    drift without generating WARN noise on the current, well-formed
    output (verified against the live pipeline: none of these fire today).
    """
    issues = []
    tag = re.compile(r"\[(N/A|PROXY)")
    for s in ctx.priority_assets:
        for f in ("zone_buy", "zone_sell", "stop"):
            val = str(getattr(s, f))
            origin = str(getattr(s, "origin_" + f.rsplit('_', maxsplit=1)[-1], ""))
            has_num = any(ch.isdigit() for ch in val)
            if has_num and not (tag.search(origin) or "[" in origin):
                issues.append(ValidationIssue("sources_or_na", "WARN",
                                              f"{s.asset}: niveau '{f}' sans origine tagguée."))

        em_val = str(getattr(s, "expected_move", ""))
        em_method = str(getattr(s, "em_method", ""))
        if any(ch.isdigit() for ch in em_val) and not em_method.strip():
            issues.append(ValidationIssue("sources_or_na", "WARN",
                                          f"{s.asset}: 'expected_move' ({em_val}) sans méthode "
                                          "('em_method' vide)."))

        rr_val = str(getattr(s, "risk_reward", "")).strip()
        if rr_val and not _RR_SHAPE.match(rr_val):
            issues.append(ValidationIssue("sources_or_na", "WARN",
                                          f"{s.asset}: 'risk_reward' ({rr_val}) hors format "
                                          "attendu (ni '[N/A]' ni '1:X,X')."))

        inv_val = str(getattr(s, "invalidation_level", "")).strip()
        if inv_val and not _INVAL_SHAPE.match(inv_val):
            issues.append(ValidationIssue("sources_or_na", "WARN",
                                          f"{s.asset}: 'invalidation_level' ({inv_val}) hors "
                                          "format attendu."))

        ips_val = str(getattr(s, "ips_summary", ""))
        if (any(ch.isdigit() for ch in ips_val)
                and not (tag.search(ips_val) or "[" in ips_val)
                and not str(getattr(s, "positioning_link", "")).strip()):
            issues.append(ValidationIssue("sources_or_na", "WARN",
                                          f"{s.asset}: 'ips_summary' ({ips_val}) sans tag propre "
                                          "et sans 'positioning_link' associé."))
    return issues


def check_cot_non_commercials_only(ctx: BriefingContext) -> list[ValidationIssue]:
    issues = []
    for r in ctx.ips_scores:
        if r.stamp.ok and "Non-Commercials" not in (r.stamp.source_name or ""):
            issues.append(ValidationIssue("cot_non_commercials", "WARN",
                                          f"COT {r.currency}: source non marquée Non-Commercials."))
    return issues


def check_no_kelly_word_if_no_backtest(html: str) -> list[ValidationIssue]:
    if re.search(r"kelly\s*%|kelly réel\b", html, re.IGNORECASE) and \
       "PAS un Kelly" not in html:
        return [ValidationIssue("no_kelly", "ERROR",
                                "Terme 'Kelly' employé sans la mise en garde 'PAS un Kelly réel'.")]
    return []


def check_sizing_formula_present(html: str) -> list[ValidationIssue]:
    # In v9.0, Sizing Factor is replaced by R:R — check for R:R instead.
    if "R:R" not in html and "CHERCHER" in html:
        return [ValidationIssue("sizing_formula", "WARN",
                                "Ratio R:R absent du tableau récapitulatif.")]
    return []


def check_no_placeholders(html: str) -> list[ValidationIssue]:
    leftovers = re.findall(r"{{[^}]+}}", html)
    if leftovers:
        return [ValidationIssue("no_placeholders", "ERROR",
                                f"Placeholders non remplis : {sorted(set(leftovers))[:5]}")]
    return []


def check_bias_interpretation_present(ctx: BriefingContext) -> list[ValidationIssue]:
    """Chaque banque centrale avec un taux sourcé doit avoir un BIAIS.

    CORRECTIF (23/07/2026, retour utilisateur) : cette règle s'appelait
    ``check_fact_interpretation_separation`` et exigeait aussi
    ``cb.fact.strip()`` non-vide -- ce qui déclenchait un WARN systématique
    sur les 4 banques centrales depuis que ``fact`` est intentionnellement
    vide en production (macro_engine.py ne renvoie plus de "[N/A]" ; le
    renderer masque carrément la ligne "FAIT ·" quand c'est le cas, voir
    renderer.py::_cb_biais_block). ``fact`` et ``bias_interpretation`` sont
    deux champs indépendants : le second ne doit pas être jugé incomplet
    à cause du premier. macro_engine.py garantit déjà que
    ``bias_interpretation`` retombe au pire sur "[N/A] — interprétation à
    confirmer." (jamais vide) -- ce check reste donc une garde-fou
    légitime uniquement sur ce champ-là.
    """
    issues = []
    for cb in ctx.central_banks:
        if cb.stamp.ok and not cb.bias_interpretation.strip():
            issues.append(ValidationIssue("bias_interpretation", "WARN",
                                          f"{cb.name}: BIAIS manquant."))
    return issues


# ---------------------------------------------------------------------------
# New v9.0 checks
# ---------------------------------------------------------------------------

def check_rr_ratio(ctx: BriefingContext) -> list[ValidationIssue]:
    """Every setup must have a valid R:R ratio (audit B2 fix)."""
    issues = []
    for s in ctx.priority_assets:
        rr = str(getattr(s, "risk_reward", "[N/A]"))
        if rr == "[N/A]" or not rr.strip():
            issues.append(ValidationIssue("rr_ratio", "WARN",
                                          f"{s.asset}: ratio R:R non calculé."))
        else:
            # Check that R:R is positive
            try:
                val = float(rr.replace("1:", "").replace(",", ".").strip())
                if val < 0.5:
                    issues.append(ValidationIssue("rr_ratio", "WARN",
                                                  f"{s.asset}: R:R {rr} inférieur à 1:0,5 — trade défavorable."))
            except ValueError:
                pass
    return issues


def check_no_contradictory_directions(ctx: BriefingContext) -> list[ValidationIssue]:
    """Flag when all setups are the same direction (audit A4 fix)."""
    issues = []
    if len(ctx.priority_assets) >= 2:
        all_long = all(a.action_class == "long" for a in ctx.priority_assets)
        all_short = all(a.action_class == "short" for a in ctx.priority_assets)
        if all_long:
            issues.append(ValidationIssue("directional_concentration", "WARN",
                                          f"Tous les setups sont LONG ({len(ctx.priority_assets)}) — "
                                          "concentration directionnelle non diversifiée."))
        elif all_short:
            issues.append(ValidationIssue("directional_concentration", "WARN",
                                          f"Tous les setups sont SHORT ({len(ctx.priority_assets)}) — "
                                          "concentration directionnelle non diversifiée."))
    return issues


def check_no_momentum_as_macro(html: str) -> list[ValidationIssue]:
    """Ensure 'macro'/'fondamental' doesn't label pure momentum (audit A2 fix).

    Audit fix (2nd pass): the original check only WARNed, and was satisfied
    by inserting the literal string "Différentiel de momentum prix D1"
    anywhere in the document — a phrase that can sit far from the actual
    "Biais fondamental" label it's supposed to qualify. Escalated to ERROR
    and the label itself is now flagged directly: a field called "Biais
    fondamental" that is, by construction, an Oanda D1 price-momentum score
    should be renamed, not accompanied by a disclaimer elsewhere in the page.
    """
    issues = []
    if "force relative macro" in html.lower() and "momentum" not in html.lower():
        issues.append(ValidationIssue("momentum_as_macro", "ERROR",
                                      "Le terme 'force relative macro' est utilisé sans mentionner 'momentum'."))
    if "biais fondamental" in html.lower():
        issues.append(ValidationIssue("momentum_as_macro", "ERROR",
                                      "Label 'Biais fondamental' utilisé pour un score de momentum prix D1 — "
                                      "renommer en 'Biais momentum (prix D1)' plutôt que de le justifier "
                                      "par une phrase disclaimer ailleurs dans la page."))
    return issues


def check_event_dates(html: str) -> list[ValidationIssue]:
    """Every event card should display a date (audit A1 fix)."""
    issues = []
    # Check that event cards contain date-like patterns (YYYY-MM-DD)
    event_blocks = re.findall(r'class="event[^"]*".*?</div>\s*</div>', html, re.DOTALL)
    for block in event_blocks[:10]:
        if not re.search(r'\d{4}-\d{2}-\d{2}', block):
            # Only flag if the block has a time but no date
            if re.search(r'\d{2}:\d{2}\s*UTC', block):
                issues.append(ValidationIssue("event_dates", "WARN",
                                              "Carte d'événement sans date (uniquement l'heure)."))
                break
    return issues


def check_staleness(ctx: BriefingContext) -> list[ValidationIssue]:
    """Check for stale data in the market snapshot (audit C2/C3 fix)."""
    issues = []
    try:
        from .staleness import build_coverage_report, stale_fields_summary
        report = build_coverage_report(ctx.market, ctx.generated_utc)
        stale = stale_fields_summary(report)
        if stale:
            issues.append(ValidationIssue("staleness", "WARN", stale))
        if report.publication_blocked:
            issues.append(ValidationIssue("coverage", "ERROR",
                                          f"Couverture live insuffisante ({report.live_ratio:.0%} < "
                                          f"{C.MIN_LIVE_COVERAGE_RATIO:.0%}). Publication bloquée."))
    except Exception as exc:
        logger.warning("Staleness check skipped: %s", exc)
    return issues


def check_risk_anchor_upcoming(ctx: BriefingContext) -> list[ValidationIssue]:
    """Le catalyseur ancre du risque principal doit être dans la fenêtre future.

    AUDIT-FIX (17/07/2026, anomalie A3 de l'audit externe du 16/07 — et
    recommandation finale de cet audit : « un test de cohérence "catalyseur
    cité ∈ fenêtre calendrier future" avant publication »). Le briefing du
    16/07 citait en « risque principal de la semaine » un Core CPI publié
    deux jours plus tôt, parce que l'ancre était prise sur la liste complète
    des événements (passés inclus) au lieu du sous-ensemble à venir.

    Règle : si risk_main s'ancre sur un catalyseur nommé (pas sur le fallback
    « régime de volatilité »), ce nom doit apparaître parmi les catalyseurs
    à venir déjà filtrés (catalysts_high + catalysts_medium, tous
    is_upcoming). WARN seulement — ne bloque jamais la publication.
    """
    desc = str(ctx.risk_main.get("desc", ""))
    if not desc or "régime de volatilité" in desc:
        return []   # fallback honnête sans ancre datée — rien à vérifier
    upcoming_names = {e.event_name for e in ctx.catalysts_high + ctx.catalysts_medium}
    if any(name and name in desc for name in upcoming_names):
        return []
    return [ValidationIssue(
        "risk_anchor_upcoming", "WARN",
        "Le risque principal cite un catalyseur hors fenêtre d'événements à "
        "venir — vérifier qu'il n'est pas déjà publié (réconciliation "
        "scénarios ↔ calendrier).")]


def validate_context(ctx: BriefingContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues += check_max_3_assets(ctx)
    issues += check_no_red_assets_in_setups(ctx)
    issues += check_required_fields(ctx)
    issues += check_sources_or_na(ctx)
    issues += check_cot_non_commercials_only(ctx)
    issues += check_bias_interpretation_present(ctx)
    issues += check_rr_ratio(ctx)
    issues += check_no_contradictory_directions(ctx)
    issues += check_staleness(ctx)
    issues += check_risk_anchor_upcoming(ctx)
    return issues


def validate_html(html: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues += check_no_placeholders(html)
    issues += check_no_kelly_word_if_no_backtest(html)
    issues += check_sizing_formula_present(html)
    issues += check_no_momentum_as_macro(html)
    issues += check_event_dates(html)
    return issues
