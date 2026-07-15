# Production readiness audit

**Last source audit:** 2026-07-15
**Branch:** `codex/production-remediation`
**Decision:** **not approved for production customer documents**

This is the release-readiness source of truth. The planning backlog records
intent and acceptance criteria; it is not evidence that a capability is live.
Production approval requires the code-owned controls below **and** evidence
from the target environment, representative customer documents, operators,
security reviewers, and the pilot customer.

## What the application is

SOA is a multi-tenant purchase-order-to-sales-order operations platform. A
published stream pins the schema, authored instructions, rules, catalogs,
confidence policy, provider/data policy, credentials, and output mapping used
for each immutable processing run. Documents enter through browser upload,
API, or raw-MIME email intake; a durable worker renders, recognizes, extracts,
normalizes, matches, validates, and routes them to review or approval. Approved
canonical orders can be downloaded or delivered through a configured
integration with retry and audit history.

The first production slice accepts **one sales order per input**. Packet
classification and splitting remain a later, separately evaluated capability;
the runtime fails closed instead of pretending to support mixed packets.

## What works in code

| Area | Current evidence-backed capability |
| --- | --- |
| Tenant and identity | Organization creation, invitation acceptance, membership/role management, API credentials, server-side authorization, production OIDC/JWT validation, Firebase and generic OIDC browser flows, and PostgreSQL RLS coverage. Production rejects development identity. |
| Intake and storage | Direct signed uploads, durable abandoned-upload cleanup, bounded multipart API intake, authenticated/bounded raw-MIME email intake, media inspection, duplicate detection, page/resource limits, mandatory production malware scanning, filesystem development storage, S3/MinIO, and GCS with checksum verification. |
| Durable execution | PostgreSQL claims, heartbeats, expired-lock recovery, retry/dead-letter handling, bounded worker concurrency, terminal-domain reconciliation, stage replay, immutable run snapshots, and registered preprocess/stage/export/evaluation/outbox/data-export/document-deletion handlers. |
| Document understanding | Native PDF text, bounded Tesseract fallback, page artifacts, schema-constrained mock/local/Anthropic/Gemini/OpenAI-compatible extraction, bounded malformed-output repair, deterministic normalization, authored rules, catalog/customer/product/UOM matching, duplicate-PO checks, and risk-based review routing. |
| Human review | Review queue and workbench, evidence viewer, field and line editing, catalog candidate selection, comments, assignment, conflict handling, approval/rejection/reopen, and an append-oriented audit trail. |
| Configuration and quality | Versioned processes/streams/schemas/rules/instructions/provider policies, immutable execution fingerprints, server-executed evaluation runs, candidate/baseline comparison, publish gating, runtime provenance, provider health aggregates, and per-call usage/cost attribution. |
| Delivery | Canonical JSON/CSV, formula-safe CSV, signed and replay-bounded webhooks, idempotent QuickBooks Online delivery, retry/dead-letter history, connection tests, activation controls, and destination allowlist/SSRF checks. NetSuite, Dynamics 365, and SAP are connection-test-only and fail delivery closed until their vendor-specific idempotent upserts are implemented and certified. |
| Data governance | Retention state machine, persisted legal-hold history, two-person approval-gated durable document deletion with reconciled tombstones, complete document-link erasure/anonymization inventory, document-level export, durable organization export with snapshots/manifests/expiry cleanup, opaque secret references, and durable retired integration-secret revocation. Provider rotation instead retains superseded values for immutable policy/run pins. |
| Release and operations | Non-root digest-pinned images, separately promoted API/migrator/worker/web artifacts, GCP Terraform for Cloud SQL/GCS/Cloud Run/Firebase, separate migrator/runtime identities, DML-only runtime database grants, structured safe logging, OTLP traces, provider/job metrics, alert catalog, runbooks, SBOM/dependency/secret/container scanning workflows, and expand/contract migration policy. |

## Defects closed during the production audit

These were real runtime or release defects, not documentation-only cleanup:

1. The worker entry point did not execute durable jobs. It now constructs the
   database queue, registers every supported handler, heartbeats work, recovers
   expired locks, and reconciles terminal failures.
