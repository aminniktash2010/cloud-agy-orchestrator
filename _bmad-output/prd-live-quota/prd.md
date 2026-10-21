# PRD — Live Quota per Provider for the Model Hub
**Project:** cloud-agy-orchestrator · **Feature:** Live quota/usage per provider
**Author:** 📋 John (BMAD PM) · **Date:** 2026-10-21 · **Status:** Ready for architecture
**Inputs:** `_bmad-output/brief-live-quota/brief.md` · `_bmad-output/architecture-model-hub/architecture.md` · `agent-service/hub/` (live) · `GET /hub/status` (live) · `web-chat-pwa/index.html` (live)

---

## 1. Background & scope

The Model Hub (Epic 1, live) routes across a 7-tier ladder with per-tier circuit breakers and a static `daily_request_cap` per tier. `GET /hub/status` reports `requests_today` from a **local counter** — it knows what the hub sent, not what the provider has left. Amin asked for the real numbers, pointing at his Antigravity usage page (per-family weekly % remaining + refresh countdowns) as the reference UX.

Research (brief §5) classifies each tier: **LIVE API** (OpenRouter), **HEADERS** (Groq), **per-call harvest** (AI Studio `usageMetadata`, Requesty `usage.cost`), **LIVE via on-device CLI probe** (AGY — the `agy` CLI on Cloud Run refreshes quota on startup and exposes `models`/`/usage`; verified against the v1.4.3 binary), **NO SIGNAL** (NVIDIA). This PRD turns that classification into buildable requirements. It is **additive**: no existing `/hub/status` field is removed or renamed, no ladder order changes, no capability-mode changes.

**Amin's locked decisions (2026-10-21):** epic order **C → A → B** (AGY families + CLI probe first), auto-skip dry AGY families = **yes**, poll/probe cadence **10 min**.

## 2. Functional requirements

### FR-1 Live quota poller
- FR-1.1: A background poller (`hub/quota_live.py`, new module) fetches quota from LIVE-API providers on a per-provider cadence (default: OpenRouter every 10 min). It writes `{value, quota_source: "live", quota_updated_at}` into the quota store.
- FR-1.2: The poller **never runs on the chat request path**. `/chat`, `/chat/stream` read the cache only. A chat request must never wait on, or be failed by, a quota fetch.
- FR-1.3: Poller failures are per-provider and silent to users: the tier's `quota_source` downgrades to `"estimated"`, the failure is logged loudly, and `/hub/status` exposes `poller_healthy: false` for that provider.
- FR-1.4: The poller respects each provider's own rate limits on the check endpoint (OpenRouter: ≤144 polls/day of the 500/day account budget; back off on 429 with jitter). Poll cadence is config, not code.
- FR-1.5: A value older than the staleness threshold (default 30 min, config) is **never** presented as `live` — it downgrades to `estimated` automatically.

### FR-2 Header & per-call harvesting (zero extra traffic)
- FR-2.1: The Groq path of `OpenAICompatAdapter` captures `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`, `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-requests`, `x-ratelimit-reset-tokens` from every real response into the quota store as `quota_source: "headers"`. Missing headers are ignored, never an error.
- FR-2.2: `GeminiAdapter` records per-call `usageMetadata` (prompt/eval token counts) into local accounting (feeds the existing `requests_today` and a new `tokens_today` counter). Display stays `estimated` — this is accounting, not remaining-quota.
- FR-2.3: The Requesty path records per-call `usage.cost` into a `spend_today_usd` counter (streaming: requires `stream_options.include_usage`). Display stays `estimated`.
- FR-2.4: Harvesting is defensive: unknown/renamed headers are logged (one log line per new variant) and ignored.

