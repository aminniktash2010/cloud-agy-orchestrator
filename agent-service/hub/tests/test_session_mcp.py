"""Unit + integration tests for self-healing model-hub MCP registration.

Covers PRD AC-1…AC-6 (unit level) plus build_isolated_env() integration
(AC-5 caller level, stale-token refresh, trailing-slash URL).
"""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.session_mcp import ensure_hub_mcp_entry  # noqa: E402

HUB_URL = "https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app"
TOKEN = "test-token-abc123"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def entry_of(path: Path) -> dict:
    return read_json(path)["mcpServers"]["model-hub"]


# --- AC-1: fresh write -------------------------------------------------------

def test_fresh_write_creates_exact_entry_with_mode_600(tmp_path):
    cfg = tmp_path / "sub" / "dir" / "mcp_config.json"
    changed = ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN)
    assert changed is True
    assert cfg.exists()
    entry = entry_of(cfg)
    assert entry["serverUrl"] == f"{HUB_URL}/mcp"
    assert entry["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert entry["disabled"] is False
    # key order matches what `agy mcp add` itself writes (probe-verified)
    assert list(entry.keys()) == ["disabled", "headers", "serverUrl"]
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


# --- AC-2: third-party entries preserved -------------------------------------

def test_third_party_entries_preserved(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    before = {
        "mcpServers": {
            "my-server": {
                "command": "npx",
                "args": ["-y", "my-mcp"],
                "headers": {"X-Custom": "keep-me"},
            }
        }
    }
    cfg.write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
    changed = ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN)
    assert changed is True
    data = read_json(cfg)
    assert data["mcpServers"]["my-server"] == before["mcpServers"]["my-server"]
    assert entry_of(cfg)["headers"]["Authorization"] == f"Bearer {TOKEN}"


# --- AC-3: stale token updated, user disabled respected -----------------------

def test_stale_token_updated_but_user_disabled_respected(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "model-hub": {
                "disabled": True,  # user explicitly disabled — must survive
                "headers": {"Authorization": "Bearer stale-token"},
                "serverUrl": "https://old.example/mcp",
                "customKey": "untouched",
            }
        }
    }), encoding="utf-8")
    changed = ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN)
    assert changed is True
    entry = entry_of(cfg)
    assert entry["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert entry["serverUrl"] == f"{HUB_URL}/mcp"
    assert entry["disabled"] is True
    assert entry["customKey"] == "untouched"


def test_missing_headers_dict_created(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"model-hub": {"disabled": False, "serverUrl": "x"}}
    }), encoding="utf-8")
    assert ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN) is True
    assert entry_of(cfg)["headers"]["Authorization"] == f"Bearer {TOKEN}"


# --- AC-4: idempotent, no churn ----------------------------------------------

def test_idempotent_rerun_writes_nothing(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    assert ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN) is True
    digest_before = hashlib.sha256(cfg.read_bytes()).hexdigest()
    mtime_before = cfg.stat().st_mtime_ns
    changed = ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN)
    assert changed is False
    assert hashlib.sha256(cfg.read_bytes()).hexdigest() == digest_before
    assert cfg.stat().st_mtime_ns == mtime_before


# --- AC-6: corrupt file replaced ----------------------------------------------

def test_corrupt_file_replaced_fail_closed(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text("{not valid json!!!", encoding="utf-8")
    changed = ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN)
    assert changed is True
    entry = entry_of(cfg)  # must not raise
    assert entry["serverUrl"] == f"{HUB_URL}/mcp"


def test_malformed_mcp_servers_non_dict_handled(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps({"mcpServers": "junk", "other": 1}), encoding="utf-8")
    assert ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN) is True
    data = read_json(cfg)
    assert data["other"] == 1
    assert entry_of(cfg)["serverUrl"] == f"{HUB_URL}/mcp"


def test_top_level_non_dict_replaced(tmp_path):
    cfg = tmp_path / "mcp_config.json"
    cfg.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert ensure_hub_mcp_entry(cfg, HUB_URL, TOKEN) is True
    assert entry_of(cfg)["headers"]["Authorization"] == f"Bearer {TOKEN}"


# --- integration through build_isolated_env -----------------------------------

def _import_main():
    import main as main_module  # noqa: E402
    return main_module


def test_build_isolated_env_injects_entry(tmp_path, monkeypatch):
    main_module = _import_main()
    monkeypatch.setenv("HUB_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("HUB_PUBLIC_URL", raising=False)
    repo = tmp_path / "some-repo"
    main_module.build_isolated_env(repo)
    cfg = repo / ".agy_session" / ".gemini" / "config" / "mcp_config.json"
    assert cfg.exists()
    entry = entry_of(cfg)
    assert entry["serverUrl"] == f"{HUB_URL}/mcp"
    assert entry["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_build_isolated_env_skips_without_token(tmp_path, monkeypatch, caplog):
    main_module = _import_main()
    monkeypatch.delenv("HUB_MCP_TOKEN", raising=False)
    repo = tmp_path / "some-repo"
    # AC-5: setup must succeed, nothing written, warning logged
    main_module.build_isolated_env(repo)
    cfg = repo / ".agy_session" / ".gemini" / "config" / "mcp_config.json"
    assert not cfg.exists()


def test_build_isolated_env_refreshes_stale_token(tmp_path, monkeypatch):
    main_module = _import_main()
    monkeypatch.setenv("HUB_MCP_TOKEN", TOKEN)
    monkeypatch.delenv("HUB_PUBLIC_URL", raising=False)
    repo = tmp_path / "some-repo"
    session_home = repo / ".agy_session"
    cfg = session_home / ".gemini" / "config" / "mcp_config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "mcpServers": {
            "model-hub": {
                "disabled": False,
                "headers": {"Authorization": "Bearer stale-after-deploy"},
                "serverUrl": f"{HUB_URL}/mcp",
            }
        }
    }), encoding="utf-8")
    main_module.build_isolated_env(repo)
    assert entry_of(cfg)["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_build_isolated_env_trailing_slash_url_normalized(tmp_path, monkeypatch):
    main_module = _import_main()
    monkeypatch.setenv("HUB_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("HUB_PUBLIC_URL", HUB_URL + "/")
    repo = tmp_path / "some-repo"
    main_module.build_isolated_env(repo)
    cfg = repo / ".agy_session" / ".gemini" / "config" / "mcp_config.json"
    assert entry_of(cfg)["serverUrl"] == f"{HUB_URL}/mcp"
    assert "//mcp" not in entry_of(cfg)["serverUrl"]
