"""Per-provider exhaustion signal table (architecture §6, AD-5).

AGY tier: existing ``is_credit_exhausted()`` markers moved verbatim, plus
UNKNOWN_MODEL markers (handles removed/renamed models gracefully) and
auth-fault markers (surfaced, never silently advanced on required tiers).

API tiers: HTTP 429 is the universal exhaustion signal (signal-agnostic —
correct under both credit and rate-limit regimes). Auth failures and
malformed requests are surfaced, never advanced.
"""

from __future__ import annotations

from typing import Optional

from hub.types import AttemptOutcome


# ---------------------------------------------------------------------------
# AGY CLI signals (Tier 0) — kept verbatim from agent-service/main.py
# ---------------------------------------------------------------------------

def agy_exhausted(return_code: int, output_text: str) -> bool:
    """Detects whether an agy execution failed due to depleted credits or quota."""
    if return_code == 3:
        return True
    low_markers = [
        "credits balance is too low",
        "resource_exhausted",
        "check quota",
        "code 429",
        "rate limit",
        "out of credits",
        'error_code":429',
    ]
    lower = output_text.lower()
    return any(marker in lower for marker in low_markers)


_AGY_UNKNOWN_MODEL_MARKERS = [
    "unknown model",
    "model not found",
    "invalid model",
    "unsupported model",
    "no such model",
]


def agy_unknown_model(output_text: str) -> bool:
    """A stale/removed model name -> advance to next model *within* the tier."""
    lower = output_text.lower()
    return any(marker in lower for marker in _AGY_UNKNOWN_MODEL_MARKERS)


_AGY_AUTH_FAULT_MARKERS = [
    "unauthenticated",
    "invalid_grant",
    "invalid grant",
    "authentication failed",
    "invalid credentials",
    "token expired",
    "token invalid",
    "refresh token",
    "not signed in",
    "sign in required",
]


def agy_auth_fault(output_text: str) -> bool:
    """OAuth/token problems -> surface as auth fault, never advance silently."""
    lower = output_text.lower()
    return any(marker in lower for marker in _AGY_AUTH_FAULT_MARKERS)


def agy_outcome(return_code: int, output_text: str) -> AttemptOutcome:
    """Full outcome classification for an agy run."""
    if return_code == 0:
        return AttemptOutcome.SUCCESS
    if agy_auth_fault(output_text):
        return AttemptOutcome.AUTH_FAULT
    if agy_unknown_model(output_text):
        return AttemptOutcome.UNKNOWN_MODEL
    if agy_exhausted(return_code, output_text):
        return AttemptOutcome.EXHAUSTED
    return AttemptOutcome.TRANSIENT  # non-zero rc without a known signature: advance tier, don't spin


# ---------------------------------------------------------------------------
# HTTP API tiers (429 = universal exhaustion signal)
# ---------------------------------------------------------------------------

def parse_retry_after(headers: dict) -> Optional[float]:
    """Returns Retry-After in seconds, or None if absent/unparseable/unsafe.

    Only numeric (delta-seconds) values are honoured. HTTP-date values are
    ignored (rare and ambiguous); values > 30s are rejected by the caller.
    """
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    raw = lowered.get("retry-after")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def parse_quota_hints(headers: dict) -> dict:
    """Extracts x-ratelimit-* hints for /hub/status (never key material)."""
    if not headers:
        return {}
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    hints: dict = {}
    for key in (
        "x-ratelimit-remaining",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-limit",
        "x-ratelimit-reset",
        "retry-after",
    ):
        if key in lowered:
            hints[key] = lowered[key]
    return hints


def http_outcome(
    status_code: Optional[int],
    body_text: str = "",
    headers: Optional[dict] = None,
    *,
    timeout: bool = False,
    provider: str = "",
) -> AttemptOutcome:
    """Maps an HTTP API-tier result to an AttemptOutcome (AD-5).

    429 (with optional usable Retry-After) -> EXHAUSTED / RETRYABLE_429
    401/403 -> AUTH_FAULT (skip optional tiers, surface on required tiers)
    400     -> BAD_REQUEST (surface, never advance)
    402     -> EXHAUSTED (xAI insufficient-credits; advance)
    5xx     -> TRANSIENT (advance, don't spin)
    timeout -> TRANSIENT
    """
    if timeout:
        return AttemptOutcome.TRANSIENT

    body = (body_text or "").lower()

    if status_code == 429:
        retry_after = parse_retry_after(headers or {})
        if retry_after is not None and 0 < retry_after <= 30:
            return AttemptOutcome.RETRYABLE_429
        return AttemptOutcome.EXHAUSTED
    if status_code in (401, 403):
        return AttemptOutcome.AUTH_FAULT
    if status_code == 400:
        return AttemptOutcome.BAD_REQUEST
    if status_code == 402:
        # xAI insufficient-credits: advance like exhaustion
        return AttemptOutcome.EXHAUSTED
    if status_code is not None and 500 <= status_code <= 599:
        return AttemptOutcome.TRANSIENT
    if status_code is not None and 200 <= status_code <= 299:
        return AttemptOutcome.SUCCESS

    # Body-sniffed fallbacks for SDKs that hide the status code
    if "resource_exhausted" in body or "quotaexceeded" in body or "quota_exceeded" in body:
        if "retry" in body:
            return AttemptOutcome.EXHAUSTED
        return AttemptOutcome.EXHAUSTED
    if "rate limit" in body or "too many requests" in body:
        return AttemptOutcome.EXHAUSTED
    if "unauthenticated" in body or "invalid api key" in body or "invalid_api_key" in body:
        return AttemptOutcome.AUTH_FAULT
    return AttemptOutcome.TRANSIENT
