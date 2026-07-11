# Architecture — Marketing Operations Platform

## 1. Architecture goals

- Build a secure multi-tenant B2B SaaS product.
- Run the full primary journey locally without third-party social or AI access.
- Preserve easy migration from free-tier development services to paid production services.
- Keep social-network, AI, identity, storage, email, and telemetry providers replaceable.
- Support collaborative project-management workloads and durable scheduled publishing.
- Make every external side effect idempotent, observable, retryable, and auditable.
- Keep the codebase understandable to a small team and coding agents.
- Deliver premium UI performance despite data-dense views.
- Begin as a modular monolith with separately runnable web, API, and worker processes.

## 2. Fixed P0 stack

### Language and workspace

- TypeScript end to end
- `pnpm` workspaces
- Turborepo or equivalent task orchestration
- Strict TypeScript configuration
- ESLint and formatter configured at product root

### Web

- React application using a server-capable React framework
- Route-level data loading where useful
- TanStack Query for server state
- TanStack Table and virtualization for large grids
- React Hook Form plus schema validation
- Accessible headless primitives under a custom design system
- CSS variables and design tokens; utility CSS may assist implementation but cannot replace the design system
- Playwright for end-to-end and visual tests

### API

- Node.js TypeScript service
- Fastify as HTTP runtime
- Zod-compatible request/response validation
- OpenAPI generated from the same contracts used by implementation
- REST/JSON for P0
- Server-sent events or WebSocket gateway for narrowly scoped real-time updates

### Worker and jobs

- Node.js TypeScript worker
- PostgreSQL-backed durable job queue for P0
- Transactional outbox for events and external side effects
- Advisory locks or queue-native exclusive claims where required
- Separate queues and concurrency controls for notifications, media, social publishing, analytics sync, AI generation, and automations

### Data and files

- PostgreSQL as canonical relational store
- SQL migrations in repository
- S3-compatible object storage
- MinIO locally
- Redis optional for ephemeral caching and rate coordination, never canonical workflow state

### Local services

- Docker Compose
- PostgreSQL
- MinIO
- Mailpit or equivalent email catcher
- Optional Redis
- Ollama-compatible local LLM endpoint
- Mock social provider
- Local webhook receiver
- Local OpenTelemetry collector and developer-friendly telemetry backend where practical

## 3. Planned repository structure

```text
products/marketing-ops/
├── apps/
│   ├── web/
│   │   ├── src/app/
│   │   ├── src/features/
│   │   ├── src/components/
│   │   └── tests/
│   ├── api/
│   │   ├── src/modules/
│   │   ├── src/http/
│   │   └── tests/
│   └── worker/
│       ├── src/jobs/
│       ├── src/schedulers/
│       └── tests/
├── packages/
│   ├── ui/
│   ├── contracts/
│   ├── domain/
│   ├── database/
│   ├── auth/
│   ├── social-connectors/
│   ├── ai/
│   ├── automation/
│   ├── observability/
│   ├── config/
│   └── test-fixtures/
├── infrastructure/
│   ├── local/
│   └── cloud/
├── migrations/
├── tests/
│   ├── integration/
│   ├── end-to-end/
│   ├── security/
│   ├── performance/
│   └── provider-contracts/
├── scripts/
└── docs/
```

## 4. Domain modules

### Identity and tenancy

Responsibilities:

- Organizations
- Workspaces
- Brands
- Users and memberships
- Teams
- Roles and permissions
- Invitations
- Sessions
- Service credentials
- Support-access grants

### Campaigns

- Campaigns and templates
- Briefs
- Objectives and key results
- Audiences and offers
- Channel plans
- Budgets as metadata in P0
- Milestones
- Readiness calculation
- Campaign portfolio

### Work management

- Work items and subtasks
- Dependencies
- Status workflows
- Custom fields
- Saved views
- Forms and request intake
- Recurrence
- Assignment and workload
- Comments, mentions, and activity

### Content operations

- Content items
- Channel variants
- Versions
- Rich copy blocks and structured publishing fields
- Brand-policy checks
- Links and tracking parameters
- Locale variants
- Content templates

### Asset management

- Assets and versions
- Uploads
- Metadata and previews
- Rights and expiration
- Associations to campaigns/content
- Review annotations
- Malware and file validation

### Approvals

- Approval policies
- Approval requests
- Approval steps
- Decisions
- Change requests
- Invalidation rules
- External reviewer access

### Social connections

- Provider apps
- OAuth state
- Social accounts
- Encrypted token envelopes
- Capabilities
- Connection health
- Reauthorization

### Publishing

- Schedules
- Publication groups
- Publications
- Publication attempts
- Media-upload sessions
- Provider status synchronization
- Retry and cancellation

### Engagement

- Synced conversations
- Messages/comments/mentions
- Assignments
- Reply drafts
- Reply approvals
- Resolution and SLA

