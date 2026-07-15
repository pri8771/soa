# UI/UX Blueprint — Marketing Operations Platform

> **Design objective:** make campaign operations feel coordinated, creative, fast, and calm—even when teams manage many clients, deadlines, approvals, assets, and social channels.

> **Quality bar:** the product must feel deliberately designed for marketing operations, not assembled from a generic admin template, copied from a project-management tool, or reduced to a social calendar.

## 1. Experience mandate

The interface should unite two modes that usually conflict:

- **Operational density:** assignments, dates, status, dependencies, approvals, publishing health, and metrics.
- **Creative focus:** briefs, copy, assets, variants, visual review, and brand context.

The product must shift between these modes without making the user feel they entered a different application.

### 1.1 Desired emotional qualities

- Confident
- Contemporary
- Precise
- Warm without becoming playful
- Creative without becoming decorative
- Dense without becoming cramped
- Powerful without becoming intimidating
- Trustworthy around consequential publishing actions

### 1.2 Explicitly avoid

- A landing-page hero inside the app
- A dashboard made entirely of disconnected cards
- Neon gradients used as “AI” decoration
- Excessive glass effects
- Emoji as primary navigation
- Invisible icon-only actions without labels or tooltips
- Social-network logos dominating the product identity
- Generic Kanban as the entire product
- A chat panel as the default answer to every workflow
- Hiding critical state behind hover
- Color-only status meaning
- Motion that slows frequent work
- Copying another product’s layout, branding, iconography, or proprietary interactions

## 2. Core interaction principles

1. **Campaign context is persistent.** Objective, owner, dates, brand, channel plan, blockers, and approval state remain easy to access.
2. **One object, many views.** List, board, calendar, timeline, and workload are projections of the same records.
3. **Content lineage is visible.** Users can move from master content to variants, approvals, publications, and metrics without searching.
4. **Action follows attention.** The most important next action is visible near the reason it is needed.
5. **Readiness is explained.** The system never displays a vague percentage without the underlying requirements and blockers.
6. **High-risk actions feel different.** Publishing, deleting, revoking access, changing an approved item, and bulk automation require clear consequence summaries.
7. **Keyboard-first for repeated work.** Power users can triage tasks, approvals, and posts quickly.
8. **Fast perceived performance.** Navigation, filters, and local edits respond immediately; long work shows meaningful progress.
9. **Progressive disclosure.** Beginners see the main workflow while experts can reveal dependencies, custom fields, versions, payloads, and provider details.
10. **Accessibility is structural.** Semantic controls, keyboard behavior, focus order, contrast, labels, and announcements are designed from the beginning.

## 3. Information architecture

### 3.1 Primary navigation

```text
Home
Campaigns
Work
Calendar
Content
Assets
Approvals
Publishing
Inbox
Analytics
Automations
```

Administration is separated visually:

```text
Brands
Templates
Integrations
Team & Access
Security & Audit
Usage & Billing
```

### 3.2 Context hierarchy

The application shell shows:

- Organization
- Workspace/client
- Optional brand filter
- Current view
- Environment when not production

The user must always know which client or brand is active. Agency users must not be able to confuse similarly named campaigns across clients.

### 3.3 Global utilities

- Command palette
- Universal search
- Create menu
- Notifications
- Help and shortcut reference
- Connection-health indicator when action is required
- User menu

### 3.4 Command palette

Capabilities:

- Navigate anywhere
- Search campaigns, work, content, assets, and people
- Create campaign, task, content, asset request, or post
- Switch workspace/brand
- Open recent items
- Run allowed actions such as mark complete or request approval
- Open keyboard-shortcut help

Commands display scope and consequence. High-risk actions require confirmation outside the palette.

## 4. Application shell

### 4.1 Desktop layout

```text
┌───────────────────────────────────────────────────────────────┐
│ global bar: context · search · create · notifications · user │
├──────────────┬────────────────────────────────────────────────┤
│ primary nav  │ page header                                    │
│              ├────────────────────────────────────────────────┤
│              │ main content                                   │
│              │                                                │
│              │                                                │
└──────────────┴────────────────────────────────────────────────┘
```

