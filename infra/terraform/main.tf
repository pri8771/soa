# Provider + required Google APIs (REL-002).

provider "google" {
  project = var.project_id
  region  = var.region
}

# APIs the module depends on. Enabling is idempotent; other resources
# depend_on this so the first apply does not race API activation.
resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "servicenetworking.googleapis.com",
    "vpcaccess.googleapis.com",
    "compute.googleapis.com",
    "monitoring.googleapis.com",
    "artifactregistry.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# Dynamic tenant credentials live outside the runtime project. Project-wide
# permissions are unavoidable for secrets created after Terraform apply; a
# dedicated project prevents those permissions from reaching the migrator DB
# URL, application keys, or other platform credentials.
resource "google_project_service" "tenant_secret_manager" {
  project            = var.tenant_secrets_project_id
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}
