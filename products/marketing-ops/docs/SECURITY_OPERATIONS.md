# Security, Privacy, Reliability, and Operations

## 1. Security posture

Marketing Ops will hold confidential campaign plans, unpublished announcements, customer lists, brand assets, social-account credentials, performance data, private comments, and client approvals. A compromised account could expose sensitive information or publish externally under a customer’s identity.

Security and publishing reliability are release requirements, not later enterprise add-ons.

## 2. Threat model priorities

High-impact threats include:

- Cross-tenant or cross-client data access
- Stolen sessions or social OAuth tokens
- Unauthorized or duplicate publication
- Malicious uploaded assets
- Public exposure of unpublished content
- External reviewer privilege escalation
- Webhook spoofing and replay
- Automation loops and unintended bulk actions
- Prompt injection and private-note leakage through AI
- Provider callback manipulation
- Supply-chain compromise
- Support access abuse
- Data loss or unrecoverable scheduled work

The threat model must be updated for each material architecture or provider change.

## 3. Identity and session security

P0 requirements:

- Standards-based identity boundary
- Secure passwordless or password authentication through an approved provider
- MFA support
- Short-lived sessions
- Secure, HTTP-only, same-site cookies where browser sessions are used
- CSRF protection
- Session revocation
- Device/session visibility
- Brute-force and credential-stuffing protection
- Email verification and invitation expiry
- Reauthentication for sensitive operations

Sensitive operations may include:

- Connecting/disconnecting social accounts
- Viewing or rotating service credentials
- Changing roles
- Granting support access
- Publishing now outside normal policy
- Disabling approval requirements
- Exporting audit or tenant data
- Deleting a tenant/workspace

## 4. Authorization

Authorization is application-owned and enforced server-side.

Rules:

- Deny by default.
- Every request resolves organization and narrower scope.
- Background jobs carry and validate tenant scope.
- Real-time subscriptions are permission filtered.
- Search applies permissions before counts or result exposure.
- Signed object URLs are short-lived and scope checked before issue.
- External reviewer access is explicit, narrow, and tested against internal-note leakage.
- Bulk operations authorize every affected object or use a permission-safe scoped query.
- Audit export is separately permissioned.

Cross-tenant tests are mandatory for every new resource family.

## 5. Social credential security

Social credentials are among the highest-risk secrets.

Requirements:

- Exchange authorization codes server-side only.
- Encrypt access and refresh tokens using envelope encryption.
- Store key version and provider identity metadata.
- Restrict decryption to connector execution paths.
- Never send tokens to the web client.
- Never include tokens in logs, traces, analytics, exceptions, or ordinary database exports.
- Rotate platform app secrets through a documented procedure.
- Support provider revocation callbacks.
- Record connection and scope changes in audit.
- Revalidate connection ownership and account mapping after reauthorization.
- Remove token material on disconnect according to provider and retention obligations.

## 6. API and application security

- Central schema validation
- Parameterized database access
- Output encoding
- Content Security Policy
- Strict transport security
- Secure headers
- Rate limits by identity, tenant, endpoint, and provider operation
- Request-size limits
- Pagination limits
- Idempotency for consequential actions
- Optimistic concurrency for collaborative edits
- Safe redirect allowlists
- SSRF protection for link previews and provider pull URLs
- Dependency and secret scanning
- Static analysis
- Container scanning
- Software bill of materials
- Signed release artifacts where practical
- Security review for every public endpoint and webhook

## 7. File and media security

- Validate extension, declared media type, and detected media type
- Malware scan before normal access or processing
- Never execute macros or embedded active content
- Sandbox rendering/transcoding
- Archive-bomb and decompression protection
- Pixel, duration, frame, memory, file-size, and processing-time limits
- Metadata stripping or policy-controlled preservation
- Quarantine suspicious files
- Immutable originals
- Short-lived signed URLs
- Tenant-scoped object paths
- Content-disposition and safe content-type headers
- Duplicate detection without cross-tenant information leakage

## 8. Webhook security

Inbound provider webhooks:

- Exact provider route
- Signature/challenge verification
- Timestamp and replay checks where available
- Body-size and processing-time limits
- Raw-body preservation only as required for verification
- Persist-before-process
- Event deduplication
- Trusted account mapping
- Unknown event quarantine

