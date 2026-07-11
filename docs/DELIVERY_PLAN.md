# Master Implementation and Delivery Plan

> **Status:** implementation contract
>
> **Audience:** product, design, engineering, security, QA, operations, and coding agents
>
> **Primary objective:** build a local-first, free-first, paid-ready, production-grade B2B intelligent document operations platform whose first complete solution converts customer purchase orders into validated, ERP-ready sales orders.

This document is the controlling build plan for the repository. It turns the product, architecture, AI/OCR, infrastructure, security, and UX requirements into an ordered implementation program. A developer or coding agent should be able to take one numbered work item, implement it, prove it with tests, and submit it without inventing missing product behavior.

Read this document together with:

- [`PRODUCT.md`](PRODUCT.md) for product boundaries and user outcomes.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system model and core entities.
- [`AI_OCR.md`](AI_OCR.md) for document understanding, evidence, confidence, and evaluation.
- [`INFRASTRUCTURE.md`](INFRASTRUCTURE.md) for local, free-tier, paid, and migration rules.
- [`SECURITY_OPERATIONS.md`](SECURITY_OPERATIONS.md) for security, reliability, privacy, and release gates.
- [`UI_UX_BLUEPRINT.md`](UI_UX_BLUEPRINT.md) for the application experience and visual-quality contract.
- [`BUILD_BACKLOG.md`](BUILD_BACKLOG.md) for executable task IDs, dependencies, acceptance criteria, and test requirements.
- [`DECISIONS.md`](DECISIONS.md) for settled architecture decisions and explicitly deferred choices.

---

## 1. Product mandate

### 1.1 Product promise

> Turn incoming customer purchase orders into validated, ERP-ready sales orders, with every value traceable to its source and every automated or human action auditable.

### 1.2 What is being built

SOA is not an upload form plus an LLM call. It is an operational system that:

1. Receives documents through web upload, email, API, and later SFTP/shared storage.
2. Preserves and secures the original file.
3. Extracts native text or applies OCR and layout analysis.
4. Classifies documents and splits combined packets.
5. Extracts header fields and line items into a versioned schema.
6. Normalizes values into canonical types.
7. Matches customer, ship-to, sold-to, material, UOM, price, and other master data.
8. Runs deterministic business validation.
9. Calculates calibrated, risk-aware confidence.
10. Routes only unresolved or high-risk fields to human review.
11. Records corrections, comments, approvals, and audit events.
12. Delivers approved data to an integration with idempotency, retries, and replay.
13. Measures accuracy, review effort, throughput, SLA, provider cost, and export reliability.

### 1.3 First complete use case

The first release must support one complete purchase-order-to-sales-order stream:

```text
Create organization
→ create workspace
→ create sales-order process
→ create country/location stream
→ configure schema, rules, catalogs, and provider policy
→ upload or email a purchase order
→ inspect and process the document asynchronously
→ extract header and line items
→ match customer, ship-to, and materials
→ validate critical values
→ review uncertain fields with source evidence
→ approve
→ export canonical JSON
→ deliver through a signed webhook or adapter
→ inspect the immutable processing and audit timeline
```

### 1.4 Platform path

The architecture must later support invoice processing, remittances, delivery notes, bills of lading, claims, customs documents, contracts, and other document processes without rebuilding tenancy, storage, review, orchestration, security, audit, or integration foundations.

---

## 2. Fixed constraints

The following constraints are requirements, not suggestions.

### 2.1 Local-first

The complete development application must run locally on a normal developer machine. Local operation must not require a cloud account for the core workflow.

Required local capabilities:

- Web application.
- API.
- Background worker.
- PostgreSQL.
- S3-compatible object storage.
- Durable job execution.
- Local email catcher.
- File scanning or a clearly marked development substitute.
- Native PDF extraction.
- Open-source OCR.
- Mock extraction provider.
- Optional local LLM through an OpenAI-compatible endpoint.
- Seeded organization, users, process, stream, catalogs, and sample documents.
- End-to-end tests.

### 2.2 Free-first, paid-ready

Development and a controlled demonstration should fit within free allowances where practical. Free services must sit behind stable contracts so upgrading a plan or changing a provider is an infrastructure and data-migration exercise, not a rewrite.

### 2.3 Production-grade foundations

Even before enterprise features are enabled, the design must include:

- Server-side tenant isolation.
- Strong authorization.
- Durable state.
- Idempotency.
- Auditability.
- Versioned configuration.
- Evidence for extracted values.
- Provider abstraction.
- Structured errors.
- Observability.
- Tested backup and restore on the production tier.
- Data export and deletion.
- Bounded resource use.

### 2.4 Premium UI/UX

The application must feel like a purpose-built operations product, not a generic component-library dashboard. Review speed, clarity, evidence, keyboard operation, density, accessibility, and error recovery are release criteria. The design contract is in `UI_UX_BLUEPRINT.md`.

### 2.5 No silent AI behavior

No AI or OCR result may be treated as truth without source evidence, schema validation, deterministic normalization, business validation, confidence policy, and an audit trail.

### 2.6 No customer-data shortcuts

Confidential customer documents may not be sent to free or consumer AI plans unless the applicable terms explicitly satisfy the customer's privacy, retention, training, and processing requirements.

---

## 3. Definition of production-ready

The product is production-ready only when all of the following are true for at least one real sales-order stream.

### 3.1 Functional completeness

- Organization, membership, roles, workspace, process, and stream management work.
- A document can enter through upload, email, and API.
- The original document is stored immutably and safely.
- Processing stages are asynchronous and visible.
- Header and line items are extracted with evidence.
- Catalog matching and deterministic validation run.
- Review, correction, approval, rejection, reopen, and comments work.
- Approved data can be exported and delivered.
- Failed delivery can be retried or replayed without re-extraction.
- Every important action appears in the timeline and audit log.

### 3.2 Quality

- Representative gold documents exist.
- Critical-field accuracy is measured.
- Line-item accuracy is measured.
- False auto-approval is below the agreed threshold.
- Provider, prompt, rule, schema, and matching changes run regression tests.
- End-to-end, integration, security, and accessibility suites pass.

### 3.3 Security and privacy

- Cross-tenant tests pass.
- File-type, page, pixel, decompression, and malware controls work.
- Secrets are managed outside source code.
- Logs exclude raw document content and sensitive field values.
- Prompt-injection tests pass.
- Retention, export, and deletion paths are tested.
- No unresolved critical security findings remain.

