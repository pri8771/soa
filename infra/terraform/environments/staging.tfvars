# Staging environment (REL-002). Production-like but single-zone and
# smaller; where REL-004/006 rehearsals and the pilot game-day run.

environment = "staging"
region      = "europe-west1"

db_tier                  = "db-custom-2-7680"
db_disk_gb               = 20
db_availability_type     = "ZONAL"
db_backup_retention_days = 14

api_min_instances = 1
api_max_instances = 4
worker_instances  = 1

api_allow_unauthenticated = true

storage_location    = "EU"
deletion_protection = true
