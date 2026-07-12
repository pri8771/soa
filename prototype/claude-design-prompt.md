Build a high-fidelity, clickable prototype of "SOA" — a B2B intelligent document operations platform that converts customer purchase orders into validated, ERP-ready sales orders. This is a design prototype only: no real backend, use realistic mock/seeded data throughout. Build it as a single React app (Tailwind CSS utility classes) with client-side view-state navigation (a simple screen switcher is fine — no real router needed) so every screen is reachable and clickable.

This prompt supersedes any earlier version — the navigation model below is "skills-first" (similar in spirit to ABBYY Vantage), not the admin-hierarchy nav.

## Product context

North-star: "Turn incoming purchase orders into validated, ERP-ready sales orders — with every value traceable to its source."

A "Skill" is a configured document-extraction workflow for one document type + scope (e.g., "PO Extraction — UK", "PO Extraction — Germany", "PO Extraction — Acme Corp EMEA"). Internally this is a Process/Stream, but the user-facing mental model is: pick a skill, see its documents, see how it's performing, teach it when it gets something wrong.

Core workflow: receive PO (upload/email/API) → extract header + line items with evidence → match customer/ship-to/materials against catalogs → validate → route uncertain fields to human review → approve → export canonical JSON / deliver via webhook → immutable audit timeline.

Primary users: Reviewer (processes documents day to day), Supervisor/Skill Admin (configures skills, monitors quality, publishes learned templates).

## Design mandate (do not deviate)

Tone: confident, restrained, modern, operational software for consequential business data — NOT a generic admin template, NOT a marketing-style dashboard, NOT a neon-gradient "AI product," NOT a chat UI with a file uploader bolted on.

Principles:
- Evidence sits beside decisions — never make the user navigate away to see where a value came from.
- High-confidence/valid work recedes visually; uncertainty and risk are unmistakable.
- Dense but not cramped — real operational information density.
- Every state explains itself (confidence, validation, routing, errors all state their reason).
- No dead ends — every error/empty state offers a next action.
- Nothing the system "learns" is applied silently — corrections become a proposed, versioned update that a human explicitly reviews and publishes (see "Teach this field" flow below). This is a hard product rule, not a nice-to-have: never auto-apply a learned template without an explicit publish action.

## Design tokens (use exactly these)

Typography: variable sans-serif (Inter or equivalent). Tabular numerals for quantities/prices/totals/dates.

Type scale:
- display-sm: 30/38, weight 650 — empty-state/major title only
- heading-xl: 24/32, weight 650 — page title
- heading-lg: 20/28, weight 650 — section
- heading-md: 16/24, weight 650 — card/panel title
- body-lg: 15/24, weight 450
- body-md: 14/20, weight 450 — primary text
- body-sm: 13/18, weight 450 — dense tables
- label: 12/16, weight 600 — field labels, column headers
- caption: 11/16, weight 500 — timestamps, metadata

Sentence case everywhere. Avoid all-caps except short IDs.

Color tokens (light theme as CSS variables / Tailwind config; support a dark-mode toggle with an equivalent neutral blue-black palette, not pure black):
```
canvas          #F6F7F9
surface         #FFFFFF
surface-subtle  #F0F2F5
surface-raised  #FFFFFF
border          #D8DDE5
border-strong   #B8C0CC
text-primary    #172033
text-secondary  #556176
text-muted      #7B8799
accent          #3157D5
accent-hover    #2849B8
accent-soft     #E9EEFF
focus           #4C74FF
success         #147A55
success-soft    #E6F6EF
warning         #9A6200
warning-soft    #FFF3D5
critical        #B4233A
critical-soft   #FDE9ED
info            #166AA3
info-soft       #E6F3FC
```
Never encode confidence/risk with color alone — pair with icon + label/numeric value.

Spacing: 4px base unit, scale 4/8/12/16/20/24/32/40/48/64. Dense table rows 36–40px, comfortable rows 44–48px. Page gutters 24px desktop.

Shape: 6px radius controls, 8px panels/menus, 12px large dialogs. Borders/tonal separation before shadows; shadows subtle, reserved for overlays/menus/dialogs.

## Navigation model

### Sign-in
Minimal branded layout, org domain/SSO entry, email/password fields, error states for invalid credentials / suspended org / expired invite / SSO required (toggle-able demo states).

