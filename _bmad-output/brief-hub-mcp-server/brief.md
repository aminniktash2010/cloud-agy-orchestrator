# Brief — Model Hub as an MCP Server
**Project:** cloud-agy-orchestrator (Cloud Antigravity Orchestrator)
**Feature:** Expose the Model Hub as a remote MCP server so Amin's **local** `agy` CLI can call it as a tool when local credits run dry
**Author:** 📋 John (BMAD PM) · **Date:** 2026-10-22
**Status:** Ready for architecture (next: BMAD architect)
**Depends on:** Model Hub Epic 1 + live-quota Epics C & A (all LIVE on Cloud Run) · `_bmad-output/{brief,prd,architecture}-model-hub/` · `_bmad-output/{brief,prd}-live-quota/` · `_bmad-output/architecture-live-quota/`
**Replaces:** the laptop-worker polling idea (discussed 2026-10-22, discarded as unnecessarily complex — no job queue, no worker script, no inbound problem to solve)

---

## 1. Problem

Amin's stated goal: *"main goal of this project is to have a AI hub that I can have request flow and use the ai credits, including the agy cli that running at cloud run or my local laptop cli."*

Today the hub is a great front door for the PWA, the Gmail bridge, and the Android app — but his **local laptop CLI** can't reach it. When his local Antigravity credits die (same account as Cloud Run — confirmed by Amin 2026-10-22, one shared pool), his local agent is stuck: it has no path to the fallback ladder (AI Studio → Groq → NVIDIA → OpenRouter → Requesty) that the hub already operates.

The natural integration point is **MCP**: the `agy` CLI is already an MCP client. If the hub exposes itself as a remote MCP server, the local CLI gets a `hub_ask` tool — "credits are dead, ask the hub" — with zero new infrastructure and a 3-step laptop setup.

The same server will later serve his **desktop Antigravity app** (one server, two clients) — that onboarding is explicitly out of scope for this epic, but the design must not preclude it.

## 2. Goals

1. **The local CLI can ask the hub.** A `hub_ask` tool: question in, answer out, via the fallback ladder — with `tier_used` reported so Amin always knows what served it.
2. **Correct tier-skipping, structurally enforced.** Same account = shared credit pool: when the local CLI's credits are dry, the hub's tier 0 (cloud agy) is dry too. `hub_ask` therefore **skips tier 0 by default** — not via prompt text, but as a router-enforced `skip_tiers: ["agy"]` default. An explicit override exists for the rare exception.
3. **Loop-proof by construction.** Local CLI → hub → cloud CLI must never cycle. Guaranteed two ways: the tier-0 skip (structural) and `answer_only` capability (fallback adapters never spawn subprocesses or touch repos).
4. **A `hub_status` tool** exposing the same secret-free tier/quota state as `GET /hub/status`, so the local agent can check the ladder before asking.
5. **Strong auth, zero new infra.** One `POST /mcp` endpoint on the existing Cloud Run service; bearer token (`HUB_MCP_TOKEN`, new Secret Manager secret) on every request.
6. **Copy-paste laptop setup.** Three commands, no scripts to install, no firewall changes.

## 3. Non-Goals

