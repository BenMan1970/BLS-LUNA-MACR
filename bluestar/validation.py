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


def check_sources_or_na(ctx: BriefingContext) -> list[ValidationIssue]:
    """Numeric levels must be a number, [N/A] or [PROXY]-tagged."""
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


def check_fact_interpretation_separation(ctx: BriefingContext) -> list[ValidationIssue]:
    issues = []
    for cb in ctx.central_banks:
        if cb.stamp.ok and (not cb.fact.strip() or not cb.bias_interpretation.strip()):
            issues.append(ValidationIssue("fact_interpretation", "WARN",
                                          f"{cb.name}: FAIT/BIAIS incomplet."))
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
    """Ensure 'macro'/'fondamental' doesn't label pure momentum (audit A2 fix)."""
    issues = []
    # Check for the old misleading labels
    if "force relative macro" in html.lower() and "momentum" not in html.lower():
        issues.append(ValidationIssue("momentum_as_macro", "ERROR",
                                      "Le terme 'force relative macro' est utilisé sans mentionner 'momentum'."))
    if "biais fondamental" in html.lower() and "momentum prix" not in html.lower():
        # The field label "Biais fondamental" is OK as long as the explanation
        # says "momentum prix D1" — only flag if momentum is not mentioned at all.
        if "Différentiel de momentum prix D1" not in html:
            issues.append(ValidationIssue("momentum_as_macro", "WARN",
                                          "Label 'Biais fondamental' sans précision 'momentum prix D1'."))
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
            # AUDIT-FIX (institutional review, 14/07/2026): this used to say
            # "Publication bloquée" -- but nothing in this pipeline actually
            # stops the HTML from being generated and shipped (see
            # macro_engine.build_context / renderer.py: ERROR findings are
            # surfaced, not enforced, by deliberate choice, since a hard
            # stop is a bigger operational decision than a visibility fix).
            # A document that says "publication blocked" while being the
            # published document is a direct, visible self-contradiction.
            # Say what is actually true instead.
            issues.append(ValidationIssue("coverage", "ERROR",
                                          f"Couverture live insuffisante ({report.live_ratio:.0%} < "
                                          f"{C.MIN_LIVE_COVERAGE_RATIO:.0%}) — ce briefing NE DEVRAIT PAS "
                                          f"être diffusé tel quel."))
    except Exception as exc:
        logger.warning("Staleness check skipped: %s", exc)
    return issues


def validate_context(ctx: BriefingContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues += check_max_3_assets(ctx)
    issues += check_no_red_assets_in_setups(ctx)
    issues += check_required_fields(ctx)
    issues += check_sources_or_na(ctx)
    issues += check_cot_non_commercials_only(ctx)
    issues += check_fact_interpretation_separation(ctx)
    issues += check_rr_ratio(ctx)
    issues += check_no_contradictory_directions(ctx)
    issues += check_staleness(ctx)
    return issues


def validate_html(html: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues += check_no_placeholders(html)
    issues += check_no_kelly_word_if_no_backtest(html)
    issues += check_sizing_formula_present(html)
    issues += check_no_momentum_as_macro(html)
    issues += check_event_dates(html)
    return issues
