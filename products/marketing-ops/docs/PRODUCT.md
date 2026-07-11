# Product Requirements — Marketing Operations Platform

## 1. Product definition

Marketing Ops is a multi-tenant B2B application that unifies campaign planning, marketing project management, content production, approval, social scheduling, publication, engagement operations, and performance analysis.

The product is not a generic task manager with a calendar plugin and it is not only a social scheduler. Its defining feature is a connected campaign model in which goals, work, assets, channel-ready content, approvals, publications, metrics, and follow-up actions remain linked throughout the lifecycle.

### North-star promise

> Move from campaign brief to published, measurable content in one coordinated workspace.

### First target market

- In-house B2B marketing teams
- Marketing agencies managing multiple clients
- Multi-brand organizations
- Distributed marketing teams with formal approval requirements
- Content and social teams that currently coordinate through project tools, spreadsheets, chat, and separate schedulers

## 2. Primary problems

1. Campaign strategy is stored separately from execution work.
2. Project statuses do not reflect content readiness or publication status.
3. Social posts are duplicated manually across channels.
4. Approvals happen in chat, email, documents, and screenshots without a reliable record.
5. Assets are difficult to find, compare, approve, and reuse.
6. Marketing calendars are stale because tasks and scheduled posts are maintained independently.
7. Publishing failures are discovered late and handled manually.
8. Teams cannot explain why a campaign shipped late or which operational bottleneck hurt performance.
9. Agencies struggle to give clients visibility without exposing every internal detail.
10. AI-generated content often lacks brand context, provenance, quality gates, and approval controls.

## 3. Product hierarchy

```text
Organization
├── Members, teams, roles, billing, security, integrations
└── Workspace
    ├── Internal department or agency client boundary
    └── Brand
        ├── Brand profile, voice, audiences, channels, assets, policies
        └── Campaign
            ├── Brief, objective, audience, offer, budget, dates, channels
            ├── Milestones and work items
            ├── Deliverables and assets
            ├── Content items
            │   └── Channel variants
            ├── Approval flows
            ├── Publications
            ├── Conversations and engagement work
            └── Performance and follow-up actions
```

### Organization

The tenant and contractual security boundary.

### Workspace

A business unit, region, department, or agency client. Users may have different access in different workspaces.

### Brand

A reusable marketing identity containing:

- Name and visual identity
- Brand voice and tone
- Target audiences
- Products, services, and offers
- Required claims and prohibited claims
- Legal/compliance rules
- Default channels and account connections
- Asset library
- Content pillars
- Hashtag and terminology policy
- Locale and time-zone defaults
- Approval policy

### Campaign

The primary operating container. Every campaign has an owner, objective, status, date range, audience, channel plan, measurable outcomes, deliverables, milestones, and connected content.

### Work item

A task, approval step, content deliverable, milestone, review, or operational action. Work items can have dependencies, owners, due dates, custom fields, comments, attachments, and automations.

### Content item

The canonical content concept: a product announcement, thought-leadership post, event promotion, case study, newsletter excerpt, video, campaign message, or other publishable unit.

### Channel variant

A platform-specific version of a content item. Variants own channel-specific copy, media, alt text, first comment, hashtags, audience options, link tracking, publishing controls, and preview.

### Publication

A scheduled or attempted delivery of one channel variant to one connected social account.

## 4. Personas

### Organization Owner

- Creates the tenant
- Manages plan, security, billing, and organization-level policy
- Can delegate administration

### Marketing Operations Admin

- Configures workflows, templates, statuses, custom fields, automations, views, and integrations
- Monitors adoption, throughput, and operational health

### Marketing Leader

- Oversees portfolio, goals, budget, workload, campaign health, and performance
- Needs executive summaries without task-level noise

### Campaign Manager

- Creates briefs, plans milestones, assigns work, coordinates dependencies, requests approvals, and manages launch readiness

### Content Strategist / Copywriter

- Creates master content and channel variants
- Uses brand context and AI drafting tools
- Collaborates through comments and versions

### Designer / Video Producer

- Receives asset requests
- Uploads versions
- Responds to annotated feedback
- Hands off approved assets

### Social Media Manager

- Connects accounts
- Adapts content per platform
- Schedules and publishes
- Handles failed posts and engagement queues
- Monitors channel performance

### Reviewer / Legal / Executive Approver

- Reviews assigned content through a focused approval inbox
- Can approve, request changes, add comments, or delegate

### External Client Reviewer

