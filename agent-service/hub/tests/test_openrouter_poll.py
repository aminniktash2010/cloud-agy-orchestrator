"""Tests for hub/openrouter_poll.py + the OpenRouter poller coroutine (Epic A, AD-19).

The recorded fixture below mirrors the REAL /api/v1/key response shape
verified 2026-10-21 against the stored key (free tier, no spend cap,
free_model_daily_requests {used: 0, limit: 50, remaining: 50}). Values are
representative; no live network is used in any test (AC-1).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub import openrouter_poll, quota_poller  # noqa: E402
from hub.types import HubConfig, QuotaProviderSettings, QuotaSettings  # noqa: E402

from conftest import make_hub, make_tier, run  # noqa: E402

BASE = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)

# Recorded 2026-10-21 — shape of the live key (free tier, no spend cap).
RECORDED_KEY_BODY = {
    "data": {
        "label": "recorded-fixture",
        "is_free_tier": True,
        "limit": None,
        "limit_remaining": None,
        "limit_reset": None,
        "budgets": [],
        "usage": 0,
        "usage_daily": 0,
        "usage_weekly": 0,
        "free_model_daily_requests": {"used": 0, "limit": 50, "remaining": 50},
        "rate_limit": {"requests": -1, "interval": "10s", "note": "deprecated"},
    }
}


def _cfg(**or_kw):
    quota = QuotaSettings(providers={
        "openrouter": QuotaProviderSettings(
            key_url="https://example.invalid/api/v1/key",
            free_rpd_cap=50,
            **or_kw,
        )
    })
    return HubConfig(version=1, tiers=[make_tier("openrouter", 4, ["m"])], quota=quota)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(openrouter_poll, "_utcnow", lambda: BASE)


@pytest.fixture(autouse=True)
def reset_backoff():
    quota_poller._openrouter_backoff_until = None
    quota_poller._openrouter_backoff_s = 600.0
    yield
    quota_poller._openrouter_backoff_until = None
    quota_poller._openrouter_backoff_s = 600.0


# ----------------------------------------------------------------------
# AD-19 derivation chain (pure function)
# ----------------------------------------------------------------------

def test_parse_spend_cap_path():
    body = {"data": {
        "limit": 10.0, "limit_remaining": 2.5,
        "limit_reset": BASE.timestamp() + 3600,
        "is_free_tier": False,
    }}
    r = openrouter_poll.parse_key_status(body, 50, 0, now=BASE)
    assert r.ok and r.remaining_pct == 25.0
    assert r.reset_in == "1h"
    assert "spend cap" in r.detail


def test_parse_free_tier_real_shape():
    """The exact shape the live key returns (AC-1)."""
    r = openrouter_poll.parse_key_status(RECORDED_KEY_BODY, 50, 0, now=BASE)
    assert r.ok and r.remaining_pct == 100.0
    assert r.reset_in is not None  # counts to UTC midnight
    assert "50" in r.detail and "daily" in r.detail


def test_parse_free_tier_partial_use():
    body = {"data": {
        "is_free_tier": True, "limit": None, "limit_remaining": None,
        "free_model_daily_requests": {"used": 38, "limit": 50, "remaining": 12},
    }}
    r = openrouter_poll.parse_key_status(body, 50, 0, now=BASE)
    assert r.remaining_pct == 24.0


def test_parse_fallback_local_count():
    """No provider counter -> AD-19 local estimate (2b)."""
    body = {"data": {"is_free_tier": True, "limit": None}}
    r = openrouter_poll.parse_key_status(body, 50, 10, now=BASE)
    assert r.remaining_pct == 80.0
    assert "local count" in r.detail


def test_parse_null_plus_honest_detail():
    """No cap, no counter, not free tier -> null, never a guess (AD-19.3)."""
    body = {"data": {"is_free_tier": False, "limit": None}}
    r = openrouter_poll.parse_key_status(body, 50, 0, now=BASE)
    assert r.remaining_pct is None
    assert r.detail  # explains why, doesn't invent


def test_parse_malformed_body_never_raises():
    for bad in ({}, {"data": None}, {"data": "x"}, [], "x"):
        r = openrouter_poll.parse_key_status(bad, 50, 0, now=BASE)
        assert r.ok and r.remaining_pct is None


# ----------------------------------------------------------------------
# poller coroutine
# ----------------------------------------------------------------------

def _ok_fetch(body):
    async def fake(api_key, key_url, timeout_s=20.0):
        assert key_url == "https://example.invalid/api/v1/key"
        assert timeout_s == 20.0
        return body
    return fake


def test_poller_success_writes_store(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(openrouter_poll, "fetch_key_status",
                        _ok_fetch(RECORDED_KEY_BODY))
    hub = make_hub(_cfg())
    run(quota_poller._openrouter_poll_once(hub))
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {})
    tier = snap["tiers"]["openrouter"]
    assert tier["quota_source"] == "live"
    assert tier["remaining_pct"] == 100.0
    assert tier["quota_updated_at"] is not None
    assert snap["poller"]["providers"]["openrouter"]["healthy"] is True
    assert snap["poller"]["healthy"] is True


def test_poller_429_backoff_and_recovery(monkeypatch):
    calls = []

    async def fake_429(api_key, key_url, timeout_s=20.0):
        calls.append(1)
        req = httpx.Request("GET", key_url)
        raise httpx.HTTPStatusError(
            "429", request=req, response=httpx.Response(429, request=req))

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(openrouter_poll, "fetch_key_status", fake_429)
    hub = make_hub(_cfg())
    run(quota_poller._openrouter_poll_once(hub))
    assert len(calls) == 1
    assert quota_poller._openrouter_backoff_until is not None
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {})
    assert snap["poller"]["providers"]["openrouter"]["healthy"] is False
    assert snap["tiers"]["openrouter"]["quota_source"] == "estimated"

    # Immediate second cycle: skipped (no fetch), keeps last-known state.
    run(quota_poller._openrouter_poll_once(hub))
    assert len(calls) == 1

    # Recovery: success clears the backoff.
    monkeypatch.setattr(openrouter_poll, "fetch_key_status",
                        _ok_fetch(RECORDED_KEY_BODY))
    quota_poller._openrouter_backoff_until = BASE - timedelta(seconds=1)
    run(quota_poller._openrouter_poll_once(hub))
    assert quota_poller._openrouter_backoff_until is None
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {})
    assert snap["tiers"]["openrouter"]["quota_source"] == "live"


def test_poller_exception_never_propagates(monkeypatch):
    """NFR-2: a crashing poll can't kill the supervisor task."""

    async def boom(api_key, key_url, timeout_s=20.0):
        raise RuntimeError("boom")

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(openrouter_poll, "fetch_key_status", boom)
    hub = make_hub(_cfg())
    run(quota_poller._openrouter_poll_once(hub))  # must not raise
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {})
    assert snap["poller"]["providers"]["openrouter"]["healthy"] is False
    assert snap["tiers"]["openrouter"]["quota_source"] == "estimated"


