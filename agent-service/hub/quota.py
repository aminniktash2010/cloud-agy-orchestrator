"""Quota, budgets, pacing, and the paid spend ledger (architecture §7).

- RPM pacing: router sleeps ``rpm_pacing_s`` remainder before each attempt.
- Daily request caps: in-memory, date-keyed (UTC); at cap the tier is skipped.
- Per-task budgets: ``max_output_tokens`` for answer_only (adapter-enforced);
  ``max_input_chars`` global — over-length prompts are tail-truncated with an
  appended honesty note.
- Paid spend ledger (Epic 4 wiring): ``/mnt/cache/hub/spend.json`` on the
  shared GCS cold-cache mount (visible to all instances). Atomic-ish write via
  temp-file rename; last-writer-wins is acceptable at this volume.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("model-hub.quota")

SPEND_LEDGER_PATH = Path("/mnt/cache/hub/spend.json")

TRUNCATION_NOTE = (
    "\n\n[hub note: input truncated to {n} chars; the answer may be partial.]"
)


def truncate_prompt(prompt: str, max_chars: int) -> tuple[str, bool]:
    if max_chars and len(prompt) > max_chars:
        note = TRUNCATION_NOTE.format(n=max_chars)
        if len(note) >= max_chars:
            return note[:max_chars], True
        return prompt[: max_chars - len(note)] + note, True
    return prompt, False


class QuotaTracker:
    """Daily caps + RPM pacing, in-memory per instance."""

    def __init__(self, now_fn=time.time) -> None:
        self._now = now_fn
        self._lock = threading.Lock()
        self._daily_counts: dict[str, dict[str, int]] = {}  # tier_id -> {date: count}
        self._last_attempt_at: dict[str, float] = {}        # tier_id -> monotonic ts

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def check_cap(self, tier_id: str, daily_cap: int | None) -> bool:
        """True when the tier may still be used today (cap not reached)."""
        if not daily_cap:
            return True
        with self._lock:
            used = self._daily_counts.get(tier_id, {}).get(self._today(), 0)
        return used < daily_cap

    def account_attempt(self, tier_id: str) -> None:
        with self._lock:
            day = self._today()
            bucket = self._daily_counts.setdefault(tier_id, {})
            bucket[day] = bucket.get(day, 0) + 1
            self._last_attempt_at[tier_id] = self._now()

    def pacing_wait_s(self, tier_id: str, rpm_pacing_s: float) -> float:
        """Seconds the router should sleep before the next attempt on this tier."""
        if not rpm_pacing_s or rpm_pacing_s <= 0:
            return 0.0
        with self._lock:
            last = self._last_attempt_at.get(tier_id, 0.0)
        elapsed = self._now() - last
        return max(0.0, rpm_pacing_s - elapsed)

    def usage_today(self, tier_id: str) -> int:
        with self._lock:
            return self._daily_counts.get(tier_id, {}).get(self._today(), 0)


class SpendLedger:
    """Paid spend accounting (AD-7 triple gate support). Epic 4 wires xAI here.

    Ledger file: {"date": "YYYY-MM-DD", "usd": 0.0}. Rolls over daily.
    """

    def __init__(self, path: Path = SPEND_LEDGER_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("date") == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                return {"date": data["date"], "usd": float(data.get("usd", 0.0))}
        except Exception:
            pass
        return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "usd": 0.0}

    def spend_today_usd(self) -> float:
        with self._lock:
            return self._read()["usd"]

    def can_spend(self, estimated_usd: float, cap_usd: float) -> bool:
        with self._lock:
            current = self._read()["usd"]
        return (current + estimated_usd) <= cap_usd

    def record(self, usd: float) -> float:
        """Adds usd to today's total; returns the new total."""
        with self._lock:
            data = self._read()
            data["usd"] = round(data["usd"] + usd, 6)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix="spend.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self._path)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return data["usd"]

    @staticmethod
    def estimate_cost_usd(input_tokens: int, output_tokens: int,
                          price_in: float, price_out: float) -> float:
        return (input_tokens / 1_000_000) * price_in + (output_tokens / 1_000_000) * price_out
