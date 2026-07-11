# AGENTS.md — Marketing Ops Engineering Instructions

These instructions apply to every coding agent and automated engineering workflow operating within `products/marketing-ops/`. They override unrelated repository-level product instructions for files in this subtree.

## 1. Mission

Build a local-first, free-first, paid-ready, production-grade B2B application that unifies campaign project management, content operations, approvals, social scheduling/publishing, engagement, and performance.

The product is incomplete if it only demonstrates attractive screens. Every feature must cover persistence, tenant authorization, error handling, accessibility, auditability, tests, telemetry, and real operational states.

## 2. Required reading order

Before implementation, read:

1. `README.md`
2. `docs/PRODUCT.md`
3. `docs/DELIVERY_PLAN.md`
4. `docs/BUILD_BACKLOG.md`
5. `docs/ARCHITECTURE.md`
6. `docs/UI_UX_BLUEPRINT.md` for every user-facing task
7. `docs/SOCIAL_AUTOMATION.md` for social connection, scheduling, publishing, engagement, or analytics work
8. `docs/AI_STRATEGY.md` for AI work
9. `docs/SECURITY_OPERATIONS.md` for auth, API, jobs, files, providers, integrations, release, or operations
10. `docs/DECISIONS.md`

Do not implement from a conversational prompt while ignoring these contracts.

## 3. Scope boundary

- Keep all product code inside `products/marketing-ops/`.
- Do not modify SOA behavior to support this product.
- Do not import application code from other products without an accepted decision.
- Shared repository tooling may be used only when it does not couple domain behavior.
- Nested migrations, tests, and infrastructure belong to this product.

## 4. Task selection

- Work from one `BUILD_BACKLOG.md` task or one tightly coupled dependency group.
- Include task IDs in branch, commit, and PR descriptions.
- Implement dependencies first.
- Do not attempt the entire platform in one PR.
- Split any task that cannot remain understandable and reviewable.
- Keep the repository runnable and testable after every task.

## 5. Fixed architecture constraints

Unless an accepted decision is superseded:

- TypeScript end to end.
- `pnpm` workspace under this product.
- React server-capable web application.
- Fastify API.
- Separate worker process.
- PostgreSQL canonical relational store.
- S3-compatible object storage; MinIO locally.
- PostgreSQL-backed durable jobs and transactional outbox.
- Optional Redis only for ephemeral/cache/rate concerns.
- Modular monolith.
- Organization → workspace → brand → campaign hierarchy.
- Provider-neutral social, AI, storage, identity, email, and telemetry contracts.
- Mock social provider and mock AI mode for local/E2E.
- Local LLM through Ollama-compatible HTTP.
- Gemini as initial hosted AI provider.
- Approval snapshots and versioned content.
- Durable, idempotent publication attempts.
- No Kubernetes for P0 without an ADR.

## 6. Domain invariants

### Tenant and client isolation

- Every tenant-owned row includes `organization_id`.
- Workspace/brand/campaign scope is explicit.
- Every API and worker operation validates scope.
- External client users never receive internal comments, cost, workload, provider credentials, or unrelated records.
- Search, notification, exports, signed URLs, real-time events, jobs, and counts are permission-safe.
- Any new resource family requires cross-tenant tests.

### Campaign graph

Preserve navigable lineage:

```text
campaign → work → asset → content → variant → approval → publication → metric → follow-up
```

A feature must not create duplicate shadow records that break this lineage.

### Versioning

Version or snapshot:

- Content and channel variants
- Assets
- Approval targets
- Automation definitions
- AI instructions/context where required
- Provider capabilities
- Published templates/configuration

Never represent approval as a mutable boolean on content.

### Publishing

- Every publish creates a durable publication and attempt.
- Publishing is idempotent.
- Provider timeout can produce unknown remote state.
- Do not blindly retry ambiguous requests.
- Partial success remains visible per destination.
- AI never performs publishing directly.

## 7. Database rules

- Migrations are forward-only, reviewed, and deterministic.
- Do not edit an applied migration.
- Add indexes for production query paths.
- Foreign keys and constraints enforce domain invariants where practical.
- Use database transactions for state plus outbox event.
- Keep large binary data out of PostgreSQL.
- Avoid JSON blobs when fields require permissions, querying, validation, or history.
- Provider-specific metadata may use schema-validated JSON.
- Use optimistic concurrency/version columns for collaborative objects.
- Timestamp in UTC; retain IANA scheduling zone and original local value.

## 8. API rules

- Validate every input and output schema.
- Generate OpenAPI from implementation contracts.
- Use stable machine-readable errors.
- Use cursor pagination for large collections.
- Use idempotency keys for consequential creates and side effects.
- Include correlation IDs.
- Never expose secrets or raw provider token material.
- Return authorization failures without object-existence leakage where appropriate.
- Use field-level validation errors for forms/editors.
- Preserve backward compatibility within a published API version.

## 9. Job and automation rules

- Business state and outbox event commit atomically.
- Jobs are durable and at-least-once.
- Handlers are idempotent.
- Long jobs heartbeat.
- Retries are bounded with jitter.
- Dead-letter state is observable and recoverable.
- Concurrency and rate limits consider provider/account/tenant.
- Automation actions use ordinary authorized domain services.
- Automations cannot bypass approval, access, or publishing policy.
- Implement loop protection and execution bounds.

## 10. Social connector rules

