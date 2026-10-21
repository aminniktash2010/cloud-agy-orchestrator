"""OpenAI-compatible adapter (answer_only) — parameterized httpx client.

Covers Epic 2 tiers (Groq, NVIDIA Build, OpenRouter) and Epic 4 (xAI) from a
single ~100-line httpx implementation (AD-8: no ``openai`` package, keeps the
Tier-1 container layer slim). All per-tier parameters come from config:
``base_url``, ``api_key_env``, ``extra_headers`` (OpenRouter HTTP-Referer /
X-Title), ``free_only`` (OpenRouter: rejects model ids not ending ``:free``
so ``openrouter/auto`` can never route paid).

Data-use ToS per provider is verified in Epic 2 (E2-S4) and recorded in the
config ``data_use`` field + Drive footer; see ``hub/observability.py``.

Epic 1 scope: registry scaffolding + graceful skip-on-missing-key. Live
Groq/NVIDIA/OpenRouter wiring (keys, model pinning) lands in Epic 2.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator

import httpx

from hub.adapters.base import CapabilityMode, ProviderAdapter
from hub.signals import http_outcome, parse_quota_hints
from hub.types import AdapterResponse, AttemptOutcome, HubEvent, TierConfig


class OpenAICompatAdapter(ProviderAdapter):
    name = "openai_compat"
    capability = CapabilityMode.ANSWER_ONLY

    # ------------------------------------------------------------------
    def _api_key(self, tier: TierConfig) -> str:
        if not tier.api_key_env:
            return ""
        return os.environ.get(tier.api_key_env, "").strip()

    def _headers(self, tier: TierConfig) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        headers.update(tier.extra_headers or {})
        return headers

    def _payload(self, prompt: str, model: str, max_output_tokens: int, stream: bool) -> dict:
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_output_tokens,
            "stream": stream,
        }

    def _check_free_only(self, tier: TierConfig, model: str) -> str | None:
        """Returns a skip reason when free_only policy rejects the model id."""
        if tier.free_only and not model.endswith(":free"):
            return f"free_only policy: model '{model}' does not end with ':free'"
        return None

    # ------------------------------------------------------------------
    def parse_exhaustion(self, exc_or_response: Any) -> AttemptOutcome:
        if isinstance(exc_or_response, httpx.HTTPStatusError):
            resp = exc_or_response.response
            return http_outcome(resp.status_code, resp.text, dict(resp.headers),
                                provider="openai-compat")
        if isinstance(exc_or_response, httpx.TimeoutException):
            return AttemptOutcome.TRANSIENT
        if isinstance(exc_or_response, httpx.HTTPError):
            return AttemptOutcome.TRANSIENT
        return http_outcome(None, str(exc_or_response), {}, provider="openai-compat")

    @staticmethod
    def quota_hints_from_response(resp: httpx.Response) -> dict:
        return parse_quota_hints(dict(resp.headers))

    # ------------------------------------------------------------------
    async def run(
        self,
        prompt: str,
        model: str,
        tier: TierConfig,
        timeout_s: int,
        max_output_tokens: int,
        repo_ctx: Any = None,
        conversation_id: str | None = None,  # noqa: ARG002 - answer_only: no sessions
        continue_session: bool = True,  # noqa: ARG002
    ) -> tuple[AttemptOutcome, Any]:
        skip = self._check_free_only(tier, model)
        if skip:
            return AttemptOutcome.UNKNOWN_MODEL, skip  # next model within tier

        api_key = self._api_key(tier)
        if not api_key:
            return AttemptOutcome.AUTH_FAULT, f"missing API key ({tier.api_key_env})"

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(
                    f"{(tier.base_url or '').rstrip('/')}/chat/completions",
                    headers={**self._headers(tier), "Authorization": f"Bearer {api_key}"},
                    json=self._payload(prompt, model, max_output_tokens, stream=False),
                )
        except httpx.TimeoutException:
            return AttemptOutcome.TRANSIENT, "request timed out"
        except httpx.HTTPError as e:
            return self.parse_exhaustion(e), str(e)[-500:]

        latency_ms = int((time.monotonic() - started) * 1000)
        hints = self.quota_hints_from_response(resp)

        if resp.status_code == 200:
            try:
                data = resp.json()
            except json.JSONDecodeError:
                return AttemptOutcome.BAD_REQUEST, "non-JSON 200 response"
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                return AttemptOutcome.BAD_REQUEST, "unexpected response shape"
            usage: dict = {}
            raw_usage = data.get("usage") or {}
            if "prompt_tokens" in raw_usage:
                usage["input_tokens"] = raw_usage["prompt_tokens"]
            if "completion_tokens" in raw_usage:
                usage["output_tokens"] = raw_usage["completion_tokens"]
            return AttemptOutcome.SUCCESS, AdapterResponse(
                text=text, model=model, usage=usage,
                quota_hints=hints, latency_ms=latency_ms,
            )

        outcome = self.parse_exhaustion(
            httpx.HTTPStatusError("error", request=resp.request, response=resp)
        )
        if outcome == AttemptOutcome.RETRYABLE_429:
            # surface retry info; the router decides the single delayed retry
            return outcome, {"retry_after_s": hints.get("retry-after"), "body": resp.text[-500:]}
        return outcome, resp.text[-500:]

    # ------------------------------------------------------------------
    async def run_stream(
        self,
        prompt: str,
        model: str,
        tier: TierConfig,
        timeout_s: int,
        max_output_tokens: int,
        repo_ctx: Any = None,
        conversation_id: str | None = None,  # noqa: ARG002 - answer_only: no sessions
        continue_session: bool = True,  # noqa: ARG002
    ) -> AsyncIterator[HubEvent]:
        skip = self._check_free_only(tier, model)
        if skip:
            yield HubEvent(kind="done", payload={"outcome": AttemptOutcome.UNKNOWN_MODEL,
                                                 "response": skip, "latency_ms": 0})
            return

        api_key = self._api_key(tier)
        if not api_key:
            yield HubEvent(kind="done", payload={"outcome": AttemptOutcome.AUTH_FAULT,
                                                 "response": f"missing API key ({tier.api_key_env})",
                                                 "latency_ms": 0})
            return

        started = time.monotonic()
        chunks: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                async with client.stream(
                    "POST",
                    f"{(tier.base_url or '').rstrip('/')}/chat/completions",
                    headers={**self._headers(tier), "Authorization": f"Bearer {api_key}"},
                    json=self._payload(prompt, model, max_output_tokens, stream=True),
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        outcome = http_outcome(resp.status_code, body.decode("utf-8", "replace"),
                                               dict(resp.headers), provider="openai-compat")
                        yield HubEvent(kind="done",
                                       payload={"outcome": outcome,
                                                "response": body.decode("utf-8", "replace")[-500:],
                                                "latency_ms": 0})
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            delta = obj["choices"][0]["delta"].get("content") or ""
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            continue
                        if delta:
                            chunks.append(delta)
                            yield HubEvent(kind="log", payload=delta)
        except httpx.TimeoutException:
            yield HubEvent(kind="done", payload={"outcome": AttemptOutcome.TRANSIENT,
                                                 "response": "stream timed out", "latency_ms": 0})
            return
        except httpx.HTTPError as e:
            yield HubEvent(kind="done", payload={"outcome": self.parse_exhaustion(e),
                                                 "response": str(e)[-500:], "latency_ms": 0})
            return

        latency_ms = int((time.monotonic() - started) * 1000)
        yield HubEvent(kind="done", payload={
            "outcome": AttemptOutcome.SUCCESS,
            "response": AdapterResponse(text="".join(chunks), model=model, latency_ms=latency_ms),
            "latency_ms": latency_ms,
        })
