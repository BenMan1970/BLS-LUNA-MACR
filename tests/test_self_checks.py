"""Internal self-checks -- run with `pytest` or via `run_self_checks()`.

These tests use **synthetic, offline** data (no network) so they are
deterministic and prove the engine's contracts without external dependencies.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytz

from bluestar.calendar_layer import build_calendar, fmt_until, get_session
from bluestar.macro_engine import build_context
from bluestar.market_data import build_market_snapshot, fr_num
from bluestar.models import MarketSnapshot, Datum, SourceStamp, Reliability, na_stamp
from bluestar.renderer import render_html
from bluestar.validation import validate_context, validate_html

UTC = pytz.UTC


def _fake_raw(now: datetime) -> list[dict]:
    """Two high-impact events: one upcoming NFP, one recent (within 72h)."""
    soon = (now + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        {"title": "Non-Farm Employment Change", "country": "USD", "date": soon,
         "impact": "High", "forecast": "114K", "previous": "120K", "actual": ""},
        {"title": "CPI y/y", "country": "EUR", "date": past,
         "impact": "High", "forecast": "2.3%", "previous": "2.2%", "actual": "2.4%"},
        {"title": "Low one", "country": "GBP", "date": soon, "impact": "Low"},
    ]


def _offline_market(now: datetime) -> MarketSnapshot:
    """Deterministic snapshot with a couple of priced instruments + VIX."""
    snap = MarketSnapshot(as_of_utc=now)

    def d(v, disp):
        return Datum(v, SourceStamp("test", Reliability.PRIMARY, timestamp=now), disp, "→ 0,1%")

    snap.gauges["VIX"] = d(18.9, "18,9")
    snap.gauges["MOVE"] = Datum(None, na_stamp(), "N/A")
    snap.gauges["DXY"] = d(101.3, "101,3")
    snap.gauges["US10Y"] = d(4.38, "4,38")
    snap.gauges["XAU/USD"] = d(4088.0, "4 088")
    snap.gauges["GDP_NOWCAST"] = Datum(None, na_stamp(), "N/A")
    snap.gauges["SURPRISE_IDX"] = Datum(None, na_stamp(), "N/A")
    snap.prices["EUR/USD"] = d(1.1383, "1,1383")
    snap.prices["USD/JPY"] = d(161.73, "161,73")
    snap.atr["EUR/USD"] = 0.0070
    snap.atr["USD/JPY"] = 0.90
    return snap


def test_calendar_contract():
    now = datetime(2026, 6, 28, 15, 0, tzinfo=UTC)
    cal = build_calendar(now_utc=now, raw_data=_fake_raw(now))
    assert cal["metadata"]["total_high_impact"] == 2  # Low filtered out
    # events_engine keeps upcoming + past-within-72h
    assert len(cal["events_engine"]) == 2
    # events (UI) keeps only upcoming
    assert all(e["is_upcoming"] for e in cal["events"])


def test_session_and_fmt():
    assert get_session(datetime(2026, 6, 28, 10, tzinfo=UTC)) == "LONDON"
    assert fmt_until(0.0) == "PASSED"
    assert fmt_until(0.5) == "30m"


def test_overrides_are_proxy():
    now = datetime(2026, 6, 28, 15, 0, tzinfo=UTC)
    snap = build_market_snapshot(now_utc=now, overrides={"VIX": 18.9})
    assert snap.gauge("VIX").is_proxy  # user override -> [PROXY]


def test_no_setup_when_no_prices():
    now = datetime(2026, 6, 28, 15, 0, tzinfo=UTC)
    empty = MarketSnapshot(as_of_utc=now)
    empty.gauges["VIX"] = Datum(18.0, SourceStamp("t", Reliability.PRIMARY, timestamp=now), "18,0")
    cal = build_calendar(now_utc=now, raw_data=_fake_raw(now))
    ctx = build_context(now, empty, cal, overrides={}, mode="Normal")
    assert ctx.priority_assets == []
    assert ctx.no_setup_reason  # honest no-setup, not a forced trade


def test_max_3_and_render_no_placeholders():
    now = datetime(2026, 6, 28, 15, 0, tzinfo=UTC)
    cal = build_calendar(now_utc=now, raw_data=_fake_raw(now))
    snap = _offline_market(now)
    overrides = {
        "central_banks": {
            "FED": {"rate": "3,50–3,75%", "fact": "Maintenu (test).",
                    "bias": "Hawkish — test.", "next": "29/07/2026",
                    "pause": 70, "cut": 0, "hike": 30},
        },
        "cot": {"JPY": {"net": -129000, "delta": "−", "momentum": "↓↓"},
                "EUR": {"net": 48000, "delta": "+", "momentum": "↑"}},
    }
    ctx = build_context(now, snap, cal, overrides=overrides, mode="Aggressive")
    assert len(ctx.priority_assets) <= 3
    html = render_html(ctx)
    # Weekend (28/06/2026 is a Sunday) -> operational note must be present.
    assert "NOTE OPÉRATIONNELLE" in html
    assert validate_html(html) == [] or all(i.severity != "ERROR" for i in validate_html(html))
    issues = validate_context(ctx)
    assert all(i.severity != "ERROR" for i in issues), [i.message for i in issues]


def test_fr_num():
    assert fr_num(1234.5, 2, thousands=True) == "1\u00a0234,50"
    assert fr_num(18.9, 1) == "18,9"


def run_self_checks() -> dict:
    """Run all checks without pytest; returns a summary dict."""
    import traceback
    results = {}
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                results[name] = "PASS"
            except Exception as e:  # pragma: no cover
                results[name] = f"FAIL: {e}\n{traceback.format_exc()}"
    return results


if __name__ == "__main__":
    import json
    print(json.dumps(run_self_checks(), indent=2, ensure_ascii=False))
