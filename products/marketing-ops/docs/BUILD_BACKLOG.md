# Executable Build Backlog

> **Purpose:** dependency-ordered implementation tasks for turning the Marketing Operations documentation into a local-first, production-grade B2B application.
>
> **Audience:** Codex, other coding agents, engineers, designers, QA, security, and release owners.
>
> **Execution rule:** complete tasks in dependency order. A task is not complete because its happy path renders; it is complete only when its data, authorization, failure states, accessibility, tests, telemetry, audit behavior, and documentation obligations are satisfied.

This backlog is the implementation index for the product defined in `PRODUCT.md`, `ARCHITECTURE.md`, `UI_UX_BLUEPRINT.md`, `SOCIAL_AUTOMATION.md`, `AI_STRATEGY.md`, `SECURITY_OPERATIONS.md`, and `DELIVERY_PLAN.md`.

---

## 1. How to execute a task

Every task implementation must include:

- **Scope control:** implement only the task and its direct dependencies.
- **Domain behavior:** encode invariants in domain services and database constraints where practical.
- **Authorization:** enforce organization, workspace, brand, campaign, and client visibility server-side.
- **Persistence:** use migrations and durable state; do not leave business state in browser memory or an ephemeral queue.
- **UX:** implement loading, empty, success, error, permission, conflict, offline/reconnecting, partial-success, and retry states that apply.
- **Accessibility:** keyboard behavior, focus management, labels, announcements, contrast, zoom, and reduced motion.
- **Security:** validate input, protect secrets, scope files and events, and consider abuse/rate limits.
- **Reliability:** idempotency, retries, recovery, cancellation, reconciliation, and safe replay for asynchronous work.
- **Telemetry:** structured logs, metrics, traces, and audit events for consequential or asynchronous actions.
- **Tests:** unit, integration, security, E2E, accessibility, visual, or provider contract tests listed by the task.
- **Documentation:** update canonical documents and accepted decisions when implementation changes a contract.
- **PR evidence:** screenshots or recordings for UI, migrations, test output, security notes, rollout/rollback notes, and known limitations.

### Priority

- **P0:** required for the first paid pilot and complete core journey.
- **P1:** required for common commercial adoption or immediately after the first pilot.
- **P2:** expansion or advanced enterprise capability.

### Size

- **S:** one focused, reviewable implementation session.
- **M:** several related modules with one coherent outcome.
- **L:** must be split into subtasks in the PR description if it cannot remain reviewable.

### Status convention

Keep task status outside this file unless a lightweight checklist is intentionally maintained. GitHub issues or project tooling may mirror these IDs, but this file remains the dependency and acceptance contract.

---

## 2. Foundation and repository

### FND-001 — Create product-local workspace and application skeleton

- **Priority:** P0
- **Size:** M
- **Depends on:** none
- **Outcome:** the isolated product folder contains a runnable TypeScript workspace with web, API, and worker applications.

Deliverables:

- `pnpm-workspace.yaml` scoped to `products/marketing-ops/`.
- Root product `package.json` with pinned package-manager version and commands.
- `apps/web`, `apps/api`, and `apps/worker` packages.
- `packages/ui`, `packages/contracts`, `packages/domain`, `packages/social-connectors`, `packages/ai`, `packages/config`, and `packages/test-fixtures`.
- Strict TypeScript configuration shared through a product-local package.
- Formatting, linting, typechecking, unit-test, build, and clean commands.
- Product-local `.gitignore`, `.editorconfig`, and environment example.
- Health endpoint placeholders for API and worker.

Acceptance criteria:

- `pnpm install`, `pnpm lint`, `pnpm typecheck`, `pnpm test`, and `pnpm build` succeed from the product folder.
- No application code imports SOA domain packages.
- A clean checkout produces identical lockfile-resolved dependencies.
- The web application renders a deliberate bootstrap screen rather than a framework default.

Tests/evidence:

- CI log for all commands.
- Dependency graph confirms intended package boundaries.
- README contains exact local commands.

### FND-002 — Add environment schema and configuration composition

- **Priority:** P0
- **Size:** S
- **Depends on:** FND-001
- **Outcome:** every runtime reads typed, validated configuration and fails safely when required values are absent.

Deliverables:

- Shared environment schema.
- Local, test, development, staging, and production modes.
- Adapter-selection configuration for database, object store, jobs, identity, social, AI, email, and telemetry.
- Secret fields excluded from logs and client bundles.
- Startup diagnostics that reveal capability availability without revealing credentials.

Acceptance criteria:

- Invalid configuration blocks startup with actionable messages.
- Browser bundles contain no server-only secret.
- Unit tests cover required, optional, malformed, and mode-specific values.

### FND-003 — Build local infrastructure with one command

- **Priority:** P0
- **Size:** M
- **Depends on:** FND-001, FND-002
- **Outcome:** developers can run the complete platform locally without paid external services.

Deliverables:

- Docker Compose for PostgreSQL, MinIO, mail catcher, optional Ollama, and optional local telemetry collector.
- Health checks and named persistent volumes.
- Bucket initialization and local credentials.
- `pnpm local:up`, `local:status`, `local:logs`, `local:reset`, and `local:down`.
- Architecture-aware defaults that match production contracts.

Acceptance criteria:

- A fresh machine can start infrastructure and reach every health endpoint.
- Reset is explicit and cannot accidentally target a non-local environment.
- Core test suite runs without Gemini or any real social credential.

Tests/evidence:

- Automated Compose smoke test.
- Documented port map and troubleshooting.

### FND-004 — Establish CI and change-scoped quality gates

- **Priority:** P0
- **Size:** M
- **Depends on:** FND-001
- **Outcome:** every PR receives deterministic build, test, security, and UX checks.

Deliverables:

- GitHub Actions workflow for install, format, lint, typecheck, unit tests, build, migration validation, integration tests, E2E smoke, accessibility smoke, and dependency scanning.
- Product-folder path filters without skipping required shared changes.
- Caching keyed by lockfile and toolchain.
- Test artifact retention for failures.
- Concurrency cancellation for superseded branch runs.

Acceptance criteria:

- Deliberate lint, type, test, migration, and E2E failures each fail the workflow.
- CI uses locked dependencies.
- No production secret is required.

### FND-005 — Create realistic seed-data and fixture framework

- **Priority:** P0
- **Size:** M
- **Depends on:** FND-003, DB-001
- **Outcome:** development, screenshots, UX review, E2E, and load tests share deterministic, credible marketing-operation data.

Deliverables:

- Agency and in-house organizations.
- Client workspaces, multiple brands, teams, and roles.
- Product launch, webinar, and evergreen campaigns.
- Mixed work, content, approval, publication, and metric states.
- Asset fixture metadata and safe generated fixture files.
- Seed reset and stable identifiers for tests.

Acceptance criteria:

- Seed runs repeatedly without duplicates.
- Screens avoid lorem ipsum and trivial one-row states.
- Fixture content contains no third-party copyrighted marketing assets or real credentials.

---

## 3. Database, jobs, events, and core platform services

### DB-001 — Add migration system and database conventions

- **Priority:** P0
- **Size:** M
- **Depends on:** FND-001, FND-003
- **Outcome:** schema changes are deterministic, reviewable, and runnable outside a specific hosting vendor.

Deliverables:

- PostgreSQL migration tool and product-local migrations directory.
- UUID/external-ID, timestamp, soft-delete/archive, optimistic-version, and organization-scope conventions.
- Migration lock and startup checks.
- Test database lifecycle.
- Forward-only migration policy documented.

Acceptance criteria:

- Empty database migrates to latest.
- Latest database can be recreated from migrations alone.
- CI detects drift or malformed migrations.
- Applied migrations are never edited by later tasks.

### DB-002 — Add organization-scoped repository primitives

- **Priority:** P0
- **Size:** M
- **Depends on:** DB-001
- **Outcome:** ordinary application queries cannot accidentally omit tenant scope.

Deliverables:

- Request/worker scope object.
- Repository base helpers requiring `organization_id`.
- Workspace/brand/campaign scope helpers.
- Transaction helper that carries actor and correlation context.
- Optional PostgreSQL row-level security design and tests where used.

Acceptance criteria:

- Unscoped tenant repository calls are impossible or fail closed.
- Cross-tenant read, write, count, search, and update tests fail safely.
- Background jobs re-authorize immutable scope before executing.

### JOB-001 — Implement PostgreSQL-backed durable job queue

- **Priority:** P0
- **Size:** L
- **Depends on:** DB-001, FND-002
- **Outcome:** scheduled and asynchronous work survives restarts and supports retries, leases, cancellation, and inspection.

Deliverables:

