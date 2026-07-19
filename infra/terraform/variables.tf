# Input variables (REL-002). Every environment difference is a parameter
# here — there is no manual snowflake production resource. The per-env
# .tfvars files under environments/ supply the values.

variable "project_id" {
  type        = string
  description = "The GCP project that owns runtime, database, storage, and platform secrets."
}

variable "tenant_secrets_project_id" {
  type        = string
  description = "A separate GCP project used only for application-managed tenant credentials. This isolates dynamic BYO secrets from database and platform secrets."
  validation {
    condition = (
      length(trimspace(var.tenant_secrets_project_id)) > 0 &&
      var.tenant_secrets_project_id != var.project_id
    )
    error_message = "tenant_secrets_project_id must be non-empty and different from project_id."
  }
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

variable "api_concurrency" {
  type        = number
  description = "Maximum concurrent HTTP requests per API instance. Kept bounded because upload completion and malware scanning hold document bytes in memory."
  default     = 8
  validation {
    condition     = var.api_concurrency >= 1 && var.api_concurrency <= 32 && floor(var.api_concurrency) == var.api_concurrency
    error_message = "api_concurrency must be a whole number between 1 and 32."
  }
}

variable "worker_instances" {
  type        = number
  description = "Manually allocated Cloud Run worker-pool instances (>=1 so jobs drain)."
  default     = 1
  validation {
    condition     = var.worker_instances >= 1 && floor(var.worker_instances) == var.worker_instances
    error_message = "worker_instances must be a positive whole number."
  }
}

variable "api_cpu" {
  type        = string
  description = "Cloud Run API CPU limit."
  default     = "1"
}

variable "api_memory" {
  type        = string
  description = "Cloud Run API memory limit."
  default     = "1Gi"
}

variable "worker_cpu" {
  type        = string
  description = "Cloud Run worker-pool CPU limit; OCR needs more than the platform minimum."
  default     = "2"
}

variable "worker_memory" {
  type        = string
  description = "Cloud Run worker-pool memory limit for bounded rendering and OCR."
  default     = "4Gi"
}

variable "worker_concurrency" {
  type        = number
  description = "Maximum concurrently active durable jobs per worker instance. Maps to SOA_WORKER_MAX_CONCURRENCY; the application default is 1, which serializes every job behind the slowest LLM call, so deployments should set this explicitly."
  default     = 4
  validation {
    condition     = var.worker_concurrency >= 1 && var.worker_concurrency <= 32 && floor(var.worker_concurrency) == var.worker_concurrency
    error_message = "worker_concurrency must be a whole number between 1 and 32."
  }
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

# --- Deployed application configuration -----------------------------------

variable "api_oidc_issuer" {
  type        = string
  description = "Production OIDC issuer URL."
  validation {
    condition     = startswith(var.api_oidc_issuer, "https://")
    error_message = "api_oidc_issuer must use HTTPS."
  }
}

variable "api_oidc_audience" {
  type        = string
  description = "Audience accepted by the API OIDC validator."
  validation {
    condition     = length(trimspace(var.api_oidc_audience)) > 0
    error_message = "api_oidc_audience must not be empty."
  }
}

variable "api_oidc_jwks_url" {
  type        = string
  description = "HTTPS JWKS endpoint for the configured OIDC issuer."
  validation {
    condition     = startswith(var.api_oidc_jwks_url, "https://")
    error_message = "api_oidc_jwks_url must use HTTPS."
  }
}

variable "api_cors_allowed_origins" {
  type        = list(string)
  description = "Explicit HTTPS browser origins allowed to call the API."
  validation {
    condition = (
      length(var.api_cors_allowed_origins) > 0 &&
      alltrue([for origin in var.api_cors_allowed_origins : startswith(origin, "https://") && !strcontains(origin, "*")])
    )
    error_message = "api_cors_allowed_origins must contain explicit HTTPS origins without wildcards."
  }
}

variable "api_clamav_host" {
  type        = string
  description = "Private hostname or address of the production malware scanner."
  validation {
    condition     = length(trimspace(var.api_clamav_host)) > 0
    error_message = "api_clamav_host must not be empty."
  }
}

variable "api_clamav_port" {
  type        = number
  description = "TCP port exposed by the malware scanner."
  default     = 3310
  validation {
    condition     = var.api_clamav_port >= 1 && var.api_clamav_port <= 65535
    error_message = "api_clamav_port must be between 1 and 65535."
  }
}

variable "worker_export_destination_allowlist" {
  type        = list(string)
  description = "Exact destination hostnames workers may deliver approved exports to."
  validation {
    condition = (
      length(var.worker_export_destination_allowlist) > 0 &&
      alltrue([for host in var.worker_export_destination_allowlist : length(trimspace(host)) > 0 && !strcontains(host, "*")])
    )
    error_message = "worker_export_destination_allowlist must contain explicit hostnames without wildcards."
  }
}

variable "worker_outbox_publish_url" {
  type        = string
  description = "Authenticated HTTPS receiver for transactional domain events."
  validation {
    condition     = startswith(var.worker_outbox_publish_url, "https://")
    error_message = "worker_outbox_publish_url must use HTTPS."
  }
}

variable "telemetry_otlp_endpoint" {
  type        = string
  description = "HTTPS base URL of the production OTLP/HTTP collector used by API and worker traces and metrics."
  validation {
    condition     = startswith(var.telemetry_otlp_endpoint, "https://")
    error_message = "telemetry_otlp_endpoint must use HTTPS."
  }
}

variable "api_allow_unauthenticated" {
  type        = bool
  description = "Allow browser access to Cloud Run; application OIDC still protects tenant routes."
  default     = false
}

variable "api_uptime_host" {
  type        = string
  description = "Optional custom API hostname for uptime checks; null uses the Cloud Run hostname."
  default     = null
  nullable    = true
  validation {
    condition = (
      var.api_uptime_host == null ||
      (!strcontains(var.api_uptime_host, "://") && !strcontains(var.api_uptime_host, "/"))
    )
    error_message = "api_uptime_host must be a bare hostname without a scheme or path."
  }
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
