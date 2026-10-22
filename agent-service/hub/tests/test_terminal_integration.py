"""Integration tests: terminal hub-mode through the real /terminal/ws handler (T1-S5).

Real ``pty.openpty()`` pairs + a stub "TUI" script + a stub websocket drive
``websocket_terminal`` end to end. The subprocess spawn is monkeypatched to
run the stub instead of open-agy; the model hub is stubbed. AC-1, AC-2, AC-5,
AC-6, AC-7.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import main as main_module  # noqa: E402
from conftest import run  # noqa: E402
from hub.types import HubEvent  # noqa: E402

STUB_TUI_SRC = """\
import sys, time
mode = sys.argv[1] if len(sys.argv) > 1 else "linger"
if mode == "exit3":
    sys.exit(3)
if mode == "benign":
    sys.stdout.write("Antigravity CLI 1.4.5\\n> ")
    sys.stdout.flush()
    time.sleep(60)
# linger: quota-death message split across writes — no single read
# contains the full marker, exercising the 16KB tail join.
sys.stdout.write("some prompt output\\n")
sys.stdout.flush()
time.sleep(0.2)
sys.stdout.write("Error: individual quota")
sys.stdout.flush()
time.sleep(0.3)
sys.stdout.write(" reached. Resets in 98h0m20s.\\n")
sys.stdout.flush()
time.sleep(60)
"""


class StubWS:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.texts: list[str] = []
        self.binary: list[bytes] = []

    async def send_text(self, data):
        self.texts.append(data)

    async def send_bytes(self, data):
        self.binary.append(data)

    async def receive(self):
        return await self.inbox.get()

    async def accept(self):
        pass

    async def close(self, code=1000):
        pass

    def all_text(self):
        return "".join(self.texts)


class IntegHub:
    def __init__(self, events):
        self._events = events
        self.seen = []

    async def execute_stream(self, request):
        self.seen.append(request)
        for ev in self._events:
            yield ev

    def status(self):
        return {"tiers": [], "retired_models": []}

    def resolve_model_pin(self, name):
        return (None, [])


async def wait_for(cond, timeout=15, what="condition"):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {what}")


def _raise_repo(*a, **k):
    raise RuntimeError("no network in test")


def install_patches(monkeypatch, tmp_path, stub_mode, hub, procs):
    stub = tmp_path / "stub_tui.py"
    stub.write_text(STUB_TUI_SRC)
    real_spawn = asyncio.create_subprocess_exec

    async def fake_spawn(*args, **kwargs):
        proc = await real_spawn(
            sys.executable, str(stub), stub_mode,
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            close_fds=True,
        )
        procs.append(proc)
        return proc

    monkeypatch.setattr(main_module, "prepare_cached_repo", _raise_repo)
    monkeypatch.setattr(main_module, "HOT_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(
        main_module, "build_isolated_env", lambda rd: dict(os.environ))
    monkeypatch.setattr(main_module, "model_hub", hub)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)


def start_handler(ws):
    return asyncio.create_task(
        main_module.websocket_terminal(ws, repo="t1-test-repo",
                                      token="agy-public-client"))


async def stop_handler(handler):
    handler.cancel()
    try:
        await handler
    except asyncio.CancelledError:
        pass


# --- AC-1: split-marker banner, exactly once; Ctrl+H parks the TUI -----------

def test_ac1_banner_once_then_ctrl_h_parks_tui(tmp_path, monkeypatch):
    async def _inner():
        ws = StubWS()
        procs: list = []
        hub = IntegHub([
            HubEvent(kind="log", payload="ok"),
            HubEvent(kind="done", payload={
                "status": "success", "serving_line": "— served by groq/x"}),
        ])
        install_patches(monkeypatch, tmp_path, "linger", hub, procs)
        handler = start_handler(ws)
        try:
            await wait_for(
                lambda: "Press Ctrl+H for HUB MODE" in ws.all_text(),
                what="offer banner")
            await asyncio.sleep(0.6)  # later output must not re-banner
            assert ws.all_text().count("Press Ctrl+H for HUB MODE") == 1
            assert "Resets in ~4d 2h." in ws.all_text()
            assert procs and procs[0].returncode is None  # TUI alive

            ws.inbox.put_nowait({"bytes": b"\x08"})  # Ctrl+H
            await wait_for(lambda: "hub> " in ws.all_text(),
                           what="hub prompt")
            await wait_for(lambda: procs[0].returncode is not None,
                           what="TUI parked")
            assert "HUB MODE" in ws.all_text()
        finally:
            await stop_handler(handler)
    run(_inner())


# --- AC-2: /hub typed line switches too ---------------------------------------

def test_ac2_typed_hub_switches(tmp_path, monkeypatch):
    async def _inner():
        ws = StubWS()
        procs: list = []
        hub = IntegHub([])
        install_patches(monkeypatch, tmp_path, "linger", hub, procs)
        handler = start_handler(ws)
        try:
            await wait_for(
                lambda: "Press Ctrl+H for HUB MODE" in ws.all_text(),
                what="offer banner")
            for ch in b"/hub\r":
                ws.inbox.put_nowait({"bytes": bytes([ch])})
            await wait_for(lambda: "hub> " in ws.all_text(),
                           what="hub prompt via /hub")
            await wait_for(lambda: procs[0].returncode is not None,
                           what="TUI parked via /hub")
        finally:
            await stop_handler(handler)
    run(_inner())


# --- AC-3: hub-mode question skips agy structurally ----------------------------

def test_ac3_question_served_with_agy_never_attempted(tmp_path, monkeypatch):
    async def _inner():
        ws = StubWS()
        procs: list = []
        hub = IntegHub([
            HubEvent(kind="log", payload="ok"),
            HubEvent(kind="done", payload={
                "status": "success",
                "serving_line": "— served by groq/llama",
                "failover_attempts": [
                    {"tier": "agy", "outcome": "skipped",
                     "reason": "skip: skip_tiers"},
                ]}),
        ])
        install_patches(monkeypatch, tmp_path, "linger", hub, procs)
        handler = start_handler(ws)
        try:
            await wait_for(
                lambda: "Press Ctrl+H for HUB MODE" in ws.all_text(),
                what="offer banner")
            ws.inbox.put_nowait({"bytes": b"\x08"})
            await wait_for(lambda: "hub> " in ws.all_text(),
                           what="hub prompt")

            async def driver():
                ws.inbox.put_nowait({"bytes": b"hi\r"})
                await wait_for(
                    lambda: "— served by groq/llama" in ws.all_text(),
                    what="answer + serving line")

            await driver()
            req = hub.seen[0]
            assert req.skip_tiers == ["agy"]
            assert req.repo_ctx is None
            assert "ok" in ws.all_text()
        finally:
            await stop_handler(handler)
    run(_inner())


# --- AC-5: /exit with future estimate confirms, respawns, re-arms -------------

def test_ac5_exit_confirms_respawns_and_rearms(tmp_path, monkeypatch):
    async def _inner():
        ws = StubWS()
        procs: list = []
        hub = IntegHub([])
        install_patches(monkeypatch, tmp_path, "linger", hub, procs)
        handler = start_handler(ws)
        try:
            await wait_for(
                lambda: "Press Ctrl+H for HUB MODE" in ws.all_text(),
                what="offer banner")
            ws.inbox.put_nowait({"bytes": b"\x08"})
            await wait_for(lambda: "hub> " in ws.all_text(),
                           what="hub prompt")
            assert len(procs) == 1

            for ch in b"/exit\r":
                ws.inbox.put_nowait({"bytes": bytes([ch])})
            await wait_for(
                lambda: "Reconnect the agy TUI anyway? [y/N]" in ws.all_text(),
                what="exit confirmation")
            for ch in b"y\r":
                ws.inbox.put_nowait({"bytes": bytes([ch])})
            await wait_for(
                lambda: "agy TUI reconnected" in ws.all_text(),
                what="respawn note")
            assert len(procs) == 2  # fresh TUI incarnation
            # The new TUI prints quota death again -> detector re-arms.
            await wait_for(
                lambda: ws.all_text().count("Press Ctrl+H for HUB MODE") == 2,
                what="second banner after respawn")
        finally:
            await stop_handler(handler)
    run(_inner())


# --- AC-6: Path B — TUI exits on exhaustion; Ctrl+H -> REPL, loop alive -------

def test_ac6_path_b_exited_tui(tmp_path, monkeypatch):
    async def _inner():
        ws = StubWS()
        procs: list = []
        hub = IntegHub([
            HubEvent(kind="log", payload="ok"),
            HubEvent(kind="done", payload={
                "status": "success", "serving_line": "— served by groq/x"}),
        ])
        install_patches(monkeypatch, tmp_path, "exit3", hub, procs)
        handler = start_handler(ws)
        try:
            await wait_for(
                lambda: "The agy session ended on exhausted quota." in ws.all_text(),
                what="Path B banner")
            # Input loop must be alive despite the dead PTY.
            ws.inbox.put_nowait({"bytes": b"\x08"})
            await wait_for(lambda: "hub> " in ws.all_text(),
                           what="hub prompt (Path B, no park crash)")

            async def driver():
                ws.inbox.put_nowait({"bytes": b"hi\r"})
                await wait_for(lambda: "— served by groq/x" in ws.all_text(),
                               what="answer in Path-B hub mode")

            await driver()
            assert hub.seen and hub.seen[0].skip_tiers == ["agy"]
        finally:
            await stop_handler(handler)
    run(_inner())


# --- AC-7: no false positives on normal traffic ---------------------------------

def test_ac7_benign_traffic_never_banners(tmp_path, monkeypatch):
    async def _inner():
        ws = StubWS()
        procs: list = []
        hub = IntegHub([])
        install_patches(monkeypatch, tmp_path, "benign", hub, procs)
        handler = start_handler(ws)
        try:
            await asyncio.sleep(2.5)
            assert "HUB MODE" not in ws.all_text()
            assert "quota exhausted" not in ws.all_text()
            assert procs and procs[0].returncode is None  # TUI untouched
        finally:
            await stop_handler(handler)
    run(_inner())