- Collapsible left navigation
- Persistent global bar
- Page header may become sticky when scrolling
- Optional contextual right rail
- Resizable panes for Campaign Room, Content Studio, and approvals

### 4.2 Breakpoints

- **Large desktop:** full nav, multi-pane workspaces, dense tables
- **Laptop:** compact nav, resizable panes, condensed secondary metadata
- **Tablet landscape:** navigation drawer, two-pane maximum
- **Mobile:** approvals, comments, status updates, notifications, and light scheduling only; full campaign planning remains desktop-first

### 4.3 Page header grammar

Every primary page can include:

- Breadcrumb or parent context
- Title and compact state
- Owner/avatar group
- Date range or schedule
- Key health indicator
- Primary action
- Overflow action menu
- View switcher
- Filter/search controls

Do not repeat the same information in both header and first card.

## 5. Design system

## 5.1 Token architecture

Tokens are semantic and mode-aware:

```text
color.background.canvas
color.background.subtle
color.surface.default
color.surface.raised
color.surface.sunken
color.border.default
color.border.strong
color.text.primary
color.text.secondary
color.text.muted
color.text.inverse
color.action.primary
color.action.primaryHover
color.selection.background
color.status.success
color.status.warning
color.status.danger
color.status.info
color.status.neutral
color.focus.ring
```

Avoid component-specific arbitrary colors. Provider brand colors may appear in small account/network indicators but never control status meaning.

## 5.2 Visual theme

Recommended direction:

- Neutral canvas with subtly warm undertone
- Crisp white or near-white primary surfaces in light mode
- Deep graphite surfaces in dark mode
- One distinctive product accent used sparingly
- Slightly tinted status backgrounds rather than saturated blocks
- Fine borders and restrained shadows
- Editorial typography hierarchy for campaign and content surfaces
- Tabular numerals for dates, budgets, counts, and metrics

## 5.3 Typography roles

- Display: rare, campaign/title moments only
- Page title
- Section title
- Component title
- Body
- Compact body
- Label
- Metadata
- Code/identifier
- Metric numeral

Requirements:

- Highly legible sans-serif
- Comfortable x-height
- Clear numeral shapes
- Robust weights
- No font dependence on private local files
- System fallback stack

## 5.4 Spacing

Use a 4-pixel base scale with named semantic spacing.

- Tight inline gap: 4
- Compact control gap: 8
- Standard component gap: 12
- Section interior: 16–20
- Major section separation: 24–32
- Page edge: 24 laptop, 32 large desktop

Dense mode reduces row height and vertical gaps, not text size below accessibility targets.

## 5.5 Shape and elevation

- Moderate radius, not pill-shaped everything
- Buttons may use a smaller radius than large panels
- Status chips use compact radius
- Cards used only for meaningful grouping
- Tables, split panes, and canvases are primary structural patterns
- Elevation denotes overlay or layering, not decorative importance

## 5.6 Motion

- 120–180 ms for micro-interactions
- 180–240 ms for panel transitions
- No long entrance animations in repeated workflows
- Motion communicates relationship, state change, or successful placement
- Respect reduced-motion preference
- Publishing and automation progress use stateful indicators, not endless decorative animation

## 6. Component inventory

### Navigation and context

- App shell
- Workspace/client switcher
- Brand switcher
- Breadcrumbs
- Tabs
- Segmented view switcher
- Command palette
- Global search

### Data display

- Virtualized data table
- Board column and card
- Calendar grid and event
- Timeline/Gantt row
- Workload chart
- Metric tile
- Trend chart
- Activity timeline
- Audit event
- Status chip
- Health badge
- Avatar stack
- Provider/account badge

### Input and editing

- Inline field editor
- Rich text editor
- Plain social copy editor
- Date/time-zone picker
- User/team picker
- Multi-select
- Custom-field editor
- Dependency picker
- Link/UTM editor
- Asset picker
- Channel selector
- Approval policy builder
- Automation builder

