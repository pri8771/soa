# Free-First Infrastructure and Paid Upgrade Path

## 1. Decision

Use free or low-cost managed services for development, demonstrations, and controlled internal testing, while designing every dependency behind a stable application interface so production can move to paid plans without rewriting product logic.

The goal is **free-first, not free-dependent**.

## 2. Environments

### Local development

Runs without external services where practical:

```text
Web application
FastAPI API
Worker
PostgreSQL
Filesystem or MinIO object storage
PostgreSQL durable queue
Tesseract OCR
Mock or local LLM
Local email catcher
Local telemetry
```

Local development must support synthetic documents and deterministic mocked extraction so engineers can work without consuming cloud quotas or exposing sensitive data.

### Shared development/demo

May use free allowances for:

- Static/edge web hosting
- Managed PostgreSQL and authentication
- S3-compatible object storage
- Scale-to-zero API/worker containers
- Managed task queue
- Open-source OCR
- Developer/free LLM endpoints using synthetic or non-confidential documents only
- GitHub Actions

### Customer pilot

Must use paid services wherever required for:

- Suitable customer-data terms
- Non-pausing database operation
- Automated backups and restore
- Monitoring and log retention
- Predictable processing capacity
- Transactional email
- Security controls and support

### Commercial production

Uses paid plans matched to contractual availability, recovery, security, data residency, support, and scale commitments.

## 3. Implemented reference choices

These are replaceable defaults rather than permanent dependencies.

| Capability | Local/development | Reference staging/production | Honest status |
| --- | --- | --- | --- |
| Web | Vite | Firebase Hosting, publishing exact scanned web-image bytes | Implemented; live project/domain/auth configuration pending |
| API/worker | Local processes or Docker | Cloud Run API plus Cloud Run worker pool | Terraform/workflows implemented; live apply/soak pending |
| Database/jobs | PostgreSQL | Private Cloud SQL PostgreSQL with database-backed queue | Code/IaC implemented; backup/PITR/restore evidence pending |
| Authentication | Development identity | Firebase Authentication / Identity Platform through OIDC/JWT | Browser/API implemented; live MFA/session/revocation tests pending |
| Object storage | Filesystem or MinIO | Regional GCS with versioning/lifecycle/checksums | Adapter/IaC implemented; real-bucket and restore evidence pending |
| Secrets | Memory/file | Dedicated tenant-credential GCP Secret Manager project, isolated from runtime-project platform secrets (AWS adapter also available) | References/rotation/IAM boundary implemented; live cross-project plan and rotation drill pending |
| OCR/malware | Native text + Tesseract; optional ClamAV profile | Tesseract + mandatory ClamAV | Code implemented; scanner capacity/containment evidence pending |
| LLM | Mock or local OpenAI-compatible model | Stream-pinned local or tenant BYO hosted provider | Implemented; corpus, contract, region, and invoice gates pending |
| Telemetry | Console or local collector | Authenticated OTLP/HTTP collector | Batched export implemented; collector/alerts/on-call pending |
| Email/events | Mailpit; raw-MIME development seam | Provider forwarding into bounded intake; signed HTTPS outbox receiver | Application seams implemented; providers/receivers pending |

## 4. Portability contracts

Application code must depend on internal contracts such as:

```text
DatabaseRepository
ObjectStore
TaskQueue
WorkflowEngine
IdentityProvider
SecretStore
OcrProvider
ExtractionProvider
EmailIngress
NotificationProvider
TelemetrySink
```

Vendor implementations are selected through configuration and dependency injection.

### Example

```text
ObjectStore
├── FilesystemObjectStore
├── S3ObjectStore (including MinIO)
└── GcsObjectStore
```

Moving from a free object-storage plan to paid capacity on the same provider should be an account/plan change with no code change. Moving to another compatible provider should require an adapter/configuration change and data copy, not a business-logic rewrite.

## 5. What makes free-to-paid easy

### Same provider, upgraded plan

Usually the easiest path:

- Change billing plan
- Increase limits/capacity
- Enable backups, retention, support, or advanced security
- Keep existing endpoints and data
- Update quotas and monitoring thresholds

Expected application change: little or none.

### Same technology, new managed provider

Example: one hosted PostgreSQL service to another.

Required work:

- Provision destination
- Apply migrations
- Copy data
- Validate extensions and settings
- Rotate connection secrets
- Run reconciliation and cutover
- Maintain rollback window

Expected application change: none when standard PostgreSQL and portable migrations are used.

### Different provider, compatible contract

Example: R2-compatible storage to S3.

Required work:

- Implement or activate the destination adapter
- Copy objects and metadata
- Verify checksums
- Update environment configuration
- Test signed URLs, lifecycle, and deletion

Expected application change: adapter and infrastructure configuration only.

### Different underlying technology

Example: a simple task queue to a full workflow engine.

This is the highest-effort migration. Reduce the impact by keeping workflow state, stage contracts, idempotency, and business logic outside vendor-specific code.

## 6. Data portability rules

- PostgreSQL is the canonical relational store.
- Migrations live in the repository and are runnable outside a specific hosting platform.
- Original and derived documents use standard object-storage semantics.
- Provider-specific IDs are stored as metadata, never as the only business identifier.
- Canonical extraction and export payloads use versioned JSON schemas.
- Configuration and reference data are exportable.
- Audit data is queryable and exportable.
- No essential business state may exist only inside an ephemeral queue or provider dashboard.

## 7. Configuration rules

Use environment and secret configuration for:

- Database URL
- Object-store endpoint/bucket/region
- Queue/workflow implementation
- Authentication issuer/client
- OCR and LLM provider credentials
- Email provider
- Telemetry exporter
- Feature flags

Do not scatter vendor checks throughout business code. Provider selection is resolved at application composition boundaries.

**Worker concurrency.** `SOA_WORKER_MAX_CONCURRENCY` (`max_concurrency` in
`apps/worker/src/soa_worker/settings.py`, range 1–32) bounds how many jobs one
worker instance runs at once. The application default is **1**, which
serializes every job behind the slowest LLM call — deployments must set this
explicitly. `infra/terraform/variables.tf`'s `worker_concurrency` variable
(default `4`) sets it for the Cloud Run worker pool via `compute.tf`; raise or
lower it per environment in `infra/terraform/environments/*.tfvars`.

## 8. Free-tier constraints and product impact

Free plans may impose inactivity pausing, storage limits, compute limits, request quotas, short log retention, weak backup options, and no availability commitment.

Therefore development/demo environments should enforce configurable limits for:

- File size and page count
- Concurrent documents
- Processing retries
- Retention duration
- Number of active streams
- OCR languages
- Model escalation passes
- Bulk-upload size
- Evaluation frequency
- Analytics history

These are quota policies, not architectural limitations. Production plans can raise them by configuration.

## 9. Prohibited production shortcuts

- Do not process confidential customer documents through consumer/developer AI plans without suitable terms.
- Do not promise an uptime SLA while relying on pausable or unsupported free services.
- Do not launch without tested backups because the application is “still small.”
- Do not store important files only on ephemeral container disks.
- Do not place essential workflow state only in an in-memory queue.
- Do not hard-code provider-specific identifiers into domain models.
- Do not use a hosting plan whose terms prohibit commercial use.

## 10. Migration testing

Before a real customer pilot, complete at least one portability rehearsal:

1. Export and restore the development database into a fresh instance.
2. Copy all document objects and verify hashes.
3. Rotate every application secret.
4. Switch an OCR or LLM adapter in a non-production stream.
5. Re-run a golden dataset and compare outputs.
6. Confirm queues are drained or replayed safely.
7. Confirm signed webhooks and integrations still work.
8. Document cutover and rollback steps.

## 11. Cost controls

- Track usage and cost per tenant, stream, provider, document, page, and accepted order.
- Set per-stream model and OCR budgets.
- Prefer native parsing before OCR.
- Prefer local/open-source OCR before managed fallback when quality permits.
- Use the least expensive extraction path that passes quality gates.
- Cache only tenant-safe deterministic artifacts.
- Prevent unbounded retries and page/token consumption.
- Alert before free or paid quotas are exhausted.

## 12. Answer to the upgrade question

Yes, switching from free to paid should be easy when it is merely a plan upgrade, and manageable when changing vendors. The architecture must make migration an infrastructure and data-operation task rather than a product rewrite. The largest unavoidable differences are enterprise identity, backup/recovery, regional deployment, private networking, and durable workflow guarantees; their interfaces and data requirements are defined now even if the first implementation is simpler.