Outbound customer webhooks:

- Per-endpoint signing secret
- Timestamped signature
- Delivery ID
- Idempotency/replay protection
- Bounded retries
- Endpoint disablement after repeated terminal failures
- Safe payload preview
- Secret rotation

## 9. AI-specific security

- Document, web, comment, and imported content is untrusted data.
- Models receive no arbitrary tool, filesystem, credential, or network access.
- Prompt sections are separated by role and source.
- Private/internal content cannot enter client-facing generation without explicit policy.
- Outputs are bounded and schema validated.
- Generated HTML/Markdown is sanitized.
- Tenant context, examples, embeddings, and caches are isolated.
- Provider requests use the minimum necessary context.
- Customer policy controls external provider use.
- No customer data is used for cross-tenant training or examples.
- Prompt-injection and data-leakage tests are part of release evidence.

## 10. Publishing safety

- Approval policy is checked at scheduling and execution.
- Approved content version is immutable for a publication attempt.
- Material edits invalidate approvals according to policy.
- “Publish now” requires explicit permission and consequence confirmation.
- Provider timeouts enter unknown-state reconciliation rather than blind retry.
- Publication retries are idempotent.
- Bulk publishing has preview, count, destinations, and schedule summary.
- Automation cannot bypass permission or approval checks.
- A kill switch can pause publication jobs globally, by provider, tenant, account, or campaign.
- Incident operators can quarantine an account without deleting data.

## 11. Privacy and governance

Before paid pilot:

- Data inventory
- Data-flow diagram
- Processor/subprocessor inventory
- Privacy policy
- Terms of service
- Data-processing agreement template
- Retention policy
- Customer export workflow
- Deletion workflow
- Provider-specific data-use and deletion obligations
- Cookie/session disclosure
- Support-access policy
- Incident-response policy
- Vulnerability-disclosure process

Deletion must cover:

- Relational records
- Object originals and derivatives
- Search indexes
- Job payloads
- Caches
- Webhook artifacts
- AI generation artifacts
- Analytics aggregates
- Backups according to documented expiry

## 12. Client and external-reviewer privacy

External users must not infer:

- Other workspaces or clients
- Internal users not exposed to them
- Internal comments
- Drafts not shared
- Staff workload
- Internal budget/cost fields
- Provider credentials
- Automation definitions
- Private AI context
- Unpublished campaigns outside their access

Tests include URL guessing, search, notifications, counts, exports, activity, asset URLs, and real-time updates.

## 13. Audit events

Append-oriented audit events are required for:

- Login, logout, MFA, session revocation, and security events
- Membership, role, team, and access changes
- Workspace/brand/campaign creation and archive
- Workflow/template/automation changes
- Content and asset version changes
- Approval requests and decisions
- Social connection, account, scope, and health changes
- Scheduling, rescheduling, cancellation, and publish-now actions
- Publication attempts, retries, reconciliation, and deletion
- Engagement replies
- AI generation and accepted candidate provenance
- API key and webhook changes
- Retention, export, and deletion actions
- Support access

Audit records include tenant, actor, action, target, timestamp, correlation ID, source metadata where appropriate, and a safe summary. Secret values and unnecessary full content are excluded.

## 14. Reliability model

- Durable PostgreSQL job state
- Transactional outbox
- At-least-once processing with idempotent side effects
- Lease/heartbeat and worker-loss recovery
- Bounded retries with jitter
- Dead-letter handling
- Provider circuit breakers
- Rate-aware scheduling
- Publication status reconciliation
- Stage-level and operation-level replay
- Graceful degradation of analytics/AI when publishing and project work remain healthy
- Production database backups and point-in-time recovery
- Object versioning/lifecycle where justified
- Tested restores

## 15. Service objectives

Define and measure separate objectives for:

- Interactive API availability
- Scheduled publication eligibility latency
- Publication job pickup latency
- Notification latency
- Social connection health checks
- Webhook processing
- Data durability
- Recovery point objective
- Recovery time objective

Do not promise customer SLAs until the deployed architecture and support model can meet them.

## 16. Observability

Use correlation across browser/API/job/provider/webhook operations.

### Metrics