- Job table, payload version, queue name, state, schedule time, attempt count, lease owner/expiry, error code, and correlation ID.
- Enqueue, claim, heartbeat, complete, fail, reschedule, cancel, and dead-letter operations.
- Exponential backoff with jitter and bounded attempts.
- Concurrency controls by queue and organization/provider where required.
- Worker graceful shutdown and abandoned-lease recovery.

Acceptance criteria:

- Killing a worker mid-job does not lose work.
- Two workers do not execute the same valid lease concurrently.
- Scheduled jobs do not run early.
- Retryable and terminal failures are distinguishable.
- Dead-letter jobs can be inspected and safely replayed.

Tests/evidence:

- Lease race tests.
- Worker termination integration test.
- Clock-boundary and retry tests.

### JOB-002 — Implement transactional outbox and domain event dispatcher

- **Priority:** P0
- **Size:** M
- **Depends on:** DB-002, JOB-001
- **Outcome:** state changes and emitted events cannot diverge.

Deliverables:

- Outbox table and event envelope.
- Event type/version registry.
- Transaction helper writing domain state and outbox atomically.
- Dispatcher with deduplication and delivery attempts.
- Event consumers for activity, notifications, automations, analytics, and webhooks.

Acceptance criteria:

- Committed state always produces its event.
- Rolled-back state never produces an event.
- Consumer retries are idempotent.
- Unknown event versions quarantine rather than corrupt state.

### PLT-001 — Add audit and activity event foundations

- **Priority:** P0
- **Size:** M
- **Depends on:** DB-002, JOB-002
- **Outcome:** consequential user and system actions have an append-oriented, permission-safe history.

Deliverables:

- Audit event schema for actor, tenant, action, target, timestamp, safe before/after summary, source, correlation, and support grant.
- Activity event schema optimized for user-facing timelines.
- Redaction and field allowlist helpers.
- Query APIs with tenant and visibility filters.

Acceptance criteria:

- Secrets, tokens, raw authorization headers, and private internal comments never enter audit payloads.
- External clients cannot see internal activity.
- Campaign timeline can consume activity without reconstructing mutable tables.

### PLT-002 — Add real-time event transport

- **Priority:** P0
- **Size:** M
- **Depends on:** JOB-002, IAM-003
- **Outcome:** users see job, publication, approval, and collaboration updates without unsafe broad broadcasts.

Deliverables:

- Server-sent events or WebSocket transport selected through an ADR.
- Permission-scoped subscription topics.
- Reconnect cursor and missed-event recovery.
- Backpressure and connection limits.
- Browser client with stale/reconnecting states.

Acceptance criteria:

- A user cannot subscribe to another organization, client, or private campaign.
- Reconnect restores missed state without duplicate UI effects.
- Real-time transport failure does not prevent ordinary polling/reload recovery.

---

## 4. Identity, tenancy, roles, and application shell

### IAM-001 — Implement identity-provider boundary and developer auth

- **Priority:** P0
- **Size:** M
- **Depends on:** FND-002, DB-001
- **Outcome:** local development works without external identity while production remains OIDC-compatible.

Deliverables:

- Identity-provider interface.
- Local developer login with seeded users and explicit non-production guard.
- OIDC session contract, callback boundary, logout, and revocation hooks.
- Secure cookie/session defaults.
- User identity linkage table.

Acceptance criteria:

- Developer auth cannot start in production mode.
- Session expiry/revocation works.
- Authentication does not imply organization authorization.

### IAM-002 — Implement organization, workspace, brand, team, and membership model

- **Priority:** P0
- **Size:** L
- **Depends on:** IAM-001, DB-002
- **Outcome:** the B2B hierarchy and agency/client model are persisted and navigable.

Deliverables:

- Organization, workspace, brand, team, membership, invitation, and active-context tables.
- CRUD APIs with archive behavior.
- First-run organization setup.
- Organization/workspace/brand selectors.
- Explicit client-facing workspace/brand metadata.

Acceptance criteria:

- One user may belong to multiple organizations with separate roles.
- Archived context cannot receive new work or publications.
- Context switching updates every query and visible breadcrumb.
- URL navigation cannot leak previously selected tenant state.

### IAM-003 — Implement roles and permission evaluation

- **Priority:** P0
- **Size:** L
- **Depends on:** IAM-002
- **Outcome:** every operation has a centralized, testable authorization decision.

Deliverables:

- Built-in roles: organization admin, workspace admin, brand admin, campaign manager, contributor, reviewer, publisher, analyst, auditor, external client reviewer, platform support.
- Permission registry and scope levels.
- API guard and domain-service authorization helpers.
- UI capability response that improves presentation without replacing server checks.
- Denial reason codes safe for users.

Acceptance criteria:

- Permission matrix tests cover every API family.
- External reviewer receives only intentionally shared fields and assets.
- Publisher permission cannot bypass approval/readiness policy.
- UI hides or disables actions while APIs still independently enforce rules.

### IAM-004 — Implement invitations and external client access

- **Priority:** P0
- **Size:** M
- **Depends on:** IAM-003, NTF-001
- **Outcome:** internal and external collaborators can join safely with explicit scope.

Deliverables:

- Time-limited, single-use invitation tokens stored as hashes.
- Workspace/brand/campaign invitation scope.
- Invite acceptance, expiry, resend, revoke, and audit.
- External-client landing and minimal navigation.

Acceptance criteria:

- Revoked/expired/used tokens fail.
- Invitation does not grant broader organization visibility.
- Internal notes and operational cost fields remain excluded.

### IAM-005 — Build automated cross-tenant and cross-client security suite

- **Priority:** P0
- **Size:** L
- **Depends on:** IAM-003
- **Outcome:** tenant isolation is a continuously tested release invariant.

Coverage:

- Direct IDs and external IDs.
- List/count/search endpoints.
- Bulk actions.
- Signed URLs and asset previews.
- Comments, mentions, notifications, real-time events, audit, and exports.
- Jobs, retries, reconciliation, and webhooks.
- Client-visible versus internal records.

Acceptance criteria:

- Suite runs in CI.
- Every new resource family must add cases before merge.
- Failures expose no resource existence beyond safe error behavior.

### DS-001 — Implement design tokens and foundational UI package

- **Priority:** P0
- **Size:** M
- **Depends on:** FND-001
- **Outcome:** visual quality is governed by semantic tokens rather than page-specific styling.

Deliverables:

- Color, typography, spacing, radius, elevation, motion, density, focus, and z-index tokens from `UI_UX_BLUEPRINT.md`.
- Light and dark themes.
- Tabular number and monospace utility styles.
- Reduced-motion behavior.
- Storybook or equivalent isolated component environment.

Acceptance criteria:

- No product page hard-codes arbitrary status colors.
- Contrast meets WCAG 2.2 AA for required states.
- Theme switch does not flash incorrect colors or lose preferences.

### DS-002 — Build core interaction components

- **Priority:** P0
- **Size:** L
- **Depends on:** DS-001
- **Outcome:** high-frequency operations use consistent, accessible components.

Components:

- Button, icon button, link, input, textarea, select, combobox, date/time, checkbox, radio, switch.
- Modal, drawer, popover, tooltip, menu, command palette primitives.
- Data table, virtualized list/grid, board card, timeline row, calendar cell.
- Tabs, split panes, resizable panels, sticky action bar.
- Status, priority, risk, approval, connection, and publication badges.
- Avatar, presence, comment thread, activity event.
- Empty, loading, skeleton, error, permission, quota, offline, stale, and conflict states.

Acceptance criteria:

- Keyboard, focus trapping/restoration, screen-reader labels, and pointer behavior are tested.
- Component states match the blueprint.
- Visual regression covers all variants.

### WEB-001 — Build premium application shell and context navigation

- **Priority:** P0
- **Size:** M
- **Depends on:** IAM-002, DS-002
- **Outcome:** the application immediately communicates organization, workspace, brand, and current work context.

Deliverables:

- Desktop-first shell, collapsible navigation, top utility bar, breadcrumbs, context selector, environment indicator, notifications, help, and profile/session menu.
- Responsive tablet behavior.
- Permission-aware navigation.
- Route-level loading and error boundaries.

Acceptance criteria:

- Navigation never resembles a marketing landing page inside the app.
- Context remains visible on campaign, content, approval, calendar, and analytics screens.
- Keyboard users can reach primary navigation and return to content efficiently.

### WEB-002 — Add global search and command palette

- **Priority:** P1
- **Size:** M
- **Depends on:** WEB-001, CMP-004, WRK-001, CNT-001
- **Outcome:** expert users can navigate and act without traversing menus.

Deliverables:

- Permission-safe search across campaigns, work, content, assets, and publications.
- Command registry and contextual actions.
- Recent items and keyboard shortcuts.
- Search highlighting and result-type grouping.

