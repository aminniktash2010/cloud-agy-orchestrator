# PRD — Model-Pinned Failover (Model Stickiness)

## 1. Background
The Model Hub routes by *provider tier*; each tier serves its own default
model list. Amin selects flagship models (e.g. **Claude Opus 5.5**) in his
local CLI / apps and expects the hub to keep serving *that model* while it
picks the provider. Today a failover from OpenRouter to Requesty silently
changes the model. This epic adds a cross-tier **model pin**.

Current code (verified 2026-10-22):
- `HubRequest` has `requested_model_override` (legacy — pins first Tier-0
  model only) and `skip_tiers`. `/chat` maps `ChatCommand.model`
  (`"gemini-3.8-flash-high"` default; `"auto"` = ladder) to the override.
- `hub_ask` (MCP) takes **no** model parameter.
- Router `_models_for_tier()`: agy honors the override; other tiers walk
  their own `tier.models`. Within a tier, `UNKNOWN_MODEL` → next model;
  `EXHAUSTED`/`TRANSIENT` → next tier.
- `http_outcome` has **no 404 → UNKNOWN_MODEL mapping** (404 falls through to
  body-sniffing, usually `TRANSIENT`).

## 2. Goals
- G-1: A user-selected model is served across the whole ladder; provider
  failover never changes the model silently.
- G-2: Provider exhaustion on the pinned model advances to the next carrier
  transparently (no user interruption, no re-prompt).
- G-3: Unpinned requests behave **exactly** as today.

## 3. Non-goals
- Hub as a model backend for the agy CLI (unchanged: impossible).
- Spending money: `free_only` / `paid_gate` / disabled-xai semantics
  unchanged. A pinned *paid* model is simply not a carrier on free tiers.
- Live catalog-scraping daemons in v1.

## 4. Functional requirements

