variable "project_id" {
  type        = string
  description = "Google Cloud Project ID"
  default     = "project-51459e90-fbb3-471f-9f9"
}

variable "project_number" {
  type        = string
  description = "Google Cloud Project Number"
  default     = "202076903469"
}

variable "region" {
  type        = string
  description = "GCP Region (Always-Free Tier eligible region)"
  default     = "us-central1"
}

variable "service_name" {
  type        = string
  description = "Name of the Cloud Run Antigravity Orchestrator service"
  default     = "agy-cloud-orchestrator"
}

variable "existing_service_account" {
  type        = string
  description = "Pre-configured WIF Service Account Email"
  default     = "play-upload@project-51459e90-fbb3-471f-9f9.iam.gserviceaccount.com"
}

variable "drive_status_file_id" {
  type        = string
  description = "Optional File ID of pre-created latest_build_status.md in your 5TB Google Drive (_AGY_Cloud_Vault)"
  default     = ""
}
