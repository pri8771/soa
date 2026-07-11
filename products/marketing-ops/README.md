# Marketing Operations Platform

> **Working name:** Marketing Ops
>
> **Status:** product and implementation foundation
>
> **Product type:** multi-tenant B2B SaaS

Marketing Ops is a unified campaign operations and social publishing platform for in-house marketing teams, agencies, and multi-brand organizations.

It combines the planning depth of project-management software with the execution depth of social-media management software. A campaign should move through one connected system from brief, plan, task, asset, and approval to scheduled channel variants, publication, engagement, and performance analysis.

## Product promise

> Plan the campaign, produce the work, approve the content, publish every channel, and understand the result without stitching together five disconnected tools.

The product should be inspired by the speed, clarity, and polish users expect from modern work-management products, while remaining an original clean-room design rather than a visual or functional clone of any one competitor.

## Core operating model

```text
Organization
└── Workspace / Client
    └── Brand
        └── Campaign
            ├── Brief and objectives
            ├── Work items, milestones, dependencies, and owners
            ├── Assets and versions
            ├── Content items
            │   └── Channel variants
            ├── Approvals
            ├── Scheduled publications
            ├── Engagement
            └── Performance
```

The central product object is the **Campaign Room**: one persistent workspace that connects strategy, execution, content, approvals, publishing, and measurement.

## Why this product should exist

Marketing teams commonly split work across project management, documents, spreadsheets, asset storage, chat, approval tools, social schedulers, and analytics dashboards. That separation creates duplicated data, lost context, unclear ownership, stale calendars, late approvals, manual status updates, and weak links between work completed and business results.

Marketing Ops treats project work and distribution as one graph:

```text
Goal
→ Campaign
→ Deliverable
→ Work item
→ Asset
→ Content item
→ Channel variant
→ Approval
→ Publication
→ Metric
→ Follow-up action
```

This graph enables automations that generic project tools and isolated social schedulers cannot perform safely.

## Flagship experiences

1. **Campaign Room** — the complete command center for one campaign.
2. **Launch Board** — a dependency-aware view of everything needed before content can publish.
3. **Content Studio** — one master content item with native variants and previews for each channel.
4. **Marketing Calendar** — campaigns, milestones, tasks, approvals, and posts on one calendar.
5. **Approval Inbox** — fast review for internal stakeholders and external clients.
6. **Automation Builder** — deterministic rules that connect campaign states to operational actions.
7. **Performance Loop** — converts publication results into recommendations and actionable work.

## Initial production slice

```text
Create organization
→ Create a workspace and brand
→ Connect or mock a social account
→ Create a campaign from a template
→ Complete a brief
→ Generate tasks and milestones
→ Create a content item
→ Produce LinkedIn and Instagram variants
→ Attach an asset
→ Request and receive approval
→ Schedule publications
→ Publish through a provider adapter
→ Capture publication status and metrics
→ Inspect the campaign timeline and audit trail
```

## Product principles

1. **One source of campaign truth.** Work and publishing are never separate records with loose links.
2. **Platform-native content.** Each network receives a deliberate variant rather than blindly copied text.
3. **Human control over consequential automation.** AI drafts and suggests; users approve brand and publishing decisions.
4. **Beautiful under real workload.** The interface must remain fast and coherent with many brands, campaigns, tasks, assets, approvals, and posts.
5. **Context beside action.** Goals, owners, dependencies, brand rules, approval state, and publication readiness remain visible.
6. **Automation is explainable.** Every rule execution shows its trigger, conditions, actions, result, and rollback path.
7. **Provider independence.** Social networks and AI providers sit behind stable internal contracts.
8. **Local-first development.** The complete product journey runs locally with mock social providers and local AI.
9. **Free-first, paid-ready.** Development may use free allowances, but architecture must not depend on them.
10. **Enterprise foundations from day one.** Tenant isolation, roles, auditability, encrypted credentials, and reliable background work are not deferred.

## Canonical documentation

- [`docs/PRODUCT.md`](docs/PRODUCT.md) — product definition, personas, requirements, workflows, and release scope
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — modules, data model, tenancy, events, jobs, APIs, and local architecture
- [`docs/UI_UX_BLUEPRINT.md`](docs/UI_UX_BLUEPRINT.md) — visual system, screen specifications, interaction behavior, accessibility, and design gates
- [`docs/SOCIAL_AUTOMATION.md`](docs/SOCIAL_AUTOMATION.md) — account connections, capability matrix, scheduling, publishing, analytics, and provider rules
- [`docs/AI_STRATEGY.md`](docs/AI_STRATEGY.md) — local LLM, Gemini, future OpenAI/Anthropic adapters, brand context, prompts, safety, and evaluation
- [`docs/SECURITY_OPERATIONS.md`](docs/SECURITY_OPERATIONS.md) — security, privacy, reliability, compliance, runbooks, and production gates
- [`docs/DELIVERY_PLAN.md`](docs/DELIVERY_PLAN.md) — start-to-finish implementation sequence
- [`docs/BUILD_BACKLOG.md`](docs/BUILD_BACKLOG.md) — dependency-ordered tasks for Codex or engineers
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — accepted and deferred architectural decisions
- [`AGENTS.md`](AGENTS.md) — coding-agent execution contract for this folder

## Repository isolation

This product lives entirely under `products/marketing-ops/`. Its application code, packages, infrastructure, migrations, tests, and documentation must remain inside that boundary unless a shared repository package is explicitly approved later.

Planned structure:

```text
products/marketing-ops/
├── apps/
│   ├── web/
│   ├── api/
│   └── worker/
├── packages/
│   ├── ui/
│   ├── contracts/
│   ├── domain/
│   ├── social-connectors/
│   ├── ai/
│   └── test-fixtures/
├── infrastructure/
│   ├── local/
│   └── cloud/
├── migrations/
├── tests/
└── docs/
```

## Current phase

**Phase 0 — product lock, architecture, design contract, and executable backlog.**

The folder does not yet represent a completed application. Production readiness may only be claimed after the functional, usability, security, provider, reliability, and operational gates in these documents are met.