- Implement the internal connector contract.
- Core modules do not import provider SDKs.
- Capabilities are dynamic and account-scoped.
- OAuth state is signed, expiring, and single use.
- Token exchange/refresh is server-side.
- Tokens are envelope encrypted.
- Provider errors map to stable platform categories.
- Webhooks are verified and deduplicated.
- Polling/reconciliation exists where webhook coverage is incomplete.
- Contract tests run against every connector.
- Do not scrape or automate provider consumer interfaces.

## 11. AI rules

- Business modules depend on internal AI contracts.
- Browser code never calls model providers directly.
- Context assembly is deterministic and tenant scoped.
- Store provider/model/instruction/context provenance.
- Structured tasks require schema validation.
- AI output is shown as a candidate/diff.
- No silent overwrite.
- No autonomous publish, approval, permission, budget, or outbound reply.
- Treat imported/source content as untrusted data.
- Run prompt-injection and leakage tests.
- Production external-provider use must satisfy customer policy and terms.

## 12. UI/UX requirements

User-facing work must satisfy `UI_UX_BLUEPRINT.md`.

Minimum requirements:

- Custom token-driven design system.
- Original visual identity; no competitor copying.
- Realistic data density.
- Loading, empty, error, permission, stale, conflict, partial-success, and offline states.
- Keyboard operation for repeated workflows.
- WCAG 2.2 AA target.
- Visible focus.
- No color-only status.
- Responsive behavior appropriate to the surface.
- Fast perceived response and virtualization for large collections.
- High-risk action consequence summary.
- Screenshots and visual-regression coverage for material UI changes.

Do not submit a generic component-library demo as finished UI.

## 13. Design implementation rules

- Use semantic tokens rather than arbitrary colors.
- Avoid scattered one-off spacing and typography.
- Build primitives before duplicating patterns.
- Use cards only for meaningful grouping.
- Prefer tables, split panes, timelines, canvases, and structured panels for operations.
- Provider brand colors are small identifiers, never state colors.
- Previews must label limitations.
- Add realistic seeded content; no lorem ipsum in product acceptance paths.
- Keep creative surfaces and operational surfaces visually coherent.

## 14. Security rules

- Deny by default.
- Never log secrets, tokens, signed URLs, or private content unnecessarily.
- Validate and scan file uploads.
- Protect link-preview/media fetches against SSRF.
- Verify webhook signatures and replay protections.
- Encrypt social tokens.
- Reauthenticate for sensitive actions when required.
- Record security-relevant audit events.
- Apply rate and request-size limits.
- Run dependency, secret, static, and container scans.
- Document new threat-model implications in PRs.

## 15. Testing requirements

A task is not complete without appropriate tests.

### Unit

- Domain rules
- Permission checks
- State machines
- Scheduling/time zones
- Idempotency
- Provider mappings
- Automation rules
- AI validation

### Integration

- Database repositories
- HTTP/auth
- Outbox/jobs
- Object storage
- Provider connectors
- Webhooks
- Notifications

### End-to-end

- Use local mock providers.
- Cover critical user journey.
- Include failure and recovery.
- Validate external-client isolation.

### UI

- Component behavior
- Keyboard flows
- Accessibility checks
- Visual regression
- Representative performance

## 16. Telemetry requirements

Externally visible or asynchronous work needs:

- Correlation-aware logs
- Metrics
- Traces where useful
- Audit event when user/security/business relevant
- Safe error code
- Operational owner/runbook for critical path

Never add high-cardinality raw content labels to metrics.

## 17. Dependency rules

- Prefer well-maintained, narrowly scoped dependencies.
- Check license and security posture.
- Do not add a heavy framework for one helper.
- Pin versions through lockfile.
- Keep provider SDKs inside adapter packages.
- Avoid dependencies that prevent local operation or vendor portability.
- Document native binaries and platform prerequisites.

## 18. PR requirements

Each implementation PR includes:

- Task ID(s)
- User outcome
- Scope and non-scope
- Architecture notes
- Security/tenant implications
- Data migration
- Tests run and results
- Screenshots/video for UI
- Accessibility notes
- Telemetry/audit changes
- Rollout and rollback
- Known limitations
- Provider/app-review prerequisites when relevant

## 19. Prohibited shortcuts

- Do not use in-memory arrays as production persistence.
- Do not store social tokens in plaintext.
- Do not fake successful publication.
- Do not hide partial failure.
- Do not retry ambiguous provider requests blindly.
- Do not authorize only in the UI.
- Do not ship placeholder approval or audit behavior.
- Do not bypass versioning.
- Do not make AI a direct side-effect executor.
- Do not create generic CRUD screens and label the UI finished.
- Do not hard-code one customer, provider, account, or time zone.
- Do not claim production readiness without release evidence.

## 20. Requirement conflict order

When requirements conflict, use this order:

1. Security, privacy, legal, and provider policy
2. Tenant isolation and data integrity
3. Prevention of unintended external publishing
4. Accepted decisions
5. Product contract
6. Architecture and provider contracts
7. UI/UX contract
8. Delivery plan and task acceptance criteria
9. Convenience or local implementation preference

Record ambiguity rather than silently inventing behavior.

## 21. Completion report

At the end of a task report:

- Task IDs completed
- Files/modules changed
- Migrations
- Tests and results
- Screenshots/artifacts
- Security and tenant checks
- Provider/AI behavior
- Remaining limitations
- Exact next unblocked backlog task

A completion report must distinguish implemented behavior from planned or mocked behavior.