### 3.4 Reliability and operations

- Jobs survive worker termination.
- Every stage is idempotent or safely compensating.
- Provider timeouts, retries, circuit breakers, and fallback are tested.
- Database backups exist and a restore has succeeded.
- Object hashes reconcile after restore or migration.
- Alerts and runbooks exist for critical failure modes.
- Usage and cost can be reconciled by tenant and stream.

### 3.5 Commercial readiness

- Terms, privacy policy, DPA, subprocessor list, support model, and availability statements match the actual system.
- Provider contracts are suitable for customer data.
- No SLA depends on unsupported free-tier availability.

---

## 4. Delivery strategy

### 4.1 Build a vertical slice before breadth

The first engineering objective is not to create every administration screen. It is to prove the complete path from a document entering the system to approved canonical JSON leaving it.

The preferred sequence is:

1. Repository and local environment.
2. Identity and tenant boundary.
3. Process and stream configuration.
4. Upload and immutable storage.
5. Durable processing state.
6. Native PDF extraction and mock extraction.
7. Evidence model.
8. Validation and review.
9. Approval and JSON delivery.
10. Audit timeline.
11. Real OCR and LLM adapters.
12. Catalog matching, email intake, analytics, and ERP depth.

### 4.2 Avoid parallel feature islands

A feature is not complete if it exists only as a UI mock or isolated API. Each milestone must end in a demonstrable user journey with real persistence, authorization, error handling, tests, and instrumentation.

### 4.3 Use feature flags

Incomplete or risky capabilities must be controlled through typed, auditable feature flags. Flags may be global, organization-specific, or stream-specific. A flag must have an owner, purpose, default, expiration/review date, and removal task.

### 4.4 Use one canonical contract

OCR/LLM output, review state, and ERP-specific payloads must not become the domain model. All document variants map into a versioned canonical sales-order contract. Integrations map from that contract.

---

## 5. Technical stack

Versions must be pinned in lockfiles and upgraded through explicit dependency PRs. Use currently supported stable releases at implementation time.

### 5.1 Monorepo and tooling

- Git repository with protected `main`.
- `pnpm` workspaces for TypeScript packages.
- `uv` for Python dependency and environment management.
- Task runner through root scripts; use Turborepo only if it materially simplifies caching and orchestration.
- Pre-commit hooks for format, lint, generated-file drift, secret checks, and migration checks.
- Conventional commit style is preferred but not required for every local commit; PR titles must be clear and release-friendly.

### 5.2 Frontend

- React and TypeScript.
- Vite for fast local builds and portable static deployment.
- TanStack Router for typed routes.
- TanStack Query for server state.
- TanStack Table and Virtual for large operational tables.
- React Hook Form with Zod schemas for forms.
- React Aria Components and selected low-level primitives for accessibility.
- Tailwind CSS or equivalent token-driven utility layer; visual design must be custom rather than stock templates.
- PDF.js for document rendering, search, selection, and coordinate overlays.
- i18next or equivalent for UI localization.
- MSW for deterministic API mocks.
- Vitest and Testing Library for component/unit tests.
- Playwright for end-to-end and visual regression tests.

### 5.3 Backend

- Python with FastAPI.
- Pydantic for request, response, provider, and canonical schemas.
- SQLAlchemy and Alembic.
- PostgreSQL driver with async support.
- `httpx` for provider and integration HTTP.
- Structured logging.
- OpenTelemetry instrumentation.
- Explicit service/repository boundaries; HTTP handlers must not contain domain logic.

### 5.4 Database

- Standard PostgreSQL is canonical.
- Use portable migrations.
- Recommended extensions: `pg_trgm` and `unaccent`; `pgvector` only when a measured use case requires it.
- Avoid provider-specific database features in core correctness paths.
- Use application-generated sortable UUIDs when practical; store them as PostgreSQL `uuid`.
- All timestamps are `timestamptz` in UTC.
- Monetary values use decimal/numeric plus an ISO currency code.
- User-facing sequence numbers are separate from primary keys.

### 5.5 Durable jobs

Begin with a PostgreSQL-backed job and outbox implementation so local and cloud behavior are the same and no essential state lives only in Redis or a provider queue.

Required mechanics:

- `jobs` table with type, payload, status, priority, `run_after`, attempt count, maximum attempts, dedupe key, lock owner, lock expiration, correlation ID, and safe error summary.
- Workers claim jobs with `FOR UPDATE SKIP LOCKED`.
- Heartbeats extend locks for long operations.
- Expired locks return to the queue.
- Jobs are idempotent through dedupe keys and stage-run uniqueness constraints.
- Transactional outbox publishes follow-up jobs and events only after database commits.
- Dead-letter state is inspectable and replayable.

A dedicated workflow engine may replace the scheduler later behind `WorkflowEngine`; workflow state and business logic must remain portable.

### 5.6 Object storage

- Local: MinIO or filesystem adapter for tests.
- Shared/production: S3-compatible storage.
- API depends on `ObjectStore`, not a vendor SDK.
- Original files are immutable.
- Derived artifacts are versioned and content-addressed where practical.
- Signed URLs are short-lived and tenant-scoped.

### 5.7 Authentication

- Production: generic OIDC/JWT integration with a configured identity provider.
- Local: deterministic development identity mode and optional local OIDC profile.
- Production startup must fail if development authentication is enabled.
- Authorization remains application-owned through organization memberships, roles, permissions, and resource scope.

### 5.8 Document tooling

- PyMuPDF or equivalent for native PDF text and coordinates.
- PDFium/Poppler-compatible rendering isolated in a sandboxed process/container.
- OpenCV for image preprocessing.
- Tesseract as the baseline local OCR adapter.
- PaddleOCR or another layout-capable open-source adapter as an optional local provider.
- Managed OCR adapters added behind the same contract.

### 5.9 LLM/extraction providers

- Provider-neutral `ExtractionProvider` contract.
- Dedicated adapters for selected hosted providers.
- OpenAI-compatible local adapter for Ollama/vLLM or similar.
- Deterministic mock provider for development and tests.
- Every call uses a JSON Schema or equivalent typed output contract.
- Provider request and response metadata are recorded without leaking sensitive content to ordinary logs.

### 5.10 Local supporting services

