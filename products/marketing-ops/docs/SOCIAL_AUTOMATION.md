# Social Automation and Publishing

## 1. Purpose

This document defines the provider-neutral model for connecting social accounts, creating native channel variants, scheduling, publishing, reconciling remote state, collecting metrics, and operating an engagement inbox.

Social providers change capabilities, permissions, rate limits, review requirements, and policies frequently. The platform must discover and enforce current account capabilities rather than assuming one universal publishing contract.

## 2. Product rule

> A social post is not a string copied to several networks. It is a content concept with deliberate channel variants and independent publication outcomes.

The product stores:

```text
Content item
├── LinkedIn variant
├── Instagram variant
├── Facebook variant
├── X variant
├── TikTok variant
└── YouTube variant
```

Each variant can have different copy, media, alt text, title, CTA, link, hashtags, audience/privacy, first comment, thumbnail, and publishing settings.

## 3. Provider rollout

### P0

- Fully deterministic local mock provider
- Provider contract test suite
- One business-oriented real provider completed end to end
- Second provider completed before paid pilot when customer workflow requires it
- Adapter shells and capability model for remaining named providers

Recommended real-provider order for the first B2B pilot:

1. LinkedIn organization/member publishing
2. Meta Facebook Page and Instagram professional-account publishing
3. X publishing
4. TikTok content posting
5. YouTube video publishing
6. Pinterest or other networks based on customer demand

The final order depends on actual pilot customers, API access approval, geography, required formats, and provider terms.

## 4. Connection model

### SocialProviderApp

Represents the platform-owned developer application registered with a provider:

- Provider
- Environment
- Client/application identifier
- Secret reference
- Redirect URIs
- Approved products/scopes
- App-review status
- Region restrictions
- Terms/privacy version
- Operational owner

### SocialConnection

Represents an OAuth grant by an authorized provider user:

- Organization/workspace
- Provider
- Provider user identifier
- Granted scopes
- Encrypted access/refresh token envelope
- Token type
- Issued/expiry times where supplied
- Last refresh
- Revocation state
- Connection owner
- Health state
- Reauthorization reason

### SocialAccount

A publishable or monitorable destination discovered from a connection:

- Provider
- Provider account/page/channel/organization identifier
- Display name and handle
- Account type
- Avatar/logo reference
- Locale/time zone where available
- Permission relationship
- Connection used
- Capability snapshot
- Enabled brands/workspaces
- Default approval/schedule policy
- Status

One connection may expose several accounts. Access to an account is granted explicitly inside the platform.

## 5. OAuth and authorization flow

1. User selects provider.
2. API creates short-lived signed OAuth state containing tenant, user, intended workspace, nonce, and return path.
3. User is redirected to provider authorization.
4. Callback validates state, issuer/provider, nonce, redirect, and authenticated user.
5. API exchanges code server-side.
6. Token is encrypted before persistence.
7. Account-discovery job lists available destinations.
8. User selects accounts and assigns brands/workspaces.
9. Capability sync runs.
10. Audit events record connection and account enablement without recording secrets.

Security requirements:

- Authorization code never reaches ordinary client logs.
- PKCE is used where supported/required.
- State is single use and expires quickly.
- Redirect URIs are exact allowlisted values.
- Tokens are never returned to the browser after exchange.
- Refresh occurs only server-side.
- Provider revocation and data-deletion callbacks are supported where required.

## 6. Token lifecycle

Connection health:

```text
healthy
expiring
refresh_required
scope_missing
reauthorization_required
revoked
provider_error
disabled
```

Rules:

- Encrypt token payloads with versioned envelope encryption.
- Store only provider-required token material.
- Refresh early enough to avoid scheduled-post failure.
- Serialize refresh operations per connection.
- Validate that a refreshed token belongs to the same provider identity.
- Re-run account and capability discovery after scope changes.
- Notify owners before known expiration where possible.
- Block scheduling beyond a known token lifetime only when policy requires it; otherwise display risk.
- Remove or render unusable token material during connection deletion according to retention policy.

## 7. Dynamic capability model

A capability snapshot is tied to:

- Provider
- Provider API version
- Account type
- Granted scopes
- App approval status
- Region
- Connection health
- Retrieval time

Representative capabilities:

```text
publish.text
publish.link
publish.image.single
publish.image.multiple
publish.video
publish.short_video
publish.story
publish.document
publish.poll
publish.first_comment
publish.alt_text
publish.thumbnail
publish.location
publish.audience
publish.mentions
publish.hashtags
publish.schedule_native
publish.delete
analytics.post
analytics.account
engagement.read
engagement.reply
webhook.publication
webhook.engagement
```

Each capability may include constraints:

- Text limits
- Media count
- Accepted formats/codecs
- Size and duration limits
- Aspect ratios
- Required fields
- Visibility options
- Upload method
- Processing behavior
- Rate-limit category