- Sees only shared campaigns, assets, content, and approval requests
- Does not see internal notes, cost, staffing, or unrelated clients

### Analyst

- Builds reports across campaigns, channels, audiences, content types, and operational metrics

### Platform Operator

- Internal support role with explicit, time-bound, audited tenant access

## 5. Core lifecycle

```text
Idea
→ Intake
→ Briefing
→ Planned
→ In production
→ Internal review
→ Client/compliance approval
→ Ready to schedule
→ Scheduled
→ Publishing
→ Live
→ Measuring
→ Complete
→ Archived
```

Campaign and content statuses are independently configurable but must map to canonical states for reporting and automation.

## 6. Required end-to-end workflows

### 6.1 Campaign creation

1. User selects workspace and brand.
2. User creates from blank, template, duplicate, intake request, or AI-assisted brief.
3. User sets objective, audience, offer, dates, channels, budget, owner, and success measures.
4. Template generates milestones, work items, approval steps, and required content.
5. System validates owner, dates, missing required fields, and dependency structure.
6. Campaign Room opens with a readiness summary and next actions.

### 6.2 Marketing request intake

1. Requester submits a configurable form.
2. Request enters triage with requested date, audience, goal, deliverables, and attachments.
3. Operations user accepts, requests clarification, rejects, merges, or converts it.
4. Accepted request creates a campaign or work item from a template.
5. Requester receives status updates without receiving broader workspace access.

### 6.3 Work planning and execution

1. Campaign manager creates or generates work items.
2. Work is visible as list, board, calendar, timeline, and workload views.
3. Dependencies, milestones, owners, approval gates, and dates are enforced.
4. Contributors update status, attach assets, comment, and request review.
5. Campaign readiness updates from real underlying records rather than manual percentages.

### 6.4 Content production

1. User creates a canonical content item linked to campaign and deliverable.
2. User chooses content type and intended channels.
3. Content Studio loads brand, audience, campaign, and channel constraints.
4. User writes directly or uses AI to draft, rewrite, summarize, expand, translate, or repurpose.
5. User creates channel variants.
6. System runs content checks for required fields, length, links, claims, accessibility, assets, and brand policy.
7. Variants enter approval workflow.

### 6.5 Asset production

1. Asset request is created from campaign or content item.
2. Designer uploads one or more versions.
3. Reviewers annotate or comment on a version.
4. Version is approved, rejected, or superseded.
5. Approved version can be assigned to one or more channel variants.
6. Rights, expiration, owner, locale, and usage restrictions remain attached.

### 6.6 Approval

1. Requester selects an approval policy or uses brand default.
2. System creates ordered or parallel approval steps.
3. Approvers receive in-app and optional email/Slack/Teams notifications.
4. Reviewer sees copy, media, platform previews, changes, policy checks, and context.
5. Reviewer approves, requests changes, comments, or delegates.
6. Material changes after approval invalidate affected approvals according to policy.
7. Approval event is immutable and auditable.

### 6.7 Scheduling and publishing

1. Approved variant is added to calendar or assigned a scheduled time.
2. System validates account connection, platform capabilities, media, copy, links, approval state, time zone, and conflicts.
3. Publication job becomes eligible at scheduled time.
4. Worker publishes through the network adapter.
5. System stores provider ID, status, response, published URL, and retry context.
6. Partial success across a multi-channel group is visible and independently recoverable.
7. Failures include remediation and safe replay.

### 6.8 Engagement operations

1. Available comments, mentions, and messages are synchronized where provider capability and permissions allow.
2. Items are classified into inbox queues.
3. Team member assigns, replies, resolves, escalates, or creates follow-up work.
4. Sensitive replies can require approval.
5. Response history and provider delivery state are retained.

### 6.9 Performance loop

1. Publication metrics are collected as time-series snapshots.
2. Metrics are normalized while preserving raw provider values.
3. Campaign dashboards compare performance to goals and prior cohorts.
4. AI may summarize patterns and propose actions.
5. Users can convert a recommendation into an owned work item or experiment.
6. Decisions and results remain linked to the original content.

## 7. P0 functional requirements

### 7.1 Tenant and access

- Organizations and workspaces
- Brands
- Members, invitations, and teams
- Role-based permissions
- Workspace-, brand-, campaign-, and client-level access
- External reviewer role
- Service accounts and API keys
- Session management
- MFA-ready identity boundary
- Audit events for access and permission changes

### 7.2 Campaign and project management