### FR-3 AGY per-model-family state (+ live CLI probe)
- FR-3.1: `signals.py` gains family-aware exhaustion parsing. Families (initial taxonomy; the probe derives the live model list at runtime — architect to confirm the mapping): `gemini` (Gemini Flash/Pro variants — screenshot: "Gemini Models", weekly 23%, five-hour 100%), `claude_gpt` (Claude Sonnet/Opus, GPT-OSS — screenshot: "Claude and GPT models", weekly 0%), `argon` (Gemini Argon-class — screenshot: "Gemini 4 Argon", weekly 0%). The family is inferred from the model name in the failed attempt **and** from family-specific marker strings in CLI error output.
- FR-3.2: The circuit breaker tracks open/closed **per family** within Tier 0 (in addition to the existing whole-tier state). Whole-tier skip only happens when all families are open.
- FR-3.3: The router, on entering Tier 0, tries families in config order, skipping families whose breaker is open. Unknown error output falls back to today's behavior (whole-tier signal), never to a crash.
- FR-3.4: `GET /hub/status` reports Tier 0 as `families: [{id, models[], circuit, remaining_pct, reset_in, note}]` alongside the tier-level fields.
- FR-3.5 (`cli_probe`): A probe component runs **inside the Cloud Run container** against the on-the-fly `agy` CLI on the same 10-min TTL as the poller. Each run: (a) `agy models` (subprocess, 30s timeout) → parse the model list → derive the model→family taxonomy at runtime (no hardcoded model list); (b) rely on the CLI's startup quota refresh (`doRefreshQuota`, free summary RPC — **not** a model call) and read the local quota-summary cache (`retrieveUserQuotaSummaryCache`, protobuf) for per-bucket `quota_remaining`/`quota_resets`; (c) fallback to `agy --print "/usage"` text parsing if the cache is absent or stale. The probe writes per-family `{remaining_pct, reset_in, quota_source: "live", quota_updated_at}` into the quota store.
- FR-3.6 (probe cost guards): the probe **never** sends a prompt — its subprocess argv allowlist is exactly `{agy models, agy --print /usage, agy --print /credits}` (no `-p/--prompt`); a static test asserts this. Probe timeout/non-zero exit → keep the previous value, tier degrades to `estimated`, failure logged loudly. The probe never runs on the chat request path (background thread only, same supervision boundary as the poller).
- FR-3.7 (probe environment guard): the probe runs only when the `agy` binary exists **and** the container is authenticated (OAuth token present, same check `agy-wrapper.sh` uses). Elsewhere (local dev, CI) it no-ops and Tier 0 uses the error-parsing path — tests use recorded fixtures, never a live CLI.

