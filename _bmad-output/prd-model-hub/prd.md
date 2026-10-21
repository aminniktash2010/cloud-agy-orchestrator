# PRD — Model Hub with Priority Failover Chain
**Project:** cloud-agy-orchestrator · **Feature:** Model Hub
**Author:** John (BMAD PM) · **Date:** 2026-10-21 · **Status:** Draft for architect review
**Companion:** `_bmad-output/brief-model-hub/brief.md`

---

## 1. Background & scope

Today `agent-service/main.py` runs `agy` (Antigravity CLI) in autopilot mode per `/chat` task, failing over only across three hard-coded intra-AGY model variants (`gemini-3.8-flash-high/medium/low`, `resolve_model_candidates()` / `is_credit_exhausted()`). When AGY credits die, all entry points die.

The Model Hub generalizes this into a **priority-ordered provider chain** owned by `agent-service`, covering every model option available to Amin — inside Antigravity first, then external free tiers, then cheapest paid. Scope: orchestrator (`agent-service`) only; no changes to the PWA UI beyond surfacing hub metadata, no Play track changes, no billing accounts.

**Hard constraints (non-negotiable):** $0.00/month default config · keyless-only GCP (WIF; org policy blocks SA keys) · secrets in Secret Manager only, never code/git · GitHub writes via branch→PR→merge with Amin's tap (agents push nothing) · never touch Amin's Cloud Shell · `agy` CLI autopilot stays the primary agentic path · hub serves ALL entry points (`/chat`, `/chat/stream`, Gmail Apps Script bridge, PWA, Android app module) · chain is configurable · every report shows tier/provider + failover reasons.

---

## 2. Functional requirements

### FR-1 Hub core
- **FR-1.1** `ModelHub` is a single module in `agent-service` exposing one entry: `execute(request) -> HubResult`. `/chat` and `/chat/stream` call it; the Gmail bridge inherits failover because it calls `/chat`.
- **FR-1.2** The hub walks a configured ordered tier list. For each tier it attempts execution; on **exhaustion** (see per-provider signals in §4) it records the attempt (tier, provider, model, reason, latency) and moves to the next tier. Non-exhaustion errors (auth failure, malformed request) do **not** silently advance — they surface, except: an auth failure on an *optional* tier logs a warning and skips that tier (a missing optional key must never kill the chain).
- **FR-1.3** Intra-tier model failover is preserved and generalized: each tier declares an ordered model list; the hub tries models in order within the tier before moving to the next tier. (This keeps today's `gemini-3.8-flash-high → medium → low` behavior as Tier 0's model list.)
- **FR-1.4** Per-tier **circuit breaker**: if a tier exhausts 3 consecutive times within 10 minutes, the hub short-circuits it for a configurable cooldown (default 15 min) to avoid burning latency on a known-dead tier. Health resets on first success.
- **FR-1.5** Idempotency of side effects: repo mutation (git add/commit/push, snapshot) happens **once**, after the winning tier completes — never per attempt. Failed attempts must not leave partial commits.

### FR-2 Configuration
- **FR-2.1** Tier order, per-tier `enabled`, per-tier model list, per-tier daily cap, per-task token budget, and tier `capability` (`agent_loop` | `answer_only`) are configuration, not code. Source of truth: `MODEL_HUB_CONFIG` JSON in Secret Manager; env-var fallback for local/dev.
- **FR-2.2** Changing config requires no redeploy (hub re-reads on each request or on a short TTL, ≤60s) and no code change.
- **FR-2.3** Default config ships with the ladder in §5 and **all paid tiers disabled**.

### FR-3 Capability modes (honest degradation)
- **FR-3.1** Tier 0 (`agy` CLI) is `agent_loop`: full autopilot (PTY, file/shell tools, git, commit/push) — unchanged from today.
- **FR-3.2** API fallback tiers (1–4) ship as `answer_only`: the hub returns the model's completion as an advisory answer. The response, SSE stream, and Drive report **must label it**: `"mode": "answer_only"`, with user-facing text like *"AGY credits exhausted — answered by Groq (gpt-oss-120b) in advisory mode. No code was changed."*
- **FR-3.3** `answer_only` tiers must never execute shell, write files, or push git. Enforced in the adapter, not by prompt wording.
- **FR-3.4** (Epic 3, not v1) A constrained `agent_loop` adapter for API tiers: ReAct-style tool loop with an explicit tool allowlist (read_file, list_files, write_file, edit_file, run_shell allowlisted commands, git status/diff/commit), least-privilege defaults, and destructive ops (push, arbitrary shell) behind a config flag. **Requires Amin's explicit capability-mode tap (§8.6).**

