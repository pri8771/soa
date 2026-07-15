# UI/UX Blueprint

> **Design objective:** make complex document operations feel precise, fast, calm, and trustworthy.
>
> **Primary UX outcome:** a trained reviewer should process exceptions faster and with fewer errors than manual order entry, while an administrator should understand exactly how a stream is configured and why a document was routed or held.

This is the visual and interaction contract for SOA. It defines the application structure, screens, components, states, keyboard behavior, accessibility rules, performance targets, and design-quality gates that implementation must satisfy.

It is a target/acceptance contract, not a statement that every screen or human
quality gate has passed. Current implementation and release evidence live in
[`PRODUCTION_READINESS_AUDIT.md`](PRODUCTION_READINESS_AUDIT.md).

---

## 1. Design mandate

SOA must look and behave like premium operations software designed around documents, evidence, and consequential decisions. It must not look like:

- A generic admin template.
- A marketing landing page inside an application shell.
- A collection of unrelated cards.
- A chat interface with document upload attached.
- A copy of a component-library demonstration.
- A neon-gradient “AI product.”
- A low-density consumer app that hides operational context.

The visual tone is **confident, restrained, modern, and human**. The interface communicates that the system handles important business data without feeling bureaucratic or dated.

### 1.1 Product-design principles

1. **Evidence beside decisions.** Users do not navigate away to understand where a value came from.
2. **Exceptions, not noise.** High-confidence valid work recedes; uncertainty and risk are unmistakable.
3. **Fast by keyboard, clear by mouse.** Power users receive a complete keyboard workflow without sacrificing discoverability.
4. **Dense, not cramped.** Operational screens fit real information while preserving hierarchy and legibility.
5. **Explain the system.** Confidence, validation, matching, routing, and failure states state their reasons.
6. **Preserve context.** Organization, workspace, process, stream, document, and task context remain visible.
7. **Progressive disclosure.** Common actions are immediate; advanced details are available without overwhelming new users.
8. **Reversible where possible.** Users can undo, reopen, replay, compare, or restore rather than fear every action.
9. **No dead ends.** Errors offer a next action, owner, or support reference.
10. **Accessibility is structural.** Semantics, focus, contrast, keyboard behavior, motion, and screen-reader output are designed from the start.

---

## 2. Users and priority jobs

### 2.1 Reviewer

Primary jobs:

- Understand why a document needs review.
- Navigate directly to questionable fields.
- Verify a value against visible evidence.
- Correct header fields and line items.
- Resolve catalog candidates.
- Understand validation errors.
- Approve or escalate with confidence.

**Design implication:** Review Studio receives the greatest design, prototyping, and usability-testing investment.

### 2.2 Supervisor

Primary jobs:

- See backlog, SLA risk, throughput, accuracy, and blockers.
- Assign or rebalance work.
- Inspect difficult documents.
- Approve high-value or escalated orders.
- Identify recurring causes of manual work.

**Design implication:** queues, saved views, operational metrics, and drill-down are first-class.

### 2.3 Process or stream administrator

Primary jobs:

- Configure a stream without rebuilding its parent process.
- Understand inherited versus overridden configuration.
- Test changes against historical documents.
- Publish safely.
- Roll back.

**Design implication:** editors make inheritance, versioning, impact, validation, and publication status explicit.

### 2.4 Integration administrator

Primary jobs:

- Map canonical fields.
- Test connections and payloads.
- Inspect failures.
- Retry or replay safely.
- Rotate credentials.

**Design implication:** integration status and delivery history are understandable without backend access.

### 2.5 Organization administrator and auditor

Primary jobs:

- Manage people, permissions, retention, security, usage, and provider policy.
- Review an immutable timeline.
- Export evidence.

**Design implication:** governance screens prioritize scope, consequence, history, and plain-language explanations.

---

## 3. Information architecture

### 3.1 Primary navigation

Desktop navigation order:

1. **Overview**
2. **Documents**
3. **Review**
4. **Processes**
5. **Streams**
6. **Catalogs**
7. **Integrations**
8. **Analytics**
9. **Settings**

Internal platform operators receive a separately permissioned **Operations** area. It must never appear to customer users.

### 3.2 Global context

The application shell always exposes:

- Current organization.
- Current workspace when applicable.
- Current process or stream when the page is scoped.
- Global search and command palette.
- Notifications and assigned work.
- Help and documentation.
- User menu.

Changing organization is deliberate and visually obvious. The interface must never allow a user to believe they are editing one organization while operating on another.

### 3.3 Route model

```text
/login
/select-organization
/app/:organizationSlug/overview
/app/:organizationSlug/documents
/app/:organizationSlug/documents/:documentId
/app/:organizationSlug/review
/app/:organizationSlug/review/:taskId
/app/:organizationSlug/processes
/app/:organizationSlug/processes/:processId
/app/:organizationSlug/processes/:processId/versions/:versionId
/app/:organizationSlug/streams
/app/:organizationSlug/streams/:streamId
/app/:organizationSlug/streams/:streamId/configuration
/app/:organizationSlug/streams/:streamId/simulations/:simulationId
/app/:organizationSlug/catalogs
/app/:organizationSlug/catalogs/:catalogId
/app/:organizationSlug/integrations
/app/:organizationSlug/integrations/:integrationId
/app/:organizationSlug/analytics/operations
/app/:organizationSlug/analytics/quality
/app/:organizationSlug/analytics/cost
/app/:organizationSlug/settings/*
```

### 3.4 App-shell layout

At widths of 1280 px and above:

- Left navigation: 248 px expanded, 72 px collapsed.
- Top context bar: 56 px.
- Page header: 64–88 px depending on actions and breadcrumbs.
- Main content: fluid, maximum width only on forms and narrative pages; queues and Review Studio use the full viewport.
- Right utility panel: optional 360–440 px drawer.

At widths from 1024–1279 px:

- Navigation defaults collapsed.
- Secondary panels become overlays or tabs.
- Review Studio preserves document and form side-by-side where possible.

Below 1024 px:

- Administrative tasks remain supported.
- Review is functional but uses tabbed document/data views.
- Mobile is intended for status, lightweight approvals, and incident awareness—not dense line-item editing.

---

## 4. Visual system

### 4.1 Typography

Use a variable sans-serif optimized for UI, preferably Inter Variable or an equivalent open-source family. Use tabular numerals for quantities, prices, totals, dates, queue age, and analytics.

Type scale:

| Token | Size/line-height | Weight | Use |
|---|---:|---:|---|
| `display-sm` | 30/38 | 650 | major setup or empty-state title only |
| `heading-xl` | 24/32 | 650 | page title |
| `heading-lg` | 20/28 | 650 | major section |
| `heading-md` | 16/24 | 650 | card/panel title |
| `body-lg` | 15/24 | 450 | explanatory copy |
| `body-md` | 14/20 | 450 | primary application text |
| `body-sm` | 13/18 | 450 | dense tables and metadata |
| `label` | 12/16 | 600 | field labels and column headers |
| `caption` | 11/16 | 500 | timestamps, helper metadata |

Rules:

- Sentence case, not title case, for labels and buttons.
- Avoid all caps except very short identifiers.
- Truncation always provides a full-value disclosure route.
- Long IDs use monospace only when users need to compare or copy them.

### 4.2 Color tokens

Colors are semantic tokens, never hard-coded in feature components.

Light theme foundation:

```text
canvas                 #F6F7F9
surface                #FFFFFF
surface-subtle         #F0F2F5
surface-raised         #FFFFFF
border                 #D8DDE5
border-strong          #B8C0CC
text-primary           #172033
text-secondary         #556176
text-muted             #7B8799
accent                 #3157D5
accent-hover           #2849B8
accent-soft            #E9EEFF
focus                   #4C74FF
success                #147A55
success-soft           #E6F6EF
warning                #9A6200
warning-soft           #FFF3D5
critical               #B4233A
critical-soft          #FDE9ED
info                    #166AA3
info-soft               #E6F3FC
```

