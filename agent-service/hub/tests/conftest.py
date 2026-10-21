"""Shared fixtures for the Model Hub unit tests."""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

# agent-service/ on sys.path so `import hub...` works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.adapters import ADAPTER_REGISTRY  # noqa: E402
from hub.adapters.base import CapabilityMode, ProviderAdapter  # noqa: E402
from hub.breaker import CircuitBreaker  # noqa: E402
from hub.config import ConfigLoader  # noqa: E402
from hub.quota import QuotaTracker, SpendLedger  # noqa: E402
from hub.router import ModelHub  # noqa: E402
from hub.types import (  # noqa: E402
    AdapterResponse,
    AttemptOutcome,
    HubConfig,
    HubRequest,
    TierConfig,
)


class FakeAdapter(ProviderAdapter):
    """Scripted adapter. SCRIPT[model] = list of (outcome, payload) consumed in order.

    The router instantiates adapters with no args, so the script lives on the
    class and tests set ``FakeAdapter.SCRIPT`` per test (auto-reset by fixture).
    """

    name = "fake"
    SCRIPT: dict[str, list] = {}

    def _next(self, model: str):
        steps = list(self.SCRIPT.get(model) or self.SCRIPT.get("*") or [])
        if steps:
            self.SCRIPT[model if model in self.SCRIPT else "*"] = steps[1:]
            return steps[0]
        return AttemptOutcome.SUCCESS, AdapterResponse(text=f"fake answer from {model}", model=model)

    def __init__(self):
        pass

    LAST_KWARGS: dict = {}

    async def run(self, prompt, model, tier, timeout_s, max_output_tokens, repo_ctx=None,
                  conversation_id=None, continue_session=True):
        FakeAdapter.LAST_KWARGS = {"conversation_id": conversation_id,
                                   "continue_session": continue_session}
        return self._next(model)

    async def run_stream(self, prompt, model, tier, timeout_s, max_output_tokens, repo_ctx=None,
                         conversation_id=None, continue_session=True):
        from hub.types import HubEvent

        FakeAdapter.LAST_KWARGS = {"conversation_id": conversation_id,
                                   "continue_session": continue_session}
        outcome, payload = self._next(model)
        if isinstance(payload, AdapterResponse) and outcome == AttemptOutcome.SUCCESS:
            yield HubEvent(kind="log", payload=payload.text)
        yield HubEvent(kind="done", payload={"outcome": outcome, "response": payload,
                                             "latency_ms": 1})

    def parse_exhaustion(self, exc_or_response: Any) -> AttemptOutcome:
        return AttemptOutcome.TRANSIENT


@pytest.fixture
def registry_with_fake(monkeypatch):
    """Registers 'fake' adapters and restores the registry afterwards."""
    monkeypatch.setitem(ADAPTER_REGISTRY, "fake", FakeAdapter)
    return ADAPTER_REGISTRY


@pytest.fixture(autouse=True)
def reset_fake_script():
    FakeAdapter.SCRIPT = {}
    FakeAdapter.LAST_KWARGS = {}
    yield
    FakeAdapter.SCRIPT = {}
    FakeAdapter.LAST_KWARGS = {}


class StubConfigLoader:
    def __init__(self, cfg: HubConfig, source: str = "test"):
        self._cfg = cfg
        self.source = source

    def get(self) -> HubConfig:
        return self._cfg


def make_tier(tier_id: str, order: int, models: list[str],
              adapter: str = "fake", provider: str = "fake-provider",
              capability: CapabilityMode = CapabilityMode.ANSWER_ONLY,
              **kw) -> TierConfig:
    kw.setdefault("rpm_pacing_s", 0)
    return TierConfig(
        id=tier_id, order=order, models=models, adapter=adapter,
        provider=provider, capability=capability, **kw,
    )


def make_hub(cfg: HubConfig, **kw) -> ModelHub:
    return ModelHub(
        config_loader=StubConfigLoader(cfg),
        breaker=kw.get("breaker", CircuitBreaker()),
        quota=kw.get("quota", QuotaTracker()),
        ledger=kw.get("ledger", SpendLedger(path=Path("/tmp/hub-test-spend.json"))),
    )


def run(coro):
    return asyncio.run(coro)
