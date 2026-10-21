"""Contract test for GET /hub/status with the additive live_quota block.

AC-3 (shape): every tier carries live_quota with quota_source in the four-way
enum; the AGY tier carries families[]/unmapped_buckets[]; the top-level
poller block exists. Existing fields keep their Epic-1 semantics.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.live_quota import QUOTA_SOURCES  # noqa: E402
from hub.types import (  # noqa: E402
    CapabilityMode,
    HubConfig,
    QuotaProviderSettings,
    QuotaSettings,
)

from conftest import make_hub, make_tier, run  # noqa: E402

BASE = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


def _cfg():
    quota = QuotaSettings(providers={
        "agy": QuotaProviderSettings(families={
            "gemini": {"label": "Gemini Models", "match": ["gemini"]},
        })
    })
    return HubConfig(
        version=1,
        tiers=[
            make_tier("agy", 0, ["gemini-x"], adapter="agy", provider="antigravity",
                      capability=CapabilityMode.AGENT_LOOP, optional=False),
            make_tier("groq", 2, ["g1"], provider="groq"),
        ],
        quota=quota,
    )


def test_status_payload_live_quota_shape(registry_with_fake):
    hub = make_hub(_cfg())
    payload = hub.status()

    assert set(QUOTA_SOURCES) == {"live", "headers", "measured", "estimated"}
    for t in payload["tiers"]:
        lq = t["live_quota"]
        assert lq["quota_source"] in QUOTA_SOURCES, t["id"]
        assert set(lq) >= {
            "quota_source", "quota_updated_at", "remaining_pct", "reset_in", "detail",
        }
    agy = next(t for t in payload["tiers"] if t["id"] == "agy")
    assert isinstance(agy["live_quota"]["families"], list)
    assert isinstance(agy["live_quota"]["unmapped_buckets"], list)
    # cold start: seed family present, honest estimated
    assert agy["live_quota"]["quota_source"] == "estimated"
    assert {f["id"] for f in agy["live_quota"]["families"]} == {"gemini"}

    assert set(payload["poller"]) >= {"healthy", "last_run", "providers"}
    assert payload["poller"]["healthy"] is True

    # Existing Epic-1 fields untouched.
    assert payload["config_version"] == 1
    assert set(payload["paid"]) == {"enabled", "spend_today_usd", "cap_usd"}
    assert set(payload["breaker"]) == {
        "exhaustions_to_open", "window_s", "cooldown_s",
    }
    assert agy["requests_today"] == 0
    assert agy["circuit"] == "closed"


def test_status_payload_with_live_probe(registry_with_fake):
    hub = make_hub(_cfg())
    now = datetime.now(timezone.utc)
    fams = [{
        "id": "gemini", "label": "Gemini Models", "models": ["gemini-x"],
        "remaining_pct": 23.0,
        "resets_at_epoch": now.timestamp() + 5 * 86400,
        "note": "",
    }]
    run(hub.live_quota.set_probe_result(fams, {"gemini-x": "gemini"}, [], now,
                                        detail="quota cache"))
    run(hub.live_quota.set_poller_health("agy_probe", True, now))
    payload = hub.status()
    agy = next(t for t in payload["tiers"] if t["id"] == "agy")
    lq = agy["live_quota"]
    assert lq["quota_source"] == "live"
    assert lq["remaining_pct"] == 23.0
    assert lq["reset_in"] is not None  # exact value is clock-sensitive; format covered in test_live_quota
    assert lq["families"][0]["quota_source"] == "live"
    # serializes cleanly (what the PWA actually parses)
    json.dumps(payload)


def test_status_payload_never_contains_secrets(registry_with_fake):
    """AC-10 lineage: the new block carries percentages/timestamps only."""
    hub = make_hub(_cfg())
    payload = hub.status()
    blob = json.dumps(payload)
    for needle in ("sk-", "ya29.", "gsk_", "nvapi-", "Bearer"):
        assert needle not in blob
