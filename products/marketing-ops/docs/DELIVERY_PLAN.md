# Master Implementation and Delivery Plan

> **Status:** implementation contract
>
> **Audience:** product, design, engineering, QA, security, operations, and coding agents
>
> **Objective:** deliver a local-first, free-first, paid-ready, production-grade B2B platform that connects campaign project management with content production, approval, social publishing, engagement, and performance.

This plan is intentionally explicit. An engineer or coding agent should be able to select one task from `BUILD_BACKLOG.md`, implement it, prove it, and hand it off without inventing missing product behavior.

## 1. Controlling documents

Read together:

- `PRODUCT.md` — product outcomes and scope
- `ARCHITECTURE.md` — system and data model
- `UI_UX_BLUEPRINT.md` — product experience and design-quality bar
- `SOCIAL_AUTOMATION.md` — social provider and publication behavior
- `AI_STRATEGY.md` — local/Gemini/future AI behavior
- `SECURITY_OPERATIONS.md` — release, security, privacy, and operations gates
- `BUILD_BACKLOG.md` — executable task inventory
- `DECISIONS.md` — accepted and deferred choices
- `../AGENTS.md` — agent execution rules

## 2. Product mandate

The system must make this journey coherent:

```text
request or idea
→ campaign brief
→ project plan and dependencies
→ content and asset production
→ internal/client approval
→ platform-native channel variants
→ schedule
→ reliable publication
→ engagement and metrics
→ follow-up action
```

The first paid-pilot release must support the journey end to end for at least one customer, brand, campaign template, and required set of social providers.

## 3. What production-ready means

Production-ready does not mean “the demo works.” It means:

- Tenant and client isolation is enforced and tested.
- Campaign, work, content, approval, publication, and metric records persist correctly.
- Scheduled work survives restarts.
- External publishing is idempotent and reconcilable.
- Provider tokens are encrypted and revocable.
- A user can recover from partial failure.
- Backups restore successfully.
- Critical workflows are usable, accessible, and visually finished.
- Provider app review/permissions support real customer operation.
- Privacy, support, incidents, and deletion have owners and runbooks.
- Release evidence exists.

## 4. Delivery strategy

### 4.1 Vertical slices first

Build one complete local journey before creating broad disconnected administration screens.

First local slice:

```text
sign in
→ create organization/workspace/brand
→ create campaign from template
→ manage generated work
→ create content item and two variants
→ attach asset
→ request external approval
→ approve exact version
→ schedule to two mock accounts
→ simulate one success and one timeout-after-acceptance
→ reconcile and recover
→ show metrics and audit timeline
```

### 4.2 Mock providers before real providers

The local mock social provider and deterministic AI provider must model real failure, timing, capabilities, webhooks, and metrics. This enables reliable development without app-review or external quotas.

Real provider work begins only after the internal contracts and user journey are stable.

### 4.3 Design alongside domain work

UI/UX is not a late polish phase.

For each major phase:

1. Confirm workflow and data model.
2. Design screen and state matrix.
3. Build accessible components.
4. Implement server behavior.
5. Add realistic seed data.
6. Run visual, usability, and performance checks.

### 4.4 Keep paid migration simple

Local/free implementations must use the same contracts as production:

- PostgreSQL
- S3-compatible storage
- durable jobs
- OIDC-compatible auth boundary
- provider adapters
- OpenTelemetry-compatible telemetry

## 5. Team and parallel workstreams

Fast credible delivery benefits from:

- Product/domain lead
- Senior product designer
- Two full-stack engineers
- Platform/integration engineer
- QA/security support

Coding agents can accelerate bounded tasks, test creation, migrations, component work, and documentation. Human review remains required for architecture, security, provider policy, product behavior, and usability acceptance.

Parallel workstreams after foundation:

- Domain/API/database
- Design system and web shell
- Job/storage/media foundation
- Campaign/work experience
- Content/approval experience
- Provider connector research and app registration
- Security/testing/operations

## 6. Phase 0 — product, design, and architecture lock

### Outcomes

- Canonical documents approved
- Working product name remains replaceable
- P0 pilot persona and social-provider needs confirmed
- Information architecture approved
- High-fidelity designs started for flagship surfaces
- Provider app-registration process started early
- Golden/local demo data defined

### Deliverables

- PRD
- Architecture
- UI/UX blueprint
- Social and AI contracts
- Threat model
- Data model diagram
- Critical journey prototype
- Backlog and dependency map
- Pilot provider access checklist

### Exit criteria

- No unresolved contradiction in hierarchy, approval, publication, or permissions.
- P0 scope is small enough to execute but complete enough to deliver value.
- First implementation tasks are unblocked.

