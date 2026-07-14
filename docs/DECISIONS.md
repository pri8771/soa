# Architecture and Product Decisions

> **Last updated:** 2026-07-14
>
> **Purpose:** record decisions that implementation agents must treat as constraints, plus unresolved choices that require evidence before selection.

A material change to an accepted decision requires a new or superseding ADR in this file (a standalone `docs/adr/` directory is created when the first ADR outgrows it). A pull request must not silently reverse an accepted decision.

Status values:

- **Accepted** — implementation constraint.
- **Provisional** — proceed with this choice, but validate during the named phase.
- **Deferred** — intentionally not selected yet.
- **Superseded** — retained for history; replacement identified.

---

## ADR-001 — Sell a vertical solution on a horizontal document platform

- **Status:** Accepted
- **Decision:** Build reusable document operations foundations, but make purchase-order-to-sales-order automation the first complete and commercially positioned solution.
- **Rationale:** A narrow outcome gives clear users, schema, validation, integrations, ROI, and quality metrics. The shared foundations preserve expansion to invoices and other document processes.
- **Consequences:** P0 backlog prioritizes sales-order fields, catalogs, review, and canonical order export. Generic workflow capabilities that do not serve this flow are deferred.

## ADR-002 — Use organization → workspace → process → stream hierarchy

- **Status:** Accepted
- **Decision:** Organization is the contractual tenant boundary; workspace is optional grouping; process defines reusable workflow defaults; stream is an active operational bucket with explicit overrides.
- **Rationale:** Supports multiple countries, locations, business units, languages, ERP targets, and SLAs without duplicating entire configurations.
- **Consequences:** Every tenant-owned resource includes organization scope. Document processing pins a resolved stream version.

## ADR-003 — Local-first, free-first, paid-ready

- **Status:** Accepted
- **Decision:** The full mock vertical slice runs locally without cloud dependencies. Shared development may use free allowances. Customer pilots and production upgrade to services with suitable availability, backup, support, and data terms.
- **Rationale:** Minimize pre-revenue cost without creating disposable architecture or risking customer data.
- **Consequences:** Stable interfaces isolate storage, identity, jobs, providers, email, secrets, and telemetry. Production startup blocks unsafe development modes.

## ADR-004 — Start as a modular monolith

- **Status:** Accepted
- **Decision:** Use one repository with separately runnable web, API, and worker processes and strongly separated modules. Do not begin with microservices.
- **Rationale:** Faster delivery, easier transactions and local operation, lower operational burden, and sufficient boundaries for later extraction.
- **Consequences:** Modules communicate through application interfaces and domain events. A service is extracted only for independent scaling, deployment boundary, failure isolation, stable ownership, or customer-hosted processing.

## ADR-005 — React/TypeScript frontend and Python/FastAPI backend

- **Status:** Accepted
- **Decision:** Use React and TypeScript for the web application; Python and FastAPI for API, domain services, and document workers.
- **Rationale:** Strong UI ecosystem for dense operational applications and strong Python document/AI ecosystem.
- **Consequences:** Shared contracts are generated from OpenAPI and JSON Schema rather than attempting to share runtime source code between languages.

## ADR-006 — Use Vite and a portable client application

- **Status:** Accepted
- **Decision:** Use Vite for the authenticated application rather than tying core product routing/data behavior to one server-rendering hosting vendor.
- **Rationale:** The product is an authenticated operations application, benefits from fast local development, and must deploy locally or to multiple hosting platforms.
- **Consequences:** Marketing/public content may later use a separate rendering strategy. Authentication and API configuration remain deployment-neutral.

## ADR-007 — PostgreSQL is the canonical relational store

- **Status:** Accepted
- **Decision:** Use standard PostgreSQL, portable migrations, and explicit tenant-scoped repositories.
- **Rationale:** Transactions, indexing, JSON, constraints, mature operations, and portability fit the domain.
- **Consequences:** Avoid making a hosted platform's proprietary database API the domain layer. Provider migration should not require product logic changes.

## ADR-008 — Use PostgreSQL-backed durable jobs and transactional outbox first

- **Status:** Accepted
- **Decision:** Implement job state, claims, heartbeats, retries, dead letters, and outbox in PostgreSQL for the initial platform.
- **Rationale:** Local/cloud parity, no essential ephemeral state, transactional scheduling, and lower complexity than adopting a workflow platform before the workflow is stable.
- **Consequences:** The job layer implements a `WorkflowEngine` boundary. A dedicated workflow engine may replace scheduling later without moving business state out of canonical storage.
- **Revisit when:** sustained scale, timers/compensation complexity, customer-hosted workers, or operational evidence justifies migration.

## ADR-009 — S3-compatible object storage with immutable originals

