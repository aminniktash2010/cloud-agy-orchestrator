"""Terminal hub-mode: quota-death REPL inside /terminal/ws (Epic T1, AD-52).

All hub-mode logic that can be unit-tested without a live websocket lives
here: banner/help text contracts, the echoing line reader, the stream-event
renderer, and the Ctrl+C race. The ``websocket_terminal`` handler keeps only
the state machine, the intercept, and the spawn/park plumbing.

The REPL is deliberately NOT a shell: input is a question or a ``/command``,
never executed. Model answers render as inert terminal text.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from hub.types import HubRequest

# ---------------------------------------------------------------------------
# ANSI
# ---------------------------------------------------------------------------

_DIM = "\x1b[90m"
_RESET = "\x1b[0m"
_YELLOW = "\x1b[1;33m"
_GREEN = "\x1b[1;32m"

# ---------------------------------------------------------------------------
# Text contracts (AD-55) — pinned by tests
# ---------------------------------------------------------------------------

HONEST_CEILING = (
    "Answers only: hub mode cannot edit repos, run tools, "
    "or continue autopilot work."
)


def offer_banner(reset_estimate: Optional[str]) -> str:
    """Path A: quota death seen in the live PTY stream of a running TUI."""
    est = f" Resets in {reset_estimate}." if reset_estimate else ""
    return (
        f"\r\n{_YELLOW}⚠️ Antigravity quota exhausted in this terminal.{est}{_RESET}\r\n"
        "This TUI can't reach the hub's fallback models.\r\n"
        "Press Ctrl+H for HUB MODE — Q&A through the hub ladder (answers only,\r\n"
        "no repo edits). Or type /hub and press Enter. (Ignore this to keep the TUI.)\r\n"
        f"{_DIM}{HONEST_CEILING}{_RESET}\r\n"
    )


def path_b_banner(reset_estimate: Optional[str]) -> str:
    """Path B: the TUI already exited on exhausted quota."""
    est = f" Resets in {reset_estimate}." if reset_estimate else ""
    return (
        f"\r\n{_YELLOW}The agy session ended on exhausted quota.{est}{_RESET}\r\n"
        "Press Ctrl+H for HUB MODE — Q&A through the hub ladder (answers only,\r\n"
        "no repo edits). Or type /hub and press Enter.\r\n"
        f"{_DIM}{HONEST_CEILING}{_RESET}\r\n"
    )


def hubmode_banner() -> str:
    """Printed once when hub mode is entered."""
    return (
        f"\r\n{_GREEN}HUB MODE{_RESET} — Q&A through the model hub (fallback providers).\r\n"
        f"{_DIM}{HONEST_CEILING}{_RESET}\r\n"
        f"{_DIM}Type /help for commands, /exit to bring the TUI back.{_RESET}\r\n"
    )


def help_text() -> str:
    return (
        "\r\nHub-mode commands:\r\n"
        "  /exit            leave hub mode (reconnect the agy TUI)\r\n"
        "  /model <name>    pin a model across providers (e.g. /model Claude Opus 5.5)\r\n"
        "  /model auto      clear the pin (default ladder)\r\n"
        "  /status          compact hub status (tiers + retired models)\r\n"
        "  /help            this list\r\n"
        "Anything else is answered through the hub ladder (agy tier skipped).\r\n"
        "\r\n"
        "Answers only: hub mode cannot edit repos, run tools, or continue "
        "autopilot work. When your Antigravity quota resets, /exit brings the TUI back.\r\n"
    )


# ---------------------------------------------------------------------------
# Per-connection state (AD-49)
# ---------------------------------------------------------------------------

@dataclass
class HubTermState:
    phase: str = "tui"  # "tui" | "armed" | "hubmode"
    banner_shown: bool = False
    reset_estimate: Optional[str] = None
    armed_line: bytearray = field(default_factory=bytearray)
    session_pin: Optional[str] = None


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

SendText = Callable[[str], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]


class HubRepl:
    """Line-oriented hub Q&A. ``send_text``/``send_bytes`` are the websocket
    senders; ``hub_q`` carries inbound input bytes; ``model_hub`` is the
    module-level singleton (or a test double exposing ``execute_stream``,
    ``status`` and ``resolve_model_pin``)."""

    def __init__(
        self,
        send_text: SendText,
        send_bytes: SendBytes,
        hub_q: "asyncio.Queue[bytes]",
        model_hub: Any,
        state: HubTermState,
    ) -> None:
        self._send_text = send_text
        self._send_bytes = send_bytes
        self._q = hub_q
        self._hub = model_hub
        self._state = state

    # -- output helpers ----------------------------------------------------

    async def _print(self, text: str) -> None:
        # xterm needs CRLF; keep content raw otherwise.
        await self._send_text(text.replace("\n", "\r\n"))

    async def _prompt(self) -> None:
        pin = self._state.session_pin
        await self._send_text(f"\r\nhub [{pin}]> " if pin else "\r\nhub> ")

    # -- line reader: own echo (no PTY line discipline) ---------------------

    async def _read_line(self) -> tuple[str, Optional[str]]:
        """Returns ("line", text) | ("ctrl_c", None). Own echo; the handler
        feeds this queue only while in HUBMODE."""
        buf = bytearray()
        while True:
            chunk = await self._q.get()
            for b in chunk:
                if b in (0x0D, 0x0A):  # \r / \n submits
                    await self._send_bytes(b"\r\n")
                    return "line", bytes(buf).decode("utf-8", errors="replace")
                if b == 0x7F:  # DEL erases
                    if buf:
                        buf.pop()
                        await self._send_bytes(b"\b \b")
                elif b == 0x03:  # Ctrl+C
                    return "ctrl_c", None
                elif b < 0x20:
                    continue  # other control bytes ignored
                else:
                    buf.append(b)
                    await self._send_bytes(bytes([b]))

    # -- stream renderer (AD-53) --------------------------------------------

    async def _render(self, ev: Any) -> None:
        kind = ev.kind
        p = ev.payload
        if kind == "log":
            # Adapters yield answer chunks as `log` — raw passthrough.
            text = p if isinstance(p, str) else (p.get("text", "") if isinstance(p, dict) else str(p))
            await self._print(text)
        elif kind == "status":
            text = p if isinstance(p, str) else ((p or {}).get("text") or str(p))
            await self._print(f"{_DIM}{text}{_RESET}\r\n")
        elif kind == "tier_switch":
            to = (p or {}).get("to", {}) if isinstance(p, dict) else {}
            reason = (p or {}).get("reason", "") if isinstance(p, dict) else ""
            await self._print(
                f"{_DIM}→ {to.get('tier', '?')} ({to.get('model', '?')})"
                f"{f' — {reason}' if reason else ''}{_RESET}\r\n"
            )
        elif kind == "done":
            await self._render_done(p if isinstance(p, dict) else {})
        elif kind == "error":
            await self._render_error(p)
        # unknown kinds: ignored (forward-compatible)

    async def _render_done(self, payload: dict) -> None:
        if payload.get("ladder_empty"):
            n = len(payload.get("failover_attempts") or [])
            await self._print(f"🚫 All hub tiers exhausted ({n} attempts).\r\n")
            return
        if payload.get("status") == "model_unavailable":
            msg = ((payload.get("model_error") or {}).get("message")
                   or "model unavailable")
            await self._print(f"🚫 {msg}\r\n")
            return
        serving_line = payload.get("serving_line") or ""
        if serving_line:
            await self._print(f"\r\n{serving_line}\r\n")

    async def _render_error(self, payload: Any) -> None:
        if isinstance(payload, dict):
            code = payload.get("code", "error")
            msg = (payload.get("message") or "").strip()
            lines = [f"⚠️ {code}{': ' + msg if msg else ''}"]
            for s in payload.get("suggestions") or []:
                lines.append(f"  • {s}")
            await self._print("\r\n".join(lines) + "\r\n")
        else:
            await self._print(f"⚠️ {payload}\r\n")

    # -- question path: consumer + Ctrl+C race (AD-54) -----------------------

    async def _consume(self, agen: Any) -> None:
        try:
            async for ev in agen:
                await self._render(ev)
        finally:
            # Idempotent: already-exhausted generators close as a no-op.
            aclose = getattr(agen, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

    async def _wait_ctrl_c(self) -> None:
        while True:
            chunk = await self._q.get()
            if b"\x03" in chunk:
                return
            # Non-^C input arriving mid-stream is discarded (AD-54).

    async def _ask(self, line: str) -> None:
        request = HubRequest(
            prompt=line,
            skip_tiers=["agy"],  # structural: the tier is known-dead (AD-53)
            repo_ctx=None,
            model=self._state.session_pin,
            allow_model_fallback=True,
        )
        agen = self._hub.execute_stream(request)
        consumer = asyncio.create_task(self._consume(agen))
        watcher = asyncio.create_task(self._wait_ctrl_c())
        try:
            done, _pending = await asyncio.wait(
                {consumer, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if watcher in done:
                consumer.cancel()
                try:
                    await consumer
                except asyncio.CancelledError:
                    pass
                await self._print("^C")
            else:
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass
                # Any bytes the watcher ate mid-stream stay discarded (AD-54).
        finally:
            for t in (consumer, watcher):
                if not t.done():
                    t.cancel()

    # -- built-ins (T1-S4) ----------------------------------------------------

    async def _do_model(self, arg: str) -> None:
        arg = arg.strip()
        if not arg:
            pin = self._state.session_pin
            await self._print(f"model pin: {pin if pin else 'auto'}\r\n")
            return
        if arg.lower() in ("auto", "default"):
            self._state.session_pin = None
            await self._print("model pin cleared (auto ladder).\r\n")
            return
        canonical, suggestions = self._hub.resolve_model_pin(arg)
        if canonical:
            self._state.session_pin = canonical
            await self._print(f"model pin set: {canonical}\r\n")
        else:
            lines = [f"⚠️ no_match: no carrier serves '{arg}' — pin unchanged"]
            for s in suggestions or []:
                lines.append(f"  • {s}")
            await self._print("\r\n".join(lines) + "\r\n")

    async def _do_status(self) -> None:
        try:
            st = self._hub.status()
        except Exception as e:  # noqa: BLE001 — REPL never raises (FR-8)
            await self._print(f"⚠️ status unavailable: {e}\r\n")
            return
        tiers = st.get("tiers") or []
        parts = []
        for t in tiers:
            tid = t.get("id", "?")
            circuit = (t.get("breaker") or {}).get("state") or t.get("circuit") or "?"
            parts.append(f"{tid}: {circuit}")
        retired = st.get("retired_models") or []
        await self._print(
            "hub status:\r\n"
            f"  tiers: {' · '.join(parts) if parts else '?'}\r\n"
            f"  retired models: {len(retired)}\r\n"
        )

    async def _do_exit(self) -> Optional[str]:
        """Returns "respawn" | "stay" | None (None = keep the REPL running)."""
        est = self._state.reset_estimate
        if est:
            await self._print(
                f"Quota resets in {est}. Reconnect the agy TUI anyway? [y/N] "
            )
            kind, answer = await self._read_line()
            if kind == "line" and (answer or "").strip().lower() in ("y", "yes"):
                return "respawn"
            await self._print("staying in hub mode.\r\n")
            return None
        return "respawn"

    async def _builtin(self, line: str) -> Optional[str]:
        cmd, _, rest = line.partition(" ")
        if cmd == "/exit":
            return await self._do_exit()
        if cmd == "/model":
            await self._do_model(rest)
            return None
        if cmd == "/status":
            await self._do_status()
            return None
        if cmd == "/help":
            await self._print(help_text())
            return None
        await self._print("unknown hub command — /help\r\n")
        return None

    # -- main loop ------------------------------------------------------------

    async def run(self) -> str:
        """Runs the REPL. Returns "respawn" | "stay" (the handler acts on it).
        The handler cancels this task on websocket close (FR-8)."""
        await self._print(hubmode_banner())
        while True:
            await self._prompt()
            kind, payload = await self._read_line()
            if kind == "ctrl_c":
                await self._print("^C")
                continue
            line = (payload or "").strip()
            if not line:
                continue
            try:
                if line.startswith("/"):
                    action = await self._builtin(line)
                    if action in ("respawn", "stay"):
                        return action
                    continue
                await self._ask(line)
            except Exception as e:  # noqa: BLE001 — REPL never raises (FR-8)
                await self._print(f"⚠️ hub error: {e}\r\n")


async def run_hub_repl(
    send_text: SendText,
    send_bytes: SendBytes,
    hub_q: "asyncio.Queue[bytes]",
    model_hub: Any,
    state: HubTermState,
) -> str:
    """Module entry point used by the websocket handler (AD-52)."""
    return await HubRepl(send_text, send_bytes, hub_q, model_hub, state).run()