## 7. Phase 1 — repository and local platform foundation

### Build

- Product-local `pnpm` workspace
- Web, API, worker applications
- Shared packages
- Strict TypeScript
- Lint, format, typecheck, unit-test commands
- Docker Compose
- PostgreSQL
- MinIO
- Mail catcher
- Mock provider service or module
- Optional local telemetry
- Environment validation
- CI foundation

### Required commands

```bash
pnpm install
pnpm local:up
pnpm db:migrate
pnpm db:seed
pnpm dev
pnpm lint
pnpm typecheck
pnpm test
pnpm test:e2e
pnpm local:down
```

### Build health

- Each app has health/readiness endpoints.
- Worker can claim and complete a test job.
- Web can call API.
- Seed data loads deterministically.
- Clean checkout setup is documented and verified.

### Exit criteria

- One command starts required local infrastructure.
- CI passes on a clean branch.
- No secret is required for local core tests.

## 8. Phase 2 — tenancy, identity, authorization, and app shell

### Domain

- Organization
- Workspace
- Brand
- User
- Membership
- Team
- Role and permission grant
- Invitation
- Session/auth adapter
- Audit event

### Web

- Sign in/developer auth
- First-run setup
- Organization/workspace/brand switchers
- App shell
- Primary navigation
- Global search shell
- Command palette shell
- Notification shell
- Permission-denied and missing-context states

### Security

- Server-side scope object
- Tenant-safe repository pattern
- Cross-tenant tests
- External-client role foundation
- Session revocation

### Exit criteria

- Two seeded organizations cannot access each other by any tested route.
- Agency client/workspace context is unmistakable in the UI.
- App shell meets visual and accessibility baseline.

## 9. Phase 3 — campaign and work-management core

### Domain

- Campaign
- Campaign template
- Brief
- Objective
- Audience/offer references
- Milestone
- Work item/subtask
- Dependency
- Assignment
- Workflow/status
- Custom fields
- Saved views
- Comments/activity

### API

- Campaign CRUD and templates
- Work CRUD
- Dependency validation
- Bulk edit
- Filtering/sorting/pagination
- Readiness projection foundation

### Web

- Campaign portfolio table/board/timeline
- Campaign Room shell
- Plan tab
- Work item drawer
- My Work and Team Work
- Keyboard workflows
- Loading/error/conflict states

### Rules

- Prevent dependency cycles.
- Calculate blocked state.
- Validate date dependencies.
- Preserve activity history.
- Apply optimistic concurrency.

### Exit criteria

- User creates campaign from template and completes/blocks work.
- Realistic portfolio and campaign data remain performant.
- Readiness shows explainable requirements rather than a manual percentage.

## 10. Phase 4 — asset and content operations

### Asset foundation

- Signed upload sessions
- Immutable originals
- File validation and malware adapter
- Metadata extraction
- Thumbnail/preview jobs
- Asset versions
- Rights and expiration
- Campaign/content relationships

### Content foundation

- Content item
- Content version
- Channel variant and version
- Structured fields
- Templates
- Brand context
- Content checks
- Link/UTM metadata

### Web

- Asset library
- Asset detail/version rail
- Content hub
- Content Studio
- Variant navigator
- Platform preview framework
- Checks rail
- Version comparison

### Exit criteria

- User creates content and variants, attaches an approved asset version, and understands all unresolved checks.
- Autosave and conflict handling do not lose work.
- Asset permission and signed URL tests pass.

## 11. Phase 5 — approvals and external client portal

### Domain

- Approval policy/version
- Approval request
- Step
- Decision
- Immutable approval snapshot
- Material-change invalidation
- Reminder jobs

### Web

- Approval Inbox
- Review canvas
- Version comparison
- Decision bar
- Client portal shell
- Shared campaign summary
- External comments

### Security

- Client visibility matrix
- Internal-note exclusion
- Cross-client tests
- Approval-decision authorization

### Exit criteria

- Sequential internal + client approval works.
- Changing approved content invalidates the correct decisions.
- External user sees only intentionally shared context.

## 12. Phase 6 — durable publishing with mock provider

### Domain

- Social provider app
- Connection and account
- Capability snapshot
- Publication group
- Publication
- Attempt
- Media upload
- Provider webhook event
- Connection health

### Worker

- Schedule eligibility
- Validate
- Upload media
- Publish
- Poll status
- Reconcile timeout
- Retry
- Dead-letter
- Metrics snapshots

### Mock provider

- OAuth-like local connection
- Multiple account types
- Dynamic capabilities
- Success
- Validation failure
- Rate limit
- token revoked
- timeout before acceptance
- timeout after acceptance
- async processing
- webhook delivery
- metric growth

### Web

