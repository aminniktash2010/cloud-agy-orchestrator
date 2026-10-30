"""Self-healing model-hub MCP registration for terminal sessions.

Deploys wipe container-local session dirs, so every session setup re-asserts
the model-hub entry in the session's ~/.gemini/config/mcp_config.json.

Pure helper (AD-59): no env reads, no logging, no network — policy lives in
the caller (build_isolated_env). The only side effect is the file write +
chmod 600.
"""

import json
import os
from pathlib import Path

ENTRY_NAME = "model-hub"


def ensure_hub_mcp_entry(config_path: Path, hub_url: str, token: str) -> bool:
    """Ensure the model-hub MCP entry exists with the current token.

    Merges with existing entries: only ``serverUrl`` and
    ``headers.Authorization`` on the ``model-hub`` entry are managed; every
    other key (notably a user-set ``disabled: true``) and all sibling entries
    are preserved.

    Returns True iff the file was written; False means already correct
    (no mtime/chmod churn).
    """
    config_path = Path(config_path)
    try:
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            parsed = {}
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        # Missing or corrupt -> fail closed toward the known-good entry.
        parsed = {}

    servers = parsed.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    auth = f"Bearer {token}"
    server_url = f"{hub_url}/mcp"
    existing = servers.get(ENTRY_NAME)
    if isinstance(existing, dict):
        entry = dict(existing)
        entry["serverUrl"] = server_url
        headers = entry.get("headers")
        headers = dict(headers) if isinstance(headers, dict) else {}
        headers["Authorization"] = auth
        entry["headers"] = headers
        # 'disabled' and any other unmanaged keys are left exactly as they
        # were — a user-set disabled:true is respected, never reset.
    else:
        # Key order matches what `agy mcp add` itself writes (probe-verified).
        entry = {
            "disabled": False,
            "headers": {"Authorization": auth},
            "serverUrl": server_url,
        }

    new_servers = dict(servers)  # preserve insertion order, no sibling churn
    new_servers[ENTRY_NAME] = entry
    new_data = dict(parsed)
    new_data["mcpServers"] = new_servers

    if new_data == parsed:
        return False

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(new_data, indent=2) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)
    return True
