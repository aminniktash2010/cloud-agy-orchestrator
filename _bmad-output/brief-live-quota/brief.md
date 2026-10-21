# Brief — Live Quota per Provider for the Model Hub
**Project:** cloud-agy-orchestrator (Cloud Antigravity Orchestrator)
**Feature:** Live quota/usage per provider, surfaced in `GET /hub/status` and the PWA status panel
**Author:** 📋 John (BMAD PM) · **Date:** 2026-10-21
**Status:** Ready for architecture (next: BMAD architect)
**Depends on:** Model Hub Epic 1 (LIVE on Cloud Run) · `_bmad-output/{brief,prd,architecture}-model-hub/`

---

## 1. Problem

The Model Hub's `GET /hub/status` endpoint and the PWA status panel currently show **static estimates**: a `daily_request_cap` from config and a local `requests_today` counter that only counts what the hub itself sent. Amin's words: *"make sure we check each provider api calls to pull the correct date"* — and he sent a screenshot of his Antigravity usage page as the bar to hit:

- Available AI Credits: 0
- Gemini Models: Weekly Limit Remaining **23%** (full refresh in 5 days 6 hours); Five Hour Limit Remaining **100%**
- Claude and GPT models: Weekly Limit **0%** (refresh in 5 days 12 hours); five-hour limit n/a
- Gemini 4 Argon: Weekly Limit **0%** (refresh in 5 days 23 hours)

Two things make this non-trivial, and both are why this brief exists:

