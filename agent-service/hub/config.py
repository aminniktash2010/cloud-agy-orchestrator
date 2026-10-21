"""Hot-loaded hub config (E1-S2, AD-4).

Source precedence:
  1. Secret Manager ``MODEL_HUB_CONFIG`` (60s TTL) — operator edits, zero redeploy
  2. ``MODEL_HUB_CONFIG_JSON`` env var — local/dev fallback
  3. Baked ``DEFAULT_LADDER`` — cold-start seed only

Invalid config fails closed to ``DEFAULT_LADDER`` + a loud log (never crashes
the request path on a bad config push).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from hub import secrets_provider
from hub.types import HubConfig

logger = logging.getLogger("model-hub.config")

CONFIG_SECRET_ID = "MODEL_HUB_CONFIG"
CONFIG_ENV_VAR = "MODEL_HUB_CONFIG_JSON"
PROJECT_ENV_VAR = "GCP_PROJECT_ID"

# Baked cold-start seed. Mirrors _bmad-output/architecture-model-hub §4.
# Tier-0 (agy) model names are config-driven: exact names come from `agy models`
# on a signed-in session; UNKNOWN_MODEL stderr advances within the tier.
DEFAULT_LADDER: dict = {
    "version": 1,
    "updated_at": "2026-10-21",
    "paid": {"enabled": False, "daily_spend_cap_usd": 1.0},
    "tiers": [
        {
            "id": "agy",
            "order": 0,
            "enabled": True,
            "optional": False,
            "capability": "agent_loop",
            "adapter": "agy",
            "provider": "antigravity",
            "models": ["gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low"],
            "per_task_timeout_s": 540,
            "cost": "free",
            "data_use": "google-tos",
        },
        {
            "id": "aistudio",
            "order": 1,
            "enabled": True,
            "optional": True,
            "capability": "answer_only",
            "adapter": "gemini",
            "provider": "google-ai-studio",
            "api_key_env": "GEMINI_API_KEY",
            "models": ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"],
            "model_limits_rpd": {
                "gemini-2.5-flash": 1500,
                "gemini-3.5-flash-lite": 500,
                "gemini-3.8-flash": 20,
            },
            "per_task_timeout_s": 120,
            "per_task_max_output_tokens": 4096,
            "daily_request_cap": 1200,
            "rpm_pacing_s": 6,
            "cost": "free",
            "data_use": "may-train-google-models",
        },
        {
            "id": "groq",
            "order": 2,
            "enabled": True,
            "optional": True,
            "capability": "answer_only",
            "adapter": "openai_compat",
            "provider": "groq",
            "api_key_env": "GROQ_API_KEY",
            "base_url": "https://api.groq.com/openai/v1",
            "models": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
            "per_task_timeout_s": 120,
            "per_task_max_output_tokens": 4096,
            "daily_request_cap": 800,
            "rpm_pacing_s": 2,
            "cost": "free",
            "data_use": "verify-tos-at-build",
        },
        {
            "id": "nvidia",
            "order": 3,
            "enabled": True,
            "optional": True,
            "capability": "answer_only",
            "adapter": "openai_compat",
            "provider": "nvidia-build",
            "api_key_env": "NVIDIA_API_KEY",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "models": ["deepseek-v4-flash", "llama-4-maverick", "qwen-3.5"],
            "per_task_timeout_s": 120,
            "per_task_max_output_tokens": 4096,
            "daily_request_cap": None,
            "rpm_pacing_s": 1.5,
            "cost": "free",
            "data_use": "prototyping-dev-research-only",
        },
        {
            "id": "openrouter",
            "order": 4,
            "enabled": True,
            "optional": True,
            "capability": "answer_only",
            "adapter": "openai_compat",
            "provider": "openrouter",
            "api_key_env": "OPENROUTER_API_KEY",
            "base_url": "https://openrouter.ai/api/v1",
            "extra_headers": {
                "HTTP-Referer": "https://aminniktash2010.github.io/cloud-agy-orchestrator/",
                "X-Title": "Cloud AGY Orchestrator",
            },
            "models": ["openrouter/auto:free"],
            "per_task_timeout_s": 120,
            "per_task_max_output_tokens": 4096,
            "daily_request_cap": 40,
            "rpm_pacing_s": 3,
            "free_only": True,
            "cost": "free",
            "data_use": "upstream-provider-policies-vary",
        },
        {
            "id": "xai",
            "order": 5,
            "enabled": False,
            "optional": True,
            "capability": "answer_only",
            "adapter": "openai_compat",
            "provider": "xai",
            "api_key_env": "XAI_API_KEY",
            "base_url": "https://api.x.ai/v1",
            "models": ["grok-4.1-fast", "grok-4.5"],
            "price_per_1m_in_usd": 0.20,
            "price_per_1m_out_usd": 0.50,
            "per_task_timeout_s": 120,
            "per_task_max_output_tokens": 4096,
            "daily_request_cap": None,
            "rpm_pacing_s": 1,
            "cost": "paid",
            "paid_gate": True,
            "data_use": "promo-standard; data-share-opt-in-trains-xai",
        },
    ],
    "breaker": {"exhaustions_to_open": 3, "window_s": 600, "cooldown_s": 900},
    "global": {"max_input_chars": 200000, "config_ttl_s": 60},
    # Live-quota subsystem (Epic C): AGY family taxonomy seed + CLI probe knobs.
    # pb_field_roles stays null until the build-time Cloud Run capture (C-S7)
    # confirms which numeric protobuf field is quota_remaining — until then the
    # probe degrades honestly to estimated/detail-only, never guessed numbers.
    "quota": {
        "staleness_s": 1800,
        "poller_interval_s": 600,
        "probe_interval_s": 600,
        "probe_initial_delay_s": 60,
        "family_breaker": {"exhaustions_to_open": 3, "window_s": 600, "cooldown_s": 900},
        "providers": {
            "agy": {
                "probe_enabled": True,
                "pb_field_roles": None,
                "families": {
                    # Order matters: substring ties resolve in config order, so
                    # the distinctive "argon" family precedes the generic
                    # "gemini" (e.g. "gemini-4-argon" is Argon-class). The
                    # first real Cloud Run probe run (C-S7) confirms this
                    # against the live model list.
                    "argon": {"label": "Gemini Argon-class", "match": ["argon"]},
                    "gemini": {"label": "Gemini Models", "match": ["gemini"]},
                    "claude_gpt": {"label": "Claude and GPT models", "match": ["claude", "gpt"]},
                },
                "family_markers": {
                    "claude_gpt": ["claude", "anthropic"],
                    "argon": ["argon"],
                },
            },
            # Epic A: OpenRouter live key poll. free_rpd_cap verified
            # 2026-10-21 against the stored key: the /api/v1/key response
            # carries free_model_daily_requests.limit = 50 (not 1000).
            "openrouter": {
                "quota_enabled": True,
                "key_url": "https://openrouter.ai/api/v1/key",
                "free_rpd_cap": 50,
            },
        },
    },
}


class ConfigLoader:
    """Hot-reloading hub config with TTL caching."""

    def __init__(self, ttl_s: int = 60) -> None:
        self._ttl_s = ttl_s
        self._cached: HubConfig | None = None
        self._cached_at: float = 0.0
        self._source: str = "default"
        self._lock = threading.Lock()

    def get(self) -> HubConfig:
        now = time.time()
        with self._lock:
            if self._cached and (now - self._cached_at) < self._ttl_s:
                return self._cached
        cfg, source = self._load()
        with self._lock:
            self._cached = cfg
            self._cached_at = now
            self._source = source
        return cfg

    @property
    def source(self) -> str:
        return self._source

    def _load(self) -> tuple[HubConfig, str]:
        ttl = self._ttl_s
        try:
            global_cfg = None
            # 1. Secret Manager API
            raw = secrets_provider.fetch_secret(
                CONFIG_SECRET_ID,
                project_id=os.environ.get(PROJECT_ENV_VAR, ""),
                ttl_s=ttl,
            )
            if raw:
                return self._parse(raw, "secret-manager"), "secret-manager"
            # 2. Env JSON
            raw = os.environ.get(CONFIG_ENV_VAR, "")
            if raw:
                return self._parse(raw, "env"), "env"
        except Exception as e:
            logger.error("MODEL_HUB_CONFIG load failed; failing closed to DEFAULT_LADDER: %s", e)
        return HubConfig.model_validate(DEFAULT_LADDER), "default"

    @staticmethod
    def _parse(raw: str, source: str) -> HubConfig:
        try:
            data = json.loads(raw)
            return HubConfig.model_validate(data)
        except Exception as e:
            logger.error(
                "Invalid MODEL_HUB_CONFIG from %s (%s); failing closed to DEFAULT_LADDER",
                source,
                e,
            )
            return HubConfig.model_validate(DEFAULT_LADDER)


_loader = ConfigLoader()


def load_config() -> HubConfig:
    """Process-wide hot-loaded config (60s TTL)."""
    return _loader.get()