Dark theme uses a neutral blue-black foundation rather than pure black. Maintain equivalent semantic contrast; do not simply invert values.

Confidence must not be encoded only by red/yellow/green. Use icon, label, and numeric/range detail where appropriate.

### 4.3 Spacing

Base unit: 4 px.

Common scale:

```text
4, 8, 12, 16, 20, 24, 32, 40, 48, 64
```

- Dense table rows: 36–40 px.
- Comfortable table rows: 44–48 px.
- Inputs: 36 px compact, 40 px default, 44 px touch-oriented.
- Primary page gutters: 24 px desktop, 16 px tablet.
- Review Studio panel gutters: 12–16 px.

### 4.4 Shape and elevation

- Radius 6 px for controls and compact surfaces.
- Radius 8 px for panels and menus.
- Radius 12 px for large dialogs and onboarding surfaces.
- Avoid excessive pill shapes; reserve pills for status, filters, tags, and compact segmented controls.
- Use borders and tonal separation before shadows.
- Shadows are subtle and limited to overlays, menus, dialogs, and floating bars.

### 4.5 Icons

Use one consistent line-icon family. Icons supplement labels; they do not replace unfamiliar actions. Every icon-only control has an accessible name and visible tooltip.

### 4.6 Motion

- Typical transition: 120–180 ms.
- Panel transition: up to 220 ms.
- No decorative looping animation.
- Avoid moving content during data refresh.
- Respect reduced-motion settings.
- Processing indicators show meaningful stage progress where possible, not an indefinite spinner.

---

## 5. Design-system component inventory

### 5.1 Foundations

- Typography.
- Color and semantic status.
- Spacing and layout.
- Elevation.
- Icons.
- Motion.
- Focus rings.
- Responsive breakpoints.

### 5.2 Primitives

- Button: primary, secondary, subtle, destructive, icon, split.
- Link.
- Text input, textarea, number, currency, date, date/time.
- Select, combobox, multi-select.
- Checkbox, radio, switch.
- Segmented control.
- Tooltip, popover, menu.
- Dialog, alert dialog, drawer.
- Tabs.
- Badge, status indicator, tag.
- Toast and persistent banner.
- Skeleton, spinner, progress.
- Separator.
- Scroll area.
- Empty state.

### 5.3 Operational composites

- App shell.
- Context switcher.
- Command palette.
- Page header.
- Filter bar.
- Saved-view selector.
- Data table.
- Bulk-action bar.
- Metric tile.
- Trend chart.
- Activity timeline.
- Stage progress.
- SLA indicator.
- Confidence indicator.
- Validation message.
- Catalog-match candidate.
- Evidence reference.
- Configuration inheritance badge.
- Version status and comparison.
- Code/JSON viewer with redaction.
- Connection status card.
- Usage/quota meter.

### 5.4 Document components

- PDF/document viewer.
- Thumbnail rail.
- Page controls.
- Search within document.
- Selection/evidence overlay.
- Evidence legend.
- Field editor.
- Field provenance panel.
- Candidate picker.
- Line-item grid.
- Cell evidence popover.
- Review reason navigator.
- Review completion summary.

Every component must have documented states in Storybook or an equivalent component workbench.

---

## 6. Required screens

## 6.1 Sign-in and organization selection

### Sign-in

- Minimal branded layout.
- Organization domain or SSO entry when configured.
- Email/password only where supported by the identity provider.
- Clear privacy and support links.
- Error states distinguish invalid credentials, suspended organization, expired invitation, SSO requirement, and temporary service failure.

### Organization selection

- Show only accessible organizations.
- Display organization name, role, environment badge when non-production, and last accessed time.
- Search appears only when needed.
- Remember the last selected organization but never skip authorization checks.

## 6.2 Overview / operations command center

