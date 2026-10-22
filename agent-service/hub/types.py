"""Shared types for the Model Hub (Epic 1).

Pydantic models so the config JSON schema is validated at load time and the
API surfaces (HubResult, /hub/status) are serialization-safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class CapabilityMode(str, Enum):
    AGENT_LOOP = "agent_loop"
    ANSWER_ONLY = "answer_only"


class AttemptOutcome(str, Enum):
    SUCCESS = "success"
    EXHAUSTED = "exhausted"          # credits/quota gone -> advance tier
    RETRYABLE_429 = "retryable_429"  # 429 + usable Retry-After -> one delayed retry, same model
    AUTH_FAULT = "auth_fault"        # 401/403, bad/expired key
    BAD_REQUEST = "bad_request"      # 400-class -> surface, never advance
    TRANSIENT = "transient"          # timeouts, 5xx -> advance tier (don't spin)
    UNKNOWN_MODEL = "unknown_model"  # -> advance to next model *within* tier only


class TierConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")  # tolerate _comment_* keys in JSON

    id: str
    order: int = 0
    enabled: bool = True
    optional: bool = True
    capability: CapabilityMode = CapabilityMode.ANSWER_ONLY
    adapter: str = "openai_compat"
    provider: str = ""
    models: list[str] = Field(default_factory=list)
    api_key_env: Optional[str] = None
    base_url: Optional[str] = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    per_task_timeout_s: int = 120
    per_task_max_output_tokens: int = 4096
    daily_request_cap: Optional[int] = None
    rpm_pacing_s: float = 1.0
    model_limits_rpd: dict[str, int] = Field(default_factory=dict)
    price_per_1m_in_usd: float = 0.0
    price_per_1m_out_usd: float = 0.0
    free_only: bool = False
    paid_gate: bool = False
    cost: str = "free"
    data_use: str = ""


class BreakerSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    exhaustions_to_open: int = 3
    window_s: int = 600
    cooldown_s: int = 900


class PaidSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    daily_spend_cap_usd: float = 1.0


class GlobalSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_input_chars: int = 200000
    config_ttl_s: int = 60


class QuotaProviderSettings(BaseModel):
    """Per-provider live-quota knobs (live-quota Epic C; extra keys ignored)."""

    model_config = ConfigDict(extra="ignore")
    quota_enabled: bool = True
    probe_enabled: bool = True
    harvest_enabled: bool = True
    accounting_enabled: bool = True
    pb_field_roles: Optional[dict[str, int]] = None
    families: dict[str, dict] = Field(default_factory=dict)
    family_markers: dict[str, list[str]] = Field(default_factory=dict)
    # Epic A: OpenRouter key poll knobs (FR-6.1). key_url None -> the
    # canonical endpoint; free_rpd_cap verified 50 vs 1000 at build time
    # (2026-10-21: live key reports free_model_daily_requests.limit = 50).
    key_url: Optional[str] = None
    free_rpd_cap: int = 50


class QuotaSettings(BaseModel):
    """Live-quota subsystem config (hot-loaded, AD-4)."""

    model_config = ConfigDict(extra="ignore")
    staleness_s: int = 1800
    poller_interval_s: int = 600
    probe_interval_s: int = 600
    probe_initial_delay_s: int = 60
    family_breaker: BreakerSettings = Field(default_factory=BreakerSettings)
    providers: dict[str, QuotaProviderSettings] = Field(default_factory=dict)


class ModelCarrierMap(BaseModel):
    """One canonical model: aliases + per-tier provider model ids (operator data)."""
    model_config = ConfigDict(extra="ignore")  # tolerate _comment keys in JSON
    aliases: list[str] = Field(default_factory=list)
    carriers: dict[str, str] = Field(default_factory=dict)  # tier_id -> provider model id


class ModelCatalogConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    models: dict[str, ModelCarrierMap] = Field(default_factory=dict)


class HubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    version: int = 1
    updated_at: str = ""
    paid: PaidSettings = Field(default_factory=PaidSettings)
    tiers: list[TierConfig] = Field(default_factory=list)
    breaker: BreakerSettings = Field(default_factory=BreakerSettings)
    global_: GlobalSettings = Field(default_factory=GlobalSettings, alias="global")
    quota: QuotaSettings = Field(default_factory=QuotaSettings)
    model_catalog: ModelCatalogConfig = Field(default_factory=ModelCatalogConfig)

    def tiers_sorted(self) -> list[TierConfig]:
        return sorted(self.tiers, key=lambda t: t.order)

    def get_tier(self, tier_id: str) -> Optional[TierConfig]:
        for t in self.tiers:
            if t.id == tier_id:
                return t
        return None


class RepoContext(BaseModel):
    """Prepared by the FastAPI handler (repo prep stays out of adapters)."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    repo_dir: str
    env: dict[str, str] = Field(default_factory=dict)
    has_active_session: bool = False
    target_branch: str = "main"
    cache_tier: str = ""


class AdapterResponse(BaseModel):
    text: str = ""
    model: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)       # {"input_tokens": int, "output_tokens": int}
    quota_hints: dict[str, Any] = Field(default_factory=dict)  # parsed x-ratelimit-* headers
    latency_ms: int = 0


class HubRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    prompt: str
    repo: str = ""
    branch: str = "main"
    use_feature_branch: bool = False
    conversation_id: Optional[str] = None
    continue_session: bool = True
    repo_ctx: Optional[RepoContext] = None
    requested_model_override: Optional[str] = None  # pins first Tier-0 model (legacy ChatCommand.model)
    skip_tiers: list[str] = Field(default_factory=list)  # tier IDs the router must never attempt (MCP hub_ask defaults to ["agy"])
    model: Optional[str] = None  # free-text model pin; None/""/"auto"/"default" = unpinned (today's ladder)
    allow_model_fallback: bool = False  # loud substitution: serve a different model when no carrier serves the pin


class ModelAttempt(BaseModel):
    tier: str
    provider: str = ""
    model: str = ""
    outcome: str  # AttemptOutcome value or skip reason (disabled/circuit_open/cap_reached/missing_key/...)
    reason: str = ""
    latency_ms: int = 0
    at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    usage: dict[str, Any] = Field(default_factory=dict)
    quota_hints: dict[str, Any] = Field(default_factory=dict)


class HubResult(BaseModel):
    status: str  # "success" | "ladder_empty" | "model_unavailable"
    answer: str = ""
    tier_used: str = ""
    provider: str = ""
    model: str = ""
    capability_mode: str = ""
    failover_attempts: list[ModelAttempt] = Field(default_factory=list)
    advisory_banner: str = ""
    serving_line: str = ""
    ladder_empty: bool = False
    pinned_model: Optional[str] = None  # canonical model id when the request was pinned
    requested_model: str = ""  # raw model text the caller asked for
    model_substituted: bool = False  # True only via allow_model_fallback (loud, never silent)
    model_error: Optional[dict] = None  # structured no_match / no_carrier payload


class HubEvent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    kind: str  # "log" | "status" | "tier_switch" | "done" | "error"
    payload: Any = None
