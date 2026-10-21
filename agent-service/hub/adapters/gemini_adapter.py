"""Tier 1 — Google AI Studio adapter (answer_only), E1-S5, AD-8.

Uses the native ``google-genai`` SDK (only new dependency) against
``generativelanguage.googleapis.com/v1beta``. Lazy ``google.genai`` import so
unit tests and tiers without the package never pay for it.

Exhaustion signals (§6):
  HTTP 429 + RESOURCE_EXHAUSTED/quotaExceeded (RPD) -> EXHAUSTED
  429 with Retry-After <= 30s (RPM)              -> RETRYABLE_429
  401/403 -> AUTH_FAULT ; 400 -> BAD_REQUEST ; 5xx/timeout -> TRANSIENT

Data-use note: free-tier AI Studio prompts may train Google models
(config ``data_use: may-train-google-models``; never send secrets in prompts).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncIterator

from hub.adapters.base import CapabilityMode, ProviderAdapter
from hub.signals import http_outcome, parse_quota_hints, parse_retry_after
from hub.types import AdapterResponse, AttemptOutcome, HubEvent, TierConfig


def _require_genai():
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-genai is not installed (add it to requirements.txt)"
        ) from e
    return genai, types


def _exc_details(exc: BaseException) -> tuple[int | None, str, dict]:
    """Best-effort (status_code, body_text, headers) extraction from SDK errors."""
    code = getattr(exc, "code", None)
    headers: dict = {}
    resp = getattr(exc, "response", None)
    if resp is not None:
        raw_headers = getattr(resp, "headers", None)
        if raw_headers:
            try:
                headers = dict(raw_headers)
            except Exception:
                headers = {}
        if code is None:
            code = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    body = getattr(exc, "message", None) or str(exc)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return code, body, headers


def _extract_usage(response: Any) -> dict:
    usage: dict = {}
    meta = getattr(response, "usage_metadata", None)
    if meta is not None:
        in_tok = getattr(meta, "prompt_token_count", None)
        out_tok = getattr(meta, "candidates_token_count", None)
        if in_tok is not None:
            usage["input_tokens"] = in_tok
        if out_tok is not None:
            usage["output_tokens"] = out_tok
    return usage


class GeminiAdapter(ProviderAdapter):
    name = "gemini"
    capability = CapabilityMode.ANSWER_ONLY

    def _client(self, tier: TierConfig):
        genai, _ = _require_genai()
        api_key = os.environ.get(tier.api_key_env or "", "") if tier.api_key_env else ""
        if not api_key:
            raise RuntimeError(f"missing API key for tier '{tier.id}' ({tier.api_key_env})")
        return genai.Client(api_key=api_key)

    def parse_exhaustion(self, exc_or_response: Any) -> AttemptOutcome:
        if isinstance(exc_or_response, BaseException):
            code, body, headers = _exc_details(exc_or_response)
            if code == 429:
                # RPM (has usable Retry-After) vs RPD distinction
                ra = parse_retry_after(headers)
                if ra is not None and 0 < ra <= 30:
                    return AttemptOutcome.RETRYABLE_429
                return AttemptOutcome.EXHAUSTED
            return http_outcome(code, body, headers, provider="google-ai-studio")
        # non-exception passthroughs are treated as opaque failures
        return http_outcome(None, str(exc_or_response), {}, provider="google-ai-studio")

    async def run(
        self,
        prompt: str,
        model: str,
        tier: TierConfig,
        timeout_s: int,
        max_output_tokens: int,
        repo_ctx: Any = None,
    ) -> tuple[AttemptOutcome, Any]:
        _, types = _require_genai()
        started = time.monotonic()
        try:
            client = self._client(tier)
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        max_output_tokens=max_output_tokens,
                        temperature=0.2,
                    ),
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return AttemptOutcome.TRANSIENT, "ai-studio request timed out"
        except Exception as e:  # noqa: BLE001 - mapped to AttemptOutcome, never raised past router
            outcome = self.parse_exhaustion(e)
            if outcome == AttemptOutcome.RETRYABLE_429:
                _, _, headers = _exc_details(e)
                return outcome, {"retry_after_s": parse_retry_after(headers),
                                 "detail": str(e)[-500:]}
            return outcome, str(e)[-500:]

        latency_ms = int((time.monotonic() - started) * 1000)
        text = getattr(response, "text", "") or ""
        return AttemptOutcome.SUCCESS, AdapterResponse(
            text=text,
            model=model,
            usage=_extract_usage(response),
            latency_ms=latency_ms,
        )

    async def run_stream(
        self,
        prompt: str,
        model: str,
        tier: TierConfig,
        timeout_s: int,
        max_output_tokens: int,
        repo_ctx: Any = None,
    ) -> AsyncIterator[HubEvent]:
        _, types = _require_genai()
        started = time.monotonic()
        try:
            client = self._client(tier)
            stream = await client.aio.models.generate_content_stream(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_output_tokens,
                    temperature=0.2,
                ),
            )
            chunks: list[str] = []
            async for chunk in stream:
                piece = getattr(chunk, "text", "") or ""
                if piece:
                    chunks.append(piece)
                    yield HubEvent(kind="log", payload=piece)
            latency_ms = int((time.monotonic() - started) * 1000)
            yield HubEvent(
                kind="done",
                payload={
                    "outcome": AttemptOutcome.SUCCESS,
                    "response": AdapterResponse(
                        text="".join(chunks), model=model, latency_ms=latency_ms
                    ),
                    "latency_ms": latency_ms,
                },
            )
        except asyncio.TimeoutError:
            yield HubEvent(kind="done", payload={"outcome": AttemptOutcome.TRANSIENT,
                                                 "response": "ai-studio stream timed out",
                                                 "latency_ms": 0})
        except Exception as e:  # noqa: BLE001
            outcome = self.parse_exhaustion(e)
            if outcome == AttemptOutcome.RETRYABLE_429:
                _, _, headers = _exc_details(e)
                payload: Any = {"retry_after_s": parse_retry_after(headers),
                                "detail": str(e)[-500:]}
            else:
                payload = str(e)[-500:]
            yield HubEvent(kind="done", payload={"outcome": outcome,
                                                 "response": payload,
                                                 "latency_ms": 0})

    @staticmethod
    def quota_hints_from_exc(exc: BaseException) -> dict:
        _, _, headers = _exc_details(exc)
        return parse_quota_hints(headers)