1. **Not every provider exposes a quota API.** Some do (OpenRouter), some leak it in response headers (Groq), some expose nothing at all (AI Studio, NVIDIA, AGY's IDE page is a private UI). A design that assumes a pollable endpoint per tier will silently lie for half the ladder.
2. **AGY exhaustion is partial, not binary.** The screenshot proves it: Gemini models still have 23% weekly quota while Claude/GPT and Argon are at 0%. The hub's current tier-0 logic (`agy_exhausted()` → skip the whole tier) leaves Gemini-family quota on the table when only the Claude family is dry. The router needs per-family state, not a single exhausted/not bit.

3. **Amin's follow-up requirement (2026-10-21, locked):** *"make sure we do make it connect to our on the fly agy cli on cloud run too so in the cli user would be able to check all its models."* The Model Hub runs on Cloud Run where the `agy` CLI (v1.4.3) runs in autopilot per repo, authenticated with his OAuth token. He wants the AGY tier's quota state sourced from that **live on-the-fly CLI** — a proactive per-model/per-family probe, not just failure-driven error parsing — so the user can check all its models and their quota from the hub (`/hub/status` + PWA). Feasibility was verified against the real binary (see §5, AGY row): the CLI refreshes quota on startup via its internal quota manager and exposes `agy models` + `/usage` + `/credits`; there is no public HTTP quota API, so the probe runs on-device in the container.

## 2. Goals

1. **Real numbers where they exist.** For every tier, `GET /hub/status` reports the best available quota signal, and the PWA renders it — each value clearly badged by source: `live` (polled from a provider API), `headers` (harvested from real response headers), or `estimated` (today's static config + local counter, honestly labeled as such).
2. **Per-family AGY state.** Track exhaustion per Antigravity model family (Gemini / Claude-GPT / Argon-class) so the router skips only the dry families instead of abandoning Tier 0 wholesale.
3. **The hub reads the live CLI on Cloud Run.** A lightweight `cli_probe` runs the on-the-fly `agy` CLI in the container (where his OAuth token lives) to list all its models and refresh per-family quota state — so the user can check every model and its quota from the hub, not just infer it from failures. The probe never sends a prompt and never burns AI credits (inspection only).
3. **Never block a chat request on a quota fetch.** Quota checks run on a background poller with a TTL cache (5–15 min); a chat request reads the cache, never waits on a provider.
4. **The check itself costs ~$0.** No paid quota APIs, no quota-burning probe calls. Polling must be free (dedicated usage endpoints, header harvesting) or near-free (one tiny request per poll interval, worst case).
5. **Graceful per-provider degradation.** If a provider's quota signal is unreachable, that tier falls back to today's estimated display — the panel shows "estimated", never a stale number presented as live, and never an error that breaks the page.

## 3. Non-Goals

- Reverse-engineering private/undocumented endpoints (AGY's IDE usage page, Requesty's dashboard session APIs). Fragile, ToS-gray, and breaks without notice. Listed as a research spike at most.
- Spending real quota or money to check quota. A poll that costs a request against the daily cap defeats its purpose.
- Real-time (per-second) quota. 5–15 min staleness is fine for a status panel and router hints; exhaustion is still detected post-hoc via the existing 429/marker signals.
- Moving the xAI tier forward — it stays disabled until Amin funds it. Its quota API shape is documented in the PRD for later, nothing is built for it now.
- Changing the failover ladder order or capability modes — that's config, untouched by this feature.

## 4. Users

- **Amin (primary, sole operator).** Opens the PWA, wants to see at a glance which providers have juice left — the same "23% remaining, refreshes in 5 days" view he gets from the AGY page, but for the whole ladder.
- **The router itself (system user).** Reads cached per-tier/per-family quota state to skip provably-dry tiers before attempting them (faster failover, fewer wasted calls).

## 5. Provider verification verdicts (researched 2026-10-21)

Classification: **LIVE API** (poll it) · **HEADERS** (harvest from normal traffic) · **NO SIGNAL** (keep honest estimates).

| # | Tier | Verdict | What we found |
|---|---|---|---|
| 0 | AGY / Antigravity | ✅ **LIVE via on-device CLI probe** (+ error parsing as fallback) | The `agy` CLI (v1.4.3) has **no quota/account/usage subcommand** (`models`, `agent`, `mcp`… only) and the usage page in Amin's screenshot is the IDE's private web UI — no public HTTP API. **But the binary was verified to carry a real quota path:** an internal quota manager (`third_party/jetski/cli/store/quota_manager.go`, `doRefreshQuota`) refreshes quota on CLI startup when logged in (observed in the CLI log: `doRefreshQuota: skipped (not logged in)`), calling `ReconnectExtensionServer/RetrieveUserQuotaSummaryLEGACY` → `QuotaSummaryResponse` with `QuotaSummaryBucket`s carrying `quota_remaining`, `quota_resets`, `quota_bucket_name`, `quota_cost_metrics`. The summary is cached locally (`retrieveUserQuotaSummaryCache` / `internal/quota_summary.pb` under the CLI state dir). The CLI also ships `/usage` and `/credits` slash commands (`agy --print "/usage"`) and `agy models` lists all available models when logged in. **On Cloud Run the CLI is logged in** (OAuth token at `/mnt/cache/antigravity-oauth-token` via `agy-wrapper.sh`), so a `cli_probe` can: run `agy models` (live model→family taxonomy, no hardcoding), trigger the startup quota refresh, and read the local cache — zero prompts sent, zero AI credits burned, zero extra auth. What it **cannot** do: machine-readable JSON output (`models` has no `--output-format`), or a direct HTTP call (the RPC goes through CLI auth — reverse-engineering it is out of scope). Fallback stays: per-family exhaustion parsed from CLI error output (`signals.py`). |
| 1 | Google AI Studio | ❌ **NO SIGNAL** | No public quota/usage API for API keys — Google deliberately points developers at the AI Studio dashboard's rate-limit page (web UI only). The GCP Service Usage API is per-project and reports aggregate usage, not remaining free-tier quota. Responses carry per-call `usageMetadata` (tokens used) — harvestable for local accounting, not for "X% remaining". |
| 2 | Groq | ✅ **HEADERS** | No usage endpoint, but **every response carries** `x-ratelimit-limit-requests` (RPD cap), `x-ratelimit-remaining-requests` (RPD left), `x-ratelimit-limit-tokens` (TPM cap), `x-ratelimit-remaining-tokens` (TPM left), `x-ratelimit-reset-requests`, `x-ratelimit-reset-tokens` (+ `retry-after` on 429). Source: Groq rate-limit docs (console.groq.com/docs/rate-limits). Zero extra calls needed. |
| 3 | NVIDIA Build | ❌ **NO SIGNAL** | No documented usage/balance endpoint; no documented rate-limit response headers beyond `Retry-After` on 429. Credits are account-level (1,000 initial, top-up via forum request). → 429 stays the exhaustion signal. (Architect: capture and log all response headers on first integration — if undocumented `x-*` headers appear, reclassify.) |
| 4 | OpenRouter | ✅ **LIVE API** | `GET https://openrouter.ai/api/v1/key` (current docs; `/auth/key` also observed working) with the key's Bearer token returns key metadata incl. `limit`, `limit_remaining`, `limit_reset`, `budgets`/`effective_budget`, free-tier status fields. **Poll budget:** the Data API is rate-limited 30 req/min per key and 500 req/day per account — the quota poller must stay well under that (e.g. every 10 min = 144/day leaves headroom; share the budget with any analytics use). Cheap, free, authoritative. |
| 5 | Requesty | ⚠️ **NO SIGNAL** (documented) — harvest `usage.cost` | No documented public balance/credits endpoint for API keys (dashboard-only; a "Management APIs" section exists for keys/groups/members but no balance endpoint is documented). **But every response carries `usage.cost`** (USD for the call, incl. streaming with `include_usage`) — harvestable for local spend accounting against the known $0 free tier (200 req/day on free models). 402/429 = exhausted. |
| 6 | xAI | 📝 **Documented for later** | Tier disabled (no funding). No confirmed public usage/balance API found; rate limits are spend-tier based with 429 on exceed. Re-verify when Amin funds the tier. Nothing built now. |

**Bottom line:** 1 live API (OpenRouter), 1 headers source (Groq), 2 harvestable per-call signals (AI Studio `usageMetadata`, Requesty `usage.cost`), 2 no-signal (AGY public, NVIDIA) — AGY gets per-family error parsing as its "live" signal.

## 6. Proposed shape (for the architect to refine)

- **A `hub/quota_live.py` poller**: one function per LIVE-API provider (OpenRouter now; registry pattern for later), running on a background thread/loop every N minutes (default 10), writing `{value, quota_source, quota_updated_at}` into the existing quota store. Chat requests read the cache; a stale cache (poller failing) degrades that tier to `estimated`, never to an error.
- **Header harvesting in the adapters**: the Groq path of `OpenAICompatAdapter` records the latest `x-ratelimit-*` values per model after each real call (no extra traffic). `GeminiAdapter` records per-call `usageMetadata` for local token accounting; Requesty path records `usage.cost`.
- **AGY per-family state + `cli_probe`**: extend `signals.py` with family-aware markers (gemini-family / claude-gpt-family / argon-family) parsed from CLI error output; the breaker tracks per-family open/closed; the router tries remaining families before leaving Tier 0. **Plus the probe:** a `cli_probe` component runs inside the Cloud Run container against the on-the-fly CLI on the same 10-min TTL as the poller — `agy models` for the live model list (model→family taxonomy derived at runtime, not hardcoded), the CLI's startup quota refresh for per-bucket `quota_remaining`/`quota_resets`, read from the local quota-summary cache (zero network of our own, zero prompts sent, zero credits burned). Probe failures degrade to the error-parsing path; the probe never runs on the chat request path.
- **`/hub/status` gains a `live_quota` block per tier**: `{quota_source: live|headers|estimated, quota_updated_at, remaining_pct?, reset_in?, note}` — additive to today's payload, never removing existing fields (the PWA panel and anything else reading it keeps working).
- **PWA panel**: renders the live numbers with "as of HH:MM" and a `live`/`headers`/`estimated` badge per tier; AGY tier shows per-family rows (the screenshot's three rows, essentially).

## 7. Success metrics

- **Coverage:** ≥3 of 6 active tiers report better-than-estimated quota (OpenRouter live, Groq headers, AGY per-family; AI Studio/Requesty/NVIDIA show honest `estimated` + whatever harvested accounting exists).
- **Freshness:** 95% of `quota_updated_at` values < 20 min old during normal operation.
- **Zero chat latency impact:** p99 `/chat` latency unchanged vs. today (quota is cache-read only; verified by the existing health/latency checks).
- **Honesty:** zero instances of a stale value displayed without its "as of" timestamp; any tier whose live signal fails shows `estimated`, never a frozen "live" number.
- **Cost:** $0.00/month incremental (no paid APIs; poll traffic within free rate limits).

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| OpenRouter poll eats the 500/day account budget | 10-min cadence = 144/day; single shared poller; budget accounted in config; poller backs off on 429 |
| Header names change silently (Groq/NVIDIA) | Harvest defensively (missing header = keep last value, don't crash); log unknown `x-ratelimit-*` variants for review |
| AGY family error strings change with CLI updates | Markers are config/data, not code; unknown output falls back to today's whole-tier behavior |
| Stale "live" badge misleads Amin | `quota_updated_at` always rendered; staleness threshold (e.g. >30 min) auto-downgrades badge to `estimated` |
| Poller thread crashes take down nothing, but silently stop updates | Poller is supervised: failure logged loudly, `/hub/status` exposes `poller_healthy` so the PWA can show a warning |

## 9. Amin-tap checkpoints (explicit)

**LOCKED 2026-10-21 — Amin's decisions, no longer open:**
1. **Epic order: C → A → B.** AGY families + CLI probe first (the one that looks like his screenshot), then OpenRouter live poll, then header harvesting.
2. **AGY per-family auto-skip: YES.** When one family is dry, Tier 0 auto-prefers the healthy families next time.
3. **Poll/probe cadence: 10 min** for OpenRouter and the CLI probe (header harvesting is continuous/free).

Still informational:
4. **Informational:** AI Studio and NVIDIA have no public quota API — their panel rows stay `estimated` (with 429-driven circuit breakers doing the real protection). Nothing for him to do.
5. Standing rule: implementation lands via branch → PR → merge with his tap.

## 10. Open questions for the architect

- Exact OpenRouter `/api/v1/key` response fields for a free-tier key — verify at build time against the stored key (fields may differ from docs); decide `remaining_pct` derivation (free models: no spend limit → report `limit_remaining` only if present, else requests-today vs the 50/1,000 RPD free cap).
- Where the poller lives: background thread in the FastAPI process vs. a Cloud Run–friendly pattern (note: Cloud Run can scale to zero — the poller only runs when an instance is warm; is that acceptable, or should the first request after cold start trigger a refresh?).
- AGY family taxonomy: exact model→family mapping from the CLI's model list; marker strings per family from real exhausted-output samples.
- Should harvested header values feed the circuit breaker directly (e.g. Groq `remaining-requests: 0` pre-opens the breaker), or stay display-only in v1? (PM recommendation: display + router hint in v1, breaker integration in v2 — say why if you disagree.)