2. Non-mock extraction could receive no document text. The pipeline now
   recognizes each page, persists the recognized input, and supplies it to the
   pinned extraction provider.
3. Runtime execution could drift from published stream configuration. Runs now
   authenticate a complete immutable snapshot and fingerprint before executing
   schema, rules, catalogs, instructions, provider policy, data policy, and
   credentials.
4. Evaluation accepted caller-supplied predictions. Evaluation jobs now execute
   the candidate configuration on the server and persist comparable results;
   stream promotion gates the exact candidate fingerprint.
5. Catalog field selections were not durable/runtime-complete. The selected
   customer, product, and UOM catalogs and match evidence now participate in
   validation and review routing.
6. Organization export was incomplete and mutable while paging. It is now a
   durable, resumable, batched export with a fixed snapshot, complete category
   manifests, cancellation, expiring downloads, and object cleanup.
7. Rate limiting was process-local. Staging/production use atomic PostgreSQL
   windows with HMAC-pseudonymized identities; public credential probing is
   limited before credential lookup.
8. Hosted-provider fallback, health, token usage, repair calls, and costs were
   incomplete or winner-only. Each attempted call is now attributed to its
   provider/model/rate card, unknown usage stays explicitly unknown, bounded
   repair is on the runtime path, and tenant-scoped health aggregates are
   durable across replicas.
9. Browser authentication and administration had placeholder paths. The web app
   now supports session/popup/redirect login, token refresh/logout, organization
   creation and invitation acceptance, member/role administration, data
   exports, integration connection testing/status/activation, and credential
   rotation.
10. Integration safety claims exceeded behavior. All outbound tests/deliveries
    now share exact-host/public-address validation; paused integrations cannot
    export; the last active organization administrator is protected;
    integration-credential revocation is transactionally scheduled; and
    unproved ERP delivery paths fail closed rather than issuing duplicate-prone
    POSTs.
11. Reference Cloud Run request limits conflicted with configured email and API
    upload sizes. Raw MIME and multipart bodies are now streamed/bounded below
    the platform ceiling with envelope headroom.
12. The Cloud SQL runtime user would have retained the provider-created
    `cloudsqlsuperuser` membership. Migration `0045` strips elevated attributes
    and membership, then grants only the runtime DML/sequence access exercised
    by the API and worker. The migrator remains a separate DDL identity.
13. OTLP span export could block request/job completion. Production OTLP uses a
    batch span processor and flushes during graceful shutdown.
14. Release workflows could promote mismatched or rebuilt artifacts. The
    release manifest now records independent scanned digests, the web deploy
    publishes the exact scanned image bytes, and deploy failure rolls services
    back to the prior recorded digests.
15. Signed upload sessions could leave object bytes behind and race
    completion/abort at expiry. Session persistence is now shared by API and
    worker; the signed URL cannot outlive the database session; creation
    schedules primary and delayed-sweep `upload.cleanup` jobs; and cleanup,
    completion, and abort are fenced by tenant-scoped state, row locks, and a
    durable verification lease. Storage reads, hashing, and malware scanning
    run outside the row lock. The sweep removes a late PUT admitted before
    expiry/abort while never deleting a completed session.
16. Provider keys were environment-only and policy clients could handle raw
    secret references. Tenant-scoped provider credential metadata now lives
    under RLS; values write directly to the secret store and are never returned;
    policy clients submit only a credential ID; the server binds the immutable
    reference, validates provider/data/region/current-state constraints, and
    guards revocation against referenced versions. Provider and integration
    secret writes also register idempotent revoke-on-rollback compensation. If
    immediate compensation fails, a tenant-fenced durable cleanup intent
    redrives it under a bounded retry budget without storing secret values. The
    provider screen now exposes write-only key rotation, a
    primary-plus-fallback policy editor, provider/confidence draft validation
    and publication, history, and guarded break-glass revocation.

17. Service credentials lacked a complete production lifecycle. Authorized
    administrators can now list metadata, create and rotate one-time values,
    revoke keys, set bounded expiry, grant only allowlisted machine scopes, and
    restrict each key to explicit stream IDs. Legacy unscoped keys fail closed,
    every mutation is concurrency-checked, and public intake hides unauthorized
    streams behind the same response as a missing stream.
