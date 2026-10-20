variable "project_id" {
  type        = string
  description = "Google Cloud Project ID"
  default     = "project-51459e90-fbb3-471f-9f9"
}

variable "region" {
  type        = string
  description = "Google Cloud Primary Region"
  default     = "us-central1"
}

variable "service_account_email" {
  type        = string
  description = "Service account used by Workload Identity Federation & Cloud Run"
  default     = "play-upload@project-51459e90-fbb3-471f-9f9.iam.gserviceaccount.com"
}

variable "service_name" {
  type        = string
  description = "Name of the Cloud Run orchestrator service"
  default     = "cloud-agy-orchestrator"
}
