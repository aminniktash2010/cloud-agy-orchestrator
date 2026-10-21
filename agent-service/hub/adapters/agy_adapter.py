"""Tier 0 — Antigravity CLI adapter (agent_loop), E1-S4, AD-2.

Reuses today's invocation path verbatim (``agy -p … --mode accept-edits
--dangerously-skip-permissions --model <m>``), PTY or pipe, isolated env.
The model list comes from config, not ``ACTIVE_QUOTA_MODELS``.

Repo preparation (``prepare_cached_repo`` / ``build_isolated_env``) stays in
the FastAPI handler and arrives via ``repo_ctx``.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any, AsyncIterator

from hub.adapters.base import CapabilityMode, ProviderAdapter
from hub.signals import agy_outcome
from hub.types import AdapterResponse, AttemptOutcome, HubEvent, RepoContext, TierConfig


class AgyAdapter(ProviderAdapter):
    name = "agy"
    capability = CapabilityMode.AGENT_LOOP

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _cmd_args(prompt: str, model: str, repo_ctx: RepoContext,
                  conversation_id: str | None, continue_session: bool) -> list[str]:
        cmd_args = [
            "agy", "-p", prompt,
            "--mode", "accept-edits",
            "--dangerously-skip-permissions",
            "--model", model,
        ]
        if conversation_id:
            cmd_args.extend(["--conversation", conversation_id])
        elif continue_session and repo_ctx.has_active_session:
            cmd_args.append("-c")
        return cmd_args

    # ------------------------------------------------------------------
    # collect mode
    # ------------------------------------------------------------------
    async def run(
        self,
        prompt: str,
        model: str,
        tier: TierConfig,
        timeout_s: int,
        max_output_tokens: int,
        repo_ctx: Any = None,
    ) -> tuple[AttemptOutcome, Any]:
        if repo_ctx is None:
            return AttemptOutcome.BAD_REQUEST, "AgyAdapter requires repo_ctx (repo prep is the handler's job)"
        repo_dir = Path(repo_ctx.repo_dir)
        cmd_args = self._cmd_args(prompt, model, repo_ctx, None, True)

        started = time.monotonic()
        try:
            # run blocking subprocess in a thread so the event loop stays alive
            agy_proc = await asyncio.to_thread(
                subprocess.run,
                cmd_args,
                cwd=str(repo_dir),
                env=repo_ctx.env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            output_str = (agy_proc.stdout or "") + (agy_proc.stderr or "")
            outcome = agy_outcome(agy_proc.returncode, output_str)
        except subprocess.TimeoutExpired:
            return AttemptOutcome.TRANSIENT, "agy timed out"
        latency_ms = int((time.monotonic() - started) * 1000)

        if outcome == AttemptOutcome.SUCCESS:
            return outcome, AdapterResponse(
                text=agy_proc.stdout or "", model=model, latency_ms=latency_ms
            )
        return outcome, output_str[-500:]

    # ------------------------------------------------------------------
    # streaming mode (PTY, mirrors today's /chat/stream banners)
    # ------------------------------------------------------------------
    async def run_stream(
        self,
        prompt: str,
        model: str,
        tier: TierConfig,
        timeout_s: int,
        max_output_tokens: int,
        repo_ctx: Any = None,
    ) -> AsyncIterator[HubEvent]:
        if repo_ctx is None:
            yield HubEvent(kind="error", payload="AgyAdapter requires repo_ctx")
            return

        repo_dir = Path(repo_ctx.repo_dir)
        session_label = (
            "Resuming Last Active Session"
            if repo_ctx.has_active_session
            else "Fresh Isolated Session"
        )
        yield HubEvent(
            kind="status",
            payload=f"Running Antigravity CLI [{repo_ctx.cache_tier}] with '{model}' (Autopilot)...",
        )
        yield HubEvent(kind="log", payload="\x1b[1;36m====================================================\x1b[0m\r\n")
        yield HubEvent(kind="log", payload="  \x1b[1;36m🚀 ANTIGRAVITY CLOUD CLI SESSION (PTY Enabled)\x1b[0m\r\n")
        yield HubEvent(kind="log", payload=f"  \x1b[1;33mModel:\x1b[0m      {model} \x1b[1;32m[Autopilot & Full Permissions]\x1b[0m\r\n")
        yield HubEvent(kind="log", payload=f"  \x1b[1;33mSession:\x1b[0m    {session_label} \x1b[1;32m[Conflict-Free Isolation]\x1b[0m\r\n")
        yield HubEvent(kind="log", payload=f"  \x1b[1;33mCache Tier:\x1b[0m {repo_ctx.cache_tier}\r\n")
        yield HubEvent(kind="log", payload="\x1b[1;36m====================================================\x1b[0m\r\n\r\n")

        cmd_args = self._cmd_args(prompt, model, repo_ctx, None, True)

        pty_env = dict(repo_ctx.env)
        pty_env["TERM"] = "xterm-256color"
        pty_env["COLORTERM"] = "truecolor"
        pty_env["FORCE_COLOR"] = "1"
        pty_env["PYTHONUNBUFFERED"] = "1"

        use_pty = False
        master_fd = None
        slave_fd = None
        try:
            master_fd, slave_fd = pty.openpty()
            winsize = struct.pack("HHHH", 32, 120, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
            use_pty = True
        except Exception:
            use_pty = False

        collected: list[str] = []
        return_code = 0
        started = time.monotonic()

        if use_pty:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=str(repo_dir),
                env=pty_env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()

            def on_readable():
                try:
                    chunk = os.read(master_fd, 1024)
                    if chunk:
                        q.put_nowait(chunk)
                    else:
                        loop.remove_reader(master_fd)
                        q.put_nowait(None)
                except OSError:
                    try:
                        loop.remove_reader(master_fd)
                    except Exception:
                        pass
                    q.put_nowait(None)

            loop.add_reader(master_fd, on_readable)

            while True:
                item = await q.get()
                if item is None:
                    break
                text = item.decode("utf-8", errors="replace")
                collected.append(text)
                yield HubEvent(kind="log", payload=text)

            try:
                return_code = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                return_code = 124
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            try:
                os.close(master_fd)
            except Exception:
                pass
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=str(repo_dir),
                env=pty_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                while True:
                    chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=timeout_s)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    collected.append(text)
                    yield HubEvent(kind="log", payload=text)
                return_code = await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                return_code = 124

        output_str = "".join(collected)
        latency_ms = int((time.monotonic() - started) * 1000)
        outcome = agy_outcome(return_code, output_str)
        if outcome == AttemptOutcome.SUCCESS:
            payload = AdapterResponse(text=output_str, model=model, latency_ms=latency_ms)
        else:
            payload = output_str[-2000:]
        yield HubEvent(kind="done", payload={"outcome": outcome, "response": payload,
                                             "latency_ms": latency_ms})

    # ------------------------------------------------------------------
    def parse_exhaustion(self, exc_or_response: Any) -> AttemptOutcome:
        if isinstance(exc_or_response, tuple) and len(exc_or_response) == 2:
            return exc_or_response[0]
        return agy_outcome(1, str(exc_or_response))
