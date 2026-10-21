# Brief — Model Hub with Priority Failover Chain
**Project:** cloud-agy-orchestrator (Cloud Antigravity Orchestrator)
**Feature:** Model Hub — unified, priority-ordered provider failover
**Author:** John (BMAD PM) · **Date:** 2026-10-21
**Status:** Ready for architecture (next: BMAD architect)

---

## 1. Problem

Amin's cloud orchestrator (`agent-service/main.py`) runs the `agy` (Antigravity) CLI in autopilot mode per task, with a hard-coded failover across three intra-AGY model variants (`gemini-3.8-flash-high/medium/low`). When AGY credits/quota run dry, **every entry point dies together** — the PWA chat, the SSE stream, the Gmail bridge, and the Android app all get a quota-exhausted failure with nowhere else to go. There is no second act.

Amin's ask (2026-10-21): *"lets build a hub that cover all below options to be available inside antigravity with priority level like when agy credits all done first use google ai studio, next grok and..."*

He also supplied a provider shortlist: Google AI Studio, GitHub Models, NVIDIA Build, Groq, OpenRouter free models — plus Grok/xAI (needs his xAI key) after AGY credits. Each was verified before this brief was written (see §5 verification verdicts).

## 2. Goals

1. **Never dead-end a task.** When one tier's credits/quota are exhausted, the hub automatically walks a priority ladder to the next provider — AGY credits → free tiers → cheapest paid — until the task is served or the ladder is genuinely empty.
2. **Serve every entry point from one place.** The hub lives in `agent-service` (the orchestrator tier), so `/chat`, `/chat/stream`, the Gmail Apps Script bridge, the PWA, and the Android app module all inherit failover automatically.
3. **$0.00/month by default.** Free tiers first; nothing paid enabled unless Amin explicitly funds it. This is his first filter and a non-goal-breaker.
4. **Configurable by Amin.** Tier order, provider enable/disable, and per-tier caps are configuration (Secret Manager JSON + env), not code.
5. **Honest observability.** Every task report (Drive markdown, API response, SSE stream) shows which tier/provider served it, which tiers were tried, and why each failover happened.

## 3. Non-Goals

- Moving any app to Play production (standing Amin rule — Internal testing only).
- Any paid subscription or billing account creation on Amin's behalf.
- Microsoft Foundry Models (the paid replacement for retired GitHub Models) — explicitly out of the $0 scope.
- Local/self-hosted inference (Ollama etc.) — Cloud Run's $0 tier doesn't fit GPU inference.
- Replacing the `agy` CLI autopilot as the primary agentic path. Fallback tiers **start** as answer/advisory mode; the agent-loop adapter for API tiers is a later epic.
- Image/video generation tiers — text/code chat completion only for v1.

## 4. Users

- **Amin (primary, sole operator).** Sends prompts from the PWA, Gemini app, Gmail, or Android app; expects the thing to keep working when AGY credits run out; configures the ladder rarely.
- **The orchestrator itself (system user).** `/chat` and `/chat/stream` handlers call one `ModelHub.execute()`; the hub owns routing, retry, and reporting.

## 5. Provider verification verdicts (checked 2026-10-21)

| # | Provider (Amin's list) | Verdict | What we verified |
|---|---|---|---|
| 1 | Google AI Studio | ✅ Verified, with correction | Key signup from AI Studio, no card. Multimodal (text/audio/vision) confirmed. **No published "~1M tokens/day" figure exists** — the binding constraint is per-model RPD: Gemini 2.5 Flash ≈1,500/day, 3.5 Flash-Lite 500/day, 3.8 Flash only 20/day; 250k TPM. Free-tier prompts may be used to train Google's models (privacy caveat). Native function calling. |
| 2 | GitHub Models | ❌ **DEAD — do not build** | Fully retired 2026-07-30 per official Microsoft docs (playground, catalog, inference API, BYOK all gone). Any integration would call a dead endpoint. Successor is Microsoft Foundry Models = paid/Azure = out of scope. |
| 3 | NVIDIA Build | ✅ Verified | `build.nvidia.com`, free developer account (email + phone verification, no card), `nvapi-` key. OpenAI-compatible `https://integrate.api.nvidia.com/v1`. ~40 RPM per key (HTTP 429 on exceed). 80+ open models (DeepSeek V4, Nemotron 3, Llama 4, Qwen 3.5, Kimi K2.5, GLM 5.1, GPT-OSS-120B…). Credit system ambiguous post-Oct-2026 (some accounts report credits removed); hub treats 429 as the signal either way. ToS: prototyping/dev/research only. |
| 4 | Groq | ✅ Verified | `console.groq.com`, `gsk_` key, no card. Free: gpt-oss-120b/20b, qwen3.x-27b — 30 RPM, 1,000 RPD, 8K TPM, 200K TPD per class. Tool calling supported. OpenAI-compatible `api.groq.com/openai/v1`. 429 + `x-ratelimit-*` headers. |
| 5 | OpenRouter | ✅ Verified, with caveats | `:free` model pool (16+ models, roster rotates weekly). 20 RPM; 50 RPD free, 1,000 RPD after $10 lifetime credit purchase. `openrouter.ai/api/v1`, needs `HTTP-Referer`/`X-Title`. Best as last-chance free tier, not primary. |
| 6 | Grok / xAI (Amin named it earlier) | ✅ Verified — **paid tier** | No permanent free tier. New accounts: $25 promo credits (expire 30 days), up to $150–175/mo via data-sharing opt-in (may require prior spend). Then pay-per-token: grok-4.1-fast $0.20 in / $0.50 out per 1M — cheapest paid option. OpenAI-compatible `https://api.x.ai/v1`. **Needs Amin's `XAI_API_KEY` + a funding decision. Disabled by default; sits below all free tiers.** |

