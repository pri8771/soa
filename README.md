# SOA — Intelligent Document Operations Platform

SOA is a multi-tenant B2B platform that converts incoming business documents into validated, traceable, system-ready data. The first commercial workflow is **sales-order automation from customer purchase orders**; the platform architecture is designed to support invoices, remittances, claims, shipping documents, and additional document processes later.

## Product promise

> Turn incoming purchase orders into validated, ERP-ready sales orders—with every value traceable to the original document.

SOA is not an OCR upload utility. It combines document intake, native text extraction, OCR, classification, packet splitting, schema-constrained LLM extraction, business validation, master-data matching, human review, approval, and reliable delivery into downstream systems.

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

## Phase 0 status

The repository is being established around a **free-first, paid-ready** architecture. Development and a controlled demonstration should run mostly within free allowances, while production capabilities remain portable to paid plans without redesigning the application.

The canonical documentation set is intentionally small:

- [`docs/PRODUCT.md`](docs/PRODUCT.md) — product scope, users, workflows, requirements, and release boundaries
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design, modules, entities, tenancy, and processing lifecycle
- [`docs/AI_OCR.md`](docs/AI_OCR.md) — OCR/LLM pipeline, provider abstraction, evidence, confidence, and evaluation
- [`docs/SECURITY_OPERATIONS.md`](docs/SECURITY_OPERATIONS.md) — security baseline, quality gates, reliability, and operational readiness
- [`docs/INFRASTRUCTURE.md`](docs/INFRASTRUCTURE.md) — free-first stack and explicit upgrade paths
- [`docs/DELIVERY_PLAN.md`](docs/DELIVERY_PLAN.md) — vertical-slice plan, backlog order, and production gates
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — architectural decisions and unresolved decisions

## Initial production slice

```text
Create organization
→ Create purchase-order process
→ Create a country/location stream
→ Upload or email a PO
→ Extract header and line items
→ Match customer, ship-to, and materials
→ Validate critical values
→ Review uncertain fields with source evidence
→ Approve
→ Export canonical JSON / deliver to an integration
→ Inspect the immutable audit timeline
```

## Engineering principles

1. Tenant isolation is enforced server-side and tested as a security invariant.
2. Every extracted value retains page, bounding box, source text, method, model, configuration version, and correction history.
3. OCR and LLM providers sit behind stable interfaces; no workflow depends directly on one vendor.
4. Long-running processing is asynchronous, idempotent, retryable, observable, and replayable.
5. LLM output is schema-constrained and never trusted without deterministic normalization and validation.
6. Human corrections become evaluation data; they never silently retrain or alter production behavior.
7. Free-tier infrastructure may limit capacity, but must not create disposable architecture.

## Source of truth

Product planning also exists in the Notion **Sales Order Automation — Restart Hub**. Repository documentation is the implementation-facing contract; Notion remains the planning, research, backlog, and discussion workspace.

## Current phase

**Phase 0 — Foundation and product lock**

No production customer documents should be processed until the security, privacy, reliability, provider-contract, backup, and release requirements in this repository are satisfied.
