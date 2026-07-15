# Architecture and Product Decisions

This file records accepted decisions, deferred decisions, and explicit constraints for the Marketing Ops product. A coding agent must not replace an accepted decision because another stack or pattern is personally preferred. Material changes require an ADR or an update to this document in the same pull request.

## Decision status

- **Accepted:** implementation should follow it.
- **Deferred:** architecture must preserve options; do not choose silently.
- **Rejected:** do not implement without a new decision.
- **Superseded:** retained for history but replaced.

---

## D-001 — Separate product boundary

**Status:** Accepted

The marketing product lives under `products/marketing-ops/` and owns its code, migrations, tests, infrastructure, and documentation within that subtree.

### Rationale

- Prevents accidental coupling to Sales Order Automation.
- Allows nested `AGENTS.md` rules.
- Preserves an eventual repository split.
- Makes Codex task scope explicit.

### Consequence

Do not import SOA-specific application packages. Shared repository tooling requires an explicit decision.

---

## D-002 — Original clean-room product

**Status:** Accepted

“Like Ordinal” is treated as a directional benchmark for quality and category, not an instruction to copy proprietary screens, branding, text, assets, or interactions.

### Rationale

The product needs a distinct identity and should compete through its campaign graph, operational integration, and design quality.

---

## D-003 — Product category

**Status:** Accepted

The product is a combined marketing project-management and social-operations platform. Neither half is a plugin or secondary afterthought.

The defining graph is:

```text
campaign → work → asset → content → variant → approval → publication → metric → follow-up
```

---

## D-004 — Campaign Room as flagship

**Status:** Accepted

The Campaign Room is the primary organizing experience. It must connect strategy, plan, content, assets, approvals, publishing, performance, and activity.

### Consequence

Do not build disconnected modules with only foreign keys and separate navigation. Cross-object navigation and summaries are P0.

---

## D-005 — TypeScript end to end

**Status:** Accepted

Use TypeScript for web, API, worker, shared contracts, social adapters, automation, and AI adapters.

### Rationale

- Shared schemas and domain types
- Strong ecosystem for collaborative web applications and social APIs
- Easier agent handoff across layers
- Reduces language-boundary overhead for the first product team

Python services may be introduced later for specialized media/ML work through an accepted ADR.

---

## D-006 — Modular monolith

**Status:** Accepted

Use one product workspace with separately runnable web, API, and worker processes and strongly separated domain modules.

### Rejected for P0

- Dozens of microservices
- Kubernetes as a prerequisite
- Event sourcing as the only persistence model

Services are extracted only for measured scaling, failure, region, security, or ownership boundaries.

---

## D-007 — Core technology choices

**Status:** Accepted

- React server-capable framework for web
- Fastify TypeScript API
- PostgreSQL canonical database
- PostgreSQL-backed durable jobs and transactional outbox
- S3-compatible object storage, MinIO locally
- Optional Redis for ephemeral concerns only
- `pnpm` workspaces
- Playwright for E2E

Pin stable versions during implementation and document upgrades.

---

## D-008 — Provider-neutral connectors

**Status:** Accepted

Social, AI, identity, object storage, email, notification, and telemetry vendors are accessed through internal contracts.

Core domain tables store stable internal fields. Provider-specific fields live in typed metadata or provider artifacts.

---

## D-009 — Local-first primary journey

**Status:** Accepted

The complete P0 journey runs locally using:

- PostgreSQL
- MinIO
- local email catcher
- deterministic mock social provider
- deterministic AI fixtures or Ollama
- local worker and scheduler

No external API key is required for core E2E tests.

---

## D-010 — Free-first, paid-ready

**Status:** Accepted

Development and demos may use free allowances. Production cannot rely on pausable, unsupported, non-commercial, or contractually unsuitable tiers.

Provider and infrastructure migration is an adapter/configuration/data operation, not a product rewrite.

---

## D-011 — AI provider order

**Status:** Accepted

Initial:

1. Mock/deterministic provider
2. Ollama-compatible local provider
3. Gemini

Prepared:

4. OpenAI
5. Anthropic
6. Azure OpenAI or other enterprise routes

AI cannot publish, approve, change permissions, or perform external side effects directly.

---

## D-012 — Social provider rollout

**Status:** Accepted with pilot-dependent order

Implement:

1. Mock provider and contract tests
2. One business-oriented real provider end to end
3. Second provider required by pilot
4. Remaining adapters based on customer value and provider approval

Recommended order is LinkedIn, Meta, X, TikTok, YouTube, then others. Actual P0 providers require pilot confirmation.

