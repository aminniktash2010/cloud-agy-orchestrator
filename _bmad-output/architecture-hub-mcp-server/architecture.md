# Architecture — Model Hub as an MCP Server
**Project:** cloud-agy-orchestrator · **Feature:** Hub MCP server (`POST /mcp`: `hub_ask` + `hub_status`)
**Author:** 🏗️ Winston (BMAD architect) · **Date:** 2026-10-22 · **Status:** Ready for dev
**Inputs:** `_bmad-output/brief-hub-mcp-server/brief.md` · `_bmad-output/prd-hub-mcp-server/prd.md` · `_bmad-output/architecture-model-hub/architecture.md` (AD-1…AD-8) · `_bmad-output/architecture-live-quota/architecture.md` (AD-9…AD-19) · `agent-service/main.py` (live, read) · `agent-service/hub/` (live, read) · `terraform/main.tf` (live, read)

**Epic:** M1 (single epic; small). This doc continues the decision numbering at AD-20.

---

## 1. Design paradigm (extends Epic 1 + live-quota)

**The hub gains a machine client, not a new brain.** `POST /mcp` is a thin JSON-RPC skin over the two things the hub already does: `ModelHub.execute()` (answer) and `ModelHub.status()` (state). No new routing logic, no new provider code, no new quota machinery. The MCP layer's only jobs are protocol translation, auth, and the structural tier-skip that makes the same-account design loop-proof.

Corollaries:
- The MCP surface is **read/compute-only by construction**: the handler module imports `ModelHub.execute` and `ModelHub.status` and nothing else that can mutate (no git, no subprocess, no Secret Manager writes).
- Auth is an **HTTP-layer gate**, not a JSON-RPC concern: a bad token never reaches method dispatch (mirrors the `/terminal/ws` hardening from live-quota Epic C).
- Everything the PM marked out of scope stays out: no model-backend role, no job queue, no write tools, no ladder/quota/PWA changes.

---

## 2. Architecture decisions (the invariants — continue AD-1…AD-19)