### FR-4 `/hub/status` payload (additive)
- FR-4.1: Each tier gains a `live_quota` object: `{quota_source: "live"|"headers"|"estimated", quota_updated_at: ISO8601|null, remaining_pct: float|null, reset_in: string|null, detail: string|null}`. All existing fields stay byte-identical.
- FR-4.2: `remaining_pct` semantics per provider: OpenRouter = derived from `limit_remaining`/`limit` when the key has a spend cap, else requests-today vs the free-tier RPD cap (50 or 1,000 depending on lifetime purchases — architect verifies which applies); Groq = `remaining-requests / limit-requests` from the latest headers per model (report the min across the tier's models = the binding constraint); AGY = per-family `quota_remaining` from the CLI probe's quota-summary buckets (bucket→family mapping verified at build time; `null` with detail when the probe hasn't run yet); others = `null` with `detail` explaining why (`"no public quota API — estimate shown"`).
- FR-4.3: Top-level `poller: {healthy: bool, last_run: ISO8601|null}` added.
- FR-4.4: The full payload shape is documented in `docs/` (new file, architect's choice of name) and covered by a contract test.

### FR-5 PWA status panel
- FR-5.1: Each tier card shows the quota value with a source badge: `live` (green) / `headers` (blue) / `estimated` (gray), plus "as of HH:MM" from `quota_updated_at`.
- FR-5.2: AGY tier renders per-family rows (mirroring Amin's screenshot: family name, % remaining or circuit state, reset note).
- FR-5.3: A tier whose poller is unhealthy shows a subtle warning affordance, never a blocking error; the panel keeps rendering everything else.
- FR-5.4: No new auth, no secrets displayed (the endpoint returns none; the panel must not invent any).

### FR-6 Configuration
- FR-6.1: Poll cadences, staleness threshold, and the AGY family taxonomy live in `MODEL_HUB_CONFIG` (hot-loaded, same pattern as the ladder) — not in code.
- FR-6.2: A per-provider `quota_enabled` flag allows Amin to disable live polling for any tier (falls back to `estimated`).

### FR-7 Cost guardrails
- FR-7.1: The quota subsystem makes **zero** paid API calls. The only network traffic it adds is the free OpenRouter `/api/v1/key` poll; everything else rides existing responses.
- FR-7.4: If OpenRouter's poll ever returns 429/402, the poller backs off exponentially and the tier downgrades to `estimated` — the check must never burn the budget it's measuring.

## 3. Non-functional requirements

- NFR-1: **Latency:** p99 `/chat` latency unchanged vs. today (quota = cache read; CI measures before/after).
- NFR-2: **Robustness:** poller exceptions never propagate to request handlers (supervision boundary; tested).
- NFR-3: **Honesty:** no value older than the staleness threshold is labeled `live`/`headers` (property test on the downgrade logic).
- NFR-4: **Cold start:** on Cloud Run cold start with an empty cache, every tier reports `estimated` until the first poll/harvest lands — never blank, never fake-live.
- NFR-5: **Scale-to-zero awareness:** Cloud Run may scale to zero; the poller runs only on warm instances. Document this: "as of" timestamps make the staleness visible rather than hidden.
- NFR-6: **Secrets:** the poller uses the existing Secret Manager keys; no new credentials; `no_secret_leak` test extended to the new payload.

## 4. Provider catalog (verified 2026-10-21)

### Tier 0 — AGY · `agent_loop` · $0 · quota_source: `live` (on-device CLI probe; error parsing as fallback)
No public HTTP quota API (CLI v1.4.3 has no usage subcommand; IDE page is private). **Live signal = the `cli_probe` (FR-3.5):** on Cloud Run the CLI is authenticated (OAuth token at `/mnt/cache/antigravity-oauth-token` via `agy-wrapper.sh`), so the probe runs `agy models` for the live model list and reads the CLI's own quota refresh (`doRefreshQuota` → `RetrieveUserQuotaSummaryLEGACY` → `QuotaSummaryBucket{quota_remaining, quota_resets}`, cached locally as `quota_summary.pb`; `/usage` slash command as text fallback). Verified against the v1.4.3 binary 2026-10-21. Fallback: per-family exhaustion parsed from CLI error output (FR-3.1). Families: `gemini` / `claude_gpt` / `argon` (probe derives the live model→family mapping at runtime; architect confirms the taxonomy against the first real probe run).

### Tier 1 — Google AI Studio · `answer_only` · $0 · quota_source: `estimated`
No public quota API for API keys (dashboard-only by Google's design). Harvest per-call `usageMetadata` → local `tokens_today`. 429 remains the exhaustion signal.

### Tier 2 — Groq · `answer_only` · $0 · quota_source: `headers`
Capture per response: `x-ratelimit-limit-requests` (= RPD cap), `x-ratelimit-remaining-requests`, `x-ratelimit-limit-tokens` (= TPM cap), `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-requests`, `x-ratelimit-reset-tokens` (docs: console.groq.com/docs/rate-limits). Binding constraint for our chat use is RPD/TPD per model — report the min remaining across the tier's models.

### Tier 3 — NVIDIA Build · `answer_only` · $0 · quota_source: `estimated`
No documented usage endpoint or rate-limit headers (beyond `Retry-After` on 429). Architect: log all response headers on first integration; reclassify if `x-*` quota headers appear. 429 stays the signal.

### Tier 4 — OpenRouter · `answer_only` · $0 · quota_source: `live`
`GET https://openrouter.ai/api/v1/key`, header `Authorization: Bearer $OPENROUTER_API_KEY`. Returns key metadata incl. `limit`, `limit_remaining`, `limit_reset`, `budgets`, `effective_budget`, free-tier status (exact free-tier field set verified at build time against the stored key; `/auth/key` observed as an alias). Poll every 10 min (144/day of the 500/day account budget). Free-model RPD cap (50, or 1,000 after $10 lifetime purchases) is the binding constraint — derive `remaining_pct` accordingly.

### Tier 5 — Requesty · `answer_only` · $0 · quota_source: `estimated`
No documented public balance API (dashboard-only). Harvest per-response `usage.cost` → `spend_today_usd` (set `stream_options: {include_usage: true}` on streams). Free tier = 200 req/day on free models; 402/429 = exhausted.

### Tier 6 — xAI · disabled · quota_source: n/a
Out of scope. When funded: re-verify for a usage/billing API (none confirmed 2026-10-21); spend-tier rate limits with 429 on exceed are the known signal.

## 5. Epics (locked order C → A → B — smallest shippable first, Amin's call)

### Epic C — AGY per-family state + live CLI probe (shippable; FIRST)
FR-3 in full: `cli_probe` module (FR-3.5–3.7), family taxonomy + markers, per-family breaker, router family ordering with auto-skip of dry families, per-family rows in `/hub/status` and PWA (the closest to Amin's screenshot — "check all its models" from the hub).

### Epic A — OpenRouter live poll + status shape (shippable; SECOND)
Poller module, `live_quota` payload (FR-4), contract test, PWA badges (FR-5.1/5.4). Proves the whole pipeline end-to-end on the one tier with a real HTTP API.

### Epic B — Header harvesting: Groq (+ AI Studio/Requesty accounting) (shippable; THIRD)
FR-2 in full; Groq `remaining_pct` from headers; `tokens_today`/`spend_today_usd` counters; PWA shows them. Zero new network calls.

Epics are independently shippable in C → A → B order; each merges to main behind the standard branch → PR → merge flow.

## 6. Acceptance criteria

- AC-1: For OpenRouter, a test asserts the adapter parses a **recorded fixture** of `GET /api/v1/key` into `live_quota` (no live network in tests).
- AC-2: For Groq, a test asserts a recorded response-header set updates `remaining_pct` and `quota_updated_at`; missing headers leave the previous value untouched.
- AC-3: `/hub/status` contract test: every tier has `live_quota` with `quota_source ∈ {live, headers, estimated}`; a value older than the staleness threshold is never labeled `live`/`headers` (property test with a frozen clock).
- AC-4: AGY: a test feeds recorded exhausted-output samples per family and asserts only that family's breaker opens; Tier 0 is skipped only when all families are open.
- AC-5: Poller failure test: simulated 429 on the OpenRouter poll → tier downgrades to `estimated`, `poller.healthy=false`, no exception reaches handlers.
- AC-6: PWA: panel renders `live`/`headers`/`estimated` badges, "as of" timestamps, and per-family AGY rows from a fixture payload; fetch failure keeps old data (existing behavior, regression-tested).
- AC-7: Load/latency: p99 `/chat` latency within 5% of pre-feature baseline in a smoke comparison.
- AC-8: AGY probe: a test feeds a recorded `agy models` fixture and asserts the correct model→family mapping (no hardcoded list in the assertion — the mapping comes from the fixture).
- AC-9: AGY probe: a test feeds a recorded quota-summary (protobuf bytes or `/usage` text fixture) and asserts per-family `live_quota` with `remaining_pct`/`reset_in`/`quota_source: "live"`; Tier 0's `families[]` rows match.
- AC-10: Probe failure: simulated timeout/non-zero exit from the probe subprocess → tier degrades to `estimated`, `poller.healthy=false` for the probe, chat requests unaffected.
- AC-11: Probe cost guard: a static test asserts the probe's subprocess argv allowlist contains no prompt-sending invocation (no `-p`/`--prompt`/`--print` with a free-form prompt); only `models` and the `/usage`+`/credits` slash-command forms are permitted.

## 7. Amin-tap register (explicit — nothing here happens without him)

**LOCKED 2026-10-21 — decided, recorded for the build:**
1. **Epic order: C → A → B.** AGY families + CLI probe first.
2. **AGY auto-skip: YES.** Dry families are skipped automatically; healthy families are preferred.
3. **Poll/probe cadence: 10 min** (OpenRouter poll + CLI probe; header harvesting is continuous).

Still informational:
4. **Informational:** AI Studio + NVIDIA stay `estimated` (no public API exists — not our choice). 429-driven breakers remain the real protection.
5. Standing rule: branch → PR → merge per epic, his tap each time.

## 8. Open questions (for architect)

1. Exact OpenRouter free-tier key fields at build time (verify against the stored key; docs and observed `/auth/key` shape may differ).
2. Poller hosting on Cloud Run scale-to-zero: background thread on warm instances only, vs. lazy refresh on first request after cold start — recommend one.
3. AGY family taxonomy: the probe derives the live model list from `agy models` at runtime — confirm the model→family mapping rules against the first real probe run on Cloud Run (initial: gemini / claude_gpt / argon per the screenshot's three rows); capture real exhausted-output samples per family for the marker strings (from the next real exhaustion; don't burn quota to produce them).
4. Should Groq `remaining-requests: 0` pre-open the breaker (router skips before attempting), or stay display-only in v1? (PM: display + router hint in v1, breaker integration in v2.)
5. Undocumented-header watch: log-and-ignore policy for new `x-ratelimit-*` variants (FR-2.4) — confirm the log volume is sane.
6. CLI probe parsing: the quota-summary cache is protobuf (`QuotaSummaryResponse`/`QuotaSummaryBucket`) without a published schema — recommend the decode strategy (field-name-guided generic decode vs. capturing a real sample on first Cloud Run run and code-generating the parser; tests use recorded bytes either way). Also confirm the runtime cache path under the container's CLI state dir.
7. Probe scheduling on scale-to-zero: the probe only runs on warm instances — should the first request after a cold start trigger a one-shot probe refresh (bounded, background), or is "estimated until the next 10-min tick" acceptable? (PM: acceptable for v1 — the "as of" timestamp keeps it honest.)

## 9. Out of scope (explicit)

- Reverse-engineering AGY's IDE usage API or Requesty's dashboard session APIs.
- Any paid quota/billing API or new credential.
- xAI tier work (stays disabled; §4 Tier 6 is documentation only).
- Changing ladder order, capability modes, or the failover semantics of Epic 1.
- Sub-minute real-time quota; 5–15 min freshness is the design point.