---

## D-013 — Platform-managed scheduling

**Status:** Accepted

The platform owns the canonical schedule and durable publication state. A connector may use provider-native scheduling when beneficial, but the platform must reconcile it and retain a consistent state model.

---

## D-014 — Approval snapshot

**Status:** Accepted

An approval decision applies to an immutable snapshot/version of content and assets. Material changes invalidate relevant approvals according to policy.

### Consequence

A mutable “approved=true” field is insufficient.

---

## D-015 — Unknown remote state

**Status:** Accepted

Provider timeout after an outbound request is not automatically a failure. Publications enter `unknown_remote_state` and reconcile before retry to prevent duplicate posts.

---

## D-016 — Dynamic capability matrix

**Status:** Accepted

The UI and validation use a versioned account capability snapshot. Do not hard-code that all accounts or providers support the same fields and formats.

---

## D-017 — Real-time scope

**Status:** Accepted

Use real-time updates selectively for collaboration and operational state. The server/database remains authoritative and clients recover by refetching.

Presence is ephemeral and never business state.

---

## D-018 — Premium custom design system

**Status:** Accepted

Use accessible headless primitives and a token-driven custom design system. A third-party component kit may accelerate primitives but must not define the product’s visual identity.

### Rejected

- Shipping framework defaults as final UI
- Generic admin-template layouts
- Copying competitor styling

---

## D-019 — Desktop-first, mobile-supporting

**Status:** Accepted

Full campaign planning, timelines, Content Studio, automation, and analytics are optimized for desktop. Mobile focuses on notifications, approvals, comments, status, campaign health, and simple scheduling.

---

## D-020 — PostgreSQL search first

**Status:** Accepted

Begin with PostgreSQL full text/trigram search. Introduce a dedicated search engine only after measured need.

---

## D-021 — PostgreSQL analytics first

**Status:** Accepted

Store metric snapshots and initial aggregates in PostgreSQL. Add a warehouse/export pipeline when volume or reporting requirements justify it.

---

## D-022 — Structured automations

**Status:** Accepted

Automations use deterministic trigger/condition/action definitions with versioning, dry run, limits, permission checks, run history, and loop prevention.

An LLM may suggest an automation but cannot publish one.

---

## D-023 — Audit model

**Status:** Accepted

Use append-oriented audit events plus versioned domain records. Do not force the entire application into pure event sourcing.

---

## D-024 — External client model

**Status:** Accepted

External reviewers are real users with narrow permissions and a simplified portal, not public links with broad access by default.

Secure share links may be added later for specific review flows with expiry and policy.

---

## D-025 — Branding/name

**Status:** Deferred

`Marketing Ops` is a descriptive working name and `products/marketing-ops/` is the implementation folder. Final product name, logo, domain, and visual identity will be selected separately.

The design system must support rebranding without code-wide replacements.

---

## D-026 — Identity provider

**Status:** Deferred

Use a generic OIDC/auth boundary. Final managed provider depends on hosting, pricing, SSO roadmap, and customer requirements.

Local development must have a deterministic developer-auth mode that cannot be enabled in production.

---

## D-027 — Initial cloud host

**Status:** Deferred

Do not hard-code cloud-specific services in domain code. Choose deployment vendors after local vertical slice and expected pilot region are known.

---

## D-028 — Billing provider

**Status:** Deferred

Model plans, entitlements, usage, and billing events internally. Select checkout/invoicing provider after packaging and sales motion are defined.

---

## D-029 — Link shortener and tracking

**Status:** Deferred

P0 supports UTM/link metadata. Native short links, redirect domains, click tracking, and associated security/privacy require a later decision.

---

## D-030 — Paid social execution

**Status:** Deferred

Campaign planning may store paid-media metadata in P1, but directly creating or changing advertisements is outside P0 and needs separate provider, permission, budget, and safety decisions.

---

## D-031 — Social listening/scraping

**Status:** Rejected outside authorized APIs

Do not scrape social sites or automate consumer interfaces. Listening/engagement features must use provider-authorized APIs or approved partners.

---

## D-032 — Autonomous AI posting

**Status:** Rejected

AI may create drafts and recommendations. It does not independently approve or publish external content.

---

## D-033 — Production-ready definition

**Status:** Accepted

The product may be called production-ready only when a customer can plan, collaborate, create, approve, schedule, publish, reconcile, measure, audit, restore, export, and delete according to policy—and when security, usability, provider approval, reliability, and operational evidence has been recorded.