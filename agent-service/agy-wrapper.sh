#!/bin/bash
# ==============================================================================
# Antigravity CLI Autopilot & Session Isolation Wrapper
# Enforces:
# 1. Project-path isolation (never opens at root or /app)
# 2. Per-project isolated session history (never conflicts between projects)
# 3. Direct auto-open into the project's last active conversation session (-c)
# 4. Full autonomous permissions (--dangerously-skip-permissions --mode accept-edits)
# ==============================================================================

set -e

# 1. Ensure working directory is the targeted project (never root, /, or /app)
if [ -n "$AGY_TARGET_REPO" ]; then
    mkdir -p "/workspace/$AGY_TARGET_REPO"
    cd "/workspace/$AGY_TARGET_REPO"
elif [ "$PWD" = "/" ] || [ "$PWD" = "/root" ] || [ "$PWD" = "/app" ]; then
    if [ -d "/workspace/cloud-agy-orchestrator" ]; then
        cd "/workspace/cloud-agy-orchestrator"
    elif [ -d "/workspace" ]; then
        cd "/workspace"
    fi
fi

CURRENT_REPO="$(basename "$PWD")"
SESSION_DIR="$PWD/.agy_session"

# If current repo doesn't have .agy_session yet, check if workspace has it
if [ ! -d "$SESSION_DIR" ] && [ -d "/workspace/$CURRENT_REPO/.agy_session" ]; then
    SESSION_DIR="/workspace/$CURRENT_REPO/.agy_session"
fi

mkdir -p "$SESSION_DIR/.gemini/antigravity-cli/conversations"
mkdir -p "$SESSION_DIR/.gemini/antigravity/conversations"
mkdir -p "$SESSION_DIR/.config" "$SESSION_DIR/.cache"

# 2. Sync OAuth token to isolated session
TOKEN_SRC=""
if [ -f /mnt/cache/antigravity-oauth-token ]; then
    TOKEN_SRC="/mnt/cache/antigravity-oauth-token"
elif [ -f /root/.gemini/antigravity-cli/antigravity-oauth-token ]; then
    TOKEN_SRC="/root/.gemini/antigravity-cli/antigravity-oauth-token"
elif [ -n "$AGY_OAUTH_TOKEN" ]; then
    echo "$AGY_OAUTH_TOKEN" > "$SESSION_DIR/.gemini/antigravity-cli/antigravity-oauth-token"
fi

if [ -n "$TOKEN_SRC" ]; then
    if [ ! -f "$SESSION_DIR/.gemini/antigravity-cli/antigravity-oauth-token" ]; then
        cp "$TOKEN_SRC" "$SESSION_DIR/.gemini/antigravity-cli/antigravity-oauth-token"
    fi
    if [ ! -f /root/.gemini/antigravity-cli/antigravity-oauth-token ]; then
        mkdir -p /root/.gemini/antigravity-cli
        cp "$TOKEN_SRC" /root/.gemini/antigravity-cli/antigravity-oauth-token
    fi
fi

# 3. Ensure pre-accepted ToS state exists so interactive ToS never triggers
STATE_PBTXT='post_onboarding: {\n  completed_steps: POST_ONBOARDING_STEP_TYPE_MANAGER_WELCOME\n  completed_steps: POST_ONBOARDING_STEP_TYPE_USAGE_MODE\n  completed_steps: POST_ONBOARDING_STEP_TYPE_AGENT_CONFIGURATION\n  completed_steps: POST_ONBOARDING_STEP_TYPE_ADD_WORKSPACE\n}\nseen_nuxs: {\n  uids: 24\n  uids: 58\n}\nagent_onboarding_completed: AGENT_ONBOARDING_STATE_COMPLETED\ninstallation_uuid: "011f849c-f100-4b95-8941-4e7a2b604dd8"\n'

if [ ! -f "$SESSION_DIR/.gemini/antigravity/antigravity_state.pbtxt" ]; then
    printf "$STATE_PBTXT" > "$SESSION_DIR/.gemini/antigravity/antigravity_state.pbtxt"