- **Status:** Accepted
- **Decision:** Use MinIO locally and S3-compatible object storage in hosted environments behind `ObjectStore`.
- **Rationale:** Standard object semantics, inexpensive storage, direct uploads, lifecycle control, and provider portability.
- **Consequences:** Database stores metadata/hashes, not large blobs. Originals are immutable; derived artifacts are run-scoped and versioned.

## ADR-010 — Generic OIDC identity boundary; application-owned authorization

- **Status:** Accepted
- **Decision:** Authenticate through OIDC/JWT adapters and own organization membership, roles, permissions, and resource authorization in the application.
- **Rationale:** Avoid identity-provider lock-in and preserve consistent authorization across web, API keys, workers, SSO, and local operation.
- **Consequences:** Development identity is local-only. Production OIDC provider selection remains configurable. SAML/SCIM are P1 through an identity provider or adapter.

## ADR-011 — Tenant isolation is enforced in several layers

- **Status:** Accepted
- **Decision:** Require organization scope in repositories and application use cases, use PostgreSQL RLS where it adds defense in depth, scope object keys/caches/jobs, and run cross-tenant tests.
- **Rationale:** A UI filter or one middleware check is insufficient for confidential B2B documents.
- **Consequences:** Unscoped tenant queries are prohibited in normal production code. Internal support access is explicit, time-limited, and audited.

## ADR-012 — Version and snapshot all processing configuration

- **Status:** Accepted
- **Decision:** Schemas, rules, workflows, prompts/instructions, provider policies, matching, confidence, catalogs, retention, and mapping profiles are versioned. A published stream resolves to an immutable snapshot.
- **Rationale:** Reproducibility, simulation, rollback, audit, and safe multi-stream inheritance.
- **Consequences:** Published versions are never edited. Reprocessing creates a new run tied to selected versions.

## ADR-013 — Use a canonical sales-order contract

- **Status:** Accepted
- **Decision:** Reviewed extraction maps into a versioned canonical order model; ERP and delivery adapters map from that model.
- **Rationale:** Prevent provider/customer/ERP-specific fields from contaminating document understanding and review logic.
- **Consequences:** Every target integration has a versioned mapping profile. Backward compatibility of canonical schema is managed explicitly.

## ADR-014 — Hybrid OCR and LLM pipeline

- **Status:** Accepted
- **Decision:** Use file inspection, native text, rendering/preprocessing, OCR/layout, classification/splitting, schema extraction, deterministic normalization, catalogs/rules, confidence, and review. Do not treat one multimodal LLM call as the pipeline.
- **Rationale:** Better cost, explainability, reliability, evidence, and provider flexibility.
- **Consequences:** Separate provider contracts and stage artifacts. Not every document runs every stage.

## ADR-015 — Evidence is mandatory for automation

- **Status:** Accepted
- **Decision:** Every accepted field retains source page, coordinates when genuinely available, quote, method/provider, configuration version, confidence, validation, and correction history.
- **Rationale:** Users must verify consequential data and the platform must support audit and quality analysis.
- **Consequences:** No fabricated coordinates. Page/text-level evidence is labeled honestly. Auto-approval requires evidence policy.

## ADR-016 — Structured LLM output and no model tools during extraction

- **Status:** Accepted
- **Decision:** Extraction providers return typed schema-constrained output. Models cannot browse, call arbitrary tools, fetch URLs, access secrets, or obey document instructions.
- **Rationale:** Documents are untrusted and may contain prompt injection.
- **Consequences:** Request builder separates instructions from document data, bounds content/cost, validates output, and runs adversarial tests.

## ADR-017 — Human corrections are evaluation data, not automatic training

- **Status:** Accepted
- **Decision:** Store corrections and use them to measure errors, create test cases, and propose versioned changes. Do not silently change production prompts/models/rules from corrections.
- **Rationale:** Prevent uncontrolled behavior drift and cross-customer leakage.
- **Consequences:** Promotion requires evaluation, comparison, approval, publication, and rollback support.

## ADR-018 — Deterministic business rules and matching are first-class

- **Status:** Accepted
- **Decision:** Use inspectable typed rules and explainable exact/normalized/fuzzy matching. LLMs may propose drafts but do not become the runtime rule engine.
- **Rationale:** Sales orders require reliable customer, ship-to, material, price, UOM, totals, and duplicate checks.
- **Consequences:** Rule/matching versions and feature scores are retained and shown in review.

## ADR-019 — Review Studio is the product's primary UX investment

- **Status:** Accepted
- **Decision:** Build an evidence-first, keyboard-driven, high-density review workbench with a document viewer, header fields, line-item grid, explanations, conflicts, and approval summary.
- **Rationale:** Human exception handling determines realized automation value and user trust.
- **Consequences:** Usability and accessibility failures can block pilot release. A generic CRUD form is unacceptable.

## ADR-020 — Custom token-driven design system