- Mailpit for email testing.
- ClamAV container or a development scanner adapter that makes its reduced guarantees explicit.
- Optional OpenTelemetry/Grafana local profile.
- Optional local LLM profile.

---

## 6. Target repository layout

```text
soa/
├── apps/
│   ├── web/
│   │   ├── src/app/                 # app shell, providers, routes
│   │   ├── src/features/            # feature-owned UI and hooks
│   │   ├── src/components/          # shared composites
│   │   ├── src/lib/                 # API, auth, telemetry, utilities
│   │   └── src/test/
│   ├── api/
│   │   └── soa_api/
│   │       ├── main.py
│   │       ├── http/                # routers, dependencies, error mapping
│   │       ├── application/         # use cases and orchestration
│   │       ├── domain/              # entities, value objects, policies
│   │       ├── infrastructure/      # database, storage, auth, providers
│   │       └── telemetry/
│   └── worker/
│       └── soa_worker/
│           ├── main.py
│           ├── scheduler.py
│           ├── handlers/
│           └── heartbeat.py
├── packages/
│   ├── contracts/                   # OpenAPI-derived TS client and JSON schemas
│   ├── design-system/               # tokens, primitives, patterns, Storybook
│   ├── provider-contracts/          # provider DTOs and shared fixtures
│   ├── canonical-order/             # versioned sales-order schema
│   ├── config/                      # shared lint/build/test config
│   └── test-fixtures/               # synthetic documents and expected output
├── migrations/
├── infrastructure/
│   ├── local/
│   ├── cloud/
│   ├── scripts/
│   └── runbooks/
├── tests/
│   ├── integration/
│   ├── end-to-end/
│   ├── security/
│   ├── performance/
│   └── evaluations/
├── docs/
├── docker-compose.yml
├── Makefile or justfile
├── pnpm-workspace.yaml
├── pyproject.toml
├── .env.example
└── README.md
```

### 6.1 Ownership rule

A feature owns its UI, API use cases, domain rules, tests, and documentation changes. Shared packages must not become dumping grounds. New cross-cutting abstractions require at least two proven consumers or a clear security/portability need.

---

## 7. Local development contract

### 7.1 Required commands

The repository must expose a small, stable command surface:

```bash
make bootstrap       # install toolchains/dependencies and copy env template
make local-up        # start PostgreSQL, storage, mail, scanner, optional telemetry
make migrate         # apply database migrations
make seed            # load deterministic demo tenant and sample data
make dev             # run web, API, and worker with reload
make test            # fast unit and component tests
make test-integration
make test-e2e
make test-security
make eval            # golden document evaluation
make lint
make format
make local-down
```

Equivalent `just` or package scripts are acceptable, but the README and CI must use the same commands.

### 7.2 Local ports

Document and reserve default ports in `.env.example`. Avoid common collisions and allow overrides. At minimum expose:

- Web.
- API.
- PostgreSQL.
- MinIO API and console.
- Mailpit SMTP and UI.
- Scanner service.
- Optional telemetry UI.
- Optional local model endpoint.

### 7.3 Seed data

The seed command must create:

- Demo organization: `Northstar Distribution`.
- Demo workspace: `Europe`.
- Process: `Sales Orders`.
- Streams: `United Kingdom` and `Spain`.
- Users for admin, reviewer, supervisor, integration admin, and auditor roles.
- Customer, ship-to, material, UOM, and price catalogs.
- A published schema and deterministic validation rules.
- A mock provider policy.
- At least five synthetic purchase orders covering success, low confidence, validation failure, multi-page lines, and duplicate PO behavior.

### 7.4 Offline behavior

The default test suite must not call external providers. Network access in provider tests must be mocked or blocked. Live provider tests run only through an explicit opt-in profile and never in ordinary pull-request CI.

---

## 8. Domain and data implementation

`ARCHITECTURE.md` defines entities. This section specifies implementation rules.

### 8.1 Common columns

Every mutable business table should include, where applicable:

```text
id uuid primary key
organization_id uuid not null
created_at timestamptz not null
created_by uuid/null
updated_at timestamptz not null
updated_by uuid/null
version integer not null default 1
archived_at timestamptz/null
```

Do not use a generic soft-delete flag for immutable evidence, audit events, processing runs, or delivery attempts.

### 8.2 Tenant root

#### `organizations`

Key fields:

- `id`, `slug`, `name`, `status`.
- `default_locale`, `default_timezone`, `default_currency`.
- `data_region`.
- `retention_policy_id`.
- `plan_code`, `quota_policy_id`.
- `support_access_policy`.

Constraints:

- Slug unique globally.
- Status is `trial`, `active`, `suspended`, `closing`, or `closed`.
- Suspended organizations may read/export according to policy but may not ingest or process.

#### `users`, `memberships`, `roles`, `permissions`

- A user identity is global; authorization is membership-specific.
- One user may belong to several organizations.
- Roles may be system templates or organization-defined.
- Permission checks use explicit actions such as `document.read`, `document.review`, `stream.configure`, `integration.replay`.
- Membership status is `invited`, `active`, `suspended`, or `removed`.

### 8.3 Product hierarchy

#### `workspaces`

Optional business-unit grouping under an organization.

#### `processes` and `process_versions`

- Process is the stable identity.
- Process version is immutable after publication.
- Draft versions may be edited with optimistic concurrency.
- Published versions contain resolved references to schema, workflow, rules, and defaults.

#### `streams` and `stream_versions`

- Stream is the active operational bucket.
- Stream version stores explicit overrides and a resolved immutable configuration snapshot.
- A run references the exact stream version used.
- Publishing must validate inheritance, required providers, catalog compatibility, and integration mappings.

### 8.4 Configuration versions

Use stable root records plus immutable version records for:

- Schemas.
- Workflows.
- Rule sets.
- Provider policies.
- Matching policies.
- Confidence policies.
- Mapping profiles.
- Retention policies.

Version lifecycle:

```text
draft → testing → approved → published → superseded
                         ↘ rejected
```

A published version is never mutated. Rollback changes the active pointer to a previously published version while recording a new decision event.

### 8.5 Documents and artifacts

#### `documents`

Key fields:

- Tenant, workspace, process, stream, and input source.
- External reference and client idempotency key.
- Original filename, detected media type, byte size, content hash.
- Page count and detected language.
- Current projected state.
- Priority, SLA due time, and retention class.
- Duplicate-of reference where applicable.

