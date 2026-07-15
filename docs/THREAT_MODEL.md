# Threat model (SEC-001)

> **Status: draft — owner/security review required before pilot.** Residual
> risks are release inputs, not accepted risks. Read with
> [`SECURITY_OPERATIONS.md`](SECURITY_OPERATIONS.md),
> [`SECURITY_AND_PRIVACY.md`](SECURITY_AND_PRIVACY.md), and
> [`SANDBOX_PROFILE.md`](SANDBOX_PROFILE.md).

## Scope and method

This is a STRIDE-informed review of every externally reachable or
customer-data-bearing boundary: web identity, API, browser upload, public API
intake, email intake, parsers, worker and database queue, object storage,
LLM/OCR providers, outbound integrations, audit/export/deletion, deployment,
and internal support. A mitigation is listed as shipped only when its runtime
path and tests exist. Target-environment and contractual controls remain
residual until exercised.

## Critical assets

| Asset | Location | Primary harm |
| --- | --- | --- |
| Original documents, page artifacts, extracted/corrected values | Object store; document, artifact, page, run, field, and correction rows | Commercial-data disclosure or tampering |
| Canonical orders and delivery payloads | Canonical/export/outbox rows and object parts | Incorrect or duplicate customer ERP transactions |
| Customer/material/UOM/ship-to catalogs | Catalog/version/record rows | Sensitive master-data disclosure or wrong matching |
| API, provider, and integration credentials | Hashed API keys; opaque secret references; managed secret store | Account/ERP/provider compromise |
| Published configuration and runtime provenance | Versioned process/stream/schema/rule/instruction/provider/mapping rows | Silent behavior drift or unrepeatable decisions |
| Audit, usage, provider-health, and evaluation facts | PostgreSQL | Covering activity, falsifying cost/quality, or erasing evidence |
| Tenant boundary and service availability | `organization_id`, RLS GUC, jobs/locks | Cross-tenant breach or order-flow outage |

## Trust boundaries and controls

### Browser, identity, and authorization

- **Threats:** forged/expired tokens, redirect or token leakage, invitation
  takeover, session replay, disabled-user access, privilege escalation, and
  removing the last administrator.
- **Controls:** production rejects development identity; the API verifies
  asymmetric OIDC JWT issuer/audience/JWKS/expiry; the browser supports
  Firebase SDK or generic OIDC authorization-code/PKCE flows with refresh and
  logout; authorization resolves an active organization membership and
  registered permission on every tenant route; invitations bind to a
  case-normalized email and require an explicitly verified identity; role
  changes are audited; the last active organization administrator cannot be
  removed or stripped of administration.
- **Residual:** a real Firebase/Identity Platform or compatible OIDC tenant,
  MFA/session/revocation policy, custom domains, invitation delivery, emergency
  administrator recovery, and disabled-user tests must be proved in staging.
  SAML/SCIM are not product capabilities today.

### Tenant database and object boundary

- **Threats:** ID/slug guessing, unscoped queries, worker payload spoofing,
  signed-URL leakage, object-key confusion, privileged runtime users, and
  cache/config leakage across tenants.
- **Controls:** a central authorization service creates the tenant context;
  repositories carry `OrganizationContext`; PostgreSQL forces RLS for every
  registered tenant table and CI cross-checks the registry; jobs include and
  rebind immutable organization context; object keys and artifact records are
  tenant/run scoped and checksum verified; quarantined downloads are refused;
  signed URLs have bounded TTLs. Migration `0045` removes Cloud SQL's broad
  bootstrap role and all bypass/create attributes from `soa_app`; runtime owns
  no application table and receives DML/sequence access only.
- **Residual:** a leaked signed URL remains a bearer capability until expiry.
  RLS still depends on correct tenant GUC binding, so repository scope and
  tests remain necessary defense in depth. The exact Cloud SQL identity/grants
  need target-environment verification.

### Hostile files and resource exhaustion

- **Threats:** parser exploits, malware distribution, decompression/pixel/page
  bombs, oversized multipart/MIME bodies, CPU/memory exhaustion, and active PDF
  content.
