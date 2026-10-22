"""Supervised in-process background poller/probe (live-quota, AD-11).

One asyncio task, started from the FastAPI lifespan (main.py). Epic C runs
the AGY CLI probe coroutine; Epic A runs the OpenRouter key-poll coroutine.
A provider failure sets that provider unhealthy and logs loudly — the task
itself never dies (NFR-2). No Cloud Scheduler, no new infra: on Cloud Run
scale-to-zero the poller simply doesn't run, and ``quota_updated_at`` makes
that staleness visible (NFR-5).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("model-hub.quota-poller")

# OpenRouter 429/402 backoff (AD-19/PRD FR-7.4): the check must never burn the
# budget it's measuring. Module-level: one poller task per process.
_openrouter_backoff_until: datetime | None = None
_openrouter_backoff_s: float = 600.0
_OPENROUTER_BACKOFF_CAP_S = 3600.0


async def _probe_once(model_hub) -> None:
    """One AGY CLI probe cycle (Epic C). Never raises past this boundary."""
    from hub import cli_probe

    cfg = model_hub._config()
    agy_cfg = cfg.quota.providers.get("agy")
    agy = agy_cfg.model_dump() if agy_cfg is not None else {}
    store = model_hub.live_quota
    now = datetime.now(timezone.utc)

    if not agy.get("probe_enabled", True):
        await store.set_poller_health(
            "agy_probe", False, now, detail="probe disabled in config"
        )
        return
    try:
        result = await cli_probe.run_probe(agy)
    except Exception:  # noqa: BLE001 - supervision boundary (NFR-2)
        logger.exception("agy probe crashed")
        await store.set_poller_health(
            "agy_probe", False, now, detail="probe exception (see logs)"
        )
        return
    if result is None:
        # Not runnable here (local dev / CI): quiet no-op, honest health flag.
        await store.set_poller_health(
            "agy_probe",
            False,
            now,
            detail="probe not available in this environment (no agy CLI or no OAuth token)",
        )
        return
    if not result.ok:
        logger.warning("agy probe unhealthy: %s", result.detail)
        await store.set_poller_health("agy_probe", False, result.at, detail=result.detail)
        return
    await store.set_probe_result(
        result.families, result.taxonomy, result.unmapped, result.at,
        detail=result.detail,
    )
    await store.set_poller_health("agy_probe", True, result.at)


async def _openrouter_poll_once(model_hub) -> None:
    """Epic A — OpenRouter live poll. Never raises past this boundary.

    Free metadata call (never a model call): 144/day of the 500/day account
    budget at the 10-minute cadence. On 429/402 the poll backs off
    exponentially (cap 1h) and keeps the last-known value; any other failure
    marks the provider unhealthy. The chat path never waits on this.
    """
    global _openrouter_backoff_until, _openrouter_backoff_s
    from hub import openrouter_poll

    cfg = model_hub._config()
    or_cfg = cfg.quota.providers.get("openrouter")
    or_ = or_cfg.model_dump() if or_cfg is not None else {}
    store = model_hub.live_quota
    now = datetime.now(timezone.utc)

    if not or_.get("quota_enabled", True):
        await store.set_poller_health(
            "openrouter", False, now, detail="poll disabled in config"
        )
        return
    if _openrouter_backoff_until is not None and now < _openrouter_backoff_until:
        logger.debug(
            "openrouter poll backing off until %s", _openrouter_backoff_until
        )
        return
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        # No-op in local dev/CI without the key: honest health flag, no crash.
        await store.set_poller_health(
            "openrouter", False, now, detail="OPENROUTER_API_KEY not set"
        )
        return
    key_url = or_.get("key_url") or openrouter_poll.DEFAULT_KEY_URL
    try:
        body = await openrouter_poll.fetch_key_status(
            api_key, key_url, timeout_s=20.0
        )
    except httpx.HTTPStatusError as e:
        code = e.response.status_code if e.response is not None else None
        if code in (402, 429):
            _openrouter_backoff_until = now + timedelta(
                seconds=_openrouter_backoff_s
            )
            logger.warning(
                "openrouter poll HTTP %s — backing off %ds",
                code,
                int(_openrouter_backoff_s),
            )
            _openrouter_backoff_s = min(
                _OPENROUTER_BACKOFF_CAP_S, _openrouter_backoff_s * 2
            )
        else:
            logger.exception("openrouter poll failed")
        await store.set_poller_health(
            "openrouter",
            False,
            now,
            detail=f"key poll HTTP {code}" if code else "key poll failed",
        )
        return
    except Exception:  # noqa: BLE001 - supervision boundary (NFR-2)
        logger.exception("openrouter poll failed")
        await store.set_poller_health(
            "openrouter", False, now, detail="key poll failed (see logs)"
        )
        return

    try:
        requests_today = model_hub._quota.usage_today("openrouter")
    except Exception:  # noqa: BLE001 - defensive; local counter is advisory
        requests_today = 0
    result = openrouter_poll.parse_key_status(
        body, or_.get("free_rpd_cap", 50), requests_today
    )
    _openrouter_backoff_until = None
    _openrouter_backoff_s = 600.0
    await store.set_poll_result(
        "openrouter",
        result.remaining_pct,
        result.reset_in,
        result.detail,
        result.at,
    )
    await store.set_poller_health("openrouter", True, result.at)


async def _supervisor(model_hub) -> None:
    cfg = model_hub._config()
    q = cfg.quota
    await asyncio.sleep(q.probe_initial_delay_s + random.uniform(0, 15))
    last_hygiene: datetime | None = None
    while True:
        for coro in (_probe_once(model_hub), _openrouter_poll_once(model_hub)):
            try:
                # Whole-cycle backstop: a hung provider coroutine must never
                # starve the other one.
                await asyncio.wait_for(coro, timeout=180)
            except Exception:  # noqa: BLE001 - supervision boundary (NFR-2)
                logger.exception("quota poller coroutine failed")
        # Model-hygiene proactive catalog diff (AD-45): own ~6h cadence with
        # jitter, fully supervised. A failed run retries next tick.
        try:
            h = model_hub._hygiene_cfg()
        except Exception:  # noqa: BLE001 - never break the supervisor on config read
            logger.exception("hygiene scheduling: cannot read hygiene config")
            h = {}
        now = datetime.now(timezone.utc)
        interval = h.get("poll_interval_s", 21600)
        due = last_hygiene is None or (now - last_hygiene).total_seconds() > interval + random.uniform(0, 600)
        if h.get("enabled") and h.get("poll_enabled") and due:
            try:
                await asyncio.wait_for(_hygiene_check_once(model_hub), timeout=300)
                last_hygiene = now
            except Exception:  # noqa: BLE001 - supervision boundary (NFR-2)
                logger.exception("hygiene check failed")
        await asyncio.sleep(q.probe_interval_s)


def start(model_hub):
    """Start the supervised poller task.

    Must be called from a running event loop (the FastAPI lifespan).
    Returns an async ``stop()`` callable for graceful shutdown.
    """
    task = asyncio.get_running_loop().create_task(
        _supervisor(model_hub), name="hub-quota-poller"
    )
    logger.info("quota poller started")

    async def stop() -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("quota poller stopped")

    return stop


# ---------------------------------------------------------------------------
# Model-hygiene proactive catalog diff (Epic H1, AD-45)
# ---------------------------------------------------------------------------

# tier_id -> (catalog URL, id-list shape, api_key_env or None for public)
_HYGIENE_CATALOGS: dict[str, tuple[str, str, str | None]] = {
    "openrouter": ("https://openrouter.ai/api/v1/models", "openai_list", None),
    "requesty": ("https://router.requesty.ai/v1/models", "openai_list", None),
    "groq": ("https://api.groq.com/openai/v1/models", "openai_list", "GROQ_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1/models", "openai_list", "NVIDIA_API_KEY"),
    "aistudio": (
        "https://generativelanguage.googleapis.com/v1beta/models",
        "aistudio_list",
        "GEMINI_API_KEY",
    ),
}


def _extract_catalog_ids(body: object, shape: str) -> set[str] | None:
    """Defensive id-set extraction. None = unparseable/empty (fetch failure)."""
    try:
        if not isinstance(body, dict):
            return None
        ids: set[str] = set()
        if shape == "openai_list":
            data = body.get("data") or []
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    ids.add(str(item["id"]))
        elif shape == "aistudio_list":
            for item in body.get("models") or []:
                name = item.get("name", "") if isinstance(item, dict) else ""
                name = str(name)
                if name.startswith("models/"):
                    name = name[len("models/"):]
                if name:
                    ids.add(name)
        else:
            return None
        # An empty id set is suspicious (outage/parse drift) — treat as failure.
        return ids or None
    except Exception:  # noqa: BLE001 - defensive; never crash the poller
        return None


async def _fetch_provider_catalog(tier) -> set[str] | None:
    """Returns the provider's live model-id set, or None on ANY failure.

    Failure (network, auth, unexpected shape, empty set) quarantines nothing —
    absence of evidence is not evidence of retirement (AD-45).
    """
    spec = _HYGIENE_CATALOGS.get(tier.id)
    if spec is None:
        logger.debug("hygiene: no catalog fetcher for tier '%s'", tier.id)
        return None
    url, shape, key_env = spec
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if key_env:
        key = (os.environ.get(key_env, "") or "").strip()
        if not key:
            logger.debug("hygiene: %s not set, skipping %s catalog", key_env, tier.id)
            return None
        if tier.id == "aistudio":
            params["key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            body = resp.json()
    except Exception as e:  # noqa: BLE001 - any fetch failure -> skip provider
        logger.warning("hygiene: catalog fetch failed for tier '%s': %s", tier.id, e)
        return None
    ids = _extract_catalog_ids(body, shape)
    if ids is None:
        logger.warning("hygiene: unparseable catalog shape for tier '%s'", tier.id)
    return ids


async def _hygiene_check_tier(model_hub, tier, now: datetime) -> bool:
    """Diffs one tier's configured ids against the live catalog.

    Returns True when the provider was checked; False on fetch failure.
    Drift quarantines with source="proactive" — but a reactive entry is
    never touched (AD-45: catalog presence != servability).
    """
    catalog_ids = await _fetch_provider_catalog(tier)
    if catalog_ids is None:
        return False
    cfg = model_hub._config()
    candidates: set[str] = set(tier.models or [])
    mc = cfg.model_catalog.models if cfg.model_catalog else {}
    for entry in mc.values():
        pid = entry.carriers.get(tier.id)
        if pid:
            candidates.add(pid)
    for cid in sorted(candidates):
        if cid in catalog_ids:
            continue
        key = (tier.id, cid)
        existing = model_hub._retired_models.get(key)
        if existing is not None and existing.get("source") == "reactive":
            # Reactive truth (a real call 404'd) outranks the poller diff —
            # never downgrade it.
            continue
        model_hub._quarantine(
            tier.id, cid,
            f"absent from {tier.provider or tier.id} catalog",
            source="proactive",
        )
    return True


async def _hygiene_check_once(model_hub) -> None:
    """One proactive catalog-diff cycle. Never raises past this boundary."""
    cfg = model_hub._config()
    now = datetime.now(timezone.utc)
    checked = 0
    failed: list[str] = []
    for tier in cfg.tiers_sorted():
        if not tier.enabled:
            continue  # disabled tiers (incl. xai) skipped
        if tier.id == "agy":
            continue  # no CLI catalog endpoint; reactive UNKNOWN_MODEL covers it
        try:
            ok = await _hygiene_check_tier(model_hub, tier, now)
        except Exception:  # noqa: BLE001 - per-tier supervision boundary
            logger.exception("hygiene check crashed for tier '%s'", tier.id)
            ok = False
        if ok:
            checked += 1
        else:
            failed.append(tier.id)
    await model_hub.live_quota.set_poller_health(
        "model_hygiene",
        not failed,
        now,
        detail=(f"catalog diff complete ({checked} tiers)"
                if not failed
                else f"fetch failed for: {', '.join(failed)} (quarantined nothing)"),
    )