18. Regulatory deletion was not a safe end-to-end operation. Document deletion
    now requires a recorded request and a distinct approver, respects persisted
    legal holds at approval and execution, runs as a durable tenant-fenced job,
    reconciles terminal failures, erases or anonymizes every inventoried typed,
    JSON, object, and export reference, and preserves only a minimal tombstone.
    Closed reference inventories make a newly introduced document link fail the
    completeness test until it has an explicit erasure policy.
19. Object storage, malware scanning, renderer/OCR/provider calls, secret-store
    operations, ERP delivery, and outbox publication could hold database
    transactions open. Intake and worker phases now use short read/commit/write
    units, perform external or CPU-heavy work without a checked-out SQL
    transaction, then rebind the tenant for an idempotent fenced commit.
    Rollback cleanup itself runs after SQL rollback and has the durable cleanup
    fallback described above.
20. Cross-replica quota, version allocation, and lease-acknowledgement decisions
    were not proven. PostgreSQL advisory/row locks now serialize pending-upload
    caps, provider rotation, and policy version slots; attempt tokens fence stale
    worker acknowledgements; and real-PostgreSQL concurrency tests exercise the
    races rather than relying on SQLite scheduling.
21. A release could briefly publish mismatched API and web generations or roll
    back to a label instead of an exact revision. Deployment now records exact
    Cloud Run revisions, traffic, worker image, Firebase live version, and
    immutable web bytes; verifies a preview first; promotes the exact API
    revision and canonical/authenticated smoke before Firebase; and restores
    the precise prior state on failure.
22. Unexpected API exceptions and malformed scanner protocol responses could
    expose unsafe diagnostics or behave ambiguously. Logs now retain request
    correlation and exception type without request/provider payloads, while
    truncated or oversized ClamAV frames fail closed under bounded reads.

## Local verification evidence for this branch

The final local gate on 2026-07-15 produced the following evidence:

- Ruff formatting/lint passed across 501 Python files; strict mypy passed across
  236 source files; the aggregate pytest run collected 1,796 tests and every
  locally runnable test passed. Environment-marked cases were run separately
  where a local dependency was available.
- A disposable real PostgreSQL 16 database upgraded to migration `0053`,
  downgraded to base, and re-upgraded to `0053`. The runtime role lost elevated
  role membership/attributes as intended, and all 15 PostgreSQL-only RLS,
  claim, dedupe, rate-limit, metrics, quota, policy, deletion, and stale-lease
  tests passed. PostgreSQL 17 remains the CI/target-environment confirmation.
- ESLint, Prettier, TypeScript, 353 Vitest component/unit tests, and the
  production web build passed on Node 22. All three Chromium accessibility
  tests passed after the protected-workbench session fixture was corrected.
  Pixel comparison remains Linux-CI canonical; macOS rendered all 15 cases but
  differs from the checked-in Linux raster baselines as documented.
- The 12 deterministic performance tests, eight resilience tests, ten release
  contract tests, documentation checks, dependency policy/audits, Actionlint,
  and Terraform 1.12.2 format/init/validate gates passed. Dependency scans
  reported zero known findings and no active exceptions.

This evidence does **not** convert unavailable gates into passes. Container
build/Trivy, MinIO, live ClamAV, GCS signed-upload, PostgreSQL 17, target GCP
plan/apply, live identity/provider/ERP, load/soak, backup/restore, Linux visual,
and external security/customer/legal gates remain unverified until their
corresponding systems and owners produce evidence.

## What is still blocking production

Most remaining blockers require target-environment, customer, vendor, legal,
security, or operator evidence. The few explicit code additions below are
bounded gaps; completing them still would not replace that external evidence.

### Target-environment evidence

1. Apply the committed Terraform to a billed staging GCP project through
   Workload Identity Federation. Verify private Cloud SQL, runtime/migrator
   roles, GCS lifecycle/versioning, Cloud Run API and worker pool, Firebase
   Hosting/Auth, DNS/TLS, Secret Manager, and the authenticated OTLP collector.
