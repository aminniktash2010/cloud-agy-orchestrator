"""Unit tests: model catalog normalization / resolution / suggestions (P1-S1)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.model_catalog import ModelCatalog, normalize_model_name  # noqa: E402
from hub.types import ModelCatalogConfig, ModelCarrierMap  # noqa: E402


def catalog() -> ModelCatalog:
    return ModelCatalog(ModelCatalogConfig(models={
        "claude-opus-5.5": ModelCarrierMap(
            aliases=["claude opus 5.5", "opus 5.5", "anthropic/claude-opus-5.5"],
            carriers={"openrouter": "anthropic/claude-opus-5.5",
                      "requesty": "anthropic/claude-opus-5-5"}),
        "gpt-oss-120b": ModelCarrierMap(
            aliases=["gpt oss 120b"],
            carriers={"groq": "openai/gpt-oss-120b"}),
    }))


# --- normalization ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Claude Opus 5.5", "claude-opus-5.5"),
    ("anthropic/claude-opus-5.5", "claude-opus-5.5"),
    ("  OPUS 5.5  ", "opus-5.5"),
    ("gpt_oss__120b", "gpt-oss-120b"),
    ("GEMINI-3.8-FLASH-HIGH", "gemini-3.8-flash-high"),
    ("  ", ""),
    ("", ""),
])
def test_normalize_table(raw, expected):
    assert normalize_model_name(raw) == expected


def test_dots_preserved_dash_dot_diverge():
    # 5.5 != 5-5: the divergence is absorbed by aliases, never fuzzy rules.
    assert normalize_model_name("claude-opus-5.5") != normalize_model_name("claude-opus-5-5")


# --- resolution -------------------------------------------------------------

def test_resolve_canonical_exact():
    can, entry = catalog().resolve("claude-opus-5.5")
    assert can == "claude-opus-5.5"
    assert entry.carriers["requesty"] == "anthropic/claude-opus-5-5"


def test_resolve_alias_free_text():
    can, _ = catalog().resolve("Claude Opus 5.5")
    assert can == "claude-opus-5.5"


def test_resolve_vendor_prefixed_alias():
    can, _ = catalog().resolve("anthropic/claude-opus-5.5")
    assert can == "claude-opus-5.5"


def test_resolve_short_alias():
    can, _ = catalog().resolve("opus 5.5")
    assert can == "claude-opus-5.5"


def test_resolve_no_match_returns_none():
    assert catalog().resolve("clod opus 99") is None
    assert catalog().resolve("") is None
    assert catalog().resolve("   ") is None


# --- suggestions ------------------------------------------------------------

def test_suggest_close_match():
    assert "claude-opus-5.5" in catalog().suggest("clod opus 99")


def test_suggest_prefix_match():
    assert "claude-opus-5.5" in catalog().suggest("opus")


def test_suggest_unrelated_returns_empty():
    assert catalog().suggest("zzzzzz-nope") == []


def test_suggest_bounded():
    assert len(catalog().suggest("o", n=2)) <= 2


# --- config validation (fail-soft) ------------------------------------------

def test_config_validation_drops_bad_carriers():
    from hub.config import ConfigLoader
    import json
    raw = json.dumps({
        "version": 1,
        "tiers": [{"id": "requesty", "order": 0, "models": ["x"]}],
        "model_catalog": {"models": {
            "claude-opus-5.5": {
                "aliases": [],
                "carriers": {
                    "requesty": "anthropic/claude-opus-5-5",
                    "nope-tier": "whatever",
                    "empty-tier": "   ",
                },
            },
        }},
    })
    cfg = ConfigLoader._parse(raw, "test")
    carriers = cfg.model_catalog.models["claude-opus-5.5"].carriers
    assert carriers == {"requesty": "anthropic/claude-opus-5-5"}


def test_config_without_catalog_loads_empty():
    from hub.config import ConfigLoader
    import json
    cfg = ConfigLoader._parse(json.dumps({"version": 1, "tiers": []}), "test")
    assert cfg.model_catalog.models == {}