Acceptance criteria:

- Search counts/results do not leak unauthorized records.
- Destructive or publishing actions require ordinary safeguards.
- Response remains usable with large seeded datasets.

---

## 5. Campaign and work management

### CMP-001 — Implement campaign, brief, objective, and audience domain

- **Priority:** P0
- **Size:** L
- **Depends on:** IAM-003, PLT-001
- **Outcome:** campaigns have structured strategy and ownership rather than being decorative folders.

Deliverables:

- Campaign, brief version, objective, audience, offer/product reference, channel plan, owner, status, priority, dates, budget metadata, and archive state.
- Status transition policy.
- Optimistic concurrency.
- Audit/activity events.

Acceptance criteria:

- Campaign belongs to one brand and retains workspace/organization lineage.
- Brief edits are versioned or historically attributable.
- Invalid date/status transitions fail with actionable codes.

### CMP-002 — Implement campaign templates and instantiation

- **Priority:** P0
- **Size:** M
- **Depends on:** CMP-001, WRK-001
- **Outcome:** teams can create repeatable campaigns with structured work and approval defaults.

Deliverables:

- Template/version model.
- Draft, publish, archive, clone, and instantiate lifecycle.
- Template variables and date offsets.
- Preview of generated milestones/work/content placeholders.

Acceptance criteria:

- Instantiation is transactional and idempotent.
- Published template versions remain reproducible.
- Editing a template does not mutate existing campaigns.

### CMP-003 — Build campaign API and portfolio queries

- **Priority:** P0
- **Size:** M
- **Depends on:** CMP-001
- **Outcome:** portfolio screens can filter, sort, paginate, aggregate, and bulk-update safely.

Deliverables:

- Cursor pagination.
- Filters for brand, owner, status, priority, date, risk, channel, objective, and client.
- Sorts and saved query format.
- Bulk archive/status/owner operations with per-item results.
- Readiness projection endpoint.

Acceptance criteria:

- Query plans and indexes support realistic dataset targets.
- Bulk operations are permission-safe and partially report failures.
- Readiness exposes missing prerequisites, not an arbitrary manually entered percentage.

### CMP-004 — Build campaign portfolio experience

- **Priority:** P0
- **Size:** L
- **Depends on:** CMP-003, DS-002
- **Outcome:** users can understand campaign health and move quickly into action.

Views:

- Table.
- Board by lifecycle/status.
- Timeline/roadmap.
- Saved views.
- Compact and comfortable density.

Acceptance criteria:

- View changes preserve filters and URL state.
- Bulk selection works by keyboard.
- Risk, approval, and launch readiness are explainable.
- Empty, loading, partial, error, and permission states are designed.

### CMP-005 — Build Campaign Room shell and overview

- **Priority:** P0
- **Size:** L
- **Depends on:** CMP-001, CMP-004, WEB-001
- **Outcome:** the flagship campaign workspace connects strategy, work, content, approvals, publishing, and performance.

Deliverables:

- Persistent campaign header with owner, dates, status, health, readiness, and actions.
- Tabs/sections for Overview, Plan, Work, Content, Calendar, Approvals, Publishing, Performance, Files, and Activity.
- Overview with objectives, milestones, blockers, approvals, upcoming posts, and recent activity.
- Contextual right rail/drawer patterns from the UX blueprint.

Acceptance criteria:

- Users never lose campaign/brand context.
- Each metric or warning links to the underlying filtered view.
- Campaign Room remains responsive with realistic data.

### WRK-001 — Implement work item, subtask, milestone, assignment, and workflow domain

- **Priority:** P0
- **Size:** L
- **Depends on:** CMP-001, IAM-003
- **Outcome:** marketing work can be planned and executed with accountable structure.

Deliverables:

- Work item type, status, priority, dates, effort, owner, assignees, team, parent, campaign, milestone, deliverable/content link, custom metadata, and version.
- Workflow/status definitions scoped by organization/workspace.
- CRUD, bulk update, and activity events.

Acceptance criteria:

- Parent/child and campaign lineage is validated.
- Completed/archived work respects edit policy.
- Bulk updates return per-record outcomes.
- Concurrent edits do not silently overwrite.

### WRK-002 — Implement dependencies, blocked state, and scheduling validation

- **Priority:** P0
- **Size:** M
- **Depends on:** WRK-001
- **Outcome:** launch readiness reflects real dependency state.

Deliverables:

- Finish-to-start dependency for P0; extensible dependency type model.
- Cycle detection.
- Blocked-state projection and reason list.
- Date violation warnings.
- Dependency graph query.

Acceptance criteria:

- Cycles are rejected transactionally.
- Completing/uncompleting dependencies updates downstream readiness.
- Large graphs remain within documented performance bounds.

### WRK-003 — Build work views and work-item drawer

- **Priority:** P0
- **Size:** L
- **Depends on:** WRK-001, WRK-002, DS-002
- **Outcome:** users manage the same work through list, board, timeline, and personal views without inconsistent behavior.

Deliverables:

- Campaign Plan table.
- Kanban board.
- Timeline.
- My Work and Team Work.
- Work-item drawer with details, dependencies, files, comments, history, and linked content.
- Saved filters/sorts/columns/density.

Acceptance criteria:

- Drag/drop uses optimistic updates with rollback and announcements.
- Keyboard alternatives exist for every drag action.
- Virtualization preserves focus and selection.
- Drawer URL is deep-linkable.

### WRK-004 — Implement comments, mentions, and internal/client visibility

- **Priority:** P0
- **Size:** M
- **Depends on:** IAM-003, NTF-001, PLT-001
- **Outcome:** collaboration is contextual and permission-safe.

Deliverables:

- Threaded comments, edits, resolve/reopen, mentions, attachments, and visibility classification.
- Internal-only and client-visible threads.
- Mention notification events.
- Safe rich-text format.

Acceptance criteria:

- External clients cannot infer or access internal threads.
- Editing retains history/audit according to policy.
- Mentioning an unauthorized user fails.
- Malicious HTML/script content is sanitized.

### WRK-005 — Implement custom fields and organization workflow configuration

- **Priority:** P1
- **Size:** L
- **Depends on:** WRK-001, IAM-003
- **Outcome:** commercial teams can model process differences without schema forks.

Deliverables:

- Typed custom-field definitions.
- Option/version management.
- Scope and applicability rules.
- Form/table/filter support.
- Migration behavior for changed definitions.

Acceptance criteria:

- Data remains queryable and permission-safe.
- Changing a definition cannot silently invalidate historical values.
- Filters and exports preserve types.

---

## 6. Assets and content operations

### AST-001 — Implement object-store adapter and signed upload sessions

- **Priority:** P0
- **Size:** L
- **Depends on:** FND-003, IAM-003
- **Outcome:** files upload directly and securely through a provider-neutral object-store contract.

Deliverables:

- Local MinIO and S3-compatible adapter.
- Upload session with allowed media type, size, organization, purpose, expiry, and expected checksum.
- Completion verification.
- Tenant-safe object keys and short-lived download/preview URLs.
- Orphan cleanup job.

Acceptance criteria:

- A user cannot complete another tenant’s upload.
- Declared and actual file properties are verified.
- Important files never rely on ephemeral app disks.
- Signed URLs expire and cannot cross scope.

### AST-002 — Add file security, metadata, and preview pipeline

- **Priority:** P0
- **Size:** L
- **Depends on:** AST-001, JOB-001
- **Outcome:** assets are validated and previewable without executing untrusted content.

Deliverables:

- Malware-scanner adapter and local safe mock/ClamAV option.
- Media-type inspection.
- Image/video/document metadata extraction.
- Bounded thumbnail/preview generation.
- Quarantine state and remediation UI data.
- Archive/decompression and pixel/duration limits.

Acceptance criteria:

- Suspicious files never become usable assets.
- Processing tools run with bounded resources.
- Preview failure does not destroy the original.
- Security tests cover spoofed extensions and oversized media.

### AST-003 — Implement asset and immutable version domain

- **Priority:** P0
- **Size:** M
- **Depends on:** AST-002
- **Outcome:** campaign assets retain versions, provenance, rights, and approval lineage.

Deliverables:

- Asset, asset version, file artifact, rights owner, license/usage notes, expiration, tags, campaign/content relationships, and archive.
- Version compare metadata.
- Current-version pointer without mutating history.

Acceptance criteria:

- Approved content points to an exact asset version.
- Expired/restricted assets block or warn according to brand policy.
- Replacing an asset creates a version rather than overwriting the file.

### AST-004 — Build asset library and asset detail experience

