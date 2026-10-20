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
    && chmod +x /usr/local/bin/agy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_config.json /root/.config/antigravity/mcp_config.json
COPY main.py .

ENV PORT=8080
ENV PATH="/usr/local/bin:${PATH}"

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