The composer uses capabilities to render fields and validation. Unsupported features are not silently removed.

## 8. Canonical variant model

Common fields:

- `content_item_id`
- `provider`
- `format`
- `body`
- `title`
- `description`
- `link_url`
- `cta`
- `hashtags[]`
- `mentions[]`
- `asset_assignments[]`
- `alt_text[]`
- `first_comment`
- `locale`
- `audience_settings`
- `privacy_settings`
- `thumbnail_asset_version_id`
- `provider_settings`
- `approval_state`
- `version`

Provider settings are schema-validated adapter metadata. Core product logic never assumes those fields exist across networks.

## 9. Composer validation

Validation layers:

### Platform-internal

- Required brand/campaign association
- Required owner
- Content and asset existence
- Rights/expiration
- Approval state
- Account access
- Link/UTM policy
- Brand/compliance checks

### Capability

- Supported format
- Required provider fields
- Text length
- Media count/type/dimensions/duration
- Alt text availability
- Audience/privacy options
- Mention syntax

### Connection

- Account enabled
- Token healthy
- Required scopes granted
- Provider app approved for action
- Capability snapshot fresh enough

### Schedule

- Valid date and time zone
- Not in blocked window
- Not conflicting with campaign policy
- Future enough for media processing and retries
- Approval remains valid

Validation results contain severity, code, message, field path, source, and fix action.

## 10. Schedule model

A publication schedule stores:

- Requested local date/time
- IANA time zone
- Resolved UTC instant
- Resolution policy for daylight-saving ambiguity
- Scheduling user
- Scheduling version
- Eligibility time
- Optional campaign embargo
- Optional provider-native schedule reference

Default P0 behavior uses platform-managed scheduling so all providers share one durable orchestration model. Provider-native scheduling may be used behind the connector when it materially improves reliability and can be reconciled safely.

### Reschedule

- Creates schedule history.
- Cancels or updates pending provider-native state when applicable.
- Revalidates approval and account capability.
- Does not mutate a currently publishing attempt.

### Unschedule

- Removes future eligibility.
- Attempts remote cancellation when applicable.
- Records whether remote cancellation is confirmed.

## 11. Publication group

A user action such as “schedule LinkedIn, Instagram, and X” creates a publication group with independent child publications.

Group status is derived:

```text
all_scheduled
in_progress
all_published
partial_success
all_failed
cancelled
```

A group never hides per-destination outcomes.

## 12. Publication lifecycle

```text
draft
→ validating
→ scheduled
→ queued
→ uploading_media
→ publishing
→ provider_processing
→ published
```

Failure paths:

```text
validation_failed
failed_retryable
failed_terminal
unknown_remote_state
cancelled
deleted_remote
```

### Unknown remote state

This state is critical. A timeout after sending a provider request may mean the provider accepted the post but the response was lost.

Required behavior:

1. Do not immediately retry.
2. Query remote status where possible.
3. Search using provider request/idempotency reference where possible.
4. Reconcile webhook events.
5. Escalate for human verification when remote status cannot be determined safely.
6. Prevent duplicate retries until uncertainty is resolved or explicitly overridden.

## 13. Idempotency

Platform idempotency key:

```text
organization + publication_id + approved_variant_version + scheduled_generation
```

Rules:

- Every attempt references the publication and idempotency generation.
- Provider idempotency keys are used when supported.
- Media upload references are reused only when safe and not expired.
- A published provider post ID permanently satisfies that publication generation.
- Manual duplicate creates a new publication, not a retry.
- Webhook and poll events are deduplicated by provider event/reference.

## 14. Media upload and transformation

Connector declares whether it needs:

- Direct multipart upload
- Chunked/resumable upload
- Pull from a verified URL
- Prior media-container creation
- Asynchronous processing

Pipeline:

1. Validate approved asset version.
2. Check rights and expiration at schedule and publish time.
3. Select or create compliant derivative.
4. Upload or expose bounded signed/verified URL.
5. Store provider media reference and expiry.
6. Poll processing when required.
7. Publish using provider reference.
8. Retain transformation and upload attempt history.

Original assets remain immutable.

## 15. Provider request logging

Store safe operational metadata:

- Provider
- API operation
- Provider request ID
- Status code/category
- Latency
- Rate-limit metadata
- Retry-after
- Payload schema/version
- Redacted request/response artifact reference when policy permits

Never place access tokens, authorization headers, unredacted private messages, or unnecessary post content in general logs.

## 16. Rate limits

Implement rate limiting at:

- Provider app
- Connection/user
- Destination account
- Endpoint category
- Organization plan

Requirements:

- Parse provider rate-limit headers when supplied.
- Respect retry-after.
- Apply jitter.
- Separate interactive validation from background synchronization where possible.
- Prioritize scheduled publications over non-urgent metric refreshes.
- Avoid thundering-herd token refresh or midnight scheduling.
- Display quota/rate-limited state with expected recovery when known.