- **Priority:** P0
- **Size:** L
- **Depends on:** AST-003, DS-002
- **Outcome:** teams can find, inspect, version, and safely reuse assets.

Deliverables:

- Grid/list library, search, filters, upload, bulk tagging, rights/expiry indicators, campaign usage, and archive.
- Asset detail with preview, metadata, version rail, comments, usage links, and activity.
- Upload progress and recovery states.

Acceptance criteria:

- Large libraries remain responsive.
- Users understand exactly which version a content item uses.
- Expired/restricted assets are not presented as ordinary approved options.

### CNT-001 — Implement content item, version, channel variant, and variant version domain

- **Priority:** P0
- **Size:** L
- **Depends on:** CMP-001, AST-003, IAM-003
- **Outcome:** one master content idea can have deliberate, versioned platform-native variants.

Deliverables:

- Content item type, purpose, campaign, owner, status, master narrative, linked work, assets, tags, and version.
- Channel variant with provider/network, account intent, structured fields, locale, version, checks, and publish readiness.
- Immutable approval/publication references to exact versions.

Acceptance criteria:

- Editing creates or advances an attributable version according to autosave policy.
- Channel variants do not silently overwrite master content.
- Publication cannot target a mutable draft reference.

### CNT-002 — Implement autosave, optimistic concurrency, and version comparison

- **Priority:** P0
- **Size:** L
- **Depends on:** CNT-001, PLT-002
- **Outcome:** collaboration never silently loses content.

Deliverables:

- Debounced autosave with explicit saving/saved/error/stale indicators.
- Entity version/ETag checks.
- Conflict response containing safe comparison data.
- Restore/clone prior version.
- Presence indicator where useful without overpromising real-time co-editing.

Acceptance criteria:

- Two-user edit test produces conflict resolution, not last-write-wins loss.
- Network interruption retains recoverable local draft state.
- Restored versions create new history rather than deleting later versions.

### CNT-003 — Build Content Studio

- **Priority:** P0
- **Size:** L
- **Depends on:** CNT-001, CNT-002, AST-004, DS-002
- **Outcome:** users create and review master content and native channel variants in one premium workspace.

Deliverables:

- Campaign/content header.
- Master content editor.
- Variant navigator.
- Platform-specific structured editors.
- Asset selection/version indicator.
- Preview canvas.
- Checks and readiness rail.
- Version/history access.
- Comment and approval entry points.

Acceptance criteria:

- The interface remains coherent with many variants and long copy.
- Keyboard navigation reaches editor, variants, preview, and checks predictably.
- Users can distinguish draft, approved, scheduled, published, and stale-after-change versions.

### CNT-004 — Implement capability-aware platform checks and previews

- **Priority:** P0
- **Size:** M
- **Depends on:** CNT-001, SOC-001
- **Outcome:** previews and validation are honest about the selected account/provider’s current capabilities.

Deliverables:

- Character, media, aspect, count, field, and feature checks derived from capability snapshot.
- Warning versus blocking classifications.
- Platform preview framework with explicit approximation labels.
- Account capability refresh behavior.

Acceptance criteria:

- Preview never claims pixel-perfect provider rendering.
- Scheduled content keeps the capability snapshot used for validation.
- Capability changes surface revalidation needs.

### CNT-005 — Implement link, UTM, and content-quality checks

- **Priority:** P0
- **Size:** M
- **Depends on:** CNT-001
- **Outcome:** teams catch operational content defects before approval/publishing.

Deliverables:

- URL extraction and normalized link metadata.
- UTM policy and campaign defaults.
- Broken/unsafe link checker through bounded server job.
- Missing alt text, unresolved placeholder, forbidden-term, rights-expiry, duplicate-copy, and required-disclosure checks.
- Explainable override with permission and audit.

Acceptance criteria:

- External URL checks have SSRF protections.
- Overrides record actor, reason, and exact check/version.
- Blocking policy cannot be bypassed by client-side manipulation.

---

## 7. Approvals and client review

### APR-001 — Implement versioned approval policy and workflow

- **Priority:** P0
- **Size:** L
- **Depends on:** CNT-001, IAM-003
- **Outcome:** approvals support internal, client, sequential, and parallel review without mutable booleans.

Deliverables:

- Approval policy/version, steps, approver selectors, quorum, due/reminder settings, and applicability rules.
- Draft/publish/archive lifecycle.
- Policy resolution for campaign/content/variant.

Acceptance criteria:

- Published policy versions remain reproducible.
- A user cannot approve for a role/scope they do not hold.
- Policy resolution is explainable in the UI.

### APR-002 — Implement approval requests, immutable snapshots, decisions, and invalidation

- **Priority:** P0
- **Size:** L
- **Depends on:** APR-001, AST-003, CNT-002
- **Outcome:** every decision applies to an exact reviewable version and material changes invalidate the correct scope.

Deliverables:

- Approval request, snapshot manifest, step instance, decision, comment, expiry, cancel, reopen, and supersede behavior.
- Material-change fingerprint/policy.
- Approval completion events.

Acceptance criteria:

- Decision records cannot be edited into a different outcome.
- Approved copy/asset changes create clear stale/invalidation state.
- Non-material metadata changes follow explicit policy.
- Snapshot can be reconstructed without current mutable records.

### APR-003 — Build Approval Inbox and review canvas

- **Priority:** P0
- **Size:** L
- **Depends on:** APR-002, DS-002
- **Outcome:** reviewers can process work quickly with complete context and clear decisions.

Deliverables:

- Assigned, requested, overdue, completed, and changes-requested views.
- Review canvas with content/asset preview, version comparison, campaign context, checks, comments, and sticky decision bar.
- Approve, request changes, decline, and abstain where policy permits.
- Keyboard workflow and focus restoration.

Acceptance criteria:

- Review can be completed without a mouse.
- Approver sees exact snapshot and consequences.
- Decision submission is idempotent.
- Error/expired/superseded states explain next action.

### APR-004 — Build external client portal

- **Priority:** P0
- **Size:** L
- **Depends on:** IAM-004, APR-003
- **Outcome:** external clients can review intentionally shared campaign/content context without entering the internal operating system.

Deliverables:

- Branded but restrained portal shell.
- Client campaign summary.
- Approval queue and review canvas.
- Client-visible comments/files only.
- Session and invitation management.

Acceptance criteria:

- Automated tests confirm no internal comments, cost, workload, provider credentials, other clients, or unsupported navigation leaks.
- Portal works at common laptop/tablet widths.
- Client decision is fully audited.

### APR-005 — Add approval reminders, escalation, and SLA tracking

- **Priority:** P0
- **Size:** M
- **Depends on:** APR-002, NTF-001, JOB-001
- **Outcome:** late approvals become actionable work rather than silent schedule drift.

Deliverables:

- Reminder schedule, digest suppression, escalation policy, overdue projection, and notification history.
- Campaign readiness impact.
- Safe manual resend.

Acceptance criteria:

- Duplicate reminders are prevented.
- Time zones and business-day rules are explicit.
- Users can inspect why a reminder/escalation occurred.

---

## 8. Social account, scheduling, publishing, and metrics

### SOC-001 — Define social provider contracts and capability registry

- **Priority:** P0
- **Size:** L
- **Depends on:** FND-001
- **Outcome:** domain and UI code depend on stable internal contracts rather than network SDKs.

Deliverables:

- Provider, connection, account discovery, capability, media upload, publish, status, delete, metrics, engagement, webhook, and revocation contracts.
- Canonical error taxonomy.
- Capability snapshot schema with version/source/fetched time.
- Provider adapter contract test kit.

Acceptance criteria:

- Mock and future real adapters implement the same suite.
- Unsupported capability is represented explicitly, never guessed.
- Provider raw payloads are retained only where policy permits and never become the sole canonical state.

### SOC-002 — Implement encrypted social connection and account model

- **Priority:** P0
- **Size:** L
- **Depends on:** IAM-003, SOC-001, SEC-002
- **Outcome:** organizations can connect accounts with securely stored, revocable credentials.

Deliverables:

- Provider app, connection, account, token envelope, scopes, expiry, refresh status, health, reconnect, revoke, and capability snapshot records.
- Envelope encryption through key-provider interface.
- Token refresh job and distributed lock.
- Audit without token contents.

Acceptance criteria:

- Tokens never appear in browser responses, logs, audit, or ordinary database exports.
- Revocation disables new schedules/publishes with actionable status.
- Concurrent refresh cannot corrupt credentials.
- Key rotation path is tested.

### SOC-003 — Build deterministic mock social provider

- **Priority:** P0
- **Size:** L
- **Depends on:** SOC-001, JOB-001
- **Outcome:** the complete social journey and difficult failure modes work locally and in CI.

Scenarios:

- OAuth-like connect/reconnect/revoke.
- Multiple accounts and account types.
- Dynamic capabilities.
- Synchronous success.
- Provider validation error.
- Rate limit with reset.
- Timeout before acceptance.
- Timeout after remote acceptance.
- Async media processing.
- Webhook delivery/duplication/out-of-order event.
- Metric growth and delayed metrics.
- Remote deletion.

Acceptance criteria:

- Tests can select deterministic scenarios.
- Provider creates stable remote IDs/URLs.
- Timeout-after-acceptance can be reconciled without duplicate publication.

### SOC-004 — Build social account settings and connection-health UX

- **Priority:** P0
- **Size:** M
- **Depends on:** SOC-002, SOC-003, DS-002
- **Outcome:** admins understand connection scope, capabilities, health, and remediation.

Deliverables:

- Provider cards, connect flow, account selection, scopes, capability view, last refresh, health, reconnect, revoke, and provider incident banner.
- Permission and app-review limitation messaging.

Acceptance criteria:

- UI never suggests an account is publish-ready when scopes/capabilities are insufficient.
- Revoke is safeguarded and explains impact on scheduled publications.

### PUB-001 — Implement publication group, publication, attempt, and state machine

- **Priority:** P0
- **Size:** L
- **Depends on:** CNT-001, APR-002, SOC-002, JOB-001
- **Outcome:** every destination has durable, idempotent, reconcilable publication state.

Deliverables:

- Publication group for one content release across destinations.
- Publication referencing exact variant/asset/capability versions.
- Attempt, idempotency key, remote ID/URL, unknown state, retry eligibility, cancel/delete, and audit.
- State transition service and constraints.

Acceptance criteria:

- Partial success is represented per destination.
- Invalid transitions fail.
- Retrying cannot create a second remote post when reconciliation proves acceptance.
- Approval/readiness is revalidated before enqueue and publish.

### PUB-002 — Implement scheduler and eligibility calculation

- **Priority:** P0
- **Size:** L
- **Depends on:** PUB-001, JOB-001
- **Outcome:** scheduled publications survive restarts, time zones, edits, account health changes, and approval changes.

Deliverables:

- Schedule/unschedule/reschedule APIs with idempotency.
- Organization/brand time zone semantics and DST tests.
- Eligibility/readiness projection with blocking reasons.
- Lead-time and quiet-hour policies where configured.
- Due-job enqueue and cancellation behavior.

Acceptance criteria:

- Jobs never publish before scheduled instant.
- Changing an approved/scheduled version produces an explicit stale/reschedule flow.
- Account revocation or capability change blocks with remediation.
- Scheduling the same idempotency key twice has one effect.

### PUB-003 — Implement provider media preparation and upload workflow

- **Priority:** P0
- **Size:** L
- **Depends on:** PUB-001, AST-003, SOC-003
- **Outcome:** provider-specific media is prepared and uploaded through durable bounded stages.

Deliverables:

- Media rendition request and artifact lineage.
- Capability-based validation/transformation policy.
- Async upload states and polling.
- Provider media ID caching scoped to content/version/account when permitted.
- Cleanup/retention behavior.

Acceptance criteria:

- Original assets remain immutable.
- Transformations are bounded and traceable.
- A failed destination does not corrupt other destinations.
- Reuse does not cross tenant/account boundaries.

### PUB-004 — Implement publish, webhook, polling, and reconciliation workflow

- **Priority:** P0
- **Size:** L
- **Depends on:** PUB-002, PUB-003
- **Outcome:** the platform reaches a trustworthy terminal or actionable unknown state.

Deliverables:

- Validate, upload, publish, await async result, webhook ingest, poll, reconcile, and finalize jobs.
- Signed webhook verification, replay protection, deduplication, and out-of-order handling.
- Provider timeout taxonomy.
- Unknown-state reconciliation strategy.

Acceptance criteria:

- Timeout before acceptance follows safe retry policy.
- Timeout after acceptance searches/reconciles before retrying.
- Duplicate/out-of-order webhooks do not regress state.
- Every attempt has correlation and safe diagnostics.

### PUB-005 — Add retry, dead-letter, replay, kill-switch, and incident controls

- **Priority:** P0
- **Size:** M
- **Depends on:** PUB-004
- **Outcome:** operators can recover failures without unsafe database edits.

Deliverables:

- Retry policy by error class/provider.
- Manual retry/reconcile/cancel with permission and reason.
- Provider/account/organization publish kill switch.
- Dead-letter queue view data.
- Replay safeguards and audit.

Acceptance criteria:

- Manual actions remain idempotent.
- Kill switch blocks queued work and explains state.
- Operators cannot retry a confirmed-success post into duplication.

### PUB-006 — Build Marketing Calendar

- **Priority:** P0
- **Size:** L
- **Depends on:** CMP-005, WRK-003, PUB-002, DS-002
- **Outcome:** campaigns, milestones, work, approvals, and posts share one coherent time view.

Deliverables:

- Month/week/list modes.
- Layer toggles and saved filters.
- Time-zone display.
- Drag reschedule with dependency/readiness validation.
- Dense multi-brand indicators and accessible non-drag alternative.

Acceptance criteria:

- Users can distinguish task due date, approval due date, campaign milestone, and publish time.
- Dragging a post with blockers does not silently schedule.
- Large calendars remain usable and keyboard accessible.

### PUB-007 — Build Publishing Center and failure recovery UX

- **Priority:** P0
- **Size:** L
- **Depends on:** PUB-004, PUB-005, DS-002
- **Outcome:** users and operators can understand scheduled, publishing, partial, failed, unknown, and published work.

Deliverables:

- Queue/table with filters and saved views.
- Publication group detail.
- Attempt timeline and provider-safe error explanation.
- Partial-success recovery.
- Reconcile/retry/cancel controls.
- Remote post link and capability snapshot.

Acceptance criteria:

- Every failure offers a safe next action or states why none exists.
- Unknown state is visually distinct from failed.
- Internal diagnostics are permission-restricted.

### MET-001 — Implement social metric snapshots and normalized definitions

- **Priority:** P0
- **Size:** L
- **Depends on:** PUB-004, SOC-001
- **Outcome:** provider metrics are retained faithfully and compared honestly.

Deliverables:

- Raw metric snapshot, canonical metric definition, normalization version, source time, fetch time, freshness, and missing/unsupported state.
- Fetch schedule and backoff.
- Provider adapter metric contract tests.

Acceptance criteria:

- Raw values remain traceable.
- Unsupported versus zero versus unavailable is distinct.
- Cross-provider aggregates disclose definition differences.

### MET-002 — Build campaign and content performance dashboards

- **Priority:** P1
- **Size:** L
- **Depends on:** MET-001, CMP-005
- **Outcome:** teams can connect output and performance to campaign work.

Deliverables:

- Campaign performance, channel breakdown, content leaderboard, operational throughput, approval latency, publish reliability, and freshness indicators.
- Underlying-data drill-down and export.
- Saved date/brand/channel filters.

Acceptance criteria:

- Every aggregate can be traced to underlying records/definitions.
- Incomplete data is clearly labeled.
- Charts meet accessibility and performance requirements.

---

## 9. Automations, notifications, and AI

### NTF-001 — Implement notification and delivery foundation

- **Priority:** P0
- **Size:** M
- **Depends on:** JOB-002, IAM-002
- **Outcome:** in-app and email notifications are preference-aware, durable, and deduplicated.

Deliverables:

- Notification, recipient, channel delivery, preference, digest, read/dismiss state, and template version.
- In-app center.
- Local mail adapter and production email interface.
- Retry and safe failure handling.

Acceptance criteria:

- Unauthorized resource details never appear in notification payloads.
- Duplicate events do not spam users.
- Users can inspect delivery state for consequential reminders.

### AUTO-001 — Implement automation definition, version, trigger, condition, and action model

- **Priority:** P0
- **Size:** L
- **Depends on:** JOB-002, IAM-003
- **Outcome:** deterministic automations connect campaign operations without hidden magic.

Deliverables:

- Draft/publish/disable/archive lifecycle.
- Typed trigger registry.
- Inspectable condition AST.
- Action registry.
- Scope, owner, bounds, cooldown, and loop lineage.
- Dry-run/simulation result.

Acceptance criteria:

- Published versions remain reproducible.
- Conditions/actions validate before publication.
- Automation cannot grant itself permissions or bypass ordinary domain services.

### AUTO-002 — Implement initial triggers and actions

- **Priority:** P0
- **Size:** L
- **Depends on:** AUTO-001, WRK-001, APR-002, PUB-002, NTF-001
- **Outcome:** high-value campaign workflows can be automated safely.

Initial triggers:

