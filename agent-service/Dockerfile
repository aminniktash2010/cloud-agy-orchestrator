FROM python:3.11-slim

# System utilities, Git, Curl, decompression tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl bash ca-certificates zstd xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global user.name "Antigravity Cloud Agent" \
    && git config --global user.email "agent@amptech.cloud" \
    && git config --global protocol.version 2

# Pre-install Node.js 20 LTS into /usr/local for zero-delay startup
RUN curl -fsSL https://nodejs.org/dist/v20.18.0/node-v20.18.0-linux-x64.tar.xz | tar -xJ --strip-components=1 -C /usr/local

# Pre-install Antigravity CLI directly into /usr/local/bin/agy with native execution permissions
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- -d /usr/local/bin \
    && if [ ! -f /usr/local/bin/agy ] && [ -f /root/.local/bin/agy ]; then cp /root/.local/bin/agy /usr/local/bin/agy; fi \
    && mv /usr/local/bin/agy /usr/local/bin/agy-real \
    && chmod +x /usr/local/bin/agy-real

# Install Intelligent Autopilot & Session-Isolation Launcher Wrapper
COPY agy-wrapper.sh /usr/local/bin/agy
RUN chmod +x /usr/local/bin/agy

# Pre-accept Terms of Service & onboarding so agy never gets stuck on interactive ToS
RUN mkdir -p /root/.gemini/antigravity /root/.gemini/antigravity-cli \
    && printf 'post_onboarding: {\n  completed_steps: POST_ONBOARDING_STEP_TYPE_MANAGER_WELCOME\n  completed_steps: POST_ONBOARDING_STEP_TYPE_USAGE_MODE\n  completed_steps: POST_ONBOARDING_STEP_TYPE_AGENT_CONFIGURATION\n  completed_steps: POST_ONBOARDING_STEP_TYPE_ADD_WORKSPACE\n}\nseen_nuxs: {\n  uids: 24\n  uids: 58\n}\nagent_onboarding_completed: AGENT_ONBOARDING_STATE_COMPLETED\ninstallation_uuid: "011f849c-f100-4b95-8941-4e7a2b604dd8"\n' > /root/.gemini/antigravity/antigravity_state.pbtxt \
    && printf 'post_onboarding: {\n  completed_steps: POST_ONBOARDING_STEP_TYPE_MANAGER_WELCOME\n  completed_steps: POST_ONBOARDING_STEP_TYPE_USAGE_MODE\n  completed_steps: POST_ONBOARDING_STEP_TYPE_AGENT_CONFIGURATION\n  completed_steps: POST_ONBOARDING_STEP_TYPE_ADD_WORKSPACE\n}\ninstallation_uuid: "164c21cf-20c9-4451-8c37-de0ae61960f9"\n' > /root/.gemini/antigravity-cli/jetski_state.pbtxt \
    && printf '{"ide":{"enabled":true,"hasSeenNudge":true},"security":{"auth":{"selectedType":"oauth-personal"}},"permissions":{"autoApprove":true,"terminal":"auto","files":"auto"}}\n' > /root/.gemini/settings.json

# Pre-seed captured project session for cloud-agy-orchestrator so active session is immediately available
COPY default_session.tar.gz /tmp/default_session.tar.gz
RUN mkdir -p /workspace/cloud-agy-orchestrator/.agy_session \
    && tar -xzf /tmp/default_session.tar.gz -C /workspace/cloud-agy-orchestrator/.agy_session \
    && rm -f /tmp/default_session.tar.gz

# System-wide auto-cd to workspace so any container entry (interactive, docker exec, sh, bash) never lands in root
RUN printf 'if [ "$PWD" = "/" ] || [ "$PWD" = "/root" ] || [ "$PWD" = "/app" ]; then cd /workspace/cloud-agy-orchestrator 2>/dev/null || true; fi\n' >> /etc/bash.bashrc \
    && printf 'if [ "$PWD" = "/" ] || [ "$PWD" = "/root" ] || [ "$PWD" = "/app" ]; then cd /workspace/cloud-agy-orchestrator 2>/dev/null || true; fi\n' >> /etc/profile \
    && printf '\nif [ "$PWD" = "/" ] || [ "$PWD" = "/root" ] || [ "$PWD" = "/app" ]; then cd /workspace/cloud-agy-orchestrator; fi\nif [ -t 0 ]; then exec /usr/local/bin/agy; fi\n' >> /root/.bashrc \
    && printf '#!/bin/bash\nTARGET="${1:-/workspace/cloud-agy-orchestrator}"\nmkdir -p "$TARGET" && cd "$TARGET"\nexec /usr/local/bin/agy "${@:2}"\n' > /usr/local/bin/open-agy \
    && chmod +x /usr/local/bin/open-agy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_config.json /root/.config/antigravity/mcp_config.json
COPY default_session.tar.gz .
COPY main.py .

# Switch default container working directory directly into the active project (never root)
WORKDIR /workspace/cloud-agy-orchestrator

ENV PORT=8080
ENV PATH="/usr/local/bin:${PATH}"

CMD ["sh", "-c", "exec uvicorn --app-dir /app main:app --host 0.0.0.0 --port ${PORT:-8080}"]
