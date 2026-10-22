# Architecture — Model-Pinned Failover (Model Stickiness)
**Project:** cloud-agy-orchestrator · **Feature:** model pin — one user-selected model served across every ladder rung, provider failover without model substitution
**Author:** 🏗️ Winston (BMAD architect) · **Date:** 2026-10-22 · **Status:** Ready for dev
**Inputs:** `_bmad-output/brief-model-pinning/brief.md` · `_bmad-output/prd-model-pinning/prd.md` · `_bmad-output/architecture-model-hub/architecture.md` (AD-1…AD-8) · `_bmad-output/architecture-live-quota/architecture.md` (AD-9…AD-19) · `_bmad-output/architecture-hub-mcp-server/architecture.md` (AD-20…AD-27) · `agent-service/hub/types.py`, `router.py`, `config.py`, `signals.py`, `adapters/openai_compat_adapter.py`, `main.py` (live, read) · live `MODEL_HUB_CONFIG` secret (read) · `web-chat-pwa/index.html` (live, read)

**Epic:** P1 (single epic, shippable in one PR). This doc continues the decision numbering at **AD-28**.

---

## 1. Design paradigm

**The pin narrows the ladder; it never rewrites it.** A pinned request walks the *same* tier order, through the *same* skip/exhaustion machinery, but each tier is asked exactly one question — "can you serve this model id?" — and tiers that can't are skipped without burning a call. The unpinned path is not refactored, not reordered, not touched: pin resolution happens once at request entry, returns `None`, and the router proceeds exactly as today. That is what makes AC-4 (byte-identical unpinned behavior) a structural property rather than a test hope.

Corollaries:
- The catalog is **operator data, not code**: canonical ids and provider mappings live in the hot-loaded `MODEL_HUB_CONFIG`, validated fail-soft at load (AD-28). A bad mapping degrades to a structured error, never a crash.
- **No silent substitution is a router invariant, not a policy note**: under a pin, the intra-tier multi-model walk is suspended — there is literally no code path that can serve a different model id than the mapped one (AD-31).
- Staleness is **signaled, not polled**: a provider 404 on a mapped id is recorded and surfaced in `/hub/status`; the operator fixes the mapping in the secret and the 60s hot-load picks it up. No scraping daemon in v1 (AD-38).

---

## 2. Architecture decisions (AD-28…)

### AD-28 — `model_catalog` lives in `MODEL_HUB_CONFIG`; validation is fail-soft [ADOPTED]
New top-level block, hot-loaded with the existing 60s TTL. Schema (§3). `ConfigLoader` validates at load: carrier tier ids must exist in `tiers` (unknown → warning log + carrier dropped); empty provider model ids → warning + carrier dropped; a model left with zero carriers stays resolvable but yields `no_carrier`. A malformed block never crashes the ladder — worst case, pins error structurally.
*Binds:* FR-2, FR-7. *Prevents:* a typo'd mapping taking down unpinned traffic (validation runs for all requests, but only affects the pinned path).