- Campaign created/status changed.
- Work item changed, blocked, due, or overdue.
- Content/variant state changed.
- Approval completed, changed, expired, or overdue.
- Publication scheduled, failed, unknown, or published.
- Metric threshold reached.
- Scheduled time.

Initial actions:

- Create/update/assign work.
- Request approval.
- Send notification.
- Schedule or unschedule through ordinary validation.
- Call signed outbound webhook.
- Create AI generation request.

Acceptance criteria:

- Each action uses actor/service permission scope and emits audit/activity.
- Bounds prevent unbounded fan-out.
- Loop lineage stops recursive automations.
- Partial action failures are visible and retry policy is explicit.

### AUTO-003 — Build Automation Builder and run history

- **Priority:** P0
- **Size:** L
- **Depends on:** AUTO-002, DS-002
- **Outcome:** administrators can understand, simulate, publish, and troubleshoot automations.

Deliverables:

- Trigger/condition/action builder.
- Natural-language summary generated deterministically from configuration.
- Test with sample event.
- Impact/bounds preview.
- Run history with inputs, decisions, actions, failures, and correlation links.

Acceptance criteria:

- Keyboard workflow covers builder controls.
- Unsupported combinations are blocked before publish.
- UI never hides the deterministic rule behind AI-generated prose.

### AI-001 — Define AI provider, task, policy, and structured-output contracts

- **Priority:** P0
- **Size:** L
- **Depends on:** FND-001, SEC-001
- **Outcome:** campaign/content domain code remains independent of model vendors.

Deliverables:

- AI task registry.
- Provider/model capability interface.
- Policy and routing contract.
- Structured request/context/output/citation envelope.
- Usage, latency, cost, retention mode, and provenance.
- Canonical AI error taxonomy.

Acceptance criteria:

- Mock, Ollama, Gemini, future OpenAI, and future Anthropic adapters can implement the same contract.
- AI output never writes domain state without explicit application validation/acceptance.
- Model-generated instructions cannot invoke social publishing tools.

### AI-002 — Implement deterministic mock and Ollama-compatible local adapters

- **Priority:** P0
- **Size:** M
- **Depends on:** AI-001, FND-003
- **Outcome:** all AI-assisted flows work locally and in CI without external data transfer.

Deliverables:

- Deterministic fixture adapter for tests.
- Ollama-compatible HTTP adapter with health/model discovery.
- Timeouts, cancellation, bounded context/output, and structured-output repair limit.
- Local-only policy.

Acceptance criteria:

- CI uses deterministic adapter.
- Ollama absence produces a clear optional-capability state.
- No local mode unexpectedly falls back to a hosted provider.

### AI-003 — Implement Gemini hosted adapter

- **Priority:** P0
- **Size:** M
- **Depends on:** AI-001
- **Outcome:** approved non-local environments may use Gemini through the same task contracts.

Deliverables:

- Credential/config handling.
- Structured-output requests.
- Retry/rate-limit/error mapping.
- Usage accounting and provider request IDs.
- Data-policy/retention configuration surfaced to administrators.

Acceptance criteria:

- Adapter can be disabled by organization/brand policy.
- Secrets never reach browser/logs.
- Provider failures do not lose user input or candidates.

### AI-004 — Implement brand context, instruction versions, and context preview

- **Priority:** P0
- **Size:** L
- **Depends on:** AI-001, IAM-003, CNT-001
- **Outcome:** AI uses explicit, permission-safe brand and campaign context that users can inspect.

Deliverables:

- Brand voice, audience, terminology, claims, forbidden language, disclosures, examples, locales, and instruction versions.
- Context resolver with field-level sources.
- Context preview and inclusion controls.
- Prompt/instruction version provenance.

Acceptance criteria:

- Context never crosses organization/client boundaries.
- Users can see what will be sent before generation where required.
- Sensitive/internal fields follow task/provider policy.

### AI-005 — Implement durable generation requests and candidate acceptance

- **Priority:** P0
- **Size:** L
- **Depends on:** AI-002, AI-003, AI-004, JOB-001
- **Outcome:** AI generation is asynchronous, recoverable, comparable, and never silently replaces human work.

Initial tasks:

- Brief draft.
- Campaign task-plan candidate.
- Content draft.
- Channel adaptation.
- Rewrite/repurpose.
- Alt text.
- Campaign status summary.
- Performance summary with metric citations.

Deliverables:

- Generation request, run, candidate, source context snapshot, status, cancellation, retry, and usage.
- Candidate diff/partial acceptance service.
- Domain validation before acceptance.

Acceptance criteria:

- Candidate acceptance creates attributable domain edits/versions.
- Rejecting or ignoring a candidate has no side effect.
- Cancellation and provider timeout are recoverable.
- AI cannot approve or publish.

### AI-006 — Build AI assistance UX

- **Priority:** P0
- **Size:** L
- **Depends on:** AI-005, DS-002
- **Outcome:** AI feels like controlled assistance, not a chat overlay controlling the product.

Deliverables:

- Context preview.
- Task-specific generation controls.
- Candidate cards/diff.
- Partial accept, regenerate with instruction, reject, and history.
- Provider/policy/error/provenance details.
- Local-versus-hosted indicator.

Acceptance criteria:

- Users understand generated versus authored content.
- No universal chat bubble replaces structured workflows.
- Accessibility and keyboard paths cover candidate comparison/acceptance.

### AI-007 — Add AI evaluation, safety, and regression suite

- **Priority:** P0
- **Size:** L
- **Depends on:** AI-005
- **Outcome:** model, prompt, and context changes are measured before release.

Coverage:

- Brand-voice adherence.
- Required facts retained.
- Unsupported claim/hallucination checks.
- Channel constraints.
- Sensitive-data leakage.
- Prompt injection in campaign/content inputs.
- Citation correctness for metric summaries.
- Latency/cost.

Acceptance criteria:

- Golden fixtures run in CI for deterministic portions and scheduled evaluation for hosted models.
- Critical regressions block promotion.
- Results retain provider/model/instruction versions.

### AI-008 — Add OpenAI and Anthropic adapters

- **Priority:** P1
- **Size:** M each
- **Depends on:** AI-001, AI-007
- **Outcome:** organizations can select additional hosted providers without domain rewrites.

Acceptance criteria:

- Each adapter passes provider contract and evaluation gates.
- Provider-specific features remain behind capability checks.
- No automatic fallback violates organization data policy.

---

## 10. Real social providers and engagement

### INT-001 — Complete first real social-provider integration

- **Priority:** P0
- **Size:** L
- **Depends on:** SOC-004, PUB-005, MET-001
- **Outcome:** the pilot’s highest-priority provider can connect, publish, reconcile, and fetch metrics under approved production access.

Required work:

- Provider app registration and review evidence.
- OAuth/state/PKCE behavior as required.
- Account discovery.
- Capability mapping.
- Media upload and publishing.
- Async status/webhook/poll reconciliation.
- Metrics.
- Revocation, deletion, and data-use obligations.
- Sandbox/live contract tests.
- Provider-specific runbook.

Acceptance criteria:

- Authorized pilot account publishes a test post and platform record reconciles to remote state.
- Token revoke/reconnect works.
- Rate limits and known API limitations are visible.
- No unsupported preview or capability is promised.

### INT-002 — Complete second real social-provider integration

- **Priority:** P0 when required by pilot; otherwise P1
- **Size:** L
- **Depends on:** INT-001
- **Outcome:** one campaign can publish across the pilot’s minimum channel mix.

Additional acceptance criteria:

- Publication group partial-success tests cover both providers.
- Capability and metric differences remain visible.
- One provider outage does not prevent safe recovery of the other destination.

### INT-003 — Add remaining provider adapter stubs and capability research registry

- **Priority:** P1
- **Size:** M
- **Depends on:** SOC-001
- **Outcome:** LinkedIn, Meta/Facebook/Instagram, X, TikTok, YouTube, and Pinterest can be planned without pretending all are implemented.

Deliverables:

- Non-production adapter skeletons where useful.
- Official documentation/review checklist.
- Capability research record with verification date.
- Product feature flags and honest unavailable states.

Acceptance criteria:

- Planned providers cannot be selected for production publishing.
- Capability records expire/review rather than becoming permanent assumptions.

### ENG-001 — Implement supported social conversation and message sync

- **Priority:** P1
- **Size:** L
- **Depends on:** INT-001, IAM-003, JOB-001
- **Outcome:** supported comments/messages can enter a permission-safe engagement queue.

Deliverables:

- Conversation, message, participant handle metadata, sync cursor, assignment, SLA, labels, and linked campaign/publication.
- Webhook/poll sync with deduplication.
- Unsupported/private-data handling.

