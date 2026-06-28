"""End-to-end pipeline glue: data -> engine -> render -> validate.

Kept free of Streamlit so it can be driven from the UI, a cron job, or tests.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz

from .calendar_layer import build_calendar
from .macro_engine import build_context
from .oanda_data import build_market_snapshot
from .models import BriefingContext, ValidationIssue
from .renderer import render_html
from .validation import validate_context, validate_html

logger = logging.getLogger(__name__)


def generate_briefing(
    now_utc: Optional[datetime] = None,
    overrides: Optional[dict] = None,
    mode: str = "Normal",
    allow_proxy_levels: bool = True,
    raw_calendar: Optional[list] = None,
    market_snapshot=None,
) -> tuple[str, BriefingContext, list[ValidationIssue]]:
    """Run the full pipeline.

    Returns ``(html, context, issues)``. ``raw_calendar`` / ``market_snapshot``
    can be injected for tests (offline / deterministic).
    """
    now_utc = now_utc or datetime.now(pytz.UTC)
    calendar = build_calendar(now_utc=now_utc, raw_data=raw_calendar)
    market = market_snapshot or build_market_snapshot(
        now_utc=now_utc,
        overrides=(overrides or {}).get("market"),
        allow_proxy_levels=allow_proxy_levels,
    )
    ctx = build_context(now_utc, market, calendar, overrides, mode, allow_proxy_levels)
    issues = validate_context(ctx)
    html = render_html(ctx)
    issues += validate_html(html)
    ctx.issues = issues
    errors = [i for i in issues if i.severity == "ERROR"]
    if errors:
        logger.warning("Validation produced %d error(s): %s",
                       len(errors), [e.message for e in errors])
    return html, ctx, issues
  