#### `artifacts`

Store metadata for originals and derivatives:

- Kind: original, page image, thumbnail, native text, OCR JSON, layout JSON, raw provider response, canonical extraction, export payload, audit bundle.
- Object key, hash, size, media type, encryption metadata, producer stage, run ID, retention class.

Original artifacts are immutable. Derived artifacts are immutable within a run.

### 8.6 Processing

#### `processing_runs`

- One document may have many runs.
- Record trigger, resolved configuration snapshot, status, start/end, cost, and correlation ID.
- A reprocess creates a new run; it does not overwrite history.

#### `stage_runs`

- Unique on `(processing_run_id, stage_name, input_fingerprint)`.
- Store attempt, provider, start/end, status, output artifact IDs, safe error, retry decision, and metrics.

#### `extracted_fields`

- Store field path, raw value, normalized value, type, row identity for line items, evidence, selected candidate, platform confidence, criticality, validation summary, and review state.
- Preserve all candidates when they matter to review or explanation.

### 8.7 Reference data

#### `catalogs`, `catalog_versions`, `catalog_records`

- Catalog versions are immutable after activation.
- Records support effective start/end dates and source-system IDs.
- Imports produce a validation report before activation.
- Matching indexes are generated asynchronously.
- A stream version pins catalog versions or an explicitly allowed rolling policy.

### 8.8 Review

#### `review_tasks`

Fields include queue, assignee, priority, SLA, reason codes, status, opened/claimed/completed timestamps, and lock/version.

Task states:

```text
unassigned → assigned → in_review → approved | rejected | blocked
                                      ↘ reopened
```

#### `field_corrections`

Append-only record containing old value, new value, evidence selection, reason, reviewer, timestamp, and UI/client version.

### 8.9 Delivery

#### `export_jobs` and `delivery_attempts`

- Export job references an approved canonical payload and mapping version.
- Idempotency key is stable across retries.
- Every network attempt is recorded separately.
- Responses are redacted before storage where necessary.
- Replay uses the same approved payload unless an authorized user explicitly creates a new export version.

### 8.10 Audit

`audit_events` are append-oriented and tamper-evident at the application level. Record:

- Tenant.
- Actor type and ID.
- Action.
- Target type and ID.
- Timestamp.
- Correlation ID.
- Source channel/IP/device where appropriate.
- Safe before/after summary.
- Reason or approval context.

Audit events are not an alternative to domain history; both are required.

---

## 9. API contract

### 9.1 General rules

- Base path `/api/v1`.
- JSON request and response bodies.
- OpenAPI generated from source and checked for drift.
- Cursor pagination.
- Idempotency headers on create/ingest/approve/deliver operations.
- ETag or explicit version field for mutable configuration.
- Correlation ID returned on every response.
- Never expose internal stack traces.

Error envelope:

```json
{
  "error": {
    "code": "DOCUMENT_NOT_REVIEWABLE",
    "message": "This document is not currently available for review.",
    "details": {
      "current_state": "processing"
    },
    "correlation_id": "..."
  }
}
```

### 9.2 Identity and organization

```text
GET    /me
GET    /organizations
POST   /organizations
GET    /organizations/{organization_id}
PATCH  /organizations/{organization_id}
GET    /organizations/{organization_id}/memberships
POST   /organizations/{organization_id}/invitations
PATCH  /memberships/{membership_id}
GET    /roles
POST   /roles
PATCH  /roles/{role_id}
POST   /service-credentials
DELETE /service-credentials/{credential_id}
```

### 9.3 Workspaces, processes, and streams

```text
GET/POST       /workspaces
GET/PATCH      /workspaces/{workspace_id}
GET/POST       /processes
GET/PATCH      /processes/{process_id}
POST           /processes/{process_id}/versions
POST           /process-versions/{version_id}/test
POST           /process-versions/{version_id}/publish
POST           /processes/{process_id}/rollback
GET/POST       /streams
GET/PATCH      /streams/{stream_id}
POST           /streams/{stream_id}/clone
POST           /streams/{stream_id}/versions
POST           /stream-versions/{version_id}/simulate
POST           /stream-versions/{version_id}/publish
```

### 9.4 Documents

```text
POST   /documents/upload-sessions
POST   /documents/upload-sessions/{id}/complete
POST   /documents:ingest
GET    /documents
GET    /documents/{document_id}
GET    /documents/{document_id}/timeline
GET    /documents/{document_id}/artifacts
POST   /documents/{document_id}/reprocess
POST   /documents/{document_id}/cancel
POST   /documents/{document_id}/archive
GET    /documents/{document_id}/download-url
```

Upload flow:

1. Client requests a session with filename, size, hash if known, stream, and idempotency key.
2. API authorizes and returns a signed object-store upload target.
3. Client uploads directly.
4. Client completes the session.
5. API verifies object metadata/hash, creates the document, writes audit/outbox events, and schedules scanning.

### 9.5 Review

```text
GET    /review-tasks
POST   /review-tasks/{task_id}/claim
POST   /review-tasks/{task_id}/release
GET    /review-tasks/{task_id}
PATCH  /review-tasks/{task_id}/fields
POST   /review-tasks/{task_id}/comments
POST   /review-tasks/{task_id}/approve
POST   /review-tasks/{task_id}/reject
POST   /review-tasks/{task_id}/block
POST   /review-tasks/{task_id}/reopen
```

Review updates require optimistic concurrency. A conflict returns the current version and a safe merge path; it must never silently overwrite another reviewer's work.

### 9.6 Catalogs

```text
GET/POST  /catalogs
GET       /catalogs/{catalog_id}
POST      /catalogs/{catalog_id}/imports
GET       /catalog-imports/{import_id}
POST      /catalog-imports/{import_id}/activate
GET       /catalogs/{catalog_id}/versions
GET       /catalogs/{catalog_id}/records
POST      /catalogs/{catalog_id}/match-preview
```

### 9.7 Integrations

```text
GET/POST  /integrations
GET/PATCH /integrations/{integration_id}
POST      /integrations/{integration_id}/test
GET/POST  /mapping-profiles
POST      /mapping-profiles/{profile_id}/validate
GET       /export-jobs
GET       /export-jobs/{export_job_id}
POST      /export-jobs/{export_job_id}/retry
POST      /export-jobs/{export_job_id}/replay
```