### Home = Skills list (the landing page after login)
Grid or list of skill cards. Each card shows:
- Skill name and a short description (e.g., "PO Extraction — UK Sales Orders")
- Document type icon/badge
- Documents processed (last 30 days) and current backlog count
- Straight-through processing rate (with trend arrow)
- Status: Active / Draft / Needs attention (e.g., a sender's template awaiting publish, or an SLA breach)
- Last activity timestamp
Primary action per card: "Open." Secondary: "+ Create skill" (opens a lightweight setup flow: name, document type, sample documents upload, initial schema suggestion — can be a shallow 2–3 step mock, doesn't need full schema builder depth).
A skill card with zero documents shows an empty/setup state instead of stats.

### Skill workspace (opened from a skill card)
Persistent header: skill name, status badge, back to Skills, org/workspace context.
Tabs:
1. **Documents** (default tab)
2. **Analytics**
3. **Senders & templates** (the "teach this field" memory — see below)
4. **Training documents** — ONLY shown if the skill has gold/test documents configured; otherwise this tab is absent (don't show an empty tab for skills with no training set). When present: list of labeled reference documents used to validate the skill, each showing whether current extraction still matches expected ground truth (pass/regressed), with a "run evaluation" action and a simple pass-rate summary.
5. **Configuration** — secondary/collapsed by default (schema fields, validation rules, provider routing). Keep this lighter-weight than a full schema builder; a reasonable field list + basic rule list is enough for the prototype.

#### Tab: Documents
Same requirements as a standard document queue, scoped to this skill:
Columns: document/reference, sender/customer, PO number, received time, state, confidence/risk summary, validation issue count, review owner, SLA age, export state.
Filters: state, sender, received range, confidence/risk, validation category, reviewer, SLA, export status.
Saved views, column chooser, bulk actions where valid, sticky header, 30+ realistic mock rows with pagination and a genuine mix of states (approved, needs review, failed export, quarantined).
Row click → Document detail / Review Studio (see below).

#### Tab: Analytics
Scoped to this skill/bucket only (not org-wide). Three sub-sections:
- **Operations:** throughput, backlog and SLA, processing time, review time, export success rate.
- **Quality:** critical-field accuracy, field/line-item accuracy, correction rate, false-auto-approval rate, confidence calibration, performance broken down by sender (this is where you'd see "Acme Corp documents have 98% accuracy since template was published, but Beta Inc is still at 81%").
- **Cost:** cost per document/accepted order, native-text vs. OCR path split, provider distribution.
Every chart paired with a precise-value table. Label weak/empty cohorts explicitly. No decorative charts without a stated decision purpose.

#### Tab: Senders & templates ("teach this field" memory)
This is the ABBYY-style learning surface, built as an explicit, inspectable, human-approved mechanism (never silent).
List of senders/customers seen by this skill. Each sender row expands to show:
- Sender name/identifier (email domain, customer ID, or detected layout fingerprint)
- Documents received from this sender
- Learned field anchors: for each field, whether a layout hint exists (e.g., "PO date — top-right region, near label 'Order Date:'"), its status (**Proposed** / **Published** / **Rejected**), the correction(s) it was derived from (link to source document), and a confidence/match-rate stat since publishing.
- For a **Proposed** anchor: show a side-by-side of "before" (original extraction) and "after" (corrected value + captured region) with **Publish** and **Reject** actions. Publishing requires the same draft→review→publish pattern as the rest of the product — show a brief confirmation summarizing what will change for future documents from this sender.
- Once published, subsequent mock documents from that sender in the Documents tab should visibly reflect higher confidence / pre-filled values for that field (you can represent this by having 2–3 "later" documents from the same sender show green/high-confidence status on that field, with a small "matched sender template" badge).

### Document detail
Header: filename, PO number/customer, state, skill, source, primary next action.
Tabs: Summary, Extracted data, Validation, Processing, Delivery, Audit.
Summary: page preview, outcome, key issues, key fields, review/export status, exact configuration/template versions used (including which sender-template version, if any, informed this extraction).
Processing tab: vertical stage timeline (received → validating → queued → preprocessing → classifying → splitting → extracting → normalizing → validating_data → review_required/approved → exporting → completed) with attempt details, latency, provider, safe error codes, retry controls.

### Review Studio (opened from a document needing review)
Three-pane layout at desktop width: thumbnails rail (72–96px) | document viewer (52–60% width, showing a realistic mock PO with visible text) | field/reason panel (380–520px). Optional bottom line-item grid panel.
Header: back to queue, task position ("4 of 12"), document/customer/PO, skill+SLA, assignment state, save status, approve + reject/escalate actions.
Right panel top: review reason navigator (critical low confidence, failed validation, ambiguous catalog match, missing required field, model disagreement, QC sample), toggle to "all fields."
**Field-click behavior (core interaction):** clicking a field in the right panel highlights ONLY that field's bounding region on the document viewer (a single highlighted box, not all fields at once); clicking a highlighted region on the document does the reverse and focuses that field in the panel.
Field editor per field: label + criticality badge, editable normalized value, raw value when different, confidence label, validation state, catalog-match state, evidence source link, correction-history indicator.
**"Teach this field" flow:** when a reviewer edits a field's value, after save show an inline, dismissible prompt: *"Save this position as a template hint for future [Sender name] documents?"* with **Yes, propose it** / **Not now**. Choosing yes does NOT change live behavior — it creates a **Proposed** entry visible in the skill's Senders & templates tab, awaiting a supervisor's publish decision. Make this rule visible in the UI copy so it's clear nothing changes silently.
Provenance popover (click "evidence"): source page+quote, coordinate certainty, extraction method/provider/model, schema+instruction version, candidate values, validation outcomes, catalog match feature scores (e.g., company name similarity 20%, address 25%, city 15%, postal code 30%, country 10%), correction history, and — when applicable — "matched sender template: [name], published [date]."
Line-item grid: spreadsheet-like, frozen line#/item/qty/issue columns, multi-line descriptions, cell-level evidence on click, add/remove row, candidate material picker, totals reconciliation footer, continuation-page row indicator.
Save state indicator: saved / saving / offline-retrying / conflict (show a conflict-resolution mock: server value vs. your value with a resolution choice).
Approval summary inline on the panel (not a surprise modal): remaining warnings, overrides made, critical fields accepted, export destination, second-approval requirement.
Keyboard legend accessible via "?": J/K reason nav, Tab field nav, E evidence, M match candidates, C comment, A approval, R reject, [ ] page nav, +/- zoom.

### Upload / import (reachable from within a skill's Documents tab)
Tabs: Upload, Email, API, Batch history. Flow: drag/drop or browse (skill already selected by context) → per-file validation (type/size/duplicates) → per-file progress → batch result screen distinguishing uploaded / duplicate / rejected / quarantined / failed (never summarize partial failure as success).

### Secondary admin area (not primary nav — reachable via a small "Manage" link/icon in the top bar, for supervisors/admins)
Keep this lightweight in the prototype — a simple sub-nav is enough, don't need full depth here:
- **Catalogs** — list + import flow (upload CSV/XLSX → map columns → validate → preview changes → activate version).
- **Integrations** — connection/mapping/deliveries/credentials tabs; canonical fields ↔ target fields mapping UI; delivery history with retry/replay.
- **Org analytics** — cross-skill rollup of the same Operations/Quality/Cost views.
- **Settings** — organization profile, users & roles, authentication, API credentials, retention & deletion, audit exports, notifications.

## States every major screen (Documents tab, Review Studio, Upload, Senders & templates) must demonstrate via a visible toggle or realistic default mix
Loading/skeleton, empty state (explain what belongs here + one next action), no-results-from-filter, permission-denied, partial data, retryable failure, terminal failure, conflict, long-running operation (non-blocking), success-with-warnings.

Error copy pattern: "What happened / what was preserved / what you can do now / correlation ID." Example: "Export failed because the destination rejected customer code GB-1042. Your approved order has not been changed. Update the mapping or customer record, then replay delivery. Reference: EXP-7H2K."

## Data realism
Populate every table/list with plausible business data — real company names, PO numbers, SKUs, dates, currencies, addresses — not "Lorem ipsum" or "Item 1/Item 2." Vary confidence levels, SLA states, sender-template status, and validation issues across rows so the UI demonstrates its full range of states, not just the happy path. Include at least 2–3 skills (e.g., "PO Extraction — UK," "PO Extraction — Germany," one in "Draft" status with no documents yet) and at least one sender with a **Published** template and one with a **Proposed** (not yet approved) template, so the teach-and-publish flow is visible without extra clicks to "seed" it.

## Deliverable
A single interactive React artifact where every nav item and every in-context link (Skills → skill workspace → Documents/Analytics/Senders tabs → document → Review Studio → field click → teach-this-field → Senders & templates proposed entry) actually navigates and reflects state changes. Build order priority: Sign-in → Skills list → Skill workspace/Documents tab → Review Studio (most detail, including field-click highlight and teach-this-field) → Senders & templates tab → Analytics tab → everything else. If you must trim scope, keep those six at full fidelity and simplify the secondary admin area rather than dropping it entirely.
