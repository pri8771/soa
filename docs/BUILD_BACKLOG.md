# Executable Build Backlog

> **Purpose:** the original dependency-ordered acceptance catalog. Much of the
> code now exists; task presence is not status or release evidence. Use
> [`PRODUCTION_READINESS_AUDIT.md`](PRODUCTION_READINESS_AUDIT.md) for current
> implementation truth and Jira/Notion for active task state.
>
> **Rule:** a task is complete only when its acceptance criteria, tests, telemetry, security implications, and documentation impact are satisfied.

This backlog is designed for human engineers or coding agents. It should be executed in order unless a task explicitly states that it can run in parallel. Canonical requirements remain in the other documents; this file translates them into bounded implementation work.

---

## 1. Task execution format

Every task implementation must include:

- **Scope:** only the behavior described by the task and its direct dependencies.
- **Code:** production implementation, not an isolated mock unless the task explicitly requests a mock adapter.
- **Tests:** listed tests plus any regression tests needed for changed behavior.
- **Security:** tenant, data, secret, file, and provider implications considered.
- **Telemetry:** logs, metrics, traces, and audit events where the operation is externally visible or long-running.
- **UX:** loading, empty, error, permission, conflict, and accessibility states for user-facing work.
- **Docs:** update commands, API, ADR, or runbook when behavior changes.
- **PR evidence:** screenshots for UI, test output, migrations, rollout/rollback notes, and known limitations.

Priority:

- **P0:** required for the first production-quality paid pilot.
- **P1:** required immediately after the first stable pilot or for common enterprise adoption.
- **P2:** expansion capability.

Size:

- **S:** normally one focused implementation session.
- **M:** several related modules but one reviewable outcome.
- **L:** split into subtasks before implementation if it cannot remain one coherent PR.

---

## 2. Dependency map

```text
FND repository foundation
 ├─→ TEN identity and tenancy
 ├─→ DSN design system and app shell
 └─→ JOB durable jobs and outbox

TEN + JOB
 └─→ CFG processes, streams, and versioned configuration

TEN + CFG + STO storage
 └─→ ING document ingestion

ING + JOB
 └─→ PRC document processing and mock extraction

PRC + DSN
 └─→ REV Review Studio and approval

REV + CAN canonical contract
 └─→ EXP exports and delivery

PRC
 ├─→ AIO real OCR/LLM providers and evaluation
 └─→ CAT catalogs, matching, and rules

REV + EXP + AIO + CAT
 └─→ ANA analytics and operational administration

All P0 capabilities
 └─→ SEC hardening, REL reliability, and PIL pilot readiness
```

No production customer documents may be processed before the pilot-readiness gates pass.

---

# EPIC FND — Repository, toolchain, and local environment

## FND-001 — Establish monorepo and toolchain

- **Priority/size:** P0 / M
- **Dependencies:** none
- **Deliver:** root workspace, `apps/web`, `apps/api`, `apps/worker`, shared package directories, lockfiles, formatting, linting, type checking, and standard commands.
- **Acceptance:**
  - `pnpm install` and `uv sync` work from a fresh clone.
  - Root commands can lint, type-check, test, and build every app.
  - Node and Python versions are pinned.
  - No generated or secret local files are committed.
- **Tests/evidence:** clean checkout bootstrap in CI; build all apps.

## FND-002 — Add standardized developer commands

- **Priority/size:** P0 / S
- **Dependencies:** FND-001
- **Deliver:** `Makefile` or `justfile` implementing the command contract in `DELIVERY_PLAN.md`.
- **Acceptance:** commands have help text, return non-zero on failure, and are used by CI and README.
- **Tests:** command smoke test on Linux CI.

## FND-003 — Scaffold FastAPI application

- **Priority/size:** P0 / S
- **Dependencies:** FND-001
- **Deliver:** application factory, settings, dependency container, `/health/live`, `/health/ready`, version endpoint, structured error envelope.
- **Acceptance:** startup validates configuration; readiness checks dependencies; production errors do not expose traces.
- **Tests:** API unit tests and health integration test.

## FND-004 — Scaffold worker process

- **Priority/size:** P0 / S
- **Dependencies:** FND-001
- **Deliver:** worker entry point, settings, graceful shutdown, heartbeat skeleton, handler registry.
- **Acceptance:** worker starts independently, emits service/version metadata, and stops without abandoning an active handler without recording state.
- **Tests:** startup/shutdown and signal tests.

## FND-005 — Scaffold React web application

- **Priority/size:** P0 / M
- **Dependencies:** FND-001
- **Deliver:** React/TypeScript/Vite application, typed routing, query client, error boundary, environment validation, test harness.
- **Acceptance:** local and production builds work; unknown routes and fatal errors have designed states.
- **Tests:** component smoke test and production build.

## FND-006 — Add local Docker services

- **Priority/size:** P0 / M
- **Dependencies:** FND-002
- **Deliver:** PostgreSQL, MinIO, Mailpit, ClamAV or explicit development scanner, optional telemetry profile, health checks, persistent named volumes.
- **Acceptance:** `make local-up` becomes healthy deterministically; data survives restart; no default credential is usable in production.
- **Tests:** service-health script in CI where supported.

## FND-007 — Environment and secret configuration

- **Priority/size:** P0 / S
- **Dependencies:** FND-003, FND-004, FND-005
- **Deliver:** typed settings, `.env.example`, environment profiles, secret redaction, startup validation.
- **Acceptance:** missing required production settings fail startup; development-only flags fail in production; settings never print secret values.
- **Tests:** configuration matrix tests.

## FND-008 — Logging and correlation foundation

- **Priority/size:** P0 / M
- **Dependencies:** FND-003, FND-004
- **Deliver:** structured logs, request/job correlation IDs, safe error codes, log redaction helpers.
- **Acceptance:** a correlation ID travels from request to audit/job context; sensitive-key tests show redaction.
- **Tests:** logging capture and redaction assertions.

## FND-009 — OpenTelemetry foundation

- **Priority/size:** P0 / M
- **Dependencies:** FND-003, FND-004, FND-005
- **Deliver:** traces and metrics abstraction with no-op/local/exporter profiles.
- **Acceptance:** API requests and worker handlers create correlated spans; telemetry failure never breaks business processing.
- **Tests:** in-memory exporter integration test.

## FND-010 — CI baseline

- **Priority/size:** P0 / M
- **Dependencies:** FND-001 through FND-009
- **Deliver:** GitHub Actions for format, lint, type check, unit tests, build, migration checks, dependency and secret scans.
- **Acceptance:** required checks are deterministic and cache dependencies safely; CI does not require production secrets.
- **Tests:** workflow runs successfully on branch.

## FND-011 — Seed and fixture framework

- **Priority/size:** P0 / M
- **Dependencies:** database tasks below may complete in the same milestone
- **Deliver:** deterministic seed runner and synthetic fixture package.
- **Acceptance:** repeated seed is idempotent; IDs or stable aliases allow E2E tests to locate records.
- **Tests:** seed twice and compare expected counts.

## FND-012 — Documentation link and command validation

- **Priority/size:** P0 / S
- **Dependencies:** FND-002, FND-010
- **Deliver:** CI check for broken repository-relative links and documented command drift.
- **Acceptance:** README links and command examples are verified automatically.

---

# EPIC DB — Database and persistence foundation

## DB-001 — Configure SQLAlchemy and Alembic

- **Priority/size:** P0 / M
- **Dependencies:** FND-003, FND-006
- **Deliver:** async engine/session management, transaction helpers, migration structure, naming conventions.
- **Acceptance:** clean database upgrades to head; connection lifecycle is safe; migrations do not run automatically in web replicas.
- **Tests:** fresh migration and downgrade/upgrade smoke where safe.

## DB-002 — Add common persistence types and conventions

- **Priority/size:** P0 / S
- **Dependencies:** DB-001
- **Deliver:** UUID, UTC timestamps, decimal money, version columns, enum conventions, pagination primitives.
- **Acceptance:** no float monetary columns; all timestamps are timezone-aware; optimistic concurrency helper exists.
- **Tests:** model serialization and concurrency tests.

## DB-003 — Scoped repository base

- **Priority/size:** P0 / M
- **Dependencies:** DB-001
- **Deliver:** repositories that require organization scope and transaction context; no unscoped tenant list/query helper.
- **Acceptance:** tenant-owned queries cannot compile/run without scope; repository APIs expose pagination and locking explicitly.
- **Tests:** cross-organization fixture tests.

## DB-004 — Transactional outbox schema

- **Priority/size:** P0 / M
- **Dependencies:** DB-001
- **Deliver:** outbox records, publication state, dedupe key, retry metadata.
- **Acceptance:** domain state and outbox event commit atomically; duplicate publication is safe.
- **Tests:** rollback and duplicate publisher integration tests.

## DB-005 — Audit-event schema and writer

- **Priority/size:** P0 / M
- **Dependencies:** DB-001, DB-003
- **Deliver:** append-oriented audit model and application service.
- **Acceptance:** actor, tenant, action, target, timestamp, correlation, and safe change summary are required; updates/deletes are prohibited through application repositories.
- **Tests:** audit creation and attempted mutation tests.

---

# EPIC TEN — Identity, organizations, roles, and tenant isolation

## TEN-001 — Define authentication principal contract

- **Priority/size:** P0 / S
- **Dependencies:** FND-003
- **Deliver:** provider-neutral principal containing subject, issuer, user identity, authentication method, and claims needed for membership lookup.
- **Acceptance:** domain/application code does not depend on one auth vendor's SDK or claim names.
- **Tests:** provider claim mapping tests.