### 9.8 Analytics and audit

```text
GET /analytics/operations
GET /analytics/quality
GET /analytics/cost
GET /analytics/review
GET /usage
GET /audit-events
POST /audit-exports
```

### 9.9 Internal worker endpoints

Prefer direct database/job coordination. If internal HTTP is necessary, require service authentication, tenant context, correlation ID, idempotency key, strict network policy, and separate internal routes.

---

## 10. Document-processing workflow

### 10.1 Stages

```text
register
→ verify_upload
→ malware_scan
→ inspect_file
→ render_pages
→ extract_native_text
→ assess_text_quality
→ preprocess_images
→ run_ocr
→ classify
→ split_or_assemble
→ extract_schema
→ normalize
→ match_reference_data
→ validate_business_rules
→ calculate_confidence
→ route_decision
→ create_review_task | auto_approve
→ map_export
→ deliver
→ finalize
```

Not every document runs every stage. Stage selection is deterministic from file characteristics and published stream policy.

### 10.2 Stage contract

Every stage handler accepts:

```text
organization_id
stream_id
processing_run_id
stage_name
input_artifact_ids
resolved_configuration_ids
input_fingerprint
correlation_id
attempt
```

It returns:

```text
status
output_artifact_ids
structured_summary
metrics
cost
warnings
next_stage_events
```

### 10.3 Idempotency

- Compute an input fingerprint from stage name, artifact hashes, and configuration version IDs.
- Enforce uniqueness on run, stage, and fingerprint.
- A repeated call returns the existing successful result.
- External calls use provider idempotency when available and record request IDs.
- Delivery uses a stable business idempotency key independent of network attempts.

### 10.4 Retry policy

Classify failures:

- `transient_provider` — bounded exponential backoff.
- `transient_platform` — retry after health recovery.
- `rate_limited` — respect provider retry hints and stream budget.
- `invalid_input` — terminal or review/quarantine.
- `schema_invalid` — bounded repair, fallback provider, then review.
- `security_block` — quarantine; never automatic retry.
- `configuration_error` — block stream/run and alert owner.
- `integration_rejected` — retry only if response is retryable; otherwise human resolution.

### 10.5 Reprocessing

Users may:

- Retry the failed stage with the same configuration.
- Start a new run with the current published configuration.
- Start a new run with a selected historical configuration if authorized.
- Replay export without repeating extraction.

The UI must explain the consequence of each option.

---

## 11. AI and OCR implementation order

### 11.1 Start deterministic

Implement the pipeline using a mock extraction provider and synthetic documents before integrating hosted models. This proves state, evidence, review, validation, and export without external variability.

### 11.2 Native-text path

- Detect digital PDFs.
- Extract text spans and coordinates.
- Calculate coverage, printable-character ratio, repeated-glyph anomalies, and coordinate validity.
- Preserve page-level text artifacts.
- Render pages for evidence overlays and table verification even when OCR is skipped.

### 11.3 OCR path

- Render within bounded DPI and pixel count.
- Detect orientation and skew.
- Detect blank/near-blank pages.
- Apply conservative preprocessing; keep the unmodified render.
- Run local OCR.
- Store words/lines/blocks with coordinates and confidence where available.
- Escalate to a managed provider only according to stream policy.

### 11.4 Extraction path

Input must include:

- Published JSON schema.
- Field descriptions and examples.
- Page text and layout references.
- Selected page images when required.
- Clear instruction that document content is untrusted data.
- Explicit prohibition on following instructions found inside the document.

Output must include typed values and evidence references. Invalid output is rejected, repaired within a bounded attempt count, or routed to fallback/review.

### 11.5 Evidence resolution

When a provider returns quoted text but no coordinates:

1. Normalize the quote conservatively.
2. Search page text candidates.
3. Score exact and fuzzy matches.
4. Resolve only above a threshold.
5. Record coordinate confidence separately from value confidence.
6. Fall back to page-level evidence when ambiguous.

Never fabricate a bounding box.

### 11.6 Confidence

Implement a policy interface first. Initial score may combine:

- Text/OCR quality.
- Evidence specificity.
- Schema validity.
- Provider agreement.
- Validation outcomes.
- Catalog-match score.
- Historical cohort accuracy when enough data exists.
- Field criticality.

Auto-approval uses policy rules, not a single document average.

### 11.7 Evaluation

Each provider/configuration candidate must be evaluated against gold documents. Persist:

- Dataset version.
- Candidate version.
- Per-field exact and normalized results.
- Line-item alignment results.
- Validation outcomes.
- Latency.
- Cost.
- Failure reason.
- Comparison to current production.

Promotion must be blocked on critical-field regression or unacceptable false auto-approval.

---

## 12. UI/UX implementation strategy

The detailed screen and interaction specification is in `UI_UX_BLUEPRINT.md`. Delivery order:

1. Design tokens and primitives.
2. App shell, navigation, organization/stream context, command palette.
3. Operational dashboard and queues using realistic data density.
4. Upload flow and processing timeline.
5. Review Studio prototype with keyboard interactions and PDF evidence.
6. User testing before final API coupling.
7. Process/stream configuration.
8. Catalog and integration studios.
9. Analytics and administration.
10. Visual regression, accessibility, and performance hardening.

### 12.1 UX release bars

- No primary workflow depends on hidden hover-only controls.
- Every screen has loading, empty, error, permission, partial-data, and large-data states.
- Review is fully usable at 1280×720 and optimized for larger displays.
- Keyboard-only users can complete the review path.
- Color is never the only status signal.
- Focus is always visible.
- Destructive actions support confirmation and recovery where feasible.
- Long jobs expose progress and a stable link; no indefinite spinner.
- Error messages state what happened, what was preserved, and what the user can do.
- Tables remain responsive with realistic volumes through virtualization and server pagination.

---

## 13. Security implementation plan

### 13.1 Authorization

- Define a permission matrix before endpoint implementation.
- Every application use case receives an authenticated principal and resource scope.
- Repositories require organization scope explicitly; no unscoped list functions in production code.
- Workers revalidate tenant and resource ownership from trusted IDs.
- Signed URLs are generated only after authorization and expire quickly.

### 13.2 File security

