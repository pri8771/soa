# SOA — Intelligent Document Operations Platform

SOA is a multi-tenant B2B platform that converts incoming business documents into validated, traceable, system-ready data. The first commercial workflow is **sales-order automation from customer purchase orders**; the platform architecture is designed to support invoices, remittances, claims, shipping documents, and additional document processes later.

## Product promise

> Turn incoming purchase orders into validated, ERP-ready sales orders—with every value traceable to the original document.

SOA is not an OCR upload utility. The current launch path combines document
intake, native text/OCR, schema-constrained extraction, business validation,
master-data matching, human review, approval, and reliable delivery into
downstream systems. Classification/packet splitting is a later evaluated
capability; the first release enforces one sales order per input.

## Core hierarchy

```text
Organization
└── Workspace / Business Unit
    └── Process
        └── Stream
            ├── Input sources
            ├── Document types and schemas
            ├── Provider-routing policy
            ├── Validation rules and reference data
            ├── Review queues and SLA
            └── Output integrations
```

A stream is an active operational bucket such as `UK Sales Orders`, `Spain Sales Orders`, `Pittsburgh Invoices`, or `North America Remittances`. Streams inherit process configuration and may override language, fields, models, rules, catalogs, permissions, thresholds, retention, and integrations.

## Current status

The repository contains a broad working vertical slice, but it is **not yet
approved for production customer documents**. Runtime remediation is active;
the remaining release blockers and verified gates are tracked in
[`docs/PRODUCTION_READINESS_AUDIT.md`](docs/PRODUCTION_READINESS_AUDIT.md).

## Getting started

Prerequisites: Node 22 (`.nvmrc`), pnpm 10, Python 3.11
(`.python-version`), [uv](https://docs.astral.sh/uv/), PostgreSQL, and GNU
Make. Docker is optional for local supporting services.

```bash
make bootstrap       # install toolchains/dependencies and copy env template
make local-up        # start PostgreSQL, MinIO, and local supporting services
make migrate         # apply database migrations
make seed            # idempotently load the Northstar demo tenant
make dev             # supervise web, API, and durable worker
make lint            # ruff + eslint + prettier checks
make typecheck       # mypy + tsc
make test            # fast unit and component tests
make test-e2e        # portable browser accessibility/functional gate
make test-visual     # pinned-Linux pixel regression gate
make eval            # offline deterministic evaluation smoke
make build           # build all apps
make help            # list every command
```

The full command contract is defined in
[`docs/DELIVERY_PLAN.md`](docs/DELIVERY_PLAN.md) §7.1.

## Canonical implementation documentation

The documentation set is intentionally small and authoritative:

- [`docs/PRODUCT.md`](docs/PRODUCT.md) — product scope, users, workflows, requirements, and release boundaries
- [`docs/DELIVERY_PLAN.md`](docs/DELIVERY_PLAN.md) — master start-to-finish implementation and production-readiness plan
- [`docs/BUILD_BACKLOG.md`](docs/BUILD_BACKLOG.md) — dependency-ordered tasks, acceptance criteria, required tests, and release gates
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design, modules, entities, tenancy, and processing lifecycle
- [`docs/UI_UX_BLUEPRINT.md`](docs/UI_UX_BLUEPRINT.md) — premium design system, screens, Review Studio, interaction, accessibility, and UX gates
- [`docs/AI_OCR.md`](docs/AI_OCR.md) — OCR/LLM pipeline, provider abstraction, evidence, confidence, and evaluation
- [`docs/SECURITY_OPERATIONS.md`](docs/SECURITY_OPERATIONS.md) — security baseline, quality gates, reliability, and operational readiness
- [`docs/INFRASTRUCTURE.md`](docs/INFRASTRUCTURE.md) — local/free-first stack and explicit paid/provider upgrade paths
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — accepted architecture decisions and explicitly deferred provider choices
- [`AGENTS.md`](AGENTS.md) — mandatory execution rules for Codex and other coding agents

## Initial production slice

```text
Create organization
→ Create purchase-order process
→ Create a country/location stream
→ Upload or email a PO
→ Extract header and line items
→ Match customer, ship-to, and materials
→ Validate critical values
→ Review uncertain values with source evidence
→ Approve
→ Export canonical JSON / deliver to an integration
→ Inspect the immutable audit timeline
```

## Engineering principles

1. Tenant isolation is enforced server-side and tested as a security invariant.
2. Every extracted value retains page, bounding box when genuinely available, source text, method, model, configuration version, and correction history.
3. OCR and LLM providers sit behind stable interfaces; no workflow depends directly on one vendor.
4. Long-running processing is asynchronous, durable, idempotent, retryable, observable, and replayable.
5. LLM output is schema-constrained and never trusted without deterministic normalization and validation.
6. Human corrections become evaluation data; they never silently retrain or alter production behavior.
7. Free-tier infrastructure may limit capacity, but must not create disposable architecture.
8. The Review Studio and evidence-first UX are production requirements, not post-MVP polish.

## Building with Codex

Codex should implement one backlog task or tightly coupled dependency group at a time. Before coding, it must read `AGENTS.md` and the canonical documents relevant to the task. Every change must include tests, tenant/security analysis, user-facing states, telemetry where needed, and rollback notes.

Recommended execution order begins at `FND-001` in [`docs/BUILD_BACKLOG.md`](docs/BUILD_BACKLOG.md). Do not ask an agent to implement the entire platform in one unreviewable change.

## Source of truth

Product research and planning also exist in the Notion **Sales Order Automation — Restart Hub**. Repository documentation is the implementation-facing contract; Notion remains the research, backlog-discussion, and project-planning workspace. Requirements should not be duplicated into new documents when an authoritative repository section already exists.

## Release rule

No production customer documents should be processed until the accuracy,
security, privacy, reliability, provider-contract, backup, usability, and
release requirements in the
[production-readiness audit](docs/PRODUCTION_READINESS_AUDIT.md) are satisfied
with environment evidence.