- Campaign CRUD and templates
- Goals, audiences, offers, channels, dates, budget metadata, and owners
- Work items, subtasks, dependencies, milestones, estimates, priorities, and custom fields
- List, board, calendar, timeline, and workload views
- Saved views, filters, grouping, sorting, and sharing
- Comments, mentions, reactions, attachments, and activity timeline
- Recurring work
- Forms/intake requests
- Bulk edit and import
- Status workflows with transition rules
- Campaign readiness calculated from requirements and blockers

### 7.3 Content operations

- Canonical content items
- Platform-specific variants
- Rich text and plain-text editing
- Asset attachment
- Link metadata and tracking parameters
- Alt text and accessibility fields
- Content pillars, tags, audience, locale, and campaign associations
- Version history and comparison
- Content templates
- Duplicate and repurpose actions
- Brand and policy checks
- Draft, review, approved, scheduled, live, and archived states

### 7.4 Asset library

- Image, video, audio, PDF, and document assets
- Folderless organization through collections, campaigns, tags, and filters
- Versioning
- Metadata extraction
- Thumbnails and previews
- Rights owner, usage terms, expiration, locale, and accessibility metadata
- Duplicate detection by hash
- Search by filename, metadata, tags, campaign, and content relationship
- Signed uploads/downloads
- Quarantine and malware checks

### 7.5 Approvals

- Reusable approval policies
- Sequential and parallel steps
- Internal and external approvers
- Due dates and reminders
- Approve, request changes, reject, delegate, and withdraw
- Approval invalidation on material edits
- Approval evidence and immutable decision history
- Focused approval inbox
- Client-friendly shared review link or restricted portal

### 7.6 Social accounts and publishing

- Provider-neutral account connection model
- OAuth state and token lifecycle
- LinkedIn, Meta, X, TikTok, YouTube, and future network adapter interfaces
- Mock provider for local and automated tests
- Per-account capability discovery
- Composer validation against provider capabilities
- Schedule, reschedule, unschedule, cancel, publish now, retry, and duplicate
- Time-zone aware scheduling
- Media processing and upload stages
- Idempotent publication attempts
- Provider status polling and webhook handling
- Revoked/expired connection handling
- Published URL and provider identifiers
- Calendar visibility for every publication state

### 7.7 Automations

- Trigger, condition, action model
- Draft/test/publish/disable lifecycle
- Event triggers such as field change, status change, date reached, approval completed, publication failed, and metric threshold reached
- Scheduled triggers
- Actions such as create work item, update field, assign user, request approval, schedule publication, notify, call webhook, and generate AI draft
- Dry run and execution preview
- Bounded retries
- Run history and explanation
- Loop prevention
- Permission and scope validation

### 7.8 AI assistance

Initial providers:

- Local LLM through Ollama-compatible HTTP
- Gemini

Provider interfaces prepared for:

- OpenAI
- Anthropic
- Azure OpenAI
- Other enterprise or self-hosted models

Initial capabilities:

- Campaign brief drafting
- Task and milestone suggestions
- Content ideation
- Master-content drafting
- Platform-specific adaptation
- Tone and length rewriting
- Content repurposing
- Summarization
- Translation suggestions
- Alt text
- Content-risk and brand-policy review assistance
- Performance summaries and recommended experiments

AI output is always attributable, reviewable, and versioned. Publishing never occurs solely because an LLM requested it.

### 7.9 Notifications

- In-app notifications
- Email notification adapter
- Mentions
- Assignment
- Approval request and reminder
- Due/overdue work
- Publishing result and failure
- Connection expiration/revocation
- Automation failure
- Digest preferences

### 7.10 Reporting

Operational:

- Campaign status and risk
- Work completion and overdue trends
- Approval latency
- Time from brief to publish
- Workload by user/team
- Content throughput
- Publication success/failure

Performance:

- Posts and impressions/reach where available
- Engagements and engagement rate
- Clicks and link performance where available
- Video views and completion metrics where available
- Follower/audience trends where available
- Performance by campaign, brand, channel, content type, pillar, audience, and owner

Governance:

- Account connection health
- Audit events
- Automation executions
- AI usage and cost
- Storage and media usage
- API/webhook usage

## 8. P1 requirements

- SAML SSO and SCIM
- Custom roles
- Native Slack and Microsoft Teams integrations
- Google Drive, OneDrive, Dropbox, Box, Figma, and Canva integrations where feasible
- Social inbox expansion and response SLAs
- UTM builder and link shortener integration
- URL and landing-page review
- Paid-media work management and ad-platform metadata
- Budget planning and actuals
- CRM campaign/member sync
- Web analytics and conversion data
- Advanced portfolio/resource planning
- Localization workflows
- Content experiments and variant testing
- Client portal branding
- Public API and SDKs
- Data warehouse export