### AD-20 — Stateless JSON-RPC on `POST /mcp`, hand-rolled dispatch, no `mcp` SDK dependency [ADOPTED]
The v1 method surface is four methods (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`) with two tools. A hand-rolled dispatcher (~120 lines) gives exact control over the auth-before-dispatch ordering (AD-21) and keeps the dependency tree untouched (AD-8's slim-image posture). The `mcp` PyPI SDK would pull session machinery, an ASGI mount, and its own auth story for zero v1 benefit.
*Binds:* the MCP layer never imports provider SDKs or the `agy` adapter directly. *Prevents:* dependency bloat and session-state creep.
*Rejected:* `mcp` SDK `StreamableHTTPSessionManager` — right tool for a stateful server, wrong tool for a stateless two-tool skin.

### AD-21 — Bearer auth at the HTTP layer, before any JSON-RPC processing [ADOPTED]
`require_mcp_token()` runs first in the route: missing/malformed/invalid `Authorization` → HTTP **401**, no body parsing beyond what's needed, no method dispatch, no tool listing. Comparison via `hmac.compare_digest` (constant-time). The token value is never logged, never echoed, never in any response — including 401s.
*Binds:* NFR-3. *Prevents:* auth-oracle behavior (the 401 is identical for missing vs. wrong tokens) and any unauthenticated JSON-RPC surface.

### AD-22 — `hub_ask` skips tier `"agy"` by default, structurally, via `HubRequest.skip_tiers` [ADOPTED]
New additive field `HubRequest.skip_tiers: list[str] = []`. The tool layer maps "absent" → `["agy"]`; an explicit `[]` lifts the structural skip — but tier 0 still cannot serve an MCP request: `AgyAdapter` returns `BAD_REQUEST` when `repo_ctx is None` (the MCP route never builds one), which the router converts to a `skip: no_repo_ctx` tier skip (review fix; graceful skip, not `-32603`). The router checks membership at the top of the tier loop and records `skip: skip_tiers` — the same skip-recording path as every other skip reason, so `failover_attempts` stays honest. Unknown tier ids in the list simply never match (no validation error, no crash).
*Binds:* the default is not prompt guidance the agent can ignore — the router never attempts the tier. *Prevents:* the shared-pool double-burn (local dry ⇒ cloud dry, so attempting tier 0 wastes a CLI run).

### AD-23 — The MCP handler is mutation-free by construction; `capability_mode` is reported honestly [ADOPTED]
Repo mutation (git add/commit/push, snapshots, Drive reports) lives in the `/chat`/`/chat/stream` handlers (AD-6) — the MCP route never calls that code. `hub_ask` therefore performs zero repo side effects on every path. Correction (review fix): the `skip_tiers: []` escape hatch does **not** let the agy adapter run its normal subprocess — `AgyAdapter` returns `BAD_REQUEST` when `repo_ctx is None` (the MCP route never builds a repo context), which the router converts to a `skip: no_repo_ctx` tier skip, so no CLI subprocess can ever spawn via MCP (an unclaimed 4th loop-safety layer, with graceful skip instead of a hard error). The response still reports the **actual** `capability_mode` from `HubResult` (always `answer_only` on the MCP path) — never a hardcoded claim.
*Binds:* FR-3.3/FR-5.2. *Prevents:* a "forced answer_only" label that lies when the escape hatch fires.

### AD-24 — Loop safety is three enforced layers, not documentation [ADOPTED]
1. **Structural skip** (AD-22): default calls can never invoke the cloud CLI — enforced in `router.execute()`, asserted by test (AC-2: `tier_used != "agy"` + agy subprocess entry never hit).
2. **Capability ceiling**: `hub/mcp_server.py` imports only `ModelHub` (for `execute`/`status`) — no adapter imports, no `subprocess`, no repo-mutation helpers. A module-boundary test asserts the import graph. Even a misconfigured skip cannot produce an agentic loop from this module.
3. **Deployment rule**: the hub's own URL is never in the container's `mcp_config.json` — enforced by a CI test that reads the checked-in `agent-service/mcp_config.json` and fails if any `serverUrl` contains the service hostname (or `localhost`/loopback).
*Prevents:* local-CLI → hub → cloud-CLI cycles at the router, module, and deployment layers respectively.

### AD-25 — Question/context length guards are module constants: 8k / 32k [ADOPTED]
`MCP_MAX_QUESTION_CHARS = 8_000`, `MCP_MAX_CONTEXT_CHARS = 32_000` in `hub/mcp_server.py`. Rationale: MCP tool calls are questions, not document dumps; a 200k-char prompt accidentally sent to a free fallback tier burns shared quota for no reason. The hub's global 200k truncation remains the backstop. Over-limit → JSON-RPC `-32602` naming the limit. Constants, not config — these are API-contract values, not operator tunables (AD-4's hot-load is for ladder policy, not wire contracts).
*Prevents:* accidental quota burn via the tool surface.

### AD-26 — Transport is isolated in `hub/mcp_server.py`; an SSE fallback touches only that module [ADOPTED]
All MCP wire knowledge (JSON-RPC envelope, method dispatch, notification handling, session-header policy) lives in one module behind `handle_mcp(body, model_hub) -> (status, response)`. If the build-time CLI handshake (AC-6) shows the CLI requires legacy SSE instead of Streamable HTTP, the fallback is a second transport function in the same module — tools, auth, and router plumbing are untouched.
*Binds:* the PM's open transport question. *Prevents:* a protocol surprise becoming a redesign.

### AD-27 — `HUB_MCP_TOKEN` is generated by Amin, never transits the agent [ADOPTED]
`openssl rand -hex 32`, first version created by Amin in the Secret Manager console — the standing convention from architecture-model-hub §8 ("Amin creates each secret's first version in the console"), whose entire purpose is keeping secret *values* out of agent/chat/log transit. Terraform references only the secret *name* (`local.secrets`); the value is never in code, config, or the PR.
*Rejected:* the PRD's "Mamali generates and hands it over" — there is no handoff channel that doesn't violate the no-secrets-in-chat rule, and the convention already solves this.
*Binds:* NFR-3. Runbook: secret must exist in console **before** `terraform apply` (same ordering constraint as all hub secrets — the `data` source fails on missing secrets).

---

## 3. Transport design (`POST /mcp`)

### 3.1 Request lifecycle

```
POST /mcp
  ├─ require_mcp_token(Authorization)          → 401 on missing/malformed/invalid (AD-21)
  ├─ parse JSON body                           → -32700 on malformed
  ├─ dispatch (single object or batch array)    → per-item responses
  └─ notifications (no "id")                    → HTTP 202, empty body
```

Batch arrays are processed item-by-item through the same dispatch (small loop, no extra machinery). Malformed items inside a batch get their own error entries; a batch never partially executes-then-dies.

### 3.2 Methods

**`initialize`** — params: `{protocolVersion, capabilities, clientInfo}`. Response:
```json
{"protocolVersion": "<negotiated>", "capabilities": {"tools": {}},
 "serverInfo": {"name": "model-hub", "version": "1.0.0"}}
```
Negotiation: server supports `["2025-06-18", "2025-03-26", "2024-11-05"]`; if the client's version is in the list, echo it; otherwise respond with the newest supported and let the client decide. The negotiated version is recorded by the build-time handshake test (AC-6). `Mcp-Session-Id`: **neither required nor echoed** (stateless v1, per PRD FR-1.3); a client-sent session id is ignored. If AC-6 shows the CLI *requires* session establishment, the fallback is to echo a per-process constant id — transport-only change per AD-26.

**`notifications/initialized`** (and any `notifications/*`) — accepted, no-op, HTTP 202 empty.

**`tools/list`** — returns exactly the two tools with their JSON Schemas (§5). No pagination in v1.

**`tools/call`** — params `{name, arguments}`. Unknown `name` → `-32602` ("unknown tool"). Argument validation failures → `-32602` with a message naming the bad field. Tool exceptions → `-32603` with a static message (never the traceback); the attempt is logged server-side.

**Unknown method** → `-32601`. **Invalid JSON-RPC shape** (missing `jsonrpc`/`method`) → `-32600`. Error messages are static strings — no internals, no env values (PRD FR-1.4).

### 3.3 What the CLI was verified to support (brief §5.1, carried)

`agy mcp add --type http --header "Authorization: Bearer <token>" model-hub https://<service>/mcp` stores `{mcpServers: {"model-hub": {disabled, headers: {Authorization}, serverUrl}}}`. The `/mcp` path convention matches Streamable HTTP. The one unverified item — exact wire protocol (Streamable HTTP vs legacy SSE) — is decided by the AC-6 handshake test, with the AD-26 fallback ready.

---

## 4. Auth design

```python
def require_mcp_token(authorization: str | None) -> None:
    """Raise HTTPException(401) unless the header is exactly 'Bearer <HUB_MCP_TOKEN>'."""
    scheme, _, presented = (authorization or "").partition(" ")
    expected = os.environ.get("HUB_MCP_TOKEN", "")
    if scheme.lower() != "bearer" or not expected or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
```

- Runs **first** in the route, before `await request.json()` is even inspected for dispatch (AD-21). (Body must still be read for the 401 path? No — 401 returns immediately; the unread body is discarded by the framework.)
- 401 detail is the static string `"Unauthorized"` — identical for missing, malformed, and wrong tokens (no oracle).
- The token lives only in the process env (terraform env-mount from Secret Manager, same pattern as `GROQ_API_KEY`). It never appears in logs, responses, the `/hub/status` payload, or the MCP tool schemas.
- Rotation: new secret version in console → Cloud Run picks it up on next revision (env-mounted secrets refresh on revision start); Amin re-runs setup step 2 on the laptop. Documented in the setup doc (PRD NFR-3).

---

## 5. Tools

### 5.1 `hub_ask`

```json
{"name": "hub_ask",
 "description": "Ask the Model Hub a question when your own Antigravity credits are exhausted. The answer comes from the fallback provider ladder (AI Studio → Groq → NVIDIA → OpenRouter → Requesty) — never from an Antigravity CLI. Answer-only: no repo changes, no tool use on your machine.",
 "inputSchema": {"type": "object",
   "properties": {
     "question": {"type": "string", "description": "The question to answer (max 8000 chars)."},
     "context": {"type": "string", "description": "Optional background the answer may need (max 32000 chars)."},
     "skip_tiers": {"type": "string", "items": {"type": "string"},
       "description": "Tier IDs to skip. Defaults to [\"agy\"] (your credits and the hub's cloud CLI share one pool, so a dry local CLI means a dry cloud tier). Pass [] to allow all tiers."}},
   "required": ["question"]}}
```

Execution:
```python
skip = ["agy"] if skip_tiers is None else list(skip_tiers)
prompt = question if not context else f"Context:\n{context}\n\nQuestion:\n{question}"
req = HubRequest(prompt=prompt, skip_tiers=skip)   # repo/branch left at defaults; no repo_ctx
result = await model_hub.execute(req)
```

Response (`content[0].text` = JSON string):
```json
{"answer": "...", "tier_used": "groq", "provider": "groq", "model": "qwen/qwen3.8-27b",
 "capability_mode": "answer_only",
 "failover_attempts": [{"tier": "groq", "provider": "groq", "model": "...",
                        "outcome": "success", "reason": "", "latency_ms": 1234, "at": "..."}]}
```
- `tier_used` is **never** `"agy"` unless `skip_tiers` was explicitly overridden (AC-2, asserted by test).
- Ladder empty → `{"status": "ladder_empty", "answer": null, "tier_used": null, "failover_attempts": [...]}` with `isError: false` (PRD FR-3.6 — a structured answer, not an exception).
- `HubRequest.repo` defaults to `""`: the agy adapter is unreachable-by-default here anyway (AD-22), and no repo prep runs — the MCP path never touches `/workspace`.

### 5.2 `hub_status`

Input schema `{}`. Implementation: `text = json.dumps(model_hub.status())` — the **same** `ModelHub.status()` the `GET /hub/status` handler calls (verified live at `main.py:599`), so the tool and the endpoint can never drift. The payload is secret-free by the existing AC-10 lineage (tiers, `live_quota` blocks with percentages/timestamps only, `key_configured` booleans, `poller` health).

Tool description: `"Read the Model Hub's ladder state: tier order, per-tier quota (live/measured/estimated with as-of timestamps), circuit breakers, and poller health. Contains no secrets. Use it to check what's available before asking."`

---

## 6. Loop safety — enforcement detail (AD-24)

| Layer | Enforced where | Test |
|---|---|---|
| 1. Structural skip | `router.execute()`: `if tier.id in request.skip_tiers: record(skip: mcp_skip_tiers); continue` — placed **before** the existing `_skip_reason` check so the reason is distinct in `failover_attempts` | AC-2: default call → `tier_used != "agy"`; assert the agy adapter's subprocess entry was never invoked (mock/spy) |
| 2. Capability ceiling | `hub/mcp_server.py` imports `ModelHub` only (for `execute`/`status`); module-boundary test asserts no `subprocess`, no adapter, no `shutil`/`os.system` imports in the module | import-graph test |
| 3. Deployment rule | CI test reads `agent-service/mcp_config.json`, fails if any `serverUrl` contains the service hostname or loopback | config test |

Note on layer 1 vs. Epic C's family breakers: the skip applies at **tier** granularity before family grouping runs — cheaper (no family walk at all) and unambiguous in the attempt log.

---

## 7. Module layout & integration

```text
agent-service/
  main.py                 # EXTEND: POST /mcp route (~20 lines: auth → parse → dispatch)
  hub/
    mcp_server.py         # NEW: require_mcp_token(), handle_mcp(), tool schemas/descriptions,
                          #   tool_hub_ask(), tool_hub_status(), MCP_MAX_* constants,
                          #   SUPPORTED_PROTOCOL_VERSIONS
    types.py              # EXTEND: HubRequest.skip_tiers: list[str] = [] (additive)
    router.py             # EXTEND: skip_tiers check at top of tier loop (execute + execute_stream)
  tests/
    test_mcp_server.py    # NEW: AC-1…AC-5 + import-graph + config tests
terraform/
  main.tf                 # EXTEND: local.secrets += "HUB_MCP_TOKEN" (one line; data source +
                          #   accessor grant + env mount all key off the set — zero new resource types)
docs/
  hub-mcp-setup.md        # NEW: the 3-step laptop setup + chmod 600 + troubleshooting + rotation
```

`main.py` route (thin, per AD-1's handler convention):
```python
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    require_mcp_token(request.headers.get("authorization"))   # 401 before anything else
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error"), status_code=200)
    status_code, resp_body = await handle_mcp(body, model_hub)
    if resp_body is None:                                     # notification
        return Response(status_code=202)
    return JSONResponse(resp_body, status_code=status_code)
```

Data flow:
```
laptop agy CLI --POST /mcp (Bearer)--> Cloud Run
  → require_mcp_token → handle_mcp → tools/call hub_ask
  → HubRequest(skip_tiers=["agy"]) → ModelHub.execute()
  → tier 1..n ladder (answer_only adapters) → HubResult
  → JSON text in tools/call result → laptop agent
```

No PWA changes. No `/chat`/`/chat/stream`/`/hub/status`/terminal changes. No `MODEL_HUB_CONFIG` schema changes (skip default is code, not config — it's a client-contract default, and making it config would let an operator silently re-open the loop path; AD-22 keeps it structural).

---

## 8. Terraform & secret lifecycle

```hcl
locals {
  secrets = toset([
    "GEMINI_API_KEY",
    "GITHUB_PAT",
    "WEBHOOK_SECRET",
    "HUB_MCP_TOKEN",      # ← one-line addition
  ])
}
```
The existing `data.google_secret_manager_secret.vault`, `sa_secret_access` IAM, and the dynamic `env` block all iterate `local.secrets` — the token is env-mounted with no new resource types (PRD FR-7.2).

**Ordering** (standing constraint): `HUB_MCP_TOKEN` must exist in Secret Manager **before** `terraform apply`, or the `data` source fails the run. Runbook order: (1) Amin runs `openssl rand -hex 32` locally and creates the secret's first version in the console; (2) merge → deploy workflow applies; (3) Amin pastes the token into `agy mcp add` on the laptop. The token value never appears in code, PRs, logs, or chat.

---

## 9. Security & privacy

- **Threat model**: public internet → single endpoint → bearer token. The token is single-purpose (MCP only), 256-bit, rotatable, and revocable by disabling the secret version. No other privilege attaches to it.
- **No oracle**: identical 401 for missing/malformed/wrong tokens; constant-time compare; no timing or message distinction.
- **Secret-freedom**: `hub_status` reuses the AC-10-covered payload; `hub_ask` responses contain answer text + tier metadata only. The existing `test_no_secret_leak.py` pattern is extended to both tools with the token value present in env during the test (asserts absence).
- **Laptop side**: bearer lands in plaintext in `~/.gemini/config/mcp_config.json` → setup doc mandates `chmod 600`; token is per-Amin (single operator), not per-device.
- **Logging** (PRD NFR-4): `mcp tool=hub_ask tier_used=<t> latency_ms=<n>` — tool name, tier, latency only. Never the question/context text (same policy as hub attempt logs: no prompt text), never the token.
- **Cost** (PRD NFR-5): $0 infra delta; `hub_ask` spends fallback-tier quota exactly like `/chat` — the length guards (AD-25) are the only new cost control, and they only *reduce* spend.

---

## 10. Test strategy (maps to PRD AC-1…AC-8)

| AC | Test | How |
|---|---|---|
| AC-1 | Mock MCP client E2E | raw JSON-RPC over HTTP against the real FastAPI app: `initialize` → `tools/list` (exactly 2 tools, schemas match §5) → `tools/call hub_ask` → assert `tier_used != "agy"`, `capability_mode == "answer_only"` |
| AC-2 | Default skip | spy on the agy adapter entry: default call never invokes it; `failover_attempts` contains `skip: mcp_skip_tiers` for tier `agy` |
| AC-3 | Escape hatch | `skip_tiers: []` → tier 0 attempted (use `MODEL_HUB_FORCE_EXHAUST_TIER` hook or a stubbed adapter to avoid real CLI runs) |
| AC-4 | Auth rejection | missing / malformed / wrong token → HTTP 401 with identical body; assert nothing executed (no tool side effects observable) |
| AC-5 | Secret freedom | `hub_status` + `hub_ask` outputs with `HUB_MCP_TOKEN` set in env → token absent from every byte of both payloads |
| AC-6 | Real CLI handshake | **build-time**: `agy mcp add` against a dev/staging endpoint → `mcp list` shows the server → list tools via the CLI → record negotiated `protocolVersion`; decides Streamable-HTTP-vs-SSE finally |
| AC-7 | No regressions | full existing suite green (102+ Epic C/A tests); new tests ≥ 10 |
| AC-8 | Setup doc verification | the three setup commands checked against the verified config schema (brief §5.1 — research already done; test asserts the doc's URL matches the service URL constant) |
| — | Import-graph ceiling | `hub/mcp_server.py` imports only `ModelHub`/stdlib/pydantic — no `subprocess`, no adapters (AD-24 layer 2) |
| — | Deployment rule | checked-in `mcp_config.json` contains no hub `serverUrl` (AD-24 layer 3) |
| — | Length guards | over-limit question/context → `-32602` naming the limit |

New tests live in `agent-service/hub/tests/test_mcp_server.py`; CI already runs pytest there.

---

## 11. Dev-agent handoff — Epic M1 stories

**Epic M1 — Hub MCP server** (single epic, shippable in one PR):
- **M1-S1**: `hub/mcp_server.py` skeleton — `require_mcp_token()`, JSON-RPC dispatch (`initialize`/`notifications/*`/`tools/list`/`tools/call`), error codes, batch handling, protocol negotiation; `main.py` `POST /mcp` route. **Includes the AC-6 build-time CLI handshake** against a mock/dev endpoint *before* the tools are wired — if the CLI demands SSE, implement the AD-26 transport fallback now.
- **M1-S2**: `hub_ask` tool — schemas, length guards, `skip_tiers` default/override, `HubRequest` plumbing, `router.py` skip check, response mapping (§5.1), ladder-empty shape.
- **M1-S3**: `hub_status` tool — `model_hub.status()` reuse, secret-freedom test.
- **M1-S4**: Loop-safety tests (AC-2/AC-3), import-graph test, `mcp_config.json` deployment-rule test, auth tests (AC-4), E2E mock-client test (AC-1).
- **M1-S5**: `docs/hub-mcp-setup.md` (3-step setup, `chmod 600`, troubleshooting, rotation); terraform one-liner; PR description carries the runbook (secret-before-apply ordering).

Suggested branch: `bmad/hub-mcp-server` from `origin/main`. No epic depends on another — M1 is self-contained.

---

## 12. Deployment & rollback

**Pre-deploy (Amin's tap):** create `HUB_MCP_TOKEN` in the Secret Manager console (`openssl rand -hex 32` locally; value never leaves his machine except into the console and his laptop config).

**Deploy:**
1. PR: code + terraform one-liner + `docs/hub-mcp-setup.md` → Amin's taps → merge (branch→PR→merge, standing rule).
2. Existing `deploy-agent-infra.yml`: image rebuild + `terraform apply` (picks up the new secret's accessor grant + env mount) → new Cloud Run revision.
3. Smoke: `curl` without token → 401; `initialize`→`tools/list` with token → 2 tools; `hub_status` → payload matches `GET /hub/status` shape.
4. Amin runs the 3-step laptop setup; verify with a real `hub_ask` from his laptop when credits allow (or `hub_status` anytime — it's free).

**Rollback:** previous Cloud Run revision (<2 min); the endpoint simply disappears — the laptop CLI marks the server unreachable, nothing else is affected (additive feature, no shared code paths mutated beyond the additive `skip_tiers` field and router check).

---

## 13. PM open questions — resolved with recommendations

1. **Exact `protocolVersion`**: negotiated per §3.2 from `["2025-06-18", "2025-03-26", "2024-11-05"]`; the AC-6 handshake test records what the real CLI settles on. Dev treats the list as the contract.
2. **Stateless vs `Mcp-Session-Id`**: stateless v1 confirmed — no requirement, no echo, client-sent ids ignored. Fallback (only if AC-6 demands): echo a per-process constant id; transport-only change.
3. **Length guards**: 8k question / 32k context, module constants (AD-25). Over-limit → `-32602`.

---

## 14. Risks

| Risk | Status after this design |
|---|---|
| CLI actually requires SSE, not Streamable HTTP | Contained: AC-6 handshake decides at build time; AD-26 isolates the fallback to the transport module |
| CLI requires `Mcp-Session-Id` | Contained: fallback is a constant echo; no tool/auth changes |
| Bearer token leaks from laptop plaintext config | Contained: `chmod 600` in setup; single-purpose, rotatable, revocable |
| Agent misuses `hub_ask` (non-exhaustion calls surprised by tier-0 skip) | Mitigated: tool description states the contract; `tier_used` always reported; escape hatch documented |
| `skip_tiers: []` re-opens a loop path | Neutralized: still a single `execute()`; cloud container never has the hub registered (AD-24 layer 3, CI-tested); MCP handler can't recurse (it has no MCP client) |
| `protocolVersion` list goes stale as MCP evolves | Mitigated: negotiation echoes client versions we know; unknown → newest supported; extend the list without touching tools |
| Scope creep into write tools (`hub_run`) | Blocked: out of scope §4.3; needs its own tap-gated epic |

---

*Next: BMAD dev agent implements Epic M1 per §11. PRD acceptance criteria AC-1…AC-8 map to the test rows in §10.*