fi
if [ ! -f "$SESSION_DIR/.gemini/antigravity-cli/jetski_state.pbtxt" ]; then
    printf "$STATE_PBTXT" > "$SESSION_DIR/.gemini/antigravity-cli/jetski_state.pbtxt"
fi
if [ ! -f "$SESSION_DIR/.gemini/settings.json" ]; then
    printf '{"ide":{"enabled":true,"hasSeenNudge":true},"security":{"auth":{"selectedType":"oauth-personal"}},"permissions":{"autoApprove":true,"terminal":"auto","files":"auto"}}\n' > "$SESSION_DIR/.gemini/settings.json"
fi

# 4. Set environment to isolated project session
export HOME="$SESSION_DIR"
export XDG_CONFIG_HOME="$SESSION_DIR/.config"
export XDG_CACHE_HOME="$SESSION_DIR/.cache"
export AGY_DANGEROUSLY_SKIP_PERMISSIONS=1
export ANTIGRAVITY_AUTOPILOT=1

# 5. Parse command arguments and inject Autopilot & Full Permissions flags
ARGS=()
HAS_PERMS=0
HAS_MODE=0
HAS_CONTINUE=0
HAS_MODEL=0
HAS_PRINT=0

for arg in "$@"; do
    case "$arg" in
        --dangerously-skip-permissions) HAS_PERMS=1 ;;
        --mode*) HAS_MODE=1 ;;
        -c|--continue|--conversation*) HAS_CONTINUE=1 ;;
        --model*) HAS_MODEL=1 ;;
        -p|--print|--prompt*) HAS_PRINT=1 ;;
    esac
    ARGS+=("$arg")
done

if [ $HAS_PERMS -eq 0 ]; then
    ARGS+=("--dangerously-skip-permissions")
fi
if [ $HAS_MODE -eq 0 ]; then
    ARGS+=("--mode" "accept-edits")
fi
if [ $HAS_MODEL -eq 0 ]; then
    ARGS+=("--model" "gemini-3.8-flash-high")
fi

# 6. Auto-resume last active conversation session if one exists and not explicitly starting fresh
CONVO_CLI_DIR="$SESSION_DIR/.gemini/antigravity-cli/conversations"
if [ "$AGY_FORCE_NEW" = "1" ] || [ "$AGY_CONVERSATION_ID" = "new" ]; then
    HAS_CONTINUE=1
elif [ -n "$AGY_CONVERSATION_ID" ] && [ $HAS_CONTINUE -eq 0 ]; then
    ARGS+=("--conversation" "$AGY_CONVERSATION_ID")
    HAS_CONTINUE=1
elif [ $HAS_CONTINUE -eq 0 ] && [ -d "$CONVO_CLI_DIR" ]; then
    if ls "$CONVO_CLI_DIR"/*.db >/dev/null 2>&1; then
        ARGS+=("-c")
    fi
fi

# 7. Print diagnostic info if in interactive terminal
if [ -t 0 ] && [ $HAS_PRINT -eq 0 ]; then
    echo "=========================================================="
    echo " 🚀 Antigravity CLI Autopilot (Session Isolation Active)"
    echo " 📂 Active Project: $(pwd)"
    echo " 💾 Session Home:   $SESSION_DIR"
    echo " ⚡ Autopilot Mode: accept-edits (Full Tool Permissions)"
    if [ $HAS_CONTINUE -eq 0 ] && [ -d "$CONVO_CLI_DIR" ] && ls "$CONVO_CLI_DIR"/*.db >/dev/null 2>&1; then
        echo " 🔄 Auto-Resuming:  Last Active Conversation Session"
    fi
    echo "=========================================================="
fi

# 8. Execute real binary
if [ -f /usr/local/bin/agy-real ]; then
    exec /usr/local/bin/agy-real "${ARGS[@]}"
elif [ -f /root/.local/bin/agy ]; then
    exec /root/.local/bin/agy "${ARGS[@]}"
else
    echo "[ERROR] Antigravity CLI binary not found at /usr/local/bin/agy-real" >&2
    exit 1
fi
