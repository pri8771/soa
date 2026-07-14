# Production environment (REL-002). Regional (HA) database, deletion
# protection on, longer backup retention. Differs from staging ONLY by
# these parameters — no snowflake resources.

environment = "production"
region      = "europe-west1"

db_tier                  = "db-custom-4-15360"
db_disk_gb               = 50
db_availability_type     = "REGIONAL"
db_backup_retention_days = 30

api_min_instances    = 2
api_max_instances    = 10
worker_min_instances = 2
worker_max_instances = 10

storage_location    = "EU"
deletion_protection = true
