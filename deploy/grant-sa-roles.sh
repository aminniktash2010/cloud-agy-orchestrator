#!/usr/bin/env bash
# Grants the IAM roles the GitHub Actions WIF service account needs
# to build, push, and deploy cloud-agy-orchestrator via Terraform.
# Run locally with an account that has Owner / Project IAM Admin on the project.
set -euo pipefail

PROJECT_ID="project-51459e90-fbb3-471f-9f9"
SA_EMAIL="play-upload@${PROJECT_ID}.iam.gserviceaccount.com"

ROLES=(
  "roles/secretmanager.admin"        # read secrets + set accessor IAM on them
  "roles/storage.admin"              # create the agy-repo-cache GCS bucket
  "roles/run.admin"                  # create Cloud Run service + public invoker binding
  "roles/iam.serviceAccountUser"     # let Cloud Run run as this service account
  "roles/artifactregistry.writer"    # push container images
)

# Make sure you're logged in with a user that can edit IAM
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q .; then
  echo "No active gcloud account. Running 'gcloud auth login'..."
  gcloud auth login
fi

echo "Active account: $(gcloud auth list --filter=status:ACTIVE --format='value(account)')"
echo "Project:        ${PROJECT_ID}"
echo "Service acct:   ${SA_EMAIL}"
echo

for ROLE in "${ROLES[@]}"; do
  echo "→ Granting ${ROLE}"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet > /dev/null
done

echo
echo "✓ Done. Current roles for ${SA_EMAIL}:"
gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${SA_EMAIL}" \
  --format="value(bindings.role)"