### Creative and review

- Asset previewer
- Version rail
- Annotation overlay
- Platform post preview
- Master/variant navigator
- Content checks panel
- Change comparison
- Approval decision bar

### Feedback

- Toast
- Inline error
- Persistent incident banner
- Empty state
- Loading skeleton
- Progress stepper
- Conflict dialog
- Undo banner
- Permission state
- Quota state

## 7. Home / Marketing Command Center

### Purpose

Answer:

- What needs attention now?
- Which campaigns are at risk?
- What is blocked on me or my team?
- What will publish soon?
- What failed?
- Where is performance changing?

### Layout

1. **Focus strip**
   - My overdue work
   - Approvals waiting on me
   - Failed publications
   - Account connections needing action
2. **Campaign health table**
   - Campaign
   - Brand/workspace
   - Owner
   - Stage
   - Target date
   - Readiness
   - Blockers
   - Next milestone
3. **Upcoming calendar**
   - Seven-day view of milestones, approvals, and posts
4. **Team workload**
   - Capacity and overdue risk
5. **Performance watchlist**
   - Material changes and anomalies
6. **Recent activity**

Each item links to a pre-filtered operational view. Avoid vanity charts without a next action.

### Empty state

For a new organization:

- Create workspace/brand
- Invite team
- Create campaign from template
- Connect mock or real social account
- Load sample campaign option

## 8. Campaign portfolio

### Views

- Table
- Board by canonical or custom status
- Timeline
- Calendar
- Portfolio summary

### Table columns

- Campaign name
- Workspace and brand
- Objective
- Owner
- Status
- Start/end
- Next milestone
- Readiness
- At-risk reason
- Channel count
- Scheduled/live content count
- Goal progress

### Row interaction

- Single click selects and opens contextual preview
- Enter opens Campaign Room
- Inline edit for low-risk fields
- Bulk actions for owner, status, dates, tags, archive
- High-impact bulk changes show preview

### Filters

- Workspace
- Brand
- Status
- Owner/team
- Objective
- Channel
- Date range
- Risk
- Approval state
- Publishing state
- Template
- Tags/custom fields

## 9. Campaign Room — flagship surface

The Campaign Room is the center of the product.

### 9.1 Header

- Campaign name
- Brand/workspace
- Status
- Owner
- Date range
- Goal summary
- Readiness indicator
- Primary next action
- Share/client visibility
- More menu

### 9.2 Persistent summary strip

- Objective
- Audience
- Offer/message
- Channels
- Budget metadata
- Next milestone
- Active blockers
- Approval state
- Scheduled publication count

The strip collapses but remains accessible.

### 9.3 Tabs

```text
Overview
Plan
Content
Assets
Calendar
Approvals
Publishing
Performance
Activity
Settings
```

Tabs share campaign state and should not cause full-page context loss.

### 9.4 Overview

- Readiness breakdown
- Milestone timeline
- Current blockers
- Work summary by stage
- Required deliverables
- Upcoming approvals
- Upcoming publications
- Goal/performance summary
- Recent decisions

### 9.5 Readiness model UX

Display:

- Overall readiness label
- Required categories
- Completed/total requirements
- Blocking requirements
- At-risk requirements
- Owners
- Due dates
- Evidence links

Example categories:

- Brief complete
- Audience/offer approved
- Required deliverables created
- Assets approved
- Channel variants approved
- Accounts connected
- Publication validation passed
- Schedule confirmed

A user can click readiness to open a drawer with all criteria and explanations.

### 9.6 Plan tab

Switchable list, board, and timeline.

A work-item row/card shows:

- Type icon
- Title
- Status
- Assignee
- Due date
- Priority
- Dependency/blocker state
- Approval/content association
- Comment count
- Compact custom fields

Detail opens in a right-side drawer by default so the user does not lose the campaign view.

### 9.7 Content tab

Displays content by:

- Deliverable
- Status
- Channel
- Content pillar
- Owner
- Publication date

Each content row shows master item and channel-variant progress.

