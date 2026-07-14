# Outputs (REL-002). Non-secret values the deploy workflow (REL-005) and
# the owner need after an apply. Secrets are never output.

output "api_url" {
  description = "Public URL of the API Cloud Run service."
  value       = google_cloud_run_v2_service.api.uri
}

output "worker_service" {
  description = "Name of the worker Cloud Run service."
  value       = google_cloud_run_v2_service.worker.name
}

output "database_instance" {
  description = "Cloud SQL instance connection name."
  value       = google_sql_database_instance.primary.connection_name
}

output "database_private_ip" {
  description = "Private IP the app reaches Postgres on."
  value       = google_sql_database_instance.primary.private_ip_address
}

output "artifacts_bucket" {
  description = "GCS bucket holding document artifacts."
  value       = google_storage_bucket.artifacts.name
}

output "api_service_account" {
  description = "API runtime service account email."
  value       = google_service_account.api.email
}

output "worker_service_account" {
  description = "Worker runtime service account email."
  value       = google_service_account.worker.email
}
