"""Live-quota telemetry store (live-quota Epic C, AD-9/AD-10/AD-17).

Advisory display state ONLY — enforcement (daily caps, RPM pacing, spend
ledger) stays in ``hub/quota.py``, which this module never touches.

Staleness downgrade is computed at READ time in ``snapshot()``: a stored value
older than the threshold is presented as ``estimated``, by construction. The
store never rewrites history; presentation downgrades, storage doesn't.

``quota_source`` is a four-way enum (AD-9):
  live      — polled from a provider API (OpenRouter, Epic A) or the on-device
              CLI probe (AGY, Epic C)
  headers   — harvested from real response headers (Groq, Epic B)
  measured  — locally accumulated accounting we computed ourselves
              (AI Studio tokens, Requesty spend, Epic B) — honest, but NOT
              "remaining quota"
  estimated — static config cap + local request counter (today's display)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("model-hub.live-quota")

QUOTA_SOURCES = ("live", "headers", "measured", "estimated")

# Honest per-tier display text while a tier has no live writer (Epic C:
# every non-AGY tier is `estimated`; Epics A/B add their writers).
_ESTIMATED_DETAIL = {
    "agy": "AGY quota probe has not reported yet",
    "aistudio": "no public quota API — 429-driven breaker is the protection",
    "groq": "no public quota API — 429-driven breaker is the protection",
    "nvidia": "no public quota API — 429-driven breaker is the protection",
    "openrouter": "OpenRouter key poll has not reported yet",
    "xai": "tier disabled",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_reset_in(total_seconds: float) -> str:
    """Compact human reset countdown ('5d 6h', '42m', '1h 5m').

    Matches the language of the Antigravity usage page Amin already reads.
    """
    s = max(0, int(total_seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    if m:
        return f"{m}m"
    return "<1m"


def _age_note(updated_at: datetime, now: datetime) -> str:
    age_s = (now - updated_at).total_seconds()
    if age_s < 90:
        return "just now"
    return format_reset_in(age_s) + " ago"


class LiveQuotaStore:
    """In-memory, per-instance live-quota facts.

    Writers (the background poller/probe, and later the router's _record
    funnel) swap whole structures atomically; ``snapshot()`` is a lock-free
    read. The asyncio lock guards writer critical sections only.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        # AGY probe state (Epic C)
        self._probe_families: list[dict] = []      # FamilyQuota dicts, see below
        self._probe_taxonomy: dict[str, str] = {}  # model name -> family id
        self._probe_unmapped: list[dict] = []      # buckets no family claimed
        self._probe_at: datetime | None = None
        self._probe_detail: str = "probe has not run yet"
        # Per-provider poller health: name -> {"healthy": bool, "last_run": iso|None}
        self._providers: dict[str, dict] = {}
        # Key-poll results (Epic A): provider -> {"remaining_pct", "reset_in",
        # "detail", "at"}. Writers swap the whole dict atomically.
        self._poll: dict[str, dict] = {}

    def _get_lock(self) -> asyncio.Lock:
        # Lazily created: the store may be constructed at import time with no
        # running loop (asyncio.Lock binds on first use in 3.10+).
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------
    # writers (background plane only)
    # ------------------------------------------------------------------
    async def set_probe_result(
        self,
        families: list[dict],
        taxonomy: dict[str, str],
        unmapped: list[dict],
        at: datetime,
        detail: str = "",
    ) -> None:
        """Record one AGY CLI probe run.

        FamilyQuota dict shape:
          {"id", "label", "models": [...], "remaining_pct": float|None,
           "resets_at_epoch": float|None, "note": str}
        """
        async with self._get_lock():
            self._probe_families = [dict(f) for f in families]
            self._probe_taxonomy = dict(taxonomy)
            self._probe_unmapped = [dict(u) for u in unmapped]
            self._probe_at = at
            self._probe_detail = detail or "probe ok"

    async def set_poller_health(
        self,
        provider: str,
        healthy: bool,
        at: datetime | None = None,
        detail: str = "",
    ) -> None:
        async with self._get_lock():
            self._providers[provider] = {
                "healthy": bool(healthy),
                "last_run": (at or _utcnow()).isoformat(),
                "detail": detail,
            }

    async def set_poll_result(
        self,
        provider: str,
        remaining_pct: float | None,
        reset_in: str | None,
        detail: str,
        at: datetime,
    ) -> None:
        """Record one key-poll run (Epic A: OpenRouter).

        ``remaining_pct`` may be None (AD-19 step 3: the key reported no
        usable remaining figure) — the snapshot still labels the tier
        ``live`` because the *reading* is fresh; the null is honest.
        """
        async with self._get_lock():
            self._poll[provider] = {
                "remaining_pct": remaining_pct,
                "reset_in": reset_in,
                "detail": detail,
                "at": at,
            }

    # ------------------------------------------------------------------
    # sync reads (request path: cache reads only)
    # ------------------------------------------------------------------
    def taxonomy(self) -> dict[str, str]:
        """Latest probe model->family map (empty until the first probe run)."""
        return dict(self._probe_taxonomy)

    # ------------------------------------------------------------------
    # snapshot (AD-17: staleness downgrade computed here, at read time)
    # ------------------------------------------------------------------
    def snapshot(
        self,
        cfg: Any,
        now: datetime,
        staleness_s: int,
        family_circuits: dict[str, str] | None = None,
    ) -> dict:
        """Builds the additive payload block.

        Returns {"tiers": {tier_id: live_quota-block}, "poller": {...}}.
        Pure in (stored state, now, threshold, family_circuits) — property-
        testable with a frozen clock (AC-3).
        """
        family_circuits = family_circuits or {}
        tiers: dict[str, dict] = {}
        for t in cfg.tiers_sorted():
            if t.id == "agy":
                tiers[t.id] = self._agy_block(cfg, t, now, staleness_s, family_circuits)
            elif t.id == "openrouter":
                tiers[t.id] = self._openrouter_block(now, staleness_s)
            else:
                tiers[t.id] = {
                    "quota_source": "estimated",
                    "quota_updated_at": None,
                    "remaining_pct": None,
                    "reset_in": None,
                    "detail": _ESTIMATED_DETAIL.get(
                        t.id, "no live quota signal for this tier yet"
                    ),
                }
        providers = {
            name: {"healthy": p["healthy"], "last_run": p["last_run"]}
            for name, p in self._providers.items()
        }
        last_runs = [p["last_run"] for p in self._providers.values() if p.get("last_run")]
        poller = {
            "healthy": all(p["healthy"] for p in self._providers.values())
            if self._providers
            else True,
            "last_run": max(last_runs) if last_runs else None,
            "providers": providers,
        }
        return {"tiers": tiers, "poller": poller}

    # ------------------------------------------------------------------
    def _agy_block(
        self,
        cfg: Any,
        tier: Any,
        now: datetime,
        staleness_s: int,
        family_circuits: dict[str, str],
    ) -> dict:
        probe = self._providers.get("agy_probe", {})
        probe_healthy = probe.get("healthy", True)
        age_s = (now - self._probe_at).total_seconds() if self._probe_at else None
        fresh = (
            self._probe_at is not None
            and age_s is not None
            and age_s <= staleness_s
            and bool(self._probe_families)
        )
        quota_at = self._probe_at.isoformat() if self._probe_at else None

        if probe_healthy and fresh:
            return self._agy_live_block(now, quota_at, family_circuits)

        # Degraded: config-seed families with circuit states only, honest detail.
        seed = self._seed_families(cfg)
        rows = [
            {
                "id": fid,
                "label": rule.get("label", fid),
                "models": [],
                "circuit": family_circuits.get(fid, "closed"),
                "remaining_pct": None,
                "reset_in": None,
                "quota_source": "estimated",
                "quota_updated_at": quota_at,
                "note": "",
            }
            for fid, rule in seed.items()
        ]
        if probe_healthy and self._probe_at is not None:
            detail = f"{self._probe_detail} (last live reading {_age_note(self._probe_at, now)})"
        elif not probe_healthy:
            detail = probe.get("detail") or "quota probe unhealthy"
            if self._probe_at is not None:
                detail += f" (last live reading {_age_note(self._probe_at, now)})"
        else:
            detail = self._probe_detail
        other = self._other_row(quota_at, family_circuits, "estimated")
        if other is not None:
            rows.append(other)
        return {
            "quota_source": "estimated",
            "quota_updated_at": quota_at,
            "remaining_pct": None,
            "reset_in": None,
            "detail": detail,
            "families": rows,
            "unmapped_buckets": [],
        }

    def _agy_live_block(
        self, now: datetime, quota_at: str | None, family_circuits: dict[str, str]
    ) -> dict:
        rows = []
        for f in self._probe_families:
            resets = f.get("resets_at_epoch")
            rows.append(
                {
                    "id": f["id"],
                    "label": f.get("label", f["id"]),
                    "models": list(f.get("models", [])),
                    "circuit": family_circuits.get(f["id"], "closed"),
                    "remaining_pct": f.get("remaining_pct"),
                    "reset_in": format_reset_in(resets - now.timestamp())
                    if resets
                    else None,
                    "quota_source": "live",
                    "quota_updated_at": quota_at,
                    "note": f.get("note", ""),
                }
            )
        pcts = [
            (r["remaining_pct"], r)
            for r in rows
            if r["remaining_pct"] is not None
        ]
        if pcts:
            binding_pct, binding = min(pcts, key=lambda p: p[0])
            tier_pct: float | None = round(binding_pct, 1)
            tier_reset = binding["reset_in"]
            parts = []
            for r in rows:
                if r["remaining_pct"] is None:
                    parts.append(f"{r['label']}: unknown")
                elif r["remaining_pct"] <= 0:
                    parts.append(f"{r['label']} exhausted")
                else:
                    parts.append(f"{r['label']} {r['remaining_pct']:.0f}%")
            detail = " · ".join(parts)
        else:
            tier_pct, tier_reset, detail = None, None, self._probe_detail
        other = self._other_row(quota_at, family_circuits, "estimated")
        if other is not None:
            rows.append(other)
        return {
            "quota_source": "live",
            "quota_updated_at": quota_at,
            "remaining_pct": tier_pct,
            "reset_in": tier_reset,
            "detail": detail,
            "families": rows,
            # Buckets the probe could not map to any family: visible, never dropped.
            "unmapped_buckets": [
                {
                    "name": u.get("name", ""),
                    "remaining_pct": u.get("remaining_pct"),
                    "reset_in": format_reset_in(u["resets_at_epoch"] - now.timestamp())
                    if u.get("resets_at_epoch")
                    else None,
                }
                for u in self._probe_unmapped
            ],
        }

    def _openrouter_block(
        self, now: datetime, staleness_s: int
    ) -> dict:
        """Tier-level block for the OpenRouter key poll (Epic A).

        Mirrors the AGY degrade logic: fresh + healthy -> ``live``;
        otherwise ``estimated`` with an honest detail (AD-17 — the staleness
        downgrade is computed here, at read time).
        """
        health = self._providers.get("openrouter", {})
        healthy = health.get("healthy", True)
        poll = self._poll.get("openrouter")
        at = poll["at"] if poll else None
        quota_at = at.isoformat() if at else None
        age_s = (now - at).total_seconds() if at else None
        fresh = at is not None and age_s is not None and age_s <= staleness_s

        if healthy and fresh:
            return {
                "quota_source": "live",
                "quota_updated_at": quota_at,
                "remaining_pct": poll["remaining_pct"],
                "reset_in": poll["reset_in"],
                "detail": poll["detail"],
            }

        if not healthy:
            detail = health.get("detail") or "OpenRouter key poll failing"
        elif at is not None:
            detail = (
                "OpenRouter key poll stale "
                f"(last live reading {_age_note(at, now)})"
            )
        else:
            detail = _ESTIMATED_DETAIL["openrouter"]
        if at is not None and not healthy:
            detail += f" (last live reading {_age_note(at, now)})"
        return {
            "quota_source": "estimated",
            "quota_updated_at": quota_at,
            "remaining_pct": None,
            "reset_in": None,
            "detail": detail,
        }

    @staticmethod
    def _seed_families(cfg: Any) -> dict:
        try:
            providers = cfg.quota.providers or {}
            agy = providers.get("agy")
            fams = agy.families if agy is not None else {}
            return dict(fams) if fams else {}
        except Exception:  # noqa: BLE001 - config shape drift must not break status
            return {}

    def _other_row(
        self,
        quota_at: str | None,
        family_circuits: dict[str, str],
        source: str,
    ) -> dict | None:
        """Row for probe-taxonomy models no family claimed ("other").

        They are still attempted and breaker-tracked (AD-15); this keeps them
        visible instead of silently absent from families[].
        """
        models = sorted(m for m, f in self._probe_taxonomy.items() if f == "other")
        if not models:
            return None
        return {
            "id": "other",
            "label": "Other models",
            "models": models,
            "circuit": family_circuits.get("other", "closed"),
            "remaining_pct": None,
            "reset_in": None,
            "quota_source": source,
            "quota_updated_at": quota_at,
            "note": "no family mapping yet",
        }
