"""Route-level tests for POST /mcp against the real FastAPI app (AC-1/AC-4).

Auth ordering (401 before any JSON-RPC dispatch) and the 503-not-configured
path are route concerns that handle_mcp() unit tests cannot cover.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app, raise_server_exceptions=False)

TEST_TOKEN = "test-hub-mcp-token-route"


def _headers(token="Bearer " + TEST_TOKEN):
    return {"Authorization": token, "Content-Type": "application/json"}


def _rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


# ---------------------------------------------------------------------------
# AC-4: 401 before dispatch — identical body for missing/malformed/wrong
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": ""},
    {"Authorization": "Token abc"},
    {"Authorization": "Bearer wrong-token"},
])
def test_mcp_rejects_unauthenticated_identically(monkeypatch, headers):
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    r = client.post("/mcp", json=_rpc("tools/list"), headers=headers)
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}


def test_mcp_401_executes_nothing(monkeypatch):
    # Even a well-formed tools/call must not execute without a valid token.
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    r = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "hub_ask", "arguments": {"question": "hi"}}),
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_mcp_503_when_token_not_configured(monkeypatch):
    monkeypatch.delenv("HUB_MCP_TOKEN", raising=False)
    r = client.post("/mcp", json=_rpc("tools/list"), headers=_headers())
    assert r.status_code == 503
    assert "HUB_MCP_TOKEN" in r.json()["error"]["message"]


# ---------------------------------------------------------------------------
# AC-1: E2E over HTTP — initialize → list → call (hub_status is key-free)
# ---------------------------------------------------------------------------
def test_mcp_e2e_over_http(monkeypatch):
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    h = _headers()

    r = client.post("/mcp", json=_rpc(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {},
         "clientInfo": {"name": "e2e", "version": "0"}}), headers=h)
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"

    r = client.post("/mcp", json=_rpc("tools/list"), headers=h)
    assert r.status_code == 200
    assert [t["name"] for t in r.json()["result"]["tools"]] == ["hub_ask", "hub_status"]

    r = client.post("/mcp", json=_rpc("tools/call", {"name": "hub_status", "arguments": {}}), headers=h)
    assert r.status_code == 200
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert "tiers" in payload
    assert TEST_TOKEN not in r.text


def test_mcp_malformed_json_is_32700(monkeypatch):
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    r = client.post("/mcp", content=b"{not json",
                    headers={"Authorization": f"Bearer {TEST_TOKEN}",
                             "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32700


def test_mcp_notification_is_202(monkeypatch):
    monkeypatch.setenv("HUB_MCP_TOKEN", TEST_TOKEN)
    r = client.post("/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                    headers=_headers())
    assert r.status_code == 202
    assert r.content == b""
