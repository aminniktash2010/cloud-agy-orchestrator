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
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from hub.adapters import ADAPTER_REGISTRY
from hub.adapters.base import ProviderAdapter
from hub.breaker import CircuitBreaker
from hub.config import ConfigLoader
from hub.live_quota import LiveQuotaStore
from hub.model_catalog import ModelCatalog, Pin, PinError
from hub.observability import (
    advisory_banner,
    log_attempt,
    serving_line,
    status_payload,
)
from hub.quota import QuotaTracker, SpendLedger, truncate_prompt
from hub.signals import agy_family_of, parse_retry_after
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
        family_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._config_loader = config_loader or ConfigLoader()
        self._breaker: CircuitBreaker | None = breaker
        self._quota = quota or QuotaTracker()
        self._ledger = ledger or SpendLedger()
        self._quota_hints: dict[str, dict] = {}
        # Live-quota Epic C: per-family breakers (composite keys "agy:<family>")
        # + the advisory telemetry store (AD-15, AD-10).
        self._family_breaker: CircuitBreaker | None = family_breaker
        self._live_quota = LiveQuotaStore()
        # Stale catalog mappings: (canonical_model, tier_id) -> (provider_model_id, first_seen_iso)
        self._stale_mappings: dict[tuple[str, str], tuple[str, str]] = {}

    @property
    def live_quota(self) -> LiveQuotaStore:
        """Advisory live-quota store (read by the quota poller + /hub/status)."""
        return self._live_quota

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
        # Family breaker thresholds come from the quota config block (AD-15).
        fb = cfg.quota.family_breaker
        if self._family_breaker is None:
            self._family_breaker = CircuitBreaker(
                fb.exhaustions_to_open, fb.window_s, fb.cooldown_s
            )
        else:
            self._family_breaker.configure(
                fb.exhaustions_to_open, fb.window_s, fb.cooldown_s
            )
        return cfg

    def _agy_cfg(self, cfg: HubConfig) -> dict:
        """The quota.providers.agy config block as a plain dict."""
        providers = cfg.quota.providers or {}
        agy = providers.get("agy")
        return agy.model_dump() if agy is not None else {}

    def _family_models(
        self, tier: TierConfig, request: HubRequest, models_override: list[str] | None = None
    ) -> tuple[list[tuple[str, list[str]]], list[str]]:
        """Groups a tier's models into (family_id, models) walk order (AD-15).

        Returns (walk_groups, skipped_families). Non-AGY tiers walk a single
        "default" group (today's behavior). AGY families walk closed-first in
        config order; open families are skipped entirely (locked auto-skip=yes)
        and reported so the caller can record them. Empty walk -> the caller
        skips the whole tier ("skip: all_families_open"). A model matching no
        family lands in "default" (still attempted).
        """
        models = self._models_for_tier(tier, request, models_override)
        if tier.id != "agy":
            return [("default", models)], []
        assert self._family_breaker is not None
        cfg = self._config()
        agy_cfg = self._agy_cfg(cfg)
        taxonomy = self._live_quota.taxonomy()
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for m in models:
            fid = agy_family_of(m, "", agy_cfg, taxonomy) or "default"
            if fid not in groups:
                groups[fid] = []
                order.append(fid)
            groups[fid].append(m)
        walk, skipped = [], []
        for fid in order:
            if self._family_breaker.is_open(f"agy:{fid}"):
                skipped.append(fid)
            else:
                walk.append((fid, groups[fid]))
        return walk, skipped

    def _note_family_result(
        self, tier: TierConfig, model: str, outcome: AttemptOutcome, reason_text: str
    ) -> None:
        """Records per-family breaker state for Tier 0 (AD-15).

        Family is re-inferred from the model name AND the failure output, so
        family-specific error text is honoured. Unparseable output ("default")
        falls back to legacy whole-tier behavior (FR-3.3) — no family note.
        """
        if tier.id != "agy" or self._family_breaker is None:
            return
        cfg = self._config()
        fid = (
            agy_family_of(model, reason_text, self._agy_cfg(cfg),
                          self._live_quota.taxonomy())
            or "default"
        )
        if fid == "default":
            return
        key = f"agy:{fid}"
        if outcome == AttemptOutcome.SUCCESS:
            self._family_breaker.note_success(key)
        elif outcome == AttemptOutcome.EXHAUSTED:
            self._family_breaker.note_exhaustion(key)
            logger.info("hub agy family '%s' exhaustion noted (model '%s')", fid, model)

    @staticmethod
    def _force_exhaust_tier() -> str:
        return os.environ.get(FORCE_EXHAUST_ENV, "").strip()

    def _models_for_tier(self, tier: TierConfig, request: HubRequest,
                         models_override: list[str] | None = None) -> list[str]:
        # Pinned walk supplies the single mapped id; the legacy override is
        # ignored whenever a pin drives the walk (AD-29 precedence).
        if models_override is not None:
            return list(models_override)
        models = list(tier.models or [])
        override = (request.requested_model_override or "").strip()
        # Tier 0 honors the legacy ChatCommand.model: non-"auto" pins the first attempt
        if tier.id == "agy" and override and override.lower() not in ("auto", "", "default"):
            models = [override] + [m for m in models if m != override]
        return models

    # ------------------------------------------------------------------
    # Model pin (AD-28…AD-36)
    # ------------------------------------------------------------------
    def _resolve_pin(self, request: HubRequest, cfg: HubConfig) -> Pin | PinError | None:
        """None = unpinned (the catalog is never consulted — AC-4 relies on
        this being structural). Sentinels "auto"/"default"/"" mean unpinned."""
        raw = (request.model or "").strip()
        if not raw or raw.lower() in ("auto", "default"):
            return None
        catalog = ModelCatalog(cfg.model_catalog)
        resolved = catalog.resolve(raw)
        if resolved is None:
            return PinError(kind="no_match", raw=raw, suggestions=catalog.suggest(raw))
        canonical, entry = resolved
        return Pin(canonical=canonical, raw=raw, carriers=dict(entry.carriers or {}))

    def _note_stale(self, canonical: str, tier_id: str, provider_id: str) -> None:
        key = (canonical, tier_id)
        if key not in self._stale_mappings:
            self._stale_mappings[key] = (
                provider_id, datetime.now(timezone.utc).isoformat())
            logger.warning("model_catalog: stale mapping for '%s' on tier '%s' (mapped id '%s')",
                           canonical, tier_id, provider_id)

    def _pin_error_result(self, err: PinError, request: HubRequest) -> HubResult:
        """no_match: zero provider calls were made; the input never resolved."""
        return HubResult(
            status="model_unavailable",
            requested_model=err.raw,
            failover_attempts=[],
            model_error={
                "code": "no_match",
                "message": f"Unknown model '{err.raw}'. No provider was contacted.",
                "input": err.raw,
                "suggestions": list(err.suggestions),
            },
            serving_line=f"🚫 Unknown model '{err.raw}' — no provider contacted",
        )

    def _model_unavailable_result(self, pin: Pin, attempts: list[ModelAttempt]) -> HubResult:
        carriers_tried: list[dict] = []
        exclusions: list[dict] = []
        for a in attempts:
            if a.outcome == "skipped":
                exclusions.append({"tier": a.tier, "reason": a.reason})
            else:
                carriers_tried.append({
                    "tier": a.tier, "provider": a.provider,
                    "provider_model_id": a.model, "outcome": a.outcome,
                    "reason": a.reason,
                })
        return HubResult(
            status="model_unavailable",
            pinned_model=pin.canonical,
            requested_model=pin.raw,
            failover_attempts=attempts,
            model_error={
                "code": "no_carrier",
                "message": f"No carrier could serve pinned model '{pin.canonical}'.",
                "pinned_model": pin.canonical,
                "carriers_tried": carriers_tried,
                "exclusions": exclusions,
            },
            serving_line=f"🚫 Pinned model {pin.canonical} unavailable — no carrier served it",
        )

    async def _execute_pinned(
        self, request: HubRequest, pin: Pin, cfg: HubConfig,
        prompt: str, truncated: bool, attempts: list[ModelAttempt],
        force_exhaust: str,
    ) -> HubResult:
        """Pinned walk (AD-31): same tier order, one mapped model id per tier.
        The intra-tier multi-model walk is suspended — silent substitution is
        structurally impossible. Pinned traffic does not touch the tier-level
        circuit breaker (model-specific exhaustion != tier exhaustion); agy
        per-family notes are preserved (AD-15)."""
        for tier in cfg.tiers_sorted():
            provider_id = pin.carriers.get(tier.id)
            if provider_id is None:
                # Capability pre-check FIRST (AC-5): no mapping -> no API call
                # is ever burned, and the skip reason names the model exclusion
                # even when the tier is also in skip_tiers.
                reason = ("skip: model_pinned_non_agy" if tier.id == "agy"
                          else "skip: model_not_carried")
                self._record(attempts, tier, "", "skipped", reason)
                continue
            if tier.id in (request.skip_tiers or []):
                self._record(attempts, tier, "", "skipped", "skip: skip_tiers")
                continue
            skip = self._skip_reason(tier, cfg)
            if skip:
                self._record(attempts, tier, "", "skipped", f"skip: {skip}")
                continue
            if tier.free_only and not provider_id.endswith(":free"):
                # Paid pin on a free_only tier: excluded pre-attempt, no spend ever.
                self._record(attempts, tier, provider_id, "skipped", "skip: free_only_policy")
                continue

            adapter_cls = ADAPTER_REGISTRY.get(tier.adapter)
            if adapter_cls is None:
                raise HubError(f"unknown adapter '{tier.adapter}' for tier '{tier.id}'")
            adapter: ProviderAdapter = adapter_cls()

            if tier.id == "agy":
                family_groups, skipped_families = self._family_models(
                    tier, request, [provider_id])
                for fid in skipped_families:
                    self._record(attempts, tier, "", "skipped",
                                 f"skip: family_circuit_open (agy:{fid})")
                if not family_groups:
                    self._record(attempts, tier, "", "skipped", "skip: all_families_open")
                    continue
                model_pairs = [(fid, m) for fid, ms in family_groups for m in ms]
            else:
                model_pairs = [("default", provider_id)]

            wait_s = self._quota.pacing_wait_s(tier.id, tier.rpm_pacing_s)
            if wait_s > 0:
                await asyncio.sleep(wait_s)

            for family_id, model in model_pairs:
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
                            conversation_id=request.conversation_id,
                            continue_session=request.continue_session,
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
                    self._note_family_result(tier, model, AttemptOutcome.SUCCESS, "")
                    self._record(attempts, tier, model, outcome.value, "",
                                 latency_ms,
                                 usage=resp.usage if isinstance(resp, AdapterResponse) else {},
                                 quota_hints=resp.quota_hints if isinstance(resp, AdapterResponse) else {})
                    return self._winner(tier, model, resp, attempts, truncated,
                                        pinned_model=pin.canonical,
                                        requested_model=pin.raw)

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
                            self._record(attempts, tier, model, outcome2.value, "retry succeeded",
                                         latency_ms)
                            return self._winner(tier, model, resp2, attempts, truncated,
                                                pinned_model=pin.canonical,
                                                requested_model=pin.raw)
                        outcome, resp = outcome2, resp2
                    else:
                        outcome = AttemptOutcome.EXHAUSTED

                reason = self._answer_text(resp)[:300]
                if outcome == AttemptOutcome.EXHAUSTED:
                    self._note_family_result(tier, model, outcome, reason)
                    self._record(attempts, tier, model, outcome.value, reason, latency_ms)
                    break  # next carrier
                if outcome == AttemptOutcome.UNKNOWN_MODEL:
                    # Under a pin the id came from our catalog: the mapping is stale.
                    self._note_stale(pin.canonical, tier.id, model)
                    self._record(attempts, tier, model, "stale_mapping",
                                 f"stale mapping: provider rejected mapped id '{model}': {reason}",
                                 latency_ms)
                    break  # next carrier (no intra-tier walk under pin)
                if outcome == AttemptOutcome.TRANSIENT:
                    if "404" in reason:
                        # AD-32: 404 on a catalog-mapped id = stale mapping (http_outcome untouched).
                        self._note_stale(pin.canonical, tier.id, model)
                        self._record(attempts, tier, model, "stale_mapping",
                                     f"stale mapping: 404 for mapped id '{model}': {reason}",
                                     latency_ms)
                    else:
                        self._record(attempts, tier, model, outcome.value, reason, latency_ms)
                    break  # next carrier
                if outcome == AttemptOutcome.AUTH_FAULT:
                    if tier.optional:
                        logger.warning("tier '%s' auth fault (optional) — skipping tier", tier.id)
                        self._record(attempts, tier, model, outcome.value, reason, latency_ms)
                        break
                    raise HubError(f"auth fault on required tier '{tier.id}': {reason}")
                if outcome == AttemptOutcome.BAD_REQUEST:
                    if request.repo_ctx is None:
                        logger.info("tier '%s' BAD_REQUEST without repo_ctx — skipping tier", tier.id)
                        self._record(attempts, tier, model, "skipped", "skip: no_repo_ctx", latency_ms)
                        break
                    raise HubError(f"bad request on tier '{tier.id}' model '{model}': {reason}")
                self._record(attempts, tier, model, outcome.value, reason, latency_ms)
                break  # next carrier (defensive)

        return await self._no_carrier_result(request, pin, cfg, prompt, truncated,
                                             attempts, force_exhaust)

    async def _execute_pinned_stream(
        self, request: HubRequest, pin: Pin, cfg: HubConfig,
        prompt: str, truncated: bool, attempts: list[ModelAttempt],
        force_exhaust: str,
    ) -> AsyncIterator[HubEvent]:
        """Pinned walk for streaming (AD-31/AD-36): same carrier semantics as
        _execute_pinned; tier_switch events carry pinned_model; the intra-tier
        "switching to X" message is suppressed (there is no next model)."""
        prev_tier: TierConfig | None = None
        for tier in cfg.tiers_sorted():
            provider_id = pin.carriers.get(tier.id)
            if provider_id is None:
                # Capability pre-check FIRST (AC-5): see _execute_pinned.
                reason = ("skip: model_pinned_non_agy" if tier.id == "agy"
                          else "skip: model_not_carried")
                self._record(attempts, tier, "", "skipped", reason)
                continue
            if tier.id in (request.skip_tiers or []):
                self._record(attempts, tier, "", "skipped", "skip: skip_tiers")
                continue
            skip = self._skip_reason(tier, cfg)
            if skip:
                self._record(attempts, tier, "", "skipped", f"skip: {skip}")
                continue
            if tier.free_only and not provider_id.endswith(":free"):
                self._record(attempts, tier, provider_id, "skipped", "skip: free_only_policy")
                continue

            if prev_tier is not None and prev_tier.id != tier.id:
                yield HubEvent(kind="tier_switch", payload={
                    "from": {"tier": prev_tier.id, "provider": prev_tier.provider},
                    "to": {"tier": tier.id, "provider": tier.provider, "model": provider_id},
                    "reason": f"tier {prev_tier.id} exhausted",
                    "capability_mode": tier.capability.value,
                    "pinned_model": pin.canonical,
                })

            adapter_cls = ADAPTER_REGISTRY.get(tier.adapter)
            if adapter_cls is None:
                raise HubError(f"unknown adapter '{tier.adapter}' for tier '{tier.id}'")
            adapter: ProviderAdapter = adapter_cls()

            if tier.id == "agy":
                family_groups, skipped_families = self._family_models(
                    tier, request, [provider_id])
                for fid in skipped_families:
                    self._record(attempts, tier, "", "skipped",
                                 f"skip: family_circuit_open (agy:{fid})")
                if not family_groups:
                    self._record(attempts, tier, "", "skipped", "skip: all_families_open")
                    continue
                model_pairs = [(fid, m) for fid, ms in family_groups for m in ms]
            else:
                model_pairs = [("default", provider_id)]

            wait_s = self._quota.pacing_wait_s(tier.id, tier.rpm_pacing_s)
            if wait_s > 0:
                await asyncio.sleep(wait_s)

            live = tier.capability == CapabilityMode.AGENT_LOOP
            for family_id, model in model_pairs:
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
                            conversation_id=request.conversation_id,
                            continue_session=request.continue_session,
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
                        outcome, resp = await self._retry_once(adapter, prompt, model, tier, request)
                        latency_ms = int((time.monotonic() - started) * 1000)
                        self._quota.account_attempt(tier.id)
                        if outcome == AttemptOutcome.SUCCESS and not live:
                            buffered = []
                    else:
                        outcome = AttemptOutcome.EXHAUSTED

                if outcome == AttemptOutcome.SUCCESS:
                    self._note_family_result(tier, model, AttemptOutcome.SUCCESS, "")
                    usage = resp.usage if isinstance(resp, AdapterResponse) else {}
                    self._record(attempts, tier, model, outcome.value, "", latency_ms,
                                 usage=usage, quota_hints=quota_hints)
                    result = self._winner(tier, model, resp, attempts, truncated,
                                          pinned_model=pin.canonical,
                                          requested_model=pin.raw)
                    if not live:
                        if result.advisory_banner:
                            yield HubEvent(kind="log", payload=result.advisory_banner)
                        for ev in buffered:
                            yield ev
                        if isinstance(resp, AdapterResponse) and not buffered:
                            yield HubEvent(kind="log", payload=resp.text)
                    yield HubEvent(kind="done", payload=result.model_dump())
                    return

                reason = self._answer_text(resp)[:300]
                if outcome == AttemptOutcome.EXHAUSTED:
                    self._note_family_result(tier, model, outcome, reason)
                    self._record(attempts, tier, model, outcome.value, reason, latency_ms)
                    break
                if outcome == AttemptOutcome.UNKNOWN_MODEL:
                    # No intra-tier switch message under a pin (suppressed by design).
                    self._note_stale(pin.canonical, tier.id, model)
                    self._record(attempts, tier, model, "stale_mapping",
                                 f"stale mapping: provider rejected mapped id '{model}': {reason}",
                                 latency_ms)
                    break
                if outcome == AttemptOutcome.TRANSIENT:
                    if "404" in reason:
                        self._note_stale(pin.canonical, tier.id, model)
                        self._record(attempts, tier, model, "stale_mapping",
                                     f"stale mapping: 404 for mapped id '{model}': {reason}",
                                     latency_ms)
                    else:
                        self._record(attempts, tier, model, outcome.value, reason, latency_ms)
                    break
                if outcome == AttemptOutcome.AUTH_FAULT:
                    if tier.optional:
                        logger.warning("tier '%s' auth fault (optional) — skipping tier", tier.id)
                        self._record(attempts, tier, model, outcome.value, reason, latency_ms)
                        break
                    yield HubEvent(kind="error", payload=f"auth fault on required tier '{tier.id}': {reason}")
                    return
                if outcome == AttemptOutcome.BAD_REQUEST:
                    if request.repo_ctx is None:
                        logger.info("tier '%s' BAD_REQUEST without repo_ctx — skipping tier", tier.id)
                        self._record(attempts, tier, model, "skipped", "skip: no_repo_ctx", latency_ms)
                        break
                    yield HubEvent(kind="error", payload=f"bad request on tier '{tier.id}' model '{model}': {reason}")
                    return
                self._record(attempts, tier, model, outcome.value, reason, latency_ms)
                break

            prev_tier = tier

        # No carrier served.
        if request.allow_model_fallback:
            yield HubEvent(kind="status", payload={
                "text": f"⚠️ Pinned model '{pin.canonical}' unavailable — falling back to the default ladder…"})
            fallback_req = request.model_copy(update={"model": None})
            async for ev in self.execute_stream(fallback_req):
                if ev.kind == "done" and isinstance(ev.payload, dict):
                    ev.payload["model_substituted"] = True
                    ev.payload["requested_model"] = pin.raw
                    ev.payload["pinned_model"] = pin.canonical
                    ev.payload["failover_attempts"] = (
                        [a.model_dump() for a in attempts]
                        + ev.payload.get("failover_attempts", []))
                yield ev
            return
        yield HubEvent(kind="done",
                       payload=self._model_unavailable_result(pin, attempts).model_dump())

    async def _no_carrier_result(
        self, request: HubRequest, pin: Pin, cfg: HubConfig,
        prompt: str, truncated: bool, attempts: list[ModelAttempt],
        force_exhaust: str,
    ) -> HubResult:
        """No carrier served the pin. Either loud fallback or model_unavailable."""
        if request.allow_model_fallback:
            # Recursive entry: model=None -> _resolve_pin returns None -> the
            # untouched unpinned ladder walk. No ladder code is duplicated.
            fallback_req = request.model_copy(update={"model": None})
            result = await self.execute(fallback_req)
            # Preserve the pinned phase in the attempt log; mark the substitution LOUDLY.
            result.failover_attempts = attempts + result.failover_attempts
            result.model_substituted = True
            result.requested_model = pin.raw
            result.pinned_model = pin.canonical
            return result
        return self._model_unavailable_result(pin, attempts)

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
        # Per-family circuit states for the AGY live_quota.families rows.
        family_circuits: dict[str, str] = {}
        if self._family_breaker is not None:
            fam_snap = self._family_breaker.snapshot()
            known_families = set(self._agy_cfg(cfg).get("families", {}))
            known_families.update(self._live_quota.taxonomy().values())
            for fid in known_families:
                family_circuits[fid] = fam_snap.get(f"agy:{fid}", {}).get(
                    "circuit", "closed"
                )
        live_quota = self._live_quota.snapshot(
            cfg,
            datetime.now(timezone.utc),
            cfg.quota.staleness_s,
            family_circuits,
        )
        payload = status_payload(
            cfg,
            config_source=self._config_loader.source,
            breaker_snapshot=self._breaker.snapshot(),
            quota_usage={t.id: self._quota.usage_today(t.id) for t in cfg.tiers},
            quota_hints=dict(self._quota_hints),
            key_status=key_status,
            spend_today_usd=self._ledger.spend_today_usd(),
            live_quota=live_quota,
        )
        # AD-37: stale catalog mappings, auto-cleared when the current catalog
        # no longer maps the same (canonical, tier) to the flagged provider id.
        catalog = cfg.model_catalog.models if cfg.model_catalog else {}
        stale: list[dict] = []
        for (canonical, tier_id), (provider_id, first_seen) in self._stale_mappings.items():
            entry = catalog.get(canonical)
            if entry is not None and entry.carriers.get(tier_id) == provider_id:
                stale.append({
                    "canonical_model": canonical,
                    "tier": tier_id,
                    "provider_model_id": provider_id,
                    "first_seen": first_seen,
                })
        payload["stale_model_mappings"] = stale
        return payload

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

        # Model pin (AD-29): resolve once at entry. None -> the untouched
        # ladder below (AC-4: the catalog is never consulted for unpinned).
        pin = self._resolve_pin(request, cfg)
        if isinstance(pin, PinError):
            return self._pin_error_result(pin, request)
        if pin is not None:
            return await self._execute_pinned(
                request, pin, cfg, prompt, truncated, attempts, force_exhaust)

        for tier in cfg.tiers_sorted():
            # Structural tier skip (AD-22, e.g. MCP hub_ask default ["agy"]):
            # checked BEFORE _skip_reason so the reason stays distinct.
            if tier.id in (request.skip_tiers or []):
                self._record(attempts, tier, "", "skipped", "skip: skip_tiers")
                continue
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

            # Tier 0 walks per-family groups (closed-first, open families
            # skipped — locked auto-skip). Flattened to (family, model) pairs
            # so the break/continue flow below is unchanged.
            family_groups, skipped_families = self._family_models(tier, request)
            for fid in skipped_families:
                self._record(attempts, tier, "", "skipped",
                             f"skip: family_circuit_open (agy:{fid})")
            if tier.id == "agy" and not family_groups:
                self._record(attempts, tier, "", "skipped", "skip: all_families_open")
                continue
            model_pairs = [(fid, m) for fid, ms in family_groups for m in ms]

            tier_had_attempts = False
            for family_id, model in model_pairs:
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
                            conversation_id=request.conversation_id,
                            continue_session=request.continue_session,
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
                    self._note_family_result(tier, model, AttemptOutcome.SUCCESS, "")
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

                if outcome == AttemptOutcome.EXHAUSTED:
                    # Per-family breaker note (AD-15); "default" falls back to
                    # the legacy whole-tier exhaustion recorded at tier end.
                    self._note_family_result(tier, model, outcome, reason)
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
                    if request.repo_ctx is None:
                        # Repo-less callers (MCP hub_ask) cannot satisfy adapters
                        # that require a repo context (e.g. AgyAdapter): skip the
                        # tier instead of failing the whole request.
                        logger.info("tier '%s' BAD_REQUEST without repo_ctx — skipping tier", tier.id)
                        self._record(attempts, tier, model, "skipped", "skip: no_repo_ctx", latency_ms)
                        break
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
                conversation_id=request.conversation_id,
                continue_session=request.continue_session,
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
                attempts: list[ModelAttempt], truncated: bool,
                pinned_model: str | None = None, requested_model: str = "") -> HubResult:
        answer = self._answer_text(resp)
        banner = ""
        if tier.capability == CapabilityMode.ANSWER_ONLY:
            banner = advisory_banner(tier.provider, model, pinned_model)
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
            pinned_model=pinned_model,
            requested_model=requested_model,
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

        # Model pin (AD-29): same entry contract as execute().
        pin = self._resolve_pin(request, cfg)
        if isinstance(pin, PinError):
            err = self._pin_error_result(pin, request)
            yield HubEvent(kind="error", payload={
                "code": "no_match",
                "message": (err.model_error or {}).get("message", ""),
                "suggestions": (err.model_error or {}).get("suggestions", []),
            })
            return
        if pin is not None:
            async for ev in self._execute_pinned_stream(
                    request, pin, cfg, prompt, truncated, attempts, force_exhaust):
                yield ev
            return

        for tier in cfg.tiers_sorted():
            # Structural tier skip (AD-22): checked BEFORE _skip_reason so the
            # reason stays distinct in failover_attempts.
            if tier.id in (request.skip_tiers or []):
                self._record(attempts, tier, "", "skipped", "skip: skip_tiers")
                continue
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

            # Tier 0 walks per-family groups (closed-first, open families
            # skipped — locked auto-skip). Flattened to (family, model) pairs
            # so the break/continue flow below is unchanged.
            family_groups, skipped_families = self._family_models(tier, request)
            for fid in skipped_families:
                self._record(attempts, tier, "", "skipped",
                             f"skip: family_circuit_open (agy:{fid})")
            if tier.id == "agy" and not family_groups:
                self._record(attempts, tier, "", "skipped", "skip: all_families_open")
                continue
            model_pairs = [(fid, m) for fid, ms in family_groups for m in ms]

            tier_had_attempts = False
            live = tier.capability == CapabilityMode.AGENT_LOOP  # forward live, else buffer
            for pi, (family_id, model) in enumerate(model_pairs):
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
                            conversation_id=request.conversation_id,
                            continue_session=request.continue_session,
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
                    self._note_family_result(tier, model, AttemptOutcome.SUCCESS, "")
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

                if outcome == AttemptOutcome.EXHAUSTED:
                    self._note_family_result(tier, model, outcome, reason)
                if outcome == AttemptOutcome.UNKNOWN_MODEL:
                    # Intra-tier model advance: restore the pre-hub banner so the
                    # PWA shows the switch live (cross-tier switches use tier_switch).
                    nxt = model_pairs[pi + 1][1] if pi + 1 < len(model_pairs) else None
                    if nxt is not None:
                        yield HubEvent(kind="status", payload={
                            "text": f"⚠️ Model '{model}' unavailable — "
                                    f"🔄 switching to '{nxt}' (same tier)…"})
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
                    if request.repo_ctx is None:
                        # Repo-less callers (MCP hub_ask) cannot satisfy adapters
                        # that require a repo context (e.g. AgyAdapter): skip the
                        # tier instead of failing the whole request.
                        logger.info("tier '%s' BAD_REQUEST without repo_ctx — skipping tier", tier.id)
                        self._record(attempts, tier, model, "skipped", "skip: no_repo_ctx", latency_ms)
                        break
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