## TEN-002 — Implement secure development identity mode

- **Priority/size:** P0 / S
- **Dependencies:** TEN-001, FND-007
- **Deliver:** local-only authenticated identities selectable from seeded users.
- **Acceptance:** enabled only in development/test; production startup fails if active; UI clearly labels development identity.
- **Tests:** environment guard tests.

## TEN-003 — Implement OIDC JWT validation adapter

- **Priority/size:** P0 / M
- **Dependencies:** TEN-001
- **Deliver:** issuer/audience/signature/time validation, key rotation cache, safe auth errors.
- **Acceptance:** algorithm confusion and invalid issuer/audience are rejected; clock skew is bounded.
- **Tests:** valid, expired, wrong audience, wrong issuer, unknown key, and rotated key cases.

## TEN-004 — Create organization and workspace models

- **Priority/size:** P0 / M
- **Dependencies:** DB-001, DB-002
- **Deliver:** migrations, models, repositories, organization status and locale/region fields.
- **Acceptance:** global slug uniqueness; suspended/closed behavior is represented; workspaces are tenant scoped.
- **Tests:** CRUD, uniqueness, scope, and status tests.

## TEN-005 — Create user and membership models

- **Priority/size:** P0 / M
- **Dependencies:** TEN-004, TEN-001
- **Deliver:** global user identity, organization memberships, invitation and membership states.
- **Acceptance:** one identity may join multiple organizations; removed/suspended membership blocks access.
- **Tests:** membership state matrix.

## TEN-006 — Implement roles and permissions

- **Priority/size:** P0 / L
- **Dependencies:** TEN-005
- **Deliver:** system role templates, custom organization roles, explicit permission registry, role assignments.
- **Acceptance:** permissions are action-oriented; unknown permissions fail closed; role changes are audited.
- **Tests:** permission matrix and migration seed tests.

## TEN-007 — Central authorization service

- **Priority/size:** P0 / M
- **Dependencies:** TEN-006, DB-003
- **Deliver:** authorization policy functions used by HTTP use cases and workers.
- **Acceptance:** checks include organization, resource ownership, action, stream scope, and organization status; handlers cannot bypass policy accidentally.
- **Tests:** allow/deny matrix including cross-tenant IDs.

## TEN-008 — Organization and membership APIs

- **Priority/size:** P0 / M
- **Dependencies:** TEN-004 through TEN-007
- **Deliver:** `/me`, organizations, memberships, invitations, roles.
- **Acceptance:** pagination, optimistic updates, audit events, safe duplicate invitation behavior.
- **Tests:** API integration and authorization tests.

## TEN-009 — Service credentials and API-key hashing

- **Priority/size:** P0 / M
- **Dependencies:** TEN-006
- **Deliver:** create-once display, strong random secret, prefix lookup, one-way hash, scopes, expiration, revoke/rotate.
- **Acceptance:** raw keys are never stored or logged; revoked/expired/scope-invalid requests fail closed.
- **Tests:** key lifecycle and brute-force-safe lookup tests.

## TEN-010 — Tenant row-level-security defense in depth

- **Priority/size:** P0 / M
- **Dependencies:** DB-003, TEN-004
- **Deliver:** PostgreSQL RLS for selected high-risk tenant tables or documented equivalent transaction scoping.
- **Acceptance:** direct accidental query under a tenant-scoped session cannot read another tenant.
- **Tests:** database-level isolation tests.

## TEN-011 — Cross-tenant security suite

- **Priority/size:** P0 / L
- **Dependencies:** TEN-007, TEN-010
- **Deliver:** reusable test matrix for reads, writes, pagination cursors, object links, jobs, audit, and exports as those modules arrive.
- **Acceptance:** suite is required in CI and expanded with each tenant-owned feature.

## TEN-012 — Organization selector and session context UI

- **Priority/size:** P0 / M
- **Dependencies:** DSN-004, TEN-008
- **Deliver:** organization selection, context provider, clear current organization display, unauthorized/suspended states.
- **Acceptance:** context persists safely; switching invalidates tenant-specific caches; no stale prior-tenant data flashes.
- **Tests:** component and E2E switching/isolation tests.

---

# EPIC DSN — Design system and premium application shell

## DSN-001 — Implement design tokens

- **Priority/size:** P0 / M
- **Dependencies:** FND-005
- **Deliver:** color, typography, spacing, radius, elevation, motion, focus, density, and theme tokens from `UI_UX_BLUEPRINT.md`.
- **Acceptance:** feature components consume semantic tokens; light/dark contrast passes automated checks.
- **Tests:** token build, contrast assertions, visual snapshots.

## DSN-002 — Implement accessible primitive components

- **Priority/size:** P0 / L
- **Dependencies:** DSN-001
- **Deliver:** buttons, inputs, select/combobox, checkbox/radio/switch, menus, tooltip, popover, dialogs, drawers, tabs, badges, banners, toasts, progress, skeleton.
- **Acceptance:** keyboard and screen-reader semantics documented; focus restoration works; no inaccessible div-button patterns.
- **Tests:** component interaction, axe, keyboard, and visual tests.

## DSN-003 — Add component workbench

- **Priority/size:** P0 / M
- **Dependencies:** DSN-001, DSN-002
- **Deliver:** Storybook or equivalent with required states, themes, long text, and localization expansion.
- **Acceptance:** visual regression runs on critical stories.

## DSN-004 — Build application shell

- **Priority/size:** P0 / L
- **Dependencies:** DSN-002, FND-005
- **Deliver:** responsive navigation, top context bar, breadcrumbs, page header, permission-aware navigation, utility areas.
- **Acceptance:** layout matches documented dimensions; works at required breakpoints; focus order is logical.
- **Tests:** route, accessibility, responsive, and visual tests.

## DSN-005 — Command palette and global search shell

- **Priority/size:** P0 / M
- **Dependencies:** DSN-004
- **Deliver:** accessible command palette with navigation/actions and future pluggable search results.
- **Acceptance:** keyboard shortcut is discoverable; commands respect permissions and organization context.
- **Tests:** keyboard and permission tests.

## DSN-006 — Data-table system

- **Priority/size:** P0 / L
- **Dependencies:** DSN-002
- **Deliver:** server pagination, sort, filters, saved-view hooks, selection, bulk bar, sticky columns, density, virtualization, accessible table semantics.
- **Acceptance:** performs with representative volume; URL state and empty/error/loading states work.
- **Tests:** component, keyboard, performance smoke, visual tests.

## DSN-007 — Operational status components

- **Priority/size:** P0 / M
- **Dependencies:** DSN-002
- **Deliver:** status, confidence, SLA, validation, stage progress, connection status, usage meter.
- **Acceptance:** not color-only; tooltips/popovers explain meaning; works in compact tables.

## DSN-008 — Visual-regression CI

- **Priority/size:** P0 / M
- **Dependencies:** DSN-003, FND-010
- **Deliver:** deterministic browser snapshots and review workflow.
- **Acceptance:** intended updates are explicit; unstable data/time/animation is controlled.

## DSN-009 — Accessibility CI and manual checklist

- **Priority/size:** P0 / M
- **Dependencies:** DSN-002, DSN-004
- **Deliver:** automated axe checks and documented manual critical-flow checklist.
- **Acceptance:** critical violations fail CI.

---

# EPIC JOB — Durable jobs, events, and worker reliability

## JOB-001 — Create job schema and states

- **Priority/size:** P0 / M
- **Dependencies:** DB-001
- **Deliver:** jobs with type, payload, tenant, status, priority, run-after, attempts, dedupe key, lock, heartbeat, error, correlation.
- **Acceptance:** state constraints prevent invalid transitions; payload schema version is recorded.
- **Tests:** migration and state tests.

## JOB-002 — Implement transactional job enqueue

- **Priority/size:** P0 / M
- **Dependencies:** DB-004, JOB-001
- **Deliver:** enqueue through outbox or same transaction, dedupe behavior, scheduled jobs.
- **Acceptance:** committed domain changes never lose their follow-up job; rolled-back changes create none.
- **Tests:** transaction boundary tests.

## JOB-003 — Implement worker claim with `SKIP LOCKED`

- **Priority/size:** P0 / M
- **Dependencies:** JOB-001, FND-004
- **Deliver:** ordered claim, concurrency limit, lock ownership and expiration.
- **Acceptance:** multiple workers never execute the same active claim; starved lower-priority jobs remain bounded by policy.
- **Tests:** concurrent integration test.

## JOB-004 — Heartbeat, timeout, and recovery

- **Priority/size:** P0 / M
- **Dependencies:** JOB-003
- **Deliver:** lock heartbeat, expired-lock recovery, graceful cancellation.
- **Acceptance:** terminating a worker returns work safely; a completed job is not resurrected.
- **Tests:** forced termination and recovery.

## JOB-005 — Retry and dead-letter policy

- **Priority/size:** P0 / M
- **Dependencies:** JOB-003
- **Deliver:** classified failures, bounded exponential backoff, retry hints, terminal/dead-letter state.
- **Acceptance:** permanent input/security errors do not retry; transient errors do; attempt history remains visible.
- **Tests:** failure-class matrix.

## JOB-006 — Job administration API

- **Priority/size:** P0 / M
- **Dependencies:** JOB-005, TEN-007
- **Deliver:** authorized list/detail/retry/cancel/replay operations.
- **Acceptance:** replay reason is required/audited; tenant users see only their jobs; internal controls are separately permissioned.
- **Tests:** API and authorization tests.

## JOB-007 — Queue health UI