Purpose: answer “What needs attention right now?”

Top area:

- Date/time and stream scope.
- Primary actions: upload documents, open review queue.
- Compact health summary.

Required metrics:

- Received today.
- Completed today.
- Review backlog.
- Oldest SLA age.
- Straight-through rate.
- Export failure count.
- Critical-field quality trend.
- Processing cost trend when permitted.

Main sections:

1. **Needs attention** — failed exports, blocked streams, provider outage, critical SLA risk.
2. **Queue health** — review queues by stream and age.
3. **Processing** — current stages, failure rate, queue depth.
4. **Recent exceptions** — documents with concise reasons and owner.
5. **Quality movement** — only statistically meaningful changes; avoid decorative charts.

Behavior:

- Every metric is clickable and opens a filtered operational view.
- Time range and stream filter persist in the URL.
- No giant hero or empty dashboard tiles.

## 6.3 Documents queue

Required columns:

- Document/reference.
- Process and stream.
- Customer.
- PO number when extracted.
- Received time.
- Current state.
- Confidence/risk summary.
- Validation issue count.
- Review owner.
- SLA age/due time.
- Export state.

Required filters:

- Process, stream, source, state, document type, language.
- Customer.
- Received range.
- Confidence/risk.
- Validation category.
- Reviewer.
- SLA.
- Export status.

Features:

- Saved personal and shared views.
- Column chooser and density.
- Sort and server pagination.
- Bulk reprocess, assign, export, and archive only when valid for selected rows.
- Sticky header and first identifier column.
- URL-addressable filters.
- Table virtualizes when needed.

Row behavior:

- Single click selects.
- Enter opens.
- Space toggles selection.
- Context menu exposes permitted actions.
- State and issue cells explain themselves in tooltips/popovers.

## 6.4 Upload/import center

Tabs:

- Upload.
- Email.
- API.
- Batch history.
- Later: SFTP/shared storage.

Upload flow:

1. Select stream first or infer it only through an explicit routing rule.
2. Drag/drop or browse.
3. Validate type, size, count, and duplicates before upload.
4. Show per-file progress.
5. Allow removal/cancel before completion.
6. Show processing links after registration.

Batch result distinguishes uploaded, duplicate, rejected, quarantined, and failed. Do not summarize partial failure as success.

## 6.5 Document detail

Header:

- File/document name.
- PO number/customer when available.
- State.
- Process/stream.
- Received/source.
- Primary valid next action.

Tabs:

- Summary.
- Extracted data.
- Validation.
- Processing.
- Delivery.
- Audit.

Summary includes:

- Page preview.
- Current outcome.
- Important issues.
- Key fields.
- Review/export status.
- Exact configuration versions used.

Processing uses a vertical stage timeline with attempt details, latency, provider, warnings, safe error codes, and retry controls.

## 6.6 Review queue

Views:

- My work.
- Unassigned.
- SLA risk.
- Supervisor review.
- Blocked.
- Quality sample.
- Shared saved views.

Queue cards or rows show:

- Document/customer/PO.
- Stream.
- Reason for review.
- Number of affected critical/important fields.
- SLA.
- Assignee.
- Estimated effort indicator based on affected fields/pages, not arbitrary AI prediction.

“Start next” follows an explicit queue policy and tells the user what policy selected the task.

## 6.7 Review Studio

### Desktop layout

