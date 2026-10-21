"""OpenRouter key-status poll (live-quota Epic A, AD-19).

Polls ``GET {key_url}`` (default ``https://openrouter.ai/api/v1/key``) with the
``OPENROUTER_API_KEY`` bearer token. This is a free metadata call — never a
model call — costing one request per poll against the 500/day account budget
(144/day at the 10-minute cadence).

Response shape verified 2026-10-21 against the stored key (PM Q1; free tier,
no spend cap)::

    {"data": {"label": str, "is_free_tier": true,
              "limit": null, "limit_remaining": null, "limit_reset": null,
              "free_model_daily_requests": {"used": int, "limit": 50, "remaining": int},
              "usage_daily": int, ...}}

The AD-19 derivation chain (spend-cap -> free-RPD-cap -> null+detail) lives in
:func:`parse_key_status`, a pure function (no network). One verified
refinement over the PRD text: the API exposes the free-tier daily counter
itself (``free_model_daily_requests``), so the free-tier branch uses the
provider's own ``remaining``/``limit`` instead of the local
``requests_today``/``free_rpd_cap`` estimate; the estimate remains as the
fallback when the object is absent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from hub.live_quota import format_reset_in

logger = logging.getLogger("model-hub.openrouter-poll")

DEFAULT_KEY_URL = "https://openrouter.ai/api/v1/key"


@dataclass
class KeyPollResult:
    ok: bool
    remaining_pct: float | None
    reset_in: str | None
    detail: str
    at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _clamp_pct(v: float) -> float:
    return round(min(100.0, max(0.0, v)), 1)


def _reset_in_from_limit_reset(limit_reset: Any, now: datetime) -> str | None:
    """Best-effort ``reset_in`` for the spend-cap path (AD-19 step 1).

    The API returns null for cap-less keys; tolerate epoch seconds or an
    ISO-8601 string. Unknown shape -> None (never a guessed countdown).
    """
    if _is_num(limit_reset) and limit_reset > 0:
        return format_reset_in(limit_reset - now.timestamp())
    if isinstance(limit_reset, str) and limit_reset.strip():
        try:
            dt = datetime.fromisoformat(limit_reset.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return format_reset_in((dt - now).total_seconds())
        except ValueError:
            return None
    return None


def _seconds_to_utc_midnight(now: datetime) -> float:
    nxt = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(0.0, (nxt - now).total_seconds())


async def fetch_key_status(
    api_key: str, key_url: str, timeout_s: float = 20.0
) -> dict:
    """GET the key endpoint and return the parsed JSON body.

    Raises :class:`httpx.HTTPStatusError` on non-2xx (callers distinguish
    429/402 for backoff) and :class:`httpx.HTTPError` on transport failure.
    Never logs the key.
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(
            key_url, headers={"Authorization": f"Bearer {api_key}"}
        )
        resp.raise_for_status()
        body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("unexpected key-status body (not a JSON object)")
    return body


def parse_key_status(
    body: dict,
    free_rpd_cap: int,
    requests_today: int,
    *,
    now: datetime | None = None,
) -> KeyPollResult:
    """AD-19 derivation chain. Pure function — no network, no secrets.

    1. Key has numeric ``limit`` + ``limit_remaining`` (spend cap)
       -> ``100 * limit_remaining / limit``.
    2. Else the API's own ``free_model_daily_requests`` counter
       -> ``100 * remaining / limit`` (verified live 2026-10-21).
    2b. Else free tier (``is_free_tier``) -> local estimate
        ``100 * (1 - requests_today / free_rpd_cap)``.
    3. Else ``remaining_pct: None`` + an honest detail. Never a guessed number.

    ``now`` is injectable for deterministic tests; defaults to real UTC time.
    """
    now = now or _utcnow()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        data = body if isinstance(body, dict) else {}

    # 1. spend cap
    limit = data.get("limit")
    limit_remaining = data.get("limit_remaining")
    if _is_num(limit) and limit > 0 and _is_num(limit_remaining):
        pct = _clamp_pct(100.0 * limit_remaining / limit)
        return KeyPollResult(
            ok=True,
            remaining_pct=pct,
            reset_in=_reset_in_from_limit_reset(data.get("limit_reset"), now),
            detail=(
                f"spend cap: {limit_remaining:g} of {limit:g} "
                "credits remaining"
            ),
            at=now,
        )

    # 2. the provider's own free-tier daily counter (preferred over 2b)
    fmdr = data.get("free_model_daily_requests")
    if isinstance(fmdr, dict):
        f_limit = fmdr.get("limit")
        f_remaining = fmdr.get("remaining")
        if _is_num(f_limit) and f_limit > 0 and _is_num(f_remaining):
            pct = _clamp_pct(100.0 * f_remaining / f_limit)
            return KeyPollResult(
                ok=True,
                remaining_pct=pct,
                reset_in=format_reset_in(_seconds_to_utc_midnight(now)),
                detail=(
                    f"free models: {int(f_remaining)} of {int(f_limit)} "
                    "daily requests remaining (resets daily)"
                ),
                at=now,
            )

    # 2b. AD-19 fallback: local request count against the configured cap
    if data.get("is_free_tier") and free_rpd_cap > 0:
        pct = _clamp_pct(100.0 * (1.0 - requests_today / free_rpd_cap))
        return KeyPollResult(
            ok=True,
            remaining_pct=pct,
            reset_in=None,
            detail=(
                f"free tier: ~{max(0, requests_today)} of {free_rpd_cap} "
                "daily requests used (local count)"
            ),
            at=now,
        )

    # 3. honest null — the key reports no usable remaining figure
    return KeyPollResult(
        ok=True,
        remaining_pct=None,
        reset_in=None,
        detail="key has no spend cap and no free-tier counter visible",
        at=now,
    )