### FR-1 — `model` parameter on every entry point
- `HubRequest.model: Optional[str]` — free-text or canonical id; `None` =
  unpinned (today's behavior).
- `/chat` + `/chat/stream`: `ChatCommand.model` maps to the pin.
  `"auto"`/`""`/`"default"` → `None`. **Precedence (locked): when the pin
  is set, legacy `requested_model_override` is ignored**; `/chat` stops
  setting the override (kept in the type for backward compat).
- MCP `hub_ask`: new optional `model` string param. The tool description
  must instruct the local agent to pass the user's selected model through
  verbatim (e.g. "claude opus 5.5").

### FR-2 — Canonical registry (config, never hardcoded)
New top-level `model_catalog` block in `MODEL_HUB_CONFIG` (hot-loaded, 60s
TTL, same pattern as tiers/quota):
```json
"model_catalog": {
  "models": {
    "claude-opus-5.5": {
      "aliases": ["claude opus 5.5", "opus 5.5", "anthropic/claude-opus-5.5"],
      "carriers": {
        "openrouter": "anthropic/claude-opus-5.5",
        "requesty": "anthropic/claude-opus-5-5"
      }
    }
  }
}
```
- Canonical id: lowercase, dashes, no vendor prefix in the id itself
  (vendor lives in the alias/carrier entries).
- `carriers` keys are tier ids; values are the exact provider model ids
  (note the verified dot-vs-dash divergence above — the map absorbs it).

### FR-3 — Normalization
At router entry, normalize free text: lowercase, trim, collapse
whitespace/separators (`"Claude Opus 5.5"` → `claude-opus-5.5`); strip a
leading `vendor/` prefix; match canonical ids then aliases.
- **No match → structured error, never a silent guess.** Error names the
  input and lists closest matches (prefix/edit-distance suggestions from
  the catalog). Surfaced as: `/chat` → 400-style hub error payload;
  `hub_ask` → JSON-RPC `-32602` with the suggestions.
- Empty/whitespace → treated as unpinned.

### FR-4 — Pinned routing
For a pinned request the router:
1. Resolves the canonical id (FR-3).
2. Builds the carrier list = tiers (ladder order) with a `carriers` entry
   for the canonical id, **minus** tiers excluded by existing machinery
   (`skip_tiers`, disabled, `circuit_open`, `missing_key`, `cap_reached`,
   `free_only` policy vs the mapped id, `paid_gate`).
   - Tier 0 (`agy`) participates **only** if the catalog maps an agy model
     for the canonical id; otherwise recorded as
     `skipped: model_pinned_non_agy` (honest: the CLI cannot serve Claude).
3. Attempts each carrier with **only** its mapped provider model id — the
   intra-tier multi-model walk is suspended under pinning.
4. Capability check **before** attempting: no mapping ⇒ no API call is ever
   burned on that provider (record `skipped: model_not_carried`).
5. On `EXHAUSTED` / `402` / quota → next carrier. On `RETRYABLE_429` the
   existing single-delayed-retry applies, then advance. Per-model RPD caps
   (`model_limits_rpd`) still apply per mapped id.
6. On provider 404 / unknown-model-shape for a *mapped* id → the mapping is
   stale: record `stale_mapping`, advance to next carrier, and flag it in
   the status payload so the operator updates the config (60s hot-load, no
   redeploy). Under pinning this must **not** fall through to another model
   in the tier.

### FR-5 — No silent substitution (locked policy)
- Same model + different provider failover = automatic, transparent.
- A **different model** than requested is **never** served silently.
- **No-carrier recommendation (locked):** when no carrier can serve the
  pinned model, return a structured `model_unavailable` result — **not**
  `ladder_empty` — containing: the canonical id, each carrier tried with
  its outcome, and the reason each non-carrier tier was excluded. `/chat`
  surfaces it as an error payload; `hub_ask` as a JSON-RPC error
  (`-32603`-class with a `model_unavailable` code in `data`).
- Opt-in escape hatch: `allow_model_fallback: bool = False` on
  `HubRequest` / `ChatCommand` / `hub_ask`. When `true` and no carrier
  serves, the router falls back to the normal unpinned ladder walk and the
  result sets `model_substituted: true` + `requested_model: <canonical>`
  prominently. **Default off** — Amin's core demand is stickiness, and a
  silent downgrade would break the promise this epic makes.

### FR-6 — Honesty in every response
- `HubResult` gains `pinned_model` (canonical id or `None`); existing
  `model` = the actual provider model id served; `tier_used` as today.
- `failover_attempts` records every carrier attempt with the mapped model
  id and distinct skip reasons (`model_not_carried`,
  `model_pinned_non_agy`, `stale_mapping`).
- Streaming: `tier_switch` events carry `pinned_model`; the serving line /
  advisory banner names the pinned model.

### FR-7 — Staleness / graceful degradation
- v1: operator-curated static map + the FR-4(6) stale-mapping signal. The
  architect decides whether to add a periodic catalog-refresh poller;
  v1 must degrade gracefully without one.
- Config validation at load: carrier tier ids must exist; unknown tier ids
  log a warning and are ignored (fail-soft, never crash the ladder).

## 5. Real capability matrix (verified live 2026-10-22)
Seed the catalog with at least these (operator extends):

| Canonical | OpenRouter | Requesty | Groq | NVIDIA | AI Studio | agy |
|---|---|---|---|---|---|---|
| `claude-opus-5.5` | ✅ `anthropic/claude-opus-5.5` | ✅ `anthropic/claude-opus-5-5` | ❌ | ❌ | ❌ | ❌ |
| `gpt-oss-120b` (example open model) | n/a | n/a | ✅ (unverified live — sandbox egress 403s to api.groq.com; **architect/dev must verify from Cloud Run**) | ❌ | ❌ | ❌ |

Verification notes: OpenRouter `/api/v1/models` (468 models, public);
Requesty `router.requesty.ai/v1/models` (805 models, public);
NVIDIA `/v1/models` (83 models, open-weights only — no Claude/GPT/Gemini
flagships); AI Studio = Google models only (public docs); Groq catalog
**could not be verified** from the sandbox.

## 6. Interaction with existing machinery (locked)
- `skip_tiers` (MCP default `["agy"]`) composes orthogonally: carriers minus
  skipped tiers.
- Per-family breakers (agy) unchanged; only relevant when tier 0 carries
  the pinned model.
- `free_only` (openrouter): enforced as a carrier-exclusion *before*
  attempt — a pinned paid model never triggers a spend attempt there.
- `MODEL_HUB_FORCE_EXHAUST_TIER` staging hook keeps working (AC-1 uses it).

## 7. Acceptance criteria
- **AC-1 (core):** pin `claude-opus-5.5` via `hub_ask`; force-exhaust
  openrouter mid-ladder; assert success with `pinned_model ==
  "claude-opus-5.5"`, `model == "anthropic/claude-opus-5-5"`,
  `tier_used == "requesty"`, and `failover_attempts` showing the openrouter
  exhaustion then the requesty success — no user-visible error.
- **AC-2:** unknown model name (`"clod opus 99"`) → structured error with
  closest-match suggestions; zero provider calls made.
- **AC-3:** pin a model whose only carrier is the disabled xai tier →
  `model_unavailable` naming the canonical id, tried carriers, and
  exclusion reasons.
- **AC-4 (regression):** unpinned `HubRequest` → byte-identical routing
  behavior to today; full existing suite green.
- **AC-5:** `hub_ask(model="claude opus 5.5")` with default `skip_tiers`
  skips agy (`model_pinned_non_agy`) and serves via carriers.
- **AC-6:** pinned paid model + openrouter `free_only` → tier excluded
  pre-attempt; no spend, no error from that tier.
- **AC-7:** `allow_model_fallback=true` with no carrier → normal ladder
  serves a different model with `model_substituted: true` explicit.
- **AC-8:** stale mapping (provider 404s a mapped id) → `stale_mapping`
  recorded, next carrier tried, status payload flags it.

## 8. NFRs
- Zero new paid spend paths (static analysis or test asserting no carrier
  attempt on `free_only`/`paid_gate` tiers for paid pins).
- No secret material in the catalog (model ids only).
- Normalization + carrier filtering add <5ms per request (in-process,
  config-cached).

## 9. Explicitly unverified — architect/dev must confirm
1. Groq's live model catalog (sandbox egress blocked; verify from Cloud
   Run or provider docs) before seeding any Groq carrier mappings.
2. Requesty model-id stability (dash-vs-dot observed 2026-10-22; re-check
   at build time).
3. Whether any OpenRouter `:free` variant exists for flagship models
   (expected no; determines whether flagships are ever carriers there).