Acceptance criteria:

- Provider policy/scope limitations are explicit.
- Sync cannot expose unrelated accounts/brands.
- Deleted/hidden remote content is reconciled.

### ENG-002 — Implement reply drafts, approval, send, and unknown-state recovery

- **Priority:** P1
- **Size:** L
- **Depends on:** ENG-001, APR-001, AI-005
- **Outcome:** teams can draft and safely send supported replies with optional review.

Acceptance criteria:

- AI only drafts; ordinary approval and send services control action.
- Reply attempts are durable and idempotent where provider permits.
- Timeout/unknown remote state is visible and reconciled.

---

## 11. Analytics, public APIs, enterprise, and administration

### ANA-001 — Implement operational analytics projections

- **Priority:** P0
- **Size:** M
- **Depends on:** JOB-002, CMP-001, WRK-001, APR-002, PUB-001
- **Outcome:** teams can measure execution health without expensive ad hoc queries.

Metrics:

- Campaign throughput and on-time rate.
- Work cycle time and blockers.
- Approval latency and overdue rate.
- Content production and reuse.
- Publication success, failure, unknown-state, and retry rate.
- Provider/account health.

Acceptance criteria:

- Projection rebuild is deterministic.
- Values link to underlying records.
- Tenant/client visibility is enforced.

### ANA-002 — Build Operations Command Center

- **Priority:** P0
- **Size:** L
- **Depends on:** ANA-001, DS-002
- **Outcome:** the home screen prioritizes actionable operational risk rather than vanity cards.

Deliverables:

- Launches at risk.
- Work blockers.
- Approvals approaching SLA.
- Upcoming/failed/unknown publications.
- Account/provider incidents.
- Team workload.
- Recent activity.

Acceptance criteria:

- Every metric opens an actionable filtered view.
- Density remains usable across several brands.
- Stale/partial analytics states are disclosed.

### ANA-003 — Implement performance-to-action loop

- **Priority:** P1
- **Size:** M
- **Depends on:** MET-002, WRK-001, AUTO-002
- **Outcome:** users can convert performance findings into follow-up work or experiments.

Deliverables:

- Create task/experiment/content follow-up from a metric cohort.
- Preserve metric snapshot and rationale.
- Optional AI summary with cited metrics.

Acceptance criteria:

- Created work links back to exact evidence/time range.
- AI summary cannot invent unavailable metrics.

### API-001 — Implement scoped public API credentials and core endpoints

- **Priority:** P1
- **Size:** L
- **Depends on:** IAM-003, SEC-002
- **Outcome:** customers can integrate campaign/work/content status through least-privilege credentials.

Deliverables:

- Hashed API credentials, scopes, expiry, rotation, revoke, last used, and rate limits.
- Versioned REST/JSON endpoints with OpenAPI.
- Idempotency keys for create/update operations where required.
- Cursor pagination and correlation IDs.

Acceptance criteria:

- Credentials are shown once and never stored plaintext.
- Scope tests cover every endpoint.
- Abuse/rate-limit behavior is documented.

### API-002 — Implement outbound webhooks, import, and export

- **Priority:** P1
- **Size:** L
- **Depends on:** JOB-002, API-001
- **Outcome:** customers receive reliable events and can move configuration/business data without vendor lock-in.

Deliverables:

- Webhook endpoint/subscription, secret rotation, event selection, signed delivery, retries, replay, and delivery history.
- CSV/JSON import for selected entities with dry run/error rows.
- Export for campaigns, work, content, assets metadata, approvals, publications, metrics, audit, and configuration subject to permission.

Acceptance criteria:

- Webhook signatures include timestamp/replay protection.
- Import is transactional or reports row-level outcomes safely.
- Exports do not include tokens/secrets or unauthorized internal data.

### ENT-001 — Add SSO/SCIM-ready enterprise identity configuration

- **Priority:** P1
- **Size:** L
- **Depends on:** IAM-001, IAM-003
- **Outcome:** architecture supports enterprise identity without replacing application authorization.

Deliverables:

- OIDC/SAML provider configuration abstraction.
- Domain verification and enforced-login policy.
- Group-to-role mapping design.
- SCIM user/group provisioning contract and deprovision behavior.

Acceptance criteria:

- Deprovision revokes sessions and access.
- Identity groups cannot grant undefined product permissions.
- Break-glass/support policy is documented and audited.

### ENT-002 — Implement usage, entitlements, plan limits, and billing foundation

- **Priority:** P1
- **Size:** L
- **Depends on:** PLT-001, SOC-002, AI-005
- **Outcome:** commercial plans can control seats, brands, storage, social accounts, publications, AI use, and retention without hard-coded product forks.

Acceptance criteria:

- Limit checks are server-side and race-safe.
- Usage can be reconciled to records.
- Quota warnings precede blocking where practical.
- Billing provider remains replaceable.

### ENT-003 — Build audit explorer, retention controls, and controlled support console

- **Priority:** P1
- **Size:** L
- **Depends on:** PLT-001, SEC-003
- **Outcome:** customers and internal operators can investigate safely.

Deliverables:

- Audit filters/export.
- Retention policy UI with impact preview.
- Time-limited support grant requested/approved/revoked workflow.
- Support views for jobs, publications, provider health, and correlation timeline.

Acceptance criteria:

- Support access is off by default, scoped, expiring, and fully audited.
- Support tools never reveal raw tokens.
- Retention changes cannot silently violate existing contract minimums.

---

## 12. Security, privacy, reliability, and release

### SEC-001 — Create and maintain formal threat model

- **Priority:** P0
- **Size:** M
- **Depends on:** ARCHITECTURE documentation; update as features ship
- **Outcome:** security controls trace to realistic assets, actors, boundaries, and abuse cases.

Coverage:

- Tenant/client data leakage.
- OAuth/token theft and redirect abuse.
- Malicious files/media.
- Social webhook spoofing/replay.
- Publication duplication or unauthorized action.
- SSRF through link/media checks.
- Prompt injection and cross-brand context leakage.
- External client escalation.
- Support access abuse.
- API key/webhook abuse.
- Supply chain and CI secrets.

Acceptance criteria:

- Each P0 threat has prevention, detection, response, and test owner.
- Material architecture changes update the model in the same PR.

### SEC-002 — Implement secret/key provider, encryption envelopes, and rotation

- **Priority:** P0
- **Size:** L
- **Depends on:** FND-002, DB-001
- **Outcome:** social tokens, AI credentials, email secrets, and API secrets are protected and rotatable.

Deliverables:

- Secret-store/key-provider interface.
- Local development implementation.
- Envelope-encrypted database fields with key version.
- Rotation/re-encryption job.
- Redaction helpers and secret scanning.

Acceptance criteria:

- Rotation succeeds without invalidating usable records.
- Application logs/tests confirm secrets are absent.
- Production mode rejects insecure local key configuration.

### SEC-003 — Implement privacy inventory, retention, export, and deletion workflows

- **Priority:** P0
- **Size:** L
- **Depends on:** core domain tables, AST-003, SOC-002, AI-005
- **Outcome:** customer data can be inventoried, exported, retained, and deleted according to policy.

Deliverables:

- Data classification and processor/subprocessor mapping.
- Organization/workspace retention policy.
- Deletion request workflow covering database, objects, derivatives, search, caches, jobs, AI artifacts, and provider obligations.
- Legal hold or blocked-deletion state where applicable.
- Deletion verification report.

Acceptance criteria:

- Deletion is idempotent and auditable.
- Deleted data does not reappear through replay/projection rebuild.
- Backup behavior matches documented policy rather than promising immediate physical purge where impossible.

### OPS-001 — Implement structured observability

- **Priority:** P0
- **Size:** L
- **Depends on:** FND-002, JOB-001
- **Outcome:** one campaign/content/publication can be traced across web, API, jobs, providers, and webhooks.

Deliverables:

- Structured logs with correlation and safe context.
- OpenTelemetry-compatible traces.
- Metrics for API, jobs, queue age, publications, providers, approvals, AI, database, storage, and quotas.
- Dashboards and actionable alerts.

Acceptance criteria:

- Raw copy, client-sensitive comments, assets, tokens, and signed URLs are excluded from ordinary logs.
- Alerts link to runbooks and affected scope.
- Trace sampling preserves critical failures.

### OPS-002 — Implement backup, restore, object recovery, and portability rehearsal

- **Priority:** P0 before pilot
- **Size:** L
- **Depends on:** DB-001, AST-001, deployment environment
- **Outcome:** data can be restored and migrated rather than merely backed up.

Deliverables:

- Database backup/PITR configuration for production tier.
- Object versioning/lifecycle strategy.
- Restore procedure into isolated environment.
- Checksum reconciliation.
- Secret rotation during rehearsal.
- Free-to-paid/provider migration test.