- **Priority/size:** P0 / M
- **Dependencies:** JOB-006, DSN-006, DSN-007
- **Deliver:** queue depth, oldest age, status/filter, safe failure reason, replay/cancel controls.
- **Acceptance:** no raw payload/secrets displayed; long-running refresh does not reset filters.

## JOB-008 — Queue metrics and alerts

- **Priority/size:** P0 / S
- **Dependencies:** JOB-003, FND-009
- **Deliver:** depth, age, claim rate, completion, retry, dead-letter metrics.
- **Acceptance:** tenant IDs appear only in approved dimensions; cardinality is bounded.

---

# EPIC CFG — Processes, streams, schemas, and versioned configuration

## CFG-001 — Process and process-version models

- **Priority/size:** P0 / M
- **Dependencies:** TEN-004, DB-002
- **Deliver:** stable process root and immutable published versions.
- **Acceptance:** draft editable with version check; published immutable; active pointer audited.
- **Tests:** lifecycle and concurrency.

## CFG-002 — Stream and stream-version models

- **Priority/size:** P0 / M
- **Dependencies:** CFG-001
- **Deliver:** stream root, process association, explicit overrides, resolved snapshot, state.
- **Acceptance:** snapshot contains all values needed to process without reading mutable drafts.
- **Tests:** inheritance and immutability.

## CFG-003 — Schema and field-definition versions

- **Priority/size:** P0 / L
- **Dependencies:** CFG-001
- **Deliver:** JSON-schema-compatible field tree, types, required, criticality, normalization, evidence requirement, examples.
- **Acceptance:** supports header and repeating line items; invalid recursion/duplicates/type changes are rejected.
- **Tests:** schema validation and canonical examples.

## CFG-004 — Rule-set version model

- **Priority/size:** P0 / M
- **Dependencies:** CFG-001
- **Deliver:** deterministic typed expression AST, severity/action, test cases, immutable versions.
- **Acceptance:** no arbitrary code execution; expressions are serializable, inspectable, and type checked.
- **Tests:** parser/type/evaluator baseline.

## CFG-005 — Provider, confidence, retention, and mapping policy roots

- **Priority/size:** P0 / M
- **Dependencies:** CFG-001
- **Deliver:** versionable policy records and references; provider secrets referenced, not embedded.
- **Acceptance:** policies can be resolved into a stream snapshot; missing capabilities block publish.

## CFG-006 — Configuration resolver

- **Priority/size:** P0 / L
- **Dependencies:** CFG-002 through CFG-005
- **Deliver:** merge process defaults, stream overrides, environment capability, and pinned version references.
- **Acceptance:** deterministic output and fingerprint; explicit provenance per resolved value.
- **Tests:** inheritance matrix, override reset, deterministic hash.

## CFG-007 — Draft validation and publish service

- **Priority/size:** P0 / L
- **Dependencies:** CFG-006, DB-005
- **Deliver:** validate, test state, approve/publish, supersede, rollback pointer.
- **Acceptance:** publish is transactional and audited; published record cannot mutate; clear validation report.
- **Tests:** lifecycle, failure rollback, concurrent publish.

## CFG-008 — Process and stream APIs

- **Priority/size:** P0 / L
- **Dependencies:** CFG-007, TEN-007
- **Deliver:** list/detail/create/update/clone/version/test/publish/rollback endpoints.
- **Acceptance:** all writes use optimistic concurrency and permission checks.
- **Tests:** API integration and cross-tenant tests.

## CFG-009 — Process browser UI

- **Priority/size:** P0 / M
- **Dependencies:** CFG-008, DSN-006
- **Deliver:** list, status, active version, streams, drafts, owner, health placeholders.
- **Acceptance:** loading/empty/error/permission states and URL filters.

## CFG-010 — Stream detail UI

- **Priority/size:** P0 / L
- **Dependencies:** CFG-008, DSN-004, DSN-007
- **Deliver:** overview, intake endpoints, published version, configuration summary, health, changes.
- **Acceptance:** hierarchy/context always visible; destructive archive requires impact explanation.

## CFG-011 — Schema builder UI

- **Priority/size:** P0 / L
- **Dependencies:** CFG-003, CFG-008, DSN-002
- **Deliver:** field tree, field editor, preview, keyboard reorder, table/nested fields, validation errors.
- **Acceptance:** unsaved changes/concurrency/inherited state visible; no invalid schema can publish.
- **Tests:** component, accessibility, E2E create/edit.

## CFG-012 — Rule builder UI

- **Priority/size:** P0 / L
- **Dependencies:** CFG-004, CFG-008
- **Deliver:** typed condition/action builder, test cases, expression preview.
- **Acceptance:** generated deterministic expression is visible; natural-language helper is not required for P0.
- **Tests:** rule authoring and invalid-expression UX.

## CFG-013 — Inheritance editor UI

- **Priority/size:** P0 / L
- **Dependencies:** CFG-006, CFG-010
- **Deliver:** inherited/overridden/not-configured labels, reset-to-parent, resolved preview.
- **Acceptance:** user can identify source of every important setting; changes show affected scope.

## CFG-014 — Configuration comparison and rollback UI

- **Priority/size:** P0 / M
- **Dependencies:** CFG-007, CFG-010
- **Deliver:** version timeline, structured diff, rollback reason/action.
- **Acceptance:** secret values never appear; rollback preserves history and creates audit event.

---

# EPIC STO — Object storage and artifact security

## STO-001 — Define `ObjectStore` interface

- **Priority/size:** P0 / S
- **Dependencies:** FND-003
- **Deliver:** put/get/head/delete/copy/list-by-manifest, signed upload/download, multipart-abort, checksum metadata.
- **Acceptance:** domain/application code does not import vendor SDK types.
- **Tests:** contract test suite.

## STO-002 — Implement local MinIO adapter

- **Priority/size:** P0 / M
- **Dependencies:** STO-001, FND-006
- **Deliver:** S3-compatible adapter, bucket initialization, signed URLs, checksum metadata.
- **Acceptance:** contract suite passes; keys are tenant/document scoped and non-guessable.

## STO-003 — Implement artifact model

- **Priority/size:** P0 / M
- **Dependencies:** DB-001, STO-001, TEN-004
- **Deliver:** artifact metadata, kind, immutable hash, run/stage producer, retention class.
- **Acceptance:** original and run artifacts cannot be overwritten through service API.
- **Tests:** immutability and tenant scope.

## STO-004 — Signed download authorization

- **Priority/size:** P0 / M
- **Dependencies:** STO-002, STO-003, TEN-007
- **Deliver:** short-lived authorized download URL service.
- **Acceptance:** user cannot obtain another tenant's URL; expired URLs fail; support access policy enforced.
- **Tests:** cross-tenant and expiry tests.

## STO-005 — Artifact manifest and hash reconciliation

- **Priority/size:** P0 / M
- **Dependencies:** STO-003
- **Deliver:** manifest export and verification command for backup/migration.
- **Acceptance:** reports missing, extra, or mismatched objects without exposing content.
- **Tests:** deliberate corruption/missing-object cases.

## STO-006 — Production S3-compatible adapter/configuration

- **Priority/size:** P0 / M
- **Dependencies:** STO-001
- **Deliver:** configurable endpoint/region/path style/encryption and lifecycle assumptions.
- **Acceptance:** same contract tests pass against an integration environment.

---

# EPIC ING — Document ingestion and file safety

## ING-001 — Document and input-source models

- **Priority/size:** P0 / M
- **Dependencies:** TEN-004, CFG-002, STO-003
- **Deliver:** document metadata, source channel, client idempotency, content hash, priority, SLA, state.
- **Acceptance:** every document has organization/stream; states constrained; source metadata safe.
- **Tests:** model/state/idempotency.

## ING-002 — Upload-session API

- **Priority/size:** P0 / L
- **Dependencies:** ING-001, STO-002, TEN-007
- **Deliver:** create session, signed target, complete, abort, expiration.
- **Acceptance:** validates quota/type/size before signing; completion verifies object metadata and creates document once.
- **Tests:** happy path, duplicate complete, wrong size/hash, unauthorized stream, expired session.

## ING-003 — File signature and media validation

- **Priority/size:** P0 / M
- **Dependencies:** ING-002
- **Deliver:** actual content detection, extension comparison, supported type policy.
- **Acceptance:** type-confusion files are rejected/quarantined; safe reason recorded.
- **Tests:** mismatched and malformed fixtures.

## ING-004 — Malware scanning stage

- **Priority/size:** P0 / M
- **Dependencies:** ING-002, JOB-002, FND-006
- **Deliver:** scanner contract, ClamAV adapter, development no-op explicitly forbidden outside dev, quarantine state.
- **Acceptance:** unscanned files cannot proceed in production; infected file unavailable for normal download/processing.
- **Tests:** standard antivirus test fixture and scanner outage behavior.

## ING-005 — File resource limits

- **Priority/size:** P0 / M
- **Dependencies:** ING-003
- **Deliver:** file size, page count, pixel, archive/decompression, conversion-time policies.
- **Acceptance:** limits are configurable by plan/stream within safe platform maximum; violations are auditable.
- **Tests:** oversized/high-pixel/archive-bomb simulation.

## ING-006 — Duplicate detection

- **Priority/size:** P0 / M
- **Dependencies:** ING-001, ING-002
- **Deliver:** exact content-hash duplicate and customer/PO duplicate hooks.
- **Acceptance:** exact duplicate behavior configurable; duplicate never silently creates two deliveries.
- **Tests:** same file/name-different file/concurrent duplicate.

## ING-007 — Document registration and job scheduling