- **Controls:** media/magic-byte validation, upload/page/pixel/decompression
  bounds, mandatory production ClamAV, quarantine, bounded Cloud Run multipart
  and raw-MIME intake, and process sandboxing for PDFium/Pillow/Tesseract with a
  secret-free environment, isolated scratch space, read-only input, rlimits,
  wall-clock process-group kill, and Python network/process denial. Runtime
  images are non-root and the deployment profile requires read-only root,
  dropped capabilities, default seccomp, PID/memory/CPU limits, and bounded
  scratch space.
- **Residual:** Python denials do not contain a native-code exploit by
  themselves. The deployed container/cgroup/seccomp profile and ClamAV capacity
  must be verified. Application rate limits run after the edge accepts/parses
  some requests; Cloud Armor/API-gateway/L7 body and flood controls remain a
  deployment requirement.

### Public API and email abuse

- **Threats:** credential probing, replay, queue/storage flooding, tenant
  enumeration, malformed MIME amplification, and duplicate orders.
- **Controls:** API keys are stored as hashes, returned only on create/rotate,
  compared in constant time, expire, and carry explicit tenant/capability/stream
  scope; invalid-key attempts are limited before lookup; authenticated ingestion
  has a per-credential window; email intake requires an independent shared secret
  and applies raw/attachment/count/rate bounds before tenant routing; staging
  and production share atomic PostgreSQL windows keyed by domain-separated HMAC
  identity digests; content and business duplicate checks plus job/export
  idempotency contain replay.
- **Residual:** application abuse limits are not billing quotas or a substitute
  for managed DDoS/WAF controls. Email provider authentication, rotation, and
  replay semantics need a live ingress contract.

### Parser, OCR, and model boundary

- **Threats:** prompt injection, document-to-system delimiter escape, secret or
  cross-document exfiltration, tool/URL execution, invalid structures,
  fabricated evidence/confidence, unbounded repair/fallback, and unauthorized
  third-party processing.
- **Controls:** document text is JSON-encoded only in the user-data message;
  platform/tenant instructions are separate and URL-bearing configuration is
  refused; model requests declare no tools, use bounded page/character/output
  and retry/cost budgets, and return only validated requested fields. Evidence
  is page/text-level unless real geometry exists. Malformed output gets bounded
  repair then safe manual-review fallback. The run authenticates its immutable
  provider/data policy, candidate chain, region, quality/cost bounds, and
  per-tenant secret references; an empty tenant credential never falls back to
  a shared key. Every repair, failure, and fallback attempt records safe usage,
  cost, health, and provenance without document/vendor error content.
- **Residual:** prompt injection can still degrade extraction correctness.
  Model confidence is uncalibrated. Hosted provider terms, region, retention,
  training policy, deletion behavior, invoice reconciliation, and real outage
  behavior require corpus/contract/staging evidence.

### Outbound integration and customer-system boundary

- **Threats:** SSRF/DNS rebinding, credentials in errors, CSV formula
  execution, response-content exfiltration, replay, and duplicate ERP orders.
- **Controls:** CSV cells are formula-defused. Webhook delivery is HMAC signed,
  timestamp/replay bounded, and idempotency keyed. Connection tests and
  delivery validate HTTPS, an exact-host allowlist, and public DNS answers
  before connecting; safe errors exclude tokens, payloads, and vendor bodies.
  Paused integrations cannot export. QuickBooks carries a stable `requestid`.
  NetSuite, Dynamics, and SAP delivery fails closed because their executable
  vendor-specific idempotent upsert contracts are not implemented.
- **Residual:** QuickBooks OAuth refresh and sandbox duplicate/retry
  certification are external launch work. Connection-test success proves only
  reachability/authorization. Each additional ERP needs official-sandbox
  idempotency, mapping, error, and credential-lifecycle evidence before its
  delivery path can be enabled.

### Credential creation and rotation

- **Threats:** plaintext storage/logging, cross-tenant secret resolution,
  deletion before transaction commit, stale secret reuse, and incomplete
  revocation after a worker failure.
- **Controls:** databases retain opaque `secretref://` values, not raw secrets;
  production uses managed GCP/AWS stores; provider/ERP resolution is scoped to
  the authenticated run/integration; responses and telemetry are canary-tested
  for leakage. Integration rotation commits the replacement plus a narrow
  durable `secret.revoke` job atomically. Provider policy clients submit only
  metadata IDs; the server binds the reference. Provider rotation retains old
  values for immutable pins, normal revocation refuses policy references, and
  audited force revocation breaks affected pins closed.
  Provider/integration writes register idempotent external revocation as
  database-rollback compensation, so failed metadata transactions do not
  orphan the new value. Dynamic tenant credentials live in a dedicated GCP
  project, isolating their runtime create/access/delete roles from platform
  and migrator secrets.
