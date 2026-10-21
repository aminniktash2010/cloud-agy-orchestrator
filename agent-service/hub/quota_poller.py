"""Supervised in-process background poller/probe (live-quota, AD-11).

One asyncio task, started from the FastAPI lifespan (main.py). Epic C runs
the AGY CLI probe coroutine; the OpenRouter poll coroutine lands in Epic A
(currently a no-op stub). A provider failure sets that provider unhealthy and
logs loudly — the task itself never dies (NFR-2). No Cloud Scheduler, no new
infra: on Cloud Run scale-to-zero the poller simply doesn't run, and
`quota_updated_at` makes that staleness visible (NFR-5).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

logger = logging.getLogger("model-hub.quota-poller")


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
    """Epic A — OpenRouter live poll. Stubbed in Epic C (no-op)."""
    return


async def _supervisor(model_hub) -> None:
    cfg = model_hub._config()
    q = cfg.quota
    await asyncio.sleep(q.probe_initial_delay_s + random.uniform(0, 15))
    while True:
        for coro in (_probe_once(model_hub), _openrouter_poll_once(model_hub)):
            try:
                # Whole-cycle backstop: a hung provider coroutine must never
                # starve the other one.
                await asyncio.wait_for(coro, timeout=180)
            except Exception:  # noqa: BLE001 - supervision boundary (NFR-2)
                logger.exception("quota poller coroutine failed")
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