Acceptance criteria:

- A documented restore completes successfully and application smoke tests pass.
- RPO/RTO evidence is recorded.
- No essential state exists only in an ephemeral queue or provider dashboard.

### OPS-003 — Write and exercise operational runbooks

- **Priority:** P0 before pilot
- **Size:** M
- **Depends on:** OPS-001, OPS-002, PUB-005
- **Outcome:** predictable incidents have owners and tested recovery steps.

Required runbooks:

- Provider outage.
- Revoked/expired credentials.
- Duplicate/unknown publication.
- Queue backlog or stuck job.
- Webhook replay/signature failure.
- Bad automation release.
- Bad AI/provider change.
- Cross-tenant/security incident.
- Credential exposure and rotation.
- Database restore/object recovery.
- Data export/deletion request.
- Free-tier quota exhaustion.

Acceptance criteria:

- Each runbook covers detection, containment, recovery, verification, communication, and follow-up.
- At least the publication, queue, credential, and restore runbooks are exercised before pilot.

### QA-001 — Build critical-path E2E suite

- **Priority:** P0
- **Size:** L
- **Depends on:** all P0 vertical-slice features
- **Outcome:** the complete local journey is continuously proven.

Required journey:

- Sign in.
- Create/switch organization, workspace, and brand.
- Create campaign from template.
- Complete work.
- Create content and two variants.
- Attach exact asset version.
- Request internal/client approval.
- Approve exact snapshot.
- Schedule two mock destinations.
- Simulate one success and one timeout-after-acceptance.
- Reconcile without duplication.
- Inspect calendar, Publishing Center, metrics, activity, and audit.

Acceptance criteria:

- Runs in CI with deterministic data.
- Captures trace/screenshots on failure.
- Includes permission-denied and recovery cases.

### QA-002 — Implement accessibility, keyboard, and visual-regression gates

- **Priority:** P0
- **Size:** L
- **Depends on:** DS-002 and each flagship screen
- **Outcome:** visual excellence and accessibility are release requirements.

Coverage:

- App shell.
- Operations Command Center.
- Campaign portfolio and Campaign Room.
- Work views/drawer.
- Content Studio.
- Approval Inbox/review canvas/client portal.
- Calendar.
- Publishing Center.
- Automation Builder.

Acceptance criteria:

- Automated accessibility smoke has no unresolved critical violations.
- Manual keyboard scripts pass.
- Visual regression covers primary states at target widths/themes.
- Focus and announcements work through async updates and dialogs.

### QA-003 — Implement performance and load tests

- **Priority:** P0 before pilot
- **Size:** L
- **Depends on:** P0 screens/APIs/jobs
- **Outcome:** real multi-brand workload does not degrade the product into an unusable dashboard.

Targets must be finalized from the UX blueprint and pilot volume. Cover:

- Campaign portfolio queries.
- Large work lists/boards/timelines.
- Content Studio with many variants/versions.
- Asset library.
- Calendar density.
- Publication burst and provider rate limits.
- Notification/automation fan-out.
- Real-time connections.

Acceptance criteria:

- Results and bottlenecks are recorded.
- Critical targets have budgets in CI or scheduled performance runs.
- Pagination/virtualization is used rather than hiding scale limits.

### REL-001 — Build staging and production deployment pipeline

- **Priority:** P0 before pilot
- **Size:** L
- **Depends on:** FND-004, OPS-001, OPS-002
- **Outcome:** releases are reproducible, reviewable, observable, and reversible.

Deliverables:

- Container builds and SBOM.
- Environment-specific deployment configuration.
- Migration job and compatibility strategy.
- Feature flags/kill switches.
- Staging smoke and production post-deploy checks.
- Rollback/run-forward procedure.

Acceptance criteria:

- No production deploy uses mutable unpinned artifacts.
- Migration failure stops deployment safely.
- Provider publishing can remain disabled during deployment validation.

### REL-002 — Complete security, legal, and procurement readiness

- **Priority:** P0 before paid pilot
- **Size:** L
- **Depends on:** SEC/OPS tasks
- **Outcome:** commercial claims match actual controls and provider terms.

Deliverables:

- Security whitepaper, data-flow diagram, subprocessor list, privacy policy, terms, DPA, SLA language, incident process, vulnerability disclosure, retention documentation, AI/provider governance, and standard questionnaire answers.
- Independent penetration test and remediation.

Acceptance criteria:

- No unresolved critical security finding.
- No uptime/privacy/data-use claim exceeds actual tier/provider capability.
- Provider applications and customer-data terms are approved for pilot use.

### REL-003 — Run controlled paid pilot and production acceptance

- **Priority:** P0
- **Size:** L
- **Depends on:** all P0 gates
- **Outcome:** one real customer completes the intended campaign-to-performance journey safely.

Rollout:

1. Configure organization/workspace/brand and users.
2. Import templates/assets where required.
3. Operate project management and approvals.
4. Schedule with manual confirmation.
5. Enable publishing for limited approved accounts.
6. Monitor every publication and provider event.
7. Enable bounded automations gradually.
8. Review daily failures, user friction, and metrics.

Acceptance criteria:

- Pilot success report covers adoption, campaign launch time, on-time work, approval latency, publish reliability, recovery, provider/AI cost, user satisfaction, and incidents.
- Critical defects are resolved or launch is explicitly blocked.
- Production-acceptance evidence is signed by product, engineering, security/operations, and customer owner.

---

## 13. P1/P2 expansion inventory

After the P0 journey is stable, prioritize from customer evidence rather than implementing every item automatically.

### P1 candidates

- Additional real social providers.
- Engagement inbox and reply workflow.
- Public API and webhooks.
- Slack/Microsoft Teams notifications and approvals.
- Google Drive/OneDrive/Dropbox asset import.
- Figma/Canva creative handoff subject to available APIs.
- Advanced custom fields/forms/intake requests.
- Recurring campaign programs and resource capacity planning.
- Budget and vendor tracking.
- SSO/SCIM.
- Billing/entitlements.
- Advanced analytics and experiments.
- White-label client portal settings within accessibility constraints.
- OpenAI and Anthropic AI adapters.
- Multilingual brand/content workflows.

### P2 candidates

- Social listening subject to provider terms.
- Paid-media planning and ad-platform integrations.
- Marketing calendar syndication.
- Content localization workflows with translation memory.
- Advanced rights-management integrations.
- Customer-hosted/VPC deployment.
- Regional data planes and customer-managed encryption keys.
- Marketplace/template ecosystem.
- Predictive capacity and campaign-risk models after sufficient quality data.

---

## 14. First Codex execution sequence

Codex should not start by generating every directory and feature at once. Use this sequence:

1. `FND-001` — workspace and app skeleton.
2. `FND-002` — typed configuration.
3. `FND-003` — local infrastructure.
4. `DB-001` — migrations.
5. `JOB-001` and `JOB-002` — durable jobs/outbox.
6. `IAM-001` through `IAM-003` — identity, hierarchy, authorization.
7. `DS-001`, `DS-002`, and `WEB-001` — visual system and shell.
8. `CMP-001`, `WRK-001`, and `CMP-005` — campaign graph foundation.
9. `AST-001` through `AST-003` — safe asset foundation.
10. `CNT-001` through `CNT-003` — content versions and Content Studio.
11. `APR-001` through `APR-004` — exact-version approvals/client portal.
12. `SOC-001` through `SOC-004` — provider boundary and mock connection.
13. `PUB-001` through `PUB-007` — scheduling, publishing, reconciliation, calendar, and operations.
14. `AUTO-001` through `AUTO-003` and `NTF-001`.
15. `AI-001` through `AI-007` — mock/Ollama/Gemini and controlled assistance.
16. `MET-001`, `ANA-001`, and `ANA-002`.
17. `QA-001` — prove the complete local journey.
18. `INT-001` and `INT-002` — real providers required by pilot.
19. Security, recovery, deployment, usability, and pilot gates.

At the end of every numbered step:

- Local startup works.
- Migrations apply from empty.
- Seed/reset works.
- Lint/typecheck/tests/build pass.
- Relevant cross-tenant tests pass.
- User-facing states meet the UX blueprint.
- Documentation reflects implemented versus planned behavior.

---

## 15. Final completion condition

The initial product is complete only when a real organization can securely plan a campaign, manage dependencies, create and version assets/content, obtain internal and external approval for exact versions, schedule and publish to the required real social accounts, recover partial and ambiguous provider failures, measure outcomes, convert findings into action, audit all consequential behavior, restore its data, and fulfill privacy obligations through a polished, accessible, high-performance interface.
