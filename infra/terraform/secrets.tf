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

# The schema-owner credential is available only to the one-shot migration
# service account. API and worker identities have no IAM access to it.
resource "google_secret_manager_secret" "migrator_database_url" {
  secret_id = "${local.name_prefix}-migrator-database-url"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "migrator_database_url" {
  secret = google_secret_manager_secret.migrator_database_url.id
  secret_data = format(
    "postgresql+psycopg://%s:%s@%s:5432/soa",
    google_sql_user.migrator.name,
    random_password.db_migrator.result,
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

# A dedicated HMAC key authenticates worker outbox deliveries. It is not
# reused for API sessions, so either credential can be rotated or revoked
# without coupling two unrelated security boundaries.
resource "google_secret_manager_secret" "outbox_signing_secret" {
  secret_id = "${local.name_prefix}-outbox-signing-secret"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "random_password" "outbox_signing_secret" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "outbox_signing_secret" {
  secret      = google_secret_manager_secret.outbox_signing_secret.id
  secret_data = random_password.outbox_signing_secret.result
}

# A separate credential protects the provider-neutral raw-MIME intake seam.
# It is not reused for sessions, rate-limit HMACs, or outbound webhooks.
resource "google_secret_manager_secret" "email_intake_secret" {
  secret_id = "${local.name_prefix}-email-intake-secret"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "random_password" "email_intake_secret" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "email_intake_secret" {
  secret      = google_secret_manager_secret.email_intake_secret.id
  secret_data = random_password.email_intake_secret.result
}