### FR-4 Observability
- **FR-4.1** Every task report — Drive `latest_build_status.md`, `/chat` JSON, `/chat/stream` `done` event — includes: `tier_used`, `provider`, `model`, `capability_mode`, and `failover_attempts[]` (each: tier, provider, model, outcome, reason, latency_ms).
- **FR-4.2** The SSE stream emits a live `status` event on every tier switch (mirroring today's quota-switch messages).
- **FR-4.3** New endpoint `GET /hub/status` (auth: same webhook token rules): current tier order, per-tier `enabled`, capability, circuit-breaker state, last-known quota hints (parsed from `x-ratelimit-*` headers where the provider sends them), and the active config version. No secrets in the response.
- **FR-4.4** Structured logs per attempt at INFO: tier/provider/model/outcome/reason. No prompt or key material in logs.

### FR-5 Entry-point behavior
- **FR-5.1** `/chat` (non-streaming): returns the winning tier's output with hub metadata; if the ladder is exhausted, returns HTTP 429-style structured error listing every tier tried and why (`"ladder_empty"`), **not** a bare 500.
- **FR-5.2** `/chat/stream`: streams tier-switch banners live; `done` event carries full hub metadata.
- **FR-5.3** `/terminal/ws` (interactive PTY): stays `agy`-only (a PTY is inherently the agy tier). On AGY exhaustion it prints an honest banner: quota exhausted, suggests using `/chat` (which will fail over), and shows `GET /hub/status` hint. It must **not** pretend to fail over.
- **FR-5.4** Gmail Apps Script bridge: no code change needed (it calls `/chat`); the forwarded reply should include the serving-tier line so Amin sees it in the Gemini app.

### FR-6 Cost guardrails
- **FR-6.1** Default config enables only $0 tiers. A paid tier (Grok/xAI) cannot become enabled by config alone: enabling requires the corresponding secret to exist **and** an explicit `paid_enabled: true` flag.
- **FR-6.2** Paid tier carries a per-day spend cap (default: $1.00/day, Amin-tunable) and per-task token budget; the hub stops paid attempts at the cap and records `spend_cap_reached`.
- **FR-6.3** No request may trigger billing-account creation, card entry, or subscription signup. Ever.

### FR-7 Secrets
- **FR-7.1** Keys: `GEMINI_API_KEY` (exists), `GROQ_API_KEY`, `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `XAI_API_KEY` (only if Amin opts in) — all in Secret Manager, mounted as env or fetched via ADC at startup. Never in code, logs, git, or Drive reports.
- **FR-7.2** Service account gets `secretAccessor` on exactly these secrets (extend `deploy/grant-sa-roles.sh` pattern). Disable/delete previous secret versions to stay under the 6-version free tier (existing README convention).
- **FR-7.3** Missing optional-tier key ⇒ tier auto-skipped with a warning log (FR-1.2). Missing `GEMINI_API_KEY` ⇒ Tier 1 skipped (it's already registered, so this is a fault path, not the norm).

---

## 3. Non-functional requirements

- **NFR-1 Cost:** $0.00/month on default config. Cloud Run stays within free tier (existing posture); no new billable GCP services (Secret Manager reads are within free quota; keep versions ≤6).
- **NFR-2 Latency:** Exhaustion detection + tier switch < 2s overhead per hop (excluding model inference). Circuit breaker prevents repeated dead-tier latency.
- **NFR-3 Reliability:** Hub must not crash the request on provider SDK errors — every adapter wraps calls with timeouts (default 120s per attempt) and exception→`attempt_failed` mapping.
- **NFR-4 Security:** Keyless GCP per org policy. Third-party API keys server-side only (Cloud Run), never exposed to PWA/Android clients. Webhook token auth unchanged. `.agy_session` isolation unchanged.
- **NFR-5 Privacy:** Prompts sent to free API tiers may be used for provider training (AI Studio: yes per ToS; xAI data-share opt-in: yes if he opts in; Groq/NVIDIA/OpenRouter: check ToS at architecture time). The PRD requires the architect to document the per-tier data-use posture in code comments and the Drive report footer. Mitigation: never send key/secret material in prompts (existing prompts don't).
- **NFR-6 Maintainability:** One adapter interface; adding a provider = new adapter class + config entry, no handler changes. Provider limit tables dated (re-verify cadence: each release touching the hub).
- **NFR-7 No regression:** With healthy AGY credits, `/chat` behaves exactly as today (same Tier-0 models/order, same autopilot env, same commit/push flow) plus the new hub metadata fields.

---

## 4. Provider catalog (verified 2026-10-21)

Each entry: auth · verified free limits · exhaustion signal · adapter approach · ladder position.

### Tier 0 — AGY credits (Antigravity `agy` CLI) · `agent_loop` · $0
- **Auth:** Existing Antigravity OAuth token (`/mnt/cache/antigravity-oauth-token` → isolated session). No change.
- **Models available inside Antigravity (free):** Gemini 3.8 Flash (high/medium/low — today's `ACTIVE_QUOTA_MODELS`); also bundled: Claude Sonnet 4.6, Claude Opus 4.6, GPT-OSS-120b. ⚠️ GPT-OSS-120b removal from Antigravity scheduled **2026-11-02** — model list is config for this reason.
- **Limits:** ~20 agent requests/day on free tier, weekly caps, ~5hr quota refresh; opaque credit system ($0.01/credit overages exist but Amin buys none).
- **Exhaustion signal:** Existing `is_credit_exhausted()` — returncode 3 + markers ("credits balance is too low", `resource_exhausted`, "check quota", 429, "rate limit", "out of credits"). Keep, extend markers as observed.
- **Adapter:** None — today's subprocess/PTY path, generalized so the model list comes from config.
- **Position:** 1st, always. Only `agent_loop` tier in v1.

### Tier 1 — Google AI Studio (Gemini API) · `answer_only` · $0
- **Auth:** API key (`AIzaSy…`) from AI Studio → **`GEMINI_API_KEY` already in Secret Manager.** No Amin action.
- **Verified free limits (community-verified Oct 2026; Google no longer publishes a central table — re-check AI Studio rate-limit console):** Gemini 2.5 Flash ≈1,500 RPD / 10 RPM / 250k TPM · Gemini 3.5 Flash-Lite 500 RPD / 10 RPM / 250k TPM · Gemini 3.8 Flash 20 RPD / 5 RPM / 250k TPM. RPD resets midnight Pacific. **Correction to Amin's "~1M tokens/day":** no published aggregate daily-token figure exists; the per-model RPD caps are the binding constraint (3.8 Flash gives only 20 requests/day). Multimodal text/audio/vision: confirmed. Native function calling: yes. Pro models (incl. Gemini 4 Argon): paid-only, not used.
- **Exhaustion signal:** HTTP 429 + `RESOURCE_EXHAUSTED` / `quotaExceeded` in error body. Distinguish RPM (retry-after seconds — hub retries once after delay *within* the tier) vs RPD (advance tier).
- **Adapter:** `google-genai` SDK against `generativelanguage.googleapis.com/v1beta` (native; best function-calling for Epic 3). Model order (high-RPD first): `gemini-2.5-flash → gemini-3.5-flash-lite → gemini-3.8-flash`.
- **Position:** 2nd. Privacy note: free-tier prompts may train Google models (NFR-5; accepted risk, flagged to Amin).

### Tier 2 — Groq · `answer_only` · $0 · 🔑 needs Amin's key
- **Auth:** `gsk_` key from console.groq.com (email/GitHub/Google signup, **no card**) → `GROQ_API_KEY` in Secret Manager.
- **Verified free limits:** gpt-oss-120b / gpt-oss-20b / qwen3.x-27b: **30 RPM, 1,000 RPD, 8,000 TPM, 200,000 TPD per model class**, org-level (daily token cap binds first on long prompts). No card needed; adding a card (not required) raises limits — out of scope.
- **Exhaustion signal:** HTTP 429 with `retry-after` + `x-ratelimit-remaining-requests` / `x-ratelimit-remaining-tokens` headers (also feed `/hub/status` quota hints).
- **Adapter:** OpenAI-compatible client → `https://api.groq.com/openai/v1`. Tool calling supported (gpt-oss-120b, qwen3.8-27b) — relevant for Epic 3. Model order: `openai/gpt-oss-120b → qwen/qwen3.8-27b`.
- **Position:** 3rd.

### Tier 3 — NVIDIA Build (NIM) · `answer_only` · $0 · 🔑 needs Amin's key
- **Auth:** `nvapi-` key from build.nvidia.com (free NVIDIA Developer account; **email + phone OTP verification, no card**) → `NVIDIA_API_KEY` in Secret Manager.
- **Verified free limits:** **~40 RPM per key**, HTTP 429 on exceed; **no daily cap** on current accounts (Oct 2026 reports: NVIDIA removed the old 1,000-credit signup grant → "unlimited API requests without daily limits", rate-limited only). ⚠️ Sources conflict on whether legacy credits still meter some accounts — hub treats **429 as the single exhaustion signal** and is correct under both regimes. Catalog 80+ open models incl. DeepSeek V4 Pro/Flash, Nemotron 3, Llama 4, Qwen 3.5, Kimi K2.5, GLM 5.1, GPT-OSS-120B, Gemma 4, Mistral, Phi; roster rotates by demand — slugs resolved at runtime from `/v1/models`.
- **Exhaustion signal:** HTTP 429. (No credit-balance API to poll; do not build one.)
- **Adapter:** OpenAI-compatible → `https://integrate.api.nvidia.com/v1`. Model order: `deepseek-v4-flash → llama-4-maverick → qwen-3.5` (verify slugs at build time). ToS: prototyping/development/research only — acceptable for this personal orchestrator; production use would need NVIDIA AI Enterprise (not pursued).
- **Position:** 4th.

### Tier 4 — OpenRouter `:free` pool · `answer_only` · $0 · 🔑 needs Amin's key (optional)
- **Auth:** key from openrouter.ai (no card to start) → `OPENROUTER_API_KEY`.
- **Verified free limits:** `:free`-suffixed models (16+ text models as of 2026-10-08; **roster rotates — treat as unstable**): 20 RPM; **50 RPD** (rises to 1,000 RPD only after $10 lifetime credit purchase — a paid action, out of default scope).
- **Exhaustion signal:** HTTP 429. Upstream host rate limits may also surface as 429/502 — map to tier-advance, not user error.
- **Adapter:** OpenAI-compatible → `https://openrouter.ai/api/v1` with required `HTTP-Referer` + `X-Title` headers. Model selection: `model: "openrouter/auto"` restricted to `:free` filter, or explicit pinned `:free` IDs from config (prefer explicit; `auto` may route to non-free — **must filter**).
- **Position:** 5th (last-chance free tier).

### ❌ EXCLUDED — GitHub Models (Amin's list #2)
**Do not implement. Service fully retired 2026-07-30** (official Microsoft Learn docs: playground, catalog, inference API, BYOK "no longer available to any customer"). Any adapter would call a dead endpoint. Replacement (Microsoft Foundry Models) requires a paid Azure subscription → excluded by the $0 constraint. Amin is informed, no action needed — this entry exists so nobody re-adds it later.

### Tier 5 — Grok / xAI · `answer_only` → `agent_loop`-eligible · 💰 PAID · 🔑🔑 needs Amin's key + funding tap
- **Auth:** `xai-` key from console.x.ai (self-serve signup) → `XAI_API_KEY`. **Disabled by default.**
- **Verified pricing:** No permanent free tier. New accounts: **$25 promo credits (expire 30 days)**; up to **$150–175/mo via data-sharing opt-in** (eligibility may require prior spend; prompts then train xAI — privacy tradeoff). Then pay-per-token: **grok-4.1-fast $0.20 in / $0.50 out per 1M** (cheapest paid tier found), grok-4.5 $2/$6.
- **Exhaustion signal:** HTTP 429 (rate limit) and 402/insufficient-credit errors → advance tier / stop paid attempts at cap.
- **Adapter:** OpenAI-compatible → `https://api.x.ai/v1`; function calling supported. Model order: `grok-4.1-fast → grok-4.5`.
- **Position:** 6th — below ALL free tiers (cost-first ordering per Amin's instruction). Enablement requires: secret exists + `paid_enabled: true` + daily spend cap (default $1.00/day) + Amin's explicit funding choice (promo-only vs data-share vs pay-per-token). **This is the single biggest Amin tap in the project.**

---

## 5. Default priority ladder (config, not code)

| Order | Tier | Provider / path | Models (in-tier order) | Capability | Cost | Needs Amin |
|---|---|---|---|---|---|---|
| 1 | 0 | AGY credits (`agy` CLI) | gemini-3.8-flash-high → medium → low (+ claude/gpt-oss if CLI-verified) | agent_loop | $0 | — |
| 2 | 1 | Google AI Studio | gemini-2.5-flash → gemini-3.5-flash-lite → gemini-3.8-flash | answer_only | $0 | — (key exists) |
| 3 | 2 | Groq | openai/gpt-oss-120b → qwen/qwen3.8-27b | answer_only | $0 | 🔑 create key |
| 4 | 3 | NVIDIA Build | deepseek-v4-flash → llama-4-maverick → qwen-3.5 | answer_only | $0 | 🔑 create key |
| 5 | 4 | OpenRouter `:free` | pinned `:free` IDs (config) | answer_only | $0 | 🔑 create key (optional) |
| 6 | 5 | Grok / xAI | grok-4.1-fast → grok-4.5 | answer_only | 💰 paid | 🔑🔑 key + funding tap — **disabled by default** |

Amin can reorder/disable tiers and edit model lists via `MODEL_HUB_CONFIG` without a redeploy (FR-2).

---

## 6. Epics

### Epic 1 — Hub core + AI Studio failover (v1, shippable)
Generalize `resolve_model_candidates()`/`is_credit_exhausted()` into `ModelHub`: tier chain engine, per-tier model lists, exhaustion detection, circuit breaker, `MODEL_HUB_CONFIG` (Secret Manager JSON + env fallback), `answer_only` adapter for **AI Studio** (no new key needed — fastest value), hub metadata in `/chat` response + Drive report + SSE `done` event, live tier-switch SSE banners, `/hub/status` endpoint, structured logging. Acceptance: with AGY quota forced-exhausted (test hook), a `/chat` request is served by AI Studio with correct `tier_used`/`failover_attempts` in all three surfaces; with healthy AGY, behavior identical to today plus metadata.

### Epic 2 — Free-tier breadth (Groq, NVIDIA, OpenRouter)
One OpenAI-compatible adapter class parameterized per provider (base URL, key env, headers, model list, 429 semantics incl. `retry-after`/`x-ratelimit-*` parsing). Secret Manager registration for `GROQ_API_KEY`, `NVIDIA_API_KEY`, `OPENROUTER_API_KEY` (+ `secretAccessor` grants). Runtime model-slug resolution for NVIDIA. Acceptance: each provider serves a request when higher tiers are exhausted; missing key ⇒ tier skipped with warning, chain continues; 429 ⇒ advance; quota hints visible on `/hub/status`.

### Epic 3 — Agent-loop adapter for API tiers (capability promotion)
Constrained ReAct tool loop (allowlisted tools, repo sandbox = today's `/workspace/<repo>`, least-privilege defaults, push/shell-destructive ops behind config flag) so fallback tiers can graduate from `answer_only` to `agent_loop` per-tier via config. **Gated on Amin's capability-mode tap (§8.6).** Acceptance: a Tier-1-served task can read/edit files and produce the same commit/push flow as Tier 0, with tool calls logged; destructive-op flag off by default.

### Epic 4 — Paid tier (Grok/xAI) + spend guardrails
`XAI_API_KEY` plumbing, `paid_enabled` gating, per-day spend cap + per-task token budget, `spend_cap_reached` reporting, 402 handling. **Gated on Amin's funding tap (§8.4).** Acceptance: with cap $0 → tier never fires; with promo credits → serves and reports; cap hit → clean stop + report, no surprise charges.

### Epic 5 — Client surfacing
PWA + Android app show serving-tier line per response (from hub metadata); `/terminal/ws` exhaustion banner (FR-5.3); Gmail bridge reply includes tier line (FR-5.4). Small, last.

**Suggested build order:** Epic 1 → Epic 2 → Epic 5 → Epic 3 → Epic 4 (3 and 4 are tap-gated and independent).

---

## 7. Acceptance criteria (v1 = Epics 1+2)

1. **AC-1 (failover):** With AGY exhaustion simulated, `POST /chat` returns `status: success` served by Tier 1, with `tier_used`, `provider`, `model`, `capability_mode: "answer_only"`, and `failover_attempts` showing Tier 0 exhausted (reason + latency) — in the JSON, the SSE `done` event, and the Drive report.
2. **AC-2 (ladder walk):** With Tiers 0–1 exhausted and Tier 2 key present, the request is served by Groq; with the key absent, Tier 2 is skipped with a warning log and the chain continues.
3. **AC-3 (ladder empty):** All tiers exhausted/disabled ⇒ structured `"ladder_empty"` response listing every attempt + reason (HTTP 429 semantics), never a bare 500 or a hallucinated answer.
4. **AC-4 (no regression):** Healthy AGY credits ⇒ Tier 0 serves exactly as today (same models/order/autopilot/commit flow); response only adds hub metadata fields.
5. **AC-5 (answer_only honesty):** Any Tier 1–4 response carries the advisory-mode label in all three surfaces; no file/shell/git side effects occur on those paths (verified by test asserting no repo diff).
6. **AC-6 (single commit):** A task that fails over mid-ladder produces at most one commit/push (only Tier 0 `agent_loop` commits in v1).
7. **AC-7 (config):** Reordering tiers via `MODEL_HUB_CONFIG` takes effect without redeploy (≤60s TTL); disabling all paid tiers is the default and verified.
8. **AC-8 (cost):** Default config makes zero paid calls (test with network stubs asserting no xAI traffic); Secret Manager shows no new billable services.
9. **AC-9 (secrets):** No key material in code, logs, git history, or Drive reports (grep-based test in CI).
10. **AC-10 (status):** `GET /hub/status` returns tier order, enabled flags, circuit-breaker state, and quota hints; contains no secrets.

---

## 8. Amin-tap register (explicit — nothing here happens without him)

| # | Tap | Effort | Default if he declines |
|---|---|---|---|
| 8.1 | Create **Groq** key (console.groq.com, ~2 min, no card) | 2 min | Tier 2 skipped |
| 8.2 | Create **NVIDIA** key (build.nvidia.com, email + phone OTP) | 5 min | Tier 3 skipped |
| 8.3 | Create **OpenRouter** key (optional) | 2 min | Tier 4 skipped |
| 8.4 | **xAI funding decision:** create `XAI_API_KEY`? Choose: (a) promo-credits-only, (b) data-sharing opt-in (+$150/mo credits, prompts train xAI), or (c) pay-per-token with monthly cap. Tier 5 stays **disabled** until he answers. | 5 min + decision | Tier 5 disabled, $0 preserved |
| 8.5 | **Informational:** GitHub Models is retired — nothing to do; recorded so nobody rebuilds it. | 0 | — |
| 8.6 | **Capability mode:** accept `answer_only` fallback labeling for v1 (recommended), or require Epic 3's agent-loop adapter before any fallback ships. | decision | `answer_only` (recommended) |
| 8.7 | **Privacy accepted risk:** AI Studio free-tier prompts may be used to train Google models. Mitigation = keep secrets out of prompts (already true). Paid API project would fix it but violates $0 rule — not proposed. | acknowledge | documented risk |
| 8.8 | Standing rule: implementation via **branch → PR → merge**, his tap per step; agents push nothing. | per PR | — |

## 9. Open questions (for architect / Amin)

1. **NVIDIA credits ambiguity:** do new accounts still receive 1,000 credits, or is it unlimited-rate-limited? (Hub is signal-agnostic — 429 either way — but the PRD records the question for the console check.)
2. **`agy --model` names** for Claude 4.6 / GPT-OSS variants inside Antigravity — verify against the CLI at build time; GPT-OSS removal 2026-11-02 may moot one entry.
3. **Per-tier daily caps & per-task token budgets:** architect proposes defaults (e.g., stop AI Studio at 80% of RPD to keep headroom for interactive use); Amin tunes.
4. **PWA manual tier override:** v1 = auto only; manual pin/override deferred — confirm Amin is fine with that.
5. **Gmail bridge reply format:** exact one-line tier attribution format — propose in Epic 5.
6. **OpenRouter model pinning:** which `:free` IDs to pin at build time (roster churns weekly) — architect picks from the live catalog on build day and dates the choice.
7. **Data-use ToS re-check:** Groq / NVIDIA / OpenRouter training-use clauses — architect verifies current ToS during Epic 2 and records in code comments (NFR-5).

## 10. Out of scope (explicit)

GitHub Models integration · Microsoft Foundry Models · any paid subscription or billing account · Play production track · local inference · image/video generation · replacing `agy` autopilot as primary path · credential changes to Amin's Cloud Shell session.

## 11. Handoff to architect

Next: BMAD architect. Inputs: this PRD + `brief-model-hub/brief.md` + `agent-service/main.py` (hub insertion points: `resolve_model_candidates`, `is_credit_exhausted`, `/chat`, `/chat/stream` handlers) + `terraform/` + `deploy/grant-sa-roles.sh` (secret grants) + `gemini-bridge/apps_script_forwarder.gs` (reply format). Suggested architecture deliverable: adapter interface, config schema for `MODEL_HUB_CONFIG`, exhaustion-signal mapping table, and the Epic 1 module layout.
