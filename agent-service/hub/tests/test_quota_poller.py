"""Tests for hub/quota_poller.py — supervised background probe (Epic C, AD-11)."""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub import cli_probe, quota_poller  # noqa: E402
from hub.types import HubConfig, QuotaProviderSettings, QuotaSettings  # noqa: E402

from conftest import make_hub, make_tier, run  # noqa: E402

BASE = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(**agy_kw):
    quota = QuotaSettings(providers={
        "agy": QuotaProviderSettings(
            families={"gemini": {"label": "Gemini Models", "match": ["gemini"]}},
            **agy_kw,
        )
    })
    return HubConfig(version=1, tiers=[make_tier("agy", 0, ["m"])], quota=quota)


def test_probe_once_not_available_marks_unhealthy(monkeypatch):
    async def fake_run_probe(agy_cfg, env=None):
        return None  # FR-3.7: no-op in this environment

    monkeypatch.setattr(cli_probe, "run_probe", fake_run_probe)
    hub = make_hub(_cfg())
    run(quota_poller._probe_once(hub))  # must not raise
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {})
    assert snap["poller"]["providers"]["agy_probe"]["healthy"] is False


def test_probe_once_success_writes_store(monkeypatch):
    result = cli_probe.ProbeResult(
        ok=True,
        families=[{
            "id": "gemini", "label": "Gemini Models", "models": ["m"],
            "remaining_pct": 42.0, "resets_at_epoch": BASE.timestamp() + 3600,
            "note": "",
        }],
        taxonomy={"m": "gemini"},
        unmapped=[],
        at=BASE,
        detail="quota cache",
    )

    async def fake_run_probe(agy_cfg, env=None):
        return result

    monkeypatch.setattr(cli_probe, "run_probe", fake_run_probe)
    hub = make_hub(_cfg())
    run(quota_poller._probe_once(hub))
    assert hub.live_quota.taxonomy() == {"m": "gemini"}
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {"gemini": "closed"})
    assert snap["tiers"]["agy"]["quota_source"] == "live"
    assert snap["tiers"]["agy"]["remaining_pct"] == 42.0
    assert snap["poller"]["healthy"] is True


def test_probe_once_exception_never_propagates(monkeypatch):
    """NFR-2: supervision boundary — a crashing probe can't kill the task."""

    async def boom(agy_cfg, env=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_probe, "run_probe", boom)
    hub = make_hub(_cfg())
    run(quota_poller._probe_once(hub))  # must not raise
    snap = hub.live_quota.snapshot(_cfg(), BASE, 1800, {})
    assert snap["poller"]["providers"]["agy_probe"]["healthy"] is False


def test_probe_disabled_in_config():
    hub = make_hub(_cfg(probe_enabled=False))
    run(quota_poller._probe_once(hub))
    snap = hub.live_quota.snapshot(_cfg(probe_enabled=False), BASE, 1800, {})
    assert snap["poller"]["providers"]["agy_probe"]["healthy"] is False


def test_start_stop_lifecycle():
    async def main():
        hub = make_hub(_cfg())
        stop = quota_poller.start(hub)
        await asyncio.sleep(0.05)
        await stop()  # must cancel cleanly mid initial-delay

    run(main())
