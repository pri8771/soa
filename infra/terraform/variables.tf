# Input variables (REL-002). Every environment difference is a parameter
# here — there is no manual snowflake production resource. The per-env
# .tfvars files under environments/ supply the values.

variable "project_id" {
  type        = string
  description = "The GCP project that owns all resources."
}

variable "environment" {
  type        = string
  description = "Deployment environment: dev | staging | production."
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of dev, staging, production."
  }
}

variable "region" {
  type        = string
  description = "Primary GCP region (e.g. europe-west1)."
  default     = "europe-west1"
}

# --- Sizing (environment-driven, no snowflakes) ----------------------------

variable "db_tier" {
  type        = string
  description = "Cloud SQL machine tier (e.g. db-custom-1-3840)."
}

variable "db_disk_gb" {
  type        = number
  description = "Cloud SQL data disk size in GB."
}

variable "db_availability_type" {
  type        = string
  description = "ZONAL (dev/staging) or REGIONAL (production HA)."
  validation {
    condition     = contains(["ZONAL", "REGIONAL"], var.db_availability_type)
    error_message = "db_availability_type must be ZONAL or REGIONAL."
  }
}

variable "db_backup_retention_days" {
  type        = number
  description = "How many automated daily backups to retain (REL-003)."
}

variable "api_image" {
  type        = string
  description = "Immutable soa-api container image, pinned by digest (REL-001/005)."
}

variable "worker_image" {
  type        = string
  description = "Immutable soa-worker container image, pinned by digest."
}

variable "api_min_instances" {
  type        = number
  description = "Cloud Run minimum instances for the API (0 in dev to scale to zero)."
  default     = 0
}

variable "api_max_instances" {
  type        = number
  description = "Cloud Run maximum instances for the API."
  default     = 4
}

variable "worker_min_instances" {
  type        = number
  description = "Cloud Run minimum instances for the worker (>=1 so jobs drain)."
  default     = 1
}

variable "worker_max_instances" {
  type        = number
  description = "Cloud Run maximum instances for the worker."
  default     = 4
}

variable "storage_location" {
  type        = string
  description = "GCS bucket location (region or multi-region)."
  default     = "EU"
}

variable "deletion_protection" {
  type        = bool
  description = "Guard against accidental destroy of stateful resources (true in production)."
}

variable "alert_notification_channels" {
  type        = list(string)
  description = "Monitoring notification channel IDs the owner created (REL-007)."
  default     = []
}

# --- Derived, not per-env -------------------------------------------------

locals {
  name_prefix = "soa-${var.environment}"
  # A CMEK could be added here later (OPEN: general BYOK); default at-rest
  # encryption is always on in GCP.
  labels = {
    app         = "soa"
    environment = var.environment
    managed_by  = "terraform"
  }
}