- **Priority/size:** P0 / M
- **Dependencies:** ING-002 through ING-006, JOB-002
- **Deliver:** atomic document/artifact/audit/outbox creation.
- **Acceptance:** successful completion queues scan/inspection exactly once; failure leaves explainable session state.
- **Tests:** transaction rollback and concurrent complete.

## ING-008 — Upload UI

- **Priority/size:** P0 / L
- **Dependencies:** ING-002, DSN-002
- **Deliver:** stream selection, drag/drop, file validation, per-file progress, cancel, partial result.
- **Acceptance:** duplicate/rejected/quarantined/failure are distinct; keyboard and screen reader work.
- **Tests:** component and E2E upload.

## ING-009 — Documents queue API

- **Priority/size:** P0 / M
- **Dependencies:** ING-001, TEN-007
- **Deliver:** cursor pagination, filter/sort/search, field projection.
- **Acceptance:** query is tenant scoped and indexed; cursors cannot cross tenants or incompatible filters.
- **Tests:** pagination/filter/security/performance.

## ING-010 — Documents queue UI

- **Priority/size:** P0 / L
- **Dependencies:** ING-009, DSN-006, DSN-007
- **Deliver:** documented columns, filters, URL state, selection, valid bulk actions.
- **Acceptance:** realistic volume/density, no stale tenant data, accessible row actions.
- **Tests:** visual, accessibility, E2E filters.

## ING-011 — Document detail and timeline API

- **Priority/size:** P0 / M
- **Dependencies:** ING-001, DB-005
- **Deliver:** summary, artifacts, current state, version context, stage/audit timeline projections.
- **Acceptance:** response redacts sensitive internals; sorted stable timeline.

## ING-012 — Document detail UI

- **Priority/size:** P0 / L
- **Dependencies:** ING-011, DSN-004
- **Deliver:** summary, extracted-data placeholder, validation, processing, delivery, audit tabs.
- **Acceptance:** state-specific actions and errors; stable deep link.

## ING-013 — Public API ingestion

- **Priority/size:** P0 / M
- **Dependencies:** TEN-009, ING-007
- **Deliver:** service-credential ingestion with idempotency and direct/multipart upload method.
- **Acceptance:** stream/capability scope; rate/quota controls; safe duplicate response.
- **Tests:** API key scope, idempotency, limits.

## ING-014 — Email intake

- **Priority/size:** P0 / L
- **Dependencies:** ING-007, CFG-002
- **Deliver:** provider-neutral inbound email contract, local Mailpit/test adapter, production adapter later, attachment extraction and routing.
- **Acceptance:** sender/body/attachment metadata retained safely; unsupported/empty/multi-attachment behavior is explicit; loops prevented.
- **Tests:** MIME fixtures and routing/security tests.

---

# EPIC PRC — Processing state, pages, mock extraction, normalization, and validation

## PRC-001 — Processing-run and stage-run models

- **Priority/size:** P0 / M
- **Dependencies:** ING-001, JOB-001
- **Deliver:** runs, stage attempts, configuration snapshot IDs, input fingerprint, metrics, cost, safe error.
- **Acceptance:** reprocess creates new run; successful stage result immutable; uniqueness enforces idempotency.
- **Tests:** run/stage lifecycle and duplicate execution.

## PRC-002 — Document state projection

- **Priority/size:** P0 / M
- **Dependencies:** PRC-001, DB-005
- **Deliver:** transition service and current-state projection from events/stages.
- **Acceptance:** only allowed transitions; actor/reason/correlation recorded; exceptional states represented.
- **Tests:** complete transition matrix.

## PRC-003 — Processing orchestrator

- **Priority/size:** P0 / L
- **Dependencies:** PRC-001, PRC-002, JOB-005
- **Deliver:** stage selection, enqueue next stage, retry classification, cancellation, replay.
- **Acceptance:** no stage lives only in process memory; restart resumes safely; configuration snapshot fixed per run.
- **Tests:** full mock workflow, cancellation, retry, duplicate messages.

## PRC-004 — Sandboxed page-rendering adapter

- **Priority/size:** P0 / L
- **Dependencies:** STO-003, ING-005
- **Deliver:** render supported documents to page images under CPU/memory/time/network restrictions.
- **Acceptance:** bounded DPI/pixels; original unmodified; converter failure is classified; active content not executed.
- **Tests:** PDF/image fixtures, malformed file, timeout.

## PRC-005 — Page and text-artifact models

- **Priority/size:** P0 / M
- **Dependencies:** PRC-004
- **Deliver:** page metadata, dimensions, image artifact, text/layout artifact references.
- **Acceptance:** stable page numbering and coordinate system documented.

## PRC-006 — Deterministic mock extraction provider

- **Priority/size:** P0 / M
- **Dependencies:** CFG-003, PRC-005
- **Deliver:** provider returns known typed values/evidence for synthetic fixtures, plus configured error/low-confidence modes.
- **Acceptance:** no network; deterministic across runs; contract matches real provider interface.
- **Tests:** contract suite.

## PRC-007 — Extracted-field and evidence models

- **Priority/size:** P0 / L
- **Dependencies:** PRC-006, CFG-003
- **Deliver:** field path, raw/normalized values, row identity, candidates, page/polygon/quote, evidence certainty, provider metadata, confidence/validation summaries.
- **Acceptance:** header and cell evidence supported; page-level evidence explicit; no fake coordinates.
- **Tests:** serialization, coordinates, candidate selection.

## PRC-008 — Canonical normalization library

- **Priority/size:** P0 / L
- **Dependencies:** DB-002, CFG-003
- **Deliver:** dates, decimals, currencies, identifiers, whitespace, UOM, boolean/enums, locale handling.
- **Acceptance:** raw value preserved; deterministic; no locale guessing without context.
- **Tests:** extensive table-driven and property-based tests.

## PRC-009 — Deterministic rule evaluator

- **Priority/size:** P0 / L
- **Dependencies:** CFG-004, PRC-008
- **Deliver:** field/cross-field rules, severity, review/block action, derived values where safe.
- **Acceptance:** pure deterministic execution; evaluation trace explains operands and result; bounded complexity.
- **Tests:** evaluator matrix and malicious/complex expression limits.

## PRC-010 — Baseline sales-order rules

- **Priority/size:** P0 / M
- **Dependencies:** PRC-009
- **Deliver:** required critical fields, date order, quantity/price/line total, header reconciliation, currency consistency, duplicate hook.
- **Acceptance:** tolerances and rounding are explicit and versioned.
- **Tests:** success/failure fixtures.

## PRC-011 — Baseline confidence policy

- **Priority/size:** P0 / M
- **Dependencies:** PRC-007, PRC-009
- **Deliver:** risk-aware confidence inputs, field criticality, route decision.
- **Acceptance:** provider confidence is only one signal; critical fields have conservative gates; reasons returned.
- **Tests:** policy decision matrix.

## PRC-012 — Mock end-to-end pipeline

- **Priority/size:** P0 / L
- **Dependencies:** PRC-003 through PRC-011
- **Deliver:** file through render/mock extract/normalize/validate/confidence/review-or-approve.
- **Acceptance:** deterministic fixture reaches expected state; all stage artifacts/timeline/audit/metrics exist.
- **Tests:** integration and worker-kill recovery.

## PRC-013 — Reprocess/cancel APIs

- **Priority/size:** P0 / M
- **Dependencies:** PRC-012, TEN-007
- **Deliver:** retry same stage, new current-config run, authorized historical-config run, cancel.
- **Acceptance:** UI/API explains consequence; approved/exported records protected by policy.
- **Tests:** state and permission matrix.

## PRC-014 — Processing timeline UI

- **Priority/size:** P0 / M
- **Dependencies:** PRC-012, ING-012
- **Deliver:** stages, attempts, provider, latency, warning, safe error, retry controls.
- **Acceptance:** live refresh preserves scroll/context; no sensitive raw response.

---

# EPIC REV — Review queues, Review Studio, corrections, and approval

## REV-001 — Review-task model and routing

- **Priority/size:** P0 / M
- **Dependencies:** PRC-011, TEN-006
- **Deliver:** queue, reasons, priority, SLA, assignment, states, version.
- **Acceptance:** one active primary task per document/run policy; reason list is field/rule linked.
- **Tests:** routing and state matrix.

## REV-002 — Review queue API

- **Priority/size:** P0 / M
- **Dependencies:** REV-001, TEN-007
- **Deliver:** list/filter/sort, my/unassigned/SLA/blocked, claim/release.
- **Acceptance:** atomic claim; unauthorized queue invisible; cursor safe.
- **Tests:** concurrent claim and security.

## REV-003 — Review queue UI

- **Priority/size:** P0 / L
- **Dependencies:** REV-002, DSN-006, DSN-007
- **Deliver:** required views, filters, reason summary, SLA, assignment, “start next” explanation.
- **Acceptance:** keyboard queue navigation; URL/deep links; empty/error/loading states.

## REV-004 — Document viewer component

- **Priority/size:** P0 / L
- **Dependencies:** PRC-005, STO-004, DSN-002
- **Deliver:** PDF/page rendering, thumbnails, zoom, rotate, page navigation, text search, responsive loading.
- **Acceptance:** bounded memory; page cache; accessible controls; signed URL renewal.
- **Tests:** component, keyboard, large-document smoke.

## REV-005 — Evidence overlay system

- **Priority/size:** P0 / L
- **Dependencies:** REV-004, PRC-007
- **Deliver:** page/polygon overlays, active/related candidates, click field→source and source→field.
- **Acceptance:** coordinate transforms correct at zoom/rotation; page-level evidence handled honestly; non-visual description exists.
- **Tests:** geometry, visual, accessibility.

