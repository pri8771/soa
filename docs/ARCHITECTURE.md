# Architecture

This document describes the stable domain and target architecture. The current
production-readiness boundary is narrower: one sales order per input, a
PostgreSQL durable queue, text/native-OCR extraction, and a GCP reference
deployment. See [`PRODUCTION_READINESS_AUDIT.md`](PRODUCTION_READINESS_AUDIT.md)
for implemented versus unverified capabilities.

## 1. Architecture goals

- Support secure multi-tenant B2B operation from the beginning.
- Preserve a clean upgrade path from free development services to paid production tiers.
- Keep document-processing providers replaceable.
- Make every processing stage idempotent, observable, retryable, and reproducible.
- Start as a modular monolith rather than premature microservices.
- Separate product configuration from document-processing execution.

## 2. Recommended repository structure

```text
soa/
├── apps/
│   ├── web/              # React/TypeScript application
│   ├── api/              # FastAPI HTTP/control-plane API
│   └── worker/           # asynchronous document-processing worker
├── packages/
│   ├── canonical/        # canonical order and mapping/export logic
│   ├── config/           # settings, telemetry, alert, and supply-chain policy
│   ├── db/               # repositories, durable state, and tenant guard
│   ├── design-system/    # tokens and reusable UI components
│   ├── integrations/     # outbound capability and destination controls
│   ├── normalize,rules/  # deterministic business processing
│   ├── storage/          # object and managed-secret adapters
│   └── test-fixtures/    # synthetic documents and expected outputs
├── infrastructure/local/ # local supporting services
├── infra/terraform/      # GCP reference deployment
├── migrations/           # database migrations
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── end-to-end/
│   ├── security/
│   └── evaluations/
└── docs/
```

## 3. Logical components

### Web application

- Organization, process, and stream administration
- Document operations command center
- Review Studio
- Catalog management
- Configuration and version comparison
- Integration setup and delivery history
- Accuracy, operational, and usage analytics

### API / control plane

- Authentication and authorization
- Organizations, memberships, roles, and API credentials
- Process, stream, schema, workflow, rule, and provider configuration
- Document metadata and signed upload/download operations
- Review, approval, catalog, integration, and reporting APIs
- Audit-event creation

### Processing worker / data plane

- File validation and preprocessing
- Native text extraction and OCR
- Classification and packet splitting
- Schema-constrained extraction
- Normalization, matching, validation, and confidence
- Thumbnail and derived-artifact generation
- Integration delivery and retry

### Durable orchestration

A document workflow must persist stage state outside process memory. The orchestration implementation may begin with a managed queue and database-backed state machine, while preserving interfaces for a dedicated workflow engine later.

Required guarantees:

- Idempotency key per stage and input version
- Retry policy and attempt history
- Dead-letter/quarantine state
- Stage-level replay
- Cancellation
- Timeouts
- Provider fallback
- No document loss when a worker exits

## 4. Control plane and processing plane

### Control plane

Contains configuration and business application state:

- Tenants, users, permissions
- Processes, streams, schemas, rules
- Catalog metadata and versions
- Integration configuration
- Billing and usage
- Analytics aggregates

### Processing plane

Handles potentially sensitive document content:

- Original and derived files
- OCR/layout output
- Extraction requests and responses
- Evidence regions
- Validation execution
- Review data
- Export payloads

Keeping these boundaries explicit allows future regional, private-cloud, or customer-hosted processing workers without rebuilding the control plane.

## 5. Core entities

### Identity and tenancy

- `Organization`
- `Workspace`
- `User`
- `Membership`
- `Role`
- `Permission`
- `ServiceCredential`

### Configuration

- `Process`
- `ProcessVersion`
- `Stream`
- `StreamVersion`
- `InputSource`
- `Schema`
- `SchemaVersion`
- `FieldDefinition`
- `WorkflowDefinition`
- `WorkflowVersion`
- `RuleSet`
- `RuleSetVersion`
- `ProviderPolicy`
- `Integration`
- `MappingProfile`
- `RetentionPolicy`

### Documents and execution

- `Document`
- `DocumentFile`
- `Page`
- `DocumentPacket`
- `ProcessingRun`
- `StageRun`
- `ExtractionRun`
- `ExtractedField`
- `FieldCandidate`
- `EvidenceRegion`
- `ValidationResult`
- `ConfidenceResult`
- `DeletionRequest`
- `LegalHold`
- `DeletionTombstone`

### Review and delivery

- `ReviewTask`
- `FieldCorrection`
- `Comment`
- `Approval`
- `ExportJob`
- `DeliveryAttempt`
- `AuditEvent`

### Reference data and evaluation

- `Catalog`
- `CatalogVersion`
- `CatalogRecord`
- `GoldDataset`
- `GoldDocument`
- `EvaluationRun`
- `EvaluationResult`
- `UsageRecord`

## 6. Tenant isolation

Every tenant-owned database row must carry an `organization_id`. Stream-owned resources also carry a `stream_id`.

