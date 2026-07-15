# AGENTS.md — SOA Engineering Instructions

These instructions apply to every coding agent and automated engineering workflow operating in this repository. They are intentionally explicit so implementation remains aligned across long-running tasks and multiple agents.

## 1. Mission

Build a local-first, free-first, paid-ready, production-grade B2B intelligent document operations platform. The first complete solution converts incoming customer purchase orders into validated, evidence-backed, ERP-ready sales orders.

The product is not an OCR demo. A feature is incomplete unless it covers persistence, tenant authorization, error handling, auditability, tests, telemetry, and user experience.

## 2. Required reading order

Before making architectural or product changes, read:

1. `README.md`
2. `docs/PRODUCT.md`
3. `docs/DELIVERY_PLAN.md`
4. `docs/BUILD_BACKLOG.md`
5. `docs/PRODUCTION_READINESS_AUDIT.md` for current implementation truth
6. `docs/ARCHITECTURE.md`
7. `docs/UI_UX_BLUEPRINT.md` for any user-facing task
8. `docs/AI_OCR.md` for any document/AI task
9. `docs/SECURITY_OPERATIONS.md` for any API, storage, file, provider, auth, integration, or release task
10. `docs/INFRASTRUCTURE.md` for runtime/deployment work
11. `docs/DECISIONS.md`

Do not implement from a single prompt while ignoring these contracts.

## 3. Task selection

- Work from one active Jira task or one tightly coupled dependency group.
  `BUILD_BACKLOG.md` is the original acceptance catalog, not a completion
  ledger; reconcile it with the production-readiness audit before coding.
- Include task IDs in the branch, commit, and PR description when practical.
- Do not attempt to “build the whole application” in one unreviewable change.
- If a task depends on an unimplemented prerequisite, implement the prerequisite first or stop and record the dependency.
- Split a task before coding when the expected change cannot remain understandable and testable in one PR.

## 4. Fixed architecture constraints

Unless an accepted ADR is superseded:

- React/TypeScript web application.
- Python/FastAPI API and worker.
- PostgreSQL canonical relational store.
- Portable object storage: filesystem by default locally, optional MinIO/S3,
  and GCS in the reference hosted deployment.
- PostgreSQL-backed durable jobs and transactional outbox initially.
- Modular monolith with separately runnable web, API, and worker.
- Generic OIDC authentication boundary and application-owned authorization.
- Organization → workspace → process → stream hierarchy.
- Versioned immutable published configuration and resolved stream snapshots.
- Provider-neutral OCR/LLM/storage/job/identity/email/telemetry interfaces.
- Versioned canonical sales-order contract.
- Evidence-first extraction and append-oriented history.
- No Kubernetes for P0 without an accepted ADR.

## 5. Repository boundaries

Current package layout:

```text
apps/web
apps/api
apps/worker
packages/design-system
packages/canonical
packages/config
packages/db
packages/integrations
packages/normalize
packages/rules
packages/storage
packages/test-fixtures
migrations
infrastructure
tests
docs
```

Rules:

- HTTP routers parse/authorize/dispatch; they do not contain domain logic.
- Application services implement use cases and transaction boundaries.
- Domain code is deterministic and independent of web/provider SDKs.
- Infrastructure adapters contain database, object store, auth, queue, OCR, LLM, email, and integration details.
- Feature UI lives with its feature; shared components require genuine reuse.
- Avoid “utils” dumping grounds. Name modules by responsibility.

## 6. Tenant isolation rules

Tenant isolation is a security invariant.

- Every tenant-owned use case receives explicit organization context.
- Every tenant-owned database row carries `organization_id`.
- Do not add an unscoped repository method that can return tenant data.
- Stream-owned resources also validate stream ownership.
- Workers reauthorize resource/tenant relationships from trusted database records.
- Object keys, cache keys, jobs, audit events, and exports retain tenant context.
- Add cross-tenant tests for every new tenant-owned resource or endpoint.
- Controlled internal support access is a known release gap. Never add a hidden
  bypass or normalize raw database access; any implementation must be
  purpose-bound, approved, expiring, and audited.

