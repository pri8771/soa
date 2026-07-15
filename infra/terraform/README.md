# Infrastructure as code — GCP (REL-002 / REL-003)

The Terraform baseline for the GCP/Firebase deployment
([`DECISIONS.md`](../../docs/DECISIONS.md) OPEN-001). One module provisions
compute, database, storage, secrets, network, and telemetry; the three
`environments/*.tfvars` files are the ONLY difference between dev,
staging, and production — there is no manually created snowflake
resource.

> **Status:** formatted and validated with Terraform 1.12.2 and Google
> provider 6.50.0, but **not yet planned or applied against the owner's GCP
> project**. A credentialed staging `plan` and apply remain deployment
> evidence, not something a local validation can claim.

## What it creates

| Concern | Resource |
| --- | --- |
| Compute | Cloud Run API service plus a continuously polling Cloud Run worker pool on independently promoted image digests |
| Database | Cloud SQL for PostgreSQL 16, **private IP only**, automated backups + PITR (REL-003) |
| Storage | One Cloud Storage bucket (uniform access, public access prevented) |
| Secrets | Secret Manager entries for the DB URL, app key, and separate outbox HMAC key; a narrow API custom role manages tenant BYO secrets |
| Identity | Separate API, worker, and one-shot migrator service accounts; keyless self-signing for GCS signed URLs |
| Network | VPC + subnet + API Serverless VPC Access connector + worker direct-VPC egress + private services peering |
| Telemetry | API uptime check + an example alert policy wired to notification channels (REL-007) |

The module supplies every required deployed runtime setting: production OIDC,
strict CORS, GCS, Secret Manager, private Postgres, malware scanner location,
export allowlist, outbox destination, and the outbox signing key. The API is
public at the Cloud Run layer only when `api_allow_unauthenticated=true` (set
explicitly in the committed environments); application OIDC remains the
tenant identity and authorization boundary.

## Prerequisites (owner, one-time)

1. Create the GCP project and enable billing.
2. Create a GCS bucket for Terraform remote state (per environment or one
   with per-env prefixes).
3. Configure Identity Platform/Firebase Auth and record issuer, audience,
   JWKS URL, and the exact Firebase Hosting origin.
4. Provide a private ClamAV-compatible scanner reachable from the VPC, an
   authenticated HTTPS outbox receiver, and exact approved export hostnames.
5. Publish the independently scanned API and worker bootstrap images to
   Artifact Registry and record their digests.
6. Grant the GitHub deployer/release identities the documented Workload
   Identity, Artifact Registry, Cloud Run, Firebase Hosting, and service-
   account act-as permissions. Runtime identities do not receive deploy
   permissions.
7. Create Monitoring notification channels and note their IDs (optional
   but recommended).

## Apply

```bash
cd infra/terraform
terraform init -backend-config="bucket=YOUR_TF_STATE_BUCKET" -backend-config="prefix=staging"

terraform plan \
  -var="project_id=YOUR_PROJECT" \
  -var="api_image=REGION-docker.pkg.dev/PROJECT/soa/api@sha256:..." \
  -var="worker_image=REGION-docker.pkg.dev/PROJECT/soa/worker@sha256:..." \
  -var="api_oidc_issuer=https://securetoken.google.com/YOUR_PROJECT" \
  -var="api_oidc_audience=YOUR_PROJECT" \
  -var="api_oidc_jwks_url=https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com" \
  -var='api_cors_allowed_origins=["https://YOUR_PROJECT.web.app"]' \
  -var="api_clamav_host=clamav.internal" \
  -var='worker_export_destination_allowlist=["erp.example.com"]' \
  -var="worker_outbox_publish_url=https://events.example.com/soa" \
  -var-file=environments/staging.tfvars

terraform apply ...   # same vars
```

Prefer `TF_VAR_*` environment variables or a non-committed auto-tfvars file in
CI for these values. The uptime check derives the managed Cloud Run hostname
automatically; set `api_uptime_host` only when a custom API domain is active.

The image variables bootstrap new infrastructure. After that, the gated
release workflow promotes scanned image digests and Terraform intentionally
ignores image-only drift so an infrastructure apply cannot roll a release
back. The same workflow extracts the exact scanned web image and atomically
publishes its static files to Firebase Hosting; `WEB_API_BASE_URL` must be an
HTTPS GitHub environment variable at publication time.

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

- **Restore rehearsal** is REL-004 (needs a live instance).
- **Identity Platform/Firebase Auth tenant configuration** is an owner setup
  step; issuer/JWKS/audience are then enforced by this module.
- **ClamAV and the external event/export receivers** are deployment-owned
  services with explicit inputs, not silently mocked Terraform resources.
- Worker pools are currently a Cloud Run preview feature (`launch_stage =
  "BETA"`) and use manual instance counts. A staging soak test is required
  before production sign-off.

State files and `.tfvars` containing secrets are gitignored; only the
committed `environments/*.tfvars` (non-secret sizing) are tracked.
