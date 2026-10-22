# Brief — Model-Pinned Failover (Model Stickiness)

## Problem
The Model Hub's failover ladder picks the *provider*, and each tier serves its
own default model. If Amin selects a flagship model (his example: **Claude
Opus 5.5**) in his CLI or any app, the hub cannot keep serving that model —
a request that fails over from OpenRouter to Requesty silently changes the
model underneath him. There is no way to say "serve *this model*, pick the
provider yourself."

## Goal (Amin's words, lightly cleaned)
"When the user selects a major model, no matter which rung of the Model Hub
failover ladder is serving, keep all requests under that model. If a provider
like OpenRouter exhausts that model's credit, switch to another provider
transparently without distracting the user."

## What this epic delivers
A `model` pin on every hub entry point (`/chat`, `/chat/stream`, MCP
`hub_ask`). When pinned:
- The router walks the ladder trying **only providers that carry that model**,
  in existing cost-first order, using each provider's own model id.
- Per-provider exhaustion (429 / quota / 402) advances to the next carrier —
  same model, different provider, no user interruption.
- Serving a *different* model than requested is **never silent**.

## Key design points (locked)
1. **Canonical model registry** in the hot-loaded `MODEL_HUB_CONFIG`
   (new top-level `model_catalog` block) — canonical id → aliases →
   per-tier provider model ids. Never hardcoded.
2. **Free-text normalization**: "claude opus 5.5" resolves to a canonical id;
   no match → structured error naming closest matches, never a silent guess.
3. **Capability check before attempting**: tiers without a mapping for the
   pinned model are skipped without burning an API call.
4. **No-carrier behavior**: structured `model_unavailable` error listing what
   was tried. Opt-in `allow_model_fallback` flag (default **off** — Amin's
   core demand is stickiness; silent downgrade breaks the promise).
5. **Tier 0 (agy)**: the Antigravity CLI can only serve Google-family models.
   Pinning applies to API tiers; tier 0 participates only if the pinned model
   maps to an agy model, otherwise it is skipped with a distinct reason.
6. **Unpinned requests behave exactly as today** (regression gate).

## Real capability matrix (verified live 2026-10-22, Amin's example model)

| Provider | Carries claude-opus-5.5? | Provider model id |
|---|---|---|
| OpenRouter | ✅ (468-model catalog) | `anthropic/claude-opus-5.5` |
| Requesty | ✅ (805-model catalog) | `anthropic/claude-opus-5-5` (dash, not dot) |
| Groq | ❌ open-weights only | — |
| NVIDIA | ❌ verified: 83 models, open-weights only | — |
| AI Studio | ❌ Google models only | — |
| agy (tier 0) | ❌ CLI serves Google models only | — |

Note the naming divergence (dot vs dash) — this is exactly why the
canonical → provider-id map exists. Also note the openrouter tier is
`free_only`: a *paid* pinned model is not a carrier there (policy check
before attempt; no spend, ever).

## Non-goals
- The hub becoming a model *backend* for the agy CLI (unchanged: impossible).
- Paid-model spending (xai stays disabled; `paid_gate` untouched).
- Automatic catalog scraping daemons (v1 = operator-curated map + graceful
  degradation on stale mappings; refresh strategy is an architect decision).

## Open questions for the architect
- Exact `HubRequest` field shape and precedence vs legacy
  `requested_model_override` (PRD recommends: pin takes precedence; `/chat`
  maps its `model` param to the pin).
- Catalog refresh strategy (poll vs static + stale-signal).
- Streaming `tier_switch` event payload additions under pinning.