## 9. P2 expansion

- Influencer and creator operations
- Employee advocacy
- Partner/channel marketing
- Digital asset management depth
- Marketing resource management
- Event management
- Email campaign connectors
- Paid social execution
- Social listening
- Competitive intelligence
- AI agents with tightly scoped, approved action plans
- Marketplace and partner ecosystem

## 10. Explicit P0 non-goals

- Replacing a complete CRM
- Replacing a full enterprise DAM
- Running paid ad auctions directly
- Guaranteeing support for every social feature on every network
- Autonomous posting without user-configurable approval and safety gates
- General-purpose software project management
- Full financial accounting
- Social scraping outside provider-authorized APIs
- Fabricating metrics unavailable from a provider
- Building a pixel-identical copy of another product

## 11. Canonical statuses

### Campaign

```text
idea
requested
briefing
planned
active
at_risk
paused
completed
cancelled
archived
```

### Work item

```text
backlog
ready
in_progress
blocked
in_review
approved
done
cancelled
```

### Content item / variant

```text
draft
in_review
changes_requested
approved
ready_to_schedule
scheduled
publishing
published
partially_published
failed
archived
```

### Approval

```text
draft
pending
approved
changes_requested
rejected
withdrawn
expired
invalidated
```

### Publication

```text
draft
validation_failed
scheduled
queued
uploading
publishing
processing
published
failed_retryable
failed_terminal
cancelled
deleted_remote
unknown_remote_state
```

## 12. Permissions model

Permission capabilities include:

- Organization administration
- Workspace administration
- Brand administration
- Campaign create/read/update/archive
- Work-item create/update/assign/delete
- Internal-note access
- Asset upload/download/delete
- Content edit
- Approval request
- Approval decision
- Social account connect/manage
- Schedule
- Publish now
- Retry/cancel publication
- Engagement read/reply
- Analytics read/export
- Automation manage
- API/integration manage
- Billing manage
- Audit read/export
- Support impersonation grant

Permissions must be enforced in APIs, workers, subscriptions, object access, exports, and search—not only in navigation.

## 13. Product differentiation

1. **Campaign graph:** strategy, work, content, publication, and performance share one model.
2. **Launch readiness:** the system explains what is blocking launch and who owns each blocker.
3. **Best-in-class Campaign Room:** one surface replaces status meetings and spreadsheet coordination.
4. **Native variant design:** one idea becomes deliberate per-channel content without losing lineage.
5. **Operational automation:** approvals, dependencies, schedules, failures, and follow-up work connect safely.
6. **Client collaboration without leakage:** agencies can expose only the right context.
7. **Transparent AI:** outputs show provider, model, instruction, brand context, and human edits.
8. **Publication reliability:** account health, validation, idempotency, retries, and partial-success recovery are first-class.
9. **Execution plus outcomes:** teams see both marketing performance and operational causes.
10. **Premium UX:** density, speed, motion, hierarchy, and keyboard behavior are treated as product capabilities.

## 14. Success metrics

Adoption:

- Weekly active marketers per organization
- Campaigns actively managed
- Percentage of content linked to campaigns
- Percentage of publications scheduled through the platform

Efficiency:

- Median time from request to approved brief
- Median time from content draft to approval
- Median time from approval to scheduled publication
- On-time campaign completion
- Approval rework cycles
- Manual status-update reduction

Reliability:

- Publication success rate
- Retry recovery rate
- Account-connection health
- Automation success rate
- Notification delivery rate

Quality:

- User-rated content quality
- Brand-policy issue rate
- Accessibility completeness
- Client approval satisfaction
- AI suggestion acceptance and edit distance

Business:

- Customer retention
- Expansion by workspace/brand/account
- Cost per active campaign
- Gross margin by social and AI provider usage

## 15. P0 acceptance scenario

A new agency can create an organization, add a client workspace and brand, invite internal contributors and an external client reviewer, create a campaign from a template, generate a dependency-aware launch plan, create a master content item and LinkedIn/Instagram variants, attach an asset version, request sequential approval, receive a client change request, revise and reapprove the content, schedule both variants, publish through the local mock provider or configured real provider, recover one simulated failure, collect metric snapshots, and inspect a complete campaign activity and audit timeline without exposing internal notes to the client.