- **Status:** Accepted
- **Decision:** Build accessible primitives and document-operation composites around the tokens and patterns in `UI_UX_BLUEPRINT.md`; do not ship a stock admin template.
- **Rationale:** Product differentiation and reviewer efficiency require coherent custom interaction design.
- **Consequences:** Component workbench, visual regression, accessibility checks, realistic density, and high-fidelity implementation are P0.

## ADR-021 — Direct-to-object-storage upload sessions

- **Status:** Accepted
- **Decision:** API authorizes a bounded upload and returns a short-lived signed target; client uploads directly and then completes the session for verification and registration.
- **Rationale:** Avoid proxying large files through API instances and improve portability/scalability.
- **Consequences:** Completion verifies metadata/hash, authorization, quota, and idempotency before creating a document.

## ADR-022 — Immutable runs and append-oriented history

- **Status:** Accepted
- **Decision:** Reprocessing creates a new run. Stage results, extraction versions, corrections, delivery attempts, and audit events preserve history.
- **Rationale:** Reproducibility and investigation require history instead of overwrite-in-place.
- **Consequences:** Current state is a projection; storage/retention policies manage volume.

## ADR-023 — Signed webhook and JSON are the first integration targets

- **Status:** Accepted
- **Decision:** Prove canonical export, signed webhook, retry, idempotency, and replay before building a deep ERP catalog.
- **Rationale:** Separates core product proof from customer-specific ERP complexity.
- **Consequences:** First production ERP adapter is selected after canonical and delivery behavior are stable.

## ADR-024 — Production must not rely on unsuitable free plans

- **Status:** Accepted
- **Decision:** Free tiers are for development/demo only unless their terms and guarantees satisfy the exact production requirement. Real pilots use paid services where needed for data terms, backups, uptime, capacity, monitoring, and support.
- **Rationale:** Cost minimization cannot override customer confidentiality or operational reliability.
- **Consequences:** Paid-pilot gate includes provider terms, backups, restore, monitoring, and operational ownership.

## ADR-025 — No Kubernetes initially

- **Status:** Accepted
- **Decision:** Use Docker locally and managed container/static services in hosted environments. Do not introduce Kubernetes until a concrete deployment, scale, or customer-hosted requirement exists.
- **Rationale:** Avoid premature operational burden.
- **Consequences:** Containers and configuration remain orchestrator-compatible, but no cluster platform is required for P0.

## ADR-026 — API-first REST contract for P0

- **Status:** Accepted
- **Decision:** Use versioned REST/JSON with generated OpenAPI client, cursor pagination, idempotency, correlation IDs, and optimistic concurrency.
- **Rationale:** Clear contract, broad tooling support, and appropriate complexity for P0.
- **Consequences:** GraphQL is not introduced without a measured need. Internal jobs use typed payloads, not public API DTOs blindly.

## ADR-027 — Feature flags are controlled configuration

- **Status:** Accepted
- **Decision:** Feature flags are typed, scoped, audited, owned, and time-bounded. They cannot be ordinary ad-hoc environment conditionals spread through code.
- **Rationale:** Safe rollout without creating permanent hidden behavior.
- **Consequences:** Flags have removal/review tasks and cannot weaken non-optional security invariants.

---

# Provisional implementation choices

## ADR-028 — Initial open-source OCR baseline

- **Status:** Provisional
- **Decision:** Tesseract is the required local baseline; evaluate a second layout-capable local OCR option against the gold dataset.
- **Validation:** Compare accuracy, table behavior, languages, latency, memory, packaging, and maintenance.
- **Do not assume:** a larger local OCR dependency is better without evidence.

## ADR-029 — Initial frontend libraries

- **Status:** Provisional
- **Decision:** TanStack Router, Query, Table, and Virtual; React Hook Form; Zod; PDF.js; React Aria-oriented accessible primitives.
- **Validation:** Confirm compatibility, bundle size, keyboard behavior, and long-term maintenance during foundation phase.
- **Constraint:** Changing a library must preserve the UX/accessibility contract and receive a dependency rationale.

## ADR-030 — `pg_trgm` for fuzzy matching

- **Status:** Provisional
- **Decision:** Use standard PostgreSQL normalization and trigram similarity before adding a separate search/vector service.
- **Validation:** Benchmark ship-to, customer, and material candidate quality and latency at representative catalog sizes.

---

# Decisions intentionally deferred

## OPEN-001 — Hosted deployment provider