- **Residual:** provider/ERP token revocation semantics and emergency drills
  require live systems. Provider credentials currently support API keys, not
  OAuth flows. Any credential retained by an immutable policy needs explicit,
  audited break-glass revocation after decommissioning.

### Audit, export, retention, and deletion

- **Threats:** erasing/rewriting history, leaking an organization export,
  exporting a moving/incomplete data set, deleting data under legal hold, and
  assuming live deletion removes backups/provider copies.
- **Controls:** application audit writes are append-oriented and summaries are
  content-safe; reads/exports require permissions. Organization exports use a
  fixed snapshot, bounded batches, complete manifests, expiring signed parts,
  cancellation, and cleanup. Deletion is persisted and two-person approval
  gated; a legal hold wins and revokes unexecuted approval. Object/upload/
  export-copy removal is tenant-fenced and reconciled, content-bearing rows
  are erased, financial evidence is unlinked, the document shell is
  anonymized, and tombstones/count-only audit facts remain. A schema inventory
  test fails if a new typed document/run reference lacks an explicit policy.
- **Residual:** a database administrator can rewrite the trail; no WORM/offsite
  audit copy is configured. PITR copies age out rather than accepting
  record-level erasure, and hosted-provider/already-delivered ERP purge is not
  automated. These must be reflected in contracts and restore/deletion
  procedures.

### Availability, release, and operations

- **Threats:** lost jobs, duplicate execution, worker death, bad migration or
  artifact mismatch, blocking telemetry, provider outage, stale backup, and
  unowned alerts.
- **Controls:** durable claims/heartbeats/recovery/retry/dead letters,
  idempotent stage/export/outbox keys, bounded concurrency, graceful shutdown,
  batched OTLP export, immutable independently scanned image digests, a
  separate migrator, expand/contract schema policy, rollback manifests,
  durable provider health, alert definitions, and runbooks.
- **Residual:** GCP apply, alert-policy/notification wiring, restore evidence,
  release rollback, worker-pool behavior, load/soak/fault tests, and incident
  game day are not proved by the local suite.

### Internal support

- **Threat:** staff browses or changes tenant data without customer consent,
  approval, expiry, or trace.
- **State:** the customer-facing support intake exists, but a platform support
  identity/grant console does not. Normal production support must not rely on
  broad raw database access.
- **Required control:** explicit purpose-bound grant, approver, tenant/scope,
  start/expiry, read/write capability, prominent impersonation state, and
  append-only access audit; emergency access needs a separately reviewed break-
  glass path.

## Residual risk register

| ID | Risk | Severity | Production disposition |
| --- | --- | --- | --- |
| R1 | Controlled internal support-access model is absent | High | Build and security-review before routine production support |
| R2 | Live IdP/MFA/session/revocation configuration is unverified | High | Configure and exercise in staging before pilot |
| R3 | Container/native-parser isolation is not verified in the target runtime | High | Run sandbox/container/penetration gates on release digests |
| R4 | Backups, PITR, object recovery, and achieved RPO/RTO lack restore evidence | High | Complete isolated restore rehearsal before customer data |
| R5 | Audit trail has no configured tamper-evident offsite/WORM copy | Medium | Owner risk decision plus infrastructure control before regulated use |
| R6 | Hosted provider contract/residency/deletion/invoice behavior is unverified | High when enabled | Keep disabled per tenant until legal and technical certification |
| R7 | QuickBooks OAuth refresh and real idempotency/mapping behavior are unverified | High when enabled | Sandbox certification and operational runbook before delivery |
| R8 | NetSuite/Dynamics/SAP delivery is not implemented | High if advertised | Keep fail-closed and remove from production claims |
| R9 | Managed WAF/DDoS/body-rate controls are not applied | Medium | Add edge controls and load/abuse test in staging |
| R10 | Alerts/on-call/incident and load/fault behavior are not live-tested | High | Arm/fire every alert and complete game day/soak |

## Review log

| Date | Reviewer | Outcome |
| --- | --- | --- |
| _pending_ | owner + security reviewer | Required before pilot go/no-go |
