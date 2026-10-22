"""Security test: no key material leaks into logs, results, SSE, or reports (AC-9)."""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub import observability as hub_observability  # noqa: E402
from hub.types import AttemptOutcome, CapabilityMode, HubConfig, HubRequest  # noqa: E402

from conftest import FakeAdapter, make_hub, make_tier, run  # noqa: E402

FAKE_KEY = "sk-test-FAKE-KEY-xyz123-not-real"


def test_no_secret_leak_across_surfaces(registry_with_fake, monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    FakeAdapter.SCRIPT = {
        "m1": [(AttemptOutcome.EXHAUSTED, "credits balance is too low")],
    }
    cfg = HubConfig(version=1, tiers=[
        make_tier("agy", 0, ["m1"], optional=False,
                  capability=CapabilityMode.AGENT_LOOP),
        make_tier("aistudio", 1, ["g1"], api_key_env="GEMINI_API_KEY",
                  provider="google-ai-studio", data_use="may-train-google-models"),
    ])
    hub = make_hub(cfg)

    with caplog.at_level(logging.INFO, logger="model-hub.attempts"):
        result = run(hub.execute(HubRequest(prompt="do the thing")))

    assert result.tier_used == "aistudio"

    # 1. logs
    assert FAKE_KEY not in caplog.text
    # 2. HubResult JSON (what /chat returns)
    assert FAKE_KEY not in result.model_dump_json()
    # 3. /hub/status payload
    assert FAKE_KEY not in json.dumps(hub.status())
    # 4. Drive report surfaces
    assert FAKE_KEY not in hub_observability.drive_hub_block(result)
    assert FAKE_KEY not in hub_observability.drive_footer(cfg)
    # 5. Gmail one-liner / serving line
    assert FAKE_KEY not in result.serving_line
    assert FAKE_KEY not in result.advisory_banner
    # 6. attempt records
    for a in result.failover_attempts:
        assert FAKE_KEY not in a.model_dump_json()


def test_no_secret_leak_retired_model_surfaces(registry_with_fake, monkeypatch, caplog):
    """Retired-model excerpts (status + logs) never carry key material.

    The hygiene path only handles (tier, model_id, excerpt, source); this
    verifies the env key never enters those surfaces through the plumbing.
    The body is long to exercise the 200-char truncation."""
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    long_body = ('{"error": {"code": "model_not_found", "message": "The model '
                 '`dead` does not exist. ' + "x" * 500 + '"}}')
    FakeAdapter.SCRIPT = {
        "dead": [(AttemptOutcome.TRANSIENT,
                  {"status_code": 404, "body": long_body})],
        "live": [(AttemptOutcome.SUCCESS,
                  __import__("hub.types", fromlist=["AdapterResponse"]).AdapterResponse(
                      text="ok", model="live"))],
    }
    cfg = HubConfig(version=1, tiers=[
        make_tier("openrouter", 0, ["dead"], api_key_env="OPENROUTER_API_KEY",
                  provider="openrouter"),
        make_tier("fallback", 1, ["live"]),
    ])
    hub = make_hub(cfg)

    with caplog.at_level(logging.WARNING, logger="model-hub.router"):
        result = run(hub.execute(HubRequest(prompt="do the thing")))

    assert result.status == "success"
    assert ("openrouter", "dead") in hub._retired_models
    # 1. router log line for the quarantine
    assert FAKE_KEY not in caplog.text
    # 2. /hub/status retired_models section
    status_json = json.dumps(hub.status())
    assert FAKE_KEY not in status_json
    section = hub.status()["retired_models"]
    assert len(section) == 1
    assert len(section[0]["signal_excerpt"]) <= 200
    # 3. attempt records (reason strings)
    for a in result.failover_attempts:
        assert FAKE_KEY not in a.model_dump_json()
    # 4. result JSON
    assert FAKE_KEY not in result.model_dump_json()