Isolation requirements:

- Authorization is applied server-side for every request and background task.
- Database queries are scoped by tenant through a shared repository layer and, where supported, database row-level security.
- Object-storage keys include non-guessable organization and document identifiers.
- Cache keys include the organization boundary.
- Queue messages contain immutable tenant context and are re-authorized by workers.
- Internal support access is time-limited, approved, and audited.
- Automated tests attempt cross-tenant reads, writes, signed URLs, job replay, and cache access.

## 7. Document state model

```text
received
→ validating_file
→ queued
→ preprocessing
→ classifying
→ splitting
→ extracting
→ normalizing
→ validating_data
→ review_required | approved
→ exporting
→ completed
```

Exceptional states:

```text
quarantined
failed_retryable
failed_terminal
rejected
cancelled
archived
```

The current state is a projection of immutable or append-oriented workflow events. State changes must record actor/system, timestamp, reason, version context, and correlation ID.

`deleted` is a separate terminal erasure outcome, not another name for
`archived`:

```text
completed | rejected | cancelled | failed_terminal | quarantined | archived
→ pending deletion request
→ independently approved durable deletion job
→ reconciled erasure
→ deleted
```

The intermediate deletion states belong to `DeletionRequest`; the `Document`
remains in its settled state until the reconciled erasure transaction moves it
directly to `deleted`.

The requester and approver must be different authorized principals. An active
legal hold is an absolute veto at approval and execution; placing a hold before
erasure commits revokes unexecuted approval, and releasing it never resumes the
workflow automatically. Lifecycle mutations and worker execution serialize on
the tenant/document boundary so cancellation, holds, approval, and erasure have
a deterministic winner. Stale queue deliveries and retries are idempotent.

Successful erasure removes content and derived data, verifies every external
object is absent, invalidates copied exports and evaluation evidence, and
anonymizes the retained document row. The request, hold history, counts-only
audit evidence, and one tombstone remain as attributable proof. The document
then has state `deleted`, with no outgoing transition. Default document lists
hide these terminal shells; authorized callers can retrieve them with the
explicit `?document_state=deleted` filter.

## 8. Storage model

- Original files are immutable.
- Derived page images, OCR output, layout JSON, raw provider output, canonical output, and export payloads are stored as separate artifacts.
- The relational database stores metadata, hashes, structured fields, evidence pointers, and indexes—not large file blobs.
- Signed URLs are short-lived and tenant-scoped.
- Each artifact records content hash, media type, size, encryption metadata, creation stage, and retention class.

Suggested object-key shape:

```text
organizations/{organization_id}/documents/{document_id}/original/{file_id}
organizations/{organization_id}/documents/{document_id}/runs/{run_id}/pages/{page}.png
organizations/{organization_id}/documents/{document_id}/runs/{run_id}/ocr.json
organizations/{organization_id}/documents/{document_id}/runs/{run_id}/extraction.json
```

## 9. Stable provider interfaces

```text
ObjectStore
TaskQueue
EmailIngress
OcrProvider
LayoutProvider
ClassifierProvider
ExtractionProvider
EmbeddingProvider
SecretStore
NotificationProvider
IntegrationAdapter
TelemetrySink
```

Application services depend on these interfaces, never directly on a vendor SDK. Vendor-specific credentials and capabilities are resolved from environment and organization/stream policy.

## 10. Canonical sales-order contract

Every provider and customer-specific schema maps into an internal canonical order representation. Integrations consume this canonical contract through mapping profiles.

This prevents ERP-specific fields from leaking into OCR/LLM extraction logic and allows the same approved order to be delivered in JSON, CSV, SAP, Oracle, Dynamics, or another target representation.

## 11. Configuration inheritance and versioning

- Processes define reusable defaults.
- Streams store only explicit overrides.
- A published stream version resolves into a complete immutable configuration snapshot.
- Every processing run references the resolved version IDs used.
- Publishing runs validation and optional historical simulation.
- Rollback creates a new active pointer; historical runs remain reproducible.

Version at minimum:

- Schema
- Workflow
- Rules
- Prompts/instructions
- Provider policy
- Catalogs
- Confidence thresholds
- Mapping profiles

## 12. API requirements

- REST/JSON for P0 with generated OpenAPI specification
- Cursor-based pagination for large queues
- Idempotency keys on ingestion, approval, and delivery operations
- Optimistic concurrency/version checks for configuration edits
- Signed webhooks with timestamp and replay protection
- Stable external IDs separate from internal database keys
- Request and correlation IDs across API, workflow, provider, and integration events

## 13. Evolution path

Begin as a modular monolith with separately deployable web, API, and worker processes. Extract a service only when one of these conditions exists:

- Independent scaling is repeatedly required.
- A customer deployment boundary requires it.
- Failure isolation materially improves reliability.
- A team requires independent ownership and release cadence.
- The module has a stable contract and operational maturity.

Do not create microservices solely to imitate enterprise architecture diagrams.
