"""Provider adapter ABC (AD-3: fallback tiers are answer_only by interface).

``answer_only`` adapters expose no shell/file/git surface at all — they return
text, period. Honesty is an interface boundary, not a prompt instruction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from hub.types import AttemptOutcome, CapabilityMode, HubEvent, TierConfig


class ProviderAdapter(ABC):
    """Single interface every provider implements. Never raises provider SDK
    errors past the router: everything maps to (AttemptOutcome, payload)."""

    name: str = "base"
    capability: CapabilityMode = CapabilityMode.ANSWER_ONLY

    @abstractmethod
    async def run(
        self,
        prompt: str,
        model: str,
        tier: TierConfig,
        timeout_s: int,
        max_output_tokens: int,
        repo_ctx: Any = None,
    ) -> tuple[AttemptOutcome, Any]:
        """Collect mode. Returns (outcome, AdapterResponse | reason-str)."""

    @abstractmethod
    async def run_stream(
        self,
        prompt: str,
        model: str,
        tier: TierConfig,
        timeout_s: int,
        max_output_tokens: int,
        repo_ctx: Any = None,
    ) -> AsyncIterator[HubEvent]:
        """Streaming mode: yields HubEvent(kind='log'|'status'|'error')."""

    @abstractmethod
    def parse_exhaustion(self, exc_or_response: Any) -> AttemptOutcome:
        """Maps an exception/response to AttemptOutcome (AD-5, NFR-3)."""

    def key_configured(self, tier: TierConfig) -> bool:
        """True when the tier's required API key is present in the environment."""
        import os

        if not tier.api_key_env:
            return True  # tiers without a key requirement (e.g. agy OAuth)
        return bool(os.environ.get(tier.api_key_env, "").strip())
