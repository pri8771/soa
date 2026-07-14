# Secret Manager (REL-002 / SEC-005). The database URL and the app
# session secret are provisioned here so Cloud Run can mount them; tenant
# BYO keys are created by the application at runtime via the
# GcpSecretManagerStore, not here.

resource "google_secret_manager_secret" "database_url" {
  secret_id = "${local.name_prefix}-database-url"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  # Private-IP connection string for the app role. The password comes from
  # the generated random_password; the host is the instance's private IP.
  secret_data = format(
    "postgresql+asyncpg://%s:%s@%s:5432/soa",
    google_sql_user.app.name,
    random_password.db_app.result,
    google_sql_database_instance.primary.private_ip_address,
  )
}

resource "google_secret_manager_secret" "app_secret_key" {
  secret_id = "${local.name_prefix}-app-secret-key"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "random_password" "app_secret_key" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "app_secret_key" {
  secret      = google_secret_manager_secret.app_secret_key.id
  secret_data = random_password.app_secret_key.result
}