At widths of 1440 px or greater:

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ Context / task header / progress / primary actions                        │
├───────────────┬──────────────────────────────────┬─────────────────────────┤
│ thumbnails    │ document viewer                  │ field/reason panel      │
│ 72–96 px      │ flexible, normally 52–60%        │ 380–520 px              │
├───────────────┴──────────────────────────────────┴─────────────────────────┤
│ optional expandable line-item grid / issue tray                           │
└────────────────────────────────────────────────────────────────────────────┘
```

Panels are resizable with sensible minimums and persisted per user.

### Header

Contains:

- Back to queue.
- Task position or remaining count.
- Document/customer/PO.
- Stream and SLA.
- Assignment state.
- Review completion indicator.
- Save status.
- More actions.
- Reject/escalate and approve actions.

### Review reason navigator

The right panel begins with a compact list of unresolved reasons ordered by risk:

- Critical low confidence.
- Failed validation.
- Ambiguous catalog match.
- Missing required field.
- Model disagreement.
- Quality-control sample.

Users can switch to “all fields.” The active reason is announced to assistive technology and linked to the corresponding field and evidence.

### Field editor

Each field presents:

- Label and criticality.
- Editable normalized value.
- Raw value when different.
- Confidence label and detail.
- Validation state.
- Catalog match state.
- Evidence source.
- History indicator.

Selecting the field highlights its source. Hover may preview evidence, but click and keyboard selection are required alternatives.

### Provenance drawer/popover

Shows:

- Source page and quote.
- Coordinate/evidence certainty.
- Extraction method/provider/model.
- Schema and instruction version.
- Candidate values.
- Validation outcomes.
- Catalog match feature scores.
- Previous correction history.

Do not expose opaque chain-of-thought. Show inspectable evidence and system decisions.

### Line-item grid

Requirements:

- Spreadsheet-like keyboard movement.
- Frozen line number, item identifier, quantity, and issue columns where space permits.
- Row virtualization.
- Multi-line descriptions.
- Cell-level evidence.
- Add/remove/split/merge row with undo.
- Bulk apply UOM or delivery date when safe.
- Candidate material picker.
- Totals reconciliation footer.
- Clear indication of continuation-page rows.
- Copy/paste support with validation.
- No horizontal-scroll trap; provide column presets and focused cell panel.

### Save and concurrency

- Changes autosave after a short idle delay and on field exit.
- Explicit save state: saved, saving, offline/retrying, conflict.
- Optimistic concurrency prevents silent overwrite.
- Conflict UI shows the current server value, the user's value, editor, and safe resolution choices.

### Approval

Approval is disabled only with a visible reason. Approval summary shows:

- Remaining warnings.
- Overrides made.
- Critical fields accepted.
- Export destination.
- Whether a second approval is required.

Do not use a surprise confirmation dialog when the primary screen already provides a complete approval summary. Use a dialog only for consequential exceptions or policy acknowledgment.

### Required keyboard map

| Key | Action |
|---|---|
| `J` / `K` | next/previous review reason |
| `Tab` / `Shift+Tab` | next/previous editable field |
| `Enter` | edit/commit current field |
| `Esc` | cancel edit or close overlay |
| `E` | focus evidence/source |
| `M` | open match candidates |
| `C` | add comment |
| `A` | open approval summary when eligible |
| `R` | open reject/escalate menu |
| `[` / `]` | previous/next page |
| `+` / `-` | zoom document |
| `?` | show keyboard guide |

Shortcuts must avoid typing contexts, be configurable where practical, and be listed in-product.

## 6.8 Process browser and process detail

Process list shows:

- Name.
- Purpose/document family.
- Active streams.
- Published version.
- Draft state.
- Recent quality signal.
- Owner.

Process detail tabs:

- Overview.
- Schema.
- Workflow.
- Rules.
- Provider policy.
- Confidence.
- Test sets.
- Versions.

Inheritance is visualized as “process default” versus stream override—not by duplicating every value.

## 6.9 Stream detail and configuration

Header shows organization/workspace/process/stream hierarchy.

Overview includes:

- Health.
- Intake endpoints.
- Current published version.
- Queue and SLA.
- Provider route.
- Catalogs.
- Export destination.
- Recent changes.

Configuration editor:

- Persistent left section navigation.
- “Inherited,” “overridden,” and “not configured” labels.
- Reset-to-parent action.
- Unsaved-change indicator.
- Validation summary.
- Draft/test/publish actions.
- Impact summary listing affected documents, users, and integrations.

## 6.10 Schema builder

Three-pane model:

1. Field tree.
2. Field configuration.
3. Sample/test preview.

Field configuration includes:

- Path and label.
- Type.
- Description and examples.
- Required/criticality.
- Normalization.
- Aliases.
- Evidence requirement.
- Validation references.
- Table/nesting behavior.

Support drag reorder where order matters, but provide keyboard reorder and explicit hierarchy controls.

## 6.11 Rule builder

Use an inspectable expression builder:

```text
WHEN [field] [operator] [value]
AND  [condition]
THEN [error/warning/review/derived value]
```

Required features:

- Natural-language draft helper may propose a rule.
- Generated deterministic expression is always visible.
- Type-aware operands.
- Test cases with input and expected outcome.
- Rule priority and stop/continue behavior.
- Change comparison.
- No publication with syntax/type errors.

## 6.12 Simulation and version comparison

Show candidate versus current production:

- Documents tested.
- Critical-field improvements/regressions.
- Review-rate estimate.
- False-auto-approval changes.
- Cost and latency changes.
- New failures by cohort.

Users can inspect exact changed documents and fields. Averages must never hide critical regressions.

## 6.13 Catalog manager

Catalog list shows type, source, active version, record count, freshness, and streams using it.

Import flow:

1. Upload CSV/XLSX.
2. Map columns.
3. Validate types and required values.
4. Review duplicates and invalid rows.
5. Preview changes: added/updated/deactivated.
6. Activate a version.

Record view supports search, filters, effective dates, aliases, source IDs, and change history.

## 6.14 Integrations studio

Integration detail tabs:

- Connection.
- Mapping.
- Test.
- Deliveries.
- Credentials.
- Audit.

Mapping interface:

- Canonical fields on left.
- Target fields on right.
- Transform expression in center or drawer.
- Required target-field validation.
- Sample payload preview.
- Versioned mapping.

Delivery list shows business idempotency key, payload version, attempts, response class, latency, and available retry/replay action.

Secrets are never displayed after creation; show last rotated, creator, scope, and rotate/revoke actions.

## 6.15 Analytics

Separate operational and quality intent.

### Operations

- Throughput.
- Backlog and SLA.
- Processing time.
- Review time.
- Export success.
- Provider availability.

### Quality

- Critical-field accuracy.
- Field and line-item accuracy.
- Correction rate.
- False-auto-approval.
- Confidence calibration.
- Performance by stream/customer/layout/provider/version.

### Cost

- Cost per page/document/accepted order.
- Native versus OCR path.
- Provider and fallback distribution.
- Budget and quota usage.

Chart rules:

- Every chart has a clear decision purpose.
- Tables accompany charts when precise values matter.
- Axes and denominators are explicit.
- Empty or statistically weak cohorts are labeled.
- Color remains consistent with semantic roles.

## 6.16 Settings and governance

Sections:

- Organization profile.
- Workspaces.
- Users and invitations.
- Roles and permissions.
- Authentication and SSO readiness.
- API/service credentials.
- Provider credentials and data-use policy.
- Retention and deletion.
- Audit and exports.
- Usage and billing.
- Notifications.

Changes with broad impact include a scope preview and audit reason.

## 6.17 Internal support console

Strictly internal and permissioned. Required capabilities:

- Tenant lookup.
- Document transaction timeline.
- Queue/provider health.
- Safe metadata and error codes.
- Time-limited support session request.
- Feature flags and quotas.
- Replay controls according to policy.

Support users cannot view original documents or extracted values unless an explicit, approved, time-limited access grant exists and is audited.

---

## 7. State and feedback contract

Every feature must design and implement:

1. Initial loading.
2. Background refresh.
3. Empty state.
4. No search/filter results.
5. Permission denied.
6. Feature unavailable by plan/policy.
7. Partial data.
8. Retryable failure.
9. Terminal failure.
10. Offline or connectivity loss where relevant.
11. Optimistic success.
12. Conflict.
13. Long-running operation.
14. Success with warnings.

### 7.1 Empty states

An empty state explains:

- What belongs here.
- Why it may be empty.
- The one most useful next action.
- A secondary documentation link only when needed.

Avoid decorative illustrations that consume operational space.

### 7.2 Errors

Error copy follows:

```text
What happened
What was preserved or not changed
What the user can do now
Correlation/reference ID when support may be needed
```

Example:

> Export failed because the destination rejected customer code `GB-1042`. Your approved order is محفوظ and has not been changed. Update the mapping or customer record, then replay delivery. Reference: EXP-7H2K.

Use localized wording in implementation; never include stack traces or raw provider messages.

### 7.3 Long-running operations

- Immediately acknowledge the action.
- Provide a stable activity or document link.
- Show meaningful stage and last update time.
- Permit navigation away.
- Notify on completion/failure according to preferences.
- Never block the entire app with a spinner for document processing.

---

## 8. Accessibility contract

Target WCAG 2.2 AA.

Required:

- Semantic landmarks, headings, forms, tables, dialogs, and live regions.
- Complete keyboard operation.
- Visible focus with at least 2 px equivalent contrast.
- Logical focus order.
- Focus restoration after dialogs/drawers.
- Minimum 4.5:1 normal-text contrast and 3:1 large text/essential UI graphics.
- Status conveyed by text/icon, not color alone.
- Accessible names for icon buttons.
- Error summary linked to invalid fields.
- Screen-reader announcements for autosave, task navigation, evidence selection, conflicts, and stage changes.
- Reduced-motion support.
- Zoom to 200% without loss of critical function.
- Touch targets at least 44 px in touch-oriented layouts; dense desktop controls may be smaller only with adequate spacing and keyboard access.
- Document overlays have a non-visual evidence description.

Manual accessibility review must cover login, queue, upload, Review Studio, approval, process configuration, and integration failure recovery.

---

## 9. Performance experience targets

Measured on a representative production-like environment after warm-up:

- App shell interactive: target under 2.5 seconds at p75 on a typical business laptop/network.
- Navigation feedback: under 100 ms perceived response.
- Queue filter feedback: immediate local indication; server results target under 1 second p75.
- Review field navigation: under 50 ms.
- Page change after render cache: under 200 ms.
- Initial document page display: target under 1.5 seconds for ordinary documents.
- Autosave confirmation: target under 750 ms p75.
- Tables scroll smoothly with realistic row counts.
- No route should download the full document list or full-resolution pages unnecessarily.

Use skeletons only when they match final layout. Avoid spinner flashes for operations below 300 ms.

---

## 10. Responsive strategy

### Desktop, 1440–1920+

Primary target for Review Studio and operational work. Use space to keep evidence and data visible together; do not stretch form text to unreadable widths.

### Compact desktop, 1024–1439

- Collapsed nav.
- Narrower field panel.
- Line grid can switch between bottom panel and dedicated tab.
- Preserve reviewer keyboard flow.

### Tablet, 768–1023

- App shell uses overlay nav.
- Review uses document/data tabs with persistent issue navigator.
- Approval and lightweight correction are supported.
- Dense schema/mapping editing may display a recommendation to use a larger screen without blocking read-only access.

### Mobile, below 768

Supported jobs:

- View overview/alerts.
- Inspect document status.
- Approve simple supervisor tasks when policy permits.
- Comment or reassign.

Do not attempt to reproduce the full line-item workbench on a phone.

---

## 11. Design and implementation workflow

### 11.1 Required design artifacts before implementation

For each major screen:

- User goal and success condition.
- Information hierarchy.
- High-fidelity default state.
- Loading, empty, error, permission, and large-data states.
- Keyboard behavior.
- Responsive behavior.
- Accessibility notes.
- Component inventory.
- API/data requirements.

### 11.2 Prototype order

1. App shell and navigation.
2. Documents queue.
3. Review queue.
4. Review Studio header-fields flow.
5. Review Studio line-item flow.
6. Approval and conflict recovery.
7. Stream configuration and inheritance.
8. Simulation comparison.
9. Catalog import/matching.
10. Integration mapping and failure recovery.

### 11.3 Usability studies

Before paid pilot, complete moderated tests with users approximating reviewers, supervisors, and admins.

Core reviewer test:

- Open assigned task.
- Identify why it was routed.
- Verify and correct PO number.
- Resolve ship-to candidate.
- Correct a line-item quantity.
- Understand totals validation.
- Approve.

Measure:

- Completion rate.
- Time on task.
- Errors and backtracks.
- Mouse versus keyboard use.
- Confidence in approval.
- Ability to explain why the system selected a value.
- Comparison to baseline manual entry.

A critical usability failure blocks release just like a critical code defect.

---

## 12. Visual and UX quality assurance

### 12.1 Storybook or component workbench

Required stories:

- Every primitive and composite.
- Light and dark themes.
- Keyboard focus.
- Long labels and localization expansion.
- Error and disabled states.
- High-density and comfortable table modes.
- Permission variants.
- Loading/skeleton states.

### 12.2 Visual regression

Capture stable snapshots for:

- Login.
- App shell.
- Overview.
- Documents queue.
- Empty and error states.
- Upload.
- Document detail.
- Review queue.
- Review Studio header and line-item modes.
- Schema/rule builder.
- Stream inheritance editor.
- Catalog import.
- Integration mapping.
- Analytics.
- Settings.

### 12.3 Design review checklist

A screen cannot be marked complete until:

- Hierarchy is clear within five seconds.
- Primary action is obvious and valid for the current state.
- Organization/stream context is visible.
- Terminology matches the product model.
- Content uses realistic lengths and volumes.
- All states are designed.
- Keyboard and focus behavior are specified.
- Contrast and semantics pass.
- Layout does not shift unexpectedly.
- Error recovery is clear.
- No stock-template patterns undermine the product's identity.

---

## 13. UX analytics and product signals

Instrument privacy-safe events for:

- Queue view and filter use.
- Task claim/release.
- Review reason navigation.
- Field correction and reason category.
- Candidate selection.
- Keyboard shortcut use.
- Time per affected field and document.
- Conflict occurrence.
- Approval/rejection/block/reopen.
- Configuration validation and publication failure.
- Integration test and replay.

Do not record raw document text or sensitive field values in analytics.

Use signals to improve workflow, not to create punitive reviewer surveillance. Customer-facing reviewer productivity reporting must be transparent and contextualized by document complexity.

---

## 14. UI acceptance scenarios

### Scenario A — first-time admin

An admin can identify the organization, create a stream from a process, understand inherited settings, configure an intake endpoint, test a sample PO, see problems, and publish without documentation outside the application.

### Scenario B — experienced reviewer

A reviewer can claim the next task, understand why it needs review, navigate every affected field by keyboard, see evidence, correct values and line items, resolve catalog matches, and approve without losing context.

### Scenario C — supervisor

A supervisor can identify SLA risk, rebalance assignments, inspect a difficult document, understand why automation stopped, and require a second approval.

### Scenario D — integration failure

An integration admin can identify the failed target and reason, verify the exact payload/mapping version, correct configuration, test it, and replay the approved order without re-running extraction.

### Scenario E — configuration regression

A process admin can compare a candidate version with production, identify a critical-field regression, inspect affected documents, reject publication, and preserve production behavior.

---

## 15. Non-negotiable design release gate

SOA does not ship to a paid pilot merely because the backend works. The release requires:

- A coherent custom design system.
- High-fidelity implementation of all P0 screens.
- Successful reviewer and admin usability tests.
- Complete keyboard operation for Review Studio.
- WCAG 2.2 AA automated and manual checks on critical flows.
- Visual-regression coverage.
- Realistic data-density and performance tests.
- No unresolved critical UX issues.

The product's UI is part of its competitive advantage and must be treated as production infrastructure, not post-MVP polish.