2. Configure real Firebase/Identity Platform or compatible OIDC connections and
   exercise login, invitation email ownership, MFA policy, refresh, revocation,
   disabled users, role changes, and last-admin recovery in staging.
3. Operate real ClamAV capacity, inbound-email forwarding/authentication, the
   signed outbox receiver, and cleanup schedules. Prove degraded dependencies
   make readiness/alerts fail safely.
4. Run the full PostgreSQL/RLS migration and rollback matrix with the exact
   Cloud SQL roles; deploy N and N-1 images against the expanded schema; record
   the migration duration and lock profile.
5. Execute backup/PITR and object-version recovery, restore to an isolated
   environment, reconcile every artifact hash, and record achieved RPO/RTO.
6. Exercise provider outage/fallback, worker termination, database/storage
   faults, release rollback, queue drain, export burst, and sustained load.
   Capture API p95, queue age, stage latency, memory, concurrency scaling, and
   cost under the launch document-size distribution.
7. Arm every alert in the actual monitoring backend, map owner labels to real
   rotations/pagers, fire each test signal, and complete an incident game day.

### Accuracy, integration, and customer evidence

1. Replace synthetic gold-manifest placeholders with rights-cleared source
   bytes and reconciled ground truth from the pilot distribution. Measure
   critical-field, line-cell, catalog-match, false-auto-approval, review-rate,
   latency, and cost results by customer/layout/language cohort.
2. Calibrate field/risk thresholds from observed correctness. Until the
   false-auto-approval bound is met, keep consequential orders in human review;
   model confidence is an uncalibrated input, not proof.
3. Configure tenant-specific provider contracts, regions, retention terms,
   credentials, rate cards, and budgets. Reconcile ledger estimates to real
   invoices and test timeout/unknown-usage handling. Add a separate OAuth
   credential lifecycle if a chosen provider requires it.
4. Choose the pilot ERP. QuickBooks still needs a real sandbox, OAuth token
   refresh/rotation operations, mapping fixtures, vendor error cases, and
   duplicate/retry certification. NetSuite, Dynamics, and SAP delivery must
   remain disabled until each vendor's executable idempotent upsert contract is
   implemented and tested against an official sandbox.
5. Import and reconcile representative customer/material/UOM/ship-to master
   data; test ambiguous, missing, stale, and conflicting records; obtain the
   customer's mapping and approval sign-off.
6. Complete reviewer usability and accessibility studies with actual operators,
   including keyboard-only and screen-reader passes, concurrency conflicts, and
   time-to-review benchmarks.

### Security, legal, and operations evidence

1. Obtain independent penetration testing and threat-model owner sign-off;
   close all release-blocking findings.
2. Establish controlled support access. Raw operator database access is not an
   acceptable normal support path; grants must be explicit, time-limited,
   approved, and audited.
3. Complete the DPA, privacy policy, terms, subprocessor inventory, provider
   data-use terms, retention schedule, incident contacts, support commitments,
   and deletion/export request process.
4. Verify container runtime hardening in the deployed platform (read-only root,
   non-root, dropped capabilities, default seccomp, bounded scratch/cgroups)
   and run Trivy/SBOM/signature/provenance gates on the released digests.

## Remove, defer, and add

**Remove from launch claims:** mixed-packet splitting, autonomous high-risk
approval, per-field geometric evidence when only page/text evidence exists,
and production delivery through NetSuite/Dynamics/SAP. Also remove any shared
development key or development identity from a deployed environment.

**Defer until evidence justifies it:** semantic/vector matching, managed OCR,
multi-region/private workers, SAML/SCIM, a dedicated workflow engine, and
automatic learning from corrections.

**Add before launch:** live alert policies/dashboards, backup/restore evidence,
identity and connector runbooks with real owners, corpus-based acceptance
thresholds, invoice reconciliation, OAuth refresh operations, customer master
data fixtures, a support-access boundary, and release evidence attached to a
go/no-go record.

## Ordered path to a controlled production release

1. Apply and smoke-test staging with production-equivalent identities,
   secrets, networking, storage, telemetry, scanner, and outbound receivers.
2. Build the rights-cleared pilot corpus and establish the conservative
   baseline; fix regressions, then pin the winning configuration and rate card.
