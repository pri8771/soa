# Terraform + provider version pins (REL-002).
# Pinned so `terraform apply` is reproducible across dev/staging/production.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state lives in a GCS bucket the owner creates once, per
  # environment, before the first apply (see README). Configured via
  # `-backend-config` so the bucket/prefix are not hard-coded here.
  backend "gcs" {}
}
