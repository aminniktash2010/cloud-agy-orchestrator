"""Unit tests: terminal hub-mode REPL module (T1-S2 / T1-S4)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from conftest import run  # noqa: E402
from hub.terminal_hubmode import (  # noqa: E402
    HONEST_CEILING,
    HubRepl,
    HubTermState,
    help_text,
    hubmode_banner,
    offer_banner,
    path_b_banner,
)
from hub.types import HubEvent  # noqa: E402


# --- Exact-text contracts (AD-55) -------------------------------------------

def test_offer_banner_with_estimate():
    b = offer_banner("~4d 2h")
    assert "Antigravity quota exhausted in this terminal." in b
    assert "Resets in ~4d 2h." in b
    assert "Press Ctrl+H for HUB MODE" in b
    assert "Or type /hub and press Enter." in b
    assert "(Ignore this to keep the TUI.)" in b
    assert HONEST_CEILING in b


def test_offer_banner_without_estimate_omits_bracket():
    b = offer_banner(None)
    assert "Resets in" not in b
    assert "Press Ctrl+H for HUB MODE" in b


def test_path_b_banner_variant_first_line():
    b = path_b_banner("~4d 2h")
    first = next(ln for ln in b.splitlines() if ln.strip())
    assert "The agy session ended on exhausted quota." in first
    assert "Press Ctrl+H" in b


def test_hubmode_banner_states_ceiling():
    b = hubmode_banner()
    assert HONEST_CEILING in b
    assert "/exit" in b


def test_help_text_ends_with_ceiling_contract():
    h = help_text()
    assert h.rstrip().endswith(
        "Answers only: hub mode cannot edit repos, run tools, or continue "
        "autopilot work. When your Antigravity quota resets, /exit brings the TUI back."
    )
    for cmd in ("/exit", "/model", "/status", "/help"):
        assert cmd in h


# --- Test doubles -----------------------------------------------------------

class StubWebSocket:
    def __init__(self):
        self.sent = []  # (kind, data)

    async def send_text(self, data):
        self.sent.append(("text", data))

    async def send_bytes(self, data):
        self.sent.append(("bytes", data))

    def texts(self):
        return "".join(d for k, d in self.sent if k == "text")

    def raw_bytes(self):
        return b"".join(d for k, d in self.sent if k == "bytes")


class StubHub:
    """Minimal ModelHub double: execute_stream / status / resolve_model_pin."""

    def __init__(self, events=None, pin_map=None):
        self._events = events or []
        self.pin_map = pin_map or {}
        self.seen_requests = []

    async def execute_stream(self, request):
        self.seen_requests.append(request)
        for ev in self._events:
            yield ev

    def status(self):
        return {
            "tiers": [
                {"id": "agy", "circuit": "open"},
                {"id": "groq", "circuit": "closed"},
            ],
            "retired_models": [],
        }

    def resolve_model_pin(self, name):
        # (canonical | None, suggestions)
        return self.pin_map.get(name.strip().lower(), (None, ["claude opus 5.5"]))


def make_repl(events=None, pin_map=None, state=None):
    ws = StubWebSocket()
    q: asyncio.Queue = asyncio.Queue()
    hub = StubHub(events=events, pin_map=pin_map)
    st = state or HubTermState()
    repl = HubRepl(ws.send_text, ws.send_bytes, q, hub, st)
    return repl, ws, q, hub, st


def feed(q, *chunks):
    for c in chunks:
        q.put_nowait(c if isinstance(c, bytes) else c.encode())


# --- Line reader: own echo (no PTY) ------------------------------------------

def test_read_line_echo_submit():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        feed(q, b"hi", b"\r")
        kind, text = await repl._read_line()
        assert (kind, text) == ("line", "hi")
        assert ws.raw_bytes() == b"hi\r\n"
    run(_inner())


def test_read_line_erase():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        feed(q, b"abc", b"\x7f", b"\r")
        kind, text = await repl._read_line()
        assert (kind, text) == ("line", "ab")
        assert ws.raw_bytes() == b"abc\b \b\r\n"
    run(_inner())


def test_read_line_ignores_other_control_bytes():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        feed(q, b"\x01\x02a\r")
        kind, text = await repl._read_line()
        assert (kind, text) == ("line", "a")
    run(_inner())


def test_read_line_ctrl_c_at_prompt():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        feed(q, b"\x03")
        kind, text = await repl._read_line()
        assert kind == "ctrl_c"
    run(_inner())


# --- Renderer: event mapping --------------------------------------------------

def test_render_log_chunk_raw_passthrough():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        await repl._render(HubEvent(kind="log", payload="hello "))
        await repl._render(HubEvent(kind="log", payload="world"))
        assert ws.texts() == "hello world"
    run(_inner())


def test_render_status_dim():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        await repl._render(HubEvent(kind="status", payload={"text": "trying groq…"}))
        t = ws.texts()
        assert "trying groq" in t and "\x1b[90m" in t
    run(_inner())


def test_render_tier_switch_dim_arrow():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        await repl._render(HubEvent(kind="tier_switch", payload={
            "to": {"tier": "groq", "model": "llama-3.3-70b-versatile"},
            "reason": "tier agy exhausted",
        }))
        t = ws.texts()
        assert "→ groq (llama-3.3-70b-versatile)" in t
    run(_inner())


def test_render_done_serving_line():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        await repl._render(HubEvent(kind="done", payload={
            "status": "success", "serving_line": "— served by groq/llama-3.3-70b",
        }))
        assert "— served by groq/llama-3.3-70b" in ws.texts()
    run(_inner())


def test_render_done_ladder_empty():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        await repl._render(HubEvent(kind="done", payload={
            "status": "ladder_empty", "ladder_empty": True,
            "failover_attempts": [1, 2],
        }))
        assert "All hub tiers exhausted" in ws.texts()
    run(_inner())


def test_render_error_no_match_with_suggestions():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        await repl._render(HubEvent(kind="error", payload={
            "code": "no_match", "message": "no carrier serves 'xyz'",
            "suggestions": ["claude opus 5.5"],
        }))
        t = ws.texts()
        assert "no_match" in t and "claude opus 5.5" in t
    run(_inner())


# --- Built-ins -----------------------------------------------------------------

def test_model_pin_set_and_prompt_shows_it():
    async def _inner():
        repl, ws, q, hub, st = make_repl(
            pin_map={"claude opus 5.5": ("Claude Opus 5.5", [])})
        await repl._builtin('/model Claude Opus 5.5')
        assert st.session_pin == "Claude Opus 5.5"
        assert "Claude Opus 5.5" in ws.texts()
    run(_inner())


def test_model_pin_unknown_prints_suggestions_pin_unchanged():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        st.session_pin = "Claude Opus 5.5"
        await repl._builtin("/model xyz")
        assert st.session_pin == "Claude Opus 5.5"  # unchanged
        assert "claude opus 5.5" in ws.texts()  # suggestions printed
    run(_inner())


def test_model_auto_clears_pin():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        st.session_pin = "Claude Opus 5.5"
        await repl._builtin("/model auto")
        assert st.session_pin is None
    run(_inner())


def test_status_compact_secret_free():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        await repl._builtin("/status")
        t = ws.texts()
        assert "agy" in t and "groq" in t and "retired" in t
        assert "sk-" not in t and "Bearer" not in t
    run(_inner())


def test_unknown_slash_command_guidance():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        await repl._builtin("/bogus")
        assert "unknown hub command" in ws.texts()
    run(_inner())


# --- End-to-end REPL loop (stubbed hub) ----------------------------------------

def test_ask_skips_agy_structurally():
    async def _inner():
        events = [
            HubEvent(kind="log", payload="ok"),
            HubEvent(kind="done", payload={
                "status": "success",
                "serving_line": "— served by groq/llama",
                "failover_attempts": [
                    {"tier": "agy", "outcome": "skipped",
                     "reason": "skip: skip_tiers"},
                ],
            }),
        ]
        repl, ws, q, hub, st = make_repl(events=events)

        async def driver():
            q.put_nowait(b"hi\r")
            # Keystrokes typed mid-stream are discarded by design (AD-54),
            # so /exit goes in only after the answer lands.
            for _ in range(500):
                if "— served by groq/llama" in ws.texts():
                    break
                await asyncio.sleep(0.01)
            q.put_nowait(b"/exit\r")

        result = await asyncio.gather(repl.run(), driver())
        assert result[0] == "respawn"  # no reset estimate -> direct respawn
        req = hub.seen_requests[0]
        assert req.skip_tiers == ["agy"]
        assert req.repo_ctx is None
        t = ws.texts()
        assert "ok" in t and "— served by groq/llama" in t
    run(_inner())


def test_exit_with_future_estimate_confirms():
    async def _inner():
        repl, ws, q, hub, st = make_repl()
        st.reset_estimate = "~4d 2h"
        feed(q, b"/exit\r", b"n\r", b"/exit\r", b"y\r")
        result = await repl.run()
        assert result == "respawn"
        assert "Reconnect the agy TUI anyway? [y/N]" in ws.texts()
        # first decline kept us in the REPL (second /exit accepted)
    run(_inner())


def test_ctrl_c_cancels_inflight_stream():
    async def _inner():
        started = asyncio.Event()

        async def hanging(request):
            yield HubEvent(kind="status", payload={"text": "thinking…"})
            started.set()
            await asyncio.sleep(30)
            yield HubEvent(kind="done", payload={"status": "success"})

        ws = StubWebSocket()
        q: asyncio.Queue = asyncio.Queue()
        hub = StubHub()
        hub.execute_stream = hanging
        st = HubTermState()
        repl = HubRepl(ws.send_text, ws.send_bytes, q, hub, st)

        async def driver():
            q.put_nowait(b"what is 2+2\r")
            await started.wait()
            await asyncio.sleep(0.05)  # let the consumer park in sleep(30)
            q.put_nowait(b"\x03")  # Ctrl+C mid-stream
            await asyncio.sleep(0.05)
            q.put_nowait(b"/exit\r")

        result = await asyncio.gather(repl.run(), driver())
        assert result[0] == "respawn"
        assert "^C" in ws.texts()
        assert "hub> " in ws.texts()  # back at prompt after cancel
    run(_inner())
