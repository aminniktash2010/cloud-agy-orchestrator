"""Model Hub MCP server (Epic M1) — stateless JSON-RPC 2.0 over POST /mcp.

AD-20: hand-rolled dispatch, no `mcp` SDK. AD-21: bearer auth is enforced by the
route (require_mcp_token) BEFORE any MCP processing. AD-22: hub_ask defaults
skip_tiers=["agy"], applied structurally by the router. AD-24: this module
imports ModelHub only (for execute/status) — no subprocess, no adapters, no
repo-mutation helpers. AD-25: length guards are module constants. AD-26: all
MCP wire knowledge is isolated in this module (handle_mcp).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from typing import Any, Optional

from fastapi import HTTPException

from hub import HubRequest, ModelHub

logger = logging.getLogger("agent-service.hub.mcp")

# ---------------------------------------------------------------------------
# Contract constants (AD-25: API-contract values, not operator tunables)
# ---------------------------------------------------------------------------
MCP_MAX_QUESTION_CHARS = 8_000
MCP_MAX_CONTEXT_CHARS = 32_000

SERVER_NAME = "model-hub"
SERVER_VERSION = "1.0.0"
# Negotiated per AC-6 build-time handshake (CLI binary vendors these versions).
SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

DEFAULT_SKIP_TIERS = ["agy"]


# ---------------------------------------------------------------------------
# Auth (AD-21) — runs at the HTTP layer, before any JSON-RPC processing
# ---------------------------------------------------------------------------
def require_mcp_token(authorization: Optional[str]) -> None:
    """Raise HTTPException(401) unless the header is exactly 'Bearer <HUB_MCP_TOKEN>'.

    Identical 401 for missing/malformed/wrong tokens (no oracle). The token
    value is never logged, never echoed, never in any response. If the secret
    is not configured, every request is rejected (fail closed).
    """
    scheme, _, presented = (authorization or "").partition(" ")
    expected = os.environ.get("HUB_MCP_TOKEN", "")
    if scheme.lower() != "bearer" or not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelope helpers
# ---------------------------------------------------------------------------
def _rpc_result(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
HUB_ASK_DESCRIPTION = (
    "Ask the Model Hub a question when your own Antigravity credits are exhausted. "
    "The answer comes from the fallback provider ladder "
    "(AI Studio \u2192 Groq \u2192 NVIDIA \u2192 OpenRouter \u2192 Requesty) \u2014 "
    "never from an Antigravity CLI. Answer-only: no repo changes, no tool use. "
    "If the user has selected a specific model (e.g. 'claude opus 5.5'), pass it "
    "verbatim in `model` \u2014 the hub will serve that model across every provider "
    "that carries it, failing over providers transparently. Omit for the default ladder."
)

HUB_STATUS_DESCRIPTION = (
    "Read the Model Hub's ladder state: tier order, per-tier quota "
    "(live/measured/estimated with as-of timestamps), circuit breakers, and "
    "poller health. Contains no secrets. Use it to check what's available before asking."
)


def _tool_schemas() -> list[dict]:
    return [
        {
            "name": "hub_ask",
            "description": HUB_ASK_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": f"The question to answer (max {MCP_MAX_QUESTION_CHARS} chars).",
                    },
                    "context": {
                        "type": "string",
                        "description": f"Optional background the answer may need (max {MCP_MAX_CONTEXT_CHARS} chars).",
                    },
                    "skip_tiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Tier IDs to skip. Defaults to [\"agy\"] (your credits and the "
                            "hub's cloud CLI share one pool, so a dry local CLI means a dry "
                            "cloud tier). Pass [] to allow all tiers."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional: the user's selected model, passed verbatim "
                            "(e.g. 'claude opus 5.5'). The hub serves that model across "
                            "every provider that carries it, failing over providers "
                            "transparently. Omit (or pass 'auto') for the default ladder."
                        ),
                    },
                    "allow_model_fallback": {
                        "type": "boolean",
                        "description": (
                            "If true and no provider can serve the pinned model, fall back "
                            "to the default ladder and serve a different model — the "
                            "response sets model_substituted=true loudly. Default false: "
                            "the hub reports model_unavailable instead of silently "
                            "substituting."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
        {
            "name": "hub_status",
            "description": HUB_STATUS_DESCRIPTION,
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


# ---------------------------------------------------------------------------
# Dispatch (AD-26: all wire knowledge isolated here)
# ---------------------------------------------------------------------------
async def handle_mcp(body: Any, model_hub: ModelHub) -> tuple[int, Optional[dict]]:
    """Dispatch a parsed JSON-RPC body.

    Returns (http_status, response_dict | None). A None response means the
    input was a notification — the caller must return HTTP 202 with an empty
    body. Batch arrays are dispatched item-by-item.
    """
    if isinstance(body, list):
        responses = []
        for item in body:
            resp = await _dispatch(item, model_hub)
            if resp is not None:
                responses.append(resp)
        return 200, responses
    resp = await _dispatch(body, model_hub)
    if resp is None:
        return 202, None
    return 200, resp


async def _dispatch(msg: Any, model_hub: ModelHub) -> Optional[dict]:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        rid = msg.get("id") if isinstance(msg, dict) else None
        return _rpc_error(rid, -32600, "Invalid Request")
    method = msg["method"]
    if "id" not in msg:
        # Notification (e.g. notifications/initialized): accepted, no-op.
        return None
    rid = msg["id"]

    if method == "initialize":
        return _rpc_result(rid, _initialize_result(msg.get("params") or {}))
    if method == "tools/list":
        return _rpc_result(rid, {"tools": _tool_schemas()})
    if method == "tools/call":
        return await _tools_call(rid, msg.get("params"), model_hub)
    return _rpc_error(rid, -32601, f"Method not found: {method}")


def _initialize_result(params: Any) -> dict:
    offered = params.get("protocolVersion", "") if isinstance(params, dict) else ""
    negotiated = offered if offered in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
    return {
        "protocolVersion": negotiated,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


async def _tools_call(rid: Any, params: Any, model_hub: ModelHub) -> dict:
    if not isinstance(params, dict):
        return _rpc_error(rid, -32602, "Invalid params: expected an object")
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _rpc_error(rid, -32602, "Invalid params: arguments must be an object")
    if name == "hub_ask":
        return await _call_hub_ask(rid, arguments, model_hub)
    if name == "hub_status":
        return await _call_hub_status(rid, model_hub)
    return _rpc_error(rid, -32602, f"Unknown tool: {name}")


async def _call_hub_ask(rid: Any, arguments: dict, model_hub: ModelHub) -> dict:
    question = arguments.get("question")
    if not isinstance(question, str) or not question.strip():
        return _rpc_error(rid, -32602, "Invalid params: question is required and must be a non-empty string")
    if len(question) > MCP_MAX_QUESTION_CHARS:
        return _rpc_error(
            rid, -32602,
            f"Invalid params: question exceeds {MCP_MAX_QUESTION_CHARS} characters",
        )
    context = arguments.get("context") or ""
    if not isinstance(context, str):
        return _rpc_error(rid, -32602, "Invalid params: context must be a string")
    if len(context) > MCP_MAX_CONTEXT_CHARS:
        return _rpc_error(
            rid, -32602,
            f"Invalid params: context exceeds {MCP_MAX_CONTEXT_CHARS} characters",
        )
    raw_skip = arguments.get("skip_tiers")
    if raw_skip is None:
        skip_tiers = list(DEFAULT_SKIP_TIERS)
    elif isinstance(raw_skip, list) and all(isinstance(s, str) for s in raw_skip):
        skip_tiers = list(raw_skip)
    else:
        return _rpc_error(rid, -32602, "Invalid params: skip_tiers must be an array of strings")
    raw_model = arguments.get("model")
    if raw_model is not None and not isinstance(raw_model, str):
        return _rpc_error(rid, -32602, "Invalid params: model must be a string")
    raw_fallback = arguments.get("allow_model_fallback", False)
    if not isinstance(raw_fallback, bool):
        return _rpc_error(rid, -32602, "Invalid params: allow_model_fallback must be a boolean")

    prompt = question if not context else f"Context:\n{context}\n\nQuestion:\n{question}"
    req = HubRequest(prompt=prompt, skip_tiers=skip_tiers, model=raw_model,
                     allow_model_fallback=raw_fallback)
    t0 = time.monotonic()
    try:
        result = await model_hub.execute(req)
    except Exception:  # noqa: BLE001 — never leak tracebacks to the MCP client
        logger.exception("mcp tool=hub_ask execution failed")
        return _rpc_error(rid, -32603, "Hub execution failed")
    latency_ms = int((time.monotonic() - t0) * 1000)
    # NFR-4: tool name, tier, latency only — never the question/context, never the token.
    logger.info("mcp tool=hub_ask tier_used=%s latency_ms=%d", result.tier_used, latency_ms)

    if result.status == "model_unavailable":
        # AD-34: no_match is a client error (-32602); no_carrier is -32603
        # with a model_unavailable code in data. Never a bare success.
        err = result.model_error or {}
        if err.get("code") == "no_match":
            return _rpc_error(
                rid, -32602,
                f"Unknown model '{err.get('input', '')}'. "
                f"Suggestions: {', '.join(err.get('suggestions', [])) or 'none'}",
                data={"code": "no_match", "suggestions": err.get("suggestions", [])},
            )
        return _rpc_error(
            rid, -32603,
            f"Model unavailable: {err.get('message', '')}",
            data={**err, "code": "model_unavailable"},
        )

    payload = {
        "status": result.status,
        "answer": result.answer or None,
        "tier_used": result.tier_used or None,
        "provider": result.provider,
        "model": result.model,
        "capability_mode": result.capability_mode,
        "pinned_model": result.pinned_model,
        "requested_model": result.requested_model or None,
        "model_substituted": result.model_substituted,
        "failover_attempts": [a.model_dump() for a in result.failover_attempts],
    }
    return _rpc_result(
        rid, {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}
    )


async def _call_hub_status(rid: Any, model_hub: ModelHub) -> dict:
    # Same builder as GET /hub/status — the tool and the endpoint cannot drift.
    # The payload is secret-free by the AC-10 lineage (booleans/percentages only).
    text = json.dumps(model_hub.status())
    return _rpc_result(
        rid, {"content": [{"type": "text", "text": text}], "isError": False}
    )
