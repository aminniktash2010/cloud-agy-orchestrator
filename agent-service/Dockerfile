FROM python:3.11-slim

# Ultra-slim OS dependencies (~95MB compressed image total to stay well under 500MB Artifact Registry Free Tier)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl bash ca-certificates zstd xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global user.name "Antigravity Cloud Agent" \
    && git config --global user.email "agent@amptech.cloud" \
    && git config --global protocol.version 2

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_config.json /root/.config/antigravity/mcp_config.json
COPY main.py .

# Point PATH to persistent 5GB Cloud Storage tool cache (/mnt/cache/tools)
ENV TOOLS_DIR="/mnt/cache/tools"
ENV PATH="/mnt/cache/tools/node/bin:/mnt/cache/tools/agy/.antigravity/bin:/mnt/cache/tools/agy/bin:${PATH}"
ENV PORT=8080

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
