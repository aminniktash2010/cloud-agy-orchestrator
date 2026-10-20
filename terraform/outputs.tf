output "cloud_run_service_url" {
  description = "The base HTTPS endpoint for your Antigravity Cloud Orchestrator"
  value       = google_cloud_run_v2_service.agy_service.uri
}

output "cloud_run_webhook_url" {
  description = "The HTTPS endpoint for your Antigravity Cloud Orchestrator"
  value       = "${google_cloud_run_v2_service.agy_service.uri}/chat"
}

output "cache_bucket_name" {
  description = "The 5GB Free Tier GCS Bucket storing tools and .tar.zst repo snapshots"
  value       = google_storage_bucket.agent_cache.name
}

output "service_account_email" {
  description = "Active service account running the orchestrator"
  value       = var.existing_service_account
}
