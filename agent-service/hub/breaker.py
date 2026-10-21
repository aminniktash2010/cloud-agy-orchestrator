"""Per-tier circuit breaker (architecture §3.4).

In-memory per instance: {tier_id: {consecutive_exhaustions, first_at, opened_until}}.
Opens after ``exhaustions_to_open`` exhaustions within ``window_s`` -> the tier
is skipped for ``cooldown_s``. Resets on first success.

Multi-instance note: state is per-instance; with max 2 instances the worst
case is one redundant attempt per instance — acceptable, documented.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("model-hub.breaker")


class CircuitBreaker:
    def __init__(
        self,
        exhaustions_to_open: int = 3,
        window_s: int = 600,
        cooldown_s: int = 900,
        now_fn=time.time,
    ) -> None:
        self.exhaustions_to_open = exhaustions_to_open
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._now = now_fn
        self._state: dict[str, dict] = {}

    # ------------------------------------------------------------------
    def _entry(self, tier_id: str) -> dict:
        return self._state.setdefault(
            tier_id, {"consecutive": 0, "first_at": 0.0, "opened_until": 0.0}
        )

    def is_open(self, tier_id: str) -> bool:
        entry = self._entry(tier_id)
        now = self._now()
        if entry["opened_until"] and now < entry["opened_until"]:
            return True
        if entry["opened_until"] and now >= entry["opened_until"]:
            # cooldown elapsed -> half-open: allow one attempt, keep counters
            entry["opened_until"] = 0.0
            entry["consecutive"] = 0
            entry["first_at"] = 0.0
        return False

    def note_exhaustion(self, tier_id: str) -> None:
        entry = self._entry(tier_id)
        now = self._now()
        if entry["first_at"] == 0.0 or (now - entry["first_at"]) > self.window_s:
            entry["consecutive"] = 1
            entry["first_at"] = now
        else:
            entry["consecutive"] += 1
        if entry["consecutive"] >= self.exhaustions_to_open:
            entry["opened_until"] = now + self.cooldown_s
            logger.warning(
                "circuit breaker OPEN for tier '%s' (%d exhaustions in %ds window; cooldown %ds)",
                tier_id, entry["consecutive"], self.window_s, self.cooldown_s,
            )

    def note_success(self, tier_id: str) -> None:
        entry = self._entry(tier_id)
        entry["consecutive"] = 0
        entry["first_at"] = 0.0
        entry["opened_until"] = 0.0

    def configure(self, exhaustions_to_open: int, window_s: int, cooldown_s: int) -> None:
        self.exhaustions_to_open = exhaustions_to_open
        self.window_s = window_s
        self.cooldown_s = cooldown_s

    def snapshot(self) -> dict[str, dict]:
        """For GET /hub/status — never includes secrets."""
        out: dict[str, dict] = {}
        for tier_id, entry in self._state.items():
            out[tier_id] = {
                "circuit": "open" if self.is_open(tier_id) else "closed",
                "consecutive_exhaustions": entry["consecutive"],
                "opened_until": entry["opened_until"] or None,
            }
        return out
