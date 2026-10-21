"""Unit tests: circuit breaker (E1-S6)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.breaker import CircuitBreaker  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s: float):
        self.t += s


def test_breaker_opens_after_three_exhaustions_in_window():
    clock = Clock()
    b = CircuitBreaker(exhaustions_to_open=3, window_s=600, cooldown_s=900, now_fn=clock)
    assert b.is_open("agy") is False
    b.note_exhaustion("agy")
    b.note_exhaustion("agy")
    assert b.is_open("agy") is False
    b.note_exhaustion("agy")
    assert b.is_open("agy") is True


def test_breaker_window_resets_stale_exhaustions():
    clock = Clock()
    b = CircuitBreaker(exhaustions_to_open=3, window_s=600, cooldown_s=900, now_fn=clock)
    b.note_exhaustion("agy")
    b.note_exhaustion("agy")
    clock.advance(601)  # outside the window -> counter restarts
    b.note_exhaustion("agy")
    b.note_exhaustion("agy")
    assert b.is_open("agy") is False


def test_breaker_cooldown_expires():
    clock = Clock()
    b = CircuitBreaker(exhaustions_to_open=2, window_s=600, cooldown_s=900, now_fn=clock)
    b.note_exhaustion("agy")
    b.note_exhaustion("agy")
    assert b.is_open("agy") is True
    clock.advance(901)
    assert b.is_open("agy") is False


def test_breaker_success_resets():
    clock = Clock()
    b = CircuitBreaker(exhaustions_to_open=2, window_s=600, cooldown_s=900, now_fn=clock)
    b.note_exhaustion("agy")
    b.note_success("agy")
    b.note_exhaustion("agy")
    assert b.is_open("agy") is False


def test_breaker_is_per_tier():
    clock = Clock()
    b = CircuitBreaker(exhaustions_to_open=1, window_s=600, cooldown_s=900, now_fn=clock)
    b.note_exhaustion("agy")
    assert b.is_open("agy") is True
    assert b.is_open("aistudio") is False


def test_breaker_snapshot_has_no_secrets():
    clock = Clock()
    b = CircuitBreaker(now_fn=clock)
    b.note_exhaustion("agy")
    snap = b.snapshot()
    assert snap["agy"]["circuit"] == "closed"
    assert "secret" not in str(snap).lower()
