# Production evaluation corpus

`gold_sales_orders_v1.json` is a versioned, synthetic ten-case **manifest
fixture**. It describes digital/scanned cohorts, three currencies,
locale-sensitive dates, multiple pages and lines, missing optional values,
ambiguous catalog identifiers, duplicate purchase orders, large quantities,
and a required-field negative case.

It is not an executable production corpus. The repository contains no source
document bytes for these cases, and each `document_sha256` is an obvious
sequential placeholder (`00…0001` through `00…000a`), not the digest of an
existing file. The fixture can exercise manifest parsing and evaluation state
machinery; it cannot establish extraction accuracy, candidate quality, or a
production promotion result.

Before any production promotion:

1. create a rights-cleared, representative corpus in tenant-confidential
   storage;
2. calculate each immutable source document's real SHA-256 digest and bind the
   reconciled ground truth to that digest;
3. publish a new dataset version rather than relabeling these placeholders;
4. execute the current and candidate configurations server-side on the same
   version; and
5. retain the run IDs, configuration fingerprints, cohort results, false-auto-
   approval result, latency, cost, reviewer sign-off, and promotion decision.

Customer-derived examples and document bytes must never be committed here.