Example:

```text
Product launch announcement
  LinkedIn    Approved · scheduled Jul 21 09:00
  Instagram   Changes requested
  X           Draft
```

### 9.8 Performance tab

- Goal progress
- Publishing and engagement overview
- Content leaderboard
- Channel comparison with definition caveats
- Operational timeline overlay
- Recommendations converted into work

## 10. Work hub

### My Work

Sections:

- Today
- Overdue
- Upcoming
- Waiting on others
- Reviews requested
- Recently completed

### Team Work

- Workload by person/team
- Due-date risk
- Unassigned work
- Blocked work
- Approval bottlenecks

### Work item detail

Header:

- Type
- Title
- Status
- Assignee
- Due date
- Priority
- Campaign/brand context

Body:

- Description
- Checklist/subtasks
- Dependencies
- Custom fields
- Linked content/assets/publications
- Comments
- Activity

Right rail:

- Owner/team
- Dates
- Status history
- Estimate
- Campaign milestone
- Client visibility
- Automation effects

### Keyboard

- `c` create work item
- `e` edit selected item
- `a` assign
- `s` change status
- `d` change due date
- `m` comment/mention
- `[` and `]` previous/next item
- `Esc` close drawer

Shortcuts are disabled while typing and are discoverable through `?`.

## 11. Marketing Calendar

### Layers

- Campaign ranges
- Milestones
- Work-item due dates
- Approval deadlines
- Scheduled publications
- Events and launches

Users can toggle layers independently.

### Views

- Month
- Week
- Agenda
- Campaign timeline
- Channel schedule

### Calendar behavior

- Drag to reschedule when permitted
- Show local time plus account/workspace time-zone indicator
- Conflict warning for ambiguous daylight-saving times
- Visual distinction between due date and publish time
- Multi-select and batch move with preview
- Unscheduled tray for approved content
- Publication status visible after scheduled time

### Event card hierarchy

- Type and status
- Short title
- Brand/account
- Time
- Owner
- Blocker/approval indicator

No card should rely on color alone.

## 12. Content hub

### Views

- Table
- Gallery
- Status board
- Calendar
- By campaign
- By content pillar

### Content inventory columns

- Title
- Campaign
- Brand
- Type
- Owner
- Master status
- Variant completion
- Approval state
- Next publication
- Performance signal
- Updated time

### Quick actions

- Open Studio
- Duplicate
- Repurpose
- Create variant
- Request approval
- Add to schedule
- Archive

## 13. Content Studio — flagship creative surface

### 13.1 Layout

```text
┌────────────────┬──────────────────────────────┬──────────────────┐
│ master/variant │ editor + asset composition   │ context/checks   │
│ navigator      │                              │ preview/details  │
└────────────────┴──────────────────────────────┴──────────────────┘
```

All panes are resizable. The editor can enter focus mode.

### 13.2 Left navigator

- Master content
- Channel variants
- Locale variants
- Version history
- Publication state per variant

Status and unresolved issues appear as compact indicators.

### 13.3 Main editor

Master fields:

- Working title
- Core message
- Long-form source copy
- Audience
- Offer/CTA
- Links
- Notes

Variant fields change by platform capability:

- Post text/caption
- Headline/title
- Media
- Alt text
- First comment
- Hashtags
- Link
- Audience/privacy
- Thumbnail/cover
- Additional provider-specific settings

### 13.4 Context rail

Tabs:

- Preview
- Checks
- Brand
- Campaign
- Approval
- Versions
- AI

### 13.5 Platform previews

Previews are honest approximations, not claims of pixel-perfect platform rendering.

Each preview labels:

- Account
- Platform
- Format
- Truncation/overflow risk
- Unsupported preview elements
- Last capability sync

### 13.6 Content checks

Categories:

- Required fields
- Platform limits
- Asset compatibility
- Link validity
- Alt text/accessibility
- Brand terminology
- Required claims
- Prohibited claims
- Approval state
- Account connection
- Schedule conflicts

Results include severity, explanation, affected field, and fix action.

