"""Validation Engine.

Audits a :class:`BriefingContext` (and optionally the rendered HTML) against the
BLUESTAR v8.1 golden rules. Returns a list of :class:`ValidationIssue`. ERROR
findings indicate a contract breach the renderer must never ship; WARN/INFO are
advisory.
"""
from __future__ import annotations

import re

from . import config as C
from .models import BriefingContext, ValidationIssue

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
    if "Sizing Factor" in html and "1/(1+VIX/30" not in html.replace(" ", ""):
        return [ValidationIssue("sizing_formula", "WARN",
                                "Formule du Sizing Factor absente du pied de tableau.")]
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


def validate_context(ctx: BriefingContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues += check_max_3_assets(ctx)
    issues += check_no_red_assets_in_setups(ctx)
    issues += check_required_fields(ctx)
    issues += check_sources_or_na(ctx)
    issues += check_cot_non_commercials_only(ctx)
    issues += check_fact_interpretation_separation(ctx)
    return issues


def validate_html(html: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues += check_no_placeholders(html)
    issues += check_no_kelly_word_if_no_backtest(html)
    issues += check_sizing_formula_present(html)
    return issues