3. Certify the selected ERP and customer mappings under retries, timeouts,
   credential expiry, duplicate requests, and replay.
4. Complete load/fault, restore, security, privacy, accessibility, and incident
   exercises; close all blocking findings.
5. Run a shadow/parallel pilot with human approval required. Reconcile every
   input, output, correction, delivery, usage record, and customer outcome.
6. Approve a small canary only after all evidence is attached. Define rollback
   triggers, on-call ownership, and an explicit stop condition before raising
   traffic or automation thresholds.

## Post-launch improvement roadmap

### Better discussions and review quality

- Replace flat document comments with field/evidence-anchored threads, replies,
  resolution state, typed mentions, notification delivery, and reviewer
  handoff/escalation workflows.
- Add structured correction reasons and disagreement categories so discussion
  becomes useful quality data rather than free-text noise.
- Show configuration/provider/catalog provenance beside each disputed value and
  let reviewers compare candidate evidence without leaving the workbench.
- Measure review time, reopen rate, inter-reviewer agreement, unresolved thread
  age, and corrections after approval. Use those measures to simplify the UI
  and rules, not to rank individual workers.

### Better LLM and OCR quality

- Segment evaluation by field, layout, language, scan quality, customer, and
  ambiguity; promote only statistically meaningful improvements with explicit
  false-auto-approval bounds.
- Calibrate confidence per provider/field/cohort. Use selective fallback or
  adjudication only where expected error reduction exceeds added cost/latency.
- Mine reviewed corrections into tenant-isolated candidate tests; require human
  curation and versioned promotion rather than automatic retraining.
- Add multimodal page inputs or a layout-capable OCR provider only after a
  corpus experiment proves improvement over the current text/OCR path.
- Track malformed-output repair, fallback, disagreement, abstention, evidence
  quality, and cost for every attempt. Prefer explicit abstention to plausible
  but unsupported values.

### Better processing phases

- Persist phase-specific quality reasons and service-level budgets so a slow or
  inaccurate render/OCR/extract/match/validate/export step is attributable.
- Short-circuit native-text pages, deduplicate identical artifacts and provider
  requests, and avoid re-running unaffected phases after a correction.
- Separate external/CPU-heavy work from long database transactions while
  retaining immutable inputs, idempotent commits, and recoverable leases.
- Prioritize documents and review fields by business risk and SLA, not FIFO
  alone; expose pause/backpressure state to operators.

### Better code quality

- Keep contract tests for every storage, provider, secret, identity, and ERP
  adapter; run them against real sandboxes on a schedule.
- Enforce architecture boundaries, migration/RLS coverage, strict typing,
  dependency updates, mutation/property tests for mapping and normalization,
  and a documented removal date for every feature flag/waiver.
- Split very large routers/orchestrators by use case once behavior is stable,
  while keeping transactions and domain invariants centralized.
- Treat docs, Terraform validation, OpenAPI compatibility, database privileges,
  and release manifests as tested code.

### Better speed and cost

- Reuse bounded HTTP clients/connections for model and connector calls and tune
  concurrency separately for CPU, database, and external-provider stages.
- Parallelize independent page recognition with measured memory bounds; batch
  catalog and export operations; cache only immutable, tenant-keyed snapshots.
- Instrument queue wait, stage execution, provider latency, object I/O, database
  time, and browser interaction separately, then optimize the measured critical
  path.
- Use native text and the least expensive evaluated provider first; reserve
  larger/multimodal/fallback calls for low-quality or high-risk cases.

## Verification contract

The local aggregate gate is:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
volta run --node 22 pnpm run lint
volta run --node 22 pnpm run format:check
volta run --node 22 pnpm run typecheck
volta run --node 22 pnpm run test
volta run --node 22 pnpm run build
python3 scripts/check_docs.py
uv run pytest tests/performance tests/resilience -o addopts=""
```

PostgreSQL/RLS, browser, container, Terraform, scanner, migration/rollback,
backup/restore, load, provider, and connector gates remain separate because
they require the corresponding runtime or external system. A skipped or
unavailable gate is **unverified**, never passed.
