"""Regression tests: AgyAdapter session-flag threading (AC-4) + intra-tier banners.

Covers the MUST-FIX/SHOULD-FIX from code review on Epic 1:
- HubRequest.conversation_id / continue_session must reach the `agy` CLI flags
  exactly as the pre-hub code did: --conversation <id> wins; else -c when
  continue_session and an active session exists; else fresh (no flag).
- Intra-tier model advance in execute_stream() must emit a `status` event.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.adapters.agy_adapter import AgyAdapter  # noqa: E402
from hub.types import AttemptOutcome, CapabilityMode, HubConfig, HubRequest  # noqa: E402

from conftest import FakeAdapter, make_hub, make_tier  # noqa: E402


def ctx(active: bool):
    return SimpleNamespace(has_active_session=active)


# --- _cmd_args flag semantics (must match pre-hub main.py exactly) --------

def test_conversation_id_wins_over_continue():
    args = AgyAdapter._cmd_args("p", "m", ctx(True), "conv-123", True)
    assert "--conversation" in args
    assert args[args.index("--conversation") + 1] == "conv-123"
    assert "-c" not in args


def test_continue_session_with_active_session_adds_dash_c():
    args = AgyAdapter._cmd_args("p", "m", ctx(True), None, True)
    assert "-c" in args
    assert "--conversation" not in args


def test_continue_session_false_forces_fresh():
    args = AgyAdapter._cmd_args("p", "m", ctx(True), None, False)
    assert "-c" not in args
    assert "--conversation" not in args


def test_no_active_session_no_continue_flag():
    args = AgyAdapter._cmd_args("p", "m", ctx(False), None, True)
    assert "-c" not in args
    assert "--conversation" not in args


def test_base_flags_preserved():
    args = AgyAdapter._cmd_args("p", "m", ctx(False), None, False)
    assert args[:2] == ["agy", "-p"]
    assert "--mode" in args and "accept-edits" in args
    assert "--dangerously-skip-permissions" in args
    assert "--model" in args and args[args.index("--model") + 1] == "m"


# --- router threads request fields through to the adapter -------------------

def _hub_with_fake_tier():
    cfg = HubConfig(version=1, tiers=[
        make_tier("agy", 0, ["m1"], capability=CapabilityMode.AGENT_LOOP, optional=False),
    ])
    return make_hub(cfg)


def test_router_threads_session_fields_collect(registry_with_fake):
    hub = _hub_with_fake_tier()
    req = HubRequest(prompt="hi", conversation_id="conv-9", continue_session=False)
    result = asyncio.run(hub.execute(req))
    assert result.status == "success"
    assert FakeAdapter.LAST_KWARGS == {"conversation_id": "conv-9",
                                       "continue_session": False}


def test_router_threads_session_fields_stream(registry_with_fake):
    hub = _hub_with_fake_tier()
    req = HubRequest(prompt="hi", conversation_id=None, continue_session=True)
    events = asyncio.run(_drain(hub.execute_stream(req)))
    assert any(e.kind == "done" for e in events)
    assert FakeAdapter.LAST_KWARGS == {"conversation_id": None,
                                       "continue_session": True}


async def _drain(gen):
    return [e async for e in gen]


def test_intra_tier_advance_emits_status_banner(registry_with_fake):
    FakeAdapter.SCRIPT = {"m-old": [(AttemptOutcome.UNKNOWN_MODEL, "unknown model 'm-old'")]}
    cfg = HubConfig(version=1, tiers=[
        make_tier("agy", 0, ["m-old", "m-new"],
                  capability=CapabilityMode.AGENT_LOOP, optional=False),
    ])
    hub = make_hub(cfg)
    events = asyncio.run(_drain(hub.execute_stream(HubRequest(prompt="hi"))))
    status_texts = [e.payload.get("text", "") for e in events
                    if e.kind == "status" and isinstance(e.payload, dict)]
    assert any("m-old" in t and "m-new" in t for t in status_texts), \
        f"expected intra-tier banner, got: {status_texts}"
    assert any(e.kind == "done" for e in events)
