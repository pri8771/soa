# Terraform + provider version pins (REL-002).
# Pinned so `terraform apply` is reproducible across dev/staging/production.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source = "hashicorp/google"
      # 6.42 is the first baseline used here with the Cloud Run v2 worker
      # pool resource. Keep the minor line bounded until a reviewed upgrade.
      version = "~> 6.42"
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