## 17. Webhooks

Webhook ingress:

1. Route by provider and environment.
2. Verify signature/challenge according to provider contract.
3. Enforce size and time limits.
4. Persist raw event reference before asynchronous processing.
5. Acknowledge within provider deadline.
6. Normalize into internal events.
7. Deduplicate.
8. Resolve tenant/account through trusted mappings.
9. Apply state transition or enqueue reconciliation.

Unknown or unmapped events are quarantined, not guessed.

## 18. Polling and reconciliation

Because provider webhook coverage varies, polling remains required.

Jobs:

- Newly published status: frequent bounded polling
- Provider-processing media: format-specific polling
- Recent publication reconciliation
- Daily connection/account health
- Metric snapshots
- Engagement sync
- Periodic remote-deletion detection where permitted

Polling cursors and windows are persisted. Jobs must be replayable and rate-aware.

## 19. Metrics

### Raw metric snapshot

- Provider
- Account/post ID
- Metric name
- Metric value
- Dimensions
- Provider definition/version when known
- Collected at
- Data-through time where supplied
- Raw payload reference

### Normalized metric

Examples:

- impressions
- reach
- engagements
- reactions
- comments
- shares/reposts
- clicks
- video views
- watch time
- followers

Normalization rules are versioned and never imply that metrics with different definitions are directly equivalent.

### Collection cadence

Configurable cohorts, for example:

- Shortly after publish for status only
- 1 day
- 3 days
- 7 days
- 14 days
- 30 days
- Long-term cadence for evergreen content where useful

Cost and rate limits control cadence.

## 20. Engagement inbox

Provider support differs. The internal model must distinguish:

- Comment
- Reply
- Mention
- Direct message
- Review or other interaction type

Each item stores provider identifiers, account, source publication/campaign when known, author data allowed by provider, body/media reference, timestamps, sync state, assignment, resolution, and reply history.

### Reply safety

- Validate permission and connection at send time.
- Support approval-required policies.
- Store draft and approved reply versions.
- Treat timeout as unknown remote state where duplicate replies are possible.
- Never simulate success if provider does not support replying.

## 21. Local mock provider

The mock provider is a production-quality test adapter, not a trivial stub.

It supports configurable scenarios:

- Successful text/image/video publish
- Validation failure
- Token expiration
- Missing scope
- Rate limit
- Upload failure
- Provider timeout before acceptance
- Provider timeout after acceptance
- Asynchronous processing
- Webhook delivery
- Delayed metric growth
- Remote deletion
- Engagement and replies

Mock state is inspectable through a local provider console and seeded fixtures.

## 22. Provider contract tests

Every connector passes the same suite:

- Authorization URL construction
- State/callback validation boundary
- Token exchange/refresh parsing
- Account discovery
- Capability mapping
- Validation mapping
- Media upload
- Publish success
- Publish validation failure
- Retryable and terminal failure mapping
- Timeout/unknown-state behavior
- Status reconciliation
- Webhook verification and normalization
- Metric normalization
- Token redaction
- Rate-limit handling

Live sandbox tests are separate from deterministic contract tests.

## 23. App review and compliance

A connector cannot be called production-ready until:

- Provider developer application exists in production environment.
- Required products and scopes are approved.
- Redirect, privacy, terms, deletion, and review requirements are satisfied.
- Test accounts are available.
- Provider branding and UX requirements are implemented.
- Data retention and deletion obligations are documented.
- Rate and quota behavior is measured.
- Support and incident path exists.

For example, TikTok direct posting requires an approved publishing scope and authorization by the target user; unaudited clients are restricted in visibility. The implementation must therefore model app-review state and cannot assume that successful sandbox code means public production publishing is enabled.

## 24. Prohibited behavior

- Scraping networks or automating consumer web interfaces
- Circumventing app review or permissions
- Posting to an account without explicit authorization
- Fabricating unsupported platform features
- Storing tokens unencrypted
- Logging secrets
- Retrying ambiguous publishes blindly
- Hiding partial failure
- Reusing media or content in violation of rights/expiration
- Sending AI-generated replies or posts without configured review policy
- Misrepresenting preview as exact provider rendering

## 25. Production gates

- At least one real provider passes authorization, publish, status, metric, and revocation flows.
- Mock and real connectors pass contract tests.
- Account tokens are encrypted and rotation is tested.
- Scheduled jobs survive worker termination.
- Time-zone and daylight-saving tests pass.
- Unknown remote state prevents duplicates.
- Partial-success recovery is usable.
- Provider rate limits are respected.
- App-review and policy requirements are documented and accepted.
- Published IDs/URLs and audit history reconcile with the provider.
- Metrics show freshness and definition caveats.
- Deletion/disconnect handling is tested.