- Request volume/error/latency
- Database and storage health
- Job queue depth and oldest age
- Job success/retry/dead-letter rate
- Scheduled publications due versus picked up
- Provider publish success/failure/latency
- Unknown remote state count
- Token refresh and connection-health failures
- Webhook verification and processing failures
- Approval latency
- Automation execution failures
- AI latency, error, usage, and cost
- Tenant storage and quota

### Logs

- Structured
- Correlation-aware
- Redacted
- No tokens or secrets
- No raw private content by default
- No signed URLs
- Machine-readable error codes

### Traces

Trace:

- API request
- Database operations
- Job enqueue and execution
- Provider call
- Webhook processing
- Notification delivery

## 17. Alerts

Alert on:

- Scheduled publication queue delay
- Provider outage or elevated failure
- Unknown-state growth
- Repeated duplicate-prevention events
- Token-refresh failure
- Expiring/revoked high-value connections
- Webhook signature failures
- Automation loop protection
- Cross-tenant authorization anomaly
- Backup or restore failure
- Storage/database quota risk
- AI cost anomaly
- Malware/quarantine spike
- Error-budget burn

Each alert has owner, severity, runbook, and customer-impact definition.

## 18. Required runbooks

- Social provider outage
- Scheduled publication backlog
- Unknown remote publication state
- Duplicate or unauthorized publication suspicion
- Revoked/expired account connection
- Webhook outage
- Automation runaway or incorrect bulk change
- Cross-tenant access incident
- Credential exposure and rotation
- Malware upload
- Database restore
- Object recovery
- Customer data export/deletion
- AI provider outage or leakage concern
- Free-tier quota exhaustion
- Security incident communication

Runbooks contain detection, triage, containment, recovery, verification, communication, and post-incident actions.

## 19. Testing strategy

### Unit

- Permissions
- Status transitions
- Approval invalidation
- Schedule/time-zone handling
- Idempotency
- Provider error mapping
- Automation conditions/actions
- AI schema and context policies

### Integration

- Auth and tenant repositories
- Upload/object storage
- Job/outbox
- Approval flow
- Schedule and publish mock connector
- Token encryption
- Webhooks
- Metrics sync
- Notifications

### End-to-end

- Organization to campaign
- Work and content production
- External approval
- Scheduling
- Success and partial failure
- Retry/reconciliation
- Analytics and audit

### Security

- Cross-tenant and cross-client access
- OAuth state/callback attacks
- Token redaction
- Signed URL authorization
- Webhook spoof/replay
- SSRF
- File limits
- Prompt injection
- Permission bypass through automation/jobs

### Performance

- Large campaign portfolio
- Thousands of work items
- Dense calendar
- Large content inventory
- Concurrent autosave/comments
- Scheduled publication burst
- Metric ingestion

## 20. Release evidence

Every production release contains:

- Test results
- Security scans
- Provider contract results
- Migration and rollback plan
- Performance comparison
- Accessibility/visual-regression results for UI changes
- Known limitations
- Feature-flag/rollout plan
- On-call owner
- Go/no-go decision

## 21. Production gates

### Security

- Cross-tenant suite passes
- No unresolved critical findings
- Token encryption and rotation verified
- OAuth and webhook security tests pass
- File scanning/limits verified
- Prompt-injection/leakage tests pass
- Privacy export/deletion tested

### Publishing

- Mock connector passes full contract
- Required real providers have approved production access
- Scheduled jobs survive worker loss
- Idempotency and unknown-state reconciliation tested
- Partial failure recovery tested
- Provider kill switch tested

### Reliability

- Backup restore succeeds
- Jobs/outbox recover correctly
- Alerts and runbooks exist
- Provider outage behavior tested
- Time-zone and daylight-saving tests pass

### UX

- Flagship journeys pass usability tests
- Keyboard and accessibility checks pass
- Failure states provide remediation
- External client access is understandable and isolated
- High-density performance targets pass

### Operations and legal

- Support/severity process exists
- Provider and subprocessor terms fit customer data
- Terms, privacy, and DPA exist
- Usage/cost can be reconciled
- Availability promises match actual infrastructure

## 22. Paid-pilot gate

No paid pilot starts until one customer can plan a campaign, complete work, produce and approve content, schedule and publish through required real providers, recover a simulated failure, collect metrics, audit all actions, export/delete data according to policy, and receive support in an environment with appropriate backups, monitoring, provider approvals, and operational ownership.