- Social account settings
- Account health
- Calendar scheduling
- Publishing Center
- Failure drawer
- Partial-success group
- Published URL

### Exit criteria

- Scheduled posts survive API/worker restart.
- Timeout-after-acceptance does not duplicate.
- Partial success can be recovered per destination.
- Full audit timeline exists.

## 13. Phase 7 — automations and notifications

### Automation engine

- Trigger registry
- Condition AST/schema
- Action registry
- Versioning
- Dry run
- Run history
- Loop prevention
- Bounds
- Permission evaluation

### Initial triggers

- Campaign created/status changed
- Work item changed/due
- Content/variant status changed
- Approval completed/expired
- Publication scheduled/failed/published
- Metric threshold reached
- Scheduled time

### Initial actions

- Create/update work
- Assign user/team
- Request approval
- Send notification
- Schedule/unschedule publication after ordinary validation
- Call signed webhook
- Create AI generation request

### Notifications

- In-app
- Email
- Preferences
- Digests
- Retry and delivery status

### Exit criteria

- Users can build, test, publish, disable, and inspect automations.
- Automations cannot bypass permissions or approval.
- No loop can create unbounded jobs.

## 14. Phase 8 — local AI and Gemini

### Foundation

- AI provider registry
- Mock AI adapter
- Ollama adapter
- Gemini adapter
- Policy and routing
- Instruction versions
- Generation jobs
- Candidates
- Usage/cost
- Evaluation fixtures

### Initial product capabilities

- Brief draft
- Task-plan candidate
- Content draft
- Channel adaptation
- Rewrite/repurpose
- Alt text
- Campaign status summary
- Performance summary with citations

### Web

- Context preview
- Candidate/diff acceptance
- Partial accept
- Generation history/provenance
- Policy/provider errors

### Exit criteria

- Full AI-assisted journey works locally without external calls.
- Gemini can be enabled by configuration.
- Provider/model can change without changing campaign/content domain code.
- AI cannot directly publish or approve.

## 15. Phase 9 — first real social provider

Start provider registration/review much earlier; implementation happens after internal contract stability.

### Work

- Production app configuration
- OAuth flow
- Account discovery
- Capabilities
- Media upload
- Publishing
- Status/webhook reconciliation
- Metrics
- Revocation/deletion obligations
- Contract tests
- Sandbox/live test plan

### UX

- Provider-specific connection requirements
- Account selection
- Permission/scope remediation
- Honest previews and limitations

### Exit criteria

- Provider application has required production approval.
- Authorized pilot account can connect and publish.
- Published record reconciles with remote post.
- Token revocation and reconnect work.
- Rate-limit/failure behavior is measured.

## 16. Phase 10 — second provider and provider hardening

Implement provider required by pilot’s real channel mix.

Add:

- Capability differences
- Multi-provider partial-success tests
- Publication group UX
- Provider-specific metrics caveats
- Kill switch by provider/account
- Provider incident runbook

Exit when one campaign can reliably publish to the pilot’s minimum channel set.

## 17. Phase 11 — analytics and performance loop

### Data

- Metric definitions
- Raw snapshots
- Normalization versions
- Campaign aggregates
- Operational aggregates
- Data freshness

### Web

- Campaign performance
- Content leaderboard
- Channel breakdown
- Operations analytics
- Underlying-data drill-down
- Export
- AI summary with metric citations
- Create follow-up action/experiment

### Exit criteria

- Users can explain both outcomes and operational bottlenecks.
- Metrics never imply false cross-provider equivalence.
- Data freshness and missing data are visible.

## 18. Phase 12 — engagement inbox

Scope based on real provider access.

### Build

- Conversation/message model
- Sync cursors
- Queue and assignment
- Reply drafts
- Approval policy
- Provider reply adapter
- Unknown reply state
- SLA and follow-up work

### Exit criteria

- Supported interactions sync and are permission-safe.
- Replies are durable, approved when required, and reconciled.
- Unsupported provider behavior is clearly labeled.

## 19. Phase 13 — enterprise and integration basics

- Public API keys and scope
- Outbound webhooks
- Import/export
- SSO-ready configuration
- Audit explorer/export
- Retention UI
- Usage and entitlement model
- Billing foundation
- Slack/Teams or customer-priority integration
- Support console with controlled grants

## 20. Phase 14 — production hardening

### Security

- Formal threat-model review
- Cross-tenant suite
- Penetration test
- Dependency/secret/container scans
- Token-key rotation rehearsal
- OAuth/webhook review
- Prompt-injection/leakage suite

### Reliability

- Load and burst tests
- Worker termination tests
- Queue backlog recovery
- Provider outage simulation
- Backup and restore
- Object recovery
- Failure injection
- Runbook exercises