## REV-006 — Review read model API

- **Priority/size:** P0 / M
- **Dependencies:** REV-001, PRC-007
- **Deliver:** task, fields, candidates, validations, line items, evidence, history, configuration context.
- **Acceptance:** optimized bounded response; artifact URLs separate/short-lived; permission enforced.

## REV-007 — Header field editor

- **Priority/size:** P0 / L
- **Dependencies:** REV-006, REV-005, DSN-002
- **Deliver:** reason navigator, fields, raw/normalized, confidence, validation, candidates, provenance.
- **Acceptance:** documented keyboard map; changes validated; source highlight; autosave state visible.
- **Tests:** component, keyboard, screen reader, E2E correction.

## REV-008 — Line-item grid

- **Priority/size:** P0 / L
- **Dependencies:** REV-006, REV-005, DSN-006
- **Deliver:** virtualized editable grid, frozen columns, cell evidence, add/remove/split/merge, undo, copy/paste, totals footer.
- **Acceptance:** hundreds of cells remain responsive; keyboard spreadsheet flow; row identity stable; destructive edit undoable.
- **Tests:** grid unit/component/performance/E2E.

## REV-009 — Field correction persistence

- **Priority/size:** P0 / L
- **Dependencies:** REV-006, DB-005
- **Deliver:** append-only corrections, reason, evidence selection, reviewer, task version; recompute affected validation/confidence.
- **Acceptance:** optimistic concurrency; no silent overwrite; immutable original extraction retained.
- **Tests:** conflict, audit, recomputation.

## REV-010 — Catalog candidate interaction shell

- **Priority/size:** P0 / M
- **Dependencies:** REV-007; full data arrives in CAT epic
- **Deliver:** candidate picker contract/UI and manual override reason.
- **Acceptance:** feature scores/explanation supported; loading/no-match/error states.

## REV-011 — Comments and escalation

- **Priority/size:** P0 / M
- **Dependencies:** REV-001, REV-006
- **Deliver:** comments, mentions if available, block/escalate reason and ownership.
- **Acceptance:** comments audited and tenant scoped; sensitive notifications avoid raw content.
- **Tests:** permissions and state changes.

## REV-012 — Approval and rejection service

- **Priority/size:** P0 / L
- **Dependencies:** REV-009, PRC-009, PRC-011
- **Deliver:** final validation, policy gate, approval/rejection/block/reopen, second-approval hook.
- **Acceptance:** approval transactional and idempotent; remaining warnings/overrides captured; critical blockers cannot be bypassed without authorized override policy.
- **Tests:** approval matrix, duplicate request, concurrency.

## REV-013 — Approval UX

- **Priority/size:** P0 / M
- **Dependencies:** REV-012, REV-007, REV-008
- **Deliver:** completion summary, remaining warnings, export destination, approve/reject/escalate actions.
- **Acceptance:** disabled reason visible; shortcut does not accidentally approve; screen-reader announcement.

## REV-014 — Review conflict UI

- **Priority/size:** P0 / M
- **Dependencies:** REV-009
- **Deliver:** server/user values, editor metadata, reload/merge choices.
- **Acceptance:** no data loss; task state remains clear.

## REV-015 — Review Studio responsive layout

- **Priority/size:** P0 / M
- **Dependencies:** REV-004 through REV-014
- **Deliver:** resizable panels, persisted preference, compact desktop/tablet modes.
- **Acceptance:** usable at 1280×720; no inaccessible hidden content; mobile read/approve path defined.

## REV-016 — Review usability benchmark

- **Priority/size:** P0 / M
- **Dependencies:** REV-015
- **Deliver:** scripted study, baseline manual-entry comparison, issues and remediation tasks.
- **Acceptance:** critical path completion, error rate, evidence comprehension, and timing meet agreed pilot targets; critical UX issues block release.

---

# EPIC CAN — Canonical sales-order contract

## CAN-001 — Define versioned canonical order JSON Schema

- **Priority/size:** P0 / L
- **Dependencies:** PRODUCT requirements, CFG-003
- **Deliver:** header, parties/addresses, dates, terms, totals, line items, identifiers, notes, source references, extension mechanism.
- **Acceptance:** schema has explicit nullability/required fields/decimal and date representation; no ERP-specific leakage.
- **Tests:** valid/invalid fixtures and backward-compatibility rules.

## CAN-002 — Generate Python and TypeScript models

- **Priority/size:** P0 / M
- **Dependencies:** CAN-001
- **Deliver:** deterministic generation and drift check.
- **Acceptance:** generated files are reproducible; API/provider mappings use generated/validated models.

## CAN-003 — Extraction-to-canonical mapper

- **Priority/size:** P0 / L
- **Dependencies:** CAN-002, PRC-008
- **Deliver:** selected reviewed values to canonical payload with provenance references.
- **Acceptance:** mapping errors block approval/export clearly; canonical payload immutable by version.
- **Tests:** fixture mappings including line items.

## CAN-004 — Canonical payload viewer

- **Priority/size:** P0 / M
- **Dependencies:** CAN-003, DSN-002
- **Deliver:** formatted/tree/JSON views, copy/download permission, redaction.
- **Acceptance:** large payload safe; secrets never included.

---

# EPIC EXP — Mapping, export, webhook delivery, retry, and replay

## EXP-001 — Integration and mapping-profile models

- **Priority/size:** P0 / M
- **Dependencies:** CAN-001, CFG-005
- **Deliver:** integration root, type, state, credential reference, mapping versions, target schema.
- **Acceptance:** published mapping immutable; credentials separated.

## EXP-002 — Deterministic mapping engine

- **Priority/size:** P0 / L
- **Dependencies:** EXP-001, CAN-002
- **Deliver:** field mapping, constants, formatting, conditional/default transforms, target validation.
- **Acceptance:** no arbitrary code; transform trace; bounded execution.
- **Tests:** transform matrix and malformed mapping.

## EXP-003 — Mapping profile API and validation

- **Priority/size:** P0 / M
- **Dependencies:** EXP-002, TEN-007
- **Deliver:** draft, validate with sample, publish, compare.
- **Acceptance:** secret/credential values absent from response; optimistic concurrency.

## EXP-004 — Mapping studio UI

- **Priority/size:** P0 / L
- **Dependencies:** EXP-003, DSN-002
- **Deliver:** canonical/target mapping, transform editor, required-field status, sample preview.
- **Acceptance:** keyboard accessible; validation errors linked to mapping row; version state visible.

## EXP-005 — Export-job and delivery-attempt models

- **Priority/size:** P0 / M
- **Dependencies:** CAN-003, JOB-001
- **Deliver:** approved payload reference, mapping version, business idempotency key, state, attempts.
- **Acceptance:** payload fixed across retry; attempts append-only.

## EXP-006 — JSON and CSV export adapters

- **Priority/size:** P0 / M
- **Dependencies:** EXP-002, EXP-005, STO-003
- **Deliver:** immutable export artifacts and authorized download.
- **Acceptance:** deterministic encoding, decimal/date correctness, schema/version metadata.
- **Tests:** golden exports.

## EXP-007 — Signed webhook adapter

- **Priority/size:** P0 / L
- **Dependencies:** EXP-005, TEN-009
- **Deliver:** timestamped signature, idempotency header, timeout, response classification, redacted storage.
- **Acceptance:** receiver can verify signature; retries do not create new business key; SSRF protections and destination allowlist policy.
- **Tests:** signature, replay window, timeout, retryable/non-retryable responses, private-network blocking.

## EXP-008 — Export orchestration

- **Priority/size:** P0 / L
- **Dependencies:** EXP-005 through EXP-007, REV-012, JOB-005
- **Deliver:** approval triggers mapping/export/delivery; retries/replay/cancel.
- **Acceptance:** exactly-once business intent; no re-extraction for replay; state/timeline/audit complete.
- **Tests:** duplicate approval, receiver timeout, retry, terminal rejection.

## EXP-009 — Delivery history API/UI

- **Priority/size:** P0 / L
- **Dependencies:** EXP-008, DSN-006
- **Deliver:** jobs, attempts, payload/mapping version, safe response, retry/replay controls.
- **Acceptance:** permission separation; replay reason required; no secret headers displayed.

## EXP-010 — Generic ERP adapter contract

- **Priority/size:** P0 / M
- **Dependencies:** EXP-008
- **Deliver:** connection test, map, deliver, classify result, health capabilities.
- **Acceptance:** first specific ERP can be added without changing export orchestration.

## EXP-011 — First production ERP adapter

- **Priority/size:** P1 / L
- **Dependencies:** EXP-010 and pilot ERP choice ADR
- **Deliver:** the pilot-selected ERP adapter, sandbox tests, credential
  lifecycle, and runbook. QuickBooks Online is the current executable candidate;
  NetSuite/Dynamics/SAP are connection-test-only until their idempotent upserts
  exist.
- **Acceptance:** idempotency/retry/error classification, mapping, OAuth/token
  lifecycle, and support ownership are proved with the destination owner in an
  official sandbox.

---

# EPIC AIO — Native text, OCR, LLM providers, routing, and evaluation

## AIO-001 — Define provider contracts and capability registry

- **Priority/size:** P0 / M
- **Dependencies:** PRC-006, CFG-005
- **Deliver:** native text, OCR/layout, classifier/splitter, extraction interfaces, capability/region/language/data-policy metadata.
- **Acceptance:** vendor DTOs remain inside adapters; contract fixtures shared.

