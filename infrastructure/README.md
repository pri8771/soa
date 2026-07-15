# Infrastructure

Local supporting services live in `infrastructure/local/docker-compose.yml`:
PostgreSQL, MinIO, and Mailpit in the default profile, with optional ClamAV and
local telemetry profiles. The init script creates the non-superuser `soa_app`
runtime role; Alembic still runs through the separate owner/migrator URL.

The GCP staging/production reference lives separately in `infra/terraform/`.
It defines Cloud Run API/worker/migrator identities, private Cloud SQL, GCS,
Secret Manager, networking, Firebase-facing inputs, and baseline telemetry.
It has been formatted and locally validated but not applied to the owner's
project; live plan/apply, restore, alert, identity, scanner, and soak evidence
remain release gates.

Use [`docs/LOCAL_DEV.md`](../docs/LOCAL_DEV.md) for local startup and
[`infra/terraform/README.md`](../infra/terraform/README.md) for the hosted
deployment contract.
