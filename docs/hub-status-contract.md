# `GET /hub/status` payload contract

**Endpoint:** `GET /hub/status` (public-readable; secret-free by contract — AC-10).
**Source:** `ModelHub.status()` → `hub/observability.py::status_payload`.
**Versioning:** additive only. Existing fields are never removed or renamed; new
blocks are added beside them. The PWA renders `live_quota` when present and
falls back to the static fields when absent (backward compatible).

## Top-level shape

```json
{
  "config_version": 1,
  "config_source": "secret-manager | env | default",
  "tiers": [ /* per-tier objects, ladder order, see below */ ],
  "poller": {
    "healthy": true,
    "last_run": "2026-10-21T17:30:00Z | null",
    "providers": {
      "agy_probe":   { "healthy": true, "last_run": "… | null" },
      "openrouter":  { "healthy": true, "last_run": "… | null" }   // Epic A
    }
  },
  "paid":  { "enabled": false, "spend_today_usd": 0.0, "cap_usd": 1.0 },
  "breaker": { "exhaustions_to_open": 3, "window_s": 600, "cooldown_s": 900 }
}
```

`poller.healthy` is the AND over registered providers; `true` when nothing has
reported yet (nothing failing). On Cloud Run scale-to-zero the poller only runs
on warm instances — `last_run` / `quota_updated_at` make that staleness visible
rather than hidden (NFR-5).

## Per-tier object

```json
{
  "id": "agy",
  "order": 0,
  "enabled": true,
  "capability": "agent_loop | answer_only",
  "provider": "antigravity",
  "models": ["gemini-3.8-flash-high", "…"],
  "key_configured": true,
  "circuit": "open | closed",
  "requests_today": 12,
  "daily_request_cap": 1200,          // null = no cap
  "quota_hints": { "x-ratelimit-remaining": "…" },   // last-seen raw hints
  "cost": "free | credits | paid",
  "live_quota": { /* additive block, see below */ }
}
```

All pre-existing fields keep their Epic-1 semantics byte-identically.

## `live_quota` block (additive, live-quota feature)

```json
"live_quota": {
  "quota_source": "live | headers | measured | estimated",
  "quota_updated_at": "2026-10-21T17:30:00Z | null",
  "remaining_pct": 23.0,            // null unless a provider truly reports it
  "reset_in": "5d 6h",               // compact human countdown; null when unknown
  "detail": "Gemini Models 23% · Claude and GPT models exhausted",
  "families": [ /* agy tier only */ ],
  "unmapped_buckets": [ /* agy tier only */ ]
}
```

### `quota_source` semantics (AD-9)

| value       | meaning                                              | writer (per tier)        |
|-------------|------------------------------------------------------|--------------------------|
| `live`      | polled from a provider API or the on-device CLI probe| OpenRouter poll (A), AGY probe (C) |
| `headers`   | harvested from real response headers                 | Groq (B)                 |
| `measured`  | locally accumulated accounting (not remaining quota) | AI Studio tokens, Requesty spend (B) |
| `estimated` | static config cap + local request counter            | default for every tier   |

**Staleness rule (AD-17, NFR-3):** the effective source is computed at read
time — a value older than `quota.staleness_s` (default 1800s) is presented as
`estimated`, with the last live reading's age noted in `detail`
(e.g. `"… (last live reading 42m ago)"`). A stale value can never wear a
`live`/`headers`/`measured` badge, by construction.

### AGY `families[]` rows

```json
"families": [
  {
    "id": "gemini", "label": "Gemini Models",
    "models": ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"],
    "circuit": "closed",
    "remaining_pct": 23.0, "reset_in": "5d 6h",
    "quota_source": "live", "quota_updated_at": "…", "note": ""
  },
  {
    "id": "claude_gpt", "label": "Claude and GPT models",
    "models": ["claude-sonnet-4.6"], "circuit": "open",
    "remaining_pct": 0.0, "reset_in": "5d 12h",
    "quota_source": "live", "quota_updated_at": "…",
    "note": "weekly limit exhausted"
  }
]
```

Tier-level `remaining_pct` = the minimum across families with non-null values
(the binding constraint); `reset_in` = that family's reset. `circuit` comes
from the per-family breaker (`agy:<family>` composite keys). Before the first
probe run, families come from the config seed with `remaining_pct: null` and
`quota_source: "estimated"` (cold start is honest, never blank — NFR-4).

`unmapped_buckets[]` lists quota buckets the probe decoded but could not map
to any family (`{name, remaining_pct, reset_in}`) — visible, never silently
dropped ("check all its models", carried through honestly).

### OpenRouter tier (Epic A)

The tier-level block comes from the key poll (`GET {quota.providers.openrouter.key_url}`,
default `https://openrouter.ai/api/v1/key`, `Authorization: Bearer $OPENROUTER_API_KEY`,
10-min cadence). `remaining_pct` follows AD-19 exactly:

1. Key has numeric `limit` + `limit_remaining` (spend cap) → `100 * limit_remaining / limit`;
   `reset_in` from `limit_reset`.
2. Else the API's own `free_model_daily_requests` counter → `100 * remaining / limit`
   (verified live 2026-10-21: `{used: 0, limit: 50, remaining: 50}` on the stored
   key — `free_rpd_cap` is 50); `reset_in` counts to UTC midnight ("resets daily").
3. Else free tier without the counter → local estimate `100 * (1 - requests_today / free_rpd_cap)`.
4. Else `remaining_pct: null` + an honest detail — never a guessed number.

On 429/402 the poll backs off exponentially (cap 1h) so the check never burns
the budget it measures; other failures keep the last-known value and mark
`poller.providers.openrouter.healthy: false` (AD-17 downgrades the display to
`estimated`).

## Secret-freedom (AC-10)

The payload carries percentages, timestamps, booleans, model names and bucket
names only. No key material, no tokens, no credential-adjacent strings. The
`test_no_secret_leak` suite covers the full payload including `live_quota`.