### Analytics

- Raw metric snapshots
- Normalized metrics
- Campaign aggregates
- Operational aggregates
- Attribution metadata
- Report queries and exports

### Automations

- Automation definitions and versions
- Triggers
- Conditions
- Actions
- Execution plans
- Runs and step results
- Loop protection

### AI

- Provider registry
- Brand-context assembly
- Prompt/instruction versions
- Generation runs
- Candidate outputs
- Evaluations
- Cost and latency records

### Integrations and notifications

- Email
- Slack/Teams later
- Webhooks
- Public API keys
- Third-party storage/design tools later
- Notification preferences and delivery

### Governance

- Audit events
- Retention
- Data export/deletion
- Usage
- Billing events
- Feature flags
- Security events

## 5. Core entity model

### Tenancy

- `Organization`
- `Workspace`
- `Brand`
- `User`
- `Membership`
- `Team`
- `TeamMember`
- `Role`
- `PermissionGrant`
- `Invitation`
- `ServiceCredential`
- `SupportAccessGrant`

### Campaign and work

- `Campaign`
- `CampaignTemplate`
- `CampaignBrief`
- `CampaignObjective`
- `Audience`
- `Offer`
- `ChannelPlan`
- `Milestone`
- `WorkItem`
- `WorkItemDependency`
- `WorkItemAssignee`
- `WorkflowDefinition`
- `WorkflowStatus`
- `CustomFieldDefinition`
- `CustomFieldValue`
- `SavedView`
- `RequestForm`
- `RequestSubmission`

### Collaboration

- `Comment`
- `Mention`
- `Reaction`
- `ActivityEvent`
- `Notification`
- `NotificationDelivery`

### Assets and content

- `Asset`
- `AssetVersion`
- `AssetAnnotation`
- `ContentItem`
- `ContentVersion`
- `ChannelVariant`
- `ChannelVariantVersion`
- `ContentTemplate`
- `ContentPolicyResult`
- `LinkMetadata`

### Approvals

- `ApprovalPolicy`
- `ApprovalPolicyVersion`
- `ApprovalRequest`
- `ApprovalStep`
- `ApprovalDecision`
- `ApprovalSnapshot`

### Social and publishing

- `SocialProviderApp`
- `SocialConnection`
- `SocialAccount`
- `SocialCapabilitySnapshot`
- `PublicationGroup`
- `Publication`
- `PublicationAttempt`
- `ProviderMediaUpload`
- `ProviderWebhookEvent`
- `ConnectionHealthEvent`

### Engagement and analytics

- `SocialConversation`
- `SocialMessage`
- `SocialReply`
- `MetricDefinition`
- `MetricSnapshot`
- `NormalizedMetric`
- `ReportDefinition`
- `ReportExport`

### Automation and AI

- `AutomationDefinition`
- `AutomationVersion`
- `AutomationRun`
- `AutomationStepRun`
- `AIProviderConfiguration`
- `AIInstruction`
- `AIInstructionVersion`
- `AIGenerationRun`
- `AICandidate`
- `AIEvaluation`

### Platform

- `Job`
- `OutboxEvent`
- `WebhookEndpoint`
- `WebhookDelivery`
- `AuditEvent`
- `UsageRecord`
- `RetentionPolicy`
- `FeatureFlag`

## 6. Tenant isolation

Every tenant-owned row carries `organization_id`. Workspace- and brand-owned records also carry their narrower scope identifiers.

Requirements:

- API repository methods require an authorization scope object.
- Direct unscoped tenant-table queries are prohibited outside migrations, support tooling, and explicitly reviewed platform operations.
- Database row-level security should provide defense in depth when compatible with operational requirements.
- Object keys include non-guessable organization and asset identifiers.
- Cache keys include organization and authorization-relevant scope.
- Job payloads include immutable tenant context and are re-authorized or validated at execution.
- Search indexes and analytics aggregates preserve tenant boundaries.
- External client reviewers cannot infer internal work, users, comments, or other clients through IDs, counts, search, notifications, or exports.
- Cross-tenant tests cover HTTP, real-time subscriptions, signed URLs, jobs, exports, search, and provider webhooks.

## 7. ID and time rules

- Use sortable globally unique IDs for application entities.
- Expose stable public IDs separate from provider IDs.
- Store timestamps in UTC.
- Store an IANA time zone on organization/workspace/brand/user/account as appropriate.
- Resolve scheduled local times into UTC while retaining original local time and zone.
- Handle daylight-saving ambiguity explicitly; never silently shift a scheduled post.
- Use database-generated timestamps for canonical creation/update events where possible.

## 8. Append-oriented history

Mutable current records provide fast reads, while important transitions also create immutable or append-oriented events.

History is required for:

- Campaign status and date changes
- Work-item assignment, status, dependency, and due-date changes
- Content and asset versions
- Approval snapshots and decisions
- Publication schedule and attempt history
- Social connection changes
- Automation versions and runs
- AI instructions, inputs, candidates, and human selections
- Permission and security changes

No audit event should contain full secrets or unnecessary sensitive content.

## 9. Durable job architecture

### Job categories

- `notification.send`
- `asset.inspect`
- `asset.preview`
- `asset.transcode`
- `social.token.refresh`
- `social.capabilities.sync`
- `publication.validate`
- `publication.media.upload`
- `publication.publish`
- `publication.status.sync`
- `engagement.sync`
- `metrics.sync`
- `automation.evaluate`
- `automation.execute`
- `ai.generate`
- `report.export`
- `retention.delete`

### Job guarantees

- Durable state in PostgreSQL
- At-least-once execution
- Idempotency per business side effect
- Attempt history
- Lease/heartbeat for long tasks
- Bounded exponential backoff
- Dead-letter state
- Cancellation where provider semantics allow
- Concurrency and rate limits by organization, provider, account, and job type
- Graceful recovery after worker termination
- Operational replay with permission and audit event

### Transactional outbox

Business state and the event requesting a side effect must commit atomically. A dispatcher converts outbox events into jobs or webhook deliveries. Consumers record deduplication keys.

## 10. Publication state machine

```text
draft
→ validating
→ validation_failed | scheduled | queued
→ uploading_media
→ publishing
→ provider_processing
→ published
```

Exceptional states:

```text
failed_retryable
failed_terminal
cancelled
unknown_remote_state
deleted_remote
```

Rules:

- `publish now` still creates a durable publication and attempt.
- A publication cannot become eligible without a valid approval snapshot when policy requires approval.
- Editing material fields after approval invalidates approval and unschedules according to policy.
- Provider timeout is not automatically treated as failure; status reconciliation must detect possible remote success before retrying.
- Partial success is represented at publication-group level, not hidden.
- Retry uses the same business idempotency key where the provider supports it and platform-side duplicate protection where it does not.

## 11. Social provider interfaces

```ts
interface SocialConnector {
  provider: SocialProvider;
  getAuthorizationUrl(input: AuthorizationRequest): Promise<AuthorizationUrl>;
  exchangeAuthorizationCode(input: AuthorizationCode): Promise<TokenEnvelope>;
  refreshToken(input: RefreshTokenRequest): Promise<TokenEnvelope>;
  listAccounts(input: AccountDiscoveryRequest): Promise<DiscoveredAccount[]>;
  getCapabilities(input: CapabilityRequest): Promise<AccountCapabilities>;
  validatePublication(input: PublicationDraft): Promise<ValidationResult>;
  uploadMedia(input: MediaUploadRequest): Promise<ProviderMediaReference>;
  publish(input: PublishRequest): Promise<PublishResult>;
  getPublicationStatus(input: StatusRequest): Promise<ProviderPublicationStatus>;
  deletePublication?(input: DeleteRequest): Promise<DeleteResult>;
  fetchMetrics?(input: MetricsRequest): Promise<ProviderMetricSnapshot[]>;
  fetchEngagement?(input: EngagementRequest): Promise<ProviderConversationPage>;
  reply?(input: ReplyRequest): Promise<ReplyResult>;
  verifyWebhook?(input: WebhookVerificationInput): Promise<boolean>;
  parseWebhook?(input: ProviderWebhookInput): Promise<NormalizedProviderEvent[]>;
}
```

Connector implementations must never leak provider-specific fields into core campaign and content tables. Provider payloads live in typed adapter metadata and raw event storage.

## 12. Capability model

Capabilities are resolved per provider, account type, permission set, region, and current connection state.

Examples:

- Text post
- Image post
- Multi-image post
- Video post
- Short-form video
- Link card
- First comment
- Alt text
- Audience selection
- Location
- Mentions
- Hashtags
- Scheduled publishing through API
- Deletion
- Metrics
- Comments/messages

The UI renders only supported controls and explains unsupported or permission-gated behavior. Capabilities are cached with an expiration and resynchronized after connection or permission changes.

## 13. AI interfaces

```ts
interface AIProvider {
  provider: AIProviderName;
  listModels(): Promise<AIModelDescriptor[]>;
  generateStructured<T>(request: StructuredGenerationRequest<T>): Promise<StructuredGenerationResult<T>>;
  generateText(request: TextGenerationRequest): Promise<TextGenerationResult>;
  embed?(request: EmbeddingRequest): Promise<EmbeddingResult>;
  healthCheck(): Promise<ProviderHealth>;
}
```

Initial adapters:

- `OllamaAIProvider`
- `GeminiAIProvider`

Prepared adapters:

- `OpenAIProvider`
- `AnthropicProvider`

