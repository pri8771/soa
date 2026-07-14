# Cloud Run services for the API and worker (REL-002), running the
# immutable REL-001 images pinned by digest. Each has its own least-
# privilege service account. Secrets are mounted from Secret Manager; the
# database is reached over the private VPC connector.

# --- Service accounts -----------------------------------------------------

resource "google_service_account" "api" {
  account_id   = "${local.name_prefix}-api"
  display_name = "SOA API (${var.environment})"
}

resource "google_service_account" "worker" {
  account_id   = "${local.name_prefix}-worker"
  display_name = "SOA worker (${var.environment})"
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

# --- Services -------------------------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name     = "${local.name_prefix}-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

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
      ports {
        container_port = 8000
      }
      env {
        name  = "SOA_API_ENVIRONMENT"
        value = var.environment == "production" ? "production" : "staging"
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
}

resource "google_cloud_run_v2_service" "worker" {
  name     = "${local.name_prefix}-worker"
  location = var.region
  # The worker takes no external traffic; it polls the job queue.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.worker.email
    scaling {
      min_instance_count = var.worker_min_instances
      max_instance_count = var.worker_max_instances
    }
    vpc_access {
      connector = google_vpc_access_connector.serverless.id
      egress    = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.worker_image
      env {
        name  = "SOA_WORKER_SECRETS_BACKEND"
        value = "gcp-secret-manager"
      }
      env {
        name  = "SOA_WORKER_SECRETS_GCP_PROJECT"
        value = var.project_id
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
    }
  }

  depends_on = [google_project_service.required]
}