def test_poller_disabled_in_config(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    hub = make_hub(_cfg(quota_enabled=False))
    run(quota_poller._openrouter_poll_once(hub))
    snap = hub.live_quota.snapshot(_cfg(quota_enabled=False), BASE, 1800, {})
    assert snap["poller"]["providers"]["openrouter"]["healthy"] is False


def test_poller_no_key_is_honest_noop(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    hub = make_hub(_cfg())
    run(quota_poller._openrouter_poll_once(hub))  # must not raise
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {})
    assert snap["poller"]["providers"]["openrouter"]["healthy"] is False
    assert snap["tiers"]["openrouter"]["quota_source"] == "estimated"


# ----------------------------------------------------------------------
# snapshot contract + staleness (AC-3)
# ----------------------------------------------------------------------

def test_snapshot_stale_downgrade_frozen_clock(monkeypatch):
    """A value older than staleness_s is never labeled live (NFR-3)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(openrouter_poll, "fetch_key_status",
                        _ok_fetch(RECORDED_KEY_BODY))
    hub = make_hub(_cfg())
    run(quota_poller._openrouter_poll_once(hub))
    cfg = _cfg()
    fresh = hub.live_quota.snapshot(cfg, BASE, 1800, {})
    assert fresh["tiers"]["openrouter"]["quota_source"] == "live"
    stale = hub.live_quota.snapshot(
        cfg, BASE + timedelta(seconds=1801), 1800, {})
    assert stale["tiers"]["openrouter"]["quota_source"] == "estimated"
    assert stale["tiers"]["openrouter"]["remaining_pct"] is None


def test_snapshot_contract_shape():
    """Every tier carries a well-formed live_quota block (AC-3)."""
    cfg = _cfg()
    hub = make_hub(cfg)
    snap = hub.live_quota.snapshot(cfg, BASE, 1800, {})
    lq = snap["tiers"]["openrouter"]
    assert lq["quota_source"] in ("live", "headers", "measured", "estimated")
    assert set(lq) >= {"quota_source", "quota_updated_at", "remaining_pct",
                       "reset_in", "detail"}


# ----------------------------------------------------------------------
# secret-freedom (AC-10 extension)
# ----------------------------------------------------------------------

def test_no_secret_leak_openrouter(monkeypatch):
    """The bearer key must not appear in the status payload."""
    fake_key = "sk-or-THIS-IS-A-TEST-KEY-0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(openrouter_poll, "fetch_key_status",
                        _ok_fetch(RECORDED_KEY_BODY))
    hub = make_hub(_cfg())
    run(quota_poller._openrouter_poll_once(hub))
    assert fake_key not in json.dumps(hub.status())
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {})
    assert fake_key not in json.dumps(snap)
