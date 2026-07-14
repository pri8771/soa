# Cloud SQL for PostgreSQL (REL-002) with automated backups + PITR
# (REL-003). Private IP only; at-rest encryption is always on (Google
# default; a CMEK can be added later). Sizing/HA/retention are variables,
# so production differs from dev only by its .tfvars.

resource "google_sql_database_instance" "primary" {
  name             = "${local.name_prefix}-pg"
  region           = var.region
  database_version = "POSTGRES_16"

  # In production this is true, so a stray `terraform destroy` cannot drop
  # the database; it is disabled in dev to allow teardown.
  deletion_protection = var.deletion_protection

  depends_on = [google_service_networking_connection.private_vpc]

  settings {
    tier              = var.db_tier
    availability_type = var.db_availability_type
    disk_size         = var.db_disk_gb
    disk_type         = "PD_SSD"
    disk_autoresize   = true
    user_labels       = local.labels

    # REL-003: automated daily backups with point-in-time recovery (WAL
    # archiving) and an explicit retention window. RPO/RTO are documented
    # in the README and docs/runbooks/restore.md.
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "02:00"
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = var.db_backup_retention_days
        retention_unit   = "COUNT"
      }
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
    }

    # Deletion protection at the settings level as well as the instance
    # level (belt and suspenders for production).
    deletion_protection_enabled = var.deletion_protection

    insights_config {
      query_insights_enabled = true
    }
  }
}

resource "google_sql_database" "app" {
  name     = "soa"
  instance = google_sql_database_instance.primary.name
}

# The application role. Its password is generated and stored in Secret
# Manager (secrets.tf) — never written to state as plaintext beyond the
# random_password resource, and never checked in.
resource "google_sql_user" "app" {
  name     = "soa_app"
  instance = google_sql_database_instance.primary.name
  password = random_password.db_app.result
}

resource "random_password" "db_app" {
  length  = 32
  special = false
}