- Verify actual content type.
- Reject unsupported formats before conversion.
- Apply size, page, pixel, archive, and timeout limits.
- Scan before processing.
- Run converters with restricted filesystem, no network, low privileges, and resource limits.
- Quarantine suspicious files and show a non-sensitive reason.

### 13.3 Web security

- Secure headers and Content Security Policy.
- CSRF controls where cookie sessions are used.
- Strict CORS allowlist.
- Rate limiting by IP, principal, tenant, and sensitive operation.
- Safe redirect handling.
- No secrets or provider keys in the browser.

### 13.4 AI security

- Document content never controls tools, URLs, credentials, or system instructions.
- No model tool use in the extraction path.
- Strict output schemas and size limits.
- Provider allowlist and approved model registry.
- Tenant-specific requests, examples, caches, and audit metadata.
- Prompt-injection corpus in security/evaluation tests.

### 13.5 Supply chain

- Lock dependencies.
- Automated dependency and container scanning.
- Secret scanning.
- SBOM per release.
- Signed container images where deployment supports it.
- Review base-image provenance and update cadence.

---

## 14. Observability and operations

### 14.1 Correlation

Generate a correlation ID at ingress and propagate it through API requests, jobs, stage runs, provider calls, review changes, and delivery attempts.

### 14.2 Metrics

At minimum:

- HTTP request count, latency, and error class.
- Job queue depth and oldest age.
- Stage latency and failure rate.
- Documents/pages processed.
- OCR and extraction calls, latency, fallback, and cost.
- Schema-invalid and repair rate.
- Review queue size, age, and completion time.
- Approval/rejection/reopen rate.
- Export success, retry, and terminal failure.
- Database pool/latency and storage quota.
- Tenant quotas and budget consumption.

### 14.3 Logs

Structured fields should include timestamp, severity, service, environment, correlation ID, tenant ID where safe, resource ID, operation, result, and safe error code. Do not log raw file contents, extracted values, tokens, credentials, signed URLs, or full provider payloads.

### 14.4 Traces

Trace major API operations, job claims, stage execution, provider calls, database operations, object-store calls, and delivery attempts. Sampling must preserve errors and slow traces.

### 14.5 Runbooks

Each production alert links to a runbook containing detection, impact, owner, containment, recovery, verification, communication, and follow-up.

---

## 15. Testing strategy

### 15.1 Unit tests

Required for:

- Value objects and canonical schema.
- Normalization.
- Rule evaluation.
- Matching scores.
- Confidence policy.
- State transitions.
- Authorization decisions.
- Idempotency fingerprints.
- Provider response parsing.
- Mapping transformations.

### 15.2 Component tests

Required for:

- Forms and validation.
- Tables and filters.
- Permission states.
- Evidence overlay behavior.
- Review field editor.
- Line-item grid.
- Conflict handling.
- Error and empty states.

### 15.3 Integration tests

Run against real local PostgreSQL and object storage for:

- Upload session and verification.
- Job claiming, heartbeat, retry, and recovery.
- Artifact storage and signed URLs.
- Full mock document pipeline.
- Catalog import and activation.
- Review save and optimistic conflict.
- Approval and export.
- Webhook signing and replay prevention.

### 15.4 End-to-end tests

Critical path:

1. Sign in as admin.
2. Create or select organization/process/stream.
3. Upload synthetic PO.
4. Observe processing.
5. Open review task.
6. Correct a low-confidence ship-to field using highlighted evidence.
7. Resolve a material candidate.
8. Approve.
9. Inspect successful export.
10. Inspect timeline/audit.

Additional E2E tests cover rejection, failure/retry, concurrent review, permissions, and deletion/export requests.

### 15.5 Security tests

- Cross-tenant API access.
- Cross-tenant object URL access.
- Job payload tenant tampering.
- Cache-key isolation.
- API-key scope and expiry.
- CSRF/CORS/security headers.
- Upload type confusion and archive bombs.
- Prompt-injection documents.
- Sensitive-data log assertions.

### 15.6 Performance tests

Define and test representative workloads:

- Queue listing with large volume.
- Document detail with many pages.
- Review with hundreds of line-item cells.
- Bulk upload.
- Concurrent worker claims.
- Large catalog matching.
- Export bursts.

### 15.7 Accessibility and visual tests

- Automated accessibility checks in component and E2E suites.
- Manual keyboard and screen-reader smoke test for critical paths.
- Visual snapshots for app shell, queue, review, forms, dialogs, errors, and responsive breakpoints.
- Reduced-motion behavior.

---

## 16. CI/CD

### 16.1 Pull-request pipeline

Run:

1. Formatting and lint.
2. Type checking.
3. Unit and component tests.
4. Migration validation from clean and previous schema.
5. Integration tests.
6. OpenAPI/client generation drift check.
7. Security/secret/dependency scans.
8. Build web, API, and worker artifacts.
9. Selected E2E smoke tests.
10. Documentation-link check.

Evaluation suites may use a small deterministic PR subset; the full gold suite runs on release candidates and model/configuration changes.

### 16.2 Deployment

- Build immutable web and container artifacts once.
- Promote the same artifact across environments.
- Apply migrations through a controlled job before application rollout.
- Use readiness and liveness checks.
- Roll out incrementally where supported.
- Verify smoke tests, queue health, and error rate.
- Record deployment evidence.

### 16.3 Rollback

- Application rollback must not require schema rollback for ordinary releases.
- Prefer expand/migrate/contract database changes.
- Keep the previous deployable artifact available.
- Configuration versions support immediate pointer rollback.
- Provider changes are feature-flagged and reversible.

---

## 17. Environment and upgrade model

### 17.1 Local

All core services on the developer machine. Mock or local AI by default.

### 17.2 Shared development

Free allowances may host web, API, worker, database, storage, email, and telemetry. Only synthetic/non-confidential documents may use provider plans without production-suitable data terms.

### 17.3 Staging

Mirrors production architecture and security settings at smaller capacity. Uses production-like authentication, backups, monitoring, and paid AI terms where real customer documents are present.

### 17.4 Production

Paid plans sized to contractual requirements. Production must not rely on pausing databases, ephemeral disks, unsupported commercial hosting, or consumer AI terms.

### 17.5 Upgrade procedure

For a same-provider free-to-paid change:

1. Review plan features and contractual terms.
2. Enable billing and required capacity.
3. Enable backups/PITR, retention, support, and alerts.
4. Validate endpoints and credentials.
5. Run smoke, restore, and quota tests.
6. Update cost budgets and operational documentation.

