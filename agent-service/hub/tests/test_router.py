"""Unit tests: ladder walk with fake adapters (E1-S7)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.router import HubError, ModelHub  # noqa: E402
from hub.types import AdapterResponse, AttemptOutcome, CapabilityMode, HubConfig, HubRequest, RepoContext  # noqa: E402

from conftest import FakeAdapter, make_hub, make_tier, run  # noqa: E402


def base_cfg(*tiers) -> HubConfig:
    return HubConfig(version=1, tiers=list(tiers))


def req(**kw) -> HubRequest:
    kw.setdefault("prompt", "do the thing")
    return HubRequest(**kw)


# --- ladder walk ------------------------------------------------------------

def test_failover_agy_to_aistudio(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "m-high": [(AttemptOutcome.EXHAUSTED, "credits balance is too low")],
        "m-low": [(AttemptOutcome.EXHAUSTED, "code 429")],
    }
    cfg = base_cfg(
        make_tier("agy", 0, ["m-high", "m-low"], capability=CapabilityMode.AGENT_LOOP,
                  optional=False),
        make_tier("aistudio", 1, ["g1"], api_key_env=None),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.status == "success"
    assert result.tier_used == "aistudio"
    assert result.provider == "fake-provider"
    assert result.model == "g1"
    assert result.capability_mode == "answer_only"
    assert result.advisory_banner != ""  # FR-3.2 honesty label
    tiers_tried = [a.tier for a in result.failover_attempts]
    assert tiers_tried[0] == "agy"
    assert "aistudio" in tiers_tried
    assert all(a.outcome == "exhausted" for a in result.failover_attempts if a.tier == "agy")


def test_in_tier_model_failover_on_unknown_model(registry_with_fake):
    FakeAdapter.SCRIPT = {"m-old": [(AttemptOutcome.UNKNOWN_MODEL, "unknown model 'm-old'")]}
    cfg = base_cfg(make_tier("agy", 0, ["m-old", "m-new"], optional=False,
                             capability=CapabilityMode.AGENT_LOOP))
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.status == "success"
    assert result.model == "m-new"
    assert result.failover_attempts[0].outcome == "unknown_model"
    assert result.failover_attempts[0].model == "m-old"
    assert result.tier_used == "agy"  # stayed within the tier


def test_requested_model_override_pins_first_tier0_model(registry_with_fake):
    cfg = base_cfg(make_tier("agy", 0, ["m-a", "m-b"], optional=False,
                             capability=CapabilityMode.AGENT_LOOP))
    hub = make_hub(cfg)
    result = run(hub.execute(req(requested_model_override="m-z")))
    assert result.failover_attempts[0].model == "m-z"
    assert result.model == "m-z"


def test_disabled_tier_skipped(registry_with_fake):
    cfg = base_cfg(
        make_tier("agy", 0, ["m1"], enabled=False),
        make_tier("aistudio", 1, ["g1"]),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.tier_used == "aistudio"
    skipped = result.failover_attempts[0]
    assert skipped.tier == "agy" and skipped.outcome == "skipped"
    assert skipped.reason == "skip: disabled"


def test_missing_key_skips_tier(registry_with_fake, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = base_cfg(
        make_tier("agy", 0, ["m1"], enabled=False),
        make_tier("aistudio", 1, ["g1"], api_key_env="GEMINI_API_KEY"),
        make_tier("groq", 2, ["q1"]),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.tier_used == "groq"
    skip = [a for a in result.failover_attempts if a.tier == "aistudio"][0]
    assert skip.outcome == "skipped" and skip.reason == "skip: missing_key"


def test_ladder_empty_shape(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "m1": [(AttemptOutcome.EXHAUSTED, "no credits")],
        "g1": [(AttemptOutcome.EXHAUSTED, "429")],
    }
    cfg = base_cfg(
        make_tier("agy", 0, ["m1"], optional=False),
        make_tier("aistudio", 1, ["g1"]),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.ladder_empty is True
    assert result.status == "ladder_empty"
    assert len(result.failover_attempts) == 2


def test_force_exhaust_hook(registry_with_fake, monkeypatch):
    monkeypatch.setenv("MODEL_HUB_FORCE_EXHAUST_TIER", "agy")
    cfg = base_cfg(
        make_tier("agy", 0, ["m1"], optional=False),
        make_tier("aistudio", 1, ["g1"]),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.tier_used == "aistudio"
    forced = result.failover_attempts[0]
    assert forced.outcome == "exhausted" and "forced" in forced.reason


# --- retry / fault semantics -------------------------------------------------

def test_retry_on_retry_after_once_then_success(registry_with_fake):
    FakeAdapter.SCRIPT = {
        "g1": [
            (AttemptOutcome.RETRYABLE_429, {"retry_after_s": 0.01, "detail": "slow down"}),
            (AttemptOutcome.SUCCESS, AdapterResponse(text="ok", model="g1")),
        ]
    }
    cfg = base_cfg(make_tier("aistudio", 0, ["g1"]))
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.status == "success"
    assert result.answer == "ok"
    outcomes = [a.outcome for a in result.failover_attempts]
    assert outcomes == ["retryable_429", "success"]


def test_auth_fault_optional_tier_skips(registry_with_fake):
    FakeAdapter.SCRIPT = {"g1": [(AttemptOutcome.AUTH_FAULT, "401 bad key")]}
    cfg = base_cfg(
        make_tier("aistudio", 0, ["g1"], optional=True),
        make_tier("groq", 1, ["q1"]),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.tier_used == "groq"


def test_auth_fault_required_tier_surfaces(registry_with_fake):
    FakeAdapter.SCRIPT = {"m1": [(AttemptOutcome.AUTH_FAULT, "invalid_grant")]}
    cfg = base_cfg(make_tier("agy", 0, ["m1"], optional=False))
    hub = make_hub(cfg)
    with pytest.raises(HubError, match="auth fault on required tier"):
        run(hub.execute(req()))


def test_bad_request_surfaces_never_advances(registry_with_fake):
    # With a repo context (the /chat path), BAD_REQUEST still fails loud —
    # the fail-loud raise is preserved for genuine /chat misuse.
    FakeAdapter.SCRIPT = {"g1": [(AttemptOutcome.BAD_REQUEST, "400 malformed")]}
    cfg = base_cfg(
        make_tier("aistudio", 0, ["g1"]),
        make_tier("groq", 1, ["q1"]),
    )
    hub = make_hub(cfg)
    with pytest.raises(HubError, match="bad request"):
        run(hub.execute(req(repo_ctx=RepoContext(repo_dir="/tmp/x"))))


def test_bad_request_without_repo_ctx_skips_tier(registry_with_fake):
    # Repo-less callers (MCP hub_ask): BAD_REQUEST skips the tier instead of
    # raising, so fallback tiers still serve the request.
    FakeAdapter.SCRIPT = {"g1": [(AttemptOutcome.BAD_REQUEST, "400 malformed")]}
    cfg = base_cfg(
        make_tier("aistudio", 0, ["g1"]),
        make_tier("groq", 1, ["q1"]),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req()))  # no repo_ctx
    assert result.status == "success"
    assert result.tier_used == "groq"
    skips = [a for a in result.failover_attempts
             if a.tier == "aistudio" and a.outcome == "skipped"]
    assert any(a.reason == "skip: no_repo_ctx" for a in skips), \
        f"expected a 'skip: no_repo_ctx' record, got: {result.failover_attempts}"


def test_bad_request_without_repo_ctx_skips_tier_stream(registry_with_fake):
    # Same contract on the streaming path: no error event, fallback serves.
    FakeAdapter.SCRIPT = {"g1": [(AttemptOutcome.BAD_REQUEST, "400 malformed")]}
    cfg = base_cfg(
        make_tier("aistudio", 0, ["g1"]),
        make_tier("groq", 1, ["q1"]),
    )
    hub = make_hub(cfg)

    async def collect():
        events = []
        async for ev in hub.execute_stream(req()):  # no repo_ctx
            events.append(ev)
        return events

    events = run(collect())
    kinds = [e.kind for e in events]
    assert "error" not in kinds
    done = next(e for e in events if e.kind == "done")
    assert done.payload["tier_used"] == "groq"


def test_paid_tier_triple_gate_default_off(registry_with_fake):
    cfg = base_cfg(
        make_tier("xai", 0, ["grok-1"], paid_gate=True, cost="paid"),
    )
    hub = make_hub(cfg)
    result = run(hub.execute(req()))
    assert result.ladder_empty is True
    assert result.failover_attempts[0].reason == "skip: paid_disabled"


# --- streaming ----------------------------------------------------------------

def test_execute_stream_emits_tier_switch_and_banner(registry_with_fake):
    FakeAdapter.SCRIPT = {"m1": [(AttemptOutcome.EXHAUSTED, "no credits")]}
    cfg = base_cfg(
        make_tier("agy", 0, ["m1"], optional=False),  # answer_only in test -> buffered
        make_tier("aistudio", 1, ["g1"]),
    )
    hub = make_hub(cfg)

    async def collect():
        events = []
        async for ev in hub.execute_stream(req()):
            events.append(ev)
        return events

    events = run(collect())
    kinds = [e.kind for e in events]
    assert "tier_switch" in kinds
    switch = next(e for e in events if e.kind == "tier_switch")
    assert switch.payload["to"]["tier"] == "aistudio"
    assert switch.payload["capability_mode"] == "answer_only"
    # advisory banner precedes the answer chunks
    logs = [e.payload for e in events if e.kind == "log"]
    assert any("Advisory answer" in str(p) for p in logs)
    done = next(e for e in events if e.kind == "done")
    assert done.payload["tier_used"] == "aistudio"
    assert done.payload["status"] == "success"
