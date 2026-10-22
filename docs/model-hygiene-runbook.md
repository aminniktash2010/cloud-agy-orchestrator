# Model Hygiene Runbook — `retired_models`

The hub quarantines dead model ids automatically. It never edits its own
config — when `GET /hub/status` shows a non-empty `retired_models` section,
a human (or a follow-up agent session) fixes `MODEL_HUB_CONFIG`. Five steps.

## 1. Read the entry

```bash
curl -s https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/hub/status \
  | python3 -c "import json,sys; [print(r) for r in json.load(sys.stdin)['retired_models']]"
```

Each entry: `tier`, `model_id`, `first_seen`, `last_seen`, `hits`,
`signal_excerpt` (the provider's own words, truncated), `source`
(`reactive` = a real call 404'd; `proactive` = missing from the provider's
catalog diff).

## 2. Verify a replacement id

- Check the provider's catalog for the current id. For AI Studio:
  `GET https://generativelanguage.googleapis.com/v1beta/models?key=…`
  (Google's 404 message usually names the replacement itself).
- For OpenRouter: `GET https://openrouter.ai/api/v1/models`.
- For Requesty: `GET https://router.requesty.ai/v1/models`.
- If the model serves again (provider un-retired it), no config change is
  needed — the 24h TTL re-probe self-heals; or just wait.

## 3. Edit the tier's `models` list in `MODEL_HUB_CONFIG`

Remove the dead id; insert the replacement first if desired. Example —
aistudio tier:

```json
"models": ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
```

(`gemini-2.5-flash` was retired by Google: 404 "no longer available to new
users". `gemini-3.8-flash` is Google's named replacement.)

## 4. Publish a new secret version

```bash
# fetch current, edit the JSON, then:
echo "$NEW_JSON" | gcloud secrets versions add MODEL_HUB_CONFIG \
  --project=project-51459e90-fbb3-471f-9f9 --data-file=-
```

60s hot-load, independently revertible (each version is kept). No redeploy.

## 5. Confirm the section clears

```bash
curl -s https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app/hub/status \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['retired_models'])"
# → []
```

The hub prunes the entry on the next status build once the id is no longer
referenced by any tier `models` list or `model_catalog` carrier mapping.

## Notes

- **TTL re-probe:** a quarantined id is re-attempted once per 24h. A repeat
  404 refreshes the quarantine; any other outcome clears it immediately.
- **Proactive vs reactive:** `source: proactive` means "absent from the
  catalog". A real successful call always outranks it — the poller never
  clears or downgrades a reactive entry.
- **False positives:** a wrong verdict costs one failed call per day and
  self-heals. If an entry looks wrong, verify the id serves (step 2) and
  either wait out the TTL or remove/re-add the id in config.
- **What the hub will NOT do:** rewrite `MODEL_HUB_CONFIG` itself, add
  newly-seen models to any tier, or quarantine on 429/5xx/auth failures.