## 6. Proposed default priority ladder (cost-first)

1. **Tier 0 — AGY credits** (`agy` CLI autopilot, today's behavior, full agent loop) — intra-agy failover first: `gemini-3.8-flash-high → medium → low` (existing), extendable to other Antigravity-bundled models (`claude-sonnet-4.6`, `claude-opus-4.6`, `gpt-oss-120b`) pending CLI verification. **Note:** gpt-oss-120b removal from Antigravity scheduled 2026-11-02.
2. **Tier 1 — Google AI Studio** (Gemini API; `GEMINI_API_KEY` already in Secret Manager) — order high-RPD models first: `gemini-2.5-flash → gemini-3.5-flash-lite → gemini-3.8-flash`.
3. **Tier 2 — Groq free** — `openai/gpt-oss-120b → qwen/qwen3.8-27b`.
4. **Tier 3 — NVIDIA Build free** — `deepseek-v4-flash → llama-4-maverick → qwen-3.5` (slugs verified at build time).
5. **Tier 4 — OpenRouter `:free` pool** — rotating catch-all.
6. **Tier 5 — Grok/xAI** — `grok-4.1-fast` — **paid; disabled until Amin supplies key + funding tap.**

Capability classes: Tier 0 = `agent_loop` (tools, repo edits, commits — today's autopilot). Tiers 1–5 = `answer_only` at launch (advisory answer, no repo mutation, honestly labeled), promotable to `agent_loop` via a constrained tool-loop adapter in a later epic.

## 7. Success metrics

- **Zero dead-end rate:** ≥99% of `/chat` requests that exhaust AGY quota receive either a served response from a fallback tier or an explicit "ladder empty + why" message (measured via hub logs).
- **Failover latency:** tier switch decision < 2s after exhaustion is detected; streaming endpoint surfaces the switch live.
- **Observability:** 100% of task reports include `tier_used`, `provider`, `model`, and per-attempt failover reasons.
- **Cost:** $0.00/month on default config (no paid tier enabled without Amin's explicit funding tap).
- **No regression:** AGY-primary path behaves exactly as today when credits are healthy (same models, same autopilot permissions, same reports + new hub fields).

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Free-tier limits change silently (they do — Gemini limits changed 3× in 2026) | Hub reads live `x-ratelimit-*` headers where offered; limits are config, not code; PRD requires a limits table re-verified at architecture time. |
| AI Studio free-tier data-use-for-training on Amin's private repo prompts | Flag to Amin as accepted risk; mitigation = keep secrets out of prompts, use non-sensitive routing labels; paid API project explicitly out of scope ($0 rule). |
| GPT-OSS removal from Antigravity 2026-11-02 breaks intra-agy chain entry | Intra-agy list is config; architect verifies `agy --model` names at build time. |
| Fallback tiers can't run the agy agent loop → degraded answers | Honest labeling (`answer_only` mode banner in PWA/SSE); agent-loop adapter scoped as Epic 3. |
| Credential sprawl (5+ API keys) | All keys in Secret Manager, never code/git; reuse existing `grant-sa-roles.sh` pattern for `secretAccessor`. |

## 9. Amin-tap checkpoints (explicit)

1. Create Groq key (console.groq.com, ~2 min, no card) — OR decline and hub skips Tier 2.
2. Create NVIDIA key (build.nvidia.com, email + phone OTP) — OR decline and hub skips Tier 3.
3. Create OpenRouter key (optional, last-chance tier).
4. **xAI decision:** create `XAI_API_KEY`? Choose promo-credits-only vs data-sharing opt-in (+$150/mo credits, prompts train xAI) vs pay-per-token with monthly cap. Tier 5 stays disabled until he answers.
5. **Informational:** GitHub Models is dead — he doesn't need to do anything; the PRD just drops it so he doesn't chase it.
6. **Capability mode:** accept `answer_only` fallback labeling for v1, or require the agent-loop adapter before any fallback ships (scope decision for architect handoff).
7. Standing rule: every implementation lands via branch → PR → merge with his tap. No direct-to-main, nothing pushed by agents.

## 10. Open questions for the architect

- Exact `agy --model` names for the non-Gemini Antigravity models (Claude 4.6 variants); verify against the CLI at build time.
- NVIDIA credit ambiguity: confirm on his console whether new accounts still get 1,000 credits or are unlimited-rate-limited; hub design must be signal-agnostic (429 either way).
- Per-tier daily caps and per-task token budgets: propose defaults, let Amin tune.
- Should the PWA let Amin manually pin/override the tier per prompt, or always auto? (Default: auto, with manual override in a later iteration.)