### AD-29 — The pin is opt-in and strict; "auto" (and friends) means today's ladder [ADOPTED]
`HubRequest.model: Optional[str]`; `None`/empty/whitespace = unpinned → the router's existing path runs untouched. `/chat` maps `ChatCommand.model` with the same sentinel rule as today (`"auto"`/`""`/`"default"` → `None`). This resolves the default-value question the PM left open: the PWA already sends `"auto"` by default (verified live in `web-chat-pwa/index.html` — the dropdown's selected option), so the default path is unpinned with **zero PWA changes**. An explicitly selected model (PWA dropdown's named options, or `hub_ask(model=...)`) becomes a strict pin — which is exactly the semantic change Amin asked for: named selections stop being tier-0 preferences and become cross-tier promises.
*Binds:* FR-1, G-3. *Prevents:* the default path silently narrowing to 2 tiers (the failure mode if the old `"gemini-3.8-flash-high"` API default were treated as a pin).
*Rejected:* repurposing the legacy `requested_model_override` for pins — its tier-0-only semantics would leak into the pin path. It stays in the type for backward compat, `main.py` stops setting it, and the router ignores it whenever `model` is set (precedence locked per PRD).

### AD-30 — Normalization is deterministic; no match is a structured error, never a guess [ADOPTED]
`hub/model_catalog.py` (new, pure logic, no I/O): lowercase → trim → strip a single leading `vendor/` segment → `[\s_]+` → `-` → collapse repeats. Dots are **preserved** (`5.5` ≠ `5-5`); the dash/dot divergence is absorbed by aliases, not by fuzzy rules. Match order: canonical id → normalized alias. No match → `PinError(no_match)` carrying up to 3 suggestions (`difflib.get_close_matches` over canonical ids + aliases, cutoff 0.6, plus prefix matches). Zero provider calls are made on this path.
*Binds:* FR-3. *Prevents:* "clod opus 99" becoming somebody's bill.

### AD-31 — Pinned routing: carrier walk, one mapped id per tier, capability checks before any call [ADOPTED]
`_execute_pinned()` (new, beside `execute()`): for each tier in ladder order — `skip_tiers` → `_skip_reason` (disabled/circuit/missing_key/cap/paid, unchanged) → carrier lookup → `free_only` pre-check → single `adapter.run(prompt, provider_model_id, tier, …)`. The intra-tier multi-model walk is suspended: `UNKNOWN_MODEL` under a pin advances to the next carrier (it means the mapped id was rejected — §6). Per-model RPD: `model_limits_rpd` is declared in `TierConfig` but **not enforced anywhere in the router today**; the pinned path keeps that status quo (per-tier `daily_request_cap` still gates pre-attempt). Wiring per-model RPD enforcement is a flagged follow-up, not silently claimed.
*Binds:* FR-4. *Prevents:* burning API calls on providers that can't serve the pin, and any silent model drift mid-ladder.

### AD-32 — Stale mappings are detected on the pinned path only; `http_outcome` is untouched [ADOPTED]
`http_outcome` has no 404 branch today (404 → body-sniff → usually `TRANSIENT`), and adding one would change unpinned routing — violating the AC-4 regression gate. Instead, the pinned branch sniffs the attempt's reason text for a 404 (`httpx.HTTPStatusError` stringifies as `"Client error '404 Not Found' for url …"`): under a pin, a 404 on a catalog-mapped id unambiguously means the mapping is stale → record outcome `stale_mapping`, advance to the next carrier, and flag it in `/hub/status` (AD-37). Same treatment for `UNKNOWN_MODEL` under a pin (the only remaining way a mapped id gets rejected, since `free_only` is pre-checked).
*Binds:* FR-4.6, FR-7. *Prevents:* a renamed provider model id silently killing the pin, and any regression to unpinned 404 handling.
*Rejected:* a global 404→`UNKNOWN_MODEL` mapping in `http_outcome` — correct-looking, but it reroutes unpinned 404s from "next tier" to "next model", breaking byte-identical behavior.

### AD-33 — Tier 0 participates only with a catalog mapping; otherwise `skipped: model_pinned_non_agy` [ADOPTED]
The Antigravity CLI serves its own model namespace (verified: PWA lists `gemini-4-argon-medium`, `claude-sonnet-5-5-high` as CLI options). If the catalog maps an agy model id for the pin, tier 0 walks it with the existing per-family breaker machinery unchanged (single-model family group). Otherwise the tier is recorded as `skipped: model_pinned_non_agy` — honest, distinct from `model_not_carried`, and it composes with MCP's default `skip_tiers=["agy"]` (AC-5).
*Binds:* FR-4.2. *Prevents:* pretending the CLI can serve arbitrary API models.

### AD-34 — Terminal states: `model_unavailable` is distinct from `ladder_empty` [ADOPTED]
`HubResult.status` gains `"model_unavailable"`. `/chat` → HTTP **422** with `{"error": "model_unavailable", "pinned_model", "carriers_tried", "exclusions"}` (422 = well-formed request, semantically unservable; `ladder_empty` keeps its 429). `hub_ask` → JSON-RPC `-32603` with `data.code = "model_unavailable"` and the same payload (PRD's letter). `no_match` is a *client* error instead: `/chat` → 400, `hub_ask` → `-32602` with suggestions.
*Binds:* FR-3, FR-5. *Prevents:* clients mistaking "your model pick can't be served" for "the ladder is down".

### AD-35 — `allow_model_fallback` (default off) is the only sanctioned substitution, and it is loud [ADOPTED]
`HubRequest.allow_model_fallback: bool = False`, plumbed through `ChatCommand` and `hub_ask`. When `true` and no carrier serves, the router runs the normal unpinned walk; the result sets `model_substituted: true`, `requested_model: <canonical>`, `pinned_model: <canonical>` (what was asked), and `model`/`tier_used` = what actually served. If the fallback walk is also empty → `ladder_empty` with the substitution flags preserved. Default off — Amin's core demand is stickiness.
*Binds:* FR-5. *Prevents:* the exact silent downgrade this epic exists to kill.

### AD-36 — Every result names both the pin and the pipe [ADOPTED]
`HubResult` gains `pinned_model` (canonical or `None`), `requested_model` (raw input), `model_substituted: bool`, `model_error: Optional[dict]`. Existing `model` = the **actual provider model id served**; `tier_used` unchanged. `failover_attempts` records the mapped id per attempt with distinct reasons (`skip: model_not_carried`, `skip: model_pinned_non_agy`, `skip: free_only_policy`, `stale_mapping`, `skip: skip_tiers`). Streaming `tier_switch` events carry `pinned_model`; the serving line and advisory banner name it.
*Binds:* FR-6. *Prevents:* "which model actually answered?" becoming unanswerable.

### AD-37 — Stale mappings are flagged in `/hub/status` and auto-clear on config fix [ADOPTED]
`ModelHub._stale_mappings: dict[(canonical, tier_id), first_seen_iso]`, recorded by the router. `status()` exposes `stale_model_mappings: [{canonical_model, tier, provider_model_id, first_seen}]`, filtered against the **current** catalog: entries whose (canonical, tier) no longer maps to the same provider id are dropped — so an operator fixing the secret sees the flag disappear within the 60s hot-load, no restart. `hub_status` inherits it (same builder, AD-23 lineage).
*Binds:* FR-7. *Prevents:* stale flags haunting the dashboard after the fix.

### AD-38 — v1 has no catalog poller; the stale signal + 60s hot-load is the refresh loop [ADOPTED]
The catalog is operator-curated. The PM's poll-vs-static question resolves to static-plus-signal: the cost of a poller (new background task, provider API churn, auth) buys nothing while mappings change rarely and every staleness event is self-reporting via AD-37/AD-32. Revisit if the catalog exceeds ~50 models or mappings churn monthly.
*Rejected:* a quota-poller-style catalog refresh task — machinery without a problem.

---

## 3. Config — `model_catalog` block

```json
"model_catalog": {
  "models": {
    "claude-opus-5.5": {
      "aliases": ["claude opus 5.5", "opus 5.5", "claude-opus-5-5", "anthropic/claude-opus-5.5"],
      "carriers": {
        "openrouter": "anthropic/claude-opus-5.5",
        "requesty": "anthropic/claude-opus-5-5"
      }
    },
    "gpt-oss-120b": {
      "aliases": ["gpt oss 120b"],
      "carriers": { "groq": "openai/gpt-oss-120b" }
    },
    "gemini-3.8-flash-high": {
      "aliases": ["gemini 3.8 flash high"],
      "carriers": { "agy": "gemini-3.8-flash-high", "aistudio": "gemini-3.8-flash" }
    },
    "gemini-3.8-flash-medium": {
      "carriers": { "agy": "gemini-3.8-flash-medium" }
    },
    "gemini-3.8-flash-low": {
      "carriers": { "agy": "gemini-3.8-flash-low" }
    },
    "gemini-4-argon-medium": {
      "carriers": { "agy": "gemini-4-argon-medium" }
    },
    "claude-sonnet-5-5-high": {
      "aliases": ["claude sonnet 5.5 high"],
      "carriers": { "agy": "claude-sonnet-5-5-high" }
    }
  }
}
```

Notes for dev (seed with exactly this, then operator extends):
- `openrouter` maps the paid flagship id, but the tier is `free_only` — so it is **excluded pre-attempt** (AD-31); the entry stays because the exclusion is policy-driven and reversible (see §13: what Amin flips to enable it).
- `gpt-oss-120b → groq` cites the live groq tier's own `models` list as its source (operator-asserted, not guessed); the public Groq catalog remains unverified — dev must verify from Cloud Run before adding any further groq carriers (§13).
- The `aistudio` mapping for `gemini-3.8-flash-high` is the operator's best match (`gemini-3.8-flash` is the closest id in the live aistudio tier); medium/low map agy-only until the operator confirms better API ids. Seed entries may carry `"_comment"` keys (tolerated by `extra="ignore"`).
- Canonical ids: lowercase, dashes, no vendor prefix; dots preserved (`5.5` ≠ `5-5` — the Requesty dash divergence is exactly what the map absorbs).

Pydantic (in `types.py`, beside `TierConfig`):
```python
class ModelCarrierMap(BaseModel):
    model_config = ConfigDict(extra="ignore")
    aliases: list[str] = Field(default_factory=list)
    carriers: dict[str, str] = Field(default_factory=dict)  # tier_id -> provider model id

class ModelCatalogConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    models: dict[str, ModelCarrierMap] = Field(default_factory=dict)
```
`HubConfig` gains `model_catalog: ModelCatalogConfig = Field(default_factory=ModelCatalogConfig)` — additive; secrets without the block load an empty catalog (every pin then resolves to `no_match`, unpinned traffic unaffected).

**Load-time validation** (`ConfigLoader._load`, after parse): for each model → for each carrier: drop + `logger.warning` when the tier id is unknown or the provider id is empty/blank. Never raise. Rationale: the catalog is the most human-edited block in the secret; fail-soft keeps the ladder alive through typos (AD-28).

---

## 4. Normalization & resolution — `hub/model_catalog.py` (new)

Pure-logic module, no I/O, no config imports (takes the validated `ModelCatalogConfig`). Cheap enough to build per request (<1ms for catalogs under a few hundred entries; the 60s config cache bounds reload cost anyway).

```python
def normalize_model_name(text: str) -> str:
    t = text.strip().lower().split("/")[-1]      # strip one vendor/ prefix
    t = re.sub(r"[\s_]+", "-", t)                # separators -> dashes (dots kept)
    t = re.sub(r"-{2,}", "-", t)
    return t.strip("-")

class ModelCatalog:
    def __init__(self, cfg: ModelCatalogConfig): ...
    def resolve(self, raw: str) -> tuple[str, ModelCarrierMap] | None
        # exact canonical -> normalized alias -> None
    def suggest(self, raw: str, n: int = 3) -> list[str]
        # prefix matches first, then difflib.get_close_matches(cutoff=0.6)
        # over canonical ids + aliases
```

Resolution result feeds `_resolve_pin` in the router (§6). The module is unit-tested in isolation (normalization table, alias match, suggestion quality) — no router needed.

---

## 5. Plumbing — entry points

### 5.1 `HubRequest` (additive, `types.py`)
```python
model: Optional[str] = None               # free-text pin; None/"" = unpinned
allow_model_fallback: bool = False        # FR-5 escape hatch
# requested_model_override stays, but main.py stops setting it;
# router ignores it whenever model is set (AD-29 precedence).
```

### 5.2 `/chat` + `/chat/stream` (`main.py`, both `HubRequest(` sites)
```python
hub_request = HubRequest(
    ...
    model=(None if (cmd.model or "auto").strip().lower() in ("auto", "", "default")
           else cmd.model.strip()),
    allow_model_fallback=cmd.allow_model_fallback,
    # requested_model_override=...  REMOVED (legacy retired from the handler)
)
```
`ChatCommand` gains `allow_model_fallback: bool = False`. The `model` field default (`"gemini-3.8-flash-high"`) is unchanged — direct API callers omitting it get today's tier-0-preference behavior via… wait, no: with the override no longer set, omitting `model` entirely now means **unpinned** (previously: tier-0 first-model pin to gemini-3.8-flash-high). Delta analysis: the legacy override only ever re-ordered tier 0's first attempt, and `gemini-3.8-flash-high` is already first in the live agy tier's model list — so for the default value the behavioral delta is nil. Any *explicit* non-default value previously pinned tier-0-first-attempt; now it pins cross-tier (the intended semantic change). This is documented in the PR description as the one deliberate contract shift.

Terminal mapping (both handlers, next to the existing `ladder_empty` → 429):
```python
if result.status == "model_unavailable":
    code = result.model_error["code"]  # "no_match" | "no_carrier"
    raise HTTPException(status_code=400 if code == "no_match" else 422, detail={
        "error": "model_unavailable", **result.model_error,
        "attempts": [a.model_dump() for a in result.failover_attempts]})
```
Streaming: the `done` event payload is `result.model_dump()` — new fields flow automatically; no SSE shape change.

### 5.3 MCP `hub_ask` (`hub/mcp_server.py`)
New optional `model: str` argument. Tool description appends: *"If the user has selected a specific model (e.g. 'claude opus 5.5'), pass it verbatim in `model` — the hub will serve that model across every provider that carries it, failing over providers transparently. Omit for the default ladder."* New optional `allow_model_fallback: bool = false` (documented as loud-substitution opt-in). Error mapping: `PinError(no_match)` → `-32602` with suggestions; `model_unavailable(no_carrier)` → `-32603` with `data = {"code": "model_unavailable", ...}`.

---

## 6. Router — the pinned walk (`hub/router.py`)

### 6.1 Entry: `_resolve_pin`
At the top of `execute()` and `execute_stream()`, after `self._config()`:
```python
pin = self._resolve_pin(request, cfg)   # None | Pin(canonical, carriers) | PinError
if isinstance(pin, PinError):
    return self._pin_error_result(pin, request)        # collect path
    # stream path: yield HubEvent(kind="error", payload={...}); return
if pin is not None:
    return await self._execute_pinned(request, pin, cfg, prompt, truncated, attempts)
# ... existing ladder, byte-identical ...
```
`_resolve_pin`: blank/None → `None` (catalog never consulted — this is what the AC-4 differential test spies on). Else `ModelCatalog(cfg.model_catalog).resolve(raw)` → `Pin` or `PinError("no_match", suggestions)`.

### 6.2 `_execute_pinned` (collect) — minimal-diff shape
Mirrors the existing tier loop; per tier, in order:
1. `skip_tiers` → `record(skipped, "skip: skip_tiers")`, continue.
2. `provider_id = pin.carriers.get(tier.id)` → None → `record(skipped, "skip: model_pinned_non_agy" if tier.id == "agy" else "skip: model_not_carried")`, continue. **No API call.**
3. `_skip_reason(tier, cfg)` (unchanged) → record, continue.
4. `free_only` pre-check: `tier.free_only and not provider_id.endswith(":free")` → `record(skipped, "skip: free_only_policy")`, continue. (Same predicate as the adapter's `_check_free_only`; the adapter is untouched.)
5. Single attempt: `adapter.run(prompt, provider_id, tier, timeout_s=…, max_output_tokens=…, repo_ctx=…, conversation_id=…, continue_session=…)`.
6. Outcome handling (pinned deltas vs. the existing loop):
   - `SUCCESS` → `_winner` with `pinned_model=pin.canonical`, `requested_model=request.model`.
   - `RETRYABLE_429` → existing single-delayed-retry (`_retry_once`), then advance on failure.
   - `EXHAUSTED` → advance to next carrier.
   - `TRANSIENT` → if `"404"` in reason text: record outcome `stale_mapping` (reason names the mapped id), note in `self._stale_mappings`, advance; else record transient, advance.
   - `UNKNOWN_MODEL` → record `stale_mapping` (under a pin the id came from our catalog — the provider rejected it), advance. (The adapter-level `free_only` rejection can't reach here: pre-checked in step 4.)
   - `AUTH_FAULT` / `BAD_REQUEST` → identical to today (optional→skip tier / raise; repo_ctx-None→skip / raise).
7. No carrier served → `_model_unavailable_result(pin, attempts)`; if `request.allow_model_fallback` → run the **unpinned** walk instead (recursive call with `model=None`) and set `model_substituted=True`, `requested_model`, `pinned_model` on whatever it returns (including `ladder_empty`).

`_models_for_tier` and `_family_models` are **not called** on the pinned path — except: when the catalog maps an agy id, tier 0 still goes through `_family_models` with `models=[provider_id]` so per-family breakers keep working (single-model group; AD-15 untouched).

### 6.3 `_execute_pinned_stream`
Same walk as §6.2, yielding events. Deltas: `tier_switch` payload gains `"pinned_model": pin.canonical`; the `UNKNOWN_MODEL` intra-tier "switching to X (same tier)" status message is **suppressed** under pinning (there is no next model); `BAD_REQUEST`/`AUTH_FAULT` terminal yields mirror the collect path. The terminal `done` event carries the full `HubResult` dump.

### 6.4 Terminal result builders
```python
def _pin_error_result(self, err: PinError, request) -> HubResult  # no_match, zero attempts
def _model_unavailable_result(self, pin: Pin, attempts) -> HubResult
# status="model_unavailable",
# model_error={"code": "no_match"|"no_carrier", "message": ..., "pinned_model": ...,
#              "suggestions": [...],            # no_match only
#              "carriers_tried": [{tier, provider_model_id, outcome, reason}...],
#              "exclusions": [{tier, reason}...] }   # no_carrier only
```

---

## 7. Observability honesty

- `failover_attempts`: every carrier attempt records the **mapped provider id** in `ModelAttempt.model`; skips use the distinct reasons from §6.2. A reader can reconstruct exactly which provider ids were tried for the pin.
- `serving_line` / advisory banner: when `pinned_model` is set, the serving line names it (`serving pinned claude-opus-5.5 via requesty`), and the answer-only advisory banner names the pin rather than just the provider default.
- `/hub/status` gains `stale_model_mappings` (AD-37). No other status shape changes; `hub_status` inherits it.

---

## 8. Module layout & integration

```text
agent-service/
  main.py                 # EXTEND: ChatCommand.allow_model_fallback; both HubRequest sites
                          #   (model mapping, drop requested_model_override); 400/422 mapping
                          #   for model_unavailable next to the ladder_empty 429
  hub/
    types.py              # EXTEND: HubRequest.model, .allow_model_fallback;
                          #   HubResult.pinned_model/.requested_model/.model_substituted/
                          #   .model_error; status "model_unavailable"; ModelCatalogConfig types
    model_catalog.py      # NEW: normalize/resolve/suggest (pure logic)
    router.py             # EXTEND: _resolve_pin, _execute_pinned (+_stream),
                          #   _pin_error_result, _model_unavailable_result,
                          #   self._stale_mappings; UNPINNED PATH UNTOUCHED
    mcp_server.py         # EXTEND: hub_ask model/allow_model_fallback params,
                          #   tool description, -32602/-32603 mappings
    config.py             # EXTEND: catalog validation in _load (fail-soft)
  tests/
    test_model_pinning.py # NEW: AC-1…AC-8 + differential regression test
    test_model_catalog.py # NEW: normalization/resolution/suggestion tables
```

PWA: **no changes**. It already sends `"auto"` by default (unpinned) and explicit names on selection — which now become true pins, the intended semantic change. Follow-up (not this epic): add flagship pins (e.g. `claude-opus-5.5`) to the PWA model dropdown.

---

## 9. Test strategy (maps to PRD AC-1…AC-8)

| AC | Test |
|---|---|
| AC-1 | `hub_ask(model="claude opus 5.5")` with `MODEL_HUB_FORCE_EXHAUST_TIER=openrouter`: stub requesty adapter success → assert `pinned_model == "claude-opus-5.5"`, `model == "anthropic/claude-opus-5-5"`, `tier_used == "requesty"`, attempts show openrouter exhaustion then requesty success |
| AC-2 | `model="clod opus 99"` → structured no_match with suggestions; spy asserts **zero** adapter invocations |
| AC-3 | pin resolving only to disabled-xai carrier → `model_unavailable`/`no_carrier` naming canonical id, tried carriers, exclusion reasons |
| AC-4 | **Differential regression**: fixed unpinned scenario (stubbed adapters) run against the pinned-branch entry — assert `ModelCatalog.resolve` never called and attempt sequence equals the pre-change recording; plus the full existing suite green |
| AC-5 | `hub_ask(model="claude opus 5.5")` default `skip_tiers` → agy recorded `skip: model_pinned_non_agy`, served via carriers |
| AC-6 | pinned paid model vs openrouter `free_only` → `skip: free_only_policy` pre-attempt; assert no HTTP call made (transport spy) |
| AC-7 | `allow_model_fallback=true`, no carrier → unpinned ladder serves; `model_substituted is true`, `requested_model`/`pinned_model` set |
| AC-8 | stubbed provider 404 on a mapped id → `stale_mapping` recorded, next carrier tried, `stale_model_mappings` appears in `status()`; then fix the mapping in a reloaded config → flag disappears |
| — | Normalization table: `"Claude Opus 5.5"` → `claude-opus-5.5`; `"anthropic/claude-opus-5.5"` → same; `"  OPUS 5.5  "` → same; dots preserved (`5.5` ≠ `5-5`) |
| — | Config validation: unknown carrier tier id → warning + dropped; empty provider id → dropped; ladder still serves unpinned traffic |
| — | Streaming: `tier_switch` carries `pinned_model`; no intra-tier switch message under pin |

---

## 10. Dev-agent handoff — Epic P1 stories

**Epic P1 — Model-pinned failover** (single epic, one PR):
- **P1-S1**: `types.py` additions + `hub/model_catalog.py` (normalize/resolve/suggest) + `config.py` fail-soft validation + `test_model_catalog.py`. No router changes yet.
- **P1-S2**: router `_resolve_pin`, `_execute_pinned` (collect), terminal builders, `HubResult` fields, `_stale_mappings`. AC-1/2/3/6/8 (collect).
- **P1-S3**: `_execute_pinned_stream`, `tier_switch` payload, suppressed intra-tier message. AC-1 (stream variant), banner/serving-line.
- **P1-S4**: `main.py` plumbing (both handlers, 400/422), `ChatCommand.allow_model_fallback`; `mcp_server.py` `model` param + descriptions + error mappings. AC-5/7.
- **P1-S5**: `status()` `stale_model_mappings` + auto-clear; AC-8 status assertions.
- **P1-S6**: AC-4 differential regression test; full suite green; seed `model_catalog` into the `MODEL_HUB_CONFIG` secret (new version — **config-only, no redeploy**); PR description documents the §5.2 contract shift.

Suggested branch: `bmad/model-pinning` from `origin/main`.

---

## 11. Deployment & rollback

**Deploy:** code ships via the normal PR → merge → `deploy-agent-infra.yml`. The seed catalog goes into `MODEL_HUB_CONFIG` as a **new secret version** — picked up by the 60s hot-load with no redeploy, and independently revertable. Order doesn't matter (empty catalog = pins error cleanly; code without catalog = same).

**Smoke:** `hub_ask(model="claude opus 5.5")` → expect `model_unavailable`/`no_carrier` naming requesty as the sole carrier pre-exhaustion… then a real ask → `tier_used: requesty`, `model: anthropic/claude-opus-5-5` (spends Requesty credits — Amin's call).

**Rollback:** previous Cloud Run revision (<2 min) + revert the secret version. The laptop CLI is unaffected (it just sees the tools).

---

## 12. PM open items — resolved

1. **Groq catalog verification.** The sandbox cannot reach `api.groq.com` (egress 403s — verified artifact, not a key verdict). Resolution: v1 seeds **only** model ids already present in the live groq tier's `models` list (`openai/gpt-oss-120b` — operator-asserted in the running config, not guessed). Dev verifies any *additional* groq carriers from Cloud Run (where egress works) or provider docs before adding them; the catalog's fail-soft validation + `stale_mapping` signal make a wrong entry self-reporting rather than fatal.
2. **Requesty paid-gate vs paid pins.** Resolved from the live secret: the requesty tier has **no** `paid_gate` (`cost: "credits"`, enabled) — `paid.enabled=false` does not block it. A paid pin like `claude-opus-5.5` **is servable today via Requesty**, spending Amin's Requesty credits. Honest caveat recorded: Requesty's credit balance is not hard-gated by the hub (per-call `usage.cost` accounting is Epic B, unbuilt) — so the flagship pin currently has exactly **one** carrier, and a dead Requesty balance yields `model_unavailable`, not a silent fallback. The `model_unavailable` payload makes this visible (carrier list of one).
3. **OpenRouter `:free` variants of flagships.** Expected none; the design doesn't depend on it — `free_only` is a pre-attempt exclusion either way. **What Amin flips to make OpenRouter a flagship carrier** (config-only, no code, hot-loaded): (a) add credits to his OpenRouter account; (b) add a new tier `openrouter-paid` (`base_url` same, `free_only` absent, `paid_gate: true`, `cost: "paid"`, order after `requesty`) — keeping the existing free tier untouched; (c) set `paid.enabled: true` with a `daily_spend_cap_usd` he's comfortable with. The `SpendLedger` then caps spend per the existing AD-7 triple-gate. Until then, the catalog entry documents the mapping and the router excludes it with `skip: free_only_policy` — no spend, ever.

---

## 13. Risks

| Risk | Status after this design |
|---|---|
| Pinned flagship has a single carrier (requesty) today | Accepted and visible: `model_unavailable` names the single carrier; §12 gives the config runbook for a second carrier |
| Operator typo in a provider model id | Contained: fail-soft validation + `stale_mapping` self-report + status flag + 60s fix loop |
| `requested_model_override` removal surprises a direct API caller | Documented contract shift (§5.2); behavioral delta is nil for the default value (already first in tier 0's list) |
| Normalization false-match (dot vs dash) | Prevented by AD-30: dots preserved, divergence handled by aliases; suggestions surface near-misses instead of guessing |
| `allow_model_fallback` gets left on by a client | Mitigated: default off; when on, `model_substituted: true` is prominent in every response surface |
| Catalog grows stale as providers rename models | Contained: AD-37/AD-38 — every staleness event flags itself in `/hub/status`; no silent rot |
| Scope creep: per-model RPD enforcement, catalog poller, PWA picker | Blocked: noted as follow-ups, not in this epic |

---

*Next: BMAD dev agent implements Epic P1 per §10. PRD acceptance criteria AC-1…AC-8 map to the test rows in §9.*
