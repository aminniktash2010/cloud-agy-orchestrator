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


class HubConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    version: int = 1
    updated_at: str = ""
    paid: PaidSettings = Field(default_factory=PaidSettings)
    tiers: list[TierConfig] = Field(default_factory=list)
    breaker: BreakerSettings = Field(default_factory=BreakerSettings)
    global_: GlobalSettings = Field(default_factory=GlobalSettings, alias="global")

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
    status: str  # "success" | "ladder_empty"
    answer: str = ""
    tier_used: str = ""
    provider: str = ""
    model: str = ""
    capability_mode: str = ""
    failover_attempts: list[ModelAttempt] = Field(default_factory=list)
    advisory_banner: str = ""
    serving_line: str = ""
    ladder_empty: bool = False


class HubEvent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    kind: str  # "log" | "status" | "tier_switch" | "done" | "error"
    payload: Any = None