### 13.7 AI interaction

AI is contextual, not a permanent chat sidebar.

Actions near relevant fields:

- Draft from brief
- Create variants
- Shorten
- Expand
- Change tone
- Add CTA
- Repurpose
- Translate
- Suggest alt text
- Check brand alignment

Before generation, the user can review:

- Brand
- Audience
- Campaign objective
- Source content
- Provider/model
- Instruction mode

Generated output appears as a diff/candidate. It never silently replaces approved content.

### 13.8 Autosave and conflicts

- Local edits feel immediate
- Saving state is subtle but visible
- Version number protects against overwrite
- Concurrent edits show presence
- On conflict, show field-aware comparison with keep mine, keep theirs, or merge
- Approved content changes show approval invalidation warning before commit

## 14. Asset library and review

### Asset library

- Gallery and table views
- Large visual search field
- Filters by brand, campaign, type, owner, rights, expiration, approval, locale, and tags
- Collections are optional views, not the only organization method
- Duplicate indication
- Drag upload and bulk metadata

### Asset detail

- Large preview
- Version rail
- Metadata
- Usage relationships
- Rights and expiration
- Approval state
- Download variants
- Comments/activity

### Visual review

- Point and region annotations
- Timestamp comments for video
- Version comparison
- Resolve/reopen threads
- Reviewer identity and time
- Approved version clearly distinguished

## 15. Approval Inbox

### Goal

Allow a reviewer to process many decisions quickly without losing context.

### Layout

```text
queue list | review canvas / platform preview | decision context
```

Queue filters:

- Assigned to me
- Due soon
- Overdue
- Client
- Brand
- Campaign
- Content type
- Approval step

### Review canvas

- Copy and asset preview
- Platform variants
- Change history since last review
- Brand/policy checks
- Campaign brief and goal
- Internal notes hidden from external reviewers

### Decision bar

- Approve
- Request changes
- Reject when policy allows
- Delegate
- Add comment

Request changes requires a reason or comment. Approval shows exactly what version is being approved.

### Keyboard

- `j/k` next/previous request
- `a` approve
- `r` request changes
- `c` comment
- `v` toggle version comparison
- `p` toggle platform preview

Approval shortcut always requires a deliberate confirmation gesture when risk policy demands it.

## 16. Publishing Center

### Views

- Scheduled
- Publishing now
- Published
- Failed
- Connection issues
- Calendar

### Publication row

- Content/variant
- Campaign
- Account
- Scheduled time/time zone
- Approval status
- Validation status
- Current state
- Attempt count
- Published URL
- Owner

### Failure experience

Failure drawer includes:

- Human-readable summary
- Provider error category
- Affected fields/media/account
- Whether remote success is uncertain
- Recommended remediation
- Retry eligibility
- Last attempt timeline
- Raw provider reference for admins

Never display “Something went wrong” as the only guidance.

### Multi-channel group

Display each destination separately with aggregate state:

- 3 published
- 1 failed
- 1 awaiting reauthorization

Users can retry only failed destinations.

## 17. Social Inbox

P0/P1 scope depends on provider capabilities.

### Columns

- Queue list
- Conversation thread
- Profile/context rail

### Queue labels

- Unassigned
- Assigned to me
- Needs response
- Waiting
- Escalated
- Resolved
- Spam/ignored

### Context rail

- Network/account
- Campaign or publication source
- Contact/profile fields returned by provider
- Reply policy
- Previous interactions
- Related work items

### Reply composer

- Provider-aware limits
- Saved replies
- AI drafting with review
- Approval-required indicator
- Assign/escalate
- Create work item

## 18. Analytics

### Design rules

- Explain metric definitions
- Do not combine unlike provider metrics without disclosure
- Show data freshness
- Preserve comparison context
- Let every chart open underlying content/campaign records
- Separate operational efficiency from audience performance

### Campaign analytics

- Goal progress
- Publication timeline
- Reach/impressions where available
- Engagement and clicks where available
- Top content
- Channel breakdown
- Content-pillar breakdown
- Operational bottlenecks
- Approval and production time

