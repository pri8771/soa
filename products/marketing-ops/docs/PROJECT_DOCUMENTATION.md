# Marketing Operations Platform — Project Documentation

> **Status:** Phase 0 implementation contract
>
> **Repository boundary:** `products/marketing-ops/`
>
> **Source of truth:** the documents linked below and implementation code in this subtree

## Product summary

Marketing Operations Platform is a multi-tenant B2B application that connects campaign project management, content and asset production, internal/client approvals, social scheduling and publishing, engagement, and performance in one campaign graph.

The initial product promise is:

> Plan the campaign, produce the work, approve the content, publish every channel, and understand the result without stitching together five disconnected tools.

The flagship product object is the **Campaign Room**, which connects:

```text
strategy
→ milestones and work
→ assets and content
→ channel variants
→ exact-version approval
→ scheduling and publication
→ metrics
→ follow-up action
```

## Canonical documents

Read in this order:

1. [`../README.md`](../README.md) — product orientation, operating model, and repository boundary.
2. [`PRODUCT.md`](PRODUCT.md) — personas, workflows, requirements, release scope, and success metrics.
3. [`DELIVERY_PLAN.md`](DELIVERY_PLAN.md) — start-to-finish delivery sequence and production definition.
4. [`BUILD_BACKLOG.md`](BUILD_BACKLOG.md) — task IDs, dependencies, deliverables, acceptance criteria, tests, and Codex execution order.
5. [`ARCHITECTURE.md`](ARCHITECTURE.md) — stack, modules, data model, APIs, jobs, events, tenancy, and local/hosted composition.
6. [`UI_UX_BLUEPRINT.md`](UI_UX_BLUEPRINT.md) — information architecture, design system, flagship screen behavior, states, accessibility, performance, and visual-quality gates.
7. [`SOCIAL_AUTOMATION.md`](SOCIAL_AUTOMATION.md) — account connections, provider capabilities, media, scheduling, publishing, reconciliation, metrics, and engagement.
8. [`AI_STRATEGY.md`](AI_STRATEGY.md) — mock/local LLM, Gemini, future OpenAI/Anthropic adapters, context, candidates, safety, and evaluation.
9. [`SECURITY_OPERATIONS.md`](SECURITY_OPERATIONS.md) — privacy, security, reliability, observability, runbooks, and production gates.
10. [`DECISIONS.md`](DECISIONS.md) — accepted architecture choices and intentionally deferred decisions.
11. [`../AGENTS.md`](../AGENTS.md) — mandatory rules for Codex and other coding agents.

Do not create overlapping PRDs or alternative architecture documents. Update the canonical document in the same PR as the behavior it governs.

## Fixed implementation direction

Unless superseded by an accepted decision:

- TypeScript end to end.
- Product-local `pnpm` workspace.
- React server-capable web application.
- Fastify API and a separately runnable worker.
- PostgreSQL canonical relational store.
- S3-compatible object storage with MinIO locally.
- PostgreSQL-backed durable jobs and transactional outbox.
- Modular monolith with provider-neutral boundaries.
- Organization → workspace/client → brand → campaign hierarchy.
- Mock social provider for local and E2E operation.
- Local AI through an Ollama-compatible adapter.
- Gemini as the first hosted AI adapter.
- OpenAI and Anthropic adapters after the provider contract and evaluation suite are stable.
- Exact content/asset versions for approvals and publications.
- Idempotent, reconcilable publishing with explicit unknown state.
- No Kubernetes for P0 without an accepted architecture decision.

## UX quality contract

The product must not look like a generic admin template, a marketing site inside an application shell, a collection of unrelated cards, or a chat interface with project management attached.

The required visual and interaction qualities are:

- Calm, premium, contemporary, and information-dense.
- Clear organization, brand, campaign, approval, and publication context.
- Keyboard-complete frequent workflows.
- Resizable panes, persistent context, realistic tables, and deliberate empty/error states.
- Evidence and readiness explanations beside consequential actions.
- High-confidence automation that remains inspectable and reversible.
- WCAG 2.2 AA target.
- Visual regression and role-based usability acceptance before release.

The highest-priority design surfaces are:

1. Operations Command Center.
2. Campaign portfolio.
3. Campaign Room.
4. Work views and work-item drawer.
5. Content Studio.
6. Approval Inbox and client review canvas.
7. Marketing Calendar.
8. Publishing Center and failure recovery.
9. Automation Builder.
10. Campaign/content performance.

## First complete local journey

```text
sign in
→ create/switch organization, workspace, and brand
→ create campaign from template
→ complete and unblock work
→ create one content item with two channel variants
→ attach an exact asset version
→ request internal and external approval
→ approve the exact review snapshot
→ schedule to two mock accounts
→ simulate one successful post and one timeout after remote acceptance
→ reconcile without duplication
→ inspect calendar, Publishing Center, metrics, activity, and audit
```

This journey must run locally without real social credentials or paid AI services.

## Current implementation state

- Product definition: documented.
- Architecture: documented.
- UI/UX contract: documented.
- Social provider contract: documented.
- AI strategy: documented.
- Security/operations gates: documented.
- Dependency-ordered backlog: documented.
- Executable application code: not started on this branch.
- Production readiness: not claimed.

## Next engineering action

Begin with `FND-001` in [`BUILD_BACKLOG.md`](BUILD_BACKLOG.md). Implement one task or tightly coupled dependency group per focused PR. At the end of each task, the product must still install, start locally, migrate from an empty database, seed, build, and pass relevant tests.

## Completion statement

The product may be called complete for its initial commercial scope only after a real organization can plan and execute a campaign, collaborate securely, approve exact versions, publish reliably to required real providers, recover partial and ambiguous failures, measure outcomes, audit actions, restore data, and fulfill privacy obligations through a visually excellent and accessible product.
