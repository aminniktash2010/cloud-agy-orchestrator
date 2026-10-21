"""AC-4: per-family circuit breakers for Tier 0 (live-quota Epic C).

Recorded exhausted-output samples per family -> only that family's breaker
opens; Tier 0 is skipped only when ALL families are open. Healthy families
keep serving while a dry family cools down.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.adapters import ADAPTER_REGISTRY  # noqa: E402
from hub.router import ModelHub  # noqa: E402
from hub.types import (  # noqa: E402
    AdapterResponse,
    AttemptOutcome,
    BreakerSettings,
    CapabilityMode,
    HubConfig,
    HubEvent,
    HubRequest,
    QuotaProviderSettings,
    QuotaSettings,
)

from conftest import FakeAdapter, StubConfigLoader, make_tier, run  # noqa: E402

GEMINI_M = "gemini-3.8-flash-high"
CLAUDE_M = "claude-sonnet-4.6"


def _agy_quota(family_threshold=1):
    return QuotaSettings(
        family_breaker=BreakerSettings(
            exhaustions_to_open=family_threshold, window_s=60, cooldown_s=60
        ),
        providers={
            "agy": QuotaProviderSettings(
                families={
                    "gemini": {"label": "Gemini Models", "match": ["gemini"]},
                    "claude_gpt": {"label": "Claude and GPT models",
                                   "match": ["claude", "gpt"]},
                },
                family_markers={"claude_gpt": ["claude"]},
            )
        },
    )


def _agy_hub(monkeypatch, family_threshold=1):
    monkeypatch.setitem(ADAPTER_REGISTRY, "agy", FakeAdapter)
    tier = make_tier(
        "agy", 0, [GEMINI_M, CLAUDE_M],
        adapter="agy", provider="antigravity",
        capability=CapabilityMode.AGENT_LOOP, optional=False,
    )
    cfg = HubConfig(version=1, tiers=[tier], quota=_agy_quota(family_threshold))
    # Family breaker thresholds come from config (hot-reloadable); the
    # constructor arg would be re-configured by _config() anyway.
    return ModelHub(config_loader=StubConfigLoader(cfg))


def test_only_exhausted_family_breaker_opens(monkeypatch):
    hub = _agy_hub(monkeypatch)
    FakeAdapter.SCRIPT = {
        GEMINI_M: [(AttemptOutcome.EXHAUSTED,
                    "gemini weekly quota exhausted (resource_exhausted)")],
        CLAUDE_M: [(AttemptOutcome.SUCCESS,
                    AdapterResponse(text="ok", model=CLAUDE_M))],
    }
    result = run(hub.execute(HubRequest(prompt="x")))
    # Existing semantics: EXHAUSTED advances past the tier in this request.
    assert result.ladder_empty
    # ...but only the gemini family breaker opened, not claude_gpt's.
    assert hub._family_breaker.is_open("agy:gemini")
    assert not hub._family_breaker.is_open("agy:claude_gpt")
    # Whole-tier breaker untouched by the family note (threshold 3, default).
    assert not hub._breaker.is_open("agy")


def test_open_family_skipped_healthy_family_serves(monkeypatch):
    hub = _agy_hub(monkeypatch)
    hub._config()  # lazily creates the family breaker
    hub._family_breaker.note_exhaustion("agy:gemini")  # threshold 1 -> open
    FakeAdapter.SCRIPT = {
        CLAUDE_M: [(AttemptOutcome.SUCCESS,
                    AdapterResponse(text="served by claude", model=CLAUDE_M))],
    }
    result = run(hub.execute(HubRequest(prompt="x")))
    assert result.status == "success"
    assert result.model == CLAUDE_M
    reasons = [a.reason for a in result.failover_attempts]
    assert any("skip: family_circuit_open (agy:gemini)" in r for r in reasons)
    assert all(a.model != GEMINI_M for a in result.failover_attempts)


def test_all_families_open_skips_tier(monkeypatch):
    hub = _agy_hub(monkeypatch)
    hub._config()  # lazily creates the family breaker
    hub._family_breaker.note_exhaustion("agy:gemini")
    hub._family_breaker.note_exhaustion("agy:claude_gpt")
    result = run(hub.execute(HubRequest(prompt="x")))
    assert result.ladder_empty
    reasons = [a.reason for a in result.failover_attempts]
    assert any("skip: all_families_open" in r for r in reasons)
    # No model was attempted at all.
    assert all(a.model == "" for a in result.failover_attempts)


def test_family_success_clears_consecutive(monkeypatch):
    hub = _agy_hub(monkeypatch, family_threshold=2)
    FakeAdapter.SCRIPT = {
        GEMINI_M: [(AttemptOutcome.EXHAUSTED, "gemini resource_exhausted")],
    }
    run(hub.execute(HubRequest(prompt="x")))
    assert hub._family_breaker._state["agy:gemini"]["consecutive"] == 1
    assert not hub._family_breaker.is_open("agy:gemini")

    FakeAdapter.SCRIPT = {
        GEMINI_M: [(AttemptOutcome.SUCCESS, AdapterResponse(text="ok", model=GEMINI_M))],
    }
    result = run(hub.execute(HubRequest(prompt="x")))
    assert result.status == "success"
    assert hub._family_breaker._state["agy:gemini"]["consecutive"] == 0


def test_unparseable_output_falls_back_to_whole_tier(monkeypatch):
    """FR-3.3: a model matching no family -> legacy whole-tier behavior."""
    monkeypatch.setitem(ADAPTER_REGISTRY, "agy", FakeAdapter)
    tier = make_tier(
        "agy", 0, ["mystery-model-9"],
        adapter="agy", provider="antigravity",
        capability=CapabilityMode.AGENT_LOOP, optional=False,
    )
    cfg = HubConfig(version=1, tiers=[tier], quota=_agy_quota())
    hub = ModelHub(config_loader=StubConfigLoader(cfg))
    FakeAdapter.SCRIPT = {
        "mystery-model-9": [(AttemptOutcome.EXHAUSTED, "totally generic error")],
    }
    result = run(hub.execute(HubRequest(prompt="x")))
    assert result.ladder_empty
    # No family exhaustion was noted ("default" -> legacy path): every entry
    # in the family breaker is still at zero consecutive exhaustions.
    assert all(
        e["consecutive"] == 0 for e in hub._family_breaker._state.values()
    )


async def _collect(aiter):
    return [e async for e in aiter]


def test_stream_skips_open_family(monkeypatch):
    hub = _agy_hub(monkeypatch)
    hub._config()  # lazily creates the family breaker
    hub._family_breaker.note_exhaustion("agy:gemini")
    FakeAdapter.SCRIPT = {
        CLAUDE_M: [(AttemptOutcome.SUCCESS,
                    AdapterResponse(text="stream ok", model=CLAUDE_M))],
    }
    events = run(_collect(hub.execute_stream(HubRequest(prompt="x"))))
    done = [e for e in events if e.kind == "done"]
    assert done, "stream must terminate with a done event"
    assert done[-1].payload["status"] == "success"
    assert done[-1].payload["model"] == CLAUDE_M