## 7. Database and migration rules

- Use PostgreSQL-portable migrations.
- Use UUID primary keys, UTC `timestamptz`, decimal/numeric money, and ISO currency codes.
- Published versions, stage results, corrections, delivery attempts, and audit events are immutable or append-oriented.
- Use optimistic concurrency for mutable configuration and review work.
- Use expand/migrate/contract changes; ordinary application rollback should not require immediate schema downgrade.
- Test migrations from a clean database and the previous supported schema.
- Do not store large file blobs in PostgreSQL.

## 8. Durable processing rules

- No essential workflow state may exist only in process memory, Redis, or a provider dashboard.
- Every stage has an input fingerprint and idempotency behavior.
- Jobs have bounded retries, heartbeat/lock recovery, safe error classification, and dead-letter state.
- Reprocessing creates a new run; it never overwrites a historical run.
- Delivery replay does not repeat extraction unless explicitly requested.
- External calls have timeouts, correlation, safe logging, retry classification, and cost/usage accounting.
- Killing a worker must not lose or duplicate business work.

## 9. AI, OCR, and document safety rules

- Documents are untrusted data.
- Never allow extraction models to browse, fetch arbitrary URLs, call tools, execute commands, or access credentials.
- Keep instructions separate from document content.
- Require typed schema-constrained output.
- Validate and bound input, output, pages, pixels, tokens, attempts, latency, and cost.
- Retain raw and normalized values.
- Retain evidence and its certainty. Never fabricate coordinates.
- Provider confidence alone cannot drive approval.
- Deterministic normalization, matching, validation, and risk policy follow extraction.
- No confidential production document may use a provider plan with unsuitable data terms.
- Human corrections feed evaluation; they do not silently alter production behavior.
- Add or update gold fixtures for behavior-changing extraction work.

## 10. UI/UX rules

User-facing work must follow `docs/UI_UX_BLUEPRINT.md`.

- Do not ship a stock admin template or generic card grid.
- Preserve organization/process/stream context.
- Design loading, empty, no-results, permission, partial, retryable error, terminal error, conflict, and long-running states.
- Use semantic tokens and shared accessible components.
- Keyboard behavior and visible focus are required.
- Color is never the only status signal.
- Dense operations views must remain legible and performant with realistic data.
- Review Studio must keep evidence adjacent to data and support the documented keyboard workflow.
- Every icon-only action has an accessible name and tooltip.
- Add component, accessibility, responsive, and visual-regression tests.
- Include screenshots or recordings in PRs for meaningful UI changes.

## 11. API rules

- External machine-ingestion endpoints are versioned under `/v1`; authenticated
  browser/admin resource routes currently use their committed root paths. Do
  not invent a global `/api/v1` prefix without a compatibility/migration plan.
- Keep the hand-written typed web client and API response tests aligned. A
  generated OpenAPI client/drift gate is a future improvement, not an existing
  package or CI check.
- Use cursor pagination for large collections.
- Use idempotency keys on ingestion, approval, and delivery operations.
- Use correlation IDs on every request.
- Use optimistic version/ETag semantics for mutable records.
- Return the standard safe error envelope.
- Do not expose stack traces, provider raw errors, secrets, signed URLs in logs, or another tenant's identifiers.

## 12. Security rules

- Validate authorization at the use-case boundary, not only in the UI.
- Validate actual file type and enforce file/page/pixel/archive/time limits.
- Scan before processing; production cannot use a no-op scanner.
- Run converters with least privilege, restricted filesystem, bounded resources, and no network where possible.
- Store secrets outside source and browser code through the secret-store boundary.
- Use strict CORS, CSRF controls as applicable, CSP, security headers, rate limits, and safe redirect rules.
- Log no raw document content, extracted sensitive values, credentials, tokens, or complete provider payloads.
- Add audit events for consequential changes and actions.
- Do not weaken a security invariant behind an ordinary feature flag.