### Operations analytics

- Request-to-launch time
- Work completion by phase
- Approval latency
- Rework rate
- Workload
- On-time publication
- Publication failures
- Account health
- Automation reliability

### AI insight UX

AI summaries must:

- Cite underlying metrics and date ranges
- Label inference versus fact
- State missing data
- Offer actions such as “create experiment” or “create follow-up task”
- Never invent causal attribution

## 19. Automation Builder

### Layout

- Trigger block
- Condition groups
- Action sequence
- Test panel
- Run history

### Builder behavior

- Plain-language summaries alongside structured configuration
- Field and event pickers constrained to current scope
- Validation before publish
- Estimated affected objects from test sample
- Dry run without side effects
- Version comparison
- Draft/published badge
- Disable and rollback

### Example summary

> When a channel variant becomes Approved and has a scheduled time, validate the destination account. If validation passes, mark the linked work item Done. If validation fails, assign the social manager and send a high-priority notification.

### Safety UX

- Loop detection
- Duplicate action warning
- Publish action badge for external side effects
- Maximum execution limits
- Preview of permissions used
- Test data clearly separated from production data

## 20. Brand workspace

Sections:

- Overview
- Identity
- Voice and writing rules
- Audiences
- Products/offers
- Content pillars
- Claims and compliance
- Channels/accounts
- Assets
- Approval policy
- Templates
- AI context

Brand voice should be structured:

- Traits
- Examples
- Do
- Avoid
- Preferred terms
- Prohibited terms
- Channel-specific guidance
- Locale guidance

Do not represent brand voice as one unbounded text box.

## 21. External client experience

The client portal must feel intentional rather than like a restricted internal app.

Client can see:

- Shared campaign summary
- Milestones selected for sharing
- Content/asset approval requests
- Decision history
- Published content and selected performance
- Comments intended for client

Client cannot see:

- Internal notes
- Unshared tasks
- Staff workload
- Internal cost/budget detail
- Other clients/brands
- Automation internals
- Provider credentials
- Private AI prompts or drafts

Client navigation is simplified to:

```text
Campaigns
Approvals
Calendar
Reports
```

## 22. Settings and governance

### Team & access

- Members
- Teams
- Roles
- Workspace access
- Brand access
- External reviewers
- Service accounts
- Access audit

### Integrations

- Social accounts
- AI providers
- Email
- Webhooks
- Storage/design/collaboration integrations later

Each connection shows:

- Status
- Scope
- Owner
- Last successful use
- Token/authorization expiry when known
- Required action
- Audit history

### Security & audit

- Session/security events
- Audit explorer
- Data retention
- Export/deletion requests
- Support-access grants
- Provider data-use settings

## 23. State design

Every major screen must design and test:

- First-run empty
- Filtered empty
- Loading
- Partial loading
- Optimistic update
- Saved
- Offline/reconnecting
- Permission denied
- Deleted or archived parent
- Stale data
- Concurrent edit
- Validation error
- Provider unavailable
- Account authorization expired
- Quota/rate limit reached
- Background job queued/running
- Partial success
- Retry exhausted
- Read-only external view

## 24. Error-writing standard

Every user-facing error answers:

1. What happened?
2. What is affected?
3. Was any external action completed?
4. What can the user do next?
5. Is retry safe?
6. Where can an admin find technical details?

Example:

> Instagram publishing did not start because the connected account no longer grants media-publishing permission. The LinkedIn post was published successfully. Reconnect the Instagram account, then retry only that destination.

## 25. Accessibility

Target WCAG 2.2 AA.

Requirements:

- Complete keyboard operation for critical workflows
- Logical focus order
- Visible focus ring
- Skip links
- Semantic headings and landmarks
- Screen-reader names for icon controls
- Live announcements for save, publish, approval, and background-status changes
- No color-only meaning
- Minimum contrast
- Text zoom to 200 percent without loss of function
- Reduced-motion mode
- Large target sizes for touch contexts
- Accessible tables with headers and row actions
- Calendar alternatives such as agenda/table view
- Chart summaries and underlying data tables
- Annotation alternatives for users unable to use pointer precision

