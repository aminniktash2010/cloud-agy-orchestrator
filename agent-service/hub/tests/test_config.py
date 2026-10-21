"""Unit tests: config loading (E1-S2, AD-4)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub import config as config_mod  # noqa: E402
from hub.config import ConfigLoader, DEFAULT_LADDER  # noqa: E402
from hub.types import HubConfig  # noqa: E402


def test_default_ladder_validates():
    cfg = HubConfig.model_validate(DEFAULT_LADDER)
    ordered = cfg.tiers_sorted()
    assert [t.id for t in ordered] == ["agy", "aistudio", "groq", "nvidia", "openrouter", "xai"]
    assert cfg.paid.enabled is False
    xai = cfg.get_tier("xai")
    assert xai.enabled is False and xai.paid_gate is True
    assert cfg.get_tier("agy").capability.value == "agent_loop"


def test_env_fallback_json(monkeypatch):
    custom = {
        "version": 2,
        "tiers": [
            {"id": "aistudio", "order": 0, "adapter": "gemini", "provider": "google-ai-studio",
             "models": ["gemini-2.5-flash"], "capability": "answer_only"},
            {"id": "agy", "order": 1, "adapter": "agy", "provider": "antigravity",
             "models": ["m1"], "capability": "agent_loop", "optional": False},
        ],
    }
    monkeypatch.setenv("MODEL_HUB_CONFIG_JSON", json.dumps(custom))
    loader = ConfigLoader(ttl_s=0)
    cfg = loader.get()
    assert loader.source == "env"
    assert cfg.version == 2
    assert [t.id for t in cfg.tiers_sorted()] == ["aistudio", "agy"]


def test_invalid_json_fails_closed_to_default(monkeypatch, caplog):
    monkeypatch.setenv("MODEL_HUB_CONFIG_JSON", "{not valid json")
    loader = ConfigLoader(ttl_s=0)
    cfg = loader.get()
    assert cfg.get_tier("agy") is not None  # default ladder present
    assert cfg.version == 1


def test_secret_manager_source(monkeypatch):
    custom = {"version": 3, "tiers": []}
    monkeypatch.setattr(
        config_mod.secrets_provider, "fetch_secret",
        lambda secret_id, project_id="", ttl_s=60: json.dumps(custom),
    )
    loader = ConfigLoader(ttl_s=0)
    cfg = loader.get()
    assert loader.source == "secret-manager"
    assert cfg.version == 3


def test_ttl_caches_and_rerefreshes(monkeypatch):
    monkeypatch.setenv("MODEL_HUB_CONFIG_JSON", json.dumps({"version": 7, "tiers": []}))
    loader = ConfigLoader(ttl_s=10_000)
    assert loader.get().version == 7
    # change env: cached value wins within TTL
    monkeypatch.setenv("MODEL_HUB_CONFIG_JSON", json.dumps({"version": 8, "tiers": []}))
    assert loader.get().version == 7
    # force expiry
    loader._cached_at = 0
    assert loader.get().version == 8


def test_secret_manager_unavailable_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        config_mod.secrets_provider, "fetch_secret",
        lambda secret_id, project_id="", ttl_s=60: None,
    )
    monkeypatch.delenv("MODEL_HUB_CONFIG_JSON", raising=False)
    loader = ConfigLoader(ttl_s=0)
    cfg = loader.get()
    assert cfg.version == 1
    assert cfg.get_tier("agy") is not None
