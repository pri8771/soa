# Infrastructure as code — GCP (REL-002 / REL-003)

The Terraform baseline for the GCP/Firebase deployment
([`DECISIONS.md`](../../docs/DECISIONS.md) OPEN-001). One module provisions
compute, database, storage, secrets, network, and telemetry; the three
`environments/*.tfvars` files are the ONLY difference between dev,
staging, and production — there is no manually created snowflake
resource.

> **Status:** authored and reviewed, **not yet applied** — it is written
> ahead of a GCP project existing, so it has not been run through
> `terraform validate`/`plan` against a real project. Treat the first
> `plan` as part of bring-up; expect to fill the placeholders noted below.

## What it creates

| Concern | Resource |
| --- | --- |
| Compute | Two Cloud Run services (`api`, `worker`) on the REL-001 images, each with a least-privilege service account |
| Database | Cloud SQL for PostgreSQL 16, **private IP only**, automated backups + PITR (REL-003) |
| Storage | One Cloud Storage bucket (uniform access, public access prevented) |
| Secrets | Secret Manager entries for the DB URL and app secret key; the worker gets project-wide accessor for tenant BYO keys |
| Network | VPC + subnet + Serverless VPC Access connector + private services peering |
| Telemetry | API uptime check + an example alert policy wired to notification channels (REL-007) |

The app is wired to the GCP adapters by environment variables the module
sets: `SOA_API_STORAGE_BACKEND=gcs`, `SOA_*_SECRETS_BACKEND=gcp-secret-manager`,
and the DB URL / secret key mounted from Secret Manager.

## Prerequisites (owner, one-time)

1. Create the GCP project and enable billing.
2. Create a GCS bucket for Terraform remote state (per environment or one
   with per-env prefixes).
3. Build and push the REL-001 images to Artifact Registry; note their
   digests.
4. Create Monitoring notification channels and note their IDs (optional
   but recommended).

## Apply

```bash
cd infra/terraform
terraform init -backend-config="bucket=YOUR_TF_STATE_BUCKET" -backend-config="prefix=staging"

terraform plan \
  -var="project_id=YOUR_PROJECT" \
  -var="api_image=REGION-docker.pkg.dev/PROJECT/soa/api@sha256:..." \
  -var="worker_image=REGION-docker.pkg.dev/PROJECT/soa/worker@sha256:..." \
  -var-file=environments/staging.tfvars

terraform apply ...   # same vars
```

After the first apply, set the API host on the uptime check
(`REPLACE_WITH_API_HOST`, from the `api_url` output) and re-apply.

## Backups, RPO, and RTO (REL-003)

- **Automated daily backups** at 02:00 with **point-in-time recovery**
  (WAL/transaction-log archiving) enabled. Transaction logs are retained
  7 days; full backups are retained per environment
  (`db_backup_retention_days`: dev 7, staging 14, production 30).
- **RPO (recovery point objective): ≤ 5 minutes** — PITR replays
  transaction logs to any point within the retention window, so at most
  the last few minutes of writes are at risk.
- **RTO (recovery time objective): ≤ 1 hour** for a full instance
  restore to a new instance (dominated by Cloud SQL restore time for the
  configured disk size).
- **Encryption at rest** is always on (Google-managed keys; a CMEK can be
  added later).
- **Restore ownership**: the platform on-call runs the restore, following
  [`docs/runbooks/restore.md`](../../docs/runbooks/restore.md). The
  restore rehearsal that proves these numbers is REL-004, run against
  staging.

## Not included here

- **Deploy workflow** (build → migrate → deploy → readiness → smoke) is
  REL-005.
- **Restore rehearsal** is REL-004 (needs a live instance).
- **Firebase Auth / Hosting** are configured in the Firebase console /
  their own config, not this Terraform (they are not first-class Google
  provider resources for our use).

State files and `.tfvars` containing secrets are gitignored; only the
committed `environments/*.tfvars` (non-secret sizing) are tracked.