For a provider migration:

1. Provision destination through infrastructure code.
2. Apply migrations/configuration.
3. Copy data and objects.
4. Reconcile row counts, hashes, and artifact manifests.
5. Rotate secrets.
6. Run gold and E2E suites.
7. Shadow or dual-write only when safely designed.
8. Cut over behind configuration.
9. Monitor.
10. Retain rollback window and remove old data according to policy.

---

## 18. Implementation phases

The detailed task list is in `BUILD_BACKLOG.md`. Phases are dependency-ordered; some UI and infrastructure work may overlap once contracts are stable.

### Phase 0 — Product and architecture lock

Deliverables:

- Canonical documentation.
- Decision log.
- Build backlog.
- Information architecture and UI blueprint.
- Threat model outline.
- Canonical sales-order schema draft.
- Local/cloud portability decisions.

Exit gate:

- No unresolved decision blocks Phase 1.
- Scope and production definition are explicit.

### Phase 1 — Repository and local platform

Deliverables:

- Monorepo.
- Web/API/worker skeletons.
- PostgreSQL migrations.
- Object-store adapter.
- Durable job/outbox skeleton.
- Local Docker environment.
- Seed data.
- CI baseline.
- Logging, correlation, health endpoints.

Exit gate:

- `make bootstrap && make local-up && make seed && make dev` produces a working local application.
- CI builds and tests all three processes.

### Phase 2 — Identity, tenancy, and application shell

Deliverables:

- OIDC integration and secure local mode.
- Organizations, memberships, roles, permissions.
- Tenant-scoped repositories.
- Cross-tenant tests.
- Premium app shell, navigation, organization/stream context, command palette.

Exit gate:

- Users can only access authorized organizations and actions.
- The app shell passes accessibility and visual review.

### Phase 3 — Processes, streams, and versioned configuration

Deliverables:

- Workspaces, processes, streams.
- Schema versions.
- Rule/provider/confidence/mapping configuration roots and versions.
- Inheritance and resolved snapshots.
- Draft/test/publish/rollback.
- Admin UI.

Exit gate:

- A published stream version is immutable and reproducible.
- Publishing catches invalid configuration.

### Phase 4 — Ingestion and immutable storage

Deliverables:

- Direct upload session.
- Hash/media verification.
- Malware/file limits.
- Artifact model.
- Duplicate detection.
- Document list/detail/timeline skeleton.

Exit gate:

- Authorized users upload supported files; originals are immutable and queued exactly once.

### Phase 5 — Durable pipeline and mock extraction

Deliverables:

- Job claiming/heartbeat/recovery.
- Processing and stage runs.
- Page rendering.
- Deterministic mock extractor.
- Evidence records.
- Normalization and basic validation.
- Visible processing states and retry.

Exit gate:

- A synthetic PO completes the full pipeline without external services.
- Killing a worker does not lose the document.

### Phase 6 — Review Studio and approval

Deliverables:

- Review queues and task claiming.
- PDF viewer with evidence overlays.
- Header and line-item editors.
- Keyboard navigation.
- Validation explanations and candidates.
- Comments, approve, reject, block, reopen.
- Concurrency protection.

Exit gate:

- A reviewer completes the critical path faster than baseline manual entry in usability testing.

### Phase 7 — Canonical export and integration reliability

Deliverables:

- Canonical sales-order schema.
- Mapping profiles.
- JSON/CSV export.
- Signed webhook.
- Export jobs, attempts, retries, replay.
- Delivery UI and runbook.

Exit gate:

- Approval produces one stable payload and exactly-once business behavior at the receiver boundary.

### Phase 8 — Real OCR, LLM, and provider routing

Deliverables:

- Native PDF adapter.
- Local OCR adapter.
- Hosted OCR fallback adapter.
- Hosted LLM adapters.
- Local OpenAI-compatible adapter.
- Routing, budgets, timeout, repair, and fallback.
- Provider registry and configuration UI.

Exit gate:

- Providers can be switched by configuration.
- Gold tests capture quality, latency, and cost.

### Phase 9 — Catalogs, matching, and business validation

Deliverables:

- CSV/XLSX import.
- Catalog versioning/activation.
- Exact, normalized, fuzzy, and weighted matching.
- Match explanations.
- Deterministic rule engine.
- Sales-order validation set.

Exit gate:

- Customer, ship-to, material, UOM, price, and duplicate checks work with traceable decisions.

### Phase 10 — Additional ingestion, analytics, and administration

Deliverables:

- Email intake.
- API keys and API ingestion.
- Operational, quality, review, and cost dashboards.
- Usage/quota controls.
- Internal support console with audited access.
- Audit export.

Exit gate:

- Operations can monitor and resolve a stream without database access.

### Phase 11 — Hardening and pilot readiness

Deliverables:

- Full threat model.
- Penetration test remediation.
- Load and resilience tests.
- Backup/restore rehearsal.
- Provider outage and migration rehearsals.
- Privacy export/deletion.
- Legal/security package.
- Release evidence and runbooks.

Exit gate:

- Every production gate in this document and `SECURITY_OPERATIONS.md` passes.

### Phase 12 — Controlled paid pilot

Deliverables:

- One customer stream configured with real master data.
- Historical parallel run.
- Human-supervised approval.
- Daily quality and incident review.
- ROI, accuracy, and operations report.
- Expansion decision.

Exit gate:

- Agreed accuracy, review time, export reliability, and operational stability are achieved before increasing automation or adding streams.

---

## 19. Agent execution protocol

This section is specifically intended to let a capable coding model implement the plan safely.

### 19.1 Work-unit rule

Implement one backlog task or one tightly coupled task group per pull request. Do not combine unrelated infrastructure, UI, schema, and provider changes.

### 19.2 Before coding

For each task:

1. Read the relevant canonical documents.
2. Identify dependencies and existing interfaces.
3. Restate the task's acceptance criteria in the PR description.
4. Identify migrations, security impact, UI states, telemetry, tests, and documentation changes.
5. Prefer existing patterns; do not introduce a new framework for convenience.

### 19.3 During implementation

