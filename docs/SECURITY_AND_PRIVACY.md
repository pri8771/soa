# Security and privacy overview (SEC-013)

A plain-language description of how the platform protects customer
documents and data, written for a prospective or pilot customer's
security review. **Every claim here reflects what is actually built** —
where a control is delegated to the hosting platform or is on the
roadmap rather than shipped, this document says so explicitly. It
summarizes and links the authoritative internal references; it does not
supersede them.

Scope note: the platform is pre-certification. It does **not** claim SOC 2,
ISO 27001, or any third-party audit attestation, and this document is not
a Data Processing Agreement or privacy policy — those are produced as
part of contracting.

## Data flow

A purchase order moves through the platform in stages, each isolated to
one tenant (organization):

1. **Intake** — a document arrives by upload, public API, or email. It is
   validated by magic-byte signature against its declared type
   (type-confusion rejected), size/page limits, and a malware scan
   (ClamAV) before anything else touches it. A development no-op scanner
   is refused in production.
2. **Processing** — the document is rendered and its fields extracted by
   the configured provider (local by default — see [AI use](#ai-use)).
   Originals are stored immutably; derived artifacts are run-scoped.
3. **Review** — low-confidence or rule-flagged fields go to a human
   reviewer; corrections are persisted with evidence.
4. **Export** — the approved order is mapped to the customer's canonical
   / ERP / webhook target and delivered, with signed, retryable delivery.

Every stage records structured, correlation-tagged telemetry and, where
externally visible, an immutable audit event.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`PRODUCT.md`](PRODUCT.md).

## Tenant isolation

Customer data is separated at several layers, not by a UI filter alone:

- **Application** — every tenant-owned query runs through an
  organization-scoped repository; unscoped access is prohibited in normal
  code paths.
- **Database** — PostgreSQL **row-level security** with a
  `tenant_isolation` policy is FORCED on ~40 tenant tables as
  defense-in-depth beneath the repository boundary. (RLS is a
  PostgreSQL-only layer; the SQLite used in unit tests does not enforce
  it. A small set of cross-tenant tables — users, organizations, audit
  events, jobs — are deliberately excluded, each with a documented
  reason.)
- **Storage / jobs / caches** — object keys and job rows are tenant-scoped.
- **Tests** — a cross-tenant security suite proves one tenant cannot read
  or write another's rows or artifacts.

See [`THREAT_MODEL.md`](THREAT_MODEL.md) and `soa_db.tenant_guard`.

## Encryption

- **In transit** — the API emits strict security headers on every
  response: HSTS (`max-age` two years, `includeSubDomains`, production
  only), a locked-down Content-Security-Policy, `X-Frame-Options: DENY`,
  `X-Content-Type-Options`, and a restrictive `Permissions-Policy`. **TLS
  termination itself is handled by the hosting/ingress layer**, not the
  application.
- **At rest** — object storage supports server-side encryption as
  configuration: SSE-S3 (`AES256`) or KMS with an optional
  customer-supplied key (`aws:kms` + key id). **Database and backup
  at-rest encryption are provided by the managed hosting platform**
  (Cloud SQL for PostgreSQL — see [`DECISIONS.md`](DECISIONS.md)
  OPEN-001), not by application code. General customer-managed-key (BYOK)
  key management is not a product feature today.

See [`SANDBOX_PROFILE.md`](SANDBOX_PROFILE.md) and
[`CONTAINER_IMAGES.md`](CONTAINER_IMAGES.md).

## Access control

- **Authentication** — login is via OIDC/JWT (asymmetric signatures only;
  `none`/HMAC algorithms rejected to prevent algorithm confusion; issuer,
  audience, and expiry validated). A separate development identity mode
  exists and is **refused at startup in production**. Machine access uses
  API keys stored only as SHA-256 hashes, shown once, compared in
  constant time. Multi-factor authentication is delegated to the identity
  provider.
- **Authorization** — a central authorization service is the only
  sanctioned path to a tenant context: it checks the organization is
  operational, the membership active, the permission registered
  (fail-closed on anything unknown), and rechecks resource ownership.
  Roles and permissions are a validated registry.
- **Abuse controls** — request rate limiting is enforced at the API edge.

See `soa_api.auth.authorization` and `soa_api.domain.rbac`.

## Subprocessors

By default, **document processing is entirely local to the deployment** —
extraction runs on local models and content never leaves the environment.
The platform engages a third-party AI provider **only when the customer
opts in with their own API key** and the tenant's data policy explicitly
allows third-party processing; hosted providers declare this honestly and
are otherwise never selected (see [AI use](#ai-use)).

A formal, published subprocessor list and Data Processing Agreement are
produced during contracting; they do not yet exist as standing documents
in this repository. The hosting platform (Google Cloud / Cloud SQL /
Cloud Storage) is the primary infrastructure subprocessor for a hosted
deployment.

## Data retention and deletion

- **Retention** — a policy engine evaluates each document against a
  pinned (not live-editable) retention window, with an absolute
  **legal-hold** override that always wins, and a gated state machine
  (`RETAINED → ELIGIBLE → PENDING_APPROVAL → APPROVED → DELETED`).
- **Deletion** — erasure runs only after explicit approval: it removes
  the document's object-store artifacts and derived rows, reconciles that
  the objects are gone, and leaves an immutable **tombstone** plus a
  counts-only audit record for attributability. It is idempotent and
  retry-safe. **Scope caveat, stated honestly:** deletion covers the live
  database and object store; point-in-time **backups age out under their
  own retention** rather than being individually purged, and (because
  extraction is local) there is no third-party provider cache to purge.
- **Customer export** — a tenant can export a document's data across all
  documented data categories (including audit events) as a signed,
  expiring bundle.

See `soa_db.retention`, `soa_db.data_deletion`, `soa_db.data_export`, and
the [`deletion`](runbooks/deletion.md) runbook.

## AI use

- **Local-first.** Field extraction defaults to a local model; content
  stays inside the deployment and is not sent to any third party.
- **No autonomy.** No model request ever carries tools — a model reads a
  document, it never acts; a test asserts this structurally for each
  adapter.
- **Injection-safe by construction.** The request builder separates fixed
  platform instructions from document text (which travels only as JSON
  string values), strips control characters, refuses configuration that
  contains URLs, and bounds page/character counts. An adversarial
  prompt-injection corpus is run in CI.
- **Bring-your-own hosted key (optional).** Claude / Gemini / OpenAI can
  be enabled with the customer's own key; a missing key means the
  capability simply does not exist (fail-closed). Confidence is treated
  as one signal into human review, never the decision.
- **Honest limitation.** Hosted-AI keys are currently deployment-level,
  not yet isolated per tenant via the secret store (a tracked follow-on).

See [`LLM_PROVIDERS.md`](LLM_PROVIDERS.md) and [`AI_OCR.md`](AI_OCR.md).

## Secret handling

Secrets are never stored as raw values in the database — only opaque
`secretref://…` references are persisted, with values resolved from a
secret store behind a stable interface (AWS Secrets Manager in
production, forced by settings validation; GCP Secret Manager is the
planned adapter for the chosen hosting — see [`DECISIONS.md`](DECISIONS.md)
OPEN-001). Rotation issues a new reference rather than mutating one, so an
audit trail can name exactly which credential version was in use.

A required CI test suite sweeps logs, telemetry spans, metrics, and API
responses for leaked secrets or document content, and audit-event
summaries pass through key-based redaction.

See `soa_config.secrets` and `tests/security/test_sensitive_logging.py`.

## Vulnerability management and disclosure

- **Supply chain** — CI runs dependency auditing (Python `pip-audit`,
  JavaScript `pnpm audit`), full-history secret scanning (`gitleaks`),
  container image scanning (Trivy, images run non-root), and emits
  CycloneDX SBOMs. A code-level policy gate — not a scanner's exit code —
  blocks critical/high/unscored findings; exceptions are time-limited and
  expire loudly.
- **Disclosure** — suspected vulnerabilities should be reported privately
  to the security contact established in the pilot agreement; the
  credential-exposure and security-communication runbooks drive the
  response.

See [`SUPPLY_CHAIN_SECURITY.md`](SUPPLY_CHAIN_SECURITY.md).

## Incident response

Incidents follow a defined severity model (SEV1 data-at-risk /
platform-outage, SEV2 significant degradation, SEV3 minor/contained) with
a single incident lead and a single communications owner, a customer
status path, and message templates agreed in advance. Operational
response is driven by a set of runbooks (provider outage, quota
exhaustion, tenant-access incident, credential exposure, data deletion,
failed export, restore, object recovery, bad release). The process is
exercised by a game-day drill during the pilot.

See [`INCIDENT_COMMUNICATION.md`](INCIDENT_COMMUNICATION.md) and
[`runbooks/`](runbooks/README.md).

## Not yet available (roadmap, not claimed)

Stated plainly so a security reviewer is not misled:

- SAML SSO and SCIM provisioning (OIDC/JWT only today; enterprise
  identity is post-pilot — [`DECISIONS.md`](DECISIONS.md)).
- Multi-region / data-residency controls (single-region today).
- General customer-managed encryption keys / BYOK beyond object-store KMS
  configuration.
- SOC 2 / ISO 27001 or other formal certification.
- A published subprocessor list, DPA, and privacy policy (produced at
  contracting).
- Per-tenant isolation of hosted-AI provider keys.
- Immediate erasure of data from point-in-time backups (backups age out
  under their own retention).
