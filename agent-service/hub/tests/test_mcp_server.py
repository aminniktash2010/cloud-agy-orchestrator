"""Tests for the Hub MCP server (Epic M1) — AC-1…AC-8 + AD-24 loop-safety layers."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from hub import HubRequest
from hub.mcp_server import (
    MCP_MAX_CONTEXT_CHARS,
    MCP_MAX_QUESTION_CHARS,
    SUPPORTED_PROTOCOL_VERSIONS,
    handle_mcp,
    require_mcp_token,
)
from hub.types import CapabilityMode, HubConfig

from conftest import make_hub, make_tier, registry_with_fake, run  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEST_TOKEN = "test-hub-mcp-token-abc123"


def _hub_two_tiers(**kw):
    """Hub with tier 0 = real agy adapter (never instantiated when skipped)
    and tier 1 = fake adapter that answers."""
    cfg = HubConfig(tiers=[
        make_tier("agy", 0, ["m1"], adapter="agy", provider="antigravity",
                  capability=CapabilityMode.AGENT_LOOP),
        make_tier("fallback", 1, ["m2"], adapter="fake", provider="fake-provider"),
    ])
    return make_hub(cfg, **kw)


def _call_body(name, arguments, rid=7):
    return {"jsonrpc": "2.0", "id": rid,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _text_of(result):
    assert result["content"][0]["type"] == "text"
    assert result.get("isError") is False
    return json.loads(result["content"][0]["text"])


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------
def test_initialize_negotiates_offered_version():
    code, resp = run(handle_mcp(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"}}},
        None))
    assert code == 200
    assert resp["result"]["protocolVersion"] == "2025-03-26"
    assert resp["result"]["capabilities"] == {"tools": {}}
    assert resp["result"]["serverInfo"]["name"] == "model-hub"


def test_initialize_unknown_version_falls_back_to_newest():
    code, resp = run(handle_mcp(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01"}},
        None))
    assert code == 200
    assert resp["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]


def test_notification_returns_202_empty():
    code, resp = run(handle_mcp(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        None))
    assert code == 202
    assert resp is None


def test_unknown_method_is_32601():
    code, resp = run(handle_mcp(
        {"jsonrpc": "2.0", "id": 3, "method": "frobnicate", "params": {}},
        None))
    assert code == 200
    assert resp["error"]["code"] == -32601


def test_malformed_shape_is_32600():
    code, resp = run(handle_mcp({"nope": True}, None))
    assert code == 200
    assert resp["error"]["code"] == -32600


def test_batch_dispatches_each_item():
    code, resp = run(handle_mcp([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ], None))
    assert code == 200
    assert [r["id"] for r in resp] == [1, 2]  # notification produces no entry


def test_tools_list_returns_exactly_two_tools():
    code, resp = run(handle_mcp(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        None))
    assert code == 200
    tools = resp["result"]["tools"]
    assert [t["name"] for t in tools] == ["hub_ask", "hub_status"]
    ask = tools[0]
    assert ask["inputSchema"]["required"] == ["question"]
    assert "skip_tiers" in ask["inputSchema"]["properties"]


def test_unknown_tool_is_32602():
    code, resp = run(handle_mcp(_call_body("nope", {}), None))
    assert resp["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# AC-1 / AC-2: hub_ask E2E with default skip (hub consumes fake tier only)
# ---------------------------------------------------------------------------
def test_hub_ask_default_skips_agy_tier(registry_with_fake, monkeypatch):
    import hub.adapters.agy_adapter as agy_mod

    calls = []

    def spy(*a, **k):
        calls.append((a, k))
        raise AssertionError("agy subprocess must never run for default hub_ask")

    monkeypatch.setattr(agy_mod.subprocess, "run", spy)
    hub = _hub_two_tiers()

    code, resp = run(handle_mcp(_call_body("hub_ask", {"question": "2+2?"}), hub))
    assert code == 200
    payload = _text_of(resp["result"])
    assert payload["status"] == "success"
    assert payload["tier_used"] == "fallback"
    assert payload["tier_used"] != "agy"
    assert payload["capability_mode"] == "answer_only"
    assert payload["answer"]
    assert calls == [], "agy adapter subprocess was invoked"
    skips = [a for a in payload["failover_attempts"]
             if a["tier"] == "agy" and a["outcome"] == "skipped"]
    assert skips and skips[0]["reason"] == "skip: mcp_skip_tiers"


def test_hub_ask_escape_hatch_reallows_tier_zero(registry_with_fake):
    # agy tier served by the fake adapter here so "attempted" is observable
    # without spawning a real CLI.
    cfg = HubConfig(tiers=[
        make_tier("agy", 0, ["m1"], adapter="fake", provider="antigravity",
                  capability=CapabilityMode.AGENT_LOOP),
        make_tier("fallback", 1, ["m2"], adapter="fake", provider="fake-provider"),
    ])
    hub = make_hub(cfg)
    code, resp = run(handle_mcp(
        _call_body("hub_ask", {"question": "2+2?", "skip_tiers": []}), hub))
    payload = _text_of(resp["result"])
    assert payload["tier_used"] == "agy"
    assert not any(a["tier"] == "agy" and a["outcome"] == "skipped"
                   for a in payload["failover_attempts"])


def test_hub_ask_ladder_empty_is_structured(registry_with_fake):
    cfg = HubConfig(tiers=[
        make_tier("agy", 0, ["m1"], adapter="agy", provider="antigravity"),
    ])
    hub = make_hub(cfg)
    code, resp = run(handle_mcp(_call_body("hub_ask", {"question": "hi"}), hub))
    payload = _text_of(resp["result"])
    assert payload["status"] == "ladder_empty"
    assert payload["answer"] is None
    assert payload["tier_used"] is None
    assert resp["result"].get("isError") is False


def test_hub_ask_requires_question():
    code, resp = run(handle_mcp(_call_body("hub_ask", {}), None))
    assert resp["error"]["code"] == -32602
    code, resp = run(handle_mcp(_call_body("hub_ask", {"question": "   "}), None))
    assert resp["error"]["code"] == -32602


def test_hub_ask_length_guards():
    code, resp = run(handle_mcp(
        _call_body("hub_ask", {"question": "x" * (MCP_MAX_QUESTION_CHARS + 1)}), None))
    assert resp["error"]["code"] == -32602
    assert str(MCP_MAX_QUESTION_CHARS) in resp["error"]["message"]
    code, resp = run(handle_mcp(
        _call_body("hub_ask", {"question": "ok", "context": "y" * (MCP_MAX_CONTEXT_CHARS + 1)}),
        None))
    assert resp["error"]["code"] == -32602
    code, resp = run(handle_mcp(
        _call_body("hub_ask", {"question": "ok", "skip_tiers": "agy"}), None))
    assert resp["error"]["code"] == -32602  # must be an array, not a string


def test_hub_ask_with_context(registry_with_fake):
    hub = _hub_two_tiers()
    code, resp = run(handle_mcp(
        _call_body("hub_ask", {"question": "summarize", "context": "some background"}), hub))
    payload = _text_of(resp["result"])
    assert payload["status"] == "success"


# ---------------------------------------------------------------------------
# hub_status — same builder as GET /hub/status, secret-free
# ---------------------------------------------------------------------------
def test_hub_status_matches_status_builder(registry_with_fake):
    hub = _hub_two_tiers()
    code, resp = run(handle_mcp(_call_body("hub_status", {}), hub))
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload == hub.status()
    assert [t["id"] for t in payload["tiers"]] == ["agy", "fallback"]


def test_hub_status_and_hub_ask_are_secret_free(registry_with_fake, monkeypatch):
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fake-secret-xyz")
    hub = _hub_two_tiers()
    _, s_resp = run(handle_mcp(_call_body("hub_status", {}), hub))
    _, a_resp = run(handle_mcp(_call_body("hub_ask", {"question": "hi"}), hub))
    blob = json.dumps(s_resp) + json.dumps(a_resp)
    assert TEST_TOKEN not in blob
    assert "gsk-fake-secret-xyz" not in blob


# ---------------------------------------------------------------------------
# AC-4: auth at the HTTP layer (require_mcp_token unit)
# ---------------------------------------------------------------------------
def test_require_mcp_token_accepts_exact_bearer(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    require_mcp_token(f"Bearer {TEST_TOKEN}")  # no raise


@pytest.mark.parametrize("header", [
    None,
    "",
    "Token abc",
    "Bearer wrong-token",
    "bearer ",            # empty presented
    "Bearer",             # no presented part
])
def test_require_mcp_token_rejects(monkeypatch, header):
    from fastapi import HTTPException
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    with pytest.raises(HTTPException) as ei:
        require_mcp_token(header)
    assert ei.value.status_code == 401
    assert ei.value.detail == "Unauthorized"


def test_require_mcp_token_fails_closed_without_secret(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.delenv("HUB_MCP_TOKEN", raising=False)
    with pytest.raises(HTTPException) as ei:
        require_mcp_token(f"Bearer {TEST_TOKEN}")
    assert ei.value.status_code == 401


def test_require_mcp_token_is_constant_time(monkeypatch):
    import hmac as hmac_mod
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    seen = {}
    real = hmac_mod.compare_digest

    def spy(a, b):
        seen["called"] = True
        return real(a, b)

    monkeypatch.setattr(hmac_mod, "compare_digest", spy)
    require_mcp_token(f"Bearer {TEST_TOKEN}")
    assert seen.get("called") is True


# ---------------------------------------------------------------------------
# AD-24 layer 2: import-graph ceiling for hub/mcp_server.py
# ---------------------------------------------------------------------------
def test_mcp_server_import_graph_ceiling():
    src = (PROJECT_ROOT / "agent-service" / "hub" / "mcp_server.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top not in {"subprocess", "shutil", "pty", "socket",
                                   "multiprocessing", "socketserver"}, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "hub":
                allowed = {"ModelHub", "HubRequest", "HubResult"}
                for alias in node.names:
                    assert alias.name in allowed, f"unexpected hub import: {alias.name}"
            else:
                assert not mod.startswith("hub."), f"forbidden hub submodule import: {mod}"
        elif isinstance(node, ast.Call):
            # no os.system / os.popen / os.exec* call sites
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                assert not (func.value.id == "os" and func.attr in
                            {"system", "popen", "execv", "execl", "execvp", "spawnl", "spawnlp"}), \
                    f"forbidden os call: {func.attr}"


# ---------------------------------------------------------------------------
# AD-24 layer 3: deployment rule — hub never in the container's mcp_config.json
# ---------------------------------------------------------------------------
def test_container_mcp_config_has_no_hub_server():
    cfg_path = PROJECT_ROOT / "agent-service" / "mcp_config.json"
    cfg = json.loads(cfg_path.read_text())
    for name, server in cfg.get("mcpServers", {}).items():
        url = server.get("serverUrl", "") if isinstance(server, dict) else ""
        assert "run.app" not in url, f"{name} points at the hub service"
        assert "localhost" not in url and "127.0.0.1" not in url, f"{name} points at loopback"


# ---------------------------------------------------------------------------
# AC-8: setup doc matches the verified config schema
# ---------------------------------------------------------------------------
def test_setup_doc_commands_match_schema():
    doc = (PROJECT_ROOT / "docs" / "hub-mcp-setup.md").read_text()
    assert "https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/mcp" in doc
    assert 'agy mcp add --header "Authorization: Bearer' in doc
    assert "chmod 600" in doc
