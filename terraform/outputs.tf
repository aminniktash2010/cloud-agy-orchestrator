output "gemini_api_key_secret_id" {
  description = "Secret ID for Gemini API Key"
  value       = google_secret_manager_secret.gemini_api_key.secret_id
}

output "github_pat_secret_id" {
  description = "Secret ID for GitHub Personal Access Token"
  value       = google_secret_manager_secret.github_pat.secret_id
}

output "webhook_secret_id" {
  description = "Secret ID for Webhook Auth Secret"
  value       = google_secret_manager_secret.webhook_secret.secret_id
}
