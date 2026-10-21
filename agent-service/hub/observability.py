"""Observability helpers (E1-S8, FR-4).

Structured per-attempt logging (no prompt text, no key material — AC-9),
the fixed advisory-banner copy (FR-3.2), the /hub/status payload (AC-10),
the Drive report hub block + data-use footer (NFR-5), and the Gmail one-liner.
"""

from __future__ import annotations

import logging

from hub.types import HubConfig, HubResult, ModelAttempt

logger = logging.getLogger("model-hub.attempts")


def log_attempt(attempt: ModelAttempt) -> None:
    """One structured INFO line per attempt. Never logs prompts or keys."""
    logger.info(
        "hub_attempt tier=%s provider=%s model=%s outcome=%s reason=%s latency_ms=%d",
        attempt.tier, attempt.provider, attempt.model, attempt.outcome,
        attempt.reason.replace("\n", " ")[:200], attempt.latency_ms,
    )


def advisory_banner(provider: str, model: str) -> str:
    """Fixed advisory-mode copy shown whenever an answer_only tier serves (FR-3.2)."""
    return (
        f"🤖 Advisory answer — AGY credits exhausted; served by {provider} ({model}). "
        "No code was changed."
    )


def serving_line(result: HubResult) -> str:
    """Gmail bridge one-liner (PRD Q5): appended to the bridge reply."""
    mode = "advisory" if result.capability_mode == "answer_only" else "agent"
    n = len(result.failover_attempts)
    return (
        f"🤖 Served by {result.provider} ({result.model}) — {mode} mode "
        f"· {n} tier(s) tried"
    )


def drive_hub_block(result: HubResult) -> str:
    """Hub block for the Drive latest_build_status.md report (replaces 'Model Used')."""
    lines = [
        "## 🤖 Model Hub",
        f"- **Tier Used:** `{result.tier_used}` ({result.provider})",
        f"- **Model:** `{result.model}`",
        f"- **Capability:** `{result.capability_mode}`",
    ]
    for a in result.failover_attempts:
        lines.append(
            f"  - `{a.tier}` / `{a.model or '—'}` → {a.outcome}"
            + (f" ({a.reason[:120]})" if a.reason else "")
        )
    return "\n".join(lines)


def drive_footer(cfg: HubConfig) -> str:
    """Data-use disclosure footer (NFR-5)."""
    parts = []
    for t in cfg.tiers_sorted():
        if t.data_use:
            parts.append(f"{t.provider or t.id}: {t.data_use}")
    lines = ["", "---", "", "### 🔒 Model Hub data-use notes"]
    lines += [f"- {p}" for p in parts]
    return "\n".join(lines)


def status_payload(cfg: HubConfig, config_source: str,
                   breaker_snapshot: dict, quota_usage: dict[str, int],
                   quota_hints: dict[str, dict], key_status: dict[str, bool],
                   spend_today_usd: float) -> dict:
    """GET /hub/status body. Never contains secrets (AC-10)."""
    tiers = []
    for t in cfg.tiers_sorted():
        tiers.append({
            "id": t.id,
            "order": t.order,
            "enabled": t.enabled,
            "capability": t.capability.value,
            "provider": t.provider,
            "models": list(t.models or []),
            "key_configured": bool(key_status.get(t.id, False)),
            "circuit": breaker_snapshot.get(t.id, {}).get("circuit", "closed"),
            "requests_today": quota_usage.get(t.id, 0),
            "daily_request_cap": t.daily_request_cap,
            "quota_hints": quota_hints.get(t.id, {}),
            "cost": t.cost,
        })
    return {
        "config_version": cfg.version,
        "config_source": config_source,
        "tiers": tiers,
        "paid": {
            "enabled": cfg.paid.enabled,
            "spend_today_usd": spend_today_usd,
            "cap_usd": cfg.paid.daily_spend_cap_usd,
        },
        "breaker": {
            "exhaustions_to_open": cfg.breaker.exhaustions_to_open,
            "window_s": cfg.breaker.window_s,
            "cooldown_s": cfg.breaker.cooldown_s,
        },
    }
