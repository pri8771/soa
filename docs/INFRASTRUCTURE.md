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
S3-compatible object storage
Local queue/task runner
Tesseract/PaddleOCR
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

## 3. Initial provider choices

These are replaceable defaults rather than permanent dependencies.

| Capability | Local/default | Shared free-first | Production upgrade |
|---|---|---|---|
| Web | Local Node server | Cloudflare-style edge/static hosting | Paid edge hosting or cloud CDN |
| API/worker | Docker | Scale-to-zero container platform | Paid container compute with reserved capacity as needed |
| Database | Local PostgreSQL | Managed PostgreSQL free plan | Paid PostgreSQL with backups/PITR/read replicas as needed |
| Authentication | Local/dev mode | Managed auth free plan | Paid auth, SSO/SCIM, or enterprise identity provider |
| Object storage | MinIO/local filesystem | S3-compatible free allowance | Paid regional object storage with lifecycle and stronger support |
| Queue | Local database/Redis task runner | Managed free-allowance task queue | Paid durable workflow/queue service |
| OCR | Native parsing + Tesseract/PaddleOCR | Same, optional developer managed OCR | Managed OCR fallback or dedicated licensed engine |
| LLM | Mock/local model | Free developer endpoint for synthetic data | Paid zero/limited-retention enterprise endpoint |
| Telemetry | Console/local collector | Free observability allowance | Paid retention, alerting, audit, and on-call integration |
| Email | Local catcher | Developer email service | Paid inbound and transactional email service |

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
├── LocalObjectStore
├── R2ObjectStore
└── S3ObjectStore
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