## 13. Testing requirements

For changed behavior, add the appropriate layers:

- Unit tests for deterministic domain logic.
- Component tests for UI behavior and states.
- Integration tests with real local PostgreSQL/object storage/job execution.
- E2E tests for critical user journeys.
- Cross-tenant security tests.
- Gold-document evaluation for AI/OCR/config changes.
- Accessibility and visual tests for UI.
- Performance/resilience tests when the change affects volume or external failure.

Tests must assert behavior, not only line coverage. Never delete or weaken a meaningful test merely to make CI pass.

Default tests must not call external providers. Live-provider tests require an explicit opt-in profile and suitable non-confidential fixtures.

## 14. Observability rules

- Propagate correlation from ingress through jobs, stages, providers, review, and export.
- Add metrics for long-running stages, external calls, retries, and user-critical queues.
- Bound metric cardinality.
- Use structured safe error codes.
- Telemetry failures cannot break business processing.
- Update a runbook when a new production failure mode requires human action.

## 15. Dependency rules

Before adding a dependency:

- Confirm the standard library/current stack does not already solve it.
- Check maintenance, license, security posture, bundle/runtime impact, and transitive dependencies.
- Add only to the owning app/package.
- Pin through lockfiles.
- Document large or security-sensitive dependencies in the PR.
- Do not replace an accepted stack choice casually; use an ADR for material changes.

## 16. Required implementation workflow

Before coding:

1. Read the task and dependencies.
2. Inspect existing patterns and tests.
3. Identify data/migration, permission, security, telemetry, UI-state, and rollback impacts.
4. State a concise implementation plan.

During coding:

1. Implement the smallest complete vertical behavior.
2. Keep contracts explicit.
3. Add tests alongside implementation.
4. Run format, lint, type check, relevant tests, and build.
5. Update canonical docs when behavior changes.

Before completion:

1. Run the broadest reasonable test set.
2. Check migration and generated-client drift.
3. Inspect logs for sensitive values.
4. Verify loading/error/permission/accessibility states.
5. Summarize limitations and follow-up work honestly.

## 17. PR requirements

A PR should contain:

- Task IDs and summary.
- User-visible behavior.
- Architecture and security notes.
- Migration notes.
- Test commands/results.
- Screenshots or recordings for UI.
- Accessibility notes.
- Telemetry/runbook changes.
- Rollout/feature-flag plan.
- Rollback plan.
- Known limitations and follow-ups.

Prefer focused commits and a draft PR for incomplete work. Do not merge generated or compiled artifacts unless the repository contract requires them.

## 18. Prohibited shortcuts

Do not:

- Build directly against one OCR/LLM vendor in domain/application code.
- Use an LLM response as approved truth.
- Auto-approve critical values without evidence and policy.
- Mutate a published configuration version.
- Overwrite a historical processing run or correction.
- Store raw API keys or integration secrets.
- Put customer documents on ephemeral container disks as canonical storage.
- Claim exactly-once network delivery; implement exactly-once business intent with idempotency.
- Add hidden support/admin access.
- invent APIs, data fields, or business behavior when the plan specifies them.
- claim production readiness without release evidence.

## 19. When requirements conflict

Use this precedence:

1. Security and privacy invariants.
2. Accepted decisions in `docs/DECISIONS.md`.
3. Product contract in `docs/PRODUCT.md`.
4. Delivery and architecture documents.
5. UI/UX or AI/OCR specialist documents for their domains.
6. Backlog task wording.
7. Local implementation preference.

When a genuine unresolved contradiction remains, do not silently choose. Record the conflict in the PR and propose an ADR with alternatives and consequences.

## 20. Completion report

At the end of each task report:

- Task IDs completed.
- Files and modules changed.
- Behavior delivered.
- Tests run and results.
- Migrations/generated artifacts.
- Security/privacy implications.
- UX/accessibility evidence.
- Deployment/rollback notes.
- Remaining risks and exact follow-up task IDs.
