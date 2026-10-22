"""Unit tests: model-pinned failover (P1-S6, PRD AC-1…AC-8)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.mcp_server import _call_hub_ask  # noqa: E402
from hub.types import (  # noqa: E402
    AdapterResponse, AttemptOutcome, CapabilityMode, HubConfig, HubRequest,
    ModelCarrierMap, ModelCatalogConfig,
)

from conftest import FakeAdapter, make_hub, make_tier, run  # noqa: E402


def opus_catalog() -> dict:
    return {
        "claude-opus-5.5": ModelCarrierMap(
            aliases=["claude opus 5.5", "opus 5.5", "anthropic/claude-opus-5.5"],
            carriers={"openrouter": "anthropic/claude-opus-5.5",
                      "requesty": "anthropic/claude-opus-5-5"}),
    }


def cfg_with_catalog(*tiers, catalog=None) -> HubConfig:
    cfg = HubConfig(version=1, tiers=list(tiers))
    if catalog is not None:
        cfg.model_catalog = ModelCatalogConfig(models=catalog)
    return cfg


def req(**kw) -> HubRequest:
    kw.setdefault("prompt", "do the thing")
    return HubRequest(**kw)


async def collect_events(hub, request):
    events = []
    async for ev in hub.execute_stream(request):
        events.append(ev)
    return events


# --- AC-1: pinned failover across providers ----------------------------------

def test_ac1_pinned_failover_openrouter_to_requesty(registry_with_fake, monkeypatch):
    monkeypatch.setenv("MODEL_HUB_FORCE_EXHAUST_TIER", "openrouter")
    cfg = cfg_with_catalog(
        make_tier("openrouter", 0, ["anthropic/claude-opus-5.5"]),
        make_tier("requesty", 1, ["anthropic/claude-opus-5-5"]),
        catalog=opus_catalog(),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="claude opus 5.5")))
    assert result.status == "success"
    assert result.pinned_model == "claude-opus-5.5"
    assert result.requested_model == "claude opus 5.5"
    assert result.model == "anthropic/claude-opus-5-5"  # requesty's mapped id
    assert result.tier_used == "requesty"
    assert result.model_substituted is False
    seq = [(a.tier, a.model, a.outcome) for a in result.failover_attempts]
    assert ("openrouter", "anthropic/claude-opus-5.5", "exhausted") in seq
    assert ("requesty", "anthropic/claude-opus-5-5", "success") in seq
    assert seq.index(("openrouter", "anthropic/claude-opus-5.5", "exhausted")) < \
        seq.index(("requesty", "anthropic/claude-opus-5-5", "success"))
    assert "claude-opus-5.5" in result.serving_line
    assert "pinned model claude-opus-5.5" in result.advisory_banner


# --- AC-2: unknown model -> structured error, zero provider calls ------------

def test_ac2_unknown_model_no_provider_calls(registry_with_fake, monkeypatch):
    calls = []
    orig_run = FakeAdapter.run

    async def spy(self, *a, **k):
        calls.append(a)
        return await orig_run(self, *a, **k)

    monkeypatch.setattr(FakeAdapter, "run", spy)
    cfg = cfg_with_catalog(
        make_tier("openrouter", 0, ["x"]),
        catalog=opus_catalog(),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="clod opus 99")))
    assert result.status == "model_unavailable"
    assert result.model_error["code"] == "no_match"
    assert "claude-opus-5.5" in result.model_error["suggestions"]
    assert result.failover_attempts == []
    assert calls == []


# --- AC-3: only carrier disabled -> strict False is model_unavailable ---------

def test_ac3_only_carrier_disabled_strict_false(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("xai", 0, ["grok-x"], enabled=False),
        catalog={"zzz-model": ModelCarrierMap(aliases=[], carriers={"xai": "grok-x"})},
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="zzz-model", allow_model_fallback=False)))
    assert result.status == "model_unavailable"
    err = result.model_error
    assert err["code"] == "no_carrier"
    assert err["pinned_model"] == "zzz-model"
    assert err["carriers_tried"] == []
    assert {"tier": "xai", "reason": "skip: disabled"} in err["exclusions"]


def test_ac3_only_carrier_disabled_default_falls_back(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("xai", 0, ["grok-x"], enabled=False),
        make_tier("fallback", 1, ["plain-model"]),
        catalog={"zzz-model": ModelCarrierMap(aliases=[], carriers={"xai": "grok-x"})},
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="zzz-model")))
    assert result.status == "success"
    assert result.model_substituted is True
    assert result.pinned_model == "zzz-model"
    assert result.requested_model == "zzz-model"
    assert result.tier_used == "fallback"
    assert result.model == "plain-model"  # served model differs from the pin


# --- AC-4: differential regression (unpinned byte-identical) -------------------

def test_ac4_unpinned_never_consults_catalog(registry_with_fake, monkeypatch):
    import hub.router as router_mod
    instantiations = []
    RealCatalog = router_mod.ModelCatalog

    class SpyCatalog(RealCatalog):
        def __init__(self, *a, **kw):
            instantiations.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(router_mod, "ModelCatalog", SpyCatalog)
    FakeAdapter.SCRIPT = {
        "m1": [(AttemptOutcome.UNKNOWN_MODEL, "unknown model 'm1'")],
        "m2": [(AttemptOutcome.EXHAUSTED, "quota dead")],
    }
    cfg = HubConfig(version=1, tiers=[
        make_tier("t1", 0, ["m1", "m2"]),
        make_tier("t2", 1, ["n1"]),
    ])
    seq = None
    for model in (None, "", "auto", "DEFAULT", "  "):
        FakeAdapter.SCRIPT = {
            "m1": [(AttemptOutcome.UNKNOWN_MODEL, "unknown model 'm1'")],
            "m2": [(AttemptOutcome.EXHAUSTED, "quota dead")],
        }
        hub = make_hub(cfg)  # fresh hub per variant: no breaker/quota bleed
        result = run(hub.execute(req(model=model)))
        assert result.status == "success"
        assert result.tier_used == "t2"
        assert result.pinned_model is None
        assert result.model_substituted is False
        if seq is None:
            seq = [(a.tier, a.model, a.outcome) for a in result.failover_attempts]
    # Stream entry too.
    FakeAdapter.SCRIPT = {
        "m1": [(AttemptOutcome.UNKNOWN_MODEL, "unknown model 'm1'")],
        "m2": [(AttemptOutcome.EXHAUSTED, "quota dead")],
    }
    events = run(collect_events(make_hub(cfg), req(model="auto")))
    assert [e for e in events if e.kind == "done"][0].payload["status"] == "success"
    assert instantiations == []
    assert seq == [
        ("t1", "m1", "unknown_model"),
        ("t1", "m2", "exhausted"),
        ("t2", "n1", "success"),
    ]


# --- AC-5: hub_ask default skip_tiers ------------------------------------------

def test_ac5_skip_tiers_composes_with_pin(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("agy", 0, ["m1"], adapter="fake", provider="antigravity",
                  capability=CapabilityMode.AGENT_LOOP),
        make_tier("requesty", 1, ["anthropic/claude-opus-5-5"]),
        catalog=opus_catalog(),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="claude opus 5.5", skip_tiers=["agy"])))
    assert result.status == "success"
    assert result.tier_used == "requesty"
    reasons = {(a.tier, a.reason) for a in result.failover_attempts
               if a.outcome == "skipped"}
    assert ("agy", "skip: model_pinned_non_agy") in reasons


def test_ac5_mcp_hub_ask_model_pin(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("requesty", 0, ["anthropic/claude-opus-5-5"]),
        catalog=opus_catalog(),
    )
    hub = make_hub(cfg)
    res = run(_call_hub_ask(7, {"question": "hi", "model": "claude opus 5.5"}, hub))
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["pinned_model"] == "claude-opus-5.5"
    assert payload["model"] == "anthropic/claude-opus-5-5"
    assert payload["tier_used"] == "requesty"
    assert payload["model_substituted"] is False


# --- AC-6: free_only excludes paid pins pre-attempt -----------------------------

def test_ac6_free_only_policy_no_spend_no_call(registry_with_fake, monkeypatch):
    probed_models = []
    orig_run = FakeAdapter.run

    async def spy(self, *a, **k):
        probed_models.append(a[1] if len(a) > 1 else k.get("model"))
        return await orig_run(self, *a, **k)

    monkeypatch.setattr(FakeAdapter, "run", spy)
    cfg = cfg_with_catalog(
        make_tier("openrouter", 0, ["x"], free_only=True),
        make_tier("requesty", 1, ["anthropic/claude-opus-5-5"]),
        catalog=opus_catalog(),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="claude-opus-5.5")))
    assert result.status == "success"
    assert result.tier_used == "requesty"
    skips = [(a.tier, a.reason) for a in result.failover_attempts if a.outcome == "skipped"]
    assert ("openrouter", "skip: free_only_policy") in skips
    assert "anthropic/claude-opus-5.5" not in probed_models  # never attempted


# --- AC-7: allow_model_fallback is loud -----------------------------------------

def test_ac7_allow_model_fallback_loud_substitution(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("xai", 0, ["grok-x"], enabled=False),
        make_tier("fallback", 1, ["plain-model"]),
        catalog={"zzz-model": ModelCarrierMap(aliases=[], carriers={"xai": "grok-x"})},
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="zzz-model", allow_model_fallback=True)))
    assert result.status == "success"
    assert result.model_substituted is True
    assert result.requested_model == "zzz-model"
    assert result.pinned_model == "zzz-model"
    assert result.tier_used == "fallback"
    assert result.model == "plain-model"  # a different model, served loudly
    # The pinned phase is preserved in the attempt log.
    assert any(a.reason == "skip: disabled" for a in result.failover_attempts)


def test_ac7_explicit_false_is_model_unavailable(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("xai", 0, ["grok-x"], enabled=False),
        make_tier("fallback", 1, ["plain-model"]),
        catalog={"zzz-model": ModelCarrierMap(aliases=[], carriers={"xai": "grok-x"})},
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="zzz-model", allow_model_fallback=False)))
    assert result.status == "model_unavailable"
    assert result.model_substituted is False


def test_ac7_default_on_falls_back_to_ladder(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("xai", 0, ["grok-x"], enabled=False),
        make_tier("fallback", 1, ["plain-model"]),
        catalog={"zzz-model": ModelCarrierMap(aliases=[], carriers={"xai": "grok-x"})},
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="zzz-model")))
    assert result.status == "success"
    assert result.model_substituted is True
    assert result.requested_model == "zzz-model"
    assert result.pinned_model == "zzz-model"
    assert result.tier_used == "fallback"
    assert result.model == "plain-model"  # a different model, served loudly
    # The pinned phase is preserved in the attempt log.
    assert any(a.reason == "skip: disabled" for a in result.failover_attempts)


# --- AC-8: stale mappings -------------------------------------------------------

def test_ac8_stale_mapping_advances_and_flags_status(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "anthropic/claude-opus-5-5-BAD": [(AttemptOutcome.UNKNOWN_MODEL, "unknown model id")],
    }
    catalog = {"claude-opus-5.5": ModelCarrierMap(
        aliases=[],
        carriers={"bad-tier": "anthropic/claude-opus-5-5-BAD",
                  "requesty": "anthropic/claude-opus-5-5"})}
    cfg = cfg_with_catalog(
        make_tier("bad-tier", 0, ["x"]),
        make_tier("requesty", 1, ["anthropic/claude-opus-5-5"]),
        catalog=catalog,
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="claude-opus-5.5")))
    assert result.status == "success"
    assert result.tier_used == "requesty"
    stale = [(a.tier, a.model, a.outcome) for a in result.failover_attempts
             if a.outcome == "stale_mapping"]
    assert ("bad-tier", "anthropic/claude-opus-5-5-BAD", "stale_mapping") in stale
    flags = hub.status()["stale_model_mappings"]
    assert any(f["canonical_model"] == "claude-opus-5.5" and f["tier"] == "bad-tier"
               and f["provider_model_id"] == "anthropic/claude-opus-5-5-BAD"
               for f in flags)
    # Operator fixes the mapping -> flag auto-clears on next status() (no restart).
    cfg.model_catalog.models["claude-opus-5.5"].carriers["bad-tier"] = \
        "anthropic/claude-opus-5-5-FIXED"
    assert hub.status()["stale_model_mappings"] == []


def test_ac8_stale_mapping_via_404_sniff(registry_with_fake):
    # AD-40: real adapters return the {"status_code", "body"} envelope on
    # HTTP-error paths; the router sniffs the envelope, not bare strings.
    FakeAdapter.SCRIPT = {
        "req-404": [(AttemptOutcome.TRANSIENT,
                     {"status_code": 404,
                      "body": "Client error '404 Not Found' for url 'https://x/v1/chat: "
                              "The model `req-404` does not exist"})],
    }
    catalog = {"m": ModelCarrierMap(
        aliases=[], carriers={"t1": "req-404", "t2": "good-id"})}
    cfg = cfg_with_catalog(
        make_tier("t1", 0, ["x"]),
        make_tier("t2", 1, ["good-id"]),
        catalog=catalog,
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req(model="m")))
    assert result.status == "success"
    assert result.model == "good-id"
    assert any(a.outcome == "stale_mapping" and a.model == "req-404"
               for a in result.failover_attempts)


# --- streaming ------------------------------------------------------------------

def test_pinned_stream_tier_switch_carries_pin(registry_with_fake, monkeypatch):
    monkeypatch.setenv("MODEL_HUB_FORCE_EXHAUST_TIER", "openrouter")
    cfg = cfg_with_catalog(
        make_tier("openrouter", 0, ["x"]),
        make_tier("requesty", 1, ["anthropic/claude-opus-5-5"]),
        catalog=opus_catalog(),
    )
    hub = make_hub(cfg)
    events = run(collect_events(hub, req(model="claude opus 5.5")))
    switches = [e for e in events if e.kind == "tier_switch"]
    assert switches
    assert all(s.payload.get("pinned_model") == "claude-opus-5.5" for s in switches)
    done = [e for e in events if e.kind == "done"][0]
    assert done.payload["pinned_model"] == "claude-opus-5.5"
    assert done.payload["tier_used"] == "requesty"


def test_pinned_stream_no_intra_tier_switch_message(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "bad-id": [(AttemptOutcome.UNKNOWN_MODEL, "unknown model id")],
    }
    cfg = cfg_with_catalog(
        make_tier("only-tier", 0, ["x"]),
        catalog={"m": ModelCarrierMap(aliases=[], carriers={"only-tier": "bad-id"})},
    )
    hub = make_hub(cfg)
    events = run(collect_events(hub, req(model="m", allow_model_fallback=False)))
    texts = [e.payload.get("text", "") for e in events
             if e.kind == "status" and isinstance(e.payload, dict)]
    assert not any("switching to" in t for t in texts)
    done = [e for e in events if e.kind == "done"][0]
    assert done.payload["status"] == "model_unavailable"


def test_pinned_stream_pin_error_is_error_event(registry_with_fake):
    cfg = cfg_with_catalog(make_tier("t1", 0, ["x"]), catalog=opus_catalog())
    hub = make_hub(cfg)
    events = run(collect_events(hub, req(model="clod opus 99")))
    errors = [e for e in events if e.kind == "error"]
    assert len(errors) == 1
    assert errors[0].payload["code"] == "no_match"
    assert "claude-opus-5.5" in errors[0].payload["suggestions"]


# --- MCP error mappings -----------------------------------------------------------

def test_mcp_hub_ask_unknown_model_minus_32602(registry_with_fake):
    cfg = cfg_with_catalog(make_tier("requesty", 0, ["x"]), catalog=opus_catalog())
    hub = make_hub(cfg)
    res = run(_call_hub_ask(7, {"question": "hi", "model": "clod opus 99"}, hub))
    err = res["error"]
    assert err["code"] == -32602
    assert "claude-opus-5.5" in err["data"]["suggestions"]


def test_mcp_hub_ask_no_carrier_minus_32603(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("xai", 0, ["grok-x"], enabled=False),
        catalog={"zzz-model": ModelCarrierMap(aliases=[], carriers={"xai": "grok-x"})},
    )
    hub = make_hub(cfg)
    res = run(_call_hub_ask(7, {"question": "hi", "model": "zzz-model",
                                "allow_model_fallback": False}, hub))
    err = res["error"]
    assert err["code"] == -32603
    assert err["data"]["code"] == "model_unavailable"
    assert err["data"]["pinned_model"] == "zzz-model"


def test_mcp_hub_ask_no_carrier_falls_back_by_default(registry_with_fake):
    cfg = cfg_with_catalog(
        make_tier("xai", 0, ["grok-x"], enabled=False),
        make_tier("fallback", 1, ["plain-model"]),
        catalog={"zzz-model": ModelCarrierMap(aliases=[], carriers={"xai": "grok-x"})},
    )
    hub = make_hub(cfg)
    res = run(_call_hub_ask(7, {"question": "hi", "model": "zzz-model"}, hub))
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["status"] == "success"
    assert payload["model_substituted"] is True
    assert payload["pinned_model"] == "zzz-model"
    assert payload["tier_used"] == "fallback"
    assert payload["model"] == "plain-model"