- Keep HTTP handlers thin.
- Keep domain logic deterministic and testable.
- Do not bypass tenant-scoped repositories.
- Do not call provider SDKs outside adapters.
- Do not store essential state only in memory.
- Do not hide errors behind generic success states.
- Do not add placeholder UI without loading, error, empty, permission, and accessibility behavior.
- Add correlation and metrics to new long-running or external operations.
- Update generated API clients and schemas in the same PR.

### 19.4 Required PR contents

Every implementation PR should include:

- Summary.
- Linked task IDs.
- User-visible behavior.
- Architecture/security notes.
- Migration notes.
- Test evidence.
- Screenshots or recordings for UI changes.
- Accessibility notes for interactive UI.
- Rollback/feature-flag plan.
- Known limitations.

### 19.5 Prohibited agent behavior

A coding agent must not:

- Change the canonical product hierarchy without an ADR.
- Replace PostgreSQL or object-storage contracts without an ADR.
- Introduce a direct dependency on one OCR/LLM vendor in domain code.
- Disable or weaken tests to make CI pass.
- invent production credentials or commit secrets.
- claim production readiness without evidence.
- auto-approve critical fields merely because an LLM returned high confidence.
- fabricate source coordinates.
- log provider prompts/responses containing customer content to ordinary logs.
- silently change a published configuration version.

### 19.6 Completion response

When a task is complete, report:

- Files changed.
- Behavior delivered.
- Tests run and results.
- Migrations.
- Security implications.
- Remaining risks or follow-up tasks.

---

## 20. Global acceptance gates

### Gate A — Local developer experience

- Fresh clone setup is documented and repeatable.
- No cloud account is required for the mock vertical slice.
- Seed and sample workflow are deterministic.
- Local services have health checks and clear errors.

### Gate B — Product vertical slice

- One complete PO travels from upload through export.
- Evidence, validation, review, approval, and timeline are real.
- Failure and retry paths are usable.

### Gate C — Design quality

- Required screens pass design review against `UI_UX_BLUEPRINT.md`.
- Critical workflows pass moderated usability tests.
- Accessibility checks pass.
- Visual regressions are controlled.
- Performance remains acceptable at realistic density.

### Gate D — Tenant security

- Cross-tenant test suite passes for API, objects, jobs, caches, audit, and exports.
- Authorization matrix is complete.
- Internal support access is controlled and auditable.

### Gate E — AI quality

- Gold dataset and metrics exist.
- Critical-field and line-item accuracy are measured.
- Confidence policy is conservative and explainable.
- Provider changes require evaluation and versioned promotion.

### Gate F — Reliability

- Worker termination, provider outage, retry, dead-letter, and export replay are tested.
- Restore succeeds.
- No essential state is ephemeral.

### Gate G — Pilot release

- Appropriate paid/provider terms, backups, monitoring, support, privacy, and legal documents are in place.
- Release evidence is approved.

---

## 21. Major risks and mitigations

| Risk | Consequence | Required mitigation |
|---|---|---|
| Building a generic OCR demo | Weak differentiation and poor operational value | Prioritize validation, evidence, review, integrations, and stream configuration |
| LLM variability | Incorrect orders or hidden regressions | Structured output, deterministic validation, gold evaluation, field-level review, versioning |
| Line-item complexity | High correction rate | Dedicated table model, row alignment, continuation-page handling, grid UX, targeted evaluation |
| Tenant isolation defect | Severe security incident | Scoped repositories, RLS where appropriate, cross-tenant automated tests, security review |
| Free-tier dependency | Pauses, quota failures, unsuitable terms | Portability interfaces, quota controls, paid pilot gate, migration rehearsal |
| Generic UI | Slow reviewers and low trust | Dedicated design system, Review Studio usability tests, evidence-first interactions |
| Premature microservices | Delivery delay and operational burden | Modular monolith with explicit boundaries |
| Overbuilt workflow designer | Lost time before product proof | Fixed document pipeline with configurable policies first |
| Provider lock-in | Cost/privacy/availability constraints | Stable provider adapters and canonical artifacts |
| Corrections silently changing behavior | Uncontrolled quality regressions | Corrections feed evaluation; production changes require versioned promotion |
| ERP integration complexity | Pilot delays | Canonical contract, webhook/JSON first, one adapter after core stability |
| Poor observability | Slow incident resolution | Correlation, stage metrics, safe logs, traces, support timeline |

---

## 22. First build sequence for an implementation agent

An agent starting from the empty or documentation-only repository should execute in this order:

1. Create root toolchain files, workspace configuration, and standardized commands.
2. Scaffold the React web app, FastAPI API, and Python worker.
3. Add Docker Compose for PostgreSQL, MinIO, Mailpit, and scanning.
4. Add health endpoints and local service readiness.
5. Add SQLAlchemy base, Alembic, organization/user/membership/role migrations.
6. Implement development identity and generic OIDC validation boundary.
7. Implement tenant-scoped repositories and cross-tenant tests.
8. Build app shell and organization context.
9. Add process, stream, and immutable version models.
10. Add upload sessions, object-store adapter, artifact model, and file verification.
11. Add jobs/outbox, worker claims, heartbeat, retry, and recovery tests.
12. Add document/processing/stage models and state projection.
13. Add page rendering and deterministic mock extraction fixture.
14. Add canonical sales-order schema, extracted fields, evidence, normalization, and validation.
15. Build queue and document detail.
16. Build Review Studio and keyboard workflow.
17. Add approval, canonical payload generation, webhook delivery, retry, and replay.
18. Add native PDF and OCR adapters.
19. Add hosted/local extraction adapters and routing.
20. Add catalogs, matching, rule editor, analytics, email intake, and hardening in backlog order.

At the end of each numbered step, the repository must still build, migrate, run locally, and pass the relevant test suite.

---

## 23. Documentation maintenance

The repository documentation is the implementation contract. Notion remains the planning, research, and discussion workspace.

When behavior changes:

- Update the relevant canonical document in the same PR.
- Add or amend an ADR for a material architectural decision.
- Update backlog dependencies and acceptance criteria.
- Do not duplicate the same requirement in several new documents.
- Prefer links to authoritative sections.
- Mark deferred capabilities explicitly rather than leaving ambiguous placeholders.

---

## 24. Final release statement

The system may be called a production-ready sales-order automation platform only when a customer document can be received, secured, processed, explained, validated, reviewed, approved, delivered, audited, retried, restored, exported, and deleted according to policy—and when the evidence for accuracy, security, reliability, usability, and operational ownership has been recorded.