## 26. Localization and time zones

- Locale-aware date, number, currency, and time formatting
- IANA time zones displayed explicitly at scheduling boundaries
- Organization/workspace/account/user defaults
- Longer translated strings supported
- Right-to-left readiness in layout primitives
- Variant and asset locale visible
- Week-start preference
- 12/24-hour preference
- Daylight-saving ambiguity dialog

## 27. Performance targets

Measured on representative data, not empty demos.

- App shell interactive quickly on a standard business laptop
- Navigation feedback under 100 ms
- Common local interactions under 100 ms perceived latency
- Table scroll at smooth frame rate with thousands of records through virtualization
- Campaign Room tab transition without full reinitialization
- Content editor keystrokes never blocked by network save
- Calendar supports hundreds of visible events without jank
- Asset thumbnails lazy-loaded and size-bounded
- Heavy charts loaded after operational content
- Large exports and AI/media operations always asynchronous

Performance regressions block release for flagship surfaces.

## 28. Responsive priorities

### Desktop-first

Full capability:

- Campaign planning
- Timeline
- Content Studio
- Asset review
- Automation Builder
- Analytics exploration

### Tablet

Supported:

- Campaign overview
- Board/list/calendar
- Content review
- Approvals
- Basic editing
- Scheduling

### Mobile

Optimized:

- Notifications
- My Work
- Comments
- Approval decisions
- Publication status
- Simple rescheduling
- Campaign health

Do not squeeze desktop multi-pane editors into unusable mobile layouts.

## 29. Dark mode

Dark mode is a complete semantic theme, not a color inversion.

- Preserve status hierarchy
- Avoid pure black large surfaces
- Rebalance borders and elevation
- Verify asset previews on checker/neutral backgrounds
- Ensure provider logos remain readable
- Test charts and annotations

Dark mode may follow after the light production theme but design tokens must support it from the start.

## 30. Design-quality process

Before implementation:

- Information architecture review
- Low-fidelity critical journeys
- High-fidelity design of flagship surfaces
- Component state matrix
- Accessibility review
- Prototype usability study

During implementation:

- Storybook or equivalent component catalog
- Screenshot/visual regression tests
- Realistic seed data
- Responsive review
- Keyboard test
- Screen-reader smoke test
- Performance profiling

Before pilot:

- Five representative-user usability sessions across campaign manager, content/social contributor, approver, agency admin, and external client
- Critical journey completion without facilitator intervention
- Measured time for approval and scheduling workflows
- Defect remediation

## 31. UX acceptance scenarios

### Campaign manager

Can create a campaign from template, understand generated work, identify blockers, adjust dependencies, and know what must happen before launch without consulting a spreadsheet.

### Content contributor

Can open one content item, understand brand and campaign context, produce channel variants, attach the correct asset version, resolve checks, and request approval without switching tools.

### Approver

Can process an approval from inbox, see exactly what changed and where it will publish, comment, request changes, and approve the correct version using keyboard or pointer.

### Social manager

Can view all upcoming publications, detect account or validation issues before scheduled time, publish, recover partial failure, and verify published URLs.

### External client

Can review only shared work, make a decision, and understand campaign status without seeing internal operations.

## 32. Final design release gate

The first paid pilot is not ready until:

- Campaign Room, Content Studio, Approval Inbox, Calendar, and Publishing Center have complete designs and implemented states.
- The app is visibly original and coherent across every surface.
- A user can trace campaign → work → content → variant → approval → publication → metric.
- High-risk actions show consequences and safe recovery.
- External client permissions are obvious and tested.
- Keyboard workflows pass.
- Accessibility checks pass for critical journeys.
- Realistic high-density data remains usable and performant.
- Empty, loading, partial, failure, permission, stale, and conflict states are implemented rather than delegated to framework defaults.
- No placeholder UI, lorem ipsum, fake charts, or misleading provider previews remain in pilot paths.