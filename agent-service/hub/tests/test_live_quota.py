"""Unit tests for hub/live_quota.py — LiveQuotaStore (live-quota Epic C)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.live_quota import (  # noqa: E402
    LiveQuotaStore,
    format_reset_in,
)
from hub.types import (  # noqa: E402
    QuotaProviderSettings,
    QuotaSettings,
    TierConfig,
    HubConfig,
)

from conftest import run  # noqa: E402

BASE = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)

AGY_FAMILIES_CFG = {
    "argon": {"label": "Gemini Argon-class", "match": ["argon"]},
    "gemini": {"label": "Gemini Models", "match": ["gemini"]},
    "claude_gpt": {"label": "Claude and GPT models", "match": ["claude", "gpt"]},
}


def _cfg_with_agy_quota(**kw):
    quota = QuotaSettings(
        providers={"agy": QuotaProviderSettings(families=dict(AGY_FAMILIES_CFG), **kw)}
    )
    return HubConfig(
        version=1,
        tiers=[TierConfig(id="agy", order=0, models=["gemini-x", "claude-y"])],
        quota=quota,
    )


def _probe_families():
    return [
        {
            "id": "gemini", "label": "Gemini Models", "models": ["gemini-x"],
            "remaining_pct": 23.0,
            "resets_at_epoch": (BASE + timedelta(days=5, hours=6)).timestamp(),
            "note": "",
        },
        {
            "id": "claude_gpt", "label": "Claude and GPT models", "models": ["claude-y"],
            "remaining_pct": 0.0,
            "resets_at_epoch": (BASE + timedelta(days=5, hours=12)).timestamp(),
            "note": "weekly limit exhausted",
        },
    ]


def _seed(store, at=BASE):
    run(store.set_probe_result(
        _probe_families(), {"gemini-x": "gemini", "claude-y": "claude_gpt"}, [],
        at, detail="quota cache quota_summary.pb",
    ))
    run(store.set_poller_health("agy_probe", True, at))


# ---------------------------------------------------------------------------
def test_format_reset_in():
    assert format_reset_in(5 * 86400 + 6 * 3600) == "5d 6h"
    assert format_reset_in(86400) == "1d"
    assert format_reset_in(42 * 60) == "42m"
    assert format_reset_in(65 * 60) == "1h 5m"
    assert format_reset_in(3600) == "1h"
    assert format_reset_in(30) == "<1m"
    assert format_reset_in(-5) == "<1m"


def test_staleness_downgrade_property_frozen_clock():
    """AC-3/NFR-3: older than the threshold is NEVER labeled live (property)."""
    store = LiveQuotaStore()
    _seed(store)
    cfg = _cfg_with_agy_quota()
    for age_s in (0, 60, 1799, 1800, 1801, 3600, 86400, 7 * 86400):
        now = BASE + timedelta(seconds=age_s)
        snap = store.snapshot(cfg, now, 1800, {"gemini": "closed"})
        src = snap["tiers"]["agy"]["quota_source"]
        if age_s <= 1800:
            assert src == "live", f"age {age_s}s should still be live"
        else:
            assert src == "estimated", f"age {age_s}s must downgrade, got {src}"
            assert snap["tiers"]["agy"]["quota_source"] not in (
                "live", "headers", "measured",
            )


def test_fresh_probe_block_shape():
    store = LiveQuotaStore()
    _seed(store)
    cfg = _cfg_with_agy_quota()
    snap = store.snapshot(cfg, BASE, 1800, {"gemini": "closed", "claude_gpt": "open"})
    agy = snap["tiers"]["agy"]
    assert agy["quota_source"] == "live"
    assert agy["quota_updated_at"] == BASE.isoformat()
    # binding constraint = min across families
    assert agy["remaining_pct"] == 0.0
    assert agy["reset_in"] == "5d 12h"
    assert "Gemini Models 23%" in agy["detail"]
    assert "Claude and GPT models exhausted" in agy["detail"]
    fams = {f["id"]: f for f in agy["families"]}
    assert set(fams) == {"gemini", "claude_gpt"}
    assert fams["gemini"]["circuit"] == "closed"
    assert fams["claude_gpt"]["circuit"] == "open"
    assert fams["gemini"]["models"] == ["gemini-x"]
    assert fams["gemini"]["quota_source"] == "live"
    assert fams["gemini"]["reset_in"] == "5d 6h"
    assert agy["unmapped_buckets"] == []


def test_unmapped_buckets_visible_not_dropped():
    store = LiveQuotaStore()
    unmapped = [{"name": "mystery-bucket", "remaining_pct": 55.0,
                 "resets_at_epoch": (BASE + timedelta(hours=2)).timestamp()}]
    run(store.set_probe_result(_probe_families(), {}, unmapped, BASE, detail="ok"))
    run(store.set_poller_health("agy_probe", True, BASE))
    cfg = _cfg_with_agy_quota()
    snap = store.snapshot(cfg, BASE, 1800, {})
    ub = snap["tiers"]["agy"]["unmapped_buckets"]
    assert len(ub) == 1
    assert ub[0]["name"] == "mystery-bucket"
    assert ub[0]["remaining_pct"] == 55.0
    assert ub[0]["reset_in"] == "2h"


def test_cold_start_is_estimated_never_blank():
    """NFR-4: empty cache -> estimated with config-seed families, never fake-live."""
    store = LiveQuotaStore()
    cfg = _cfg_with_agy_quota()
    snap = store.snapshot(cfg, BASE, 1800, {"gemini": "closed"})
    agy = snap["tiers"]["agy"]
    assert agy["quota_source"] == "estimated"
    assert agy["quota_updated_at"] is None
    assert agy["remaining_pct"] is None
    assert "not run yet" in agy["detail"]
    assert {f["id"] for f in agy["families"]} == set(AGY_FAMILIES_CFG)
    assert all(f["quota_source"] == "estimated" for f in agy["families"])
    # non-agy tiers are estimated with honest details
    assert snap["tiers"].get("aistudio", {}).get("quota_source", "estimated") == "estimated"


def test_degraded_probe_keeps_age_note():
    store = LiveQuotaStore()
    _seed(store, at=BASE)
    run(store.set_poller_health("agy_probe", False, BASE, detail="probe timed out"))
    cfg = _cfg_with_agy_quota()
    now = BASE + timedelta(seconds=2400)  # 40m later, past the 30m threshold
    snap = store.snapshot(cfg, now, 1800, {})
    agy = snap["tiers"]["agy"]
    assert agy["quota_source"] == "estimated"
    assert "probe timed out" in agy["detail"]
    assert "40m ago" in agy["detail"]


def test_poller_block_shape():
    store = LiveQuotaStore()
    cfg = _cfg_with_agy_quota()
    snap = store.snapshot(cfg, BASE, 1800, {})
    assert snap["poller"]["healthy"] is True  # nothing registered -> nothing failing
    assert snap["poller"]["last_run"] is None
    run(store.set_poller_health("agy_probe", False, BASE, detail="x"))
    snap = store.snapshot(cfg, BASE, 1800, {})
    assert snap["poller"]["healthy"] is False
    assert snap["poller"]["providers"]["agy_probe"]["healthy"] is False
    assert snap["poller"]["providers"]["agy_probe"]["last_run"] == BASE.isoformat()
    assert snap["poller"]["last_run"] == BASE.isoformat()


def test_non_agy_tiers_estimated_in_epic_c():
    cfg = HubConfig(
        version=1,
        tiers=[
            TierConfig(id="agy", order=0, models=["m"]),
            TierConfig(id="groq", order=2, models=["g"]),
            TierConfig(id="nvidia", order=3, models=["n"]),
        ],
        quota=QuotaSettings(),
    )
    store = LiveQuotaStore()
    snap = store.snapshot(cfg, BASE, 1800, {})
    for tid in ("groq", "nvidia"):
        blk = snap["tiers"][tid]
        assert blk["quota_source"] == "estimated"
        assert blk["remaining_pct"] is None
        assert blk["quota_updated_at"] is None
        assert "no public quota API" in blk["detail"]


def test_other_family_row_visible():
    """AD-15: taxonomy models no family claimed still get a families[] row."""
    store = LiveQuotaStore()
    run(store.set_probe_result(
        _probe_families(),
        {"gemini-x": "gemini", "mystery-9": "other"},
        [], BASE, detail="ok",
    ))
    run(store.set_poller_health("agy_probe", True, BASE))
    cfg = _cfg_with_agy_quota()
    snap = store.snapshot(cfg, BASE, 1800, {"other": "closed"})
    rows = {f["id"]: f for f in snap["tiers"]["agy"]["families"]}
    assert "other" in rows
    assert rows["other"]["models"] == ["mystery-9"]
    assert rows["other"]["label"] == "Other models"
    assert rows["other"]["quota_source"] == "estimated"