AI business services depend only on the internal interface. Prompt assembly, brand context, safety checks, and output validation live outside provider SDK code.

## 14. Media pipeline

1. Validate declared and actual media type.
2. Malware scan.
3. Compute content hash.
4. Extract dimensions, duration, codec, orientation, and metadata.
5. Store immutable original.
6. Generate safe preview and thumbnails.
7. Detect duplicates.
8. Evaluate target-platform requirements.
9. Create provider-specific derivative only when needed.
10. Preserve transformation provenance.
11. Upload or provide verified signed/pull URL according to connector capability.

Media transformations must be bounded by size, duration, pixel, memory, and time limits.

## 15. API boundaries

P0 endpoints are grouped under `/v1`.

Examples:

```text
POST   /v1/organizations
GET    /v1/workspaces
POST   /v1/brands
GET    /v1/campaigns
POST   /v1/campaigns
GET    /v1/campaigns/{id}
POST   /v1/campaigns/{id}/work-items
POST   /v1/content-items
POST   /v1/content-items/{id}/variants
POST   /v1/assets/upload-sessions
POST   /v1/approval-requests
POST   /v1/approval-steps/{id}/decision
POST   /v1/social/connections/{provider}/authorize
GET    /v1/social/connections/callback/{provider}
POST   /v1/publications
POST   /v1/publications/{id}/schedule
POST   /v1/publications/{id}/publish-now
POST   /v1/publications/{id}/retry
GET    /v1/calendar
POST   /v1/automations
POST   /v1/ai/generations
GET    /v1/analytics/campaigns/{id}
GET    /v1/audit-events
```

API rules:

- Generated OpenAPI
- Cursor pagination
- Idempotency keys for create/publish/approval/integration operations
- Optimistic concurrency for collaborative edits
- Field-level validation errors
- Stable machine-readable error codes
- Request/correlation IDs
- ETags or version numbers where appropriate
- Signed webhooks with timestamp and replay protection

## 16. Real-time model

Use real-time updates only for experiences that benefit:

- Presence in Content Studio and Campaign Room
- Comment and approval updates
- Work-item changes
- Publication status
- Automation progress
- Notification count

The server remains authoritative. Clients recover through refetch after reconnect. Presence is ephemeral and must not be used as business state.

## 17. Search

P0 may begin with PostgreSQL full-text and trigram search.

Searchable objects:

- Campaigns
- Work items
- Content
- Assets and metadata
- Comments when permitted
- Social accounts
- Publications

Search results must honor tenant and object permissions before ranking or displaying counts. A dedicated search engine is introduced only when scale or capability requires it.

## 18. Analytics storage

P0 uses PostgreSQL with partitioned or time-oriented metric tables and materialized aggregates where needed.

Rules:

- Retain raw provider metric name, value, dimensions, collection time, and payload reference.
- Map to normalized metrics through versioned definitions.
- Never imply equivalence when providers define metrics differently.
- Store snapshots rather than overwriting latest values.
- Aggregation jobs are replayable.
- Large-scale warehouse export is P1.

## 19. Configuration and secrets

Configuration is loaded from validated environment variables and database policy.

Secrets include:

- Database credentials
- Object-store credentials
- Session/OIDC secrets
- Social app credentials
- Social account token envelopes
- AI provider keys
- Webhook signing secrets
- Email credentials

Rules:

- No secrets in client bundles, source control, logs, analytics, or job error text.
- Production uses a managed secret store and encryption key service.
- Social tokens use envelope encryption with key version metadata.
- Rotation and revocation are supported.
- Local `.env.example` contains names and safe defaults only.

## 20. Local development contract

A new developer must be able to run:

```bash
pnpm install
pnpm local:up
pnpm db:migrate
pnpm db:seed
pnpm dev
pnpm test
```

The seeded environment includes:

- One organization
- One agency workspace and one client workspace
- Two brands
- Internal users and an external reviewer
- Campaign templates
- A sample campaign with tasks, assets, content, approvals, and publications
- Mock LinkedIn and Instagram accounts
- Configurable publication success/failure scenarios
- Synthetic metric snapshots
- Local AI disabled, mocked, or connected to Ollama

No external API key is required for the core E2E journey.

## 21. Deployment topology

Initial managed topology:

```text
CDN / edge
    ↓
Web application
    ↓
API service
    ↓
PostgreSQL + object storage
    ↓
Worker service(s)
    ↓
Social / AI / email providers
```

Scale independently by process before extracting microservices. Candidate future boundaries are media processing, provider webhooks, publication workers, analytics ingestion, and AI generation.

## 22. Evolution rules

Extract a service only when:

- Independent scaling is repeatedly required.
- A failure domain needs isolation.
- Provider network or security boundaries require it.
- A separate deployment region is required.
- A team has clear ownership and the module has a stable contract.

Do not introduce distributed systems merely to look enterprise-ready.