"""Model Hub — priority-ordered provider failover chain for cloud-agy-orchestrator.

One ``ModelHub`` owns the priority ladder; every provider is a small adapter
behind a single interface; all policy (tier order, models, caps, enable flags)
is hot-loaded config, never code. See ``_bmad-output/architecture-model-hub/``.
"""

from hub.types import (  # noqa: F401
    AttemptOutcome,
    CapabilityMode,
    HubConfig,
    HubEvent,
    HubRequest,
    HubResult,
    ModelAttempt,
    TierConfig,
)
from hub.router import ModelHub  # noqa: F401
from hub.config import ConfigLoader, DEFAULT_LADDER, load_config  # noqa: F401

__all__ = [
    "AttemptOutcome",
    "CapabilityMode",
    "ConfigLoader",
    "DEFAULT_LADDER",
    "HubConfig",
    "HubEvent",
    "HubRequest",
    "HubResult",
    "ModelAttempt",
    "ModelHub",
    "TierConfig",
    "load_config",
]
