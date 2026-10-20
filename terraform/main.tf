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

# 1. Secret Manager Secrets Provisioning
# (Secret metadata containers; version values are added per Step 3)
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "GEMINI_API_KEY"
  project   = var.project_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "github_pat" {
  secret_id = "GITHUB_PAT"
  project   = var.project_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "webhook_secret" {
  secret_id = "WEBHOOK_SECRET"
  project   = var.project_id

  replication {
    auto {}
  }
}

# 2. Grant Service Account Access to Read Secrets
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
