"""Unit tests: exhaustion signal parsers (E1-S1)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.signals import (  # noqa: E402
    agy_auth_fault,
    agy_exhausted,
    agy_outcome,
    agy_unknown_model,
    http_outcome,
    parse_quota_hints,
    parse_retry_after,
)
from hub.types import AttemptOutcome  # noqa: E402


# --- AGY CLI ---------------------------------------------------------------

def test_agy_exhausted_return_code_3():
    assert agy_exhausted(3, "anything") is True


def test_agy_exhausted_markers():
    for marker in [
        "credits balance is too low",
        "RESOURCE_EXHAUSTED",
        "please check quota and try again",
        "code 429",
        "Rate limit exceeded",
        "out of credits",
        '{"error_code":429}',
    ]:
        assert agy_exhausted(1, f"agy failed: {marker}"), marker


def test_agy_not_exhausted_on_clean_output():
    assert agy_exhausted(0, "done") is False
    assert agy_exhausted(1, "some random build error") is False


def test_agy_unknown_model_markers():
    assert agy_unknown_model("Error: unknown model 'gemini-9'") is True
    assert agy_unknown_model("model not found") is True
    assert agy_unknown_model("Invalid model specified") is True
    assert agy_unknown_model("all good") is False


def test_agy_auth_fault_markers():
    assert agy_auth_fault("UNAUTHENTICATED: credentials rejected") is True
    assert agy_auth_fault("invalid_grant: token expired") is True
    assert agy_auth_fault("not signed in") is True
    assert agy_auth_fault("credits balance is too low") is False


def test_agy_outcome_classification():
    assert agy_outcome(0, "ok") == AttemptOutcome.SUCCESS
    assert agy_outcome(3, "x") == AttemptOutcome.EXHAUSTED
    assert agy_outcome(1, "unknown model 'zzz'") == AttemptOutcome.UNKNOWN_MODEL
    assert agy_outcome(1, "invalid_grant") == AttemptOutcome.AUTH_FAULT
    assert agy_outcome(1, "weird crash") == AttemptOutcome.TRANSIENT


# --- HTTP API tiers ---------------------------------------------------------

def test_http_429_is_universal_exhaustion_signal():
    assert http_outcome(429, "{}", {}) == AttemptOutcome.EXHAUSTED


def test_http_429_with_retry_after_is_retryable():
    assert http_outcome(429, "{}", {"Retry-After": "5"}) == AttemptOutcome.RETRYABLE_429
    assert http_outcome(429, "{}", {"retry-after": "30"}) == AttemptOutcome.RETRYABLE_429


def test_http_429_with_long_retry_after_is_not_retryable():
    assert http_outcome(429, "{}", {"Retry-After": "120"}) == AttemptOutcome.EXHAUSTED


def test_http_auth_and_bad_request():
    assert http_outcome(401, "{}", {}) == AttemptOutcome.AUTH_FAULT
    assert http_outcome(403, "{}", {}) == AttemptOutcome.AUTH_FAULT
    assert http_outcome(400, "{}", {}) == AttemptOutcome.BAD_REQUEST


def test_http_402_insufficient_credits_advances():
    assert http_outcome(402, "insufficient credits", {}) == AttemptOutcome.EXHAUSTED


def test_http_5xx_and_timeout_are_transient():
    assert http_outcome(500, "boom", {}) == AttemptOutcome.TRANSIENT
    assert http_outcome(503, "boom", {}) == AttemptOutcome.TRANSIENT
    assert http_outcome(None, "", {}, timeout=True) == AttemptOutcome.TRANSIENT


def test_http_200_success():
    assert http_outcome(200, "{}", {}) == AttemptOutcome.SUCCESS


def test_parse_retry_after():
    assert parse_retry_after({"Retry-After": "5"}) == 5.0
    assert parse_retry_after({"retry-after": "0"}) == 0.0
    assert parse_retry_after({}) is None
    assert parse_retry_after({"Retry-After": "soon"}) is None


def test_parse_quota_hints_extracts_only_rate_headers():
    hints = parse_quota_hints({
        "X-Ratelimit-Remaining": "9",
        "Retry-After": "3",
        "Authorization": "Bearer SECRET",
        "Content-Type": "application/json",
    })
    assert hints["x-ratelimit-remaining"] == "9"
    assert hints["retry-after"] == "3"
    assert "authorization" not in hints
    assert "content-type" not in hints