- **The hub does NOT become the laptop CLI's model backend.** Same reasoning as established before: the Antigravity CLI only speaks to Google backends — it cannot run Groq/NVIDIA/OpenRouter "as a model." `hub_ask` is answer-only by design, not a model plug-in.
- **No job queue, no worker script, no polling.** That was the earlier laptop-worker idea; this epic replaces it. The laptop is a *client* of the hub here, not an execution backend.
- **No write tools in v1.** `hub_ask` performs zero repo side effects. (A future `hub_run` with repo edits would need Amin's explicit tap-gated design — not this epic.)
- **Desktop Antigravity app onboarding.** Same server will serve it later; the desktop client's config/verification is a separate task.
- **Changing the ladder, capability modes, or quota machinery.** Untouched.

## 4. Users

- **Amin's local `agy` CLI agent (primary client).** When its own credits are exhausted, it calls `hub_ask` and keeps working with fallback-provider answers.
- **Amin (operator).** Runs the 3-step laptop setup once; holds the bearer token; reads `tier_used` in answers.
- **Future: the desktop Antigravity app.** Same server, same tools, separate onboarding later.

## 5. Research verdicts (verified 2026-10-22 against `agy` v1.4.4 in this VM)

### 5.1 The CLI speaks remote MCP natively ✅ — no bridge script needed

`agy mcp add --help`:
```
Usage: agy mcp add [flags] <name> <commandOrUrl> [args...]
  -t / --type    Server type: 'stdio' or 'http' (default stdio)
  --header       HTTP header Key: Value (repeatable)
  --env          Environment variable KEY=value (repeatable)
  <commandOrUrl> Executable for a stdio server, or the URL for an http server.
Examples:
  agy mcp add --header "Authorization: Bearer <token>" api https://example.com/mcp
```

- **Remote servers are first-class**: `--type http` (also auto-detected from `http(s)://` URLs). The `/mcp` path convention in the example matches MCP Streamable HTTP.
- **Auth is built in**: repeatable `--header` flags → stored per-server headers. Bearer token goes here; no env-var plumbing needed.
- **Stored config schema** (verified by adding/removing a probe server; file: `~/.gemini/config/mcp_config.json`):
  ```json
  {
    "mcpServers": {
      "model-hub": {
        "disabled": false,
        "headers": { "Authorization": "Bearer <token>" },
        "serverUrl": "https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/mcp"
      }
    }
  }
  ```
- **Binary confirmation**: the `agy` binary contains `McpServer_HttpTransport` (`Url`, `Headers`, `AuthProviderType`) and `McpHttpServerLaunchSpec` (`DontAddMcpSuffixToUrl`) — HTTP transport is real, not a stub flag.
- **What I could NOT verify**: the exact MCP wire protocol version the CLI negotiates (Streamable HTTP vs. legacy SSE). The `/mcp` convention strongly implies Streamable HTTP (the current standard). → **Build-time verification required**: the dev stage must handshake a mock MCP server with the real CLI and confirm `initialize` → `tools/list` → `tools/call` over plain JSON-RPC POSTs. If the CLI turns out to require SSE, the transport layer gets a fallback — the tools and auth design are unchanged either way.
- **Token storage note**: the bearer token lands in plaintext in the laptop's `mcp_config.json` → the setup guide mandates `chmod 600`.

### 5.2 Tier-skipping design (the key decision — locked)

Same account ⇒ shared pool ⇒ when the local agent is dry, cloud tier 0 is dry too. The design:

- `hub_ask(question, context?, skip_tiers? = ["agy"])` — the **default** skips the `agy` tier. The router applies this structurally before the ladder walk; it is not prompt guidance the agent can ignore.
- **Escape hatch**: passing `skip_tiers: []` explicitly re-allows tier 0 (e.g. a future caller that knows cloud has quota the local box lacks). Documented, tested.
- **Tool description** (what the local agent sees) states the contract plainly: *"Ask the Model Hub when your own Antigravity credits are exhausted. Answers come from the fallback ladder (AI Studio → Groq → NVIDIA → OpenRouter → Requesty), never from an Antigravity CLI. Answer-only: no repo changes."*

### 5.3 Loop safety (documented, then enforced in code)

The cycle local-CLI → hub → cloud-CLI cannot happen, three layers deep:

1. **Structural skip**: `hub_ask` defaults `skip_tiers=["agy"]` — the router never invokes the cloud CLI for these requests.
2. **Capability ceiling**: `hub_ask` is `answer_only`. Fallback adapters never spawn subprocesses, never call `agy`, never write repos. Even a misconfigured skip cannot produce an agentic loop.
3. **Deployment rule**: the hub MCP server must **never** be registered in the Cloud Run container's *own* `mcp_config.json`. (The container's config currently holds only stdio servers; keep it that way.) The hub never calls out to the laptop — requests flow one way.

### 5.4 Transport & auth shape (for the architect)

- **One endpoint**: `POST /mcp` on the existing Cloud Run service. No new service, no new infra, no terraform changes beyond the secret.
- **Protocol**: MCP Streamable HTTP (JSON-RPC 2.0). Methods: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`. **Stateless v1**: no `Mcp-Session-Id` tracking; each request independent.
- **Auth**: `Authorization: Bearer $HUB_MCP_TOKEN` on every request, constant-time compare against the Secret Manager value (env-mounted like `GROQ_API_KEY`). Checked **before** any MCP processing → 401 on missing/invalid. (Mirrors the `/terminal/ws` hardening pattern from Epic C.)
- **New secret**: `HUB_MCP_TOKEN` — 256-bit random, created at deploy time, handed to Amin once for his laptop config.

## 6. Epic & stories

**Epic M1 — Hub MCP server** (single epic; small):
- M1-S1: `POST /mcp` transport — JSON-RPC dispatch, `initialize`/`tools/list`/`tools/call`, stateless, bearer auth gate. (Includes the build-time CLI handshake verification.)
- M1-S2: `hub_ask` tool — ladder execution with default `skip_tiers=["agy"]`, `answer_only` enforcement, `tier_used` in the response.
- M1-S3: `hub_status` tool — secret-free tier/quota state (reuses the `/hub/status` payload builder).
- M1-S4: Laptop setup doc + config snippet (the 3-step copy-paste); `chmod 600` note.
- M1-S5: E2E tests — mock MCP client through the real endpoint (list → call → verify skip + tier_used), auth rejection, secret-freedom.

## 7. Open questions (for the architect)

1. Exact MCP `protocolVersion` the CLI negotiates — verify at build time against a mock server (see §5.1).
2. Stateless v1 vs. session tracking — PM recommends stateless; confirm no CLI requirement for `Mcp-Session-Id`.
3. Error mapping: HTTP 401 for auth (before JSON-RPC) vs. JSON-RPC errors inside 200s — PM recommends 401-at-HTTP for auth, JSON-RPC errors for method/params.
4. Should `hub_ask` enforce a max question/context length? (Cost guard against accidental huge prompts on fallback tiers.)

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| CLI requires SSE, not Streamable HTTP | Low | Build-time handshake test; transport fallback is isolated from tools/auth |
| Bearer token leaks from laptop config | Low | `chmod 600` in setup; token is single-purpose and rotatable |
| Agent calls `hub_ask` for non-exhaustion reasons and is surprised tier 0 was skipped | Low | Tool description states the contract; `tier_used` is always reported |
| `skip_tiers: []` override re-opens the loop path | Very low | Still single `execute()`; cloud CLI never has the hub registered (deployment rule §5.3.3) |
