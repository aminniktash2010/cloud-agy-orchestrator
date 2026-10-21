"""ModelHub — policy-driven router over the plugin adapter bus (E1-S7).

``execute()`` (collect) and ``execute_stream()`` (async generator) are the
only ways to run a model task (AD-1). The router walks the priority ladder,
fails over within a tier across models, then across tiers, recording every
attempt. Adapters never raise provider SDK errors past the router (NFR-3):
anything unexpected becomes a HubError (surface) or TRANSIENT (advance).

Staging hook: ``MODEL_HUB_FORCE_EXHAUST_TIER=<tier-id>`` simulates exhaustion
for one tier (dev/staging only; never in the default config).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, AsyncIterator

from hub.adapters import ADAPTER_REGISTRY
from hub.adapters.base import ProviderAdapter
from hub.breaker import CircuitBreaker
from hub.config import ConfigLoader
from hub.observability import (
    advisory_banner,
    log_attempt,
    serving_line,
    status_payload,
)
from hub.quota import QuotaTracker, SpendLedger, truncate_prompt
from hub.signals import parse_retry_after
from hub.types import (
    AdapterResponse,
    AttemptOutcome,
    CapabilityMode,
    HubConfig,
    HubEvent,
    HubRequest,
    HubResult,
    ModelAttempt,
    TierConfig,
)

logger = logging.getLogger("model-hub.router")

FORCE_EXHAUST_ENV = "MODEL_HUB_FORCE_EXHAUST_TIER"


class HubError(Exception):
    """Surfaced errors: auth fault on required tiers, bad requests, config bugs."""


class ModelHub:
    def __init__(
        self,
        config_loader: ConfigLoader | None = None,
        breaker: CircuitBreaker | None = None,
        quota: QuotaTracker | None = None,
        ledger: SpendLedger | None = None,
    ) -> None:
        self._config_loader = config_loader or ConfigLoader()
        self._breaker: CircuitBreaker | None = breaker
        self._quota = quota or QuotaTracker()
        self._ledger = ledger or SpendLedger()
        self._quota_hints: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _config(self) -> HubConfig:
        cfg = self._config_loader.get()
        if self._breaker is None:
            b = cfg.breaker
            self._breaker = CircuitBreaker(b.exhaustions_to_open, b.window_s, b.cooldown_s)
        else:
            b = cfg.breaker
            self._breaker.configure(b.exhaustions_to_open, b.window_s, b.cooldown_s)
        return cfg

    @staticmethod
    def _force_exhaust_tier() -> str:
        return os.environ.get(FORCE_EXHAUST_ENV, "").strip()

    def _models_for_tier(self, tier: TierConfig, request: HubRequest) -> list[str]:
        models = list(tier.models or [])
        override = (request.requested_model_override or "").strip()
        # Tier 0 honors the legacy ChatCommand.model: non-"auto" pins the first attempt
        if tier.id == "agy" and override and override.lower() not in ("auto", "", "default"):
            models = [override] + [m for m in models if m != override]
        return models

    def _skip_reason(self, tier: TierConfig, cfg: HubConfig) -> str | None:
        """Returns a skip reason, or None when the tier may be attempted."""
        if not tier.enabled:
            return "disabled"
        assert self._breaker is not None
        if self._breaker.is_open(tier.id):
            return "circuit_open"
        if tier.paid_gate and not cfg.paid.enabled:
            return "paid_disabled"
        if tier.api_key_env and not os.environ.get(tier.api_key_env, "").strip():
            return "missing_key"
        if not self._quota.check_cap(tier.id, tier.daily_request_cap):
            return "cap_reached"
        if tier.paid_gate and tier.cost == "paid":
            est = SpendLedger.estimate_cost_usd(
                4000, tier.per_task_max_output_tokens,
                tier.price_per_1m_in_usd, tier.price_per_1m_out_usd,
            )
            if not self._ledger.can_spend(est, cfg.paid.daily_spend_cap_usd):
                return "spend_cap_reached"
        return None

    def _record(self, attempts: list[ModelAttempt], tier: TierConfig, model: str,
                outcome: str, reason: str = "", latency_ms: int = 0,
                usage: dict | None = None, quota_hints: dict | None = None) -> ModelAttempt:
        attempt = ModelAttempt(
            tier=tier.id, provider=tier.provider, model=model, outcome=outcome,
            reason=reason, latency_ms=latency_ms,
            usage=usage or {}, quota_hints=quota_hints or {},
        )
        attempts.append(attempt)
        if attempt.quota_hints:
            self._quota_hints[tier.id] = dict(attempt.quota_hints)
        log_attempt(attempt)
        return attempt

    @staticmethod
    def _answer_text(resp: Any) -> str:
        if isinstance(resp, AdapterResponse):
            return resp.text
        if isinstance(resp, dict):
            return str(resp.get("detail", resp.get("body", resp)))
        return str(resp)

    # ------------------------------------------------------------------
    # /hub/status support (no secrets, ever)
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """Payload for GET /hub/status (AC-10)."""
        cfg = self._config()
        key_status: dict[str, bool] = {}
        for tier in cfg.tiers_sorted():
            adapter_cls = ADAPTER_REGISTRY.get(tier.adapter)
            if adapter_cls is None:
                key_status[tier.id] = False
                continue
            try:
                key_status[tier.id] = adapter_cls().key_configured(tier)
            except Exception:  # noqa: BLE001
                key_status[tier.id] = False
        assert self._breaker is not None
        return status_payload(
            cfg,
            config_source=self._config_loader.source,
            breaker_snapshot=self._breaker.snapshot(),
            quota_usage={t.id: self._quota.usage_today(t.id) for t in cfg.tiers},
            quota_hints=dict(self._quota_hints),
            key_status=key_status,
            spend_today_usd=self._ledger.spend_today_usd(),
        )

    # ------------------------------------------------------------------
    # collect
    # ------------------------------------------------------------------
    async def execute(self, request: HubRequest) -> HubResult:
        cfg = self._config()
        prompt, truncated = truncate_prompt(request.prompt, cfg.global_.max_input_chars)
        attempts: list[ModelAttempt] = []
        force_exhaust = self._force_exhaust_tier()
        if truncated:
            logger.info("hub prompt truncated to %d chars", cfg.global_.max_input_chars)

        for tier in cfg.tiers_sorted():
            skip = self._skip_reason(tier, cfg)
            if skip:
                self._record(attempts, tier, "", "skipped", f"skip: {skip}")
                continue

            wait_s = self._quota.pacing_wait_s(tier.id, tier.rpm_pacing_s)
            if wait_s > 0:
                await asyncio.sleep(wait_s)

            adapter_cls = ADAPTER_REGISTRY.get(tier.adapter)
            if adapter_cls is None:
                raise HubError(f"unknown adapter '{tier.adapter}' for tier '{tier.id}'")
            adapter: ProviderAdapter = adapter_cls()
            models = self._models_for_tier(tier, request)
            if not models:
                self._record(attempts, tier, "", "skipped", "skip: no models configured")
                continue

            tier_had_attempts = False
            for model in models:
                tier_had_attempts = True
                started = time.monotonic()
                if force_exhaust == tier.id:
                    outcome, resp = AttemptOutcome.EXHAUSTED, "forced by MODEL_HUB_FORCE_EXHAUST_TIER"
                else:
                    try:
                        outcome, resp = await adapter.run(
                            prompt, model, tier,
                            timeout_s=tier.per_task_timeout_s,
                            max_output_tokens=tier.per_task_max_output_tokens,
                            repo_ctx=request.repo_ctx,
                        )
                    except HubError:
                        raise
                    except Exception as e:  # noqa: BLE001 - adapter contract breach -> transient
                        logger.exception("adapter '%s' raised past contract", tier.adapter)
                        outcome, resp = AttemptOutcome.TRANSIENT, f"adapter error: {e}"[:500]

                latency_ms = int((time.monotonic() - started) * 1000)
                if outcome == AttemptOutcome.SUCCESS and isinstance(resp, AdapterResponse):
                    latency_ms = resp.latency_ms or latency_ms
                self._quota.account_attempt(tier.id)

                if outcome == AttemptOutcome.SUCCESS:
                    assert self._breaker is not None
                    self._breaker.note_success(tier.id)
                    self._record(attempts, tier, model, outcome.value, "",
                                 latency_ms,
                                 usage=resp.usage if isinstance(resp, AdapterResponse) else {},
                                 quota_hints=resp.quota_hints if isinstance(resp, AdapterResponse) else {})
                    return self._winner(tier, model, resp, attempts, truncated)

                if outcome == AttemptOutcome.RETRYABLE_429:
                    ra = self._retry_after_s(resp)
                    if ra is not None:
                        self._record(attempts, tier, model, outcome.value,
                                     f"retry-after {ra}s: single delayed retry", latency_ms)
                        await asyncio.sleep(min(ra, 30))
                        outcome2, resp2 = await self._retry_once(
                            adapter, prompt, model, tier, request)
                        latency_ms = int((time.monotonic() - started) * 1000)
                        self._quota.account_attempt(tier.id)
                        if outcome2 == AttemptOutcome.SUCCESS:
                            assert self._breaker is not None
                            self._breaker.note_success(tier.id)
                            self._record(attempts, tier, model, outcome2.value, "retry succeeded",
                                         latency_ms)
                            return self._winner(tier, model, resp2, attempts, truncated)
                        outcome, resp = outcome2, resp2
                    else:
                        outcome = AttemptOutcome.EXHAUSTED

                reason = self._answer_text(resp)[:300]
                self._record(attempts, tier, model, outcome.value, reason, latency_ms)

                if outcome == AttemptOutcome.UNKNOWN_MODEL:
                    continue  # next model, same tier
                if outcome in (AttemptOutcome.EXHAUSTED, AttemptOutcome.TRANSIENT):
                    break  # next tier
                if outcome == AttemptOutcome.AUTH_FAULT:
                    if tier.optional:
                        logger.warning("tier '%s' auth fault (optional) — skipping tier", tier.id)
                        break
                    raise HubError(f"auth fault on required tier '{tier.id}': {reason}")
                if outcome == AttemptOutcome.BAD_REQUEST:
                    raise HubError(f"bad request on tier '{tier.id}' model '{model}': {reason}")
                break

            # No winner from this tier -> count toward the circuit breaker
            if tier_had_attempts:
                assert self._breaker is not None
                self._breaker.note_exhaustion(tier.id)

        return HubResult(status="ladder_empty", ladder_empty=True, failover_attempts=attempts)

    async def _retry_once(self, adapter: ProviderAdapter, prompt: str, model: str,
                          tier: TierConfig, request: HubRequest):
        try:
            return await adapter.run(
                prompt, model, tier,
                timeout_s=tier.per_task_timeout_s,
                max_output_tokens=tier.per_task_max_output_tokens,
                repo_ctx=request.repo_ctx,
            )
        except Exception as e:  # noqa: BLE001
            return AttemptOutcome.TRANSIENT, f"retry error: {e}"[:500]

    @staticmethod
    def _retry_after_s(resp: Any) -> float | None:
        if isinstance(resp, dict):
            ra = resp.get("retry_after_s")
            try:
                ra_f = float(ra) if ra is not None else None
            except (TypeError, ValueError):
                ra_f = None
            if ra_f is not None and 0 < ra_f <= 30:
                return ra_f
        return None

    def _winner(self, tier: TierConfig, model: str, resp: Any,
                attempts: list[ModelAttempt], truncated: bool) -> HubResult:
        answer = self._answer_text(resp)
        banner = ""
        if tier.capability == CapabilityMode.ANSWER_ONLY:
            banner = advisory_banner(tier.provider, model)
        result = HubResult(
            status="success",
            answer=answer,
            tier_used=tier.id,
            provider=tier.provider,
            model=model,
            capability_mode=tier.capability.value,
            failover_attempts=attempts,
            advisory_banner=banner,
            serving_line="",
        )
        result.serving_line = serving_line(result)
        return result

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------
    async def execute_stream(self, request: HubRequest) -> AsyncIterator[HubEvent]:
        cfg = self._config()
        prompt, truncated = truncate_prompt(request.prompt, cfg.global_.max_input_chars)
        attempts: list[ModelAttempt] = []
        force_exhaust = self._force_exhaust_tier()
        prev_tier: TierConfig | None = None

        for tier in cfg.tiers_sorted():
            skip = self._skip_reason(tier, cfg)
            if skip:
                self._record(attempts, tier, "", "skipped", f"skip: {skip}")
                continue

            if prev_tier is not None and prev_tier.id != tier.id:
                yield HubEvent(kind="tier_switch", payload={
                    "from": {"tier": prev_tier.id, "provider": prev_tier.provider},
                    "to": {"tier": tier.id, "provider": tier.provider,
                           "model": (tier.models or [""])[0]},
                    "reason": f"tier {prev_tier.id} exhausted",
                    "capability_mode": tier.capability.value,
                })

            wait_s = self._quota.pacing_wait_s(tier.id, tier.rpm_pacing_s)
            if wait_s > 0:
                await asyncio.sleep(wait_s)

            adapter_cls = ADAPTER_REGISTRY.get(tier.adapter)
            if adapter_cls is None:
                raise HubError(f"unknown adapter '{tier.adapter}' for tier '{tier.id}'")
            adapter: ProviderAdapter = adapter_cls()
            models = self._models_for_tier(tier, request)
            if not models:
                self._record(attempts, tier, "", "skipped", "skip: no models configured")
                continue

            tier_had_attempts = False
            live = tier.capability == CapabilityMode.AGENT_LOOP  # forward live, else buffer
            for model in models:
                tier_had_attempts = True
                started = time.monotonic()
                buffered: list[HubEvent] = []
                outcome: AttemptOutcome = AttemptOutcome.TRANSIENT
                resp: Any = ""
                quota_hints: dict = {}

                if force_exhaust == tier.id:
                    outcome, resp = AttemptOutcome.EXHAUSTED, "forced by MODEL_HUB_FORCE_EXHAUST_TIER"
                else:
                    try:
                        async for ev in adapter.run_stream(
                            prompt, model, tier,
                            timeout_s=tier.per_task_timeout_s,
                            max_output_tokens=tier.per_task_max_output_tokens,
                            repo_ctx=request.repo_ctx,
                        ):
                            if ev.kind == "done":
                                payload = ev.payload or {}
                                outcome = payload.get("outcome", AttemptOutcome.TRANSIENT)
                                resp = payload.get("response", "")
                                if isinstance(resp, AdapterResponse):
                                    quota_hints = resp.quota_hints
                            elif live:
                                yield ev
                            else:
                                buffered.append(ev)
                    except Exception as e:  # noqa: BLE001
                        logger.exception("adapter '%s' stream raised past contract", tier.adapter)
                        outcome, resp = AttemptOutcome.TRANSIENT, f"adapter error: {e}"[:500]

                latency_ms = int((time.monotonic() - started) * 1000)
                if isinstance(resp, AdapterResponse) and resp.latency_ms:
                    latency_ms = resp.latency_ms
                self._quota.account_attempt(tier.id)

                if outcome == AttemptOutcome.RETRYABLE_429:
                    ra = self._retry_after_s(resp)
                    if ra is not None:
                        self._record(attempts, tier, model, outcome.value,
                                     f"retry-after {ra}s: single delayed retry", latency_ms)
                        yield HubEvent(kind="status", payload={
                            "text": f"Rate-limited on '{model}'; retrying once in {ra:.0f}s…"})
                        await asyncio.sleep(min(ra, 30))
                        # single delayed retry via collect path (answer tiers are short)
                        outcome, resp = await self._retry_once(adapter, prompt, model, tier, request)
                        latency_ms = int((time.monotonic() - started) * 1000)
                        self._quota.account_attempt(tier.id)
                        if outcome == AttemptOutcome.SUCCESS and not live:
                            buffered = []  # retry answer replaces buffered chunks
                    else:
                        outcome = AttemptOutcome.EXHAUSTED

                if outcome == AttemptOutcome.SUCCESS:
                    assert self._breaker is not None
                    self._breaker.note_success(tier.id)
                    usage = resp.usage if isinstance(resp, AdapterResponse) else {}
                    self._record(attempts, tier, model, outcome.value, "", latency_ms,
                                 usage=usage, quota_hints=quota_hints)
                    result = self._winner(tier, model, resp, attempts, truncated)
                    if not live:
                        # buffered answer_only: banner first, then the chunks
                        if result.advisory_banner:
                            yield HubEvent(kind="log", payload=result.advisory_banner)
                        for ev in buffered:
                            yield ev
                        if isinstance(resp, AdapterResponse) and not buffered:
                            # collect-path retry produced the answer after streaming failed
                            yield HubEvent(kind="log", payload=resp.text)
                    yield HubEvent(kind="done", payload=result.model_dump())
                    return

                reason = self._answer_text(resp)[:300]
                self._record(attempts, tier, model, outcome.value, reason, latency_ms)

                if outcome == AttemptOutcome.UNKNOWN_MODEL:
                    continue
                if outcome in (AttemptOutcome.EXHAUSTED, AttemptOutcome.TRANSIENT):
                    break
                if outcome == AttemptOutcome.AUTH_FAULT:
                    if tier.optional:
                        logger.warning("tier '%s' auth fault (optional) — skipping tier", tier.id)
                        break
                    yield HubEvent(kind="error", payload=f"auth fault on required tier '{tier.id}': {reason}")
                    return
                if outcome == AttemptOutcome.BAD_REQUEST:
                    yield HubEvent(kind="error", payload=f"bad request on tier '{tier.id}' model '{model}': {reason}")
                    return
                break

            # No winner from this tier -> count toward the circuit breaker
            if tier_had_attempts:
                assert self._breaker is not None
                self._breaker.note_exhaustion(tier.id)
                prev_tier = tier

        result = HubResult(status="ladder_empty", ladder_empty=True, failover_attempts=attempts)
        yield HubEvent(kind="done", payload=result.model_dump())
