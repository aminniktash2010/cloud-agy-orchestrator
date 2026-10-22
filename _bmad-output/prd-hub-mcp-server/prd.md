# PRD — Model Hub as an MCP Server
**Project:** cloud-agy-orchestrator · **Feature:** Hub MCP server (`POST /mcp`: `hub_ask` + `hub_status`)
**Author:** 📋 John (BMAD PM) · **Date:** 2026-10-22
**Status:** Ready for architecture · **Source brief:** `_bmad-output/brief-hub-mcp-server/brief.md`

---

## 1. Functional requirements

### FR-1 — MCP transport (`POST /mcp`)
- **FR-1.1** The existing Cloud Run service exposes `POST /mcp` implementing MCP Streamable HTTP (JSON-RPC 2.0).
- **FR-1.2** Supported methods: `initialize` (returns `protocolVersion`, `capabilities: {tools: {}}`, `serverInfo: {name: "model-hub", version}`), `notifications/initialized` (accepted, no-op), `tools/list`, `tools/call`. Unknown methods → JSON-RPC `-32601`.
- **FR-1.3** Stateless v1: the server tracks no sessions and requires no `Mcp-Session-Id`. (Architect to confirm the CLI doesn't demand one — see brief §7 Q2.)
- **FR-1.4** Malformed JSON-RPC → `-32600`; invalid params → `-32602`. Errors never leak internals (no stack traces, no env values).
- **FR-1.5** Build-time verification: the dev stage handshakes a mock MCP server with the **real** `agy` CLI (`agy mcp add` → list/call) to confirm the wire protocol before shipping.

### FR-2 — Authentication
- **FR-2.1** Every `POST /mcp` request must carry `Authorization: Bearer $HUB_MCP_TOKEN`. Checked **before** any MCP processing.
- **FR-2.2** Missing/invalid token → HTTP **401** (not a JSON-RPC error — auth happens at the HTTP layer). No tool listing, no method execution.
- **FR-2.3** Comparison is constant-time. The token value is never logged, never echoed, never appears in any response.
- **FR-2.4** `HUB_MCP_TOKEN`: 256-bit random, stored in GCP Secret Manager, env-mounted into the container (same pattern as `GROQ_API_KEY`). Terraform change is limited to the new secret + mount; no other infra changes.

### FR-3 — `hub_ask` tool
- **FR-3.1** Input schema: `{question: string (required), context?: string, skip_tiers?: string[]}`.
- **FR-3.2** **Default `skip_tiers = ["agy"]`, applied structurally by the router before the ladder walk.** The `agy` tier is never attempted unless the caller explicitly overrides.
- **FR-3.3** Execution reuses `ModelHub.execute()` with the skip applied; capability is forced `answer_only` regardless of caller input (no repo side effects, no subprocess spawning beyond the existing adapter paths).
- **FR-3.4** Output schema: `{answer: string, tier_used: string, provider: string, model: string, capability_mode: "answer_only", failover_attempts: [{tier, outcome}]}`. `tier_used` is **never** `"agy"` unless `skip_tiers` was explicitly overridden — asserted by test.
- **FR-3.5** Tool description (verbatim intent, exact wording at dev's discretion): *"Ask the Model Hub a question when your own Antigravity credits are exhausted. The answer comes from the fallback provider ladder (AI Studio → Groq → NVIDIA → OpenRouter → Requesty) — never from an Antigravity CLI. Answer-only: no repo changes, no tool use."*
- **FR-3.6** Exhaustion semantics: if every fallback tier is exhausted, return a structured "all tiers exhausted" answer (not an exception) — same convention as the hub's existing fallback behavior.

### FR-4 — `hub_status` tool
- **FR-4.1** Input schema: `{}` (no params).
- **FR-4.2** Returns the tier/quota state in the same shape as `GET /hub/status` (tiers, `live_quota` blocks, `quota_source` labels) — reuses the existing payload builder.
- **FR-4.3** The output contains **zero secrets** (key material, tokens, config internals) — covered by a secret-freedom test extending the Epic C `test_no_secret_leak` pattern.

### FR-5 — Loop safety (structural, tested)
- **FR-5.1** There is **no code path** by which a `hub_ask` call can invoke any `agy` CLI while `skip_tiers` holds its default. (Test: default call → assert `tier_used != "agy"` and assert the agy adapter's subprocess entry was never hit.)
- **FR-5.2** `answer_only` is enforced server-side, not requested by the client: even a crafted `tools/call` cannot escalate capability.
- **FR-5.3** Deployment rule (documented in the repo, e.g. in the MCP doc): the hub MCP server is **never** added to the Cloud Run container's own `mcp_config.json`.

### FR-6 — Laptop setup UX (Amin-facing)
- **FR-6.1** Setup is exactly three copy-paste steps (token handed to Amin once, privately):
  ```bash
  # 1. (Mamali gives you HUB_MCP_TOKEN once — keep it private)
  # 2. Register the hub (paste your token):
  agy mcp add --header "Authorization: Bearer PASTE_TOKEN_HERE" model-hub https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/mcp
  # 3. Verify:
  agy mcp list
  # → model-hub  http  enabled  https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/mcp
  ```
- **FR-6.2** The setup doc instructs `chmod 600 ~/.gemini/config/mcp_config.json` (the token is stored in plaintext there).
- **FR-6.3** Troubleshooting section: 401 → token wrong/missing; `disabled` in list → `agy mcp enable model-hub`.

### FR-7 — No regressions
- **FR-7.1** `/chat`, `/chat/stream`, `/hub/status`, the PWA, and the terminal are untouched and green (full suite).
- **FR-7.2** No terraform changes beyond the `HUB_MCP_TOKEN` secret + env mount.

## 2. Non-functional requirements

- **NFR-1 Latency**: MCP handshake overhead < 500ms; `hub_ask` latency = ladder latency (unchanged).
- **NFR-2 Availability**: the endpoint rides the existing service (no new infra); a hung fallback provider must not hang the MCP call — the hub's existing per-tier timeouts apply.
- **NFR-3 Security**: per §FR-2 + brief §5.4. Token rotation = new secret version + re-run step 2 of setup (documented).
- **NFR-4 Observability**: MCP calls are logged like hub calls (tool name, tier_used, latency; never the question content beyond existing hub logging policy, never the token).
- **NFR-5 Cost**: $0 infra delta. `hub_ask` spends the same fallback-tier quota as `/chat` — no new spend vectors.

## 3. Acceptance criteria

- **AC-1** A mock MCP client (raw JSON-RPC over HTTP) completes `initialize` → `tools/list` → `tools/call hub_ask` against the real endpoint; `tools/list` returns exactly `hub_ask` + `hub_status` with the §FR-3/FR-4 schemas.
- **AC-2** `hub_ask` with default params on a healthy ladder returns an answer with `tier_used != "agy"` and `capability_mode == "answer_only"` (asserted, not assumed).
- **AC-3** `hub_ask` with `skip_tiers: []` is allowed to use tier 0 (escape hatch works end-to-end).
- **AC-4** Requests without/invalid bearer → HTTP 401; nothing executes (verified at the HTTP layer, before JSON-RPC dispatch).
- **AC-5** `hub_status` output passes the secret-freedom test (no token/key material anywhere in the payload).
- **AC-6** The real `agy` CLI (v1.4.4+) can `mcp add` the dev/staging endpoint and list the two tools (build-time CLI handshake — brief §5.1; documents the negotiated `protocolVersion`).
- **AC-7** Full existing suite green (FR-7.1); new tests ≥ 10 covering AC-1..AC-5.
- **AC-8** The laptop setup doc's three commands are verified against the real config schema (§brief 5.1 — already verified in research).

## 4. Out of scope (explicit)

1. The hub as the laptop CLI's **model backend** (impossible — CLI speaks only Google backends; see brief §3).
2. **Job queue / worker polling** — the replaced idea; explicitly not built.
3. **Write tools** (`hub_run` etc.) — future, tap-gated, not this epic.
4. **Desktop Antigravity app onboarding** — same server later; separate task.
5. Changes to the ladder order, quota machinery, or PWA.

## 5. Rollout

1. Merge → deploy workflow rebuilds the image + applies the `HUB_MCP_TOKEN` secret (terraform).
2. Mamali generates the token, stores it in Secret Manager, hands it to Amin once (private).
3. Amin runs the 3-step laptop setup; Mamali verifies via `hub_status` through the real CLI if Amin wants.
4. PWA/status panel: no changes needed (the MCP tools don't touch it).

## 6. Open questions (carried from the brief — for the architect)

1. Exact MCP `protocolVersion` the CLI negotiates (build-time handshake).
2. Stateless v1 vs `Mcp-Session-Id` — confirm.
3. Max question/context length guard on `hub_ask` (cost guard).