## AIO-002 — Native PDF text adapter

- **Priority/size:** P0 / L
- **Dependencies:** AIO-001, PRC-004
- **Deliver:** text spans, coordinates, page coverage, quality measures.
- **Acceptance:** encrypted/corrupt/missing-font PDFs classified; coordinate system matches viewer.
- **Tests:** digital PDF corpus.

## AIO-003 — Image preprocessing pipeline

- **Priority/size:** P0 / M
- **Dependencies:** PRC-004
- **Deliver:** orientation, deskew, blank detection, conservative contrast/noise options, quality report.
- **Acceptance:** original render retained; processing bounded; changes recorded.
- **Tests:** image fixtures and quality metrics.

## AIO-004 — Tesseract OCR adapter

- **Priority/size:** P0 / L
- **Dependencies:** AIO-001, AIO-003
- **Deliver:** word/line/block text, coordinates, confidence, language packs.
- **Acceptance:** supported languages configured per stream; timeout/resource limits; contract suite.

## AIO-005 — Optional local layout OCR adapter

- **Priority/size:** P1 / L
- **Dependencies:** AIO-001, AIO-003
- **Deliver:** PaddleOCR or selected equivalent with tables/layout where useful.
- **Acceptance:** benchmarked, not assumed superior; isolated dependency footprint.

## AIO-006 — Hosted OCR adapter

- **Priority/size:** P0 / L
- **Dependencies:** AIO-001, provider decision
- **Deliver:** one managed OCR provider with retention/region metadata, timeout, request ID, cost accounting.
- **Acceptance:** unavailable credentials disable capability clearly; raw customer content not logged.
- **Tests:** mocked provider and opt-in live smoke.

## AIO-007 — Local OpenAI-compatible extraction adapter

- **Priority/size:** P0 / M
- **Dependencies:** AIO-001, CAN-001
- **Deliver:** configurable local endpoint, structured schema request, timeout, no tool access.
- **Acceptance:** optional profile; clear model capability warning; contract suite.

## AIO-008 — First hosted extraction adapter

- **Priority/size:** P0 / L
- **Dependencies:** AIO-001, CAN-001, production provider decision
- **Deliver:** structured output, minimal document context, request metadata, cost/latency, retention mode.
- **Acceptance:** schema invalid output rejected; customer-data terms documented before production use.
- **Tests:** mocked errors, repair, timeout, rate limit.

## AIO-009 — Second hosted extraction adapter

- **Priority/size:** P1 / L
- **Dependencies:** AIO-008
- **Deliver:** alternate provider proving portability/fallback.
- **Acceptance:** same gold evaluation and contract suite.

## AIO-010 — Prompt/instruction version storage

- **Priority/size:** P0 / M
- **Dependencies:** CFG-005, AIO-008
- **Deliver:** versioned extraction instructions linked to schema/stream version.
- **Acceptance:** published immutable; every call references exact version; content access permissioned.

## AIO-011 — Prompt-injection-safe request builder

- **Priority/size:** P0 / L
- **Dependencies:** AIO-008, AIO-010
- **Deliver:** system/document separation, data delimiters, no tools/URLs, bounded pages/tokens, redaction hook.
- **Acceptance:** adversarial document text cannot alter tool/provider policy; security corpus passes.

## AIO-012 — Schema repair and fallback policy

- **Priority/size:** P0 / M
- **Dependencies:** AIO-008, JOB-005
- **Deliver:** bounded repair attempt, fallback classification, review route.
- **Acceptance:** retry count/cost bounded; every attempt recorded; no infinite loops.
- **Tests:** invalid JSON/type/missing field/rate-limit cases.

## AIO-013 — Provider router

- **Priority/size:** P0 / L
- **Dependencies:** AIO-002 through AIO-012, CFG-005
- **Deliver:** route by native/scanned, language, region, privacy, capability, budget, quality, health, fallback order.
- **Acceptance:** deterministic explanation; local-only honored; budget cannot be exceeded silently.
- **Tests:** policy matrix.

## AIO-014 — Evidence resolver

- **Priority/size:** P0 / L
- **Dependencies:** AIO-002, AIO-004, AIO-008, PRC-007
- **Deliver:** quote-to-coordinate exact/fuzzy matching, ambiguity handling, coordinate confidence.
- **Acceptance:** no fabricated coordinates; page-level fallback explicit.
- **Tests:** exact, duplicate quote, OCR variance, no match.

## AIO-015 — Gold dataset schema and storage

- **Priority/size:** P0 / M
- **Dependencies:** CAN-001, STO-003
- **Deliver:** dataset/version/document ground truth, split/class/fields/lines/validations, privacy classification.
- **Acceptance:** datasets tenant/test scoped; no customer data reused across tenants without explicit agreement.

## AIO-016 — Evaluation runner

- **Priority/size:** P0 / L
- **Dependencies:** AIO-015, AIO-013
- **Deliver:** run candidate configuration, compute exact/normalized/line metrics, latency/cost/errors.
- **Acceptance:** repeatable, resumable, no ordinary PR external calls by default.
- **Tests:** known-score fixtures.

## AIO-017 — Evaluation comparison and promotion gate

- **Priority/size:** P0 / L
- **Dependencies:** AIO-016, CFG-007
- **Deliver:** current/candidate diff by field/cohort, critical regression and false-auto-approval gate.
- **Acceptance:** publication blocked on gate; authorized waiver requires reason/audit and cannot bypass prohibited critical policy.

## AIO-018 — Simulation UI

- **Priority/size:** P0 / L
- **Dependencies:** AIO-017, CFG-014
- **Deliver:** aggregate and document/field drill-down, cost/review/quality change.
- **Acceptance:** averages never hide critical regression; accessible tables accompany charts.

## AIO-019 — Provider administration UI

- **Priority/size:** P0 / M
- **Dependencies:** AIO-013, DSN-002
- **Deliver:** approved providers, capability, health, region, data policy, credential reference, routing preview.
- **Acceptance:** secrets never redisplayed; local-only/data-retention warnings explicit.

---

# EPIC CAT — Catalogs, matching, and business validation

## CAT-001 — Catalog/version/record models

- **Priority/size:** P0 / M
- **Dependencies:** TEN-004, CFG-002
- **Deliver:** type, source, active version, records, aliases, effective dates, source IDs.
- **Acceptance:** activated version immutable; stream pins version or explicit rolling policy.

## CAT-002 — CSV import pipeline

- **Priority/size:** P0 / L
- **Dependencies:** CAT-001, JOB-005, STO-003
- **Deliver:** upload, parse, map, validate, preview added/changed/deactivated, activate.
- **Acceptance:** malformed rows reported; partial import not activated accidentally; encoding/formula injection handled.
- **Tests:** CSV variants and malicious cells.

## CAT-003 — XLSX import pipeline

- **Priority/size:** P0 / M
- **Dependencies:** CAT-002
- **Deliver:** safe workbook reading, sheet selection, formula handling, same validation preview.
- **Acceptance:** macros never executed; formula values treated safely.

## CAT-004 — Catalog API

- **Priority/size:** P0 / M
- **Dependencies:** CAT-003, TEN-007
- **Deliver:** list/detail/import/status/activate/versions/records/search.
- **Acceptance:** large records paginated and indexed; permission split between read/import/activate.

## CAT-005 — Catalog manager UI

- **Priority/size:** P0 / L
- **Dependencies:** CAT-004, DSN-006
- **Deliver:** list, import wizard, mapping, validation report, version activation, record browser.
- **Acceptance:** partial failures clear; keyboard and accessibility complete.

## CAT-006 — Normalized and exact matching

- **Priority/size:** P0 / M
- **Dependencies:** CAT-001, PRC-008
- **Deliver:** indexed identifiers, normalized text, date-effective filtering.
- **Acceptance:** deterministic reasons and candidates; tenant/stream scope.
- **Tests:** identifier/alias/effective-date matrix.

## CAT-007 — Weighted fuzzy matching

- **Priority/size:** P0 / L
- **Dependencies:** CAT-006
- **Deliver:** configurable feature weights, trigram/string metrics, candidate cap, feature score explanation.
- **Acceptance:** no unexplained aggregate score; thresholds/version pinned.
- **Tests:** ship-to/customer/material labeled fixtures.

## CAT-008 — Optional semantic matching adapter

- **Priority/size:** P1 / M
- **Dependencies:** CAT-007, AIO-001
- **Deliver:** embeddings only for measured ambiguity; tenant-safe index.
- **Acceptance:** never sole evidence for a critical match without policy; provider/data terms apply.

## CAT-009 — Matching policy and confidence integration

- **Priority/size:** P0 / M
- **Dependencies:** CAT-007, PRC-011
- **Deliver:** candidate selection/auto-match/review thresholds by field type.
- **Acceptance:** selected record and feature scores retained; reviewer override feeds evaluation.

## CAT-010 — Review candidate picker integration

- **Priority/size:** P0 / M
- **Dependencies:** CAT-009, REV-010
- **Deliver:** search/candidates, feature explanation, manual record selection, no-match flow.
- **Acceptance:** correction/reason/audit; recalculates dependent validation.

## CAT-011 — Customer and ship-to validation rules

- **Priority/size:** P0 / M
- **Dependencies:** CAT-009, PRC-009
- **Deliver:** ship-to belongs to customer, sold-to/bill-to validity, country/postcode consistency.
- **Tests:** valid/invalid fixtures.

## CAT-012 — Material, UOM, price, and package validation