- **Status:** Accepted (2026-07-14) — Google Cloud / Firebase family, **not Firestore**.
- **Decision:** Deploy on the Firebase/GCP ecosystem: **Cloud SQL for PostgreSQL** (the RLS tenancy model and Postgres job queue require a real Postgres, so Firestore is a wrong fit and was rejected), **Cloud Run** for the API and worker containers (REL-001 images), **Cloud Storage** for artifacts (behind the existing `ObjectStore` interface), **GCP Secret Manager** for secrets and BYO keys (behind the existing `SecretStore` interface, alongside the AWS adapter), **Firebase Auth / Identity Platform** for login (issues OIDC JWTs the TEN-003 adapter already consumes), and **Firebase Hosting** for the web app.
- **Rationale:** Owner preference for the Firebase ecosystem, satisfied without discarding the Postgres-RLS security model. Two thin adapters (GCS object store, GCP Secret Manager) are the only new code; both sit behind interfaces that already exist. Keeps local parity — the same containers and Postgres run locally.
- **Consequences:** Staging infrastructure (REL-002) targets Cloud SQL + Cloud Run. Add a `GcsObjectStore` (behind STO-001) and a `GcpSecretManagerStore` (behind SEC-005). Firestore and GCP-proprietary datastores are out of scope for tenant data.

## OPEN-002 — Production identity provider

- **Status:** Deferred behind OIDC boundary
- **Options:** managed auth platform or enterprise identity broker.
- **Decision criteria:** OIDC, SAML/SCIM roadmap, MFA, custom domains, audit, pricing, migration/export, organization connections.
- **Deadline:** before production authentication configuration.

## OPEN-003 — First managed OCR provider

- **Status:** Deferred behind provider contract
- **Decision criteria:** accuracy on pilot corpus, coordinates/tables, language, region, retention terms, latency, pricing, and fallback behavior.
- **Deadline:** after local OCR and gold evaluation runner exist.

## OPEN-004 — First hosted extraction provider and model

- **Status:** Accepted (2026-07-14) — local-first, with optional BYO hosted keys.
- **Decision:** Default to **local models** (Qwen2.5-VL-7B-Instruct for vision, Qwen2.5-7B-Instruct for text, via Ollama's OpenAI-compatible endpoint — AIO-007), so no customer data leaves the deployment unless a tenant opts in. Optionally register a **customer-supplied hosted key**: Claude via the dedicated Messages-API adapter (AIO-008), and Gemini/OpenAI via the OpenAI-compatible adapter with Bearer auth. Hosted providers declare third-party processing honestly and are only selected when tenant policy allows it.
- **Rationale:** Owner direction to run and test locally first and to let customers bring their own Claude/Gemini/OpenAI keys. The provider contract (AIO-001) and router (AIO-013) already make this a configuration choice, and the gold evaluation (AIO-016/017) governs any promotion. See [`LLM_PROVIDERS.md`](LLM_PROVIDERS.md).
- **Consequences:** Hosted keys are fail-closed (no key → no provider). Per-tenant BYO keys via the SEC-005 secret store are the follow-on (tracked with AIO-006); today a deployment-level key applies deployment-wide. Contract/retention terms for any hosted provider are documented before production use.

## OPEN-005 — First production ERP adapter

- **Status:** Deferred
- **Decision criteria:** pilot customer's destination, sandbox availability, idempotency method, API maturity, field mapping, and support ownership.
- **Deadline:** after canonical JSON and webhook delivery are stable.

## OPEN-006 — Billing vendor

- **Status:** Deferred
- **Decision criteria:** B2B invoicing, annual contracts, PO billing, usage reconciliation, taxes, credits, and geography.
- **Constraint:** usage ledger and plan model remain vendor-neutral.

## OPEN-007 — Dedicated workflow engine

- **Status:** Deferred
- **Trigger criteria:** proven need for long timers, complex compensation, high scale, multi-region orchestration, external worker fleets, or operational limitations in the PostgreSQL job engine.
- **Constraint:** migration cannot make the workflow provider the only holder of business state.

## OPEN-008 — Semantic/vector matching

- **Status:** Deferred
- **Trigger criteria:** exact/normalized/trigram matching fails measured pilot cases and embeddings improve candidate quality without unacceptable privacy/cost/complexity.

## OPEN-009 — Multi-region and private processing

- **Status:** Deferred to enterprise phase
- **Constraint:** control/processing plane boundaries, provider policy, artifact locations, and tenant data region are modeled now.

## OPEN-010 — Brand name and final visual identity

- **Status:** Deferred
- **Constraint:** implementation uses semantic tokens and a replaceable identity layer; UX structure and quality do not wait on final naming.

---

# Decision review checklist

Before accepting a new architecture decision, answer:

1. What user, security, reliability, cost, or delivery problem requires the decision?
2. What alternatives were considered?
3. Does the decision preserve local operation?
4. Does it create vendor lock-in or customer-data implications?
5. Does it affect tenant isolation, audit, retention, or reproducibility?
6. Does it add a service/team operational burden?
7. What tests and migration/rollback path prove it?
8. When should it be revisited?

Unresolved implementation convenience is not a sufficient reason to reverse an accepted decision.
