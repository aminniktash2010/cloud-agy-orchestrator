# Hub MCP Server — Laptop Setup

Connect your **local** `agy` CLI to the Model Hub as an MCP server. When your
local Antigravity credits run dry, the agent can call the hub's `hub_ask` tool
and get answers from the fallback ladder (AI Studio → Groq → NVIDIA →
OpenRouter → Requesty). Your laptop and the Cloud Run service share one
Antigravity account, so the hub skips its own cloud CLI tier automatically —
no double-burn.

> **Security first:** you generate the token yourself, on your own machine.
> It never goes through chat, email, or any agent. It is stored in plaintext
> in your laptop's MCP config — hence step 3.

## Prerequisites

- `agy` CLI installed on your laptop and logged in (`agy` → sign in).
- Access to the GCP console for project `project-51459e90-fbb3-471f-9f9`
  (Secret Manager).

## Step 1 — Generate the token (on your laptop, private)

```bash
openssl rand -hex 32
```

Copy the output. This is your `HUB_MCP_TOKEN`. **Do not paste it into any
chat window, ticket, or log.**

## Step 2 — Create the secret (GCP console, BEFORE merging the PR)

1. Go to **Secret Manager** → **Create secret**.
2. Name: `HUB_MCP_TOKEN`, region/project: `project-51459e90-fbb3-471f-9f9`.
3. Secret value: paste the token from step 1. Create.

The deploy workflow's `terraform apply` reads this secret by name — if it
doesn't exist yet, the apply fails. Create it before merging.

## Step 3 — Register the hub on your laptop

```bash
agy mcp add --header "Authorization: Bearer PASTE_TOKEN_HERE" model-hub https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/mcp
chmod 600 ~/.gemini/config/mcp_config.json
agy mcp list
# → model-hub  http  enabled  https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/mcp
```

What this does:
- Registers a remote (`http`) MCP server named `model-hub` pointing at the
  hub's Streamable-HTTP endpoint (`POST /mcp`).
- Stores the bearer token in `~/.gemini/config/mcp_config.json`
  (`chmod 600` keeps it readable only by you).

## Step 4 — Verify

In any `agy` session, ask the agent something like
*"use the model-hub tools to check hub status"*, or trigger a `hub_ask`
call when your credits are exhausted. The answer always reports which tier
served it (`tier_used`).

Quick server-side check (no token needed for the negative test):

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/mcp
# → 503 = token not configured yet · 401 = token configured, auth required (good)
```

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| `401 Unauthorized` on every MCP call | Token wrong or missing in the laptop config → re-run step 3 with the exact token from step 1 |
| `503` from `/mcp` | `HUB_MCP_TOKEN` not set server-side yet → finish step 2, then redeploy |
| `agy mcp list` shows `disabled` | `agy mcp enable model-hub` |
| Agent never calls the tools | The tools are named `hub_ask` / `hub_status`; ask explicitly, e.g. *"check hub_status on model-hub"* |
| `hub_ask` answers say `tier_used: "agy"` | You passed `skip_tiers: []` explicitly — the default skips the cloud CLI tier; remove the override |

## Token rotation

1. Secret Manager → `HUB_MCP_TOKEN` → **Add new version** (new `openssl rand -hex 32`).
2. Redeploy (or wait for the next deploy) — the container picks up `latest` on revision start.
3. Re-run step 3 on the laptop with the new token.
4. Optionally disable the old version in the console.

## What the hub exposes

| Tool | What it does |
|---|---|
| `hub_ask(question, context?, skip_tiers?)` | Answers via the fallback ladder. Defaults to skipping the `agy` tier (shared credit pool). Answer-only: no repo changes, no tool use. Max 8k question / 32k context. |
| `hub_status()` | Ladder state: tier order, per-tier quota (live/measured/estimated + as-of timestamps), breakers, poller health. Zero secrets. |

## Never do this

- **Never** add the hub's own URL to the Cloud Run container's
  `agent-service/mcp_config.json` (a CI test enforces this — it would create a
  call cycle).
- Never commit the token to git, and never paste it into chat.
