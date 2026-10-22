"""Unit tests: automatic retired-model hygiene (H1-S6, PRD AC-1…AC-6)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub import quota_poller  # noqa: E402
from hub.signals import is_retired_model  # noqa: E402
from hub.types import (  # noqa: E402
    AdapterResponse, AttemptOutcome, HubConfig, HubRequest, ModelCarrierMap,
    ModelCatalogConfig,
)

from conftest import FakeAdapter, make_hub, make_tier, run  # noqa: E402


GOOGLE_RETIRED_BODY = (
    '{"error": {"code": 404, "message": "This model models/gemini-2.5-flash '
    'is no longer available to new users. Please update your code to use '
    'models/gemini-3.8-flash.", "status": "NOT_FOUND"}}'
)

OPENAI_RETIRED_BODY = (
    '{"error": {"code": "model_not_found", "message": "The model `x` does not '
    'exist", "type": "not_found"}}'
)


def req(**kw) -> HubRequest:
    kw.setdefault("prompt", "do the thing")
    return HubRequest(**kw)


def retired_envelope(body: str) -> dict:
    return {"status_code": 404, "body": body}


async def collect_events(hub, request):
    events = []
    async for ev in hub.execute_stream(request):
        events.append(ev)
    return events


# --- AC-1: reactive quarantine, unpinned ---------------------------------------

def test_ac1_reactive_quarantine_zero_further_calls(registry_with_fake):
    """404 + retired-shape body -> id quarantined; next requests skip it with
    zero provider calls; sibling models still serve."""
    calls = []

    orig_run = FakeAdapter.run

    async def spy(self, prompt, model, tier, **kw):
        calls.append(model)
        return await orig_run(self, prompt, model, tier, **kw)

    FakeAdapter.run = spy
    try:
        FakeAdapter.SCRIPT = {
            "dead-id": [(AttemptOutcome.TRANSIENT, retired_envelope(GOOGLE_RETIRED_BODY))],
            "live-id": [(AttemptOutcome.SUCCESS,
                         AdapterResponse(text="alive", model="live-id"))],
        }
        cfg = HubConfig(version=1, tiers=[
            make_tier("t1", 0, ["dead-id"]),
            make_tier("t2", 1, ["live-id"]),
        ])
        hub = make_hub(cfg)

        r1 = run(hub.execute(req()))
        assert r1.status == "success"
        assert r1.model == "live-id"  # dead-id 404'd, advanced within tier
        assert ("t1", "dead-id") in hub._retired_models
        assert hub._retired_models[("t1", "dead-id")]["source"] == "reactive"
        first_calls = list(calls)

        # Next 3 identical requests: dead-id skipped, zero calls for it.
        for _ in range(3):
            calls.clear()
            # Reset the script so live-id keeps serving.
            FakeAdapter.SCRIPT["live-id"] = [
                (AttemptOutcome.SUCCESS, AdapterResponse(text="alive", model="live-id"))]
            r = run(hub.execute(req()))
            assert r.status == "success"
            assert r.model == "live-id"
            assert "dead-id" not in calls
            skips = [a for a in r.failover_attempts
                     if a.outcome == "skipped" and a.reason == "skip: model_quarantined"]
            assert any(a.model == "dead-id" for a in skips), \
                "expected skip: model_quarantined for dead-id"
        assert first_calls.count("dead-id") == 1  # exactly one failed call, ever
    finally:
        FakeAdapter.run = orig_run


def test_ac1_stream_quarantine(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "dead-id": [(AttemptOutcome.TRANSIENT, retired_envelope(OPENAI_RETIRED_BODY))],
        "live-id": [(AttemptOutcome.SUCCESS,
                     AdapterResponse(text="alive", model="live-id"))],
    }
    cfg = HubConfig(version=1, tiers=[
        make_tier("t1", 0, ["dead-id"]),
        make_tier("t2", 1, ["live-id"]),
    ])
    hub = make_hub(cfg)
    events = run(collect_events(hub, req()))
    done = next(e for e in events if e.kind == "done")
    assert done.payload["status"] == "success"
    assert ("t1", "dead-id") in hub._retired_models
    # Second run: quarantined skip recorded.
    FakeAdapter.SCRIPT["live-id"] = [
        (AttemptOutcome.SUCCESS, AdapterResponse(text="alive", model="live-id"))]
    events = run(collect_events(hub, req()))
    done = next(e for e in events if e.kind == "done")
    attempts = done.payload["failover_attempts"]
    assert any(a["outcome"] == "skipped" and a["reason"] == "skip: model_quarantined"
               and a["model"] == "dead-id" for a in attempts)


# --- AC-2: status surfacing + auto-clear ---------------------------------------

def test_ac2_status_lists_and_clears(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "dead-id": [(AttemptOutcome.TRANSIENT, retired_envelope(GOOGLE_RETIRED_BODY))],
        "live-id": [(AttemptOutcome.SUCCESS,
                     AdapterResponse(text="alive", model="live-id"))],
    }
    cfg = HubConfig(version=1, tiers=[make_tier("t1", 0, ["dead-id", "live-id"])])
    hub = make_hub(cfg)
    run(hub.execute(req()))

    section = hub.status()["retired_models"]
    assert len(section) == 1
    e = section[0]
    assert e["tier"] == "t1" and e["model_id"] == "dead-id"
    assert e["source"] == "reactive"
    assert e["hits"] == 1
    assert e["first_seen"] and e["last_seen"]
    assert "no longer available" in e["signal_excerpt"]
    assert len(e["signal_excerpt"]) <= 200

    # Operator removes the id from the tier config -> section clears + pruned.
    cfg2 = HubConfig(version=1, tiers=[make_tier("t1", 0, ["live-id"])])
    hub2 = make_hub(cfg2)
    hub2._retired_models = hub._retired_models  # same store, new config
    assert hub2.status()["retired_models"] == []
    assert ("t1", "dead-id") not in hub2._retired_models


def test_ac2_pinned_id_referenced_via_catalog(registry_with_fake):
    """A pinned carrier id lives in the catalog, not the tier list — the
    status must still consider it referenced (no premature auto-clear)."""
    catalog = {"m": ModelCarrierMap(aliases=[], carriers={"t1": "mapped-dead"})}
    cfg = HubConfig(version=1, tiers=[make_tier("t1", 0, ["x"])])
    cfg.model_catalog = ModelCatalogConfig(models=catalog)
    hub = make_hub(cfg)
    hub._quarantine("t1", "mapped-dead", "404 model_not_found", source="reactive")
    section = hub.status()["retired_models"]
    assert any(r["model_id"] == "mapped-dead" for r in section)
    # Change the mapping -> auto-clear.
    cfg.model_catalog.models["m"].carriers["t1"] = "mapped-fixed"
    assert hub.status()["retired_models"] == []


# --- AC-3: TTL re-probe --------------------------------------------------------

def test_ac3_ttl_expiry_reprobes_and_clears_on_success(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "dead-id": [(AttemptOutcome.TRANSIENT, retired_envelope(GOOGLE_RETIRED_BODY))],
        "live-id": [(AttemptOutcome.SUCCESS,
                     AdapterResponse(text="alive", model="live-id"))],
    }
    cfg = HubConfig(version=1, tiers=[make_tier("t1", 0, ["dead-id", "live-id"])])
    hub = make_hub(cfg)
    run(hub.execute(req()))
    assert ("t1", "dead-id") in hub._retired_models

    # Force TTL expiry: backdate last_seen.
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    hub._retired_models[("t1", "dead-id")]["last_seen"] = old
    assert not hub._is_quarantined("t1", "dead-id")

    # Re-probe succeeds -> entry cleared immediately.
    FakeAdapter.SCRIPT = {
        "dead-id": [(AttemptOutcome.SUCCESS,
                     AdapterResponse(text="back from the dead", model="dead-id"))],
        "live-id": [(AttemptOutcome.SUCCESS,
                     AdapterResponse(text="alive", model="live-id"))],
    }
    r = run(hub.execute(req()))
    assert r.status == "success" and r.model == "dead-id"
    assert ("t1", "dead-id") not in hub._retired_models


def test_ac3_ttl_expiry_repeat_404_refreshes(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "dead-id": [(AttemptOutcome.TRANSIENT, retired_envelope(GOOGLE_RETIRED_BODY))],
        "live-id": [(AttemptOutcome.SUCCESS,
                     AdapterResponse(text="alive", model="live-id"))],
    }
    cfg = HubConfig(version=1, tiers=[make_tier("t1", 0, ["dead-id", "live-id"])])
    hub = make_hub(cfg)
    run(hub.execute(req()))
    old_last = hub._retired_models[("t1", "dead-id")]["last_seen"]
    hub._retired_models[("t1", "dead-id")]["last_seen"] = \
        (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()

    FakeAdapter.SCRIPT["dead-id"] = [
        (AttemptOutcome.TRANSIENT, retired_envelope(GOOGLE_RETIRED_BODY))]
    FakeAdapter.SCRIPT["live-id"] = [
        (AttemptOutcome.SUCCESS, AdapterResponse(text="alive", model="live-id"))]
    run(hub.execute(req()))
    e = hub._retired_models[("t1", "dead-id")]
    assert e["hits"] == 2
    assert e["last_seen"] > old_last  # refreshed for another TTL window


# --- AC-4: proactive poller ----------------------------------------------------

def _tier_cfg(*tiers):
    return HubConfig(version=1, tiers=list(tiers))


def test_ac4_proactive_drift_quarantines(monkeypatch, registry_with_fake):
    """Catalog missing a configured id -> proactive entry; fetch failure -> nothing."""
    async def fake_fetch(tier):
        if tier.id == "t1":
            return {"live-id"}  # dead-id absent -> drift
        return None

    monkeypatch.setattr(quota_poller, "_fetch_provider_catalog", fake_fetch)
    cfg = _tier_cfg(make_tier("t1", 0, ["dead-id", "live-id"]))
    hub = make_hub(cfg)
    run(quota_poller._hygiene_check_once(hub))
    e = hub._retired_models.get(("t1", "dead-id"))
    assert e is not None and e["source"] == "proactive"
    assert ("t1", "live-id") not in hub._retired_models


def test_ac4_fetch_failure_quarantines_nothing(monkeypatch, registry_with_fake):
    async def fake_fetch(tier):
        return None  # network/auth/shape failure

    monkeypatch.setattr(quota_poller, "_fetch_provider_catalog", fake_fetch)
    cfg = _tier_cfg(make_tier("t1", 0, ["dead-id", "live-id"]))
    hub = make_hub(cfg)
    run(quota_poller._hygiene_check_once(hub))
    assert hub._retired_models == {}


def test_ac4_proactive_never_downgrades_reactive(monkeypatch, registry_with_fake):
    """Reactive entry exists; poller diff shows the id PRESENT -> untouched."""
    async def fake_fetch(tier):
        return {"dead-id", "live-id"}  # catalog lists it (presence != servability)

    monkeypatch.setattr(quota_poller, "_fetch_provider_catalog", fake_fetch)
    cfg = _tier_cfg(make_tier("t1", 0, ["dead-id", "live-id"]))
    hub = make_hub(cfg)
    hub._quarantine("t1", "dead-id", "404 no longer available", source="reactive")
    before = dict(hub._retired_models[("t1", "dead-id")])
    run(quota_poller._hygiene_check_once(hub))
    after = hub._retired_models[("t1", "dead-id")]
    assert after["source"] == "reactive"
    assert after["hits"] == before["hits"]
    assert after["last_seen"] == before["last_seen"]


def test_ac4_proactive_reactive_upgrade(monkeypatch, registry_with_fake):
    """Proactive entry + later real-call 404 -> source upgrades to reactive."""
    async def fake_fetch(tier):
        return {"live-id"}

    monkeypatch.setattr(quota_poller, "_fetch_provider_catalog", fake_fetch)
    cfg = _tier_cfg(make_tier("t1", 0, ["dead-id", "live-id"]))
    hub = make_hub(cfg)
    run(quota_poller._hygiene_check_once(hub))
    assert hub._retired_models[("t1", "dead-id")]["source"] == "proactive"

    FakeAdapter.SCRIPT = {
        "dead-id": [(AttemptOutcome.TRANSIENT, retired_envelope(GOOGLE_RETIRED_BODY))],
        "live-id": [(AttemptOutcome.SUCCESS,
                     AdapterResponse(text="alive", model="live-id"))],
    }
    # TTL not expired but the id IS quarantined -> force a re-probe by expiry.
    hub._retired_models[("t1", "dead-id")]["last_seen"] = \
        (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    run(hub.execute(req()))
    assert hub._retired_models[("t1", "dead-id")]["source"] == "reactive"


# --- AC-5: catalog-present-but-dead (the key insight) ---------------------------

def test_ac5_google_retired_body_quarantines(registry_with_fake):
    """Exact live Google shape: 404 + 'no longer available to new users'."""
    FakeAdapter.SCRIPT = {
        "gemini-2.5-flash": [(AttemptOutcome.TRANSIENT,
                              retired_envelope(GOOGLE_RETIRED_BODY))],
        "gemini-3.8-flash": [(AttemptOutcome.SUCCESS,
                              AdapterResponse(text="ok", model="gemini-3.8-flash"))],
    }
    cfg = _tier_cfg(
        make_tier("aistudio", 0, ["gemini-2.5-flash"], provider="google-ai-studio"),
        make_tier("fallback", 1, ["gemini-3.8-flash"], provider="google-ai-studio"),
    )
    hub = make_hub(cfg)
    r = run(hub.execute(req()))
    assert r.status == "success" and r.model == "gemini-3.8-flash"
    assert ("aistudio", "gemini-2.5-flash") in hub._retired_models


def test_ac5_openai_compat_retired_body_quarantines(registry_with_fake):
    """OpenAI-compat shape: body carries model_not_found, no numeric 404."""
    FakeAdapter.SCRIPT = {
        "x": [(AttemptOutcome.TRANSIENT, retired_envelope(OPENAI_RETIRED_BODY))],
        "y": [(AttemptOutcome.SUCCESS, AdapterResponse(text="ok", model="y"))],
    }
    cfg = _tier_cfg(make_tier("t1", 0, ["x", "y"]))
    hub = make_hub(cfg)
    run(hub.execute(req()))
    assert ("t1", "x") in hub._retired_models


# --- AC-6: non-retired signals never quarantine ---------------------------------

@pytest.mark.parametrize("status_code,body", [
    (404, "<html><body>502 Bad Gateway</body></html>"),  # bare 404, no markers
    (404, ""),                                            # empty body
    (400, '{"error": {"message": "model_not_found"}}'),   # 400: bad request, not death
    (401, '{"error": {"message": "model_not_found"}}'),   # 401: auth fault
    (429, '{"error": {"message": "model_not_found"}}'),   # 429: rate pressure
    (503, '{"error": {"message": "model_not_found"}}'),   # 503: transient
    (None, '{"error": {"message": "model_not_found"}}'),  # unknown status
])
def test_ac6_non_retired_never_quarantine(registry_with_fake, status_code, body):
    FakeAdapter.SCRIPT = {
        "m": [(AttemptOutcome.TRANSIENT,
               {"status_code": status_code, "body": body}
               if status_code is not None else body)],
        "n": [(AttemptOutcome.SUCCESS, AdapterResponse(text="ok", model="n"))],
    }
    cfg = _tier_cfg(make_tier("t1", 0, ["m", "n"]))
    hub = make_hub(cfg)
    run(hub.execute(req()))
    assert ("t1", "m") not in hub._retired_models


def test_ac6_unknown_model_quarantines_without_http_signal(registry_with_fake):
    """UNKNOWN_MODEL (CLI markers) quarantines even with a bare-string reason."""
    FakeAdapter.SCRIPT = {
        "m": [(AttemptOutcome.UNKNOWN_MODEL, "unknown model 'm'")],
        "n": [(AttemptOutcome.SUCCESS, AdapterResponse(text="ok", model="n"))],
    }
    cfg = _tier_cfg(make_tier("t1", 0, ["m", "n"]))
    hub = make_hub(cfg)
    r = run(hub.execute(req()))
    assert r.status == "success" and r.model == "n"
    assert ("t1", "m") in hub._retired_models


def test_ac6_free_only_policy_unknown_model_never_quarantines(registry_with_fake):
    """A free_only policy rejection is a spend guard, not a dead model —
    UNKNOWN_MODEL carrying the free_only reason must not quarantine."""
    FakeAdapter.SCRIPT = {
        "paid-id": [(AttemptOutcome.UNKNOWN_MODEL,
                     "free_only policy: model 'paid-id' does not end with ':free'")],
        "n": [(AttemptOutcome.SUCCESS, AdapterResponse(text="ok", model="n"))],
    }
    cfg = _tier_cfg(make_tier("t1", 0, ["paid-id"]),
                    make_tier("t2", 1, ["n"]))
    hub = make_hub(cfg)
    r = run(hub.execute(req()))
    assert r.status == "success" and r.model == "n"
    assert ("t1", "paid-id") not in hub._retired_models


# --- pinned-path migration ------------------------------------------------------

def test_pinned_retired_id_quarantines_and_skips_next_time(registry_with_fake):
    catalog = {"m": ModelCarrierMap(aliases=[], carriers={"t1": "mapped-dead",
                                                          "t2": "good-id"})}
    cfg = HubConfig(version=1, tiers=[
        make_tier("t1", 0, ["x"]),
        make_tier("t2", 1, ["good-id"]),
    ])
    cfg.model_catalog = ModelCatalogConfig(models=catalog)
    FakeAdapter.SCRIPT = {
        "mapped-dead": [(AttemptOutcome.TRANSIENT, retired_envelope(OPENAI_RETIRED_BODY))],
        "good-id": [(AttemptOutcome.SUCCESS,
                     AdapterResponse(text="ok", model="good-id"))],
    }
    hub = make_hub(cfg)
    r = run(hub.execute(req(model="m")))
    assert r.status == "success" and r.model == "good-id"
    # Both the catalog stale-mapping AND the physical-id quarantine fire.
    assert ("t1", "mapped-dead") in hub._retired_models
    assert any(a.outcome == "stale_mapping" for a in r.failover_attempts)

    # Next pinned request: carrier skipped with zero provider calls.
    calls = []
    orig_run = FakeAdapter.run

    async def spy(self, prompt, model, tier, **kw):
        calls.append(model)
        return await orig_run(self, prompt, model, tier, **kw)

    FakeAdapter.run = spy
    try:
        FakeAdapter.SCRIPT["good-id"] = [
            (AttemptOutcome.SUCCESS, AdapterResponse(text="ok", model="good-id"))]
        r2 = run(hub.execute(req(model="m")))
        assert r2.status == "success"
        assert "mapped-dead" not in calls
        assert any(a.outcome == "skipped" and a.reason == "skip: model_quarantined"
                   and a.model == "mapped-dead" for a in r2.failover_attempts)
    finally:
        FakeAdapter.run = orig_run


# --- breaker interplay ----------------------------------------------------------

def test_all_quarantined_tier_never_trips_breaker(registry_with_fake):
    """A tier whose ids are all quarantined degrades to visible skips; the
    tier breaker records no exhaustion (AD-42)."""
    cfg = _tier_cfg(make_tier("t1", 0, ["a", "b"]))
    hub = make_hub(cfg)
    hub._quarantine("t1", "a", "404 not_found", source="reactive")
    hub._quarantine("t1", "b", "404 not_found", source="reactive")
    before = hub._breaker.snapshot().get("t1", {}).get("consecutive_exhaustions", 0)
    r = run(hub.execute(req()))
    assert r.status == "ladder_empty"
    assert all(a.outcome == "skipped" and a.reason == "skip: model_quarantined"
               for a in r.failover_attempts)
    after = hub._breaker.snapshot().get("t1", {}).get("consecutive_exhaustions", 0)
    assert after == before  # quarantine skips never touch the breaker (AD-42)


def test_discovering_attempt_counts_normally(registry_with_fake):
    """The attempt that discovers the retirement is a real call: it counts
    toward the tier breaker like any other attempt (AD-42)."""
    FakeAdapter.SCRIPT = {
        "dead-id": [(AttemptOutcome.TRANSIENT, retired_envelope(GOOGLE_RETIRED_BODY))],
    }
    cfg = _tier_cfg(make_tier("t1", 0, ["dead-id"]))
    hub = make_hub(cfg)
    before = hub._breaker.snapshot().get("t1", {}).get("consecutive_exhaustions", 0)
    run(hub.execute(req()))
    after = hub._breaker.snapshot().get("t1", {}).get("consecutive_exhaustions", 0)
    # The real failed call noted one exhaustion (breaker math unchanged).
    assert after == before + 1
    assert ("t1", "dead-id") in hub._retired_models


# --- signal unit table (router-level integration of is_retired_model) -----------

@pytest.mark.parametrize("status_code,body,expected", [
    (404, GOOGLE_RETIRED_BODY, True),
    (404, OPENAI_RETIRED_BODY, True),
    (404, "NOT_FOUND: no such model", True),
    (404, "Model `x` is not found for API version v1beta", True),
    (404, "Model `x` is not supported for generateContent", True),
    (404, "<html><body>Error 404</body></html>", False),  # bare 404, no model marker
    (404, "", False),
    (400, OPENAI_RETIRED_BODY, False),
    (401, OPENAI_RETIRED_BODY, False),
    (429, OPENAI_RETIRED_BODY, False),
    (500, OPENAI_RETIRED_BODY, False),
    (503, OPENAI_RETIRED_BODY, False),
    (None, OPENAI_RETIRED_BODY, False),
    (200, OPENAI_RETIRED_BODY, False),
])
def test_is_retired_model_table(status_code, body, expected):
    assert is_retired_model(status_code, body) is expected
    # Case-insensitivity is structural: uppercased bodies behave identically.
    assert is_retired_model(status_code, body.upper()) is expected
