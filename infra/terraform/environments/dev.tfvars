# Dev environment (REL-002). Smallest sizing; scales to zero; no deletion
# protection so the environment can be torn down cheaply.

environment = "dev"
region      = "europe-west1"

db_tier                  = "db-custom-1-3840"
db_disk_gb               = 10
db_availability_type     = "ZONAL"
db_backup_retention_days = 7

api_min_instances    = 0
api_max_instances    = 2
worker_min_instances = 1
worker_max_instances = 2

storage_location    = "EU"
deletion_protection = false

# project_id, api_image, worker_image are supplied at apply time
# (-var), since they change per project and per release.
