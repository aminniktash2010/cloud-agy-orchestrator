terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.38.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Ensure required Google Cloud APIs are enabled
locals {
  services = [
    "secretmanager.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com"
  ]
}

resource "google_project_service" "enabled_services" {
  for_each           = toset(local.services)
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# 2. Secret Manager Secrets Provisioning
# (Values are added in Secret Manager console as versions per Step 3)
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "GEMINI_API_KEY"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_services]
}

resource "google_secret_manager_secret" "github_pat" {
  secret_id = "GITHUB_PAT"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_services]
}

resource "google_secret_manager_secret" "webhook_secret" {
  secret_id = "WEBHOOK_SECRET"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_services]
}

# 3. Grant Service Account Access to Read Secrets
resource "google_secret_manager_secret_iam_member" "gemini_api_key_accessor" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_email}"
}

resource "google_secret_manager_secret_iam_member" "github_pat_accessor" {
  secret_id = google_secret_manager_secret.github_pat.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_email}"
}

resource "google_secret_manager_secret_iam_member" "webhook_secret_accessor" {
  secret_id = google_secret_manager_secret.webhook_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_email}"
}

# 4. Artifact Registry Repository for Container Images
resource "google_artifact_registry_repository" "agent_repo" {
  location      = var.region
  repository_id = "cloud-agy-images"
  description   = "Docker container repository for Cloud Antigravity Orchestrator"
  format        = "DOCKER"
  project       = var.project_id

  depends_on = [google_project_service.enabled_services]
}
