#!/usr/bin/env bash
set -euo pipefail

# Configuration Defaults
SERVICE_NAME="cloud-agy-orchestrator"
REGION="${GCP_REGION:-us-central1}"
PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo '')}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Error: GCP Project ID is not configured. Run 'gcloud config set project <PROJECT_ID>' or set GCP_PROJECT_ID."
  exit 1
fi

IMAGE_TAG="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"

echo "=== Deploying ${SERVICE_NAME} to Google Cloud Run ==="
echo "Project:  ${PROJECT_ID}"
echo "Region:   ${REGION}"
echo "Image:    ${IMAGE_TAG}"

# 1. Build and submit image to Google Cloud Build
echo "Building and pushing container..."
gcloud builds submit --tag "${IMAGE_TAG}" .

# 2. Deploy to Cloud Run
echo "Deploying Cloud Run service..."
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE_TAG}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi \
  --cpu 1 \
  --set-env-vars "ENVIRONMENT=production,AGY_AGENT_ENABLED=true"

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --platform managed --region "${REGION}" --format 'value(status.url)')
echo "✓ Successfully deployed! Service URL: ${SERVICE_URL}"
