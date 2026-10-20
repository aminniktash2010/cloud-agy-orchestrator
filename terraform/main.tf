terraform {
  required_version = ">= 1.5.0"

  # Remote state so each GitHub Actions run sees previously created resources
  backend "gcs" {
    bucket = "project-51459e90-fbb3-471f-9f9-tfstate"
    prefix = "cloud-agy-orchestrator"
  }

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

# 1. Artifact Registry Reference (Pre-created agy-agent-repo in us-central1)
data "google_artifact_registry_repository" "agent_repo" {
  location      = var.region
  repository_id = "agy-agent-repo"
  project       = var.project_id
}

# 2. Persistent 5GB Free Tier Cloud Storage Bucket for Tools & Repo Snapshots (.tar.zst)
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
}

resource "google_storage_bucket_iam_member" "cache_sa_access" {
  bucket = google_storage_bucket.agent_cache.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.existing_service_account}"
}

# 3. Secure Key Vault (Reference existing secrets created in Secret Manager)
locals {
  secrets = toset([
    "GEMINI_API_KEY",
    "GITHUB_PAT",
    "WEBHOOK_SECRET"
  ])
}

data "google_secret_manager_secret" "vault" {
  for_each  = local.secrets
  secret_id = each.key
  project   = var.project_id
}

resource "google_secret_manager_secret_iam_member" "sa_secret_access" {
  for_each  = local.secrets
  secret_id = data.google_secret_manager_secret.vault[each.key].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.existing_service_account}"
}

# 4. Deploy Cloud Run v2 Service with Dual Volumes (Hot RAM Disk + Cold GCS Cache)
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

    # Persistent 5GB Free GCS Cache for tools & repo snapshots
    volumes {
      name = "cold-cache"
      gcs {
        bucket    = google_storage_bucket.agent_cache.name
        read_only = false
      }
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${data.google_artifact_registry_repository.agent_repo.repository_id}/agy-orchestrator:${var.image_tag}"

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
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
              secret  = data.google_secret_manager_secret.vault[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
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
