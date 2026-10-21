"""Adapter registry: adapter name -> ProviderAdapter class (E1-S3)."""

from hub.adapters.agy_adapter import AgyAdapter
from hub.adapters.gemini_adapter import GeminiAdapter
from hub.adapters.openai_compat_adapter import OpenAICompatAdapter

ADAPTER_REGISTRY: dict[str, type] = {
    "agy": AgyAdapter,
    "gemini": GeminiAdapter,
    "openai_compat": OpenAICompatAdapter,
}

__all__ = ["ADAPTER_REGISTRY"]
