terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Enable Required Google Cloud APIs
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "drive.googleapis.com",
    "storage.googleapis.com"
  ])
  service            = each.key
  disable_on_destroy = false
}

# 2. Artifact Registry (~95 MB Slim Container + Strict 500MB Free Tier Cleanup Guardrail)
resource "google_artifact_registry_repository" "agent_repo" {
  location      = var.region
  repository_id = "agy-agent-repo"
  description   = "Slim Docker repository for Antigravity Cloud Orchestrator"
  format        = "DOCKER"

  cleanup_policies {
    id     = "delete-untagged-images"
    action = "DELETE"
    condition {
      tag_state = "UNTAGGED"
    }
  }

  cleanup_policies {
    id     = "keep-only-latest-version"
    action = "KEEP"
    most_recent_versions {
      keep_count = 1
    }
  }

  depends_on = [google_project_service.apis]
}

# 3. Persistent 5GB Free Tier Cloud Storage Bucket for Tools (Node/AGY/Stitch) & Repo Snapshots (.tar.zst)
resource "google_storage_bucket" "agent_cache" {
  name                        = "${var.project_id}-agy-repo-cache"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "cache_sa_access" {
  bucket = google_storage_bucket.agent_cache.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.existing_service_account}"
}

# 4. Secure Key Vault (Google Secret Manager - 3 Secrets = Well Under 6 Free Active Versions)
locals {
  secrets = toset([
    "GEMINI_API_KEY",
    "GITHUB_PAT",
    "WEBHOOK_SECRET"
  ])
}

resource "google_secret_manager_secret" "vault" {
  for_each  = local.secrets
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "vault_placeholder" {
  for_each    = local.secrets
  secret      = google_secret_manager_secret.vault[each.key].id
  secret_data = "CHANGE_ME_IN_GCP_CONSOLE"

  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_iam_member" "sa_secret_access" {
  for_each  = local.secrets
  secret_id = google_secret_manager_secret.vault[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.existing_service_account}"
}

# 5. Deploy Cloud Run v2 Service with Dual Volumes (Hot RAM Disk + Cold GCS Cache)
resource "google_cloud_run_v2_service" "agy_service" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.existing_service_account
    timeout         = "600s"

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    # Tier 2: Hot In-Memory RAM Disk for isolated Git & AGY operations
    volumes {
      name = "hot-workspace"
      empty_dir {
        medium     = "MEMORY"
        size_limit = "1Gi"
      }
    }

    # Tier 3: Persistent 5GB Free GCS Cache for tools & repo snapshots
    volumes {
      name = "cold-cache"
      gcs {
        bucket    = google_storage_bucket.agent_cache.name
        read_only = false
      }
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/agy-agent-repo/agy-orchestrator:latest"

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
      }

      volume_mounts {
        name       = "hot-workspace"
        mount_path = "/workspace"
      }

      volume_mounts {
        name       = "cold-cache"
        mount_path = "/mnt/cache"
      }

      env {
        name  = "DRIVE_STATUS_FILE_ID"
        value = var.drive_status_file_id
      }

      dynamic "env" {
        for_each = local.secrets
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.vault[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.vault_placeholder,
    google_secret_manager_secret_iam_member.sa_secret_access
  ]
}

# Allow public webhook triggers (protected by x-webhook-token header inside FastAPI)
resource "google_cloud_run_v2_service_iam_member" "public_webhook" {
  name     = google_cloud_run_v2_service.agy_service.name
  location = google_cloud_run_v2_service.agy_service.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
