# Private networking (REL-002). Cloud Run reaches Cloud SQL over a
# private IP through a Serverless VPC Access connector, so the database
# has NO public IP.

resource "google_compute_network" "vpc" {
  name                    = "${local.name_prefix}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "primary" {
  name          = "${local.name_prefix}-subnet"
  ip_cidr_range = "10.10.0.0/24"
  region        = var.region
  network       = google_compute_network.vpc.id
}

# A reserved range for Google-managed services (Cloud SQL private IP),
# peered into the VPC via servicenetworking.
resource "google_compute_global_address" "private_services" {
  name          = "${local.name_prefix}-priv-svc"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_vpc" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}

resource "google_vpc_access_connector" "serverless" {
  name          = "${local.name_prefix}-vpcconn"
  region        = var.region
  network       = google_compute_network.vpc.name
  ip_cidr_range = "10.11.0.0/28"
  depends_on    = [google_project_service.required]
}