- **Priority/size:** P0 / L
- **Dependencies:** CAT-009, PRC-009
- **Deliver:** customer item mapping, material effective date, UOM conversion/allowed set, price/tolerance, package multiple.
- **Acceptance:** rounding/currency/effective-date semantics explicit.
- **Tests:** line-item matrix.

## CAT-013 — Duplicate PO business validation

- **Priority/size:** P0 / M
- **Dependencies:** ING-006, CAT-009
- **Deliver:** customer/PO/date/status duplicate candidates and policy.
- **Acceptance:** warns/blocks according to stream; legitimate revisions can be overridden with reason.

---

# EPIC ANA — Dashboards, audit, usage, and support operations

## ANA-001 — Operational analytics event model

- **Priority/size:** P0 / M
- **Dependencies:** PRC-001, REV-001, EXP-005
- **Deliver:** safe fact tables or queries for volume, latency, backlog, SLA, export.
- **Acceptance:** metrics have documented numerator/denominator/timezone; tenant scoped.

## ANA-002 — Quality analytics model

- **Priority/size:** P0 / M
- **Dependencies:** AIO-016, REV-009
- **Deliver:** field accuracy/correction, line accuracy, STP, false auto-approval, calibration cohorts.
- **Acceptance:** ground-truth availability and sample size exposed; no misleading quality claim.

## ANA-003 — Cost and usage ledger

- **Priority/size:** P0 / L
- **Dependencies:** AIO provider calls, PRC-001
- **Deliver:** usage by tenant/stream/document/page/provider/model/cost category, immutable adjustments.
- **Acceptance:** provider billed units and estimated cost distinguished; reconciliation possible.

## ANA-004 — Operations dashboard API/UI

- **Priority/size:** P0 / L
- **Dependencies:** ANA-001, DSN-007
- **Deliver:** needs attention, metrics, queue health, processing, exceptions, drill-down.
- **Acceptance:** every tile links to filtered operational view; no decorative metrics.

## ANA-005 — Quality dashboard API/UI

- **Priority/size:** P0 / L
- **Dependencies:** ANA-002
- **Deliver:** critical fields, lines, corrections, STP, false approval, calibration, cohort/version drill-down.
- **Acceptance:** sample sizes and confidence caveats visible.

## ANA-006 — Cost dashboard and quota UI

- **Priority/size:** P0 / M
- **Dependencies:** ANA-003
- **Deliver:** usage and budget by stream/provider, quota risk, alerts.
- **Acceptance:** values labeled estimated/final; no provider secret/billing identifiers.

## ANA-007 — Audit query and export

- **Priority/size:** P0 / L
- **Dependencies:** DB-005, STO-003
- **Deliver:** filtered audit API/UI and asynchronous signed export bundle.
- **Acceptance:** export permissioned, integrity manifest included, sensitive fields controlled.
- **Tests:** large export, authorization, hash manifest.

## ANA-008 — Internal support console

- **Priority/size:** P0 / L
- **Dependencies:** ANA-001, JOB-006, TEN support policy
- **Deliver:** tenant lookup, transaction timeline, health, safe errors, controlled actions.
- **Acceptance:** customer navigation cannot reach it; support content access requires explicit grant; every action audited.

## ANA-009 — Feature flags and quota policy

- **Priority/size:** P0 / M
- **Dependencies:** TEN-004, ANA-003
- **Deliver:** typed flags/quotas with scope, owner, expiry/review date.
- **Acceptance:** no security control disabled solely by a normal feature flag; changes audited.

---

# EPIC SEC — Security, privacy, and compliance implementation

## SEC-001 — Threat model

- **Priority/size:** P0 / L
- **Dependencies:** architecture baseline
- **Deliver:** assets, trust boundaries, entry points, abuse cases, mitigations, residual risk for web/API/worker/storage/providers/integrations/support.
- **Acceptance:** reviewed before pilot and linked to test/runbook tasks.

## SEC-002 — Security headers, CSP, CORS, CSRF

- **Priority/size:** P0 / M
- **Dependencies:** FND-003, FND-005
- **Deliver:** production policies and tests.
- **Acceptance:** strict allowlists; no wildcard credentialed CORS; CSP supports viewer without broad unsafe exceptions.

## SEC-003 — Rate limiting and abuse controls

- **Priority/size:** P0 / M
- **Dependencies:** TEN-001, FND-003
- **Deliver:** limits by IP/principal/tenant/key/operation and safe backpressure.
- **Acceptance:** login, upload, API ingestion, signed URL, retry/replay protected; limits observable.

## SEC-004 — Converter and worker sandbox hardening

- **Priority/size:** P0 / L
- **Dependencies:** PRC-004, infrastructure
- **Deliver:** non-root, read-only root, temp isolation, no network, seccomp/capability/resource controls where platform supports.
- **Acceptance:** sandbox profile documented and tested.

## SEC-005 — Secret-store interface and production adapter

- **Priority/size:** P0 / M
- **Dependencies:** FND-007
- **Deliver:** reference-based secrets, local env/test adapter, production secret manager adapter.
- **Acceptance:** database stores references; secret values not returned by API; rotation supported.

## SEC-006 — Sensitive logging and telemetry test suite

- **Priority/size:** P0 / M
- **Dependencies:** FND-008, AIO/EXP modules
- **Deliver:** canary secret/document values through critical paths and assert absent from logs/traces/metrics/errors.
- **Acceptance:** required CI security test.

## SEC-007 — Prompt-injection corpus and tests

- **Priority/size:** P0 / M
- **Dependencies:** AIO-011
- **Deliver:** malicious instructions, URLs, data exfiltration attempts, cross-document references.
- **Acceptance:** extraction remains schema-only and does not perform prohibited actions.

## SEC-008 — Retention-policy engine

- **Priority/size:** P0 / L
- **Dependencies:** STO-003, CFG-005
- **Deliver:** retention classes, legal hold hook, scheduled eligibility, deletion state.
- **Acceptance:** published stream policy pins behavior; deletion never occurs before eligibility/approval.

## SEC-009 — Customer export workflow

- **Priority/size:** P0 / L
- **Dependencies:** ANA-007, STO-005
- **Deliver:** scoped organization/document export with manifest, asynchronous generation, expiry.
- **Acceptance:** includes documented categories; authorization and audit complete.

## SEC-010 — Data deletion workflow

- **Priority/size:** P0 / L
- **Dependencies:** SEC-008, STO-005
- **Deliver:** database/object/derived/cache/provider deletion orchestration and tombstone/audit.
- **Acceptance:** reconciles all artifacts; backup retention exception documented; retries safe.
- **Tests:** complete and partial deletion recovery.

## SEC-011 — Dependency, container, secret, and SBOM pipeline

- **Priority/size:** P0 / M
- **Dependencies:** FND-010
- **Deliver:** scans, severity policy, SBOM artifacts, exception process.
- **Acceptance:** unresolved critical findings block release.

## SEC-012 — External penetration test and remediation

- **Priority/size:** P0 / L
- **Dependencies:** complete staging P0
- **Deliver:** scoped test, findings, fixes, retest evidence.
- **Acceptance:** no unresolved critical; high findings have approved treatment before pilot.

## SEC-013 — Security and privacy documentation package

- **Priority/size:** P0 / L
- **Dependencies:** SEC-001 through SEC-012
- **Deliver:** data flow, encryption, access, subprocessors, retention, AI use, vulnerability disclosure, incident response summary.
- **Acceptance:** claims match actual implementation.

---

# EPIC REL — Reliability, performance, deployment, and operations

## REL-001 — Container images

- **Priority/size:** P0 / M
- **Dependencies:** FND apps
- **Deliver:** minimal non-root API/worker images, web artifact, pinned base images, health checks.
- **Acceptance:** no build tools/secrets in runtime; scanner passes.

## REL-002 — Infrastructure-as-code baseline

- **Priority/size:** P0 / L
- **Dependencies:** REL-001, provider choices
- **Deliver:** shared dev/staging/production modules for compute, database, storage, secrets, network, telemetry.
- **Acceptance:** environment differences are parameters; no manual snowflake production resource.

## REL-003 — Database backup and PITR configuration

- **Priority/size:** P0 / M
- **Dependencies:** production database choice
- **Deliver:** automated backups/PITR, encryption, monitoring, retention.
- **Acceptance:** documented RPO/RTO and restore ownership.

## REL-004 — Restore rehearsal

- **Priority/size:** P0 / L
- **Dependencies:** REL-003, STO-005
- **Deliver:** restore fresh database, restore/copy objects, reconcile, run smoke/gold tests.
- **Acceptance:** achieved RPO/RTO recorded; discrepancies resolved.

## REL-005 — Deployment workflow

- **Priority/size:** P0 / L
- **Dependencies:** REL-001, REL-002, FND-010
- **Deliver:** immutable build, migration job, deploy, readiness, smoke, evidence.
- **Acceptance:** same artifact promoted; failed readiness stops rollout.

## REL-006 — Rollback strategy and expand/contract migration policy

- **Priority/size:** P0 / M
- **Dependencies:** REL-005
- **Deliver:** prior artifact rollback, schema compatibility guidelines, configuration/provider rollback.
- **Acceptance:** tested on staging.

## REL-007 — Alerts and ownership

- **Priority/size:** P0 / M
- **Dependencies:** metrics from jobs/processing/export/security
- **Deliver:** queue, provider, schema error, cost, SLA, export, backup, auth anomaly, quota alerts.
- **Acceptance:** each alert has severity, owner, threshold rationale, runbook, test signal.

## REL-008 — Required runbooks

