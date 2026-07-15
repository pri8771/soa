# Cloud Run service for the API and a Cloud Run worker pool for the
# continuously polling worker (REL-002), running immutable REL-001 images
# pinned by digest. A one-shot migration job is deployed by REL-005 under
# its own identity. Secrets are mounted from Secret Manager; the database
# is reached over private VPC networking.

# --- Service accounts -----------------------------------------------------

resource "google_service_account" "api" {
  account_id   = "${local.name_prefix}-api"
  display_name = "SOA API (${var.environment})"
}

resource "google_service_account" "worker" {
  account_id   = "${local.name_prefix}-worker"
  display_name = "SOA worker (${var.environment})"
}

resource "google_service_account" "migrator" {
  account_id   = "${local.name_prefix}-migrator"
  display_name = "SOA database migrator (${var.environment})"
}

# The API and worker may read their mounted secrets and use the bucket.
resource "google_secret_manager_secret_iam_member" "api_db_url" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_secret_key" {
  secret_id = google_secret_manager_secret.app_secret_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_db_url" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_secret_key" {
  secret_id = google_secret_manager_secret.app_secret_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_outbox_signing_secret" {
  secret_id = google_secret_manager_secret.outbox_signing_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "migrator_db_url" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrator.email}"
}

# The API creates, resolves, and revokes opaque tenant integration secrets.
# A custom role grants exactly the four Secret Manager operations exercised
# by GcpSecretManagerStore rather than the broad secret-admin role.
resource "google_project_iam_custom_role" "api_tenant_secret_manager" {
  role_id     = "${replace(local.name_prefix, "-", "_")}_tenant_secret_manager"
  title       = "SOA tenant secret manager (${var.environment})"
  description = "Create, resolve, and revoke application-managed tenant secrets."
  permissions = [
    "secretmanager.secrets.create",
    "secretmanager.secrets.delete",
    "secretmanager.versions.access",
    "secretmanager.versions.add",
  ]
}

resource "google_project_iam_member" "api_tenant_secret_manager" {
  project = var.project_id
  role    = google_project_iam_custom_role.api_tenant_secret_manager.name
  member  = "serviceAccount:${google_service_account.api.email}"
}

# The worker resolves tenant BYO keys at runtime, so it needs project-wide
# accessor for secrets it did not create at apply time.
resource "google_project_iam_member" "worker_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_storage_bucket_iam_member" "api_bucket" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_bucket_iam_member" "worker_bucket" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
}

# V4 signed GCS URLs use the IAM Credentials signBlob API when the API runs
# with keyless Cloud Run credentials. Grant the runtime identity permission
# to sign only as itself; no service-account key file is created.
resource "google_service_account_iam_member" "api_self_token_creator" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.api.email}"
}

# --- Services -------------------------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name                = "${local.name_prefix}-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = var.api_min_instances
      max_instance_count = var.api_max_instances
    }
    vpc_access {
      connector = google_vpc_access_connector.serverless.id
      egress    = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.api_image
      resources {
        limits = {
          cpu    = var.api_cpu
          memory = var.api_memory
        }
      }
      ports {
        container_port = 8000
      }
      env {
        name  = "SOA_API_ENVIRONMENT"
        value = var.environment == "production" ? "production" : "staging"
      }
      env {
        name  = "SOA_API_AUTH_DEV_MODE"
        value = "false"
      }
      env {
        name  = "SOA_API_OIDC_ISSUER"
        value = var.api_oidc_issuer
      }
      env {
        name  = "SOA_API_OIDC_AUDIENCE"
        value = var.api_oidc_audience
      }
      env {
        name  = "SOA_API_OIDC_JWKS_URL"
        value = var.api_oidc_jwks_url
      }
      env {
        name  = "SOA_API_CORS_ALLOWED_ORIGINS"
        value = jsonencode(var.api_cors_allowed_origins)
      }
      env {
        name  = "SOA_API_CLAMAV_HOST"
        value = var.api_clamav_host
      }
      env {
        name  = "SOA_API_CLAMAV_PORT"
        value = tostring(var.api_clamav_port)
      }
      env {
        name  = "SOA_API_STORAGE_BACKEND"
        value = "gcs"
      }
      env {
        name  = "SOA_API_STORAGE_BUCKET"
        value = google_storage_bucket.artifacts.name
      }
      env {
        name  = "SOA_API_STORAGE_GCS_PROJECT"
        value = var.project_id
      }
      env {
        name  = "SOA_API_SECRETS_BACKEND"
        value = "gcp-secret-manager"
      }
      env {
        name  = "SOA_API_SECRETS_GCP_PROJECT"
        value = var.project_id
      }
      env {
        name = "SOA_API_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SOA_API_SECRET_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secret_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.required]

  # Images are promoted by the gated release workflow. Terraform owns all
  # service configuration but deliberately does not roll a released digest
  # back to the bootstrap value on the next infrastructure-only apply.
  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }
}

# Browser requests reach Cloud Run and are authenticated by the API's OIDC
# middleware. The public platform binding is explicit and environment-driven,
# rather than an undocumented gcloud side effect.
resource "google_cloud_run_v2_service_iam_member" "api_public_invoker" {
  count    = var.api_allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# A continuously polling process has no HTTP listener and therefore cannot
# be a Cloud Run service. Worker pools are the Cloud Run primitive for this
# pull-based workload and keep a fixed number of instances alive.
resource "google_cloud_run_v2_worker_pool" "worker" {
  name                = "${local.name_prefix}-worker"
  location            = var.region
  launch_stage        = "BETA"
  deletion_protection = var.deletion_protection
  labels              = local.labels

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.worker_instances
  }

  template {
    service_account = google_service_account.worker.email
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.primary.id
      }
    }
    containers {
      image = var.worker_image
      resources {
        limits = {
          cpu    = var.worker_cpu
          memory = var.worker_memory
        }
      }
      env {
        name  = "SOA_WORKER_ENVIRONMENT"
        value = var.environment == "production" ? "production" : "staging"
      }
      env {
        name  = "SOA_WORKER_SECRETS_BACKEND"
        value = "gcp-secret-manager"
      }
      env {
        name  = "SOA_WORKER_SECRETS_GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "SOA_WORKER_STORAGE_BACKEND"
        value = "gcs"
      }
      env {
        name  = "SOA_WORKER_STORAGE_BUCKET"
        value = google_storage_bucket.artifacts.name
      }
      env {
        name  = "SOA_WORKER_STORAGE_GCS_PROJECT"
        value = var.project_id
      }
      env {
        name  = "SOA_WORKER_EXPORT_DESTINATION_ALLOWLIST"
        value = jsonencode(var.worker_export_destination_allowlist)
      }
      env {
        name  = "SOA_WORKER_OUTBOX_PUBLISH_URL"
        value = var.worker_outbox_publish_url
      }
      env {
        name = "SOA_WORKER_OUTBOX_SIGNING_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.outbox_signing_secret.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SOA_WORKER_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SOA_WORKER_SECRET_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secret_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.required]

  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }
}
