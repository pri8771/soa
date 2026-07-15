# Product Requirements

## 1. Product definition

SOA is a multi-tenant intelligent document operations platform. It receives business documents, understands them through native parsing, OCR, and LLMs, validates the extracted information against deterministic rules and customer master data, routes exceptions to humans, and delivers approved data to downstream systems.

The first commercial solution is **purchase-order-to-sales-order automation**.

### North-star promise

> Turn incoming customer purchase orders into validated, ERP-ready sales orders—with every value traceable to its source.

## 2. Initial customers and users

### Buyer

Operations, customer service, order management, shared services, finance transformation, or IT leaders responsible for manual document-entry workflows.

### User roles

- **Organization Admin** — tenant configuration, users, security, billing, and organization-wide defaults.
- **Process Admin** — schemas, rules, catalogs, provider routing, workflow, and integrations.
- **Stream Admin** — local country/location overrides and operational configuration.
- **Reviewer** — reviews uncertain fields and exceptions.
- **Supervisor** — manages queues, quality, assignments, SLA, and approvals.
- **Integration Admin** — manages API credentials, mappings, delivery, failures, and replay.
- **Auditor** — read-only access to configuration versions, records, evidence, and audit events.
- **Platform Operator** — internal support and system operations with controlled, audited access.

## 3. Product hierarchy

```text
Organization
└── Workspace / Business Unit
    └── Process
        └── Stream
```

- **Organization:** contractual tenant and security boundary.
- **Workspace:** optional business unit, region, or legal-entity grouping.
- **Process:** reusable workflow such as sales orders or invoice processing.
- **Stream:** active operational bucket such as UK Sales Orders or Pittsburgh Invoices.

Streams inherit process configuration and may override language, locale, fields, thresholds, catalogs, rules, users, SLA, retention, provider routing, and delivery destination.

## 4. Required end-to-end workflow

1. Receive a document by upload, email, API, or batch import.
2. Validate the file, scan for malware, hash it, and preserve the immutable original.
3. Extract native text where possible; run image preprocessing and OCR where needed.
4. Enforce the published input contract; a later evaluated capability may
   classify and split multi-document packets.
5. Extract fields and tables into a versioned schema.
6. Normalize dates, numbers, currencies, identifiers, addresses, and units.
7. Match customer, sold-to, ship-to, materials, price lists, and other reference data.
8. Run deterministic field, cross-field, and external validations.
9. Calculate calibrated confidence and route only unresolved risk to review.
10. Let a reviewer correct fields with adjacent source evidence and match explanations.
11. Approve, reject, escalate, or request more information.
12. Map the canonical order to a downstream integration.
13. Deliver with idempotency, retries, and replay.
14. Record the complete processing and audit timeline.

## 5. Initial purchase-order schema

### Header

- Purchase order number and date
- Requested delivery date
- Customer name and account
- Buyer/contact
- Sold-to, bill-to, and ship-to names, addresses, and IDs
- Currency
- Payment terms
- Incoterms
- Shipping method and instructions
- Tax, freight, discounts, subtotal, and order total
- Document language and country
- Notes and attachments

### Line items

- Customer line number
- Customer item number
- Internal material number/match
- Description
- Quantity
- Unit of measure
- Unit price
- Discount
- Tax
- Requested delivery date
- Line total
- Customer-specific instructions

## 6. P0 functional requirements

### Initial release boundary

The first production slice accepts one sales order per input. It rejects an
unsupported mixed packet instead of silently processing only part of it.
Classification/splitting remains a platform requirement, but it is not a launch
claim until source-byte gold cohorts measure document-class and split-boundary
accuracy.

### Tenant and access

- Organizations, memberships, invitations, and roles
- Server-side tenant and stream authorization
- Session management and MFA-ready identity design
- API/service credentials scoped by tenant, stream, and capability

### Process and stream configuration

- Create, clone, archive, and version processes and streams
- Process-to-stream inheritance with explicit overrides
- Schema definition and publishing
- Thresholds, workflow, retention, and provider-routing settings
- Draft, test, publish, and rollback lifecycle

### Ingestion

- Manual and bulk upload
- Dedicated email intake
- REST API intake
- PDF, JPEG, PNG, and TIFF for P0
- Duplicate detection, corrupt-file quarantine, page/file limits, and malware scanning

### Processing

- Native PDF text extraction
- OCR fallback
- Single-sales-order input enforcement; evaluated packet classification and
  splitting after P0
- Schema-constrained extraction
- Header and line-item extraction
- Evidence regions and source snippets
- Normalization, catalog matching, validation, and confidence
- Retryable processing stages and visible failures

### Human review

- Queue views, assignment, priority, SLA age, and filtering
- Side-by-side document and field editor
- Source highlighting for each value
- Keyboard-first field review
- Line-item grid
- Catalog candidate selection
- Validation explanations
- Comments, escalation, approval, rejection, and reopen

### Reference data

- CSV/XLSX import
- Customer, ship-to, sold-to, material, customer-item mapping, UOM, and price catalogs
- Versioning and effective dates
- Exact and fuzzy matching with match explanations

### Output

- Canonical JSON
- CSV export
- Signed outbound webhook
- One production-quality ERP adapter after the canonical flow is stable
- Idempotency, retry, delivery history, and replay

### Reporting

- Volume and backlog
- Processing and review time
- Straight-through processing rate
- Field and document accuracy
- Correction rate
- Export success rate
- Provider cost and latency

## 7. Explicit non-goals for the first paid pilot

- General-purpose workflow automation unrelated to documents
- Every ERP connector
- On-premises deployment
- Customer-managed encryption keys
- Full SAML/SCIM enterprise identity suite
- Automatic model training from corrections
- Invoice, claims, remittance, and contract workflows
- Marketplace or partner ecosystem
- Fully autonomous submission of high-risk orders without configurable approval gates

## 8. Product differentiation requirements

The product must compete on more than extraction accuracy:

1. **Fast onboarding:** create a draft schema, rules, and test run from representative documents.
2. **Evidence-first operation:** every value shows where it came from and why it was accepted or flagged.
3. **Best-in-class review:** fast keyboard review, field-level routing, and inline master-data resolution.
4. **Configuration inheritance:** global processes with safe country/customer overrides.
5. **Simulation before release:** compare configuration versions against historical gold documents.
6. **Provider independence:** route by privacy, language, cost, latency, and quality.
7. **Business validation:** customer-specific catalogs and rules are first-class, not afterthoughts.
8. **Reproducibility:** every document can be tied to exact schema, prompt, model, rule, and catalog versions.

## 9. Success metrics

- Critical-field accuracy
- Line-item cell accuracy
- Straight-through processing rate
- False auto-approval rate
- Average review time per document
- Corrections per accepted document
- Time to onboard a new stream
- Export success rate and recovery time
- Processing cost per accepted document
- SLA attainment

## 10. P0 acceptance scenario

A new organization can create a purchase-order process and UK stream, upload a representative PO, extract header and line items, resolve a ship-to match, correct a low-confidence field using highlighted evidence, approve the order, export canonical JSON, and view an immutable timeline showing every automated and human action.