- **Priority/size:** P0 / L
- **Dependencies:** REL-007
- **Deliver:** provider outage, backlog, failed export, bad release, tenant access incident, credential exposure, deletion, restore, object recovery, quota exhaustion, security communication.
- **Acceptance:** runbooks include detection, containment, recovery, verification, communication, follow-up.

## REL-009 — Performance test suite

- **Priority/size:** P0 / L
- **Dependencies:** queues/review/catalog/export
- **Deliver:** workloads and thresholds for API, queue, viewer, line grid, catalog matching, worker concurrency, export burst.
- **Acceptance:** results stored per release; regressions beyond threshold block release or require waiver.

## REL-010 — Resilience tests

- **Priority/size:** P0 / L
- **Dependencies:** JOB-005, AIO-013, EXP-008
- **Deliver:** worker kill, provider timeout/outage, database transient error, storage failure, duplicate event, receiver timeout.
- **Acceptance:** no data loss or duplicate business delivery; user-visible state recoverable.

## REL-011 — Status and incident communication

- **Priority/size:** P0 / M
- **Dependencies:** REL-007
- **Deliver:** internal incident channel/process, customer status path, templates, severity model.
- **Acceptance:** commitments match support plan; test exercise completed.

## REL-012 — Free-to-paid/provider migration rehearsal

- **Priority/size:** P0 / L
- **Dependencies:** REL-002, REL-004, AIO-013
- **Deliver:** move a dev database/object set or switch provider adapter, reconcile, run gold/E2E, rollback.
- **Acceptance:** written cutover timings, gaps, and remediation.

---

# EPIC GTM — Billing readiness, onboarding, and customer operations

## GTM-001 — Usage and plan model

- **Priority/size:** P0 / M
- **Dependencies:** ANA-003
- **Deliver:** plan, included units, quotas, overage policy representation without tying to one billing vendor.
- **Acceptance:** product enforcement and reporting use same quota source.

## GTM-002 — Billing export/reconciliation

- **Priority/size:** P0 / M
- **Dependencies:** GTM-001
- **Deliver:** monthly tenant usage statement and adjustment workflow.
- **Acceptance:** pages/documents/provider costs/reprocessed usage rules documented.

## GTM-003 — Guided onboarding checklist

- **Priority/size:** P0 / L
- **Dependencies:** CFG, ING, CAT, EXP features
- **Deliver:** organization→stream→schema→catalog→sample→integration→go-live checklist.
- **Acceptance:** resumable; each item links to exact screen and validation.

## GTM-004 — Sample stream template

- **Priority/size:** P0 / M
- **Dependencies:** CFG-007, PRC-010
- **Deliver:** baseline sales-order process/stream/schema/rules/provider mock and sample catalogs.
- **Acceptance:** versioned and importable; not silently modified for existing tenants.

## GTM-005 — In-app help and operational guidance

- **Priority/size:** P0 / M
- **Dependencies:** core UI
- **Deliver:** contextual help for upload, review, confidence, publish, replay, errors.
- **Acceptance:** concise and linked to canonical docs; does not obscure work.

## GTM-006 — Support intake and severity workflow

- **Priority/size:** P0 / M
- **Dependencies:** ANA-008, REL-011
- **Deliver:** support reference, secure attachment path, severity/response ownership.
- **Acceptance:** support access/data handling follows policy.

---

# EPIC PIL — Pilot preparation and controlled launch

## PIL-001 — Select pilot workflow and destination

- **Priority/size:** P0 / S
- **Dependencies:** customer decision
- **Deliver:** exact stream, languages, volumes, document sources, catalogs, ERP/export, critical fields, risk thresholds.
- **Acceptance:** signed scope and data-processing assumptions.

## PIL-002 — Build representative gold dataset

- **Priority/size:** P0 / L
- **Dependencies:** PIL-001, AIO-015
- **Deliver:** labeled documents by customer/layout/quality/language/exception.
- **Acceptance:** double-review process for critical ground truth; privacy and retention agreed.

## PIL-003 — Import and reconcile master data

- **Priority/size:** P0 / M
- **Dependencies:** PIL-001, CAT tasks
- **Deliver:** customer/ship-to/material/UOM/price catalogs and quality report.
- **Acceptance:** duplicates, missing keys, effective dates, and mappings resolved or documented.

## PIL-004 — Configure and simulate pilot stream

- **Priority/size:** P0 / L
- **Dependencies:** PIL-002, PIL-003, AIO-018
- **Deliver:** published candidate after simulation and gate review.
- **Acceptance:** critical metrics and false-auto-approval meet agreed threshold; all regressions reviewed.

## PIL-005 — Parallel-run mode

- **Priority/size:** P0 / M
- **Dependencies:** PIL-004
- **Deliver:** process real incoming documents without autonomous destination submission; compare to existing process.
- **Acceptance:** every output supervised; discrepancy capture standardized.

## PIL-006 — Daily pilot operations review

- **Priority/size:** P0 / M
- **Dependencies:** PIL-005
- **Deliver:** dashboard/report covering volume, backlog, accuracy, corrections, exports, incidents, cost.
- **Acceptance:** owner and remediation assigned for every material issue.

## PIL-007 — Controlled auto-release policy

- **Priority/size:** P0 / L
- **Dependencies:** sufficient pilot evidence
- **Deliver:** field/stream/customer risk policy, second approval, rollback switch.
- **Acceptance:** only cohorts with proven performance enabled; critical exceptions remain reviewed.

## PIL-008 — Pilot exit report

- **Priority/size:** P0 / M
- **Dependencies:** pilot period
- **Deliver:** ROI, accuracy, review time, STP, false approvals, export reliability, incidents, cost, expansion recommendation.
- **Acceptance:** evidence linked; unresolved risks explicit.

---

# P1 enterprise expansion backlog

## ENT-001 — SAML SSO

- **Dependencies:** TEN-003
- **Acceptance:** organization domain/connection routing, safe account linking, tested logout/session behavior.

## ENT-002 — SCIM provisioning

- **Dependencies:** TEN roles/memberships
- **Acceptance:** create/update/deactivate/group-role mapping, idempotency, audit.

## ENT-003 — Customer-managed keys

- **Dependencies:** storage/database provider capability
- **Acceptance:** key policy/rotation/revocation/recovery and customer responsibility documented.

## ENT-004 — Regional processing plane

- **Dependencies:** control/processing separation
- **Acceptance:** data routing and residency tests, regional provider policy, operational support.

## ENT-005 — Customer-hosted/private worker

- **Dependencies:** stable worker and provider contracts
- **Acceptance:** secure enrollment, outbound-only control channel where possible, upgrade/revocation/audit.

## ENT-006 — SFTP/shared-folder intake

- **Dependencies:** ING contract
- **Acceptance:** polling/idempotency/archive/error handling and credential rotation.

## ENT-007 — Supervisor quality sampling

- **Dependencies:** REV tasks
- **Acceptance:** random/risk-based samples, unbiased metrics, reviewer feedback workflow.

## ENT-008 — Additional ERP adapters

- **Dependencies:** EXP-010
- **Acceptance:** adapter contract, destination sandbox, idempotency/error mapping/runbook.

## ENT-009 — Additional document processes

- **Dependencies:** proven platform P0
- **Acceptance:** new process uses existing foundations; no sales-order assumptions leak into generic modules.

---

## 3. Cross-cutting completion checklist

Before merging any task, verify:

### Product

- [ ] Behavior matches a documented user outcome.
- [ ] Terminology uses organization/workspace/process/stream consistently.
- [ ] Permission and plan behavior are explicit.

### Backend

- [ ] Tenant scope required.
- [ ] Transactions and idempotency defined.
- [ ] Error codes are safe and actionable.
- [ ] External/provider calls have timeout, retry classification, and correlation.
- [ ] Migrations are forward-compatible and tested.

### Frontend

- [ ] Loading, empty, error, permission, conflict, and long-running states exist.
- [ ] Keyboard and focus behavior work.
- [ ] Accessible semantics and labels pass.
- [ ] Responsive and realistic-data states are tested.
- [ ] Visual snapshots added where relevant.

### AI/document processing

- [ ] Structured schema validated.
- [ ] Evidence retained honestly.
- [ ] Raw and normalized values retained.
- [ ] Provider/configuration versions recorded.
- [ ] Resource/cost bounds exist.

### Security/privacy

- [ ] No secrets or raw customer content in logs/analytics.
- [ ] Cross-tenant tests added.
- [ ] File/provider/integration attack surface reviewed.
- [ ] Audit event recorded for consequential action.

### Operations

- [ ] Metric and trace added for long-running/external operation.
- [ ] Retry/replay/cancel behavior documented.
- [ ] Runbook updated when failure requires human action.
- [ ] Rollback or feature-flag path exists.

---

## 4. P0 release dependency gate

The first paid pilot cannot begin until all P0 tasks required by the selected pilot are complete and the following evidence exists:

1. Local bootstrap and deterministic vertical slice.
2. Tenant-isolation suite.
3. Premium implemented Review Studio with usability results.
4. Complete mock and real-provider processing paths.
5. Gold dataset and promotion report.
6. Catalog/matching and sales-order validation.
7. Approval, canonical payload, delivery, retry, and replay.
8. Audit, usage, operations, and support visibility.
9. Threat model, security scans, prompt-injection tests, and penetration-test treatment.
10. Backup restore, resilience, and migration rehearsals.
11. Appropriate paid provider/infrastructure terms for customer data.
12. Legal, privacy, incident, support, and subprocessor documents matching the system.

Tasks may be refined into smaller subtasks, but their acceptance criteria may not be weakened without an explicit decision record and approval.