### UX

- High-fidelity implementation review
- Accessibility audit
- Keyboard audit
- Performance profiling
- Visual regression
- Five-role usability study
- Error copy and recovery review

### Legal/operations

- Terms/privacy/DPA
- Subprocessor list
- Support severity model
- Incident process
- Status communication
- Usage/cost reconciliation
- Provider policy acceptance

## 21. Phase 15 — pilot and launch

### Pilot setup

- Configure one real organization/workspace/brand
- Import users, templates, campaigns, assets where needed
- Connect approved social accounts
- Configure brand and approval policy
- Train internal and client users
- Run historical/synthetic rehearsal

### Safe rollout

1. Project management and approvals first.
2. Schedule through platform with manual confirmation.
3. Enable publish for limited accounts/campaigns.
4. Monitor every publication.
5. Expand automation gradually.
6. Review daily failures and user friction.

### Pilot success report

- Adoption
- Time to launch
- Approval latency
- On-time completion
- Publication success
- User satisfaction
- Provider and AI costs
- Security/reliability incidents
- Prioritized remediation

## 22. Seed-data contract

Seed data must look like a real B2B marketing operation, not lorem ipsum.

Include:

- Agency organization
- Internal SaaS marketing organization
- Client workspaces
- Distinct brands and brand voices
- Product launch campaign
- Webinar campaign
- Evergreen thought-leadership campaign
- Mixed statuses and realistic due dates
- Assets and versions
- Internal/client comments
- Approved and changes-requested content
- Multiple network variants
- Publication successes/failures
- Metric history
- Automation examples

Seed data supports screenshots, UX review, E2E, and performance tests.

## 23. CI/CD pipeline

Required checks:

- Install with locked dependencies
- Format check
- Lint
- Typecheck
- Unit tests
- Migration validation
- Integration tests
- Build web/API/worker
- Security/dependency scan
- E2E critical path
- Accessibility smoke tests
- Visual regression for changed stories/pages

Protected production deployment requires:

- Approved migration plan
- Release notes
- Rollback plan
- Security and provider checks
- Feature flags
- Post-deploy smoke test

## 24. Environments

### Local

- Developer data only
- Mock provider
- Mock/Ollama AI
- Developer auth

### Shared development

- Synthetic/non-confidential data
- Test provider apps
- Free/low-cost services where suitable

### Staging

- Production-like topology
- Test provider accounts
- Sanitized data
- Release candidate validation

### Production

- Paid services appropriate to contracts
- Backups/PITR
- Managed secrets/keys
- Monitoring/on-call
- Approved provider apps and AI terms
- No developer-auth mode

## 25. Free-tier upgrade path

- Keep standard PostgreSQL and repository migrations.
- Keep S3-compatible object APIs.
- Keep provider contracts.
- Keep environment-driven composition.
- Export all configuration and tenant data.
- Rehearse database and object migration before pilot.
- Treat free quotas as configurable limits, not domain rules.

## 26. Global definition of done

A task is done only when:

- User outcome works.
- Domain and authorization rules are enforced.
- Migrations and rollback/compatibility are addressed.
- Happy, error, permission, conflict, and partial states exist.
- Tests pass.
- Telemetry and audit are present where needed.
- UI meets design/accessibility requirements.
- Documentation changes with behavior.
- Known limitations are explicit.

## 27. Implementation execution order

1. Product workspace and toolchain
2. Local infrastructure
3. Database/migration foundation
4. Jobs/outbox
5. Identity and tenancy
6. Design tokens/app shell
7. Campaign/work domain and UI
8. Assets/content domain and UI
9. Approval domain and client portal
10. Mock social connection and capability model
11. Schedule/publication/reconciliation
12. Calendar and Publishing Center
13. Automations/notifications
14. Mock/Ollama/Gemini AI
15. First real provider
16. Second real provider
17. Metrics and analytics
18. Engagement
19. APIs/integrations/enterprise basics
20. Hardening and pilot

At the end of every step, local startup, migrations, seed, build, and relevant tests must still pass.

## 28. Documentation maintenance

- Repository documents are the implementation source of truth.
- Notion may hold planning, research, and discussion.
- Update the relevant canonical document in the same PR as behavior.
- Do not create duplicate overlapping documents.
- Record material decisions.
- Keep backlog dependencies current.
- Mark planned versus implemented behavior honestly.

## 29. Final release statement

The platform may be called complete for its initial commercial scope only when an organization can plan and execute a campaign, collaborate securely, create and approve platform-native content, schedule and publish reliably through required real social providers, recover failure, measure results, audit actions, restore data, and fulfill privacy obligations through a visually excellent and usable product.